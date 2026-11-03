"""T5 验收：runtime HTTP/WS 客户端接缝。

一律从 HTTP/WS 进（不 import runner 内部）；LLM 用 MockLLM/SlowLLM 注入，
不连 8766、不真调模型。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lib.agent_runtime.builtin_tool_executor import BuiltinToolExecutor
from lib.agent_runtime.event_store import EventStore
from lib.agent_runtime.mock_llm import MockLLM
from lib.agent_runtime.mock_tool_executor import MockToolExecutor
from lib.agent_runtime.runtime_manager import RuntimeManager, RuntimeSettings
from lib.agent_runtime.server import create_app
from lib.agent_runtime.types import LLMResponse, RunnerDeps

TERMINAL = {"completed", "interrupted", "failed"}
ASK_QUESTIONS = [
    {
        "question": "走哪条路？",
        "header": "路线",
        "options": [{"label": "A", "description": "快"}, {"label": "B"}],
        "multiSelect": False,
    }
]


class SlowLLM:
    """可控慢速网关：用于并发/中断测试。第一次可返回 tool_calls。"""

    def __init__(self, *, delay: float = 0.6, tool_calls: list[dict] | None = None):
        self.delay = delay
        self.tool_calls = tool_calls or []
        self.call_count = 0

    async def call(self, request):
        self.call_count += 1
        await asyncio.sleep(self.delay)
        if self.call_count == 1 and self.tool_calls:
            return LLMResponse(tool_calls=self.tool_calls, stop_reason="tool_use")
        return LLMResponse(content="slow done", stop_reason="end_turn")

    async def stream(self, request):
        self.call_count += 1
        await asyncio.sleep(self.delay)
        if self.call_count == 1 and self.tool_calls:
            for tc in self.tool_calls:
                yield {"type": "tool_call_delta", "tool_call": tc}
            yield {"type": "done", "finish_reason": "tool_calls"}
            return
        yield {"type": "text_delta", "delta": "slow done"}
        yield {"type": "usage", "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        yield {"type": "done", "finish_reason": "stop"}


def _ask_tool_call() -> dict:
    return {
        "id": "call_ask_1",
        "type": "function",
        "function": {
            "name": "ask_user",
            "arguments": json.dumps({"questions": ASK_QUESTIONS}, ensure_ascii=False),
        },
    }


@pytest.fixture()
def runtime(tmp_path: Path) -> Iterator[tuple[TestClient, EventStore, dict]]:
    store = EventStore(db_path=str(tmp_path / "agent.db"))
    store.init()
    holder: dict = {"llm": MockLLM(default_text="ok")}

    async def deps_factory(interrupt_event, provider) -> RunnerDeps:
        builtin = BuiltinToolExecutor(ask_callback=provider.ask)
        return RunnerDeps(
            llm_gateway=holder["llm"],
            event_store=store,
            tool_executor=MockToolExecutor(),
            builtin_executor=builtin,
            interrupt_event=interrupt_event,
        )

    manager = RuntimeManager(
        store=store,
        settings=RuntimeSettings(ask_user_timeout_secs=5.0),
        deps_factory=deps_factory,
    )
    app = create_app(
        store=store,
        manager=manager,
        host="127.0.0.1",
        port=0,
        preferred_port=0,
        degraded=False,
        runtime_dir=tmp_path / "runtime",
        started_at=time.time(),
        api_base_url="http://127.0.0.1:8766",
    )
    with TestClient(app) as client:
        yield client, store, holder
    store.close()


def _wait_status(client: TestClient, sid: str, *, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        r = client.get(f"/sessions/{sid}")
        if r.status_code == 200:
            last = r.json()["session"]["status"]
            if last in TERMINAL:
                return last
        time.sleep(0.03)
    raise AssertionError(f"session {sid} did not reach terminal (last={last!r})")


def _submit(client: TestClient, sid: str, text: str = "hi", **extra) -> dict:
    r = client.post(f"/sessions/{sid}/submit", json={"text": text, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_submit_events_and_terminal(runtime) -> None:
    client, store, _ = runtime
    sid = "sess-seam-1"
    body = _submit(client, sid, "hello")

    assert body["ok"] is True and body["status"] == "running"
    assert _wait_status(client, sid) == "completed"

    events = client.get(f"/sessions/{sid}/events").json()
    types = [e["type"] for e in events["events"]]
    assert "user_message_appended" in types
    assert "assistant_message_appended" in types
    assert "session_finalized" in types
    assert events["next_seq"] == events["events"][-1]["seq"]

    all_events = client.get(f"/sessions/{sid}/events", params={"include_live": "true"}).json()
    assert len(all_events["events"]) > len(events["events"])
    assert any(e["domain"] == "live" for e in all_events["events"])

    msgs = client.get(f"/sessions/{sid}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hello"

    stats = client.get(f"/sessions/{sid}/stats").json()["stats"]
    assert stats["message_count"] == 2
    assert stats["status"] == "completed"
    assert client.get(f"/sessions/{sid}").json()["session"]["title"] == "hello"

    outline = client.get(f"/sessions/{sid}/outline").json()["turns"]
    assert len(outline) == 1
    assert outline[0]["status"] == "completed"


def test_concurrent_submit_and_client_message_idempotency(runtime) -> None:
    client, _, holder = runtime
    holder["llm"] = SlowLLM(delay=0.6)
    sid = "sess-busy"

    first = _submit(client, sid, "one", client_message_id="cmid-1")
    assert first["deduplicated"] is False

    second = client.post(f"/sessions/{sid}/submit", json={"text": "two"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "session_busy"

    dup = client.post(
        f"/sessions/{sid}/submit",
        json={"text": "one", "client_message_id": "cmid-1"},
    )
    assert dup.status_code == 200
    assert dup.json()["deduplicated"] is True
    assert dup.json()["status"] == "running"

    assert _wait_status(client, sid) == "completed"
    assert holder["llm"].call_count == 1

    replay = client.post(
        f"/sessions/{sid}/submit",
        json={"text": "one", "client_message_id": "cmid-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["deduplicated"] is True
    assert replay.json()["status"] == "completed"
    assert holder["llm"].call_count == 1

    msgs = client.get(f"/sessions/{sid}/messages").json()["messages"]
    assert len([m for m in msgs if m["role"] == "user"]) == 1


def test_steer_and_interrupt(runtime) -> None:
    client, _, holder = runtime
    holder["llm"] = SlowLLM(delay=5.0)
    sid = "sess-interrupt"
    _submit(client, sid, "long job")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        r = client.get(f"/sessions/{sid}")
        if r.status_code == 200 and r.json()["session"]["status"] == "streaming":
            break
        time.sleep(0.03)

    steer = client.post(f"/sessions/{sid}/steer", json={"text": "换个方向"})
    assert steer.status_code == 200, steer.text
    msgs = client.get(f"/sessions/{sid}/messages").json()["messages"]
    assert any(m["source"] == "steer" and m["content"] == "换个方向" for m in msgs)

    stop = client.post(f"/sessions/{sid}/interrupt")
    assert stop.status_code == 200 and stop.json()["interrupted"] is True
    assert _wait_status(client, sid) == "interrupted"

    events = client.get(f"/sessions/{sid}/events").json()["events"]
    transitions = [e for e in events if e["type"] == "transition"]
    assert any(e["payload"].get("reason") == "user_interrupted" for e in transitions)

    idle = client.post(f"/sessions/{sid}/steer", json={"text": "太晚了"})
    assert idle.status_code == 409
    assert idle.json()["error"]["code"] == "session_not_running"


def test_ask_user_without_client_is_no_host(runtime) -> None:
    client, _, holder = runtime
    holder["llm"] = MockLLM(script=[
        LLMResponse(tool_calls=[_ask_tool_call()], stop_reason="tool_use"),
        LLMResponse(content="没人在，我自己定", stop_reason="end_turn"),
    ])
    sid = "sess-nohost"
    _submit(client, sid, "需要你拍板")
    assert _wait_status(client, sid) == "completed"

    events = client.get(f"/sessions/{sid}/events").json()["events"]
    assert not [e for e in events if e["type"] == "ask_user_request"]
    tool_msgs = [
        m for m in client.get(f"/sessions/{sid}/messages").json()["messages"]
        if m["role"] == "tool"
    ]
    assert tool_msgs and "no GUI callback" in json.dumps(tool_msgs[0]["content"], ensure_ascii=False)


def test_ask_user_with_ws_client_answers(runtime) -> None:
    client, _, holder = runtime
    holder["llm"] = MockLLM(script=[
        LLMResponse(tool_calls=[_ask_tool_call()], stop_reason="tool_use"),
        LLMResponse(content="按你选的走", stop_reason="end_turn"),
    ])
    sid = "sess-ask"
    with client.websocket_connect(f"/ws?session_id={sid}") as ws:
        ready = ws.receive_json()
        assert ready["kind"] == "ready"
        _submit(client, sid, "需要你拍板")

        rid = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            frame = ws.receive_json()
            if frame.get("kind") == "event" and frame["event"]["type"] == "ask_user_request":
                rid = frame["event"]["payload"]["rid"]
                break
        assert rid, "WS 没有收到 ask_user_request"

        answer = client.post(
            f"/ask_user/{rid}",
            json={
                "outcome": "answered",
                "answers": [{"question": ASK_QUESTIONS[0]["question"], "selected": ["A"]}],
            },
        )
        assert answer.status_code == 200 and answer.json()["resolved"] is True

        assert _wait_status(client, sid) == "completed"

    events = client.get(f"/sessions/{sid}/events").json()["events"]
    resolved = [e for e in events if e["type"] == "ask_user_resolved"]
    assert resolved and resolved[-1]["payload"]["outcome"] == "answered"
    tool_msgs = [
        m for m in client.get(f"/sessions/{sid}/messages").json()["messages"]
        if m["role"] == "tool"
    ]
    assert any("A" in json.dumps(m["content"], ensure_ascii=False) for m in tool_msgs)

    unknown = client.post("/ask_user/does-not-exist", json={"outcome": "skipped"})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"


def test_ws_reconnect_after_seq_has_no_gap_or_dup(runtime) -> None:
    client, _, _ = runtime
    sid = "sess-ws"
    _submit(client, sid, "第一轮")
    assert _wait_status(client, sid) == "completed"

    all_events = client.get(f"/sessions/{sid}/events", params={"include_live": "true"}).json()["events"]
    assert len(all_events) >= 4
    mid = all_events[len(all_events) // 2]["seq"]

    with client.websocket_connect(f"/ws?session_id={sid}&after_seq={mid}") as ws:
        assert ws.receive_json()["kind"] == "ready"
        expected = [e["seq"] for e in all_events if e["seq"] > mid]
        got: list[int] = []
        for _ in range(len(expected)):
            frame = ws.receive_json()
            assert frame.get("kind") == "event", frame
            got.append(frame["event"]["seq"])
        assert got == expected, "重连回放有缺口、重复或乱序"


def test_ws_uplink_submit_and_interrupt(runtime) -> None:
    client, _, holder = runtime
    holder["llm"] = SlowLLM(delay=5.0)
    sid = "sess-ws-up"

    with client.websocket_connect(f"/ws?session_id={sid}") as ws:
        assert ws.receive_json()["kind"] == "ready"
        ws.send_json({"op": "submit", "text": "从 WS 发起", "client_message_id": "ws-cmid"})
        ack = _recv_until(ws, lambda f: f.get("kind") == "ack" and f.get("op") == "submit")
        assert ack["data"]["status"] == "running"

        ws.send_json({"op": "ping"})
        pong = _recv_until(ws, lambda f: f.get("kind") == "ack" and f.get("op") == "ping")
        assert pong["data"]["pong"] is True

        ws.send_json({"op": "interrupt"})
        stopped = _recv_until(ws, lambda f: f.get("kind") == "ack" and f.get("op") == "interrupt")
        assert stopped["data"]["interrupted"] is True

    assert _wait_status(client, sid) == "interrupted"
    msgs = client.get(f"/sessions/{sid}/messages").json()["messages"]
    assert msgs[0]["content"] == "从 WS 发起"


def _recv_until(ws, predicate, *, limit: int = 500) -> dict:
    for _ in range(limit):
        frame = ws.receive_json()
        if predicate(frame):
            return frame
    raise AssertionError("expected WS frame did not arrive")


def test_session_management_endpoints(runtime) -> None:
    client, _, _ = runtime
    sid = "sess-manage"
    _submit(client, sid, "第一轮")
    assert _wait_status(client, sid) == "completed"

    patch = client.patch(
        f"/sessions/{sid}",
        json={"title": "改过的标题", "pinned": True, "group_name": "实验"},
    )
    assert patch.status_code == 200, patch.text
    session = patch.json()["session"]
    assert session["title"] == "改过的标题"
    assert session["pinned"] is True
    assert session["group_name"] == "实验"

    raw_events = client.get(f"/sessions/{sid}/events").json()["events"]
    upto = raw_events[0]["seq"]
    branch = client.post(f"/sessions/{sid}/branch", json={"upto_seq": upto, "title": "分支"})
    assert branch.status_code == 200, branch.text
    new_sid = branch.json()["session_id"]
    branched = client.get(f"/sessions/{new_sid}/messages").json()["messages"]
    assert len(branched) == 1 and branched[0]["content"] == "第一轮"

    messages = client.get(f"/sessions/{sid}/messages").json()["messages"]
    victim = messages[-1]["id"]
    deleted = client.delete(f"/messages/{victim}")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert client.get(f"/sessions/{sid}/stats").json()["stats"]["message_count"] == 1

    assert client.delete(f"/messages/{victim}").status_code == 404
    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert client.get(f"/sessions/{sid}").status_code == 404
    assert client.delete(f"/sessions/{sid}").status_code == 404

    rebuilt = client.post("/admin/projections/rebuild", json={})
    assert rebuilt.status_code == 200
    assert rebuilt.json()["sessions"] >= 1
