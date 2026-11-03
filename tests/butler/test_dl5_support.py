"""DL5 面板支撑：run 列表、候选按 run 过滤、候选采纳/退回。"""

from __future__ import annotations

import pytest

from server.butler.store import ButlerStore
from server.memory.store import MemoryStore
from server.memory.wrapup import list_pending_candidates
from server.memory.write import MemoryWriteError, review_candidate


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "mem.db")
    mem = MemoryStore(db)
    mem.initialize()
    butler = ButlerStore(mem)
    yield mem, butler
    mem.close()


def _run(butler, prompt="DL5", **kw):
    return butler.create_run(prompt, project_id="parcae", **kw)


def test_list_runs_filters_and_hides_hash(env):
    mem, butler = env
    r1 = _run(butler, "一", task_scope="wip", task_id="w1")
    r2 = _run(butler, "二", task_scope="todos", task_id="t1")
    butler.open_run(r1["session_code"], owner="h1")

    all_rows = butler.list_runs()
    assert {r["run_id"] for r in all_rows} == {r1["run_id"], r2["run_id"]}
    assert all("code_hash" not in r for r in all_rows)

    active = butler.list_runs(statuses=["opened"])
    assert [r["run_id"] for r in active] == [r1["run_id"]]

    by_task = butler.list_runs(task_scope="todos", task_id="t1")
    assert [r["run_id"] for r in by_task] == [r2["run_id"]]

    assert butler.list_runs(limit=1) and len(butler.list_runs(limit=1)) == 1


def test_candidate_accept_writes_and_leaves_list(env):
    mem, _ = env
    cid = mem.record_memory_candidate(
        "rejected",
        {"content": "人工改过的候选", "kind": "bad_kind", "scope": "global", "title": "候选"},
        run_id="run_x",
        reason="unknown_kind",
    )
    assert len(list_pending_candidates(mem, run_id="run_x")) == 1

    out = review_candidate(mem, cid, "accept", overrides={"kind": "fact"})
    assert out["status"] == "accepted"
    assert out["memory"]["status"] == "accepted"
    assert mem.find_fact_by_dedup(out["memory"]["dedup_hash"]) is not None
    assert list_pending_candidates(mem, run_id="run_x") == []

    row = mem.get_memory_candidate(cid)
    assert row["status"] == "accepted"
    assert row["fact_key"] == out["memory"]["key"]
    assert row["reason"] == "unknown_kind"


def test_candidate_reject_marks_dismissed(env):
    mem, _ = env
    cid = mem.record_memory_candidate(
        "rejected", {"content": "不要这条", "kind": "fact", "scope": "global"}, run_id="run_y"
    )
    out = review_candidate(mem, cid, "reject", reason="不准确")
    assert out["status"] == "dismissed"
    row = mem.get_memory_candidate(cid)
    assert row["status"] == "dismissed"
    assert row["reason"] == "不准确"
    assert list_pending_candidates(mem) == []


def test_review_non_pending_is_409(env):
    mem, _ = env
    cid = mem.record_memory_candidate(
        "rejected", {"content": "只处理一次", "kind": "fact", "scope": "global"}
    )
    review_candidate(mem, cid, "reject")
    with pytest.raises(MemoryWriteError) as exc:
        review_candidate(mem, cid, "reject")
    assert exc.value.code == "candidate_not_pending"
    assert exc.value.http_status == 409


def test_accept_duplicate_marks_duplicate(env):
    mem, _ = env
    payload = {"content": "同一条事实", "kind": "fact", "scope": "global", "title": "去重"}
    first = mem.record_memory_candidate("rejected", payload)
    assert review_candidate(mem, first, "accept")["status"] == "accepted"

    second = mem.record_memory_candidate("rejected", payload)
    out = review_candidate(mem, second, "accept")
    assert out["status"] == "duplicate"
    assert mem.get_memory_candidate(second)["status"] == "duplicate"


def test_accept_matches_legacy_fact_without_dedup_hash(env):
    mem, _ = env
    mem.upsert_fact(
        "legacy_seed", "老规范正文", title="老规范", scope="global", kind="norm",
        origin="system", must_read=1,
    )
    cid = mem.record_memory_candidate(
        "rejected",
        {"content": "老规范正文", "kind": "bad_kind", "scope": "global", "title": "老规范"},
    )
    out = review_candidate(mem, cid, "accept", overrides={"kind": "norm"})
    assert out["status"] == "duplicate"
    assert out["memory"]["key"] == "legacy_seed"


def test_http_support_endpoints(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.butler.router as butler_router
    import server.memory.router as memory_router

    mem, butler = env
    run = _run(butler, "HTTP 面板", task_scope="wip", task_id="w1")
    cid = mem.record_memory_candidate(
        "rejected", {"content": "HTTP 候选", "kind": "fact", "scope": "global"}, run_id=run["run_id"]
    )

    class _Mgr:
        store = mem

    monkeypatch.setattr(butler_router, "get_butler_store", lambda: butler)
    monkeypatch.setattr(memory_router, "get_memory_manager", lambda: _Mgr())

    app = FastAPI()
    app.include_router(butler_router.router)
    app.include_router(memory_router.router)
    client = TestClient(app)

    resp = client.get("/butler/runs", params={"task_scope": "wip", "task_id": "w1"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    resp = client.get("/memory/candidates", params={"run_id": run["run_id"]})
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    resp = client.post(f"/memory/candidates/{cid}/review", json={"action": "accept"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    resp = client.post(f"/memory/candidates/{cid}/review", json={"action": "reject"})
    assert resp.status_code == 409
