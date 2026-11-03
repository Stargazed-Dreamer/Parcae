"""检索通道 golden query 评测（Z3 口径）

判两条通道，不判 summaries、不判端到端融合排序：
- messages：BM25（与生产默认一致，排除 source='tool'）
- facts：`MemoryManager._search_facts` 的 SQL 词项召回 + 覆盖率排序

golden query 来源见 `tests/memory_eval/search_gold.jsonl` 的 `query_style`：
- `trace`：历史 `search_traces` 里真实发出过的查询
- `contract`：按 fact 自己声明的 trigger_keywords 构造，测"声明了触发词就必须能被召回"

gold 是 fact key（可机械核对），不是主观相关度标注；因此本脚本不声称测的是
"语义相关性"，只测"点名目标能不能被这条通道召回"。

只读：先把生产库用 SQLite backup API 复制到 temp/，所有查询打在副本上。
输出：temp/audit/search_eval.md（默认）+ 可选 JSON。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.memory.bm25 import BM25Index  # noqa: E402
from server.memory.manager import MemoryManager  # noqa: E402
from server.memory.store import MemoryStore  # noqa: E402

DEFAULT_GOLD = ROOT / "tests" / "memory_eval" / "search_gold.jsonl"
DEFAULT_DB = ROOT / "data" / "memory" / "memory.db"
DEFAULT_OUT = ROOT / "temp" / "audit" / "search_eval.md"
TMP_DB = ROOT / "temp" / "audit" / "search_eval.db"
K = 10


@dataclass
class RowResult:
    id: str
    query: str
    gold_keys: list[str]
    query_style: str
    fact_rank: int | None
    fact_top: list[str]
    msg_rank: int | None
    msg_top: list[str]
    gold_has_messages: bool


def load_gold(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def validate(rows: list[dict], active_keys: set[str]) -> list[str]:
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    for r in rows:
        rid = str(r.get("id") or "")
        if not rid or not str(r.get("query") or "").strip():
            problems.append(f"缺 id 或 query: {r}")
        if rid in seen_ids:
            problems.append(f"重复 id: {rid}")
        seen_ids.add(rid)
        q = str(r.get("query") or "")
        if q in seen_queries:
            problems.append(f"重复 query: {q}")
        seen_queries.add(q)
        keys = r.get("gold_keys") or []
        if not keys:
            problems.append(f"{rid} 没有 gold_keys")
        for k in keys:
            if k not in active_keys:
                problems.append(f"{rid} 的 gold key 不在活跃 facts 里: {k}")
        if r.get("query_style") not in {"trace", "contract"}:
            problems.append(f"{rid} 的 query_style 非法: {r.get('query_style')}")
    if not 30 <= len(rows) <= 50:
        problems.append(f"样本数 {len(rows)} 不在 30-50 之间")
    return problems


def copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def rank_of(haystack: list[str], golds: list[str]) -> int | None:
    for i, key in enumerate(haystack, 1):
        if key in golds:
            return i
    return None


def run(rows: list[dict], store: MemoryStore, bm25: BM25Index) -> list[RowResult]:
    gold_msg_keys = {
        str(k)
        for r in rows
        for k in (r.get("gold_keys") or [])
    }
    has_msg: dict[str, bool] = {
        k: bool(
            store.conn.execute(
                "SELECT 1 FROM messages WHERE key = ? AND source <> 'tool' LIMIT 1", (k,)
            ).fetchone()
        )
        for k in gold_msg_keys
    }

    stub = type("FactSearchStub", (), {"store": store})()
    results: list[RowResult] = []
    for r in rows:
        golds = [str(k) for k in r["gold_keys"]]
        facts = MemoryManager._search_facts(stub, str(r["query"]), top_k=K)  # type: ignore[arg-type]
        fact_keys = [str(f.get("key")) for f in facts]

        msgs = bm25.search(str(r["query"]), top_k=K, exclude_sources={"tool"})
        msg_ids = [int(m["message_id"]) for m in msgs]
        msg_keys: list[str] = []
        if msg_ids:
            placeholders = ",".join("?" * len(msg_ids))
            key_by_id = {
                int(row[0]): str(row[1] or "")
                for row in store.conn.execute(
                    f"SELECT id, key FROM messages WHERE id IN ({placeholders})", msg_ids
                )
            }
            msg_keys = [key_by_id.get(mid, "") for mid in msg_ids]

        results.append(
            RowResult(
                id=str(r["id"]),
                query=str(r["query"]),
                gold_keys=golds,
                query_style=str(r["query_style"]),
                fact_rank=rank_of(fact_keys, golds),
                fact_top=fact_keys[:3],
                msg_rank=rank_of(msg_keys, golds),
                msg_top=msg_keys[:3],
                gold_has_messages=any(has_msg.get(k, False) for k in golds),
            )
        )
    return results


def _rate(results: list[RowResult], field: str, k: int, *, only_measurable: bool = False) -> tuple[int, int]:
    subset = [r for r in results if (r.gold_has_messages or not only_measurable)]
    ranks = [getattr(r, field) for r in subset]
    return sum(1 for x in ranks if x is not None and x <= k), len(subset)


def _mrr(results: list[RowResult], field: str, *, only_measurable: bool = False) -> float:
    subset = [r for r in results if (r.gold_has_messages or not only_measurable)]
    if not subset:
        return 0.0
    vals = [1.0 / getattr(r, field) for r in subset if getattr(r, field) is not None]
    return sum(vals) / len(subset)


def render(results: list[RowResult], gold_path: Path) -> str:
    n = len(results)
    trace = [r for r in results if r.query_style == "trace"]
    contract = [r for r in results if r.query_style == "contract"]
    lines = [
        "# 检索通道 golden query 基线",
        "",
        "> 口径（Z3=②）：messages 判 BM25（排除 `source='tool'`），facts 判 SQL 词项召回 + 覆盖率排序；",
        "> **不判 summaries、不判端到端融合排序**。gold 是 fact key，只回答“点名目标能不能被召回”，",
        "> 不测“语义相似但用词不同”的能力（96.5% 语料没有向量，这条能力现在测不了）。",
        f"> gold: `{gold_path.relative_to(ROOT)}`；样本 {n} 条"
        f"（真实检索留痕 {len(trace)} / 声明触发词契约 {len(contract)}）。",
        "",
        "## 总览",
        "",
        "| 通道 | hit@1 | hit@3 | hit@5 | hit@10 | MRR@10 |",
        "|---|---|---|---|---|---|",
    ]
    measurable = [r for r in results if r.gold_has_messages]
    for label, field in (("facts（词项覆盖率）", "fact_rank"), ("messages（BM25）", "msg_rank")):
        only = field == "msg_rank"
        counts = [_rate(results, field, k, only_measurable=only) for k in (1, 3, 5, 10)]
        cells = " | ".join(f"{v}/{m} ({v / m:.0%})" if m else "n/a" for v, m in counts)
        lines.append(f"| {label} | {cells} | {_mrr(results, field, only_measurable=only):.3f} |")

    lines += ["", "## 分 query_style", "", "| 风格 | 条数 | facts hit@5 | messages hit@5（可判） |", "|---|---|---|---|"]
    for label, subset in (("trace", trace), ("contract", contract)):
        f_hit, f_n = _rate(subset, "fact_rank", 5)
        m_hit, m_n = _rate(subset, "msg_rank", 5, only_measurable=True)
        lines.append(f"| {label} | {len(subset)} | {f_hit}/{f_n} | {m_hit}/{m_n} |")
    if measurable:
        lines.append("")
        lines.append(f"> messages 的分母 = gold 在消息侧真有对应 key 的 {len(measurable)} 条；"
                     f"另有 {len(results) - len(measurable)} 条结构上无法在消息通道命中，单列不计分。")

    lines += ["", "## hit@5 未命中清单", ""]
    misses = [
        r for r in results
        if r.fact_rank is None or r.fact_rank > 5
        or (r.gold_has_messages and (r.msg_rank is None or r.msg_rank > 5))
    ]
    if not misses:
        lines.append("（无）")
    for r in misses:
        flags = []
        if r.fact_rank is None or r.fact_rank > 5:
            flags.append(f"facts 未进前5（实际 rank={r.fact_rank or '无'}）")
        if r.gold_has_messages and (r.msg_rank is None or r.msg_rank > 5):
            flags.append(f"messages 未进前5（实际 rank={r.msg_rank or '无'}）")
        lines.append(f"- `{r.id}` {r.query} ｜ {' / '.join(flags)} ｜ gold: {', '.join(r.gold_keys)}")
        lines.append(f"  - facts top3: {r.fact_top or '（空）'}")
        lines.append(f"  - messages top3 keys: {r.msg_top or '（空）'}")

    lines += [
        "",
        "## 读法与限制",
        "",
        "- 这是**机制基线**，不是用户满意度；阈值等至少 14 天重跑趋势再定。",
        "- `contract` 条目按 fact 自己声明的 `trigger_keywords` 构造，属“自证契约”：命中说明声明有效，",
        "  不命中说明声明与索引脱节（facts 检索只查 key/value，不索引 trigger_keywords）。",
        "- 未命中不代表该 query 在端到端里没用：EvidenceLedger 还会混入 messages/summaries；这里只隔离判单通道。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(DEFAULT_GOLD))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--json", action="store_true", help="同时写 JSON 到同名 .json")
    args = ap.parse_args()

    gold_path = Path(args.gold)
    rows = load_gold(gold_path)
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB 不存在: {db_path}", file=sys.stderr)
        return 2

    copy_db(db_path, TMP_DB)
    store = MemoryStore(str(TMP_DB))
    store.initialize()
    try:
        active_keys = {
            str(r[0]) for r in store.conn.execute("SELECT key FROM facts WHERE archived_at IS NULL")
        }
        problems = validate(rows, active_keys)
        if problems:
            print("gold 校验失败：", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 2

        bm25 = BM25Index(store)
        results = run(rows, store, bm25)
    finally:
        store.conn.close()
        TMP_DB.unlink(missing_ok=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(results, gold_path), encoding="utf-8")
    if args.json:
        payload = {
            "gold": str(gold_path),
            "n": len(results),
            "facts_hit@5": _rate(results, "fact_rank", 5),
            "messages_hit@5": _rate(results, "msg_rank", 5, only_measurable=True),
            "messages_measurable": sum(1 for r in results if r.gold_has_messages),
            "facts_mrr@10": round(_mrr(results, "fact_rank"), 4),
            "messages_mrr@10": round(_mrr(results, "msg_rank", only_measurable=True), 4),
            "rows": [asdict(r) for r in results],
        }
        out.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    f_hit, f_n = _rate(results, "fact_rank", 5)
    m_hit, m_n = _rate(results, "msg_rank", 5, only_measurable=True)
    print(f"golden {len(results)} 条 ｜ facts hit@5 {f_hit}/{f_n} ｜ messages hit@5 {m_hit}/{m_n}（可判）")
    print(f"报告: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
