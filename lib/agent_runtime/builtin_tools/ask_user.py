"""T17: ask_user 工具（向用户提问）+ 2026-10-07 交互卡片接线（点单①③④）。

GUI 通过 `ask_callback` 注入提问回调（`AskUserBridge.ask` 的形状），未注入时返回
USER_DENIED（fail-closed，不让 agent 卡住）。

回调契约（2026-10-07 起）：`callback(questions) -> {"outcome": str, "answers": [...]}`。
`outcome` 决定 ToolResult 的形态，**五种档位各有各的文案**——把"没回答"和"拒绝回答"
混成一条字符串，agent 就会把超时读成反对。

⚠️ 超时值不在工具入参里：只从 config `[runner] ask_user_timeout_secs` 读（0 = 永不超时）。
agent 传 timeout 等于可以传 1 秒，把"问你"变相成"替你拍板"。
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from lib.agent_runtime.ask_user_outcomes import (
    OUTCOME_ANSWERED,
    OUTCOME_CANCELLED,
    OUTCOME_NO_HOST,
    OUTCOME_SKIPPED,
    OUTCOME_TIMEOUT,
)
from lib.agent_runtime.builtin_tools.base import BuiltinTool
from lib.agent_runtime.types import ToolCall, ToolResult, ToolResultVariant

# 每张卡最多几道题（超过就报错，而不是静默截断让 agent 以为问完了）
MAX_QUESTIONS = 4


def normalize_questions(raw: Any) -> tuple[list[dict], str]:
    """把 agent 传来的 questions 洗成卡片能直接画的形状。

    Returns:
        (questions, error)：error 非空表示输入不可用，questions 为空列表。

    接受形态：
    - question: 必填非空字符串
    - header: 可选短标签
    - options: list[str] 或 list[{"label","description"}]（混合也行）；空 = 纯开放题
    - multiSelect: 可选 bool（默认 False ⇒ 单选）
    - allow_free_text: 可选 bool（默认 True ⇒ 卡片恒挂一条自由输入框）
    """
    if not isinstance(raw, list) or not raw:
        return [], "questions must be a non-empty list"
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], f"question entry must be an object, got {type(item).__name__}"
        text = item.get("question")
        if not isinstance(text, str) or not text.strip():
            return [], "each question needs a non-empty 'question' string"
        options = _normalize_options(item.get("options"))
        out.append({
            "question": text.strip(),
            "header": (item.get("header") or "").strip() if isinstance(item.get("header"), str) else "",
            "options": options,
            "multiSelect": bool(item.get("multiSelect", False)),
            "allow_free_text": bool(item.get("allow_free_text", True)),
        })
    if len(out) > MAX_QUESTIONS:
        return [], f"too many questions ({len(out)} > {MAX_QUESTIONS})"
    return out, ""


def _normalize_options(raw: Any) -> list[dict]:
    """选项归一化成 [{"label", "description"}]；无法识别的选项直接丢掉而不是变成空条目。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for opt in raw:
        if isinstance(opt, str):
            if opt.strip():
                out.append({"label": opt.strip(), "description": ""})
        elif isinstance(opt, dict):
            label = opt.get("label")
            if isinstance(label, str) and label.strip():
                desc = opt.get("description")
                out.append({
                    "label": label.strip(),
                    "description": desc.strip() if isinstance(desc, str) else "",
                })
    return out


def describe_outcome(
    outcome: str,
    answers: list[dict] | None,
    waited_secs: float,
) -> tuple[str, ToolResultVariant]:
    """把 outcome 翻成 (给 agent 看的文本, ToolResultVariant)。

    三档"没有回答"（timeout / cancelled / no_host）都必须说清楚**这不是用户的反对**，
    否则 agent 会把它当成"用户不同意"继续往下推。档位也分开：超时走 TIMEOUT，
    主动跳过/中断走 USER_DENIED——按变体分流的那些逻辑才分得开谁没答。
    """
    if outcome == OUTCOME_ANSWERED:
        return (
            json.dumps(answers or [], ensure_ascii=False, indent=2),
            ToolResultVariant.SUCCESS,
        )
    if outcome == OUTCOME_SKIPPED:
        return (
            "ask_user: 用户选择「跳过，你自己定」，把决定权交回给你。"
            "按你自己的判断继续，不要就同一件事反复追问。",
            ToolResultVariant.USER_DENIED,
        )
    if outcome == OUTCOME_TIMEOUT:
        return (
            f"ask_user: 用户在 {waited_secs:g} 秒内没有回答（config "
            "[runner] ask_user_timeout_secs）。"
            "这不是拒绝，也不是同意——只是没人应答。要么换个更省事的问法重问一次，"
            "要么改用其他证据自行判断，别把超时读成反对。",
            ToolResultVariant.TIMEOUT,
        )
    if outcome == OUTCOME_CANCELLED:
        return (
            "ask_user: 会话被中断或切走，问题未回答（不是用户的反对）。",
            ToolResultVariant.USER_DENIED,
        )
    return (
        "ask_user: no GUI callback available (cannot prompt user)",
        ToolResultVariant.USER_DENIED,
    )


class AskUserTool(BuiltinTool):
    """向用户提问并等待选择（单选 / 多选 / 自由输入三态）。"""

    operation_id = "ask_user"

    def __init__(
        self,
        ask_callback: Callable[[list[dict]], dict | Awaitable[dict]] | None = None,
    ):
        """ask_callback: 接收归一化后的 questions，返回 {"outcome", "answers"}。

        GUI 注入 `AskUserBridge.ask`（同步阻塞，卡片在主线程渲染）；runtime 注入
        `RuntimeAskUserProvider.ask`（async，不阻塞事件循环）。
        未注入时返回 USER_DENIED（fail-closed）。
        """
        self._ask_callback = ask_callback

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        questions, error = normalize_questions(args.get("questions"))
        if error:
            return self._error(tool_call, error, "invalid_argument")

        if self._ask_callback is None:
            # fail-closed：无 GUI 回调时返回 USER_DENIED
            content, variant = describe_outcome(OUTCOME_NO_HOST, None, 0.0)
            return ToolResult(tool_call_id=tool_call.id, content=content, variant=variant)

        try:
            result = self._ask_callback(questions)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:
            return self._error(
                tool_call,
                f"ask_user failed: {type(e).__name__}: {e}",
                "ask_user_error",
            )

        outcome = str(result.get("outcome") or OUTCOME_NO_HOST)
        content, variant = describe_outcome(
            outcome, result.get("answers"), float(result.get("waited_secs") or 0.0),
        )
        return ToolResult(tool_call_id=tool_call.id, content=content, variant=variant)
