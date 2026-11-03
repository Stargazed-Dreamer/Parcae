"""_search_facts 分词召回测试（工具体检 B1 中档修复）

背景：2026-10-10 体检发现 `POST /memory/search` 拿到的结果**永远只有 message**，
fact 零命中。根因不是 EvidenceLedger 没开（默认值 True、facts 表有 161 条），
而是 `_search_facts` 用 `LIKE '%整串 query%'`：中文 query 一加空格，整串就不再
连续，于是全漏；同时 messages 走向量+BM25 总能凑满 top_k 把位置占掉 —— 静默。

这几个用例钉住三件事：
1. 加空格的自然语言 query 现在能命中 fact（回归锚，正是体检失败的那次查询）
2. 排序按命中词项数，全命中 > 半命中
3. 老行为不回退：整串精确子串仍然满分命中；通配符不会被当成 LIKE 元字符
"""

import os
import sqlite3
import tempfile
from collections.abc import Callable

import pytest

from server.memory.manager import _escape_like, _fact_query_terms, _MAX_FACT_TERMS


# ==================== Fixtures ====================

@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for suffix in ("", "-wal", "-shm"):
        f = path + suffix
        if os.path.exists(f):
            try:
                os.unlink(f)
            except OSError:
                pass


@pytest.fixture
def conn(tmp_db):
    """最小 facts 表（只含 _search_facts 真正 SELECT 的列）"""
    c = sqlite3.connect(tmp_db)
    c.execute(
        """CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            confidence REAL DEFAULT 1.0,
            fact_type TEXT,
            occurred_at TEXT,
            updated_at TEXT NOT NULL,
            archived_at TEXT               -- C1(v7)：_search_facts 现在按它过滤，最小表也必须带上
        )"""
    )
    yield c
    c.close()


class _FakeStore:
    """只提供 _search_facts 用到的 conn"""

    def __init__(self, conn):
        self.conn = conn


class _FakeMgr:
    """只提供 _search_facts 用到的 store；避开 MemoryManager 的整套初始化"""

    # 由下面的 fixture 绑定到 MemoryManager._search_facts 真函数（不复制实现）
    _search_facts: Callable[..., list[dict]]

    def __init__(self, conn):
        self.store = _FakeStore(conn)


@pytest.fixture
def mgr(conn):
    from server.memory.manager import MemoryManager

    m = _FakeMgr(conn)
    m._search_facts = MemoryManager._search_facts.__get__(m)
    return m


def _add(conn, key, value, updated_at="2026-10-10 00:00:00"):
    conn.execute(
        "INSERT INTO facts (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, updated_at),
    )
    conn.commit()


# ==================== 词项切分 ====================

class TestFactQueryTerms:
    def test_chinese_splits_and_drops_single_ascii(self):
        terms = _fact_query_terms("抽卡 游戏 状态")
        assert "抽卡" in terms and "状态" in terms
        # 纯空白不进词项
        assert all(t.strip() for t in terms)

    def test_empty_query_returns_nothing(self):
        assert _fact_query_terms("") == []
        assert _fact_query_terms("   ") == []

    def test_fallback_when_no_usable_token(self):
        """全单字符 ASCII（被 bm25 口径过滤掉）时回退成整串，不能静默变空"""
        assert _fact_query_terms("a b") == ["a b"]

    def test_dedup(self):
        terms = _fact_query_terms("工具 工具 工具")
        assert terms.count("工具") == 1

    def test_capped(self):
        long_q = " ".join(f"词{i}" for i in range(50))
        assert len(_fact_query_terms(long_q)) <= _MAX_FACT_TERMS

    def test_escape_like_neutralizes_wildcards(self):
        assert _escape_like("100%") == "100\\%"
        assert _escape_like("a_b") == "a\\_b"


# ==================== 召回行为 ====================

class TestSearchFacts:
    def test_spaced_natural_language_hits_fact(self, mgr, conn):
        """★ 回归锚：体检失败的那次查询。整串不连续但每个词都在 value 里。"""
        _add(conn, "gacha_games_status", "异环与鸣潮属于抽卡游戏，当前状态是永久保留")
        _add(conn, "unrelated", "今天天气不错")

        hits = mgr._search_facts("抽卡 游戏 状态", top_k=10)
        assert hits, "加空格的自然语言 query 必须能命中 fact（B1 原始症状）"
        assert hits[0]["key"] == "gacha_games_status"
        assert hits[0]["match_score"] == pytest.approx(1.0)

    def test_partial_match_ranks_below_full(self, mgr, conn):
        _add(conn, "full", "抽卡游戏状态")
        _add(conn, "partial", "只提到抽卡")

        hits = mgr._search_facts("抽卡 游戏 状态", top_k=10)
        keys = [h["key"] for h in hits]
        assert keys.index("full") < keys.index("partial")
        assert hits[0]["match_score"] > hits[-1]["match_score"]

    def test_exact_substring_still_full_score(self, mgr, conn):
        """老行为不回退：整串精确命中仍然满分"""
        _add(conn, "mcp_endpoint_removal_verdict", "裁决：移除该端点")
        hits = mgr._search_facts("mcp_endpoint_removal_verdict", top_k=10)
        assert hits and hits[0]["key"] == "mcp_endpoint_removal_verdict"
        assert hits[0]["match_score"] == pytest.approx(1.0)

    def test_wildcard_in_query_is_literal(self, mgr, conn):
        """query 里的 % 不能被当成 LIKE 元字符，否则退化成全表匹配"""
        _add(conn, "a", "覆盖率 100%达标")
        _add(conn, "b", "与本次查询毫无关系的另一条事实")
        hits = mgr._search_facts("100%", top_k=10)
        assert [h["key"] for h in hits] == ["a"]

    def test_no_hit_returns_empty(self, mgr, conn):
        _add(conn, "x", "完全不相关的记忆内容")
        assert mgr._search_facts("量子隧穿效应", top_k=10) == []

    def test_top_k_respected(self, mgr, conn):
        for i in range(8):
            _add(conn, f"k{i}", f"抽卡游戏状态 第{i}条")
        assert len(mgr._search_facts("抽卡 游戏 状态", top_k=3)) == 3

    def test_key_only_match_counts(self, mgr, conn):
        """命中 key 也算命中（agent 常直接搜 key 名）"""
        _add(conn, "disk_io_audit", "正文里没有任何一个查询词")
        hits = mgr._search_facts("disk_io_audit", top_k=10)
        assert hits and hits[0]["key"] == "disk_io_audit"


# ==================== 打分传导到 EvidenceLedger ====================

def test_fact_score_is_confidence_times_coverage():
    """fact 的 retrieval_score 必须随覆盖率衰减，否则任何 fact 命中都稳压 message"""
    import tempfile as _tf

    from server.memory.config import MemoryConfig
    from server.memory.evidence import EvidenceLedger
    from server.memory.store import MemoryStore

    fd, path = _tf.mkstemp(suffix=".db")
    os.close(fd)
    store = MemoryStore(path)
    store.initialize()
    cfg = MemoryConfig()
    cfg.evidence_top_k = 10
    ledger = EvidenceLedger(store=store, config=cfg)

    facts_full = [{"id": 1, "key": "k1", "value": "抽卡游戏状态", "confidence": 1.0,
                   "updated_at": "2026-10-10 00:00:00", "match_score": 1.0}]
    facts_half = [{"id": 2, "key": "k2", "value": "只提到抽卡", "confidence": 1.0,
                   "updated_at": "2026-10-10 00:00:00", "match_score": 0.33}]
    msgs = [{"message_id": 9, "score": 0.5, "content": "某条中等相关消息",
             "timestamp": 1000.0, "source": "manual"}]

    out = ledger.organize_evidence(
        query="抽卡 游戏 状态", message_results=msgs, fact_results=facts_full
    )
    full = next(e for e in out if e["source_type"] == "fact")
    assert full["retrieval_score"] == pytest.approx(1.0)
    assert full["retrieval_score"] > 0.5  # 全命中仍稳压同批 message

    out2 = ledger.organize_evidence(
        query="抽卡 游戏 状态", message_results=msgs, fact_results=facts_half
    )
    half = next(e for e in out2 if e["source_type"] == "fact")
    assert half["retrieval_score"] == pytest.approx(0.33)
    assert half["retrieval_score"] < 0.5  # 半命中要排在中等相关 message 之后

    store.close()
    for suffix in ("", "-wal", "-shm"):
        f = path + suffix
        if os.path.exists(f):
            try:
                os.unlink(f)
            except OSError:
                pass
