"""DL2 事务分类测试：四类结构轴 + 三档目的轴 + 到期行为。

对应设计 `workspace/local_butler/30_架构与设计/13_事务分类_DL2实施设计_20261027.md`。
四条硬判据各有用例守着：
1) 四类各跑出自己的到期行为；
2) 映射不上的一律列出来（不猜）；
3) 目的轴不越权改 L0-L5；
4) occurrence 幂等，模板不被标 done。
"""

from datetime import datetime, timedelta

import pytest

from server.todos import classify as C
from server.todos.store import TodosStore


@pytest.fixture
def store(tmp_path):
    st = TodosStore(str(tmp_path / "tasks.db"))
    st.initialize()
    yield st
    st.close()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _days(n: int) -> str:
    return (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")


def _mk_todo(store, **kw) -> dict:
    payload = {
        "title": kw.pop("title", "测试周期任务"),
        "type": kw.pop("type", "recurring"),
        "frequency": kw.pop("frequency", "weekly"),
    }
    payload.update(kw)
    return store.create_todo(payload)


def _mk_wip(store, **kw) -> dict:
    payload = {"title": kw.pop("title", "测试 WIP"), "goal": kw.pop("goal", "保住目标")}
    payload.update(kw)
    return store.create_wip(payload)


# ─── 1. schema ────────────────────────────────────────────────


def test_schema_has_axis_columns(store):
    todo_cols = {r[1] for r in store.conn.execute("PRAGMA table_info(todos)")}
    wip_cols = {r[1] for r in store.conn.execute("PRAGMA table_info(wip_tasks)")}
    assert {"task_class", "purpose", "next_review_at"} <= todo_cols
    assert {"task_class", "purpose", "due_at", "done_when", "next_review_at"} <= wip_cols


def test_schema_has_occurrences(store):
    tables = {r[0] for r in store.conn.execute("select name from sqlite_master where type='table'")}
    assert "task_occurrences" in tables


# ─── 2. 结构轴判定 ───────────────────────────────────────────


def test_recurring_with_frequency(store):
    todo = _mk_todo(store, frequency="monthly")
    c = C.classify_todo(todo)
    assert c.task_class == C.STRUCT_RECURRING
    assert c.unmapped is False


def test_recurring_purpose_daily_only_for_routines(store):
    routine = C.classify_todo(_mk_todo(store, frequency="weekly"))
    assert routine.purpose == C.PURPOSE_DAILY
    # quarterly（复盘节奏）与带 condition 的都不推目的轴，留空待标
    quarterly = C.classify_todo(_mk_todo(store, frequency="quarterly"))
    conditioned = C.classify_todo(_mk_todo(store, frequency="weekly", condition="找工作期间"))
    assert quarterly.purpose is None and quarterly.purpose_unset
    assert conditioned.purpose is None and conditioned.purpose_unset


def test_triggered_is_unmapped_not_forced(store):
    todo = _mk_todo(store, type="triggered", frequency=None,
                    trigger_condition='{"event":"file_arrived"}')
    c = C.classify_todo(todo)
    assert c.task_class is None
    assert c.unmapped is True
    assert any("没有它的位置" in n for n in c.notes)


def test_recurring_without_frequency_is_unmapped(store):
    todo = {"id": "x", "type": "recurring", "frequency": None}
    c = C.classify_todo(todo)
    assert c.unmapped is True and c.task_class is None


def test_wip_default_long(store):
    task = _mk_wip(store)
    assert C.classify_wip(task).task_class == C.STRUCT_LONG


def test_wip_with_due_is_one_shot(store):
    task = _mk_wip(store, due_at=_days(2))
    assert C.classify_wip(task).task_class == C.STRUCT_ONE_SHOT


def test_wip_one_session_is_small(store):
    task = _mk_wip(store, extra_data={"one_session": True})
    assert C.classify_wip(task).task_class == C.STRUCT_SMALL


def test_wip_closed_stays_out_of_queue(store):
    task = _mk_wip(store)
    store.update_wip(task["id"], {"status": "completed"})
    closed = store.get_wip(task["id"])
    assert closed is not None
    c = C.classify_wip(closed)
    assert c.task_class == C.STRUCT_LONG
    view = C.due_view({**closed, "task_class": c.task_class})
    assert view["view"] == C.VIEW_CLOSED


# ─── 3. 四类到期行为 ─────────────────────────────────────────


def test_recurring_overdue_view(store):
    todo = _mk_todo(store, frequency="weekly")
    store.update_todo(todo["id"], {"next_due_at": _days(-3), "last_done_at": _days(-10)})
    row = {**store.get_todo(todo["id"]), "task_class": C.STRUCT_RECURRING}
    view = C.due_view(row)
    assert view["view"] == C.VIEW_OVERDUE
    assert view["days"] == -3


def test_recurring_never_scheduled_is_not_fake_overdue(store):
    todo = _mk_todo(store, frequency="weekly")  # next_due_at / last_done_at 都是 NULL
    row = {**store.get_todo(todo["id"]), "task_class": C.STRUCT_RECURRING}
    view = C.due_view(row)
    assert view["view"] == C.VIEW_NEVER_SCHEDULED


def test_one_shot_overdue_does_not_disappear(store):
    task = _mk_wip(store, due_at=_days(-5))
    row = {**store.get_wip(task["id"]), "task_class": C.STRUCT_ONE_SHOT}
    view = C.due_view(row)
    assert view["view"] == C.VIEW_OVERDUE
    assert view["days"] == -5
    # 过期不是状态，库里 status 仍是 active
    assert store.get_wip(task["id"])["status"] == "active"


def test_small_never_overdue_only_residual(store):
    task = _mk_wip(store, extra_data={"one_session": True})
    store.conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (_days(-30) + " 00:00:00", task["id"])
    )
    store.conn.commit()
    row = {**store.get_wip(task["id"]), "task_class": C.STRUCT_SMALL}
    view = C.due_view(row)
    assert view["view"] == C.VIEW_RESIDUAL
    assert view["days"] == 30  # 只报年龄，不判逾期


def test_long_ages_into_review(store):
    task = _mk_wip(store)
    row = {**store.get_wip(task["id"]), "task_class": C.STRUCT_LONG,
           "updated_at": _days(-(C.LONG_REVIEW_AFTER_DAYS + 1))}
    assert C.due_view(row)["view"] == C.VIEW_REVIEW
    fresh = {**row, "updated_at": _days(-1)}
    assert C.due_view(fresh)["view"] == C.VIEW_RUNNING


def test_long_review_at_wins_over_age(store):
    task = _mk_wip(store)
    row = {**store.get_wip(task["id"]), "task_class": C.STRUCT_LONG,
           "updated_at": _days(-1), "next_review_at": _days(7)}
    assert C.due_view(row)["view"] == C.VIEW_RUNNING
    row2 = {**row, "next_review_at": _days(-1)}
    assert C.due_view(row2)["view"] == C.VIEW_REVIEW


# ─── 4. 队列顺序 ─────────────────────────────────────────────


def test_queue_order_follows_six_levels(store):
    overdue_once = _mk_wip(store, title="逾期一次性", due_at=_days(-2))
    soon_once = _mk_wip(store, title="本周到期一次性", due_at=_days(1))
    recurring = _mk_todo(store, title="到期的周期", frequency="weekly")
    store.update_todo(recurring["id"], {"next_due_at": _days(-1), "last_done_at": _days(-8)})
    small = _mk_wip(store, title="小任务", extra_data={"one_session": True})
    long_task = _mk_wip(store, title="长期任务")
    paused = _mk_wip(store, title="暂停的")
    store.update_wip(paused["id"], {"status": "paused"})

    queue = store.task_queue()
    by_title = {item["title"]: item for item in queue}

    assert by_title["逾期一次性"]["level"] == 1
    assert by_title["本周到期一次性"]["level"] == 2
    assert by_title["到期的周期"]["level"] == 3
    assert by_title["小任务"]["level"] == 4
    assert by_title["长期任务"]["level"] == 5
    assert by_title["暂停的"]["level"] == 6
    # 暂停的虽然也是长期，但排在最后
    assert queue[-1]["title"] == "暂停的"
    assert "逾期一次性" in by_title
    assert small["id"] in {i["id"] for i in queue}
    assert long_task["id"] in {i["id"] for i in queue}
    assert overdue_once["id"] in {i["id"] for i in queue}


def test_queue_excludes_closed(store):
    task = _mk_wip(store, title="已完成")
    store.update_wip(task["id"], {"status": "completed"})
    titles = {item["title"] for item in store.task_queue()}
    assert "已完成" not in titles


# ─── 5. 报告：映射不上 / 目的轴留空 ──────────────────────────


def test_report_lists_unmapped_and_purpose_unset(store):
    _mk_todo(store, title="事件触发", type="triggered", frequency=None,
             trigger_condition='{"event":"file_arrived"}')
    _mk_wip(store, title="长期无目的")
    report = store.task_summary()
    unmapped_titles = {u["title"] for u in report["unmapped"]}
    assert "事件触发" in unmapped_titles
    unset_titles = {u["title"] for u in report["purpose_unset"]}
    assert "长期无目的" in unset_titles
    # 目的轴不掺 L 档
    assert "L5" not in str(report["delegation"])


def test_report_delegation_mapping(store):
    assert "完全自动" in C.PURPOSE_DELEGATION[C.PURPOSE_DAILY]
    assert "基本自动" in C.PURPOSE_DELEGATION[C.PURPOSE_ONE_SHOT]
    assert "辅助" in C.PURPOSE_DELEGATION[C.PURPOSE_GROWTH]


# ─── 6. 回填：dry-run 不写库 ─────────────────────────────────


def test_classify_tasks_dry_run_writes_nothing(store):
    todo = _mk_todo(store, frequency="weekly")
    _mk_wip(store)
    report = store.classify_tasks(apply=False)
    assert report["applied"] is False
    assert report["planned"] >= 2
    assert store.get_todo(todo["id"])["task_class"] is None
    assert store.get_wip(store.list_wip()[0]["id"])["task_class"] is None


def test_classify_tasks_apply_persists(store):
    todo = _mk_todo(store, frequency="weekly")
    task = _mk_wip(store, due_at=_days(3))
    report = store.classify_tasks(apply=True)
    assert report["applied"] is True
    assert store.get_todo(todo["id"])["task_class"] == C.STRUCT_RECURRING
    assert store.get_todo(todo["id"])["purpose"] == C.PURPOSE_DAILY
    assert store.get_wip(task["id"])["task_class"] == C.STRUCT_ONE_SHOT
    # 二次跑：不再产生变更
    again = store.classify_tasks(apply=False)
    assert again["planned"] == 0


# ─── 7. 人工改分类 ───────────────────────────────────────────


def test_set_task_class_validates(store):
    todo = _mk_todo(store)
    with pytest.raises(ValueError):
        store.set_task_class("todos", todo["id"], task_class="nonsense")
    with pytest.raises(ValueError):
        store.set_task_class("todos", todo["id"], purpose="growth2")
    with pytest.raises(ValueError):
        store.set_task_class("todos", todo["id"], due_at=_days(1))
    with pytest.raises(ValueError):
        store.set_task_class("nope", todo["id"], task_class=C.STRUCT_LONG)


def test_set_task_class_persists(store):
    task = _mk_wip(store)
    out = store.set_task_class("wip_tasks", task["id"],
                               task_class=C.STRUCT_SMALL,
                               purpose=C.PURPOSE_GROWTH)
    assert out is not None
    assert out["task_class"] == C.STRUCT_SMALL
    assert out["purpose"] == C.PURPOSE_GROWTH


# ─── 8. occurrence ───────────────────────────────────────────


def test_materialize_is_idempotent(store):
    todo = _mk_todo(store, frequency="weekly")
    store.update_todo(todo["id"], {"next_due_at": _days(-1), "last_done_at": _days(-8)})
    first = store.materialize_occurrences()
    assert len(first["created"]) == 1
    second = store.materialize_occurrences()
    assert second["created"] == []
    assert len(second["existing"]) == 1
    assert len(store.list_occurrences()) == 1


def test_materialize_skips_future(store):
    todo = _mk_todo(store, frequency="weekly")
    store.update_todo(todo["id"], {"next_due_at": _days(5), "last_done_at": _today()})
    out = store.materialize_occurrences()
    assert out["created"] == []
    assert any("未到期" in s["why"] for s in out["skipped"])


def test_complete_occurrence_advances_template_not_done(store):
    todo = _mk_todo(store, frequency="weekly")
    store.update_todo(todo["id"], {"next_due_at": _days(-1), "last_done_at": _days(-8)})
    store.materialize_occurrences()
    occ = store.list_occurrences("pending")[0]
    out = store.complete_occurrence(occ["id"], evidence="跑完了")
    assert out is not None and out["status"] == "done"
    # 模板不被标 done，只把 next_due 推到下一周期
    template = store.get_todo(todo["id"])
    assert template is not None
    assert template["status"] != "done"
    # next_due 从"完成日"（今天）起算下一周期，不按错过的那次顺推 —— 现有 mark_done 口径
    assert template["next_due_at"] == _days(7)
    assert store.list_occurrences("done")[0]["evidence"] == "跑完了"


def test_complete_occurrence_marks_older_pending_missed(store):
    todo = _mk_todo(store, frequency="weekly")
    store.update_todo(todo["id"], {"next_due_at": _days(-1), "last_done_at": _days(-8)})
    store.materialize_occurrences()
    # 手工插一条更早的 pending，模拟"补做旧的一次"
    store.conn.execute(
        """INSERT INTO task_occurrences (id, template_id, due_at, status)
           VALUES ('occ_old', ?, ?, 'pending')""",
        (todo["id"], _days(-8)),
    )
    store.conn.commit()
    occ = next(o for o in store.list_occurrences("pending") if o["due_at"] == _days(-1))
    store.complete_occurrence(occ["id"], evidence="补做")
    statuses = {o["id"]: o["status"] for o in store.list_occurrences()}
    assert statuses["occ_old"] == "missed"


def test_complete_occurrence_unknown_id(store):
    assert store.complete_occurrence("occ_missing") is None
