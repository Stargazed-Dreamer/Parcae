#!/usr/bin/env python3
"""
项目结构 baseline 同步脚本：扫描磁盘 + 把 unknown/missing 路径机械同步到 baseline。

机械同步规则（不推断意图）：
  - 磁盘上不存在的顶层路径（missing top）→ 从 baseline.top_level 移除
  - 磁盘上不存在的二级子目录（missing sub）→ 从 baseline.subdirs[parent] 移除
  - 磁盘新增的顶层路径（unknown top）→ 加入 baseline.top_level（description 留空，agent 后续 task_closure 补）
  - 磁盘新增的二级子目录（unknown sub）→ 加入 baseline.subdirs[parent]

典型场景：发版前调用一次，把累积的漂移全部同步，避免 baseline 与磁盘持续偏离。

用法:
  uv run python tools/sync_project_structure.py            # 同步并打印摘要
  uv run python tools/sync_project_structure.py --dry-run   # 只打印将做什么，不写入
  uv run python tools/sync_project_structure.py --verify     # 只跑 diff，不写入（发版前自检）
"""

import argparse
import io
import json
import sys
from pathlib import Path

# 中文 Windows 控制台默认 cp936，而本脚本的摘要行含 ⚠️/✅：print 就能抛 UnicodeEncodeError
# （2026-10-03 实跑 --verify 撞在"检测到漂移"那一行，发版第 7 步直接断在半路）。
# 与 tools/bump_version.py 同一写法：先把 stdout/stderr 包一层 utf-8/replace。
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    if _stream is not None and hasattr(_stream, "buffer"):
        setattr(sys, _name, io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace"))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.project_structure import (  # noqa: E402
    diff_structure,
    load_baseline,
    scan_project_structure,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="同步项目结构 baseline 与磁盘扫描结果")
    parser.add_argument("--dry-run", action="store_true", help="只打印将做什么，不写入")
    parser.add_argument("--verify", action="store_true", help="只跑 diff，不写入")
    args = parser.parse_args()

    if args.verify:
        baseline = load_baseline()
        current = scan_project_structure()
        diff = diff_structure(baseline, current)
        print(json.dumps(diff, ensure_ascii=False, indent=2))
        if diff["unknown_paths"] or diff["missing_paths"]:
            print("\n⚠️  发现漂移，建议跑 `uv run python tools/sync_project_structure.py` 同步。")
            return 1
        print("\n✅ 无漂移。")
        return 0

    baseline = load_baseline()
    current = scan_project_structure()
    diff = diff_structure(baseline, current)

    if not diff["unknown_paths"] and not diff["missing_paths"]:
        print("✅ baseline 与磁盘一致，无需同步。")
        return 0

    print("=== 同步前漂移 ===")
    print(json.dumps(diff, ensure_ascii=False, indent=2))
    print()

    if args.dry_run:
        print("--dry-run：不写入。")
        return 0

    # 直接调 server.project_structure.sync_baseline（保持单源真源）
    from server.project_structure import sync_baseline
    result = sync_baseline()
    print("=== 同步结果 ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
