"""权限模式定义。

三档权限模式（时限真源是 `[screen]` 配置键，不在这里写数字）：
- NO_PERMISSION: 只读端点放行，副作用端点返回 403
- NORMAL: 普通执行模式，空闲两阶段（`idle_warning_seconds` 进告警 +
  `idle_grace_seconds` 撤销）后降级到无权限
- WATCHDOG: 看门狗模式，不因空闲撤销，`watchdog_default_hours` 硬上限到期降级到 NORMAL

副作用端点**不靠名单判定**：每个受把关的 handler 在开头显式调用
`server/screen/routes.py::_enforce_session_permission()`。曾在此文件维护过
`ENDPOINTS_SIDE_EFFECT`/`ENDPOINTS_READONLY` 两张名单，但它们全仓零消费、内容还和
实际把关集合不一致（2026-09-30 删除）。需要清单时调
`routes.gated_side_effect_operations()`（现场扫描路由表，`/health` 的
`screen.takeover_confirm.protected_endpoints` 即其结果）。
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Computer Use 权限模式。"""

    NO_PERMISSION = "no_permission"
    NORMAL = "normal"
    WATCHDOG = "watchdog"
