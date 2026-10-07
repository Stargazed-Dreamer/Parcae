"""_index.md 组件化模块段生成器

扫描 workspace/<module>/manifest.toml，程序化生成 _index.md 的"组件化模块"段。
核心 skill 段（### 1. - ### N.）保留手工维护，本工具只更新组件化模块段。

用法：
    uv run python -m tools.generate_index          # 写入 _index.md
    uv run python -m tools.generate_index --dry-run # 仅打印不写入
    uv run python -m tools.generate_index --json    # JSON 输出（CI 友好）

设计原则：
- 核心 skill 段（.agents/skills/）手工维护，本工具不动
- 组件化模块段（manifest 声明的）程序化生成，避免手工同步
- 删除 workspace/<module>/ 后 manifest 消失，组件化模块段自动清除该条目
- **fail-closed**：manifest 一条都没加载到时拒绝写回。本工具用"生成的段"整体替换
  "已有的段"，所以加载失败若被当成"0 个组件"，就会静默清空组件段（实测一次 -155 行）
  且退出码 0。要真的清空全部组件段，显式传 --allow-empty。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 项目根目录（脚本位于 tools/generate_index.py）
ROOT = Path(__file__).resolve().parent.parent
INDEX_FILE = ROOT / ".agents" / "skills" / "_index.md"

# 组件化模块段的 section 标记
COMPONENT_SECTION_HEADER = "## 组件化模块（自动生成，勿手工编辑）"
COMPONENT_SECTION_END = "<!-- END component section -->"


class ManifestLoadError(RuntimeError):
    """manifest 一条都没加载到。此时"0 个组件"是假象，绝不能拿去覆盖已有组件段。"""


def _collect_component_entries() -> list[dict]:
    """扫描 manifest，收集组件化模块段条目

    返回 list of dict，每个 dict 含 name/slug/description/task_type/skill_file。
    """
    try:
        from server.component_manifest import load_manifests
        manifests = load_manifests()
    except Exception as e:
        raise ManifestLoadError(
            f"manifest 加载失败（{e}），拒绝写回以免清空组件段。"
            f"正确姿势：uv run python -m tools.generate_index"
            f"（-m 会把仓库根放进 sys.path；或 PYTHONPATH=. 后再跑脚本路径）"
        ) from None

    entries: list[dict] = []
    for name, m in sorted(manifests.items()):
        if m.skill is None:
            continue
        entries.append({
            "name": name,
            "slug": name,
            "description": m.description,
            "task_type": m.skill.task_type,
            "skill_file": f"workspace/{name}/{m.skill.file}",
            "scope": m.skill.task_type.split(".", 1)[0] if "." in m.skill.task_type else "component",
        })
    return entries


def _format_component_section(entries: list[dict]) -> str:
    """格式化组件化模块段内容"""
    if not entries:
        return f"{COMPONENT_SECTION_HEADER}\n\n*（暂无组件化模块）*\n\n{COMPONENT_SECTION_END}"

    lines = [COMPONENT_SECTION_HEADER, ""]
    for e in entries:
        lines.append(f"### {e['name']} ({e['slug']}) [{e['scope']}]")
        lines.append("")
        lines.append(f"- **描述**：{e['description']}")
        lines.append(f"- **task_type**：`{e['task_type']}`")
        lines.append(f"- **Skill 文件**：`{e['skill_file']}`")
        lines.append("")
    lines.append(COMPONENT_SECTION_END)
    return "\n".join(lines)


def _replace_or_append_component_section(content: str, new_section: str) -> str:
    """替换 _index.md 中的组件化模块段；不存在则追加到 ## Skill 列表 段末尾"""
    # 匹配已有的组件化模块段（含 header 和 end 标记）
    pattern = re.compile(
        re.escape(COMPONENT_SECTION_HEADER) + r".*?" + re.escape(COMPONENT_SECTION_END),
        re.DOTALL,
    )
    if pattern.search(content):
        return pattern.sub(new_section, content)

    # 不存在：在 ## Skill 列表 段末尾追加
    # 找到下一个 ## 标题（## 后端 API 接口 等）作为 Skill 列表段的结束
    next_section_match = re.search(r"\n## (?!Skill 列表)", content)
    if next_section_match:
        insert_pos = next_section_match.start()
        return content[:insert_pos] + new_section + "\n\n" + content[insert_pos:]
    # 兜底：追加到文件末尾
    return content.rstrip() + "\n\n" + new_section + "\n"


def _existing_component_count(content: str) -> int:
    """已有组件段里的条目数（段内 ### 标题行数）"""
    m = re.search(
        re.escape(COMPONENT_SECTION_HEADER) + r"(.*?)" + re.escape(COMPONENT_SECTION_END),
        content,
        re.DOTALL,
    )
    if not m:
        return 0
    return sum(1 for line in m.group(1).splitlines() if line.startswith("### "))


def generate(dry_run: bool = False, allow_empty: bool = False) -> dict:
    """生成组件化模块段并写入 _index.md

    Args:
        dry_run: 只打印不写入
        allow_empty: 承认"确实没有组件了"，允许用空段覆盖已有段

    Returns:
        报告 dict，含 entries count 和是否写入

    Raises:
        ManifestLoadError: 一条组件都没加载到、而已有的段里有内容——判定为加载失败，
            拒绝写回（要真的清空全部组件段请显式传 allow_empty）。
    """
    entries = _collect_component_entries()

    if not INDEX_FILE.exists():
        print(f"[generate_index] _index.md 不存在: {INDEX_FILE}", file=sys.stderr)
        return {"entries": len(entries), "written": False, "error": "index not found"}

    content = INDEX_FILE.read_text(encoding="utf-8")
    existing = _existing_component_count(content)
    if not entries and existing and not allow_empty:
        raise ManifestLoadError(
            f"扫描到 0 个组件，但 _index.md 现有组件段有 {existing} 条——"
            f"判定为加载失败，拒绝清空。确认要清空全部组件段才传 --allow-empty。"
        )

    new_section = _format_component_section(entries)
    new_content = _replace_or_append_component_section(content, new_section)

    if dry_run:
        print(new_section)
        return {"entries": len(entries), "written": False, "dry_run": True}

    if new_content != content:
        INDEX_FILE.write_text(new_content, encoding="utf-8")
        return {"entries": len(entries), "written": True}
    return {"entries": len(entries), "written": False, "unchanged": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="_index.md 组件化模块段生成器")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不写入")
    parser.add_argument("--json", action="store_true", help="JSON 输出（CI 友好）")
    parser.add_argument(
        "--allow-empty", action="store_true",
        help="承认组件确实为零，允许用空段覆盖已有组件段（默认拒绝）",
    )
    args = parser.parse_args()

    try:
        report = generate(dry_run=args.dry_run, allow_empty=args.allow_empty)
    except ManifestLoadError as e:
        print(f"[generate_index] {e}", file=sys.stderr)
        if args.json:
            print(json.dumps({"entries": 0, "written": False, "error": str(e)}, ensure_ascii=False))
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"组件化模块段条目数: {report['entries']}")
        if report.get("written"):
            print(f"已写入: {INDEX_FILE}")
        elif report.get("unchanged"):
            print("无变化（组件化模块段已是最新）")
        elif report.get("dry_run"):
            print("dry-run 模式，未写入")
        elif report.get("error"):
            print(f"错误: {report['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
