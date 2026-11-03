"""headless 测试替身：记录 submit、按脚本返回终态（不连 HTTP）。

T6 起 headless/judge 通过 RuntimeClient 与 runtime 交互。编排层测试用本替身
验证"提交了什么、拿到终态后怎么处理"；真实 HTTP 接缝由
tests/agent_runtime/test_t6_headless_via_runtime.py 用真 runtime 覆盖。

"测试替身不许自动补货"：本替身只返回测试显式设定的 messages/status，
不替被测代码补任何默认业务数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lib.agent_runtime.runtime_client import RuntimeOutcome


def assistant(content: str, **extra: Any) -> dict:
    return {"role": "assistant", "content": content, "source": "assistant", **extra}


def user(content: str, **extra: Any) -> dict:
    return {"role": "user", "content": content, "source": "user", **extra}


@dataclass
class FakeSession:
    status: str = "completed"
    stop_reason: str = "end_turn"
    messages: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    tool_summary: dict = field(default_factory=dict)
    total_tokens: int = 0


class FakeRuntimeClient:
    """RuntimeClient 的按脚本替身。"""

    def __init__(self) -> None:
        self.submits: list[dict] = []
        self.interrupts: list[str] = []
        self.sessions: dict[str, FakeSession] = {}
        self.by_mode: dict[str, FakeSession] = {}
        self.default = FakeSession()
        self.submit_error: Exception | None = None
        self.submit_error_by_mode: dict[str, Exception] = {}
        self.interrupt_error: Exception | None = None
        self.interrupt_result = True
        self.probe_result: dict = {}
        self.probe_calls: list[dict] = []

    def on(self, mode: str, **fields: Any) -> FakeSession:
        session = FakeSession(**fields)
        self.by_mode[mode] = session
        return session

    async def submit(self, session_id: str, text: str, **kwargs: Any) -> dict:
        record = {"session_id": session_id, "text": text, **kwargs}
        self.submits.append(record)
        mode_error = self.submit_error_by_mode.get(kwargs.get("mode", "dialogue"))
        if mode_error is not None:
            raise mode_error
        if self.submit_error is not None:
            raise self.submit_error
        spec = self.by_mode.get(kwargs.get("mode", "dialogue"), self.default)
        self.sessions[session_id] = FakeSession(
            status=spec.status,
            stop_reason=spec.stop_reason,
            messages=list(spec.messages),
            events=list(spec.events),
            tool_summary=dict(spec.tool_summary),
            total_tokens=spec.total_tokens,
        )
        return {"ok": True, "session_id": session_id, "status": "running", "deduplicated": False}

    async def wait_terminal(self, session_id: str, *, timeout: float | None = None) -> dict:
        session = self.sessions[session_id]
        return {"session_id": session_id, "status": session.status}

    async def outcome(self, session_id: str, *, timeout: float | None = None) -> RuntimeOutcome:
        session = self.sessions[session_id]
        return RuntimeOutcome(
            session_id=session_id,
            status=session.status,
            stop_reason=session.stop_reason,
            total_tokens=session.total_tokens,
        )

    async def messages(self, session_id: str, *, after_seq: int = 0) -> list[dict]:
        return list(self.sessions[session_id].messages)

    async def tool_call_summary(self, session_id: str) -> dict:
        return dict(self.sessions[session_id].tool_summary)

    async def events(self, session_id: str, *, after_seq: int = 0, include_live: bool = False) -> list[dict]:
        return list(self.sessions[session_id].events)

    async def stats(self, session_id: str) -> dict:
        return {"total_tokens": self.sessions[session_id].total_tokens}

    async def get_session(self, session_id: str) -> dict:
        return {"session_id": session_id, "status": self.sessions[session_id].status}

    async def interrupt(self, session_id: str) -> dict:
        self.interrupts.append(session_id)
        if self.interrupt_error is not None:
            raise self.interrupt_error
        return {"ok": True, "session_id": session_id, "interrupted": self.interrupt_result}

    async def probe(self, params: dict) -> dict:
        self.probe_calls.append(dict(params))
        return dict(self.probe_result)
