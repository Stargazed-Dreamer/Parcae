"""Windows Job Object：supervisor 退出时连带杀掉浏览器进程树。

`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 只保证「job 句柄关闭时」杀 job 内进程；
Chrome 可能在 assign 前已 spawn 子进程，且某些启动方式会 breakaway。因此 supervisor
仍保留 PID + 启动时间 + psutil 进程树的兜底（见 process.py），Job Object 是快路径。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Any

logger = logging.getLogger("parcae.browser.supervisor.job")

_IS_WINDOWS = sys.platform == "win32"
KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_ERROR_ACCESS_DENIED = 5


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> Any:
    if not _IS_WINDOWS:
        return None
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


class JobObject:
    """一个 kill-on-close 的 Windows Job Object；非 Windows 上全部 no-op。"""

    def __init__(self) -> None:
        self._handle: int | None = None
        self._api = _kernel32()

    @property
    def active(self) -> bool:
        return self._handle is not None

    def create(self) -> bool:
        if self._api is None:
            return False
        handle = self._api.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("CreateJobObjectW 失败: %s", ctypes.get_last_error())
            return False
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
        ok = self._api.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            logger.warning("SetInformationJobObject 失败: %s", ctypes.get_last_error())
            self._api.CloseHandle(handle)
            return False
        self._handle = int(handle)
        return True

    def assign(self, pid: int) -> bool:
        if self._api is None or self._handle is None:
            return False
        process = self._api.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid,
        )
        if not process:
            logger.warning("OpenProcess(%s) 失败: %s", pid, ctypes.get_last_error())
            return False
        try:
            ok = self._api.AssignProcessToJobObject(self._handle, process)
            if not ok:
                err = ctypes.get_last_error()
                if err != _ERROR_ACCESS_DENIED:
                    logger.warning("AssignProcessToJobObject(%s) 失败: %s", pid, err)
            return bool(ok)
        finally:
            self._api.CloseHandle(process)

    def close(self) -> None:
        if self._api is None or self._handle is None:
            return
        self._api.CloseHandle(self._handle)
        self._handle = None
