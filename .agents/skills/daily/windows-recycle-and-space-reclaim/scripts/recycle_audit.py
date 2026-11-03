"""列出 Windows 回收站条目（原始路径 / 大小 / 删除时间 / 数据体是否完好）。

为什么需要它
------------
Windows 资源管理器看不出「回收站里哪条是我刚才删的那个大目录」，而
「进回收站」并不释放磁盘空间 —— 要真正回收空间必须先认出条目、再永久删除。
本脚本把 `$I` 元数据解析出来，让「认条目」这一步变成一条命令。

`$I` 元数据格式（v1/v2 布局相同，v2 = Windows 10+）
--------------------------------------------------
    offset  0  int64   版本（1 或 2）
    offset  8  int64   数据体字节数（$R 项）
    offset 16  int64   删除时间，FILETIME（**UTC**，100ns 起算于 1601-01-01）
    offset 24  int32   路径长度，**单位是「字符数」且含结尾 \\0，不是字节数**
    offset 28  ...     原始路径，UTF-16LE，以 \\0 结尾

三个已经踩过的坑（都别再踩）
---------------------------
1. **长度字段是字符数不是字节数**。按字节数读会把路径读成半截（`...\\wechatDec`），
   于是按路径搜索**永远 0 命中**，看起来像"回收站里没有这条"。本脚本按字符数走，
   并带 `28 + 2n == len(file)` 一致性校验，不过则按剩余字节兜底。
2. **FILETIME 是 UTC**，直接格式化会显示成 8 小时前的时刻（东八区）。
3. **`$R` 必须原样带扩展名**。删文件时 Windows 保留原扩展名（`$I4X51EV.json`
   对应 `$R4X51EV.json`），删目录时没有扩展名。ID 一律取 `$I` 后面全部内容，
   去掉扩展名就找不到数据体。

用法
----
    python recycle_audit.py                      # 列全部（默认 F 盘）
    python recycle_audit.py --drive F --filter sync_v3
    python recycle_audit.py --json               # 给机器读
    python recycle_audit.py --sid S-1-5-21-...   # 多用户目录时指定
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import struct
import sys

FILETIME_EPOCH = datetime.datetime(1601, 1, 1)


def recycle_root(drive: str) -> str:
    """`--drive F` / `F:` / `F:/` 一律归一成 `<data_drive>:\$RECYCLE.BIN`。

    ⚠️ 曾经写成 `os.path.join(drive + os.sep, '$RECYCLE.BIN')` —— 少了个冒号，
    变成相对路径 `F\\$RECYCLE.BIN`，`isdir` 恒为 False，脚本静默报"回收站为空"。
    """
    d = drive.rstrip(":\\/")
    return os.path.join(f"{d}:{os.sep}", "$RECYCLE.BIN")


def sid_dirs(drive: str) -> list[str]:
    """返回该盘回收站里所有含 `$I` 记录的 SID 目录。"""
    root = recycle_root(drive)
    if not os.path.isdir(root):
        return []
    out = []
    for d in sorted(glob.glob(os.path.join(root, "S-*"))):
        if os.path.isdir(d) and glob.glob(os.path.join(d, "$I*")):
            out.append(d)
    return out


def read_i_meta(path: str):
    """解析一个 `$I` 文件 -> (ver, size, when_local, original_path)；解析不了返回 None。"""
    b = open(path, "rb").read()
    if len(b) < 28:
        return None
    ver = struct.unpack_from("<q", b, 0)[0]
    if ver not in (1, 2):
        return None
    size = struct.unpack_from("<q", b, 8)[0]
    ft = struct.unpack_from("<q", b, 16)[0]
    n = struct.unpack_from("<i", b, 24)[0]
    if n <= 0 or 28 + 2 * n != len(b):          # 一致性校验，不过则按剩余字节兜底
        n = (len(b) - 28) // 2
    name = b[28:28 + 2 * n].decode("utf-16-le", "replace").rstrip("\x00")
    when_utc = FILETIME_EPOCH + datetime.timedelta(microseconds=ft // 10)
    offset = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
    return ver, size, when_utc + offset, name          # 换算成本地时间


def count_files(path: str) -> int:
    if os.path.isfile(path):
        return 1
    total = 0
    for _root, _dirs, files in os.walk(path):
        total += len(files)
    return total


def collect(drive: str = "F", sid: str | None = None) -> list[dict]:
    """返回该盘回收站全部条目（已解析元数据）。供本模块与 purge 复用。"""
    dirs = sid_dirs(drive)
    if sid:
        dirs = [d for d in dirs if os.path.basename(d) == sid]
    entries = []
    for d in dirs:
        for ip in glob.glob(os.path.join(d, "$I*")):
            meta = read_i_meta(ip)
            if not meta:
                continue
            ver, size, when, name = meta
            rid = os.path.basename(ip)[2:]          # ⚠️ 原样保留扩展名
            rp = os.path.join(d, "$R" + rid)
            entries.append({
                "id": rid,
                "sid": os.path.basename(d),
                "sid_dir": d,
                "i_file": ip,
                "size": size,
                "size_gib": round(size / 1024 ** 3, 3),
                "deleted_at": when.strftime("%Y-%m-%d %H:%M:%S"),
                "original_path": name,
                "payload_exists": os.path.exists(rp),
                "payload": rp,
                "file_count": count_files(rp) if os.path.exists(rp) else 0,
            })
    entries.sort(key=lambda e: e["deleted_at"], reverse=True)
    return entries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="列出 Windows 回收站条目")
    ap.add_argument("--drive", default="F", help="盘符（默认 F）")
    ap.add_argument("--sid", default=None, help="指定 SID 目录（多用户时用）")
    ap.add_argument("--filter", default=None, help="只列原始路径含该子串的条目（大小写不敏感）")
    ap.add_argument("--limit", type=int, default=0, help="最多列几条，0=全部")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if not sys.platform.startswith("win"):
        print("只支持 Windows", file=sys.stderr)
        return 2

    dirs = sid_dirs(args.drive)
    if not dirs:
        print(f"{recycle_root(args.drive)} 不存在或没有任何 $I 记录", file=sys.stderr)
        return 2
    if len(dirs) > 1 and not args.sid:
        print(f"该盘有 {len(dirs)} 个 SID 目录，请用 --sid 指定：", file=sys.stderr)
        for d in dirs:
            print("   ", os.path.basename(d), file=sys.stderr)
        return 2

    entries = collect(args.drive, args.sid)
    if args.filter:
        entries = [e for e in entries if args.filter.lower() in e["original_path"].lower()]
    if args.limit:
        entries = entries[: args.limit]

    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0

    total = 0
    for e in entries:
        total += e["size"]
        flag = "数据体OK" if e["payload_exists"] else "数据体缺失"
        print(f'{e["id"]:<16s} {e["size_gib"]:>8.3f} GiB  {e["deleted_at"]}  {flag}  '
              f'{e["file_count"]} 个文件')
        print(f'    原路径: {e["original_path"]}')
    print(f'\n共 {len(entries)} 条，合计 {total / 1024 ** 3:.2f} GiB')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
