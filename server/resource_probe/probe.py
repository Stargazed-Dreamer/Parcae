"""采样指标抽象：一个 Probe = 一个可独立开关、独立周期的指标

加新指标（磁盘 IO、GPU 温度、单进程 RSS、别的厂商的 GPU…）= 写一个 Probe 子类
+ 在 `[resource_probe.probes]` 注册一行，消费方按 `probe_id` 读缓存，不碰其它代码。

失败分两类，语义不同（这是过去"无卡机器永久 503"的根因所在）：
- `ProbeAbsent`：本机就没有这个数据源（如非 NVIDIA 卡）→ 判"无卡"，不做无意义重试
- 其它异常：瞬时故障 → 下周期重试，连续失败由消费方按降级姿态处理
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from server.resource_probe.nvml import NvmlQueryError, NvmlSession, NvmlUnavailable

# 指标 id（消费方、配置段 `[resource_probe.probes.<id>]` 都用这些字面量；
# 用下划线而非点号，TOML 子表名带点需要额外引号）
PROBE_GPU_MEM = "gpu_mem"
PROBE_GPU_UTIL = "gpu_util"
PROBE_MEM_RAM = "mem_ram"
PROBE_CPU_PERCENT = "cpu_percent"


class ProbeAbsent(RuntimeError):
    """数据源在本机不存在（非瞬时故障）"""


class ProbeTransient(RuntimeError):
    """数据源存在，本次采样失败"""


@dataclass(frozen=True)
class Sample:
    """一次采样的不可变结果"""

    probe_id: str
    value: float
    total: float | None
    ts: float
    source: str

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - self.ts

    @property
    def ratio(self) -> float | None:
        if not self.total:
            return None
        return self.value / self.total


class Probe(ABC):
    """阻塞式采样单元——`sample()` 由中枢放到线程池调用，自身不得依赖事件循环"""

    id: str
    source: str

    @abstractmethod
    def sample(self) -> Sample:
        """返回最新样本；数据源缺失抛 ProbeAbsent，瞬时失败抛任意异常"""

    # 以下两个是有意的可选钩子（子类按需覆盖），不是漏写 @abstractmethod
    def prime(self) -> None:  # noqa: B027
        """预热（差分计数器首次读值为 0 之类的需要）"""

    def reset(self) -> None:  # noqa: B027
        """会话失效后重建内部状态"""


class NvmlMemoryProbe(Probe):
    """整卡显存 used/total（字节）——与 nvidia-smi 同源，但走常驻会话"""

    id = PROBE_GPU_MEM
    source = "nvml"

    def __init__(self, session: NvmlSession) -> None:
        self._session = session

    def sample(self) -> Sample:
        try:
            self._session.open()
            card = self._session.card0()
        except NvmlUnavailable as e:
            raise ProbeAbsent(str(e)) from e
        except NvmlQueryError as e:
            raise ProbeTransient(str(e)) from e
        return Sample(
            probe_id=self.id,
            value=float(card.used_bytes),
            total=float(card.total_bytes),
            ts=time.time(),
            source=self.source,
        )

    def reset(self) -> None:
        self._session.close()


class NvmlUtilizationProbe(Probe):
    """整卡 GPU 利用率（0-100 %），供活动采集的高负载门使用"""

    id = PROBE_GPU_UTIL
    source = "nvml"

    def __init__(self, session: NvmlSession) -> None:
        self._session = session

    def sample(self) -> Sample:
        try:
            self._session.open()
            card = self._session.card0()
        except NvmlUnavailable as e:
            raise ProbeAbsent(str(e)) from e
        except NvmlQueryError as e:
            raise ProbeTransient(str(e)) from e
        if card.util_percent is None:
            raise ProbeTransient("该驱动不支持 utilization 查询")
        return Sample(
            probe_id=self.id,
            value=card.util_percent,
            total=100.0,
            ts=time.time(),
            source=self.source,
        )

    def reset(self) -> None:
        self._session.close()


class RamProbe(Probe):
    """系统物理内存 used/total（字节）"""

    id = PROBE_MEM_RAM
    source = "psutil"

    def sample(self) -> Sample:
        try:
            import psutil
        except ImportError as e:  # pragma: no cover - 依赖缺失
            raise ProbeAbsent(f"psutil 未安装: {e}") from e
        vm = psutil.virtual_memory()
        return Sample(
            probe_id=self.id,
            value=float(vm.total - vm.available),
            total=float(vm.total),
            ts=time.time(),
            source=self.source,
        )


class CpuPercentProbe(Probe):
    """CPU 总占用（0-100 %），用 psutil 差分量而非阻塞采样

    过去 `_get_system_load` 用 `cpu_percent(interval=0.1)`，每次调用固定阻塞 100ms；
    这里改成 `interval=None` 的差分读数，由中枢的 tick 天然提供时间窗口。
    """

    id = PROBE_CPU_PERCENT
    source = "psutil"

    def sample(self) -> Sample:
        try:
            import psutil
        except ImportError as e:  # pragma: no cover - 依赖缺失
            raise ProbeAbsent(f"psutil 未安装: {e}") from e
        return Sample(
            probe_id=self.id,
            value=float(psutil.cpu_percent(interval=None)),
            total=100.0,
            ts=time.time(),
            source=self.source,
        )

    def prime(self) -> None:
        try:
            import psutil

            psutil.cpu_percent(interval=None)  # 首次调用只建立基线，返回值无意义
        except ImportError:
            pass
