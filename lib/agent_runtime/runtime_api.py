"""Runtime HTTP / WS API（spec §7）。

错误统一 `{error:{code,message,detail}}`；稳定 code：`session_busy`、
`session_not_running`、`format_too_new`（启动期）、`projection_missing`、`not_found`。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from lib.agent_runtime.runtime_manager import RuntimeApiError, RuntimeManager

logger = logging.getLogger("parcae.agent.runtime_api")


class SubmitBody(BaseModel):
    text: str
    mode: str = "dialogue"
    model_override: str = ""
    template_skills: list[str] | None = None
    client_message_id: str = ""
    max_iterations: int | None = None
    wall_clock_budget_secs: int | None = None
    use_stream: bool | None = None
    context_window: int | None = None


class ProbeBody(BaseModel):
    trigger_text: str
    mode: str = "script"
    script: list[Any] = []
    pattern: str = ""
    use_stream: bool = False
    trigger_approval: bool = False
    max_context_messages: int = 0
    auto_cleanup: bool = True
    db_path: str = ""
    base_url: str = ""
    persist: bool = False
    include_full: bool = False


class SteerBody(BaseModel):
    text: str


class ApprovalBody(BaseModel):
    decision: str
    feedback: str = ""


class AskUserBody(BaseModel):
    outcome: str
    answers: list[dict] | None = None


class PatchSessionBody(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    group_name: str | None = None


class BranchBody(BaseModel):
    upto_seq: int
    title: str | None = None


class QueueBody(BaseModel):
    content: str


class RebuildBody(BaseModel):
    session_id: str | None = None


def error_payload(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def create_runtime_router(
    manager: RuntimeManager,
    *,
    port: int,
    host_allowed: Any,
    allow_any_host: bool = False,
) -> APIRouter:
    router = APIRouter()

    @router.post("/sessions/{session_id}/submit")
    async def submit(session_id: str, body: SubmitBody) -> dict[str, Any]:
        return await manager.submit(
            session_id,
            body.text,
            mode=body.mode,
            model_override=body.model_override,
            template_skills=body.template_skills,
            client_message_id=body.client_message_id,
            max_iterations=body.max_iterations,
            wall_clock_budget_secs=body.wall_clock_budget_secs,
            use_stream=body.use_stream,
            context_window=body.context_window,
        )

    @router.post("/sessions/{session_id}/steer")
    async def steer(session_id: str, body: SteerBody) -> dict[str, Any]:
        await manager.steer(session_id, body.text)
        return {"ok": True, "session_id": session_id}

    @router.post("/sessions/{session_id}/interrupt")
    async def interrupt(session_id: str) -> dict[str, Any]:
        return await manager.interrupt(session_id)

    @router.post("/approvals/{approval_id}")
    async def approve(approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        result = await manager.approve(approval_id, body.decision, body.feedback)
        return {"ok": True, **(result if isinstance(result, dict) else {"result": result})}

    @router.post("/ask_user/{rid}")
    async def answer_ask_user(rid: str, body: AskUserBody) -> dict[str, Any]:
        if not manager.answer_ask_user(rid, body.outcome, body.answers):
            raise RuntimeApiError(
                404, "not_found", f"no pending ask_user request: {rid}", {"rid": rid}
            )
        return {"ok": True, "rid": rid, "resolved": True}

    @router.get("/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"ok": True, "sessions": await manager.list_sessions()}

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return {"ok": True, "session": await manager.get_session(session_id)}

    @router.patch("/sessions/{session_id}")
    async def patch_session(session_id: str, body: PatchSessionBody) -> dict[str, Any]:
        fields = body.model_fields_set
        group: Any = ... if "group_name" not in fields else body.group_name
        session = await manager.patch_session(
            session_id,
            title=body.title if "title" in fields else None,
            pinned=body.pinned if "pinned" in fields else None,
            group_name=group,
        )
        return {"ok": True, "session": session}

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        await manager.delete_session(session_id)
        return {"ok": True, "deleted": True}

    @router.delete("/messages/{message_id}")
    async def delete_message(message_id: str) -> dict[str, Any]:
        await manager.delete_message(message_id)
        return {"ok": True, "deleted": True}

    @router.post("/sessions/{session_id}/branch")
    async def branch(session_id: str, body: BranchBody) -> dict[str, Any]:
        new_id = await manager.branch(session_id, body.upto_seq, body.title)
        return {"ok": True, "session_id": new_id}

    @router.get("/sessions/{session_id}/events")
    async def events(
        session_id: str,
        after_seq: int = Query(0, ge=0),
        include_live: bool = False,
    ) -> dict[str, Any]:
        rows = await manager.events(session_id, after_seq=after_seq, include_live=include_live)
        return {"ok": True, "events": rows, "next_seq": rows[-1]["seq"] if rows else after_seq}

    @router.get("/sessions/{session_id}/messages")
    async def messages(
        session_id: str,
        after_seq: int = Query(0, ge=0),
        include_invisible: bool = Query(False),
    ) -> dict[str, Any]:
        rows = await manager.messages(
            session_id, after_seq=after_seq, include_invisible=include_invisible,
        )
        return {"ok": True, "messages": rows, "next_seq": rows[-1]["seq"] if rows else after_seq}

    @router.get("/sessions/{session_id}/tool_calls")
    async def tool_calls(session_id: str) -> dict[str, Any]:
        return {"ok": True, "tool_calls": await manager.tool_calls(session_id)}

    @router.post("/sessions/{session_id}/queue")
    async def enqueue_message(session_id: str, body: QueueBody) -> dict[str, Any]:
        return {"ok": True, "message": await manager.enqueue_message(session_id, body.content)}

    @router.get("/queue/pending")
    async def pending_queue() -> dict[str, Any]:
        return {"ok": True, "messages": await manager.pending_queue()}

    @router.get("/sessions/{session_id}/stats")
    async def stats(session_id: str) -> dict[str, Any]:
        return {"ok": True, "stats": await manager.stats(session_id)}

    @router.get("/sessions/{session_id}/tool_calls/summary")
    async def tool_call_summary(session_id: str) -> dict[str, Any]:
        return {"ok": True, "summary": await manager.tool_call_summary(session_id)}

    @router.post("/probe/run")
    async def probe_run(body: ProbeBody) -> dict[str, Any]:
        from dataclasses import asdict

        from lib.agent_runtime.probe_runner import ProbeParams, run_probe

        params = ProbeParams(
            trigger_text=body.trigger_text,
            mode=body.mode,
            script=body.script,
            pattern=body.pattern,
            use_stream=body.use_stream,
            trigger_approval=body.trigger_approval,
            max_context_messages=body.max_context_messages,
            auto_cleanup=body.auto_cleanup,
            db_path=body.db_path,
            base_url=body.base_url or manager.tool_base_url,
            persist=body.persist,
            include_full=body.include_full,
        )
        try:
            summary = await run_probe(params)
        except ValueError as exc:
            raise RuntimeApiError(400, "invalid_argument", str(exc)) from exc
        return {"ok": True, "summary": asdict(summary)}

    @router.get("/sessions/{session_id}/outline")
    async def outline(session_id: str) -> dict[str, Any]:
        return {"ok": True, "turns": await manager.outline(session_id)}

    @router.get("/admin/recovery")
    async def recovery() -> dict[str, Any]:
        return {"ok": True, **manager.recovery_report()}

    @router.post("/admin/projections/rebuild")
    async def rebuild(body: RebuildBody) -> dict[str, Any]:
        return {"ok": True, **(await manager.rebuild_projections(body.session_id))}

    # ------------------------------------------------------------------
    # WebSocket：下行事件 + 上行操作
    # ------------------------------------------------------------------

    @router.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        session_id = websocket.query_params.get("session_id") or ""
        after_seq = _int_param(websocket.query_params.get("after_seq"), 0)
        if not session_id:
            await websocket.close(code=1008)
            return
        if not allow_any_host:
            scope_server = websocket.scope.get("server") or (None, None)
            actual_port = int(scope_server[1] or port)
            if not host_allowed(websocket.headers.get("host", ""), actual_port):
                await websocket.close(code=1008)
                return
        await websocket.accept()
        manager.client_connected()
        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        sender = asyncio.create_task(
            _ws_sender(websocket, manager, session_id, after_seq, outbox),
            name=f"ws-send-{session_id}",
        )
        try:
            while True:
                try:
                    raw = await websocket.receive_json()
                except ValueError:
                    outbox.put_nowait(
                        {"kind": "error", "op": "?", "error": error_payload("invalid_argument", "frame must be JSON")["error"]}
                    )
                    continue
                op = str(raw.get("op") or "")
                try:
                    data = await _ws_dispatch(manager, session_id, op, raw)
                except RuntimeApiError as exc:
                    outbox.put_nowait(
                        {"kind": "error", "op": op, "error": error_payload(exc.code, exc.message, exc.detail)["error"]}
                    )
                else:
                    outbox.put_nowait({"kind": "ack", "op": op, "data": data})
        except WebSocketDisconnect:
            pass
        finally:
            manager.client_disconnected()
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    return router


async def _ws_dispatch(
    manager: RuntimeManager, session_id: str, op: str, raw: dict[str, Any]
) -> dict[str, Any]:
    if op == "submit":
        return await manager.submit(
            session_id,
            str(raw.get("text") or ""),
            mode=str(raw.get("mode") or "dialogue"),
            model_override=str(raw.get("model_override") or ""),
            template_skills=raw.get("template_skills"),
            client_message_id=str(raw.get("client_message_id") or ""),
            max_iterations=raw.get("max_iterations"),
            wall_clock_budget_secs=raw.get("wall_clock_budget_secs"),
            use_stream=raw.get("use_stream"),
            context_window=raw.get("context_window"),
        )
    if op == "steer":
        await manager.steer(session_id, str(raw.get("text") or ""))
        return {"ok": True}
    if op == "interrupt":
        return await manager.interrupt(session_id)
    if op == "approve":
        result = await manager.approve(
            str(raw.get("approval_id") or ""),
            str(raw.get("decision") or ""),
            str(raw.get("feedback") or ""),
        )
        return {"ok": True, "result": result}
    if op == "ask_user":
        rid = str(raw.get("rid") or "")
        answers = raw.get("answers")
        if not manager.answer_ask_user(rid, str(raw.get("outcome") or ""), answers):
            raise RuntimeApiError(404, "not_found", f"no pending ask_user request: {rid}", {"rid": rid})
        return {"ok": True, "rid": rid}
    if op == "ping":
        return {"ok": True, "pong": True}
    raise RuntimeApiError(400, "invalid_argument", f"unknown op: {op!r}", {"op": op})


async def _ws_sender(
    websocket: WebSocket,
    manager: RuntimeManager,
    session_id: str,
    after_seq: int,
    outbox: asyncio.Queue[dict[str, Any]],
) -> None:
    last_seq = after_seq
    try:
        await websocket.send_json(
            {"kind": "ready", "session_id": session_id, "after_seq": last_seq}
        )
        while True:
            while True:
                try:
                    msg = outbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await websocket.send_json(msg)
            try:
                rows = await manager.events(session_id, after_seq=last_seq, include_live=True)
            except Exception as exc:  # 事件读失败不能杀掉整个连接，回报后继续重试
                logger.exception("ws replay failed (session=%s)", session_id)
                await websocket.send_json(
                    {"kind": "error", "op": "replay", "error": error_payload("replay_failed", str(exc))["error"]}
                )
                rows = []
            for row in rows:
                last_seq = int(row["seq"])
                await websocket.send_json({"kind": "event", "event": row})
            try:
                msg = await asyncio.wait_for(outbox.get(), timeout=0.1)
            except TimeoutError:
                continue
            await websocket.send_json(msg)
    except (WebSocketDisconnect, RuntimeError, OSError):
        return


def _int_param(raw: str | None, default: int) -> int:
    try:
        return max(0, int(raw or default))
    except (TypeError, ValueError):
        return default
