"""watch.py —— 设置看门狗统一 CLI

一条命令驱动所有 provider（file_associations / env_path / url_browser / autostart）：

  snapshot   采集当前状态存快照（--set-baseline 同时设为基线）
  diff       当前 vs 基线（或 --vs 指定快照），列出被偷偷改动的项；有改动返回码 1
  restore    按基线产出恢复动作（默认干跑；--apply 执行自动档，逐个确认，--yes 免问）
  list       列出内置条目
  status     看基线时间与各条目快照记录数

约定：打印只用 ASCII 符号 + 中文；reg 写走 subprocess 列表形式（见 base/items）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import base  # noqa: E402
import items  # noqa: E402,F401  (导入即注册 providers)


def _pick_items(item_arg: Optional[str]) -> list[str] | None:
    if not item_arg:
        return None
    ids = [s.strip() for s in item_arg.split(",") if s.strip()]
    known = {it.id for it in base.all_items()}
    bad = [i for i in ids if i not in known]
    if bad:
        print(f"[X] 未知条目: {', '.join(bad)}；可选: {', '.join(sorted(known))}")
        sys.exit(2)
    return ids


def _target_records(item_id: str, vs: Optional[str]) -> tuple[Optional[str], list[dict[str, Any]]]:
    """返回 (快照ts, records)。vs 指定则用之，否则用基线。找不到返回 (None, [])。"""
    if vs:
        snap = base.load_snapshot(vs)
    else:
        ts = base.current_baseline() or base.latest_snapshot()
        if not ts:
            return None, []
        snap = base.load_snapshot(ts)
    ent = snap.get("items", {}).get(item_id)
    if not ent:
        return ts, []
    return ts, ent.get("records", [])


# ============================ snapshot ============================

def cmd_snapshot(args: argparse.Namespace) -> int:
    ids = _pick_items(args.item)
    data = base.build_snapshot(ids)
    ts = base.new_ts()
    p = base.save_snapshot(ts, data)
    print(f"[OK] 快照已保存: {ts}  ({p})")
    for iid, ent in data["items"].items():
        flag = f"错误:{ent['error']}" if ent.get("error") else f"{ent['count']} 条"
        print(f"   - {iid}: {flag}")
    if args.set_baseline:
        base.set_baseline(ts)
        print(f"[OK] 基线已设为 {ts}")
    return 0


# ============================ diff ============================

def cmd_diff(args: argparse.Namespace) -> int:
    ids = _pick_items(args.item)
    ts_used: Optional[str] = None
    total = 0
    for it in base.all_items():
        if ids and it.id not in ids:
            continue
        ts, target = _target_records(it.id, args.vs)
        if not target:
            print(f"[!] {it.id}: 无基线/目标快照，先运行 snapshot --set-baseline")
            continue
        ts_used = ts
        cur = it.collect()
        changes = base.diff_records(it, target, cur)
        if not changes:
            print(f"[OK] {it.id}: 无变化（vs {ts}）")
            continue
        total += len(changes)
        print(f"[!] {it.id}: {len(changes)} 处变化（vs {ts}）")
        for ch in changes:
            print("     " + ch.detail)
    if ts_used:
        print(f"\n对比基准快照: {ts_used}")
    return 1 if total else 0


# ============================ restore ============================

def _confirm(msg: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(msg).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_restore(args: argparse.Namespace) -> int:
    ids = _pick_items(args.item)
    all_manual: list[str] = []
    ran_any = False
    for it in base.all_items():
        if ids and it.id not in ids:
            continue
        if not it.supports_restore:
            print(f"[i] {it.id}: 该条目仅对比，不支持恢复")
            continue
        ts, target = _target_records(it.id, args.vs)
        if not target:
            print(f"[!] {it.id}: 无基线，无法恢复")
            continue
        cur = it.collect()
        ops = it.restore_ops(target, cur)
        if not ops:
            print(f"[OK] {it.id}: 无差异，无需恢复")
            continue
        auto = [o for o in ops if not o.manual]
        manual = [o for o in ops if o.manual]
        print(f"[!] {it.id}: {len(auto)} 项可自动恢复, {len(manual)} 项需手动（基准 {ts}）")
        for o in ops:
            tag = "手动" if o.manual else "自动"
            print(f"     [{tag}] {o.desc}")
        all_manual += [o.desc for o in manual]
        if not args.apply:
            continue
        if not auto:
            continue
        if not _confirm(f"  执行 {it.id} 的 {len(auto)} 项自动恢复? [y/N] ", args.yes):
            print("     已跳过")
            continue
        ran_any = True
        for o in auto:
            ok, detail = o.run()
            print(f"     [{'OK' if ok else 'X'}] {detail}")

    if not args.apply:
        print("\n（干跑，未改动系统。加 --apply 执行自动档；手动档始终需你处理）")
    elif ran_any:
        print("\n复核（自动恢复后再 diff）：")
        rc = cmd_diff(argparse.Namespace(item=args.item, vs=args.vs))
        return rc
    if all_manual:
        print(f"\n仍需手动处理 {len(all_manual)} 项，见上面标 [手动]。")
    return 0


# ============================ list / status ============================

def cmd_list(_args: argparse.Namespace) -> int:
    print("内置条目：")
    for it in base.all_items():
        r = "支持恢复" if it.supports_restore else "仅对比"
        print(f"  - {it.id:<16} [{it.kind:<9}] {it.name}  ({r})")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ids = _pick_items(args.item)
    ts = base.current_baseline()
    snaps = base.list_snapshots()
    print(f"基线: {ts or '(未设置)'}")
    print(f"快照数: {len(snaps)}" + (f"  最新: {snaps[-1]}" if snaps else ""))
    ref = args.vs or ts or (snaps[-1] if snaps else None)
    if not ref:
        print("无快照可显示记录数。")
        return 0
    snap = base.load_snapshot(ref)
    print(f"（记录数取自 {ref}）")
    for iid, ent in snap.get("items", {}).items():
        if ids and iid not in ids:
            continue
        err = f" 错误:{ent['error']}" if ent.get("error") else ""
        print(f"  - {iid:<16} {ent.get('count', 0)} 条{err}")
    return 0


def _apply_root_override(root_arg: Optional[str]) -> None:
    if root_arg:
        base.WATCH_ROOT = Path(root_arg)
    elif os.environ.get("SWATCH_ROOT"):
        base.WATCH_ROOT = Path(os.environ["SWATCH_ROOT"])


def main() -> None:
    ap = argparse.ArgumentParser(description="设置看门狗（备份/篡改对比/恢复）")
    ap.add_argument("--root", metavar="DIR", help="覆盖快照根目录（测试用；也可用环境变量 SWATCH_ROOT）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot", help="采集快照")
    p.add_argument("--item", help="仅这些条目，逗号分隔")
    p.add_argument("--set-baseline", action="store_true", help="同时设为基线")

    p = sub.add_parser("diff", help="当前 vs 基线，列出改动")
    p.add_argument("--item")
    p.add_argument("--vs", metavar="TS", help="对比指定快照而非基线")

    p = sub.add_parser("restore", help="按基线恢复（默认干跑）")
    p.add_argument("--item")
    p.add_argument("--vs", metavar="TS", help="恢复到指定快照")
    p.add_argument("--apply", action="store_true", help="真正执行自动档")
    p.add_argument("--yes", action="store_true", help="配合 --apply 不逐个询问")

    sub.add_parser("list", help="列出条目")

    p = sub.add_parser("status", help="基线/快照概况")
    p.add_argument("--item")
    p.add_argument("--vs", metavar="TS", help="看指定快照的记录数")

    args = ap.parse_args()
    _apply_root_override(args.root)

    handlers = {
        "snapshot": cmd_snapshot,
        "diff": cmd_diff,
        "restore": cmd_restore,
        "list": cmd_list,
        "status": cmd_status,
    }
    sys.exit(handlers[args.cmd](args))


if __name__ == "__main__":
    main()
