#!/usr/bin/env python3
"""
CHANGELOG 迁移脚本：把旧 release 段从 CHANGELOG.md 移到 docs/changelog-archive.md

策略：
  - CHANGELOG.md 只保留 [Unreleased] + 最近 N 个 release（默认 N=1）
  - 较老的 release 移到 docs/changelog-archive.md（newest first，与 CHANGELOG.md 顺序一致）
  - 归档文件已存在时，新归档段插在归档头部（紧跟 # Changelog Archive 标题）
  - 幂等：已在归档文件中的版本不会被重复写入；CHANGELOG.md 若无可归档段则跳过

用法:
  uv run python tools/migrate_changelog.py             # 默认保留 1 个 release
  uv run python tools/migrate_changelog.py --keep 2     # 保留最近 2 个 release
  uv run python tools/migrate_changelog.py --dry-run    # 只打印，不写入

写完之后会自动跑一次"发布口径复检"（wip_fe6da6fa 遗留 C）：用发布 profile 的
豁免口径重扫 CHANGELOG.md 与 docs/changelog-archive.md，比对归档前后的命中数。
豁免是按路径登记的，条目从主文件搬进归档后原豁免就不再覆盖它 —— HIGH 上涨即
以退码 3 失败，并列出新增命中的 路径:行号:规则（不打印行内容）。
  --profile NAME          指定 profile（默认取 release/policy.toml 的 default_profile）
  --skip-release-check    跳过复检（只在动的是非仓库 changelog 路径时用）
"""

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"
DEFAULT_ARCHIVE = PROJECT_ROOT / "docs" / "changelog-archive.md"

ARCHIVE_HEADER = """# Changelog Archive

历史 release 条目归档。主 `CHANGELOG.md` 仅保留 `[Unreleased]` + 最近 1 个 release 段，
避免活跃段过长导致 agent 编辑时误伤历史版本段。

如需查询完整变更历史，请同时阅读 `CHANGELOG.md` 和本文件。本文件按版本号倒序排列（最新在最上）。

"""

SECTION_HEADER_RE = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)

_PROFILES_DIR = PROJECT_ROOT / "release" / "profiles"
_POLICY_FILE = PROJECT_ROOT / "release" / "policy.toml"
# 归档动的是这两个文件，复检就只按发布口径扫它们（同一个 collect_sensitive_hits，
# 所以这里数的就是 prepare 的 static gate 会数的）
_SCANNED_FILES = ("CHANGELOG.md", "docs/changelog-archive.md")
_EXIT_RELEASE_SCAN_REGRESSION = 3


def _profile_name(policy_profile: str | None) -> str:
    """未显式指定时读 release/policy.toml 的 default_profile（与 cli 同一来源）。"""
    if policy_profile:
        return policy_profile
    import toml

    return str(toml.load(_POLICY_FILE).get("default_profile") or "public-full")


def scan_release_counts(profile: str) -> dict:
    """按发布 profile 的豁免口径统计两个 changelog 文件的敏感命中。

    Returns: {"counts": {"HIGH": n, ...}, "hits": [...]}；行内容绝不返回（不脱敏不回显）。
    """
    import toml

    sys.path.insert(0, str(PROJECT_ROOT))
    from tools.release.engine.models import FileEntry
    from tools.release.engine.prepare import collect_sensitive_hits

    profile_data = toml.load(_PROFILES_DIR / f"{profile}.toml")
    entries = []
    for rel in _SCANNED_FILES:
        path = PROJECT_ROOT / rel
        entries.append(
            FileEntry(
                rel_path=rel,
                sha256="",
                size=path.stat().st_size if path.exists() else 0,
                source="tracked",
            )
        )
    hits, scanned, _skipped = collect_sensitive_hits(
        tuple(entries),
        profile_data.get("sensitive_line_skips", {}),
        profile_data.get("post_process_remove_lines", {}),
        list(profile_data.get("scan", {}).get("personal_identifiers", []) or []),
    )
    counts: dict[str, int] = {}
    for h in hits:
        counts[h["severity"]] = counts.get(h["severity"], 0) + 1
    return {"counts": counts, "hits": hits, "scanned": scanned}


def compare_scan(before: dict, after: dict) -> tuple[int, list[str]]:
    """纯判断：HIGH 上升即回归。返回 (退出码, 给人看的行)。

    拆成纯函数是为了能被合成账本测到 —— 真实仓库里此刻没有复活案例，
    只测真值的那道判据等于没测（恒绿）。
    """
    b, a = before["counts"], after["counts"]
    keys = sorted(set(b) | set(a))
    summary = "、".join(
        f"{k} {b.get(k, 0)}→{a.get(k, 0)}"
        + ("（+）" if a.get(k, 0) > b.get(k, 0) else "")
        for k in keys
    )
    if a.get("HIGH", 0) <= b.get("HIGH", 0):
        return 0, [f"[发布口径复检] OK 归档没有让豁免失效（{summary}）"]
    before_keys = {(h["path"], h["line"], h["rule"]) for h in before["hits"]}
    lines = [
        f"[发布口径复检] ⛔ HIGH 从 {b.get('HIGH', 0)} 涨到 {a.get('HIGH', 0)}"
        f"（{summary}）—— 搬进归档的条目里，有原本靠 CHANGELOG.md 豁免的行现在无人豁免。",
    ]
    for h in after["hits"]:
        if h["severity"] != "HIGH" or (h["path"], h["line"], h["rule"]) in before_keys:
            continue
        lines.append(f"  新增 HIGH：{h['path']}:{h['line']} rule={h['rule']}")
    return _EXIT_RELEASE_SCAN_REGRESSION, lines


def verify_after_archive(before: dict, profile: str) -> int:
    """归档后复跑一次发布口径扫描，比对命中数。

    为什么要这一道：豁免是按**路径**登记的（[sensitive_line_skips] 的 key 是文件名），
    归档把已豁免的历史条目从 CHANGELOG.md 搬进 docs/changelog-archive.md 后，
    那条豁免就不再覆盖它 —— 下个发版周期 prepare 的 HIGH 会复活，而现场没人提示。
    """
    try:
        after = scan_release_counts(profile)
    except Exception as e:
        # 复检跑不起来 = 没验证过，不能当通过
        print(
            f"[发布口径复检] ⛔ 无法执行（{type(e).__name__}: {e}）。"
            "文件已归档，但豁免是否失效未知 —— 请手工重跑 prepare 看命中数。",
            file=sys.stderr,
        )
        return _EXIT_RELEASE_SCAN_REGRESSION
    code, out_lines = compare_scan(before, after)
    stream = sys.stderr if code else sys.stdout
    for line in out_lines:
        print(line, file=stream)
    if code:
        print(
            "  处置：把这些行的正则补进 "
            f"release/profiles/{profile}.toml 的 [sensitive_line_skips]/"
            "[post_process_remove_lines]（按 docs/changelog-archive.md 登记），"
            "然后重跑 `tools/release/cli.py prepare` 并重新审批。",
            file=sys.stderr,
        )
    return code



def parse_sections(content: str):
    """拆 CHANGELOG 内容为 (preamble, sections)。

    preamble 是首个 `## [...]` 行之前的所有内容（标题 + 说明 + 空行）。
    每个 section 是 (header_line, body)，header_line 不含末尾换行符；
    body 是 header 行换行后到下一个 section 起点之间的全部内容（含尾随空行）。
    """
    matches = list(SECTION_HEADER_RE.finditer(content))
    if not matches:
        return content, []

    preamble = content[: matches[0].start()]
    sections = []
    for i, m in enumerate(matches):
        header_start = m.start()
        newline_pos = content.find("\n", m.end())
        if newline_pos == -1:
            header_line = content[header_start:]
            body_start = len(content)
        else:
            header_line = content[header_start:newline_pos]
            body_start = newline_pos + 1

        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end]
        sections.append((header_line, body))
    return preamble, sections


def extract_version(header_line: str):
    """从 `## [0.7.1] - 2026-07-13` 提取 version 字符串；`## [Unreleased]` 返回 'Unreleased'。"""
    m = re.match(r"^## \[([^\]]+)\]", header_line)
    return m.group(1) if m else None


def main():
    parser = argparse.ArgumentParser(description="把旧 release 段从 CHANGELOG.md 迁到归档文件")
    parser.add_argument("--keep", type=int, default=1, help="保留最近 N 个 release 段（默认 1）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的操作，不写入文件")
    parser.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG, help="CHANGELOG.md 路径")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="归档文件路径")
    parser.add_argument(
        "--profile", default=None,
        help="发布复检用的 profile 名（默认读 release/policy.toml 的 default_profile）",
    )
    parser.add_argument(
        "--skip-release-check", action="store_true",
        help="跳过归档后的发布口径复检（只在改的是非仓库 changelog 路径时用）",
    )
    args = parser.parse_args()

    if not args.changelog.exists():
        print(f"ERROR: CHANGELOG.md 不存在：{args.changelog}", file=sys.stderr)
        return 1

    # 复检只对仓库里的真 changelog 有意义（profile 豁免按仓库相对路径登记）
    repo_targets = (
        args.changelog.resolve() == (PROJECT_ROOT / "CHANGELOG.md").resolve()
        and args.archive.resolve() == (PROJECT_ROOT / "docs" / "changelog-archive.md").resolve()
    )
    release_check: tuple[str, dict] | None = None  # (profile, 归档前基线)
    if args.skip_release_check or not repo_targets:
        why = "传了 --skip-release-check" if args.skip_release_check else "changelog 路径不是仓库默认位置"
        print(f"[发布口径复检] 跳过（{why}）")
    else:
        try:
            chosen = _profile_name(args.profile)
            release_check = (chosen, scan_release_counts(chosen))
        except Exception as e:
            # 探测跑不起来不等于没问题——假正常比崩更不可接受
            print(
                f"ERROR: 发布口径复检无法建立基线（{type(e).__name__}: {e}）。"
                "修好探测，或显式 --skip-release-check 承担后果。",
                file=sys.stderr,
            )
            return _EXIT_RELEASE_SCAN_REGRESSION

    content = args.changelog.read_text(encoding="utf-8")
    preamble, sections = parse_sections(content)

    unreleased = None
    releases = []
    for header_line, body in sections:
        version = extract_version(header_line)
        if version == "Unreleased":
            unreleased = (header_line, body)
        elif version is not None:
            releases.append((header_line, body))

    if unreleased is None:
        print("ERROR: CHANGELOG.md 缺少 [Unreleased] 段", file=sys.stderr)
        return 1

    print(f"CHANGELOG.md 现有 {len(releases)} 个 release 段：")
    for h, _ in releases:
        print(f"  {h}")

    if len(releases) <= args.keep:
        print(f"\n保留 {args.keep} 个，当前只有 {len(releases)} 个，无需归档")
        return 0

    to_keep = releases[: args.keep]
    to_archive = releases[args.keep :]

    print(f"\n保留 [Unreleased] + 最近 {len(to_keep)} 个 release：")
    for h, _ in to_keep:
        print(f"  {h}")
    print(f"待归档 {len(to_archive)} 个 release 到 {args.archive}：")
    for h, _ in to_archive:
        print(f"  {h}")

    if args.dry_run:
        print("\n[dry-run] 不写入文件")
        return 0

    # 读取已有归档（保留 release 段，丢掉老 header）
    existing_archive_releases = ""
    if args.archive.exists():
        old = args.archive.read_text(encoding="utf-8")
        m = SECTION_HEADER_RE.search(old)
        if m:
            existing_archive_releases = old[m.start():]

    already_archived_versions = set()
    for header_line, _ in parse_sections(existing_archive_releases)[1]:
        v = extract_version(header_line)
        if v:
            already_archived_versions.add(v)

    fresh_to_archive = []
    skipped = []
    for header_line, body in to_archive:
        v = extract_version(header_line)
        if v in already_archived_versions:
            skipped.append(header_line)
        else:
            fresh_to_archive.append((header_line, body))

    if skipped:
        print(f"\n跳过已归档的 {len(skipped)} 个版本：")
        for h in skipped:
            print(f"  {h}")

    # 重写 CHANGELOG.md：preamble + [Unreleased] + 保留的 release
    new_changelog_parts = [preamble.rstrip() + "\n\n"]
    new_changelog_parts.append(unreleased[0] + "\n" + unreleased[1].rstrip() + "\n")
    for header_line, body in to_keep:
        new_changelog_parts.append("\n" + header_line + "\n" + body.rstrip() + "\n")
    new_changelog_parts.append("\n")
    new_changelog = "".join(new_changelog_parts)
    args.changelog.write_text(new_changelog, encoding="utf-8")

    # 写归档文件：header + 新归档段（newest first）+ 已有归档段
    new_archive_parts = [ARCHIVE_HEADER]
    for header_line, body in fresh_to_archive:
        new_archive_parts.append(header_line + "\n" + body.rstrip() + "\n\n")
    new_archive_parts.append(existing_archive_releases)
    new_archive = "".join(new_archive_parts)

    args.archive.parent.mkdir(parents=True, exist_ok=True)
    args.archive.write_text(new_archive, encoding="utf-8")

    print(f"\nOK {args.changelog}: 保留 [Unreleased] + {len(to_keep)} 个 release")
    print(
        f"OK {args.archive}: 新增 {len(fresh_to_archive)} 个，"
        f"总计 {len(fresh_to_archive) + len(already_archived_versions)} 个归档版本"
    )
    if release_check is None:
        return 0
    chosen_profile, before_scan = release_check
    return verify_after_archive(before_scan, chosen_profile)


if __name__ == "__main__":
    sys.exit(main())
