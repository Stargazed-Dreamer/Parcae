"""assoc_restore.py —— 文件关联分档恢复（2026-10-11 实证校准版）

把当前被改动/丢失的后缀，恢复到目标快照记录的生效程序。

为什么不用 reg import 导 UserChoice：UserChoice.Hash 绑定 SID+后缀+ProgID+写入时刻，
旧值导回会被 Windows 判哈希失败而忽略——这正是"备份了却恢复不回来"的根因。

★ 2026-10-11 校准（详见 settings_watch/FILE_ASSOCIATION.md）：
  哈希无关两步法（设经典层 + 删 UserChoice）**只对文档类后缀成立**。
  音视频/图像被 Windows 的「按类别默认 AppX」盯着：UserChoice 一删，+15 秒就被
  电影和电视/媒体播放器/照片 补回去。所以本脚本现在分三档：
    - 可自动修： non-contended 且目标程序还在 → 设经典层 + winreg 删 UserChoice
    - 需引导  ： 有类别竞争(video/audio/image) → 只能让目标程序自己关联，脚本不假装成功
    - 跳过    ： 快照里就没有目标 或 目标程序已不存在（免得白修）
  并且 verify 默认**延时 20 秒**再做（旧版紧跟 apply 执行，会把"马上要被抢回去"误报成成功）。

安全：默认干跑（只打印计划）。加 --apply 才写。UserChoice 删除前快照的 raw/FileExts.reg 已留存。

用法：
  uv run python workspace/disk_manager/scripts/assoc_restore.py                 # 干跑：看计划
  uv run python workspace/disk_manager/scripts/assoc_restore.py --apply         # 执行
  uv run python workspace/disk_manager/scripts/assoc_restore.py --apply --yes   # 不逐个问
  uv run python workspace/disk_manager/scripts/assoc_restore.py --ext .pdf --apply
  uv run python workspace/disk_manager/scripts/assoc_restore.py --vs 20260926_120000
  uv run python workspace/disk_manager/scripts/assoc_restore.py --apply --settle 30
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Optional

import assoc_common as C


def _set_classic(ext: str, progid: str) -> tuple[int, str]:
    # ⛔ reg.exe 输出是 GBK，必须走 run_reg_gbk（旧版 text=True 会把中文报错吞掉）
    return C.run_reg_gbk(
        "add", f"HKCU\\Software\\Classes\\{ext}",
        "/ve", "/t", "REG_SZ", "/d", progid, "/f",
    )


def _has_userchoice(ext: str) -> bool:
    uc = C.get_userchoice(ext)
    return uc["progid"] is not None or uc["hash_present"]


def build_plan(target: dict[str, Any], ext_filter: Optional[str]) -> list[dict[str, Any]]:
    cur = C.build_inventory()
    changes = C.diff_inventories(target, cur)
    cur_by = {r["ext"]: r for r in cur["records"]}
    plan: list[dict[str, Any]] = []
    for ch in changes:
        ext = ch["ext"]
        if ext_filter and ext.lower() != ext_filter.lower():
            continue
        tgt_rec = next((r for r in target["records"] if r["ext"] == ext), None)
        want = tgt_rec.get("effective_progid") if tgt_rec else None
        now = C.effective_app(ext)
        base = {
            "ext": ext, "kind": ch["kind"], "want": want,
            "want_app": (tgt_rec or {}).get("friendly") or (tgt_rec or {}).get("exe"),
            "now": cur_by.get(ext, {}).get("effective_progid"),
            "now_app": now["friendly"],
        }
        # 同一程序的不同 ProgID 变体（尤其 AppX 会随着系统更新换 Interop ID）不算真的变了
        want_app = base["want_app"]
        already = bool(want_app) and now["friendly"] == want_app
        if already:
            plan.append({**base, "action": "skip-equivalent", "reason": "已是目标程序（ProgID 变体差异）"})
            continue
        # 两边都是 AppX：这类 ProgID（Interop ID）会随系统更新变化，无法判定是否同一应用，
        # 也不该由脚本去改（改了也可能只是换个 ID），交给人工确认后重设基线。
        if want and now["progid"] and want.startswith("AppX") and now["progid"].startswith("AppX"):
            plan.append({
                **base, "action": "skip-appx-variant",
                "reason": "AppX Interop ID 随系统更新变化，无法判定是否同一应用；确认现状后重设基线",
            })
            continue
        if not want:
            base.update(action="skip-no-target", reason="快照里没有有效目标")
        else:
            alive, why = C.progid_alive(want)
            if not alive:
                base.update(action="skip-target-missing", reason=why)
            elif C.is_contended(ext):
                base.update(
                    action="manual-app-claim",
                    reason=f"Windows 有类别默认 AppX（{C.perceived_type(ext)}），"
                           f"删了也会被 电影和电视/媒体播放器/照片 补位，只能让目标程序自己关联",
                )
            else:
                base.update(
                    action="set_classic" + ("+del_uc" if _has_userchoice(ext) else ""),
                    reason="",
                )
        plan.append(base)
    return plan


_GROUP = [
    ("set_classic", "可自动修复（写经典层 + 删 UserChoice）"),
    ("manual-app-claim", "需引导：让目标程序自己关联（注册表改不动）"),
    ("skip-appx-variant", "跳过：AppX Interop ID 变体（无法判定同一应用）"),
    ("skip-equivalent", "无需处理：已是目标程序"),
    ("skip-target-missing", "跳过：目标程序已不存在"),
    ("skip-no-target", "跳过：快照无目标"),
]


def _print_plan(plan: list[dict[str, Any]]) -> None:
    if not plan:
        print("[OK] 无差异，无需恢复。")
        return
    print(f"共 {len(plan)} 项差异：\n")
    for prefix, title in _GROUP:
        items = [p for p in plan if p["action"].startswith(prefix)]
        if not items:
            continue
        print(f"── {title}：{len(items)} 项")
        for p in items:
            line = f"  {p['ext']:<10} 现在={str(p['now_app'] or p['now'] or '(未设置)')[:18]:<18} -> 目标={str(p['want_app'] or p['want'])[:28]}"
            if p["reason"]:
                line += f"\n        └ {p['reason'][:100]}"
            print(line)
        print()


def _confirm(msg: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(msg).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def apply_plan(plan: list[dict[str, Any]], assume_yes: bool) -> list[dict[str, Any]]:
    """只执行「可自动修复」那一档；其它档在计划里已说明，不动手。"""
    executed: list[dict[str, Any]] = []
    for p in plan:
        if not p["action"].startswith("set_classic"):
            continue
        ext, want = p["ext"], p["want"]
        if not _confirm(f"恢复 {ext} -> {want} ? [y/N] ", assume_yes):
            print(f"  跳过 {ext}")
            continue
        rc, out = _set_classic(ext, want)
        if rc != 0:
            p["result"] = f"设置经典层失败: {out}"
            print(f"  [X] {ext} {p['result']}")
            continue
        msg = "经典层已设"
        if "del_uc" in p["action"]:
            ok, why = C.delete_userchoice_key(ext)
            msg += f"；删 UserChoice {'成功' if ok else '失败: ' + why}"
        p["result"] = msg
        executed.append(p)
        print(f"  [v] {ext}: {msg}")
    return executed


def verify(plan: list[dict[str, Any]], executed: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """用 Windows 官方判定复核。**必须在延时之后调用**，否则会把「即将被抢回」误报成成功。"""
    done: list[str] = []
    failed: list[str] = []
    for p in executed:
        now = C.effective_app(p["ext"])
        hit_progid = now["progid"] == p["want"]
        hit_app = bool(p["want_app"]) and now["friendly"] == p["want_app"]
        if hit_progid or hit_app:
            done.append(p["ext"])
        else:
            failed.append(p["ext"])
            print(f"  [!] {p['ext']} 当前生效={now['friendly'] or now['progid']}，"
                  f"未达目标 {p['want_app'] or p['want']}")
    return done, failed


def main() -> None:
    import assoc_snapshot as S

    ap = argparse.ArgumentParser(description="文件关联分档恢复")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认干跑）")
    ap.add_argument("--yes", action="store_true", help="配合 --apply，逐个不询问")
    ap.add_argument("--ext", metavar=".pdf", help="只处理指定后缀")
    ap.add_argument("--vs", metavar="SNAP", help="恢复到指定快照（默认基线/最新）")
    ap.add_argument("--settle", type=int, default=20,
                    help="写入后静置多少秒再复核（默认 20，用于抓 Windows 自动补位；设 0 关闭）")
    args = ap.parse_args()

    if args.vs:
        ts, target = args.vs, C.load_inventory(args.vs)
    else:
        ts = S.current_baseline() or C.latest_snapshot()
        if not ts:
            print("没有目标快照可恢复。先 assoc_snapshot.py --set-baseline")
            sys.exit(2)
        target = C.load_inventory(ts)

    print(f"恢复目标快照： {ts}（{target.get('captured_at', '?')}）\n")
    plan = build_plan(target, args.ext)
    _print_plan(plan)

    auto = [p for p in plan if p["action"].startswith("set_classic")]
    manual = [p for p in plan if p["action"] == "manual-app-claim"]
    skipped = [p for p in plan if p["action"].startswith("skip")]
    print(f"汇总：可自动修复 {len(auto)} / 需引导 {len(manual)} / 跳过 {len(skipped)}")

    if manual:
        print("\n以下后缀注册表改不动，请让对应程序自己关联一次"
              "（例：爱奇艺万能联播 → 设置 → 文件关联 → 全选 → 确定；VLC → 工具 → 首选项 → 设置关联）：")
        print("   " + ", ".join(p["ext"] for p in manual))
    if skipped:
        missing = [p["ext"] for p in skipped if p["action"] == "skip-target-missing"]
        if missing:
            print(f"\n以下后缀目标程序已不在本机，关联修好也打不开（属软件被卸载/挪走）：")
            print("   " + ", ".join(missing))

    if not args.apply:
        print("\n（干跑，未改动系统。加 --apply 执行）")
        return
    if not plan:
        return
    if not _confirm("\n确认对「可自动修复」那一档执行注册表恢复？[y/N] ", args.yes):
        print("已取消。")
        return

    print("\n执行恢复...")
    executed = apply_plan(plan, args.yes)
    if executed and args.settle > 0:
        print(f"\n静置 {args.settle} 秒，等 Windows 有没有把 AppX 补回来...")
        time.sleep(args.settle)
    print("\n复核（用 Windows 官方判定，不是注册表读数）：")
    done, failed = verify(plan, executed)

    print("\n" + "=" * 60)
    print(f"[OK] 已生效 {len(done)} 项: {', '.join(done) or '（无）'}")
    if failed:
        print(f"[!] 未达目标 {len(failed)} 项: {', '.join(failed)}")
        print("    这些多半是有类别竞争或被 AppX 接管，请按上面的「让目标程序自己关联」处理。")
    print("=" * 60)


if __name__ == "__main__":
    main()
