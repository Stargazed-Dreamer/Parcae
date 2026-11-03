"""DL4 业务编排：open → context.next → wrapup.submit，外加 memory.write。

原则：这里不直接 ``INSERT INTO facts``（记忆走 ``upsert_fact`` 唯一漏斗），
也不把 run/session 概念塞进记忆接口（bundle 是纯数据）。
"""

from __future__ import annotations

import json
from typing import Any

from server.butler.bundles import build_manifest, build_sources, page_for_cursor
from server.butler.store import ButlerError, ButlerStore
from server.memory.store import MemoryStore
from server.memory.wrapup import submit_wrapup as memory_submit_wrapup

# `as write_memory` 是显式再导出（router 与 test_dl4_flow 一直从这里取）：裸 import 在本文件没有
# 本地调用，ruff F401 会当死导入删掉，一删就打断调用方——真的断过一次，故用别名钉住。
from server.memory.write import write_memory as write_memory
from server.todos.store import TodosStore

_OUTCOMES = {"progress", "done", "partial", "blocked", "failed"}
# v14 起 report 不再直写事务状态（见 DD-run级状态与并发.md §2 第 2/3 条）：
# 「一路 done」不等于「事务完成」，「一路 blocked」也不等于「整个事务卡住」。
_CLOSED_RUN_STATUS = ("closed", "abandoned", "failed")


def _memory_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in candidates or []:
        item = dict(c)
        if not item.get("content") and item.get("value"):
            item["content"] = item["value"]
        out.append(item)
    return out


def _clean_done_items(raw: Any) -> list[dict[str, str]]:
    """交接单里的「已完成」统一成 ``[{"item", "evidence"}]``。

    老 skill（`.agents/skills/dev/handoff`）的硬规则：没有验证方式的「已完成」
    等价于「我以为做完了」。这里不替 agent 编造证据，缺就留空——接手说明里会
    显式写成「（上一任未给验证方式，需自己核）」，不让空白看起来像已核实。
    形状兼容：字符串、dict、evidence 为列表。上限 8 条，避免把交接单写成流水账。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for entry in raw[:8]:
        if isinstance(entry, dict):
            item = str(entry.get("item") or entry.get("what") or "").strip()[:300]
            evidence = entry.get("evidence") or entry.get("verify") or ""
            if isinstance(evidence, (list, tuple)):
                evidence = "；".join(str(x) for x in evidence)
            evidence = str(evidence).strip()[:300]
        else:
            item, evidence = str(entry).strip()[:300], ""
        if item:
            out.append({"item": item, "evidence": evidence})
    return out


def open_session(butler: ButlerStore, memory: MemoryStore, todos: TodosStore,
                 code: str, *, owner: str | None = None,
                 harness: str | None = None, model: str | None = None) -> dict[str, Any]:
    run = butler.open_run(code, owner=owner, harness=harness, model=model)
    sources = build_sources(run, memory, todos, butler)
    manifest = build_manifest(run, sources)
    butler.set_manifest(run["run_id"], manifest)
    return {"run": run, "manifest": manifest}


def _deliver_page(butler: ButlerStore, run: dict[str, Any],
                  page: dict[str, Any]) -> dict[str, Any]:
    delivery, stale = butler.record_delivery(
        run["run_id"], page["bundle"], page["cursor"], page["digest"], page["chars"]
    )
    butler.append_event(run["run_id"], "bundle.delivered", {
        "bundle": page["bundle"], "cursor": page["cursor"],
        "chars": page["chars"], "stale": stale,
    })
    page["stale"] = stale
    page["delivery_id"] = delivery.get("id")
    return page


def next_context(butler: ButlerStore, memory: MemoryStore, todos: TodosStore,
                 code: str, cursor: str | None = None,
                 cursors: list[str] | None = None) -> dict[str, Any]:
    run = butler.get_run_by_code(code)
    if run["status"] == "created":
        # 2026-10-28：提示词已带 pages，允许跳过 session.open，直接取上下文。
        run = butler.open_run(code)
    if run["status"] in ("closed", "abandoned", "failed"):
        raise ButlerError("run_closed", "run 已结束", 409)
    butler.touch_run(run["run_id"])
    sources = build_sources(run, memory, todos, butler)

    if cursors:
        pages: list[dict[str, Any]] = []
        for c in cursors:
            try:
                pages.append(_deliver_page(butler, run, page_for_cursor(sources, c)))
            except ButlerError as err:
                pages.append({
                    "cursor": c, "error": err.code, "message": err.message,
                    "status": "error",
                })
        return {"pages": pages, "count": len(pages)}

    return _deliver_page(butler, run, page_for_cursor(sources, cursor))


def session_note(butler: ButlerStore, code: str, text: str, *,
                 kind: str = "note", client_note_id: str | None = None) -> dict[str, Any]:
    run = butler.get_run_by_code(code)
    if run["status"] in ("closed", "abandoned", "failed"):
        raise ButlerError("run_closed", "run 已结束", 409)
    body = str(text or "").strip()
    if not body:
        raise ButlerError("note_text_required", "note.text 不能为空")
    row = butler.add_note(
        run["run_id"], body, direction="agent", kind=kind,
        client_note_id=client_note_id,
    )
    return {
        "note_id": row.get("id"),
        "kind": row.get("kind"),
        "duplicate": bool(row.get("_duplicate")),
    }


def _task_of(todos: TodosStore, run: dict[str, Any]) -> dict[str, Any]:
    scope, tid = run.get("task_scope"), run.get("task_id")
    if scope == "wip" and tid:
        return todos.get_wip(tid) or {}
    if scope == "todos" and tid:
        return todos.get_todo(tid) or {}
    return {}


def _write_run_state(butler: ButlerStore, run: dict[str, Any], outcome: str,
                     summary: str, task_updates: dict[str, Any]) -> dict[str, Any]:
    """把「这一路」的进度 / 下一步 / 现状写到 run 行（v14）。

    2026-10-29 用户拍板：一个事务可以并行多路，各路各写自己的状态；
    事务行上的同名字段归用户手动，agent 不再覆盖它。
    """
    raw_progress = task_updates.get("progress")
    progress: int | None = None
    if isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool):
        progress = int(raw_progress)
    next_steps = task_updates.get("next_steps")
    state_note = task_updates.get("current_state") or summary
    butler.set_run_state(
        run["run_id"],
        progress=progress,
        next_steps=next_steps,
        state_note=state_note if isinstance(state_note, (str, dict)) else None,
    )
    return {"updated": True, "target": "run", "progress": progress}


def _backfill_task(todos: TodosStore, run: dict[str, Any],
                   task_updates: dict[str, Any]) -> dict[str, Any]:
    """任务级字段只允许「回填」，不允许覆盖。

    标题与判据是目标域的东西：首轮可以补全/改写（那时通常还是「待命名 · …」），
    首轮之后只有空值才补，绝不覆盖用户已经认过的标题。
    """
    scope = run.get("task_scope")
    task_id = run.get("task_id")
    if not scope or not task_id:
        return {"updated": False, "reason": "adhoc_or_no_task"}
    task = _task_of(todos, run)
    if not task:
        raise ButlerError("task_not_found", f"任务不存在: {task_id}", 404)
    first_round = int(run.get("report_count") or 0) == 0
    updates: dict[str, Any] = {}
    refused: list[str] = []
    title = str(task_updates.get("title") or "").strip()[:200]
    if title:
        if first_round or not task.get("title") or str(task.get("title")).startswith("待命名"):
            updates["title"] = title
        elif title != str(task.get("title")):
            refused.append("title")
    done_when = str(task_updates.get("done_when") or "").strip()[:500]
    if done_when:
        if first_round or not task.get("done_when"):
            updates["done_when"] = done_when
        elif done_when != str(task.get("done_when")):
            refused.append("done_when")
    if not updates:
        if refused:
            # 事务字段是用户认过的口径：执行者想改，得让用户来改
            return {"updated": False, "scope": scope,
                    "reason": "task_field_locked", "ignored": refused}
        return {"updated": False, "scope": scope, "reason": "no_changes"}
    updated = (todos.update_wip(task_id, updates) if scope == "wip"
               else todos.update_todo(task_id, updates))
    if not updated:
        raise ButlerError("task_not_found", f"任务不存在: {task_id}", 404)
    result: dict[str, Any] = {"updated": True, "scope": scope, "target": "task",
                              "fields": sorted(updates)}
    if refused:
        result["ignored"] = refused
    return result


def _run_finished_clean(butler: ButlerStore, run: dict[str, Any]) -> bool:
    """这一路是否「做完了且给了证据」。缺证据的 done 不算收尾。

    证据可能在顶层 `evidence`（小步 report），也可能在 `task_updates.evidence`
    （整包 submit 的老写法），两处都要认。
    """
    if run.get("status") != "idle" or run.get("outcome") != "done":
        return False
    payload = butler.latest_report_payload(run["run_id"])
    updates = payload.get("task_updates")
    updates = updates if isinstance(updates, dict) else {}
    evidence = payload.get("evidence") or updates.get("evidence") or updates.get("evidence_refs")
    return bool(evidence)


def _maybe_complete_task(butler: ButlerStore, todos: TodosStore,
                         run: dict[str, Any]) -> dict[str, Any]:
    """所有未关的路都收尾了 ⇒ 事务可置完成（用户拍板的第 ② 条后半句）。

    只在事务当前是 `active` 时自动置完成：用户手动设成 paused / blocked /
    completed / archived，说明他已经表过态，系统不越权改。
    事务进度不在这里动 —— 那是用户自己拖的（第 ① 条）。
    """
    scope, task_id = run.get("task_scope"), run.get("task_id")
    if not scope or not task_id:
        return {"completed": False, "reason": "adhoc_or_no_task"}
    runs = butler.list_runs(task_scope=scope, task_id=task_id, limit=200)
    open_runs = [r for r in runs if r.get("status") not in _CLOSED_RUN_STATUS]
    if not open_runs:
        return {"completed": False, "reason": "no_open_run"}
    if not all(_run_finished_clean(butler, r) for r in open_runs):
        return {"completed": False, "reason": "still_running",
                "open_runs": len(open_runs)}
    task = _task_of(todos, run)
    if not task:
        return {"completed": False, "reason": "task_not_found"}
    if task.get("status") != "active":
        return {"completed": False, "reason": f"task_status:{task.get('status')}"}
    if scope == "wip":
        todos.update_wip(task_id, {"status": "completed"})
    else:
        todos.mark_done(task_id)
    return {"completed": True, "open_runs": len(open_runs)}


def run_stage(run: dict[str, Any]) -> str:
    """面板 5 步进度条的状态：开事务 / 已开跑 / 在跑 / 待处理 / 已关闭。"""
    status = run.get("status")
    if status == "created":
        return "created"
    if status == "opened":
        return "opened"
    if status == "running":
        return "running"
    if status == "stale":
        return "stale"
    if status == "closed":
        return "closed"
    if status == "idle":
        return "done_pending_close" if run.get("outcome") == "done" else "reported"
    return str(status or "unknown")


def close_run_by_user(butler: ButlerStore, run_id: str) -> dict[str, Any]:
    """用户关闭 run（只由面板/用户触发；agent 没有 close 动作）。"""
    run = butler.get_run(run_id)
    if run["status"] == "closed":
        raise ButlerError("run_closed", "run 已关闭", 409)
    if run["status"] in ("abandoned", "failed"):
        raise ButlerError("run_closed", "run 已结束", 409)
    if run["status"] == "running":
        raise ButlerError("run_running", "本轮还在跑，先等报告或中断", 409)
    butler.close_run_by_user(run_id)
    return {"run_id": run_id, "status": "closed"}


SUMMARY_HARD_LIMIT = 500  # 提示词仍写 200，判定放宽到 500


def _step_run(butler: ButlerStore, code: str) -> dict[str, Any]:
    run = butler.get_run_by_code(code)
    if run["status"] == "created":
        raise ButlerError("session_not_open", "先取上下文再回报", 409)
    if run["status"] in ("closed", "abandoned", "failed"):
        raise ButlerError("run_closed", "run 已结束", 409)
    return run


def _parse_quotes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    quotes: list[dict[str, Any]] = []
    for q in (payload.get("quotes") or []):
        item = {"text": q} if isinstance(q, str) else dict(q)
        if not str(item.get("text") or "").strip():
            raise ButlerError("quote_text_required", "quotes[].text 不能为空")
        quotes.append(item)
    return quotes


def _apply_feedback(butler: ButlerStore, run: dict[str, Any],
                    feedback: list[dict[str, Any]]) -> int:
    count = 0
    for fb in feedback or []:
        bundle = str(fb.get("bundle") or "")
        if not bundle or not butler.record_feedback(run["run_id"], bundle, fb):
            raise ButlerError(
                "feedback_item_not_delivered",
                f"召回反馈引用了本 run 未投递的 bundle: {bundle or '(空)'}",
                422,
            )
        count += 1
    return count


def _apply_task_fields(butler: ButlerStore, todos: TodosStore, run: dict[str, Any],
                       task_updates: dict[str, Any]) -> dict[str, Any]:
    """小步 task 更新：进度/下一步/现状写**本路**（run），标题/判据回填事务。

    v14 之前这些都写 tasks 行，多路并发时后写覆盖先写；现在拆开：
    执行域字段行级隔离，目标域字段只允许回填。
    """
    scope = run.get("task_scope")
    task_id = run.get("task_id")
    run_fields = {"progress", "next_steps", "current_state"}
    task_fields = {"title", "done_when", "status"}
    touched_run = sorted(run_fields & set(task_updates))
    touched_task = sorted(task_fields & set(task_updates))
    if not touched_run and not touched_task:
        return {"updated": False, "reason": "no_changes"}

    landed: dict[str, Any] = {"updated": False, "target": "run"}
    if touched_run:
        _write_run_state(butler, run, "", "", task_updates)
        landed = {"updated": True, "target": "run", "fields": touched_run}
    if not scope or not task_id:
        return landed if touched_run else {"updated": False,
                                           "reason": "adhoc_or_no_task"}
    # status 不允许 agent 直接写：事务状态是聚合视图，只有用户或「全部收尾」能改
    if "status" in touched_task:
        touched_task.remove("status")
        landed["ignored"] = ["status"]
    if touched_task:
        task_result = _backfill_task(todos, run, task_updates)
        ignored = list(task_result.get("ignored") or [])
        if task_result.get("updated"):
            landed = {"updated": True, "target": "both",
                      "fields": touched_run + touched_task}
        elif not landed["updated"]:
            landed = task_result
        if ignored:
            landed["ignored"] = sorted(set(list(landed.get("ignored") or []) + ignored))
    elif "ignored" in landed and not landed["updated"]:
        return {"updated": False, "reason": "status_not_agent_writable"}
    return landed


_TASK_REASON_TEXT = {
    "no_changes": "你给的字段和库里一样，没有可写的变化",
    "adhoc_or_no_task": "这个 run 没挂任务，task_updates 无处可写",
    "status_not_agent_writable": (
        "事务状态不归执行者改（多路时它是聚合视图）；要改状态请在面板上点，"
        "或在 summary 里说明，由用户决定"
    ),
    "task_field_locked": (
        "事务的标题/判据已经被用户认过，执行者不能覆盖；要改请在 summary 里说明"
    ),
}


def _task_view(result: dict[str, Any]) -> dict[str, Any]:
    """小步 task 返回：是否落地；没落地就把原因说成人话。

    提示词里已经不再解释返回字段（2026-10-28 用户口径：要解释就是返回值不明确），
    所以这里必须自带一句能读懂的话。
    """
    view: dict[str, Any] = {"updated": bool(result.get("updated"))}
    if view["updated"] and result.get("target"):
        view["target"] = result["target"]
    if result.get("ignored"):
        view["ignored"] = result["ignored"]
        view["note"] = (
            f"{'、'.join(result['ignored'])} 没写：事务的标题/判据/状态归用户，"
            "执行者只能补空值；要改请在 summary 里说清，由用户改"
        )
    if not view["updated"] and result.get("reason"):
        reason = str(result["reason"])
        view["reason"] = reason
        view["note"] = _TASK_REASON_TEXT.get(reason, f"未写入，原因代码 {reason}")
    return view


def _memory_view(result: dict[str, Any]) -> dict[str, Any]:
    """小步 memory 返回：计数 + 需要当轮修的 issues（被拒 / 被降级）。"""
    cands = result.get("candidates") or {}
    view: dict[str, Any] = {
        "accepted": int(cands.get("accepted") or 0),
        "merged": int(cands.get("merged") or 0),
        "duplicate": int(cands.get("duplicate") or 0),
        "rejected": int(cands.get("rejected") or 0),
    }
    issues: list[dict[str, Any]] = []
    for row in result.get("results") or []:
        if row.get("status") == "rejected":
            issues.append({
                "index": row.get("index"),
                "reason": row.get("reason"),
                "message": row.get("message"),
            })
        elif row.get("downgraded_from_user_quote"):
            issues.append({
                "index": row.get("index"),
                "reason": "downgraded_from_user_quote",
                "message": "声明了 user_quote 但没有原话支撑，已按 agent_extracted 落库",
            })
    if issues:
        view["issues"] = issues
    return view


def report_step(butler: ButlerStore, todos: TodosStore,
                code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """最小报告：只交 outcome + summary（done 带 evidence），run 置 idle。"""
    run = _step_run(butler, code)
    outcome = payload.get("outcome")
    if outcome not in _OUTCOMES:
        raise ButlerError(
            "bad_outcome", "outcome 必须是 progress/done/partial/blocked/failed"
        )
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise ButlerError("report_missing_required", "summary 必填")
    # 提示词写 200（引导写重点），系统实际放行到 500：
    # 中文一句完整结论很容易超 200，卡死在 200 会逼 agent 反复重发（2026-10-28 用户裁定）。
    if len(summary) > SUMMARY_HARD_LIMIT:
        raise ButlerError(
            "summary_too_long",
            f"summary 最多 {SUMMARY_HARD_LIMIT} 字符（实际 {len(summary)}）",
        )

    evidence = payload.get("evidence") or []
    claimed_done = outcome == "done" and not evidence
    blocked_on_user = payload.get("blocked_on_user")
    blocked_unexplained = outcome == "blocked" and not blocked_on_user
    next_run_hint = payload.get("next_run_hint")
    artifacts = payload.get("artifacts") or []

    # 执行身份（2026-10-28 用户发现：跳过 session.open 后没人再报 harness/model，
    # run 上这两列一直是空的，面板无法区分「这轮是谁在跑」）。
    harness = str(payload.get("harness") or "").strip()[:60]
    model = str(payload.get("model") or "").strip()[:60]
    if harness or model:
        butler.set_run_identity(run["run_id"], harness=harness or None,
                                model=model or None)

    _write_run_state(butler, run, outcome, summary, {})

    if run.get("handoff_id") and outcome == "done":
        butler.set_handoff_status(run["handoff_id"], "archived")

    attempt = int(run.get("report_count") or 0) + 1
    butler.set_run_json_fields(
        run["run_id"],
        blocked_on_user=blocked_on_user or {},
        next_run_hint=next_run_hint or {},
    )
    result = {
        "status": "idle",
        "outcome": outcome,
        "attempt": attempt,
        "claimed_done": claimed_done,
        "blocked_unexplained": blocked_unexplained,
    }
    # 这两句原来写在提示词的【检查返回】里；提示词删掉那段之后，说明必须跟着返回值走。
    if claimed_done:
        result["note"] = (
            "outcome=done 但没有 evidence：本次不计完成，任务也不会置 completed。"
            "补上 evidence 后重发 report。"
        )
    elif blocked_unexplained:
        result["note"] = (
            "outcome=blocked 但没有 blocked_on_user：看不出卡在什么上。"
            "补上 blocked_on_user 后重发 report。"
        )
    butler.append_report(
        run["run_id"], attempt=attempt, outcome=outcome, summary=summary,
        payload={
            "outcome": outcome, "summary": summary, "evidence": evidence,
            "next_run_hint": next_run_hint, "artifacts": artifacts,
            "blocked_on_user": blocked_on_user,
        },
        result=result, idempotency_key=payload.get("idempotency_key"),
    )
    butler.append_event(run["run_id"], "wrapup.report", {
        "attempt": attempt, "outcome": outcome,
        "claimed_done": claimed_done, "blocked_unexplained": blocked_unexplained,
    })
    # 收尾判定必须在报告落库之后：它要看「所有未关的路是否都 idle+done+有证据」
    completion = _maybe_complete_task(butler, todos, run)
    if completion.get("completed"):
        result["task_completed"] = True
    return result


def task_step(butler: ButlerStore, todos: TodosStore,
              code: str, task_updates: dict[str, Any]) -> dict[str, Any]:
    run = _step_run(butler, code)
    result = _apply_task_fields(butler, todos, run, task_updates)
    butler.append_event(run["run_id"], "wrapup.task", {
        "updated": result.get("updated", False),
        "target": result.get("target"),
        "reason": result.get("reason"),
    })
    return _task_view(result)


def memory_step(butler: ButlerStore, memory: MemoryStore,
                code: str, payload: dict[str, Any]) -> dict[str, Any]:
    run = _step_run(butler, code)
    quotes = _parse_quotes(payload)
    result = memory_submit_wrapup(memory, {
        "run_id": run["run_id"],
        "project_id": run.get("project_id"),
        "quotes": quotes,
        "candidates": _memory_candidates(payload.get("memory_candidates") or []),
    }, default_scope="global")
    cands = result.get("candidates", {})
    butler.append_event(run["run_id"], "wrapup.memory", {
        "accepted": cands.get("accepted", 0),
        "rejected": cands.get("rejected", 0),
    })
    return _memory_view(result)


def feedback_step(butler: ButlerStore, code: str,
                  feedback: list[dict[str, Any]]) -> dict[str, Any]:
    run = _step_run(butler, code)
    count = _apply_feedback(butler, run, feedback)
    butler.append_event(run["run_id"], "wrapup.feedback", {"count": count})
    return {"count": count}


def handoff_step(butler: ButlerStore, code: str,
                 payload: dict[str, Any]) -> dict[str, Any]:
    run = _step_run(butler, code)
    blocked_on_user = payload.get("blocked_on_user")
    if blocked_on_user is not None:
        butler.set_run_json_fields(
            run["run_id"], blocked_on_user=blocked_on_user or {}
        )
    new_handoff = payload.get("handoff") or {}
    created = None
    ignored = None
    if new_handoff.get("needed"):
        reason = str(new_handoff.get("reason") or "").strip()
        if not reason:
            raise ButlerError("handoff_reason_required", "handoff.reason 必填")
        if run.get("outcome") == "done":
            ignored = "outcome_done"
        else:
            created_row = butler.create_handoff(
                {"blocker": reason,
                 "done": _clean_done_items(new_handoff.get("done")),
                 "next_steps": new_handoff.get("next_steps") or []},
                title=(new_handoff.get("title") or reason[:80] or None),
                task_scope=run.get("task_scope"),
                task_id=run.get("task_id"),
                project_id=run.get("project_id"),
                run_id=run.get("run_id"),
                created_by="agent",
            )
            created = created_row.get("handoff_id")
    butler.append_event(run["run_id"], "wrapup.handoff", {
        "created": created, "ignored": ignored,
        "blocked": bool(blocked_on_user),
    })
    result: dict[str, Any] = {}
    if created:
        result["created"] = created
    if ignored:
        result["ignored"] = ignored
    return result


def submit_session_wrapup(
    butler: ButlerStore,
    memory: MemoryStore,
    todos: TodosStore,
    code: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """每轮报告（可重复）：写报告、更新任务/记忆、run 置 idle，**不关闭 run**。"""
    run = butler.get_run_by_code(code)
    if run["status"] == "created":
        raise ButlerError("session_not_open", "先调 session.open 再报告", 409)
    if run["status"] in ("closed", "abandoned", "failed"):
        raise ButlerError("run_closed", "run 已结束", 409)

    idem = payload.get("idempotency_key")
    existing = butler.get_report_by_idem(run["run_id"], idem)
    if existing and existing.get("result_json"):
        result = json.loads(existing["result_json"])
        result["duplicate"] = True
        return result

    outcome = payload.get("outcome")
    if outcome not in _OUTCOMES:
        raise ButlerError(
            "bad_outcome", "outcome 必须是 progress/done/partial/blocked/failed"
        )
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise ButlerError("report_missing_required", "summary 必填")
    # 提示词写 200（引导写重点），系统实际放行到 500：
    # 中文一句完整结论很容易超 200，卡死在 200 会逼 agent 反复重发（2026-10-28 用户裁定）。
    if len(summary) > SUMMARY_HARD_LIMIT:
        raise ButlerError(
            "summary_too_long",
            f"summary 最多 {SUMMARY_HARD_LIMIT} 字符（实际 {len(summary)}）",
        )

    task_updates = payload.get("task_updates") or {}
    evidence = (
        task_updates.get("evidence")
        or task_updates.get("evidence_refs")
        or payload.get("evidence")
        or []
    )
    claimed_done = outcome == "done" and not evidence
    blocked_on_user = payload.get("blocked_on_user")
    next_run_hint = payload.get("next_run_hint")
    blocked_unexplained = outcome == "blocked" and not blocked_on_user

    quotes = []
    for q in (payload.get("quotes") or []):
        item = {"text": q} if isinstance(q, str) else dict(q)
        if not str(item.get("text") or "").strip():
            raise ButlerError("quote_text_required", "quotes[].text 不能为空")
        quotes.append(item)

    feedback_count = 0
    for fb in payload.get("recall_feedback") or []:
        bundle = str(fb.get("bundle") or "")
        if not bundle or not butler.record_feedback(run["run_id"], bundle, fb):
            raise ButlerError(
                "feedback_item_not_delivered",
                f"召回反馈引用了本 run 未投递的 bundle: {bundle or '(空)'}",
                422,
            )
        feedback_count += 1

    memory_result = memory_submit_wrapup(memory, {
        "run_id": run["run_id"],
        "project_id": run.get("project_id"),
        "quotes": quotes,
        "candidates": _memory_candidates(payload.get("memory_candidates") or []),
    }, default_scope="global")

    # v14：本路状态写到 run；事务字段只做回填（标题/判据）
    _write_run_state(butler, run, outcome, summary, task_updates)
    try:
        task_result = _backfill_task(todos, run, task_updates)
    except ButlerError:
        task_result = {"updated": False, "reason": "task_not_found"}

    handoff_result: dict[str, Any] = {"closed": None, "created": None}
    if run.get("handoff_id") and outcome == "done":
        butler.set_handoff_status(run["handoff_id"], "archived")
        handoff_result["closed"] = {
            "handoff_id": run["handoff_id"], "status": "archived",
        }
    new_handoff = payload.get("handoff") or {}
    if new_handoff.get("needed") and not str(new_handoff.get("reason") or "").strip():
        raise ButlerError("handoff_reason_required", "handoff.reason 必填")
    if new_handoff.get("needed") and outcome == "done":
        handoff_result["ignored"] = "outcome_done"
    elif new_handoff.get("needed"):
        created = butler.create_handoff(
            {
                "blocker": new_handoff.get("reason") or summary,
                "next_steps": new_handoff.get("next_steps") or [],
            },
            title=(new_handoff.get("title") or summary[:80] or None),
            task_scope=run.get("task_scope"),
            task_id=run.get("task_id"),
            project_id=run.get("project_id"),
            run_id=run.get("run_id"),
            created_by="agent",
        )
        handoff_result["created"] = created.get("handoff_id")

    attempt = int(run.get("report_count") or 0) + 1
    butler.set_run_json_fields(
        run["run_id"],
        blocked_on_user=blocked_on_user or {},
        next_run_hint=next_run_hint or {},
    )

    result = {
        "run_id": run["run_id"],
        "status": "idle",
        "outcome": outcome,
        "attempt": attempt,
        "report_count": attempt,
        "claimed_done": claimed_done,
        "blocked_unexplained": blocked_unexplained,
        "evidence_ok": bool(evidence) or outcome != "done",
        "memory": memory_result,
        "task": task_result,
        "handoff": handoff_result,
        "feedback": {"count": feedback_count},
        "artifacts": payload.get("artifacts") or [],
    }
    butler.append_report(
        run["run_id"],
        attempt=attempt,
        outcome=outcome,
        summary=summary,
        payload=payload,
        result=result,
        idempotency_key=idem,
    )
    butler.append_event(run["run_id"], "wrapup.submitted", {
        "attempt": attempt,
        "report_count": attempt,
        "outcome": outcome,
        "claimed_done": claimed_done,
        "blocked_unexplained": blocked_unexplained,
        "memory_accepted": memory_result.get("candidates", {}).get("accepted", 0),
        "memory_rejected": memory_result.get("candidates", {}).get("rejected", 0),
        "feedback_count": feedback_count,
        "artifacts": payload.get("artifacts") or [],
    })
    completion = _maybe_complete_task(butler, todos, run)
    if completion.get("completed"):
        result["task_completed"] = True
    return result
