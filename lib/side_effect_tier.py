"""副作用分档（side-effect tier）——单一真源。

用途：当一步执行的**结局不明**（超时、崩溃、进程被杀）时，决定系统怎么处置：
- 只读 / 本地可逆 → 可以自动补做，重试建议可以照发
- 外部不可逆 → 不许自动重做，只能停下来告诉人

为什么不让模型临场判断：见 `temp/sdd/unknown-outcome-evidence/design-decisions.md` D3——
在"防误判"的前提下，拿一个误判去防另一个误判不成立，所以分档必须是静态表。

为什么放 `lib/`：C4 硬规则禁止 client→server 与 server→client 互相 import
（`tools/check_hard_rules.py:91-100`），而消费方恰好一边一个（client 的 reconciler
决定重试文案，server 的 loop_manager 决定补跑）。`lib/` 是唯一合法共享层。

默认方向是**保守**：未列出的操作一律按外部不可逆处理。宁可少补跑一次，
也不要重复发出去一条消息。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final

TIER_READ_ONLY: Final = "read_only"
TIER_LOCAL_REVERSIBLE: Final = "local_reversible"
TIER_EXTERNAL_IRREVERSIBLE: Final = "external_irreversible"

TIERS: Final = (TIER_READ_ONLY, TIER_LOCAL_REVERSIBLE, TIER_EXTERNAL_IRREVERSIBLE)

# 结局不明时是否允许系统自动补做
_AUTO_RERUNNABLE: Final = {TIER_READ_ONLY, TIER_LOCAL_REVERSIBLE}

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- 只读：没有副作用，重跑无害 -------------------------------------------------
_READ_ONLY_EXACT: Final = {
    "agent_guide",
    "ask_user",
    "browser_list_tabs",
    "browser_match_site",
    "browser_snapshot",
    "browser_status",
    "browser_wait_for",
    "capture_screen",
    "docviewer_status",
    "exec_inspect",
    "exec_kill",
    "exec_status",
    "exec_terminal_detail",
    "exec_terminal_kill",
    "exec_terminal_stream",
    "focus_window",
    "health_health_get",
    "inbound_calls_list",
    "inbound_stats",
    "keep_awake_status",
    "list_windows",
    "parcae_get_docs",
    "parcae_list_templates",
    "parcae_list_tools",
    "llm_pool_health_check",
    "llm_pool_stats",
    "llm_pool_status",
    "memory_list",
    "memory_get",
    "memory_maintain_status",
    "memory_search",
    "memory_search_traces_list",
    "memory_search_traces_detail",
    "evidence_ledger_recent",
    "memory_evidence_recent",
    "memory_evidence_detail",
    "todos_list",
    "todos_due",
    "wip_list",
    "wip_get",
    "wip_get_many",
    "vision_status",
    "ocr_path",
    "screen_ocr",
    "screen_snapshot",
    "screen_analyze",
    "docviewer_read",
    "file_read",
    "file_grep",
    "file_glob",
    "file_ls",
    "web_search",
    "web_fetch",
    "activity_daily_read",
    "activity_daily_list",
}

_READ_ONLY_PREFIXES: Final = (
    "get_",
    "list_",
    "read_",
    "search_",
    "describe_",
)

# --- 本地可逆：改的是本仓库/本机数据，且留下可回滚材料 --------------------------
_LOCAL_REVERSIBLE_EXACT: Final = {
    "file_write",
    "file_edit",
    "exec_apply_patch",
    "memory_set",
    "memory_delete",
    "todos_create",
    "todos_update",
    "todos_mark_done",
    "wip_create",
    "wip_update",
    "wip_delete",
    "inbox_resolve",
    "inbox_batch",
    "inbox_ignore",
    "activity_daily_update",
    "browser_write_lesson",
    "clear_skip_cache",
    "memory_search_traces_cleanup",
    "memory_evidence_cleanup",
}

# --- 外部不可逆：一旦落地就无法靠本机撤回 -------------------------------------
_EXTERNAL_IRREVERSIBLE_EXACT: Final = {
    "browser_action",
    "browser_navigate",
    "browser_screenshot",
    "browser_session_close",
    "browser_session_create",
    "browser_set_http_credentials",
    "execute_action",
    "exec_cmd",
    "exec_python",
    "exec_send_input",
    "exec_terminal_input",
    "exec_terminal_spawn",
    "llm_pool_call",
    "llm_pool_call_simple",
    "llm_pool_chat_tools",
    "agent_chat",
    "send_message_to_chat_session",
    "create_chat_session",
    "fork_chat_session",
    "inbound_keys_create",
    "inbound_keys_delete",
    "inbound_keys_update",
    "command_guard_request_approval",
    "batch_actions",
}

# 兜不住的前缀族（宁可保守）
_EXTERNAL_IRREVERSIBLE_PREFIXES: Final = (
    "screen_",
    "desktop_",
    "vm_",
    "git_",
    "net_",
)

# workspace/*/manifest.toml 里 [side_effect_tier] 段可覆盖上面任何一条
_OVERRIDES_CACHE: dict[str, str] | None = None
_OVERRIDES_STAMP: tuple[int, int] | None = None


def _scan_manifest_overrides() -> dict[str, str]:
    """收集 workspace manifest 的 [side_effect_tier] 覆盖项。

    返回 {operation_id: tier}；非法 tier 值忽略（不让一个写错的 manifest 把默认方向翻掉）。
    """
    overrides: dict[str, str] = {}
    ws_root = _PROJECT_ROOT / "workspace"
    if not ws_root.exists():
        return overrides
    for manifest in sorted(ws_root.glob("*/manifest.toml")):
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        section = data.get("side_effect_tier")
        if not isinstance(section, dict):
            continue
        for name, tier in section.items():
            if isinstance(tier, str) and tier in TIERS:
                overrides[str(name)] = tier
    return overrides


def _overrides() -> dict[str, str]:
    """按 manifest 数量 + 最新 mtime 作缓存指纹，避免每次调用都扫盘。"""
    global _OVERRIDES_CACHE, _OVERRIDES_STAMP
    ws_root = _PROJECT_ROOT / "workspace"
    files = list(ws_root.glob("*/manifest.toml")) if ws_root.exists() else []
    stamp = (len(files), max((f.stat().st_mtime_ns for f in files), default=0))
    if _OVERRIDES_CACHE is None or stamp != _OVERRIDES_STAMP:
        _OVERRIDES_CACHE = _scan_manifest_overrides()
        _OVERRIDES_STAMP = stamp
    return _OVERRIDES_CACHE


def clear_override_cache() -> None:
    """测试/运维用：强制下次重新扫描 manifest 覆盖项。"""
    global _OVERRIDES_CACHE, _OVERRIDES_STAMP
    _OVERRIDES_CACHE = None
    _OVERRIDES_STAMP = None


def tier_for(name: str) -> str:
    """返回该操作的副作用档。未知名字一律 external_irreversible。"""
    if not name:
        return TIER_EXTERNAL_IRREVERSIBLE
    override = _overrides().get(name)
    if override is not None:
        return override
    if name in _EXTERNAL_IRREVERSIBLE_EXACT:
        return TIER_EXTERNAL_IRREVERSIBLE
    if name in _LOCAL_REVERSIBLE_EXACT:
        return TIER_LOCAL_REVERSIBLE
    if name in _READ_ONLY_EXACT:
        return TIER_READ_ONLY
    if name.startswith(_EXTERNAL_IRREVERSIBLE_PREFIXES):
        return TIER_EXTERNAL_IRREVERSIBLE
    if name.startswith(_READ_ONLY_PREFIXES):
        return TIER_READ_ONLY
    return TIER_EXTERNAL_IRREVERSIBLE


def can_auto_rerun(name: str) -> bool:
    """结局不明时是否允许自动补做。"""
    return tier_for(name) in _AUTO_RERUNNABLE


def should_suggest_retry(name: str) -> bool:
    """恢复/重试提示里是否可以建议"再跑一次"。

    与 can_auto_rerun 同判据，但语义独立：外部不可逆动作连"建议"都不该发出去，
    因为模型会把建议当成授权。
    """
    return can_auto_rerun(name)


def tier_summary() -> dict[str, int]:
    """当前静态表规模（供 /health 与状态面板展示，不写死数字进文档）。"""
    return {
        TIER_READ_ONLY: len(_READ_ONLY_EXACT) + len(_READ_ONLY_PREFIXES),
        TIER_LOCAL_REVERSIBLE: len(_LOCAL_REVERSIBLE_EXACT),
        TIER_EXTERNAL_IRREVERSIBLE: (
            len(_EXTERNAL_IRREVERSIBLE_EXACT) + len(_EXTERNAL_IRREVERSIBLE_PREFIXES)
        ),
        "manifest_overrides": len(_overrides()),
    }
