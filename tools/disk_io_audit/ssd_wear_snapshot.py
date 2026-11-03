# -*- coding: utf-8 -*-
"""SSD 累计写入 + 使用强度指纹 快照 —— 跨周/跨月耐久监测（loop 任务 / 开机启动通用）。

## 一、真值：设备计数器，不是 SRUM / psutil

`Data Units Written` 是 **NVMe 盘控制器自己数的、单调递增的**主机写入量，
两次取样相减 = 该区间绝对真值，与进程、页缓存、IPC、口径全无关。
（本机实测：SRUM 的 BytesWritten 含 IPC 与重复脏化，可虚高数倍；
 psutil `io_counters()` 在 Electron 负载下虚高过 36 倍；任务管理器同源。）

★ 因为计数器是**盘级累计**，取样的时间点间隔多长都不影响正确性：
  后端崩了、机器关了、漏采了几次 —— 下一行照样能和上一行相减，只是窗口变宽。
  这是本方案能挂在 loop 任务上（后端会重启）而不用担心中断丢数据的原因。

## 二、怎么判断"是不是高强度使用" —— 不设主观阈值，看三个可观测量的关系

区间窗口（默认 30 分钟）内并列记录三个量：

  1. `dw`     = 该区间写入字节（设备真值）        → GB/通电小时 = 耐久压力
  2. `cores`  = harness 进程消耗 CPU 秒 / 窗口秒数  → 「这段时间 agent 平均占了几个核」
  3. `log_n` / `log_wr_mb` = 被写到的日志文件数 / 日志新增字节
                → 会话条数与工具调用量的下限（日志会轮转，故是下限不是真值）

判定靠**散点关系**，不靠「多少个核算高强度」这种拍脑袋：
  - 高写入窗口若同时是 cores 高、log_n 大的窗口 ⇒ 写入确实跟着 agent 负载走；
  - 高写入窗口里 cores≈0、log_n≈0 ⇒ 与 harness 无关，另有来源（系统更新/索引/备份/别的进程），
    这时再回头查 SRUM 归因，而不是继续怪 IDE。
  - 两者混合 ⇒ 按窗口切分算各自的写入占比，给每个来源定份额。

历史教训（这些指标踩过坑，别再换回去）：
  - 系统累计 CPU（GetSystemTimes）**开机即清零**，跨重启差分必为负。
  - harness 的 CPU 累计**随进程退出丢失** ⇒ 本脚本按 `pid+create_time` 匹配上一轮快照求差，
    中途退出的进程那一段 CPU 拿不到 ⇒ `cores` 是**下限**（首轮 `new_pids` 会很高，正常）。
  - 日志目录总体积**会被轮转清掉**（WorkBuddy 曾 1088MB → 204MB）⇒ 不能拿目录体积做增量，
    必须按文件记 (size, mtime) 基线，见 `ssd_intensity_state.json`。

## 三、用法

    uv run python tools/disk_io_audit/ssd_wear_snapshot.py            # 人读报告
    python  tools/disk_io_audit/ssd_wear_snapshot.py --json           # 机器读（loop 任务用）
    python  tools/disk_io_audit/ssd_wear_snapshot.py --delay-secs 90  # 开机启动
    python  tools/disk_io_audit/ssd_wear_snapshot.py --skip-if-recent 600  # 10 分钟内已采过就跳过

⚠ 不要用「寿命百分比没变」当安全证据：粒度 1%，按当前速率约 82 天通电才动一格。
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import datetime as dt
import json
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes

IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
PropertyId_ProtocolSpecific = 49
PropertyStandardQuery = 0
ProtocolTypeNvme = 3
NVMeDataTypeLogPage = 2
SMART_LOG_PAGE = 0x02

UNIT_BYTES = 1000 * 512  # 1 data unit = 1000 × 512 B

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CSV = os.path.join(_ROOT, "workspace", "disk_manager", "ssd_wear_log.csv")
DEFAULT_STATE = os.path.join(_ROOT, "workspace", "disk_manager", "ssd_intensity_state.json")
DEFAULT_LOG = os.path.join(_ROOT, "workspace", "disk_manager", "ssd_wear_snapshot.log")

_LOG_STATE_KEEP_S = 2 * 86400  # 日志基线保留 2 天：隔太久再被写到时基线已无意义


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


# harness 识别：进程按「进程名 / exe 路径」关键字；日志按目录（存在才扫）。
HARNESS_PROCS: list[tuple[str, tuple[str, ...]]] = [
    ("WorkBuddy", ("workbuddy",)),
    ("Qoder", ("qoder",)),
    ("Trae", ("trae",)),
    ("Cursor", ("cursor.exe", "\\cursor\\")),
    ("VSCode", ("\\microsoft vs code\\",)),
    ("Windsurf", ("windsurf",)),
]

HARNESS_LOGS: dict[str, list[str]] = {
    "WorkBuddy": [_home(".workbuddy", "logs")],
    "Qoder": [_home("AppData", "Roaming", "com.qodercn.app.stable", "logs")],
    "Trae": [
        _home("AppData", "Roaming", "Trae CN", "logs"),
        _home("AppData", "Roaming", "Trae", "logs"),
        _home("AppData", "Roaming", "TRAE SOLO CN", "logs"),
    ],
}


# ---------------------------------------------------------------- NVMe SMART
class STORAGE_PROTOCOL_SPECIFIC_DATA(ctypes.Structure):
    _fields_ = [
        ("ProtocolType", wintypes.DWORD),
        ("DataType", wintypes.DWORD),
        ("ProtocolDataRequestValue", wintypes.DWORD),
        ("ProtocolDataRequestSubValue", wintypes.DWORD),
        ("ProtocolDataOffset", wintypes.DWORD),
        ("ProtocolDataLength", wintypes.DWORD),
        ("FixedProtocolReturnData", wintypes.DWORD),
        ("ProtocolDataRequestSubValue2", wintypes.DWORD),
        ("ProtocolDataRequestSubValue3", wintypes.DWORD),
        ("ProtocolDataRequestSubValue4", wintypes.DWORD),
    ]


class STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [
        ("PropertyId", wintypes.DWORD),
        ("QueryType", wintypes.DWORD),
        ("ProtocolSpecific", STORAGE_PROTOCOL_SPECIFIC_DATA),
    ]


def u128(b: bytes) -> int:
    return int.from_bytes(b, "little")


def read_smart(drive: int) -> dict | None:
    """读一块 NVMe 盘的 SMART 日志页。失败返回 None（非 NVMe / 无权限）。"""
    path = r"\\.\PhysicalDrive%d" % drive
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)
    if h == wintypes.HANDLE(-1).value:
        return None

    q = STORAGE_PROPERTY_QUERY()
    q.PropertyId = PropertyId_ProtocolSpecific
    q.QueryType = PropertyStandardQuery
    q.ProtocolSpecific.ProtocolType = ProtocolTypeNvme
    q.ProtocolSpecific.DataType = NVMeDataTypeLogPage
    q.ProtocolSpecific.ProtocolDataRequestValue = SMART_LOG_PAGE
    q.ProtocolSpecific.ProtocolDataRequestSubValue = 0
    q.ProtocolSpecific.ProtocolDataOffset = ctypes.sizeof(STORAGE_PROTOCOL_SPECIFIC_DATA)
    q.ProtocolSpecific.ProtocolDataLength = 512

    out = ctypes.create_string_buffer(4096)
    returned = wintypes.DWORD(0)
    ok = k32.DeviceIoControl(
        wintypes.HANDLE(h), IOCTL_STORAGE_QUERY_PROPERTY,
        ctypes.byref(q), ctypes.sizeof(q),
        out, 4096, ctypes.byref(returned), None)
    if not ok:
        k32.CloseHandle(wintypes.HANDLE(h))
        return None

    raw = out.raw[: returned.value]
    off, length = struct.unpack_from("<II", raw, 8 + 16)
    data_off = 8 + off
    log = raw[data_off: data_off + (length or 512)]
    k32.CloseHandle(wintypes.HANDLE(h))
    if len(log) < 512:
        return None

    temp_raw = struct.unpack_from("<H", log, 1)[0]
    return {
        "written_bytes": u128(log[48:64]) * UNIT_BYTES,
        "read_bytes": u128(log[32:48]) * UNIT_BYTES,
        "power_hours": u128(log[128:144]),
        "power_cycles": u128(log[112:128]),
        "unsafe_shutdowns": u128(log[144:160]),
        "wear_pct": log[5],
        "spare_pct": log[3],
        "temp_c": (temp_raw - 273) if temp_raw > 200 else temp_raw,
    }


# ---------------------------------------------------------------- 强度指纹
def uptime_s() -> float:
    try:
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0.0


def _new_group() -> dict:
    return {
        "proc": 0, "ws": 0.0,
        "cpu_s": 0.0, "new_pids": 0,        # 区间 CPU 秒（下限）/ 本轮才见到的进程数
        "log_n": 0, "log_new_n": 0, "log_wr_mb": 0.0,  # 被写文件数 / 新建文件数 / 新增字节
    }


def scan_harness(
    since: float,
    prev_pids: dict[str, list],
    prev_logs: dict[str, list],
) -> tuple[dict, int, dict[str, list], dict[str, list]]:
    """采集 harness 强度指纹。

    cpu_s 用 `pid:create_time` 做键与上一轮快照求差（防 pid 复用），
    进程中途退出则该进程最后一段 CPU 无法结算 ⇒ cpu_s 是**下限**。
    log_wr_mb 只统计「能在上轮基线里找到同一文件」的增量；上轮没有基线的老文件
    无法区分「本来就 200MB」和「本区间写了 200MB」，按 0 计（宁可少算，别虚报）。

    返回 (分组指纹, 全系统进程数, 本轮 pid 基线, 本轮日志基线)。
    """
    now = time.time()
    groups: dict[str, dict] = {k: _new_group() for k in HARNESS_LOGS}
    cur_pids: dict[str, list] = {}
    nproc = 0

    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        for p in psutil.process_iter(["pid", "name", "exe", "cpu_times", "memory_info", "create_time"]):
            nproc += 1
            info = p.info
            name = (info.get("name") or "").lower()
            exe = (info.get("exe") or "").lower()
            if not name and not exe:
                continue
            hay = name + "|" + exe
            for grp, keys in HARNESS_PROCS:
                if not any(k in hay for k in keys):
                    continue
                ct = info.get("cpu_times")
                mi = info.get("memory_info")
                cpu = float(ct.user + ct.system) if ct else 0.0
                ctime = float(info.get("create_time") or 0.0)
                slot = groups.setdefault(grp, _new_group())
                slot["proc"] += 1
                slot["ws"] += (mi.rss / 1048576.0) if mi else 0.0
                key = "%d:%.0f" % (int(info.get("pid") or 0), ctime)
                cur_pids[key] = [ctime, round(cpu, 3), grp]
                old = prev_pids.get(key)
                if old is None:
                    slot["new_pids"] += 1
                else:
                    # 进程退出后 cpu_times 不再变，差值恒 ≥0；max 防时钟/浮点抖动
                    slot["cpu_s"] += max(0.0, cpu - float(old[1]))
                break

    cur_logs: dict[str, list] = {}
    for grp, dirs in HARNESS_LOGS.items():
        slot = groups.setdefault(grp, _new_group())
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for root, _sub, files in os.walk(d):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    if st.st_mtime < since:
                        continue
                    slot["log_n"] += 1
                    old = prev_logs.get(fp)
                    if old is not None:
                        delta = st.st_size - float(old[0])
                        if delta > 0:
                            slot["log_wr_mb"] += delta / 1048576.0
                    elif st.st_ctime >= since:
                        # 本区间内新建的文件 ⇒ 整个文件都是本区间写出来的
                        slot["log_new_n"] += 1
                        slot["log_wr_mb"] += st.st_size / 1048576.0
                    cur_logs[fp] = [st.st_size, now]

    # 上一轮基线里还「新鲜」的条目并进来：文件隔一个窗口才再被写时，基线才不会丢
    cutoff = now - _LOG_STATE_KEEP_S
    for fp, val in prev_logs.items():
        if fp not in cur_logs and len(val) > 1 and float(val[1]) >= cutoff:
            cur_logs[fp] = val

    out: dict[str, dict] = {}
    for k, v in groups.items():
        if not (v["proc"] or v["log_n"]):
            continue  # 全 0 的组不落库，省列宽
        v["ws"] = round(v["ws"], 1)
        v["cpu_s"] = round(v["cpu_s"], 1)
        v["log_wr_mb"] = round(v["log_wr_mb"], 1)
        out[k] = v
    return out, nproc, cur_pids, cur_logs


# ---------------------------------------------------------------- 基线状态
def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("pids", {})
            data.setdefault("logs", {})
            return data
    except (OSError, ValueError):
        pass
    return {"pids": {}, "logs": {}}


def save_state(path: str, pids: dict, logs: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"updated_at": int(time.time()), "pids": pids, "logs": logs},
                  fh, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 内存压力
class _MEMSTATUS(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def commit_gb() -> tuple[float | None, float | None]:
    """(已提交 GB, 提交上限 GB)。GlobalMemoryStatusEx 是微秒级调用，不走 WMI。"""
    try:
        m = _MEMSTATUS()
        m.dwLength = ctypes.sizeof(_MEMSTATUS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None, None
        limit = m.ullTotalPageFile / 2**30
        return round(limit - m.ullAvailPageFile / 2**30, 2), round(limit, 2)
    except Exception:
        return None, None


def pagefile_mb() -> tuple[float | None, float | None]:
    """(页文件已用 MB, 本次开机峰值 MB)。只能走 WMI，约 1 秒，故一次取两个值。"""
    ps = ("$p=Get-CimInstance Win32_PageFileUsage;"
          "'{0};{1}' -f (($p|Measure-Object CurrentUsage -Sum).Sum),"
          "(($p|Measure-Object PeakUsage -Sum).Sum)")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=40)
        used, peak = out.stdout.strip().splitlines()[-1].split(";")
        u, p = round(float(used), 1), round(float(peak), 1)
        # 本机页文件是固定 31.9 GB，用过几百 MB ⇒ "0 MB 已用 + 0 MB 峰值"不可能成立。
        # 2026-11-03 18:28 的实测行就是 0.0/0.0（同刻 commit 17 GB，自相矛盾）⇒ 该次查询返回了垃圾值。
        # 记为缺失（空串）比记成 0 安全：0 会让事后判读误以为"这窗口页文件没动"。
        if u == 0.0 and p == 0.0:
            return None, None
        return u, p
    except Exception:
        return None, None


# ---------------------------------------------------------------- 记录
FIELDS = [
    "ts", "epoch", "disk", "written_bytes", "read_bytes",
    "power_hours", "power_cycles", "unsafe_shutdowns", "wear_pct", "spare_pct", "temp_c",
    "uptime_s", "nproc",
    # v2（2026-11-03 起）：区间量 —— 只有它才能回答"高强度时是不是写得更多"
    "win_secs", "dw_bytes", "dph", "cpu_s", "cores", "exited_n",
    "harness_json",
    # v3（2026-11-03 起）：内存压力。⛔ pagefile.sys 的 mtime 冻结在开机时刻，
    # 往它写字节**不留任何文件时间戳** ⇒ 只看文件系统永远找不到这类写入，
    # 必须同时记页文件用量/峰值才能事后判断"大突发是不是分页写回落"。
    "commit_gb", "commit_limit_gb", "pf_used_mb", "pf_peak_mb",
]


def load_prev(csv_path: str) -> dict[int, dict]:
    """每块盘取最后一行（CSV 是追加写，最后一行最新）。"""
    prev: dict[int, dict] = {}
    if not os.path.exists(csv_path):
        return prev
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                prev[int(row["disk"])] = row
            except (KeyError, ValueError, TypeError):
                continue
    return prev


def migrate_csv(csv_path: str, quiet: bool = False) -> None:
    """老 schema（无 win_secs 等列）→ 新 schema：重写表头，老行缺的列留空。

    csv 模块按表头写，列不齐会抛错，所以必须一次性补齐；文件小（每窗口 2 行），
    先备份再重写。
    """
    if not os.path.exists(csv_path):
        return
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames == FIELDS:
            return
        rows = list(reader)
    backup = csv_path + ".bak-schema"
    if not os.path.exists(backup):
        with open(csv_path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in FIELDS})
    if not quiet:
        print("CSV schema 已升级到 v2（%d 行，原文件备份为 %s）" % (len(rows), backup))


def num(row: dict, key: str) -> float | None:
    v = row.get(key)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tb(n: float) -> float:
    return n / 1e12


def _ensure_stdout(log_path: str) -> None:
    """pythonw / DETACHED_PROCESS 下 stdout 可能是 None，也可能是无效句柄 ——
    两种情况都会让 print 抛异常（脚本本身能跑完前段，但尾部报错会掩盖结果）。
    所以先试写一次探测，不通就重定向到日志文件。
    注：写 CSV 在 print 之前完成，即使这里彻底失败，设备数据也不会丢。
    """
    alive = False
    try:
        if sys.stdout is not None:
            sys.stdout.write("")
            sys.stdout.flush()
            alive = True
    except Exception:  # noqa: BLE001
        alive = False
    if alive:
        return
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        fh = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        fh = open(os.devnull, "w", buffering=1)
    sys.stdout = fh  # type: ignore[assignment]
    sys.stderr = fh  # type: ignore[assignment]


def main() -> int:
    ap = argparse.ArgumentParser(description="SSD 累计写入 + 使用强度快照")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="累计记录 CSV")
    ap.add_argument("--state", default=DEFAULT_STATE, help="CPU / 日志基线状态文件")
    ap.add_argument("--log", default=DEFAULT_LOG, help="pythonw 下的输出日志")
    ap.add_argument("--tbw", type=float, default=600.0, help="标称 TBW（默认 600）")
    ap.add_argument("--delay-secs", type=int, default=0, help="先等待 N 秒再采集（开机启动用）")
    ap.add_argument("--skip-if-recent", type=int, default=0,
                    help="距上次采集不足 N 秒则跳过（防止重启风暴刷行）")
    ap.add_argument("--json", action="store_true", help="输出单行 JSON（loop 任务用）")
    ap.add_argument("--quiet", action="store_true", help="只写 CSV，不打印分析")
    args = ap.parse_args()

    _ensure_stdout(args.log)
    if args.delay_secs > 0:
        time.sleep(args.delay_secs)

    os.makedirs(os.path.dirname(args.csv), exist_ok=True)
    migrate_csv(args.csv, quiet=args.json or args.quiet)

    now = dt.datetime.now()
    now_epoch = int(now.timestamp())
    prev = load_prev(args.csv)

    prev_epochs = [int(r["epoch"]) for r in prev.values() if r.get("epoch")]
    if args.skip_if_recent and prev_epochs and now_epoch - max(prev_epochs) < args.skip_if_recent:
        gap = now_epoch - max(prev_epochs)
        if args.json:
            print(json.dumps({"ok": True, "skipped": "recent", "gap_secs": gap}, ensure_ascii=False))
        elif not args.quiet:
            print("距上次采集仅 %d 秒（< %d），跳过。" % (gap, args.skip_if_recent))
        return 0

    # 活跃度窗口 = 自上次快照以来（首次运行退化为最近 24h）
    since = max(prev_epochs) if prev_epochs else now_epoch - 86400
    win_secs = max(1, now_epoch - since)

    up = uptime_s()
    state = load_state(args.state)
    harness, nproc, cur_pids, cur_logs = scan_harness(since, state["pids"], state["logs"])
    exited_n = max(0, len(state["pids"]) - sum(1 for k in state["pids"] if k in cur_pids))

    samples: dict[int, dict] = {}
    for d in (0, 1):
        s = read_smart(d)
        if s is not None:
            samples[d] = s
    if not samples:
        if args.json:
            print(json.dumps({"ok": False, "error": "读不到任何 NVMe SMART"}, ensure_ascii=False))
        else:
            print("[%s] 读不到任何 NVMe SMART" % now.strftime("%F %T"))
        return 1

    harness_json = json.dumps(harness, ensure_ascii=False, separators=(",", ":")) if harness else ""
    cpu_s = round(sum(g["cpu_s"] for g in harness.values()), 1)
    cores = round(cpu_s / win_secs, 3)

    c_gb, c_limit = commit_gb()
    pf_used, pf_peak = pagefile_mb()

    new_file = not os.path.exists(args.csv)
    with open(args.csv, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for d, s in samples.items():
            p = prev.get(d)
            dw = dph = ""
            if p is not None:
                try:
                    dw = s["written_bytes"] - int(p["written_bytes"])
                    dph = s["power_hours"] - int(p["power_hours"])
                except (KeyError, ValueError, TypeError):
                    dw = dph = ""
            w.writerow({
                "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "epoch": now_epoch,
                "disk": d, **s,
                "uptime_s": round(up, 1), "nproc": nproc,
                "win_secs": win_secs, "dw_bytes": dw, "dph": dph,
                "cpu_s": cpu_s, "cores": cores, "exited_n": exited_n,
                "harness_json": harness_json,
                "commit_gb": "" if c_gb is None else c_gb,
                "commit_limit_gb": "" if c_limit is None else c_limit,
                "pf_used_mb": "" if pf_used is None else pf_used,
                "pf_peak_mb": "" if pf_peak is None else pf_peak,
            })

    save_state(args.state, cur_pids, cur_logs)

    disks_json: dict[str, dict] = {}
    for d, s in samples.items():
        p = prev.get(d)
        dw_v = None
        if p is not None:
            try:
                dw_v = s["written_bytes"] - int(p["written_bytes"])
            except (KeyError, ValueError, TypeError):
                dw_v = None
        disks_json[str(d)] = {
            "written_bytes": s["written_bytes"],
            "wear_pct": s["wear_pct"],
            "power_hours": s["power_hours"],
            "dw_bytes": dw_v,
            "gb_per_h": round(dw_v / 1e9 / (win_secs / 3600.0), 2) if dw_v is not None else None,
        }

    if args.json:
        print(json.dumps({
            "ok": True, "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "win_secs": win_secs,
            "cpu_s": cpu_s, "cores": cores, "exited_n": exited_n,
            "nproc": nproc, "disks": disks_json, "harness": harness,
            "commit_gb": c_gb, "commit_limit_gb": c_limit,
            "pf_used_mb": pf_used, "pf_peak_mb": pf_peak,
        }, ensure_ascii=False))
        return 0

    if args.quiet:
        print("已追加 %d 条 -> %s" % (len(samples), args.csv))
        return 0

    print("== 快照 %s   本次开机 %.1f h   进程 %d   窗口 %d 分 =="
          % (now.strftime("%Y-%m-%d %H:%M:%S"), up / 3600, nproc, win_secs // 60))
    for d, s in samples.items():
        print("Disk %d: 累计写 %7.2f TB  读 %7.2f TB  通电 %6d h  寿命 %d%%  %dC"
              % (d, tb(s["written_bytes"]), tb(s["read_bytes"]), s["power_hours"], s["wear_pct"], s["temp_c"]))
    if harness:
        print("  强度指纹（自 %s 起 %d 分钟）:" % (
            dt.datetime.fromtimestamp(since).strftime("%m-%d %H:%M"), win_secs // 60))
        for grp, g in sorted(harness.items()):
            print("    %-11s 进程 %2d 内存 %6.0fMB | 本区间 CPU %7.1fs(%.2f 核) 新增进程 %2d"
                  % (grp, g["proc"], g["ws"], g["cpu_s"], g["cpu_s"] / win_secs, g["new_pids"]))
            print("    %-11s 被写日志 %4d 个（新建 %3d）  日志新增 %8.1f MB"
                  % ("", g["log_n"], g["log_new_n"], g["log_wr_mb"]))
        print("  ⇒ harness 合计 %.1f CPU 秒 = 平均 %.2f 核%s"
              % (cpu_s, cores, "（有 %d 个进程中途退出，实际更高）" % exited_n if exited_n else ""))
    else:
        print("  （无 harness 在跑，也无新日志）")
    if c_gb is not None:
        print("  内存压力: 提交 %.1f/%.1f GB%s"
              % (c_gb, c_limit or 0,
                 ("  页文件已用 %.0f MB（开机峰值 %.0f MB）" % (pf_used, pf_peak))
                 if pf_used is not None and pf_peak is not None else ""))

    print("\n== 与上次快照对比 ==")
    for d, s in samples.items():
        p = prev.get(d)
        if not p:
            print("Disk %d: 首次记录，本次即基线" % d)
            continue
        try:
            d_epoch = now_epoch - int(p["epoch"])
            d_wr = s["written_bytes"] - int(p["written_bytes"])
            d_ph = s["power_hours"] - int(p["power_hours"])
        except (KeyError, ValueError, TypeError):
            print("Disk %d: 上条记录损坏，跳过" % d)
            continue
        if d_epoch < 60:
            print("Disk %d: 距上次不足 1 分钟，跳过" % d)
            continue

        days, hours = d_epoch // 86400, (d_epoch % 86400) // 3600
        print("Disk %d  (%s ~ now)" % (d, p.get("ts", "?")))
        print("   墙钟 %d 天 %d 小时   通电 %+d h   写入 %+.2f GB   ⇒ %.2f GB/墙钟小时"
              % (days, hours, d_ph, d_wr / 1e9, d_wr / 1e9 / (d_epoch / 3600.0)))

        # power_hours 是整数小时，短窗口差分会放大成荒谬值（曾算出 638% 通电占比）。
        if d_ph >= 1:
            print("   ★ 强度 %.2f GB/通电小时" % (d_wr / 1e9 / d_ph))

        if d_epoch >= 6 * 3600:
            per_day = d_wr / 1e9 / (d_epoch / 86400.0)
            per_year = tb(d_wr / (d_epoch / (365.2425 * 86400)))
            print("   折日 %.1f GB/天   折年 %.1f TB/年" % (per_day, per_year))
            remain = args.tbw * 1e12 - s["written_bytes"]
            if per_year > 0:
                print("   距 %.0f TBW 还剩 %.1f TB ⇒ 按此速率 %.1f 年"
                      % (args.tbw, tb(remain), tb(remain) / per_year))
        else:
            print("   （窗口不足 6 小时，不做折日/折年外推以免噪声放大）")

        pw = num(p, "wear_pct")
        if pw is not None and s["wear_pct"] != int(pw):
            print("   ⚠ 寿命 %d%% → %d%%（粒度 1%% ⇒ 该区间已消耗 ≥1%%）" % (pw, s["wear_pct"]))

    print("\n记录已追加: %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
