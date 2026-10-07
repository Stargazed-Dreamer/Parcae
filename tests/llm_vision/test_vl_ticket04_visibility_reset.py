"""VL Ticket 04：状态可见性（models[] / trigger_model）+ 一键复位（wip_c3716c0b）

粒度是两级的（ADR 0044）：判死按 (key_id, model)，执行锁与冷却按 key_id。
只看 key 级视图一定会读错现状——"这条 key 在冷却"和"这条 key 的模型全被下架"
是两种处置，而 daily_exhausted 此前只有前者能翻起。

覆盖：
- V1 providers[].models[] 按模型展开（name/current/dead_today），代表模型跳过已判死
- V2 模型全判死的 key 在视图里算不可用（available=False）
- V3 provider_exhausted 带 trigger_model：说清是哪个 model 把整条 key 拖停的
- V4 _all_vl_candidates 的 models[] 只含 vl scope，不拿 LLM 模型冒充 VL 候选
- R1 reset 清三处（判死名单 / key 级冷却 / 聚合标记含配额侧）并逐项回报条数
- R2 reset 不动限流冷却与 used_today（复位是"再给一次机会"，不是抹账）
- R3 POST /vision/reset 端点 200 + 字段形状
- A1 available 属性把当日判死模型排除，否则 /vision/understand 的 503 前置形同虚设
"""

import time

import pytest

from server.llm_pool.key_store import KeyRecord, ResolvedKey
from server.vl.remote_vl import RemoteVLClient


def _key(key_id: str, model: str, models: list[str] | None = None) -> ResolvedKey:
    """VL 候选的测试替身；models 默认与 _all_vl_candidates 一致（vl scope 子集，tier 升序）"""
    return ResolvedKey(
        key_id=key_id,
        api_key=f"sk-{key_id}",
        base_url="https://api.example.cn/v1",
        model=model,
        models=models if models is not None else [model],
        label=key_id,
        privacy_warning="",
        max_concurrency=1,
        source="test",
        vision_params={"max_concurrency": 1, "timeout": 60, "rate_limit_cooldown": 60},
    )


@pytest.fixture
def vl_client(monkeypatch, tmp_path):
    """隔离的 RemoteVLClient：配额走 tmp_path，绝不碰生产 vl_quota_YYYYMMDD.json"""
    client = RemoteVLClient()
    client._log_dir = tmp_path
    from server.activity_tracker.vl_quota import VLQuotaManager

    test_quota = VLQuotaManager()
    test_quota.configure(daily_quota=100, reset_hour=0, min_interval_seconds=0,
                        quota_dir=str(tmp_path))
    client._vl_quota_override = test_quota
    monkeypatch.setattr(
        "server.config.get_vision_config",
        lambda: {
            "vl_enabled": True,
            "vl_max_image_edge": 1280,
            "vl_jpeg_quality": 85,
            "vl_retry_backoffs": (0.01, 0.01),
        },
    )
    monkeypatch.setattr(
        "server.config.get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    client.reload_config()
    return client


# ==================== V1/V2 模型级视图 ====================


class TestModelsVisibility:
    def test_models_expand_with_dead_flags_and_current_skips_dead(self, vl_client):
        # tier 首（model 位）恰好是已被判死的那个 ID——展示时必须换成还能用的
        keys = [_key("ms", "qwen-vl-gone", models=["qwen-vl-gone", "qwen3-vl", "vl-late"])]
        vl_client._all_vl_candidates = lambda: keys
        vl_client._mark_model_dead_today("ms", "qwen-vl-gone")

        providers = vl_client.status()["providers"]
        assert len(providers) == 1
        p = providers[0]
        # key 级旧字段一个不少（客户端按这些渲染）
        for legacy in ("key_id", "label", "model", "base_url", "available",
                       "cooldown_remaining", "active_count", "max_concurrency",
                       "last_error", "privacy_warning"):
            assert legacy in p
        assert [m["name"] for m in p["models"]] == ["qwen-vl-gone", "qwen3-vl", "vl-late"]
        assert [m["dead_today"] for m in p["models"]] == [True, False, False]
        # current 是"这条 key 现在会被拿去调的模型"，不是"tier 最低的那个"
        assert p["model"] == "qwen3-vl"
        assert [m["current"] for m in p["models"]] == [False, True, False]

    def test_all_models_dead_makes_key_unavailable(self, vl_client):
        keys = [_key("ms", "a", models=["a", "b"])]
        vl_client._all_vl_candidates = lambda: keys
        vl_client._mark_model_dead_today("ms", "a")
        vl_client._mark_model_dead_today("ms", "b")

        p = vl_client.status()["providers"][0]
        # 没有冷却、没有并发占用，但一个模型都不剩 → 必须显示不可用
        assert p["available"] is False
        assert all(m["dead_today"] for m in p["models"])
        # 全死时 current 退回 tier 首（至少指出"本来会用谁"），不能一个都不标
        assert sum(1 for m in p["models"] if m["current"]) == 1

    def test_live_model_elsewhere_in_pool_keeps_others_available(self, vl_client):
        keys = [
            _key("ms", "a", models=["a", "b"]),
            _key("ice", "gpt-vl", models=["gpt-vl"]),
        ]
        vl_client._all_vl_candidates = lambda: keys
        vl_client._mark_model_dead_today("ms", "a")

        by_key = {p["key_id"]: p for p in vl_client.status()["providers"]}
        # 只死了一个模型：ms 仍可用，且视图看得出"还剩一个"
        assert by_key["ms"]["available"] is True
        assert [m["dead_today"] for m in by_key["ms"]["models"]] == [True, False]
        assert by_key["ice"]["available"] is True

    def test_partial_pool_does_not_flip_daily_exhausted(self, vl_client, monkeypatch):
        """负向地板：只死一部分候选时**不得**置位聚合标记（票面这条是反例）。

        ms 的两个模型全判死、ice 活着但这次 503（未达 3 次止损阈值）→ 整轮跑完没成功，
        必须**走到聚合判据那一步**再说"没全灭"。
        ⚠️ 上一版这条是恒绿的假判据：让它走 ice 成功返回，聚合代码根本没被执行过，
        于是把 `all(...)` 摘成 `any(...)` 照样全绿。所以这里故意让唯一活着的 key 失败，
        但失败原因（503 未达阈值）不构成"当日不可用"。
        """
        from PIL import Image

        from server.vl.remote_vl import VLHTTPError

        keys = [_key("ms", "a", models=["a", "b"]), _key("ice", "gpt-vl")]
        vl_client._all_vl_candidates = lambda: keys

        def fake_resolve(use_case, tier=None, exclude_models=None):
            excluded = exclude_models or {}
            return [
                rec for rec in keys
                if rec.model not in excluded.get(rec.key_id, set())
            ]

        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", fake_resolve)
        vl_client._mark_model_dead_today("ms", "a")
        vl_client._mark_model_dead_today("ms", "b")
        calls: list[str] = []

        def fake_call(rk, messages, timeout=None):
            calls.append(f"{rk.key_id}/{rk.model}")
            raise VLHTTPError(503, "Service Unavailable", "upstream busy", "u")

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(
            Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr"
        )

        # 地板：聚合判据真的被执行到了（重试全跑完、且没有任何 provider 被止损）
        assert calls == ["ice/gpt-vl", "ice/gpt-vl"]
        assert vl_client._provider_exhausted == {}
        assert result["status"] == "error"
        # 关键那条：ice 当日仍可用 ⇒ 池子没全灭 ⇒ 不能置位聚合标记
        assert "所有重试失败" in result["detail"]
        assert "daily_exhausted" not in result
        assert vl_client._daily_exhausted is False
        # 视图仍然分开：ms 不可用、ice 可用
        by_key = {p["key_id"]: p for p in vl_client.status()["providers"]}
        assert by_key["ms"]["available"] is False
        assert by_key["ice"]["available"] is True


class TestCandidatesCarryOnlyVlScope:
    def test_models_excludes_non_vl_models(self, monkeypatch, vl_client):
        """_all_vl_candidates 的 models[] 必须是 vl scope 子集。

        _to_resolved_key 给的 models[] 是该 key 全部启用模型（含 chat 模型），
        直接拿去展开会把"这条 VL key 还剩 3 个能用的"算成 5 个。
        """
        rec = KeyRecord(
            id="ms",
            label="魔搭",
            key="sk-ms",
            base_url="https://api.example.cn/v1",
            models=[
                {"name": "glm-chat", "scope": ["llm"], "tier": 2, "enabled": True},
                {"name": "qwen3-vl", "scope": ["vl", "ocr"], "tier": 1, "enabled": True},
                {"name": "vl-high", "scope": ["vl"], "tier": 4, "enabled": True},
                {"name": "vl-off", "scope": ["vl"], "tier": 1, "enabled": False},
            ],
        )
        monkeypatch.setattr("server.llm_pool.key_store.load_keys", lambda: [rec])

        candidates = vl_client._all_vl_candidates()
        assert len(candidates) == 1
        rk = candidates[0]
        # tier 升序，且只留 vl scope + enabled
        assert rk.models == ["qwen3-vl", "vl-high"]
        assert rk.model == "qwen3-vl"


# ==================== V3 触发模型可见 ====================


class TestTriggerModelVisibility:
    def test_provider_exhausted_carries_trigger_model(self, vl_client):
        vl_client._mark_provider_exhausted(
            "ms", "HTTP 429 quota", kind="quota_exhausted", source="modelscope",
            model="qwen3-vl",
        )
        pex = vl_client.status()["provider_exhausted"]["ms"]
        assert pex["trigger_model"] == "qwen3-vl"
        # 连坐范围仍是 key：其余字段不能因为"点名了 model"就变语义
        assert pex["kind"] == "quota_exhausted"
        assert pex["cooldown_remaining"] > 0


# ==================== R1/R2 一键复位 ====================


class TestResetDailyState:
    def _seed(self, vl_client):
        quota = vl_client._vl_quota_override
        quota.record_call("ok")  # used_today=1，复位不该抹掉真实消耗
        vl_client._mark_model_dead_today("ms", "gone-a")
        vl_client._mark_model_dead_today("ms", "gone-b")
        vl_client._mark_model_dead_today("ice", "gone-c")
        vl_client._mark_provider_exhausted("ice", "429 quota", model="gone-c")
        vl_client._mark_daily_exhausted("所有 VL 模型当日判死: x", kind="model_unavailable")
        # 限流冷却是另一套（秒级自愈），复位不该碰
        state = vl_client._get_runtime_state("ms")
        state.cooldown_until = time.time() + 600.0
        return quota, state

    def test_reset_clears_three_places_and_reports_counts(self, vl_client):
        quota, _state = self._seed(vl_client)

        cleared = vl_client.reset_daily_state()

        assert cleared["dead_models_cleared"] == 3  # ms 2 条 + ice 1 条
        assert cleared["dead_model_keys_cleared"] == 2
        assert cleared["provider_exhausted_cleared"] == 1
        assert cleared["daily_exhausted_cleared"] is True
        assert cleared["quota_daily_exhausted_cleared"] is True

        assert vl_client._dead_models_today == {}
        assert vl_client._dead_models_date == ""
        assert vl_client._provider_exhausted == {}
        assert vl_client._daily_exhausted is False
        assert vl_client._daily_exhausted_reason == ""
        # 配额侧真的解除了：should_call 不再 deny_exhausted
        assert quota.is_daily_exhausted() is False

    def test_reset_keeps_rate_limit_cooldown_and_consumed_quota(self, vl_client):
        quota, state = self._seed(vl_client)

        vl_client.reset_daily_state()

        assert state.cooldown_until > time.time(), "限流冷却被复位误清"
        assert quota.status()["used_today"] == 1, "复位把真实消耗一起擦了"

    def test_reset_on_clean_state_reports_zeros(self, vl_client):
        cleared = vl_client.reset_daily_state()
        assert cleared["dead_models_cleared"] == 0
        assert cleared["provider_exhausted_cleared"] == 0
        assert cleared["daily_exhausted_cleared"] is False
        assert cleared["quota_daily_exhausted_cleared"] is False


class TestQuotaClearDailyExhausted:
    def test_clear_returns_whether_it_was_set(self, tmp_path):
        from server.activity_tracker.vl_quota import VLQuotaManager

        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, reset_hour=0, min_interval_seconds=0,
                      quota_dir=str(tmp_path))
        assert mgr.clear_daily_exhausted() is False

        mgr.mark_daily_exhausted("所有 VL 模型当日判死: ms", kind="model_unavailable")
        assert mgr.is_daily_exhausted() is True
        assert mgr.clear_daily_exhausted() is True
        assert mgr.is_daily_exhausted() is False
        # kind 也一并复位，否则下轮 deny 文案仍按旧成因写
        assert mgr.status()["metrics"]["exhausted_kind"] is None
        # 幂等：再清一次回报 False
        assert mgr.clear_daily_exhausted() is False

    def test_clear_survives_reload(self, tmp_path):
        """解除必须落盘——否则重启后 _load_if_needed 又把今天的耗尽读回来"""
        from server.activity_tracker.vl_quota import VLQuotaManager

        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, reset_hour=0, min_interval_seconds=0,
                      quota_dir=str(tmp_path))
        mgr.mark_daily_exhausted("所有 VL provider 额度耗尽: ms", kind="quota_exhausted")
        mgr.clear_daily_exhausted()

        reloaded = VLQuotaManager()
        reloaded.configure(daily_quota=100, reset_hour=0, min_interval_seconds=0,
                           quota_dir=str(tmp_path))
        assert reloaded.is_daily_exhausted() is False


class TestDenyReasonFollowsKind:
    def test_model_death_does_not_read_as_429(self, tmp_path):
        """deny 文案必须跟着成因走：写成"ModelScope 返回 429"会把人引向下调配额"""
        from server.activity_tracker.vl_quota import VLQuotaManager

        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, reset_hour=0, min_interval_seconds=0,
                      quota_dir=str(tmp_path))
        mgr.mark_daily_exhausted("所有 VL 模型当日判死: ms", kind="model_unavailable")
        allow, reason, debug = mgr.should_call({"state": "active"})
        assert allow is False
        assert debug["decision"] == "deny_exhausted"
        assert "/vision/reset" in reason
        assert "429" not in reason

        mgr.clear_daily_exhausted()
        mgr.mark_daily_exhausted("所有 VL provider 额度耗尽: ms", kind="quota_exhausted")
        _allow2, reason2, _debug2 = mgr.should_call({"state": "active"})
        assert "429" in reason2


# ==================== A1 available 口径 ====================


class TestAvailableExcludesDeadModels:
    def test_available_false_when_only_model_is_dead_today(self, vl_client, monkeypatch):
        keys = [_key("ms", "qwen-vl-gone")]
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys",
                            lambda use_case, tier=None, exclude_models=None: [
                                k for k in keys
                                if k.model not in (exclude_models or {}).get(k.key_id, set())
                            ])
        assert vl_client.available is True
        vl_client._mark_model_dead_today("ms", "qwen-vl-gone")
        assert vl_client.available is False


# ==================== R3 REST 端点 ====================


class TestResetEndpoint:
    def test_post_vision_reset_returns_counts(self, client, monkeypatch):
        from server.vl import vision as vision_mod

        calls: list[bool] = []

        def fake_reset():
            calls.append(True)
            return {
                "dead_models_cleared": 2,
                "dead_model_keys_cleared": 1,
                "provider_exhausted_cleared": 1,
                "daily_exhausted_cleared": True,
                "quota_daily_exhausted_cleared": True,
                "not_in_schema": "ignored",
            }

        # 绝不打到生产单例：那会清掉今天真实的判死名单并写生产 quota 文件
        monkeypatch.setattr(vision_mod.remote_vl, "reset_daily_state", fake_reset)

        resp = client.post("/vision/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert calls == [True]
        assert data["success"] is True
        assert data["dead_models_cleared"] == 2
        assert data["provider_exhausted_cleared"] == 1
        assert data["daily_exhausted_cleared"] is True
        assert "not_in_schema" not in data

    def test_endpoint_has_stable_operation_id(self, client):
        spec = client.get("/openapi.json").json()
        matched = [
            path for path, ops in spec["paths"].items()
            if path == "/vision/reset" and "post" in ops
        ]
        assert matched == ["/vision/reset"]
        assert spec["paths"]["/vision/reset"]["post"]["operationId"] == "vision_reset"

    def test_providers_endpoint_exposes_models_and_trigger_model(self, client, monkeypatch):
        from server.vl import remote_vl as remote_vl_mod

        payload = {
            "enabled": True,
            "available": True,
            "vl_usable_for_sensitive": False,
            "allow_privacy_warning_for_sensitive": False,
            "daily_exhausted": False,
            "daily_exhausted_at": "",
            "daily_exhausted_reason": "",
            "providers": [{
                "key_id": "ms", "label": "魔搭", "model": "qwen3-vl",
                "models": [{"name": "qwen3-vl", "current": True, "dead_today": False}],
            }],
            "provider_exhausted": {"ms": {"trigger_model": "qwen3-vl"}},
        }
        monkeypatch.setattr(remote_vl_mod.remote_vl, "status", lambda: payload)
        monkeypatch.setattr(remote_vl_mod.remote_vl, "reload_config", lambda: None)

        data = client.get("/vision/providers").json()
        assert data["providers"][0]["models"][0]["name"] == "qwen3-vl"
        # provider_exhausted 一并暴露：客户端只看 /vision/providers 时也能查到触发模型
        assert data["provider_exhausted"]["ms"]["trigger_model"] == "qwen3-vl"
