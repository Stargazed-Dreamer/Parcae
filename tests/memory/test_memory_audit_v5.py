"""v5 记忆库体检整改的回归测试

覆盖三块（对应 2026-10-03 体检发现的三个判据错配）：
- 指标日聚合 `server/memory/metrics.py`：`record_daily` 写出可查的漏斗/卫生指标
- 验证器喂料与延判 `maintainer.build_validation_preview` / `run_validate`：
  剥掉 value 内嵌的元数据信封、正文超窗时不采纳 archive
- 老化重验门控 `run_aging`：archive 不再是永久标签
- 无效判据拒绝 `is_invalid_archive_reason`：模型以"超出知识截止/未来未发生"为由的
  archive 一律不采纳（反复只给这种理由则落 keep）
- 通配限槽 `agent_guide._select_must_read`：`"*"` 记忆最多占 1 个必读位
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
from server.memory.maintainer import (
    _ARCHIVE_CONFIRM_ATTEMPTS,
    build_validation_preview,
    is_invalid_archive_reason,
)
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


# ==================== 1. schema v5 ====================

class TestSchemaV5:
    def test_metrics_table_exists(self, store):
        row = store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_metrics_daily'"
        ).fetchone()
        assert row is not None

    def test_validate_attempts_column_exists(self, store):
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(memory_meta)")}
        assert "validate_attempts" in cols

    def test_init_is_idempotent(self, tmp_db):
        from server.memory.schema import init_db

        conn = init_db(tmp_db)
        conn.close()
        conn2 = init_db(tmp_db)  # 第二次跑迁移不应报错
        conn2.close()


# ==================== 2. build_validation_preview ====================

class TestValidationPreview:
    def test_strips_metadata_envelope(self):
        """旧实现把元数据信封当正文喂给模型，于是出现"乱码/无实际语义"的 archive 判决"""
        value = json.dumps({
            "source": "manual", "updated_at": "2026-09-16 00:41:25",
            "access_count": 12, "confidence": 1.0, "created_at": "2026-09-16 00:41:25",
            "fact_type": "reference", "consumption_contexts": '["*"]',
            "trigger_keywords": '["curl"]',
            "summary": "后端停机时用 sqlite3 直接写 facts 表的方法",
        })
        preview, truncated = build_validation_preview(value, 2400)
        assert "后端停机时用 sqlite3" in preview
        for junk in ("access_count", "confidence", "updated_at"):
            assert junk not in preview
        assert truncated is False

    def test_keeps_business_keys(self):
        """非信封键不能静默丢掉——丢了就变成"看不全"，会被误判成"没价值" """
        value = json.dumps({"stock_review_output": {"no_html": True}, "rules": {"留": "材料"}})
        preview, _ = build_validation_preview(value, 2400)
        assert "stock_review_output" in preview
        assert "no_html" in preview

    def test_marks_truncated(self):
        value = json.dumps({"content": "x" * 5000})
        preview, truncated = build_validation_preview(value, 2400)
        assert truncated is True
        assert len(preview) == 2400

    def test_non_json_value_passthrough(self):
        preview, truncated = build_validation_preview("纯文本记忆", 2400)
        assert preview == "纯文本记忆"
        assert truncated is False

    def test_empty_value_does_not_vanish(self):
        """空 value 要能读成"空"，而不是"内容缺失所以判死" """
        preview, truncated = build_validation_preview("", 2400)
        assert preview == ""
        assert truncated is False


# ==================== 3. 验证器的超窗延判 ====================

class TestValidateTruncationPolicy:
    def _stub(self, maintainer, action):
        async def _fake():
            return ({"is_accurate": action != "keep", "reason": "内容被截断",
                     "suggested_action": action}, False)
        return _fake

    def test_archive_deferred_when_truncated(self, maintainer, store):
        import asyncio

        maintainer.config.validate_preview_chars = 50
        _seed_fact(store, "big", {"content": "y" * 400}, fact_type="project")
        store.conn.execute(
            "INSERT INTO memory_meta (key, validation_status) VALUES (?, 'pending')", ("big",)
        )
        store.conn.commit()

        with patch.object(maintainer, "_call_llm_validate_with_429_flag",
                          return_value=({"is_accurate": False, "reason": "内容被截断",
                                         "suggested_action": "archive"}, False)):
            stats = asyncio.run(maintainer.run_validate())

        assert stats["deferred_truncated"] == 1
        assert stats["archive"] == 0
        row = store.conn.execute(
            "SELECT validation_status, validate_attempts, validation_note "
            "FROM memory_meta WHERE key='big'"
        ).fetchone()
        assert row[0] == "pending"          # 没看全就不给死刑
        assert row[1] == 1                  # 中间量可核：延判计一次
        assert "超出" in row[2]

    def test_repeated_truncation_becomes_update_not_archive(self, maintainer, store):
        import asyncio

        maintainer.config.validate_preview_chars = 50
        _seed_fact(store, "big2", {"content": "y" * 400}, fact_type="project")
        store.conn.execute(
            "INSERT INTO memory_meta (key, validation_status, validate_attempts) "
            "VALUES (?, 'pending', ?)", ("big2", _ARCHIVE_CONFIRM_ATTEMPTS - 1)
        )
        store.conn.commit()

        with patch.object(maintainer, "_call_llm_validate_with_429_flag",
                          return_value=({"is_accurate": False, "reason": "内容被截断",
                                         "suggested_action": "archive"}, False)):
            stats = asyncio.run(maintainer.run_validate())

        assert stats["archive"] == 0
        assert stats["update"] == 1
        row = store.conn.execute(
            "SELECT validation_status, validation_note FROM memory_meta WHERE key='big2'"
        ).fetchone()
        assert row[0] == "update"           # 臃肿是重写信号，不是过时信号
        assert "压缩重写" in row[1]

    def test_archive_applied_when_content_fits(self, maintainer, store):
        import asyncio

        maintainer.config.validate_preview_chars = 2400
        _seed_fact(store, "small", {"summary": "一次性下载已完成"}, fact_type="project")
        store.conn.execute(
            "INSERT INTO memory_meta (key, validation_status) VALUES (?, 'pending')", ("small",)
        )
        store.conn.commit()

        with patch.object(maintainer, "_call_llm_validate_with_429_flag",
                          return_value=({"is_accurate": False, "reason": "一次性动作已完成",
                                         "suggested_action": "archive"}, False)):
            stats = asyncio.run(maintainer.run_validate())

        assert stats["archive"] == 1
        assert stats["deferred_truncated"] == 0

    def test_batch_limit_caps_one_cycle(self, maintainer, store):
        import asyncio

        maintainer.config.validate_batch_limit = 2
        for i in range(5):
            _seed_fact(store, f"f{i}", {"summary": "x"}, fact_type="project")
            store.conn.execute(
                "INSERT INTO memory_meta (key, validation_status, stale_score) "
                "VALUES (?, 'pending', ?)", (f"f{i}", 10.0 + i)
            )
        store.conn.commit()

        with patch.object(maintainer, "_call_llm_validate_with_429_flag",
                          return_value=({"is_accurate": True, "reason": "仍成立",
                                         "suggested_action": "keep"}, False)):
            stats = asyncio.run(maintainer.run_validate())

        assert stats["pending_selected"] == 2
        assert stats["validated"] == 2


# ==================== 4. 老化的重验门控 ====================

class TestAgingRevalidationGate:
    def _set_state(self, store, key, status, validated_days_ago):
        stamp = (datetime.now() - timedelta(days=validated_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        store.conn.execute(
            "UPDATE memory_meta SET validation_status=?, last_validated_at=? WHERE key=?",
            (status, stamp, key),
        )
        store.conn.commit()

    def _make_stale(self, store, key):
        _seed_fact(store, key, {"summary": "x"}, fact_type="project")
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        store.conn.execute("UPDATE facts SET updated_at=? WHERE key=?", (old, key))
        store.conn.commit()
        store.conn.execute(
            "INSERT OR REPLACE INTO memory_meta (key, validation_status) VALUES (?, 'pending')"
        , (key,))
        store.conn.commit()

    def test_archive_label_is_revisitable(self, maintainer, store):
        """archive 此前是永久标签：修好验证器之后必须能把存量误判重判一遍"""
        self._make_stale(store, "dead_soon")
        self._set_state(store, "dead_soon", "archive", validated_days_ago=40)

        maintainer.config.validate_revalidate_days = 30
        result = maintainer.run_aging()

        status = store.conn.execute(
            "SELECT validation_status FROM memory_meta WHERE key='dead_soon'"
        ).fetchone()[0]
        assert status == "pending"
        assert result["requeued_for_revalidation"] == 1

    def test_recently_validated_is_not_requeued(self, maintainer, store):
        self._make_stale(store, "fresh_check")
        self._set_state(store, "fresh_check", "keep", validated_days_ago=3)

        maintainer.config.validate_revalidate_days = 30
        maintainer.run_aging()

        status = store.conn.execute(
            "SELECT validation_status FROM memory_meta WHERE key='fresh_check'"
        ).fetchone()[0]
        assert status == "keep"   # 不再每 6 小时重验一次

    def test_stale_score_updates_even_when_not_requeued(self, maintainer, store):
        self._make_stale(store, "scored")
        self._set_state(store, "scored", "archive", validated_days_ago=1)

        maintainer.run_aging()

        score = store.conn.execute(
            "SELECT stale_score FROM memory_meta WHERE key='scored'"
        ).fetchone()[0]
        assert score >= 29


# ==================== 5. 指标日聚合 ====================

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
            "INSERT INTO memory_meta (key, validation_status) VALUES ('zombie', 'archive')"
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


class TestRequeueAllValidated:
    def test_resets_labels_and_clears_revalidation_gate(self, maintainer, store):
        _seed_fact(store, "a", {"summary": "x"}, fact_type="project")
        _seed_fact(store, "b", {"summary": "y"}, fact_type="reference")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        store.conn.execute(
            "INSERT INTO memory_meta (key, validation_status, last_validated_at, validate_attempts)"
            " VALUES ('a', 'archive', ?, 5)", (now,),
        )
        store.conn.execute(
            "INSERT INTO memory_meta (key, validation_status) VALUES ('b', 'keep')"
        )
        store.conn.commit()

        result = maintainer.requeue_all_validated()

        assert result == {"requeued": 2}
        rows = dict(store.conn.execute(
            "SELECT key, validation_status FROM memory_meta"
        ).fetchall())
        assert rows == {"a": "pending", "b": "pending"}
        gate = store.conn.execute(
            "SELECT validate_attempts, last_validated_at FROM memory_meta WHERE key='a'"
        ).fetchone()
        assert gate[0] == 0        # 延判计数从零开始，否则一跑就撞第 3 次直接判 update
        assert gate[1] is None     # 重验门控清空


class TestInvalidReasonRejection:
    """cheap 模型把"晚于自己知识截止的日期"读成"未来未发生 ⇒ 内容无效"。

    2026-10-03 重判存量时实测到两例（`project_ruanxie_recruit_2026`、
    `reference_opencode_zen_free_tier`），杀伤面专一：优先杀最新写入的记忆。
    """

    def _seed_pending(self, store, key, *, attempts=0, value=None):
        _seed_fact(store, key, value or {"summary": "2026-09 招新通知排期与判据"},
                   fact_type="project")
        store.conn.execute(
            "INSERT INTO memory_meta (key, validation_status, validate_attempts) "
            "VALUES (?, 'pending', ?)", (key, attempts)
        )
        store.conn.commit()

    def test_regex_hits_the_two_observed_verdicts(self):
        assert is_invalid_archive_reason(
            "时间戳显示为2026年9月，已超出模型知识库截止日期（2026年7月），属于未来未发生的事件"
        )
        assert is_invalid_archive_reason("记录时间2026-09-13超出知识截止期2026-07，已无持续参考价值")

    def test_regex_does_not_hit_legitimate_staleness_reasons(self):
        # 反向用例：正常的失效判据不能被误拦，否则 archive 通道形同废除
        for reason in (
            "一次性下载任务已完成，无可复用信息",
            "该端点已在 v0.52 移除，被新接口取代",
            "内容为空壳，读不出任何命题",
            "正文存在两套重复且冲突的数据，需清理统一时间线",
        ):
            assert not is_invalid_archive_reason(reason), reason

    def test_archive_deferred_when_reason_is_invalid(self, maintainer, store):
        import asyncio

        self._seed_pending(store, "recruit_x")
        with patch.object(maintainer, "_call_llm_validate_with_429_flag",
                          return_value=({"is_accurate": False,
                                         "reason": "超出模型知识截止日期，属于未来未发生的事件",
                                         "suggested_action": "archive"}, False)):
            stats = asyncio.run(maintainer.run_validate())

        assert stats["deferred_invalid_reason"] == 1
        assert stats["archive"] == 0
        row = store.conn.execute(
            "SELECT validation_status, validate_attempts, validation_note "
            "FROM memory_meta WHERE key='recruit_x'"
        ).fetchone()
        assert row[0] == "pending"          # 依据不成立 ⇒ 不动手
        assert row[1] == 1
        assert "无效依据" in row[2]

    def test_repeated_invalid_reason_lands_keep(self, maintainer, store):
        import asyncio

        self._seed_pending(store, "recruit_y", attempts=_ARCHIVE_CONFIRM_ATTEMPTS - 1)
        with patch.object(maintainer, "_call_llm_validate_with_429_flag",
                          return_value=({"is_accurate": False,
                                         "reason": "尚未发生，内容无效",
                                         "suggested_action": "archive"}, False)):
            stats = asyncio.run(maintainer.run_validate())

        # 反复只给无效依据 = 没有成立的失效证据 ⇒ when in doubt, keep（而不是 update）
        assert stats["keep"] == 1
        assert stats["archive"] == 0
        row = store.conn.execute(
            "SELECT validation_status, validation_note FROM memory_meta WHERE key='recruit_y'"
        ).fetchone()
        assert row[0] == "keep"
        assert "无有效失效证据" in row[1]

    def test_prompt_grounds_today_and_bans_cutoff_reasoning(self, maintainer):
        sys_p, user_p = maintainer._build_validate_prompt(
            "k", json.dumps({"summary": "x"}, ensure_ascii=False),
            fact_type="project", fact_updated_at="2026-09-30 10:00:00", stale_days=3,
        )
        assert "今天:" in user_p                      # 时间线由调用方给，不由模型的记忆力决定
        assert "你的知识截止日期与本库无关" in sys_p
        assert "不构成内容无效的证据" in sys_p


# ==================== 6. 通配限槽 ====================

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
