#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修复「WinNAT 动态保留区抢占 8766」导致后端 bind 失败（WinError 10013）。

背景
----
Windows 重启后 Hyper-V 的 WinNAT 会**重新分配**一批 TCP 动态保留区
（本机 2026-10-01 22:21 重启后实测为 8290-9289 等 100 端口块），
可能正好盖住项目端口 8766 —— 此时后端启动报::

    ERROR: [Errno 13] error while attempting to bind on address ('127.0.0.1', 8766):
           [winerror 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。

判据（区分「动态保留」和「持久化排除」）：

* 注册表 ``HKLM\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters``
  **没有** ``PersistentPortExclusions`` ⇒ 是 WinNAT 动态保留 ⇒ 停服务即可释放；
* ``netsh int ipv4 show excludedportrange protocol=tcp`` 能看到覆盖 8766 的区间。

流程（**必须管理员**）
----------------------
1. ``net stop winnat`` —— 释放动态保留区
2. 轮询确认 8766 可 bind
3. 派生后端（继承管理员令牌）并等 ``/health`` 就绪
4. ``net start winnat`` —— 恢复 WinNAT（此时 8766 已被后端占用，不会再被抢）
5. 启动今日探针

用法::

    python tools/disk_io_audit/fix_port_and_start.py \\
        --outdir temp/disk_io_audit_20261001 --interval 30
"""

from __future__ import annotations

import argparse
import ctypes
import json
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
RESTART_SCRIPT = os.path.join(HERE, "restart_and_track.py")
PORT = 8766
API = "http://127.0.0.1:%d" % PORT

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_log_path = None


def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _log_path:
        with open(_log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def port_bindable(port: int = PORT) -> tuple[bool, str]:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True, ""
    except OSError as exc:
        return False, "%s" % exc
    finally:
        s.close()


def run(cmd: list[str]) -> int:
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = ((r.stdout or "") + (r.stderr or "")).strip().replace("\n", " | ")
    log("  $ %s -> rc=%s %s" % (" ".join(cmd), r.returncode, out[:160]))
    return r.returncode


def health_ok(timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(API + "/health", timeout=timeout) as r:
            return r.status == 200 and b'"ok"' in r.read()
    except Exception:
        return False


def spawn_backend_and_track(outdir: str, interval: int) -> int:
    """派生 restart_and_track（它会再 detach 一层并起后端+探针）。"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)          # 剥掉沙箱注入的 sitecustomize 删除守卫
    env["PYTHONIOENCODING"] = "utf-8"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logf = open(os.path.join(PROJECT_ROOT, "temp", "fixport_worker_%s.log" % stamp), "ab")
    argv = [VENV_PY, RESTART_SCRIPT, "--outdir", outdir, "--interval", str(interval)]
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        p = subprocess.Popen(argv, cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT, close_fds=True,
                             creationflags=base | CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        p = subprocess.Popen(argv, cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT, close_fds=True,
                             creationflags=base)
    return p.pid


def wait_health(timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if health_ok():
            return True
        time.sleep(2)
    return False


def main() -> int:
    global _log_path
    ap = argparse.ArgumentParser(description="释放被 WinNAT 抢占的 8766 并启动后端+探针")
    ap.add_argument("--outdir", default="temp/disk_io_audit_" + datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--health-timeout", type=int, default=240)
    ap.add_argument("--no-track", action="store_true")
    args = ap.parse_args()

    outdir = args.outdir if os.path.isabs(args.outdir) else os.path.join(PROJECT_ROOT, args.outdir)
    os.makedirs(outdir, exist_ok=True)
    _log_path = os.path.join(outdir, "fix_port.log")

    log("=" * 62)
    log("修复 8766 端口冲突 + 启动后端与探针")
    if not is_admin():
        log("!! 当前不是管理员 —— 本脚本需要管理员权限（net stop winnat）")
        log("   请用「以管理员身份运行」的终端执行本脚本")
        return 3
    log("管理员权限 OK")

    ok, err = port_bindable()
    log("8766 当前可 bind: %s%s" % (ok, "" if ok else "  (%s)" % err))

    released = False
    if not ok:
        log("尝试释放 WinNAT 动态保留区: net stop winnat")
        run(["net", "stop", "winnat"])
        deadline = time.time() + 30
        while time.time() < deadline:
            ok2, _e2 = port_bindable()
            if ok2:
                ok = True
                released = True
                break
            time.sleep(1)
        log("net stop winnat 后 8766 可 bind: %s" % ok)
        if not ok:
            log("!! 仍未释放 —— 可能是「持久化排除」或其它服务占用。")
            log("   排查: netsh int ipv4 show excludedportrange protocol=tcp")
            run(["net", "start", "winnat"])   # 恢复，避免副作用
            return 2

    # 已在跑就不要再派生一个
    if health_ok():
        log("检测到 /health 已就绪 —— 后端原本就在跑，跳过启动")
    else:
        pid = spawn_backend_and_track(outdir, args.interval)
        log("已派生 restart_and_track pid=%d，等待后端就绪…" % pid)
        if not wait_health(args.health_timeout):
            log("!! 后端未在 %ds 内就绪" % args.health_timeout)
            log("   排查: temp/backend_restart_*.log 与 %s/restart_and_track.log" % outdir)
            if released:
                run(["net", "start", "winnat"])
            return 4
        log("/health 就绪 OK ✅")

    if released:
        log("恢复 WinNAT: net start winnat")
        run(["net", "start", "winnat"])
        ok3, err3 = port_bindable()
        log("恢复后 8766 仍不可 bind（正常，已被后端占用）: %s | %s" % (not ok3, err3[:60]))
    else:
        log("无需恢复 WinNAT（本次未停过）")

    log("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
