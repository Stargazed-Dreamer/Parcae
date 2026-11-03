"""supervisor 状态枚举与对外状态对象。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class SupervisorState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(slots=True)
class BrowserStatus:
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

    def as_dict(self) -> dict:
        return asdict(self)
