"""DL1 上下文红线（norms bundle）测试。

对应设计 `workspace/local_butler/30_架构与设计/14_记忆上下文红线_DL1实施设计_20261027.md`。
守四条硬判据：预算 5,000、安全类优先、超预算不静默丢、norm 不自动老化。
"""

import pytest

from server.memory.config import MemoryConfig
from server.memory.maintainer import MemoryMaintainer
from server.memory.norms import NORMS_BUDGET_CHARS, build_norms_bundle
from server.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    st = MemoryStore(str(tmp_path / "mem.db"))
    st.initialize()
    yield st
    st.close()


def _add(
    store,
    key,
    value,
    *,
    title=None,
    scope="global",
    must_read=1,
    archived=False,
):
    store.upsert_fact(
        key,
        value,
        source="manual",
        fact_type="project",
        title=title or key,
        summary=value[:60],
        scope=scope,
        kind="norm",
        origin="system",
        must_read=must_read,
    )
    if archived:
        store.conn.execute(
            "UPDATE facts SET archived_at='2026-01-01 00:00:00' WHERE key = ?", (key,)
        )
        store.conn.commit()


def _keys(out):
    return [it["key"] for it in out["items"]]


def test_budget_default_is_5000():
    assert NORMS_BUDGET_CHARS == 5_000


def test_empty_bundle(store):
    out = build_norms_bundle(store)
    assert out == {
        "items": [],
        "chars": 0,
        "budget": 5_000,
        "truncated": False,
        "omitted": [],
        "count": 0,
        "project_id": None,
    }


def test_safety_first_then_global_then_project(store):
    _add(store, "norm_efficiency", "效率类规则", must_read=0)
    _add(store, "norm_safety_global", "安全类全局规则", must_read=1)
    _add(store, "norm_safety_project", "安全类项目规则", scope="project:parcae", must_read=1)

    out = build_norms_bundle(store, project_id="parcae")
    assert _keys(out) == [
        "norm_safety_global",
        "norm_safety_project",
        "norm_efficiency",
    ]


def test_without_project_only_global(store):
    _add(store, "norm_global", "全局规则")
    _add(store, "norm_project", "项目规则", scope="project:parcae")

    out = build_norms_bundle(store)
    assert _keys(out) == ["norm_global"]


def test_other_project_is_excluded(store):
    _add(store, "norm_a", "A 项目规则", scope="project:a")
    _add(store, "norm_b", "B 项目规则", scope="project:b")

    out = build_norms_bundle(store, project_id="a")
    assert _keys(out) == ["norm_a"]


def test_efficiency_over_budget_is_omitted_not_silent(store):
    _add(store, "norm_long_1", "甲" * 80, must_read=0)
    _add(store, "norm_long_2", "乙" * 80, must_read=0)

    out = build_norms_bundle(store, budget=100)
    assert out["truncated"] is True
    assert out["count"] == 1
    assert out["chars"] <= 100
    assert out["omitted"] == [{"key": "norm_long_2", "reason": "over_budget"}]


def test_safety_item_is_truncated_not_dropped(store):
    _add(store, "norm_safety_long", "丙" * 200, must_read=1)

    out = build_norms_bundle(store, budget=80)
    assert out["truncated"] is True
    assert out["count"] == 1
    item = out["items"][0]
    assert item["key"] == "norm_safety_long"
    assert item["truncated"] is True
    assert item["text"].endswith("…[截断]")
    assert item["chars"] <= 80


def test_archived_norm_is_excluded(store):
    _add(store, "norm_alive", "仍在用")
    _add(store, "norm_archived", "已失效", archived=True)

    out = build_norms_bundle(store)
    assert _keys(out) == ["norm_alive"]


def test_norm_is_not_aged_by_maintainer(store):
    _add(store, "norm_no_age", "红线不老化", must_read=1)
    store.conn.execute(
        "UPDATE facts SET updated_at='2000-01-01 00:00:00' WHERE key='norm_no_age'"
    )
    store.conn.commit()

    MemoryMaintainer(store, MemoryConfig()).run_aging()

    row = store.conn.execute(
        "SELECT validation_status FROM memory_meta WHERE key='norm_no_age'"
    ).fetchone()
    assert row is None


def test_http_endpoint_returns_bundle(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.memory import router as router_module

    _add(store, "norm_http", "端点可达")

    class _FakeManager:
        pass

    fake = _FakeManager()
    fake.store = store
    monkeypatch.setattr(router_module, "get_memory_manager", lambda: fake)

    app = FastAPI()
    app.include_router(router_module.router)
    resp = TestClient(app).get("/memory/norms?budget=5000")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["key"] == "norm_http"
