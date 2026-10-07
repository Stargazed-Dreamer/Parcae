#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全日磁盘 I/O 审计的「启动 / 状态 / 停止 / 报告」入口。

为什么需要它
------------
`probe.py` 只管采样；一口气跑一整天时，真正容易出问题的是外围：
输出目录要今天建好、进程要能脱离启动它的终端活下去、中途挂了要能发现
并能续采（而不是白等一天）、报告要对得上文件。

用法
----
::

    python tools/disk_io_audit/daily_run.py start      # 启动全天采样（默认 0=一直跑）
    python tools/disk_io_audit/daily_run.py status     # 探针还活着吗？跑到哪了？
    python tools/disk_io_audit/daily_run.py report     # 出报告
    python tools/disk_io_audit/daily_run.py stop       # 手动结束

设计要点（照着这些前提才能安全地跑一天）
----------------------------------------
1. **产物落 ``temp/disk_io_audit_<YYYYMMDD>/``**：``probe_daily.jsonl``（采样）、
   ``probe.heartbeat``（心跳）、``probe.log``（stdout）、``RUN_INFO.json``（本次启动参数）。
2. **可续采**：``probe.py`` 以追加模式写 jsonl，重复 ``start`` 不会覆盖已有数据；
   探针重启后 ``prev_counters`` 为空 ⇒ 该轮首 tick 增量为 0，**不会重复计数**。
   所以中途挂了直接再 ``start`` 一次即可，报告侧已能识别多段并正确算跨度。
3. **防重复启动**：心跳文件在 ``interval`` 内有更新 ⇒ 判定已有探针在跑，``start``
   会拒绝（要强来加 ``--force``）。避免两个探针往同一个 jsonl 里交错写。
4. **脱离终端**：用 ``DETACHED_PROCESS`` 启动被测探针，启动它的终端退出/会话结束
   不会带走它。（⚠️ 后端如果被重启，探针仍可能一起没；``status`` 会发现，重跑即可。）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
VENV_PY = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
PROBE = os.path.join(HERE, "probe.py")

# 探针默认（可被命令行覆盖）
DEFAULT_DIRS = "data,logs,private_vault"
DEFAULT_MATCH = "server.main,client.main"

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# 心跳新鲜度倍率：超过 interval 的 N 倍没更新就认为探针已经死了
STALE_FACTOR = 3


def _fmt_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PiB"


def resolve_outdir(raw: str) -> str:
    """把 ``--outdir`` 解析成绝对路径；默认 = temp/disk_io_audit_<YYYYMMDD>。

    ⛔ **跨零点的坑（2026-10-02 01:20 实测踩到）**：追踪是「续采」式的，一轮会跨过
    午夜继续往昨天的目录里追加。而默认目录按**当天日期**拼 ⇒ 零点之后不带
    ``--outdir`` 的 ``status`` / ``stop`` / ``report`` 全会指向一个**还不存在的空目录**，
    表现为「未发现心跳文件 —— 还没跑过」——**明明探针在跑却说没跑过**，而且 ``stop``
    会因此停不掉。所以默认目录为空时，回退到**最近一个真有产物的旧目录**并明确打印。
    """
    if not raw:
        raw = os.path.join("temp", "disk_io_audit_" + datetime.now().strftime("%Y%m%d"))
    path = raw if os.path.isabs(raw) else os.path.abspath(os.path.join(PROJECT_ROOT, raw))
    if raw and raw != os.path.join("temp", "disk_io_audit_" + datetime.now().strftime("%Y%m%d")):
        return path  # 用户显式指定 ⇒ 尊重，不做任何回退

    if os.path.exists(os.path.join(path, "probe.heartbeat")) or os.path.exists(
        os.path.join(path, "probe_daily.jsonl")
    ):
        return path

    temp_root = os.path.join(PROJECT_ROOT, "temp")
    try:
        candidates = [
            d for d in os.listdir(temp_root)
            if d.startswith("disk_io_audit_")
            and os.path.exists(os.path.join(temp_root, d, "probe.heartbeat"))
        ]
    except OSError:
        return path
    if candidates:
        latest = max(candidates)
        newest = os.path.join(temp_root, latest)
        print(f"（今天 {os.path.basename(path)} 还没有产物 —— 自动回退到最近一轮："
              f"temp/{latest}；要指定别的一天请显式传 --outdir）")
        return newest
    return path


def paths_of(outdir: str) -> dict:
    return {
        "dir": outdir,
        "jsonl": os.path.join(outdir, "probe_daily.jsonl"),
        "heartbeat": os.path.join(outdir, "probe.heartbeat"),
        "log": os.path.join(outdir, "probe.log"),
        "info": os.path.join(outdir, "RUN_INFO.json"),
    }


def read_heartbeat(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def heartbeat_age(path: str) -> float | None:
    """心跳文件距上次更新过了多少秒；文件不存在返回 None。"""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def pid_alive(pid) -> bool | None:
    """进程是否存活；无法判定返回 None。

    ⛔ 只靠心跳新鲜度判「运行中」会**撒谎**：心跳阈值默认 90s，探针被杀后最长 90s
    内 ``status`` 仍报「运行中」（2026-10-01 22:52 实测：已 terminate pid=59272，
    status 依然显示运行中）。所以存活判定必须 = 心跳新鲜 **且** pid 真的在。
    """
    if not pid:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        return None


def heartbeat_written_after(path: str, ts: float) -> bool:
    """心跳是否在 ``ts`` 之后被写过。

    ⛔ 这一条是必需的：同一 ``--outdir`` 会跨多次运行复用，上一轮留下的**旧心跳**
    会让「启动即确认」立刻通过，并报出**已经死掉的旧 pid**（2026-10-01 22:30 实测
    踩到：新探针 pid=59272，确认却报旧 pid=73864）。只认本次 spawn 之后写的心跳。
    """
    try:
        return os.path.getmtime(path) >= ts - 0.5
    except OSError:
        return False


def python_exe() -> str:
    """优先用项目 venv 的解释器（有 psutil）；没有就退回当前解释器。"""
    return VENV_PY if os.path.isfile(VENV_PY) else sys.executable


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #

def cmd_start(args) -> int:
    p = paths_of(resolve_outdir(args.outdir))
    os.makedirs(p["dir"], exist_ok=True)

    age = heartbeat_age(p["heartbeat"])
    hb_now = read_heartbeat(p["heartbeat"]) or {}
    # ⛔ 同样不能只看心跳：探针刚被杀后心跳还「新鲜」，会挡住立刻重启（实测踩到）。
    #    只有「心跳新鲜 + pid 确实活着」才算真的已在跑。
    if (age is not None and age < args.interval * STALE_FACTOR
            and pid_alive(hb_now.get("probe_pid")) is not False and not args.force):
        hb = hb_now
        print(f"已有探针在跑（心跳 {age:.1f}s 前更新，pid={hb.get('probe_pid')}, "
              f"ticks={hb.get('ticks')}, out={hb.get('out')}）")
        print("如确认要再起一个（不推荐，会往同一文件交错写），加 --force")
        return 1
    if age is not None and age < args.interval * STALE_FACTOR and not args.force:
        print(f"（检测到 {age:.1f}s 前的旧心跳，但 pid={hb_now.get('probe_pid')} 已不存在，"
              f"按已停止处理）")

    argv = [
        python_exe(), PROBE,
        "--duration", str(args.duration),
        "--interval", str(args.interval),
        "--dirs", args.dirs,
        "--match", args.match,
        "--out", p["jsonl"],
        "--heartbeat", p["heartbeat"],
    ]
    if args.extra:
        argv += args.extra

    log = open(p["log"], "a", encoding="utf-8")
    spawn_ts = time.time()          # 之后写出的心跳才算「本次启动的」
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            argv, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,
        )
    except OSError:
        # 有些 Job 不允许 breakaway，退一步用普通分离
        proc = subprocess.Popen(
            argv, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=flags,
        )
    log.close()

    # venv 里的 python.exe 往往是「启动器」（uv 环境会再 exec 一次到基础解释器），
    # 所以 Popen 拿到的 pid 不是真正干活的那个。以心跳里自报的 probe_pid 为准，
    # 顺便把「启动了但其实没在采」这个长跑最坑的失败模式变成**启动即确认**。
    #
    # ⚠️ 两个坑（2026-10-01 实测）：
    #   1) 只认 spawn_ts 之后写出的心跳，否则会读上一轮遗留的旧心跳并报出死 pid；
    #   2) **不要**用 proc.poll() 早退 —— venv 启动器会先退出（探针换了个新 pid），
    #      一看它退出就 break 会误判成「没跑起来」。
    real_pid = proc.pid
    got_hb = False
    deadline = spawn_ts + 12.0
    while time.time() < deadline:
        hb = read_heartbeat(p["heartbeat"])
        if hb and hb.get("probe_pid") and heartbeat_written_after(p["heartbeat"], spawn_ts):
            real_pid = int(hb["probe_pid"])
            got_hb = True
            break
        time.sleep(0.5)

    with open(p["info"], "w", encoding="utf-8") as fh:
        json.dump(
            {
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "launcher_pid": proc.pid,
                "probe_pid": real_pid if got_hb else None,
                "argv": argv,
                "outdir": p["dir"],
            },
            fh, ensure_ascii=False, indent=2,
        )

    print(f"  采样输出 : {p['jsonl']}")
    print(f"  心跳     : {p['heartbeat']}")
    print(f"  日志     : {p['log']}")
    print(f"  参数     : interval={args.interval}s  duration={args.duration or 'inf'}s  "
          f"dirs={args.dirs}  match={args.match}")
    print()
    if got_hb:
        print(f"启动确认：探针 pid={real_pid} 已在写心跳 ✅")
        print("  随时用 `status` 看进度，`report` 出报告，`stop` 结束。")
        return 0

    print("⚠️ 启动异常：8 秒内没有收到心跳 —— 探针可能没跑起来。")
    print(f"   请查看日志：{p['log']}")
    try:
        with open(p["log"], "r", encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-800:]
        if tail.strip():
            print("   ---- probe.log 末尾 ----")
            for line in tail.strip().splitlines()[-8:]:
                print(f"   {line}")
    except OSError:
        pass
    return 1


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def cmd_status(args) -> int:
    p = paths_of(resolve_outdir(args.outdir))
    hb = read_heartbeat(p["heartbeat"])
    age = heartbeat_age(p["heartbeat"])

    if hb is None or age is None:
        print(f"未发现心跳文件：{p['heartbeat']}")
        if os.path.exists(p["jsonl"]):
            n = sum(1 for _ in open(p["jsonl"], "r", encoding="utf-8", errors="replace"))
            print(f"但采样文件存在（{n} 行）—— 探针可能已结束或从未启动。")
        else:
            print("采样文件也不存在 —— 还没跑过。")
        return 2

    stale_after = args.interval * STALE_FACTOR
    pid = hb.get("probe_pid")
    pid_ok = pid_alive(pid)
    # 存活 = 心跳新鲜 ∧ pid 真在（pid 无法判定时退回只看心跳）
    alive = age < stale_after and (pid_ok is not False)
    if alive:
        note = f"（心跳 {age:.0f}s 前更新，阈值 {stale_after:.0f}s）"
    elif age < stale_after:
        note = f"（心跳 {age:.0f}s 前更新看似新鲜，但 pid={pid} 已不存在 ⇒ 已退出）"
    else:
        note = f"（心跳 {age:.0f}s 前更新，超过阈值 {stale_after:.0f}s）"
    print(f"状态        : {'运行中' if alive else '已停止'}{note}")
    print(f"探针 pid    : {pid}{'' if pid_ok is None else ('（存活）' if pid_ok else '（不存在）')}")
    print(f"最近 tick   : {hb.get('t')}   ticks={hb.get('ticks')}   t+{hb.get('elapsed_s')}s")
    print(f"当前速率    : 写 {_fmt_bytes(hb.get('w_rate', 0))}/s   读 {_fmt_bytes(hb.get('r_rate', 0))}/s")
    print(f"被测进程    : {hb.get('targets')}")

    if os.path.exists(p["jsonl"]):
        size = os.path.getsize(p["jsonl"])
        with open(p["jsonl"], "r", encoding="utf-8", errors="replace") as fh:
            n = sum(1 for _ in fh)
        print(f"采样文件    : {n} 行 / {_fmt_bytes(size)}")
        # 累计写入量 = 所有 tick 的 total_w_b 之和
        tot_w = tot_r = 0
        with open(p["jsonl"], "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tot_w += rec.get("total_w_b", 0)
                tot_r += rec.get("total_r_b", 0)
        print(f"累计(窗口内): 写 {_fmt_bytes(tot_w)}   读 {_fmt_bytes(tot_r)}")
    return 0 if alive else 1


# --------------------------------------------------------------------------- #
# stop
# --------------------------------------------------------------------------- #

def cmd_stop(args) -> int:
    p = paths_of(resolve_outdir(args.outdir))
    hb = read_heartbeat(p["heartbeat"])
    if not hb:
        print(f"没有心跳文件，无法定位探针进程：{p['heartbeat']}")
        return 2
    pid = hb.get("probe_pid")
    if not pid:
        print("心跳里没有 probe_pid")
        return 2

    try:
        import psutil
    except ImportError:
        print("需要 psutil 才能安全地校验并停止进程（.venv 里有）")
        return 2

    try:
        proc = psutil.Process(int(pid))
        cmd = " ".join(proc.cmdline())
    except Exception as exc:
        print(f"进程 {pid} 已不存在或无法读取（{exc}）—— 视为已停止")
        return 0

    # 安全闸：只允许停「确实是本探针」的进程
    if "probe.py" not in cmd or "disk_io_audit" not in cmd:
        print(f"安全拒绝：pid={pid} 的命令行不像本探针：{cmd[:160]}")
        return 3

    # ⚠️ 两个坑：
    #   ① 探针通常由后端（管理员）派生，普通上下文里 `proc.wait()` 会因权限读不到而
    #      超时 —— 但它其实**已经死了**，所以确认存活要用 pid_exists 轮询，别信 wait()。
    #   ② 在沙箱化的 bash 里 terminate 可能被静默拦下（表现为进程还在），所以结束
    #      探针请优先从后端终端执行；这里逐级升级并给出 taskkill 兜底。
    def _alive(target: int) -> bool:
        try:
            psutil.Process(target).status()
            return True
        except psutil.NoSuchProcess:
            return False
        except Exception:
            return True  # 读不到就保守认为还活着

    for action, label in ((proc.terminate, "terminate"), (proc.kill, "kill")):
        try:
            action()
        except psutil.AccessDenied:
            print(f"无权限停止 pid={pid}（探针由更高权限进程启动）。")
            print(f"  兜底：在管理员终端执行  taskkill /F /PID {pid}")
            return 3
        except Exception as exc:
            print(f"{label} 失败：{exc}")
            continue
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if not _alive(int(pid)):
                print(f"已停止探针 pid={pid}（{label}）")
                return 0
            time.sleep(0.4)

    print(f"pid={pid} 仍在运行（可能被沙箱拦下）。")
    print(f"  兜底：在管理员终端执行  taskkill /F /PID {pid}")
    return 1


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def cmd_report(args) -> int:
    p = paths_of(resolve_outdir(args.outdir))
    if not os.path.exists(p["jsonl"]):
        print(f"没有采样文件：{p['jsonl']}")
        return 2
    sys.path.insert(0, HERE)
    import probe  # noqa: PLC0415 - 延迟导入，避免无谓的 Win32 初始化

    return probe.build_report(p["jsonl"])


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="全日磁盘 I/O 审计入口（start / status / stop / report）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("action", choices=["start", "status", "stop", "report"])
    parser.add_argument("--outdir", default="", help="产物目录（默认 temp/disk_io_audit_<YYYYMMDD>）")
    parser.add_argument("--interval", type=float, default=30.0, help="采样间隔秒（默认 30）")
    parser.add_argument("--duration", type=float, default=0.0, help="总时长秒；0=一直跑（默认 0）")
    parser.add_argument("--dirs", default=DEFAULT_DIRS, help=f"监视目录（默认 {DEFAULT_DIRS}）")
    parser.add_argument("--match", default=DEFAULT_MATCH, help=f"被测进程关键字（默认 {DEFAULT_MATCH}）")
    parser.add_argument("--force", action="store_true", help="start 时忽略「已有探针在跑」的检查")
    parser.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="透传给 probe.py 的额外参数")
    args = parser.parse_args(argv)

    return {
        "start": cmd_start,
        "status": cmd_status,
        "stop": cmd_stop,
        "report": cmd_report,
    }[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
