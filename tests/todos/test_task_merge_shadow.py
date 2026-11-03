"""T14：tasks 影子迁移（先建影子，不切读写）。"""

from __future__ import annotations

import sqlite3

from server.todos.store import TODOS_SCHEMA_SQL, TodosStore


def _legacy_db(path: str) -> None:
    """造一个“合并前”的库：只有 todos / wip_tasks 有数据，tasks 还不存在。"""
    conn = sqlite3.connect(path)
    conn.executescript(TODOS_SCHEMA_SQL)
    conn.execute("DROP TABLE tasks")
    conn.execute("DROP TABLE task_merge_meta")
    conn.execute(
        """INSERT INTO todos (id, title, type, status, next_due_at, created_at, updated_at)
           VALUES ('todo_skip', '跳过的周期', 'recurring', 'skipped', '2026-10-28',
                   '2026-10-01 00:00:00', '2026-10-01 00:00:00')"""
    )
    conn.execute(
        """INSERT INTO wip_tasks (id, title, status, due_at, created_at, updated_at)
           VALUES ('wip_due', '有日期的长期', 'active', '2026-11-01',
                   '2026-10-01 00:00:00', '2026-10-01 00:00:00')"""
    )
    conn.commit()
    conn.close()


def test_shadow_migration_maps_status_and_kind(tmp_path):
    db = str(tmp_path / "legacy.db")
    _legacy_db(db)

    store = TodosStore(db)
    store.initialize()
    rows = {r[0]: dict(r) for r in store.conn.execute(
        "SELECT id, kind, status, extra_data FROM tasks"
    )}
    assert rows["todo_skip"]["kind"] == "recurring"
    assert rows["todo_skip"]["status"] == "completed"
    assert "skipped" in rows["todo_skip"]["extra_data"]
    assert rows["wip_due"]["kind"] == "one_shot"
    assert rows["wip_due"]["status"] == "active"
    assert store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2

    # 幂等：再开一次不重复搬
    store.close()
    store = TodosStore(db)
    store.initialize()
    assert store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    flag = store.conn.execute(
        "SELECT value FROM task_merge_meta WHERE key='tasks_v1'"
    ).fetchone()
    assert flag is not None
    store.close()


def test_shadow_migration_empty_db_does_not_mark_done(tmp_path):
    store = TodosStore(str(tmp_path / "empty.db"))
    store.initialize()
    assert store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM task_merge_meta WHERE key='tasks_v1'"
    ).fetchone()[0] == 0
    store.close()
