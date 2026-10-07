"""v6 A 项（P2 版本模型）回归测试

覆盖 `temp/sdd/memory-v2/plan-A.md` 的验收判据 1/2 与 `as_of` 语义：
- schema v6：`fact_revisions` 表与索引存在、`init_db` 幂等
- 迁移回填 `schema.backfill_fact_revisions`：按 id 定序、最新一条不进历史、幂等
- 运行期捕获 `store._record_superseded_value`：同值不记、链式区间无缝且非空
  （**含同秒连续覆盖的零宽度回归** —— 首版实现就是这么坏的）
- 读侧 `store.resolve_fact_as_of` / `router._normalize_as_of`
- 索引清理 `recorder._cleanup_superseded_index`：清索引、留行、幂等
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

from server.memory.config import MemoryConfig
from server.memory.recorder import InteractionRecorder
from server.memory.schema import SCHEMA_VERSION, backfill_fact_revisions, init_db
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


def _revisions(store, key):
    return store.list_fact_revisions(key)


def _insert_manual(store, key, value, created_at=None):
    """插一条 manual 消息，可指定 created_at（默认只有秒精度，测试需要精确控制）"""
    msg_id = store.insert_message(
        content=json.dumps(value, ensure_ascii=False),
        source="manual", role="system", key=key,
    )
    if created_at:
        store.conn.execute("UPDATE messages SET created_at=? WHERE id=?", (created_at, msg_id))
        store.conn.commit()
    return msg_id


# ==================== 1. schema v6 ====================

class TestSchemaV6:
    def test_version_is_6(self, store):
        row = store.conn.execute("SELECT value FROM schema_info WHERE key='version'").fetchone()
        assert int(row[0]) == SCHEMA_VERSION

    def test_fact_revisions_table_exists(self, store):
        row = store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_revisions'"
        ).fetchone()
        assert row is not None

    def test_fact_revisions_index_exists(self, store):
        row = store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_fact_rev_key'"
        ).fetchone()
        assert row is not None

    def test_key_column_has_no_foreign_key(self, store):
        """key 刻意不设 REFERENCES facts(key)：删 key 时历史必须留下（失效 ≠ 消失）"""
        fks = store.conn.execute("PRAGMA foreign_key_list(fact_revisions)").fetchall()
        assert fks == []

    def test_init_is_idempotent(self, tmp_db):
        conn = init_db(tmp_db)
        conn.close()
        conn2 = init_db(tmp_db)
        conn2.close()


# ==================== 2. 迁移回填 ====================

class TestBackfill:
    def test_backfills_all_but_latest(self, store):
        _insert_manual(store, "k", {"v": 1}, "2026-01-01 00:00:00")
        _insert_manual(store, "k", {"v": 2}, "2026-01-02 00:00:00")
        _insert_manual(store, "k", {"v": 3}, "2026-01-03 00:00:00")

        assert backfill_fact_revisions(store.conn) == 2
        revs = _revisions(store, "k")
        assert [json.loads(r["value"])["v"] for r in revs] == [1, 2]
        # 最新一条（v3）是"当前真相"，不进历史
        assert all(json.loads(r["value"])["v"] != 3 for r in revs)

    def test_chain_tiles_without_gap(self, store):
        for i, ts in enumerate(["2026-01-01 00:00:00", "2026-01-02 00:00:00",
                                "2026-01-03 00:00:00"], start=1):
            _insert_manual(store, "k", {"v": i}, ts)
        backfill_fact_revisions(store.conn)
        revs = _revisions(store, "k")
        assert revs[0]["valid_at"] == "2026-01-01 00:00:00"
        assert revs[0]["invalid_at"] == "2026-01-02 00:00:00"
        assert revs[1]["valid_at"] == revs[0]["invalid_at"]
        assert revs[1]["invalid_at"] == "2026-01-03 00:00:00"

    def test_single_version_key_produces_nothing(self, store):
        _insert_manual(store, "solo", {"v": 1})
        assert backfill_fact_revisions(store.conn) == 0
        assert _revisions(store, "solo") == []

    def test_idempotent(self, store):
        _insert_manual(store, "k", {"v": 1}, "2026-01-01 00:00:00")
        _insert_manual(store, "k", {"v": 2}, "2026-01-02 00:00:00")
        assert backfill_fact_revisions(store.conn) == 1
        assert backfill_fact_revisions(store.conn) == 0
        assert len(_revisions(store, "k")) == 1

    def test_migrated_rows_are_flagged_and_traceable(self, store):
        _insert_manual(store, "k", {"v": 1}, "2026-01-01 00:00:00")
        last_id = _insert_manual(store, "k", {"v": 2}, "2026-01-02 00:00:00")
        backfill_fact_revisions(store.conn)
        rev = _revisions(store, "k")[0]
        assert rev["migrated"] == 1
        assert rev["source_message_id"] is not None
        assert rev["source_message_id"] != last_id


# ==================== 3. 运行期捕获 ====================

class TestSupersedeHistory:
    def test_first_write_creates_no_history(self, store):
        store.upsert_fact("k", json.dumps({"v": 1}))
        assert _revisions(store, "k") == []
        assert store.get_fact("k")["value"] == json.dumps({"v": 1})

    def test_second_write_records_old_value(self, store):
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 2}))
        # facts 仍然只有一条（key UNIQUE 不变）
        n = store.conn.execute("SELECT COUNT(*) FROM facts WHERE key='k'").fetchone()[0]
        assert n == 1
        revs = _revisions(store, "k")
        assert len(revs) == 1
        assert json.loads(revs[0]["value"]) == {"v": 1}
        assert revs[0]["invalid_at"]  # 非空 = 已被取代
        assert revs[0]["migrated"] == 0
        assert revs[0]["source_message_id"] is None

    def test_same_value_records_nothing(self, store):
        """extract_facts_from_kv 每次 set() 都会调 upsert_fact；不比较内容就会刷出噪音历史"""
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 1}))
        assert _revisions(store, "k") == []

    def test_same_second_supersedes_are_both_queryable(self, store):
        """回归：首版实现把 valid_at 取自 facts.updated_at（SQLite 只到秒），
        同秒两次覆盖会得到 [T, T) 的零宽度区间，那条历史永远查不到。"""
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 2}))
        store.upsert_fact("k", json.dumps({"v": 3}))

        revs = _revisions(store, "k")
        assert len(revs) == 2
        for r in revs:
            # 区间必须非空
            assert r["valid_at"] < r["invalid_at"], r
        # 链必须无缝
        assert revs[0]["invalid_at"] == revs[1]["valid_at"]

        # 两条历史都要能按自己的有效区间起点查回来
        for expected in ({"v": 1}, {"v": 2}):
            r = next(x for x in revs if json.loads(x["value"]) == expected)
            hit = store.resolve_fact_as_of("k", r["valid_at"])
            assert hit is not None, f"{expected} 的区间起点查不到"
            assert json.loads(hit[0]) == expected

    def test_history_survives_fact_delete(self, store):
        """失效 ≠ 消失：删掉 key 后历史仍在（memory_delete 只清 facts/messages）"""
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 2}))
        store.delete_fact("k")
        assert len(_revisions(store, "k")) == 1


# ==================== 4. as_of 读侧 ====================

class TestAsOf:
    def test_returns_old_value_in_its_window(self, store):
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 2}))
        rev = _revisions(store, "k")[0]
        value, origin, revision = store.resolve_fact_as_of("k", rev["valid_at"])
        assert json.loads(value) == {"v": 1}
        assert origin == "revision"
        assert revision is not None and revision["id"] == rev["id"]

    def test_falls_back_to_current(self, store):
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 2}))
        value, origin, revision = store.resolve_fact_as_of("k", "2099-01-01 00:00:00")
        assert json.loads(value) == {"v": 2}
        assert origin == "current"
        assert revision is None

    def test_before_all_history_returns_none(self, store):
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 2}))
        assert store.resolve_fact_as_of("k", "1999-01-01 00:00:00") is None

    def test_unknown_key_returns_none(self, store):
        assert store.resolve_fact_as_of("no_such_key", "2099-01-01 00:00:00") is None

    def test_ordering_is_not_reversed(self, store):
        """先查当前值会把"改了之后又查旧日期"错判成当前值"""
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 2}))
        rev = _revisions(store, "k")[0]
        value, origin, _ = store.resolve_fact_as_of("k", rev["invalid_at"])
        # 旧区间的上界是"开区间"，那一刻已经轮到新值
        assert json.loads(value) == {"v": 2}
        assert origin == "current"


class TestNormalizeAsOf:
    def test_date_only_becomes_end_of_day(self):
        from server.memory.router import _normalize_as_of

        assert _normalize_as_of("2026-09-30") == "2026-09-30 23:59:59"

    def test_minute_precision_gets_seconds(self):
        from server.memory.router import _normalize_as_of

        assert _normalize_as_of("2026-09-30 12:30") == "2026-09-30 12:30:00"

    def test_full_timestamp_passthrough(self):
        from server.memory.router import _normalize_as_of

        assert _normalize_as_of("2026-09-30 12:30:45") == "2026-09-30 12:30:45"
        assert _normalize_as_of("2026-09-30 12:30:45.123456") == "2026-09-30 12:30:45.123456"


# ==================== 5. superseded 索引清理 ====================

class TestSupersededIndexCleanup:
    def _seed_superseded(self, store, key="k1"):
        old_id = _insert_manual(store, key, {"v": 1})
        store.upsert_bm25_term("token_old", old_id, 1)
        store.conn.execute(
            "INSERT INTO fact_revisions (key, value, valid_at, invalid_at, "
            "source_message_id, migrated) VALUES (?,?,?,?,?,1)",
            (key, '{"v": 1}', "2026-01-01 00:00:00", "2026-01-02 00:00:00", old_id),
        )
        store.conn.commit()
        return old_id

    def _recorder(self, store):
        return InteractionRecorder(store, None, None, None, MemoryConfig(db_path=store.db_path))  # type: ignore[arg-type]

    def test_removes_index_but_keeps_rows(self, store):
        old_id = self._seed_superseded(store)
        removed = self._recorder(store)._cleanup_superseded_index()
        assert removed == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM bm25_inverted WHERE message_id=?", (old_id,)
        ).fetchone()[0] == 0
        # 行必须保留（metrics 的 superseded 指标就靠这些行算）
        assert store.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE id=?", (old_id,)
        ).fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT compressed FROM messages WHERE id=?", (old_id,)
        ).fetchone()[0] == 1

    def test_idempotent(self, store):
        self._seed_superseded(store)
        rec = self._recorder(store)
        assert rec._cleanup_superseded_index() == 1
        assert rec._cleanup_superseded_index() == 0

    def test_ignores_revisions_without_source_message(self, store):
        """运行期写入的历史没有 source_message_id，不该误伤任何消息"""
        store.upsert_fact("k", json.dumps({"v": 1}))
        store.upsert_fact("k", json.dumps({"v": 2}))
        keep_id = _insert_manual(store, "other", {"v": 9})
        store.upsert_bm25_term("token_keep", keep_id, 1)
        assert self._recorder(store)._cleanup_superseded_index() == 0
        assert store.conn.execute(
            "SELECT COUNT(*) FROM bm25_inverted WHERE message_id=?", (keep_id,)
        ).fetchone()[0] == 1


# ==================== 6. 快照行 / 事件行 ====================

class TestSnapshotRow:
    def test_get_kv_snapshot_id_picks_latest_manual(self, store):
        assert store.get_kv_snapshot_id("k") is None
        first = _insert_manual(store, "k", {"v": 1})
        second = _insert_manual(store, "k", {"v": 2})
        store.insert_message(content="{}", source="event", role="system", key="k")
        assert store.get_kv_snapshot_id("k") == second
        assert second > first

    def test_update_message_content_keeps_timestamp(self, store):
        """原地更新不动 timestamp：这一行的身份是"该 key 的记忆快照"，
        创建时间要保留首次写入的时刻（改过几次由 fact_revisions/事件行表达）"""
        msg_id = _insert_manual(store, "k", {"v": 1})
        before = store.conn.execute(
            "SELECT timestamp, created_at FROM messages WHERE id=?", (msg_id,)
        ).fetchone()
        store.update_message_content(msg_id, json.dumps({"v": 2}))
        after = store.conn.execute(
            "SELECT content, timestamp, created_at FROM messages WHERE id=?", (msg_id,)
        ).fetchone()
        assert json.loads(after[0]) == {"v": 2}
        assert after[1] == before[0]
        assert after[2] == before[1]

    def test_delete_bm25_terms_by_message(self, store):
        """BM25 的 upsert 是 tf 累加，不显式删旧词会退化成"新旧内容的并集" """
        msg_id = _insert_manual(store, "k", {"v": 1})
        store.upsert_bm25_term("t1", msg_id, 1)
        store.upsert_bm25_term("t2", msg_id, 1)
        store.delete_bm25_terms_by_message(msg_id)
        assert store.conn.execute(
            "SELECT COUNT(*) FROM bm25_inverted WHERE message_id=?", (msg_id,)
        ).fetchone()[0] == 0

    def test_upsert_bm25_term_accumulates_tf(self, store):
        """坐实"必须显式删"这个前提：同一 (term,message_id) 再插是 tf 累加而非替换"""
        msg_id = _insert_manual(store, "k", {"v": 1})
        store.upsert_bm25_term("t1", msg_id, 1)
        store.upsert_bm25_term("t1", msg_id, 1)
        tf = store.conn.execute(
            "SELECT tf FROM bm25_inverted WHERE message_id=? AND term='t1'", (msg_id,)
        ).fetchone()[0]
        assert tf == 2


# ==================== 7. 历史行与 metrics 口径一致 ====================

class TestMetricAgreement:
    def test_revision_rows_match_superseded_metric_definition(self, store):
        """迁移回填的行数必须与 metrics._superseded 的定义严格相等：
        两边都算"每个 key 除最新一条 manual 之外的其余行"。对不上就说明定序/过滤写错了。"""
        shapes = {"a": 3, "b": 1, "c": 2}
        for key, n in shapes.items():
            for i in range(n):
                _insert_manual(store, key, {"v": i}, "2026-01-0%d 00:00:00" % (i + 1))
        assert backfill_fact_revisions(store.conn) == 3

        # 复刻 metrics._superseded 的 SQL（含 key IS NULL 的边界）
        expected = store.conn.execute(
            """SELECT COUNT(*) FROM messages m
               WHERE m.source='manual' AND m.key IS NOT NULL
                 AND m.id < (SELECT MAX(m2.id) FROM messages m2
                             WHERE m2.key = m.key AND m2.source='manual')"""
        ).fetchone()[0]
        assert store.conn.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == expected
