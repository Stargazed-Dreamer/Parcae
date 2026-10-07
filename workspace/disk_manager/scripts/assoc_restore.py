"""assoc_restore.py —— 文件关联分档恢复（诚实版）

把当前被改动/丢失的后缀，恢复到目标快照（默认基线）记录的生效程序。

为什么不用 reg import 导 UserChoice：UserChoice.Hash 绑定 SID+时间+开机密钥，
旧值导回会被 Windows 判哈希失败而忽略——这正是"备份了却恢复不回来"的根因。
这里改用**哈希无关**的两步可靠路径：

  A. 自动档（经典层，无哈希）：
     reg add  HKCU\\Software\\Classes\\<ext>  /ve /d <目标ProgID>   ← 设每用户经典默认
     reg delete HKCU\\...\\FileExts\\<ext>\\UserChoice                 ← 移除带哈希的覆盖，让经典层生效
     然后重新采集校验：生效 == 目标？ 是 -> [OK]
  B. 引导档（校验没通过，多为商店/UWP 应用 ProgID 解析不出）：
     打开 ms-settings:defaultapps 定位，让用户点一下重选。脚本不谎称已自动恢复。

安全：默认干跑（只打印计划）。加 --apply 才写，且逐个确认（--yes 跳过）。
UserChoice 删除前，快照的 raw/FileExts.reg 已留存原件，可取证/回退。

用法：
  uv run python workspace/disk_manager/scripts/assoc_restore.py                 # 干跑：看要恢复哪些
  uv run python workspace/disk_manager/scripts/assoc_restore.py --apply         # 执行（逐个确认）
  uv run python workspace/disk_manager/scripts/assoc_restore.py --apply --yes   # 执行（不逐个问）
  uv run python workspace/disk_manager/scripts/assoc_restore.py --ext .pdf --apply   # 只恢复 .pdf
  uv run python workspace/disk_manager/scripts/assoc_restore.py --vs 20260926_120000  # 恢复到指定快照
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Any, Optional

import assoc_common as C


def _run(args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def _set_classic(ext: str, progid: str) -> tuple[int, str]:
    return _run([
        "reg", "add", f"HKCU\\Software\\Classes\\{ext}",
        "/ve", "/t", "REG_SZ", "/d", progid, "/f",
    ])


def _delete_userchoice(ext: str) -> tuple[int, str]:
    return _run([
        "reg", "delete", f"HKCU\\{C.FILEEXTS_SUBKEY}\\{ext}\\UserChoice", "/f",
    ])


def _has_userchoice(ext: str) -> bool:
    return C.get_userchoice(ext)["progid"] is not None or C.get_userchoice(ext)["hash_present"]


def build_plan(target: dict[str, Any], ext_filter: Optional[str]) -> list[dict[str, Any]]:
    cur = C.build_inventory()
    changes = C.diff_inventories(target, cur)
    cur_by = {r["ext"]: r for r in cur["records"]}
    plan: list[dict[str, Any]] = []
    for ch in changes:
        ext = ch["ext"]
        if ext_filter and ext.lower() != ext_filter.lower():
            continue
        # 目标 ProgID 取快照里该 ext 的生效 progid
        tgt_rec = next((r for r in target["records"] if r["ext"] == ext), None)
        want = tgt_rec.get("effective_progid") if tgt_rec else None
        if not want:
            plan.append({"ext": ext, "kind": ch["kind"], "want": None,
                         "now": cur_by.get(ext, {}).get("effective_progid"),
                         "action": "skip-no-target"})
            continue
        now = cur_by.get(ext, {}).get("effective_progid")
        action = "set_classic" + ("+del_uc" if _has_userchoice(ext) else "")
        plan.append({"ext": ext, "kind": ch["kind"], "want": want, "now": now, "action": action})
    return plan


def _print_plan(plan: list[dict[str, Any]]) -> None:
    if not plan:
        print("[OK] 无差异，无需恢复。")
        return
    print(f"计划恢复 {len(plan)} 项：\n")
    for p in plan:
        if p["action"] == "skip-no-target":
            print(f"  [跳过] {p['ext']:<8} 快照无有效目标")
        else:
            print(f"  [{p['action']:<16}] {p['ext']:<8} {p['now'] or '(未设置)'}  ->  {p['want']}")


def _confirm(msg: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(msg).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def apply_plan(plan: list[dict[str, Any]], assume_yes: bool) -> list[str]:
    manual: list[str] = []
    for p in plan:
        if p["action"] == "skip-no-target":
            continue
        ext, want = p["ext"], p["want"]
        ok = _confirm(f"恢复 {ext} -> {want} ? [y/N] ", assume_yes)
        if not ok:
            print(f"  跳过 {ext}")
            continue
        rc, out = _set_classic(ext, want)
        if rc != 0:
            print(f"  [X] {ext} 设置经典层失败: {out}")
            manual.append(ext)
            continue
        if "del_uc" in p["action"]:
            rc2, out2 = _delete_userchoice(ext)
            if rc2 != 0 and "not found" not in out2.lower():
                print(f"  [!] {ext} 删除 UserChoice 返回: {out2}")
    return manual


def verify(plan: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    cur = {r["ext"]: r for r in C.build_inventory()["records"]}
    done: list[str] = []
    still_manual: list[str] = []
    for p in plan:
        if p["action"] == "skip-no-target":
            continue
        eff = cur.get(p["ext"], {}).get("effective_progid")
        if eff == p["want"]:
            done.append(p["ext"])
        else:
            still_manual.append(p["ext"])
            print(f"  [!] {p['ext']} 当前生效={eff}，未达目标 {p['want']}")
    return done, still_manual


def open_default_apps_settings() -> None:
    _run(["cmd", "/c", "start", "", "ms-settings:defaultapps"])


def main() -> None:
    import assoc_snapshot as S

    ap = argparse.ArgumentParser(description="文件关联分档恢复")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认干跑）")
    ap.add_argument("--yes", action="store_true", help="配合 --apply，逐个不询问")
    ap.add_argument("--ext", metavar=".pdf", help="只处理指定后缀")
    ap.add_argument("--vs", metavar="SNAP", help="恢复到指定快照（默认基线/最新）")
    args = ap.parse_args()

    if args.vs:
        ts, target = args.vs, C.load_inventory(args.vs)
    else:
        ts = S.current_baseline() or C.latest_snapshot()
        if not ts:
            print("没有目标快照可恢复。先 assoc_snapshot.py --set-baseline")
            sys.exit(2)
        target = C.load_inventory(ts)

    print(f"恢复目标快照： {ts}（{target.get('captured_at','?')}）")
    plan = build_plan(target, args.ext)
    _print_plan(plan)

    if not args.apply:
        print("\n（干跑，未改动系统。加 --apply 执行）")
        return

    if not plan:
        return
    if not _confirm("\n确认执行以上注册表恢复？[y/N] ", args.yes):
        print("已取消。")
        return

    print("\n执行恢复...")
    manual = apply_plan(plan, args.yes)
    done, still_manual = verify(plan)

    print("\n" + "=" * 56)
    print(f"[OK] 自动恢复成功 {len(done)} 项: {', '.join(done) or '（无）'}")
    if still_manual:
        print(f"[!] 需手动确认 {len(still_manual)} 项: {', '.join(still_manual)}")
        if _confirm("打开『设置 -> 默认应用』帮你手动重选？[y/N] ", args.yes):
            open_default_apps_settings()
            print("已打开设置页，请为上述后缀点选目标程序，完成后 assoc_diff.py 复核。")
    print("=" * 56)


if __name__ == "__main__":
    main()
