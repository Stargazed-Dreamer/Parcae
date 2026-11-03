"""记忆收尾落库（DL3）：候选 → 去重合并 → 落库 → 下轮可读。

设计真源：``workspace/local_butler/30_架构与设计/12_记忆写入漏斗_DL3实施设计_20261027.md``

四条硬规则（都能被测试抓住）：
1. **唯一漏斗**：本模块不写 ``INSERT INTO facts``，只调 ``MemoryStore.upsert_fact``
   —— 修订史（``fact_revisions``）与写锁自动继承，不用二次实现。
2. **幂等**：同一份候选连交两次，第二次全部 ``duplicate``，``facts`` 行数不变。
3. **不冒充原话**：没有 quote 支撑的候选一律 ``origin='agent_extracted'``。
4. **不静默丢**：每条候选的去向都写进 ``memory_candidates``，被拒的带原因码。
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)

#: 单次收尾最多接受多少条候选（`15` Q10 默认档：10 条，超出去重/拒）
MAX_CANDIDATES = 10

KINDS = frozenset({
    "norm", "fact", "pitfall", "preference", "procedure", "reference", "current_state",
})
ORIGINS = frozenset({"user_quote", "agent_extracted", "system", "tool"})
SENSITIVITIES = frozenset({"public", "private-eng", "personal"})
SPEAKERS = frozenset({"user", "agent_relay", "system"})

#: 拒收码 → 人话（DL5 待审候选直接展示这句）
REASON_TEXT = {
    "empty_content": "正文为空",
    "over_limit": "超出单次 %d 条上限" % MAX_CANDIDATES,
    "unknown_kind": "kind 不在 7 个枚举内",
    "must_read_not_norm": "must_read 只允许 kind='norm'",
    "missing_scope": "没有 scope 且没有默认域",
    "missing_expires_at": "kind='current_state' 必须带 expires_at",
    "sensitivity_invalid": "sensitivity 不在 3 个枚举内",
    "bad_scope": "scope 必须形如 global / project:<id> / topic:<id> / domain:<id>",
}


def norm_text(s: str) -> str:
    """归一化：空白折叠 + 去首尾 + 小写。

    **只做这一层**，不做同义词/近义改写——去重要可复算，猜出来的相似度
    下次换了模型就判得不一样，记忆会悄悄长出重复条目。
    """
    return " ".join((s or "").split()).strip().lower()


def compute_dedup_hash(scope: str | None, kind: str | None, title_or_key: str | None, content: str) -> str:
    """幂等键：sha256(scope|kind|title 或 key|content)，四段都归一化过。"""
    raw = "|".join([
        norm_text(scope or ""),
        norm_text(kind or ""),
        norm_text(title_or_key or ""),
        norm_text(content),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# 记忆库 facts.scope / butler_runs.project_id 的取值。2026-11-04 随项目改名一起迁移
# （原 `localagent`；4 条 facts.scope 与 3 条 butler_runs 已改写，dedup_hash 重算），
# 新写入与存量因此对得上。再改名需同样先迁移数据，不能只改这里。
DEFAULT_PROJECT_ID = "parcae"


def _resolve_scope(scope: str, project_id: str | None) -> str:
    """裸 project 按会话所属项目补全。

    会话码本身绑定了 run→project，模型不该被迫去查合法 id（2026-10-28 用户口径：
    「直接给一个会话码，系统自己去匹配就好」）。只有 project 能补；topic/domain
    没有可推导的来源，仍要求写全 id。
    """
    if scope in ("project", "project:", "project:none", "project:null"):
        pid = (project_id or "").strip() or DEFAULT_PROJECT_ID
        return f"project:{pid}"
    return scope


def _valid_scope(scope: str) -> bool:
    if scope == "global":
        return True
    for prefix in ("project:", "topic:", "domain:"):
        if scope.startswith(prefix) and len(scope) > len(prefix):
            return True
    return False


def _gen_key(kind: str | None, scope: str, seed: str) -> str:
    """无 key 时生成稳定 key：同内容重复提交会得到同一个 key。"""
    digest = hashlib.sha1(norm_text(seed).encode("utf-8")).hexdigest()[:12]
    return "%s:%s:%s" % (kind or "mem", scope, digest)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ingest_quotes(
    store: MemoryStore,
    quotes: list[dict[str, Any]],
    *,
    run_id: str | None,
    project_id: str | None,
) -> tuple[list[int], dict[str, int]]:
    """先落原话（去重靠 quote_hash），返回 (ids, 统计)。"""
    ids: list[int] = []
    stat = {"new": 0, "existing": 0, "invalid": 0}
    for q in quotes:
        if not isinstance(q, dict):
            stat["invalid"] += 1
            continue
        text = str(q.get("text") or "").strip()
        if not text:
            stat["invalid"] += 1
            continue
        speaker = str(q.get("speaker") or "user")
        if speaker not in SPEAKERS:
            speaker = "user"
        qid, is_new = store.insert_source_quote(
            text,
            speaker=speaker,
            project_id=q.get("project_id") or project_id,
            run_id=run_id,
            sensitivity=str(q.get("sensitivity") or "personal"),
            captured_at=q.get("captured_at"),
        )
        ids.append(qid)
        stat["new" if is_new else "existing"] += 1
    return ids, stat


def submit_wrapup(
    store: MemoryStore,
    payload: dict[str, Any],
    *,
    default_scope: str = "global",
    max_candidates: int = MAX_CANDIDATES,
) -> dict[str, Any]:
    """收尾提交的**唯一落库入口**。返回逐条结果，调用方（MCP/HTTP）原样转给 agent。

    payload 形状::

        {
          "run_id": "r-...",
          "project_id": "proj-x",              # 可选，给 quotes 补归属
          "quotes": [{"text": "...", "speaker": "user"}],
          "candidates": [
            {"content": "...", "title": "...", "summary": "...",
             "kind": "fact", "scope": "global", "origin": "user_quote",
             "sensitivity": "personal", "evidence_refs": ["path:12"],
             "source_quote_ids": [1], "quote_text": "...",
             "must_read": 0, "expires_at": null, "key": null}
          ]
        }
    """
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")

    run_id = payload.get("run_id")
    project_id = payload.get("project_id")
    quotes = payload.get("quotes") or []
    cands = payload.get("candidates") or []
    if not isinstance(quotes, list) or not isinstance(cands, list):
        raise ValueError("quotes / candidates 必须是数组")

    quote_ids, quote_stat = _ingest_quotes(store, quotes, run_id=run_id, project_id=project_id)

    results: list[dict[str, Any]] = []
    stat = {"total": len(cands), "accepted": 0, "merged": 0, "duplicate": 0, "rejected": 0}

    def _reject(idx: int, code: str, cand: dict[str, Any], actual: str = "") -> None:
        """记一条拒收。

        **报错只说「判据 + 实际值」，不说该怎么改**（2026-10-28 用户口径）：
        「223 字超 200 字」这种事实陈述就够了，额外给建议等于替 agent 做判断，
        还会诱导它为了过校验改掉本来正确的内容（这次就是把项目内事实改成了 global）。

        同样去重：agent 重试同一份 payload 时，待审列表不该堆出 N 条一样的
        ``unknown_kind``。同内容 + 同原因已有未处理的 rejected 行就复用，不再新插。
        """
        stat["rejected"] += 1
        dedup = None
        content = str(cand.get("content") or "").strip() if isinstance(cand, dict) else ""
        if content:
            dedup = compute_dedup_hash(
                str(cand.get("scope") or default_scope),
                cand.get("kind"),
                cand.get("title") or cand.get("key"),
                content,
            )
        message = REASON_TEXT.get(code, code)
        if actual:
            message = f"{message}；实际 {actual}"
        row = {
            "index": idx,
            "status": "rejected",
            "reason": code,
            "message": message,
        }
        if dedup:
            hit = store.conn.execute(
                "SELECT id FROM memory_candidates "
                "WHERE status = 'rejected' AND reason = ? AND dedup_hash = ? LIMIT 1",
                (code, dedup),
            ).fetchone()
            if hit:
                row["already_pending"] = True
                results.append(row)
                return
        store.record_memory_candidate(
            "rejected", cand, run_id=run_id, reason=code, dedup_hash=dedup
        )
        results.append(row)

    for idx, cand in enumerate(cands):
        if not isinstance(cand, dict):
            _reject(idx, "empty_content", {"raw": str(cand)}, f"第 {idx + 1} 条不是对象（{type(cand).__name__}）")
            continue

        # ① 超限：留档但不落库（第 11 条起）
        if idx >= max_candidates:
            _reject(idx, "over_limit", cand, f"第 {idx + 1} 条，单次上限 {max_candidates} 条")
            continue

        # ② 正文
        content = str(cand.get("content") or "").strip()
        if not content:
            _reject(idx, "empty_content", cand, "content 为空")
            continue

        # ③ kind / sensitivity / scope / must_read / expires_at
        kind = cand.get("kind")
        if kind is not None:
            kind = str(kind)
            if kind not in KINDS:
                _reject(idx, "unknown_kind", cand, f'kind="{kind}"，枚举 {"/".join(sorted(KINDS))}')
                continue

        sensitivity = cand.get("sensitivity")
        if sensitivity is not None:
            sensitivity = str(sensitivity)
            if sensitivity not in SENSITIVITIES:
                _reject(idx, "sensitivity_invalid", cand, f'sensitivity="{sensitivity}"')
                continue

        raw_scope = cand.get("scope") or default_scope
        if not raw_scope:
            _reject(idx, "missing_scope", cand, "scope 与默认域都为空")
            continue
        scope = _resolve_scope(str(raw_scope), project_id)
        if not _valid_scope(scope):
            _reject(idx, "bad_scope", cand, f'scope="{scope}"')
            continue

        must_read = cand.get("must_read")
        must_read_val = 1 if must_read in (1, True, "1") else 0
        if must_read_val == 1 and kind != "norm":
            _reject(idx, "must_read_not_norm", cand, f'kind="{kind}" + must_read=true')
            continue

        expires_at = cand.get("expires_at")
        if kind == "current_state" and not expires_at:
            _reject(idx, "missing_expires_at", cand, 'kind="current_state" 未给 expires_at')
            continue

        # ④ origin：默认不冒充原话；声明了 user_quote 却没证据的降级
        downgraded = False
        origin = cand.get("origin")
        origin = str(origin) if origin is not None else None
        if origin is not None and origin not in ORIGINS:
            origin = None

        ids_here = list(cand.get("source_quote_ids") or [])
        inline_quote = str(cand.get("quote_text") or "").strip()
        if inline_quote:
            qid, _ = store.insert_source_quote(
                inline_quote,
                speaker=str(cand.get("quote_speaker") or "user"),
                project_id=project_id,
                run_id=run_id,
            )
            if qid not in ids_here:
                ids_here.append(qid)
        if not ids_here:
            ids_here = list(quote_ids)  # 整个 run 的原话可以支撑这条

        if origin is None:
            origin = "user_quote" if ids_here else "agent_extracted"
        if origin == "user_quote" and not ids_here:
            origin = "agent_extracted"
            downgraded = True

        # ⑤ 幂等：已有同内容 → duplicate（含已软删的，不复活）
        title = cand.get("title")
        summary = cand.get("summary")
        dedup = compute_dedup_hash(scope, kind, title or cand.get("key"), content)
        hit = store.find_fact_by_dedup(dedup)
        if hit:
            stat["duplicate"] += 1
            store.record_memory_candidate(
                "duplicate", cand, run_id=run_id, fact_key=hit["key"], dedup_hash=dedup
            )
            results.append({
                "index": idx,
                "status": "duplicate",
                "fact_key": hit["key"],
                "archived": bool(hit.get("archived_at")),
            })
            continue

        # ⑥ 落库：给了 key 且已存在 → merged；否则 accepted
        key = str(cand.get("key") or "") or _gen_key(kind, scope, title or summary or content)
        existed = store.get_fact_raw(key) is not None

        store.upsert_fact(
            key,
            content,
            source="auto",
            confidence=float(cand.get("confidence", 0.8)),
            fact_type=cand.get("fact_type"),
            occurred_at=cand.get("occurred_at"),
            consumption_contexts=cand.get("consumption_contexts"),
            trigger_keywords=cand.get("trigger_keywords"),
            title=title,
            summary=summary,
            scope=scope,
            kind=kind,
            origin=origin,
            sensitivity=sensitivity or "personal",
            evidence_refs=cand.get("evidence_refs"),
            source_quote_ids=ids_here or None,
            must_read=must_read_val,
            expires_at=expires_at,
            dedup_hash=dedup,
        )

        status = "merged" if existed else "accepted"
        stat[status] += 1
        store.record_memory_candidate(
            status, cand, run_id=run_id, fact_key=key, dedup_hash=dedup
        )
        row = {
            "index": idx,
            "status": status,
            "fact_key": key,
            "origin": origin,
            "source_quote_ids": ids_here,
        }
        if downgraded:
            row["downgraded_from_user_quote"] = True
        results.append(row)

    out = {
        "run_id": run_id,
        "submitted_at": _now_iso(),
        "quotes": quote_stat,
        "candidates": stat,
        "results": results,
    }
    logger.info(
        "wrapup 落库 run=%s quotes=%s candidates=%s",
        run_id, quote_stat, {k: stat[k] for k in ("accepted", "merged", "duplicate", "rejected")},
    )
    return out


def list_pending_candidates(
    store: MemoryStore, limit: int = 50, run_id: str | None = None
) -> list[dict[str, Any]]:
    """DL5「待审候选」的数据源：被拒的候选原样返回，供人工改后重提。"""
    sql = "SELECT * FROM memory_candidates WHERE status = 'rejected'"
    params: list[Any] = []
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    cur = store.conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    for r in rows:
        try:
            r["payload"] = json.loads(r["payload"])
        except (TypeError, ValueError):
            pass
    return rows
