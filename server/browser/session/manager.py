"""SessionManager：浏览器会话管理器单例。

生命周期：第一次 create_session 时连接 CDP，shutdown 时关闭所有 page。
线程安全：所有方法必须在 async 上下文调用（FastAPI 路由天然 async）。

从 server/browser_session.py 迁移至 server/browser/session/ 包（Ticket 02）。
代码与原定义完全一致，仅调整 import 路径：
- TabSession：本包内 `from .state import TabSession`
- match_site_for_url / build_site_lessons_hint：`from ..site_lessons import ...`
- get_browser_config：`from ...config import get_browser_config`（包嵌套层级变化）
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING

from ..site_lessons import build_site_lessons_hint, match_site_for_url
from .state import TabSession

if TYPE_CHECKING:
    from patchright.async_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger("parcae.browser_session")

# dialog 超时兜底秒数：5 分钟内未被 handle_dialog 处理则自动 dismiss
# （兼顾 LLM 推理时间：agent 收到 blocked_by_dialog 反馈 → 决策 → 调 handle_dialog）
_DIALOG_AUTO_DISMISS_TIMEOUT = 300.0


class SessionManager:
    """浏览器会话管理器单例。

    生命周期：第一次 create_session 时连接 CDP，shutdown 时关闭所有 page。
    线程安全：所有方法必须在 async 上下文调用（FastAPI 路由天然 async）。
    """

    def __init__(self) -> None:
        self._browser: Browser | None = None  # Playwright Browser
        self._playwright: Playwright | None = None  # Playwright 进程
        self._sessions: dict[str, TabSession] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # 2026-09-03 防泄漏：是否已把 Chrome 下载落盘指到项目临时目录（browser 级，幂等）
        self._download_behavior_configured = False
        # 2026-10-02 归属权/可追溯：已关闭 session 的墓碑（session_id -> {reason, closed_at, owner, url}）。
        # 上限 200 条 FIFO；对已关 session 的后续操作据此报 SESSION_CLOSED(reason) 而非静默死。
        self._closed_log: dict[str, dict] = {}
        # 2026-10-02 归属权常量：单 caller 同时持有的 session 上限（防一个 agent 吃满全局额度）
        self._per_caller_limit = 10

    @property
    def is_connected(self) -> bool:
        return self._browser is not None

    async def _ensure_connected(self, cdp_url: str) -> None:
        """懒加载：第一次调用时连接 CDP。已连接则跳过。"""
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            from patchright.async_api import async_playwright
            pw = await async_playwright().start()
            browser = await asyncio.wait_for(
                pw.chromium.connect_over_cdp(cdp_url), timeout=10
            )
            self._browser = browser
            self._playwright = pw
            logger.info("BrowserSession: 已连接 CDP %s", cdp_url)

    async def _reconnect_if_needed(self, cdp_url: str) -> None:
        """检测连接是否已断开，断开则重连。"""
        if self._browser is None:
            await self._ensure_connected(cdp_url)
            return
        try:
            # 探活：browser.is_connected() 是同步方法
            if not self._browser.is_connected():
                logger.warning("BrowserSession: CDP 连接已断开，尝试重连")
                await self._cleanup_dead_browser()
                await self._ensure_connected(cdp_url)
        except Exception as e:
            logger.warning("BrowserSession: 探活失败 %s，尝试重连", e)
            await self._cleanup_dead_browser()
            await self._ensure_connected(cdp_url)

    async def _new_page_with_retry(self, context: BrowserContext, max_retries: int = 3) -> Page:
        """创建新 page，带重试机制处理 CDP "Failed to open a new tab" 竞态。

        根因：page.close() 后立即 context.new_page() 时，Chrome CDP 内部
        tab 管理可能未完成清理，导致 Target.createTarget 失败。
        重试间隔 0.5s × attempt，通常第二次即可成功。
        """
        last_err = None
        for attempt in range(max_retries):
            try:
                return await context.new_page()
            except Exception as e:
                last_err = e
                if "Failed to open a new tab" in str(e) and attempt < max_retries - 1:
                    logger.debug(
                        "BrowserSession: new_page 失败（attempt=%d），0.5s 后重试: %s",
                        attempt + 1, e,
                    )
                    await asyncio.sleep(0.5)
                    continue
                raise
        raise last_err  # type: ignore[misc]

    async def _cleanup_dead_browser(self) -> None:
        """清理已断开的 browser 句柄和所有 session。"""
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._playwright = None
        # 所有 session 的 page 已失效，清空表
        if self._sessions:
            logger.info("BrowserSession: 清理 %d 个失效 session", len(self._sessions))
        self._sessions.clear()
        self._download_behavior_configured = False

    @staticmethod
    def _get_download_dir() -> str:
        """下载沙箱目录：<项目根>/temp/browser_downloads（避免污染用户 Downloads）。"""
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[3]  # server/browser/session/ -> 仓库根
        return str(repo_root / "temp" / "browser_downloads")

    async def _configure_download_behavior(self, page: Page | None = None) -> None:
        """把 CDP 浏览器的下载落盘指到项目临时目录（每次 create_session 强制配置）。

        2026-09-03 防泄漏根因：connect_over_cdp 连真实 Chrome 后若不设置下载行为，
        Chrome 一切下载默认落盘浏览器默认下载目录（用户 Downloads）——e2e download
        测试每轮泄漏 1 个 test_download*.txt 到用户下载夹，save_as 只是另存一份、
        拦不住 Chrome 先落盘。cancel 路径虽能拦住，但 save_as 路径拦不住。

        注意：不能加"已配置就跳过"的幂等短路——实测 Playwright 的 Download.save_as
        在 CDP 模式下会自己重设 Browser.setDownloadBehavior 的 downloadPath，首次
        配置只对第一次下载生效，之后下载又回落到默认目录。故每次 create_session
        都强制重发一次（单次 CDP send 开销可忽略）。
        """
        try:
            browser = self._browser
            ctx = browser.contexts[0] if browser is not None and browser.contexts else None
            if ctx is None:
                return
            target = page if page is not None and not page.is_closed() else None
            if target is None:
                for p in ctx.pages:
                    if not p.is_closed():
                        target = p
                        break
            if target is None:
                return  # 暂无可发 CDP 的 page，留待下次 create_session
            dl_dir = self._get_download_dir()
            os.makedirs(dl_dir, exist_ok=True)
            cdp = await ctx.new_cdp_session(target)
            await cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": dl_dir,
                "eventsEnabled": True,
            })
            self._download_behavior_configured = True
            logger.debug("BrowserSession: 下载落盘已指向沙箱目录 %s", dl_dir)
        except Exception as e:
            logger.warning("BrowserSession: 配置下载行为失败（不影响使用，仅可能泄漏到默认目录）%s", e)

    async def create_session(
        self,
        url_pattern: str | None = None,
        url: str | None = None,
        cdp_url: str | None = None,
        owner: str = "anonymous",
        force: bool = False,
    ) -> dict:
        """创建或复用一个 TabSession。

        优先级：
        1. url_pattern：找现有 page（url 包含 pattern），命中则复用。
           2026-10-02 归属权：若命中的 page 已被其他 agent 持有（owner 非匿名且 ≠ 本次
           caller 且非 gui），须显式 force=True 才放行，否则抛 TAB_OWNED（含 owner 信息）。
        2. url：新建 page 并 goto url
        3. 都不传：新建专用空白 tab（2026-09-03 起不复用 pages[0]，避免
           session 意外绑定用户真实 tab/扩展窗口页）

        ★ force 的真实语义 = **加入共享**，不是「夺走」（2026-11-02 正名，行为未变）：
        - 目标 tab 与对方的 session 都原样保留，对方不会被踢、不会收到通知；
        - 两个 session 从此指向同一个物理 page：DOM / 登录态 / 表单状态只有一份，
          点击与输入互相可见（并发写由 page_guard 页级锁 + STALE_NODE 兜住）；
        - 关闭自己的 session 只解绑，不会关掉这个 tab（page_refs 引用计数）。
        参数名 `force` 是 2026-10-02 的历史叫法，读作 join 即可。

        ★ 2026-10-23 导航节流：分支 2（新建 tab 并 goto）算「初始连接」，受同域
        30s/域 限制（server/browser/nav_guard.py），撞限流抛 NavRateLimited
        （ValueError 子类）。**同一网站优先用 url_pattern 复用已有 tab**——
        既省一次冷启动，也不占节流额度。返回体的 reuse_hint 会提示可复用的同域 tab。

        2026-10-02 归属权/额度（多 agent 不打架）：
        - owner 记入 TabSession.owner，后续 close 校验归属
        - 单 caller 持有数超 _per_caller_limit → 抛 PER_CALLER_LIMIT（gui 豁免）
        - 全局 session 数达 session_max_count → 先淘汰 anonymous（idle 最久优先）；
          仍满则抛 SESSION_LIMIT_REACHED（gui 同样受限，错误信息说明现状）

        Returns: {session_id, tab_id, url, title, created}
        Raises: ValueError（TAB_OWNED / PER_CALLER_LIMIT / SESSION_LIMIT_REACHED）
        """
        # ── 额度检查（在连接 CDP 之前，失败早退）─────────────────────────
        from ...config import get_browser_config as _get_cfg
        _cfg = _get_cfg()
        max_count = int(_cfg.get("session_max_count", 32))

        # per-caller 上限：gui 豁免（人在开 tab 自己有数）
        if owner != "gui":
            mine = [s for s in self._sessions.values() if s.owner == owner]
            if len(mine) >= self._per_caller_limit:
                raise ValueError(
                    f"PER_CALLER_LIMIT: 调用方 {owner} 已持有 {len(mine)} 个 session "
                    f"(上限 {self._per_caller_limit})。请先 browser_session_close 释放不再用的，"
                    f"或带 force=false 语义自行管理额度。"
                )

        # 全局上限：先淘汰 anonymous（idle 最久优先），仍满则拒绝
        if len(self._sessions) >= max_count:
            anon = sorted(
                (s for s in self._sessions.values() if s.owner == "anonymous"),
                key=lambda s: s.last_used_at,
            )
            excess = len(self._sessions) - max_count + 1
            for s in anon[:excess]:
                logger.info(
                    "BrowserSession: session %s (owner=anonymous) LRU 淘汰为新建让位", s.session_id
                )
                await self.close_session(s.session_id, closed_reason="lru_evicted_anon")
            if len(self._sessions) >= max_count:
                owners: dict[str, int] = {}
                for s in self._sessions.values():
                    owners[s.owner] = owners.get(s.owner, 0) + 1
                raise ValueError(
                    f"SESSION_LIMIT_REACHED: 全局 session 已满 {len(self._sessions)}/{max_count}，"
                    f"且无 anonymous 可淘汰。归属分布 {owners}。"
                    f"请协调其他调用方释放，或调大 [browser] session_max_count。"
                )

        if cdp_url is None:
            cdp_url = f'http://127.0.0.1:{_cfg["debug_port"]}'

        await self._reconnect_if_needed(cdp_url)
        # _reconnect_if_needed 已确保 _browser 非 None
        browser = self._browser
        assert browser is not None
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            # 极端情况：browser 已连接但还没有 context
            context = await browser.new_context()

        page = None
        # 1) url_pattern 匹配（跳过已关闭的 page）
        if url_pattern:
            for p in context.pages:
                if not p.is_closed() and url_pattern in p.url:
                    page = p
                    break
            # 2026-10-02 抢绑校验：命中的 page 若已被其他 agent 有主持有，需 force
            if page is not None and owner != "gui":
                holder = next(
                    (s for s in self._sessions.values() if s.page is page), None
                )
                if (
                    holder is not None
                    and holder.owner not in ("anonymous", owner)
                    and not force
                ):
                    raise ValueError(
                        f"TAB_OWNED: url_pattern 命中的 tab 正被 owner={holder.owner} 使用 "
                        f"(session {holder.session_id}, url={holder.url})。"
                        f"要与它共享这个 tab 请带 force=true —— 该参数语义是「加入共享」"
                        f"而不是「夺走」：对方 session 保留、不会收到通知，你们会同时操作"
                        f"同一份 DOM，改动互相可见。不想共享就改用 url= 新开一个 tab。"
                    )
        # 2) url 新建
        if page is None and url:
            # 2026-10-23 同域导航节流：新开 tab 的首次 goto 也算"初始连接"，
            # 与 reload 共用 30s/域 的窗口。撞限流抛 NavRateLimited（ValueError 子类），
            # 端点转 429 并给出 retry_after。
            from ..nav_guard import enforce_nav

            # 归因：把 owner 记进节流状态，被拒的其他 agent 才知道"额度是谁占的"。
            enforce_nav(url, "goto", caller=owner)
            page = await self._new_page_with_retry(context)
            await page.goto(url, wait_until="domcontentloaded")
        # 3) 都不传：一律新建专用空白 tab。
        # 禁止改为复用已有 page（如 contexts[0].pages[0]）：那会抓到用户真实 tab 或
        # 扩展窗口页，配合 browser_session_close 的 close_page=True 默认值，可能把
        # 用户最后一个真实窗口关掉，导致整个调试浏览器退出、9222 消失。
        if page is None:
            page = await self._new_page_with_retry(context)

        # assert 仅为类型窄化——上面三个分支均已赋值非 None page，
        # pyright 需要这句才能放行后续的 page 调用。
        assert page is not None

        # 拿 CDP target id（page 的内部 _impl 对象上有 target_id）
        tab_id = ""
        tab_id_resolved = False
        try:
            # Playwright 内部 API：page._impl._guid 或 _target_info
            # 更稳定的做法：通过 CDP HTTP /json/list 找 url 匹配的 target id
            # T07：async 函数中同步 urllib 换成 httpx.AsyncClient，避免阻塞事件循环
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=3.0) as _client:
                _resp = await _client.get(cdp_url + "/json/list")
                targets = _resp.json()
            page_url = page.url
            for t in targets:
                if t.get("type") == "page" and t.get("url") == page_url:
                    tab_id = t.get("id", "")
                    tab_id_resolved = bool(tab_id)
                    break
        except Exception:
            # 反查失败时保持 tab_id_resolved=False，让调用方知道 tab_id 不可用于跨调用定位
            pass

        # 2026-09-03 防泄漏：把浏览器下载落盘指到项目临时目录（browser 级幂等，page 已就绪可发 CDP）
        await self._configure_download_behavior(page)

        session_id = uuid.uuid4().hex
        try:
            title = await page.title()
        except Exception:
            title = ""
        # P2-1: 按 url 域名自动匹配站点经验库
        site_lessons: list[dict] = []
        if url:
            try:
                site_lessons = match_site_for_url(url)
                if site_lessons:
                    logger.info(
                        "BrowserSession: session %s 加载 %d 条站点经验（url=%s）",
                        session_id, len(site_lessons), url,
                    )
            except Exception as e:
                logger.debug("BrowserSession: site lessons 匹配失败 %s", e)
        session = TabSession(
            session_id=session_id,
            tab_id=tab_id,
            page=page,
            url=page.url,
            title=title,
            site_lessons=site_lessons,
            owner=owner,
        )
        # 注册控制台日志收集（持久 session 才能跨调用增量返回）
        def _on_console(msg):
            try:
                session.console_logs.append({
                    "ts": time.time(),
                    "level": msg.type,
                    "text": msg.text,
                    "url": msg.location.get("url", "") if hasattr(msg, "location") else "",
                    "line": msg.location.get("lineNumber", 0) if hasattr(msg, "location") else 0,
                    "column": msg.location.get("columnNumber", 0) if hasattr(msg, "location") else 0,
                })
                # P1-2 修复：环形缓冲，溢出时丢弃最旧的并累计 dropped_count
                max_len = session.console_logs_max_len
                if len(session.console_logs) > max_len:
                    overflow = len(session.console_logs) - max_len
                    session.console_logs = session.console_logs[overflow:]
                    session.console_logs_dropped_count += overflow
            except Exception:
                pass
        page.on("console", _on_console)

        def _on_pageerror(err):
            try:
                session.console_logs.append({
                    "ts": time.time(),
                    "level": "error",
                    "text": err.message,
                    "url": "",
                    "line": 0,
                    "column": 0,
                })
                max_len = session.console_logs_max_len
                if len(session.console_logs) > max_len:
                    overflow = len(session.console_logs) - max_len
                    session.console_logs = session.console_logs[overflow:]
                    session.console_logs_dropped_count += overflow
            except Exception:
                pass
        page.on("pageerror", _on_pageerror)

        # 2026-09-30 patchright 迁移说明：
        # - 主 world 不发 Runtime.enable，page.on("console") 收不到 consoleAPICalled
        #   （原理性，非 bug）——本监听器保留作为未来版本恢复时的前向兼容。
        # - CDP Log domain 兜底已被实测证伪（Chrome 140：仅 Log.enable 时
        #   console-api/network 源 entryAdded 均零事件，见 temp/browser_mgmt_audit_20260930/
        #   test_cdp_events.py 判定实验）——console 捕获改为**按需** Runtime.enable，
        #   见 enable_console_capture()，由 console_logs 端点显式触发。

        # 监听 page close，自动清理 session
        # pyright stub 将 "close" 事件 handler 标注为 (Page) -> Awaitable[None] | None，
        # 用 *args 兼容（运行时无论传不传参都不会 TypeError）。
        def _on_close(*_args: object) -> None:
            # 2026-11-01：page 真被关掉（外部/最后一个 holder 关闭）时同步解绑，
            # 避免引用表里留下指向已关闭页面的死条目。
            from ..page_refs import get_page_refs
            get_page_refs().detach(page, session_id)
            self._sessions.pop(session_id, None)
            logger.debug("BrowserSession: page 关闭，session %s 已清理", session_id)
        page.on("close", _on_close)

        # 持久 dialog/popup/filechooser 监听器：
        # expect_* context manager 跨 HTTP 请求时序不可靠（agent 顺序调用模式下
        # wait_for 返回后才会调用 click，dialog 此时还没产生），所以改为持久监听器
        # + 队列模式。wait_for 优先消费队列，再等待新事件。
        #
        # dialog 生命周期：_on_dialog 不自动 dismiss——存引用 + 启动 5 分钟超时兜底 task。
        # 在超时前 handle_dialog 可调 dlg.accept(prompt_text)/dlg.dismiss()；
        # 超时后兜底 task 自动 dismiss 并清空 last_dialog_obj，避免 dialog 永久阻塞 page。
        # 超时秒数 DIALOG_AUTO_DISMISS_TIMEOUT = 300（5 分钟，兼顾 LLM 推理时间）。
        async def _on_dialog(dlg):
            # 必须是 async handler：Playwright Python async API 下 dlg.dismiss()
            # 返回 coroutine，sync 调用不会真正 dismiss，dialog 会一直阻塞 click 等动作
            # 直到超时。
            try:
                session._append_pending("pending_dialogs", {
                    "type": dlg.type,
                    "message": dlg.message,
                    "page_url": page.url,
                    "ts": time.time(),
                    "auto_dismissed": False,
                })
                session.last_dialog_obj = dlg
                session.dialog_auto_dismissed = False
                session.dialog_event.set()
            except Exception:
                pass
            # 启动超时兜底 task：5 分钟内未被 handle_dialog 处理则自动 dismiss
            # （不放在 finally 块：finally 会立即执行，我们要的是延迟 dismiss）
            task = asyncio.ensure_future(
                _auto_dismiss_dialog_after(dlg, session, _DIALOG_AUTO_DISMISS_TIMEOUT)
            )
            session._dialog_timeout_tasks.add(task)
            task.add_done_callback(session._dialog_timeout_tasks.discard)
        page.on("dialog", _on_dialog)

        async def _auto_dismiss_dialog_after(dlg, sess: TabSession, timeout: float) -> None:
            """超时兜底：timeout 秒内未被 handle_dialog 处理则自动 dismiss。

            判断依据：sess.last_dialog_obj 仍是传入的 dlg（未被 handle_dialog 处理/清空）。
            """
            try:
                await asyncio.sleep(timeout)
                # 检查 last_dialog_obj 是否仍是这个 dlg（未被 handle_dialog 处理）
                # 用 id 比较，避免 Dialog 对象 __eq__ 行为不确定
                if sess.last_dialog_obj is dlg:
                    try:
                        await dlg.dismiss()
                    except Exception:
                        pass
                    sess.last_dialog_obj = None
                    sess.dialog_auto_dismissed = True
                    # 标记队列中对应事件为 auto_dismissed
                    for ev in reversed(sess.pending_dialogs):
                        if not ev.get("auto_dismissed"):
                            ev["auto_dismissed"] = True
                            break
                    logger.debug(
                        "BrowserSession: dialog 超时兜底自动 dismiss（%ss）", timeout
                    )
            except asyncio.CancelledError:
                # session close 时取消，正常行为
                pass
            except Exception as e:
                logger.debug("BrowserSession: _auto_dismiss_dialog_after 异常 %s", e)

        def _on_popup(new_page):
            try:
                # 2026-09-03 归属修复：监听器从 context 级（page.context.on("page")）
                # 改为 page 级——只收本 session 页面 spawn 的 popup（window.open /
                # target=_blank），不再串扰其他 session 新建页污染 pending_popups；
                # 同时持有 popup Page 引用，供 close_session 连坐关闭（孤儿 tab 修复）。
                session.popup_pages.add(new_page)
                new_page.once("close", lambda: session.popup_pages.discard(new_page))
                # 异步收集 popup 信息（new_page.title() 是 async）
                async def _collect():
                    try:
                        await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                        title = await new_page.title()
                    except Exception:
                        title = ""
                    session._append_pending("pending_popups", {
                        "url": new_page.url,
                        "title": title,
                        "ts": time.time(),
                    })
                    session.popup_event.set()
                # strong reference 防 GC：asyncio.ensure_future 创建的 task 若不持有引用，
                # 会被事件循环的弱引用机制回收（Python 文档明确警告），导致 _collect 永不执行。
                task = asyncio.ensure_future(_collect())
                session._popup_tasks.add(task)
                task.add_done_callback(session._popup_tasks.discard)
            except Exception:
                pass
        page.on("popup", _on_popup)

        def _on_filechooser(fc):
            try:
                session._append_pending("pending_filechoosers", {
                    "multiple": fc.is_multiple(),
                    "page_url": page.url,
                    "ts": time.time(),
                })
                session.last_filechooser_obj = fc
                session.filechooser_event.set()
            except Exception:
                pass
        page.on("filechooser", _on_filechooser)

        def _on_download(dl):
            try:
                session._append_pending("pending_downloads", {
                    "url": dl.url,
                    "suggested_filename": dl.suggested_filename,
                    "page_url": page.url,
                    "ts": time.time(),
                })
                session.last_download_obj = dl
                session.download_event.set()
            except Exception as e:
                logger.warning("BrowserSession: _on_download 异常 %s", e)
        page.on("download", _on_download)

        self._sessions[session_id] = session
        # 2026-11-01 页级引用表（server/browser/page_refs.py）：url_pattern 复用 /
        # force=true 加入共享会让多个 session 指向同一个 page。记下来，close 时才知道
        # "关掉 page 会不会把别人一起带走"（实测 E5 就是这么发生的）。
        from ..page_refs import get_page_refs, page_label

        _page_ref_count = get_page_refs().attach(page, session_id)
        if _page_ref_count > 1:
            logger.warning(
                "BrowserSession: session %s 与另外 %d 个 session 共享同一 page %s "
                "（DOM/登录态/表单状态互通；关闭时不会连坐关页）",
                session_id, _page_ref_count - 1, page_label(page),
            )
        # 2026-11-01 来源登记（server/browser/tab_origins.py）：主动认领，别等下一次
        # 观测。否则"建会话到关闭之间没人查过 tabs"的 tab 在表里不存在，关闭后会被
        # 当成外来脚本页，冷路径于是拒绝操作后端自己开的页面（实测复现）。
        from ..tab_origins import get_tab_origins

        get_tab_origins().claim(
            tab_id or getattr(page, "_la_tab_id", "") or "",
            session_id, owner,
            url=page.url, title=title,
        )
        # 启动清理任务（如未启动）
        self._ensure_cleanup_task()
        # 2026-10-23：复用提示。若同域还有其他已打开的 tab，把「下次直接复用」写进响应——
        # agent 不读文档也会看到，比写在规范里更有效。
        reuse_hint: str | None = None
        try:
            from urllib.parse import urlparse as _urlparse

            from ..nav_guard import registrable_domain as _reg_domain

            _host = _reg_domain(_urlparse(page.url).hostname or "")
            if _host:
                _others: list[str] = []
                for _p in context.pages:
                    if _p is page or _p.is_closed():
                        continue
                    if _reg_domain(_urlparse(_p.url or "").hostname or "") == _host:
                        _others.append(_p.url)
                if _others:
                    _sample = _others[0]
                    if len(_sample) > 100:
                        _sample = _sample[:100] + "…"
                    reuse_hint = (
                        f"该域名下已有 {len(_others)} 个打开的 tab（如 {_sample}）。"
                        f"下次直接 browser_session_create(url_pattern='{_host}') 复用已有 tab，"
                        f"不要新开：省一次冷启动，也不占用同域导航节流额度（30s/域）。"
                    )
        except Exception as e:
            logger.debug("BrowserSession: reuse hint 计算失败 %s", e)

        # 2026-11-02 正名：force=true 得到的是「共享页」而不是「独占页」。数字字段
        # （page_ref_count/page_shared_with）容易被忽略，给一句人能读懂的话。
        _shared_with = sorted(get_page_refs().others(page, session_id)) if _page_ref_count > 1 else []
        _share_note = (
            f"你正在与另外 {_page_ref_count - 1} 个 session 共享这个 tab"
            f"（{', '.join(s[:8] for s in _shared_with)}）：DOM / 登录态 / 表单状态只有一份，"
            f"双方的点击与输入互相可见；关闭你的 session 只解绑，不会关掉这个 tab。"
            if _page_ref_count > 1 else None
        )
        return {
            "session_id": session_id,
            "tab_id": tab_id,
            "tab_id_resolved": tab_id_resolved,
            "url": page.url,
            "title": title,
            "created": True,
            "site_lessons": site_lessons,
            "site_lessons_hint": build_site_lessons_hint(site_lessons),
            "reuse_hint": reuse_hint,
            # 2026-11-01：本 page 上现在共挂着几个 session（>1 表示被别人共享）。
            # 调用方据此知道「我操作的可能不是只有我在看的页面」，也解释为什么
            # 关闭自己的 session 后别人的 tab 还开着（反之亦然）。
            "page_ref_count": _page_ref_count,
            "page_shared_with": _shared_with,
            "note": _share_note,
        }

    def get_session(self, session_id: str) -> TabSession | None:
        """同步获取 session（不 touch，不重连）。"""
        return self._sessions.get(session_id)

    async def enable_console_capture(self, session_id: str) -> bool:
        """按需开启该 session 的 console 捕获（2026-09-30 patchright 迁移配套）。

        背景：patchright 主 world 不发 Runtime.enable（否则 ownKeys 原型链检测向量
        触发，见 docs/browser-anti-detection.md 与 temp/browser_mgmt_audit_20260930/
        红蓝对抗实测），page.on("console") 因此收不到事件；CDP Log domain 兜底在
        Chrome 140 上被实测证伪（无 Runtime.enable 时 entryAdded 零事件）。

        方案：**按需** Runtime.enable——只在调用方显式读 console_logs 的 session 上
        开启（粘性，直到 session 关闭），检测暴露面从"每个 tab 永久"缩到
        "agent 显式要日志的那几个 tab"。幂等：重复调用直接返回 True。

        Returns: True=已开启（或本就开启）；False=session 不存在或挂载失败。
        """
        s = self._sessions.get(session_id)
        if s is None:
            return False
        if s.console_capture_enabled:
            return True
        try:
            cdp = await s.page.context.new_cdp_session(s.page)

            def _fmt_args(args) -> str:
                parts = []
                for a in args or []:
                    if not isinstance(a, dict):
                        continue
                    if "value" in a:
                        parts.append(str(a["value"]))
                    elif a.get("description"):
                        parts.append(a["description"])
                return " ".join(parts)

            def _append(level: str, text: str, url: str, line: int, column: int) -> None:
                try:
                    s.console_logs.append({
                        "ts": time.time(),
                        "level": level,
                        "text": text,
                        "url": url,
                        "line": line,
                        "column": column,
                    })
                    max_len = s.console_logs_max_len
                    if len(s.console_logs) > max_len:
                        overflow = len(s.console_logs) - max_len
                        s.console_logs = s.console_logs[overflow:]
                        s.console_logs_dropped_count += overflow
                except Exception:
                    pass

            def _on_console_api(params) -> None:
                try:
                    if not isinstance(params, dict):
                        return
                    frames = (params.get("stackTrace") or {}).get("callFrames") or []
                    top = frames[0] if frames else {}
                    _append(
                        params.get("type", "info"),
                        _fmt_args(params.get("args")),
                        top.get("url", ""),
                        top.get("lineNumber", 0) or 0,
                        top.get("columnNumber", 0) or 0,
                    )
                except Exception:
                    pass

            def _on_exception(params) -> None:
                try:
                    if not isinstance(params, dict):
                        return
                    d = params.get("exceptionDetails") or {}
                    exc = d.get("exception") or {}
                    text = exc.get("description") or d.get("text", "")
                    frames = (d.get("stackTrace") or {}).get("callFrames") or []
                    top = frames[0] if frames else {}
                    _append("error", text, top.get("url", ""),
                            top.get("lineNumber", 0) or 0, top.get("columnNumber", 0) or 0)
                except Exception:
                    pass

            cdp.on("Runtime.consoleAPICalled", _on_console_api)
            cdp.on("Runtime.exceptionThrown", _on_exception)
            await cdp.send("Runtime.enable")
            s.cdp_log_session = cdp  # 强引用防 GC；page 关闭时自动失效
            s.console_capture_enabled = True
            logger.info("BrowserSession: session %s 已按需开启 console 捕获（Runtime.enable）",
                        session_id)
            return True
        except Exception as e:
            logger.warning("BrowserSession: session %s console 捕获开启失败: %s",
                           session_id, e)
            return False

    async def get_page(self, session_id: str) -> Page | None:
        """获取 page 句柄并 touch。session 不存在返回 None。

        2026-10-02：page 被外部关闭（用户手动/外部脚本）时也写墓碑
        （reason=page_closed_external），这正是 2026-09-30 探针 tab 事故的形态——
        事后至少能答"它是什么时候、以什么方式消失的"。
        """
        s = self._sessions.get(session_id)
        if s is None:
            return None
        # 探活：page 是否已关闭
        if s.page.is_closed():
            from ..page_refs import get_page_refs
            get_page_refs().detach(s.page, session_id)
            self._sessions.pop(session_id, None)
            self._record_closed(s, "page_closed_external")
            return None
        s.touch()
        return s.page

    def list_sessions(self) -> list[dict]:
        """列出所有活跃 session。"""
        from ..page_refs import get_page_refs

        refs = get_page_refs()
        now = time.time()
        return [
            {
                "session_id": s.session_id,
                "tab_id": s.tab_id,
                "owner": s.owner,
                "url": s.url,
                "title": s.title,
                "created_at": s.created_at,
                "last_used_at": s.last_used_at,
                "idle_secs": int(now - s.last_used_at),
                "console_logs_buffered": len(s.console_logs),
                "site_lessons_count": len(s.site_lessons),
                "site_lessons_domains": [lesson.get("domain", "") for lesson in s.site_lessons],
                # 2026-11-01：同一个物理 page 上可能挂着多个 session（url_pattern
                # 复用 / force=true 加入共享）。>1 意味着 DOM 与登录态是共用的，
                # 操作会互相影响，任何一方都不该按独占来理解。
                "page_ref_count": refs.count(s.page),
                "page_shared_with": sorted(refs.others(s.page, s.session_id)),
            }
            for s in self._sessions.values()
        ]

    def iter_sessions(self):
        """迭代所有活跃的 TabSession 对象（供 browser_status 聚合 dialog 状态用）。

        返回 snapshot 引用（不复制），调用方只读取，不修改。
        """
        return list(self._sessions.values())

    async def get_context(self) -> BrowserContext | None:
        """获取 BrowserContext（contexts[0]），用于 context 级操作
        （set_http_credentials / grant_permissions 等）。

        未连接时返回 None（不抛异常，由调用方决定如何处理）。
        """
        if self._browser is None:
            # 尝试重连一次（可能后端启动后第一次调用）
            try:
                from ...config import get_browser_config
                cfg = get_browser_config()
                cdp_url = f'http://127.0.0.1:{cfg["debug_port"]}'
                await self._reconnect_if_needed(cdp_url)
            except Exception:
                return None
        if self._browser is None:
            return None
        contexts = self._browser.contexts
        if not contexts:
            return None
        return contexts[0]

    def _record_closed(
        self, s: TabSession, reason: str, extra: dict | None = None
    ) -> None:
        """写关闭墓碑（FIFO 上限 200），供后续操作报 SESSION_CLOSED 明确错误。

        `extra` 用于补充关闭时的现场（如 `page_kept_by`：因为还有这些 session
        挂着，page 被保留没关）。
        """
        self._closed_log[s.session_id] = {
            "reason": reason,
            "closed_at": time.time(),
            "owner": s.owner,
            "url": s.url,
            **(extra or {}),
        }
        if len(self._closed_log) > 200:
            for k in list(self._closed_log.keys())[: len(self._closed_log) - 200]:
                self._closed_log.pop(k, None)

    def get_closed_info(self, session_id: str) -> dict | None:
        """查询已关闭 session 的墓碑信息；未关过或太旧返回 None。"""
        return self._closed_log.get(session_id)

    async def close_session(
        self, session_id: str, close_page: bool = True, closed_reason: str = "manual"
    ) -> bool:
        """关闭 session。close_page=True（默认）时同时关闭浏览器 page。

        历史背景：曾因 close_page=True 触发 Chrome CDP "Failed to open a new tab" 异常
        而全链路改为 False，但经 2026-08-05 验证该异常已不存在（Playwright/Chrome 升级修复）。
        现恢复默认 True，确保 session 关闭时 page 也被清理，避免 Chrome tab 无限累积。

        2026-10-02：closed_reason 记入墓碑（manual:<caller> / idle_timeout / lru_evicted_anon），
        可通过 get_closed_info 追溯"谁/为什么关了我的 session"。
        """
        s = self._sessions.pop(session_id, None)
        if s is None:
            return False
        # 2026-11-01 引用计数（G3）：先算「除我之外还有谁挂着这个 page」，
        # 有人就不许关 page——否则一人收工把同 tab 的所有会话一起带走
        # （实测 E5：A close 之后 B 用自己的 session 也报"已失效"）。
        from ..page_refs import get_page_refs, page_label

        _refs = get_page_refs()
        _others = _refs.others(s.page, session_id)
        _refs.detach(s.page, session_id)
        _page_kept = False
        _record_extra: dict = {}
        if close_page and _others and not s.page.is_closed():
            _page_kept = True
            _record_extra["page_kept_by"] = sorted(_others)
            logger.info(
                "BrowserSession: session %s 关闭时保留 page %s —— 还有 %d 个 session "
                "挂在上面（%s）。避免连坐关闭其他人的 tab。",
                session_id, page_label(s.page), len(_others), sorted(_others),
            )
        self._record_closed(s, closed_reason, extra=_record_extra or None)
        # 取消未完成的 dialog 超时兜底 task，避免 asyncio task 泄漏
        for task in list(s._dialog_timeout_tasks):
            task.cancel()
        s._dialog_timeout_tasks.clear()
        # 2026-09-03 防泄漏兜底：关 page 前 cancel 未消费的 download 对象
        # （download 事件触发后若既未 save_as 也未 cancel，CDP 连真实 Chrome 会把
        # 下载落盘到浏览器默认下载目录——实测泄漏 test_download*.txt 到用户 Downloads）
        if s.last_download_obj is not None:
            try:
                await s.last_download_obj.cancel()
            except Exception:
                pass  # 下载已完成/已被保存，cancel 失败属正常
            s.last_download_obj = None
        if close_page and not _page_kept and not s.page.is_closed():
            try:
                await s.page.close()
            except Exception as e:
                logger.debug("BrowserSession: 关闭 page 失败 %s", e)
        # 2026-09-03 popup 归属修复：连坐关闭本 session 页面 spawn 的 popup
        # （window.open 的子页若不关会成为孤儿 tab，每轮 e2e 泄漏 4-5 个）
        # 2026-11-01：同样按引用计数——别的 session 若也复用了该 popup，不能关。
        if close_page:
            for popup_page in list(s.popup_pages):
                if _refs.others(popup_page, session_id):
                    logger.info(
                        "BrowserSession: popup %s 仍被其他 session 持有，保留不关",
                        page_label(popup_page),
                    )
                    continue
                try:
                    if not popup_page.is_closed():
                        await popup_page.close()
                except Exception as e:
                    logger.debug("BrowserSession: 关闭 popup page 失败 %s", e)
            s.popup_pages.clear()
        return True

    async def close_all(self) -> None:
        """关闭所有 session（shutdown 时调用），同时关闭对应的浏览器 page。"""
        for sid in list(self._sessions.keys()):
            await self.close_session(sid, close_page=True)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._playwright = None

    def _ensure_cleanup_task(self) -> None:
        """启动后台清理任务（每 60s 扫一次）。"""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        try:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        except RuntimeError:
            # 无事件循环（测试环境）时跳过
            pass

    async def _cleanup_loop(self) -> None:
        """后台循环：清理 idle 超时的 session。

        2026-10-02 租约化：有主 session 用 session_idle_timeout_secs（900s，owner
        每次操作即续租）；anonymous（老脚本/内部调用，没人会来续租）用更短的
        session_anon_idle_timeout_secs（300s）。LRU 淘汰只针对 anonymous——
        有主的满额由 create_session 报 SESSION_LIMIT_REACHED，不静默挤别人。
        """
        from ...config import get_browser_config
        while True:
            try:
                await asyncio.sleep(60)
                cfg = get_browser_config()
                # T7：只有一套取值，anonymous 有独立更短的租约（原 cleanup 内
                # 兜底默认 300 与 config 实际 900 不一致的问题一并消除）
                owned_timeout = int(cfg.get("session_idle_timeout_secs", 900))
                anon_timeout = int(cfg.get("session_anon_idle_timeout_secs", 300))
                max_count = int(cfg.get("session_max_count", 32))
                now = time.time()
                # 1) 清理 idle 超时（按归属选租约）
                expired = [
                    sid for sid, s in self._sessions.items()
                    if now - s.last_used_at > (
                        anon_timeout if s.owner == "anonymous" else owned_timeout
                    )
                ]
                for sid in expired:
                    await self.close_session(sid, closed_reason="idle_timeout")
                    logger.info("BrowserSession: session %s idle 超时已清理（page 已关闭）", sid)
                # 2) LRU 淘汰：只淘汰 anonymous（无主 session 没人会来续租）。
                #    有主的超额由 create_session 的 SESSION_LIMIT_REACHED 报错处理，
                #    这里只记 warning 提示归属分布，不静默挤别人的 session。
                if len(self._sessions) > max_count:
                    anon = sorted(
                        (s for s in self._sessions.values() if s.owner == "anonymous"),
                        key=lambda s: s.last_used_at,
                    )
                    excess = len(self._sessions) - max_count
                    for s in anon[:excess]:
                        logger.info(
                            "BrowserSession: session %s (owner=anonymous) LRU 淘汰", s.session_id
                        )
                        await self.close_session(s.session_id, closed_reason="lru_evicted_anon")
                    if len(self._sessions) > max_count:
                        owners: dict[str, int] = {}
                        for s in self._sessions.values():
                            owners[s.owner] = owners.get(s.owner, 0) + 1
                        logger.warning(
                            "BrowserSession: 全局 session 超上限 %d/%d 且无 anonymous 可淘汰，"
                            "归属分布 %s（不淘汰有主 session，新建将被拒绝）",
                            len(self._sessions), max_count, owners,
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("BrowserSession: cleanup loop 异常 %s", e)


# 单例
_manager: SessionManager | None = None
_manager_lock = asyncio.Lock()


async def get_session_manager() -> SessionManager:
    """获取 SessionManager 单例（async 安全）。"""
    global _manager
    if _manager is not None:
        return _manager
    async with _manager_lock:
        if _manager is None:
            _manager = SessionManager()
    return _manager


async def reset_session_manager() -> None:
    """shutdown 时调用，清理单例。"""
    global _manager
    if _manager is not None:
        await _manager.close_all()
        _manager = None

def get_closed_hint(session_id: str) -> str:
    """模块级便捷查询：session 为何消失（用单例墓碑；未初始化/无记录返回空串）。

    供各端点 not-found 分支拼接 debug_detail：把"不存在或已失效"变成
    "已于 12:34:56 被关闭：idle_timeout，owner=xxx"——2026-09-30 探针 tab
    被关事故的直接教训（当时连"什么时候没的"都答不出来）。
    """
    if _manager is None:
        return ""
    info = _manager._closed_log.get(session_id)
    if not info:
        return ""
    t = time.strftime("%H:%M:%S", time.localtime(info["closed_at"]))
    return f"（已于 {t} 被关闭：{info['reason']}，owner={info['owner']}，url={info['url']}）"
