"""DL4 接口面：6 个动作（session.open / note、context.next、wrapup.submit、memory.write）。

7 个入口名保持一致（`butler_session` 等），`action` 是第一个字段；本期
其余动作不实现，遇到直接 501。

2026-10-28 起删除 `session.inbox`：「看用户有没有留言」改为复用既有的
用户补充指令通道（`server/user_message.py`），agent 调任意后端接口时自动附带。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Body, HTTPException, Query

from server.butler.bundles import build_manifest, build_sources
from server.butler.models import (
    ContextAction,
    MemoryWriteAction,
    RunArchiveRequest,
    RunCreateRequest,
    RunHandoffPromptSaveRequest,
    RunPromptAppendRequest,
    RunPromptUpdateRequest,
    RunStateUpdateRequest,
    SessionAction,
    WrapupAction,
)
from server.butler.prompts import (
    render_handoff,
    render_pb,
    render_pb_continue,
    render_takeover,
)
from server.butler.service import (
    close_run_by_user,
    feedback_step,
    handoff_step,
    memory_step,
    next_context,
    open_session,
    report_step,
    run_stage,
    session_note,
    submit_session_wrapup,
    task_step,
    write_memory,
)
from server.butler.store import ButlerError, ButlerStore
from server.memory.write import MemoryWriteError

router = APIRouter(prefix="/butler", tags=["butler"])
logger = logging.getLogger(__name__)

# run 到这几种状态就再也不能用了：会话码对 agent 已失效
_CLOSED_RUN_STATUS = {"closed", "abandoned", "failed"}


def get_butler_store() -> ButlerStore:
    from server.memory.manager import get_memory_manager

    return ButlerStore(get_memory_manager().store)


def get_todos_store():
    from server.todos.router import get_todos_store as _get
    return _get()


def _http_error(err: ButlerError | MemoryWriteError) -> HTTPException:
    return HTTPException(
        status_code=err.http_status,
        detail={"code": err.code, "message": err.message},
    )


def _agent_open_view(result: dict, code: str) -> dict:
    """只返回 agent 需要的字段；run_id/code_hash/prompt_text/manifest_json 留在面板侧。"""
    run = result["run"]
    manifest = result["manifest"]
    return {
        "ok": True,
        "action": "open",
        "code": code,
        "status": run["status"],
        "task": manifest["task"],
        "pages": manifest["pages"],
        "instructions": manifest["instructions"],
    }


def _strip_page(page: dict) -> dict:
    page.pop("delivery_id", None)
    return page


def _agent_wrapup_view(result: dict) -> dict:
    result.pop("run_id", None)
    memory = result.get("memory")
    if isinstance(memory, dict):
        memory.pop("run_id", None)
    return result


def _prompt_text(code: str, task_title: str = "", done_when: str = "",
                 user_request: str = "",
                 pages: list[dict] | None = None, mode: str = "initial",
                 terse: bool = False, with_context: bool = True) -> str:
    text = render_pb(
        code=code, task_title=task_title, done_when=done_when,
        user_request=user_request, pages=pages,
        terse=terse, with_context=with_context,
    )["prompt"]
    if mode == "handoff":
        # 接手轮：换了一个执行者，上下文要从头给；只加一句说明这是接手，不改正文。
        text = "【接手】上一任已交接，以下为重新接手所需的完整上下文。\n" + text
    return text


def _task_meta(run: dict) -> tuple[str, str]:
    """取任务当前的标题与完成判据。

    agent 可能在 `task_updates` 里补过 title / done_when，所以下一轮提示词
    必须重读任务，而不是沿用建 run 时的入参。
    """
    task = _task_row(run)
    if not task:
        return "", ""
    return str(task.get("title") or ""), str(task.get("done_when") or "")


def _run_next_steps(run: dict) -> list[str]:
    """run.next_steps_json 是 JSON 数组字符串；坏数据当空，不让面板/提示词崩。"""
    try:
        value = json.loads(run.get("next_steps_json") or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(x) for x in value][:10]


def _task_row(run: dict) -> dict:
    """读当前任务本体；任务被删或库抖动时返回空 dict，不让报告失败。"""
    todos = get_todos_store()
    scope, tid = run.get("task_scope"), run.get("task_id")
    try:
        if scope == "wip" and tid:
            return todos.get_wip(tid) or {}
        if scope == "todos" and tid:
            return todos.get_todo(tid) or {}
    except Exception:  # noqa: BLE001
        return {}
    return {}


async def _append_next_prompt(run: dict) -> dict | None:
    """重渲染下一轮派单提示词，追加成一个新版本并设为当前。

    2026-10-28 用户拍板：一轮报告落库后，系统自动生成下一波提示词，
    面板看到的就不再是「新开一轮的初始提示词」。失败不阻断报告。

    2026-10-29 起不再「遇用户手改就跳过」：版本是只增的，追加新版不会销毁
    用户改过的那一版，它在历史里还能回滚。所以自动更新恢复为无条件的。
    """
    butler = get_butler_store()
    try:
        sources = await asyncio.to_thread(
            build_sources, run, _memory(), get_todos_store(), butler,
        )
        title, done = _task_meta(run)
        code = run.get("code_prefix") or ""  # 4 位会话码，前缀即全码
        if run.get("handoff_id"):
            # 接手轮：换了人，需要完整上下文（用户 2026-10-28 裁定 a）
            manifest = build_manifest(run, sources)
            prompt_text = _prompt_text(
                code, task_title=title, done_when=done,
                user_request=run.get("prompt") or "",
                pages=manifest["pages"], mode="handoff",
            )
        else:
            # 续跑轮：同一个执行者、上下文还在，只给「接着做什么」（裁定 b）
            # v14：进度/下一步从 run 自己身上读（多路时各是各的，不能读事务行）
            prompt_text = render_pb_continue(
                code=code,
                report_count=int(run.get("report_count") or 0),
                task_title=title,
                done_when=done,
                progress=run.get("progress"),
                next_steps=_run_next_steps(run),
                last_outcome=str(run.get("outcome") or ""),
                last_summary=str(run.get("summary") or ""),
            )["prompt"]
        return await asyncio.to_thread(
            butler.append_prompt_version, run["run_id"], prompt_text,
            source="auto_next",
        )
    except Exception as err:  # 提示词生成失败不影响本轮报告
        logger.warning("自动生成下一轮提示词失败 run=%s: %s", run.get("run_id"), err)
        return None


@router.post("/runs", operation_id="butler_run_create",
             summary="[UI/临时] 建任务 run + 会话码")
async def butler_run_create(req: RunCreateRequest = Body(...)):  # noqa: B008
    """建一路新的执行。

    多路并行的入口就是这里：同一个 task_scope/task_id 连续调多次即多路，
    `label` 说明这一路做什么（面板的「再加一路」）。
    """
    butler = get_butler_store()
    try:
        run = await asyncio.to_thread(
            butler.create_run,
            req.prompt,
            task_scope=req.task_scope,
            task_id=req.task_id,
            project_id=req.project_id,
            harness=req.harness,
            owner=req.owner,
            handoff_id=req.handoff_id,
            domain_id=req.domain_id,
            label=req.label,
        )
    except ButlerError as err:
        raise _http_error(err) from err
    run_full = await asyncio.to_thread(butler.get_run, run["run_id"])
    sources = await asyncio.to_thread(
        build_sources, run_full, _memory(), get_todos_store(), butler
    )
    manifest = build_manifest(run_full, sources)
    await asyncio.to_thread(butler.set_manifest, run["run_id"], manifest)
    prompt_text = _prompt_text(
        run["session_code"], task_title=req.task_title or "",
        done_when=req.done_when or "", user_request=req.prompt,
        pages=manifest["pages"],
    )
    await asyncio.to_thread(
        butler.append_prompt_version, run["run_id"], prompt_text,
        source="initial", note="建 run 时生成的初始提示词",
    )
    return {
        **run,
        "prompt_text": prompt_text,
    }


@router.post("/session", operation_id="butler_session",
             summary="会话协议：open / note")
async def butler_session(req: SessionAction = Body(...)):  # noqa: B008
    if req.action == "open":
        try:
            result = await asyncio.to_thread(
                open_session, get_butler_store(), _memory(), get_todos_store(),
                req.code, owner=req.owner, harness=req.harness, model=req.model,
            )
        except ButlerError as err:
            raise _http_error(err) from err
        return _attach_supplement(_agent_open_view(result, req.code), req.code)
    if req.action == "note":
        try:
            result = await asyncio.to_thread(
                session_note, get_butler_store(), req.code, req.text or "",
                kind=req.kind, client_note_id=req.client_note_id,
            )
        except ButlerError as err:
            raise _http_error(err) from err
        return _attach_supplement(
            {"ok": True, "action": "note", **result}, req.code,
        )
    raise HTTPException(501, detail={"code": "not_implemented", "message": req.action})


@router.post("/context", operation_id="butler_context",
             summary="会话协议：context.next（支持多 cursor）")
async def butler_context(req: ContextAction = Body(...)):  # noqa: B008
    if req.action != "next":
        raise HTTPException(501, detail={"code": "not_implemented", "message": req.action})
    try:
        result = await asyncio.to_thread(
            next_context, get_butler_store(), _memory(), get_todos_store(),
            req.code, req.cursor, req.cursors,
        )
    except ButlerError as err:
        raise _http_error(err) from err
    if "pages" in result:
        result["pages"] = [_strip_page(p) for p in result["pages"]]
    else:
        _strip_page(result)
    return _attach_supplement({"ok": True, "action": "next", **result}, req.code)


@router.post("/wrapup", operation_id="butler_wrapup",
             summary="会话协议：wrapup 小步回报（report/task/memory/feedback/handoff）")
async def butler_wrapup(req: WrapupAction = Body(...)):  # noqa: B008
    payload = {
        "outcome": req.outcome,
        "summary": req.summary,
        "task_updates": req.task_updates,
        "memory_candidates": req.memory_candidates,
        "quotes": req.quotes,
        "recall_feedback": req.recall_feedback,
        "handoff": req.handoff,
        "artifacts": req.artifacts,
        "evidence": req.evidence,
        "blocked_on_user": req.blocked_on_user,
        "next_run_hint": req.next_run_hint,
        "idempotency_key": req.idempotency_key,
        "harness": req.harness,
        "model": req.model,
    }
    try:
        # 身份可能在任意一步带上来（2026-10-28 起放到「开工前」那条 task 里），
        # 所以不做「只认 report」的限制：任何一步带了就写，只写有值的列。
        if req.harness or req.model:
            _run = await asyncio.to_thread(
                get_butler_store().get_run_by_code, req.code
            )
            await asyncio.to_thread(
                get_butler_store().set_run_identity, _run["run_id"],
                harness=(req.harness or "").strip()[:60] or None,
                model=(req.model or "").strip()[:60] or None,
            )
        if req.action == "submit":
            result = await asyncio.to_thread(
                submit_session_wrapup, get_butler_store(), _memory(),
                get_todos_store(), req.code, payload,
            )
        elif req.action == "report":
            result = await asyncio.to_thread(
                report_step, get_butler_store(), get_todos_store(),
                req.code, payload,
            )
            # 一轮报告落库后自动生成「下一轮」提示词（用户 2026-10-28 拍板）：
            # 任务被 agent 回填过，旧提示词已经过期，同一个码下一轮要拿到新的。
            run = await asyncio.to_thread(get_butler_store().get_run_by_code, req.code)
            saved = await _append_next_prompt(run)
            if saved:
                result = {
                    **result,
                    "next_prompt_version": saved["version"],
                    "next_prompt_chars": saved["chars"],
                }
        elif req.action == "task":
            result = await asyncio.to_thread(
                task_step, get_butler_store(), get_todos_store(),
                req.code, req.task_updates,
            )
        elif req.action == "memory":
            result = await asyncio.to_thread(
                memory_step, get_butler_store(), _memory(), req.code, payload,
            )
        elif req.action == "feedback":
            result = await asyncio.to_thread(
                feedback_step, get_butler_store(), req.code,
                req.recall_feedback,
            )
        elif req.action == "handoff":
            result = await asyncio.to_thread(
                handoff_step, get_butler_store(), req.code, payload,
            )
        else:
            raise HTTPException(
                501, detail={"code": "not_implemented", "message": req.action}
            )
    except ButlerError as err:
        raise _http_error(err) from err
    return _attach_supplement(
        {"ok": True, "action": req.action, **_agent_wrapup_view(result)}, req.code,
    )


@router.post("/memory", operation_id="butler_memory",
             summary="记忆层：write")
async def butler_memory(req: MemoryWriteAction = Body(...)):  # noqa: B008
    if req.action != "write":
        raise HTTPException(501, detail={"code": "not_implemented", "message": req.action})
    payload = req.model_dump(exclude={"action"})
    try:
        result = await asyncio.to_thread(write_memory, _memory(), payload)
    except MemoryWriteError as err:
        raise _http_error(err) from err
    return {"ok": True, "action": "write", **result}


@router.get("/runs", operation_id="butler_runs_list",
            summary="[UI] run 列表（按状态/任务过滤；不返回会话码 hash）")
async def butler_runs_list(
    status: str | None = Query(None, description="逗号分隔，多值 OR"),
    task_scope: str | None = Query(None),
    task_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    archived: bool | None = Query(
        None, description="不传=全给；false=只要面板上的；true=只要归档的"),
):
    statuses = [s.strip() for s in (status or "").split(",") if s.strip()] or None
    butler = get_butler_store()
    items = await asyncio.to_thread(
        butler.list_runs,
        statuses=statuses, task_scope=task_scope, task_id=task_id, limit=limit,
        archived=archived,
    )
    items = [{**r, "stage": run_stage(r)} for r in items]
    return {"items": items, "count": len(items)}


@router.get("/runs/{run_id}", operation_id="butler_run_get",
            summary="[UI/调试] run 状态 + 事件 + 报告历史 + 计数")
async def butler_run_get(run_id: str):
    butler = get_butler_store()
    try:
        run = await asyncio.to_thread(butler.get_run, run_id)
        events = await asyncio.to_thread(butler.list_events, run_id)
        reports = await asyncio.to_thread(butler.list_reports, run_id)
        counters = await asyncio.to_thread(butler.run_counters, run_id)
    except ButlerError as err:
        raise _http_error(err) from err
    return {
        "run": {**run, "stage": run_stage(run)},
        "events": events,
        "reports": reports,
        "counters": counters,
    }


@router.get("/runs/{run_id}/reports", operation_id="butler_run_reports",
            summary="[UI] 每轮报告历史")
async def butler_run_reports(run_id: str):
    butler = get_butler_store()
    try:
        await asyncio.to_thread(butler.get_run, run_id)
        items = await asyncio.to_thread(butler.list_reports, run_id)
    except ButlerError as err:
        raise _http_error(err) from err
    return {"items": items, "count": len(items)}


@router.post("/runs/{run_id}/close", operation_id="butler_run_close",
             summary="[UI] 用户关闭 run（报告不会自动关闭）")
async def butler_run_close(run_id: str):
    try:
        result = await asyncio.to_thread(
            close_run_by_user, get_butler_store(), run_id
        )
    except ButlerError as err:
        raise _http_error(err) from err
    return {"ok": True, **result}


@router.patch("/runs/{run_id}/state", operation_id="butler_run_state_update",
              summary="[UI] 改本路的状态显示（标签/进度/下一步/现状）")
async def butler_run_state_update(run_id: str, req: RunStateUpdateRequest):
    """面板纠偏用：执行者写错了或不写了，用户可以直接改这一路的显示。

    只动 `butler_runs` 行，不碰事务行；多路时改哪一路只影响哪一路。
    """
    butler = get_butler_store()
    try:
        await asyncio.to_thread(butler.get_run, run_id)
        await asyncio.to_thread(
            butler.set_run_state, run_id,
            label=req.label,
            progress=req.progress,
            next_steps=req.next_steps,
            state_note=req.state_note,
        )
        run = await asyncio.to_thread(butler.get_run, run_id)
    except ButlerError as err:
        raise _http_error(err) from err
    return {"ok": True, "run": {**run, "stage": run_stage(run)}}


@router.put("/runs/{run_id}/prompt", operation_id="butler_run_prompt_update",
            summary="[UI] 改写派单提示词（改当前生效版的正文）")
async def butler_run_prompt_update(run_id: str, req: RunPromptUpdateRequest = Body(...)):  # noqa: B008
    """只替换提示词正文：不重渲染模板、不动 run 状态、不换会话码。

    2026-10-29 用户裁定：派单提示词有历史、可回滚。所以这里改的是**当前生效那一版**
    的正文（不新增版本）；要留旧内容就先去版本列表复制。
    """
    butler = get_butler_store()
    try:
        run = await asyncio.to_thread(butler.get_run, run_id)
        saved = await asyncio.to_thread(
            butler.set_run_prompt_text, run_id, req.prompt_text,
            source="user_edit",
        )
    except ButlerError as err:
        raise _http_error(err) from err
    return {
        "ok": True,
        "run_id": run_id,
        "chars": saved["chars"],
        "prompt_version": saved.get("version"),
        "code_prefix": run.get("code_prefix"),
    }


@router.get("/runs/{run_id}/prompts", operation_id="butler_run_prompts",
            summary="[UI] 派单提示词版本清单")
async def butler_run_prompts(run_id: str):
    butler = get_butler_store()
    try:
        return await asyncio.to_thread(butler.list_prompt_versions, run_id)
    except ButlerError as err:
        raise _http_error(err) from err


@router.post("/runs/{run_id}/prompts", operation_id="butler_run_prompt_append",
             summary="[UI] 存为新版：当前正文另存一个新版本号")
async def butler_run_prompt_append(
    run_id: str, req: RunPromptAppendRequest = Body(...)  # noqa: B008
):
    """把正文**追加**成一个新版本（版本号 +1），历史版原样保留。

    与 ``PUT /runs/{id}/prompts/{version}`` 的区别在语义，不在实现：
    那个改的是「某一版正文」，这个攒的是「又想试一种写法」。上限 10 版
    （``PROMPT_VERSION_LIMIT``），超出时删最旧、当前生效版永不删。
    """
    butler = get_butler_store()
    try:
        saved = await asyncio.to_thread(
            butler.append_prompt_version, run_id, req.prompt_text,
            source="user_edit", note=req.note, activate=req.activate,
        )
    except ButlerError as err:
        raise _http_error(err) from err
    return {"ok": True, "run_id": run_id, **saved,
            "code_prefix": _run_code(butler, run_id)}


@router.get("/runs/{run_id}/prompts/{version}", operation_id="butler_run_prompt_version",
            summary="[UI] 取某一版提示词正文")
async def butler_run_prompt_version(run_id: str, version: int):
    butler = get_butler_store()
    try:
        return await asyncio.to_thread(butler.get_prompt_version, run_id, version)
    except ButlerError as err:
        raise _http_error(err) from err


@router.put("/runs/{run_id}/prompts/{version}", operation_id="butler_run_prompt_version_update",
            summary="[UI] 改写某一版正文（历史版本也可改）")
async def butler_run_prompt_version_update(
    run_id: str, version: int, req: RunPromptUpdateRequest = Body(...)  # noqa: B008
):
    butler = get_butler_store()
    try:
        return await asyncio.to_thread(
            butler.update_prompt_version, run_id, version, req.prompt_text
        )
    except ButlerError as err:
        raise _http_error(err) from err


@router.post("/runs/{run_id}/prompts/{version}/activate",
             operation_id="butler_run_prompt_activate",
             summary="[UI] 回滚/切换：把某一版设为当前生效版")
async def butler_run_prompt_activate(run_id: str, version: int):
    butler = get_butler_store()
    try:
        return await asyncio.to_thread(butler.activate_prompt_version, run_id, version)
    except ButlerError as err:
        raise _http_error(err) from err


@router.delete("/runs/{run_id}/prompts/{version}",
               operation_id="butler_run_prompt_version_delete",
               summary="[UI] 删除某一版（最后一版不可删）")
async def butler_run_prompt_version_delete(run_id: str, version: int):
    butler = get_butler_store()
    try:
        return await asyncio.to_thread(butler.delete_prompt_version, run_id, version)
    except ButlerError as err:
        raise _http_error(err) from err


@router.get("/runs/{run_id}/prompt", operation_id="butler_run_prompt_variant",
            summary="[UI] 按开关实时渲染派单提示词（只读预览，不落库）")
async def butler_run_prompt_variant(
    run_id: str,
    terse: bool = Query(False, description="减少行为说明与字段说明"),
    with_context: bool = Query(True, description="是否要求开工时读入上下文"),
):
    """2026-10-28 用户裁定：两个独立开关，不设「档位」。

    - ``terse``：减少行为说明与字段说明（默认 False = 讲全）。
    - ``with_context``：是否要求执行者开工时读入上一轮上下文。

    只返回渲染结果，**不写库**——用户看过再决定是否保存/复制，
    否则每切一次开关就把当前提示词覆盖一次。
    """
    butler = get_butler_store()
    try:
        run = await asyncio.to_thread(butler.get_run, run_id)
    except ButlerError as err:
        raise _http_error(err) from err
    sources = await asyncio.to_thread(
        build_sources, run, _memory(), get_todos_store(), butler
    )
    manifest = build_manifest(run, sources)
    title, done = _task_meta(run)
    text = _prompt_text(
        run.get("code_prefix") or "", task_title=title, done_when=done,
        user_request=run.get("prompt") or "", pages=manifest["pages"],
        terse=terse, with_context=with_context,
    )
    return {
        "ok": True, "run_id": run_id, "prompt_text": text, "chars": len(text),
        "terse": bool(terse), "with_context": bool(with_context),
    }


@router.get("/runs/{run_id}/handoff-prompt", operation_id="butler_run_handoff_prompt",
            summary="[UI] 这一路的交接：未交接给交接指令，已交接给接手说明")
async def butler_run_handoff_prompt(run_id: str):
    """交接挂 run（2026-10-29 用户拍板）：多路并行时每一路各有各的交接。

    四态：
    - ``handoff``    还没交接 ⇒ 给「交接指令」，写的是**这一路**的码；
    - ``takeover``   已有交接单 ⇒ 给「接手说明」；若还没开新一轮则 ``need_run``；
    - ``no_open_run``这一路已关闭 ⇒ 码已失效，交接无从下手；
    - ``no_run``     只出现在任务维度（该事务还没有任何 run）。
    """
    butler = get_butler_store()
    todos = get_todos_store()
    try:
        run = await asyncio.to_thread(butler.get_run, run_id)
    except ButlerError as err:
        raise _http_error(err) from err
    return await _handoff_view(butler, todos, run)


@router.put("/runs/{run_id}/handoff-prompt", operation_id="butler_run_handoff_prompt_save",
            summary="[UI] 交接提示词手改后保存（空串 = 清除手改）")
async def butler_run_handoff_prompt_save(
    run_id: str, req: RunHandoffPromptSaveRequest = Body(...)  # noqa: B008
):
    """存住用户改过的那一份交接正文。

    平时交接正文是实时渲染的（进度/下一步一变就跟着变）。用户在面板上改完
    按「保存」，就把这一份固定下来——回给面板的是**保存后的视图**，让界面
    立刻能看出「已手改」，不用再拉一次。

    只在「还没交接」这一态生效：一旦开出交接单，正文来自交接单本身，
    override 自然不参与（换人之后要看的是接手说明，不是上一任的稿）。
    """
    butler = get_butler_store()
    todos = get_todos_store()
    try:
        await asyncio.to_thread(butler.set_handoff_override, run_id, req.prompt_text)
        run = await asyncio.to_thread(butler.get_run, run_id)
    except ButlerError as err:
        raise _http_error(err) from err
    view = await _handoff_view(butler, todos, run)
    view["saved"] = True
    return view


def _norm_scope(scope: str) -> str:
    """面板传 wip / wip_tasks / todos / todo 四种写法，统一成库里的两种。"""
    norm = {"wip": "wip", "wip_tasks": "wip",
            "todos": "todos", "todo": "todos"}.get(scope)
    if norm is None:
        raise HTTPException(
            400,
            detail={"code": "bad_scope",
                    "message": 'scope 只接受 wip / wip_tasks / todos；'
                               f'实际 scope="{scope}"'},
        )
    return norm


def _task_by_scope(todos, scope: str, task_id: str) -> dict:
    """按 scope 读任务本体；不存在返回空 dict（端点据此 404）。"""
    if scope == "wip":
        return todos.get_wip(task_id) or {}
    return todos.get_todo(task_id) or {}


async def _handoff_view(butler: ButlerStore, todos, run: dict) -> dict:
    """一路的交接视图（四态合一）。

    2026-10-29 用户拍板：**交接挂 run**。多路并行时每一路各有各的交接单，
    面板选哪一路就显示哪一路的交接，不再按事务取「最近一份」。

    - ``handoff``     这一路还没交接 ⇒ 交接指令（写这一路的码）
    - ``takeover``    已有交接单 ⇒ 接手说明；没开新一轮时 ``need_run=true``
    - ``no_open_run`` 这一路已关闭且没有待接的交接 ⇒ 码失效，无从下手
    - ``no_run``      只由任务维度端点返回（该事务还没有任何 run）
    """
    scope, tid = run.get("task_scope"), run.get("task_id")
    task: dict = {}
    if scope and tid:
        task = await asyncio.to_thread(_task_by_scope, todos, scope, tid)
    title = str(task.get("title") or "")
    done = str(task.get("done_when") or "")
    handoff = await asyncio.to_thread(
        butler.get_active_handoff, task_scope=scope, task_id=tid,
        run_id=run.get("run_id"),
    )
    if handoff:
        runs = await asyncio.to_thread(
            butler.list_runs, task_scope=scope, task_id=tid, limit=50
        )
        # 这份交接已经开出的那一轮（换人之后）；它自己的码才是接手说明该写的码。
        # 还没开 ⇒ need_run，面板显示「开新一轮」。
        takeover_run = next(
            (r for r in runs
             if r.get("handoff_id") == handoff.get("handoff_id")
             and r.get("status") not in _CLOSED_RUN_STATUS),
            None,
        )
        payload = handoff.get("payload") or {}
        result = render_takeover(
            code=str((takeover_run or {}).get("code_prefix") or ""),
            task_title=title, done_when=done,
            progress=run.get("progress"),
            reason=str(payload.get("blocker") or payload.get("reason") or ""),
            done_items=payload.get("done") or [],
            next_steps=payload.get("next_steps") or [],
            last_outcome=str(run.get("outcome") or ""),
            last_summary=str(run.get("summary") or ""),
            created_at=str(handoff.get("created_at") or ""),
        )
        return {
            "ok": True, "state": "takeover",
            "handoff_id": handoff.get("handoff_id"),
            "handoff_status": handoff.get("status"),
            # run_id = 用户当前选中的这一路；takeover_run_id = 接下来该用的那一路
            "run_id": run.get("run_id"),
            "takeover_run_id": (takeover_run or {}).get("run_id"),
            "need_run": takeover_run is None,
            # 码单独给一份：面板标签要显示「已交接 · AB12」，而正文里的码是渲染进去的，
            # 不单独返回就得从提示词里再抠一次。开接手轮之后重新读这一路也要能显示码。
            "session_code": str((takeover_run or {}).get("code_prefix") or ""),
            "prompt_text": result.pop("prompt"), **result,
        }
    if run.get("status") in _CLOSED_RUN_STATUS:
        return {
            "ok": True, "state": "no_open_run", "run_id": run.get("run_id"),
            "prompt_text": "", "chars": 0, "variant": "handoff",
        }
    result = render_handoff(
        code=run.get("code_prefix") or "",
        task_title=title, done_when=done,
        progress=run.get("progress"),
        next_steps=_run_next_steps(run),
        last_outcome=str(run.get("outcome") or ""),
        last_summary=str(run.get("summary") or ""),
    )
    rendered = result.pop("prompt")
    # v15：用户手改并存住的那一份优先。它只在「还没交接」这一态有意义——
    # 已经开出交接单的走上面的 takeover 分支，正文来自交接单本身。
    override = str(run.get("handoff_text_override") or "").strip()
    text = override or rendered
    return {
        "ok": True, "state": "handoff", "run_id": run.get("run_id"),
        **result,
        "prompt_text": text, "chars": len(text),
        "edited": bool(override),
    }


async def _open_takeover_run(butler: ButlerStore, todos, run: dict) -> dict:
    """用「这一路」的交接单开新一轮，并把接手说明写成它的派单提示词。

    交接 = 换人，所以原路在这里收尾（outcome=handoff）。不收尾的话
    「全部未关的路都 done」永远不成立，事务就永远自动完成不了。
    重复点复用同一个接手轮，不重复开。
    """
    scope, tid = run.get("task_scope"), run.get("task_id")
    handoff = await asyncio.to_thread(
        butler.get_active_handoff, task_scope=scope, task_id=tid,
        run_id=run.get("run_id"),
    )
    if not handoff:
        raise HTTPException(
            409,
            detail={"code": "no_active_handoff",
                    "message": "这一路没有进行中的交接单，先让执行者写一份"},
        )
    handoff_id = str(handoff.get("handoff_id") or "")
    runs = await asyncio.to_thread(
        butler.list_runs, task_scope=scope, task_id=tid, limit=50
    )
    existing = next(
        (r for r in runs
         if r.get("handoff_id") == handoff_id
         and r.get("status") not in _CLOSED_RUN_STATUS),
        None,
    )
    task = await asyncio.to_thread(_task_by_scope, todos, scope, tid) if (scope and tid) else {}
    if existing:
        new_run_id = str(existing.get("run_id") or "")
        reused = True
    else:
        label = f"接手 · {run.get('label') or run.get('code_prefix') or ''}"[:40]
        created = await butler_run_create(RunCreateRequest(
            prompt=str(task.get("done_when") or task.get("title") or "接手未完成任务"),
            task_scope=scope,
            task_id=tid,
            task_title=task.get("title") or None,
            done_when=task.get("done_when") or None,
            handoff_id=handoff_id,
            label=label,
        ))
        new_run_id = str(created.get("run_id") or "")
        reused = False
        # 接手轮的派单提示词就是接手说明：让新执行者拿到的是同一份东西
        view = await _handoff_view(butler, todos, await asyncio.to_thread(
            butler.get_run, new_run_id))
        await asyncio.to_thread(
            butler.append_prompt_version, new_run_id, view["prompt_text"],
            source="handoff", note="接手轮：由上一路的交接单生成",
        )
        await asyncio.to_thread(
            butler.close_run_for_handoff, str(run["run_id"]),
            to_code=str(created.get("session_code") or ""),
        )

    view = await _handoff_view(butler, todos, await asyncio.to_thread(
        butler.get_run, new_run_id))
    view["reused"] = reused
    view["session_code"] = view.get("session_code") or _run_code(butler, new_run_id)
    return view


def _run_code(butler: ButlerStore, run_id: str) -> str:
    try:
        return str(butler.get_run(run_id).get("code_prefix") or "")
    except ButlerError:
        return ""


@router.get("/tasks/{scope}/{task_id}/handoff-prompt",
            operation_id="butler_task_handoff_prompt",
            summary="[UI] 任务维度交接（兼容入口）：取该事务最后一路的交接视图")
async def butler_task_handoff_prompt(scope: str, task_id: str):
    """任务维度只是「挑一路再问」的薄封装，真正的语义在 run 上。

    面板用的是 run 级端点；这个保留给脚本/旧客户端，语义定为
    「该事务最后一个还没关的 run，若全关了则最后一个 run」。
    """
    scope_norm = _norm_scope(scope)
    butler = get_butler_store()
    todos = get_todos_store()
    task = await asyncio.to_thread(_task_by_scope, todos, scope_norm, task_id)
    if not task:
        raise HTTPException(
            404, detail={"code": "task_not_found", "message": "任务不存在"}
        )
    runs = await asyncio.to_thread(
        butler.list_runs, task_scope=scope_norm, task_id=task_id, limit=50
    )
    if not runs:
        return {
            "ok": True, "state": "no_run",
            "prompt_text": "", "chars": 0, "variant": "handoff",
        }
    run = next((r for r in runs if r.get("status") not in _CLOSED_RUN_STATUS), runs[0])
    return await _handoff_view(butler, todos, run)


@router.post("/tasks/{scope}/{task_id}/handoff-run",
             operation_id="butler_task_handoff_run",
             summary="[UI] 任务维度开接手轮（兼容入口）：转给最后一路")
async def butler_task_handoff_run(scope: str, task_id: str):
    scope_norm = _norm_scope(scope)
    butler = get_butler_store()
    todos = get_todos_store()
    task = await asyncio.to_thread(_task_by_scope, todos, scope_norm, task_id)
    if not task:
        raise HTTPException(404, detail={"code": "task_not_found", "message": "任务不存在"})
    runs = await asyncio.to_thread(
        butler.list_runs, task_scope=scope_norm, task_id=task_id, limit=50
    )
    run = next((r for r in runs if r.get("status") not in _CLOSED_RUN_STATUS), None)
    if run is None:
        raise HTTPException(
            409,
            detail={"code": "no_active_handoff",
                    "message": "该事务没有进行中的 run，也就没有可接的交接"},
        )
    return await _open_takeover_run(butler, todos, run)


@router.post("/runs/{run_id}/handoff-run",
             operation_id="butler_run_handoff_run",
             summary="[UI] 用这一路的交接开接手轮，返回带会话码的接手说明")
async def butler_run_handoff_run(run_id: str):
    butler = get_butler_store()
    todos = get_todos_store()
    try:
        run = await asyncio.to_thread(butler.get_run, run_id)
    except ButlerError as err:
        raise _http_error(err) from err
    return await _open_takeover_run(butler, todos, run)


@router.patch("/runs/{run_id}/archive", operation_id="butler_run_archive",
              summary="[UI] 把这一路收进归档视图")
async def butler_run_archive(run_id: str,
                             req: RunArchiveRequest | None = Body(None)):  # noqa: B008
    """归档 = 从事务面板收走，不动 status、不关会话。

    归档后这一路仍然能读、能看报告，只是 `GET /runs` 默认不带它；
    面板的归档视图用 `?archived=true` 取。想继续用就 restore。
    """
    butler = get_butler_store()
    reason = (req.reason if req else None)
    try:
        run = await asyncio.to_thread(butler.archive_run, run_id, reason=reason)
    except ButlerError as err:
        raise _http_error(err) from err
    return {"ok": True, "run_id": run_id,
            "archived_at": run.get("archived_at"),
            "archive_reason": run.get("archive_reason"),
            "already": bool(run.get("already"))}


@router.post("/runs/{run_id}/restore", operation_id="butler_run_restore",
             summary="[UI] 从归档视图恢复这一路，回到事务面板")
async def butler_run_restore(run_id: str):
    butler = get_butler_store()
    try:
        run = await asyncio.to_thread(butler.restore_run, run_id)
    except ButlerError as err:
        raise _http_error(err) from err
    return {"ok": True, "run_id": run_id,
            "archived_at": run.get("archived_at"),
            "already": bool(run.get("already"))}


def _attach_supplement(view: dict, code: str) -> dict:
    """把「属于这一路」的用户留言挂到响应上。

    2026-10-29：多路并行时，用户给 A 路的留言不能被 B 路取走。
    定向消息由这里按 run_id 取；广播消息仍走全局中间件（谁先调谁拿走）。
    只在真有留言时才多读一次 run_id，正常请求不增加查询。
    """
    from server import user_message as um

    if not um.has_pending():
        return view
    try:
        run_id = get_butler_store().get_run_by_code(code)["run_id"]
    except Exception:  # 码错/库抖：留言留着，别因为挂不上就吞掉
        return view
    text = um.get_supplement_for_run(run_id)
    if text:
        view["_user_supplement"] = text
    return view


def _memory():
    from server.memory.manager import get_memory_manager

    return get_memory_manager().store
