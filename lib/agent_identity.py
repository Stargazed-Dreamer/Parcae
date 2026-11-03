"""调用方身份：区分"agent 发起"与"人（GUI）发起"。

存在的理由是一次实测发现的错判：客户端发的头是 `X-Agent-Caller: v6-lite-agent`
（`lib/agent_runtime/http_client_tool_executor.py:40`），而服务端两处判据写死成
`== "true"`（`server/core/middleware.py:418`、`server/exec.py:1293`）。
于是**agent 流量被当成 GUI 用户流量**：审批中间件不看它，terminal 的 owner_token
检查也免掉。这不是越权问题，是系统对"这是谁"判断错了——属于
`temp/sdd/unknown-outcome-evidence/spec.md` 的 D-12 家族。

判据方向：只要出现该头且值不是明确的否定词，就算 agent 发起。
GUI 从不设置这个头，所以"有值即 agent"是安全方向。
"""

from __future__ import annotations

from typing import Final

AGENT_CALLER_HEADER: Final = "X-Agent-Caller"
# 客户端实际发送的值（历史命名，沿用以免同时改两端协议）
AGENT_CALLER_HEADER_VALUE: Final = "v6-lite-agent"

# 显式否定值：留着兼容任何"我不是 agent"的表态
_NEGATIVE_VALUES: Final = frozenset({"", "false", "0", "no", "gui", "user"})

# 无 request 上下文（内部调用/测试）时的 caller 归属
ANONYMOUS_CALLER: Final = "anonymous"
# GUI/用户请求（无头或显式否定值）的 caller 归属
GUI_CALLER: Final = "gui"


def is_agent_caller(headers) -> bool:
    """headers 支持 dict-like（Starlette Request.headers / requests 响应头）。"""
    try:
        raw = headers.get(AGENT_CALLER_HEADER, "")
    except Exception:
        return False
    if raw is None:
        return False
    return str(raw).strip().lower() not in _NEGATIVE_VALUES


def get_caller_id(headers) -> str:
    """提取调用方身份（用于共享资源的归属/owner 记录）。

    - 无头或显式否定值 → GUI_CALLER（"gui"）：人在操作，永远拥有最高权限
      （可关/抢任何资源，但反方向不成立：agent 不可动 gui 的资源）。
    - 有头 → 头的原值 strip 后直接作为 caller id。历史值 "v6-lite-agent"
      代表 client 的 agent；新调用方可传更具体的名字（如 "codebuddy-ide"），
      归属记录与审计日志即可区分到"哪个 agent"。
    - headers 为 None（内部调用）→ ANONYMOUS_CALLER，受最短租约约束。
    """
    if headers is None:
        return ANONYMOUS_CALLER
    try:
        raw = headers.get(AGENT_CALLER_HEADER, "")
    except Exception:
        return ANONYMOUS_CALLER
    value = str(raw or "").strip()
    if not value or value.lower() in _NEGATIVE_VALUES:
        return GUI_CALLER
    return value
