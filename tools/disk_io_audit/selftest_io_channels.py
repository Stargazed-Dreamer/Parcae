#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针方法论自校验：GetProcessIoCounters 到底统计哪些通道？

背景
----
``GetProcessIoCounters`` 的 ``WriteTransferCount`` 来自内核为「本进程发起的
``NtWriteFile``」累计的字节数。问题在于：Windows 上 **socket 是文件对象**
（AFD 设备），stdout/管道同样是文件对象 —— 所以套接字发送、控制台输出都
可能被计进 ``WriteTransferCount``，让「进程写入量」被高估成「磁盘写入量」。

本脚本用三个对照实验把这件事钉死：

  A. 纯文件写入 16 MiB           → 期望计入
  B. 纯回环 socket 发送 16 MiB   → 若计入 ⇒ 计数器**不能**直接当磁盘写入用
  C. 纯 stdout 输出 16 MiB       → 同上，检验控制台/管道

用法::

    python tools/disk_io_audit/selftest_io_channels.py

只读/写临时目录，不做任何破坏性操作。
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import tempfile
import threading

if os.name != "nt":
    print("Windows only")
    sys.exit(2)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CHUNK = 1 << 16          # 64 KiB
TOTAL = 16 << 20         # 16 MiB


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def snap() -> int:
    """返回本进程当前的 WriteTransferCount。"""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, os.getpid())
    try:
        counters = IO_COUNTERS()
        if not _k32.GetProcessIoCounters(handle, ctypes.byref(counters)):
            raise OSError("GetProcessIoCounters failed")
        return int(counters.WriteTransferCount)
    finally:
        _k32.CloseHandle(handle)


def measure(label: str, action) -> None:
    """跑一个动作，打印它带来的 WriteTransferCount 增量。

    结果一律写 stderr —— C 实验会把 16 MiB 灌进 stdout，混在一起根本看不见结论。
    跑法：``python selftest_io_channels.py >/dev/null 2>result.txt``
    """
    before = snap()
    action()
    after = snap()
    delta = after - before
    verdict = "计入" if delta > TOTAL // 2 else "不计入"
    print(f"  {label:<34} 增量 {delta / 1048576:8.2f} MiB   -> {verdict}", file=sys.stderr)


# --- A. 文件写入 ----------------------------------------------------------- #

def write_file() -> None:
    path = os.path.join(tempfile.gettempdir(), "io_selftest_file.bin")
    with open(path, "wb") as fh:
        for _ in range(TOTAL // CHUNK):
            fh.write(b"x" * CHUNK)
        fh.flush()
        os.fsync(fh.fileno())
    os.unlink(path)


# --- B. 回环 socket -------------------------------------------------------- #

def write_socket() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def drain() -> None:
        conn, _ = srv.accept()
        got = 0
        while got < TOTAL:
            data = conn.recv(CHUNK)
            if not data:
                break
            got += len(data)
        conn.close()
        srv.close()

    worker = threading.Thread(target=drain, daemon=True)
    worker.start()
    sock = socket.create_connection(("127.0.0.1", port))
    payload = b"y" * CHUNK
    sent = 0
    while sent < TOTAL:
        sock.sendall(payload)
        sent += CHUNK
    worker.join(timeout=5)
    sock.close()


# --- C. stdout / 控制台 ---------------------------------------------------- #

def write_stdout() -> None:
    blank = "z" * (CHUNK - 1) + "\n"
    for _ in range(TOTAL // CHUNK):
        sys.stdout.write(blank)
    sys.stdout.flush()


def main() -> int:
    out = sys.stderr
    print("GetProcessIoCounters 通道自校验 (本进程自身)", file=out)
    print(f"每个实验发送量 {TOTAL / 1048576:.0f} MiB  (结果走 stderr，stdout 留给 C 实验)", file=out)
    print(file=out)
    measure("A. 文件写入 (fsync)", write_file)
    measure("B. 回环 socket 发送", write_socket)
    measure("C. stdout 输出", write_stdout)
    print(file=out)
    print("结论：若 B/C 显示『计入』，则进程计数器是 I/O **上界**（含网络与管道），", file=out)
    print("      不能直接等同于磁盘写入；必须配合目录快照 diff 才能归因到文件。", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
