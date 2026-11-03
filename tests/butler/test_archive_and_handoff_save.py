"""v15：面板归档 / 交接提示词手改保存 / 存为新版 —— 三个写接口。

2026-10-30 用户「我重启了，你做」，对应 `30_架构与设计/26_…md` 台账 D8 / D11 / D12。
三件共同点：都是**面板要真能用**缺的最后一块，全都只加接口与列，不动既有语义。

判据写在这里，改代码时别改判据：
- 存为新版 = 追加版本号，不是改现有版（改某版是 `PUT /prompts/{v}`）；
- 交接手改只在「还没交接」这一态生效，开出交接单后正文来自交接单；
- 归档不动 `status`，原状态另存，恢复时照抄回去（否则 done 变 active）。
"""

from __future__ import annotations

import pytest

from server.butler.store import ButlerStore
from server.memory.store import MemoryStore
from server.todos.store import TodosStore


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "mem.db")
    mem = MemoryStore(db)
    mem.initialize()
    todos = TodosStore(db)
    todos.initialize()
    butler = ButlerStore(mem)
    yield mem, todos, butler
    mem.close()
    try:
        todos.conn.close()
    except Exception:
        pass


def _client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.butler.router as router_module

    mem, todos, butler = env
    monkeypatch.setattr(router_module, "get_butler_store", lambda: butler)
    monkeypatch.setattr(router_module, "get_todos_store", lambda: todos)
    monkeypatch.setattr(router_module, "_memory", lambda: mem)
    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app), mem, todos, butler


# ---- D8：存为新版 --------------------------------------------------------


def test_append_prompt_version_via_api(env, monkeypatch):
    client, _, _, butler = _client(env, monkeypatch)
    rid = butler.create_run("原始要求")["run_id"]
    butler.append_prompt_version(rid, "第一版", source="initial")

    r = client.post(f"/butler/runs/{rid}/prompts",
                    json={"prompt_text": "第二版", "note": "试试另一种写法"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == 2 and body["is_current"] is True
    assert body["source"] == "user_edit"
    # 追加即生效：run 正文同步到新版
    assert butler.get_run(rid)["prompt_text"] == "第二版"
    versions = butler.list_prompt_versions(rid)["items"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[1]["note"] == "试试另一种写法"


def test_append_prompt_version_activate_false_keeps_current(env, monkeypatch):
    """存为新版但先不生效：版本加进去了，当前生效版不动。"""
    client, _, _, butler = _client(env, monkeypatch)
    rid = butler.create_run("原始要求")["run_id"]
    butler.append_prompt_version(rid, "第一版", source="initial")

    r = client.post(f"/butler/runs/{rid}/prompts",
                    json={"prompt_text": "备选版", "activate": False})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2 and r.json()["is_current"] is False
    assert butler.get_run(rid)["prompt_text"] == "第一版"


def test_append_prompt_version_rejects_blank(env, monkeypatch):
    client, _, _, butler = _client(env, monkeypatch)
    rid = butler.create_run("原始要求")["run_id"]
    r = client.post(f"/butler/runs/{rid}/prompts", json={"prompt_text": "   "})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "prompt_empty"


# ---- D11：交接提示词手改保存 ---------------------------------------------


def test_handoff_override_saved_and_used(env, monkeypatch):
    client, _, _, butler = _client(env, monkeypatch)
    rid = butler.create_run("原始要求")["run_id"]

    rendered = client.get(f"/butler/runs/{rid}/handoff-prompt").json()
    assert rendered["state"] == "handoff"
    assert rendered["edited"] is False
    assert rendered["prompt_text"].startswith("【交接】")

    saved = client.put(f"/butler/runs/{rid}/handoff-prompt",
                       json={"prompt_text": "我改过的交接稿"})
    assert saved.status_code == 200, saved.text
    view = saved.json()
    assert view["state"] == "handoff" and view["edited"] is True
    assert view["prompt_text"] == "我改过的交接稿"
    assert view["chars"] == len("我改过的交接稿")
    # 保存后 GET 读到的是手改版，不再是渲染版
    assert client.get(f"/butler/runs/{rid}/handoff-prompt").json()["prompt_text"] == "我改过的交接稿"


def test_handoff_override_cleared_by_empty_string(env, monkeypatch):
    client, _, _, butler = _client(env, monkeypatch)
    rid = butler.create_run("原始要求")["run_id"]
    client.put(f"/butler/runs/{rid}/handoff-prompt", json={"prompt_text": "手改稿"})

    cleared = client.put(f"/butler/runs/{rid}/handoff-prompt", json={"prompt_text": ""}).json()
    assert cleared["edited"] is False
    assert cleared["prompt_text"].startswith("【交接】")
    assert butler.get_run(rid)["handoff_text_override"] is None


def test_handoff_override_not_used_after_handoff_created(env, monkeypatch):
    """开出交接单后走 takeover 分支，正文来自交接单，手改稿不该再冒出来。"""
    client, _, _, butler = _client(env, monkeypatch)
    rid = butler.create_run("原始要求")["run_id"]
    client.put(f"/butler/runs/{rid}/handoff-prompt", json={"prompt_text": "手改稿"})
    butler.create_handoff({"blocker": "缺权限"}, title="交接单", run_id=rid)

    view = client.get(f"/butler/runs/{rid}/handoff-prompt").json()
    assert view["state"] == "takeover"
    assert "手改稿" not in view["prompt_text"]


# ---- D12：归档 / 恢复 ----------------------------------------------------


def test_run_archive_filters_list_and_is_idempotent(env, monkeypatch):
    client, _, _, butler = _client(env, monkeypatch)
    a = butler.create_run("A")["run_id"]
    butler.create_run("B")

    assert len(client.get("/butler/runs").json()["items"]) == 2
    first = client.patch(f"/butler/runs/{a}/archive", json={"reason": "先收起来"})
    assert first.status_code == 200, first.text
    assert first.json()["archived_at"] and first.json()["already"] is False

    assert len(client.get("/butler/runs?archived=false").json()["items"]) == 1
    assert len(client.get("/butler/runs?archived=true").json()["items"]) == 1
    # 不传 archived = 全给（老调用方与「找 takeover_run」靠这个）
    assert len(client.get("/butler/runs").json()["items"]) == 2

    again = client.patch(f"/butler/runs/{a}/archive", json={"reason": "再来一次"}).json()
    assert again["already"] is True
    assert again["archived_at"] == first.json()["archived_at"]

    assert client.post(f"/butler/runs/{a}/restore").json()["archived_at"] is None
    assert len(client.get("/butler/runs?archived=false").json()["items"]) == 2
    assert len(client.get("/butler/runs?archived=true").json()["items"]) == 0


def test_run_archive_without_body(env, monkeypatch):
    """reason 是可选的：不传 body 也要能归档。"""
    client, _, _, butler = _client(env, monkeypatch)
    rid = butler.create_run("A")["run_id"]
    r = client.patch(f"/butler/runs/{rid}/archive")
    assert r.status_code == 200, r.text
    assert r.json()["archived_at"]


def test_run_archive_unknown_run_is_404(env, monkeypatch):
    client, _, _, _ = _client(env, monkeypatch)
    r = client.patch("/butler/runs/run_nope/archive", json={"reason": "x"})
    assert r.status_code == 404
