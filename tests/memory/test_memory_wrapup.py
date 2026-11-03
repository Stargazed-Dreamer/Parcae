"""DL3 记忆写入漏斗测试：候选 → 去重 → 落库 → 下轮可读。

对应设计 `workspace/local_butler/30_架构与设计/12_记忆写入漏斗_DL3实施设计_20261027.md`。
四条硬判据各有一个测试守着：幂等、不冒充原话、拒收可见、唯一漏斗。
"""

import pytest

from server.memory.store import MemoryStore
from server.memory.wrapup import (
    MAX_CANDIDATES,
    compute_dedup_hash,
    list_pending_candidates,
    submit_wrapup,
)


@pytest.fixture
def store(tmp_path):
    st = MemoryStore(str(tmp_path / "mem.db"))
    st.initialize()
    yield st
    st.close()


def _facts_count(store) -> int:
    return store.conn.execute("select count(*) from facts").fetchone()[0]


def _quotes_count(store) -> int:
    return store.conn.execute("select count(*) from source_quotes").fetchone()[0]


def _candidates(store, status=None) -> list[dict]:
    sql = "select * from memory_candidates"
    args: tuple = ()
    if status:
        sql += " where status = ?"
        args = (status,)
    cur = store.conn.execute(sql + " order by id", args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


# ─── 1. schema ────────────────────────────────────────────────

def test_schema_v8_columns_and_tables(store):
    """facts 有 11 个新列，两张新表存在。"""
    cols = [r[1] for r in store.conn.execute("PRAGMA table_info(facts)")]
    for name in (
        "title", "summary", "scope", "kind", "origin", "sensitivity",
        "evidence_refs", "source_quote_ids", "must_read", "expires_at", "dedup_hash",
    ):
        assert name in cols, name
    tables = {r[0] for r in store.conn.execute(
        "select name from sqlite_master where type='table'")}
    assert {"source_quotes", "memory_candidates"} <= tables


# ─── 2. 基本落库 ──────────────────────────────────────────────

def test_basic_accept_marks_agent_extracted(store):
    """无 quote 的候选：落库成功，但 origin 必须是 agent_extracted。"""
    out = submit_wrapup(store, {
        "run_id": "r-1",
        "candidates": [{
            "content": "本机 PowerShell 是 5.1",
            "title": "PowerShell 版本",
            "summary": "5.1，不是 7",
            "kind": "fact",
            "scope": "global",
            "sensitivity": "private-eng",
        }],
    })
    assert out["candidates"]["accepted"] == 1
    row = store.conn.execute("select * from facts where kind='fact'").fetchone()
    cols = [d[0] for d in store.conn.execute("select * from facts limit 0").description]
    rec = dict(zip(cols, row, strict=False))
    assert rec["origin"] == "agent_extracted"
    assert rec["scope"] == "global"
    assert rec["sensitivity"] == "private-eng"
    assert rec["summary"] == "5.1，不是 7"
    assert rec["source_quote_ids"] is None


def test_empty_payload_is_noop(store):
    out = submit_wrapup(store, {"run_id": "r-0"})
    assert out["candidates"]["total"] == 0
    assert _facts_count(store) == 0


# ─── 3. 幂等 ─────────────────────────────────────────────────

def test_idempotent_resubmit(store):
    """同一份 payload 连交两次：第二次全 duplicate，行数不变。"""
    payload = {
        "run_id": "r-2",
        "quotes": [{"text": "删之前一定要 dry run"}],
        "candidates": [
            {"content": "删除前必须 dry run", "kind": "norm", "scope": "global",
             "must_read": 1, "source_quote_ids": None},
            {"content": "回收站不释放同盘空间", "kind": "fact"},
        ],
    }
    first = submit_wrapup(store, payload)
    n_facts, n_quotes = _facts_count(store), _quotes_count(store)
    assert first["candidates"]["accepted"] == 2

    second = submit_wrapup(store, payload)
    assert second["candidates"]["duplicate"] == 2
    assert second["candidates"]["accepted"] == 0
    assert _facts_count(store) == n_facts
    assert _quotes_count(store) == n_quotes


def test_dedup_hash_stable_regardless_of_whitespace(store):
    """归一化只折叠空白/大小写：同内容不同排版算同一条。"""
    a = compute_dedup_hash("global", "fact", "T", "  Hello   World ")
    b = compute_dedup_hash("global", "fact", "t", "hello world")
    assert a == b
    # 但 kind 或 scope 不同必须算出不同 hash
    assert a != compute_dedup_hash("project:x", "fact", "T", "Hello World")


# ─── 4. 原话 ─────────────────────────────────────────────────

def test_inline_quote_marks_user_quote(store):
    """带内联原话的候选 → origin=user_quote，并真的落了一条 source_quotes。"""
    out = submit_wrapup(store, {
        "run_id": "r-3",
        "candidates": [{
            "content": "报告要精简",
            "kind": "preference",
            "quote_text": "报告太长了，精简一点",
        }],
    })
    assert out["candidates"]["accepted"] == 1
    assert _quotes_count(store) == 1
    assert out["results"][0]["origin"] == "user_quote"
    assert out["results"][0]["source_quote_ids"]


def test_claimed_user_quote_without_evidence_is_downgraded(store):
    """声明 user_quote 却没任何 quote 支撑 → 降级 agent_extracted 并标记。"""
    out = submit_wrapup(store, {
        "run_id": "r-4",
        "candidates": [{
            "content": "用户说他喜欢深色主题",
            "kind": "preference",
            "origin": "user_quote",
        }],
    })
    assert out["candidates"]["accepted"] == 1
    assert out["results"][0]["origin"] == "agent_extracted"
    assert out["results"][0].get("downgraded_from_user_quote") is True


def test_run_level_quotes_can_support_candidate(store):
    """run 级 quotes 也算支撑（agent 在 note 里已经记过原话）。"""
    out = submit_wrapup(store, {
        "run_id": "r-5",
        "quotes": [{"text": "不要在桌面合成按键"}],
        "candidates": [{"content": "禁止在真实桌面合成按键", "kind": "norm", "must_read": 1}],
    })
    assert out["results"][0]["origin"] == "user_quote"


# ─── 5. 拒收 ─────────────────────────────────────────────────

def test_reject_codes(store):
    """六种坏输入各自被拒，且原因码正确。"""
    out = submit_wrapup(store, {
        "run_id": "r-6",
        "candidates": [
            {"content": "   "},                                        # empty_content
            {"content": "x", "kind": "nonsense"},                       # unknown_kind
            {"content": "x", "kind": "fact", "must_read": 1},            # must_read_not_norm
            {"content": "x", "kind": "current_state"},                   # missing_expires_at
            {"content": "x", "scope": "planet:mars"},                    # bad_scope
            {"content": "x", "sensitivity": "top-secret"},               # sensitivity_invalid
        ],
    })
    assert out["candidates"]["rejected"] == 6
    assert out["candidates"]["accepted"] == 0
    reasons = [r["reason"] for r in out["results"]]
    assert reasons == [
        "empty_content", "unknown_kind", "must_read_not_norm",
        "missing_expires_at", "bad_scope", "sensitivity_invalid",
    ]
    assert _facts_count(store) == 0


def test_rejected_candidates_are_visible(store):
    """被拒的候选留在 memory_candidates，payload 可回放（DL5 待审候选）。"""
    submit_wrapup(store, {
        "run_id": "r-7",
        "candidates": [{"content": "x", "kind": "nonsense"}],
    })
    rej = _candidates(store, "rejected")
    assert len(rej) == 1
    assert rej[0]["reason"] == "unknown_kind"
    assert rej[0]["run_id"] == "r-7"
    assert "nonsense" in rej[0]["payload"]

    pending = list_pending_candidates(store)
    assert len(pending) == 1
    assert isinstance(pending[0]["payload"], dict)


def test_rejected_not_duplicated_on_retry(store):
    """重试同一份坏 payload：待审列表仍只有 1 条（不把 DL5 刷成重复项）。"""
    bad = {"run_id": "r-11", "candidates": [{"content": "x", "kind": "nonsense"}]}
    submit_wrapup(store, bad)
    again = submit_wrapup(store, bad)
    assert again["candidates"]["rejected"] == 1
    assert again["results"][0].get("already_pending") is True
    assert len(list_pending_candidates(store)) == 1


def test_over_limit_keeps_first_ten(store):
    """超限的第 11 条起被拒，但前 10 条照常落库。"""
    cands = [{"content": "事实 %02d" % i, "kind": "fact"} for i in range(MAX_CANDIDATES + 2)]
    out = submit_wrapup(store, {"run_id": "r-8", "candidates": cands})
    assert out["candidates"]["accepted"] == MAX_CANDIDATES
    assert out["candidates"]["rejected"] == 2
    assert all(r["reason"] == "over_limit" for r in out["results"][MAX_CANDIDATES:])


# ─── 6. 合并与历史 ────────────────────────────────────────────

def test_same_key_merges_and_keeps_history(store):
    """同 key 不同内容 → merged，旧值进 fact_revisions（唯一漏斗自动继承）。"""
    submit_wrapup(store, {"run_id": "r-9", "candidates": [
        {"content": "版本 1", "key": "k:demo", "kind": "fact"},
    ]})
    out = submit_wrapup(store, {"run_id": "r-9", "candidates": [
        {"content": "版本 2", "key": "k:demo", "kind": "fact"},
    ]})
    assert out["candidates"]["merged"] == 1
    assert _facts_count(store) == 1
    row = store.conn.execute("select value from facts where key='k:demo'").fetchone()
    assert row[0] == "版本 2"
    rev = store.list_fact_revisions("k:demo")
    assert rev and rev[0]["value"] == "版本 1"


# ─── 7. 下轮可读 ──────────────────────────────────────────────

def test_must_read_norm_reaches_context(store):
    """norm + must_read → list_facts_for_context 取得到；普通 fact 取不到。"""
    submit_wrapup(store, {"run_id": "r-10", "candidates": [
        {"content": "删除必须走回收站", "kind": "norm", "scope": "global", "must_read": 1},
        {"content": "本机是 Windows", "kind": "fact", "scope": "global"},
    ]})
    hits = store.list_facts_for_context(scope="global", must_read_only=True)
    assert len(hits) == 1
    assert "回收站" in hits[0]["value"]

    all_global = store.list_facts_for_context(scope="global")
    assert len(all_global) == 2
    # must_read 排在最前
    assert "回收站" in all_global[0]["value"]


def test_unscoped_old_data_not_in_context(store):
    """老数据 scope 为 NULL：按 scope 查不到（不假装知道归属）。"""
    store.upsert_fact("legacy:1", "老记忆", source="manual")
    assert store.list_facts_for_context(scope="global") == []
    assert len(store.list_facts_for_context()) == 1
