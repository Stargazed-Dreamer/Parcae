"""记忆指标日聚合 — 在 retention 删掉明细之前，把可量化的东西留下来

动机（2026-10-03 体检实测）：`search_traces` / `evidence_ledger` / `context_projections`
三张明细表各按 30 天硬删，删除前没有任何聚合。于是"记忆库召回质量随时间怎么变"
在代码里根本问不出来——累计写入过 1094 条 trace、10165 行 evidence，实测两张表当前
都是 0 行。本模块把按日可聚合的标量指标固化进 `memory_metrics_daily`，指标种类可以
继续加，不需要再动 schema。

纪律与 `context_projections` 一致：**只做事后度量，不参与 ranking，不产生 authority**。
每个指标都用结构可核实的量（计数、集合大小、覆盖差），不做"这条记忆好不好"的价值判断
——那种判断留给独立判卷人（人工标注或另一次模型评审），不能让被评对象自己出题判卷。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any

# 一次 guide 调用里被判 archive 却仍然高频上屏的"冲突"门槛（次）。
# 含义：这条记忆被归档（v15 前靠 LLM 标签，v15 后靠 facts.archived_at），
# 但它在观测窗口内上屏了这么多次——归档记录和使用互相打脸。
ARCHIVE_CONFLICT_MIN_SURFACES = 20
# 超长条目的展示窗口（字符）。历史上叫「验证窗口」（validator_window_chars），
# 2026-10-31 验证器删除后不再有验证动作，只剩「这条长到一屏塞不下」这个事实，
# 故改名，口径不变（仍取 2400）。
OVERSIZE_CHARS = 2400
# extra 里最多带多少个样本 key（避免日表被长 JSON 撑大）
_MAX_SAMPLE_KEYS = 20


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, sql: str, *args: Any) -> float:
    row = conn.execute(sql, args).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _keys_json(raw: str | None) -> list[str]:
    """解析 selected_keys / dropped_keys 列（JSON 数组字符串，可能为 NULL/坏 JSON）"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(k) for k in data if k]


def put_metric(
    conn: sqlite3.Connection,
    date: str,
    metric: str,
    value: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """写入/覆盖某日某个指标（幂等，维护器一天多轮只留最后一次快照）"""
    conn.execute(
        """INSERT INTO memory_metrics_daily (date, metric, value, extra, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(date, metric) DO UPDATE SET
               value = excluded.value,
               extra = excluded.extra,
               updated_at = excluded.updated_at""",
        (
            date,
            metric,
            value,
            json.dumps(extra, ensure_ascii=False) if extra is not None else None,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def query_metric_history(
    conn: sqlite3.Connection, metric: str, days: int = 30
) -> list[dict[str, Any]]:
    """按日期升序返回某指标最近 N 天的 {date, value}，供趋势与面板消费"""
    rows = conn.execute(
        """SELECT date, value FROM memory_metrics_daily
           WHERE metric = ? ORDER BY date DESC LIMIT ?""",
        (metric, days),
    ).fetchall()
    return [{"date": r[0], "value": r[1]} for r in reversed(rows)]


def compute_metrics(
    conn: sqlite3.Connection,
    *,
    db_path: str | None = None,
) -> dict[str, tuple[float | None, dict[str, Any]]]:
    """计算全部指标，返回 {metric_name: (value, extra)}。

    单项失败不影响其余：失败项不进结果，异常名收集到 `_errors`（调用方负责落盘，
    绝不静默吞掉——否则"指标缺失"会被读成"指标为 0"）。
    """
    out: dict[str, tuple[float | None, dict[str, Any]]] = {}
    errors: list[str] = []

    def _safe(name: str, fn: Any) -> None:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 - 单指标失败必须降级成可见错误，不能带走整轮
            errors.append(f"{name}:{type(e).__name__}")

    facts_total = _count(conn, "SELECT COUNT(*) FROM facts")
    _safe("facts_total", lambda: (facts_total, {}))

    # ── 消费漏斗：候选 → 上屏 / 被砍 / 从未出现 ─────────────────────────
    def _funnel() -> tuple[float | None, dict[str, Any]]:
        surfaced: set[str] = set()
        dropped: set[str] = set()
        calls = 0
        cumulative_dropped = 0
        if _table_exists(conn, "context_projections"):
            for sel_raw, drop_raw in conn.execute(
                "SELECT selected_keys, dropped_keys FROM context_projections"
            ):
                calls += 1
                s = _keys_json(sel_raw)
                d = _keys_json(drop_raw)
                surfaced.update(s)
                dropped.update(d)
                cumulative_dropped += len(d)
        fact_keys = {r[0] for r in conn.execute("SELECT key FROM facts")}
        ever_surfaced = fact_keys & surfaced
        dropped_only = (fact_keys & dropped) - surfaced
        never = fact_keys - surfaced - dropped
        window_days: float | None = None
        if calls and _table_exists(conn, "context_projections"):
            span = conn.execute(
                "SELECT julianday(MAX(ts)) - julianday(MIN(ts)) FROM context_projections"
            ).fetchone()[0]
            window_days = round(float(span), 2) if span is not None else None
        ratio = round(len(ever_surfaced) / facts_total, 4) if facts_total else 0.0
        return ratio, {
            "window_days": window_days,
            "window_note": "观测窗口=当前 context_projections 留存天数，非全生命周期",
            "projection_calls": calls,
            "facts_surfaced": len(ever_surfaced),
            "facts_dropped_only": len(dropped_only),
            "facts_never_surfaced": len(never),
            "cumulative_dropped_keys": cumulative_dropped,
            "surfaced_sample": sorted(ever_surfaced)[:_MAX_SAMPLE_KEYS],
            "never_surfaced_sample": sorted(never)[:_MAX_SAMPLE_KEYS],
        }

    _safe("surfaced_ratio", _funnel)

    def _projection_counts() -> tuple[float | None, dict[str, Any]]:
        if not _table_exists(conn, "context_projections"):
            return 0.0, {"absent": True}
        n = _count(conn, "SELECT COUNT(*) FROM context_projections")
        surfaces = conn.execute(
            "SELECT COUNT(DISTINCT surface) FROM context_projections"
        ).fetchone()[0]
        unjoinable = _count(
            conn, "SELECT COUNT(*) FROM context_projections WHERE trace_id IS NULL"
        )
        return n, {
            "surfaces": surfaces,
            "trace_id_null": unjoinable,
            "trace_join_note": "trace_id 全空 → 投影层与检索层无法 join，被砍条目无法归因到检索",
        }

    _safe("projection_calls", _projection_counts)

    # ── 标签与使用的矛盾 ────────────────────────────────────────────────
    def _archive_conflicts() -> tuple[float | None, dict[str, Any]]:
        if not _table_exists(conn, "context_projections") or not _table_exists(
            conn, "facts"
        ):
            return 0.0, {"absent": True}
        surfaced: dict[str, int] = {}
        for (sel_raw,) in conn.execute("SELECT selected_keys FROM context_projections"):
            for k in _keys_json(sel_raw):
                surfaced[k] = surfaced.get(k, 0) + 1
        archived = {
            r[0]
            for r in conn.execute("SELECT key FROM facts WHERE archived_at IS NOT NULL")
        }
        hits = {k: n for k, n in surfaced.items() if k in archived and n >= ARCHIVE_CONFLICT_MIN_SURFACES}
        return float(len(hits)), {
            "threshold_surfaces": ARCHIVE_CONFLICT_MIN_SURFACES,
            "archived_total": len(archived),
            "samples": sorted(hits.items(), key=lambda x: -x[1])[:_MAX_SAMPLE_KEYS],
        }

    _safe("archive_high_freq_conflicts", _archive_conflicts)

    # ── 归档分布（2026-10-31：口径从 memory_meta.validation_status 改到
    #    facts.archived_at。原因：LLM 验证器删除后 validation_status 不再有生产者，
    #    继续读它会永远冻结在 v15 的历史值上，看不出任何变化）──────────────
    def _archive_counts() -> tuple[float | None, dict[str, Any]]:
        if not _table_exists(conn, "facts"):
            return 0.0, {"absent": True}
        archived_total = _count(
            conn, "SELECT COUNT(*) FROM facts WHERE archived_at IS NOT NULL"
        )
        has_meta = _table_exists(conn, "memory_meta")
        stale = (
            _count(
                conn, "SELECT COUNT(*) FROM memory_meta WHERE stale_score > ?",
                _stale_days_of(conn),
            )
            if has_meta
            else 0.0
        )
        return archived_total, {
            "archived_total": archived_total,
            "stale_total": stale,
            "archive_ratio": round(archived_total / facts_total, 4) if facts_total else 0.0,
            "note": "口径=facts.archived_at；memory_meta.validation_status 自 2026-10-31 起是冻结的历史字段",
        }

    _safe("facts_archived", _archive_counts)

    def _taxonomy_violations() -> tuple[float | None, dict[str, Any]]:
        rows = conn.execute(
            """SELECT fact_type, COUNT(*) FROM facts
               WHERE fact_type IS NULL
                  OR fact_type NOT IN ('user','feedback','project','reference','experience')
               GROUP BY 1"""
        ).fetchall()
        return float(sum(r[1] for r in rows)), {"by_type": dict(rows)}

    _safe("taxonomy_violations", _taxonomy_violations)

    def _oversize_facts() -> tuple[float | None, dict[str, Any]]:
        n = _count(
            conn, "SELECT COUNT(*) FROM facts WHERE LENGTH(value) > ?",
            OVERSIZE_CHARS,
        )
        return n, {"window_chars": OVERSIZE_CHARS}

    # 2026-10-31 从 facts_oversize_for_validator 改名：验证器没了，"for validator"
    # 会让人以为还有验证动作。历史序列在 memory_metrics_daily 里按旧名存着，
    # 新序列自改名日重新起算。
    _safe("facts_oversize", _oversize_facts)

    # ── 索引卫生 ────────────────────────────────────────────────────────
    def _superseded() -> tuple[float | None, dict[str, Any]]:
        per_key = conn.execute(
            """SELECT key, COUNT(*) FROM messages
               WHERE source='manual' AND key IS NOT NULL GROUP BY key HAVING COUNT(*) > 1"""
        ).fetchall()
        churn = {r[0]: r[1] for r in per_key}
        # 除每个 key 的最新一条外，其余都是"已被取代的旧全文副本"
        stale_ids = [
            r[0]
            for r in conn.execute(
                """SELECT m.id FROM messages m
                   WHERE m.source='manual' AND m.key IS NOT NULL
                     AND m.id < (SELECT MAX(m2.id) FROM messages m2
                                 WHERE m2.key = m.key AND m2.source='manual')"""
            )
        ]
        indexed = 0.0
        if stale_ids and _table_exists(conn, "bm25_inverted"):
            marks = ",".join("?" * len(stale_ids))
            indexed = _count(
                conn,
                f"SELECT COUNT(*) FROM bm25_inverted WHERE message_id IN ({marks})",
                *stale_ids,
            )
        top = sorted(churn.items(), key=lambda x: -x[1])[:_MAX_SAMPLE_KEYS]
        return float(len(stale_ids)), {
            "keys_with_multiple_versions": len(churn),
            "max_versions_for_one_key": max(churn.values()) if churn else 0,
            "bm25_term_rows_on_superseded": indexed,
            "top_churn_keys": top,
        }

    _safe("superseded_message_versions", _superseded)

    def _index_coverage() -> tuple[float | None, dict[str, Any]]:
        msgs = _count(conn, "SELECT COUNT(*) FROM messages")
        # 口径（v5 修正）：分子只数**能 JOIN 回 messages 活行**的向量。
        # 旧口径把孤儿算进来，实测虚报 15% 覆盖，按可解析算只有 ~6%——
        # 孤儿行永远解析不到正文，把它们算进"覆盖率"就是给召回质量造假。
        vectors_resolvable = (
            _count(
                conn,
                "SELECT COUNT(*) FROM vector_index v "
                "WHERE v.message_id IN (SELECT id FROM messages)",
            )
            if _table_exists(conn, "vector_index")
            else 0.0
        )
        vectors_total = (
            _count(conn, "SELECT COUNT(*) FROM vector_index")
            if _table_exists(conn, "vector_index")
            else 0.0
        )
        bm25_docs = (
            _count(
                conn,
                "SELECT COUNT(DISTINCT b.message_id) FROM bm25_inverted b "
                "WHERE b.message_id IN (SELECT id FROM messages)",
            )
            if _table_exists(conn, "bm25_inverted")
            else 0.0
        )
        compressed = _count(conn, "SELECT COUNT(*) FROM messages WHERE compressed = 1")
        ratio = round(vectors_resolvable / msgs, 4) if msgs else 0.0
        return ratio, {
            "messages": msgs,
            "vectors_resolvable": vectors_resolvable,
            "vectors_total": vectors_total,
            "bm25_docs_resolvable": bm25_docs,
            "compressed_messages": compressed,
            "coverage_if_counting_orphans": round(vectors_total / msgs, 4) if msgs else 0.0,
            "note": "向量在压缩后即删（recorder._cleanup_old_vectors），BM25 从不删；"
            "两侧语料不同却按各自 max 归一化后加权融合",
        }

    _safe("vector_coverage", _index_coverage)

    # ── 索引卫生：指向已删除消息的孤儿行 ────────────────────────────────
    def _index_orphans() -> tuple[float | None, dict[str, Any]]:
        orphan_vec = (
            _count(
                conn,
                "SELECT COUNT(*) FROM vector_index "
                "WHERE message_id NOT IN (SELECT id FROM messages)",
            )
            if _table_exists(conn, "vector_index")
            else 0.0
        )
        orphan_bm = (
            _count(
                conn,
                "SELECT COUNT(*) FROM bm25_inverted "
                "WHERE message_id NOT IN (SELECT id FROM messages)",
            )
            if _table_exists(conn, "bm25_inverted")
            else 0.0
        )
        days: list[Any] = []
        if orphan_vec and _table_exists(conn, "vector_index"):
            days = [
                [r[0], r[1]]
                for r in conn.execute(
                    """SELECT SUBSTR(created_at, 1, 10) AS d, COUNT(*) FROM vector_index
                       WHERE message_id NOT IN (SELECT id FROM messages)
                       GROUP BY d ORDER BY 2 DESC LIMIT 5"""
                ).fetchall()
            ]
        return orphan_vec + orphan_bm, {
            "vector_orphan_rows": orphan_vec,
            "bm25_orphan_rows": orphan_bm,
            "orphan_vector_days": days,
            # 级联本身是好的（ON DELETE CASCADE + 连接开了 foreign_keys）。
            # 孤儿只可能来自**没开 foreign_keys 的连接**做的批量删除——SQLite 这个
            # pragma 按连接生效、默认关，裸 sqlite3.connect() 的脚本删不出级联。
            "note": "非零=有索引行指向已不存在的 message，召回到它们只能被丢弃；"
            "生产删除路径已实测级联生效，持续增长说明出现了裸连接的批量删除",
        }

    _safe("index_orphan_rows", _index_orphans)

    # ── 检索遥测（明细未被删除前）──────────────────────────────────────
    def _search_telemetry() -> tuple[float | None, dict[str, Any]]:
        if not _table_exists(conn, "search_traces"):
            return 0.0, {"absent": True}
        n = _count(conn, "SELECT COUNT(*) FROM search_traces")
        empty = _count(conn, "SELECT COUNT(*) FROM search_traces WHERE final_count = 0")
        errors_n = _count(conn, "SELECT COUNT(*) FROM search_traces WHERE error IS NOT NULL")
        lat = conn.execute(
            "SELECT latency_ms FROM search_traces ORDER BY latency_ms"
        ).fetchall()
        p95 = None
        if lat:
            idx = max(0, int(round(len(lat) * 0.95)) - 1)
            p95 = float(lat[idx][0])
        evidence = (
            _count(conn, "SELECT COUNT(*) FROM evidence_ledger")
            if _table_exists(conn, "evidence_ledger")
            else 0.0
        )
        return n, {
            "empty_result_ratio": round(empty / n, 4) if n else 0.0,
            "error_count": errors_n,
            "latency_p95_ms": p95,
            "evidence_rows": evidence,
        }

    _safe("searches_traced", _search_telemetry)

    # ── 体积 ────────────────────────────────────────────────────────────
    def _size() -> tuple[float | None, dict[str, Any]]:
        path = db_path
        if not path or not os.path.exists(path):
            return 0.0, {"absent": True}
        size_mb = round(os.path.getsize(path) / 1048576, 2)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        return size_mb, {
            "freelist_mb": round(page_size * freelist / 1048576, 2),
            "note": "freelist 只增不减：无 VACUUM 调度，删除不回收文件体积",
        }

    _safe("db_size_mb", _size)

    if errors:
        out["_errors"] = (float(len(errors)), {"errors": errors})
    return out


def _stale_days_of(conn: sqlite3.Connection) -> float:
    """memory_meta.stale_score 的天数口径：schema 里没有常量表存这个值，
    用当前配置的 maintain_stale_days（默认 7）。延迟导入避免循环依赖。"""
    try:
        from server.memory.config import get_memory_config

        return float(get_memory_config().maintain_stale_days)
    except Exception:  # noqa: BLE001 - 拿不到配置时退回保守值
        return 7.0


def record_daily(
    conn: sqlite3.Connection,
    *,
    db_path: str | None = None,
    write_lock: Any | None = None,
) -> dict[str, Any]:
    """计算并 upsert 当天全部指标。返回 {date, written, errors}。

    调用点必须在维护器的 retention 清理**之前**，否则每天先删后算，指标只剩空表。
    """
    metrics = compute_metrics(conn, db_path=db_path)
    date = datetime.now().strftime("%Y-%m-%d")

    def _write() -> None:
        for name, (value, extra) in metrics.items():
            put_metric(conn, date, name, value, extra)
        conn.commit()

    if write_lock is not None:
        with write_lock:
            _write()
    else:
        _write()

    err_entry = metrics.get("_errors")
    errors: list[str] = err_entry[1].get("errors", []) if err_entry else []  # type: ignore[assignment]
    return {
        "date": date,
        "written": len(metrics),
        "errors": errors,
    }
