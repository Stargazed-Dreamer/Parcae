"""事件域注册表（X10）：事件类型 → persistent / live / capability。

单一真源：新增事件类型时只改这里。未登记的类型一律按 persistent 处理——
漏登记最多让噪音多回放一次，不会让必要事件消失。

- persistent：会话状态、消息、工具调用、压缩、恢复等必须可回放的事实
- live：流式增量；仍落库便于排障，但默认不参与回放
- capability：能力/工具目录类；默认参与回放，供客户端重建能力快照
"""

from __future__ import annotations

EVENT_DOMAIN_PERSISTENT = "persistent"
EVENT_DOMAIN_LIVE = "live"
EVENT_DOMAIN_CAPABILITY = "capability"

EVENT_DOMAINS = frozenset({
    EVENT_DOMAIN_PERSISTENT,
    EVENT_DOMAIN_LIVE,
    EVENT_DOMAIN_CAPABILITY,
})

# 默认回放的域：live 要显式 include_live=True 才返回
REPLAY_DOMAINS = (EVENT_DOMAIN_PERSISTENT, EVENT_DOMAIN_CAPABILITY)

EVENT_TYPE_DOMAINS: dict[str, str] = {
    # ---- 会话生命周期（persistent）----
    "session_created": EVENT_DOMAIN_PERSISTENT,
    "session_status_changed": EVENT_DOMAIN_PERSISTENT,
    "session_title_changed": EVENT_DOMAIN_PERSISTENT,
    "session_group_changed": EVENT_DOMAIN_PERSISTENT,
    "session_pinned_changed": EVENT_DOMAIN_PERSISTENT,
    "session_finalized": EVENT_DOMAIN_PERSISTENT,
    "session_branched": EVENT_DOMAIN_PERSISTENT,
    "session_reconciled": EVENT_DOMAIN_PERSISTENT,
    # ---- 消息（persistent）----
    "user_message_appended": EVENT_DOMAIN_PERSISTENT,
    "assistant_message_appended": EVENT_DOMAIN_PERSISTENT,
    "system_message_appended": EVENT_DOMAIN_PERSISTENT,
    "synthetic_message_appended": EVENT_DOMAIN_PERSISTENT,
    "tool_message_appended": EVENT_DOMAIN_PERSISTENT,
    # ---- runner / 压缩（persistent）----
    "transition": EVENT_DOMAIN_PERSISTENT,
    "wall_clock_budget_exceeded": EVENT_DOMAIN_PERSISTENT,
    "max_iterations_reached": EVENT_DOMAIN_PERSISTENT,
    "compact_failed": EVENT_DOMAIN_PERSISTENT,
    "compact_noop": EVENT_DOMAIN_PERSISTENT,
    "context_compacted": EVENT_DOMAIN_PERSISTENT,
    "context_overflow_after_compact": EVENT_DOMAIN_PERSISTENT,
    "context_overflow_no_compactor": EVENT_DOMAIN_PERSISTENT,
    "streaming_events_merged": EVENT_DOMAIN_PERSISTENT,
    # ---- 工具执行（persistent）----
    "tool_call_pending": EVENT_DOMAIN_PERSISTENT,
    "tool_call_status_changed": EVENT_DOMAIN_PERSISTENT,
    "tool_call_status_write_rejected": EVENT_DOMAIN_PERSISTENT,
    "tool_result_appended": EVENT_DOMAIN_PERSISTENT,
    # ---- ask_user 跨进程（persistent：重连后卡片仍可恢复）----
    "ask_user_request": EVENT_DOMAIN_PERSISTENT,
    "ask_user_resolved": EVENT_DOMAIN_PERSISTENT,
    # ---- runtime 自身故障（persistent：不能只留在内存里）----
    "runtime_error": EVENT_DOMAIN_PERSISTENT,
    # ---- 流式增量（live）----
    "streaming_text_delta": EVENT_DOMAIN_LIVE,
    "streaming_thinking_delta": EVENT_DOMAIN_LIVE,
    "streaming_tool_call": EVENT_DOMAIN_LIVE,
}


def domain_for_event(event_type: str) -> str:
    """事件类型所属域；未登记一律 persistent。"""
    return EVENT_TYPE_DOMAINS.get(event_type, EVENT_DOMAIN_PERSISTENT)
