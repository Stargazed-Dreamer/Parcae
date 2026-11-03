"""P5 T2：每轮报告语义（长会话、不关 run、字段按需）。"""

from __future__ import annotations

import json

import pytest

from server.butler.service import (
    close_run_by_user,
    feedback_step,
    handoff_step,
    memory_step,
    next_context,
    open_session,
    report_step,
    run_stage,
    session_note,
    submit_session_wrapup,
    task_step,
)
from server.butler.store import ButlerError, ButlerStore
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


@pytest.fixture
def seeded(env):
    mem, todos, butler = env
    mem.upsert_fact(
        "norm_secret", "密钥只经 lib/secret 读取。",
        title="密钥管理", scope="global", kind="norm", origin="system", must_read=1,
    )
    task = todos.create_wip({
        "title": "T2 报告任务",
        "status": "active",
        "priority": "high",
        "progress": 0,
        "next_steps": ["做一步"],
        "done_when": "报告落库",
    })
    return mem, todos, butler, task


def test_progress_only_needs_outcome_and_summary(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    open_session(butler, mem, todos, run["session_code"])

    out = submit_session_wrapup(butler, mem, todos, run["session_code"], {
        "outcome": "progress", "summary": "还在做",
    })
    assert out["status"] == "idle"
    assert out["report_count"] == 1
    assert out["task"]["updated"] is False
    assert todos.get_wip(task["id"])["progress"] == 0
    assert butler.get_run_by_code(run["session_code"])["status"] == "idle"


def test_task_updates_current_state_written(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    out = submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "partial", "summary": "做了一半",
        "task_updates": {
            "progress": 50,
            "current_state": {"done": ["A"], "remaining": ["B"]},
        },
    })
    # v14（2026-10-29 用户拍板）：执行域字段写「这一路」，事务行不再被单路覆盖
    assert out["task"]["updated"] is False
    saved = butler.get_run_by_code(code)
    assert saved["progress"] == 50
    assert json.loads(saved["state_note"]) == {"done": ["A"], "remaining": ["B"]}
    assert todos.get_wip(task["id"])["current_state"] == {}


def test_second_round_keeps_same_code(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "第一轮",
    })
    out2 = submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "第二轮",
    })
    assert out2["report_count"] == 2
    assert butler.get_run_by_code(code)["status"] == "idle"
    rows = butler.conn.execute(
        "select attempt, summary from butler_reports order by attempt"
    ).fetchall()
    assert rows == [(1, "第一轮"), (2, "第二轮")]


def test_blocked_without_explanation_is_soft(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    out = submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "blocked", "summary": "等外部",
    })
    assert out["blocked_unexplained"] is True
    assert butler.get_run_by_code(code)["status"] == "idle"

    out2 = submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "继续了",
    })
    assert out2["blocked_unexplained"] is False
    assert butler.get_run_by_code(code)["blocked_on_user_json"] == "{}"


def test_next_run_hint_stored_as_text_object(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "还在做",
        "next_run_hint": "先确认范围默认值",
    })
    raw = butler.get_run_by_code(code)["next_run_hint_json"]
    assert json.loads(raw) == {"text": "先确认范围默认值"}


def test_split_report_and_task_steps(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    report = report_step(butler, todos, code, {
        "outcome": "partial", "summary": "做了一半",
        "next_run_hint": "继续做 B",
    })
    assert report["status"] == "idle"
    assert report["attempt"] == 1

    task_out = task_step(butler, todos, code, {
        "title": "拆步任务",
        "progress": 50,
        "current_state": {"done": ["A"], "remaining": ["B"]},
    })
    # 进度/现状写本路；标题是用户口径，非首轮不许执行者覆盖（要改请用户改）
    assert task_out["updated"] is True
    assert task_out["target"] == "run"
    assert task_out["ignored"] == ["title"]
    assert todos.get_wip(task["id"])["title"] == "T2 报告任务"
    saved = butler.get_run_by_code(code)
    assert saved["progress"] == 50
    assert json.loads(saved["state_note"]) == {"done": ["A"], "remaining": ["B"]}


def test_task_step_explains_why_not_written(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)
    empty = task_step(butler, todos, code, {})
    assert empty["updated"] is False and empty["reason"] == "no_changes"
    # 提示词不再解释返回字段，所以「为什么没写入」必须写在这句 note 里
    assert empty["note"] == "你给的字段和库里一样，没有可写的变化"

    adhoc = butler.create_run("没有挂任务", task_scope=None)
    open_session(butler, mem, todos, adhoc["session_code"])
    no_task = task_step(butler, todos, adhoc["session_code"], {"title": "x"})
    assert no_task["updated"] is False and no_task["reason"] == "adhoc_or_no_task"
    assert no_task["note"] == "这个 run 没挂任务，task_updates 无处可写"


def test_memory_step_surfaces_reject_and_downgrade(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    bad = memory_step(butler, mem, code, {"memory_candidates": [
        {"content": "坏候选", "kind": "not_a_kind"},
    ]})
    assert bad["rejected"] == 1
    # 拒收只说「判据 + 实际值」，不附带「你该怎么改」（2026-10-28 用户口径）
    assert bad["issues"] == [{
        "index": 0, "reason": "unknown_kind",
        "message": 'kind 不在 7 个枚举内；实际 kind="not_a_kind"，枚举 '
                   "current_state/fact/norm/pitfall/preference/procedure/reference",
    }]

    down = memory_step(butler, mem, code, {"memory_candidates": [{
        "content": "冒充原话", "kind": "fact", "scope": "global",
        "origin": "user_quote",
    }]})
    assert down["accepted"] == 1
    assert down["issues"][0]["reason"] == "downgraded_from_user_quote"
    assert "issues" not in memory_step(butler, mem, code, {"memory_candidates": [{
        "content": "正常候选", "kind": "fact", "scope": "global",
    }]})


def test_split_memory_feedback_and_handoff_steps(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)
    next_context(butler, mem, todos, code, None)  # 投递 norms，供 feedback 引用

    mem_out = memory_step(butler, mem, code, {
        "memory_candidates": [{
            "content": "拆步候选", "kind": "pitfall",
            "scope": "project:parcae", "origin": "agent_extracted",
        }],
    })
    assert mem_out["accepted"] == 1

    fb_out = feedback_step(butler, code, [{
        "bundle": "norms", "used": 1, "useful": 1,
    }])
    assert fb_out["count"] == 1

    blocked = [{"question": "选 A 还是 B？", "why": "两种都合理"}]
    handoff_out = handoff_step(butler, code, {
        "blocked_on_user": blocked, "handoff": {},
    })
    assert handoff_out == {}
    assert butler.get_run_by_code(code)["blocked_on_user_json"] == json.dumps(
        blocked, ensure_ascii=False
    )


def test_handoff_needs_reason(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    open_session(butler, mem, todos, run["session_code"])
    with pytest.raises(ButlerError) as exc:
        submit_session_wrapup(butler, mem, todos, run["session_code"], {
            "outcome": "partial", "summary": "要交接",
            "handoff": {"needed": True},
        })
    assert exc.value.code == "handoff_reason_required"


def test_feedback_requires_delivered_bundle(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)
    next_context(butler, mem, todos, code, None)  # 投递 norms

    out = submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "用了红线",
        "recall_feedback": [{"bundle": "norms", "item_id": "norm_secret",
                             "used": True, "useful": True}],
    })
    assert out["feedback"]["count"] == 1
    row = butler.conn.execute(
        "select used, useful, feedback_note from butler_deliveries where bundle='norms'"
    ).fetchone()
    assert row[0] == 1 and row[1] == 1 and "norm_secret" in row[2]

    with pytest.raises(ButlerError) as exc:
        submit_session_wrapup(butler, mem, todos, code, {
            "outcome": "progress", "summary": "伪造",
            "recall_feedback": [{"bundle": "memory.index", "item_id": "x"}],
        })
    assert exc.value.code == "feedback_item_not_delivered"


def test_idempotency_returns_first_result(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    payload = {"outcome": "progress", "summary": "一次", "idempotency_key": "k1"}
    first = submit_session_wrapup(butler, mem, todos, code, payload)
    second = submit_session_wrapup(butler, mem, todos, code, payload)
    assert second["duplicate"] is True
    assert second["report_count"] == first["report_count"] == 1
    count = butler.conn.execute(
        "select count(*) from butler_reports where run_id=?", (run["run_id"],)
    ).fetchone()[0]
    assert count == 1


def test_no_quotes_writes_nothing(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    open_session(butler, mem, todos, run["session_code"])
    submit_session_wrapup(butler, mem, todos, run["session_code"], {
        "outcome": "progress", "summary": "不记原话",
    })
    assert mem.conn.execute("select count(*) from source_quotes").fetchone()[0] == 0


def test_run_progress_overwrites_not_merges(seeded):
    """同一个 run 报两次进度：后报的覆盖先报的（本路自己的行，覆盖是正确语义）。"""
    mem, todos, butler, task = seeded
    run = butler.create_run("T2", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)
    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "40", "task_updates": {"progress": 40},
    })
    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "60", "task_updates": {"progress": 60},
    })
    assert butler.get_run_by_code(code)["progress"] == 60
    # 事务进度归用户手动，执行者不再写它
    assert todos.get_wip(task["id"])["progress"] == 0


def test_user_close_rejects_running(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T3", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)
    next_context(butler, mem, todos, code, None)  # opened → running
    with pytest.raises(ButlerError) as exc:
        close_run_by_user(butler, run["run_id"])
    assert exc.value.code == "run_running"


def test_user_close_then_report_rejected(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T3", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)
    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "本轮完",
    })
    out = close_run_by_user(butler, run["run_id"])
    assert out["status"] == "closed"
    assert butler.get_run_by_code(code)["status"] == "closed"
    with pytest.raises(ButlerError) as exc:
        submit_session_wrapup(butler, mem, todos, code, {
            "outcome": "progress", "summary": "关后再报",
        })
    assert exc.value.code == "run_closed"


def test_user_close_created_allowed(seeded):
    _, _, butler, task = seeded
    run = butler.create_run("T3", task_scope="wip", task_id=task["id"])
    out = close_run_by_user(butler, run["run_id"])
    assert out["status"] == "closed"


def test_note_is_idempotent(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T4", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    first = session_note(butler, code, "记下这个决策", client_note_id="n1")
    second = session_note(butler, code, "记下这个决策", client_note_id="n1")
    assert first["note_id"] == second["note_id"]
    assert second["duplicate"] is True
    count = butler.conn.execute(
        "select count(*) from butler_notes where run_id=?", (run["run_id"],)
    ).fetchone()[0]
    assert count == 1


def test_note_after_close_rejected(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T4", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)
    close_run_by_user(butler, run["run_id"])
    with pytest.raises(ButlerError) as exc:
        session_note(butler, code, "关了还记")
    assert exc.value.code == "run_closed"


def test_multi_cursor_returns_ordered_pages(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T5", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    out = next_context(butler, mem, todos, code,
                       cursors=["norms/0", "task/0", "unknown/0"])
    assert out["count"] == 3
    assert out["pages"][0]["bundle"] == "norms"
    assert out["pages"][1]["bundle"] == "task"
    assert out["pages"][2]["error"] == "bad_cursor"
    assert out["pages"][0]["digest"] and out["pages"][1]["digest"]


def test_counters_reports_and_stage(seeded):
    mem, todos, butler, task = seeded
    run = butler.create_run("T9", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)
    next_context(butler, mem, todos, code, None)

    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "第一轮",
        "memory_candidates": [{
            "content": "计数测试候选", "kind": "fact", "scope": "global",
        }],
    })
    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "done", "summary": "完成",
        "task_updates": {"evidence": ["pytest"]},
    })

    counters = butler.run_counters(run["run_id"])
    assert counters["report_count"] == 2
    assert counters["delivery_pages"] >= 1
    assert counters["delivery_chars"] > 0
    assert counters["candidate_count"] == 1

    reports = butler.list_reports(run["run_id"])
    assert [r["attempt"] for r in reports] == [1, 2]
    assert run_stage(butler.get_run(run["run_id"])) == "done_pending_close"

    item = butler.list_runs()[0]
    assert item["report_count"] == 2 and item["last_report_at"]


def test_reject_message_states_criterion_and_actual(seeded):
    """拒收只报「判据 + 实际值」，不给改法（2026-10-28 用户口径）。

    给建议会诱导 agent 改内容去迎合校验——这次就是把「项目内事实」改成了 global。
    """
    mem, todos, butler, task = seeded
    run = butler.create_run("T3", task_scope="wip", task_id=task["id"])
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    bad = memory_step(butler, mem, code, {"memory_candidates": [
        {"content": "范围写错的候选", "kind": "fact", "scope": "nonsense", "title": "范围"},
    ]})
    issue = bad["issues"][0]
    assert issue["reason"] == "bad_scope"
    assert "scope 必须形如 global / project:<id>" in issue["message"]
    assert '实际 scope="nonsense"' in issue["message"]
    for advice in ("建议", "请改", "可以改", "应该", "try"):
        assert advice not in issue["message"]


def test_bare_project_scope_resolved_from_session(seeded):
    """裸 project 按会话码所属项目补全，不再逼模型去查合法 id。

    2026-10-28 用户口径：会话码已经绑定了 run→project，模型只给码就够了，
    系统自己去匹配。之前这里会被拒，agent 为了过校验把项目内事实写成 global。
    """
    mem, todos, butler, task = seeded
    run = butler.create_run("T4", task_scope="wip", task_id=task["id"],
                            project_id="proj-x")
    code = run["session_code"]
    open_session(butler, mem, todos, code)

    res = memory_step(butler, mem, code, {"memory_candidates": [
        {"content": "裸 project 应当被补全", "kind": "fact",
         "scope": "project", "title": "裸 project"},
    ]})
    assert res["accepted"] == 1 and res["rejected"] == 0
    assert not res.get("issues")

    # 落库的事实用的是补全后的 scope，所以按项目召回时能命中
    row = mem.conn.execute(
        "SELECT scope FROM facts WHERE value = ?", ("裸 project 应当被补全",)
    ).fetchone()
    assert row is not None and row[0] == "project:proj-x"
