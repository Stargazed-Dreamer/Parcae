"""A 通道召回评测：人工 gold × context_projections.selected_keys。

判的是「task_type → 必读块」这条注入通道：标注为必须/有用的记忆，是否曾经被
选进过必读块；标注为无关的，是否仍在被注入。

用法:
    uv run python tools/audit/recall_eval.py
    uv run python tools/audit/recall_eval.py --gold tests/memory_eval/gold.jsonl --json
输出: temp/audit/recall_eval.md（默认） + 可选 JSON。

⚠ focus20 是定向样本（按行为信号挑的），率不能外推为全库比例；
要外推得另做随机抽样。这里只回答"这些点名样本是否被系统选中"。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_GOLD = ROOT / "tests" / "memory_eval" / "gold.jsonl"
DEFAULT_DB = ROOT / "data" / "memory" / "memory.db"
DEFAULT_OUT = ROOT / "temp" / "audit" / "recall_eval.md"
DEFAULT_JSON = ROOT / "temp" / "audit" / "recall_eval.json"

LABEL_ORDER = {"must": 0, "useful": 1, "noise": 2}
LABEL_CN = {"must": "必须", "useful": "有用", "noise": "无关"}


def resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    from server.memory.config import get_memory_config

    p = Path(get_memory_config().db_path)
    return p if p.is_absolute() else ROOT / p


def load_gold(path: Path) -> dict[tuple[str, str], dict]:
    """同一 (task_type, key) 以最后一行为准；`unset` 撤销。"""
    out: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            k = (str(r["task_type"]), str(r["key"]))
            if str(r["label"]) == "unset":
                out.pop(k, None)
            else:
                out[k] = r
        except Exception:
            continue
    return out


def load_projections(db: Path) -> tuple[dict[str, set[str]], dict[str, dict]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    selected: dict[str, set[str]] = defaultdict(set)
    stats: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "sel": Counter(), "rank": Counter()}
    )
    try:
        for tt, sk, rb in con.execute(
            "SELECT task_type, selected_keys, rank_before FROM context_projections "
            "WHERE task_type IS NOT NULL"
        ):
            try:
                sel = json.loads(sk or "[]")
                ranked = json.loads(rb or "[]")
            except Exception:
                sel, ranked = [], []
            selected[tt].update(str(x) for x in sel)
            s = stats[tt]
            s["n"] += 1
            s["sel"].update(str(x) for x in sel)
            s["rank"].update(str(x) for x in ranked)
    finally:
        con.close()
    return selected, stats


def build_summary(gold: dict[tuple[str, str], dict],
                  selected: dict[str, set[str]],
                  stats: dict[str, dict]) -> dict:
    items = []
    for (tt, key), row in gold.items():
        s = stats.get(tt, {"n": 0, "sel": Counter(), "rank": Counter()})
        items.append({
            "task_type": tt, "key": key, "label": str(row["label"]),
            "confidence": row.get("confidence", ""), "batch": row.get("batch", ""),
            "surfaced": key in selected.get(tt, set()),
            "sel_count": s["sel"].get(key, 0), "rank_count": s["rank"].get(key, 0),
            "proj_n": s["n"],
        })
    items.sort(key=lambda r: (LABEL_ORDER.get(r["label"], 9), r["task_type"], r["key"]))

    by_label: dict[str, dict] = {}
    for label in ("must", "useful", "noise"):
        group = [r for r in items if r["label"] == label]
        if not group:
            continue
        surfaced = sum(1 for r in group if r["surfaced"])
        by_label[label] = {
            "n": len(group), "surfaced": surfaced, "never": len(group) - surfaced,
            "surfaced_rate": round(surfaced / len(group), 4),
        }

    per_task: dict[str, dict] = {}
    for r in items:
        t = per_task.setdefault(r["task_type"], {"n": 0, "must_missed": 0, "noise_surfaced": 0})
        t["n"] += 1
        if r["label"] == "must" and not r["surfaced"]:
            t["must_missed"] += 1
        if r["label"] == "noise" and r["surfaced"]:
            t["noise_surfaced"] += 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n": len(items), "by_label": by_label,
        "per_task_type": per_task, "items": items,
    }


def render_md(summary: dict, gold_path: Path) -> str:
    lines = [
        "# A 通道召回评测（gold × 必读块）",
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 判卷集：`{gold_path}`（{summary['n']} 条）",
        "- ⚠ 定向样本，率不能外推为全库比例；只回答这些点名条目的上屏情况。",
        "",
        "## 1. 混淆矩阵",
        "",
        "| 档位 | 条数 | 曾上屏 | 从未上屏 | 上屏率 |",
        "|---|---|---|---|---|",
    ]
    for label in ("must", "useful", "noise"):
        b = summary["by_label"].get(label)
        if b:
            lines.append(
                f"| {LABEL_CN[label]} | {b['n']} | {b['surfaced']} | {b['never']} | "
                f"{b['surfaced_rate']:.0%} |"
            )
    lines += [
        "",
        "## 2. 按 task_type",
        "",
        "| task_type | 标注数 | 必须漏检 | 无关上屏 |",
        "|---|---|---|---|",
    ]
    for tt, t in sorted(summary["per_task_type"].items()):
        lines.append(f"| `{tt}` | {t['n']} | {t['must_missed']} | {t['noise_surfaced']} |")
    lines += [
        "",
        "## 3. 逐条",
        "",
        "| # | 档位 | task_type | key | 曾上屏 | 选中/投影 | 候选/投影 | 信心 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(summary["items"], 1):
        lines.append(
            f"| {i} | {LABEL_CN.get(r['label'], r['label'])} | `{r['task_type']}` | "
            f"`{r['key']}` | {'是' if r['surfaced'] else '否'} | "
            f"{r['sel_count']}/{r['proj_n']} | {r['rank_count']}/{r['proj_n']} | "
            f"{r['confidence']} |"
        )
    lines += [
        "",
        "## 4. 读法",
        "",
        "- **必须漏检**：标了必须、但 `selected_keys` 从未出现——真漏读。",
        "- **无关上屏**：标了无关、却仍在必读块里——真噪声。",
        "- 候选/投影 = 进入 `rank_before` 的次数；选中/投影 = 真正上屏的次数。",
        "- 置信度 `user` 表示用户部分凭名字判断；`confirmed_after_review` 是复核过的。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="A 通道召回评测")
    ap.add_argument("--gold", default=str(DEFAULT_GOLD))
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--json", action="store_true", help="同时写 JSON 到 temp/audit/recall_eval.json")
    args = ap.parse_args()

    gold_path = Path(args.gold)
    gold = load_gold(gold_path)
    if not gold:
        raise SystemExit(f"gold 为空或不存在: {gold_path}")

    selected, stats = load_projections(resolve_db_path(args.db))
    summary = build_summary(gold, selected, stats)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_md(summary, gold_path), encoding="utf-8")
    if args.json:
        DEFAULT_JSON.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"判卷集 {summary['n']} 条 ｜ {gold_path}")
    for label in ("must", "useful", "noise"):
        b = summary["by_label"].get(label)
        if b:
            print(
                f"  {LABEL_CN[label]}: {b['n']} 条 ｜ 曾上屏 {b['surfaced']} ｜ "
                f"从未上屏 {b['never']} ｜ 上屏率 {b['surfaced_rate']:.0%}"
            )
    print(f"报表 -> {out}")


if __name__ == "__main__":
    main()
