"""ask_user 跨进程协议与 runtime 实现（spec §6）。

`AskUserProvider` 协议 + runtime 的 asyncio 实现；桌面侧由面板消费
`ask_user_request` 事件渲染卡片，再用 `POST /ask_user/{rid}` 回答案：

- 有客户端连接 → 写 `ask_user_request` 事件并等待 `POST /ask_user/{rid}` 或 WS 回帧
- 无客户端 → 立即 `no_host`（维持 fail-closed）
- 会话被中断 → `cancelled` 唤醒所有等待

超时只从配置 `[runner] ask_user_timeout_secs` 读（0 = 永不超时），工具入参里没有。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from lib.agent_runtime.ask_user_outcomes import (
    OUTCOME_ANSWERED,
    OUTCOME_CANCELLED,
    OUTCOME_NO_HOST,
    OUTCOME_SKIPPED,
    OUTCOME_TIMEOUT,
)

logger = logging.getLogger("parcae.agent.ask_user")

VALID_OUTCOMES = frozenset({
    OUTCOME_ANSWERED,
    OUTCOME_CANCELLED,
    OUTCOME_NO_HOST,
    OUTCOME_SKIPPED,
    OUTCOME_TIMEOUT,
})


@runtime_checkable
class AskUserProvider(Protocol):
    """一次会话内的提问通道。"""

    async def ask(self, questions: list[dict]) -> dict[str, Any]:
        """阻塞到用户回答 / 超时 / 中断 / 无客户端，返回 {outcome, answers?}。"""
        ...

    def cancel_pending(self, outcome: str = OUTCOME_CANCELLED) -> int:
        """唤醒所有等待中的提问，返回被唤醒条数。"""
        ...


class RuntimeAskUserProvider:
    """runtime 的 ask_user 实现：事件 + 等待应答。

    `has_clients` 由 manager 注入（WS 连接计数）。没有客户端时直接 no_host，
    不写事件、不等待——与桌面版"No GUI ⇒ fail-closed"一致。
    """

    def __init__(
        self,
        *,
        session_id: str,
        store: Any,
        timeout_secs: float,
        has_clients: Callable[[], bool],
    ):
        self._session_id = session_id
        self._store = store
        self._timeout_secs = float(timeout_secs or 0.0)
        self._has_clients = has_clients
        self._pending: dict[str, tuple[dict[str, Any], asyncio.Event]] = {}

    async def ask(self, questions: list[dict]) -> dict[str, Any]:
        if not self._has_clients():
            return {"outcome": OUTCOME_NO_HOST}

        rid = uuid.uuid4().hex[:12]
        slot: dict[str, Any] = {}
        event = asyncio.Event()
        self._pending[rid] = (slot, event)
        started = time.monotonic()
        try:
            await self._append("ask_user_request", {
                "rid": rid,
                "questions": questions,
                "timeout_secs": self._timeout_secs,
            })
            timeout = self._timeout_secs if self._timeout_secs > 0 else None
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except TimeoutError:
                slot.setdefault("outcome", OUTCOME_TIMEOUT)
                slot.setdefault("waited_secs", self._timeout_secs)
            slot.setdefault("outcome", OUTCOME_NO_HOST)
            slot["waited_secs"] = round(time.monotonic() - started, 3)
            await self._append("ask_user_resolved", {
                "rid": rid,
                "outcome": slot["outcome"],
                "answers": slot.get("answers"),
            })
            return dict(slot)
        finally:
            self._pending.pop(rid, None)

    def resolve(self, rid: str, outcome: str, answers: list[dict] | None = None) -> bool:
        """回答/跳过某次提问。False = 已超时/已中断/rid 不存在。"""
        if outcome not in VALID_OUTCOMES:
            return False
        item = self._pending.get(rid)
        if item is None:
            return False
        slot, event = item
        if "outcome" in slot:
            return False
        slot["outcome"] = outcome
        if answers is not None:
            slot["answers"] = answers
        event.set()
        return True

    def cancel_pending(self, outcome: str = OUTCOME_CANCELLED) -> int:
        items = [(slot, ev) for slot, ev in self._pending.values() if "outcome" not in slot]
        for slot, event in items:
            slot["outcome"] = outcome
            event.set()
        return len(items)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def _append(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            await self._store.append_event(self._session_id, event_type, payload)
        except Exception as exc:  # 事件写失败不能把等待中的提问变成异常
            logger.warning("ask_user event %s failed: %s", event_type, exc)
