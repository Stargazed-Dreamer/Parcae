"""资源采样中枢（进程内常驻，禁止按采样周期创建子进程）

对外入口：
    from server.resource_probe import get_resource_hub, PROBE_GPU_MEM
    hub = get_resource_hub()
    sample = await hub.collect(PROBE_GPU_MEM)      # 事件循环里
    sample = hub.read_cached(PROBE_GPU_MEM)        # 纯缓存，永不阻塞

历史动机：GPU 采样原本每 5s `subprocess.run(["nvidia-smi", ...])` 一次
（实测 ~45ms + 一次进程创建 ≈ 1.7 万次/天），现改为常驻 NVML 会话
（实测 init 17ms 一次性、单次查询 0.03ms）。

**适配性（务必先读）**：GPU 侧只支持 NVIDIA——`nvml.dll` / `libnvidia-ml.so.1`
由 NVIDIA 驱动提供，AMD / Intel 集显 / Apple 上探针被判为 `absent`（= 本机没有
可采的 NVIDIA 卡），压力监控随之失效并放行准入，不会退回调外部命令。
加别的 GPU = 在 `PROBE_REGISTRY` 注册一个新 Probe 实现，不改 `nvml.py`。
详见 `docs/environment-constraints.md`「资源采样中枢」。
"""

from __future__ import annotations

from collections.abc import Callable

from server.resource_probe.config import ResourceProbeConfig, get_resource_probe_config
from server.resource_probe.hub import (
    STATUS_ABSENT,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNKNOWN,
    SampleHub,
)
from server.resource_probe.nvml import NvmlSession
from server.resource_probe.probe import (
    PROBE_CPU_PERCENT,
    PROBE_GPU_MEM,
    PROBE_GPU_UTIL,
    PROBE_MEM_RAM,
    CpuPercentProbe,
    NvmlMemoryProbe,
    NvmlUtilizationProbe,
    Probe,
    RamProbe,
    Sample,
)

__all__ = [
    "SampleHub",
    "Sample",
    "Probe",
    "NvmlSession",
    "PROBE_GPU_MEM",
    "PROBE_GPU_UTIL",
    "PROBE_MEM_RAM",
    "PROBE_CPU_PERCENT",
    "STATUS_OK",
    "STATUS_FAILED",
    "STATUS_ABSENT",
    "STATUS_UNKNOWN",
    "get_resource_hub",
    "reset_resource_hub",
    "build_hub",
]

# 探针注册表：加新指标 = 在这里注册一个工厂（入参是共享的 NVML 会话，非 NVIDIA 探针忽略它）
PROBE_REGISTRY: dict[str, Callable[[NvmlSession], Probe]] = {
    PROBE_GPU_MEM: NvmlMemoryProbe,
    PROBE_GPU_UTIL: NvmlUtilizationProbe,
    PROBE_MEM_RAM: lambda _session: RamProbe(),
    PROBE_CPU_PERCENT: lambda _session: CpuPercentProbe(),
}


def build_hub(cfg: ResourceProbeConfig | None = None) -> SampleHub:
    """按配置构造中枢（仅注册启用的探针）"""
    cfg = cfg or get_resource_probe_config()
    session = NvmlSession()
    probes = {
        probe_id: factory(session)
        for probe_id, factory in PROBE_REGISTRY.items()
        if cfg.probe_enabled(probe_id)
    }
    hub = SampleHub(
        probes,
        poll_interval_sec=cfg.poll_interval_sec,
        stale_multiplier=cfg.stale_multiplier,
        absent_retry_sec=cfg.absent_retry_sec,
        enabled=cfg.enabled,
    )
    for probe in probes.values():
        probe.prime()
    return hub


_hub: SampleHub | None = None


def get_resource_hub() -> SampleHub:
    """进程内单例（懒建；首次调用读 [resource_probe] 配置）"""
    global _hub
    if _hub is None:
        _hub = build_hub()
    return _hub


def reset_resource_hub() -> None:
    """重置单例（仅测试与 shutdown 使用）"""
    global _hub
    _hub = None
