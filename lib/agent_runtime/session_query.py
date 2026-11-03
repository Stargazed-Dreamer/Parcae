"""X9 会话读侧：只读三投影，不在 UI 侧临时聚合。

账本是权威，投影只是缓存。投影行缺失时抛 `ProjectionMissing`，
绝不退化成"扫账本现算"或返回 0——那会把缺投影伪装成正常空会话。
重建走 `SessionQuery.rebuild()`（委托 EventStore.rebuild_projections）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from lib.agent_runtime.event_store import EventStore

PROJECTION_MISSING_CODE = "projection_missing"


class ProjectionMissing(RuntimeError):
    """投影行不存在；调用 rebuild 后重试。"""

    code = PROJECTION_MISSING_CODE

    def __init__(self, session_id: str, projection: str):
        self.session_id = session_id
        self.projection = projection
        super().__init__(
            f"{PROJECTION_MISSING_CODE}: {projection} has no row for session "
            f"{session_id!r}; run rebuild_projections() first"
        )


@dataclass(frozen=True)
class SessionStats:
    session_id: str
    message_count: int
    event_count: int
    tool_call_count: int
    total_tokens: int
    last_event_at: float | None
    status: str
    updated_seq: int


@dataclass(frozen=True)
class TurnOutline:
    session_id: str
    turn_id: str
    started_at: float | None
    ended_at: float | None
    end_reason: str | None
    step_count: int
    tool_count: int
    status: str


@dataclass(frozen=True)
class SessionTitle:
    session_id: str
    title: str
    source_seq: int | None
    updated_at: float | None


@dataclass(frozen=True)
class SessionSummary:
    id: str
    title: str
    mode: str
    status: str
    pinned: bool
    group_name: str | None
    created_at: float | None
    updated_at: float | None
    message_count: int
    event_count: int
    tool_call_count: int
    total_tokens: int
    last_event_at: float | None
    updated_seq: int


_SUMMARY_SQL = """
SELECT s.id, s.mode, s.pinned, s.group_name, s.created_at, s.updated_at,
       st.session_id AS stats_session_id,
       st.message_count, st.event_count, st.tool_call_count, st.total_tokens,
       st.last_event_at, st.status AS stats_status, st.updated_seq,
       t.session_id AS title_session_id, t.title AS proj_title
FROM sessions s
LEFT JOIN proj_stats st ON st.session_id = s.id
LEFT JOIN proj_title t ON t.session_id = s.id
"""


class SessionQuery:
    """投影读入口。所有方法异步执行（复用 EventStore 的连接池）。"""

    def __init__(self, store: EventStore):
        self._store = store

    async def stats(self, session_id: str) -> SessionStats:
        def _sync() -> SessionStats | None:
            r = self._store.conn.execute(
                "SELECT session_id, message_count, event_count, tool_call_count, "
                "total_tokens, last_event_at, status, updated_seq "
                "FROM proj_stats WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if r is None:
                return None
            return SessionStats(
                session_id=r["session_id"],
                message_count=int(r["message_count"]),
                event_count=int(r["event_count"]),
                tool_call_count=int(r["tool_call_count"]),
                total_tokens=int(r["total_tokens"]),
                last_event_at=r["last_event_at"],
                status=str(r["status"] or ""),
                updated_seq=int(r["updated_seq"]),
            )

        result = await self._store._run_sync(_sync)
        if result is None:
            raise ProjectionMissing(session_id, "proj_stats")
        return result

    async def title(self, session_id: str) -> SessionTitle:
        def _sync() -> SessionTitle | None:
            r = self._store.conn.execute(
                "SELECT session_id, title, source_seq, updated_at "
                "FROM proj_title WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if r is None:
                return None
            return SessionTitle(
                session_id=r["session_id"],
                title=str(r["title"] or ""),
                source_seq=r["source_seq"],
                updated_at=r["updated_at"],
            )

        result = await self._store._run_sync(_sync)
        if result is None:
            raise ProjectionMissing(session_id, "proj_title")
        return result

    async def turn_outline(self, session_id: str) -> list[TurnOutline]:
        def _sync() -> list[TurnOutline]:
            sentinel = self._store.conn.execute(
                "SELECT 1 FROM proj_stats WHERE session_id = ?", (session_id,)
            ).fetchone()
            if sentinel is None:
                raise ProjectionMissing(session_id, "proj_stats")
            rows = self._store.conn.execute(
                "SELECT session_id, turn_id, started_at, ended_at, end_reason, "
                "step_count, tool_count, status FROM proj_turn_outline "
                "WHERE session_id = ? ORDER BY started_at ASC, turn_id ASC",
                (session_id,),
            ).fetchall()
            return [
                TurnOutline(
                    session_id=r["session_id"],
                    turn_id=str(r["turn_id"]),
                    started_at=r["started_at"],
                    ended_at=r["ended_at"],
                    end_reason=r["end_reason"],
                    step_count=int(r["step_count"]),
                    tool_count=int(r["tool_count"]),
                    status=str(r["status"] or ""),
                )
                for r in rows
            ]

        return await self._store._run_sync(_sync)

    async def get(self, session_id: str) -> SessionSummary | None:
        def _sync() -> SessionSummary | None:
            r = self._store.conn.execute(
                _SUMMARY_SQL + " WHERE s.id = ?", (session_id,)
            ).fetchone()
            if r is None:
                return None
            return self._row_to_summary(r)

        return await self._store._run_sync(_sync)

    async def list_sessions(self) -> list[SessionSummary]:
        def _sync() -> list[SessionSummary]:
            rows = self._store.conn.execute(
                _SUMMARY_SQL + " ORDER BY s.pinned DESC, s.updated_at DESC"
            ).fetchall()
            return [self._row_to_summary(r) for r in rows]

        return await self._store._run_sync(_sync)

    @staticmethod
    def _row_to_summary(r: sqlite3.Row) -> SessionSummary:
        session_id = str(r["id"])
        if r["stats_session_id"] is None:
            raise ProjectionMissing(session_id, "proj_stats")
        if r["title_session_id"] is None:
            raise ProjectionMissing(session_id, "proj_title")
        return SessionSummary(
            id=session_id,
            title=str(r["proj_title"] or ""),
            mode=str(r["mode"] or "dialogue"),
            status=str(r["stats_status"] or ""),
            pinned=bool(r["pinned"]),
            group_name=r["group_name"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            message_count=int(r["message_count"]),
            event_count=int(r["event_count"]),
            tool_call_count=int(r["tool_call_count"]),
            total_tokens=int(r["total_tokens"]),
            last_event_at=r["last_event_at"],
            updated_seq=int(r["updated_seq"]),
        )

    async def rebuild(self, session_id: str | None = None) -> dict[str, int]:
        """重建投影；`session_id=None` 表示全部会话。"""
        return await self._store.rebuild_projections(session_id)
