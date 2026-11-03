#!/usr/bin/env python3
"""
版本号同步脚本：统一更新项目中所有 VERSION 常量引用。

更新位置：
  1. server/version.py       — VERSION = "x.y.z"（**唯一真源**：/health 与 FastAPI app 都取它）
  2. pyproject.toml          — [project] 段 version = "x.y.z"（不碰依赖版本号）

⛔ 不要再把 `server/main.py` / `server/core/health.py` 加回目标列表：两者现在都是
   `from server.version import VERSION`，文件里**没有任何** `VERSION = "x.y.z"` 可匹配。
   2026-10-24 之前本脚本还照着旧架构改那两处，实跑结果是一个都匹配不到、只有 pyproject
   被改 ⇒ **运行期版本号不动**（/health 报旧版），且末尾 exit 1 留下"只同步一半"的中间态。
   架构说明见 `server/version.py` 顶部「为什么单独拆一个模块」。

用法:
  uv run python tools/bump_version.py 0.32.1
  uv run python tools/bump_version.py 0.32.1 --dry-run    # 只打印，不写入

退出码:
  0 — 全部更新成功
  1 — 版本号格式无效 / 文件未找到 / 更新失败
"""

import argparse
import io
import re
import sys
from pathlib import Path

# 中文 Windows 控制台默认 cp936，而本脚本的进度行含 ✓/✗/⊘：第一条 print 就能抛
# UnicodeEncodeError，且崩在写文件**之后**——于是第 1 个目标已改、后 2 个没改，
# 留下"版本号只同步了一半"的中间态（2026-09-28 实跑撞到）。先包一层 utf-8/replace。
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    if _stream is not None and hasattr(_stream, "buffer"):
        setattr(sys, _name, io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace"))

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 需要更新的文件及其匹配模式
# (相对路径, 正则模式, 替换模板, 说明)
TARGETS = [
    (
        "server/version.py",
        re.compile(r'^VERSION\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"', re.MULTILINE),
        'VERSION = "{version}"',
        "版本号唯一真源（/health 与 FastAPI app 都取它）",
    ),
    (
        "pyproject.toml",
        # 只匹配 [project] 段的 version = "..."（行首，在 [project] 段下）
        # 不匹配依赖中的版本号（如 "huggingface-hub>=0.32.0"）
        re.compile(r'^version\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"', re.MULTILINE),
        'version = "{version}"',
        "pyproject.toml [project] version",
    ),
]

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def validate_version(version: str) -> bool:
    """验证版本号格式：x.y.z（纯数字，三段）"""
    return bool(VERSION_PATTERN.match(version))


def bump_file(rel_path: str, pattern: re.Pattern, replacement: str,
              version: str, dry_run: bool) -> tuple[bool, str]:
    """更新单个文件中的版本号。返回 (是否更新, 说明信息)。"""
    file_path = PROJECT_ROOT / rel_path
    if not file_path.is_file():
        return False, f"  ✗ 文件不存在: {rel_path}"

    content = file_path.read_text(encoding="utf-8")
    matches = list(pattern.finditer(content))

    if not matches:
        return False, f"  ✗ 未找到版本号匹配: {rel_path}"

    if len(matches) > 1:
        return False, f"  ✗ 匹配到 {len(matches)} 处版本号（期望 1 处）: {rel_path}"

    new_content = pattern.sub(replacement.format(version=version), content)

    if new_content == content:
        return False, f"  ⊘ 版本号已相同，无需更新: {rel_path}"

    if not dry_run:
        file_path.write_text(new_content, encoding="utf-8")

    return True, f"  ✓ {rel_path}: → {version}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="统一更新项目中所有 VERSION 常量引用"
    )
    parser.add_argument(
        "version",
        help="目标版本号，格式 x.y.z（如 0.32.1）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印变更，不写入文件",
    )
    args = parser.parse_args()

    if not validate_version(args.version):
        print(f"错误: 版本号格式无效 '{args.version}'，期望 x.y.z（如 0.32.1）", file=sys.stderr)
        return 1

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}版本号同步 → {args.version}")
    print()

    all_ok = True
    for rel_path, pattern, replacement, desc in TARGETS:
        ok, msg = bump_file(rel_path, pattern, replacement, args.version, args.dry_run)
        print(msg)
        if desc:
            print(f"    ({desc})")
        if not ok and "✗" in msg:
            all_ok = False

    print()
    if all_ok:
        print(f"{'[DRY-RUN] ' if args.dry_run else ''}✓ 版本号同步完成: {args.version}")
        return 0
    else:
        print(f"{'[DRY-RUN] ' if args.dry_run else ''}✗ 部分文件更新失败，请检查上方输出", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
