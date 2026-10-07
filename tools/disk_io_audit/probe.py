#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalAgent 磁盘 I/O 审计探针。

用途
----
持续采样「本项目前后端进程」自身产生的磁盘读写量，并同时记录被监视目录下
文件的变化情况（哪个文件在被改、改得多频繁），用于判断是否存在不合理的
高频 / 过量磁盘读写设计。

三条采集通道（全部零第三方驱动、不需要额外权限）
------------------------------------------------
1. Win32 ``GetProcessIoCounters``
   进程级累计读/写字节数与操作次数。统计的是「该进程通过 Win32 API 产生的
   逻辑 I/O」：**含**文件、管道、控制台（stdout 也算！），**不含** socket
   （回环 socket 实测零增量，见 ``selftest_io_channels.py``）。因此它相对
   「真实落盘」是**上界**（交给 OS 的字节 ≥ 最终物理写入），用来发现数量级
   异常最合适，但不能直接换算成 SSD 磨损。
2. ``psutil.disk_io_counters()``
   全盘物理读/写，用于交叉验证「进程逻辑 I/O 是否真的落到了盘上」。
3. 目录 ``stat`` 快照 diff
   文件级归因：哪些文件被反复重写、单次增量多大、间隔多长。只读元数据，
   不产生 read transfer，对被审计对象几乎无污染。

⚠️ 重要：**不要按进程树聚合**。本探针通常由被测后端自己派生的终端会话启动，
派生出来的 runner / 子进程会写探针自己的日志文件，按树聚合会把这份噪声算到
后端头上。默认只采被测进程自身的计数器。

用法
----
::

    # 连续采样（前台），每 5 秒一 tick，跑 10 分钟
    python tools/disk_io_audit/probe.py --duration 600 --interval 5 \
        --out temp/disk_io_audit_YYYYMMDD/probe.jsonl

    # 长跑（几小时~几天）：--duration 0 表示一直跑到手动结束
    python tools/disk_io_audit/probe.py --duration 0 --interval 30 \
        --out temp/disk_io_audit_YYYYMMDD/probe.jsonl \
        --heartbeat temp/disk_io_audit_YYYYMMDD/probe.heartbeat

    # 把 jsonl 聚合成报告
    python tools/disk_io_audit/probe.py --report temp/disk_io_audit_YYYYMMDD/probe.jsonl

被测进程用 ``--match`` 指定 cmdline 关键字（逗号分隔），例如::

    --match server.main,client.main,_exec_runner

每 tick 输出一行 JSONL，字段见 ``_tick_record``。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from datetime import datetime

try:
    import psutil
except Exception as exc:  # pragma: no cover - 环境缺失时明确报错
    print(f"FATAL: 需要 psutil: {exc}")
    sys.exit(2)

if os.name != "nt":  # pragma: no cover
    print("FATAL: 本探针仅支持 Windows（依赖 GetProcessIoCounters）")
    sys.exit(2)

# --------------------------------------------------------------------------- #
# Win32 通道
# --------------------------------------------------------------------------- #

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class IO_COUNTERS(ctypes.Structure):
    """对应 Windows IO_COUNTERS 结构体。"""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.GetProcessIoCounters.restype = ctypes.c_int
_k32.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_k32.CloseHandle.restype = ctypes.c_int
_k32.CloseHandle.argtypes = [ctypes.c_void_p]


def read_io_counters(pid: int) -> dict | None:
    """读取某个进程的累计 I/O 计数器；失败返回 None。"""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return None
    try:
        counters = IO_COUNTERS()
        if not _k32.GetProcessIoCounters(handle, ctypes.byref(counters)):
            return None
        return {
            "r_b": int(counters.ReadTransferCount),
            "w_b": int(counters.WriteTransferCount),
            "o_b": int(counters.OtherTransferCount),
            "r_ops": int(counters.ReadOperationCount),
            "w_ops": int(counters.WriteOperationCount),
            "o_ops": int(counters.OtherOperationCount),
        }
    finally:
        _k32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# 进程发现
# --------------------------------------------------------------------------- #

DEFAULT_MATCH = ("server.main", "client.main", "_exec_runner")
# 探针自身的进程与它的传输外壳必须排除，否则会把「审计工具自己的写入」
# 算到被测对象头上：探针脚本名、所在目录名、以及 exec_python 的 runner。
DEFAULT_EXCLUDE = ("probe.py", "disk_io_audit", "_exec_runner.py", "exec_evidence")


def project_root_global() -> str:
    """项目根目录：本脚本位于 <root>/tools/disk_io_audit/probe.py。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def find_targets(hints, excludes=()) -> list[dict]:
    """按 cmdline 关键字找出被测进程（并排除探针自身）。"""
    found = []
    lowered = [h.lower() for h in hints if h]
    blocked = [e.lower() for e in excludes if e]
    self_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time", "exe"]):
        try:
            info = proc.info
            pid = info["pid"]
            if pid == self_pid:
                continue
            name = (info.get("name") or "").lower()
            if "python" not in name and "pythonw" not in name:
                continue
            cmdline = " ".join(info.get("cmdline") or [])
            if not cmdline:
                continue
            low = cmdline.lower()
            if any(b in low for b in blocked):
                continue
            if not any(h in low for h in lowered):
                continue
            found.append(
                {
                    "pid": pid,
                    "cmdline": cmdline[:200],
                    "started": info.get("create_time") or 0.0,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    found.sort(key=lambda item: item["pid"])
    return found


def process_extra(pid: int) -> dict:
    """补充 CPU / 句柄 / 线程数，帮助判断进程是否在忙。"""
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            times = proc.cpu_times()
            return {
                "cpu": round(times.user + times.system, 3),
                "handles": proc.num_handles(),
                "threads": proc.num_threads(),
                "rss": proc.memory_info().rss,
            }
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# 目录快照 / diff
# --------------------------------------------------------------------------- #

def scan_dirs(dirs, ignore=()) -> dict:
    """对目录树做 stat 快照。返回 {path: (size, mtime_ns)}。

    ``ignore`` 里的路径前缀会被跳过 —— 主要用于排除探针自己的输出目录，
    否则探针写的 jsonl 会冒充「被审计文件」出现在报告里。
    """
    snap: dict[str, tuple[int, int]] = {}
    for base in dirs:
        if not os.path.isdir(base):
            continue
        for root, _subdirs, files in os.walk(base):
            if any(root.startswith(prefix) for prefix in ignore):
                _subdirs[:] = []
                continue
            for fname in files:
                fpath = os.path.join(root, fname)
                if any(fpath.startswith(prefix) for prefix in ignore):
                    continue
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                snap[fpath] = (st.st_size, st.st_mtime_ns)
    return snap


def diff_snapshot(prev: dict, cur: dict, now: float, max_items: int = 300):
    """比较两次快照，返回变化列表与统计。

    返回 (changes, summary)：
      changes: [{path, kind, delta, size}]  kind ∈ new/mod/del
      summary: {n_new, n_mod, n_del, bytes_added}
    """
    changes = []
    n_new = n_mod = n_del = 0
    bytes_added = 0

    for path, (size, mtime) in cur.items():
        old = prev.get(path)
        if old is None:
            n_new += 1
            bytes_added += size
            changes.append({"path": path, "kind": "new", "delta": size, "size": size})
        elif old[0] != size or old[1] != mtime:
            delta = size - old[0]
            n_mod += 1
            if delta > 0:
                bytes_added += delta
            changes.append({"path": path, "kind": "mod", "delta": delta, "size": size})

    for path, (size, _mtime) in prev.items():
        if path not in cur:
            n_del += 1
            changes.append({"path": path, "kind": "del", "delta": -size, "size": 0})

    changes.sort(key=lambda item: -abs(item["delta"]))
    summary = {
        "n_new": n_new,
        "n_mod": n_mod,
        "n_del": n_del,
        "bytes_added": bytes_added,
        "n_files": len(cur),
        "total_bytes": sum(size for size, _ in cur.values()),
    }
    return changes[:max_items], summary


# --------------------------------------------------------------------------- #
# 采样主循环
# --------------------------------------------------------------------------- #

def _delta(cur: dict, prev: dict | None) -> dict:
    """两个累计计数器快照的差。"""
    if prev is None:
        return {k: 0 for k in cur}
    return {k: max(0, cur[k] - prev.get(k, 0)) for k in cur}


def _write_json_atomic(path: str, payload: dict) -> None:
    """原子写一个小 JSON（temp + os.replace）。

    用于「心跳」：长跑（几小时~几天）时用来判断探针还活着、跑到哪了。
    失败一律静默 —— 心跳写盘不该影响主采样循环。
    """
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _tick_record(
    elapsed: float,
    dt: float,
    proc_samples: list,
    sys_disk: dict,
    file_changes: list,
    file_summary: dict,
) -> dict:
    total_w = sum(s["d_w_b"] for s in proc_samples)
    total_r = sum(s["d_r_b"] for s in proc_samples)
    total_w_ops = sum(s["d_w_ops"] for s in proc_samples)
    return {
        "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(elapsed, 2),
        "dt": round(dt, 3),
        "procs": proc_samples,
        "total_w_b": total_w,
        "total_r_b": total_r,
        "total_w_ops": total_w_ops,
        "w_rate": round(total_w / dt, 1) if dt > 0 else 0.0,
        "r_rate": round(total_r / dt, 1) if dt > 0 else 0.0,
        "sys_disk": sys_disk,
        "files": file_summary,
        "changes": file_changes,
    }


def run_probe(args) -> int:
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    watch = [os.path.abspath(d) for d in args.dirs.split(",") if d.strip()]
    hints = [h for h in args.match.split(",") if h.strip()]
    exclude = [e for e in args.exclude.split(",") if e.strip()]
    hb_path = os.path.abspath(args.heartbeat) if args.heartbeat else ""
    # 默认忽略探针输出目录本身 + 探针所在的终端日志目录
    ignore = [os.path.dirname(out_path) + os.sep, os.path.join(project_root_global(), "temp", "terminals") + os.sep]
    ignore += [os.path.abspath(p) + os.sep for p in args.ignore.split(",") if p.strip()]
    if hb_path:
        ignore.append(os.path.dirname(hb_path) + os.sep)

    started = time.time()
    prev_counters: dict[int, dict] = {}
    prev_snap: dict | None = None
    prev_sys: dict | None = None
    prev_tick = time.time()
    ticks = 0

    print(f"[probe] out     = {out_path}")
    print(f"[probe] match   = {hints}")
    print(f"[probe] exclude = {exclude}")
    print(f"[probe] dirs    = {watch}")
    print(f"[probe] intvl   = {args.interval}s  duration = {args.duration or 'inf'}s")
    print(f"[probe] hbeat   = {hb_path or '(off)'}")
    sys.stdout.flush()

    with open(out_path, "a", encoding="utf-8") as fh:
        while True:
            loop_start = time.time()
            now = loop_start
            # ⚠️ prev_tick 必须在这里取：它代表「上一次读取计数器」的时刻。
            # 若放在 sleep 之后赋值，下一个 loop_start 会紧贴它，dt 恒为 ~0，
            # 速率全部算成 0（本探针第一版就踩了这个坑）。
            dt = now - prev_tick
            prev_tick = now

            targets = find_targets(hints, exclude)
            proc_samples = []
            for item in targets:
                pid = item["pid"]
                counters = read_io_counters(pid)
                if counters is None:
                    continue
                delta = _delta(counters, prev_counters.get(pid))
                prev_counters[pid] = counters
                sample = {
                    "pid": pid,
                    "cmd": item["cmdline"],
                    "age_h": round((now - item["started"]) / 3600.0, 2) if item["started"] else None,
                    "cum_r_b": counters["r_b"],
                    "cum_w_b": counters["w_b"],
                    "cum_r_ops": counters["r_ops"],
                    "cum_w_ops": counters["w_ops"],
                    "d_r_b": delta["r_b"],
                    "d_w_b": delta["w_b"],
                    "d_r_ops": delta["r_ops"],
                    "d_w_ops": delta["w_ops"],
                    "d_other_b": delta["o_b"],
                }
                sample.update(process_extra(pid))
                proc_samples.append(sample)

            sys_now = None
            try:
                disk = psutil.disk_io_counters()
                if disk is not None:
                    sys_now = {
                        "read_b": disk.read_bytes,
                        "write_b": disk.write_bytes,
                        "read_count": disk.read_count,
                        "write_count": disk.write_count,
                    }
            except Exception:
                sys_now = None
            sys_delta = {}
            if sys_now and prev_sys:
                sys_delta = {
                    "d_read_b": max(0, sys_now["read_b"] - prev_sys["read_b"]),
                    "d_write_b": max(0, sys_now["write_b"] - prev_sys["write_b"]),
                    "d_read_count": max(0, sys_now["read_count"] - prev_sys["read_count"]),
                    "d_write_count": max(0, sys_now["write_count"] - prev_sys["write_count"]),
                }
            prev_sys = sys_now

            changes, summary = [], {}
            if args.dirs:
                snap = scan_dirs(watch, ignore)
                if prev_snap is not None:
                    changes, summary = diff_snapshot(prev_snap, snap, now)
                prev_snap = snap

            record = _tick_record(now - started, dt, proc_samples, sys_delta, changes, summary)
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

            # 心跳：给长跑一个「还活着 + 当前速率」的零成本检查点（<= 几百字节）
            _write_json_atomic(
                hb_path,
                {
                    "t": record["t"],
                    "probe_pid": os.getpid(),
                    "out": out_path,
                    "ticks": ticks + 1,
                    "elapsed_s": record["elapsed"],
                    "targets": [s["pid"] for s in proc_samples],
                    "w_b": record["total_w_b"],
                    "r_b": record["total_r_b"],
                    "w_rate": record["w_rate"],
                    "files": summary.get("n_mod", 0) + summary.get("n_new", 0),
                },
            )

            ticks += 1
            if args.verbose or ticks % max(1, args.print_every) == 0:
                line = (
                    f"[{record['t']}] t+{record['elapsed']:8.1f}s  "
                    f"procs={len(proc_samples)}  "
                    f"W={record['total_w_b'] / 1024:9.1f} KiB ({record['w_rate'] / 1024:7.1f} KiB/s)  "
                    f"R={record['total_r_b'] / 1024:10.1f} KiB ({record['r_rate'] / 1024:7.1f} KiB/s)"
                )
                if summary:
                    line += (
                        f"  files: mod={summary['n_mod']} new={summary['n_new']} "
                        f"del={summary['n_del']} +{summary['bytes_added'] / 1024:.1f}KiB"
                    )
                print(line)
                sys.stdout.flush()

            if args.duration and (now - started) >= args.duration:
                break

            sleep_for = args.interval - (time.time() - loop_start)
            if sleep_for > 0:
                time.sleep(sleep_for)

    print(f"[probe] done, {ticks} ticks -> {out_path}")
    return 0


# --------------------------------------------------------------------------- #
# 报告聚合
# --------------------------------------------------------------------------- #

def _fmt_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PiB"


#: SQLite 附属文件后缀。它们的 mtime 变动不代表内容全量写入，单列处理。
SQLITE_SIDECAR_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")


def _is_sqlite_sidecar(path: str) -> bool:
    low = path.lower()
    return low.endswith(SQLITE_SIDECAR_SUFFIXES)


def _parse_ts(text: str) -> float | None:
    """把 tick 记录里的 ``t``（%Y-%m-%d %H:%M:%S）解析成 epoch 秒；失败返回 None。"""
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def build_report(path: str) -> int:
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not records:
        print("没有可用的采样记录")
        return 1

    # 覆盖时长：单次运行时用 elapsed（亚秒精度）；但长跑若中途重启并**追加**到同一
    # 文件（elapsed 会倒退），继续用 elapsed 会把跨度算成负数或严重偏小 —— 此时
    # 改用墙钟时间戳。这也是「探针可以被安全重启续采」的前提。
    monotonic = all(
        records[i]["elapsed"] >= records[i - 1]["elapsed"] for i in range(1, len(records))
    )
    span = records[-1]["elapsed"] - records[0]["elapsed"]
    span_src = "单次运行的 elapsed"
    if not monotonic or span < 0:
        t0 = _parse_ts(records[0].get("t", ""))
        t1 = _parse_ts(records[-1].get("t", ""))
        if t0 is not None and t1 is not None and t1 >= t0:
            span = t1 - t0
            span_src = "墙钟时间戳（文件含多次续跑）"

    print("=" * 78)
    print(f"磁盘 I/O 审计报告  ——  {path}")
    print("=" * 78)
    print(f"采样点数      : {len(records)}")
    print(f"覆盖时间      : {span / 3600:.2f} 小时 ({span:.0f} 秒)  [口径: {span_src}]")
    if records[0]["t"]:
        print(f"起止          : {records[0]['t']}  →  {records[-1]['t']}")

    # ---- 按进程聚合 ---- #
    per_proc: dict[int, dict] = {}
    for rec in records:
        for sample in rec.get("procs", []):
            entry = per_proc.setdefault(
                sample["pid"],
                {"cmd": sample["cmd"], "r_b": 0, "w_b": 0, "r_ops": 0, "w_ops": 0, "age_h": sample.get("age_h")},
            )
            entry["r_b"] += sample.get("d_r_b", 0)
            entry["w_b"] += sample.get("d_w_b", 0)
            entry["r_ops"] += sample.get("d_r_ops", 0)
            entry["w_ops"] += sample.get("d_w_ops", 0)

    print()
    print("【一】被测进程自身 I/O 汇总（采样窗口内增量）")
    print("-" * 78)
    print(f"{'PID':>8} {'写入':>12} {'读取':>12} {'W/s':>11} {'R/s':>11} {'Wops':>9}  进程")
    for pid, entry in sorted(per_proc.items(), key=lambda kv: -kv[1]["w_b"]):
        w_rate = entry["w_b"] / span if span else 0
        r_rate = entry["r_b"] / span if span else 0
        print(
            f"{pid:>8} {_fmt_bytes(entry['w_b']):>12} {_fmt_bytes(entry['r_b']):>12} "
            f"{_fmt_bytes(w_rate) + '/s':>11} {_fmt_bytes(r_rate) + '/s':>11} "
            f"{entry['w_ops']:>9}  {entry['cmd'][:44]}"
        )
    tot_w = sum(e["w_b"] for e in per_proc.values())
    tot_r = sum(e["r_b"] for e in per_proc.values())
    print("-" * 78)
    print(
        f"{'合计':>8} {_fmt_bytes(tot_w):>12} {_fmt_bytes(tot_r):>12} "
        f"{_fmt_bytes(tot_w / span if span else 0) + '/s':>11} "
        f"{_fmt_bytes(tot_r / span if span else 0) + '/s':>11}"
    )
    if span:
        print(f"\n外推（含一次性尖峰）：24 小时写入 ≈ {_fmt_bytes(tot_w / span * 86400)}，"
              f"读取 ≈ {_fmt_bytes(tot_r / span * 86400)}")
        # 一次性事件（启动 checkpoint、首次加载）会把均值顶飞，单独给一个剔除口吻
        ordered = sorted(records, key=lambda r: -r.get("total_w_b", 0))
        drop = ordered[: max(1, len(ordered) // 50)]
        drop_ids = {id(r) for r in drop}
        rest = [r for r in records if id(r) not in drop_ids]
        rest_w = sum(r.get("total_w_b", 0) for r in rest)
        rest_r = sum(r.get("total_r_b", 0) for r in rest)
        rest_span = sum(r.get("dt", 0) for r in rest)
        if rest_span > 0 and rest:
            print(f"外推（剔除最高的 2% 尖峰，更接近稳态）：24 小时写入 ≈ "
                  f"{_fmt_bytes(rest_w / rest_span * 86400)}，读取 ≈ {_fmt_bytes(rest_r / rest_span * 86400)}")

    # ---- 峰值 tick ---- #
    print()
    print("【二】写入最高的 10 个采样区间（找尖峰 / 定时任务对齐）")
    print("-" * 78)
    top = sorted(records, key=lambda r: -r.get("total_w_b", 0))[:10]
    for rec in top:
        pids = ",".join(str(s["pid"]) for s in rec.get("procs", []) if s.get("d_w_b", 0) > 0)
        print(
            f"  t+{rec['elapsed']:9.1f}s  {rec['t']}  "
            f"W={_fmt_bytes(rec.get('total_w_b', 0)):>10}  "
            f"R={_fmt_bytes(rec.get('total_r_b', 0)):>10}  pid=[{pids}]"
        )

    # ---- 文件归因 ---- #
    file_stats: dict[str, dict] = {}
    for rec in records:
        for change in rec.get("changes", []):
            entry = file_stats.setdefault(
                change["path"],
                {"n": 0, "bytes": 0, "upper": 0, "kinds": set(), "max_delta": 0, "size_max": 0},
            )
            entry["n"] += 1
            entry["bytes"] += max(0, change.get("delta", 0))
            # 全量重写（tmp+replace 那类）的「净增」恒为 0，真实写入量要看文件大小。
            # 用 Σsize 作为「假设每次修改都是整文件重写」的上界，两种口径并排给出。
            size_after = change.get("size", 0) or 0
            entry["upper"] += size_after
            entry["size_max"] = max(entry["size_max"], size_after)
            entry["kinds"].add(change["kind"])
            entry["max_delta"] = max(entry["max_delta"], abs(change.get("delta", 0)))

    def classify(entry: dict) -> tuple[str, float]:
        """区分「追加型」和「整文件重写型」，给出可信的写入量估算。

        判据：整文件重写时每次改动的**净增**几乎为 0（内容重排，大小不变）；
        追加型每次都有实打实的正增量。用「平均每次净增 < 16 字节」做分界。
        """
        avg_delta = entry["bytes"] / max(1, entry["n"])
        if avg_delta < 16:
            return "重写", entry["upper"]
        return "追加", entry["bytes"]

    for path, entry in file_stats.items():
        if _is_sqlite_sidecar(path):
            # SQLite 附属文件：mtime 变动 **不等于** 内容全量写入，两种估算口径都失真 ——
            #   * -shm 是 32 KiB 的 mmap 共享索引，改几个字节也 bump mtime ⇒ 「重写」口径
            #     会把每次 tick 记成 32 KiB 幽灵写入，把真实写手挤出 Top 榜；
            #   * -wal 被 checkpoint 时会**变小** ⇒ 净增为负、平均净增 < 16B ⇒ 被误判成
            #     「整文件重写」，同样虚高。
            # 这里只保留「净增」作为诚信下界，并单列展示，不参与主榜排序。
            entry["model"] = "附属"
            entry["estimate"] = max(0, entry["bytes"])
            entry["sidecar"] = True
        else:
            entry["model"], entry["estimate"] = classify(entry)
            entry["sidecar"] = False

    # ---- 文件级估算必须受「进程级实测」约束（否则会把上界当事实播报）---- #
    # 物理约束：归因到这些文件的写入**不可能超过**被测进程自己报告的写入总量。
    # 但「重写」口径取的是「次数 × 文件大小」这个**上界**，遇到「文件很大、每次只改
    # 一部分」就会严重虚高 —— 典型是 SQLite 主库：checkpoint 只把**脏页**回写，
    # 而我们的模型按「整库 25.9 MiB 全量重写」计。2026-10-02 实测：memory.db 被估
    # 77.8 MiB（3 次 × 25.9 MiB），而整个后端在 8.7 小时里**只写了 22.2 MiB** ——
    # 虚高 3.5 倍，且直接污染【三-b】的每日外推（214.9 MiB/天 vs 实际 ~61 MiB/天）。
    # 故：估算合计 > 实测 ⇒ **等比折算**并标注，宁可低估也不敢虚报。
    measured_w = float(sum((r.get("total_w_b") or 0) for r in records))
    est_sum = float(sum(e["estimate"] for e in file_stats.values() if not e.get("sidecar")))
    est_scale = 1.0
    if est_sum > 0 and measured_w > 0 and est_sum > measured_w:
        est_scale = measured_w / est_sum

    print()
    print("【三】被写入的文件 Top 30（按可信写入量估算排序）")
    print("-" * 78)
    print("  「重写」= 每次整文件覆写，写入量 ≈ 次数 × 文件大小；「追加」= 写入量 ≈ 净增")
    if est_scale < 1.0:
        print(f"  ⚠ 文件级估算合计 {_fmt_bytes(est_sum)} > 进程级实测 {_fmt_bytes(measured_w)}"
              f"（物理上不可能）⇒ 已按 ×{est_scale:.2f} **等比折算**（标 *），"
              f"「重写」口径对大文件只改局部时会虚高")
    print(f"{'写入量估算':>13} {'模型':>5} {'次数':>6} {'频率':>9} {'文件大小':>10}  文件")
    ranked = [kv for kv in file_stats.items() if not kv[1].get("sidecar")]
    star = " *" if est_scale < 1.0 else ""
    for path, entry in sorted(ranked, key=lambda kv: -kv[1]["estimate"])[:30]:
        rel = os.path.relpath(path, os.getcwd()) if os.path.isabs(path) else path
        freq = f"{entry['n'] / (span / 60):.1f}/min" if span > 0 else "-"
        print(
            f"{_fmt_bytes(entry['estimate'] * est_scale):>13} {entry['model']:>5} {entry['n']:>6} "
            f"{freq:>9} {_fmt_bytes(entry['size_max']):>10}  {rel}{star}"
        )

    sidecars = [kv for kv in file_stats.items() if kv[1].get("sidecar")]
    if sidecars:
        print()
        print("【三-a】SQLite 附属文件（口径特殊，单列，不计入主榜）")
        print("-" * 78)
        print("  ⚠ mtime 变动 ≠ 全量写入：-shm 是 32 KiB mmap 索引（改几字节也 bump mtime），")
        print("     -wal 被 checkpoint 时会变小。这里只给「净增」下界；.db 主库的改动另行计。")
        print(f"{'净增(下界)':>12} {'次数':>6} {'文件大小':>10}  文件")
        for path, entry in sorted(sidecars, key=lambda kv: -kv[1]["size_max"])[:10]:
            rel = os.path.relpath(path, os.getcwd()) if os.path.isabs(path) else path
            print(f"{_fmt_bytes(entry['bytes']):>12} {entry['n']:>6} "
                  f"{_fmt_bytes(entry['size_max']):>10}  {rel}")

    # ---- 按「单位时间写入量」外推每日 ---- #
    daily = [(p, e) for p, e in file_stats.items() if e["estimate"] > 0 and not e.get("sidecar")]
    if daily and span > 0:
        print()
        print("【三-b】折算成每天（按本窗口频率线性外推，仅供量级参考）")
        print("-" * 78)
        if est_scale < 1.0:
            print(f"  （下列数值同样按 ×{est_scale:.2f} 折算过，与【三】口径一致）")
        rows_d = sorted(daily, key=lambda kv: -kv[1]["estimate"])[:12]
        tot_daily = 0.0
        for path, entry in rows_d:
            per_day = entry["estimate"] * est_scale / span * 86400
            tot_daily += per_day
            rel = os.path.relpath(path, os.getcwd()) if os.path.isabs(path) else path
            print(f"{_fmt_bytes(per_day) + '/天':>14}  {entry['model']:>4}  {rel}")
        print(f"{_fmt_bytes(tot_daily) + '/天':>14}  (以上文件合计)")

    print()
    print("【四】被写入的文件 Top 20（按修改次数 —— 高频重写信号）")
    print("-" * 78)
    for path, entry in sorted(file_stats.items(), key=lambda kv: -kv[1]["n"])[:20]:
        rel = os.path.relpath(path, os.getcwd()) if os.path.isabs(path) else path
        freq = f"{entry['n'] / (span / 60):.1f}/min" if span > 0 else "-"
        print(f"{entry['n']:>6} 次 {freq:>9}  估算 "
              f"{_fmt_bytes(entry['estimate'] * est_scale):>10}  {rel}")

    # ---- 系统盘 ---- #
    sys_w = sum(r.get("sys_disk", {}).get("d_write_b", 0) for r in records)
    sys_r = sum(r.get("sys_disk", {}).get("d_read_b", 0) for r in records)
    if sys_w or sys_r:
        print()
        print("【五】全盘物理 I/O（对照，含系统与其它进程）")
        print("-" * 78)
        print(f"  全盘写入 {_fmt_bytes(sys_w)}    全盘读取 {_fmt_bytes(sys_r)}")
        if sys_w:
            print(f"  被测进程占全盘写入 = {tot_w / sys_w * 100:.2f}%")
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="LocalAgent 前后端磁盘 I/O 审计探针",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", default="temp/disk_io_audit/probe.jsonl", help="JSONL 输出路径")
    parser.add_argument("--interval", type=float, default=10.0, help="采样间隔秒（默认 10）")
    parser.add_argument("--duration", type=float, default=0.0, help="总时长秒；0=一直跑")
    parser.add_argument("--match", default=",".join(DEFAULT_MATCH), help="被测进程 cmdline 关键字，逗号分隔")
    parser.add_argument(
        "--exclude",
        default=",".join(DEFAULT_EXCLUDE),
        help="要排除的进程 cmdline 关键字（默认排除探针自身与其传输外壳），逗号分隔",
    )
    parser.add_argument("--dirs", default="data,logs,temp,private_vault", help="监视目录（逗号分隔，相对/绝对）")
    parser.add_argument("--ignore", default="", help="额外跳过的路径前缀（文件级归因时忽略），逗号分隔")
    parser.add_argument("--report", default="", help="聚合模式：直接对一个 jsonl 生成报告")
    parser.add_argument(
        "--heartbeat",
        default="",
        help="心跳文件路径（每 tick 原子写入存活状态与当前速率；长跑时用来判断探针是否还活着）",
    )
    parser.add_argument("--verbose", action="store_true", help="每个 tick 都打印")
    parser.add_argument("--print-every", type=int, default=6, help="每 N 个 tick 打印一行摘要")
    args = parser.parse_args(argv)

    if args.report:
        return build_report(args.report)

    # 把相对目录解析到项目根（脚本所在 tools/disk_io_audit/ 的上两级）
    project_root = project_root_global()
    resolved = []
    for item in args.dirs.split(","):
        item = item.strip()
        if not item:
            continue
        resolved.append(item if os.path.isabs(item) else os.path.join(project_root, item))
    args.dirs = ",".join(resolved)

    return run_probe(args)


if __name__ == "__main__":
    sys.exit(main())
