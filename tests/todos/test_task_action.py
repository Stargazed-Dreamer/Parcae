"""T14：统一任务动作面（todos / wip 同一套状态词）。"""

from __future__ import annotations

import pytest

from server.todos.store import TodosStore


@pytest.fixture()
def store(tmp_path):
    s = TodosStore(str(tmp_path / "tasks.db"))
    s.initialize()
    yield s
    s.close()


def test_todo_and_wip_share_action_vocabulary(store):
    todo = store.create_todo({"title": "周期任务", "type": "recurring"})
    wip = store.create_wip({"title": "长期任务", "status": "active"})

    for task_id, scope in ((todo["id"], "todos"), (wip["id"], "wip")):
        paused = store.apply_task_action(scope, task_id, "pause")
        assert paused["status"] == "paused"
        assert paused["table"] == ("todos" if scope == "todos" else "wip_tasks")
        resumed = store.apply_task_action(scope, task_id, "resume")
        assert resumed["status"] == "active"
        archived = store.apply_task_action(scope, task_id, "archive")
        assert archived["status"] == "archived"
        restored = store.apply_task_action(scope, task_id, "unarchive")
        assert restored["status"] == "active"


def test_complete_wip_sets_progress_100(store):
    wip = store.create_wip({"title": "做完它", "status": "active"})
    done = store.apply_task_action("wip", wip["id"], "complete")
    assert done["status"] == "completed"
    assert done["progress"] == 100


def test_complete_recurring_todo_advances_next_due(store):
    todo = store.create_todo({
        "title": "周报", "type": "recurring", "frequency": "weekly",
        "next_due_at": "2026-10-27",
    })
    done = store.apply_task_action("todos", todo["id"], "complete")
    assert done["status"] == "active"  # mark_done 后回到可执行态
    assert done["next_due_at"] > "2026-10-27"
    assert not any(t["id"] == todo["id"] for t in store.get_due_todos())


def test_paused_todo_is_not_due(store):
    todo = store.create_todo({"title": "暂停我", "type": "recurring"})
    assert any(t["id"] == todo["id"] for t in store.get_due_todos())
    store.apply_task_action("todos", todo["id"], "pause")
    assert not any(t["id"] == todo["id"] for t in store.get_due_todos())


def test_list_tasks_view_returns_canonical_rows(store):
    todo = store.create_todo({"title": "周期", "type": "recurring"})
    wip = store.create_wip({"title": "长期", "status": "active"})
    rows = {r["id"]: r for r in store.list_tasks_view()}
    assert rows[todo["id"]]["status"] == "active"
    assert rows[todo["id"]]["table"] == "todos"
    assert rows[wip["id"]]["status"] == "active"
    assert rows[wip["id"]]["table"] == "wip_tasks"


def test_unknown_action_and_missing_task(store):
    wip = store.create_wip({"title": "x"})
    with pytest.raises(ValueError):
        store.apply_task_action("wip", wip["id"], "explode")
    assert store.apply_task_action("wip", "missing", "pause") is None


# ---- v15：归档记时间戳、原状态可还原 -------------------------------------


def test_archive_records_time_and_prev_status(store):
    """归档一个已完成的事务：恢复后必须还是 completed，不能变成 active。"""
    wip = store.create_wip({"title": "归档我", "status": "active"})
    assert store.apply_task_action("wip", wip["id"], "complete")["status"] == "completed"

    arch = store.apply_task_action("wip", wip["id"], "archive", "面板归档")
    assert arch["status"] == "archived"
    assert arch["archive_reason"] == "面板归档"
    assert arch["archive_prev_status"] == "completed"
    assert arch["archived_at"]

    back = store.apply_task_action("wip", wip["id"], "unarchive")
    assert back["status"] == "completed", "恢复要回到归档前那一档，不是 active"
    assert back["archived_at"] is None
    assert back["archive_prev_status"] is None
    assert back["archive_reason"] is None


def test_archive_twice_keeps_first_prev_status(store):
    """重复归档不该把 prev_status 覆盖成 'archived'。"""
    wip = store.create_wip({"title": "重复归档", "status": "active"})
    store.apply_task_action("wip", wip["id"], "pause")
    first = store.apply_task_action("wip", wip["id"], "archive")
    assert first["archive_prev_status"] == "paused"
    second = store.apply_task_action("wip", wip["id"], "archive")
    assert second["archive_prev_status"] == "paused"
    assert store.apply_task_action("wip", wip["id"], "unarchive")["status"] == "paused"


def test_archived_excluded_when_asked(store):
    """归档视图判据沿用 status='archived'，与 include_archived 一致。"""
    a = store.create_wip({"title": "留着"})
    b = store.create_wip({"title": "收走"})
    store.apply_task_action("wip", b["id"], "archive")
    ids = {r["id"] for r in store.list_tasks_view(include_archived=False)}
    assert a["id"] in ids and b["id"] not in ids
    assert b["id"] in {r["id"] for r in store.list_tasks_view(include_archived=True)}
