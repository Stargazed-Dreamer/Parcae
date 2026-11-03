"""主动压缩接线（2026-10-10，wip_chat_3directions 接线格）。

背景：压缩判据 10-07 修准（锚点真值 + 每模型上限）后，`Compactor.should_compact()`
一直**没有任何生产调用点**——runner 每轮只 `_log_context_estimate` 打一行观测。
本文件钉住新接的主动压缩（撞墙前预防，与 413 后的 reactive 压缩互补）：

1. 估算超阈值 ⇒ compact_with_result → replace_messages →
   transition(proactive_compact) → continue 重建请求
2. 上限未知（max_context_tokens=0，keys.json 未填 context_window）⇒ 恒不压（fail-safe）
3. 未注入 compactor ⇒ 不压
4. 每 run 主动压缩次数有上限（防"压了还超"死循环），撞上限后照常调 LLM
5. compactor 抛异常 ⇒ fail-open（只 warning，不阻断主循环）
6. compaction_occurred=False（消息太少/摘要失败）⇒ 不 replace、不写 transition
7. 压缩历史与 reactive 路径共用 compactions 表记录口径
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.agent_runtime import EventStore  # noqa: E402
from lib.agent_runtime.compactor import (  # noqa: E402
    CompactionResult,
    Compactor,
    UsageAnchor,
)
from lib.agent_runtime.runner import MAX_PROACTIVE_COMPACTIONS  # noqa: E402
from lib.agent_runtime.types import Message  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_proactive.db"))
    store.init()
    return store


def _msg(text: str) -> Message:
    return Message(role="user", content=text, source="user")


def _reasons(store: EventStore, sid: str) -> list[str]:
    events = _run(store.load_events(sid))
    return [e.payload.get("reason") for e in events if e.type == "transition"]


async def _seed(store: EventStore, sid: str, *, prompt_tokens: int, n_messages: int) -> None:
    """种一个真实 usage 锚点 + n 条消息。

    锚点 prompt_tokens=900 / 上限 1000 ⇒ 阈值 850，estimate 必然越界。
    消息数需 > tail_keep(6)，压缩才有 middle 可摘要。
    """
    await store.create_session(session_id=sid)
    await store.append_usage(
        sid, seq=1, prompt_tokens=prompt_tokens,
        completion_tokens=5, total_tokens=prompt_tokens + 5, model="m-a",
    )
    for i in range(n_messages):
        await store.append_message(sid, _msg(f"m{i}-" + "x" * 20))


def _build_runner(store: EventStore, sid: str, compactor):
    from lib.agent_runtime import MockLLM, RunnerConfig, RunnerDeps, SessionRunner

    llm = MockLLM(default_text="ok")
    runner = SessionRunner(
        deps=RunnerDeps(llm_gateway=llm, event_store=store, compactor=compactor),
        config=RunnerConfig(session_id=sid, use_stream=False),
    )
    return runner, llm


# ============================================================================
# 1. 超阈值 ⇒ 主动压缩
# ============================================================================


class TestTriggersOnThreshold:
    def test_over_threshold_compacts_then_calls_llm(self, tmp_path):
        store = _new_store(tmp_path)
        sid = "sess-proactive"
        try:
            _run(_seed(store, sid, prompt_tokens=900, n_messages=8))
            runner, llm = _build_runner(
                store, sid, Compactor(max_context_tokens=1000),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            assert runner._proactive_compact_count == 1
            assert "proactive_compact" in _reasons(store, sid), _reasons(store, sid)
            # E7：compactions 表落了一行
            assert len(_run(store.load_compactions(sid))) == 1
            # 压缩后消息里出现摘要消息（visible=True，load_messages 保留）
            msgs = _run(store.load_messages(sid))
            assert any(m.source == "summary" for m in msgs), [m.source for m in msgs]
            # 摘要调用 + 主调用 = 2 次
            assert llm.call_count == 2
        finally:
            store.close()

    def test_below_threshold_does_not_compact(self, tmp_path):
        """阈值以下不许压——否则每次对话都白烧一次摘要调用。"""
        store = _new_store(tmp_path)
        sid = "sess-under"
        try:
            # 锚点 500 < 850 阈值；消息也短
            _run(_seed(store, sid, prompt_tokens=500, n_messages=8))
            runner, llm = _build_runner(
                store, sid, Compactor(max_context_tokens=1000),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            assert runner._proactive_compact_count == 0
            assert "proactive_compact" not in _reasons(store, sid)
            assert _run(store.load_compactions(sid)) == []
            assert llm.call_count == 1
        finally:
            store.close()


# ============================================================================
# 2/3. 不该压的情形（fail-safe）
# ============================================================================


class TestNoOpGuards:
    def test_unknown_limit_is_a_noop(self, tmp_path):
        """上限未知（keys.json 未填 context_window）⇒ should_compact 恒 False。"""
        store = _new_store(tmp_path)
        sid = "sess-unknown-limit"
        try:
            _run(_seed(store, sid, prompt_tokens=900_000, n_messages=8))
            runner, llm = _build_runner(
                store, sid, Compactor(max_context_tokens=0),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            assert runner._proactive_compact_count == 0
            assert "proactive_compact" not in _reasons(store, sid)
            assert _run(store.load_compactions(sid)) == []
            assert llm.call_count == 1
        finally:
            store.close()

    def test_no_compactor_injected_never_compacts(self, tmp_path):
        from lib.agent_runtime import MockLLM, RunnerConfig, RunnerDeps, SessionRunner

        store = _new_store(tmp_path)
        sid = "sess-nocomp"
        try:
            _run(_seed(store, sid, prompt_tokens=900, n_messages=8))
            llm = MockLLM(default_text="ok")
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=llm, event_store=store),
                config=RunnerConfig(session_id=sid, use_stream=False),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            assert runner._proactive_compact_count == 0
            assert "proactive_compact" not in _reasons(store, sid)
            assert llm.call_count == 1
        finally:
            store.close()


# ============================================================================
# 4. 死循环护栏
# ============================================================================


class _AlwaysCompactCompactor(Compactor):
    """永远说"要压"且每次都报 compaction_occurred=True（消息不变）——逼出护栏。

    继承 Compactor 而非另起炉灶：runner 每轮还会调 `estimate_context_tokens`
    打观测行，纯 fake 会在那儿先炸，测不到真正想测的护栏。
    """

    def __init__(self) -> None:
        super().__init__(max_context_tokens=1000)
        self.compact_calls = 0

    def should_compact(
        self, messages: list[Message], anchor: UsageAnchor | None = None,
    ) -> bool:
        return True

    async def compact_with_result(self, session_id, messages, llm_gateway):
        self.compact_calls += 1
        return CompactionResult(
            messages=list(messages), compaction_occurred=True, summary_message=None,
        )


class TestRunawayGuard:
    def test_cap_stops_runaway_and_still_calls_llm(self, tmp_path):
        store = _new_store(tmp_path)
        sid = "sess-runaway"
        try:
            _run(_seed(store, sid, prompt_tokens=900, n_messages=8))
            fake = _AlwaysCompactCompactor()
            runner, llm = _build_runner(store, sid, fake)
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            assert runner._proactive_compact_count == MAX_PROACTIVE_COMPACTIONS
            assert fake.compact_calls == MAX_PROACTIVE_COMPACTIONS
            # 撞上限后仍然去调 LLM（护栏是"别空转"，不是"别干活"）
            assert llm.call_count == 1
        finally:
            store.close()


# ============================================================================
# 5/6. fail-open 与"没真压"
# ============================================================================


class _RaisingCompactor(Compactor):
    def __init__(self) -> None:
        super().__init__(max_context_tokens=1000)

    def should_compact(
        self, messages: list[Message], anchor: UsageAnchor | None = None,
    ) -> bool:
        return True

    async def compact_with_result(self, session_id, messages, llm_gateway):
        raise RuntimeError("boom")


class TestFailOpen:
    def test_compactor_exception_is_fail_open(self, tmp_path, caplog):
        store = _new_store(tmp_path)
        sid = "sess-raise"
        try:
            _run(_seed(store, sid, prompt_tokens=900, n_messages=8))
            runner, llm = _build_runner(store, sid, _RaisingCompactor())
            with caplog.at_level("WARNING", logger="parcae.agent.runner"):
                outcome = _run(runner.run())

            assert outcome.status == "completed", outcome
            assert llm.call_count == 1
            assert "proactive_compact" not in _reasons(store, sid)
            texts = [r.getMessage() for r in caplog.records]
            assert any("compact raised" in t for t in texts), texts
        finally:
            store.close()

    def test_compaction_occurred_false_proceeds_without_rewriting(self, tmp_path):
        """消息太少（≤ tail_keep）⇒ compact 直接 skip，不许 replace/写 transition。"""
        store = _new_store(tmp_path)
        sid = "sess-noop"
        try:
            _run(_seed(store, sid, prompt_tokens=900, n_messages=2))
            runner, llm = _build_runner(
                store, sid, Compactor(max_context_tokens=1000),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            # 记一次尝试（防同一 run 里反复空转），但没真压
            assert runner._proactive_compact_count == 1
            assert "proactive_compact" not in _reasons(store, sid)
            assert _run(store.load_compactions(sid)) == []
            assert llm.call_count == 1
        finally:
            store.close()


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
