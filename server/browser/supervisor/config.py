"""supervisor 配置对象与路径约定。

配置真源在 `[browser.supervisor]`（server/config.py 的 get_supervisor_config）。
本模块只把 dict 变成不可变 dataclass，并给出项目根/默认 profile 路径。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]


def repo_root() -> Path:
    return _REPO_ROOT


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    enabled: bool = True
    browser_path: str = ""
    port: int = 9222
    port_file: str = "data/runtime/agent_browser.json"
    user_data_dir: str = ""
    adopt_existing: bool = True
    health_interval_secs: float = 5.0
    health_timeout_secs: float = 2.0
    startup_timeout_secs: float = 10.0
    close_grace_secs: float = 6.0
    exit_wait_secs: float = 3.0
    max_restarts: int = 5
    backoff_max_secs: float = 60.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> SupervisorConfig:
        if not raw:
            return cls()
        known = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in raw.items() if key in known})

    @property
    def profile_dir(self) -> Path:
        if self.user_data_dir:
            return Path(self.user_data_dir).expanduser()
        return _REPO_ROOT / "chrome_debug"

    @property
    def declaration_path(self) -> Path:
        path = Path(self.port_file).expanduser()
        return path if path.is_absolute() else _REPO_ROOT / path

    @property
    def dynamic_port(self) -> bool:
        return self.port == 0
