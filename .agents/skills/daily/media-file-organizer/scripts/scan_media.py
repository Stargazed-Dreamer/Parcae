#!/usr/bin/env python3
"""只读盘点媒体目录：大小 / 类型 / MP4 mvhd 日期 / zip 条目 / 指纹。

不写、不改、不删目标目录里的任何东西。输出可给人看，也可落 CSV 供映射表使用。

用法:
    python scan_media.py "<data_drive>:/<media_staging>\\new" --out temp/media_inventory.csv
    python scan_media.py <dir> --json
    python scan_media.py <dir> --skip-contains Goo-Engine --max-entries 200

输出约定: 所有状态行用 [OK]/[WARN]/[BAD] 前缀，不用 Unicode 勾叉（中文 Windows 控制台是 GBK）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import struct
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
ZIP_EXTS = {".zip"}
DEFAULT_SKIP = ["Goo-Engine"]
EPOCH_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)
MAX_MOOV_BYTES = 16 * 1024 * 1024


def parse_mvhd(path: Path) -> tuple[str, float] | tuple[None, None]:
    """返回 (creation_time_iso, duration_seconds)。解析失败返回 (None, None)。"""
    try:
        size_total = path.stat().st_size
        with path.open("rb") as f:
            while True:
                start = f.tell()
                head = f.read(8)
                if len(head) < 8:
                    return None, None
                size, box_type = struct.unpack(">I4s", head)
                header_len = 8
                if size == 1:
                    large = f.read(8)
                    if len(large) < 8:
                        return None, None
                    size = struct.unpack(">Q", large)[0]
                    header_len = 16
                elif size == 0:
                    size = size_total - start
                if size < header_len:
                    return None, None
                if box_type == b"moov":
                    payload_len = min(size - header_len, MAX_MOOV_BYTES)
                    payload = f.read(payload_len)
                    return _find_mvhd(payload)
                f.seek(start + size)
    except OSError:
        return None, None


def _find_mvhd(buf: bytes) -> tuple[str, float] | tuple[None, None]:
    """在 moov 载荷里递归找 mvhd。注意：返回值必须一路 return 上来。"""
    off = 0
    while off + 8 <= len(buf):
        size, box_type = struct.unpack(">I4s", buf[off:off + 8])
        header_len = 8
        if size == 1 and off + 16 <= len(buf):
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            header_len = 16
        elif size == 0:
            size = len(buf) - off
        if size < header_len or off + size > len(buf):
            break
        body = buf[off + header_len:off + size]
        if box_type == b"mvhd":
            return _decode_mvhd(body)
        if box_type in (b"trak", b"mvex"):
            found = _find_mvhd(body)
            if found[0] is not None:
                return found
        off += size
    return None, None


def _decode_mvhd(body: bytes) -> tuple[str, float] | tuple[None, None]:
    if len(body) < 20:
        return None, None
    version = body[0]
    try:
        if version == 1:
            creation, _mod, timescale, duration = struct.unpack(">QQIQ", body[4:32])
        else:
            creation, _mod, timescale, duration = struct.unpack(">IIII", body[4:20])
    except struct.error:
        return None, None
    if creation <= 0 or timescale <= 0:
        return None, None
    created = EPOCH_1904 + timedelta(seconds=creation)
    return created.strftime("%Y-%m-%d %H:%M:%S"), round(duration / timescale, 3)


def is_valid_mp4_header(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(64)
    except OSError:
        return False
    return b"ftyp" in head[:32]


def sha1_head(path: Path, nbytes: int) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        h.update(f.read(nbytes))
    return h.hexdigest()


def scan_zip(path: Path, max_entries: int) -> dict:
    info: dict = {
        "zip_entries": "",
        "zip_entry_count": 0,
        "zip_entries_truncated": False,
        "zip_encrypted": False,
        "zip_error": "",
    }
    try:
        with zipfile.ZipFile(path) as z:
            items = z.infolist()
            info["zip_entry_count"] = len(items)
            info["zip_entries_truncated"] = len(items) > max_entries
            names = [i.filename for i in items[:max_entries]]
            info["zip_entries"] = " | ".join(names)
            info["zip_encrypted"] = any(i.flag_bits & 0x1 for i in items)
    except (zipfile.BadZipFile, OSError) as exc:
        info["zip_error"] = str(exc)
    return info


def classify(ext: str) -> str:
    if ext in MEDIA_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ZIP_EXTS:
        return "zip"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser(description="只读盘点媒体目录")
    ap.add_argument("folder", help="待盘点目录")
    ap.add_argument("--out", help="写 CSV 的路径（必须在目录外）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是文本摘要")
    ap.add_argument("--max-entries", type=int, default=200,
                    help="每个 zip 最多列出多少条目，默认 200")
    ap.add_argument("--skip-contains", action="append", default=None,
                    help="跳过路径里含该子串的文件（可多次），默认跳过 Goo-Engine")
    ap.add_argument("--hash-bytes", type=int, default=4 * 1024 * 1024,
                    help="指纹取前多少字节，默认 4MB；0 表示不算")
    args = ap.parse_args()

    root = Path(args.folder).resolve()
    if not root.is_dir():
        print(f"[BAD] 目录不存在: {root}")
        return 2

    out_path = Path(args.out).resolve() if args.out else None
    if out_path and (out_path == root or root in out_path.parents):
        print(f"[BAD] --out 不能写进被盘点的目录: {out_path}")
        return 2

    skips = [s.casefold() for s in (args.skip_contains or DEFAULT_SKIP)]

    rows: list[dict] = []
    skipped: list[str] = []
    dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            dirs.append(os.path.relpath(os.path.join(dirpath, d), root))
        for name in filenames:
            full = Path(dirpath) / name
            rel = os.path.relpath(full, root)
            if any(s in rel.casefold() for s in skips):
                skipped.append(rel)
                continue
            ext = full.suffix.lower()
            kind = classify(ext)
            try:
                st = full.stat()
            except OSError as exc:
                rows.append({"path": rel, "kind": "error", "size": 0, "error": str(exc)})
                continue
            row = {
                "path": rel,
                "kind": kind,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "mvhd_created": "",
                "mvhd_duration_s": "",
                "ftyp_ok": "",
                "sha1_head": "",
                "zip_entries": "",
                "zip_entry_count": "",
                "zip_entries_truncated": "",
                "zip_encrypted": "",
                "zip_error": "",
            }
            if kind == "video" and ext == ".mp4":
                created, duration = parse_mvhd(full)
                row["mvhd_created"] = created or ""
                row["mvhd_duration_s"] = "" if duration is None else duration
                row["ftyp_ok"] = "yes" if is_valid_mp4_header(full) else "no"
            if kind == "zip":
                row.update(scan_zip(full, args.max_entries))
            if args.hash_bytes > 0 and kind in ("video", "image"):
                row["sha1_head"] = sha1_head(full, args.hash_bytes)
            rows.append(row)

    rows.sort(key=lambda r: r["path"].casefold())

    # 重复检测：大小 + 首段指纹
    dupes: list[list[str]] = []
    if args.hash_bytes > 0:
        buckets: dict[tuple, list[str]] = {}
        for r in rows:
            if r.get("sha1_head") and r["kind"] in ("video", "image"):
                buckets.setdefault((r["size"], r["sha1_head"]), []).append(r["path"])
        dupes = [v for v in buckets.values() if len(v) > 1]

    total_bytes = sum(r["size"] for r in rows)
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1

    if args.json:
        print(json.dumps({
            "root": str(root),
            "files": rows,
            "skipped": skipped,
            "dirs": dirs,
            "duplicate_groups": dupes,
            "summary": {"files": len(rows), "bytes": total_bytes, "by_kind": by_kind},
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] {root}")
    print(f"     文件 {len(rows)} 个, {total_bytes / 1024 ** 3:.2f} GB, 子目录 {len(dirs)} 个")
    print(f"     类型: {by_kind}")
    if skipped:
        print(f"[WARN] 按 --skip-contains 跳过 {len(skipped)} 个: {skipped[:5]}"
              + (" ..." if len(skipped) > 5 else ""))
    bad = [r for r in rows if r.get("ftyp_ok") == "no" or r.get("zip_error")]
    if bad:
        print(f"[WARN] {len(bad)} 个文件头/压缩包异常: {[r['path'] for r in bad[:5]]}")
    if dupes:
        print(f"[WARN] 发现 {len(dupes)} 组疑似重复")
        for group in dupes[:5]:
            print("       " + " == ".join(group))
    else:
        print("[OK] 未发现重复（按大小 + 首段指纹）")

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fields = ["path", "kind", "size", "mtime", "mvhd_created", "mvhd_duration_s",
                  "ftyp_ok", "sha1_head", "zip_entries", "zip_entry_count",
                  "zip_entries_truncated", "zip_encrypted", "zip_error"]
        with out_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"[OK] 清单已写入 {out_path}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
