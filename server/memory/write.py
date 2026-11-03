"""记忆写入与候选审核（DL3/DL5）。

写入只走 ``MemoryStore.upsert_fact`` 唯一漏斗；本模块不直接 INSERT facts。
候选审核复用同一套校验，人工改完的候选不会绕过去重。
"""

from __future__ import annotations

import json
from typing import Any

from server.memory.store import MemoryStore
from server.memory.wrapup import (
    KINDS,
    ORIGINS,
    SENSITIVITIES,
    compute_dedup_hash,
)

_REVIEWABLE = ("rejected",)
_DISMISSED = "dismissed"


class MemoryWriteError(Exception):
    """记忆写入/审核的可返回错误。"""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def write_memory(memory: MemoryStore, payload: dict[str, Any]) -> dict[str, Any]:
    content = str(payload.get("content") or payload.get("value") or "").strip()
    kind = payload.get("kind")
    scope = str(payload.get("scope") or "").strip()
    if not content:
        raise MemoryWriteError("empty_content", "正文为空")
    if kind not in KINDS:
        raise MemoryWriteError("unknown_kind", "kind 不在 7 个枚举内")
    if kind == "current_state" and not payload.get("expires_at"):
        raise MemoryWriteError("missing_expires_at", "kind='current_state' 必须带 expires_at")
    if not (scope == "global" or scope.startswith("project:") or scope.startswith("topic:")):
        raise MemoryWriteError("bad_scope", "scope 必须形如 global / project:<id> / topic:<id>")
    must_read = int(payload.get("must_read") or 0)
    if must_read and kind != "norm":
        raise MemoryWriteError("must_read_not_norm", "must_read 只允许 kind='norm'")
    origin = payload.get("origin") or "agent_extracted"
    if origin not in ORIGINS:
        raise MemoryWriteError("bad_origin", "origin 不合法")
    sensitivity = payload.get("sensitivity") or "private-eng"
    if sensitivity not in SENSITIVITIES:
        raise MemoryWriteError("bad_sensitivity", "sensitivity 不合法")

    title = payload.get("title")
    dedup_hash = compute_dedup_hash(scope, kind, title, content)
    existing = memory.find_fact_by_dedup(dedup_hash)
    if not existing:
        # v8 之前（含 DL1 种子）写入的行没有 dedup_hash，按同 scope/kind 重算比对
        for row in memory.list_fact_rows_by_scope_kind(scope, kind):
            row_hash = compute_dedup_hash(
                scope, kind, row.get("title") or row.get("key"), row.get("value") or ""
            )
            if row_hash == dedup_hash:
                existing = row
                break
    if existing:
        return {
            "status": "duplicate",
            "key": existing.get("key"),
            "dedup_hash": dedup_hash,
        }

    key = payload.get("key") or f"agent_{dedup_hash[:16]}"
    memory.upsert_fact(
        key,
        content,
        source="manual" if origin in ("user_quote", "system") else "auto",
        fact_type=payload.get("fact_type"),
        title=title,
        summary=payload.get("summary") or content[:160],
        scope=scope,
        kind=kind,
        origin=origin,
        sensitivity=sensitivity,
        evidence_refs=payload.get("evidence_refs"),
        source_quote_ids=payload.get("source_quote_ids"),
        must_read=must_read,
        expires_at=payload.get("expires_at"),
        dedup_hash=dedup_hash,
    )
    return {
        "status": "accepted",
        "key": key,
        "scope": scope,
        "kind": kind,
        "must_read": bool(must_read),
        "dedup_hash": dedup_hash,
    }


def _candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, dict):
        return payload
    try:
        loaded = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def review_candidate(
    memory: MemoryStore,
    candidate_id: int,
    action: str,
    *,
    overrides: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """采纳 / 退回一条待审候选；只有 rejected 行可处理。"""
    row = memory.get_memory_candidate(candidate_id)
    if not row:
        raise MemoryWriteError("candidate_not_found", "候选不存在", 404)
    if row.get("status") not in _REVIEWABLE:
        raise MemoryWriteError(
            "candidate_not_pending", f"候选状态为 {row.get('status')}，不能重复处理", 409
        )

    if action == "reject":
        ok = memory.resolve_memory_candidate(
            candidate_id, _DISMISSED, reason=reason or "manual_reject"
        )
        if not ok:
            raise MemoryWriteError("candidate_not_pending", "候选已被处理", 409)
        return {"candidate_id": candidate_id, "status": _DISMISSED, "memory": None}

    if action != "accept":
        raise MemoryWriteError("bad_action", "action 只能是 accept / reject")

    merged = {**_candidate_payload(row), **(overrides or {})}
    result = write_memory(memory, merged)
    status = "accepted" if result["status"] == "accepted" else result["status"]
    ok = memory.resolve_memory_candidate(
        candidate_id, status, fact_key=result.get("key")
    )
    if not ok:
        raise MemoryWriteError("candidate_not_pending", "候选已被处理", 409)
    return {"candidate_id": candidate_id, "status": status, "memory": result}
