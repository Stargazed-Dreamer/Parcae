"""T6 验收：headless / judge / probe 全部走 runtime HTTP。

真 uvicorn + 真 HTTP（不 mock RuntimeClient），LLM 用 MockLLM/SlowLLM 注入：
1. headless 作业跑完无人调端点也能取到结果（submit → 等终态 → 读消息/账本）
2. 中断在 runtime 生效：后端 `/headless/sessions/{sid}/interrupt` 转发后作业提前结束
3. judge 提交同样走 runtime
4. probe 引擎在 runtime 里跑，8766 只做代理
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.agent_runtime.builtin_tool_executor import BuiltinToolExecutor
from lib.agent_runtime.event_store import EventStore
from lib.agent_runtime.mock_llm import MockLLM
from lib.agent_runtime.mock_tool_executor import MockToolExecutor
from lib.agent_runtime.runtime_client import RuntimeCallError, RuntimeClient
from lib.agent_runtime.runtime_manager import RuntimeManager, RuntimeSettings
from lib.agent_runtime.server import create_app
from lib.agent_runtime.types import LLMResponse, RunnerDeps
from server.activity_tracker import headless_endpoints
from server.activity_tracker.headless_endpoints import router as headless_router
from server.activity_tracker.headless_runner import (
    HeadlessConfig,
    run_judge_agent,
    run_main_agent,
)


class SlowLLM:
    """慢速网关：让会话保持在跑，用于验证中断真的进了 runtime。"""

    def __init__(self, delay: float = 10.0):
        self.delay = delay
        self.calls = 0

    async def call(self, request):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return LLMResponse(content="slow done", stop_reason="end_turn")

    async def stream(self, request):
        self.calls += 1
        await asyncio.sleep(self.delay)
        yield {"type": "text_delta", "delta": "slow done"}
        yield {"type": "done", "finish_reason": "stop"}


class _RuntimeServer:
    def __init__(self, app, port: int, thread: threading.Thread, server: uvicorn.Server):
        self.app = app
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = thread
        self._server = server

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture()
def runtime_server(tmp_path: Path):
    store = EventStore(db_path=str(tmp_path / "agent.db"))
    store.init()
    holder: dict = {"llm": MockLLM(default_text="hello from runtime")}

    async def deps_factory(interrupt_event, provider) -> RunnerDeps:
        return RunnerDeps(
            llm_gateway=holder["llm"],
            event_store=store,
            tool_executor=MockToolExecutor(),
            builtin_executor=BuiltinToolExecutor(ask_callback=provider.ask),
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
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="t6-runtime")
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "runtime uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    handle = _RuntimeServer(app, port, thread, server)
    try:
        yield handle, holder
    finally:
        handle.stop()
        store.close()


async def _wait_running(client: RuntimeClient, sid: str, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            session = await client.get_session(sid)
        except RuntimeCallError as exc:
            if exc.status_code != 404:
                raise
            session = None
        if session:
            last = str(session.get("status") or "")
            if last in {"streaming", "awaiting_tools", "running"}:
                return
        await asyncio.sleep(0.05)
    raise AssertionError(f"session {sid} never started (last status={last!r})")


def test_headless_job_completes_without_endpoint_polling(runtime_server) -> None:
    """作业提交后自己跑完；后端只从 runtime 读回终态/消息，无任何回调端点。"""
    handle, _holder = runtime_server
    config = HeadlessConfig(runtime_url=handle.base_url, max_iterations=3, wall_clock_budget_secs=30)

    async def scenario():
        return await run_main_agent(
            "hello runtime", config, session_id="headless-t6-ok"
        )

    result = asyncio.run(scenario())
    assert result.error == ""
    assert result.outcome is not None
    assert result.outcome.status == "completed"
    assert result.last_assistant_messages == ["hello from runtime"]
    assert result.execution_evidence.get("total") == 0


def test_interrupt_endpoint_reaches_runtime(runtime_server) -> None:
    """中断经 8766 端点转发到 runtime：慢 LLM 被提前打断，终态 interrupted。"""
    handle, holder = runtime_server
    holder["llm"] = SlowLLM(delay=10.0)
    sid = "headless-t6-interrupt"
    config = HeadlessConfig(runtime_url=handle.base_url, max_iterations=3, wall_clock_budget_secs=60)

    app = FastAPI()
    app.include_router(headless_router)
    backend = TestClient(app)

    async def scenario():
        client = RuntimeClient(base_url=handle.base_url)
        task = asyncio.create_task(
            run_main_agent("slow task", config, session_id=sid, client=client)
        )
        await _wait_running(client, sid)
        started = time.monotonic()
        resp = backend.post(f"/headless/sessions/{sid}/interrupt")
        assert resp.status_code == 200, resp.text
        assert resp.json()["interrupted"] is True
        result = await asyncio.wait_for(task, timeout=15)
        return result, time.monotonic() - started

    # 8766 端点里的客户端要走真 HTTP（不是替身）
    original_factory = headless_endpoints._runtime_client
    headless_endpoints._runtime_client = lambda: RuntimeClient(base_url=handle.base_url)
    try:
        result, elapsed = asyncio.run(scenario())
    finally:
        headless_endpoints._runtime_client = original_factory

    assert result.outcome is not None
    assert result.outcome.status == "interrupted"
    # 慢 LLM 要 10s；中断后应显著提前返回
    assert elapsed < 5.0, f"interrupt did not cut the run short: {elapsed:.1f}s"


def test_judge_submits_through_runtime(runtime_server) -> None:
    """judge 会话也走 runtime submit，结果从 runtime 读回。"""
    handle, holder = runtime_server
    holder["llm"] = MockLLM(default_text='{"verdict": "success", "reason": "ok"}')
    config = HeadlessConfig(runtime_url=handle.base_url, judge_max_iterations=2, judge_wall_clock_budget_secs=30)

    async def scenario():
        return await run_judge_agent(
            ["main output"], "judge task", config, session_id="headless-judge-t6"
        )

    result = asyncio.run(scenario())
    assert result.verdict == "success"
    assert result.reason == "ok"
    assert result.session_id == "headless-judge-t6"


def test_probe_runs_inside_runtime(runtime_server, tmp_path) -> None:
    """probe 引擎在 runtime 进程里跑；HTTP 返回完整摘要。"""
    handle, _holder = runtime_server
    client = RuntimeClient(base_url=handle.base_url)
    params = {
        "trigger_text": "hello",
        "mode": "script",
        "script": ["hi from probe"],
        "use_stream": False,
        "trigger_approval": True,  # 跳过 8766 工具目录刷新，保持测试隔离
        "auto_cleanup": False,
        "db_path": str(tmp_path / "probe_t6.db"),
        "persist": False,
        "include_full": True,
    }

    summary = asyncio.run(client.probe(params))
    assert summary["session_id"].startswith("probe-")
    assert summary["status"] == "completed"
    assert summary["messages_count"] >= 2
    assert summary["run_dir"] == ""
    assert any(
        m["role"] == "assistant" and "hi from probe" in m.get("content", "")
        for m in summary["messages_preview"]
    )
