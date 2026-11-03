"""X8 验收：账本格式版本与 v1→v2 迁移链。

覆盖 tickets.md T1：
- 新建库直接到 format 2
- schema v9 旧库先标 1 再升 2，旧行字节不变
- 更高版本 / 畸形版本 / 无法判定基线 → 拒绝启动
- 迁移链缺步 → 拒绝启动
- 迁移步骤失败 → 整代回滚，版本停在 1
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import pytest

from lib.agent_runtime import event_store as event_store_module
from lib.agent_runtime.event_store import (
    LEDGER_FORMAT_VERSION,
    EventStore,
    LedgerFormatError,
    LedgerFormatTooNew,
)

_OLD_EVENT_COLUMNS = (
    "seq, session_id, type, payload_json, trace_id, prompt_index, "
    "invalidated_seq, parent_trace_id, depth, created_at"
)
_OLD_MESSAGE_COLUMNS = (
    "id, session_id, seq, role, content_json, tool_call_id, source, "
    "visible, created_at, tool_calls_json, thinking_json, model"
)


def _build_legacy_v9_db(db_path: str) -> None:
    """按当前 v9 形状建库但**不写 format_version**，并落几行旧数据。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(event_store_module._SCHEMA_SQL)
        conn.execute(
            "INSERT INTO sessions(id, title, mode, status, created_at, updated_at) "
            "VALUES('s-legacy', 'legacy session', 'headless', 'completed', 1000.5, 2000.5)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, seq, role, content_json, source, visible, created_at) "
            "VALUES('m-legacy', 's-legacy', 1, 'user', '\"hello\"', 'user', 1, 1001.5)"
        )
        conn.execute(
            "INSERT INTO events(seq, session_id, type, payload_json, created_at) "
            "VALUES(1, 's-legacy', 'user_message', '{\"text\":\"hello\"}', 1001.5)"
        )
        conn.execute(
            "INSERT INTO tool_calls(id, session_id, seq, name, status) "
            "VALUES('tc-legacy', 's-legacy', 1, 'exec_python', 'completed')"
        )
        conn.execute(
            "INSERT INTO usage(id, session_id, seq, total_tokens, created_at) "
            "VALUES('u-legacy', 's-legacy', 1, 42, 1002.5)"
        )
        conn.execute("INSERT INTO schema_info(key, value) VALUES('version', '9')")
        conn.commit()
    finally:
        conn.close()


def _snapshot_old_rows(db_path: str) -> tuple[str, list[tuple], list[tuple]]:
    """只取旧列；迁移前后应逐字节相同。"""
    conn = sqlite3.connect(db_path)
    try:
        events = [
            tuple(r)
            for r in conn.execute(
                f"SELECT {_OLD_EVENT_COLUMNS} FROM events ORDER BY session_id, seq"
            ).fetchall()
        ]
        messages = [
            tuple(r)
            for r in conn.execute(
                f"SELECT {_OLD_MESSAGE_COLUMNS} FROM messages ORDER BY session_id, seq"
            ).fetchall()
        ]
    finally:
        conn.close()
    digest = hashlib.sha256(repr((events, messages)).encode("utf-8")).hexdigest()
    return digest, events, messages


def _columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _tables(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()


def _schema_info(db_path: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            str(r[0]): str(r[1])
            for r in conn.execute("SELECT key, value FROM schema_info").fetchall()
        }
    finally:
        conn.close()


def test_new_db_reaches_format_2(tmp_db_path):
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        assert store.format_version == LEDGER_FORMAT_VERSION == 2
        info = _schema_info(tmp_db_path)
        assert info["format_version"] == "2"
        assert info["version"] == "9"  # _SCHEMA_VERSION 仍独立记录
    finally:
        store.close()

    assert "domain" in _columns(tmp_db_path, "events")
    assert {"proj_stats", "proj_turn_outline", "proj_title"} <= _tables(tmp_db_path)


def test_legacy_v9_is_stamped_baseline_then_migrated_without_touching_old_rows(tmp_db_path):
    _build_legacy_v9_db(tmp_db_path)
    before_hash, before_events, before_messages = _snapshot_old_rows(tmp_db_path)
    assert "format_version" not in _schema_info(tmp_db_path)
    assert "domain" not in _columns(tmp_db_path, "events")

    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        assert store.format_version == 2
        assert _schema_info(tmp_db_path)["format_version"] == "2"
    finally:
        store.close()

    after_hash, after_events, after_messages = _snapshot_old_rows(tmp_db_path)
    assert after_hash == before_hash
    assert after_events == before_events
    assert after_messages == before_messages

    conn = sqlite3.connect(tmp_db_path)
    try:
        row = conn.execute(
            "SELECT domain FROM events WHERE session_id='s-legacy' AND seq=1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "persistent"


def test_newer_format_is_rejected_before_any_schema_write(tmp_db_path):
    _build_legacy_v9_db(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute("INSERT INTO schema_info(key, value) VALUES('format_version', '99')")
        conn.commit()
    finally:
        conn.close()

    store = EventStore(db_path=tmp_db_path)
    with pytest.raises(LedgerFormatTooNew):
        store.init()
    store.close()

    assert _schema_info(tmp_db_path)["format_version"] == "99"
    assert "domain" not in _columns(tmp_db_path, "events")
    assert "proj_stats" not in _tables(tmp_db_path)


def test_malformed_format_is_rejected(tmp_db_path):
    _build_legacy_v9_db(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute("INSERT INTO schema_info(key, value) VALUES('format_version', 'abc')")
        conn.commit()
    finally:
        conn.close()

    store = EventStore(db_path=tmp_db_path)
    with pytest.raises(LedgerFormatError, match="malformed format_version"):
        store.init()
    store.close()


def test_schema_info_without_any_version_is_rejected(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute("CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()

    store = EventStore(db_path=tmp_db_path)
    with pytest.raises(LedgerFormatError, match="cannot determine baseline"):
        store.init()
    store.close()


def test_missing_chain_step_is_rejected(tmp_db_path, monkeypatch):
    _build_legacy_v9_db(tmp_db_path)
    monkeypatch.delitem(event_store_module._LEDGER_MIGRATIONS, 1)

    store = EventStore(db_path=tmp_db_path)
    with pytest.raises(LedgerFormatError, match="chain incomplete"):
        store.init()
    store.close()

    # 链校验发生在任何写操作之前：基线戳和 domain 列都不该出现
    assert "format_version" not in _schema_info(tmp_db_path)
    assert "domain" not in _columns(tmp_db_path, "events")


def test_failing_migration_rolls_back_and_keeps_baseline(tmp_db_path, monkeypatch):
    _build_legacy_v9_db(tmp_db_path)

    def _boom(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE events ADD COLUMN domain TEXT NOT NULL DEFAULT 'persistent'")
        conn.execute("CREATE TABLE proj_stats (session_id TEXT PRIMARY KEY)")
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(
        event_store_module._LEDGER_MIGRATIONS, 1, ("injected-failure", _boom)
    )

    store = EventStore(db_path=tmp_db_path)
    with pytest.raises(LedgerFormatError, match="injected-failure"):
        store.init()
    store.close()

    assert _schema_info(tmp_db_path)["format_version"] == "1"
    assert "domain" not in _columns(tmp_db_path, "events")
    assert "proj_stats" not in _tables(tmp_db_path)


def test_reinit_is_idempotent_and_keeps_session_data(tmp_db_path):
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        asyncio.run(store.create_session(session_id="s-reinit", title="before reinit"))
        assert store.format_version == 2
    finally:
        store.close()

    store2 = EventStore(db_path=tmp_db_path)
    store2.init()
    try:
        assert store2.format_version == 2
        session = asyncio.run(store2.get_session("s-reinit"))
        assert session is not None
        assert session.title == "before reinit"
    finally:
        store2.close()
