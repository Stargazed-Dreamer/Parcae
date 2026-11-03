"""Ticket 03 — 遍历方向按 use_case 分策略

SDD: temp/sdd/remote-vl-model-fallback/spec.md（Done 第 3 条 / Ticket 03）

覆盖：
- vl_ocr / vl_activity_tracker 便宜优先（tier 升序）
- vl_vision 质量优先（tier 降序）——定位坐标会直接变成点击位置，错的比贵的更糟
- 同一份 keys.json 下两种方向各自可被观察到（首选项不同）
- 首选项被判永久失效后，链上接班人仍符合该 use_case 的方向
- tier 语义（成本/质量档）不被重新定义；不新增配置项也能工作
"""

import pytest
from PIL import Image

from server.llm_pool.key_store import (
    KeyRecord,
    _pick_model_for_tier,
    model_order_for_use_case,
    ordered_models_for_use_case,
    pick_model_for_use_case,
    resolve_keys,
)
from server.vl.remote_vl import RemoteVLClient, VLHTTPError

# 一条 key 上的候选模型（故意乱序写在数组里，验证排序真的发生）
VL_MODELS = [
    {"name": "vl-t4-good", "scope": ["vl"], "tier": 4, "enabled": True},
    {"name": "vl-t2-cheap", "scope": ["vl"], "tier": 2, "enabled": True},
    {"name": "vl-t5-best", "scope": ["vl"], "tier": 5, "enabled": True},
    {"name": "vl-t3-mid", "scope": ["vl"], "tier": 3, "enabled": True},
    # tier 1 落在 vl_* 的 default_tier=(2,5) 之外 ⇒ 任何方向都不该进链
    {"name": "vl-t1-out-of-range", "scope": ["vl"], "tier": 1, "enabled": True},
    {"name": "llm-only", "scope": ["llm"], "tier": 5, "enabled": True},
    {"name": "vl-t3-disabled", "scope": ["vl"], "tier": 3, "enabled": False},
]

ASC_CHAIN = ["vl-t2-cheap", "vl-t3-mid", "vl-t4-good", "vl-t5-best"]
DESC_CHAIN = ["vl-t5-best", "vl-t4-good", "vl-t3-mid", "vl-t2-cheap"]


def _make_rec(key_id="ms", label="ModelScope") -> KeyRecord:
    return KeyRecord(
        id=key_id,
        label=label,
        key=f"sk-{key_id}",
        base_url="https://api.modelscope.cn/v1",
        models=[dict(m) for m in VL_MODELS],
        privacy_warning="",
        allowed_uses=[],
        enabled=True,
        status={"works": True, "fail_count": 0, "last_health_check": "",
                "last_check_status": "", "last_check_detail": ""},
    )


@pytest.fixture
def fake_keys(monkeypatch):
    """让 resolve_keys 只看到这一条假 key（不读真实 keys.json）"""
    rec = _make_rec()
    monkeypatch.setattr(
        "server.llm_pool.key_store._load_keys_cached", lambda *a, **kw: [rec]
    )
    return rec


@pytest.fixture
def vl_client(monkeypatch, tmp_path, fake_keys):
    """隔离的 RemoteVLClient（沿用 test_vl_provider_fallback 的 fixture 口径）"""
    client = RemoteVLClient()
    client._log_dir = tmp_path
    from server.activity_tracker.vl_quota import VLQuotaManager
    test_quota = VLQuotaManager()
    test_quota.configure(quota_dir=tmp_path)
    client._vl_quota_override = test_quota
    monkeypatch.setattr(
        "server.config.get_vision_config",
        lambda: {
            "vl_enabled": True,
            "vl_max_image_edge": 1280,
            "vl_jpeg_quality": 85,
            "vl_retry_backoffs": (0.01, 0.01, 0.01),
        },
    )
    monkeypatch.setattr(
        "server.config.get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    monkeypatch.setattr("server.vl.remote_vl.time.sleep", lambda x: None)
    client.reload_config()
    return client


# ==================== 方向映射 ====================

class TestModelOrderForUseCase:
    def test_only_vl_vision_is_quality_first(self):
        """只有定位兜底 vl_vision 走降序；其余一律升序"""
        assert model_order_for_use_case("vl_vision") == "desc"
        for uc in ("vl_ocr", "vl_activity_tracker", "agent_chat", "memory_compress"):
            assert model_order_for_use_case(uc) == "asc"

    def test_unknown_or_none_falls_back_to_cheap_first(self):
        """未注册 use_case / 不传 ⇒ 升序（不新增配置项也能工作）"""
        assert model_order_for_use_case("no_such_use_case") == "asc"
        assert model_order_for_use_case(None) == "asc"
        assert model_order_for_use_case("") == "asc"


# ==================== 链顺序 ====================

class TestOrderedModelsForUseCase:
    def test_cheap_first_use_cases_ascend(self, fake_keys):
        assert ordered_models_for_use_case(fake_keys, (2, 5), "vl", "vl_ocr") == ASC_CHAIN
        assert ordered_models_for_use_case(
            fake_keys, (2, 5), "vl", "vl_activity_tracker") == ASC_CHAIN

    def test_vl_vision_descends(self, fake_keys):
        assert ordered_models_for_use_case(fake_keys, (2, 5), "vl", "vl_vision") == DESC_CHAIN

    def test_default_is_asc_for_backward_compat(self, fake_keys):
        """不传 use_case = 改造前的行为（既有的 _pick_model_for_tier 路径）"""
        assert ordered_models_for_use_case(fake_keys, (2, 5), "vl") == ASC_CHAIN

    def test_out_of_range_and_other_scope_excluded(self, fake_keys):
        """tier 语义不被重新定义：范围外 / 非 vl scope / 未启用 都不进链"""
        assert "vl-t1-out-of-range" not in ordered_models_for_use_case(
            fake_keys, (2, 5), "vl", "vl_vision")
        assert "llm-only" not in ordered_models_for_use_case(
            fake_keys, (2, 5), "vl", "vl_vision")
        assert "vl-t3-disabled" not in ordered_models_for_use_case(
            fake_keys, (2, 5), "vl", "vl_vision")

    def test_explicit_tier_range_narrows_the_chain(self, fake_keys):
        """显式 tier 范围照旧生效（(2,3) 时降序链只剩两个）"""
        assert ordered_models_for_use_case(fake_keys, (2, 3), "vl", "vl_vision") == [
            "vl-t3-mid", "vl-t2-cheap"]

    def test_excluded_head_is_followed_by_next_in_same_direction(self, fake_keys):
        """首选项被摘后，接班人仍是同方向的下一个（不是"链尾那个"）"""
        # 降序：t5 死了 → 接 t4，不是最便宜的 t2
        assert ordered_models_for_use_case(
            fake_keys, (2, 5), "vl", "vl_vision",
            exclude_models={"vl-t5-best"}) == DESC_CHAIN[1:]
        # 升序：t2 死了 → 接 t3
        assert ordered_models_for_use_case(
            fake_keys, (2, 5), "vl", "vl_ocr",
            exclude_models={"vl-t2-cheap"}) == ASC_CHAIN[1:]

    def test_all_excluded_returns_empty(self, fake_keys):
        """候选全被排除 ⇒ 空链（调用方据此跳过这条 key）"""
        assert ordered_models_for_use_case(
            fake_keys, (2, 5), "vl", "vl_vision",
            exclude_models=set(ASC_CHAIN)) == []


class TestPickModelForUseCase:
    def test_preferred_model_is_soft_and_direction_aware(self, fake_keys):
        """preferred 只在链内生效（软偏好），且不改变方向语义"""
        assert pick_model_for_use_case(
            fake_keys, (2, 5), "vl", "vl_vision", preferred_model="vl-t3-mid") == "vl-t3-mid"
        # 不在链内（范围外）的 preferred 不生效 —— 沿用既有软偏好口径
        assert pick_model_for_use_case(
            fake_keys, (2, 5), "vl", "vl_vision",
            preferred_model="vl-t1-out-of-range") == "vl-t5-best"

    def test_legacy_pick_model_for_tier_unchanged(self, fake_keys):
        """回归地板：_pick_model_for_tier 不带 use_case ⇒ 仍是升序首位"""
        assert _pick_model_for_tier(fake_keys, (2, 5), "vl") == "vl-t2-cheap"
        assert _pick_model_for_tier(
            fake_keys, (2, 5), "vl", exclude_models={"vl-t2-cheap"}) == "vl-t3-mid"
        assert _pick_model_for_tier(fake_keys, (2, 5), "vl", "vl-t4-good") == "vl-t4-good"


# ==================== 路由层接线（resolve_keys 真的把 use_case 传下去了） ====================

class TestResolveKeysDirection:
    def test_same_keys_different_head_per_use_case(self, fake_keys):
        """同一份 keys.json：vl_ocr 拿到最便宜的，vl_vision 拿到质量最好的"""
        ocr = resolve_keys("vl_ocr")
        vision = resolve_keys("vl_vision")
        assert [rk.model for rk in ocr] == ["vl-t2-cheap"]
        assert [rk.model for rk in vision] == ["vl-t5-best"]
        assert ocr[0].model != vision[0].model

    def test_exclude_models_moves_along_each_direction(self, fake_keys):
        """排除当前首位后，两个 use_case 各自往自己的方向挪一格"""
        ocr_next = resolve_keys("vl_ocr", exclude_models={"ms": {"vl-t2-cheap"}})
        vision_next = resolve_keys("vl_vision", exclude_models={"ms": {"vl-t5-best"}})
        assert ocr_next[0].model == "vl-t3-mid"
        assert vision_next[0].model == "vl-t4-good"

    def test_activity_tracker_stays_cheap_first(self, fake_keys):
        assert resolve_keys("vl_activity_tracker")[0].model == "vl-t2-cheap"


# ==================== VL 客户端端到端：降级真的按方向走 ====================

def _failover_call(client, monkeypatch, dead_models, use_case):
    """跑一次 _call_with_failover，返回实际尝试过的模型序列与结果"""
    calls = []

    def fake_call_chat(self, rk, messages, timeout=None):
        calls.append(rk.model)
        if rk.model in dead_models:
            raise VLHTTPError(400, "Bad Request",
                              '{"error":{"message":"Invalid model id"}}', rk.base_url)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(RemoteVLClient, "_call_chat", fake_call_chat)
    result = client._call_with_failover(Image.new("RGB", (8, 8)), "x", use_case=use_case)
    return calls, result


class TestVLFailoverFollowsUseCaseOrder:
    def test_vl_vision_degrades_from_best_to_second_best(self, vl_client, monkeypatch):
        """vl_vision：最高的 t5 报 400 ⇒ 接 t4（不是最便宜的 t2）"""
        calls, result = _failover_call(vl_client, monkeypatch, {"vl-t5-best"}, "vl_vision")
        assert result["status"] == "ok"
        assert calls == ["vl-t5-best", "vl-t4-good"]

    def test_vl_ocr_degrades_from_cheapest_to_next_cheapest(self, vl_client, monkeypatch):
        """vl_ocr：最便宜的 t2 报 400 ⇒ 接 t3"""
        calls, result = _failover_call(vl_client, monkeypatch, {"vl-t2-cheap"}, "vl_ocr")
        assert result["status"] == "ok"
        assert calls == ["vl-t2-cheap", "vl-t3-mid"]

    def test_pick_provider_head_differs_per_use_case(self, vl_client):
        """_pick_provider 的首选项随 use_case 变（VL 侧只传 use_case，不传模型）"""
        picked_vision = vl_client._pick_provider(use_case="vl_vision")
        picked_ocr = vl_client._pick_provider(use_case="vl_ocr")
        assert picked_vision[0].model == "vl-t5-best"
        assert picked_ocr[0].model == "vl-t2-cheap"
