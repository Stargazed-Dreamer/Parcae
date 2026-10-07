"""常驻 NVML 会话（ctypes 直调，禁止子进程）

一次 `nvmlInit` + 句柄缓存，之后每次查询是进程内纯函数调用（实测 ~0.03ms），
取代过去每采样周期 fork 一个 nvidia-smi（实测 ~45ms + 一次进程创建）。

**厂商绑定（必须知道的适配性）**：`nvml.dll` / `libnvidia-ml.so.1` 由 **NVIDIA 驱动**
提供。AMD、Intel 集显、Apple 等其它 GPU 上这里必然失败，探针被判为 `absent`
（= 本机没有 NVIDIA 卡可采），中枢不做厂商猜测式降级，也不会退回调外部命令。
要支持别的 GPU，正确做法是新增一个 `Probe` 实现并在 `[resource_probe.probes]` 注册，
而不是改这里。详见 `docs/environment-constraints.md`「资源采样中枢」。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
from dataclasses import dataclass

_LIB_CANDIDATES = ("nvml.dll", "libnvidia-ml.so.1", "libnvidia-ml.so")
_NVML_SUCCESS = 0
_VERSION_BUF = 80


class NvmlUnavailable(RuntimeError):
    """NVML 库加载或初始化失败——按"本机没有 NVIDIA 卡"处理（absent，非瞬时故障）"""


class NvmlQueryError(RuntimeError):
    """会话已就绪但单次查询返回错误码——按瞬时故障处理（failed，下周期重试）"""


@dataclass(frozen=True)
class NvmlCardInfo:
    used_bytes: int
    total_bytes: int
    util_percent: float | None


class _MemoryUsage(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _Utilization(ctypes.Structure):
    _fields_ = [
        ("gpu", ctypes.c_uint),
        ("memory", ctypes.c_uint),
    ]


class NvmlSession:
    """进程内持有的 NVML 会话（懒初始化、线程安全、句柄缓存）

    NVML 本身线程安全，本类只额外保护"首次初始化"这段。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lib: ctypes.CDLL | None = None
        self._handle: ctypes.c_void_p | None = None
        self._driver_version: str | None = None

    @property
    def ready(self) -> bool:
        return self._handle is not None

    def open(self) -> None:
        """加载库并初始化会话；失败抛 NvmlUnavailable"""
        with self._lock:
            if self._handle is not None:
                return
            lib = self._load_library()
            self._call(lib.nvmlInit_v2(), "nvmlInit_v2", absent=True)

            count = ctypes.c_uint(0)
            self._call(lib.nvmlDeviceGetCount_v2(ctypes.byref(count)), "nvmlDeviceGetCount_v2", absent=True)
            if count.value < 1:
                raise NvmlUnavailable("NVML 报告 0 块 NVIDIA 设备")

            handle = ctypes.c_void_p()
            self._call(
                lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)),
                "nvmlDeviceGetHandleByIndex_v2",
                absent=True,
            )
            self._lib = lib
            self._handle = handle
            self._driver_version = self._read_driver_version(lib)

    def close(self) -> None:
        with self._lock:
            if self._lib is not None and self._handle is not None:
                try:
                    self._lib.nvmlShutdown()
                except OSError:
                    pass
            self._lib = None
            self._handle = None
            self._driver_version = None

    def card0(self) -> NvmlCardInfo:
        """整卡显存 + GPU 利用率（index 0）"""
        lib, handle = self._lib, self._handle
        if lib is None or handle is None:
            raise NvmlUnavailable("NVML 会话未初始化")

        mem = _MemoryUsage()
        self._call(lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)), "nvmlDeviceGetMemoryInfo")

        util = _Utilization()
        util_percent: float | None = None
        try:
            self._call(
                lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)),
                "nvmlDeviceGetUtilizationRates",
            )
            util_percent = float(util.gpu)
        except NvmlQueryError:
            util_percent = None  # 少数驱动不支持该查询；显存样本仍有效

        return NvmlCardInfo(
            used_bytes=int(mem.used),
            total_bytes=int(mem.total),
            util_percent=util_percent,
        )

    @property
    def driver_version(self) -> str | None:
        return self._driver_version

    # ---------- 内部 ----------

    def _load_library(self) -> ctypes.CDLL:
        found = ctypes.util.find_library("nvml")
        names = [found] if found else []
        names.extend(_LIB_CANDIDATES)
        last_error: Exception | None = None
        for name in names:
            try:
                return ctypes.CDLL(name)
            except OSError as e:
                last_error = e
        raise NvmlUnavailable(f"未找到 NVML 库（试过 {', '.join(n for n in names if n)}）: {last_error}")

    @staticmethod
    def _read_driver_version(lib: ctypes.CDLL) -> str | None:
        buf = ctypes.create_string_buffer(_VERSION_BUF)
        try:
            if lib.nvmlSystemGetDriverVersion(buf, ctypes.c_uint(_VERSION_BUF)) == _NVML_SUCCESS:
                return buf.value.decode(errors="replace")
        except OSError:
            return None
        return None

    @staticmethod
    def _call(rc: int, api: str, *, absent: bool = False) -> None:
        if rc == _NVML_SUCCESS:
            return
        if absent:
            raise NvmlUnavailable(f"{api} 返回 {rc}")
        raise NvmlQueryError(f"{api} 返回 {rc}")
