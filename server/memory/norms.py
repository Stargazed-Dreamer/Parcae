"""DL1：上下文红线（norms bundle）。

只做「取 + 排 + 按预算截断」，不做写入、不做模型调用。
数据源 = ``facts`` 中 ``kind='norm'`` 的活跃行；DL3 已加 ``scope`` / ``must_read``
列，所以 DL1 不再改 schema。

预算默认 5,000 字符（2026-10-27 用户把初版 2,000 提到 5,000）。超预算时
安全类（``must_read=1``）优先保留，必要时截断正文；效率类整条跳过。
``truncated`` / ``omitted`` 都回显，不静默丢。
"""

from __future__ import annotations

from typing import Any

from server.memory.store import MemoryStore

NORMS_BUDGET_CHARS = 5_000
_MIN_TRUNCATED_CHARS = 40
_TRUNCATION_MARK = "…[截断]"


def _norm_text(row: dict[str, Any]) -> str:
    """把一条 norm 格式化成可注入的一行。

    优先用 ``title`` 做短标签；没有标题时退回 key。正文用 ``value`` 原文，
    不用 ``summary``——红线要的是可执行原文，不是摘要。
    """
    title = str(row.get("title") or row.get("key") or "").strip()
    value = str(row.get("value") or "").strip()
    if not value:
        return ""
    return f"- {title}：{value}" if title else f"- {value}"


def _item(row: dict[str, Any], text: str, *, truncated: bool = False) -> dict[str, Any]:
    return {
        "key": row.get("key"),
        "title": row.get("title"),
        "text": text,
        "chars": len(text),
        "must_read": bool(int(row.get("must_read") or 0)),
        "scope": row.get("scope"),
        "truncated": truncated,
    }


def build_norms_bundle(
    store: MemoryStore,
    project_id: str | None = None,
    budget: int = NORMS_BUDGET_CHARS,
) -> dict:
    """构建 norms bundle（DL1 的投递产物）。

    排序由 ``store.list_norms`` 保证：``must_read`` 在前，global 先于 project，
    再按更新时间。超预算时：

    - 安全类：截断正文后保留（标 ``truncated``），不整条丢；
    - 效率类：整条跳过，记入 ``omitted``；
    - 全部去向都回显，调用方能看到 ``truncated`` / ``omitted``。
    """
    budget = max(1, int(budget))
    rows = store.list_norms(project_id=project_id, limit=500)
    items: list[dict[str, Any]] = []
    omitted: list[dict[str, str]] = []
    used = 0
    truncated = False

    for row in rows:
        text = _norm_text(row)
        key = str(row.get("key") or "")
        if not text:
            omitted.append({"key": key, "reason": "empty"})
            truncated = True
            continue

        remaining = budget - used
        if len(text) <= remaining:
            items.append(_item(row, text))
            used += len(text)
            continue

        if int(row.get("must_read") or 0) == 1:
            if remaining > _MIN_TRUNCATED_CHARS:
                clipped = text[: remaining - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK
                items.append(_item(row, clipped, truncated=True))
                used += len(clipped)
            else:
                omitted.append({"key": key, "reason": "safety_over_budget"})
            truncated = True
            break

        omitted.append({"key": key, "reason": "over_budget"})
        truncated = True

    return {
        "items": items,
        "chars": used,
        "budget": budget,
        "truncated": truncated,
        "omitted": omitted,
        "count": len(items),
        "project_id": project_id,
    }
