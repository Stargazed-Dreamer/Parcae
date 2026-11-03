#!/usr/bin/env python3
"""整理完成后的全量核对：清单 vs 实际文件。只读。

检查项：
  1. 清单里的文件是否都在（missing）
  2. 目录里是否有清单外的文件（unexpected）
  3. 是否有子目录（平铺目录必须为 0）
  4. 大小是否一致（清单需含 size 列）
  5. .mp4 是否有 ftyp 头
  6. 文件名是否符合命名规范
  7. 可选：按内容指纹查重

任何一个问题都会让退出码为 1。脚本会自报每项检查的覆盖范围（分母），
避免"没检查"被读成"检查通过"。

用法:
    python verify_media_folder.py "<data_drive>:/<media_staging>\\new" --csv "<data_drive>:/<media_staging>\\new_最终清单_20261023.csv"
    python verify_media_folder.py <dir> --csv <list.csv> --pattern "^\\d{1,2}-\\d{1,2} \\S+ .+\\.mp4$"
    python verify_media_folder.py <dir> --csv <list.csv> --allow-extra --hash
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path

NAME_KEYS = ["final", "name", "target", "filename", "最终名", "目标名", "文件名", "新名"]
SIZE_KEYS = ["size", "bytes", "大小", "字节"]
DEFAULT_PATTERN = r"^\d{1,2}-\d{1,2} \S+ .+\.\w+$"


def pick(row: dict, keys: list[str]) -> str:
    for k in keys:
        for actual, value in row.items():
            if actual is None:
                continue
            if actual.strip().casefold() == k.casefold():
                return (value or "").strip()
    return ""


def load_list(csv_path: Path) -> tuple[list[tuple[str, int | None]], list[str]]:
    problems: list[str] = []
    entries: list[tuple[str, int | None]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return [], [f"CSV 没有表头: {csv_path}"]
        name_key_found = any(
            (h or "").strip().casefold() in [k.casefold() for k in NAME_KEYS]
            for h in reader.fieldnames
        )
        if not name_key_found:
            return [], [f"CSV 缺少文件名列（候选: {NAME_KEYS}），实际表头: {reader.fieldnames}"]
        for i, row in enumerate(reader, start=2):
            name = pick(row, NAME_KEYS)
            if not name:
                problems.append(f"CSV 第 {i} 行文件名为空")
                continue
            raw_size = pick(row, SIZE_KEYS)
            size: int | None = None
            if raw_size:
                try:
                    size = int(float(raw_size.replace(",", "")))
                except ValueError:
                    problems.append(f"CSV 第 {i} 行 size 不是数字: {raw_size!r}")
            entries.append((name, size))
    return entries, problems


def sha1_head(path: Path, nbytes: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        h.update(f.read(nbytes))
    return h.hexdigest()


def ftyp_ok(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"ftyp" in f.read(64)[:32]
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="整理结果全量核对（只读）")
    ap.add_argument("folder", help="平铺后的目录")
    ap.add_argument("--csv", required=True, help="最终清单 CSV（含文件名列，建议含大小列）")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN, help="命名规范正则")
    ap.add_argument("--no-name-check", action="store_true", help="跳过命名规范检查")
    ap.add_argument("--allow-extra", action="store_true",
                    help="清单外文件只警告不判失败（核对父目录时用）")
    ap.add_argument("--hash", action="store_true", help="按首 4MB 指纹查重（慢）")
    args = ap.parse_args()

    root = Path(args.folder).resolve()
    csv_path = Path(args.csv).resolve()
    if not root.is_dir():
        print(f"[BAD] 目录不存在: {root}")
        return 2
    if not csv_path.is_file():
        print(f"[BAD] 清单不存在: {csv_path}")
        return 2

    entries, list_problems = load_list(csv_path)
    problems: list[str] = list(list_problems)

    expected: dict[str, int | None] = {}
    for name, size in entries:
        key = name.casefold()
        if key in expected:
            problems.append(f"清单内重名: {name}")
            continue
        expected[key] = size

    actual_files: dict[str, Path] = {}
    dirs: list[str] = []
    for child in root.iterdir():
        if child.is_dir():
            dirs.append(child.name)
        elif child.is_file():
            actual_files[child.name.casefold()] = child

    missing = [n for n in expected if n not in actual_files]
    unexpected = [n for n in actual_files if n not in expected]

    size_checked = 0
    size_mismatch: list[str] = []
    for key, size in expected.items():
        if size is None or key not in actual_files:
            continue
        size_checked += 1
        got = actual_files[key].stat().st_size
        if got != size:
            size_mismatch.append(f"{actual_files[key].name}: 清单 {size} / 实际 {got}")

    mp4s = [p for p in actual_files.values() if p.suffix.lower() == ".mp4"]
    bad_ftyp = [p.name for p in mp4s if not ftyp_ok(p)]

    name_checked = 0
    bad_names: list[str] = []
    if not args.no_name_check:
        rx = re.compile(args.pattern)
        for key, path in actual_files.items():
            if key not in expected:
                continue
            name_checked += 1
            if not rx.match(path.name):
                bad_names.append(path.name)

    dupes: list[list[str]] = []
    if args.hash:
        buckets: dict[tuple[int, str], list[str]] = {}
        for path in actual_files.values():
            if path.suffix.lower() in (".mp4", ".mkv", ".mov", ".png", ".jpg", ".jpeg", ".webp"):
                try:
                    key = (path.stat().st_size, sha1_head(path))
                except OSError:
                    continue
                buckets.setdefault(key, []).append(path.name)
        dupes = [v for v in buckets.values() if len(v) > 1]

    print(f"[INFO] 目录: {root}")
    print(f"[INFO] 清单: {csv_path}（{len(expected)} 条）")
    print(f"[INFO] 实际: {len(actual_files)} 个文件, {len(dirs)} 个子目录")
    print(f"[INFO] 已检查: 存在性 {len(expected)}/{len(expected)}, "
          f"大小 {size_checked}/{len(expected)}, 命名 {name_checked}/{len(expected)}, "
          f"MP4 头 {len(mp4s)}/{len(mp4s)}")

    if missing:
        problems.append(f"清单里有、目录里缺 {len(missing)} 个: {missing[:8]}")
    if unexpected and not args.allow_extra:
        problems.append(f"目录里有、清单里没有 {len(unexpected)} 个: {unexpected[:8]}")
    if dirs:
        problems.append(f"目录内还有子目录 {len(dirs)} 个: {dirs[:8]}")
    if size_mismatch:
        problems.append(f"大小不符 {len(size_mismatch)} 个: {size_mismatch[:8]}")
    if bad_ftyp:
        problems.append(f"MP4 头异常 {len(bad_ftyp)} 个: {bad_ftyp[:8]}")
    if bad_names:
        problems.append(f"命名不合规 {len(bad_names)} 个: {bad_names[:8]}")
    if dupes:
        problems.append(f"发现 {len(dupes)} 组重复内容: {dupes[:3]}")

    if size_checked == 0 and expected:
        print("[WARN] 清单没有 size 列（或全为空），大小这一项实际未检查")

    if problems:
        for p in problems:
            print(f"[BAD] {p}")
        return 1

    total = sum(p.stat().st_size for p in actual_files.values())
    print(f"[OK] 全部通过：{len(actual_files)} 个文件, {total / 1024 ** 3:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
