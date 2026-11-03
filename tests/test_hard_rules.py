"""硬化规则检查的 pytest 包装（quick 层）。

规则真源：tools/check_hard_rules.py。任何 C1-C5 违规让测试变红，
对应"说过三遍的规则→会变红的构建"（详见 temp/sdd/agent-trust-guardrails/spec.md）。
"""
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from check_hard_rules import _scan_mcp_refs, run_checks  # noqa: E402


def test_hard_rules_all_tracked_files():
    violations = run_checks(staged=False)
    assert not violations, (
        "硬化规则违规（修复文件或显式更新 tools/check_hard_rules.py 白名单）：\n"
        + "\n".join(violations)
    )


# --- C5 专项：两个陷阱的回归保护（真源 server/mcp_whitelist.py 的 DIRECT_TOOLS）---

# 最小替身直连表：wip_get 是直连，parcae_advanced_tool 是直连且同时在
# GATEWAY_EXCLUDE，wip_create 只在网关层。
_FAKE_DIRECT = {"wip_get", "parcae_advanced_tool"}


def test_c5_gateway_only_name_is_violation():
    """正面推荐一个只在网关层的工具 → 必须红。"""
    assert _scan_mcp_refs("用 `mcp__localagent__wip_create` 创建卡片。", _FAKE_DIRECT) == [
        (1, "wip_create")
    ]


def test_c5_direct_name_is_ok():
    """直连层名字 → 不红。"""
    assert _scan_mcp_refs("用 `mcp__localagent__wip_get` 读卡片。", _FAKE_DIRECT) == []


def test_c5_gateway_exclude_does_not_mean_absent():
    """陷阱②：parcae_advanced_tool 同时属 DIRECT_TOOLS 与 GATEWAY_EXCLUDE → 不红。

    拿 GATEWAY_EXCLUDE 当"不存在"会把这个最高频网关入口判违规。
    """
    assert _scan_mcp_refs(
        "兜底走 `mcp__localagent__parcae_advanced_tool`。", _FAKE_DIRECT
    ) == []


def test_c5_negative_context_is_exempt():
    """陷阱①：刻意写的反例（AGENTS.md:190 / CHANGELOG.md:67 型）→ 豁免。"""
    for text in (
        "按 `mcp__localagent__wip_create` 调会直接 not found",
        "照 `mcp__localagent__todos_update` 调会直接 not found",
        "`mcp__localagent__memory_search` **不是**直连工具",
        "禁止写 `mcp__localagent__wip_delete`",
        "`mcp__localagent__screen_zoom` 已移除",
    ):
        assert _scan_mcp_refs(text, _FAKE_DIRECT) == [], text


def test_c5_multiple_names_in_one_line():
    """一行内多个非直连名字 → 每条都报。"""
    got = _scan_mcp_refs(
        "用 `mcp__localagent__screen_ocr` 或 `mcp__localagent__screen_zoom`。", _FAKE_DIRECT
    )
    assert got == [(1, "screen_ocr"), (1, "screen_zoom")]


def test_c5_reports_line_numbers():
    assert _scan_mcp_refs(
        "第一行 `mcp__localagent__wip_get`\n第二行 `mcp__localagent__wip_create`\n",
        _FAKE_DIRECT,
    ) == [(2, "wip_create")]
