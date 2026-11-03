"""多路并发（v14）：状态归位、交接挂 run、定向留言、同码独占。

2026-10-29 用户拍板的四条，逐条在这里有对应用例：
  ① 事务进度归用户手动，各路进度各写各的（run.progress）；
  ② 事务完成 = 用户 或「所有未关的路都 idle+done+有证据」；
  ③ 交接挂 run（多路时按 run 精确取，不再按事务取最近一份）；
  ④ 一个码只给一个执行者（open_run 按自报身份派生默认 owner）。

这些用例的目的不是覆盖接口，而是**钉住旧缺陷不再回来**：
「一路 done 全家完成」「一路 blocked 全事务卡住」「后写覆盖先写」「留言被先到的一路吃光」。
"""

from __future__ import annotations

import pytest

from server import user_message as um
from server.butler.service import (
    handoff_step,
    open_session,
    report_step,
    task_step,
)
from server.butler.store import ButlerError, ButlerStore
from server.memory.schema import SCHEMA_VERSION
from server.memory.store import MemoryStore
from server.todos.store import TodosStore


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "mem.db")
    mem = MemoryStore(db)
    mem.initialize()
    todos = TodosStore(db)
    todos.initialize()
    butler = ButlerStore(mem)
    yield mem, todos, butler
    mem.close()
    try:
        todos.conn.close()
    except Exception:
        pass
    um.clear_all()


@pytest.fixture
def task(env):
    _, todos, _ = env
    return todos.create_wip({
        "title": "多路测试事务",
        "status": "active",
        "priority": "high",
        "goal": "三路并行互不覆盖",
        "done_when": "每路各自收尾",
    })


def _open(env, task, label: str, prompt: str = "干活") -> dict:
    mem, todos, butler = env
    run = butler.create_run(prompt, task_scope="wip", task_id=task["id"],
                            label=label)
    open_session(butler, mem, todos, run["session_code"])
    return butler.get_run(run["run_id"])


def _report(env, run, payload: dict) -> dict:
    mem, todos, butler = env
    return report_step(butler, todos, run["code_prefix"], payload)


# ---- ①② 状态归位：各自写自己的行 ------------------------------------------


def test_two_runs_keep_own_progress(env, task):
    """两路各报 40 / 80：互不覆盖，事务行不被写。"""
    mem, todos, butler = env
    a = _open(env, task, "修 bug-A")
    b = _open(env, task, "修 bug-B")

    task_step(butler, todos, a["code_prefix"], {"progress": 40})
    task_step(butler, todos, b["code_prefix"], {"progress": 80})

    assert butler.get_run(a["run_id"])["progress"] == 40
    assert butler.get_run(b["run_id"])["progress"] == 80
    assert todos.get_wip(task["id"])["progress"] == 0


def test_one_done_does_not_complete_task(env, task):
    """一路 done（有证据）只收尾自己，事务还是 active。"""
    mem, todos, butler = env
    a = _open(env, task, "修 bug-A")
    _open(env, task, "修 bug-B")

    out = _report(env, a, {"outcome": "done", "summary": "A 做完了",
                           "evidence": ["tests/x.py:1"]})
    assert out.get("task_completed") is not True
    assert todos.get_wip(task["id"])["status"] == "active"


def test_all_done_with_evidence_completes_task(env, task):
    """所有未关的路都 idle+done+有证据 ⇒ 事务自动置完成。"""
    mem, todos, butler = env
    a = _open(env, task, "修 bug-A")
    b = _open(env, task, "修 bug-B")

    _report(env, a, {"outcome": "done", "summary": "A 完成",
                     "evidence": ["a.py:1"]})
    out = _report(env, b, {"outcome": "done", "summary": "B 完成",
                           "evidence": ["b.py:1"]})
    assert out.get("task_completed") is True
    assert todos.get_wip(task["id"])["status"] == "completed"


def test_done_without_evidence_blocks_auto_complete(env, task):
    """缺证据的 done 不算收尾：事务不自动完成。"""
    mem, todos, butler = env
    a = _open(env, task, "唯一一路")
    out = _report(env, a, {"outcome": "done", "summary": "我觉得做完了"})
    assert out["claimed_done"] is True
    assert out.get("task_completed") is not True
    assert todos.get_wip(task["id"])["status"] == "active"


def test_blocked_does_not_block_task(env, task):
    """一路 blocked：只在这路上记卡点，事务不被整体标成 blocked。"""
    mem, todos, butler = env
    a = _open(env, task, "修 bug-A")
    b = _open(env, task, "修 bug-B")

    _report(env, a, {"outcome": "blocked", "summary": "缺用户决策",
                     "blocked_on_user": {"question": "要不要动测试库"}})
    assert todos.get_wip(task["id"])["status"] == "active"
    saved = butler.get_run(a["run_id"])
    assert saved["outcome"] == "blocked"
    assert saved["state_note"] == "缺用户决策"
    # 另一路照跑，不受影响
    _report(env, b, {"outcome": "progress", "summary": "B 还在做"})
    assert butler.get_run(b["run_id"])["outcome"] == "progress"


def test_user_completed_status_not_overwritten_by_reports(env, task):
    """用户手动设成 paused 后，全路 done 也不越权改成 completed。"""
    mem, todos, butler = env
    todos.update_wip(task["id"], {"status": "paused"})
    a = _open(env, task, "唯一一路")
    out = _report(env, a, {"outcome": "done", "summary": "完成",
                           "evidence": ["a.py:1"]})
    assert out.get("task_completed") is not True
    assert todos.get_wip(task["id"])["status"] == "paused"


def test_closed_run_not_waited_for(env, task):
    """关掉的路不再参与「全部收尾」判定。"""
    mem, todos, butler = env
    a = _open(env, task, "放弃的一路")
    b = _open(env, task, "在做的一路")
    butler.close_run_by_user(a["run_id"])

    out = _report(env, b, {"outcome": "done", "summary": "B 完成",
                           "evidence": ["b.py:1"]})
    assert out.get("task_completed") is True


# ---- ③ 交接挂 run ---------------------------------------------------------


def _make_handoff(env, run, reason: str = "缺用户决策") -> str:
    mem, todos, butler = env
    out = handoff_step(butler, run["code_prefix"], {
        "handoff": {"needed": True, "reason": reason, "next_steps": ["继续"]},
    })
    return out["created"]


def test_handoff_is_scoped_to_run(env, task):
    """两路各自交接：按 run 取到的是各自那份。"""
    mem, todos, butler = env
    a = _open(env, task, "修 bug-A")
    b = _open(env, task, "修 bug-B")
    ha = _make_handoff(env, a, "A 卡在接口签名")
    hb = _make_handoff(env, b, "B 卡在测试库")

    got_a = butler.get_active_handoff(task_scope="wip", task_id=task["id"],
                                      run_id=a["run_id"])
    got_b = butler.get_active_handoff(task_scope="wip", task_id=task["id"],
                                      run_id=b["run_id"])
    assert got_a["handoff_id"] == ha
    assert got_b["handoff_id"] == hb
    assert got_a["run_id"] == a["run_id"]
    assert got_b["run_id"] == b["run_id"]
    # 不给 run_id 时的兼容行为还在（按事务取最近一份）
    assert butler.get_active_handoff(task_scope="wip", task_id=task["id"])


# ---- ④ 一个码只给一个执行者 ------------------------------------------------


def test_second_agent_with_other_identity_rejected(env):
    mem, todos, butler = env
    run = butler.create_run("独占测试")
    butler.open_run(run["session_code"], harness="CodeBuddy", model="m1")
    with pytest.raises(ButlerError) as exc:
        butler.open_run(run["session_code"], harness="Cursor", model="m2")
    assert exc.value.code == "owner_conflict"


def test_same_agent_reopen_is_idempotent(env):
    mem, todos, butler = env
    run = butler.create_run("独占测试")
    butler.open_run(run["session_code"], harness="CodeBuddy", model="m1")
    again = butler.open_run(run["session_code"], harness="CodeBuddy", model="m1")
    assert again["owner"] == "CodeBuddy/m1"


def test_open_without_identity_falls_back(env):
    """不报身份的调用（context.next 自动开跑）不该被自己的开跑挡住。"""
    mem, todos, butler = env
    run = butler.create_run("无身份", task_scope="wip", task_id="wip_x")
    butler.open_run(run["session_code"], owner="某人")
    # 不报 owner 的再次 open 允许（未知身份 ≠ 另一个人）
    saved = butler.open_run(run["session_code"])
    assert saved["owner"] == "某人"


# ---- 定向留言 -------------------------------------------------------------


def test_targeted_message_only_visible_to_that_run(env):
    """给 A 路的留言，B 路取不到；A 路取走后别人也拿不到。"""
    um.add_message("给 A 的", target_run_id="run_a")
    assert um.get_supplement_for_run("run_b") is None
    assert um.has_pending() is True
    text = um.get_supplement_for_run("run_a")
    assert text and "给 A 的" in text
    assert um.has_pending() is False


def test_broadcast_message_reaches_first_run(env):
    """广播消息（系统提醒）不挑路，但仍是读后即清。"""
    um.add_message("系统提醒", origin=um.ORIGIN_SYSTEM_REMINDER)
    assert "系统提醒" in (um.get_supplement_text() or "")
    assert um.get_supplement_text() is None


def test_run_scoped_message_skips_global_consume(env):
    """定向消息不会被全局中间件消费（否则多路下必被抢走）。"""
    um.add_message("给 A 的", target_run_id="run_a")
    assert um.get_supplement_text() is None
    assert um.has_pending() is True
    assert "给 A 的" in (um.get_supplement_for_run("run_a") or "")


# ---- 面板：本路状态可纠偏 -------------------------------------------------


def test_patch_run_state_clamps_and_keeps_summary(env, task):
    mem, todos, butler = env
    a = _open(env, task, "修 bug-A")
    _report(env, a, {"outcome": "progress", "summary": "报告原话"})
    butler.set_run_state(a["run_id"], progress=140, label="改过的标签",
                         state_note="用户写的现状")
    saved = butler.get_run(a["run_id"])
    assert saved["progress"] == 100
    assert saved["label"] == "改过的标签"
    assert saved["state_note"] == "用户写的现状"
    assert saved["summary"] == "报告原话"


# ---- schema v14 ----------------------------------------------------------


def test_schema_version_is_current(env):
    mem, _, _ = env
    row = mem.conn.execute(
        "select value from schema_info where key='version'"
    ).fetchone()
    assert int(row[0]) == SCHEMA_VERSION == 15
    cols = {r[1] for r in mem.conn.execute("PRAGMA table_info(butler_runs)")}
    assert {"label", "progress", "next_steps_json", "state_note"} <= cols
    hof = {r[1] for r in mem.conn.execute("PRAGMA table_info(butler_handoffs)")}
    assert "run_id" in hof


# ---- schema v15 ----------------------------------------------------------


def test_schema_v15_archive_and_handoff_override(env):
    """v15：面板归档 + 交接手改三列；老数据全空即「未归档、未手改」。"""
    mem, _, _ = env
    run_cols = {r[1] for r in mem.conn.execute("PRAGMA table_info(butler_runs)")}
    assert {"archived_at", "archive_reason", "handoff_text_override"} <= run_cols
    task_cols = {r[1] for r in mem.conn.execute("PRAGMA table_info(tasks)")}
    assert {"archived_at", "archive_prev_status", "archive_reason"} <= task_cols


def test_handoff_run_closes_source_and_carries_code(env, monkeypatch, task):
    """开接手轮：原路收尾（outcome=handoff），新路带码、带 label、提示词即接手说明。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.butler.router as router_module

    mem, todos, butler = env
    monkeypatch.setattr(router_module, "get_butler_store", lambda: butler)
    monkeypatch.setattr(router_module, "get_todos_store", lambda: todos)
    monkeypatch.setattr(router_module, "_memory", lambda: mem)
    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    a = _open(env, task, "修 bug-A")
    _make_handoff(env, a)

    body = client.post(f"/butler/runs/{a['run_id']}/handoff-run").json()
    assert body["state"] == "takeover"
    assert body["session_code"] and body["reused"] is False
    new_run = butler.get_run(body["takeover_run_id"])
    assert new_run["handoff_id"] == body["handoff_id"]
    assert new_run["label"].startswith("接手")
    # 接手轮的派单提示词就是接手说明（新执行者拿到同一份）
    assert new_run["prompt_text"] == body["prompt_text"]
    assert body["session_code"] in new_run["prompt_text"]
    # 原路收尾但不覆盖它的最近报告
    source = butler.get_run(a["run_id"])
    assert source["status"] == "closed" and source["outcome"] == "handoff"

    again = client.post(f"/butler/runs/{a['run_id']}/handoff-run").json()
    assert again["reused"] is True
    assert again["session_code"] == body["session_code"]
