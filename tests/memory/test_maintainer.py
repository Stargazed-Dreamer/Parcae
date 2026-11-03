"""测试记忆维护器（MemoryMaintainer）

覆盖：should_run 时间判断、run_aging 只写 stale_score、get_status 的删除标记。

2026-10-31 随 LLM 验证器删除：`_parse_validate_response` 那组测试整块移除。

"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from server.memory.config import MemoryConfig
from server.memory.maintainer import MemoryMaintainer
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
    cfg = MemoryConfig(db_path=store.db_path)
    return MemoryMaintainer(store, cfg)


class TestShouldRun:
    def test_should_run_when_never_run(self, maintainer):
        assert maintainer.should_run() is True

    def test_should_not_run_when_recently_run(self, maintainer, store):
        store.conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("maintainer_last_run", datetime.now().isoformat()),
        )
        store.conn.commit()
        assert maintainer.should_run() is False

    def test_should_run_when_overdue(self, maintainer, store):
        old_time = (datetime.now() - timedelta(hours=7)).isoformat()
        store.conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("maintainer_last_run", old_time),
        )
        store.conn.commit()
        assert maintainer.should_run() is True


class TestRunAging:
    def test_aging_marks_stale_facts(self, maintainer, store):
        old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        store.upsert_fact("old_fact", '{"content": "old"}', source="test")
        store.conn.execute(
            "UPDATE facts SET updated_at = ? WHERE key = ?", (old_date, "old_fact")
        )
        store.conn.commit()

        result = maintainer.run_aging()
        assert result["stale_count"] >= 1

        meta = store.conn.execute(
            "SELECT stale_score FROM memory_meta WHERE key = 'old_fact'"
        ).fetchone()
        assert meta is not None
        assert meta[0] >= 9          # 只算分，不再写 validation_status

    def test_aging_skips_recent_facts(self, maintainer, store):
        store.upsert_fact("fresh_fact", '{"content": "fresh"}', source="test")
        result = maintainer.run_aging()
        assert result["stale_count"] == 0


class TestGetStatus:
    def test_status_returns_dict(self, maintainer):
        status = maintainer.get_status()
        assert "last_run" in status
        assert "stale_threshold_days" in status
        assert "interval_hours" in status
        assert "stale_total" in status
        assert "archived_total" in status
        assert status["validator"] == "removed_2026-10-31"
        # 验证器已删：不能再出现「看起来还能拨」的开关字段
        assert "validate_enabled" not in status
        assert "model_tier" not in status
