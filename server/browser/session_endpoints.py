"""浏览器持久 session 端点：create / list / close。

从 server/browser.py 迁移至 server/browser/ 包（Ticket 03）。
代码与原定义完全一致，仅调整 import 路径：
- get_session_manager：`from .browser_session import` → `from .session.manager import`

迁移内容：
- Pydantic 模型：BrowserSessionCreateRequest/Response、BrowserSessionListResponse、
  BrowserSessionCloseRequest/Response
- 端点函数：browser_session_create / browser_session_list / browser_session_close

策略说明（Ticket 04 补迁）：
本文件的端点函数通过 @router.post/@router.get 装饰器注册到 routes.py 的 router。
routes.py 末尾的 `from . import session_endpoints` 触发本模块加载，执行装饰器，
把端点注册到 router。Ticket 06 删除 browser.py 后本文件成为唯一定义。
"""

import logging

from fastapi import HTTPException, Request

from lib.agent_identity import get_caller_id
from lib.schema import BaseSchema

from .nav_guard import NavRateLimited
from .routes import router
from .session.manager import get_session_manager

logger = logging.getLogger("parcae.browser_session")


def _describe_nav_context(mgr, host: str) -> str:
    """限流拒绝时的"现场说明"：当前几个会话、owner 分布、该域有没有现成 tab。

    2026-11-01：原本文案只说"还需等 N 秒"，多 agent 并发抢同域时无法判断
    该停手还是该去复用别人已开着的 tab。这段把判断依据直接摊开。
    任何异常都吞掉——补充说明失败不能让 429 变成 500。
    """
    try:
        sessions = mgr.list_sessions()
    except Exception as e:  # noqa: BLE001
        logger.debug("限制流描述：读取 session 列表失败 %s", e)
        return ""
    if not sessions:
        return "当前无活跃 session（额度可能来自本进程之外的直连 CDP）"

    owners: dict[str, int] = {}
    for s in sessions:
        who = str(s.get("owner") or "?")
        owners[who] = owners.get(who, 0) + 1
    dist = "、".join(f"{k}×{v}" for k, v in sorted(owners.items()))
    parts = [f"当前活跃 session {len(sessions)} 个（{dist}）"]

    if host:
        reuse = [s for s in sessions if host in str(s.get("url") or "")]
        if reuse:
            listed = "；".join(
                f"{str(s.get('session_id'))[:8]}…(owner={s.get('owner')}, "
                f"idle={s.get('idle_secs')}s, url={str(s.get('url') or '')[:70]})"
                for s in reuse[:3]
            )
            parts.append(
                f"该域已开着 {len(reuse)} 个 tab，带 url_pattern 复用它们不占额度：{listed}"
                f"（若某条正被别的 agent 使用，会 409 TAB_OWNED；那时带 force=true 是"
                f"「加入共享」，不是把它抢过来）"
            )
        else:
            parts.append(f"当前没有任何已开着的 {host} tab，想复用也无可复用目标")
    return "。".join(parts)

# ========== 请求/响应模型 ==========

class BrowserSessionCreateRequest(BaseSchema):
    """创建持久浏览器会话。

    持久 session 解决"每次 DOM 操作都重新 connect_over_cdp + 启动子进程"
    导致的 0.5–1.4 秒固定开销。服务端维持长连接 Playwright Page 句柄，
    后续 snapshot/action/wait_for/evaluate 调用传入 session_id 即可复用，
    热态延迟 <250ms。

    - url_pattern: 复用现有 page（url 包含 pattern）；若该 tab 已被其他 agent
      有主持有，须显式 force=true 才放行（2026-10-02 归属权，否则 409 TAB_OWNED）
    - url: 新建 page 并 goto url（优先级低于 url_pattern）
    - 都不传：新建专用空白 tab（不复用已有页面，避免误绑用户真实 tab/扩展窗口页；
      2026-09-03 修复，详见 session/manager.py create_session 注释）
    - force: 历史参数名，**语义是「加入共享」而不是「夺走」**（2026-11-02 正名，
      行为未变）。传 true 后你拿到的是对方正在用的同一个 tab，对方的 session 原样
      保留、不会被踢也不会收到通知；双方的 DOM/登录态/表单状态只有一份，操作互相
      可见。不传则 409 TAB_OWNED。

    返回 session_id（调用方持有）+ tab_id（CDP target id）+ url + title。
    租约（2026-10-02）：有主 session idle 超时 900s（操作即续租）；anonymous 300s。
    单 caller 上限 10 个、全局上限 session_max_count（默认 32），超限 409。
    """
    url_pattern: str | None = None
    url: str | None = None
    force: bool = False


class BrowserSessionCreateResponse(BaseSchema):
    success: bool
    session_id: str
    tab_id: str
    # 修复问题①：create_session 返回 dict 含 tab_id_resolved，但本模型原缺该字段，
    # 因 BaseSchema extra="forbid" 触发 ValidationError → 端点 500。补齐字段即可。
    tab_id_resolved: bool = False  # tab_id 是否经 CDP /json/list 反查成功（跨调用定位可靠性）
    url: str
    title: str
    created: bool
    elapsed_ms: int
    # P2-1: 自动匹配的站点经验（评估文档 P2 第 1 项）
    site_lessons: list[dict] = []  # [{domain, aliases, body}]
    site_lessons_hint: str | None = None  # 简短提示，None=无 lessons
    # 2026-10-23：复用提示（同域已有其他 tab 时给出「下次用 url_pattern 复用」的说明）
    reuse_hint: str | None = None
    # 2026-11-01 页级引用计数：本 page 上共挂着几个 session；>1 时列出其他人的
    # session_id。共享意味着 DOM/登录态/表单状态互通，操作会互相影响。
    page_ref_count: int = 1
    page_shared_with: list[str] = []
    # 2026-11-02 正名：page_ref_count > 1 时给一句人能读懂的话（force=true 得到的是
    # 共享页，不是独占页）。数字字段容易被忽略，句子才会被读到。
    note: str | None = None


class BrowserSessionListResponse(BaseSchema):
    success: bool
    sessions: list[dict]
    elapsed_ms: int


class BrowserSessionCloseRequest(BaseSchema):
    session_id: str
    close_page: bool = True  # True=同时关闭浏览器标签（默认，避免 tab 累积）
    force: bool = False  # True=显式抢占关闭别人的有主 session（2026-10-02 用户拍板语义）


class BrowserSessionCloseResponse(BaseSchema):
    success: bool
    closed: bool
    elapsed_ms: int
    # 2026-11-01 引用计数（G3）：close_page=True 但该 page 上还挂着其他 session 时，
    # 只解绑不关页——一人收工不该把邻居的 tab 一起带走（实测曾发生）。
    page_kept: bool = False
    page_kept_by: list[str] = []
    note: str | None = None


# ========== 端点函数（@router 装饰器注册到 routes.py 的 router） ==========

@router.post("/sessions", response_model=BrowserSessionCreateResponse, operation_id="browser_session_create")
async def browser_session_create(req: BrowserSessionCreateRequest, request: Request):
    """Create a persistent browser session.

    Returns a session_id to pass to snapshot/action/wait_for/evaluate for hot-path
    reuse (~250ms vs 0.5–1.4s cold start). The server holds a long-lived Playwright
    Page handle; idle sessions are cleaned up after `session_idle_timeout_secs`
    (default 900s = 15min, page also closed on cleanup).

    Use this at the start of a multi-step browser task, then pass session_id to
    subsequent calls. Fall back to no-session mode if the session expires.

    Tip — 创建 session 时会自动按 URL 匹配并注入 site_lessons（如有），无需手动查询。
    新网站经验可用 browser_write_lesson 写入 sites/<domain>.md，下次访问自动加载。

    ★ 复用优先（2026-10-23）：同一网站已经开着 tab 时，用 url_pattern 复用，
    ⛔ 不要 url= 新开。新开 tab 的首次 goto 算「初始连接」，受同域 30s/域 节流
    （超频返回 429 NAV_RATE_LIMITED + Retry-After）；复用已有 tab 既不占额度，
    也省掉一次冷启动。响应里的 reuse_hint 会告诉你当前有哪些可复用的同域 tab。

    ★ force 读作 join（2026-11-02 正名，行为未变）：该 tab 若正被别的 agent 使用，
    url_pattern 会 409 TAB_OWNED；带 force=true 不是「抢过来」，而是「加入共享」——
    对方的 session 保留、不会收到通知，你们同时操作同一份 DOM，改动互相可见。
    响应会回 page_ref_count / page_shared_with / note 说明这件事。

    Example — attach to an existing bilibili tab:
      browser_session_create(url_pattern="bilibili.com")
      （2026-10-02 归属权：该 tab 已被其他 agent 有主持有时需 force=true，否则 409 TAB_OWNED；
         force 的语义是加入共享，详见上一条）
    Example — open a fresh tab to a URL:
      browser_session_create(url="https://www.bilibili.com")
    """
    import time as _time
    start = _time.perf_counter()
    mgr = await get_session_manager()
    caller = get_caller_id(request.headers)
    try:
        info = await mgr.create_session(
            url_pattern=req.url_pattern, url=req.url, owner=caller, force=req.force
        )
        elapsed = int((_time.perf_counter() - start) * 1000)
        return BrowserSessionCreateResponse(
            success=True, elapsed_ms=elapsed, **info,
        )
    except NavRateLimited as e:
        # 2026-10-23 同域导航节流：新建 tab 的首次 goto = 初始连接，30s/域 一次。
        # 429 + Retry-After，detail 里给足「改用 url_pattern 复用已有 tab」的做法。
        # 2026-11-01 补现场：光说"等 22s"没法判断是自己太急还是别人刚刷过，
        # 所以补上「当前有几个会话、谁在占该域、有没有现成 tab 可复用」。
        elapsed = int((_time.perf_counter() - start) * 1000)
        ctx = _describe_nav_context(mgr, e.verdict.host)
        detail = f"{e} (retry_after_secs={e.retry_after_secs:.1f}, caller={caller}, elapsed_ms={elapsed})"
        if ctx:
            detail += f" | {ctx}"
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(max(1, int(round(e.retry_after_secs))))},
        ) from e
    except ValueError as e:
        # 归属权/额度冲突（TAB_OWNED / PER_CALLER_LIMIT / SESSION_LIMIT_REACHED）
        elapsed = int((_time.perf_counter() - start) * 1000)
        raise HTTPException(status_code=409, detail=f"{e} (elapsed_ms={elapsed})") from e
    except Exception as e:
        elapsed = int((_time.perf_counter() - start) * 1000)
        raise HTTPException(
            status_code=500,
            detail=f"创建会话失败: {e} (elapsed_ms={elapsed})",
        ) from e


@router.get("/sessions", response_model=BrowserSessionListResponse, operation_id="browser_session_list")
async def browser_session_list():
    """List all active browser sessions.

    Each entry includes session_id, tab_id, url, title, last_used_at, idle_secs,
    and console_logs_buffered (number of logs captured since session start).
    """
    import time as _time
    start = _time.perf_counter()
    mgr = await get_session_manager()
    sessions = mgr.list_sessions()
    elapsed = int((_time.perf_counter() - start) * 1000)
    return BrowserSessionListResponse(success=True, sessions=sessions, elapsed_ms=elapsed)


@router.post("/sessions/close", response_model=BrowserSessionCloseResponse, operation_id="browser_session_close")
async def browser_session_close(req: BrowserSessionCloseRequest, request: Request):
    """Close a browser session.

    - close_page=True (default): also close the browser tab, avoiding tab
      accumulation in Chrome. Use this when done with the page.
    - close_page=False: only drop the session handle; the browser tab stays
      open (use this when you want to detach but keep the page alive for reuse).

    ★ 2026-10-23：任务可能还要回来就**别关**。session 有 idle 租约（有主 900s /
    无主 300s）会自动回收，主动关掉再开会重付一次冷启动，并再吃一次同域导航节流
    （30s/域）。关 tab = 下次必须 reload/goto，这两件事都不划算——先想清楚是
    「这次任务真的结束了」还是「只是中间歇一下」。
    确实结束时才调本工具（并让 close_page=True 收掉 tab，避免 tab 累积）。

    2026-10-02 归属权：非 owner 关别人的 session → 409 TAB_OWNED
    （gui / anonymous 例外：gui 是人永远放行；anonymous 是无主老脚本无人认领）。
    关闭原因记入墓碑，可用 GET /sessions 或后端日志追溯"谁关的"。
    """
    import time as _time
    start = _time.perf_counter()
    mgr = await get_session_manager()
    caller = get_caller_id(request.headers)
    holder = mgr.get_session(req.session_id)
    if (
        holder is not None
        and not req.force
        and caller != "gui"
        and holder.owner not in ("anonymous", caller)
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"TAB_OWNED: session {req.session_id} 的 owner 是 {holder.owner}，"
                f"调用方 {caller} 无权关闭。请先与 owner 协调，"
                f"或由 owner 自己释放（gui 用户不受此限）；"
                f"确要抢占时传 force=true。"
            ),
        )
    if req.force and holder is not None and caller != "gui" and holder.owner != caller:
        logger.warning(
            "BrowserSession: %s force 关闭了 %s 的 session %s（抢占已记录）",
            caller, holder.owner, req.session_id,
        )
    # 2026-11-01 引用计数：先看这个 page 上还有没有别人，关完再如实回报。
    from .page_refs import get_page_refs

    _shared_with: list[str] = []
    if holder is not None:
        _shared_with = sorted(get_page_refs().others(holder.page, req.session_id))
    closed = await mgr.close_session(
        req.session_id,
        close_page=req.close_page,
        closed_reason=f"manual:{caller}" + ("(force)" if req.force else ""),
    )
    elapsed = int((_time.perf_counter() - start) * 1000)
    _page_kept = bool(closed and req.close_page and _shared_with)
    return BrowserSessionCloseResponse(
        success=True,
        closed=closed,
        elapsed_ms=elapsed,
        page_kept=_page_kept,
        page_kept_by=_shared_with if _page_kept else [],
        note=(
            f"session 已关闭，但 page 保留未关：还有 {len(_shared_with)} 个 session "
            f"挂在同一个 tab 上（{', '.join(s[:8] for s in _shared_with)}）。"
            f"这是防连坐的默认行为——别人的会话不会被你带走。"
            if _page_kept else None
        ),
    )
