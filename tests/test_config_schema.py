"""config.toml 各段 schema 的校验行为（server/config.py `_CONFIG_SCHEMAS`）。

核心回归点：`[model_manager.models.<id>]` 这类**动态子段**必须可接受。

2026-10-19 实测：用户配了 `[model_manager.models.ocr] restore_preload = false`
（活的旋钮 —— `is_restore_preload()` 默认 True，false = OCR 被 GPU 高压逐出后
不再自动重载），`ModelManagerConfigSchema` 却因为 BaseSchema 的 `extra="forbid"`
把它判成 extra_forbidden ⇒ **每次启动一条 WARNING**。修法不是删配置，而是补上
`models: dict[str, ModelManagerModelSchema]` 这一层（与 `ResourceProbeConfigSchema.probes`
同款模式）。
"""

from __future__ import annotations

import pytest

from server import config as cfg_mod
from server.config import ModelManagerConfigSchema

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _build(**kwargs: object) -> ModelManagerConfigSchema:
    """构造 schema 实例。

    走 `model_validate` 而非直接构造：本文件**故意**要传类型错/字段错的值来验证
    校验行为，直接构造会被静态类型检查拦下（运行时其实没问题，pydantic 就是
    靠抛 ValidationError 来报的）。
    """
    return ModelManagerConfigSchema.model_validate(kwargs)


# ==================== 动态子段（per-model）必须被接受 ====================


def test_per_model_section_is_accepted():
    """模型 id 是动态的：任何 id 下的 per-model 段都不得触发 extra_forbidden。"""
    m = _build(models={"ocr": {"restore_preload": False}})
    assert m.models["ocr"].restore_preload is False
    # 未配置的字段 = None（交给读取侧 is_restore_preload / is_evictable 兜底）
    assert m.models["ocr"].evictable is None


def test_multiple_model_ids_are_free():
    """新增模型 id 不该要求同步改 schema（原设计的意图）。"""
    m = _build(
        models={
            "ocr": {"restore_preload": True},
            "memory_embedding": {"evictable": False},
            "some_future_model": {},
        }
    )
    assert set(m.models) == {"ocr", "memory_embedding", "some_future_model"}


def test_unknown_per_model_field_passes_through():
    """per-model 字段是渐进加入的（如 priority 目前只在 drivers.py 静态定义）。

    配置先行于代码时不应启动告警 —— 这是本 schema 有意放宽 extra 的原因。
    """
    m = _build(models={"ocr": {"priority": 80}})
    assert m.models["ocr"].model_dump()["priority"] == 80


def test_validate_config_quiet_for_per_model(monkeypatch):
    """端到端：含 per-model 段的配置不得产生任何校验告警（原事故现场）。"""
    monkeypatch.setattr(
        cfg_mod,
        "load_config",
        lambda: {"model_manager": {"models": {"ocr": {"restore_preload": False}}}},
    )
    assert cfg_mod.validate_config() == []


# ==================== 放宽不等于放弃校验 ====================


def test_bad_type_is_still_rejected():
    """已声明字段的类型错误必须仍能报出来（extra=allow 不该吞掉它）。"""
    with pytest.raises(Exception) as exc:
        _build(models={"ocr": {"reload_fail_cooldown_sec": -5}})
    assert "greater than 0" in str(exc.value)

    with pytest.raises(Exception) as exc:
        _build(models={"ocr": {"restore_preload": "abc"}})
    assert "bool_parsing" in str(exc.value)


def test_typo_at_top_level_is_still_rejected():
    """⛔ 放宽只作用于 per-model 子层 —— 顶层拼错的键仍要被拒（BaseSchema 的保护）。

    否则「传了但不生效」这类最难查的错误会重新变得静默。
    """
    with pytest.raises(Exception) as exc:
        _build(poll_interval_typo=5)
    assert "poll_interval_typo" in str(exc.value)


# ==================== [browser.supervisor] 继承与校验 ====================


def test_supervisor_inherits_browser_port_and_profile(monkeypatch):
    """未显式配置时，supervisor 必须和 SessionManager 看同一个端口/profile。"""
    monkeypatch.setattr(
        cfg_mod,
        "load_config",
        lambda: {"browser": {"debug_port": 9333, "user_data_dir": "D:/profiles/x"}},
    )
    data = cfg_mod.get_supervisor_config()
    assert data["port"] == 9333
    assert data["user_data_dir"] == "D:/profiles/x"


def test_supervisor_explicit_values_win(monkeypatch):
    monkeypatch.setattr(
        cfg_mod,
        "load_config",
        lambda: {
            "browser": {
                "debug_port": 9333,
                "user_data_dir": "D:/profiles/x",
                "supervisor": {"port": 0, "user_data_dir": "D:/profiles/y"},
            }
        },
    )
    data = cfg_mod.get_supervisor_config()
    assert data["port"] == 0
    assert data["user_data_dir"] == "D:/profiles/y"


def test_validate_config_accepts_supervisor_section(monkeypatch):
    monkeypatch.setattr(
        cfg_mod,
        "load_config",
        lambda: {
            "browser": {
                "debug_port": 9222,
                "supervisor": {
                    "enabled": True,
                    "port": 0,
                    "health_interval_secs": 5,
                    "max_restarts": 3,
                },
            }
        },
    )
    assert cfg_mod.validate_config() == []


def test_validate_config_rejects_bad_supervisor_port(monkeypatch):
    monkeypatch.setattr(
        cfg_mod,
        "load_config",
        lambda: {"browser": {"supervisor": {"port": 70000}}},
    )
    warnings = cfg_mod.validate_config()
    assert any("supervisor" in w for w in warnings)
