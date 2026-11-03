"""VL Ticket 06：降级链上屏所需的可见性字段（wip_c3716c0b）

Ticket 03 把"遍历方向按 use_case 分策略"做进了选路，但状态口仍恒升序展示 ⇒
面板首行指 tier 低端、而 vl_vision 实际用 tier 高端（"状态口 current ≠ 真实选路"）。
本文件钉住 Ticket 06 的收口：models[] 的顺序/current 必须跟着 use_case 走，
并给出面板要显示的角色与原因（tier / enabled / dead_reason / dead_reset_in_s）。

覆盖：
- C1 不传 use_case ⇒ tier 升序（与改造前逐字一致，/health 走这条）
- C2 use_case=vl_vision ⇒ tier 降序，current 落在降序链首个未判死模型
- C3 同 key 的未启用 vl 模型以 enabled=False 追加在链尾，不进候选链
- C4 判死原因摘要被记下来并按 (key_id, model) 精确取到（跨模型不串味）
- C5 dead_reset_in_s ∈ (0, 86400]（次日 00:00 整表解除的口径）
"""

import time

import pytest

from server.llm_pool import key_store
from server.llm_pool.key_store import KeyRecord
from server.vl.remote_vl import RemoteVLClient


def _rec(key_id: str, models: list[dict]) -> KeyRecord:
    return KeyRecord(
        id=key_id,
        label=key_id,
        key=f"sk-{key_id}",
        base_url="https://api.example.cn/v1",
        models=models,
        status={"works": True},
    )


@pytest.fixture
def vl_client(monkeypatch, tmp_path):
    client = RemoteVLClient()
    client._log_dir = tmp_path
    monkeypatch.setattr(
        "server.config.get_vision_config",
        lambda: {"vl_enabled": True, "vl_max_image_edge": 1280, "vl_jpeg_quality": 85,
                 "vl_retry_backoffs": (0.01, 0.01)},
    )
    monkeypatch.setattr(
        "server.config.get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    client.reload_config()
    return client


def _fake_keys(monkeypatch, records: list[KeyRecord]) -> None:
    """_all_vl_candidates 在函数体内 `from ... import load_keys`，所以打桩模块属性即可"""
    monkeypatch.setattr(key_store, "load_keys", lambda: records)


def _chat(name: str, tier: int, enabled: bool = True) -> dict:
    return {"name": name, "scope": ["llm"], "tier": tier, "enabled": enabled}


def _vl(name: str, tier: int, enabled: bool = True) -> dict:
    return {"name": name, "scope": ["vl"], "tier": tier, "enabled": enabled}


class TestChainOrderFollowsUseCase:
    def test_default_view_stays_ascending(self, vl_client, monkeypatch):
        """C1：不传 use_case 的默认视图（/health 用）保持 tier 升序，一字不改"""
        _fake_keys(monkeypatch, [_rec("ms", [
            _vl("vl-cheap", 2), _vl("vl-mid", 3), _vl("vl-quality", 5),
        ])])

        p = vl_client.status()["providers"][0]
        assert [m["name"] for m in p["models"]] == ["vl-cheap", "vl-mid", "vl-quality"]
        assert p["model"] == "vl-cheap"

    def test_vl_vision_view_is_descending_and_current_follows(self, vl_client, monkeypatch):
        """C2：定位链质量优先——顺序与 current 都要跟着翻到 tier 高端"""
        _fake_keys(monkeypatch, [_rec("ms", [
            _vl("vl-cheap", 2), _vl("vl-mid", 3), _vl("vl-quality", 5),
        ])])

        p = vl_client.status(use_case="vl_vision")["providers"][0]
        assert [m["name"] for m in p["models"]] == ["vl-quality", "vl-mid", "vl-cheap"]
        assert [m["current"] for m in p["models"]] == [True, False, False]
        assert p["model"] == "vl-quality"

    def test_ocr_view_is_still_ascending(self, vl_client, monkeypatch):
        """同一份 keys.json，两种 use_case 的链序必须不同（否则"方向分策略"没落到状态口）"""
        _fake_keys(monkeypatch, [_rec("ms", [_vl("vl-cheap", 2), _vl("vl-quality", 5)])])

        assert [m["name"] for m in vl_client.status("vl_ocr")["providers"][0]["models"]] \
            == ["vl-cheap", "vl-quality"]
        assert [m["name"] for m in vl_client.status("vl_vision")["providers"][0]["models"]] \
            == ["vl-quality", "vl-cheap"]

    def test_dead_head_is_skipped_but_order_kept(self, vl_client, monkeypatch):
        """C2 附带：降序链首位判死 ⇒ current 顺位到下一个，链序本身不变"""
        _fake_keys(monkeypatch, [_rec("ms", [
            _vl("vl-cheap", 2), _vl("vl-mid", 3), _vl("vl-quality", 5),
        ])])
        vl_client._mark_model_dead_today("ms", "vl-quality", reason="HTTP 404: Invalid model id")

        p = vl_client.status("vl_vision")["providers"][0]
        assert [m["name"] for m in p["models"]] == ["vl-quality", "vl-mid", "vl-cheap"]
        assert [m["current"] for m in p["models"]] == [False, True, False]
        assert [m["dead_today"] for m in p["models"]] == [True, False, False]


class TestModelRoles:
    def test_disabled_vl_model_appended_and_not_in_chain(self, vl_client, monkeypatch):
        """C3：未启用的 vl 模型要看得见（角色=未启用），但绝不进候选链"""
        _fake_keys(monkeypatch, [_rec("ms", [
            _vl("vl-on", 3), _vl("vl-off", 4, enabled=False),
        ])])

        p = vl_client.status("vl_vision")["providers"][0]
        by_name = {m["name"]: m for m in p["models"]}
        assert set(by_name) == {"vl-on", "vl-off"}
        assert by_name["vl-off"]["enabled"] is False
        assert by_name["vl-off"]["current"] is False
        assert by_name["vl-off"]["dead_today"] is False
        # 候选链只认启用的那个
        assert p["model"] == "vl-on"

    def test_tier_is_exposed_for_each_model(self, vl_client, monkeypatch):
        """面板要显示 T1..T5，tier 必须逐模型给出来（不能只有 key 级）"""
        _fake_keys(monkeypatch, [_rec("ms", [_vl("a", 2), _vl("b", 5)])])

        p = vl_client.status("vl_vision")["providers"][0]
        assert [(m["name"], m["tier"]) for m in p["models"]] == [("b", 5), ("a", 2)]

    def test_dead_reason_is_per_model(self, vl_client, monkeypatch):
        """C4：原因摘要按 (key_id, model) 精确取到，别把 A 的病历贴到 B 头上"""
        _fake_keys(monkeypatch, [_rec("ms", [_vl("a", 2), _vl("b", 5)])])
        vl_client._mark_model_dead_today("ms", "b", reason="HTTP 404: Invalid model id")

        by_name = {m["name"]: m for m in vl_client.status("vl_vision")["providers"][0]["models"]}
        assert "Invalid model id" in by_name["b"]["dead_reason"]
        assert by_name["a"]["dead_reason"] == ""
        assert vl_client.model_dead_reason("ms", "a") == ""
        assert vl_client.model_dead_reason("other", "b") == ""

    def test_reason_keeps_first_non_empty(self, vl_client, monkeypatch):
        """同日重复登记幂等，且保留首个非空原因（第一次才是真病因）"""
        _fake_keys(monkeypatch, [_rec("ms", [_vl("b", 5)])])
        vl_client._mark_model_dead_today("ms", "b", reason="HTTP 404: 下架")
        vl_client._mark_model_dead_today("ms", "b", reason="HTTP 500: 后到的噪音")

        assert vl_client.model_dead_reason("ms", "b") == "HTTP 404: 下架"

    def test_reset_clears_reasons_together_with_dead_marks(self, vl_client, monkeypatch):
        """复位必须把原因一起清掉，否则面板会显示"已复位但还挂着原因"的鬼影"""
        _fake_keys(monkeypatch, [_rec("ms", [_vl("b", 5)])])
        vl_client._mark_model_dead_today("ms", "b", reason="HTTP 404: 下架")

        vl_client.reset_daily_state()

        assert vl_client._dead_models_today == {}
        assert vl_client._dead_model_reasons == {}
        assert vl_client.model_dead_reason("ms", "b") == ""


class TestDeadResetCountdown:
    def test_countdown_within_one_day(self, vl_client, monkeypatch):
        """C5：解冻倒计时是"距本地 00:00 的秒数"，口径由服务端算一次"""
        _fake_keys(monkeypatch, [_rec("ms", [_vl("a", 3)])])

        s = vl_client.status()["dead_reset_in_s"]
        assert 0 < s <= 86400
        # 与服务端时间的同一口径（不是客户端各算各的）
        lt = time.localtime()
        expected = 86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        assert abs(s - expected) <= 2
