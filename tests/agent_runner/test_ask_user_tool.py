"""ask_user 卡片接线：工具侧契约（2026-10-07，wip_chat_3directions 点单①③④）。

这一档只管"agent 传的题面 → 卡片能画的形状 → 用户回答/没回答 → ToolResult 的档位"。
GUI 侧（卡片控件、桥接唤醒）在 `tests/client_ui/test_ask_user_card.py`。

核心取向：**"没人回答"和"用户拒绝"必须是两个不同的档位与文案**——混成一条，
agent 就会把超时读成反对，或把"交回决定权"读成"用户不同意"。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.builtin_tool_executor import _BUILTIN_PARAMETERS  # noqa: E402
from client.core.agent.builtin_tools.ask_user import (  # noqa: E402
    MAX_QUESTIONS,
    AskUserTool,
    describe_outcome,
    normalize_questions,
)
from client.core.agent.types import (  # noqa: E402
    ToolCall,
    ToolResultVariant,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(args: dict) -> ToolCall:
    return ToolCall(id="tc-1", name="ask_user", args=args)


def _one_q(**over) -> dict:
    q = {"question": "要不要现在跑全量？", "header": "全量", "options": ["跑", "先不跑"]}
    q.update(over)
    return q


# ============================================================================
# 题面归一化
# ============================================================================


class TestNormalizeQuestions:
    def test_defaults_are_single_choice_with_free_text(self):
        qs, err = normalize_questions([_one_q()])
        assert err == ""
        assert qs[0]["multiSelect"] is False
        assert qs[0]["allow_free_text"] is True, "点单①：卡片末尾恒挂自由输入框"
        assert qs[0]["options"] == [{"label": "跑", "description": ""},
                                    {"label": "先不跑", "description": ""}]

    def test_multi_select_and_dict_options(self):
        qs, err = normalize_questions([_one_q(multiSelect=True, options=[
            {"label": "A", "description": "第一个"},
            {"label": "  "},          # 空标签 → 丢掉
            {"description": "没标签"},  # 没 label → 丢掉
            3,                          # 非 str/dict → 丢掉
        ])])
        assert err == ""
        assert qs[0]["multiSelect"] is True
        assert [o["label"] for o in qs[0]["options"]] == ["A"]
        assert qs[0]["options"][0]["description"] == "第一个"

    def test_open_question_without_options_is_allowed(self):
        """options 缺省 = 开放题，卡片只有自由输入框（不是报错）。"""
        qs, err = normalize_questions([{"question": "你想怎么命名这个会话？"}])
        assert err == ""
        assert qs[0]["options"] == []

    @pytest.mark.parametrize("bad", [
        None, [], "跑一下", [{}], [{"question": "  "}], [{"question": 3}], ["纯字符串题"],
    ])
    def test_invalid_shapes_are_errors_not_silent_empty(self, bad):
        qs, err = normalize_questions(bad)
        assert err, f"{bad!r} 应该报错而不是变成空题面"
        assert qs == []

    def test_too_many_questions_is_an_explicit_error(self):
        """超过上限要报错——静默截断会让 agent 以为已经问完了。"""
        over = [{"question": f"q{i}"} for i in range(MAX_QUESTIONS + 1)]
        qs, err = normalize_questions(over)
        assert f"{len(over)} > {MAX_QUESTIONS}" in err
        assert qs == []

    def test_header_non_string_is_ignored_not_crash(self):
        qs, err = normalize_questions([{"question": "q", "header": 12}])
        assert err == ""
        assert qs[0]["header"] == ""


# ============================================================================
# 工具入参里没有 timeout 这一格
# ============================================================================


class TestSchemaHasNoTimeout:
    def test_timeout_is_not_an_input_field(self):
        schema = _BUILTIN_PARAMETERS["ask_user"]
        props = schema["properties"]
        assert "timeout" not in props and "timeout_secs" not in props, (
            "超时只由 config [runner] ask_user_timeout_secs 决定：agent 能传超时"
            "就能传 1 秒，把「问你」变相成「替你拍板」"
        )
        assert props["questions"]["type"] == "array"
        assert schema["required"] == ["questions"]


# ============================================================================
# outcome → ToolResult 档位与文案
# ============================================================================


class TestOutcomeMapping:
    def test_answered_is_success_with_json_answers(self):
        content, variant = describe_outcome("answered", [{"question": "q", "selected": ["跑"]}], 0)
        assert variant is ToolResultVariant.SUCCESS
        assert json.loads(content)[0]["selected"] == ["跑"]

    def test_timeout_is_a_distinct_variant_and_says_not_a_refusal(self):
        content, variant = describe_outcome("timeout", None, 30)
        assert variant is ToolResultVariant.TIMEOUT
        assert "30" in content
        assert "不是拒绝" in content, "超时不许被读成反对"

    def test_skipped_hands_the_decision_back(self):
        content, variant = describe_outcome("skipped", None, 0)
        assert variant is ToolResultVariant.USER_DENIED
        assert "决定权交回" in content

    def test_cancelled_is_not_an_answer(self):
        content, variant = describe_outcome("cancelled", None, 0)
        assert variant is ToolResultVariant.USER_DENIED
        assert "不是用户的反对" in content

    def test_unknown_outcome_falls_back_to_no_host_wording(self):
        """桥接层给了没见过的档位（版本漂移）⇒ 按"没人能答"处理，不许编造答案。"""
        content, variant = describe_outcome("谁也没见过的档位", None, 0)
        assert variant is ToolResultVariant.USER_DENIED
        assert "no GUI callback" in content


class TestAskUserToolExecute:
    def test_without_callback_is_fail_closed(self):
        tool = AskUserTool()
        result = _run(tool.execute(_call({"questions": [_one_q()]})))
        assert result.variant is ToolResultVariant.USER_DENIED
        assert "no GUI callback" in result.content

    @pytest.mark.parametrize("args,error_type", [
        ({}, "invalid_argument"),
        ({"questions": []}, "invalid_argument"),
        ({"questions": "不是列表"}, "invalid_argument"),
    ])
    def test_bad_args_are_errors(self, args, error_type):
        tool = AskUserTool(ask_callback=lambda qs: {"outcome": "answered", "answers": []})
        result = _run(tool.execute(_call(args)))
        assert result.variant is ToolResultVariant.ERROR
        assert result.error_type == error_type

    def test_callback_receives_normalized_questions(self):
        seen: list = []

        def cb(questions):
            seen.append(questions)
            return {"outcome": "answered",
                    "answers": [{"question": q["question"], "selected": [], "free_text": "随意"}
                                for q in questions]}

        result = _run(AskUserTool(ask_callback=cb).execute(
            _call({"questions": [_one_q(multiSelect="yes")]}),
        ))
        assert seen[0][0]["multiSelect"] is True, "字符串 'yes' 归一化成 bool 再交给卡片"
        assert result.variant is ToolResultVariant.SUCCESS

    def test_timeout_outcome_survives_to_the_result(self):
        result = _run(AskUserTool(
            ask_callback=lambda qs: {"outcome": "timeout", "waited_secs": 12.5},
        ).execute(_call({"questions": [_one_q()]})))
        assert result.variant is ToolResultVariant.TIMEOUT
        assert "12.5" in result.content

    def test_callback_exception_is_error_not_a_hang(self):
        def boom(qs):
            raise RuntimeError("卡片炸了")

        result = _run(AskUserTool(ask_callback=boom).execute(_call({"questions": [_one_q()]})))
        assert result.variant is ToolResultVariant.ERROR
        assert "卡片炸了" in result.content

    def test_payload_without_outcome_is_treated_as_no_host(self):
        """回调返回空 payload（没有 outcome）⇒ 当"没人能答"，不许当"用户已确认"。"""
        result = _run(AskUserTool(ask_callback=lambda qs: {}).execute(
            _call({"questions": [_one_q()]}),
        ))
        assert result.variant is ToolResultVariant.USER_DENIED
        assert "no GUI callback" in result.content


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
