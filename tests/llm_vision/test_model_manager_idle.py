"""空闲回收（idle unload）单测

需求来源：常驻开关关闭（keep_models=False）后，OCR 用完 N 秒无请求必须自动卸载释放显存
——原实现里 keep_models 只回读一个展示字段，没有任何代码据此回收，导致"关了常驻仍常驻"。

覆盖：
- 阈值未配置（0）/ 常驻开启 / 未加载 / 在途推理 → 不回收
- 常驻关闭 + 空闲超阈值 → 卸载一次
- per-model 覆盖全局
- last_active_at 优先于 loaded_at（加载很久但刚用过不算空闲）
- 锁内复查：sweep 判定后、卸载前用户又打开常驻 → 放弃
- 可观测：models_report 暴露阈值与最近活跃时间
- OcrDriver.last_active_at 转发到 server.ocr.models.last_used_at
"""

import asyncio
import time

from server.model_manager.manager import ModelLifecycleManager
from server.model_manager.monitor import ResourceMonitorConfig


class FakeIdleDriver:
    """带 last_active_at 的假驱动（模型驱动协议的子集，够 _idle_sweep 用即可）"""

    def __init__(
        self,
        model_id="ocr",
        loaded=True,
        idle_for=None,
        loaded_for=100.0,
        in_flight=0,
    ):
        self.model_id = model_id
        self.resource = "cpu"
        self.footprint_mb = 600
        self.priority = 80
        self.reload_cost_sec = 27.0
        self.evictable = True
        self._loaded = loaded
        now = time.time()
        self._last_active = (now - idle_for) if idle_for is not None else None
        self._loaded_at = (now - loaded_for) if loaded else None
        self._in_flight = in_flight
        self.unload_calls = 0

    def is_loaded(self):
        return self._loaded

    def load(self):
        self._loaded = True
        self._loaded_at = time.time()
        self._last_active = time.time()

    def unload(self):
        self.unload_calls += 1
        self._loaded = False

    def in_flight(self):
        return self._in_flight

    def loaded_at(self):
        return self._loaded_at

    def last_active_at(self):
        return self._last_active

    def load_duration_ms(self):
        return 1000


def make_cfg(**over):
    cfg = {
        "enabled": True,
        "poll_interval_sec": 5.0,
        "reload_fail_cooldown_sec": 30,
        "idle_unload_sec": 0.0,
        "idle_tick_sec": 1.0,
        "gpu": ResourceMonitorConfig(resource="gpu", enabled=False),
        "cpu": ResourceMonitorConfig(resource="cpu", enabled=False),
        "gpu_min_loaded_seconds": 60,
        "cpu_min_loaded_seconds": 60,
        "gpu_unload_timeout_sec": 5,
        "cpu_unload_timeout_sec": 5,
        "models": {},
    }
    cfg.update(over)
    return cfg


def make_manager(drivers=(), cfg=None):
    # monitors={} ：空闲回收不读压力采样，只读 registry + per-model 配置
    m = ModelLifecycleManager(cfg=cfg or make_cfg(), monitors={})
    for d in drivers:
        m.registry.register(d)
    return m


def sweep(m):
    asyncio.run(m._idle_sweep())


# ==================== 不回收的场景 ====================

def test_no_threshold_configured_never_reclaims():
    d = FakeIdleDriver(idle_for=999)
    m = make_manager([d], make_cfg(idle_unload_sec=0.0,
                                   models={"ocr": {"restore_preload": False}}))
    sweep(m)
    assert d.unload_calls == 0


def test_keep_models_on_never_reclaims():
    """常驻开启（默认 restore_preload=True）是唯一门闸：阈值配了也不回收"""
    d = FakeIdleDriver(idle_for=999)
    m = make_manager([d], make_cfg(idle_unload_sec=10.0))
    sweep(m)
    assert d.unload_calls == 0


def test_not_loaded_skipped():
    d = FakeIdleDriver(loaded=False, idle_for=999)
    m = make_manager([d], make_cfg(idle_unload_sec=10.0,
                                   models={"ocr": {"restore_preload": False}}))
    sweep(m)
    assert d.unload_calls == 0


def test_in_flight_blocks_reclaim():
    d = FakeIdleDriver(idle_for=999, in_flight=1)
    m = make_manager([d], make_cfg(idle_unload_sec=10.0,
                                   models={"ocr": {"restore_preload": False}}))
    sweep(m)
    assert d.unload_calls == 0


def test_recent_use_not_reclaimed():
    d = FakeIdleDriver(idle_for=2.0)
    m = make_manager([d], make_cfg(idle_unload_sec=10.0,
                                   models={"ocr": {"restore_preload": False}}))
    sweep(m)
    assert d.unload_calls == 0


# ==================== 回收的场景 ====================

def test_keep_off_and_idle_over_threshold_unloads():
    d = FakeIdleDriver(idle_for=30.0)
    m = make_manager([d], make_cfg(idle_unload_sec=10.0,
                                   models={"ocr": {"restore_preload": False}}))
    sweep(m)
    assert d.unload_calls == 1
    assert d.is_loaded() is False


def test_last_active_preferred_over_loaded_at():
    """加载了很久（loaded_for=999s）但刚用过（idle_for=2s）→ 不算空闲，不能卸。

    若判据误用 loaded_at，这条会失败——这正是"常驻开关看着没生效"的同类错误。
    """
    d = FakeIdleDriver(loaded_for=999.0, idle_for=2.0)
    m = make_manager([d], make_cfg(idle_unload_sec=10.0,
                                   models={"ocr": {"restore_preload": False}}))
    sweep(m)
    assert d.unload_calls == 0


def test_falls_back_to_loaded_at_when_driver_has_no_last_active():
    """驱动没实现可选方法 last_active_at 时，回退 loaded_at 兜底"""

    class NoActiveDriver(FakeIdleDriver):
        last_active_at = None  # 属性存在但不是 callable → 走 getattr+callable 判定

    d = NoActiveDriver(loaded_for=999.0)
    m = make_manager([d], make_cfg(idle_unload_sec=10.0,
                                   models={"ocr": {"restore_preload": False}}))
    sweep(m)
    assert d.unload_calls == 1


def test_per_model_overrides_global():
    d = FakeIdleDriver(idle_for=30.0)
    # 全局开着、本模型显式设 0 → 不回收
    m = make_manager([d], make_cfg(idle_unload_sec=10.0,
                                   models={"ocr": {"restore_preload": False,
                                                   "idle_unload_sec": 0}}))
    sweep(m)
    assert d.unload_calls == 0


def test_per_model_threshold_applies_when_global_off():
    d = FakeIdleDriver(idle_for=30.0)
    m = make_manager([d], make_cfg(idle_unload_sec=0.0,
                                   models={"ocr": {"restore_preload": False,
                                                   "idle_unload_sec": 10}}))
    sweep(m)
    assert d.unload_calls == 1


def test_other_models_unaffected():
    """只有配了阈值的模型被考虑；同 registry 的其他模型不被动到"""
    ocr = FakeIdleDriver("ocr", idle_for=999.0)
    emb = FakeIdleDriver("memory_embedding", idle_for=999.0)
    m = make_manager([ocr, emb], make_cfg(
        idle_unload_sec=10.0,
        models={"ocr": {"restore_preload": False}},
    ))
    sweep(m)
    assert ocr.unload_calls == 1
    assert emb.unload_calls == 0


def test_recheck_gives_up_if_keep_turned_on_mid_flight():
    """sweep 已判定要卸，进入串行段后用户又打开常驻 → 放弃卸载"""
    d = FakeIdleDriver(idle_for=999.0)
    cfg = make_cfg(idle_unload_sec=10.0, models={"ocr": {"restore_preload": False}})
    m = make_manager([d], cfg)

    async def _run():
        # 模拟：判定后被并发打开常驻
        cfg["models"]["ocr"]["restore_preload"] = True
        await m._idle_reclaim(d, idle_for=999.0, idle_sec=10.0)

    asyncio.run(_run())
    assert d.unload_calls == 0


# ==================== 可观测 / 驱动契约 ====================

def test_models_report_exposes_idle_fields():
    d = FakeIdleDriver(idle_for=30.0)
    m = make_manager([d], make_cfg(idle_unload_sec=10.0,
                                   models={"ocr": {"restore_preload": False}}))
    row = m.models_report()[0]
    assert row["idle_unload_sec"] == 10.0
    assert isinstance(row["last_active_at"], float)


def test_ocr_driver_last_active_at_forwards(monkeypatch):
    from server import ocr as ocr_mod
    from server.model_manager.drivers import OcrDriver

    monkeypatch.setattr(ocr_mod.models, "_last_used_at", 123456.0, raising=False)
    assert OcrDriver().last_active_at() == 123456.0
