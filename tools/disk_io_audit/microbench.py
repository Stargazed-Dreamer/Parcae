#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""微基准：一次操作给后端带来多少磁盘写入？

思路
----
``probe.py`` 给的是**总量与速率**，回答不了「设计是否合理」这种问题 —— 因为
合理与否取决于「每次操作花多少」。本脚本用同一套 ``GetProcessIoCounters``
通道，把测量切成「一次操作」的粒度：

    idle 基线速率 (B/s)
      ↓
    连打 N 次某个端点 / 工具，测该批次的 ΔW 与耗时
      ↓
    单次成本 = (ΔW - idle_rate × Δt) / N

被测对象只有后端主进程自身（不含它派生的 runner/子进程），所以数字干净。

**关键前提（已由 selftest_io_channels.py 实测确认）**：
``GetProcessIoCounters`` **不计 socket、计 stdout/管道**。所以本脚本自己必须
从「后端进程树之外」发起 HTTP —— 用本机 python 直接跑（别用后端的
exec_python），否则控制台/管道写入会把结果顶高。

用法::

    python tools/disk_io_audit/microbench.py --idle 60 --n 5
    python tools/disk_io_audit/microbench.py --pid 328796 --n 10
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
import urllib.error
import urllib.request

if os.name != "nt":
    print("Windows only")
    sys.exit(2)

BASE = "http://127.0.0.1:8766"

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def read_io(pid: int):
    """返回 (read_bytes, write_bytes)，失败返回 None。"""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return None
    try:
        counters = IO_COUNTERS()
        if not _k32.GetProcessIoCounters(handle, ctypes.byref(counters)):
            return None
        return int(counters.ReadTransferCount), int(counters.WriteTransferCount)
    finally:
        _k32.CloseHandle(handle)


def find_backend() -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    best = None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "server.main" not in cmdline:
                continue
            if any(tag in cmdline for tag in ("probe.py", "disk_io_audit", "_exec_runner", "microbench")):
                continue
            io = read_io(proc.info["pid"])
            if io is None:
                continue
            if best is None or io[1] > best[1]:
                best = (proc.info["pid"], io[1])
        except Exception:
            continue
    return best[0] if best else None


# --------------------------------------------------------------------------- #
# HTTP / MCP 客户端
# --------------------------------------------------------------------------- #

def http_get(path: str, timeout: float = 30.0) -> tuple[int, int]:
    """GET 一个端点，返回 (状态码, 响应字节数)。"""
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, len(body)
    except urllib.error.HTTPError as exc:
        return exc.code, 0


class McpClient:
    """MCP Streamable HTTP 的最小客户端（只够 tools/call）。"""

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout
        self.session_id: str | None = None
        self._seq = 0

    def _post(self, payload: dict) -> tuple[int, bytes, dict]:
        self._seq += 1
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(BASE + "/mcp", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    @staticmethod
    def _parse(body: bytes) -> dict:
        text = body.decode("utf-8", errors="replace").strip()
        if text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    candidate = line[5:].strip()
                    if candidate:
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            continue
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def initialize(self) -> tuple[bool, str]:
        status, body, headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "diskio-audit", "version": "1.0"},
                },
            }
        )
        if status != 200:
            return False, f"initialize HTTP {status}: {body[:200]!r}"
        sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        if not sid:
            return False, "initialize 未返回 mcp-session-id"
        self.session_id = sid
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return True, sid

    def call_tool(self, name: str, arguments: dict | None = None) -> tuple[bool, str]:
        self._seq += 1
        status, body, _ = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._seq,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if status != 200:
            return False, f"HTTP {status}: {body[:160]!r}"
        parsed = self._parse(body)
        if "error" in parsed:
            return False, f"RPC error: {str(parsed['error'])[:160]}"
        return True, "ok"


# --------------------------------------------------------------------------- #
# 测量
# --------------------------------------------------------------------------- #

def measure(pid: int, label: str, action, idle_rate: float | None, n: int = 1) -> dict:
    before = read_io(pid)
    t0 = time.time()
    detail = ""
    ok_count = 0
    for _ in range(n):
        try:
            ok, detail = action()
            if ok:
                ok_count += 1
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
    t1 = time.time()
    after = read_io(pid)
    if before is None or after is None:
        raise RuntimeError(f"读取 PID {pid} 的 I/O 计数器失败")
    dt = max(1e-6, t1 - t0)
    d_w = max(0, after[1] - before[1])
    d_r = max(0, after[0] - before[0])
    idle_share = (idle_rate or 0.0) * dt
    net = max(0.0, d_w - idle_share)
    return {
        "label": label,
        "n": n,
        "dt": dt,
        "d_w": d_w,
        "d_r": d_r,
        "idle_share": idle_share,
        "net_w": net,
        "per_op": net / n if n else 0.0,
        "ok": f"{ok_count}/{n}",
        "detail": detail[:110],
    }


def fmt_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TiB"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="单次操作磁盘写入成本微基准")
    parser.add_argument("--pid", type=int, default=0, help="后端 PID；0=自动探测")
    parser.add_argument("--idle", type=float, default=60.0, help="空转基线时长（秒）")
    parser.add_argument("--n", type=int, default=5, help="每个用例重复次数")
    parser.add_argument("--skip-mcp", action="store_true", help="跳过 MCP 用例")
    args = parser.parse_args(argv)

    pid = args.pid or find_backend()
    if not pid:
        print("找不到后端进程；请用 --pid 指定，或确认 `python -m server.main` 在跑")
        return 1
    print(f"后端 PID = {pid}")
    io0 = read_io(pid)
    if io0 is None:
        print("无法读取该进程的 I/O 计数器（权限不足？）")
        return 1
    print(f"初始累计: 读 {fmt_bytes(io0[0])}  写 {fmt_bytes(io0[1])}")
    print()

    # ---- 1) 空转基线 ---- #
    print(f"[1] 量空转基线 {args.idle:.0f}s ...")
    before = read_io(pid)
    t0 = time.time()
    time.sleep(args.idle)
    t1 = time.time()
    after = read_io(pid)
    if before is None or after is None:
        print("空转采样期间无法读取 I/O 计数器（进程退出？）")
        return 1
    dt = t1 - t0
    idle_w = max(0, after[1] - before[1])
    idle_r = max(0, after[0] - before[0])
    idle_rate = idle_w / dt
    print(f"    空转 {dt:.1f}s: 写 {fmt_bytes(idle_w)} ({idle_rate:.0f} B/s)  "
          f"读 {fmt_bytes(idle_r)}")
    print(f"    → 外推 24h 空转写入 ≈ {fmt_bytes(idle_rate * 86400)}")
    print()

    rows: list[dict] = []

    # ---- 2) 用例 ---- #
    rows.append(measure(pid, "GET /health ×N", lambda: (http_get("/health")[0] == 200, "200"), idle_rate, args.n))
    rows.append(measure(pid, "GET /guide ×N", lambda: (http_get("/guide")[0] == 200, "200"), idle_rate, args.n))
    rows.append(measure(pid, "GET /mcp/stats ×N", lambda: (http_get("/mcp/stats")[0] == 200, "200"), idle_rate, args.n))
    rows.append(measure(pid, "GET /exec/status ×N", lambda: (http_get("/exec/status")[0] == 200, "200"), idle_rate, args.n))
    rows.append(measure(pid, "GET /loop/status ×N", lambda: (http_get("/loop/status")[0] == 200, "200"), idle_rate, args.n))

    if not args.skip_mcp:
        client = McpClient()
        ok, info = client.initialize()
        if ok:
            print(f"[2] MCP 会话建立: {info}")
            rows.append(measure(pid, "MCP memory_status ×N",
                                lambda: client.call_tool("memory_status"), idle_rate, args.n))
            rows.append(measure(pid, "MCP exec_status ×N",
                                lambda: client.call_tool("exec_status"), idle_rate, args.n))
        else:
            print(f"[2] MCP 不可用，跳过: {info}")

    # ---- 3) 汇总 ---- #
    print()
    print("=" * 96)
    print("单次操作磁盘写入成本（已扣除空转基线份额）")
    print("=" * 96)
    header = (f"{'用例':<24}{'N':>4}{'耗时':>9}{'ΔW':>12}{'扣基线':>12}"
              f"{'净写入':>12}{'单次':>12}{'单次读':>11}  状态")
    print(header)
    print("-" * 96)
    for row in rows:
        print(f"{row['label']:<24}{row['n']:>4}{row['dt']:>8.2f}s"
              f"{fmt_bytes(row['d_w']):>12}{fmt_bytes(row['idle_share']):>12}"
              f"{fmt_bytes(row['net_w']):>12}{fmt_bytes(row['per_op']):>12}"
              f"{fmt_bytes(row['d_r'] / max(1, row['n'])):>11}  {row['ok']}")
        if row["detail"] and row["ok"].split("/")[0] != row["ok"].split("/")[1]:
            print(f"    └─ {row['detail']}")
    print("-" * 96)
    print(f"空转基线: {idle_rate:.0f} B/s  →  24h ≈ {fmt_bytes(idle_rate * 86400)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
