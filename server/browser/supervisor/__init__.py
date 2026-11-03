"""浏览器进程 supervisor：配置、进程托管、状态机与对外状态对象。"""

from .config import SupervisorConfig
from .manager import (
    BrowserSupervisor,
    get_browser_supervisor,
    reset_browser_supervisor,
)
from .process import (
    BrowserLaunchError,
    BrowserNotFound,
    BrowserPortInUse,
    BrowserProcess,
    BrowserProcessExited,
    BrowserStartTimeout,
)
from .state import BrowserStatus, SupervisorState

__all__ = [
    "BrowserLaunchError",
    "BrowserNotFound",
    "BrowserPortInUse",
    "BrowserProcess",
    "BrowserProcessExited",
    "BrowserStartTimeout",
    "BrowserStatus",
    "BrowserSupervisor",
    "SupervisorConfig",
    "SupervisorState",
    "get_browser_supervisor",
    "reset_browser_supervisor",
]
