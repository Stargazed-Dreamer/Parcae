"""T7：桌面客户端的 runtime 适配层（RuntimeStore）契约测试。

真 uvicorn + 真 HTTP；RuntimeStore 是面板生产路径的唯一读/写出口。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from client.core.runtime_store import RuntimeStore
from lib.agent_runtime.event_store import EventStore
from lib.agent_runtime.runtime_manager import RuntimeManager, RuntimeSettings
from lib.agent_runtime.server import create_app
from lib.agent_runtime.types import Message, ToolCall


async def _no_deps(*_args, **_kwargs):
    raise RuntimeError("deps_factory is not used by runtime-store contract tests")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@contextmanager
def _runtime(store: EventStore, db_path: str) -> Iterator[str]:
    manager = RuntimeManager(
        store=store,
        settings=RuntimeSettings(),
        deps_factory=_no_deps,
    )
    app = create_app(
        store=store,
        manager=manager,
        host="127.0.0.1",
        port=0,
        preferred_port=0,
        degraded=False,
        runtime_dir=Path(db_path).parent / f"runtime-{time.time_ns()}",
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
        assert not thread.is_alive()


@pytest.fixture()
def runtime_store(tmp_path):
    db_path = str(tmp_path / "agent.db")
    store = EventStore(db_path=db_path)
    store.init()
    run(store.create_session("s1", title="会话一", mode="dialogue"))
    with _runtime(store, db_path) as base_url:
        yield RuntimeStore(base_url), store
    store.close()


def test_session_metadata_writes_go_through_api(runtime_store):
    client, _store = runtime_store
    run(client.update_session_title("s1", "新标题"))
    run(client.update_session_pinned("s1", True))
    run(client.update_session_group("s1", "工作"))

    session = run(client.get_session("s1"))
    assert session is not None
    assert (session.title, session.pinned, session.group_name) == ("新标题", True, "工作")
    assert run(client.list_distinct_group_names()) == ["工作"]

    run(client.update_session_group("s1", None))
    assert run(client.get_session("s1")).group_name is None


def test_branch_and_delete_session(runtime_store):
    client, store = runtime_store
    run(store.append_message("s1", Message(role="user", content="hi", seq=1)))

    new_id = run(client.branch_session("s1", upto_seq=2**31 - 1, title="分支"))
    assert new_id and new_id != "s1"
    assert run(client.get_session(new_id)) is not None

    assert run(client.delete_session(new_id)) is True
    assert run(client.get_session(new_id)) is None

    assert run(client.delete_message_by_id("no-such-message")) is False


def test_messages_include_invisible_and_tool_calls(runtime_store):
    client, store = runtime_store
    run(store.append_message("s1", Message(role="user", content="visible", seq=1)))
    run(store.append_message(
        "s1", Message(role="synthetic", content="hidden", source="synthetic",
                      visible=False, seq=2),
    ))
    run(store.append_tool_call("s1", ToolCall(id="call_1", seq=3, name="grep")))
    run(store.update_tool_call_status("call_1", "completed"))

    visible = run(client.load_messages("s1"))
    everything = run(client.load_messages("s1", include_invisible=True))
    assert [m.content for m in visible] == ["visible"]
    assert [m.content for m in everything] == ["visible", "hidden"]

    calls = run(client.load_tool_calls("s1"))
    assert [(c.id, c.name, c.status) for c in calls] == [("call_1", "grep", "completed")]

    events = run(client.load_events("s1"))
    assert events and all(e.session_id == "s1" for e in events)


def test_queue_roundtrip(runtime_store):
    client, _store = runtime_store
    assert run(client.find_pending_queue_messages()) == []

    first = run(client.append_message("s1", Message(
        role="user", content="queued one", source="queue", visible=False,
    )))
    second = run(client.append_message("s1", Message(
        role="user", content="queued two", source="queue", visible=False,
    )))
    assert first.id and second.id and first.id != second.id

    pending = run(client.find_pending_queue_messages())
    assert [(m.id, str(m.content), m.source, m.visible) for m in pending] == [
        (first.id, "queued one", "queue", False),
        (second.id, "queued two", "queue", False),
    ]
    assert {m.session_id for m in pending} == {"s1"}

    assert run(client.delete_message_by_id(first.id)) is True
    assert [str(m.content) for m in run(client.find_pending_queue_messages())] == ["queued two"]


def test_runtime_client_submit_carries_context_window(monkeypatch):
    """提交载荷必须带 context_window（runtime 用它覆盖 Compactor 判据）。"""
    from lib.agent_runtime.runtime_client import RuntimeClient

    captured = {}

    async def fake_request(self, method, path, **kwargs):
        captured.update(method=method, path=path, kwargs=kwargs)
        return {"ok": True}

    monkeypatch.setattr(RuntimeClient, "_request", fake_request)
    run(RuntimeClient("http://127.0.0.1:1").submit(
        "s1", "hi", context_window=64000,
    ))
    assert captured["path"] == "/sessions/s1/submit"
    assert captured["kwargs"]["json"]["context_window"] == 64000


def test_context_window_overrides_compactor():
    """面板传来的模型窗口要覆盖 deps 里的 0（否则主动压缩退化成"上限未知"）。"""
    from lib.agent_runtime.compactor import Compactor
    from lib.agent_runtime.runtime_manager import apply_context_window
    from lib.agent_runtime.types import RunnerDeps

    deps = RunnerDeps(llm_gateway=object(), compactor=Compactor(max_context_tokens=0))
    overridden = apply_context_window(deps, 128000)
    assert overridden.compactor.max_context_tokens == 128000

    assert apply_context_window(deps, 0) is deps
    assert apply_context_window(deps, None) is deps


def test_recovery_report_is_runtime_startup_result(tmp_path):
    db_path = str(tmp_path / "recovery.db")
    seed = EventStore(db_path=db_path)
    seed.init()
    run(seed.create_session("crash-1", mode="dialogue"))
    run(seed.update_session_status("crash-1", "streaming"))
    seed.close()

    store = EventStore(db_path=db_path)
    store.init()
    with _runtime(store, db_path) as base_url:
        client = RuntimeStore(base_url)
        report = run(client.recovery_report())
        assert report["sessions_interrupted"] == ["crash-1"]
        assert run(client.get_session("crash-1")).status == "interrupted"
    store.close()
