"""永久删除指定的 Windows 回收站条目 —— 带强制身份核对，默认 dry-run。

为什么必须「先核对再删」
----------------------
永久删除不可逆。回收站里可能同时躺着用户自己的重要东西，只按 ID 删是危险的：
ID 是 6 位随机串（删文件时还带原扩展名，如 `4X51EV.json`），人工抄写/记忆都不可靠。
本脚本要求**同时给出条目 ID 和它当初的原始路径**，并把 `$I` 里记录的路径与之比对，
**全部条目都核对通过才动手**（任一条不符则整体中止，绝不部分执行）。
核对通过才删 `$R`（数据体）+ `$I`（元数据）。

用法
----
    # 1) 先看（dry-run，默认）：确认将要删的是哪几条
    python recycle_purge.py --entry "UNALER=F:\\path\\to\\dir"
    # 2) 再加 --yes 真正永久删除
    python recycle_purge.py --entry "UNALER=F:\\path\\to\\dir" --yes

    # 多条
    python recycle_purge.py --entry "AAAAAA=F:\\a" --entry "BBBBBB.json=F:\\b.json" --yes

⚠️ 这一步**不会**再进回收站，空间会真正释放（脚本前后各报一次磁盘可用空间）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from recycle_audit import collect, recycle_root


def free_gib(drive: str) -> float:
    return shutil.disk_usage(f"{drive.rstrip(':\\/')}:/").free / 1024 ** 3


def norm(p: str) -> str:
    return os.path.normpath(p).casefold()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="永久删除回收站条目（强制身份核对）")
    ap.add_argument("--drive", default="F", help="盘符（默认 F）")
    ap.add_argument("--entry", action="append", required=True, metavar="ID=原始路径",
                    help="条目 ID 与它当初的原始路径，可重复")
    ap.add_argument("--yes", action="store_true", help="真正执行（缺省只 dry-run）")
    args = ap.parse_args(argv)

    if not sys.platform.startswith("win"):
        print("只支持 Windows", file=sys.stderr)
        return 2

    index = {e["id"]: e for e in collect(args.drive)}
    if not index:
        print(f"{recycle_root(args.drive)} 里没有任何可解析的条目", file=sys.stderr)
        return 2

    # ---- 第一步：全部核对，任何一条不符就整体中止 ----
    plans = []
    for raw in args.entry:
        if "=" not in raw:
            print(f"❌ --entry 格式应为 ID=路径，收到: {raw}", file=sys.stderr)
            return 2
        rid, expect = raw.split("=", 1)
        e = index.get(rid)
        if not e:
            print(f"❌ {rid}: 回收站里没有这个 ID —— 整体中止，未删任何东西", file=sys.stderr)
            print(f"   （用 recycle_audit.py 查实际 ID；注意删文件的 ID 带原扩展名）", file=sys.stderr)
            return 1
        ok = norm(e["original_path"]) == norm(expect) and e["payload_exists"]
        print(f'{"✅" if ok else "❌"} {rid}  {e["size_gib"]:.3f} GiB  '
              f'删除于 {e["deleted_at"]}')
        print(f'     $I 记录路径: {e["original_path"]}')
        print(f'     预期路径   : {expect}')
        print(f'     $R 数据体  : {"存在" if e["payload_exists"] else "缺失"}')
        if not ok:
            print("     → 身份不符，**整体中止**，未删任何东西", file=sys.stderr)
            return 1
        plans.append(e)

    print(f"\n[核对全部通过] 共 {len(plans)} 条，"
          f"合计 {sum(p['size'] for p in plans) / 1024 ** 3:.2f} GiB")

    if not args.yes:
        print("（dry-run，未执行。确认无误后加 --yes 真正删除）")
        return 0

    before = free_gib(args.drive)
    for e in plans:
        t0 = time.time()
        if os.path.isdir(e["payload"]):
            shutil.rmtree(e["payload"])
        else:
            os.remove(e["payload"])
        os.remove(e["i_file"])
        print(f'已永久删除 {e["id"]}: {e["size_gib"]:.3f} GiB  {time.time() - t0:.1f}s  '
              f'残留={os.path.exists(e["payload"])}')
    after = free_gib(args.drive)
    print(f"\n{args.drive}: 可用 {before:.2f} → {after:.2f} GiB"
          f"（实际释放 {after - before:.2f} GiB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
