"""资源采样中枢配置（config.toml `[resource_probe]` 段）

缺段时整体回退默认值——中枢必须能在没有任何配置的情况下工作，否则无卡机器
会因为"读不到配置"而丢掉整条采样链。
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_POLL_INTERVAL_SEC = 5.0
DEFAULT_STALE_MULTIPLIER = 3.0
DEFAULT_ABSENT_RETRY_SEC = 300.0

# 默认启用的探针（厂商绑定的 GPU 探针在其中，absent 时自动失效，不影响其它）
DEFAULT_ENABLED_PROBES = ("gpu_mem", "gpu_util", "mem_ram", "cpu_percent")


@dataclass
class ProbeEntryConfig:
    """单个探针的开关与周期"""

    enabled: bool = True
    interval_sec: float | None = None  # None → 继承全局 poll_interval_sec


@dataclass
class ResourceProbeConfig:
    enabled: bool = True
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC
    stale_multiplier: float = DEFAULT_STALE_MULTIPLIER
    absent_retry_sec: float = DEFAULT_ABSENT_RETRY_SEC
    probes: dict[str, ProbeEntryConfig] = field(default_factory=dict)

    def interval_for(self, probe_id: str) -> float:
        entry = self.probes.get(probe_id)
        if entry and entry.interval_sec:
            return float(entry.interval_sec)
        return self.poll_interval_sec

    def probe_enabled(self, probe_id: str) -> bool:
        entry = self.probes.get(probe_id)
        if entry is not None:
            return entry.enabled
        return probe_id in DEFAULT_ENABLED_PROBES


def get_resource_probe_config() -> ResourceProbeConfig:
    """读取 `[resource_probe]` 段（全部默认值补齐）"""
    from lib.config_reader import load_config

    section = load_config().get("resource_probe", {})
    if not isinstance(section, dict):
        section = {}

    probes_raw = section.get("probes", {})
    probes: dict[str, ProbeEntryConfig] = {}
    if isinstance(probes_raw, dict):
        for probe_id, entry in probes_raw.items():
            if not isinstance(entry, dict):
                continue
            interval = entry.get("interval_sec")
            probes[str(probe_id)] = ProbeEntryConfig(
                enabled=bool(entry.get("enabled", True)),
                interval_sec=float(interval) if interval is not None else None,
            )

    return ResourceProbeConfig(
        enabled=bool(section.get("enabled", True)),
        poll_interval_sec=float(section.get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC)),
        stale_multiplier=float(section.get("stale_multiplier", DEFAULT_STALE_MULTIPLIER)),
        absent_retry_sec=float(section.get("absent_retry_sec", DEFAULT_ABSENT_RETRY_SEC)),
        probes=probes,
    )
