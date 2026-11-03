"""headless-agent-session Ticket 07 验收测试：中断端点 + 中断编排

覆盖 Ticket 07 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 新增 POST /headless/sessions/{session_id}/interrupt 端点
- [x] 端点把中断转发给 runtime（butler-runtime T6 起不再有进程内 registry）
- [x] 中断主会话 → judge 不启动（HeadlessSessionAction 检测 outcome.status != completed）
- [x] 中断 judge 会话 → judge 降级 uncertain
- [x] 端到端测试：主会话 interrupted → judge 不启动 + 整体 failed
- [x] 端到端测试：judge 中断 → 降级 uncertain + 整体 failed

测试分组：
1. REST 端点单元测试（FastAPI TestClient + scripted runtime client）
2. HeadlessSessionAction 中断 E2E 测试
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fake_runtime import FakeRuntimeClient  # noqa: E402

from lib.agent_runtime.runtime_client import RuntimeUnavailable  # noqa: E402
from lib.agent_runtime.types import RunOutcome  # noqa: E402
from server.activity_tracker import headless_endpoints as he  # noqa: E402
from server.activity_tracker.headless_endpoints import (  # noqa: E402
    router,
)
from server.activity_tracker.headless_runner import (  # noqa: E402
    MainAgentResult,
)
from server.activity_tracker.loop_actions import HeadlessSessionAction  # noqa: E402

# ============================================================================
# 1. REST 端点单元测试（FastAPI TestClient）
# ============================================================================


@pytest.fixture
def fastapi_app():
    """构造只挂 headless router 的 FastAPI app（不启动完整后端）。"""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def fake_runtime_client() -> FakeRuntimeClient:
    return FakeRuntimeClient()


@pytest.fixture
def client(fastapi_app, fake_runtime_client, monkeypatch):
    """TestClient；端点里的 runtime 客户端换成替身。"""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(he, "_runtime_client", lambda: fake_runtime_client)
    return TestClient(fastapi_app)


class TestInterruptEndpoint:
    """POST /headless/sessions/{session_id}/interrupt 端点测试。"""

    def test_interrupt_existing_session(self, client, fake_runtime_client):
        """在跑的 session 调 interrupt → 200 + interrupted=True，转发到 runtime。"""
        resp = client.post("/headless/sessions/headless-test-001/interrupt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "headless-test-001"
        assert data["interrupted"] is True
        assert "interrupt signal sent" in data["message"]
        assert fake_runtime_client.interrupts == ["headless-test-001"]

    def test_interrupt_nonexistent_session_returns_404(self, client, fake_runtime_client):
        """不在跑的 session 调 interrupt → 404。"""
        fake_runtime_client.interrupt_result = False
        resp = client.post("/headless/sessions/headless-nonexistent/interrupt")
        assert resp.status_code == 404
        assert "not running" in resp.json()["detail"].lower()

    def test_interrupt_when_runtime_unavailable_returns_503(self, client, fake_runtime_client):
        """runtime 不可达 → 503（而不是假装中断成功）。"""
        fake_runtime_client.interrupt_error = RuntimeUnavailable("runtime down")
        resp = client.post("/headless/sessions/headless-test-002/interrupt")
        assert resp.status_code == 503
        assert "runtime unavailable" in resp.json()["detail"].lower()

    def test_interrupt_judge_session(self, client, fake_runtime_client):
        """judge session 调 interrupt → 200 + interrupted=True。"""
        resp = client.post("/headless/sessions/headless-judge-test-003/interrupt")
        assert resp.status_code == 200
        assert resp.json()["interrupted"] is True
        assert fake_runtime_client.interrupts == ["headless-judge-test-003"]

    def test_interrupt_after_unregister_returns_404(self, client, fake_runtime_client):
        """会话已结束（runtime 无在跑 turn）后调 interrupt → 404。"""
        fake_runtime_client.interrupt_result = False
        resp = client.post("/headless/sessions/headless-test-004/interrupt")
        assert resp.status_code == 404


# ============================================================================
# 2. HeadlessSessionAction 中断场景 E2E 测试
# ============================================================================


def _patch_memory_and_wip(monkeypatch, capture: dict):
    """patch memory manager + todos store。"""
    mock_mem_mgr = MagicMock()
    mock_mem_mgr.set = MagicMock(return_value={"status": "ok", "key": "test"})
    capture["memory_set"] = mock_mem_mgr.set
    import server.memory.manager as mem_pkg
    monkeypatch.setattr(mem_pkg, "get_memory_manager", lambda: mock_mem_mgr)

    mock_store = MagicMock()
    mock_store.create_wip = MagicMock(return_value={"id": "wip_interrupt_001", "status": "active"})
    mock_store.update_wip = MagicMock(return_value={"id": "wip_interrupt_001", "status": "failed"})
    capture["wip_create"] = mock_store.create_wip
    capture["wip_update"] = mock_store.update_wip

    import importlib
    todos_module = importlib.import_module("server.todos.router")
    monkeypatch.setattr(todos_module, "get_todos_store", lambda: mock_store)


def _patch_loop_manager_pause(monkeypatch, capture: dict):
    """patch loop_manager.pause_task。"""
    mock_mgr = MagicMock()
    mock_mgr.pause_task = MagicMock(return_value=True)
    capture["pause_task"] = mock_mgr.pause_task
    import server.activity_tracker.loop_manager as lm_pkg
    monkeypatch.setattr(lm_pkg, "get_manager", lambda: mock_mgr)


def _patch_run_main_agent_interrupted(monkeypatch, main_session_id: str):
    """patch run_main_agent 模拟主会话被中断：返回 outcome.status=interrupted。"""
    async def fake_run_main(trigger_text, config, **kwargs):
        return MainAgentResult(
            session_id=main_session_id,
            outcome=RunOutcome(
                session_id=main_session_id,
                status="interrupted",
                stop_reason="interrupted",
                iterations=1,
            ),
            last_assistant_messages=[],
            error="",
        )
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_main_agent", fake_run_main)


def _patch_run_judge_agent_called_flag(monkeypatch, judge_called: list):
    """patch run_judge_agent 并记录是否被调用（中断主会话时不应被调用）。"""
    async def fake_run_judge(main_output, trigger_text, config, **kwargs):
        judge_called.append(True)
        from server.activity_tracker.headless_runner import JudgeResult
        return JudgeResult(
            verdict="success",
            reason="should not be called",
            session_id="headless-judge-should-not-exist",
        )
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_judge_agent", fake_run_judge)


def test_e2e_main_session_interrupted_skips_judge(tmp_db_path, monkeypatch):
    """Ticket 07 E2E：主会话被中断 → judge 不启动 → 整体 failed。

    场景：
    1. run_main_agent 返回 outcome.status="interrupted"
    2. HeadlessSessionAction 应跳过 judge（不调 run_judge_agent）
    3. 整体 failed，judge_verdict="failed"（main agent 失败，非 uncertain）
    4. memory + wip 写入 status=failed
    """
    main_session_id = "headless-interrupt-test-main"
    _patch_run_main_agent_interrupted(monkeypatch, main_session_id)

    judge_called: list = []
    _patch_run_judge_agent_called_flag(monkeypatch, judge_called)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "测试中断场景",
            "one_shot": True,
            "max_iterations": 5,
            "max_budget_usd": 0.5,
            "wall_clock_budget_secs": 60,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()
    result = asyncio.run(action.execute(context))

    # 验证整体 failed
    assert result["success"] is False
    assert result["main_session_id"] == main_session_id
    assert result["judge_session_id"] == ""  # 没启动 judge
    assert result["judge_verdict"] == "failed"
    assert "main agent" in result["judge_reason"]
    assert "interrupted" in result["judge_reason"]

    # 验证 judge 没被调用
    assert len(judge_called) == 0, "主会话中断后不应调用 judge agent"

    # 验证 memory + wip 写入 status=failed
    mem_data = capture["memory_set"].call_args[0][1]
    assert mem_data["judge_verdict"] == "failed"
    update_data = capture["wip_update"].call_args[0][1]
    assert update_data["status"] == "failed"

    # 验证 one_shot 仍调 pause_task
    assert capture["pause_task"].call_count == 1


def _patch_run_judge_agent_interrupted(monkeypatch, judge_session_id: str):
    """patch run_judge_agent 模拟 judge 会话被中断：返回 verdict=uncertain。"""
    async def fake_run_judge(main_output, trigger_text, config, **kwargs):
        from server.activity_tracker.headless_runner import JudgeResult
        return JudgeResult(
            verdict="uncertain",
            reason="judge session not completed (status=interrupted)",
            session_id=judge_session_id,
            error="interrupted",
        )
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_judge_agent", fake_run_judge)


def _patch_run_main_agent_success(monkeypatch, main_session_id: str):
    """patch run_main_agent 模拟主会话成功完成。"""
    async def fake_run_main(trigger_text, config, **kwargs):
        return MainAgentResult(
            session_id=main_session_id,
            outcome=RunOutcome(
                session_id=main_session_id,
                status="completed",
                stop_reason="end_turn",
                iterations=1,
            ),
            last_assistant_messages=["主 agent 任务完成"],
            error="",
        )
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_main_agent", fake_run_main)


def test_e2e_judge_session_interrupted_degrades_to_uncertain(tmp_db_path, monkeypatch):
    """Ticket 07 E2E：judge 会话被中断 → 降级 uncertain → 整体 failed。

    场景：
    1. run_main_agent 成功完成（outcome.status="completed"）
    2. run_judge_agent 返回 verdict="uncertain"（模拟 judge 被中断）
    3. HeadlessSessionAction 检测 verdict != "success" → 整体 failed
    4. memory + wip 写入 status=failed
    """
    main_session_id = "headless-interrupt-test-main-2"
    judge_session_id = "headless-judge-interrupt-test"
    _patch_run_main_agent_success(monkeypatch, main_session_id)
    _patch_run_judge_agent_interrupted(monkeypatch, judge_session_id)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "测试 judge 中断场景",
            "one_shot": True,
            "max_iterations": 5,
            "max_budget_usd": 0.5,
            "wall_clock_budget_secs": 60,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()
    result = asyncio.run(action.execute(context))

    # 验证整体 failed（uncertain 算 failed）
    assert result["success"] is False
    assert result["main_session_id"] == main_session_id
    assert result["judge_session_id"] == judge_session_id
    assert result["judge_verdict"] == "uncertain"
    assert "interrupted" in result["judge_reason"]

    # 验证 memory + wip 写入 status=failed
    mem_data = capture["memory_set"].call_args[0][1]
    assert mem_data["judge_verdict"] == "uncertain"
    update_data = capture["wip_update"].call_args[0][1]
    assert update_data["status"] == "failed"

    # 验证 one_shot 仍调 pause_task
    assert capture["pause_task"].call_count == 1


