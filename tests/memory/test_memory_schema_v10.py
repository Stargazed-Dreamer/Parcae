"""Schema v10 迁移测试（P5 T1）

覆盖：
- 新库直接建 v10：butler_reports 表 + butler_runs / butler_deliveries 新列
- v9 → v10 迁移：只加表/加列，老数据不受影响
- 迁移幂等（重复调用不报错）
- domain:<id> scope 校验放宽
"""

from __future__ import annotations

import sqlite3

from server.memory.schema import (
    SCHEMA_VERSION,
    V10_DELIVERY_COLUMNS,
    V10_RUN_COLUMNS,
    _ensure_columns,
    init_db,
    migrate_db,
)
from server.memory.wrapup import _valid_scope


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_is_v10(tmp_path):
    conn = init_db(str(tmp_path / "fresh.db"))
    try:
        version = conn.execute(
            "SELECT value FROM schema_info WHERE key='version'"
        ).fetchone()[0]
        assert int(version) == SCHEMA_VERSION >= 10
        assert _has_table(conn, "butler_reports")
        assert {"run_id", "attempt", "outcome", "summary",
                "payload_json", "idempotency_key"} <= _columns(conn, "butler_reports")
        assert {name for name, _ in V10_RUN_COLUMNS} <= _columns(conn, "butler_runs")
        assert {name for name, _ in V10_DELIVERY_COLUMNS} <= _columns(conn, "butler_deliveries")
    finally:
        conn.close()


def test_migrate_v9_to_v10_adds_only(tmp_path):
    """手工造一个 v9 最小库，确认迁移只加表/加列，不动老行。"""
    path = tmp_path / "v9.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_info (key, value) VALUES ('version', '9');
        CREATE TABLE butler_runs (run_id TEXT PRIMARY KEY, status TEXT);
        INSERT INTO butler_runs (run_id, status) VALUES ('r-old', 'closed');
        CREATE TABLE butler_deliveries (id INTEGER PRIMARY KEY, run_id TEXT);
        INSERT INTO butler_deliveries (id, run_id) VALUES (1, 'r-old');
    """)
    conn.commit()

    migrate_db(conn)

    assert int(conn.execute(
        "SELECT value FROM schema_info WHERE key='version'"
    ).fetchone()[0]) == SCHEMA_VERSION
    assert _has_table(conn, "butler_reports")
    assert {n for n, _ in V10_RUN_COLUMNS} <= _columns(conn, "butler_runs")
    assert {n for n, _ in V10_DELIVERY_COLUMNS} <= _columns(conn, "butler_deliveries")
    # 老行仍在
    assert conn.execute(
        "SELECT status FROM butler_runs WHERE run_id='r-old'"
    ).fetchone()[0] == "closed"
    conn.close()


def test_migrate_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "idem.db"))
    try:
        before_runs = _columns(conn, "butler_runs")
        before_deliv = _columns(conn, "butler_deliveries")
        migrate_db(conn)  # 再跑一次
        assert _columns(conn, "butler_runs") == before_runs
        assert _columns(conn, "butler_deliveries") == before_deliv
        assert _ensure_columns(conn, "butler_runs", V10_RUN_COLUMNS) == []
    finally:
        conn.close()


def test_domain_scope_allowed():
    assert _valid_scope("global")
    assert _valid_scope("project:p1")
    assert _valid_scope("topic:t1")
    assert _valid_scope("domain:home")
    assert not _valid_scope("random:xx")
    assert not _valid_scope("domain:")
