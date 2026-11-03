"""浏览器 supervisor 端点：status / ensure / stop / restart。

这组端点只负责浏览器「进程」的生命周期（启动、探活、停止、重启），
不碰 SessionManager 的 page 级连接与工具面。compact 工具面策略下它们
不进 DIRECT_TOOLS，agent 通过 parcae_advanced_tool 调用。
"""

import logging

from fastapi import HTTPException

from lib.schema import BaseSchema

from .routes import router
from .supervisor import (
    BrowserLaunchError,
    BrowserNotFound,
    BrowserPortInUse,
    BrowserStartTimeout,
    BrowserStatus,
    get_browser_supervisor,
)

logger = logging.getLogger("parcae.browser.supervisor")


class BrowserSupervisorStatusResponse(BaseSchema):
    success: bool = True
    state: str
    enabled: bool
    alive: bool
    pid: int | None = None
    port: int | None = None
    cdp_url: str | None = None
    profile: str = ""
    browser_path: str = ""
    owned: bool = False
    started_at: float | None = None
    uptime_secs: float | None = None
    restarts: int = 0
    last_error: str | None = None


class BrowserSupervisorEnsureRequest(BaseSchema):
    reason: str = ""


class BrowserSupervisorStopRequest(BaseSchema):
    force: bool = False


class BrowserSupervisorRestartRequest(BaseSchema):
    reason: str = ""


def _to_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, BrowserPortInUse):
        return HTTPException(409, detail=f"BROWSER_PORT_IN_USE: {exc}")
    if isinstance(exc, BrowserNotFound):
        return HTTPException(409, detail=f"BROWSER_NOT_FOUND: {exc}")
    if isinstance(exc, BrowserStartTimeout):
        return HTTPException(503, detail=f"BROWSER_START_TIMEOUT: {exc}")
    if isinstance(exc, BrowserLaunchError):
        return HTTPException(503, detail=f"BROWSER_LAUNCH_FAILED: {exc}")
    if isinstance(exc, RuntimeError):
        return HTTPException(409, detail=f"BROWSER_SUPERVISOR_ERROR: {exc}")
    return HTTPException(500, detail=f"BROWSER_SUPERVISOR_ERROR: {exc}")


def _response(status: BrowserStatus) -> BrowserSupervisorStatusResponse:
    return BrowserSupervisorStatusResponse(**status.as_dict())


@router.get(
    "/supervisor/status",
    response_model=BrowserSupervisorStatusResponse,
    operation_id="browser_supervisor_status",
)
async def browser_supervisor_status():
    """查询 supervisor 状态（不启动浏览器，不连 CDP）。"""
    supervisor = await get_browser_supervisor()
    return _response(await supervisor.status())


@router.post(
    "/supervisor/ensure",
    response_model=BrowserSupervisorStatusResponse,
    operation_id="browser_supervisor_ensure",
)
async def browser_supervisor_ensure(req: BrowserSupervisorEnsureRequest | None = None):
    """确保浏览器在跑（懒启动）；已在跑且健康时直接返回现有状态。"""
    supervisor = await get_browser_supervisor()
    try:
        return _response(await supervisor.ensure((req or BrowserSupervisorEnsureRequest()).reason))
    except Exception as exc:
        logger.warning("supervisor ensure 失败: %s", exc)
        raise _to_http_error(exc) from exc


@router.post(
    "/supervisor/stop",
    response_model=BrowserSupervisorStatusResponse,
    operation_id="browser_supervisor_stop",
)
async def browser_supervisor_stop(req: BrowserSupervisorStopRequest | None = None):
    """停止浏览器：先 Browser.close，超时才 terminate/kill 进程树。"""
    supervisor = await get_browser_supervisor()
    try:
        return _response(await supervisor.stop(force=(req or BrowserSupervisorStopRequest()).force))
    except Exception as exc:
        logger.warning("supervisor stop 失败: %s", exc)
        raise _to_http_error(exc) from exc


@router.post(
    "/supervisor/restart",
    response_model=BrowserSupervisorStatusResponse,
    operation_id="browser_supervisor_restart",
)
async def browser_supervisor_restart(req: BrowserSupervisorRestartRequest | None = None):
    """手动重启（重置自动重启预算）。"""
    supervisor = await get_browser_supervisor()
    try:
        return _response(await supervisor.restart((req or BrowserSupervisorRestartRequest()).reason))
    except Exception as exc:
        logger.warning("supervisor restart 失败: %s", exc)
        raise _to_http_error(exc) from exc
