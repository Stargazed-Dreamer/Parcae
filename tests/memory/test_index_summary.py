"""记忆索引标题取值：agent_guide 的【必读记忆】行不得回显原始 JSON 信封。

回归锚点：2026-10-04 实测 3 条 key 的 summary 是 `{"source": "manual", "content": ...}`
这类元数据信封截断，同清单里别的条目却是人话标题。
"""

import json

from server.memory.manager import index_summary_from_value


def test_plain_text_truncated():
    assert index_summary_from_value("") == ""
    assert index_summary_from_value("短标题") == "短标题"
    assert len(index_summary_from_value("x" * 300)) == 120


def test_non_json_and_list_fall_back_to_truncation():
    assert index_summary_from_value("不是 JSON {"[:80]) == "不是 JSON {"
    assert index_summary_from_value("[1, 2, 3]").startswith("[1, 2, 3]")


def test_title_fields_win_and_are_not_json():
    for field in ("name", "description", "title", "summary"):
        value = json.dumps({field: f"人话标题-{field}", "content": "正文"}, ensure_ascii=False)
        got = index_summary_from_value(value)
        assert got == f"人话标题-{field}"
        assert not got.startswith("{")


def test_blank_title_field_falls_through_to_body():
    value = json.dumps({"name": "   ", "content": "## 现象\n\n真实原因在第二行"}, ensure_ascii=False)
    assert index_summary_from_value(value) == "现象"


def test_envelope_without_title_uses_first_body_line():
    value = json.dumps(
        {"source": "manual", "type": "reference", "content": "## 现象\n\nheadless-agent-session 用例失败"},
        ensure_ascii=False,
    )
    got = index_summary_from_value(value)
    assert got == "现象"
    assert not got.startswith("{")


def test_structured_memory_shows_field_names_not_json():
    """顶层只有嵌套 dict/list 的那一档（真库 36 条）：上屏字段名清单，不再截 JSON 前缀。

    旧兜底是 `value[:120]`，读者看到的是 `{"stock_review_output": {"no_html": true,`，
    既不知道里面是什么，也没法判断要不要 memory_get。
    """
    value = json.dumps(
        {"stock_review_output": {"no_html": True}, "stock_advisor_analysis": {"x": 1}},
        ensure_ascii=False,
    )
    got = index_summary_from_value(value)
    assert got == "（结构化：stock_review_output、stock_advisor_analysis）"
    assert not got.startswith("{")


def test_field_name_list_capped_at_three_names():
    value = json.dumps({f"k{i}": {"v": i} for i in range(7)}, ensure_ascii=False)
    got = index_summary_from_value(value)
    assert got == "（结构化：k0、k1、k2…共 7 项）"
    assert len(got) <= 120


def test_double_envelope_uses_inner_field_names():
    # {"key":..., "value":{...}} 这种双层信封：内层才是内容，外层键不该出现在清单里
    value = json.dumps(
        {"key": "reference_e2e_test_window_coord_uia_realclient", "value": {"finding": "x", "why": "y"}},
        ensure_ascii=False,
    )
    got = index_summary_from_value(value)
    assert got == "（结构化：finding、why）"
    assert "key" not in got and "value" not in got


def test_envelope_only_dict_has_no_shape_to_show():
    # 除信封键外什么都不剩：给空串，让调用方显示"(无摘要)"，而不是编出一个像标题的字符串
    assert index_summary_from_value('{"source": "manual", "confidence": 1.0}') == ""
    assert index_summary_from_value("{}") == ""
