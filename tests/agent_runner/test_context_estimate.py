"""压缩判据修复（2026-10-07，wip_chat_3directions 点单②）：真值锚点 + 每模型上限。

背景（实测）：真实会话里单次调用 prompt_tokens 最高 97090，而旧判据把整个会话
只估出 27196（`len(text)//4` 量不到 system prompt 与工具 schema），且默认上限
写死 32000——两头都对不上，所以压缩从未触发过（compactions 表 0 行）。

覆盖：
1. estimate_context_tokens：无锚点退回 chars_only；有锚点 = 锚点真值 + 只数锚点
   之后新增的消息；锚点 prompt_tokens=0 或被一次压缩作废 ⇒ 退回 chars_only
2. should_compact：上限 0 ⇒ 判"上限未知"不判压缩；同一批消息，锚点版本翻真、
   纯字符版本翻假（这一条才是"锚点让判据变准"的证明）
3. EventStore.get_last_usage_anchor：取真实 usage + last_compaction_at
4. ChatPanel.pick_context_window：同名多 provider 取最小非零值、缺失=0
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.panels.chat import ChatPanel  # noqa: E402
from lib.agent_runtime import EventStore  # noqa: E402
from lib.agent_runtime.compactor import Compactor, UsageAnchor  # noqa: E402
from lib.agent_runtime.types import Message  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _msg(text: str, created_at: float = 0.0, tool_calls: list[dict] | None = None) -> Message:
    return Message(
        role="user", content=text, source="user",
        created_at=created_at, tool_calls=tool_calls or [],
    )


# ============================================================================
# 1. estimate_context_tokens
# ============================================================================


class TestEstimateContextTokens:
    def test_without_anchor_falls_back_to_chars(self):
        c = Compactor(max_context_tokens=100_000)
        est = c.estimate_context_tokens([_msg("a" * 400), _msg("b" * 800)])
        assert est.basis == "chars_only"
        assert est.tokens == (400 + 800) // 4
        assert est.anchor_tokens == 0

    def test_anchor_plus_messages_after_it(self):
        """锚点之前的消息已经在真实 prompt_tokens 里，不重复计；锚点同刻的算增量。

        同刻这条是实测逼出来的：本机 `time.time()` 粒度 ~15.6ms 一个 tick，连着两次
        写常拿到同一个 float。usage 行写在 LLM 响应之后，所以同刻落库的消息只可能是
        请求构建之后新增的——判 `>` 会把它静默丢掉（正是本次要修的"低估"那一侧）。
        """
        anchor = UsageAnchor(prompt_tokens=5000, created_at=150.0)
        messages = [
            _msg("x" * 4000, created_at=100.0),   # 锚点前：不增量
            _msg("y" * 400, created_at=150.0),    # 与锚点同刻：算增量
            _msg("z" * 800, created_at=200.0),    # 锚点后：算增量
        ]
        est = Compactor(max_context_tokens=100_000).estimate_context_tokens(messages, anchor)
        assert est.basis == "usage_anchor+delta"
        assert est.anchor_tokens == 5000
        assert est.delta_tokens == (400 + 800) // 4
        assert est.tokens == 5000 + 300

    def test_same_tick_message_is_counted_through_the_store(self, tmp_path, monkeypatch):
        """同一个时间戳里"先写 usage 再写消息"也必须被算进增量（走真库，不是手搓 float）。"""
        from lib.agent_runtime import event_store as es_module

        store = _new_store(tmp_path)
        sid = "sess-tick"
        fixed = 1_700_000_000.0
        monkeypatch.setattr(es_module, "time", _FixedTime(fixed))

        async def seed():
            await store.create_session(session_id=sid)
            await store.append_usage(sid, seq=1, prompt_tokens=30000,
                                     completion_tokens=1, total_tokens=30001, model="m-a")
            await store.append_message(
                sid, Message(role="assistant", content="a" * 800, source="assistant"),
            )

        _run(seed())
        row = _fetch_anchor(store, sid)
        messages = _run(store.load_messages(sid))
        anchor = UsageAnchor(
            prompt_tokens=int(row["prompt_tokens"]),
            created_at=float(row["created_at"]),
            last_compaction_at=float(row["last_compaction_at"]),
        )
        assert float(messages[0].created_at) == anchor.created_at, "台子要真的同刻"
        est = Compactor().estimate_context_tokens(messages, anchor)
        assert est.delta_tokens == 200, est
        assert est.tokens == 30200, est
        store.close()

    def test_zero_prompt_tokens_is_not_an_anchor(self):
        """usage 行存在但 prompt_tokens=0（断流/未上报）时不能当"这轮很小"用。"""
        anchor = UsageAnchor(prompt_tokens=0, created_at=150.0)
        est = Compactor().estimate_context_tokens([_msg("a" * 400)], anchor)
        assert est.basis == "chars_only"
        assert est.tokens == 100

    def test_anchor_before_a_compaction_is_invalidated(self):
        """压缩会重写 messages：比压缩更早的锚点算的是"压缩前那份上下文"。"""
        anchor = UsageAnchor(
            prompt_tokens=90_000, created_at=150.0, last_compaction_at=200.0,
        )
        assert anchor.invalidated_by_compaction is True
        est = Compactor().estimate_context_tokens([_msg("a" * 400)], anchor)
        assert est.basis == "chars_only"
        assert est.tokens == 100

    def test_tool_call_arguments_count_in_the_delta(self):
        anchor = UsageAnchor(prompt_tokens=1000, created_at=100.0)
        msg = _msg(
            "", created_at=200.0,
            tool_calls=[{"id": "t1", "function": {"name": "f", "arguments": "a" * 400}}],
        )
        est = Compactor().estimate_context_tokens([msg], anchor)
        assert est.delta_tokens == 100
        assert est.tokens == 1100


# ============================================================================
# 2. should_compact
# ============================================================================


class TestShouldCompactWithAnchor:
    def test_unknown_limit_never_compacts_and_says_so(self, caplog):
        """上限未知 ⇒ 明说"上限未知"，而不是拿编出来的默认值判压缩。"""
        c = Compactor(max_context_tokens=0)
        with caplog.at_level("INFO", logger="parcae.agent.compactor"):
            assert c.should_compact([_msg("a" * 4000)]) is False
        lines = [r.getMessage() for r in caplog.records]
        assert any("上限未知" in line for line in lines), lines

    def test_anchor_is_what_flips_the_verdict(self):
        """同一批消息：纯字符估算判"不用压"，锚点版判"要压"。

        这条是本次修复的全部意义——旧口径量不到 stable prefix，会一路低估到
        provider 真的 413。
        """
        messages = [_msg("a" * 400, created_at=100.0)]
        c = Compactor(max_context_tokens=60_000)  # 阈值 51000
        assert c.should_compact(messages) is False  # chars_only ≈ 100
        anchor = UsageAnchor(prompt_tokens=52_000, created_at=90.0)
        assert c.should_compact(messages, anchor) is True

    def test_positional_call_still_works(self):
        """旧调用形状 should_compact(messages) 不能被关键字参数绑死。"""
        assert Compactor(max_context_tokens=10).should_compact([_msg("a" * 4000)]) is True

    def test_threshold_is_85_percent(self):
        c = Compactor(max_context_tokens=1000)
        assert c.should_compact([_msg("a" * 3400)]) is False   # 850 tokens = 阈值，不越界
        assert c.should_compact([_msg("a" * 3404)]) is True    # 851 > 850


# ============================================================================
# 3. EventStore.get_last_usage_anchor
# ============================================================================


def _fetch_anchor(store: EventStore, session_id: str) -> dict:
    """取锚点并当场断言非 None——Optional 不该渗进后面每一条下标断言。"""
    row = _run(store.get_last_usage_anchor(session_id))
    assert row is not None, f"会话 {session_id} 应有 usage 锚点"
    return row


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_anchor.db"))
    store.init()
    return store


class _FixedTime:
    """把 event_store 模块里的 `time` 换成固定读数（其余属性透传真模块）。

    只替 `.time()`，别的照旧——append_compaction 那条路径以后若用到 time 的其它
    API，透传能保证这个桩不会莫名其妙把测试弄崩。
    """

    def __init__(self, value: float):
        import time as _real_time

        self._value = value
        self._real = _real_time

    def time(self) -> float:
        return self._value

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class TestUsageAnchorQuery:
    def test_none_when_session_never_called_llm(self, tmp_path):
        store = _new_store(tmp_path)
        assert _run(store.get_last_usage_anchor("sess-empty")) is None

    def test_returns_latest_usage_row(self, tmp_path):
        store = _new_store(tmp_path)
        sid = "sess-1"

        async def seed():
            await store.append_usage(sid, seq=1, prompt_tokens=1000,
                                     completion_tokens=5, total_tokens=1005, model="m-a")
            await store.append_usage(sid, seq=2, prompt_tokens=48000,
                                     completion_tokens=9, total_tokens=48009, model="m-a")

        _run(seed())
        anchor = _fetch_anchor(store, sid)
        assert anchor is not None
        assert anchor["prompt_tokens"] == 48000
        assert anchor["model"] == "m-a"
        assert anchor["last_compaction_at"] == 0.0
        # 组装成 UsageAnchor 后必须可用（字段名对得上是这里的真风险）
        ua = UsageAnchor(
            prompt_tokens=int(anchor["prompt_tokens"]),
            created_at=float(anchor["created_at"]),
            last_compaction_at=float(anchor["last_compaction_at"]),
        )
        assert ua.usable is True

    def test_seq_is_iterations_so_created_at_ordering_matters(self, tmp_path):
        """usage.seq = runner 迭代轮数，每起一次新 run 从 1 重计 ⇒ 只能按 created_at 取最新。"""
        store = _new_store(tmp_path)
        sid = "sess-2"

        async def seed():
            # 上一轮 run 的第 5 次迭代（真实大上下文）
            await store.append_usage(sid, seq=5, prompt_tokens=70000,
                                     completion_tokens=1, total_tokens=70001, model="m-a")
            # 新 run 的第 1 次迭代（seq 更小但时间更晚）
            await store.append_usage(sid, seq=1, prompt_tokens=300,
                                     completion_tokens=1, total_tokens=301, model="m-a")

        _run(seed())
        anchor = _fetch_anchor(store, sid)
        assert anchor["prompt_tokens"] == 300

    def test_compaction_newer_than_the_anchor_invalidates_it(self, tmp_path, monkeypatch):
        """压缩写在锚点之后 ⇒ 这个锚点算的是"压缩前那份上下文"，必须作废。

        时间戳用 monkeypatch 撑开一小时：usage 与 compaction 都取 `time.time()`，
        同一次调用里连着写会拿到同一个浮点值，`>` 判不出来——那种"相等"是本机
        时钟粒度，不是这里要测的语义。
        """
        from lib.agent_runtime import event_store as es_module

        store = _new_store(tmp_path)
        sid = "sess-3"

        async def seed_usage():
            await store.append_usage(sid, seq=1, prompt_tokens=60000,
                                     completion_tokens=1, total_tokens=60001, model="m-a")

        _run(seed_usage())
        anchor_before = _fetch_anchor(store, sid)
        assert float(anchor_before["last_compaction_at"]) == 0.0

        future = float(anchor_before["created_at"]) + 3600.0
        monkeypatch.setattr(es_module, "time", _FixedTime(future))

        async def seed_compaction():
            await store.append_compaction(
                sid, source_seq=2, summary="s", recent_json="[]", prompt_hash="h",
            )

        _run(seed_compaction())
        anchor = _fetch_anchor(store, sid)
        assert float(anchor["last_compaction_at"]) == future
        ua = UsageAnchor(
            prompt_tokens=int(anchor["prompt_tokens"]),
            created_at=float(anchor["created_at"]),
            last_compaction_at=float(anchor["last_compaction_at"]),
        )
        assert ua.invalidated_by_compaction is True
        # 作废之后判据退回纯字符估算（不许再拿 60000 那个大数去判压缩）
        est = Compactor(max_context_tokens=100_000).estimate_context_tokens(
            [_msg("a" * 400)], ua,
        )
        assert est.basis == "chars_only"


# ============================================================================
# 4. runner 真的在每轮读锚点（判据写得再对，没人读就等于没修）
# ============================================================================


class TestRunnerReadsTheAnchor:
    def test_estimate_line_uses_the_stored_anchor(self, tmp_path, caplog):
        from lib.agent_runtime import MockLLM, RunnerConfig, RunnerDeps, SessionRunner

        store = _new_store(tmp_path)
        sid = "sess-run"
        try:
            async def scenario():
                await store.create_session(session_id=sid)
                # 先留一条"上一轮 run 的真实 usage"，再追加本轮新增消息
                await store.append_usage(sid, seq=1, prompt_tokens=42000,
                                         completion_tokens=2, total_tokens=42002, model="m-a")
                await store.append_message(
                    sid, Message(role="user", content="x" * 400, source="user"),
                )
                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=MockLLM(default_text="ok"),
                        event_store=store,
                        compactor=Compactor(max_context_tokens=60_000),
                    ),
                    config=RunnerConfig(session_id=sid, use_stream=False),
                )
                with caplog.at_level("INFO", logger="parcae.agent.runner"):
                    return await runner.run()

            _run(scenario())
        finally:
            store.close()

        lines = [r.getMessage() for r in caplog.records if "context estimate" in r.getMessage()]
        assert lines, "runner 每轮必须留一行上下文估算"
        first = lines[0]
        assert "basis=usage_anchor+delta" in first, first
        assert "anchor=42000" in first, first
        assert "delta=100" in first, first   # 新增那条 400 字符消息 = 100 token

    def test_no_compactor_injected_still_runs(self, tmp_path, caplog):
        """观测点不许把主循环带崩：没注入 compactor 时静默跳过。"""
        from lib.agent_runtime import MockLLM, RunnerConfig, RunnerDeps, SessionRunner

        store = _new_store(tmp_path)
        sid = "sess-nocomp"

        async def scenario():
            await store.create_session(session_id=sid)
            await store.append_message(sid, Message(role="user", content="hi", source="user"))
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=MockLLM(default_text="ok"), event_store=store),
                config=RunnerConfig(session_id=sid, use_stream=False),
            )
            return await runner.run()

        outcome = _run(scenario())
        store.close()
        assert outcome.status == "completed"
        assert not [r for r in caplog.records if "context estimate" in r.getMessage()]

    def test_anchor_query_failure_degrades_to_warning(self, tmp_path, caplog, monkeypatch):
        """usage 表读失败（锁住/坏库）只降级成一条 warning，主循环照跑。"""
        from lib.agent_runtime import MockLLM, RunnerConfig, RunnerDeps, SessionRunner

        store = _new_store(tmp_path)
        sid = "sess-badanchor"

        async def boom(_session_id):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(store, "get_last_usage_anchor", boom)

        async def scenario():
            await store.create_session(session_id=sid)
            await store.append_message(sid, Message(role="user", content="hi", source="user"))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="ok"),
                    event_store=store,
                    compactor=Compactor(max_context_tokens=60_000),
                ),
                config=RunnerConfig(session_id=sid, use_stream=False),
            )
            with caplog.at_level("INFO", logger="parcae.agent.runner"):
                return await runner.run()

        outcome = _run(scenario())
        store.close()
        assert outcome.status == "completed"
        texts = [r.getMessage() for r in caplog.records]
        assert any("anchor load failed" in t for t in texts), texts
        # 关键：读失败时这一轮不产出估算行（更不许把主循环打断）
        assert not any("context estimate: session=" in t for t in texts), texts


# ============================================================================
# 5. 每模型上限：面板侧取数
# ============================================================================


class TestPickContextWindow:
    def test_missing_field_is_unknown_zero(self):
        models = [{"model": "m-a", "tier": 3, "provider": "p"}]
        assert ChatPanel.pick_context_window(models, "m-a") == 0

    def test_empty_model_selection_is_unknown(self):
        models = [{"model": "m-a", "context_window": 128000}]
        assert ChatPanel.pick_context_window(models, "") == 0

    def test_same_model_two_providers_takes_the_smaller_window(self):
        """窗口是"别越界"的约束：两家给的数不一样时取大的会骗人。"""
        models = [
            {"model": "m-a", "provider": "big", "context_window": 200000},
            {"model": "m-a", "provider": "small", "context_window": 32768},
            {"model": "m-b", "provider": "other", "context_window": 8192},
        ]
        assert ChatPanel.pick_context_window(models, "m-a") == 32768

    def test_zero_entries_do_not_mask_a_real_window(self):
        models = [
            {"model": "m-a", "provider": "unfilled", "context_window": 0},
            {"model": "m-a", "provider": "filled", "context_window": 65536},
        ]
        assert ChatPanel.pick_context_window(models, "m-a") == 65536

    def test_absent_model_returns_zero(self):
        assert ChatPanel.pick_context_window([{"model": "m-a", "context_window": 1}], "zz") == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
