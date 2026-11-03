"""RuntimeStore：EventStore 同形的桌面端适配层（T7）。

面板只通过本模块读账本 / 写会话元数据，自己不再打开 SQLite：
- 读：runtime HTTP（WS 增量由面板的 `_RuntimeEventStream` 负责，本模块是回放/轮询降级路径）
- 写：runtime API（标题/置顶/分组/删除/分支/消息删除/排队消息）

方法名与 `EventStore` 保持一致（异步方法），所以 `_safe_read` 等既有调用点
不用改；UI 单测注入真 EventStore 时同样能跑（同一个鸭子接口）。
"""

from __future__ import annotations

import logging
from typing import Any

from lib.agent_runtime.runtime_client import RuntimeCallError, RuntimeClient
from lib.agent_runtime.types import Event, Message, Session, ToolCall

logger = logging.getLogger("parcae.runtime_store")

_STREAMING_TYPES = frozenset({
    "streaming_text_delta",
    "streaming_thinking_delta",
    "streaming_tool_call",
})


def _session(row: dict[str, Any]) -> Session:
    return Session(
        id=str(row.get("id") or ""),
        title=str(row.get("title") or ""),
        mode=str(row.get("mode") or "dialogue"),
        status=str(row.get("status") or "idle"),
        created_at=float(row.get("created_at") or 0.0),
        updated_at=float(row.get("updated_at") or 0.0),
        group_name=row.get("group_name"),
        pinned=bool(row.get("pinned", False)),
    )


def _message(row: dict[str, Any]) -> Message:
    return Message(
        role=str(row.get("role") or ""),
        content=row.get("content"),
        source=str(row.get("source") or ""),
        visible=bool(row.get("visible", True)),
        tool_call_id=row.get("tool_call_id"),
        tool_calls=list(row.get("tool_calls") or []),
        thinking=str(row.get("thinking") or ""),
        model=str(row.get("model") or ""),
        id=str(row.get("id") or ""),
        seq=int(row.get("seq") or 0),
        session_id=str(row.get("session_id") or ""),
        created_at=float(row.get("created_at") or 0.0),
    )


def _event(row: dict[str, Any]) -> Event:
    return Event(
        seq=int(row.get("seq") or 0),
        session_id=str(row.get("session_id") or ""),
        type=str(row.get("type") or ""),
        payload=dict(row.get("payload") or {}),
        created_at=float(row.get("created_at") or 0.0),
        trace_id=row.get("trace_id"),
        invalidated_seq=row.get("invalidated_seq"),
        parent_trace_id=row.get("parent_trace_id"),
        depth=int(row.get("depth") or 0),
        domain=str(row.get("domain") or "persistent"),
    )


def _tool_call(row: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=str(row.get("id") or ""),
        session_id=str(row.get("session_id") or ""),
        seq=int(row.get("seq") or 0),
        name=str(row.get("name") or ""),
        args=dict(row.get("args") or {}),
        args_raw=str(row.get("args_raw") or ""),
        safety=str(row.get("safety") or ""),
        status=str(row.get("status") or ""),
        started_at=float(row.get("started_at") or 0.0),
        ended_at=float(row.get("ended_at") or 0.0),
        trace_id=row.get("trace_id"),
    )


class RuntimeStore:
    """把 butler runtime 的 HTTP API 包装成 EventStore 形状（只读 + 会话元数据写）。"""

    def __init__(self, base_url: str | None = None, *, timeout: float = 5.0):
        self._base_url = base_url
        self._timeout = timeout

    # EventStore 形状的生命周期方法（无本地连接）
    def init(self) -> None:
        return None

    def close(self) -> None:
        return None

    def _client(self) -> RuntimeClient:
        return RuntimeClient(self._base_url, timeout=self._timeout)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def list_sessions(self) -> list[Session]:
        rows = await self._client().list_sessions()
        return [_session(row) for row in rows]

    async def get_session(self, session_id: str) -> Session | None:
        try:
            row = await self._client().get_session(session_id)
        except RuntimeCallError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _session(row)

    async def load_messages(
        self, session_id: str, include_invisible: bool = False,
    ) -> list[Message]:
        rows = await self._client().messages(
            session_id, include_invisible=include_invisible,
        )
        return [_message(row) for row in rows]

    async def load_tool_calls(self, session_id: str) -> list[ToolCall]:
        rows = await self._client().tool_calls(session_id)
        return [_tool_call(row) for row in rows]

    async def load_events(self, session_id: str, after_seq: int = 0) -> list[Event]:
        rows = await self._client().events(session_id, after_seq=after_seq)
        return [_event(row) for row in rows]

    async def load_streaming_events(self, session_id: str) -> list[Event]:
        rows = await self._client().events(session_id, include_live=True)
        return [
            _event(row)
            for row in rows
            if row.get("type") in _STREAMING_TYPES and row.get("invalidated_seq") is None
        ]

    async def list_distinct_group_names(self) -> list[str]:
        sessions = await self.list_sessions()
        return sorted({s.group_name for s in sessions if s.group_name})

    async def find_pending_queue_messages(self) -> list[Message]:
        rows = await self._client().pending_queue()
        return [_message(row) for row in rows]

    async def recovery_report(self) -> dict[str, Any]:
        """runtime 启动恢复结果（启动 banner 用；runtime 未跑时返回空）。"""
        try:
            return await self._client().recovery()
        except RuntimeCallError as exc:
            if exc.status_code == 404:
                return {}
            raise

    # ------------------------------------------------------------------
    # 写（会话元数据 + 排队消息）
    # ------------------------------------------------------------------

    async def update_session_title(self, session_id: str, title: str) -> None:
        await self._client().patch_session(session_id, title=title)

    async def update_session_pinned(self, session_id: str, pinned: bool) -> None:
        await self._client().patch_session(session_id, pinned=bool(pinned))

    async def update_session_group(
        self, session_id: str, group_name: str | None,
    ) -> None:
        await self._client().patch_session(session_id, group_name=group_name)

    async def delete_session(self, session_id: str) -> bool:
        await self._client().delete_session(session_id)
        return True

    async def delete_message_by_id(self, message_id: str) -> bool:
        try:
            await self._client().delete_message(message_id)
        except RuntimeCallError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    async def branch_session(
        self, session_id: str, upto_seq: int, title: str | None = None,
    ) -> str:
        return await self._client().branch(session_id, upto_seq, title)

    async def append_message(self, session_id: str, message: Message) -> Message:
        """排队消息落账（source='queue'）；其他来源的客户端写入一律不走这里。"""
        content = message.content if isinstance(message.content, str) else str(message.content)
        row = await self._client().enqueue(session_id, content)
        return _message(row)
