"""headless-agent-session Ticket 06 验收测试：端到端测试（一次性任务）

覆盖 Ticket 06 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 新建 tests/test_headless_session_e2e.py
- [x] 测试 fixture：临时 EventStore DB + mock LLM
- [x] 测试用例 1：配置 trigger_text="回复'headless test ok'即可，不要调任何工具"
- [x] 验证 task.last_result 含 judge_verdict 字段
- [x] 验证主会话 + judge 会话都创建（2 个 session_id）
- [x] 验证主会话 mode="headless"，judge 会话 mode="headless_judge"
- [x] 验证主会话 assistant 消息含 "headless test ok"
- [x] 验证 judge 会话最后一条 assistant 消息含 JSON verdict
- [x] 验证 memory 记录含 main_session_id / judge_session_id / judge_verdict / judge_reason
- [x] 验证 wip 记录含 status（success→completed）
- [x] 测试用例 2：验证 client 进程不在线时后端仍能跑（action 不依赖 client 进程）

E2E 策略（butler-runtime T6 起）：
- scripted runtime client：main/judge 的终态与 assistant 消息由替身给出
- 真实 HeadlessSessionAction.execute()（完整编排：主 → judge → memory/wip）
- 断言提交载荷（mode / session_id）与编排结果，不依赖 8766 进程内的对话循环
- Mock memory manager + todos store（捕获写入，不真实写 memory.db）
- 直接调 action.execute()，不经 LoopManager 调度（避免等 cron 触发）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest  # noqa: E402  # pytestmark 需要

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fake_runtime import FakeRuntimeClient, assistant  # noqa: E402

from server.activity_tracker import headless_runner as hr  # noqa: E402
from server.activity_tracker.loop_actions import HeadlessSessionAction  # noqa: E402

pytestmark = pytest.mark.e2e  # E2E：依赖真实环境（CDP/后端/GUI），quick 层排除

# ============================================================================
# Fixtures 与辅助函数
# ============================================================================


def _patch_runtime(
    monkeypatch,
    *,
    main_text: str = "headless test ok",
    judge_text: str = '{"verdict": "success", "reason": "主 agent 输出含 headless test ok，任务完成"}',
) -> FakeRuntimeClient:
    """给主/judge 会话脚本化终态，替换 headless_runner 的 runtime 客户端。"""
    fake = FakeRuntimeClient()
    fake.on("headless", messages=[assistant(main_text)])
    fake.on("headless_judge", messages=[assistant(judge_text)])
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)
    return fake


def _patch_memory_and_wip(monkeypatch, capture: dict):
    """patch memory manager + todos store，捕获写入。"""
    # memory manager
    mock_mem_mgr = MagicMock()
    mock_mem_mgr.set = MagicMock(return_value={"status": "ok", "key": "test"})
    capture["memory_set"] = mock_mem_mgr.set
    import server.memory.manager as mem_pkg
    monkeypatch.setattr(mem_pkg, "get_memory_manager", lambda: mock_mem_mgr)

    # todos store
    mock_store = MagicMock()
    mock_store.create_wip = MagicMock(return_value={"id": "wip_e2e_001", "status": "active"})
    mock_store.update_wip = MagicMock(return_value={"id": "wip_e2e_001", "status": "completed"})
    capture["wip_create"] = mock_store.create_wip
    capture["wip_update"] = mock_store.update_wip

    import importlib
    todos_module = importlib.import_module("server.todos.router")
    monkeypatch.setattr(todos_module, "get_todos_store", lambda: mock_store)


def _patch_loop_manager_pause(monkeypatch, capture: dict):
    """patch loop_manager.pause_task，捕获调用。"""
    mock_mgr = MagicMock()
    mock_mgr.pause_task = MagicMock(return_value=True)
    capture["pause_task"] = mock_mgr.pause_task
    import server.activity_tracker.loop_manager as lm_pkg
    monkeypatch.setattr(lm_pkg, "get_manager", lambda: mock_mgr)


# ============================================================================
# 测试用例 1：完整 E2E 流程（success 路径）
# ============================================================================


def test_e2e_one_shot_success_full_flow(tmp_db_path, monkeypatch):
    """Ticket 06 E2E: 一次性 headless session 完整流程（success 路径）。

    流程：
    1. 配置 trigger_text="回复'headless test ok'即可，不要调任何工具"
    2. MockLLM 根据调用方返回不同响应（main / judge）
    3. 调 HeadlessSessionAction.execute()
    4. 验证：
       - 返回值 success=True
       - main_session_id + judge_session_id 都存在
       - 主会话 mode="headless"，judge 会话 mode="headless_judge"
       - 主会话 assistant 消息含 "headless test ok"
       - judge 会话最后一条 assistant 消息含 JSON verdict
       - memory 记录含 main_session_id / judge_session_id / judge_verdict / judge_reason
       - wip 记录含 status=completed
       - one_shot=True 时调 pause_task
    """
    fake = _patch_runtime(monkeypatch)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    # 配置一次性 headless session
    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "回复'headless test ok'即可，不要调任何工具",
            "one_shot": True,
            "max_iterations": 5,
            "max_budget_usd": 0.5,
            "wall_clock_budget_secs": 60,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()

    async def run():
        return await action.execute(context)

    result = asyncio.run(run())

    # ===== 验证返回值 =====
    assert result["success"] is True, f"expected success, got: {result}"
    assert "main_session_id" in result
    assert "judge_session_id" in result
    assert result["judge_verdict"] == "success"
    assert "headless test ok" in result["judge_reason"]

    main_session_id = result["main_session_id"]
    judge_session_id = result["judge_session_id"]
    assert main_session_id.startswith("headless-")
    assert judge_session_id.startswith("headless-judge-")
    assert main_session_id != judge_session_id

    # ===== 验证提交（主会话 / judge 会话各一次，mode 正确）=====
    assert len(fake.submits) == 2
    main_submit, judge_submit = fake.submits
    assert main_submit["session_id"] == main_session_id
    assert main_submit["mode"] == "headless"
    assert "headless test ok" in main_submit["text"]
    assert judge_submit["session_id"] == judge_session_id
    assert judge_submit["mode"] == "headless_judge"

    # ===== 验证主会话 assistant 消息含 "headless test ok" =====
    main_messages = fake.sessions[main_session_id].messages
    main_assistant_msgs = [m for m in main_messages if m["role"] == "assistant"]
    assert len(main_assistant_msgs) >= 1, "主会话无 assistant 消息"
    assert "headless test ok" in main_assistant_msgs[-1]["content"]

    # ===== 验证 judge 会话最后一条 assistant 消息含 JSON verdict =====
    judge_messages = fake.sessions[judge_session_id].messages
    judge_assistant_msgs = [m for m in judge_messages if m["role"] == "assistant"]
    assert len(judge_assistant_msgs) >= 1, "judge 会话无 assistant 消息"
    judge_data = json.loads(judge_assistant_msgs[-1]["content"])
    assert judge_data["verdict"] == "success"
    assert "reason" in judge_data

    # ===== 验证 memory 记录 =====
    assert capture["memory_set"].call_count == 1
    mem_call_args = capture["memory_set"].call_args
    mem_key = mem_call_args[0][0] if mem_call_args[0] else mem_call_args[1].get("key")
    mem_data = mem_call_args[0][1] if len(mem_call_args[0]) > 1 else mem_call_args[1].get("data")
    assert mem_key.startswith("headless_session_")
    assert mem_data["main_session_id"] == main_session_id
    assert mem_data["judge_session_id"] == judge_session_id
    assert mem_data["judge_verdict"] == "success"
    assert "judge_reason" in mem_data
    assert "trigger_text" in mem_data

    # ===== 验证 wip 记录 =====
    assert capture["wip_create"].call_count == 1
    assert capture["wip_update"].call_count == 1
    update_data = capture["wip_update"].call_args[0][1]
    assert update_data["status"] == "completed"
    assert update_data["progress"] == 100

    # ===== 验证 one_shot pause_task =====
    assert capture["pause_task"].call_count == 1
    pause_args = capture["pause_task"].call_args
    assert pause_args[0][0] == "headless_session.run"


# ============================================================================
# 测试用例 2：client 进程不在线时后端仍能跑
# ============================================================================


def test_e2e_runs_without_client_process(tmp_db_path, monkeypatch):
    """Ticket 06 E2E: client 进程不在线时后端仍能跑。

    验证点：
    - HeadlessSessionAction.execute() 不依赖任何 client 进程
    - 会话提交给独立 runtime（scripted 替身模拟 runtime 端跑完）
    - memory + wip 写入通过 mock（后端进程内）
    - 8766 后端进程中不组装 deps、不起对话循环
    """
    fake = _patch_runtime(monkeypatch)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "回复 ok 即可",
            "one_shot": False,  # 周期性任务（不 pause）
            "max_iterations": 3,
            "max_budget_usd": 0.3,
            "wall_clock_budget_secs": 30,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()

    async def run():
        return await action.execute(context)

    result = asyncio.run(run())

    # 验证成功执行（不依赖 client 进程）
    assert result["success"] is True, f"client 不在线时执行失败: {result}"
    assert result["judge_verdict"] == "success"

    # 验证主会话 + judge 会话都提交给 runtime
    main_session_id = result["main_session_id"]
    judge_session_id = result["judge_session_id"]
    assert main_session_id.startswith("headless-")
    assert judge_session_id.startswith("headless-judge-")
    assert [s["mode"] for s in fake.submits] == ["headless", "headless_judge"]

    # 验证 one_shot=False 时不调 pause_task（周期性任务继续）
    assert capture["pause_task"].call_count == 0

    # 验证 memory + wip 写入（后端自主留档）
    assert capture["memory_set"].call_count == 1
    assert capture["wip_create"].call_count == 1
    assert capture["wip_update"].call_count == 1


# ============================================================================
# 测试用例 3：judge verdict=failed 时整体 failed
# ============================================================================


def test_e2e_judge_failed_overall_failed(tmp_db_path, monkeypatch):
    """Ticket 06 E2E: judge verdict=failed → 整体 failed → wip status=failed。

    MockLLM 配置：
    - main agent 返回 "我无法完成这个任务"
    - judge agent 返回 {"verdict": "failed", "reason": "..."}
    """
    _patch_runtime(
        monkeypatch,
        main_text="我无法完成这个任务",
        judge_text='{"verdict": "failed", "reason": "主 agent 明确报告无法完成"}',
    )

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "回复 ok 即可",
            "one_shot": True,
            "max_iterations": 3,
            "max_budget_usd": 0.3,
            "wall_clock_budget_secs": 30,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()

    async def run():
        return await action.execute(context)

    result = asyncio.run(run())

    # judge verdict=failed → 整体 failed
    assert result["success"] is False
    assert result["judge_verdict"] == "failed"

    # wip status=failed
    update_data = capture["wip_update"].call_args[0][1]
    assert update_data["status"] == "failed"

    # memory 记录 judge_verdict=failed
    mem_data = capture["memory_set"].call_args[0][1]
    assert mem_data["judge_verdict"] == "failed"

    # one_shot 仍调 pause_task（无论成功失败）
    assert capture["pause_task"].call_count == 1
