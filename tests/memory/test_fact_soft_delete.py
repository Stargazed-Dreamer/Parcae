"""C1 回归：删除改为失效（``facts.archived_at``）+ restore。

用户 2026-10-18 拍板：「C1 硬删列为议题包 2 的前置，先修再谈边界」。
验收判据（`workspace/local_butler/20_决策过程/20_记忆第二轮/07_改造计划任务清单.md` T-C1）：
**删一条 → 库中仍有行与 revision → 可 restore → 列表可见"已失效"而非消失。**

改动前的缺陷：``store.delete_fact`` 是 ``DELETE FROM facts WHERE key = ?``（物理删），
与硬约束「``archive ≠ 删除``」「``superseded ≠ 物理删``」双冲突。
"""

import os
import sqlite3
import tempfile

import pytest

from server.memory.schema import SCHEMA_VERSION, init_db
from server.memory.store import MemoryStore


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store(tmp_db):
    s = MemoryStore(tmp_db)
    s.initialize()
    yield s
    s.close()


# ─── schema ───────────────────────────────────────────────


def test_schema_v7_has_archived_at(tmp_db):
    conn = init_db(tmp_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
    assert "archived_at" in cols
    assert SCHEMA_VERSION >= 7
    conn.close()


def test_migration_idempotent(tmp_db):
    """重复 init_db 不报错（ALTER TABLE 不支持 IF NOT EXISTS ⇒ 必须自己判列）。"""
    init_db(tmp_db).close()
    conn = init_db(tmp_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()]
    assert cols.count("archived_at") == 1
    conn.close()


# ─── 删除 = 失效 ─────────────────────────────────────────


def test_delete_archives_row_instead_of_removing(store, tmp_db):
    store.upsert_fact("user.profile", '{"name": "A"}', source="manual")
    assert store.delete_fact("user.profile") is True

    # 行还在（物理删的话这里会是 0）
    conn = sqlite3.connect(tmp_db)
    n = conn.execute("SELECT COUNT(*) FROM facts WHERE key = 'user.profile'").fetchone()[0]
    archived = conn.execute(
        "SELECT archived_at FROM facts WHERE key = 'user.profile'"
    ).fetchone()[0]
    conn.close()
    assert n == 1, "删除必须保留行（否则不可恢复）"
    assert archived, "archived_at 必须被写上"


def test_delete_twice_returns_false(store):
    store.upsert_fact("k2", "v")
    assert store.delete_fact("k2") is True
    assert store.delete_fact("k2") is False, "重复删除应返回 False（已经不是活跃态）"


def test_delete_missing_returns_false(store):
    assert store.delete_fact("no.such.key") is False


# ─── 失效 = 读不到 ───────────────────────────────────────


def test_archived_not_readable(store):
    store.upsert_fact("k3", "v3")
    store.delete_fact("k3")
    assert store.get_fact("k3") is None, "失效后 GET 必须 404 语义"
    assert store.get_fact_meta("k3") is None, "失效后索引构建也读不到"


def test_archived_not_in_list(store):
    store.upsert_fact("active", "a")
    store.upsert_fact("dead", "d")
    store.delete_fact("dead")

    keys = {f["key"] for f in store.list_facts(limit=50)}
    assert "active" in keys
    assert "dead" not in keys, "默认列表不能出现已失效项"

    keys_all = {f["key"] for f in store.list_facts(limit=50, include_archived=True)}
    assert {"active", "dead"} <= keys_all, "include_archived=True 要把失效项也带出来"


def test_archived_not_counted_as_deleted_row(store):
    """get_fact_raw 仍能读到（给恢复/审计用），这是它与 get_fact 的唯一区别。"""
    store.upsert_fact("k4", "v4")
    store.delete_fact("k4")
    assert store.get_fact("k4") is None
    raw = store.get_fact_raw("k4")
    assert raw is not None and raw["archived_at"]


# ─── restore ─────────────────────────────────────────────


def test_restore_brings_it_back(store):
    store.upsert_fact("k5", "v5", source="manual")
    store.delete_fact("k5")
    assert store.restore_fact("k5") is True
    got = store.get_fact("k5")
    assert got is not None
    assert got["archived_at"] is None
    assert got["value"] == "v5"


def test_restore_active_returns_false(store):
    store.upsert_fact("k6", "v6")
    assert store.restore_fact("k6") is False, "本来就是活跃的，不该报成功"


def test_restore_missing_returns_false(store):
    assert store.restore_fact("never.existed") is False


# ─── 写入复活 ────────────────────────────────────────────


def test_upsert_revives_archived_key(store):
    store.upsert_fact("k7", "old")
    store.delete_fact("k7")
    assert store.get_fact("k7") is None

    store.upsert_fact("k7", "new")
    got = store.get_fact("k7")
    assert got is not None, "同 key 再写入必须复活（否则新记忆写进去还是'已删'）"
    assert got["value"] == "new"
    assert got["archived_at"] is None


# ─── 历史不丢 ────────────────────────────────────────────


def test_revisions_survive_delete(store):
    store.upsert_fact("k8", "v1", source="manual")
    store.upsert_fact("k8", "v2", source="manual")  # 产生一条 revision
    before = store.list_fact_revisions("k8")
    assert before, "前置条件：应该有历史"

    store.delete_fact("k8")
    store.restore_fact("k8")
    after = store.list_fact_revisions("k8")
    assert len(after) == len(before), "失效/恢复不得改动历史链"


def test_archived_list(store):
    store.upsert_fact("a1", "x")
    store.upsert_fact("a2", "y")
    store.delete_fact("a1")
    rows = store.list_archived_facts()
    keys = {r["key"] for r in rows}
    assert "a1" in keys and "a2" not in keys
