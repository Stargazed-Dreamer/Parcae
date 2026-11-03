"""记忆库审计与召回的回归测试（v5 起；2026-10-31 随验证器删除缩编）

覆盖：
- 指标日聚合 `server/memory/metrics.py`：漏斗 / 归档 / 索引卫生 / 遥测指标可查
- 老化 `run_aging`：只写 `stale_score`，**不再翻转 `validation_status`**
  （LLM 验证器已于 2026-10-31 删除，四列冻结为历史字段）
- 通配限槽 `agent_guide._select_must_read`：`"*"` 记忆最多占 1 个必读位

被删掉的三类（不要再写回来，除非验证器也跟着回来）：`build_validation_preview`
喂料、超窗延判、无效理由拒绝、`requeue_all_validated` —— 它们测的是「怎么判生死」，
而那套东西已整块移除（见 `30_架构与设计/28` 的 `R-VALIDATE`）。
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from server.agent_guide import _MUST_READ_MAX, _select_must_read
from server.memory import metrics as mem_metrics
from server.memory.config import MemoryConfig
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


@pytest.fixture
def maintainer(store):
    from server.memory.maintainer import MemoryMaintainer

    cfg = MemoryConfig(db_path=store.db_path)
    return MemoryMaintainer(store, cfg)


def _seed_fact(store, key, value: dict, *, fact_type=None, contexts=None,
               keywords=None, updated_at=None) -> None:
    store.upsert_fact(
        key, json.dumps(value, ensure_ascii=False), source="manual",
        fact_type=fact_type, consumption_contexts=contexts, trigger_keywords=keywords,
    )
    if updated_at:
        store.conn.execute("UPDATE facts SET updated_at=? WHERE key=?", (updated_at, key))
        store.conn.commit()


# ==================== 1. schema ====================

class TestSchemaV5:
    def test_metrics_table_exists(self, store):
        row = store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_metrics_daily'"
        ).fetchone()
        assert row is not None

    def test_frozen_validation_columns_still_exist(self, store):
        """验证器删了，但四列**不删**（SQLite 删列代价大，且历史判决仍可查）。"""
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(memory_meta)")}
        assert {"validation_status", "last_validated_at",
                "validation_note", "validate_attempts"} <= cols

    def test_init_is_idempotent(self, tmp_db):
        from server.memory.schema import init_db

        conn = init_db(tmp_db)
        conn.close()
        conn2 = init_db(tmp_db)  # 第二次跑迁移不应报错
        conn2.close()


# ==================== 2. 老化只写 stale_score ====================

class TestAgingWritesOnlyStaleScore:
    """2026-10-31 验证器删除后，老化不再翻转 validation_status。

    这组是「删干净」的看门测试：如果哪天有人把 validation_status 的写入加回来，
    要么是验证器真的回来了（那就该把提示词、开关、测试一起带回来），
    要么是一次没想清楚的回归——两种都该让测试先响。
    """

    def _make_stale(self, store, key, status="archive"):
        _seed_fact(store, key, {"summary": "x"}, fact_type="project")
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        store.conn.execute("UPDATE facts SET updated_at=? WHERE key=?", (old, key))
        store.conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, validation_status) VALUES (?, ?)",
            (key, status),
        )
        store.conn.commit()

    def test_stale_score_written_and_frozen_label_untouched(self, maintainer, store):
        self._make_stale(store, "old_archive")
        result = maintainer.run_aging()

        assert result["stale_count"] == 1
        score, status = store.conn.execute(
            "SELECT stale_score, validation_status FROM memory_meta WHERE key='old_archive'"
        ).fetchone()
        assert score >= 29
        assert status == "archive"      # 历史标签冻结，不再被翻回 pending

    def test_result_has_no_revalidation_keys(self, maintainer, store):
        self._make_stale(store, "no_gate")
        result = maintainer.run_aging()
        assert "revalidate_days" not in result
        assert "requeued_for_revalidation" not in result

    def test_run_has_no_validate_stage(self, maintainer, store):
        import asyncio

        self._make_stale(store, "no_stage")
        res = asyncio.run(maintainer.run())
        assert "validate" not in res
        assert set(res) >= {"prefilter", "aging", "metrics"}

    def test_get_status_reports_validator_removed(self, maintainer, store):
        st = maintainer.get_status()
        assert st["validator"] == "removed_2026-10-31"
        assert "validate_enabled" not in st
        assert "model_tier" not in st
        assert "archived_total" in st


# ==================== 3. 指标日聚合 ====================

class TestRecordDaily:
    def test_writes_funnel_metrics(self, store):
        _seed_fact(store, "shown", {"summary": "会上屏"}, contexts=["recurring.accounting"])
        _seed_fact(store, "cut", {"summary": "被砍"}, contexts=["*"])
        _seed_fact(store, "quiet", {"summary": "从没进候选"}, contexts=["dev.grilling"])

        for _ in range(3):
            store.conn.execute(
                "INSERT INTO context_projections (surface, task_type, selected_keys, dropped_keys,"
                " injected_text_digest, injected_chars) VALUES (?, ?, ?, ?, ?, ?)",
                ("agent_guide.must_read", "recurring.accounting",
                 json.dumps(["shown"]), json.dumps(["cut"]), "dig", 100),
            )
        store.conn.commit()

        result = mem_metrics.record_daily(store.conn, db_path=store.db_path)
        assert result["errors"] == []
        assert result["written"] > 0

        rows = dict(store.conn.execute(
            "SELECT metric, value FROM memory_metrics_daily"
        ).fetchall())
        assert rows["facts_total"] == 3
        assert rows["projection_calls"] == 3
        assert rows["surfaced_ratio"] == pytest.approx(1 / 3, abs=0.001)

        extra = json.loads(store.conn.execute(
            "SELECT extra FROM memory_metrics_daily WHERE metric='surfaced_ratio'"
        ).fetchone()[0])
        assert extra["facts_surfaced"] == 1
        assert extra["facts_dropped_only"] == 1
        assert extra["facts_never_surfaced"] == 1
        assert extra["cumulative_dropped_keys"] == 3

    def test_second_run_does_not_duplicate(self, store):
        _seed_fact(store, "a", {"summary": "x"}, contexts=["*"])
        first = mem_metrics.record_daily(store.conn, db_path=store.db_path)
        second = mem_metrics.record_daily(store.conn, db_path=store.db_path)
        count = store.conn.execute(
            "SELECT COUNT(*) FROM memory_metrics_daily"
        ).fetchone()[0]
        assert count == first["written"] == second["written"]

    def test_archive_high_freq_conflict_is_flagged(self, store):
        """被判 archive 却高频上屏 = 标签与使用互相打脸，必须报出来"""
        _seed_fact(store, "zombie", {"summary": "账面已死"}, contexts=["*"])
        store.conn.execute(
            "UPDATE facts SET archived_at=? WHERE key='zombie'",
            (datetime.now().isoformat(),),
        )
        for _ in range(25):
            store.conn.execute(
                "INSERT INTO context_projections (surface, selected_keys, injected_text_digest,"
                " injected_chars) VALUES (?, ?, ?, ?)",
                ("agent_guide.must_read", json.dumps(["zombie"]), "dig", 50),
            )
        store.conn.commit()

        snap = mem_metrics.compute_metrics(store.conn, db_path=store.db_path)
        assert snap["archive_high_freq_conflicts"][0] == 1
        assert snap["archive_high_freq_conflicts"][1]["samples"][0][0] == "zombie"

    def test_db_size_history_survives_restart(self, store):
        """旧实现只存内存 dict，后端重启即归零，健康分的 DB 增长维度永远算不到"""
        store.query_db_size_history()
        store.conn.execute(
            "UPDATE memory_metrics_daily SET date=? WHERE metric='db_size_mb'",
            ((datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"),),
        )
        store.conn.commit()
        # 模拟"新进程"：另开一个 store 指向同一库，内存缓存是空的
        s2 = MemoryStore(store.db_path)
        s2.initialize()
        try:
            hist = s2.query_db_size_history()
        finally:
            s2.close()
        assert len(hist) >= 2
        assert {h["date"] for h in hist} >= {(datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")}

    def test_query_metric_history_ascending(self, store):
        for i in range(3):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            mem_metrics.put_metric(store.conn, d, "db_size_mb", 10.0 + i)
        store.conn.commit()
        hist = mem_metrics.query_metric_history(store.conn, "db_size_mb", days=7)
        assert len(hist) == 3
        # 按日期升序（最旧在前），最新值在数组末尾——健康分按 history[-1] vs history[0] 算周增长
        assert [h["date"] for h in hist] == sorted(h["date"] for h in hist)
        assert hist[-1]["date"] == datetime.now().strftime("%Y-%m-%d")


class TestIndexHygieneMetrics:
    """索引卫生两件事：覆盖率只数能解析回活行的索引行；孤儿行必须被报出来。

    起因（2026-08-18 实测）：`vector_index` 683 行里 419 行的 `message_id` 在 `messages`
    里已不存在（那天向量层首次上线批量回填，源行后来没了），旧口径把它们算进分子，
    体检报"覆盖 15%"，按可解析算只有 ~6%。
    """

    @staticmethod
    def _add_vector(store, message_id: int, *, text_hash: str = "h", day: str = "2026-08-18") -> None:
        store.conn.execute(
            "INSERT INTO vector_index (message_id, text_hash, vector, model_name, dim, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, text_hash, b"\x00\x00\x00\x00", "test-model", 4, f"{day} 21:00:00"),
        )
        store.conn.commit()

    @staticmethod
    def _add_ghost_rows(db_path: str, message_ids: list[int], *, vector: bool = True,
                        bm25: bool = False, day: str = "2026-08-18") -> None:
        """造孤儿**只能**走没开 foreign_keys 的连接——生产连接会直接拒绝（这是好事）。

        这正是 08-18 那批孤儿的来源方式：`PRAGMA foreign_keys` 在 SQLite 里是**按连接**
        生效且默认关，所以任何裸 `sqlite3.connect()` 的批量脚本都删不出/写不出级联。
        """
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
            for mid in message_ids:
                if vector:
                    conn.execute(
                        "INSERT INTO vector_index (message_id, text_hash, vector, model_name,"
                        " dim, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (mid, f"ghost{mid}", b"\x00\x00\x00\x00", "test-model", 4,
                         f"{day} 21:00:00"),
                    )
                if bm25:
                    conn.execute(
                        "INSERT INTO bm25_inverted (term, message_id, tf) VALUES (?, ?, ?)",
                        (f"ghostterm{mid}", mid, 1),
                    )
            conn.commit()
        finally:
            conn.close()

    def test_production_connection_enforces_fk(self, store):
        """守卫本身：生产连接上插一条指向不存在消息的向量行必须被拒——孤儿不可能由它产生"""
        with pytest.raises(sqlite3.IntegrityError):
            self._add_vector(store, 999_999)

    def test_coverage_counts_only_resolvable_vectors(self, store):
        live = store.insert_message("活着的消息")
        store.insert_message("有正文但没建向量的消息")  # 分母里的第二条，防口径靠分母缩小作弊
        self._add_vector(store, live, text_hash="live")
        self._add_ghost_rows(store.db_path, [999_999])

        ratio, extra = mem_metrics.compute_metrics(
            store.conn, db_path=store.db_path)["vector_coverage"]
        assert extra["messages"] == 2
        assert extra["vectors_resolvable"] == 1
        assert extra["vectors_total"] == 2
        # 口径改了之后：分子只数可解析的，孤儿不再给覆盖率充数
        assert ratio == pytest.approx(1 / 2, abs=0.001)
        assert extra["coverage_if_counting_orphans"] == pytest.approx(2 / 2, abs=0.001)

    def test_orphan_rows_are_reported_with_breakdown(self, store):
        live = store.insert_message("活着的消息")
        self._add_vector(store, live, text_hash="live")
        self._add_ghost_rows(store.db_path, [999_999])
        self._add_ghost_rows(store.db_path, [999_998], day="2026-08-19")
        self._add_ghost_rows(store.db_path, [999_997], vector=False, bm25=True)

        total, extra = mem_metrics.compute_metrics(
            store.conn, db_path=store.db_path)["index_orphan_rows"]
        assert total == 3  # 2 行向量孤儿 + 1 行 BM25 孤儿
        assert extra["vector_orphan_rows"] == 2
        assert extra["bm25_orphan_rows"] == 1
        # 按产生日分组——用于判断"封口的一批"还是"还在长"
        assert sorted(d[0] for d in extra["orphan_vector_days"]) == ["2026-08-18", "2026-08-19"]

    def test_clean_index_reports_zero_not_missing(self, store):
        store.insert_message("只有一条消息")
        total, extra = mem_metrics.compute_metrics(
            store.conn, db_path=store.db_path)["index_orphan_rows"]
        assert total == 0
        assert extra["orphan_vector_days"] == []

    def test_delete_message_leaves_no_orphans(self, store):
        """这条钉的是"会不会继续长"：生产删除路径必须把两张索引一起清掉。

        级联靠 `ON DELETE CASCADE` + **连接上开着的** `PRAGMA foreign_keys`；
        两张表都被 `messages` 的级联覆盖，所以走 `delete_message` 不可能留下孤儿。
        """
        mid = store.insert_message("要删掉的消息")
        self._add_vector(store, mid, text_hash="v")
        store.conn.execute(
            "INSERT INTO bm25_inverted (term, message_id, tf) VALUES (?, ?, ?)", ("kw", mid, 2)
        )
        store.conn.commit()

        store.delete_message(mid)

        snap = mem_metrics.compute_metrics(store.conn, db_path=store.db_path)
        assert snap["index_orphan_rows"][0] == 0
        assert snap["vector_coverage"][1]["vectors_total"] == 0


# ==================== 4. 通配限槽 ====================

class TestSelectMustRead:
    def test_wildcard_gets_only_one_slot(self):
        ordered = [{"key": f"spec{i}"} for i in range(4)] + [
            {"key": f"wild{i}", "via_wildcard": True} for i in range(5)
        ]
        chosen, dropped, displaced = _select_must_read(ordered, _MUST_READ_MAX)
        keys = [c["key"] for c in chosen]
        assert keys[:4] == ["spec0", "spec1", "spec2", "spec3"]
        assert sum(1 for k in keys if k.startswith("wild")) == 1
        assert displaced == 4
        assert set(dropped) == {f"wild{i}" for i in range(1, 5)}

    def test_slots_backfilled_when_specific_run_out(self):
        """宁少勿空：具体命中项不够时用通配补位，不能因为限槽把必读块搞短"""
        ordered = [{"key": "spec0"}] + [
            {"key": f"wild{i}", "via_wildcard": True} for i in range(5)
        ]
        chosen, _dropped, _displaced = _select_must_read(ordered, _MUST_READ_MAX)
        assert len(chosen) == _MUST_READ_MAX

    def test_no_wildcard_behaves_like_plain_slice(self):
        ordered = [{"key": f"k{i}"} for i in range(8)]
        chosen, dropped, displaced = _select_must_read(ordered, _MUST_READ_MAX)
        assert [c["key"] for c in chosen] == ["k0", "k1", "k2", "k3", "k4"]
        assert displaced == 0
        assert len(dropped) == 3

    def test_quota_zero_excludes_wildcard_when_specific_enough(self):
        ordered = [{"key": f"spec{i}"} for i in range(5)] + [
            {"key": "wild0", "via_wildcard": True}
        ]
        chosen, dropped, _displaced = _select_must_read(ordered, 5, wildcard_quota=0)
        assert "wild0" not in [c["key"] for c in chosen]
        assert "wild0" in dropped


class TestWildcardFlagFromRecall:
    def _manager(self, store):
        cfg = MemoryConfig(db_path=store.db_path)
        with patch("server.memory.manager.EmbeddingEngine"), \
             patch("server.memory.manager.SemanticSearch"), \
             patch("server.memory.manager.CompressionPipeline"), \
             patch("server.memory.manager.MemoryMaintainer"), \
             patch("server.memory.manager.InteractionRecorder"), \
             patch("server.memory.manager.RecentMemory"):
            from server.memory.manager import MemoryManager

            mgr = MemoryManager(cfg)
            mgr._initialized = True
            return mgr

    def test_pure_wildcard_marked(self, store):
        mgr = self._manager(store)
        try:
            _seed_fact(store, "w", {"summary": "通配"}, contexts=["*"])
            hits = mgr.find_consumable_memories("recurring.accounting", "记账")
            assert [h["key"] for h in hits] == ["w"]
            assert hits[0]["via_wildcard"] is True
        finally:
            mgr.store.close()

    def test_specific_context_not_marked(self, store):
        mgr = self._manager(store)
        try:
            _seed_fact(store, "both", {"summary": "通配+具体"},
                       contexts=["*", "recurring.accounting"])
            hits = mgr.find_consumable_memories("recurring.accounting", "记账")
            assert hits[0]["via_wildcard"] is False
        finally:
            mgr.store.close()

    def test_keyword_hit_lifts_wildcard_mark(self, store):
        mgr = self._manager(store)
        try:
            _seed_fact(store, "kw", {"summary": "关键词命中"},
                       contexts=["*"], keywords=["退款"])
            hits = mgr.find_consumable_memories("recurring.accounting", "帮我处理退款")
            assert hits[0]["matched_by"] == "context+keyword"
            assert hits[0]["via_wildcard"] is False
        finally:
            mgr.store.close()
