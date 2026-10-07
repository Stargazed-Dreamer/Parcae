"""ask_user 主线程桥接（2026-10-07，wip_chat_3directions 点单①③④）。

worker 线程里的 `AskUserTool` 要**同步**拿到用户选择，而卡片必须画在主线程
（v6-02 §3.1 禁止 worker 直接触发 widget）。形状照
`client/core/http_worker.py` 的 `ApprovalBridge`：Signal 投递到主线程槽，
主线程填好 slot 后 `threading.Event.set()` 唤醒 worker。

两处与审批桥有意不同，都是点单定的：
1. **超时值只从配置读**（config `[runner] ask_user_timeout_secs`，0 = 永不超时），
   工具入参里没有 timeout 这一格。理由：agent 若能传超时，就能传 1 秒把
   "问你"变相成"替你拍板"。
2. **中断必须能唤醒等待**：`cancel_pending()` 由主线程在中断/切会话时调用，
   它只 set 普通 `threading.Event`，不依赖 worker 的事件循环——那个循环此刻正被
   这次阻塞占用，走 `call_soon_threadsafe` 永远排不进去（"永不超时"就会变成线程死等）。
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger("localagent.ask_user_bridge")

# outcome 档位（AskUserTool 按此映射 ToolResult，不许含糊）
OUTCOME_ANSWERED = "answered"     # 用户点了提交并给出选择
OUTCOME_SKIPPED = "skipped"       # 用户点「跳过，你自己定」——明确把决定权交回 agent
OUTCOME_TIMEOUT = "timeout"       # 配置的等待时间内没人回答（不是拒绝）
OUTCOME_CANCELLED = "cancelled"   # 会话被中断 / 切走了
OUTCOME_NO_HOST = "no_host"       # 没有可渲染卡片的地方（GUI 未就绪）→ 维持 fail-closed


class AskUserBridge(QObject):
    """一次一个待答提问：worker 调 `ask()` 阻塞，主线程卡片答完再唤醒。"""

    # payload = {"rid", "questions", "slot", "event"} 或 {"kind": "expire", "rid"}
    _request = Signal(object)

    def __init__(self, timeout_secs: float = 0.0, parent=None):
        super().__init__(parent)
        self._timeout_secs = float(timeout_secs or 0.0)
        self._pending: dict[str, tuple[dict[str, Any], threading.Event]] = {}
        self._lock = threading.Lock()
        # 面板注册的渲染器：handler(payload) 负责画卡片；卡片答完调 resolve(rid, ...)
        self._ask_handler: Callable[[dict[str, Any]], None] | None = None
        self._expire_handler: Callable[[str], None] | None = None
        self._request.connect(self._on_request)

    # ------------------------------------------------------------------
    # 配置与注册
    # ------------------------------------------------------------------

    @property
    def timeout_secs(self) -> float:
        return self._timeout_secs

    def set_handlers(
        self,
        ask_handler: Callable[[dict[str, Any]], None] | None,
        expire_handler: Callable[[str], None] | None = None,
    ) -> None:
        """主线程注册渲染器。传 None 表示"没有 GUI"，此时 ask() 立即回落 no_host。"""
        self._ask_handler = ask_handler
        self._expire_handler = expire_handler

    # ------------------------------------------------------------------
    # worker 线程侧
    # ------------------------------------------------------------------

    def ask(self, questions: list[dict]) -> dict[str, Any]:
        """从任何线程调用，阻塞到卡片给出结果 / 超时 / 被中断。

        Returns:
            {"outcome": str, "answers": list[dict]（仅 answered 时）, ...}
        """
        rid = uuid.uuid4().hex[:12]
        slot: dict[str, Any] = {}
        event = threading.Event()
        with self._lock:
            self._pending[rid] = (slot, event)
        # 跨线程发射自动 QueuedConnection ⇒ _on_request 在主线程执行
        self._request.emit({"rid": rid, "questions": questions, "slot": slot, "event": event})

        timeout = self._timeout_secs if self._timeout_secs > 0 else None
        try:
            if not event.wait(timeout=timeout):
                with self._lock:
                    # 抢在卡片之前摘走：之后再点也 resolve 不上（返回 False）
                    self._pending.pop(rid, None)
                slot.setdefault("outcome", OUTCOME_TIMEOUT)
                slot.setdefault("waited_secs", self._timeout_secs)
                # 超时后必须把那张卡标成已失效——留着就是一张"还能点但已经作废"的假入口
                self._request.emit({"kind": "expire", "rid": rid})
            return dict(slot)
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    # ------------------------------------------------------------------
    # 主线程侧
    # ------------------------------------------------------------------

    def resolve(
        self,
        rid: str,
        outcome: str,
        answers: list[dict] | None = None,
    ) -> bool:
        """卡片答完调它。返回 False = 这次回答已经没人等了（超时/中断后用户才点）。

        两道拒绝都要，缺一不可：
        - 条目不在 `_pending`：worker 早已走完 `finally` 清理
        - slot 里已经有 `outcome`：worker 已被超时/中断唤醒，但还没来得及执行清理
          （那条清理归 `ask()` 所有，不由这里代劳）。少这一道，卡住的窗口里用户点提交
          会返回 True，卡片显示"已回答"而 agent 那边读走的其实是"被中断"。
        """
        with self._lock:
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
        """中断/切会话时唤醒所有还在等的提问（返回被唤醒的条数）。

        只 set `threading.Event`——不调 worker 事件循环里的任何东西，那是此刻唯一还能
        送达的路径（`AskUserTool.execute` 正占着那条循环）。

        条目的清除**归 `ask()` 的 finally**（谁等待谁清理），这里不抢着 pop：抢了也
        挡不住迟到点击，真正挡住它的是 `resolve()` 里"slot 已有 outcome 就拒绝"那道。
        """
        with self._lock:
            items = [(slot, ev) for slot, ev in self._pending.values() if "outcome" not in slot]
            for slot, event in items:
                slot["outcome"] = outcome
                event.set()
        return len(items)

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def _on_request(self, payload: dict[str, Any]) -> None:
        """主线程槽：渲染卡片，或通知卡片作废。渲染器缺失时立即回落 no_host（不悬等）。"""
        if payload.get("kind") == "expire":
            if self._expire_handler is not None:
                try:
                    self._expire_handler(payload.get("rid", ""))
                except Exception as e:  # 渲染器坏了不能把 worker 永久挂住
                    logger.warning("AskUserBridge: expire handler 异常: %s", e)
                    self.cancel_pending(OUTCOME_NO_HOST)
            return

        handler = self._ask_handler
        slot = payload["slot"]
        event = payload["event"]
        if handler is None:
            slot.setdefault("outcome", OUTCOME_NO_HOST)
            event.set()
            return
        try:
            handler(payload)
        except Exception as e:
            logger.warning("AskUserBridge: 卡片渲染失败: %s", e)
            slot.setdefault("outcome", OUTCOME_NO_HOST)
            event.set()
