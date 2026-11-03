"""事务分类回填（DL2）—— 默认 dry-run，`--apply` 才写库。

用法：
    uv run python tools/task_class_backfill.py                      # 只看计划，不动库
    uv run python tools/task_class_backfill.py --db <副本路径>       # 对副本跑
    uv run python tools/task_class_backfill.py --apply              # 真写

设计：workspace/local_butler/30_架构与设计/13_事务分类_DL2实施设计_20261027.md

三条纪律（与 dry-run 文化一致）：
1. 默认不写库；写必须显式 `--apply`。
2. `unmapped`（映射不上四类）与 `purpose_unset`（目的轴判不出）**原样列出**，不挑近的类别塞。
3. 只碰 `task_class` / `purpose` 两列，不动标题、状态、日期。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.todos.store import TodosStore


def main() -> int:
    ap = argparse.ArgumentParser(description="DL2 事务分类回填（默认 dry-run）")
    ap.add_argument("--db", default="data/memory/memory.db", help="memory.db 路径")
    ap.add_argument("--apply", action="store_true", help="真写库（默认只出计划）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"找不到库：{args.db}", file=sys.stderr)
        return 2

    store = TodosStore(args.db)
    store.initialize()
    try:
        report = store.classify_tasks(apply=args.apply)
        summary = store.task_summary()
        queue = store.task_queue()
    finally:
        store.close()

    if args.json:
        print(json.dumps(
            {
                "report": report,
                "summary": summary,
                "queue_levels": [
                    {"level": q["level"], "view": q["view"], "title": q["title"]}
                    for q in queue
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    mode = "APPLY（已写库）" if report["applied"] else "DRY-RUN（未写库）"
    print(f"== DL2 事务分类回填 · {mode} ==")
    print(f"库：{args.db}")
    print()
    print("-- 结构轴 --")
    for k, v in summary["by_task_class_label"].items():
        print(f"  {k:<12} {v}")
    print()
    print("-- 目的轴 --")
    for k, v in summary["by_purpose_label"].items():
        print(f"  {k:<6} {v}")
    print(f"  未标（purpose_unset） {len(summary['purpose_unset'])}")
    print()
    print("-- 派生视图 --")
    for k, v in sorted(summary["by_view"].items()):
        print(f"  {k:<18} {v}")
    print()
    print(f"-- 计划变更 {report['planned']} 条（跳过未变 {report['unchanged']} 条）--")
    for a in report["assignments"]:
        print(f"  [{a['table']:<9}] {a['id']:<22} → {a['task_class']:<16} purpose={a['purpose']}")
    if report["unmapped"]:
        print()
        print(f"-- 映射不上四类：{len(report['unmapped'])} 条（不猜，等你判）--")
        for u in report["unmapped"]:
            print(f"  [{u['table']:<9}] {u['id']:<22} {u['title']}")
            for note in u["notes"]:
                print(f"       · {note}")
    if summary["purpose_unset"]:
        print()
        print(f"-- 目的轴留空：{len(summary['purpose_unset'])} 条（等你标 日常/一次性/成长）--")
        for u in summary["purpose_unset"]:
            print(f"  [{u['table']:<9}] {u['id']:<22} {u['title']}")
    print()
    print(f"-- 队列（{len(queue)} 条，前 12）--")
    for q in queue[:12]:
        print(f"  L{q['level']} {q['view']:<16} {q['title'][:30]:<32} {q['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
