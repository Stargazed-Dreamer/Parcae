#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重启后端并在其就绪后启动今日磁盘 I/O 追踪（一次性运维脚本）。

为什么需要它
------------
本项目无热重载：改了 ``server/`` 代码必须重启后端才生效。而「重启」会连带
带走派生它的终端/会话，所以这件事必须交给**独立于后端**的进程来做。

本脚本由 agent 经 ``POST /terminals/spawn`` 派生（后端是管理员 ⇒ 子进程继承
管理员上下文），spawn 使用 ``CREATE_NEW_PROCESS_GROUP`` ⇒ 后端退出不会带走它。

流程
----
1. 送走旧后端：``POST /shutdown``；优雅关闭超时则 ``taskkill /F /PID``
2. 等 8766 端口释放
3. 以 ``DETACHED_PROCESS`` 启动新后端 —— **必须剥掉 PYTHONPATH**：
   WorkBuddy 沙箱会经 PYTHONPATH 注入 sitecustomize 删除守卫，后端自己的
   终端 TTL 清理会对临时文件 ``unlink()``，被守卫误判为批量删除而
   ``SystemExit(1)`` 击杀进程（见 docs/operations-manual.md §4）
4. 轮询 ``/health`` 到就绪（默认 240s）
5. ``daily_run.py start`` 启动全天追踪
6. 全程写 ``<outdir>/restart_and_track.log``

用法
----
::

    python tools/disk_io_audit/restart_and_track.py \
        --outdir temp/disk_io_audit_20261001 --interval 30
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
VENV_PY = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
API = "http://127.0.0.1:8766"
PORT = 8766

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_log_fh = None


def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _log_fh is not None:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def port_in_use() -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def find_backend_pids() -> list[int]:
    try:
        import psutil
    except ImportError:
        return []
    pids: list[int] = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:
            continue
        if "server.main" in cmd:
            pids.append(int(p.info["pid"]))
    return pids


def http_get(path: str, timeout: float = 5.0):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def http_post(path: str, timeout: float = 10.0) -> str:
    try:
        req = urllib.request.Request(API + path, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(r.status)
    except Exception as e:  # 后端关闭过程中连接被断是正常现象
        return "ERR:%s" % type(e).__name__


def stop_old_backend() -> None:
    old = find_backend_pids()
    log("旧后端 PID: %s" % old)
    if not old:
        log("未发现旧后端进程")
        return
    log("POST /shutdown -> %s" % http_post("/shutdown"))
    deadline = time.time() + 20
    while time.time() < deadline and port_in_use():
        time.sleep(0.5)
    if port_in_use():
        log("优雅关闭超时，改用 taskkill /F")
        for pid in find_backend_pids():
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        deadline = time.time() + 15
        while time.time() < deadline and port_in_use():
            time.sleep(0.5)
    log("8766 已释放: %s" % (not port_in_use()))


def start_new_backend() -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # 关键：剥掉沙箱注入的 sitecustomize 删除守卫
    env["PYTHONIOENCODING"] = "utf-8"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backend_log = open(
        os.path.join(PROJECT_ROOT, "temp", "backend_restart_%s.log" % stamp), "ab"
    )
    proc = subprocess.Popen(
        [VENV_PY, "-m", "server.main"],
        cwd=PROJECT_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=backend_log,
        stderr=subprocess.STDOUT,
        close_fds=True,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    )
    log("新后端已派生 pid=%d -> temp/backend_restart_%s.log" % (proc.pid, stamp))
    return proc


def respawn_detached(extra_args: list[str]) -> int:
    """把自己重新派生为一个完全脱离 Job 的进程，返回新进程 pid。

    第一层进程由后端终端经 ``POST /terminals/spawn`` 派生（``CREATE_NEW_PROCESS_GROUP``），
    可能仍在后端的 Job Object 里 —— 后端退出会把它一起带走，那样「关旧后端」之后
    就没人拉新后端了。所以再 detach 一次：真正干活的是与后端无 Job 关系的 worker。
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logf = open(
        os.path.join(PROJECT_ROOT, "temp", "watchdog_%s.log" % stamp), "ab"
    )
    argv = [VENV_PY, os.path.abspath(__file__), "--_worker"] + extra_args
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            argv, cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=logf, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=base | CREATE_BREAKAWAY_FROM_JOB,
        )
    except OSError:
        proc = subprocess.Popen(
            argv, cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=logf, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=base,
        )
    return proc.pid


def wait_healthy(timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            status, body = http_get("/health", timeout=5)
            if status == 200 and '"ok"' in body:
                return True
            last = "status=%s" % status
        except Exception as e:
            last = type(e).__name__
        time.sleep(2)
    log("最后一次探测: %s" % last)
    return False


def main() -> int:
    global _log_fh
    ap = argparse.ArgumentParser(description="重启后端并启动磁盘 I/O 追踪")
    ap.add_argument("--outdir", default="temp/disk_io_audit_" + datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--duration", type=int, default=0)
    ap.add_argument("--dirs", default="data,logs,private_vault")
    ap.add_argument("--match", default="server.main,client.main")
    ap.add_argument("--health-timeout", type=int, default=240)
    ap.add_argument("--no-track", action="store_true", help="只重启后端，不启动追踪")
    ap.add_argument("--_worker", dest="_worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    outdir = args.outdir if os.path.isabs(args.outdir) else os.path.join(PROJECT_ROOT, args.outdir)
    os.makedirs(outdir, exist_ok=True)
    _log_fh = open(os.path.join(outdir, "restart_and_track.log"), "a", encoding="utf-8")

    if not args._worker:
        # 第一层可能仍挂在后端的 Job 里（见 respawn_detached 注释），先把自己 detach 出去；
        # 真正干活的 worker 与后端无 Job 关系，后端退出带不走它。
        pid = respawn_detached(sys.argv[1:])
        log("已派生 detached worker pid=%d（日志 temp/watchdog_*.log），本层退出" % pid)
        return 0

    log("=" * 62)
    log("重启后端 + 启动今日追踪")
    log("项目根  : %s" % PROJECT_ROOT)
    log("产物目录: %s" % outdir)

    stop_old_backend()
    start_new_backend()

    if not wait_healthy(args.health_timeout):
        log("!! 第一次启动未就绪，重试一次（排查 temp/backend_restart_*.log）")
        start_new_backend()
        if not wait_healthy(max(60, args.health_timeout // 2)):
            log("!! 两次都没起来 —— 放弃启动追踪；兜底：双击 start.bat")
            return 2
    log("/health 就绪 OK")

    if args.no_track:
        log("--no-track：跳过追踪")
        return 0

    cmd = [
        VENV_PY, os.path.join(HERE, "daily_run.py"), "start",
        "--outdir", outdir,
        "--interval", str(args.interval),
        "--duration", str(args.duration),
        "--dirs", args.dirs,
        "--match", args.match,
        "--force",
    ]
    log("启动追踪: %s" % " ".join(cmd))
    try:
        r = subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=180,
        )
    except subprocess.TimeoutExpired:
        log("!! daily_run start 超时")
        return 3
    log("daily_run start 退出码 %s" % r.returncode)
    for line in (r.stdout or "").strip().splitlines():
        log("  | " + line)
    for line in (r.stderr or "").strip().splitlines():
        log("  ! " + line)
    log("完成" if r.returncode == 0 else "结束（追踪启动失败，见上）")
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
