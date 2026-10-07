"""全局磁盘 I/O 归因：两次采样求差，找出「谁在读/写」。

用法::

    python global_io_attrib.py [--window 25] [--top 25]
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

try:
    import psutil
except ImportError:
    print("NO_PSUTIL")
    sys.exit(1)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def proc_io(pid: int):
    """优先用 Win32（连管理员/受保护进程也能读），失败回退 psutil。"""
    for access in (PROCESS_QUERY_LIMITED_INFORMATION,):
        h = k32.OpenProcess(access, False, pid)
        if not h:
            continue
        c = IO_COUNTERS()
        ok = k32.GetProcessIoCounters(h, ctypes.byref(c))
        k32.CloseHandle(h)
        if ok:
            return (c.ReadTransferCount, c.WriteTransferCount,
                    c.ReadOperationCount, c.WriteOperationCount)
    try:
        p = psutil.Process(pid)
        io = p.io_counters()
        return (io.read_bytes, io.write_bytes, io.read_count, io.write_count)
    except Exception:
        return None


def snapshot():
    out = {}
    for p in psutil.process_iter(["pid", "name", "cmdline", "username"]):
        pid = p.info["pid"]
        vals = proc_io(pid)
        if vals is None:
            continue
        out[pid] = {
            "name": p.info.get("name") or "?",
            "cmd": " ".join(p.info.get("cmdline") or [])[:120],
            "r_b": vals[0], "w_b": vals[1], "r_op": vals[2], "w_op": vals[3],
        }
    return out

def disk_total():
    """整盘累计 I/O（psutil 在某些平台可能返回 None）。"""
    d = psutil.disk_io_counters()
    if d is None:
        raise SystemExit("无法读取整盘 I/O 计数器（psutil.disk_io_counters 返回 None）")
    return d


def fmt(n: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f PiB" % n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=25.0)
    ap.add_argument("--top", type=int, default=22)
    args = ap.parse_args()

    d0 = disk_total()
    a = snapshot()
    t0 = time.time()
    time.sleep(args.window)
    b = snapshot()
    t1 = time.time()
    d1 = disk_total()
    dt = max(1e-6, t1 - t0)

    rows = []
    for pid, cur in b.items():
        prev = a.get(pid)
        if prev is None:
            # 窗口内新起的进程：把它的累计值当成本窗口写入（列为"新进程"）
            rows.append({
                "pid": pid, "name": cur["name"], "cmd": cur["cmd"],
                "dw": cur["w_b"], "dr": cur["r_b"],
                "dwo": cur["w_op"], "dro": cur["r_op"], "new": True,
            })
            continue
        dw = cur["w_b"] - prev["w_b"]
        dr = cur["r_b"] - prev["r_b"]
        if dw <= 0 and dr <= 0:
            continue
        rows.append({
            "pid": pid, "name": cur["name"], "cmd": cur["cmd"],
            "dw": max(0, dw), "dr": max(0, dr),
            "dwo": max(0, cur["w_op"] - prev["w_op"]),
            "dro": max(0, cur["r_op"] - prev["r_op"]), "new": False,
        })

    sw = sum(r["dw"] for r in rows)
    sr = sum(r["dr"] for r in rows)
    disk_w = d1.write_bytes - d0.write_bytes
    disk_r = d1.read_bytes - d0.read_bytes

    print("=" * 92)
    print("全局磁盘 I/O 归因   窗口 %.1fs" % dt)
    print("=" * 92)
    print("整盘物理 I/O : 写 %-12s 读 %-12s   (写 %.1f MiB/s)"
          % (fmt(disk_w), fmt(disk_r), disk_w / dt / 1048576))
    print("进程累计之和 : 写 %-12s 读 %-12s" % (fmt(sw), fmt(sr)))
    gap_w = disk_w - sw
    print("差额(整盘-进程): 写 %-12s  ⇒ %s"
          % (fmt(gap_w),
             "大头在进程内" if abs(gap_w) < sw * 0.3 else
             ("有大块不在任何进程内（内核/页文件/缓存回写）" if gap_w > 0 else "进程读数偏高")))
    print()
    print("【写 Top %d】" % args.top)
    print("%-10s %-22s %12s %10s %10s  %s" % ("PID", "进程", "写/s", "写次数/s", "窗口写入", "命令行"))
    print("-" * 92)
    for r in sorted(rows, key=lambda x: -x["dw"])[: args.top]:
        if r["dw"] < 4096:
            break
        print("%-10d %-22s %12s %10.1f %10s  %s"
              % (r["pid"], r["name"][:22], fmt(r["dw"] / dt), r["dwo"] / dt,
                 fmt(r["dw"]), r["cmd"][:46]))
    print()
    print("【读 Top %d】" % min(args.top, 15))
    print("%-10s %-22s %12s %10s %10s  %s" % ("PID", "进程", "读/s", "读次数/s", "窗口读取", "命令行"))
    print("-" * 92)
    for r in sorted(rows, key=lambda x: -x["dr"])[: min(args.top, 15)]:
        if r["dr"] < 4096:
            break
        print("%-10d %-22s %12s %10.1f %10s  %s"
              % (r["pid"], r["name"][:22], fmt(r["dr"] / dt), r["dro"] / dt,
                 fmt(r["dr"]), r["cmd"][:46]))
    print()
    print("--- 分盘 ---")
    for name, c in sorted(psutil.disk_io_counters(perdisk=True).items()):
        if "PhysicalDrive" not in name:
            continue
        print("  %-16s 累计写 %8.2f GiB  读 %8.2f GiB"
              % (name, c.write_bytes / 2**30, c.read_bytes / 2**30))
    print()
    print("--- 内存/页文件压力（写放大常见来源）---")
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    print("  物理内存 %.1f/%.1f GiB (%.0f%%)   页文件 used %.2f GiB / total %.2f GiB"
          % (vm.used / 2**30, vm.total / 2**30, vm.percent,
             sm.used / 2**30, sm.total / 2**30))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
