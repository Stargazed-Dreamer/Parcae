"""DL4 外部循环接入：open → context.next → wrapup.submit（+ memory.write）。

对应设计 `workspace/local_butler/30_架构与设计/15_外部循环接入_DL4实施设计_20261027.md`。
守五条硬判据：会话码只存 hash、bundle 顺序、收尾必须证据、任务/记忆各变一次、
记忆接口不出现 run 概念。
"""

from __future__ import annotations

import pytest

from server.butler.service import (
    next_context,
    open_session,
    submit_session_wrapup,
    write_memory,
)
from server.butler.store import ButlerError, ButlerStore
from server.memory.schema import SCHEMA_VERSION
from server.memory.store import MemoryStore
from server.memory.write import MemoryWriteError
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


@pytest.fixture
def seeded(env):
    mem, todos, butler = env
    mem.upsert_fact(
        "norm_secret", "密钥只经 lib/secret 读取，禁止硬编码。",
        title="密钥管理", scope="global", kind="norm", origin="system", must_read=1,
    )
    mem.upsert_fact(
        "pref_style", "先给结论，再给理由。",
        title="汇报偏好", scope="global", kind="preference",
        origin="user_quote", must_read=0,
    )
    task = todos.create_wip({
        "title": "DL4 测试任务",
        "status": "active",
        "priority": "high",
        "goal": "走通会话码闭环",
        "next_steps": ["取 norms", "干活", "收尾"],
        "done_when": "wrapup 落库",
    })
    return mem, todos, butler, task


def _new_run(butler, task_id, **kw):
    return butler.create_run(
        "跑通 DL4", task_scope="wip", task_id=task_id,
        project_id="parcae", harness="pytest", **kw,
    )


def test_schema_v9_tables(env):
    mem, _, _ = env
    tables = {r[0] for r in mem.conn.execute(
        "select name from sqlite_master where type='table'")}
    assert {"butler_runs", "butler_handoffs", "butler_deliveries", "butler_events"} <= tables


def test_v8_db_upgrades_to_current(tmp_path):
    """v8 库升级到当前版本：v9 表与 v10 报告表/列都要补齐。

    原断言写死 "9"；schema v10 后升级目标是当前 SCHEMA_VERSION，
    因此改为断言版本常量 + v10 产物，而不是放宽版本检查。
    """
    db = str(tmp_path / "old.db")
    mem = MemoryStore(db)
    mem.initialize()
    for table in ("butler_runs", "butler_handoffs", "butler_deliveries", "butler_events"):
        mem.conn.execute(f"DROP TABLE IF EXISTS {table}")
    mem.conn.execute("UPDATE schema_info SET value='8' WHERE key='version'")
    mem.conn.commit()
    mem.close()

    upgraded = MemoryStore(db)
    upgraded.initialize()
    tables = {r[0] for r in upgraded.conn.execute(
        "select name from sqlite_master where type='table'")}
    assert {"butler_runs", "butler_handoffs", "butler_deliveries", "butler_events"} <= tables
    assert "butler_reports" in tables
    version = upgraded.conn.execute(
        "select value from schema_info where key='version'").fetchone()[0]
    assert version == str(SCHEMA_VERSION)
    run_cols = {r[1] for r in upgraded.conn.execute("PRAGMA table_info(butler_runs)")}
    assert {"blocked_on_user_json", "next_run_hint_json",
            "report_count", "last_report_at"} <= run_cols
    upgraded.close()


def test_session_code_is_hashed(seeded):
    _, _, butler, task = seeded
    run = _new_run(butler, task["id"])
    row = butler.conn.execute(
        "select code_hash, code_prefix, status from butler_runs where run_id=?",
        (run["run_id"],),
    ).fetchone()
    assert len(run["session_code"]) == 4
    assert run["session_code"].isalnum()
    assert row[0] != run["session_code"]
    assert len(row[0]) == 64
    assert row[1] == run["session_code"][:7]
    assert row[2] == "created"


def test_prompt_text_is_stored_for_resend(seeded):
    """面板要能随时重发提示词：完整文本（含码）落库，列表可读回。"""
    from server.butler.prompts import render_pb

    _, _, butler, task = seeded
    run = _new_run(butler, task["id"])
    text = render_pb(code=run["session_code"], task_title="DL4 测试任务")["prompt"]
    butler.set_run_prompt_text(run["run_id"], text)
    assert butler.get_run(run["run_id"])["prompt_text"] == text
    listed = butler.list_runs(limit=1)
    assert listed[0]["prompt_text"] == text


def test_open_records_agent_reported_model(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    open_session(butler, mem, todos, run["session_code"],
                 harness="claude", model="claude-opus-4")
    assert butler.get_run(run["run_id"])["model"] == "claude-opus-4"


def test_wrapup_backfills_title(seeded):
    """标题留空由 agent 补：首份报告的 task_updates.title 覆盖占位标题。"""
    mem, todos, butler, task = seeded
    placeholder = todos.create_wip({
        "title": "待命名", "status": "active", "done_when": "完成",
    })
    run = butler.create_run(
        "用户原文", task_scope="wip", task_id=placeholder["id"],
        project_id="parcae", harness="pytest",
    )
    open_session(butler, mem, todos, run["session_code"])
    submit_session_wrapup(butler, mem, todos, run["session_code"], {
        "outcome": "progress", "summary": "已根据原文补标题",
        "task_updates": {"title": "netsec 第七周实验报告"},
    })
    assert todos.get_wip(placeholder["id"])["title"] == "netsec 第七周实验报告"


def test_open_manifest_order(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    out = open_session(butler, mem, todos, run["session_code"], owner="h1")
    manifest = out["manifest"]
    names = [b["name"] for b in manifest["bundles"]]
    assert names == [
        "norms", "task", "handoff", "memory.must_read",
        "memory.index", "guide", "environment", "artifacts",
    ]
    status = {b["name"]: b["status"] for b in manifest["bundles"]}
    assert status["norms"] == "ready"
    assert status["task"] == "ready"
    assert status["memory.must_read"] == "ready"
    assert status["handoff"] == "empty"
    assert manifest["next_cursor"] == "norms/0"
    assert out["run"]["status"] == "opened"


def test_context_next_walks_ready_bundles(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    open_session(butler, mem, todos, run["session_code"])

    page = next_context(butler, mem, todos, run["session_code"], None)
    assert page["bundle"] == "norms"
    assert "密钥只经 lib/secret" in page["text"]
    assert page["next_cursor"] == "task/0"

    page2 = next_context(butler, mem, todos, run["session_code"], page["next_cursor"])
    assert page2["bundle"] == "task"
    assert "DL4 测试任务" in page2["text"]
    assert page2["next_cursor"] == "memory.must_read/0"

    page3 = next_context(butler, mem, todos, run["session_code"], page2["next_cursor"])
    assert page3["bundle"] == "memory.must_read"
    assert "先给结论" in page3["text"]
    # T6 起后四页也会按需可取；guide 改为语义匹配后可能为空，下一页直接跳到 environment
    assert page3["next_cursor"] in ("memory.index/0", "guide/0", "environment/0")

    env = next_context(butler, mem, todos, run["session_code"], "environment/0")
    assert env["bundle"] == "environment"
    assert "OS:" in env["text"]

    again = next_context(butler, mem, todos, run["session_code"], page["cursor"])
    assert again["digest"] == page["digest"]
    assert again["stale"] is False


def test_wrapup_done_updates_task_and_memory(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    open_session(butler, mem, todos, run["session_code"])

    out = submit_session_wrapup(butler, mem, todos, run["session_code"], {
        "outcome": "done",
        "summary": "跑通了",
        "task_updates": {"evidence": ["tests/butler/test_dl4_flow.py:1"]},
        "quotes": [{"text": "用户说这次要先跑通闭环"}],
        "memory_candidates": [{
            "content": "DL4 收尾会关闭 run 并更新任务",
            "title": "DL4 收尾行为",
            "summary": "done + evidence 才置 completed",
            "kind": "fact",
            "scope": "global",
        }],
    })
    assert out["claimed_done"] is False
    assert out["status"] == "idle"
    assert out["report_count"] == 1
    assert out["memory"]["candidates"]["accepted"] == 1
    # v14（2026-10-29 用户拍板）：报告不再直写事务字段 —— 一路 done 不等于事务完成，
    # 事务完成由「所有未关的路都 idle+done+有证据」聚合判定。
    assert out["task"]["updated"] is False
    assert out["task_completed"] is True
    assert todos.get_wip(task["id"])["status"] == "completed"

    still_open = butler.get_run_by_code(run["session_code"])
    assert still_open["status"] == "idle"
    assert still_open["report_count"] == 1
    reports = butler.conn.execute(
        "select count(*) from butler_reports where run_id=?", (run["run_id"],)
    ).fetchone()[0]
    assert reports == 1
    quotes = mem.conn.execute("select count(*) from source_quotes").fetchone()[0]
    assert quotes == 1


def test_wrapup_without_evidence_is_claimed_done(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    open_session(butler, mem, todos, run["session_code"])

    out = submit_session_wrapup(butler, mem, todos, run["session_code"], {
        "outcome": "done",
        "summary": "我觉得做完了",
        "task_updates": {},
    })
    assert out["claimed_done"] is True
    assert out["evidence_ok"] is False
    wip = todos.get_wip(task["id"])
    assert wip["status"] == "active"


def test_memory_write_and_duplicate(seeded):
    mem, _, _, _ = seeded
    out = write_memory(mem, {
        "kind": "norm", "scope": "global", "must_read": 1,
        "title": "新增红线", "content": "新增红线正文",
    })
    assert out["status"] == "accepted"
    again = write_memory(mem, {
        "kind": "norm", "scope": "global", "must_read": 1,
        "title": "新增红线", "content": "新增红线正文",
    })
    assert again["status"] == "duplicate"
    assert again["key"] == out["key"]


def test_memory_write_rejects_must_read_non_norm(seeded):
    mem, _, _, _ = seeded
    with pytest.raises(MemoryWriteError) as exc:
        write_memory(mem, {
            "kind": "fact", "scope": "global", "must_read": 1,
            "content": "不是红线",
        })
    assert exc.value.code == "must_read_not_norm"


def test_context_before_open_auto_opens(seeded):
    """2026-10-28：提示词已带 pages，允许跳过 open，直接取上下文。"""
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    page = next_context(butler, mem, todos, run["session_code"], None)
    assert page["bundle"] == "norms"
    assert butler.get_run_by_code(run["session_code"])["status"] == "running"


def test_reopen_keeps_active_and_rejects_other_owner(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"], owner="h1")
    open_session(butler, mem, todos, run["session_code"], owner="h1")
    next_context(butler, mem, todos, run["session_code"], None)
    assert butler.get_run_by_code(run["session_code"])["status"] == "running"

    reopened = open_session(butler, mem, todos, run["session_code"], owner="h1")
    assert reopened["run"]["status"] == "running"

    with pytest.raises(ButlerError) as exc:
        open_session(butler, mem, todos, run["session_code"], owner="h2")
    assert exc.value.code == "owner_conflict"


def test_claimed_done_cannot_force_completed(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    open_session(butler, mem, todos, run["session_code"])

    out = submit_session_wrapup(butler, mem, todos, run["session_code"], {
        "outcome": "done",
        "summary": "自报完成",
        "task_updates": {"status": "completed"},
    })
    assert out["claimed_done"] is True
    assert todos.get_wip(task["id"])["status"] == "active"


def test_done_does_not_create_handoff(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    open_session(butler, mem, todos, run["session_code"])

    out = submit_session_wrapup(butler, mem, todos, run["session_code"], {
        "outcome": "done",
        "summary": "完成",
        "task_updates": {"evidence": ["pytest"]},
        "handoff": {"needed": True, "reason": "其实还想留给下一轮"},
    })
    assert out["handoff"]["ignored"] == "outcome_done"
    count = butler.conn.execute("select count(*) from butler_handoffs").fetchone()[0]
    assert count == 0


def test_closed_run_rejects_second_open(seeded):
    mem, todos, butler, task = seeded
    run = _new_run(butler, task["id"])
    open_session(butler, mem, todos, run["session_code"])
    submit_session_wrapup(butler, mem, todos, run["session_code"], {
        "outcome": "partial", "summary": "先停",
    })
    # 报告不关 run；关闭是用户动作（T3 端点前先直接调 store）
    butler.close_run(run["run_id"], outcome="partial", summary="用户关闭")
    with pytest.raises(ButlerError) as exc:
        butler.open_run(run["session_code"])
    assert exc.value.code == "run_closed"


def test_http_endpoints(seeded, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.butler.router as router_module

    mem, todos, butler, task = seeded
    monkeypatch.setattr(router_module, "get_butler_store", lambda: butler)
    monkeypatch.setattr(router_module, "get_todos_store", lambda: todos)
    monkeypatch.setattr(router_module, "_memory", lambda: mem)

    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    resp = client.post("/butler/runs", json={
        "prompt": "HTTP 闭环", "task_scope": "wip", "task_id": task["id"],
        "project_id": "parcae",
    })
    assert resp.status_code == 200
    created = resp.json()
    code = created["session_code"]

    resp = client.post("/butler/session", json={"action": "open", "code": code})
    assert resp.status_code == 200
    opened = resp.json()
    assert opened["code"] == code
    assert opened["pages"][0]["cursor"] == "norms/0"
    assert "run" not in opened
    assert "run_id" not in opened

    resp = client.post("/butler/context", json={"action": "next", "code": code})
    assert resp.status_code == 200
    assert resp.json()["bundle"] == "norms"

    resp = client.post("/butler/context", json={
        "action": "next", "code": code, "cursors": ["task/0", "unknown/0"],
    })
    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    assert resp.json()["pages"][0]["bundle"] == "task"
    assert resp.json()["pages"][1]["error"] == "bad_cursor"

    resp = client.post("/butler/session", json={
        "action": "note", "code": code, "text": "记一笔", "client_note_id": "n1",
    })
    assert resp.status_code == 200
    assert resp.json()["action"] == "note"

    resp = client.post("/butler/session", json={"action": "inbox", "code": code})
    assert resp.status_code == 501  # 2026-10-28 用户裁定：不再有信箱查询

    resp = client.post("/butler/wrapup", json={
        "action": "submit", "code": code, "outcome": "done",
        "summary": "HTTP 收尾",
        "task_updates": {"evidence": ["pytest"]},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"

    resp = client.get("/butler/runs")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["stage"] == "done_pending_close"

    resp = client.get(f"/butler/runs/{created['run_id']}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["counters"]["report_count"] == 1
    assert len(detail["reports"]) == 1

    resp = client.get(f"/butler/runs/{created['run_id']}/reports")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    resp = client.post(f"/butler/runs/{created['run_id']}/close")
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"

    resp = client.post("/butler/wrapup", json={
        "action": "submit", "code": code, "outcome": "progress",
        "summary": "关后再报",
    })
    assert resp.status_code == 409

    resp = client.post("/butler/session", json={"action": "status", "code": code})
    assert resp.status_code == 501
