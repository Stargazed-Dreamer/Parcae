"""派单提示词**多版本 + 回滚**（v13）+ 执行身份自报 + 任务维度交接。

2026-10-29 用户裁定：「派单的有历史可回滚；交接的不需要」。
所以本文件分两块：
  ① 派单提示词：看 / 改 / 删 / 回滚（设为当前）对**任意一版**生效，
     每轮报告后自动追加一版并设为当前，上限 10 版；
  ② 交接提示词：只有当前一份，不落版本；且**必须带会话码**——
     码来自「用这份交接开新一轮」，没开就没有码。
"""

from __future__ import annotations

import pytest

from server.butler.service import open_session, report_step
from server.butler.store import ButlerError, ButlerStore
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


def _new_run(butler: ButlerStore, prompt: str = "原始要求") -> dict:
    return butler.create_run(prompt)


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


# ---- 存储层：多版本 ------------------------------------------------------


def test_append_makes_it_current_and_lists_versions(env):
    _, _, butler = env
    run = _new_run(butler)
    rid = run["run_id"]

    first = butler.append_prompt_version(rid, "第一版", source="initial")
    assert first["version"] == 1 and first["is_current"] is True
    second = butler.append_prompt_version(rid, "第二版", source="auto_next")
    assert second["version"] == 2

    saved = butler.get_run(rid)
    assert saved["prompt_text"] == "第二版"
    assert saved["prompt_version"] == 2
    assert saved["prompt_source"] == "auto_next"

    listed = butler.list_prompt_versions(rid)
    assert listed["current"] == 2 and listed["latest"] == 2
    assert [v["version"] for v in listed["items"]] == [1, 2]
    assert listed["items"][0]["is_current"] is False
    assert listed["items"][1]["chars"] == len("第二版")


def test_update_any_version_and_sync_current(env):
    """历史版可改；改的若是当前版，run.prompt_text 要跟着变。"""
    _, _, butler = env
    rid = _new_run(butler)["run_id"]
    butler.append_prompt_version(rid, "第一版", source="initial")
    butler.append_prompt_version(rid, "第二版", source="auto_next")

    done = butler.update_prompt_version(rid, 1, "改过的第一版")
    assert done == {"version": 1, "chars": len("改过的第一版"), "is_current": False}
    assert butler.get_prompt_version(rid, 1)["prompt_text"] == "改过的第一版"
    assert butler.get_run(rid)["prompt_text"] == "第二版"  # 当前版没被牵连

    butler.update_prompt_version(rid, 2, "改过的第二版")
    assert butler.get_run(rid)["prompt_text"] == "改过的第二版"

    with pytest.raises(ButlerError) as missing:
        butler.update_prompt_version(rid, 99, "不存在")
    assert missing.value.code == "prompt_version_not_found"


def test_activate_is_rollback(env):
    _, _, butler = env
    rid = _new_run(butler)["run_id"]
    butler.append_prompt_version(rid, "第一版", source="initial")
    butler.append_prompt_version(rid, "第二版", source="auto_next")

    butler.activate_prompt_version(rid, 1)
    saved = butler.get_run(rid)
    assert saved["prompt_text"] == "第一版"
    assert saved["prompt_version"] == 1
    assert saved["prompt_source"] == "rollback"
    # 只增不毁：v2 仍在，随时能切回去
    assert [v["version"] for v in butler.list_prompt_versions(rid)["items"]] == [1, 2]


def test_delete_version_switches_current_and_keeps_last(env):
    _, _, butler = env
    rid = _new_run(butler)["run_id"]
    for i in (1, 2, 3):
        butler.append_prompt_version(rid, f"第{i}版", source="auto_next")

    # 删历史版：当前版不动
    assert butler.delete_prompt_version(rid, 1)["current"] == 3
    # 删当前版：自动切到剩下的最大版，正文同步
    res = butler.delete_prompt_version(rid, 3)
    assert res["current"] == 2
    assert butler.get_run(rid)["prompt_text"] == "第2版"

    with pytest.raises(ButlerError) as last:
        butler.delete_prompt_version(rid, 2)
    assert last.value.code == "prompt_version_last"

    with pytest.raises(ButlerError) as missing:
        butler.delete_prompt_version(rid, 99)
    assert missing.value.code == "prompt_version_not_found"


def test_version_limit_keeps_latest_ten(env):
    _, _, butler = env
    rid = _new_run(butler)["run_id"]
    for i in range(1, 13):
        butler.append_prompt_version(rid, f"第{i}版", source="auto_next")

    items = butler.list_prompt_versions(rid)["items"]
    assert len(items) == 10
    assert [v["version"] for v in items] == list(range(3, 13))
    assert butler.get_run(rid)["prompt_version"] == 12


def test_legacy_run_backfills_v1(env):
    """版本表启用前的 run：第一次读取时用现存 prompt_text 补 v1，不丢正文。"""
    _, _, butler = env
    rid = _new_run(butler)["run_id"]
    with butler._lock:
        butler.conn.execute(
            "UPDATE butler_runs SET prompt_text = ?, prompt_version = NULL WHERE run_id = ?",
            ("老正文", rid),
        )
        butler.conn.commit()

    listed = butler.list_prompt_versions(rid)
    assert len(listed["items"]) == 1
    assert listed["items"][0]["version"] == 1
    assert listed["current"] == 1
    assert butler.get_prompt_version(rid, 1)["prompt_text"] == "老正文"


def test_empty_prompt_is_rejected(env):
    _, _, butler = env
    rid = _new_run(butler)["run_id"]
    with pytest.raises(ButlerError) as err:
        butler.append_prompt_version(rid, "   ", source="manual")
    assert err.value.code == "prompt_empty"
    with pytest.raises(ButlerError) as missing:
        butler.append_prompt_version("run_missing", "正文", source="manual")
    assert missing.value.code == "run_not_found"


# ---- 身份自报 ------------------------------------------------------------


def test_report_saves_agent_identity(env):
    """跳过 session.open 后没人再报 harness/model，报告必须能补上。"""
    mem, todos, butler = env
    run = _new_run(butler)
    open_session(butler, mem, todos, run["session_code"])
    report_step(butler, todos, run["session_code"], {
        "outcome": "progress", "summary": "第一轮",
        "harness": "CodeBuddy", "model": "deepseek-v4",
    })
    saved = butler.get_run(run["run_id"])
    assert saved["harness"] == "CodeBuddy"
    assert saved["model"] == "deepseek-v4"

    # 后续报告不带身份：不能把已知值清空
    report_step(butler, todos, run["session_code"], {
        "outcome": "progress", "summary": "第二轮",
    })
    again = butler.get_run(run["run_id"])
    assert again["harness"] == "CodeBuddy"
    assert again["model"] == "deepseek-v4"


# ---- HTTP 层：版本生命周期 -----------------------------------------------


def test_http_version_lifecycle(env, monkeypatch):
    client, _, todos, butler = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "版本生命周期", "status": "active", "priority": "high",
        "goal": "看/改/删/回滚", "done_when": "四个动作都能走通",
    })
    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
        "task_title": task["title"], "done_when": task["done_when"],
    }).json()
    run_id = created["run_id"]

    listed = client.get(f"/butler/runs/{run_id}/prompts").json()
    assert listed["current"] == 1 and len(listed["items"]) == 1
    assert listed["limit"] == 10
    assert listed["items"][0]["source"] == "initial"

    # 模拟下一轮自动生成的版本
    butler.append_prompt_version(run_id, "第二轮提示词", source="auto_next")
    listed = client.get(f"/butler/runs/{run_id}/prompts").json()
    assert listed["current"] == 2 and listed["latest"] == 2

    # 取某一版正文
    body = client.get(f"/butler/runs/{run_id}/prompts/1").json()
    assert body["is_current"] is False
    assert body["prompt_text"].startswith("【")

    # 改历史版
    resp = client.put(f"/butler/runs/{run_id}/prompts/1", json={"prompt_text": "改过的一版"})
    assert resp.status_code == 200
    assert resp.json()["is_current"] is False

    # 回滚到它
    resp = client.post(f"/butler/runs/{run_id}/prompts/1/activate")
    assert resp.status_code == 200 and resp.json()["is_current"] is True
    assert butler.get_run(run_id)["prompt_text"] == "改过的一版"

    # 删当前版 → 自动切到剩下的
    resp = client.delete(f"/butler/runs/{run_id}/prompts/1")
    assert resp.status_code == 200
    assert resp.json()["current"] == 2
    # 只剩一版：409
    last = client.delete(f"/butler/runs/{run_id}/prompts/2")
    assert last.status_code == 409
    assert last.json()["detail"]["code"] == "prompt_version_last"

    # 空正文：模型层 422；纯空白走存储层 400
    assert client.put(
        f"/butler/runs/{run_id}/prompts/2", json={"prompt_text": ""}
    ).status_code == 422
    blank = client.put(f"/butler/runs/{run_id}/prompts/2", json={"prompt_text": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"]["code"] == "prompt_empty"


def test_http_save_current_prompt_does_not_add_version(env, monkeypatch):
    """面板主按钮「保存」= 改当前版正文，不新增版本（历史靠每轮自动生成）。"""
    client, _, todos, butler = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "保存不改版本数", "status": "active", "priority": "medium",
        "goal": "保存只改正文", "done_when": "版本数不变",
    })
    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
    }).json()
    run_id = created["run_id"]

    resp = client.put(f"/butler/runs/{run_id}/prompt", json={"prompt_text": "我改过的提示词"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["chars"] == len("我改过的提示词")
    assert body["prompt_version"] == 1

    listed = client.get(f"/butler/runs/{run_id}/prompts").json()
    assert len(listed["items"]) == 1
    assert butler.get_run(run_id)["prompt_text"] == "我改过的提示词"
    assert butler.get_run(run_id)["prompt_source"] == "user_edit"


# ---- 每轮自动生成：追加新版本，不再跳过手改版 ----------------------------


def test_report_appends_next_prompt_version(env, monkeypatch):
    """一轮报告落库后追加一版并设为当前；用户手改过的那版留在历史里，不销毁。"""
    client, _, todos, butler = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "自动生成测试", "status": "active", "priority": "high",
        "goal": "看下一轮提示词是否更新", "done_when": "报告后正文变化",
    })
    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
        "task_title": task["title"], "done_when": task["done_when"],
    }).json()
    run_id, code = created["run_id"], created["session_code"]

    # 先手改当前版
    client.put(f"/butler/runs/{run_id}/prompt", json={"prompt_text": "手改的正文"})
    client.post("/butler/context", json={"action": "next", "code": code})
    client.post("/butler/wrapup", json={
        "action": "task", "code": code,
        "task_updates": {"done_when": "报告后正文变成续跑版"},
    })
    resp = client.post("/butler/wrapup", json={
        "action": "report", "code": code,
        "outcome": "progress", "summary": "第一轮",
        "harness": "CodeBuddy", "model": "deepseek-v4",
    })
    assert resp.status_code == 200
    assert resp.json()["next_prompt_version"] == 2

    saved = butler.get_run(run_id)
    assert saved["prompt_source"] == "auto_next"
    assert saved["prompt_version"] == 2
    assert saved["prompt_text"].startswith("【续跑】")
    assert "报告后正文变成续跑版" in saved["prompt_text"]

    # 手改的那一版还在，能回滚回去
    listed = butler.list_prompt_versions(run_id)
    assert [v["version"] for v in listed["items"]] == [1, 2]
    assert butler.get_prompt_version(run_id, 1)["prompt_text"] == "手改的正文"


def test_summary_hint_200_but_hard_limit_500(env):
    """提示词写 200 引导重点，系统放行到 500（2026-10-28 用户裁定）。"""
    mem, todos, butler = env
    run = _new_run(butler)
    open_session(butler, mem, todos, run["session_code"])

    report_step(butler, todos, run["session_code"], {
        "outcome": "progress", "summary": "汉" * 200,
    })
    report_step(butler, todos, run["session_code"], {
        "outcome": "progress", "summary": "汉" * 500,
    })
    with pytest.raises(ButlerError) as err:
        report_step(butler, todos, run["session_code"], {
            "outcome": "progress", "summary": "汉" * 501,
        })
    assert err.value.code == "summary_too_long"
    assert "501" in err.value.message and "500" in err.value.message


def test_next_prompt_is_continue_variant_and_short(env, monkeypatch):
    """续跑轮不该再塞一遍完整提示词（用户 2026-10-28 裁定 b）。"""
    client, _, todos, butler = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "续跑测试", "status": "active", "priority": "high",
        "goal": "看下一轮提示词形态", "done_when": "下一轮是精简版",
    })
    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
        "task_title": task["title"], "done_when": task["done_when"],
    }).json()
    run_id, code = created["run_id"], created["session_code"]
    client.post("/butler/context", json={"action": "next", "code": code})
    client.post("/butler/wrapup", json={
        "action": "report", "code": code,
        "outcome": "progress", "summary": "做了第一步",
    })

    text = butler.get_run(run_id)["prompt_text"]
    assert text.startswith("【续跑】")
    assert len(text) < 800
    assert "【开工前" not in text
    assert "harness" not in text
    assert "cursor=" in text
    assert "【收尾】" in text


def test_next_prompt_is_full_variant_when_handoff(env, monkeypatch):
    """接手轮才给完整提示词（裁定 a）。"""
    client, _, todos, butler = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "接手测试", "status": "active", "priority": "high",
        "goal": "验证接手版", "done_when": "下一轮是完整版",
    })
    handoff = butler.create_handoff(task_scope="wip", task_id=task["id"],
                                    payload={"next_steps": ["接着做"]})
    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
        "task_title": task["title"], "done_when": task["done_when"],
        "handoff_id": handoff["handoff_id"],
    }).json()
    run_id, code = created["run_id"], created["session_code"]
    client.post("/butler/context", json={"action": "next", "code": code})
    client.post("/butler/wrapup", json={
        "action": "report", "code": code,
        "outcome": "progress", "summary": "接手后先看了一眼",
    })

    text = butler.get_run(run_id)["prompt_text"]
    assert text.startswith("【接手】")
    assert "【开工前" in text
    assert "harness" in text
    assert len(text) > 900


def test_identity_can_be_reported_in_task_step(env, monkeypatch):
    """身份改在「开工前」那条 task 里报，服务端任何一步都要能接住。"""
    client, _, todos, butler = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "身份测试", "status": "active", "priority": "high",
        "goal": "身份挂在 task 上", "done_when": "run 上有 harness/model",
    })
    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
        "task_title": task["title"], "done_when": task["done_when"],
    }).json()
    code = created["session_code"]
    client.post("/butler/context", json={"action": "next", "code": code})

    resp = client.post("/butler/wrapup", json={
        "action": "task", "code": code,
        "harness": "CodeBuddy", "model": "deepseek-v4-flash",
        "task_updates": {"progress": 10},
    })
    assert resp.status_code == 200
    run = butler.get_run(created["run_id"])
    assert run["harness"] == "CodeBuddy"
    assert run["model"] == "deepseek-v4-flash"


def test_http_prompt_variant_two_switches(env, monkeypatch):
    """面板两个独立开关：terse / with_context 各自生效，预览不落库。"""
    client, _, todos, butler = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "开关测试", "status": "active", "priority": "medium",
        "goal": "验证开关", "done_when": "四种组合都能取到",
    })
    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
        "task_title": task["title"], "done_when": task["done_when"],
    }).json()
    run_id = created["run_id"]
    before = butler.get_run(run_id)["prompt_text"]

    sizes = {}
    for terse in ("false", "true"):
        for ctx in ("true", "false"):
            resp = client.get(
                f"/butler/runs/{run_id}/prompt?terse={terse}&with_context={ctx}"
            )
            assert resp.status_code == 200
            body = resp.json()
            sizes[(terse, ctx)] = body["chars"]
            assert body["prompt_text"].startswith("【用户原文】")
            assert 'code="' in body["prompt_text"]
            assert body["terse"] is (terse == "true")
    assert sizes[("false", "true")] > sizes[("true", "true")]
    assert sizes[("true", "true")] > sizes[("true", "false")]
    assert butler.get_run(run_id)["prompt_text"] == before


# ---- 交接：只有一份，且必须带会话码 --------------------------------------


def test_task_handoff_prompt_states_and_code(env, monkeypatch):
    """交接按任务取：没 run / 未交接（带当前码）/ 已交接（先没码，开轮后有码）。"""
    client, _, todos, butler = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "交接测试", "status": "active", "priority": "high",
        "goal": "两态内容正确", "done_when": "交接文本包含原因与待办",
    })
    url = f"/butler/tasks/wip/{task['id']}/handoff-prompt"

    first = client.get(url).json()
    assert first["state"] == "no_run"
    assert first["prompt_text"] == ""

    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
        "task_title": task["title"], "done_when": task["done_when"],
    }).json()
    code = created["session_code"]
    client.post("/butler/context", json={"action": "next", "code": code})

    writing = client.get(url).json()
    assert writing["state"] == "handoff"
    assert f'code="{code}"' in writing["prompt_text"]
    assert 'action="handoff"' in writing["prompt_text"]

    resp = client.post("/butler/wrapup", json={
        "action": "handoff", "code": code,
        "handoff": {
            "needed": True, "title": "换人",
            "reason": "缺用户决策：要不要动测试库",
            "done": [
                {"item": "改完 store 版本方法", "evidence": "pytest tests/butler 通过"},
                {"item": "读了提示词模板"},
            ],
            "next_steps": ["补测试", "更新 DD-提示词 §4.9"],
        },
    })
    assert resp.status_code == 200

    takeover = client.get(url).json()
    assert takeover["state"] == "takeover"
    assert takeover["handoff_id"]
    assert takeover["need_run"] is True          # 还没开新一轮，没有码
    text = takeover["prompt_text"]
    assert text.startswith("【交接】上一任执行者留下的交接单")
    assert "缺用户决策" in text
    assert "改完 store 版本方法" in text and "pytest tests/butler 通过" in text
    assert "上一任未给验证方式" in text
    assert "补测试" in text and "更新 DD-提示词" in text
    assert "（未分配" in text

    # 开新一轮：码出现，接手提示词写它
    opened = client.post(f"/butler/tasks/wip/{task['id']}/handoff-run")
    assert opened.status_code == 200
    body = opened.json()
    new_code = body["session_code"]
    assert new_code and new_code != code
    assert body["reused"] is False
    assert f"会话码：{new_code}" in body["prompt_text"]

    # 幂等：再点一次复用同一个 run，不重复开
    again = client.post(f"/butler/tasks/wip/{task['id']}/handoff-run").json()
    assert again["reused"] is True
    assert again["session_code"] == new_code

    # 再取交接提示词：这次带码，不再 need_run
    final = client.get(url).json()
    assert final["need_run"] is False
    assert new_code in final["prompt_text"]


def test_handoff_run_needs_active_handoff(env, monkeypatch):
    client, _, todos, _ = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "没交接就开轮", "status": "active", "priority": "low",
        "goal": "应该拒绝", "done_when": "409 no_active_handoff",
    })
    resp = client.post(f"/butler/tasks/wip/{task['id']}/handoff-run")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_active_handoff"


def test_task_handoff_prompt_validates_scope_and_task(env, monkeypatch):
    client, _, _, _ = _client(env, monkeypatch)
    bad = client.get("/butler/tasks/nope/x/handoff-prompt")
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "bad_scope"
    missing = client.get("/butler/tasks/wip/wip_missing/handoff-prompt")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "task_not_found"
    # 面板传的是 task.table（wip_tasks），别名要认（404 而不是 400）
    aliased = client.get("/butler/tasks/wip_tasks/wip_missing/handoff-prompt")
    assert aliased.status_code == 404


def test_task_handoff_requires_an_open_run(env, monkeypatch):
    """会话码随 run 关闭而失效：不能拿死码让人去写交接指令。"""
    client, _, todos, _ = _client(env, monkeypatch)
    task = todos.create_wip({
        "title": "关闭后交接", "status": "active", "priority": "low",
        "goal": "关闭后不出现交接指令", "done_when": "state=no_open_run",
    })
    created = client.post("/butler/runs", json={
        "prompt": "原始要求", "task_scope": "wip", "task_id": task["id"],
    }).json()
    assert client.post(f"/butler/runs/{created['run_id']}/close").status_code == 200

    res = client.get(f"/butler/tasks/wip/{task['id']}/handoff-prompt").json()
    assert res["state"] == "no_open_run"
    assert res["prompt_text"] == ""


def test_active_handoff_ignores_archived(env):
    """archived / superseded 视为已消费：交接卡要回到「未交接」。"""
    _, _, butler = env
    handoff = butler.create_handoff(task_scope="wip", task_id="wip_1",
                                    payload={"blocker": "卡住"})
    assert butler.get_active_handoff(task_scope="wip", task_id="wip_1")["handoff_id"] \
        == handoff["handoff_id"]

    butler.set_handoff_status(handoff["handoff_id"], "pending_archive")
    assert butler.get_active_handoff(task_scope="wip", task_id="wip_1") is not None

    butler.set_handoff_status(handoff["handoff_id"], "archived")
    assert butler.get_active_handoff(task_scope="wip", task_id="wip_1") is None


def test_user_message_relay_origin(monkeypatch):
    """中枢管家转达复用同一条补充指令队列，但来源要标清，不能冒充用户本人。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.user_message as um

    um.clear_all()
    app = FastAPI()
    app.include_router(um.router)
    client = TestClient(app)

    client.post("/user/message", json={"text": "请把范围收窄", "origin": "butler_relay"})
    client.post("/user/message", json={"text": "这是用户本人说的"})
    text = um.get_supplement_text()
    assert text is not None
    assert "中枢管家转达·非用户本人指令" in text
    assert "用户补充指令" in text
    assert "请把范围收窄" in text and "这是用户本人说的" in text
    assert "用户确认" in text  # 边界说明跟着来源走
    um.clear_all()
