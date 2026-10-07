"""assoc_diff.py —— 文件关联查看 / 篡改对比

把"当前系统状态"和某份快照（默认基线）对比，列出**哪个后缀从什么程序变成了什么程序**。
这就是"查看备份情况 + 反篡改"的核心：软件偷改了什么，一眼看到。

用法：
  uv run python workspace/disk_manager/scripts/assoc_diff.py                 # 当前 vs 基线
  uv run python workspace/disk_manager/scripts/assoc_diff.py --vs 20260926_120000   # 当前 vs 指定快照
  uv run python workspace/disk_manager/scripts/assoc_diff.py --from A --to B         # 两份快照互比
  uv run python workspace/disk_manager/scripts/assoc_diff.py --show                   # 只列当前全部关联，不比对
"""

from __future__ import annotations

import argparse
from typing import Any, Optional

import assoc_common as C


def _label(rec: Optional[dict[str, Any]]) -> str:
    if not rec:
        return "(无)"
    progid = rec.get("effective_progid") or "(未解析)"
    exe = rec.get("exe")
    fr = rec.get("friendly")
    tail = exe or fr or ""
    return f"{progid}  [{tail}]" if tail else str(progid)


def _resolve_source(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    import assoc_snapshot as S

    if getattr(args, "vs", None):
        return args.vs, C.load_inventory(args.vs)
    base = S.current_baseline() or C.latest_snapshot()
    if not base:
        raise SystemExit("没有可比对的快照，请先 assoc_snapshot.py --set-baseline")
    return base, C.load_inventory(base)


def do_diff(args: argparse.Namespace) -> int:
    if args.from_snap and args.to_snap:
        src = {"name": args.from_snap, "inv": C.load_inventory(args.from_snap)}
        dst = {"name": args.to_snap, "inv": C.load_inventory(args.to_snap)}
        cur = None
    else:
        bname, binv = _resolve_source(args)
        src = {"name": bname, "inv": binv}
        cur = C.build_inventory()
        dst = {"name": "当前系统", "inv": cur}

    changes = C.diff_inventories(src["inv"], dst["inv"])
    print(f"对比： {src['name']}（{src['inv'].get('captured_at','?')}）  ->  {dst['name']}")
    print(f"基线后缀数 {src['inv'].get('count')}  |  {dst['inv'].get('captured_at', '当前')} 后缀数 {dst['inv'].get('count')}")
    print("=" * 64)

    if not changes:
        print("[OK] 无变化：当前关联与快照一致。")
        return 0

    print(f"[!] 检测到 {len(changes)} 处差异：\n")
    for ch in changes:
        kind = ch["kind"]
        b, a = ch["before"], ch["after"]
        if kind == "changed":
            print(f"  [改] {ch['ext']:<8}  {_label(b)}  ->  {_label(a)}")
        elif kind == "added":
            print(f"  [新增] {ch['ext']:<8}  ->  {_label(a)}")
        elif kind == "removed":
            print(f"  [消失] {ch['ext']:<8}  {_label(b)}  ->  (无)")
    print("\n提示： 想还原用 assoc_restore.py（默认只还原上面这些差异项）。")
    # 有差异返回 1，方便脚本/loop 判断
    return 1


def do_show() -> None:
    inv = C.build_inventory()
    recs = inv["records"]
    print(f"当前生效文件关联（{inv['count']} 个后缀，采集于 {inv['captured_at']}）\n")
    print(f"{'后缀':<8} {'生效ProgID':<28} {'UserChoice':<14} 程序")
    for r in recs:
        uc = "有" if r["userchoice_hash"] else "无"
        print(f"{r['ext']:<8} {str(r['effective_progid'] or '-'):<28} {uc:<14} {r['exe'] or r['friendly'] or '-'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="文件关联查看/篡改对比")
    ap.add_argument("--vs", metavar="SNAP", help="与指定快照比（默认基线/最新）")
    ap.add_argument("--from", dest="from_snap", metavar="SNAP", help="互比：旧快照")
    ap.add_argument("--to", dest="to_snap", metavar="SNAP", help="互比：新快照")
    ap.add_argument("--show", action="store_true", help="仅显示当前全部关联")
    args = ap.parse_args()

    if args.show:
        do_show()
        return
    raise SystemExit(do_diff(args))


if __name__ == "__main__":
    main()
