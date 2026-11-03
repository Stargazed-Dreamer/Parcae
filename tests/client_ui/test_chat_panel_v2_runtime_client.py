"""T7：面板的 runtime 客户端接线（WS 事件 → 时间线 / ask_user 卡片 / 提交）。"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.agent_runtime.builtin_tool_executor import BuiltinToolExecutor  # noqa: E402
from lib.agent_runtime.event_store import EventStore  # noqa: E402
from lib.agent_runtime.mock_llm import MockLLM  # noqa: E402
from lib.agent_runtime.mock_tool_executor import MockToolExecutor  # noqa: E402
from lib.agent_runtime.runtime_manager import RuntimeManager, RuntimeSettings  # noqa: E402
from lib.agent_runtime.server import create_app  # noqa: E402
from lib.agent_runtime.types import RunnerDeps  # noqa: E402
from tests.client_ui._local_store import local_store_factory  # noqa: E402


def _make_panel(tmp_path, monkeypatch):
    import client.panels.chat as chat_module
    from client.panels.chat import ChatPanel

    db_path = str(tmp_path / "panel_runtime.db")
    monkeypatch.setattr(chat_module, "_make_read_store", local_store_factory(db_path))
    return ChatPanel(), db_path


def _cleanup(panel, qapp):
    panel._stop_event_stream()
    panel._session_list_timer.stop()
    panel._poll_timer.stop()
    panel.deleteLater()
    qapp.processEvents()


# ============================================================================
# WS 事件 → 时间线 / ask_user 卡片
# ============================================================================


class TestRuntimeEvents:
    def test_streaming_delta_renders_and_dedupes(self, qapp, tmp_path, monkeypatch):
        panel, _db = _make_panel(tmp_path, monkeypatch)
        try:
            panel._current_session_id = "s1"
            panel._on_runtime_event({
                "seq": 7, "session_id": "s1", "type": "streaming_text_delta",
                "payload": {"delta": "你好"}, "invalidated_seq": None,
            })
            assert panel._last_streaming_seq == 7
            assert panel._message_timeline.has_streaming_assistant()

            # 重复/更旧的回放帧不再追加
            panel._on_runtime_event({
                "seq": 7, "session_id": "s1", "type": "streaming_text_delta",
                "payload": {"delta": "你好"}, "invalidated_seq": None,
            })
            assert panel._last_streaming_seq == 7

            # 已 invalidated 的历史增量帧直接跳过
            panel._on_runtime_event({
                "seq": 9, "session_id": "s1", "type": "streaming_text_delta",
                "payload": {"delta": "旧"}, "invalidated_seq": 10,
            })
            assert panel._last_streaming_seq == 7
        finally:
            _cleanup(panel, qapp)

    def test_ask_user_request_renders_card_and_answers(self, qapp, tmp_path, monkeypatch):
        panel, _db = _make_panel(tmp_path, monkeypatch)
        posted = []
        monkeypatch.setattr(
            panel, "_post_ask_user_answer",
            lambda rid, outcome, answers: posted.append((rid, outcome, answers)),
        )
        try:
            panel._current_session_id = "s1"
            panel._session_status = "streaming"
            panel._on_runtime_event({
                "seq": 1, "session_id": "s1", "type": "ask_user_request",
                "payload": {"rid": "r1", "questions": [
                    {"question": "选哪个？", "options": [{"label": "A"}, {"label": "B"}], "multi": False},
                ]},
                "invalidated_seq": None,
            })
            assert "r1" in panel._ask_cards

            assert panel._answer_ask_user("r1", "answered", [{"question": "选哪个？", "selected": ["A"]}])
            assert "r1" not in panel._ask_cards
            # 第二次回答同一 rid 必须被拒（卡片已结束）
            assert panel._answer_ask_user("r1", "answered", []) is False

            deadline = time.monotonic() + 2
            while not posted and time.monotonic() < deadline:
                time.sleep(0.01)
            assert posted == [("r1", "answered", [{"question": "选哪个？", "selected": ["A"]}])]

            # runtime 侧结束事件（超时/中断）→ 卡片作废
            panel._on_runtime_event({
                "seq": 2, "session_id": "s1", "type": "ask_user_resolved",
                "payload": {"rid": "r2", "outcome": "timeout"},
                "invalidated_seq": None,
            })
            assert "r2" not in panel._ask_cards
        finally:
            _cleanup(panel, qapp)

    def test_ask_user_replay_ignored_when_session_not_running(self, qapp, tmp_path, monkeypatch):
        """WS 回放的历史 ask_user_request 不应长出无人能答的卡片。"""
        panel, _db = _make_panel(tmp_path, monkeypatch)
        try:
            panel._current_session_id = "s1"
            panel._session_status = "completed"
            panel._on_runtime_event({
                "seq": 1, "session_id": "s1", "type": "ask_user_request",
                "payload": {"rid": "old", "questions": [{"question": "旧问题"}]},
                "invalidated_seq": None,
            })
            assert "old" not in panel._ask_cards
            assert panel._message_timeline._layout.count() == 1  # 只有 stretch
        finally:
            _cleanup(panel, qapp)

    def test_event_stream_not_started_for_unknown_session(self, qapp, tmp_path, monkeypatch):
        panel, _db = _make_panel(tmp_path, monkeypatch)
        try:
            panel._start_event_stream("no-such-session")
            assert panel._event_stream is None
        finally:
            _cleanup(panel, qapp)

    def test_start_new_run_passes_model_context_window(self, qapp, tmp_path, monkeypatch):
        """面板按所选 model 查到的上下文窗口要随 submit 传给 runtime。"""
        import client.panels.chat as chat_module

        panel, _db = _make_panel(tmp_path, monkeypatch)
        recorded = {}

        class _FakeSignal:
            def connect(self, _slot):
                return None

        class _FakeWorker:
            finished_run = _FakeSignal()
            error = _FakeSignal()
            session_started = _FakeSignal()

            def __init__(self, **kwargs):
                recorded.update(kwargs)

            def start(self):
                return None

        monkeypatch.setattr(chat_module, "_RuntimeRunWorker", _FakeWorker)
        monkeypatch.setattr(panel, "_start_event_stream", lambda *a, **k: None)
        try:
            panel._available_models = [{"model": "m1", "context_window": 64000}]
            panel._current_model = "m1"
            panel._start_new_run("hello")
            assert recorded["context_window"] == 64000
            assert recorded["model_override"] == "m1"
        finally:
            _cleanup(panel, qapp)


# ============================================================================
# 提交 + WS 端到端（真 runtime + MockLLM）
# ============================================================================


async def _deps_factory(store, interrupt_event, provider) -> RunnerDeps:
    return RunnerDeps(
        llm_gateway=MockLLM(default_text="runtime says hi"),
        event_store=store,
        tool_executor=MockToolExecutor(),
        builtin_executor=BuiltinToolExecutor(ask_callback=provider.ask),
        interrupt_event=interrupt_event,
    )


@contextmanager
def _runtime_with_llm(db_path: str) -> Iterator[str]:
    store = EventStore(db_path=db_path)
    store.init()
    manager = RuntimeManager(
        store=store,
        settings=RuntimeSettings(ask_user_timeout_secs=5.0),
        deps_factory=lambda ie, p: _deps_factory(store, ie, p),
    )
    app = create_app(
        store=store,
        manager=manager,
        host="127.0.0.1",
        port=0,
        preferred_port=0,
        degraded=False,
        runtime_dir=Path(db_path).parent / f"rt-{time.time_ns()}",
        started_at=time.time(),
        api_base_url="http://127.0.0.1:8766",
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "runtime did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class TestRunWorkerAgainstRuntime:
    def test_worker_submits_and_reaches_terminal(self, qapp, tmp_path):
        from client.panels.chat import _RuntimeRunWorker

        db_path = str(tmp_path / "worker_e2e.db")
        with _runtime_with_llm(db_path) as base_url:
            worker = _RuntimeRunWorker(
                session_id="sess-t7",
                user_message="hello",
                runtime_url=base_url,
                config={"max_iterations": 3},
            )
            outcomes: list = []
            errors: list = []
            started: list = []
            worker.finished_run.connect(outcomes.append)
            worker.error.connect(errors.append)
            worker.session_started.connect(started.append)
            worker.start()
            assert worker.wait(15000), "worker did not finish"
            for _ in range(5):
                qapp.processEvents()

            assert errors == []
            assert started == ["sess-t7"]
            assert len(outcomes) == 1
            assert outcomes[0].status == "completed"

        store = EventStore(db_path=db_path)
        store.init()
        try:
            messages = _run(store.load_messages("sess-t7"))
            assert any(
                m.role == "assistant" and "runtime says hi" in str(m.content)
                for m in messages
            )
        finally:
            store.close()

    def test_event_stream_receives_runtime_events(self, qapp, tmp_path):
        from client.panels.chat import _RuntimeEventStream
        from lib.agent_runtime.runtime_client import RuntimeClient

        db_path = str(tmp_path / "stream_e2e.db")
        with _runtime_with_llm(db_path) as base_url:
            events: list = []
            stream = _RuntimeEventStream("sess-stream", runtime_url=base_url)
            stream.event_received.connect(events.append)
            stream.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    qapp.processEvents()
                    time.sleep(0.01)
                _run(RuntimeClient(base_url).submit("sess-stream", "hi", mode="dialogue"))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    qapp.processEvents()
                    if any(e.get("type") == "session_finalized" for e in events):
                        break
                    time.sleep(0.01)
            finally:
                stream.stop()
                stream.wait(3000)

        types = {e.get("type") for e in events}
        assert "user_message_appended" in types
        assert "session_finalized" in types


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
