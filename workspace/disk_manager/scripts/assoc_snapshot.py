"""assoc_snapshot.py —— 文件关联快照（备份层）

做两件事：
  1. 无损快照原始注册表 hive：reg save 出 FileExts 与 HKCU\\Software\\Classes 的 .dat
     （含 UserChoice 的 Hash 二进制，供取证/极端情况下挂载恢复）
  2. 生成人类可读清单 inventory.json + associations.csv（后缀→生效程序/exe/命令行）

清单是 diff 与恢复的比对基准；.dat 是"保险箱里的原件"。

用法：
  uv run python workspace/disk_manager/scripts/assoc_snapshot.py            # 建一次快照
  uv run python workspace/disk_manager/scripts/assoc_snapshot.py --set-baseline   # 同时把该快照标记为基线
  uv run python workspace/disk_manager/scripts/assoc_snapshot.py --list      # 列出历史快照
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from typing import Optional

import assoc_common as C


def _reg_export(key: str, dst: str, label: str) -> bool:
    """reg export 导出原件快照（免管理员；UserChoice Hash 以 hex 形式保留）。

    注：reg save 需要备份/还原特权（管理员），故用 reg export。恢复并不依赖这份原件——
    恢复走 assoc_restore 的 reg add + reg delete（哈希无关）；此文件仅作取证/极端回退。
    """
    try:
        # ⛔ reg.exe 输出是 GBK：text=True 会 UnicodeDecodeError
        r = subprocess.run(
            ["reg", "export", key, dst, "/y"],
            capture_output=True, timeout=120,
        )
        err_bytes = r.stderr or b""
        err = err_bytes.decode("gbk", "replace").strip()
        ok = r.returncode == 0
        print(f"  [{'已快照' if ok else '失败'}] {label}" + ("" if ok else f": {err}"))
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] {label}: {e}")
        return False


def snapshot(set_baseline: bool = False) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = C.snapshot_dir(ts)
    raw = d / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    print(f"文件关联快照 -> {d}")
    print("\n[1/3] 快照原始注册表 (reg export，免管理员)")
    _reg_export("HKCU\\" + C.FILEEXTS_SUBKEY, str(raw / "FileExts.reg"), "FileExts (UserChoice)")
    _reg_export("HKCU\\Software\\Classes", str(raw / "HKCU_Classes.reg"), "HKCU\\Software\\Classes (经典 ProgID)")

    print("\n[2/3] 采集生效关联清单")
    inv = C.build_inventory()
    C.save_inventory(ts, inv)
    print(f"  共 {inv['count']} 个后缀 -> inventory.json / associations.csv")

    print("\n[3/3] 基线")
    if set_baseline:
        (C.ASSOC_ROOT / "baseline.json").write_text(
            __import__("json").dumps({"ts": ts, "captured_at": inv["captured_at"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  已标记基线 = {ts}")
    else:
        base = current_baseline()
        print(f"  当前基线 = {base or '（未设置，用 --set-baseline 设定）'}")
    return ts


def current_baseline() -> Optional[str]:
    import json

    p = C.ASSOC_ROOT / "baseline.json"
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("ts")
        return ts if isinstance(ts, str) else None
    except Exception:
        return None


def list_snapshots() -> None:
    snaps = C.list_snapshots()
    base = current_baseline()
    if not snaps:
        print("还没有任何快照，先运行： assoc_snapshot.py --set-baseline")
        return
    latest = C.latest_snapshot()
    print(f"{'时间戳':<20} {'后缀数':>6}  标记")
    for s in snaps:
        try:
            n = C.load_inventory(s).get("count", "?")
        except Exception:
            n = "?"
        marks = []
        if s == base:
            marks.append("基线")
        if s == latest:
            marks.append("最新")
        print(f"{s:<20} {str(n):>6}  {' '.join(marks)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="文件关联快照")
    ap.add_argument("--set-baseline", action="store_true", help="把本次快照设为基线")
    ap.add_argument("--list", action="store_true", help="列出历史快照")
    args = ap.parse_args()

    if args.list:
        list_snapshots()
        return

    if not C.BACKUP_BASE.exists():
        print(f"[!] 备份盘 {C.BACKUP_BASE} 不存在，快照将尝试创建；如路径不对请改 assoc_common.BACKUP_BASE")
    C.ASSOC_ROOT.mkdir(parents=True, exist_ok=True)
    snapshot(set_baseline=args.set_baseline)


if __name__ == "__main__":
    main()
