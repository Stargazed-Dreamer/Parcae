"""P5 T6：8 页 bundle 组装（index / guide / environment / artifacts + domain）。"""

from __future__ import annotations

import pytest

import server.agent_guide as agent_guide
import server.agent_guide_embedder as agent_guide_embedder
import server.butler.bundles as bundles_mod
from server.butler.bundles import build_sources
from server.butler.service import open_session, submit_session_wrapup
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


@pytest.fixture
def seeded(env):
    mem, todos, butler = env
    mem.upsert_fact("norm_1", "密钥只经 lib/secret。",
                     title="密钥", scope="global", kind="norm", origin="system")
    mem.upsert_fact("pref_1", "先给结论。",
                     title="汇报偏好", scope="global", kind="preference")
    mem.upsert_fact("pit_1", "WAL copy2 会缺行。",
                     title="备份坑", scope="global", kind="pitfall")
    mem.upsert_fact("proc_1", "发版分三步。",
                     title="发版流程", scope="global", kind="procedure")
    mem.upsert_fact("fact_port", "本机后端端口 8766。",
                     title="本机端口", scope="project:parcae", kind="fact")
    mem.upsert_fact("fact_domain", "记账表在 home 域。",
                     title="记账偏好", scope="domain:home", kind="fact")
    mem.upsert_fact("pref_domain", "home 域记账先备份。",
                     title="域偏好", scope="domain:home", kind="preference")
    mem.upsert_fact("fact_other", "无关事实。",
                     title="无关", scope="global", kind="fact")
    task = todos.create_wip({
        "title": "检查端口 8766",
        "status": "active",
        "goal": "确认端口可用",
        "next_steps": ["读配置", "跑一次请求"],
        "done_when": "端口返回 200",
        "related_files": ["/no/such/task-file.txt"],
    })
    return mem, todos, butler, task


def _sources(butler, mem, todos, run):
    return build_sources(run, mem, todos, butler)


def _run(butler, task, **kw):
    created = butler.create_run("检查端口", task_scope="wip", task_id=task["id"],
                                project_id="parcae", **kw)
    return butler.get_run(created["run_id"])


def test_must_read_has_procedure_but_not_norm_or_fact(seeded):
    mem, todos, butler, task = seeded
    run = _run(butler, task)
    page = build_sources(run, mem, todos, butler)["memory.must_read"]
    text = page["text"]
    assert page["status"] == "ready"
    assert "汇报偏好" in text and "备份坑" in text and "发版流程" in text
    assert "密钥" not in text
    assert "无关" not in text
    assert "pref_1" in text


def test_domain_scope_only_when_run_has_domain(seeded):
    mem, todos, butler, task = seeded
    run = _run(butler, task)
    plain = _sources(butler, mem, todos, run)["memory.must_read"]["text"]
    assert "记账" not in plain

    run2 = _run(butler, task, domain_id="home")
    scoped = _sources(butler, mem, todos, run2)["memory.must_read"]["text"]
    assert "域偏好" in scoped


def test_index_uses_task_keywords_and_excludes_must_read(seeded):
    mem, todos, butler, task = seeded
    run = _run(butler, task)
    sources = _sources(butler, mem, todos, run)
    index = sources["memory.index"]
    assert index["status"] == "ready"
    assert "本机端口" in index["text"]
    assert "memory.get:fact_port" in index["text"]
    assert "汇报偏好" not in index["text"]  # 已进 must_read


def test_index_empty_when_no_keyword_hit(seeded):
    mem, todos, butler, task = seeded
    task2 = todos.create_wip({
        "title": "zzzz 无关任务", "status": "active", "done_when": "无",
    })
    run = butler.create_run("zzzz", task_scope="wip", task_id=task2["id"],
                            project_id="parcae")
    assert _sources(butler, mem, todos, run)["memory.index"]["status"] == "empty"


def test_guide_renders_semantic_strong_matches_only(seeded, monkeypatch):
    mem, todos, butler, task = seeded
    bundles_mod._GUIDE_CACHE.clear()

    def fake_semantic(query: str, top_k: int = 8):
        return [("x", 0.9), ("y", 0.4)]

    monkeypatch.setattr(agent_guide_embedder, "semantic_match", fake_semantic)
    monkeypatch.setitem(agent_guide.GUIDE_REGISTRY, "x", {
        "name": "技能X", "description": "场景X", "first_action": "第一步",
    })
    monkeypatch.setitem(agent_guide.GUIDE_REGISTRY, "y", {
        "name": "弱匹配Y", "description": "不该出现", "first_action": "",
    })
    run = _run(butler, task)
    guide = _sources(butler, mem, todos, run)["guide"]
    assert guide["status"] == "ready"
    assert "技能X" in guide["text"] and "弱匹配Y" not in guide["text"]


def test_environment_always_ready(seeded):
    mem, todos, butler, task = seeded
    env = _sources(butler, mem, todos, _run(butler, task))["environment"]
    assert env["status"] == "ready"
    assert "OS:" in env["text"] and "Python:" in env["text"]


def test_artifacts_marks_missing_and_dedupes(seeded):
    mem, todos, butler, task = seeded
    created = butler.create_run("检查端口", task_scope="wip", task_id=task["id"],
                                project_id="parcae")
    code = created["session_code"]
    open_session(butler, mem, todos, code)
    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "progress", "summary": "记产物",
        "artifacts": ["/no/such/report.txt", "/no/such/report.txt"],
    })
    run = butler.get_run(created["run_id"])
    art = _sources(butler, mem, todos, run)["artifacts"]
    assert art["status"] == "ready"
    assert art["text"].count("/no/such/report.txt") == 1
    assert "[missing]" in art["text"]
    assert "/no/such/task-file.txt" in art["text"]


def test_evidence_feeds_artifacts_page(seeded):
    """evidence 必须有人消费：上一轮的证据进下一轮的 artifacts 页。

    2026-10-28 核实：改之前 evidence 全仓只有一个 ``bool()`` 判断（判 claimed_done），
    存进 ``butler_reports.payload_json`` 之后没有任何读侧 —— 写了等于没写。
    另外 evidence 允许 path:line / 命令 / URL 三种形态，只有路径部分参与存在性检查，
    否则每条都因 ``:530`` 这种行号被误标 [missing]。
    """
    mem, todos, butler, task = seeded
    created = butler.create_run("查证据去向", task_scope="wip", task_id=task["id"],
                               project_id="parcae")
    code = created["session_code"]
    open_session(butler, mem, todos, code)

    real_file = __file__.replace("\\", "/")
    real_ref = f"{real_file}:12"
    submit_session_wrapup(butler, mem, todos, code, {
        "outcome": "done", "summary": "给了三条证据",
        "evidence": [real_ref, "https://example.com/spec", "/no/such/probe.py:9"],
    })
    run = butler.get_run(created["run_id"])
    art = _sources(butler, mem, todos, run)["artifacts"]
    assert art["status"] == "ready"
    assert real_ref in art["text"]
    assert "https://example.com/spec" in art["text"]
    # 只有那条真不存在的才标 missing；存在的文件带行号不该被误判
    def _line(ref: str) -> str:
        return next(ln for ln in art["text"].splitlines() if ln.startswith(f"- {ref}（"))

    assert "[missing]" not in _line(real_ref)
    assert "[missing]" in _line("/no/such/probe.py:9")


def test_pages_within_budget_and_digest_stable(seeded):
    mem, todos, butler, task = seeded
    run = _run(butler, task)
    s1 = _sources(butler, mem, todos, run)
    s2 = _sources(butler, mem, todos, run)
    for name in ("memory.index", "guide", "environment", "artifacts"):
        assert s1[name]["text"] == s2[name]["text"]
    assert len(s1["environment"]["text"]) <= 1_000
    assert len(s1["artifacts"]["text"]) <= 1_000
