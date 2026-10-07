"""POST /headless/sessions（wip_e67e3a05 只剩的那格：直连创建，不必绕 LoopManager）

编排本体是 HeadlessSessionAction，已有用例覆盖；这里只钉端点这一层的四件事：
- 入参过滤（留空的字段不能进 config，否则会把 HeadlessConfig 的默认值顶成 ""/0）
- one_shot 强制 False（直连会话没有所属 loop task 可暂停）
- trigger_text 与 skill 都为空 → 400，不产生空会话
- BaseSchema extra='forbid' 下，action 返回字典里的未知键必须被过滤而不是 500
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.activity_tracker import headless_endpoints, loop_actions  # noqa: E402


class _FakeAction:
    def __init__(self, captured: dict):
        self._captured = captured

    async def execute(self, context: dict) -> dict:
        self._captured["context"] = context
        return {
            "success": True,
            "main_session_id": "headless-abc",
            "judge_session_id": "headless-judge-abc",
            "judge_verdict": "success",
            "judge_reason": "产物齐",
            "loop_only_field": "不该出现在响应里",
        }


@pytest.fixture()
def rig(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        loop_actions, "HeadlessSessionAction", lambda: _FakeAction(captured))
    app = FastAPI()
    app.include_router(headless_endpoints.router)
    return TestClient(app), captured


def test_create_forwards_trigger_and_forces_one_shot_false(rig):
    client, captured = rig
    resp = client.post("/headless/sessions", json={
        "trigger_text": "查一下 dmca-backup 最近一次 run",
        "model": "deepseek-chat",
        "max_iterations": 7,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["main_session_id"] == "headless-abc"
    assert body["judge_verdict"] == "success"

    cfg = captured["context"]["config"]
    assert cfg["trigger_text"] == "查一下 dmca-backup 最近一次 run"
    assert cfg["model"] == "deepseek-chat"
    assert cfg["max_iterations"] == 7
    assert cfg["one_shot"] is False
    assert captured["context"]["task_id"] == ""


def test_unspecified_fields_stay_out_of_config_so_defaults_apply(rig):
    """只给 trigger_text 时，可选字段必须整个缺席——传 ""/None 会把默认值洗掉。"""
    client, captured = rig
    resp = client.post("/headless/sessions", json={"trigger_text": "只做一件事"})
    assert resp.status_code == 200, resp.text
    cfg = captured["context"]["config"]
    for name in ("model", "judge_model", "max_iterations", "wall_clock_budget_secs",
                 "judge_max_iterations", "judge_wall_clock_budget_secs"):
        assert name not in cfg, name
    assert cfg["skill"] == ""


def test_empty_trigger_and_skill_is_rejected_before_any_session(rig):
    client, captured = rig
    resp = client.post("/headless/sessions", json={"trigger_text": "   ", "skill": ""})
    assert resp.status_code == 400
    assert "context" not in captured  # 端点没被绕过：压根没进编排


def test_skill_only_request_passes_skill_through(rig):
    client, captured = rig
    resp = client.post("/headless/sessions", json={"skill": "recurring.yihuan_gacha"})
    assert resp.status_code == 200, resp.text
    cfg = captured["context"]["config"]
    assert cfg["skill"] == "recurring.yihuan_gacha"
    assert cfg["trigger_text"] == ""


def test_unknown_keys_in_action_result_are_filtered_not_500(rig):
    client, _ = rig
    resp = client.post("/headless/sessions", json={"trigger_text": "x"})
    assert resp.status_code == 200, resp.text
    assert "loop_only_field" not in resp.json()
    assert "elapsed_secs" in resp.json()


def test_operation_id_is_the_mcp_gateway_name(rig):
    client, _ = rig
    spec = client.app.openapi()
    assert spec["paths"]["/headless/sessions"]["post"]["operationId"] == "headless_create_session"
