"""DL4 分步上下文 bundle：manifest + 按 cursor 取页。

只产出纯数据（文本/字符数/digest），不出现宿主对象。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any

from server.butler.store import ButlerError, ButlerStore
from server.memory.norms import build_norms_bundle
from server.memory.store import MemoryStore
from server.todos.store import TodosStore

#: 首版顺序（`15` §5.8）；本层不截断，预算由 harness/循环负责
BUNDLE_ORDER: list[str] = [
    "norms",
    "task",
    "handoff",
    "memory.must_read",
    "memory.index",
    "guide",
    "environment",
    "artifacts",
]

_KIND_PRIORITY = {
    "preference": 0, "pitfall": 1, "procedure": 2, "fact": 3, "reference": 4,
}
_STOPWORDS = {
    "的", "了", "在", "是", "和", "与", "或", "把", "被", "对", "这", "那",
    "一个", "一下", "什么", "怎么", "如何", "以及", "然后", "还有", "the",
    "and", "for", "with", "this", "that", "from", "into",
}

# 单个 sqlite 连接不允许并发 execute；并行 MCP 调用时把「读库组装」串行化。
# 只锁组装（毫秒级），不锁 HTTP 请求本身。
_SOURCES_LOCK = threading.RLock()


def _scope_priority(scope: str | None) -> int:
    if scope and scope.startswith("domain:"):
        return 0
    if scope and scope.startswith("project:"):
        return 1
    if scope == "global":
        return 2
    return 3


def _allowed_scopes(run: dict[str, Any]) -> list[str]:
    scopes = ["global"]
    if run.get("project_id"):
        scopes.append(f"project:{run['project_id']}")
    if run.get("domain_id"):
        scopes.append(f"domain:{run['domain_id']}")
    return scopes


def _task_query_text(run: dict[str, Any], todos: TodosStore) -> str:
    scope = run.get("task_scope")
    task_id = run.get("task_id")
    parts: list[str] = []
    if scope == "wip" and task_id:
        task = todos.get_wip(task_id) or {}
        parts.extend(str(task.get(k) or "") for k in ("title", "goal", "done_when"))
        parts.extend(str(s) for s in (task.get("next_steps") or []))
        state = task.get("current_state")
        if state:
            parts.append(json.dumps(state, ensure_ascii=False))
    elif scope == "todos" and task_id:
        task = todos.get_todo(task_id) or {}
        parts.extend(str(task.get(k) or "") for k in ("title", "frequency"))
    if not parts:
        parts.append(str(run.get("prompt") or ""))
    return " ".join(p for p in parts if p)


def _terms(text: str, limit: int = 20) -> list[str]:
    """粗分词：ASCII 词 + 中文 2-gram；不做词性标注，够召回即可。"""
    low = text.lower()
    terms: list[str] = []
    for m in re.findall(r"[a-z][a-z0-9_\-]{1,}", low):
        if m not in _STOPWORDS and m not in terms:
            terms.append(m)
    for block in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(block) - 1):
            bg = block[i:i + 2]
            if bg not in _STOPWORDS and bg not in terms:
                terms.append(bg)
    return terms[:limit]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _task_text(run: dict[str, Any], todos: TodosStore) -> tuple[str, str]:
    scope = run.get("task_scope")
    task_id = run.get("task_id")
    if scope == "wip":
        task = todos.get_wip(task_id) if task_id else None
        if not task:
            return f"（任务 wip:{task_id} 未找到）", "error"
        steps = task.get("next_steps") or []
        lines = [
            f"# 任务：{task.get('title')}",
            f"状态：{task.get('status')}｜优先级：{task.get('priority')}｜进度：{task.get('progress')}%",
            f"目标：{task.get('goal') or '（未写）'}",
            f"完成判据：{task.get('done_when') or '（未写）'}",
            f"截止：{task.get('due_at') or '（无）'}",
        ]
        if steps:
            lines.append("下一步：")
            lines.extend(f"- {s}" for s in steps[:3])
        return "\n".join(lines), "ready"
    if scope == "todos":
        task = todos.get_todo(task_id) if task_id else None
        if not task:
            return f"（任务 todo:{task_id} 未找到）", "error"
        return "\n".join([
            f"# 任务：{task.get('title')}",
            f"类型：{task.get('type')}｜状态：{task.get('status')}",
            f"周期：{task.get('frequency') or '（无）'}｜下次到期：{task.get('next_due_at') or '（无）'}",
        ]), "ready"
    prompt = (run.get("prompt") or "").strip()
    if not prompt:
        return "（无任务描述）", "empty"
    return f"# 本次任务（adhoc）\n{prompt}", "ready"


def _handoff_text(run: dict[str, Any], butler: ButlerStore) -> tuple[str, str]:
    handoff_id = run.get("handoff_id")
    if not handoff_id:
        return "", "empty"
    handoff = butler.get_handoff(handoff_id)
    payload = handoff.get("payload") or {}
    sections = [
        ("卡点", ("blocker", "卡点")),
        ("目标与验收", ("goal", "目标", "目标与验收")),
        ("已落地且已验证", ("verified", "已验证", "已落地且已验证")),
        ("现场状态", ("state", "现场", "现场状态")),
        ("已决策与理由", ("decisions", "决策", "已决策与理由")),
        ("待裁决", ("pending", "待裁决", "待定")),
        ("下一步", ("next_steps", "下一步")),
    ]

    def pick(keys: tuple[str, ...]) -> Any:
        for k in keys:
            if k in payload:
                return payload[k]
        return None

    lines = [f"# 交接：{handoff.get('title') or handoff_id}"]
    used = set()
    for label, keys in sections:
        value = pick(keys)
        if value is None:
            continue
        for k in keys:
            used.add(k)
        if isinstance(value, list):
            lines.append(f"## {label}")
            lines.extend(f"- {v}" for v in value)
        elif isinstance(value, dict):
            lines.append(f"## {label}\n{json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"## {label}\n{value}")
    leftover = {k: v for k, v in payload.items() if k not in used}
    if leftover:
        lines.append("## 其他\n" + json.dumps(leftover, ensure_ascii=False))
    return "\n".join(lines), "ready"


def _memory_rows(run: dict[str, Any], memory: MemoryStore) -> list[dict[str, Any]]:
    """must_read 候选：preference / pitfall / procedure；全局 + 项目 + domain。"""
    rows: list[dict[str, Any]] = []
    for scope in _allowed_scopes(run):
        rows.extend(memory.list_facts_for_context(
            scope=scope, kinds=["preference", "pitfall", "procedure"], limit=30
        ))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("key") or "")
        if not key or key in seen or row.get("kind") == "norm":
            continue
        seen.add(key)
        out.append(row)
    out.sort(key=lambda r: (
        -int(r.get("must_read") or 0),
        _KIND_PRIORITY.get(str(r.get("kind")), 9),
        _scope_priority(r.get("scope")),
        str(r.get("updated_at") or ""),
        str(r.get("id") or ""),
    ))
    return out


def _memory_text(run: dict[str, Any],
                 memory: MemoryStore) -> tuple[str, str, set[str]]:
    rows = _memory_rows(run, memory)
    lines: list[str] = []
    keys: set[str] = set()
    for row in rows:
        key = str(row.get("key") or "")
        keys.add(key)
        title = row.get("title") or key
        summary = row.get("summary") or str(row.get("value") or "")[:120]
        lines.append(f"- {title}：{summary}（key={key}）")
    if not lines:
        return "", "empty", keys
    return "\n".join(lines), "ready", keys


def _memory_index_text(run: dict[str, Any], memory: MemoryStore,
                       todos: TodosStore, exclude: set[str]) -> tuple[str, str]:
    """任务关键词命中 fact/reference；只给标题 + 一句 + 指针。"""
    terms = _terms(_task_query_text(run, todos))
    if not terms:
        return "", "empty"
    allowed = set(_allowed_scopes(run))
    scored: list[tuple[float, int, str, str, dict[str, Any]]] = []
    for row in memory.list_facts(limit=500):
        kind = str(row.get("kind") or "")
        if kind not in ("fact", "reference"):
            continue
        scope = row.get("scope")
        if scope not in allowed:
            continue
        key = str(row.get("key") or "")
        if not key or key in exclude:
            continue
        hay = " ".join(str(row.get(k) or "") for k in
                       ("key", "value", "title", "summary")).lower()
        hits = sum(1 for t in terms if t in hay)
        if not hits:
            continue
        scored.append((
            hits / len(terms), _scope_priority(scope),
            str(row.get("updated_at") or ""), key, row,
        ))
    scored.sort(key=lambda x: (x[0], -x[1], x[2], x[3]), reverse=True)
    lines: list[str] = []
    seen: set[str] = set()
    for _score, _sp, _updated, key, row in scored:
        if key in seen:
            continue
        seen.add(key)
        title = row.get("title") or key
        summary = row.get("summary") or str(row.get("value") or "")[:100]
        lines.append(f"- [{row.get('kind')}] {title}：{summary}（memory.get:{key}）")
    if not lines:
        return "", "empty"
    return "\n".join(lines), "ready"


_GUIDE_CACHE: dict[str, tuple[str, str]] = {}


def _guide_text(run: dict[str, Any], todos: TodosStore) -> tuple[str, str]:
    query = _task_query_text(run, todos)
    if not query.strip():
        return "", "empty"
    cached = _GUIDE_CACHE.get(query)
    if cached is not None:
        return cached
    try:
        from server.agent_guide import GUIDE_REGISTRY
        from server.agent_guide_embedder import (
            SEMANTIC_STRONG_THRESHOLD,
            semantic_match,
        )

        matches = semantic_match(query, top_k=8)
    except Exception:
        return "", "error"
    lines: list[str] = []
    for task_type, cosine in matches:
        if cosine < SEMANTIC_STRONG_THRESHOLD:
            continue
        entry = GUIDE_REGISTRY.get(task_type) or {}
        name = entry.get("name") or task_type
        desc = entry.get("description") or ""
        if not desc:
            continue
        first = entry.get("first_action") or ""
        pointer = f"（first_action: {str(first)[:120]}）" if first else ""
        lines.append(f"- {name}：{desc}{pointer}")
    result = ("\n".join(lines), "ready") if lines else ("", "empty")
    if len(_GUIDE_CACHE) >= 64:
        _GUIDE_CACHE.clear()
    _GUIDE_CACHE[query] = result
    return result


def _environment_text() -> tuple[str, str]:
    root = Path.cwd()
    lines = [
        "# 环境",
        f"OS: {platform.system()} {platform.release()}",
        f"Python: {sys.version.split()[0]}",
        f"Shell: {os.environ.get('COMSPEC') or os.environ.get('SHELL') or '(未知)'}",
        f"Project: {root}",
        f"Date: {date.today().isoformat()}",
    ]
    runtime = root / "data" / "runtime" / "butler_runtime.json"
    if runtime.exists():
        try:
            info = json.loads(runtime.read_text(encoding="utf-8"))
            if info.get("port"):
                lines.append(f"Butler runtime: http://127.0.0.1:{info['port']}")
        except (OSError, ValueError):
            pass
    return "\n".join(lines), "ready"


def _artifact_items(run: dict[str, Any], todos: TodosStore,
                    butler: ButlerStore) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    payload = butler.latest_report_payload(run["run_id"])
    for a in payload.get("artifacts") or []:
        items.append({"path": a} if isinstance(a, str) else dict(a))
    # 上一轮 report 的 evidence 进 artifacts 页：不这么做，evidence 就是「只写不读」的
    # 死字段（2026-10-28 核实：全仓只有 bool(evidence) 一处用到它）。
    # 带 path:line / 命令 / URL 三种形态，所以校验时只核路径部分，见 _artifacts_text。
    for e in payload.get("evidence") or []:
        if isinstance(e, str) and e.strip():
            items.append({"path": e.strip(), "type": "evidence"})
        elif isinstance(e, dict):
            items.append({**e, "type": e.get("type") or "evidence"})

    task: dict[str, Any] = {}
    if run.get("task_scope") == "wip" and run.get("task_id"):
        task = todos.get_wip(run["task_id"]) or {}
    elif run.get("task_scope") == "todos" and run.get("task_id"):
        task = todos.get_todo(run["task_id"]) or {}
    for a in (task.get("related_files") or []) + (task.get("evidence_refs") or []):
        items.append({"path": a, "type": "task"})

    if run.get("handoff_id"):
        handoff = butler.get_handoff(run["handoff_id"])
        for a in (handoff.get("payload") or {}).get("files") or []:
            items.append({"path": a, "type": "handoff"})
    return items


def _artifacts_text(run: dict[str, Any], todos: TodosStore,
                    butler: ButlerStore) -> tuple[str, str]:
    lines: list[str] = []
    seen: set[str] = set()
    for item in _artifact_items(run, todos, butler):
        raw = item.get("path")
        if isinstance(raw, dict):
            raw = raw.get("path") or raw.get("file")
        path = str(raw or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        typ = str(item.get("type") or "file")
        summary = str(item.get("summary") or "").strip()
        missing = ""
        # evidence 允许三种形态：path:line / 命令 / URL。只对「像本地路径」的做存在性检查，
        # 否则每条都会因为 :530 这类行号被误判成 missing。
        check = path
        if typ == "evidence":
            check = path.split(" ", 1)[0]
            check = re.sub(r":\d+(-\d+)?$", "", check)
            if check.startswith(("http://", "https://")):
                check = ""
        if check and any(ch in check for ch in ("/", "\\", ":")):
            if not Path(check).exists():
                missing = " [missing]"
        lines.append(f"- {path}（{typ}）：{summary}{missing}")
    if not lines:
        return "", "empty"
    return "\n".join(lines), "ready"


def _build_sources(run: dict[str, Any], memory: MemoryStore,
                   todos: TodosStore, butler: ButlerStore) -> dict[str, dict[str, Any]]:
    norms = build_norms_bundle(memory, project_id=run.get("project_id"))
    norms_text = "\n".join(item["text"] for item in norms["items"])
    task_text, task_status = _task_text(run, todos)
    handoff_text, handoff_status = _handoff_text(run, butler)
    memory_text, memory_status, must_keys = _memory_text(run, memory)
    index_text, index_status = _memory_index_text(run, memory, todos, must_keys)
    guide_text, guide_status = _guide_text(run, todos)
    env_text, env_status = _environment_text()
    artifacts_text, artifacts_status = _artifacts_text(run, todos, butler)

    sources: dict[str, dict[str, Any]] = {
        "norms": {
            "text": norms_text,
            "status": "ready" if norms_text else "empty",
        },
        "task": {"text": task_text, "status": task_status},
        "handoff": {"text": handoff_text, "status": handoff_status},
        "memory.must_read": {"text": memory_text, "status": memory_status},
        "memory.index": {"text": index_text, "status": index_status},
        "guide": {"text": guide_text, "status": guide_status},
        "environment": {"text": env_text, "status": env_status},
        "artifacts": {"text": artifacts_text, "status": artifacts_status},
    }
    for name in BUNDLE_ORDER:
        if name not in sources:
            sources[name] = {"text": "", "status": "on_demand"}
    for _name, src in sources.items():
        src["chars"] = len(src["text"])
    return sources


def build_sources(run: dict[str, Any], memory: MemoryStore,
                  todos: TodosStore, butler: ButlerStore) -> dict[str, dict[str, Any]]:
    """串行化读库组装：单个 sqlite 连接不能并发 execute。"""
    with _SOURCES_LOCK:
        return _build_sources(run, memory, todos, butler)


def build_manifest(run: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    bundles = [
        {"name": name, "chars": sources[name]["chars"], "status": sources[name]["status"]}
        for name in BUNDLE_ORDER
    ]
    pages = [
        {
            "bundle": name,
            "cursor": f"{name}/0",
            "chars": sources[name]["chars"],
            "status": sources[name]["status"],
        }
        for name in BUNDLE_ORDER
    ]
    first = next(
        (name for name in BUNDLE_ORDER if sources[name]["status"] == "ready"
         and sources[name]["text"]),
        None,
    )
    return {
        "run_id": run["run_id"],
        "session_code_prefix": run.get("code_prefix"),
        "task": {
            "scope": run.get("task_scope"),
            "id": run.get("task_id"),
            "title": (run.get("prompt") or "")[:80],
        },
        "bundles": bundles,
        "pages": pages,
        "total_chars": sum(b["chars"] for b in bundles),
        "next_cursor": f"{first}/0" if first else None,
        "instructions": (
            "用 pages[].cursor 逐页取：一页一个调用，同一轮全部发出，"
            "不要把多页塞进一次 cursors[]；"
            "mark 为 ready 的页必取；"
            "每轮收尾调 wrapup 的 report（task/memory/feedback 按需）。"
        ),
    }


def _next_ready(sources: dict[str, dict[str, Any]], after: str | None) -> str | None:
    started = after is None
    for name in BUNDLE_ORDER:
        if not started:
            if name == after:
                started = True
            continue
        if sources[name]["status"] == "ready" and sources[name]["text"]:
            return name
    return None


def page_for_cursor(sources: dict[str, dict[str, Any]], cursor: str | None) -> dict[str, Any]:
    """返回整页正文；本层不做截断/分页，截断由 harness 或自建循环负责。"""
    if not cursor:
        name = _next_ready(sources, None)
        if not name:
            raise ButlerError("no_bundles", "没有可投递的 bundle", 409)
    else:
        try:
            name, raw_index = cursor.rsplit("/", 1)
            index = int(raw_index)
        except (ValueError, AttributeError):
            raise ButlerError("bad_cursor", "cursor 形如 bundle/0") from None
        if index != 0:
            raise ButlerError("bad_cursor", "cursor 只有 bundle/0；本层不截断")
        if name not in sources:
            raise ButlerError("bad_cursor", f"未知 bundle: {name}") from None
        if not sources[name]["text"]:
            following = _next_ready(sources, name)
            if not following:
                raise ButlerError("cursor_end", "bundle 已取完", 409)
            name = following

    text = sources[name]["text"]
    following = _next_ready(sources, name)
    return {
        "bundle": name,
        "cursor": f"{name}/0",
        "text": text,
        "chars": len(text),
        "digest": _digest(text),
        "next_cursor": f"{following}/0" if following else None,
        "status": sources[name]["status"],
    }
