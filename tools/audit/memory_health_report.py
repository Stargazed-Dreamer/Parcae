"""记忆库体检报告（只读，可重复跑）

回答四件事：**产出有没有人消费、标签和使用是否自相矛盾、索引在不在烂、
上一轮修的东西有没有留下证据**。

设计纪律（2026-10-03 首轮体检教训）：
- **不自出题自己判卷**。行为类指标（消费漏斗、归档率、召回精度）只报数并给趋势，
  不设红黄线——没有基线就不设阈值。攒够 ≥14 天日表后，再按实测分布定线。
- 只有**结构上无争议**的缺陷才判色：指标算不出来、遥测被删光却没人聚合、
  旧版本全文仍在索引、schema 越界、投影层与检索层无法 join。
- 每个数字都实测（数表、读 sqlite_sequence），不抄文档、不抄上一版报告。

指标计算复用 `server/memory/metrics.py`，与后端维护器同一份实现，
避免"检查器自己也会造假失实"——两处算法漂移比没有算法更糟。

用法: uv run python tools/audit/memory_health_report.py [--json] [--days 14]
输出: 控制台摘要 + temp/audit/memory_audit.md
退出码: 有 red 项时 1
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REPORT_PATH = ROOT / "temp" / "audit" / "memory_audit.md"
LINES: list[str] = []
SEVERITIES: list[tuple[str, str]] = []  # (section, severity)

Snapshot = dict[str, tuple[float | None, dict[str, Any]]]


def emit(section: str, severity: str, msg: str) -> None:
    SEVERITIES.append((section, severity))
    LINES.append(f"- **[{severity}]** {msg}")


def note_line(text: str) -> None:
    LINES.append(f"- {text}")


def val(snap: Snapshot, name: str) -> float:
    entry = snap.get(name)
    if entry is None or entry[0] is None:
        return 0.0
    return float(entry[0])


def extra_of(snap: Snapshot, name: str) -> dict[str, Any]:
    entry = snap.get(name)
    if entry is None or entry[1] is None:
        return {}
    return entry[1]


def pct(part: float, whole: float) -> str:
    return f"{part:.0f}/{whole:.0f}（{100.0 * part / whole:.0f}%）" if whole else f"{part:.0f}/0"


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def seq_of(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (name,)).fetchone()
    return int(row[0]) if row else 0


def resolve_db_path() -> Path:
    from server.memory.config import get_memory_config

    cfg = get_memory_config()
    p = Path(cfg.db_path)
    return p if p.is_absolute() else ROOT / p


def section_scale(conn: sqlite3.Connection, snap: Snapshot) -> None:
    LINES.append("\n## A. 规模快照")
    cov = extra_of(snap, "vector_coverage")
    size_x = extra_of(snap, "db_size_mb")
    summaries = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    bm25_rows = conn.execute("SELECT COUNT(*) FROM bm25_inverted").fetchone()[0]
    by_source = dict(conn.execute("SELECT source, COUNT(*) FROM messages GROUP BY 1").fetchall())
    msgs = cov.get("messages", 0)
    note_line(f"facts（结构化记忆）: {val(snap, 'facts_total'):.0f}")
    note_line(f"messages: {msgs}｜按来源 {by_source}"
              f"｜其中已压缩 {cov.get('compressed_messages')}")
    note_line(f"summaries: {summaries}｜bm25 倒排行: {bm25_rows}｜向量: {cov.get('vectors')}")
    note_line(f"DB 体积: {val(snap, 'db_size_mb'):.2f} MB"
              f"（其中 freelist 可回收 {size_x.get('freelist_mb')} MB，无 VACUUM 调度）")

    errs = extra_of(snap, "_errors").get("errors") or []
    if errs:
        emit("A", "red", f"指标计算失败 {len(errs)} 项，本报告有盲区: {errs}")
    else:
        emit("A", "ok", "全部指标计算成功（无盲区）")


def section_funnel(conn: sqlite3.Connection, snap: Snapshot) -> None:
    LINES.append("\n## B. 消费漏斗 — 候选 → 上屏 / 被砍 / 从未出现")
    fun = extra_of(snap, "surfaced_ratio")
    if not fun.get("projection_calls"):
        emit("B", "yellow", "context_projections 为空：没有任何一次 guide 注入留痕可供分析")
        return
    note_line(
        f"观测窗口 {fun.get('window_days')} 天、{fun.get('projection_calls')} 次注入"
        f"（窗口=投影表留存期，**不是全生命周期**，勿读成\"这些记忆永远不会被用\"）"
    )
    note_line(
        f"上过屏 {fun.get('facts_surfaced')}｜只被召回后裁掉 {fun.get('facts_dropped_only')}"
        f"｜从未进过候选 {fun.get('facts_never_surfaced')}"
        f"｜累计被砍次数 {fun.get('cumulative_dropped_keys')}"
    )
    note_line(f"surfaced_ratio = {val(snap, 'surfaced_ratio')}")
    note_line("*本节不判色：无基线不设阈值，攒够 14 天日表后按实测分布定线。*")


def section_labels(conn: sqlite3.Connection, snap: Snapshot) -> None:
    LINES.append("\n## C. 维护器标签 vs 实际使用")
    facts_total = val(snap, "facts_total")
    vc = extra_of(snap, "facts_archived")
    note_line(f"validation_status 分布: {vc.get('status_counts')}")
    note_line(f"archive 占比 = {pct(val(snap, 'facts_archived'), facts_total)}")

    conflicts = val(snap, "archive_high_freq_conflicts")
    c_x = extra_of(snap, "archive_high_freq_conflicts")
    if conflicts:
        emit("C", "red",
             f"{conflicts:.0f} 条被判 archive 却在窗口内上屏 ≥"
             f"{c_x.get('threshold_surfaces')} 次（标签与使用互相打脸）。"
             f"样本: {c_x.get('samples')}")
    else:
        emit("C", "ok", "没有出现\"判死却天天上屏\"的冲突")

    wildcard = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE consumption_contexts LIKE '%\"*\"%'"
    ).fetchone()[0]
    note_line(
        f"consumption_contexts 含 \"*\"（对所有任务永远相关）: {pct(wildcard, facts_total)}"
        "｜必读块通配名额见 agent_guide._MUST_READ_WILDCARD_QUOTA"
    )

    tax = val(snap, "taxonomy_violations")
    if tax:
        emit("C", "yellow",
             f"{tax:.0f} 条 fact_type 越出 closed taxonomy: "
             f"{extra_of(snap, 'taxonomy_violations').get('by_type')}"
             "（写入端不拒绝，账上留着脏值）")
    else:
        emit("C", "ok", "fact_type 全部落在 closed taxonomy 内")


def section_index_hygiene(conn: sqlite3.Connection, snap: Snapshot, preview_chars: int) -> None:
    LINES.append("\n## D. 索引卫生")
    sup = val(snap, "superseded_message_versions")
    sup_x = extra_of(snap, "superseded_message_versions")
    sup_indexed = sup_x.get("bm25_term_rows_on_superseded") or 0
    if sup and sup_indexed:
        emit("D", "yellow",
             f"{sup:.0f} 份\"已被取代的旧全文副本\"中仍有 "
             f"{sup_indexed} 行被 BM25 索引（旧内容仍可被召回，与当前真相混在同一召回通道）；"
             f"最夸张的 key 有 {sup_x.get('max_versions_for_one_key')} 个版本"
             f"（top: {(sup_x.get('top_churn_keys') or [])[:5]}）")
    elif sup:
        # v6（P2 版本模型）起，旧副本的 messages 行**刻意保留** —— 它们是
        # memory_metrics_daily.superseded_message_versions 的算法输入，也是"这个 key
        # 以前活过什么"的行级溯源（"失效 ≠ 消失"，ADR-0040 同源纪律）。
        # 因此判据必须是"旧内容还能不能被召回"，而不是"旧行在不在"：
        # 只要不再被索引就是设计内状态，不判色（否则每次体检都会误报）。
        note_line(
            f"{sup:.0f} 份已被取代的旧副本以历史形态保留（v6 fact_revisions / messages 行），"
            f"其中 0 行被 BM25 索引 —— 旧内容已不可召回，属设计内状态"
        )
        note_line(
            f"最夸张的 key 有 {sup_x.get('max_versions_for_one_key')} 个版本"
            f"（top: {(sup_x.get('top_churn_keys') or [])[:5]}）"
        )
    else:
        emit("D", "ok", "没有 superseded 版本残留")

    cov = extra_of(snap, "vector_coverage")
    vectors = cov.get("vectors_resolvable", 0)
    bm25_docs = cov.get("bm25_docs_resolvable", 0)
    msgs = cov.get("messages", 0)
    note_line(f"向量覆盖（只数能解析回活行的）{pct(vectors, msgs)}"
              f"｜BM25 文档覆盖 {pct(bm25_docs, msgs)}"
              f"｜把孤儿算进去的旧口径会是 {pct(cov.get('vectors_total', 0), msgs)}")
    if bm25_docs and vectors < bm25_docs * 0.5:
        emit("D", "yellow",
             f"向量比 BM25 少 {bm25_docs / max(vectors, 1):.1f} 倍——压缩即删向量、BM25 从不删"
             "（recorder._cleanup_old_vectors），而混合检索把两路各自按本次 max 归一化后加权，"
             "等于拿两个不同语料的分数相加")

    orphans = val(snap, "index_orphan_rows")
    orp_x = extra_of(snap, "index_orphan_rows")
    if orphans and orphans > 0:
        emit("D", "red",
             f"{orphans:.0f} 行索引指向**已不存在的 message**"
             f"（向量 {orp_x.get('vector_orphan_rows'):.0f}／BM25 {orp_x.get('bm25_orphan_rows'):.0f}）"
             f"——召回到它们只能被丢弃，等于白占名额；产生日分布 {orp_x.get('orphan_vector_days')}"
             "。生产删除路径的级联已实测生效，持续增长=出现了裸连接（未开 foreign_keys）的批量删除")
    else:
        emit("D", "ok", "索引里没有指向已删除消息的孤儿行")

    oversize = val(snap, "facts_oversize_for_validator")
    note_line(
        f"正文超过验证窗口（{preview_chars} 字）的 fact: "
        f"{pct(oversize, val(snap, 'facts_total'))}——这些走\"archive 延判\"路径"
    )


def section_telemetry(conn: sqlite3.Connection, has_metrics: bool, metric_days: int) -> None:
    LINES.append("\n## E. 遥测持久性（先聚合，再被 retention 删）")
    if not has_metrics:
        emit("E", "red",
             "memory_metrics_daily 不存在：schema v5 迁移还没跑（后端重启后自动建），"
             "目前无法积累趋势")
    else:
        note_line(f"日表已覆盖 {metric_days} 天")
        if metric_days == 0:
            emit("E", "yellow", "日表已建但一行都没有：维护器尚未跑过 record_daily")

    traces_seq, ev_seq = seq_of(conn, "search_traces"), seq_of(conn, "evidence_ledger")
    traces_now = conn.execute("SELECT COUNT(*) FROM search_traces").fetchone()[0]
    ev_now = conn.execute("SELECT COUNT(*) FROM evidence_ledger").fetchone()[0]
    note_line(
        f"search_traces 累计写入 {traces_seq} / 现存 {traces_now}"
        f"｜evidence_ledger 累计 {ev_seq} / 现存 {ev_now}"
    )
    purged = traces_seq > traces_now or ev_seq > ev_now
    if purged and not has_metrics:
        emit("E", "red", "明细表已被 retention 删过，但日表不在：这些召回遥测已永久丢失")
    elif purged:
        note_line("差额来自 30 天 retention 硬删；聚合已在删除前完成，历史留在日表")

    proj_seq = seq_of(conn, "context_projections")
    if proj_seq:
        nulls = conn.execute(
            "SELECT COUNT(*) FROM context_projections WHERE trace_id IS NULL"
        ).fetchone()[0]
        if nulls == proj_seq:
            emit("E", "yellow",
                 f"context_projections 的 trace_id 全为 NULL（{nulls} 行）："
                 "投影层与检索层无法 join，\"召回了但没上屏\"归因不到检索环节")


def section_trends(conn: sqlite3.Connection, has_metrics: bool, days: int) -> None:
    from server.memory.metrics import query_metric_history

    LINES.append(f"\n## F. 趋势（回看 {days} 天）")
    if not has_metrics:
        note_line("日表缺失，无趋势")
        return
    for metric in (
        "db_size_mb", "surfaced_ratio", "facts_oversize_for_validator",
        "archive_high_freq_conflicts", "projection_calls",
        "superseded_message_versions",
    ):
        hist = query_metric_history(conn, metric, days=days)
        vals = [h["value"] for h in hist if h["value"] is not None]
        if len(vals) < 2:
            note_line(f"{metric}: 采样 {len(vals)} 天，暂不足以看趋势")
            continue
        delta = vals[-1] - vals[0]
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        note_line(f"{metric} {arrow} {vals[0]} → {vals[-1]}（{len(vals)} 个采样）")


def section_open_questions() -> None:
    LINES.append("\n## G. 本轮只报数、留给你的判断")
    note_line("召回精度（前 5 条到底有没有用）需要一批人工标注的真实 guide 调用；"
              "`tests/guide_eval` 只测 skill 路由且 stub 掉 embedding，测不到这个")
    note_line("facts 与 KV(messages) 双写同 key：目前靠 `manager.get` 的\"facts 优先\"兜住，"
              "但两份内容会各自漂移")
    note_line("L1 Recent 层（recent.py 的 get_recent / get_context_for_prompt / search_recent）"
              "没有生产调用方，文档仍称\"三层\"之一")
    note_line("`/memory/cleanup/orphaned` 无调度器调用（端点在，没人按周期按）")


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆库体检（只读）")
    ap.add_argument("--json", action="store_true", help="额外打印指标快照 JSON")
    ap.add_argument("--days", type=int, default=14, help="趋势回看天数（默认 14）")
    args = ap.parse_args()

    db = resolve_db_path()
    if not db.exists():
        print(f"[red] 记忆库不存在: {db}")
        return 1

    from server.memory.config import get_memory_config
    from server.memory.metrics import compute_metrics

    cfg = get_memory_config()
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    snap: Snapshot = compute_metrics(
        conn, db_path=str(db), validator_window_chars=cfg.validate_preview_chars
    )

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    ver_row = conn.execute(
        "SELECT value FROM schema_info WHERE key='version'"
    ).fetchone()
    schema_ver = ver_row[0] if ver_row else "无 version 记录"
    LINES.append(f"# 记忆库体检报告 · {ts}")
    LINES.append("")
    LINES.append(f"- 库文件: `{db}`")
    LINES.append(f"- 库内 schema 版本: {schema_ver}（代码版本见 `server/memory/schema.py:SCHEMA_VERSION`）")
    LINES.append(f"- 验证窗口: {cfg.validate_preview_chars} 字｜单轮验证上限: "
                 f"{cfg.validate_batch_limit} 条｜重验间隔: {cfg.validate_revalidate_days} 天")

    has_metrics = table_exists(conn, "memory_metrics_daily")
    metric_days = 0
    if has_metrics:
        metric_days = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM memory_metrics_daily"
        ).fetchone()[0]

    section_scale(conn, snap)
    section_funnel(conn, snap)
    section_labels(conn, snap)
    section_index_hygiene(conn, snap, cfg.validate_preview_chars)
    section_telemetry(conn, has_metrics, metric_days)
    section_trends(conn, has_metrics, args.days)
    section_open_questions()

    reds = sum(1 for _s, sev in SEVERITIES if sev == "red")
    yellows = sum(1 for _s, sev in SEVERITIES if sev == "yellow")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(LINES) + "\n", encoding="utf-8")

    print(f"记忆库体检：{len(SEVERITIES)} 项判定｜red={reds} yellow={yellows}")
    for section, sev in SEVERITIES:
        if sev in ("red", "yellow"):
            print(f"  [{sev}] {section}")
    print(f"报告: {REPORT_PATH}")

    if args.json:
        print(json.dumps({k: v[0] for k, v in snap.items()}, ensure_ascii=False, indent=2))

    conn.close()
    return 1 if reds else 0


if __name__ == "__main__":
    sys.exit(main())
