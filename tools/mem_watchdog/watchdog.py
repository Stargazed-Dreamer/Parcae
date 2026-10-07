# -*- coding: utf-8 -*-
"""内存看门狗 —— 常驻轮询物理内存，越过硬线即触发强制关机。

背景：本机存在 nt! 进程拆除路径缺陷（Process/Job 对象零释放），物理内存会被
逐渐吃满到系统无响应。本看门狗是「保命最后一道防线」，不是泄漏的根治方案。

设计要点（每条都是推演或实测出来的，改动前请想清楚）：
  1. 【必须常驻，不能定时新起进程】内存 98% 时系统已在疯狂换页，此时新起一个
     Python 解释器本身就可能分配不到内存而失败。所以本进程必须一直活着，
     计划任务只负责「开机拉起 + 崩溃后补拉」，不负责周期性执行检测。
  2. 【纯标准库 ctypes】不 import psutil / numpy，自身常驻内存几 MB，
     避免看门狗自己变成负担。
  3. 【保命优先，留证其次】先发出 shutdown 命令，再去做 top 进程快照。
     留证是 best-effort，失败绝不能拖慢或阻止关机。
  4. 【冷却期】触发后进入冷却，避免「用户取消 -> 仍在 98% -> 立刻再触发」死循环。
  5. 【窗口期恶化升级】GRACE_SEC 内每 5 秒复查，若可用物理内存跌破
     ESCALATE_AVAIL_MB 就覆盖成 /t 0 立即关机（否则 30 秒可能撑不到）。
  6. 【快照必须放子进程 + 超时】本机僵尸进程对象可达 18 万量级，同步枚举会
     卡死主线程。故放独立子进程并带 timeout，超时即弃。

用法：
  python watchdog.py                      # 常驻守护（生产）
  python watchdog.py --dry-run --fake-load 99 --window-sec 6 --once
                                          # 演练：注入假内存值走完整触发链路，不发真关机命令
  python watchdog.py --once               # 只采样一次并打印，随即退出
  python watchdog.py --snapshot --top 10  # 只打印 top N 进程内存（供父进程调用）
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from datetime import datetime

# ===================== 可调参数 =====================

THRESHOLD_PCT = 98.0        # 硬线：物理内存使用率 >= 此值即触发
SAMPLE_INTERVAL_SEC = 5.0   # 采样间隔
CONFIRM_SAMPLES = 2         # 连续确认次数（防抖，避免瞬时抖动误杀）
GRACE_SEC = 30              # 可取消窗口（shutdown /t），期间可 shutdown /a 中止
ESCALATE_AVAIL_MB = 256     # 窗口期内可用物理内存跌破此值 -> 升级为立即关机
WINDOW_CHECK_SEC = 5.0      # 窗口期复查间隔
COOLDOWN_SEC = 600          # 触发后冷却时长，期间不再触发
SNAPSHOT_TIMEOUT_SEC = 12   # top 进程快照子进程超时
SNAPSHOT_MAX_PROCS = 5000   # 进程数超过此值就放弃枚举（太多必然超时，且进程数本身已是结论）
TOP_N = 10                  # 快照记录前 N 个进程

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "mem_watchdog.log")
SINGLETON_MUTEX = "LocalAgent_MemWatchdog_v1"

MB = 1024 * 1024

# ===================== Win32 结构 =====================


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wt.DWORD),
        ("ProcessCount", wt.DWORD),
        ("ThreadCount", wt.DWORD),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)

_kernel32.GlobalMemoryStatusEx.restype = wt.BOOL
_kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]

_psapi.GetPerformanceInfo.restype = wt.BOOL
_psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(_PERFORMANCE_INFORMATION), wt.DWORD]

_psapi.EnumProcesses.restype = wt.BOOL
_psapi.EnumProcesses.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, ctypes.POINTER(wt.DWORD)]

_psapi.GetProcessMemoryInfo.restype = wt.BOOL
_psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wt.DWORD]

_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

_kernel32.CreateMutexW.restype = wt.HANDLE
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, ctypes.c_wchar_p]

_kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]

_kernel32.SetPriorityClass.restype = wt.BOOL
_kernel32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000

# ===================== 基础工具 =====================

_MUTEX_HANDLE = None  # 必须全局持有，否则被 GC 关闭后单实例失效


def _log(level: str, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {level:<7} | {msg}\n"
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass  # pythonw 无控制台，忽略


def _acquire_singleton() -> bool:
    """单实例互斥。返回 False 表示已有实例在跑（本次应直接退出）。"""
    global _MUTEX_HANDLE
    ctypes.set_last_error(0)
    h = _kernel32.CreateMutexW(None, False, SINGLETON_MUTEX)
    if not h:
        _log("WARN", f"CreateMutex 失败 last_error={ctypes.get_last_error()}，跳过单实例检查")
        return True
    _MUTEX_HANDLE = h
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _raise_priority() -> None:
    """内存压力下保证本进程能被调度到（它不新分配内存，只是查询）。"""
    try:
        _kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def read_mem(fake_pct: float | None = None) -> tuple[float, int, int]:
    """返回 (使用率%, 可用物理MB, 总物理MB)。

    取 dwMemoryLoad 与自算 (Total-Avail)/Total 的较大者 —— 后者与任务管理器口径一致。
    """
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
    total_mb = m.ullTotalPhys // MB
    avail_mb = m.ullAvailPhys // MB
    pct = (m.ullTotalPhys - m.ullAvailPhys) * 100.0 / max(m.ullTotalPhys, 1)
    pct = max(float(m.dwMemoryLoad), pct)
    if fake_pct is not None:
        pct = float(fake_pct)
        avail_mb = int(total_mb * (100.0 - pct) / 100.0)
    return pct, avail_mb, total_mb


def read_perf_info() -> str | None:
    """一次系统调用拿系统级统计（进程数/句柄数/提交量），不枚举进程，极快。"""
    try:
        pi = _PERFORMANCE_INFORMATION()
        pi.cb = ctypes.sizeof(pi)
        if not _psapi.GetPerformanceInfo(ctypes.byref(pi), ctypes.sizeof(pi)):
            return None
        page = pi.PageSize or 4096
        return (
            f"ProcessCount={pi.ProcessCount} ThreadCount={pi.ThreadCount} "
            f"HandleCount={pi.HandleCount} "
            f"Commit={pi.CommitTotal * page // MB}MB/"
            f"{pi.CommitLimit * page // MB}MB "
            f"PhysAvail={pi.PhysicalAvailable * page // MB}MB"
        )
    except Exception as e:
        return f"(GetPerformanceInfo 失败: {e})"


# ===================== top 进程快照（子进程执行） =====================


def snapshot_top(top_n: int) -> None:
    """枚举进程工作集并打印 top N。仅供父进程以子进程方式调用。"""
    arr = (wt.DWORD * 65536)()
    needed = wt.DWORD()
    # 注意：argtypes 是 POINTER(DWORD)，必须 cast 数组，直接 byref(arr) 会得到
    # "pointer to c_ulong_Array_N" 而抛 ArgumentError（曾导致快照静默失败）。
    parr = ctypes.cast(arr, ctypes.POINTER(wt.DWORD))
    if not _psapi.EnumProcesses(parr, ctypes.sizeof(arr), ctypes.byref(needed)):
        print("(EnumProcesses failed)")
        return
    count = min(needed.value // ctypes.sizeof(wt.DWORD), 65536)
    if count > SNAPSHOT_MAX_PROCS:
        print(f"(进程数 {count} 超过 {SNAPSHOT_MAX_PROCS}，放弃枚举；进程数本身已说明问题)")
        return

    rows: list[tuple[int, int, str]] = []
    for pid in arr[:count]:
        if not pid:
            continue
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            continue
        try:
            pmc = _PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            if not _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), ctypes.sizeof(pmc)):
                continue
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            name = "?"
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value) or "?"
            rows.append((pmc.WorkingSetSize, pid, name))
        finally:
            _kernel32.CloseHandle(h)

    rows.sort(reverse=True)
    for ws, pid, name in rows[:top_n]:
        print(f"{ws // MB:>6} MB | pid={pid} | {name}")


def run_snapshot_subprocess(top_n: int) -> list[str]:
    """best-effort 调用自身 --snapshot 子进程，超时即弃。绝不抛异常。"""
    try:
        r = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--snapshot", "--top", str(top_n)],
            capture_output=True,
            timeout=SNAPSHOT_TIMEOUT_SEC,
            creationflags=CREATE_NO_WINDOW,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if lines:
            return lines
        # 子进程崩在 stderr 里 —— 不写出来就会变成「静默无输出」，排查时会瞎掉
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        tail = err.splitlines()[-1] if err else "(stderr 为空)"
        return [f"(快照无输出 rc={r.returncode} {tail})"]
    except subprocess.TimeoutExpired:
        return [f"(快照超时 {SNAPSHOT_TIMEOUT_SEC}s，已放弃)"]
    except Exception as e:
        return [f"(快照失败: {e})"]


# ===================== 关机动作 =====================


def issue_shutdown(sec: int, dry_run: bool) -> None:
    cmd = ["shutdown", "/s", "/f", "/t", str(sec)]
    tag = "[DRY-RUN] " if dry_run else ""
    _log("ACTION", f"{tag}下发关机命令: {' '.join(cmd)}")
    if dry_run:
        return
    try:
        subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        _log("ERROR", f"关机命令下发失败: {e}")


def fire(pct: float, avail_mb: int, total_mb: int, dry_run: bool, window_sec: int, top_n: int,
         fake_pct: float | None = None) -> None:
    """触发一次完整处置：先保命，再留证，最后守窗口。"""
    _log("ALERT", f"越过硬线: load={pct:.1f}% avail={avail_mb}MB/{total_mb}MB -> 触发关机")

    # 1) 保命优先：立刻下发带窗口的强制关机
    issue_shutdown(window_sec, dry_run)

    # 2) 快速留证：一次系统调用，不会卡
    perf = read_perf_info()
    if perf:
        _log("EVID", f"系统级: {perf}")

    # 3) best-effort 枚举 top 进程（子进程 + 超时）
    _log("EVID", f"关机前 top{top_n} 进程内存快照（limit={SNAPSHOT_TIMEOUT_SEC}s）:")
    for ln in run_snapshot_subprocess(top_n):
        _log("EVID", f"  {ln}")

    # 4) 守窗口：恶化则升级为立即关机
    deadline = time.time() + window_sec
    while time.time() < deadline:
        time.sleep(min(WINDOW_CHECK_SEC, max(0.5, deadline - time.time())))
        try:
            pct2, avail2, _ = read_mem(fake_pct)
        except Exception as e:
            _log("WARN", f"窗口期采样失败: {e}")
            break
        if avail2 < ESCALATE_AVAIL_MB:
            _log("ALERT", f"窗口期内继续恶化: avail={avail2}MB < {ESCALATE_AVAIL_MB}MB -> 升级为立即关机")
            issue_shutdown(0, dry_run)
            return
        _log("INFO", f"窗口期复查: load={pct2:.1f}% avail={avail2}MB")


# ===================== 主循环 =====================


def main() -> int:
    ap = argparse.ArgumentParser(description="内存看门狗")
    ap.add_argument("--dry-run", action="store_true", help="演练：不下发真关机命令")
    ap.add_argument("--fake-load", type=float, default=None, help="注入假的内存使用率%%用于演练")
    ap.add_argument("--once", action="store_true", help="只跑一轮检测即退出")
    ap.add_argument("--snapshot", action="store_true", help="只打印 top N 进程内存")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--threshold", type=float, default=THRESHOLD_PCT)
    ap.add_argument("--interval", type=float, default=SAMPLE_INTERVAL_SEC)
    ap.add_argument("--window-sec", type=int, default=GRACE_SEC)
    ap.add_argument("--no-singleton", action="store_true", help="跳过单实例检查（调试用）")
    args = ap.parse_args()

    if args.snapshot:
        snapshot_top(args.top)
        return 0

    if not args.no_singleton and not _acquire_singleton():
        return 0  # 已有实例在跑

    _raise_priority()
    _log(
        "START",
        f"看门狗启动 pid={os.getpid()} threshold={args.threshold}% interval={args.interval}s "
        f"window={args.window_sec}s dry_run={args.dry_run} fake_load={args.fake_load}",
    )

    consec = 0
    last_trigger_at = 0.0

    while True:
        try:
            pct, avail_mb, total_mb = read_mem(args.fake_load)
        except Exception as e:
            _log("WARN", f"采样失败: {e}")
            time.sleep(args.interval)
            if args.once:
                return 1
            continue

        _log("INFO", f"load={pct:.1f}% avail={avail_mb}MB/{total_mb}MB")

        in_cooldown = (time.time() - last_trigger_at) < COOLDOWN_SEC
        if in_cooldown:
            _log("INFO", "冷却期内，即使仍超线也不重复触发")
        elif pct >= args.threshold:
            consec += 1
            _log("WARN", f"超线确认 {consec}/{CONFIRM_SAMPLES}")
            if consec >= CONFIRM_SAMPLES:
                fire(pct, avail_mb, total_mb, args.dry_run, args.window_sec, args.top, args.fake_load)
                last_trigger_at = time.time()
                consec = 0
        else:
            consec = 0

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
