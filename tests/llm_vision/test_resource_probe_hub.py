"""常驻资源采样中枢（server/resource_probe/）单测

全部用假探针，不碰真 GPU、不装 psutil 依赖之外的东西。

覆盖：
- 缓存读与新鲜度（stale_multiplier）
- 游标语义：consume 只在有新样本时推进（消费方不把"还没出值"误判成采样失败）
- 失败分档：transient 计入连击并可恢复；absent 判"本机无此数据源"且慢速重探
- NVML 探针把 NvmlUnavailable 翻译成 ProbeAbsent（非 NVIDIA 机器的正确语义）
- 消费方接缝：PressureMonitor 默认走中枢且**不创建子进程**
- 无卡回归：probe_absent 时准入放行，不再卡在 awaiting_first_sample
- 活动采集高负载门 _get_system_load 读中枢
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from typing import Literal

import pytest

from server.model_manager.manager import ModelLifecycleManager
from server.model_manager.monitor import (
    FAIL_ABSENT,
    FAIL_NO_NEW,
    PressureMonitor,
    ResourceMonitorConfig,
)
from server.model_manager.registry import ModelRegistry
from server.model_manager.types import PressureState
from server.resource_probe import (
    PROBE_CPU_PERCENT,
    PROBE_GPU_MEM,
    PROBE_GPU_UTIL,
)
from server.resource_probe.hub import (
    STATUS_ABSENT,
    STATUS_FAILED,
    STATUS_OK,
    SampleHub,
)
from server.resource_probe.nvml import NvmlUnavailable
from server.resource_probe.probe import (
    NvmlMemoryProbe,
    Probe,
    ProbeAbsent,
    Sample,
)

# ==================== 测试台子 ====================


class FakeProbe(Probe):
    """按脚本产出样本；`clock` 可注入以便测试陈旧度"""

    id = "fake"
    source = "fake"

    def __init__(self, values=(1.0, 2.0, 3.0), total=10.0, clock=time.time, id=None, source=None):
        if id is not None:
            self.id = id
        if source is not None:
            self.source = source
        self.values = list(values)
        self.total = total
        self.clock = clock
        self.i = 0
        self.calls = 0

    def sample(self) -> Sample:
        self.calls += 1
        value = self.values[self.i] if self.i < len(self.values) else self.values[-1]
        self.i += 1
        return Sample(
            probe_id=self.id,
            value=value,
            total=self.total,
            ts=self.clock(),
            source=self.source,
        )


class AbsentProbe(FakeProbe):
    def sample(self) -> Sample:
        self.calls += 1
        raise ProbeAbsent("no NVIDIA device")


class FlakyProbe(FakeProbe):
    def sample(self) -> Sample:
        self.calls += 1
        raise RuntimeError("driver busy")


class ToggleProbe(FakeProbe):
    """可在测试中途翻面的探针：failing=True 时算瞬时失败，否则正常出值

    用来自证"失败 → 恢复"两条路径都真的被走过（calls 会跟着涨）。
    """

    def __init__(self, *args, failing: bool = True, **kw):
        super().__init__(*args, **kw)
        self.failing = failing

    def sample(self) -> Sample:
        if self.failing:
            self.calls += 1
            raise RuntimeError("driver busy")
        return super().sample()  # FakeProbe.sample 自己会计数


def make_hub(probe: Probe, **kw) -> SampleHub:
    params = {probe.id: probe}
    kw.setdefault("poll_interval_sec", 5.0)
    kw.setdefault("stale_multiplier", 3.0)
    kw.setdefault("absent_retry_sec", 300.0)
    return SampleHub(params, **kw)


def run(coro):
    return asyncio.run(coro)


class FakeDriver:
    resource: Literal["gpu", "cpu"]

    def __init__(self, model_id: str = "ocr", resource: Literal["gpu", "cpu"] = "gpu"):
        self.model_id = model_id
        self.resource = resource
        self.footprint_mb = 500
        self.priority = 50
        self.reload_cost_sec = 2.0
        self.evictable = True

    def is_loaded(self):
        return False

    def load(self):
        pass

    def unload(self):
        pass

    def in_flight(self):
        return 0

    def loaded_at(self):
        return None

    def load_duration_ms(self):
        return 0


def gpu_cfg(**kw) -> ResourceMonitorConfig:
    params = {"resource": "gpu", "enabled": True, "poll_interval_sec": 0.01}
    params.update(kw)
    return ResourceMonitorConfig(**params)


def mm_cfg() -> dict:
    return {
        "enabled": True,
        "poll_interval_sec": 0.01,
        "reload_fail_cooldown_sec": 30,
        "gpu": gpu_cfg(),
        "cpu": ResourceMonitorConfig(resource="cpu", enabled=False),
        "gpu_min_loaded_seconds": 60,
        "cpu_min_loaded_seconds": 60,
        "gpu_unload_timeout_sec": 5,
        "cpu_unload_timeout_sec": 5,
        "models": {},
    }


# ==================== 中枢：缓存 / 游标 ====================


def test_read_cached_returns_fresh_sample():
    hub = make_hub(FakeProbe(id=PROBE_GPU_MEM, values=[42.0]))
    run(hub.prime())
    sample = hub.read_cached(PROBE_GPU_MEM)
    assert sample is not None and sample.value == 42.0
    assert hub.status(PROBE_GPU_MEM) == STATUS_OK


def test_read_cached_drops_stale_sample():
    clock = {"now": time.time()}
    probe = FakeProbe(id=PROBE_GPU_MEM, clock=lambda: clock["now"])
    hub = make_hub(probe, stale_multiplier=3.0, clock=lambda: clock["now"])  # 新鲜窗口 = 15s
    run(hub.prime())
    assert hub.read_cached(PROBE_GPU_MEM) is not None

    clock["now"] += 14.0
    assert hub.read_cached(PROBE_GPU_MEM) is not None  # 仍在窗口内
    clock["now"] += 2.0
    assert hub.read_cached(PROBE_GPU_MEM) is None      # 超过 3×interval
    assert hub.latest(PROBE_GPU_MEM) is not None       # 诊断读仍拿得到


def test_consume_advances_only_on_new_sample():
    probe = FakeProbe(id=PROBE_GPU_MEM, values=[1.0, 2.0])
    hub = make_hub(probe)
    run(hub.prime())
    sample, cursor = hub.consume(PROBE_GPU_MEM, 0)
    assert sample is not None and sample.value == 1.0 and cursor == 1

    again, same_cursor = hub.consume(PROBE_GPU_MEM, cursor)
    assert again is None and same_cursor == cursor   # 没有新样本，游标不动

    run(hub.prime())
    newer, new_cursor = hub.consume(PROBE_GPU_MEM, same_cursor)
    assert newer is not None and newer.value == 2.0 and new_cursor == 2


# ==================== 中枢：失败分档 ====================


def test_transient_failure_marks_failed_and_recovers():
    probe = ToggleProbe(id=PROBE_GPU_MEM)
    hub = make_hub(probe)
    run(hub.prime())                     # 第 1 次：瞬时失败
    assert hub.status(PROBE_GPU_MEM) == STATUS_FAILED
    assert hub.fail_streak(PROBE_GPU_MEM) == 1
    assert hub.is_absent(PROBE_GPU_MEM) is False

    probe.failing = False                # 数据源恢复
    run(hub.prime())
    assert hub.status(PROBE_GPU_MEM) == STATUS_OK
    assert hub.fail_streak(PROBE_GPU_MEM) == 0
    assert probe.calls == 2              # 自证：确实又采了一次


def test_absent_probe_is_not_retried_every_cycle():
    clock = {"now": time.time()}
    probe = AbsentProbe(id=PROBE_GPU_MEM, clock=lambda: clock["now"])
    hub = make_hub(probe, absent_retry_sec=300.0, clock=lambda: clock["now"])

    run(hub.prime())                      # force：首轮必探
    assert hub.status(PROBE_GPU_MEM) == STATUS_ABSENT
    assert hub.is_absent(PROBE_GPU_MEM) is True
    assert probe.calls == 1

    for _ in range(3):                    # 后续常规采样不再空转
        clock["now"] += 5.0
        hub._collect(PROBE_GPU_MEM)
    assert probe.calls == 1

    clock["now"] += 300.0                 # 慢速重探窗口到了才再试
    hub._collect(PROBE_GPU_MEM)
    assert probe.calls == 2


def test_absent_transition_recorded_for_watchdog():
    hub = make_hub(AbsentProbe(id=PROBE_GPU_MEM))
    run(hub.prime())
    event = hub.events[-1]
    assert event["probe"] == PROBE_GPU_MEM and event["to"] == STATUS_ABSENT
    assert hub.health()["probes"][PROBE_GPU_MEM]["status"] == STATUS_ABSENT


# ==================== NVML 语义 ====================


class _DeadSession:
    ready = False

    def open(self):
        raise NvmlUnavailable("nvml.dll not found")

    def close(self):
        pass


def test_nvml_probe_translates_unavailable_to_absent():
    with pytest.raises(ProbeAbsent):
        NvmlMemoryProbe(_DeadSession()).sample()  # type: ignore[arg-type]


# ==================== 消费方：PressureMonitor ====================


def test_monitor_default_source_never_spawns_process(monkeypatch):
    """默认数据源必须走中枢：把 subprocess.run 打成炸弹，采样链路照样出值"""

    def _boom(*a, **kw):
        raise AssertionError("采样链路不得创建子进程")

    monkeypatch.setattr(subprocess, "run", _boom)
    probe = FakeProbe(id=PROBE_GPU_MEM, values=[2.0 * 1024**3], total=8.0 * 1024**3)
    hub = make_hub(probe)

    async def _scenario():
        await hub.ensure_started()
        try:
            monitor = PressureMonitor(gpu_cfg(), hub=hub)
            return await monitor.sample(), monitor
        finally:
            await hub.stop()

    values, monitor = run(_scenario())
    assert values == (int(2.0 * 1024**3), int(8.0 * 1024**3))
    assert monitor.last_failure is None
    assert monitor.probe_absent is False
    assert monitor.samples_taken == 1


def test_monitor_absent_keeps_normal_posture():
    hub = make_hub(AbsentProbe(id=PROBE_GPU_MEM))
    run(hub.prime())
    monitor = PressureMonitor(gpu_cfg(), hub=hub)

    assert run(monitor.sample()) is None
    assert monitor.probe_absent is True
    assert monitor.last_failure == FAIL_ABSENT
    assert monitor.state is PressureState.NORMAL          # 不是 PROBE_DEGRADED
    assert monitor.admission_allowed() is True


def test_monitor_no_new_sample_is_not_counted_as_failure():
    probe = FakeProbe(id=PROBE_GPU_MEM, values=[1.0], total=10.0)
    hub = make_hub(probe)
    monitor = PressureMonitor(gpu_cfg(), hub=hub)

    async def _scenario():
        await hub.prime()          # 一个样本
        first = await monitor.sample()
        rest = [await monitor.sample() for _ in range(4)]   # 中枢没出新值
        await hub.stop()
        return first, rest

    first, rest = run(_scenario())
    assert first is not None
    assert all(v is None for v in rest)
    assert all(monitor.last_failure == FAIL_NO_NEW for _ in rest)
    assert monitor.state is PressureState.NORMAL   # 4 次"没新值"不触发 PROBE_DEGRADED
    assert hub.fail_streak(PROBE_GPU_MEM) == 0


# ==================== 消费方：准入（无卡回归） ====================


def make_manager(hub: SampleHub) -> ModelLifecycleManager:
    cfg = mm_cfg()
    monitors = {
        "gpu": PressureMonitor(cfg["gpu"], hub=hub),
        "cpu": PressureMonitor(cfg["cpu"], hub=hub),
    }
    manager = ModelLifecycleManager(cfg=cfg, monitors=monitors)
    manager.registry = ModelRegistry()
    manager.registry.register(FakeDriver("ocr", resource="gpu"))
    return manager


def test_admission_allowed_when_no_card_probe_absent():
    """非 NVIDIA 机器：探针 absent → 放行，不再永久卡 awaiting_first_sample"""
    hub = make_hub(AbsentProbe(id=PROBE_GPU_MEM))
    manager = make_manager(hub)
    monitor = manager.monitors["gpu"]

    run(manager._refresh(monitor))                 # 走一次真实采样
    assert monitor.probe_absent is True            # 自证：确实进了 absent 姿态
    assert monitor.has_first_sample is False       # 零样本
    assert monitor.state is PressureState.NORMAL

    allowed, reason = manager.check_admission("ocr")
    assert allowed is True and reason == "probe_absent_no_card"
    assert manager.health_summary()["probe_absent_resources"] == ["gpu"]
    run(hub.stop())


def test_dead_hub_does_not_lock_admission():
    """中枢起不来（[resource_probe] enabled=false / lifecycle 启动失败）→ 判 absent，
    绝不能回到旧的"永久采不到首样本 → 准入永久 503"
    """
    hub = SampleHub({PROBE_GPU_MEM: FakeProbe(id=PROBE_GPU_MEM)}, enabled=False)
    manager = make_manager(hub)
    monitor = manager.monitors["gpu"]

    run(manager._refresh(monitor))
    assert monitor.probe_absent is True
    allowed, reason = manager.check_admission("ocr")
    assert allowed is True and reason == "probe_absent_no_card"


def test_transient_failure_still_gates_until_first_sample():
    """有卡但暂时采不到：保持原"首样本前拒绝"语义（不放行成无压力）"""
    hub = make_hub(FlakyProbe(id=PROBE_GPU_MEM))
    run(hub.prime())
    manager = make_manager(hub)

    allowed, reason = manager.check_admission("ocr")
    assert allowed is False and reason == "awaiting_first_sample"


def test_manager_refresh_counts_only_transient_failures():
    probe = FakeProbe(id=PROBE_GPU_MEM, values=[7.0], total=10.0)
    hub = make_hub(probe)
    manager = make_manager(hub)
    monitor = manager.monitors["gpu"]

    async def _scenario():
        await hub.prime()
        await manager._refresh(monitor)        # 用掉首样本
        for _ in range(5):
            await manager._refresh(monitor)    # 全是 no_new_sample
        await hub.stop()

    run(_scenario())
    assert monitor.state is not PressureState.PROBE_DEGRADED
    assert monitor.probe_absent is False

    # 换成瞬时失败：连击应推进到 PROBE_DEGRADED（原有保护语义未削弱）
    flaky_hub = make_hub(FlakyProbe(id=PROBE_GPU_MEM))
    flaky_manager = make_manager(flaky_hub)
    flaky_monitor = flaky_manager.monitors["gpu"]

    async def _flaky():
        for _ in range(3):
            await flaky_manager._refresh(flaky_monitor)
        await flaky_hub.stop()

    run(_flaky())
    assert flaky_monitor.state is PressureState.PROBE_DEGRADED


# ==================== 消费方：活动采集的高负载门 ====================


def test_get_system_load_reads_hub_and_spawns_nothing(monkeypatch):
    from server.activity_tracker import loop_actions

    def _boom(*a, **kw):
        raise AssertionError("高负载门不得创建子进程")

    cpu = FakeProbe(id=PROBE_CPU_PERCENT, values=[12.5], total=100.0)
    util = FakeProbe(id=PROBE_GPU_UTIL, values=[33.0], total=100.0)
    hub = SampleHub({cpu.id: cpu, util.id: util})
    monkeypatch.setattr("server.resource_probe.get_resource_hub", lambda: hub)
    monkeypatch.setattr(subprocess, "run", _boom)

    assert loop_actions._get_system_load() == (12.5, 33.0)
    assert cpu.calls == 1 and util.calls == 1


def test_get_system_load_gpu_none_when_no_card(monkeypatch):
    """非 NVIDIA 机器：GPU 侧返回 None，让高负载门只看 CPU（与旧实现同向）"""
    from server.activity_tracker import loop_actions

    cpu = FakeProbe(id=PROBE_CPU_PERCENT, values=[5.0], total=100.0)
    util = AbsentProbe(id=PROBE_GPU_UTIL)
    hub = SampleHub({cpu.id: cpu, util.id: util})
    monkeypatch.setattr("server.resource_probe.get_resource_hub", lambda: hub)

    assert loop_actions._get_system_load() == (5.0, None)
    assert hub.is_absent(PROBE_GPU_UTIL) is True


def test_config_schema_accepts_example_resource_probe_section():
    """config.example.toml 的 [resource_probe] 段必须能被 schema 收下（防示例漂移）"""
    import tomllib
    from pathlib import Path

    from server.config import _CONFIG_SCHEMAS

    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    parsed = tomllib.loads(example.read_text(encoding="utf-8"))
    section = parsed.get("resource_probe")
    assert section, "config.example.toml 缺 [resource_probe] 段"
    _CONFIG_SCHEMAS["resource_probe"](**section)  # extra='forbid'：字段名写错会炸
