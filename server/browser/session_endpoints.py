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

from .routes import router
from .session.manager import get_session_manager

logger = logging.getLogger("localagent.browser_session")

# ========== 请求/响应模型 ==========

class BrowserSessionCreateRequest(BaseSchema):
    """创建持久浏览器会话。

    持久 session 解决"每次 DOM 操作都重新 connect_over_cdp + 启动子进程"
    导致的 0.5–1.4 秒固定开销。服务端维持长连接 Playwright Page 句柄，
    后续 snapshot/action/wait_for/evaluate 调用传入 session_id 即可复用，
    热态延迟 <250ms。

    - url_pattern: 复用现有 page（url 包含 pattern）；若该 tab 已被其他 agent
      有主持有，需 force=true 才能接管（2026-10-02 归属权，否则 409 TAB_OWNED）
    - url: 新建 page 并 goto url（优先级低于 url_pattern）
    - 都不传：新建专用空白 tab（不复用已有页面，避免误绑用户真实 tab/扩展窗口页；
      2026-09-03 修复，详见 session/manager.py create_session 注释）
    - force: 允许接管其他调用方有主持有的 tab（url_pattern 场景）

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

    Example — attach to an existing bilibili tab:
      browser_session_create(url_pattern="bilibili.com")
      （2026-10-02 归属权：该 tab 已被其他 agent 有主持有时需 force=true，否则 409 TAB_OWNED）
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

    Always call this when done with a session to free server resources, even
    though idle sessions auto-expire.

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
    closed = await mgr.close_session(
        req.session_id,
        close_page=req.close_page,
        closed_reason=f"manual:{caller}" + ("(force)" if req.force else ""),
    )
    elapsed = int((_time.perf_counter() - start) * 1000)
    return BrowserSessionCloseResponse(success=True, closed=closed, elapsed_ms=elapsed)
