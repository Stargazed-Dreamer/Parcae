"""butler runtime 的 HTTP 客户端（后端 headless/judge/probe 与后续桌面端共用）。

只依赖 runtime 声明文件 + HTTP，不 import server/ 或 client/。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from lib.runtime_endpoint import RUNTIME_DIR, declared_base_url

logger = logging.getLogger("parcae.agent.runtime_client")

RUNTIME_DECLARATION = RUNTIME_DIR / "butler_runtime.json"
DEFAULT_WAIT_TIMEOUT = 3600.0
TERMINAL_STATUSES = frozenset({"completed", "interrupted", "failed"})
STARTED_STATUSES = frozenset({"streaming", "awaiting_tools", "running"}) | TERMINAL_STATUSES


class RuntimeUnavailable(RuntimeError):
    """runtime 没在跑 / 连接失败。"""


class RuntimeCallError(RuntimeError):
    """runtime 返回了结构化错误。"""

    def __init__(self, status_code: int, code: str, message: str, detail: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class RuntimeOutcome:
    """headless/judge 需要的终态摘要（RunOutcome 的最小投影）。"""
    session_id: str
    status: str
    stop_reason: str = ""
    iterations: int = 0
    total_tokens: int = 0


def runtime_base_url() -> str:
    """解析 runtime 地址：环境变量优先，其次独立声明文件；没有就明确报错。"""
    explicit = os.environ.get("PARCAE_RUNTIME_API", "").strip().rstrip("/")
    if explicit:
        return explicit if "://" in explicit else "http://" + explicit
    base = declared_base_url(declaration_path=RUNTIME_DECLARATION)
    if base:
        return base
    raise RuntimeUnavailable(
        "butler runtime is not running: no healthy declaration at "
        f"{RUNTIME_DECLARATION}. Start it with start_butler_runtime.bat."
    )


class RuntimeClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 30.0):
        self._base_url = (base_url or runtime_base_url()).rstrip("/")
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(method, f"{self._base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise RuntimeUnavailable(
                f"cannot reach butler runtime at {self._base_url}: {exc}"
            ) from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeCallError(
                resp.status_code, "bad_response", resp.text[:500]
            ) from exc
        if resp.status_code >= 400:
            error = (body or {}).get("error") or {}
            raise RuntimeCallError(
                resp.status_code,
                str(error.get("code") or "http_error"),
                str(error.get("message") or resp.text[:300]),
                error.get("detail") or {},
            )
        return body

    async def submit(
        self,
        session_id: str,
        text: str,
        *,
        mode: str = "headless",
        model_override: str = "",
        template_skills: list[str] | None = None,
        client_message_id: str = "",
        max_iterations: int | None = None,
        wall_clock_budget_secs: int | None = None,
        use_stream: bool | None = None,
        context_window: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": text,
            "mode": mode,
            "model_override": model_override,
            "client_message_id": client_message_id,
        }
        if template_skills:
            payload["template_skills"] = template_skills
        if max_iterations is not None:
            payload["max_iterations"] = max_iterations
        if wall_clock_budget_secs is not None:
            payload["wall_clock_budget_secs"] = wall_clock_budget_secs
        if use_stream is not None:
            payload["use_stream"] = use_stream
        if context_window is not None:
            payload["context_window"] = context_window
        return await self._request("POST", f"/sessions/{session_id}/submit", json=payload)

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/sessions/{session_id}/interrupt")

    async def steer(self, session_id: str, text: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/sessions/{session_id}/steer", json={"text": text}
        )

    async def answer_ask_user(
        self, rid: str, outcome: str, answers: list[dict] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST", f"/ask_user/{rid}", json={"outcome": outcome, "answers": answers}
        )

    async def get_session(self, session_id: str) -> dict[str, Any]:
        body = await self._request("GET", f"/sessions/{session_id}")
        return body["session"]

    async def list_sessions(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "/sessions")
        return body["sessions"]

    async def patch_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        body = await self._request("PATCH", f"/sessions/{session_id}", json=fields)
        return body["session"]

    async def delete_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/sessions/{session_id}")

    async def delete_message(self, message_id: str) -> None:
        await self._request("DELETE", f"/messages/{message_id}")

    async def branch(
        self, session_id: str, upto_seq: int, title: str | None = None,
    ) -> str:
        body = await self._request(
            "POST",
            f"/sessions/{session_id}/branch",
            json={"upto_seq": int(upto_seq), "title": title},
        )
        return str(body.get("session_id") or "")

    def ws_url(self, session_id: str, after_seq: int = 0) -> str:
        from urllib.parse import quote

        base = self._base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/ws?session_id={quote(session_id)}&after_seq={int(after_seq)}"

    async def stats(self, session_id: str) -> dict[str, Any]:
        body = await self._request("GET", f"/sessions/{session_id}/stats")
        return body["stats"]

    async def events(
        self, session_id: str, *, after_seq: int = 0, include_live: bool = False
    ) -> list[dict[str, Any]]:
        body = await self._request(
            "GET",
            f"/sessions/{session_id}/events",
            params={"after_seq": after_seq, "include_live": str(include_live).lower()},
        )
        return body["events"]

    async def messages(
        self, session_id: str, *, after_seq: int = 0, include_invisible: bool = False,
    ) -> list[dict[str, Any]]:
        body = await self._request(
            "GET",
            f"/sessions/{session_id}/messages",
            params={
                "after_seq": after_seq,
                "include_invisible": str(include_invisible).lower(),
            },
        )
        return body["messages"]

    async def tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        body = await self._request("GET", f"/sessions/{session_id}/tool_calls")
        return body["tool_calls"]

    async def enqueue(self, session_id: str, content: str) -> dict[str, Any]:
        body = await self._request(
            "POST", f"/sessions/{session_id}/queue", json={"content": content}
        )
        return body["message"]

    async def pending_queue(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "/queue/pending")
        return body["messages"]

    async def recovery(self) -> dict[str, Any]:
        return await self._request("GET", "/admin/recovery")

    async def tool_call_summary(self, session_id: str) -> dict[str, Any]:
        body = await self._request("GET", f"/sessions/{session_id}/tool_calls/summary")
        return body["summary"]

    async def probe(self, params: dict[str, Any]) -> dict[str, Any]:
        body = await self._request("POST", "/probe/run", json=params)
        return body["summary"]

    async def wait_terminal(
        self, session_id: str, *, timeout: float = DEFAULT_WAIT_TIMEOUT
    ) -> dict[str, Any]:
        """等到会话进入终态；返回最后一次 session 摘要。

        先等它真的启动（streaming 等），避免把创建前的 idle 误当终态。
        """
        deadline = time.monotonic() + timeout
        started = False
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                session = await self.get_session(session_id)
            except RuntimeCallError as exc:
                if exc.status_code != 404:
                    raise
                session = {}
            except RuntimeUnavailable:
                raise
            if session:
                last = session
                status = str(session.get("status") or "")
                if status in STARTED_STATUSES:
                    started = True
                if started and status in TERMINAL_STATUSES:
                    return session
            await asyncio.sleep(0.25)
        raise TimeoutError(
            f"runtime session {session_id} did not reach a terminal state in {timeout:.0f}s "
            f"(last status={last.get('status')!r})"
        )

    async def outcome(self, session_id: str, *, timeout: float = DEFAULT_WAIT_TIMEOUT) -> RuntimeOutcome:
        session = await self.wait_terminal(session_id, timeout=timeout)
        status = str(session.get("status") or "")
        events = await self.events(session_id)
        stop_reason = _stop_reason_from_events(status, events)
        try:
            stats = await self.stats(session_id)
            tokens = int(stats.get("total_tokens") or 0)
        except RuntimeCallError:
            tokens = 0
        return RuntimeOutcome(
            session_id=session_id,
            status=status,
            stop_reason=stop_reason,
            iterations=0,
            total_tokens=tokens,
        )


def _stop_reason_from_events(status: str, events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        etype = event.get("type")
        if etype == "max_iterations_reached":
            return "max_iterations"
        if etype == "session_finalized":
            return "end_turn"
        if etype == "transition":
            reason = str((event.get("payload") or {}).get("reason") or "")
            if reason:
                return reason
    return "error" if status == "failed" else ""
