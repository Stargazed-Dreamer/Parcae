"""X9 验收：三投影与 SessionQuery 读路径。

覆盖 tickets.md T3：
- 删掉三表全部行 → 读路径报 projection_missing，而不是给错数
- rebuild 后与删除前逐字段相等；重复 rebuild 结果一致
- 两会话交错写入统计不串
- steer 消息不另起 turn；新 user 消息才切轮
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from lib.agent_runtime.event_store import EventStore
from lib.agent_runtime.session_query import ProjectionMissing, SessionQuery
from lib.agent_runtime.types import Message, ToolCall


@pytest.fixture
def store(tmp_db_path) -> Iterator[EventStore]:
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


@pytest.fixture
def query(store) -> SessionQuery:
    return SessionQuery(store)


def _drive_one_turn(store: EventStore, session_id: str = "s-proj") -> None:
    async def run():
        await store.create_session(session_id=session_id, title="")
        await store.update_session_title(session_id, "投影测试")
        await store.append_message(
            session_id, Message(role="user", content="hi", source="user")
        )
        await store.append_message(
            session_id, Message(role="assistant", content="hello", source="assistant")
        )
        await store.append_tool_call(
            session_id,
            ToolCall(id="tc-1", session_id=session_id, seq=1, name="file_ls"),
        )
        await store.update_tool_call_status("tc-1", "completed")
        await store.append_message(
            session_id,
            Message(
                role="tool", content="[]", source="tool_result", tool_call_id="tc-1"
            ),
        )
        await store.append_usage(session_id, 1, 10, 5, 15, "mock")
        await store.finalize_session(session_id)

    asyncio.run(run())


def test_incremental_projections_match_expected_counts(store, query):
    _drive_one_turn(store)

    async def run():
        stats = await query.stats("s-proj")
        title = await query.title("s-proj")
        outline = await query.turn_outline("s-proj")
        summary = await query.get("s-proj")
        return stats, title, outline, summary

    stats, title, outline, summary = asyncio.run(run())

    assert stats.message_count == 3
    assert stats.tool_call_count == 1
    assert stats.total_tokens == 15
    assert stats.event_count >= 8
    assert stats.status == "completed"
    assert stats.updated_seq > 0
    assert stats.last_event_at is not None

    assert title.title == "投影测试"
    assert title.source_seq is not None

    assert len(outline) == 1
    turn = outline[0]
    assert turn.status == "completed"
    assert turn.end_reason == "completed"
    assert turn.ended_at is not None
    assert turn.step_count == 1
    assert turn.tool_count == 1

    assert summary is not None
    assert summary.id == "s-proj"
    assert summary.title == "投影测试"
    assert summary.status == "completed"
    assert summary.message_count == 3
    assert summary.total_tokens == 15


def test_delete_projections_raises_then_rebuild_restores_exact_rows(store, query):
    _drive_one_turn(store)

    async def snapshot():
        return (
            await query.stats("s-proj"),
            await query.title("s-proj"),
            await query.turn_outline("s-proj"),
            await query.get("s-proj"),
            await query.list_sessions(),
        )

    before = asyncio.run(snapshot())

    store.conn.execute("DELETE FROM proj_stats")
    store.conn.execute("DELETE FROM proj_turn_outline")
    store.conn.execute("DELETE FROM proj_title")
    store.conn.commit()

    async def expect_missing():
        with pytest.raises(ProjectionMissing) as stats_err:
            await query.stats("s-proj")
        assert stats_err.value.code == "projection_missing"
        with pytest.raises(ProjectionMissing):
            await query.title("s-proj")
        with pytest.raises(ProjectionMissing):
            await query.turn_outline("s-proj")
        with pytest.raises(ProjectionMissing):
            await query.get("s-proj")
        with pytest.raises(ProjectionMissing):
            await query.list_sessions()
        return await query.rebuild()

    result = asyncio.run(expect_missing())
    assert result["sessions"] == 1
    assert result["events"] > 0

    after = asyncio.run(snapshot())
    assert after == before

    # 幂等：再跑一次结果不变
    asyncio.run(query.rebuild())
    assert asyncio.run(snapshot()) == before


def test_two_sessions_interleaved_do_not_cross(store, query):
    async def run():
        await store.create_session(session_id="s-A", title="A")
        await store.create_session(session_id="s-B", title="B")
        await store.append_message("s-A", Message(role="user", content="a1", source="user"))
        await store.append_message("s-B", Message(role="user", content="b1", source="user"))
        await store.append_message("s-A", Message(role="assistant", content="a2", source="assistant"))
        await store.append_message("s-B", Message(role="assistant", content="b2", source="assistant"))
        await store.append_tool_call("s-B", ToolCall(id="tc-B", session_id="s-B", name="x"))
        await store.append_usage("s-A", 1, 1, 1, 2)
        await store.append_usage("s-B", 1, 3, 4, 7)
        return await query.stats("s-A"), await query.stats("s-B"), await query.turn_outline("s-A"), await query.turn_outline("s-B")

    stats_a, stats_b, outline_a, outline_b = asyncio.run(run())
    assert (stats_a.message_count, stats_a.tool_call_count, stats_a.total_tokens) == (2, 0, 2)
    assert (stats_b.message_count, stats_b.tool_call_count, stats_b.total_tokens) == (2, 1, 7)
    assert len(outline_a) == 1 and outline_a[0].step_count == 1
    assert len(outline_b) == 1 and outline_b[0].tool_count == 1


def test_delete_message_updates_projection_count(store, query):
    async def run():
        await store.create_session(session_id="s-del-msg", title="")
        msg = await store.append_message(
            "s-del-msg", Message(role="user", content="x", source="queue")
        )
        await store.append_message(
            "s-del-msg", Message(role="assistant", content="y", source="assistant")
        )
        before = await query.stats("s-del-msg")
        deleted = await store.delete_message_by_id(msg.id)
        after = await query.stats("s-del-msg")
        return before, deleted, after

    before, deleted, after = asyncio.run(run())
    assert deleted is True
    assert before.message_count == 2
    assert after.message_count == 1


def test_rebuild_one_session_does_not_touch_other(store, query):
    async def run():
        for sid in ("s-one", "s-two"):
            await store.create_session(session_id=sid, title=sid)
            await store.append_message(sid, Message(role="user", content=sid, source="user"))
        before_one = await query.stats("s-one")
        before_two = await query.stats("s-two")
        await query.rebuild("s-one")
        return before_one, before_two, await query.stats("s-one"), await query.stats("s-two")

    before_one, before_two, after_one, after_two = asyncio.run(run())
    assert after_one == before_one
    assert after_two == before_two


def test_branch_session_projection_counts_copied_messages(store, query):
    async def run():
        await store.create_session(session_id="s-src", title="源")
        await store.append_message("s-src", Message(role="user", content="q", source="user"))
        await store.append_message("s-src", Message(role="assistant", content="a", source="assistant"))
        new_id = await store.branch_session("s-src", upto_seq=2)
        summary = await query.get(new_id)
        outline = await query.turn_outline(new_id)
        return new_id, summary, outline

    new_id, summary, outline = asyncio.run(run())
    assert summary is not None
    assert summary.id == new_id
    assert summary.title.endswith("(分支)")
    assert summary.message_count == 2
    # 分支没有 message 事件，轮次投影为空是已知边界（消息数已兜底）
    assert outline == []


def test_steer_does_not_start_new_turn(store, query):
    async def run():
        await store.create_session(session_id="s-steer", title="")
        await store.append_message("s-steer", Message(role="user", content="q", source="user"))
        await store.append_message("s-steer", Message(role="assistant", content="a", source="assistant"))
        # 插话：role=user、source=steer，不应切轮
        await store.append_message("s-steer", Message(role="user", content="more", source="steer"))
        await store.append_message("s-steer", Message(role="assistant", content="b", source="assistant"))
        first = await query.turn_outline("s-steer")
        # 真正的新用户消息才切轮
        await store.append_message("s-steer", Message(role="user", content="q2", source="user"))
        second = await query.turn_outline("s-steer")
        return first, second

    first, second = asyncio.run(run())
    assert len(first) == 1
    assert first[0].step_count == 2
    assert len(second) == 2
    assert second[0].ended_at is not None
    assert second[1].ended_at is None
    assert second[1].status == "running"
