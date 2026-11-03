"""Runtime 会话管理：多会话 runner、幂等提交、steer/interrupt、ask_user 路由。

本模块不依赖 FastAPI；HTTP/WS 路由在 `runtime_api.py`，进程启动在 `server.py`。
测试可以注入 `deps_factory` 用 MockLLM，不必连 8766。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from lib.agent_identity import AGENT_CALLER_HEADER, AGENT_CALLER_HEADER_VALUE
from lib.agent_runtime.ask_user import RuntimeAskUserProvider
from lib.agent_runtime.compactor import Compactor
from lib.agent_runtime.event_store import EventStore
from lib.agent_runtime.facade import APPROVAL_DECISION_PATH, SessionFacade
from lib.agent_runtime.session_query import SessionQuery
from lib.agent_runtime.types import Message, RunnerConfig, RunnerDeps

logger = logging.getLogger("parcae.agent.runtime_manager")

TERMINAL_STATUSES = frozenset({"idle", "completed", "interrupted", "failed"})

DepsFactory = Callable[[asyncio.Event, RuntimeAskUserProvider], Awaitable[RunnerDeps]]


def apply_context_window(deps: RunnerDeps, context_window: int | None) -> RunnerDeps:
    """compress 判据按客户端给的模型上下文窗口覆盖（0/缺失 = 保持 deps 默认）。"""
    if context_window and int(context_window) > 0:
        return replace(deps, compactor=Compactor(max_context_tokens=int(context_window)))
    return deps


class RuntimeApiError(Exception):
    """带 HTTP 语义的业务错误；由 runtime_api 统一转成结构化 JSON。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class RuntimeSettings:
    base_url: str = "http://127.0.0.1:8766"
    model: str = ""
    max_iterations: int = 50
    wall_clock_budget_secs: int = 0
    ask_user_timeout_secs: float = 0.0
    stream_idle_timeout_secs: int = 90
    long_tool_first_check_secs: int = 180


@dataclass
class ActiveRun:
    session_id: str
    provider: RuntimeAskUserProvider
    interrupt_event: asyncio.Event
    text: str
    mode: str
    model_override: str
    template_skills: list[str] | None
    client_message_id: str
    started_at: float = field(default_factory=time.time)
    facade: SessionFacade | None = None
    task: asyncio.Task | None = None


class RuntimeManager:
    def __init__(
        self,
        *,
        store: EventStore,
        settings: RuntimeSettings,
        deps_factory: DepsFactory,
    ):
        self._store = store
        self._settings = settings
        self._deps_factory = deps_factory
        self._query = SessionQuery(store)
        self._active: dict[str, ActiveRun] = {}
        self._cmid_index: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._ws_clients = 0
        self._shutting_down = False
        self.recovery: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 提交与运行
    # ------------------------------------------------------------------

    async def submit(
        self,
        session_id: str,
        text: str,
        *,
        mode: str = "dialogue",
        model_override: str = "",
        template_skills: list[str] | None = None,
        client_message_id: str = "",
        max_iterations: int | None = None,
        wall_clock_budget_secs: int | None = None,
        use_stream: bool | None = None,
        context_window: int | None = None,
    ) -> dict[str, Any]:
        if not session_id:
            raise RuntimeApiError(400, "invalid_argument", "session_id is required")
        if not text or not text.strip():
            raise RuntimeApiError(400, "invalid_argument", "text must be non-empty")
        if self._shutting_down:
            raise RuntimeApiError(503, "runtime_stopping", "runtime is shutting down")

        async with self._lock:
            if client_message_id:
                cached = await self._store.get_outcome_for_client_message_id(client_message_id)
                if cached is not None:
                    return {
                        "ok": True,
                        "session_id": cached["session_id"],
                        "status": cached["outcome_status"],
                        "deduplicated": True,
                        "outcome": {
                            "status": cached["outcome_status"],
                            "stop_reason": cached["outcome_stop_reason"],
                        },
                    }
                inflight_session = self._cmid_index.get(client_message_id)
                if inflight_session is not None:
                    return {
                        "ok": True,
                        "session_id": inflight_session,
                        "status": "running",
                        "deduplicated": True,
                    }
            current = self._active.get(session_id)
            if current is not None and (current.task is None or not current.task.done()):
                raise RuntimeApiError(
                    409, "session_busy",
                    f"session {session_id} already has a running turn",
                    {"session_id": session_id},
                )
            provider = RuntimeAskUserProvider(
                session_id=session_id,
                store=self._store,
                timeout_secs=self._settings.ask_user_timeout_secs,
                has_clients=lambda: self._ws_clients > 0,
            )
            interrupt_event = asyncio.Event()
            run = ActiveRun(
                session_id=session_id,
                provider=provider,
                interrupt_event=interrupt_event,
                text=text,
                mode=mode,
                model_override=model_override,
                template_skills=template_skills,
                client_message_id=client_message_id,
            )
            # 先占位，避免构造 deps（要拉 8766 工具目录）期间并发提交穿透
            self._active[session_id] = run
            if client_message_id:
                self._cmid_index[client_message_id] = session_id

        try:
            deps = await self._deps_factory(interrupt_event, provider)
        except Exception as exc:
            async with self._lock:
                self._active.pop(session_id, None)
                if client_message_id:
                    self._cmid_index.pop(client_message_id, None)
            logger.exception("runtime deps build failed (session=%s)", session_id)
            raise RuntimeApiError(
                502, "upstream_error", f"cannot prepare runner deps: {exc}"
            ) from exc
        if context_window and int(context_window) > 0:
            # 面板知道所选 model 的上下文窗口；deps 工厂不感知模型，这里覆盖压缩判据
            deps = apply_context_window(deps, context_window)

        config = self._runner_config(session_id)
        overrides: dict[str, Any] = {}
        if model_override:
            overrides["model"] = model_override
        if template_skills:
            overrides["template_skills"] = tuple(template_skills)
        if max_iterations is not None:
            overrides["max_iterations"] = max_iterations
        if wall_clock_budget_secs is not None:
            overrides["wall_clock_budget_secs"] = wall_clock_budget_secs
        if use_stream is not None:
            overrides["use_stream"] = use_stream
        if overrides:
            config = replace(config, **overrides)
        run.facade = SessionFacade(deps=deps, config=config)
        run.task = asyncio.create_task(self._run(run), name=f"runtime-{session_id}")
        return {"ok": True, "session_id": session_id, "status": "running", "deduplicated": False}

    def _runner_config(self, session_id: str) -> RunnerConfig:
        return RunnerConfig(
            session_id=session_id,
            model=self._settings.model,
            max_iterations=self._settings.max_iterations,
            wall_clock_budget_secs=self._settings.wall_clock_budget_secs,
            long_tool_first_check_secs=self._settings.long_tool_first_check_secs,
            stream_idle_timeout_secs=self._settings.stream_idle_timeout_secs,
        )

    async def _run(self, run: ActiveRun) -> None:
        if run.facade is None:
            raise RuntimeError("facade missing before run")
        try:
            outcome = await run.facade.start(
                run.session_id,
                run.text,
                mode=run.mode,
                model_override=run.model_override,
                template_skills=run.template_skills,
                client_message_id=run.client_message_id,
            )
            logger.info(
                "turn finished: session=%s status=%s stop=%s iterations=%d",
                run.session_id, outcome.status, outcome.stop_reason, outcome.iterations,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("turn crashed (session=%s)", run.session_id)
            try:
                await self._store.append_event(
                    run.session_id, "runtime_error",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
                await self._store.update_session_status(run.session_id, "failed")
            except Exception:
                logger.exception("failed to persist runtime_error (session=%s)", run.session_id)
        finally:
            run.provider.cancel_pending()
            async with self._lock:
                if self._active.get(run.session_id) is run:
                    self._active.pop(run.session_id, None)
                if run.client_message_id and self._cmid_index.get(run.client_message_id) == run.session_id:
                    self._cmid_index.pop(run.client_message_id, None)

    # ------------------------------------------------------------------
    # steer / interrupt / approve / ask_user
    # ------------------------------------------------------------------

    def _running(self, session_id: str) -> ActiveRun | None:
        run = self._active.get(session_id)
        if run is None or run.task is None or run.task.done():
            return None
        return run

    async def steer(self, session_id: str, text: str) -> None:
        if not text or not text.strip():
            raise RuntimeApiError(400, "invalid_argument", "text must be non-empty")
        run = self._running(session_id)
        if run is None or run.facade is None:
            raise RuntimeApiError(
                409, "session_not_running",
                f"session {session_id} has no running turn",
                {"session_id": session_id},
            )
        await run.facade.steer(session_id, text)

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        run = self._running(session_id)
        if run is None or run.facade is None:
            return {"ok": True, "session_id": session_id, "interrupted": False}
        run.provider.cancel_pending()
        await run.facade.interrupt(session_id)
        return {"ok": True, "session_id": session_id, "interrupted": True}

    async def approve(self, approval_id: str, decision: str, feedback: str = "") -> dict[str, Any]:
        if not approval_id:
            raise RuntimeApiError(400, "invalid_argument", "approval_id is required")
        url = f"{self._settings.base_url.rstrip('/')}{APPROVAL_DECISION_PATH}"
        payload = {"approval_id": approval_id, "decision": decision, "feedback": feedback}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url, json=payload, headers={AGENT_CALLER_HEADER: AGENT_CALLER_HEADER_VALUE}
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeApiError(
                502, "upstream_error", f"approval proxy failed: {exc}",
                {"approval_id": approval_id},
            ) from exc

    def answer_ask_user(self, rid: str, outcome: str, answers: list[dict] | None) -> bool:
        for run in list(self._active.values()):
            if run.provider.resolve(rid, outcome, answers):
                return True
        return False

    # ------------------------------------------------------------------
    # WS 客户端计数
    # ------------------------------------------------------------------

    def client_connected(self) -> None:
        self._ws_clients += 1

    def client_disconnected(self) -> None:
        self._ws_clients = max(0, self._ws_clients - 1)
        if self._ws_clients == 0:
            for run in list(self._active.values()):
                run.provider.cancel_pending("no_host")

    # ------------------------------------------------------------------
    # 读路径
    # ------------------------------------------------------------------

    async def list_sessions(self) -> list[dict[str, Any]]:
        rows = await self._query.list_sessions()
        return [_asdict(r) for r in rows]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        row = await self._query.get(session_id)
        if row is None:
            raise RuntimeApiError(404, "not_found", f"session not found: {session_id}")
        return _asdict(row)

    async def stats(self, session_id: str) -> dict[str, Any]:
        return _asdict(await self._query.stats(session_id))

    async def outline(self, session_id: str) -> list[dict[str, Any]]:
        return [_asdict(t) for t in await self._query.turn_outline(session_id)]

    async def events(self, session_id: str, after_seq: int = 0, include_live: bool = False) -> list[dict[str, Any]]:
        events = await self._store.load_events(
            session_id, include_live=include_live, after_seq=after_seq
        )
        return [_event_dict(e) for e in events]

    async def messages(
        self, session_id: str, after_seq: int = 0, include_invisible: bool = False,
    ) -> list[dict[str, Any]]:
        rows = await self._store.load_messages(session_id, include_invisible=include_invisible)
        return [_message_dict(m) for m in rows if m.seq > after_seq]

    async def tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        return [_asdict(tc) for tc in await self._store.load_tool_calls(session_id)]

    async def enqueue_message(self, session_id: str, content: Any) -> dict[str, Any]:
        """排队消息落账（客户端唯一允许的 messages 写入）。"""
        if await self._store.get_session(session_id) is None:
            raise RuntimeApiError(404, "not_found", f"session not found: {session_id}")
        message = Message(role="user", content=content, source="queue", visible=False)
        appended = await self._store.append_message(session_id, message)
        return _message_dict(appended)

    async def pending_queue(self) -> list[dict[str, Any]]:
        rows = await self._store.find_pending_queue_messages()
        return [_message_dict(m) for m in rows]

    def recovery_report(self) -> dict[str, Any]:
        return dict(self.recovery)

    async def tool_call_summary(self, session_id: str) -> dict[str, Any]:
        if await self._store.get_session(session_id) is None:
            raise RuntimeApiError(404, "not_found", f"session not found: {session_id}")
        return await self._store.tool_call_outcome_summary(session_id)

    @property
    def tool_base_url(self) -> str:
        """runtime 自己该用的 8766 工具地址。"""
        return self._settings.base_url

    # ------------------------------------------------------------------
    # 写路径（会话管理）
    # ------------------------------------------------------------------

    async def patch_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        group_name: Any = ...,
    ) -> dict[str, Any]:
        if await self._store.get_session(session_id) is None:
            raise RuntimeApiError(404, "not_found", f"session not found: {session_id}")
        if title is not None:
            await self._store.update_session_title(session_id, title)
        if pinned is not None:
            await self._store.update_session_pinned(session_id, pinned)
        if group_name is not ...:
            await self._store.update_session_group(session_id, group_name)
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> bool:
        if self._running(session_id) is not None:
            raise RuntimeApiError(
                409, "session_busy",
                f"session {session_id} is running; interrupt it first",
                {"session_id": session_id},
            )
        deleted = await self._store.delete_session(session_id)
        if not deleted:
            raise RuntimeApiError(404, "not_found", f"session not found: {session_id}")
        return True

    async def delete_message(self, message_id: str) -> bool:
        deleted = await self._store.delete_message_by_id(message_id)
        if not deleted:
            raise RuntimeApiError(404, "not_found", f"message not found: {message_id}")
        return True

    async def branch(self, session_id: str, upto_seq: int, title: str | None = None) -> str:
        try:
            return await self._store.branch_session(session_id, upto_seq, title)
        except ValueError as exc:
            raise RuntimeApiError(404, "not_found", str(exc)) from exc

    async def rebuild_projections(self, session_id: str | None = None) -> dict[str, int]:
        return await self._query.rebuild(session_id)

    # ------------------------------------------------------------------
    # 关停
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        self._shutting_down = True
        runs = [r for r in self._active.values() if r.task is not None and not r.task.done()]
        for run in runs:
            run.provider.cancel_pending()
            if run.facade is None:
                continue
            try:
                await run.facade.interrupt(run.session_id)
            except Exception:
                logger.exception("interrupt during shutdown failed (session=%s)", run.session_id)
        tasks = [run.task for run in runs if run.task is not None]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5)


def _asdict(obj: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(obj)


def _event_dict(event: Any) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "session_id": event.session_id,
        "type": event.type,
        "payload": event.payload,
        "domain": event.domain,
        "trace_id": event.trace_id,
        "created_at": event.created_at,
        "invalidated_seq": event.invalidated_seq,
        "parent_trace_id": event.parent_trace_id,
        "depth": event.depth,
    }


def _message_dict(message: Any) -> dict[str, Any]:
    return {
        "id": message.id,
        "seq": message.seq,
        "session_id": message.session_id,
        "role": message.role,
        "content": message.content,
        "source": message.source,
        "visible": message.visible,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
        "thinking": message.thinking,
        "model": message.model,
        "created_at": message.created_at,
    }
