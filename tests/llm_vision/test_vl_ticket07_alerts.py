"""VL Ticket 07：降级发生 / 全候选判死 的可见告警（wip_c3716c0b）

本次事故的病灶不是"没有降级链"，而是"坏了四天、五千次调用、零人知晓"。
本文件钉住告警最小版：两件事发生时人**被动就知道**，且不会把收件箱刷屏。

覆盖：
- A1 一次调用内发生过模型降级 ⇒ 落一条含"被跳过的模型 + 原因摘要"的记录
- A2 同一天同一 (key, model) 只发一次（去重/限频）；不同 pair 各自独立
- A3 全候选判死 ⇒ 更高优先级告警，且与 daily_exhausted 同一触发点（不得两套真相）
- A4 复位后能再报（复位 = 再给一次机会）；跨日换血后也能再报
- A5 端到端：真跑 _call_with_failover 撞 400，降级记录确实进了告警通路
- A6 投递出口本身出错不得阻断选路（告警是旁路）
"""

import json

import pytest
from PIL import Image

from server.llm_pool.key_store import ResolvedKey
from server.vl.remote_vl import RemoteVLClient, VLHTTPError


def _key(key_id: str, model: str, models: list[str] | None = None) -> ResolvedKey:
    return ResolvedKey(
        key_id=key_id,
        api_key=f"sk-{key_id}",
        base_url="https://api.example.cn/v1",
        model=model,
        models=models if models is not None else [model],
        label=f"{key_id}-label",
        privacy_warning="",
        max_concurrency=1,
        source="test",
        vision_params={"max_concurrency": 1, "timeout": 60, "rate_limit_cooldown": 60},
    )


@pytest.fixture
def vl_client(monkeypatch, tmp_path):
    """隔离实例 + 捕获式收件箱出口（绝不碰生产 data/... inbox 库）"""
    client = RemoteVLClient()
    client._log_dir = tmp_path
    from server.activity_tracker.vl_quota import VLQuotaManager

    test_quota = VLQuotaManager()
    test_quota.configure(daily_quota=100, reset_hour=0, min_interval_seconds=0,
                         quota_dir=str(tmp_path))
    client._vl_quota_override = test_quota
    monkeypatch.setattr(
        "server.config.get_vision_config",
        lambda: {"vl_enabled": True, "vl_max_image_edge": 1280, "vl_jpeg_quality": 85,
                 "vl_retry_backoffs": (0.01, 0.01)},
    )
    monkeypatch.setattr(
        "server.config.get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    monkeypatch.setattr("server.vl.remote_vl.time.sleep", lambda x: None)
    client.reload_config()

    delivered: list[dict] = []
    client._inbox_sink = delivered.append
    client._test_delivered = delivered  # type: ignore[attr-defined]
    return client


def _patch_resolver(monkeypatch, keys):
    """按 exclude_models 过滤的假 resolve_keys——模拟 key_store 的真行为。

    ⛔ 不能写成 `lambda uc, **kw: keys`：那样被排除的 key 还会被再选中，
    failover 会原地打转，端到端断言就变成在测一个不存在的降级。
    """
    from server.llm_pool import key_store

    def fake_resolve(use_case, tier=None, exclude_models=None):
        excluded = exclude_models or {}
        out = []
        for rec in keys:
            skip = excluded.get(rec.key_id, set())
            if rec.model in skip:
                continue
            out.append(rec)
        return out

    monkeypatch.setattr(key_store, "resolve_keys", fake_resolve)


def _titles(items: list[dict]) -> list[str]:
    return [i["title"] for i in items]


class TestDegradedAlert:
    def test_permanent_error_delivers_one_record_with_reason(self, vl_client):
        """A1：降级记录必须能回答"哪个模型、为什么、还能不能好" """
        vl_client._alert_model_degraded(
            "ms", "qwen-vl-gone", "HTTP 404: Invalid model id", label="魔搭",
        )

        items = vl_client._test_delivered
        assert len(items) == 1
        it = items[0]
        assert it["category"] == "vl_model_degraded"
        assert it["source"] == "vl"
        assert "qwen-vl-gone" in it["title"] and "魔搭" in it["title"]
        assert "Invalid model id" in it["description"]
        assert it["payload"]["model"] == "qwen-vl-gone"
        assert it["payload"]["key_id"] == "ms"
        # dedupe_key 是收件箱侧的结构性兜底（进程重启也拦得住）
        assert it["dedupe_key"].startswith("vl_model_degraded:")
        assert "ms" in it["dedupe_key"] and "qwen-vl-gone" in it["dedupe_key"]

    def test_same_pair_alerted_once_per_day(self, vl_client):
        """A2：坏模型会被 loop 反复撞到，一天只吵一次"""
        for _ in range(5):
            vl_client._alert_model_degraded("ms", "dead", "HTTP 400: x")

        assert len(vl_client._test_delivered) == 1

    def test_different_pairs_alarm_independently(self, vl_client):
        """A2 附带：别的模型/别的 key 不能被同一条去重顶掉"""
        vl_client._alert_model_degraded("ms", "dead-a", "HTTP 400: a")
        vl_client._alert_model_degraded("ms", "dead-b", "HTTP 400: b")
        vl_client._alert_model_degraded("ice", "dead-a", "HTTP 400: a")

        titles = _titles(vl_client._test_delivered)
        assert len(titles) == 3
        assert len(set(titles)) == 3

    def test_new_day_re_alerts(self, vl_client):
        """A4：跨日换血后同一对可以再报一次（昨天的去重不该压住今天）"""
        vl_client._alert_model_degraded("ms", "dead", "HTTP 400: x")
        vl_client._degraded_today_date = "19990101"   # 假装表是昨天的

        vl_client._alert_model_degraded("ms", "dead", "HTTP 400: x")

        assert len(vl_client._test_delivered) == 2

    def test_reset_lets_it_alert_again(self, vl_client):
        """A4：复位 = 再给一次机会 ⇒ 同一对再坏要能再报"""
        vl_client._alert_model_degraded("ms", "dead", "HTTP 400: x")
        vl_client.reset_daily_state()
        vl_client._alert_model_degraded("ms", "dead", "HTTP 400: x")

        assert len(vl_client._test_delivered) == 2


class TestAllCandidatesDeadAlert:
    def test_all_dead_alert_is_tied_to_daily_exhausted(self, vl_client):
        """A3：告警与聚合标记同一个触发点，不得出现两套真相"""
        assert vl_client._daily_exhausted is False

        vl_client._mark_model_dead_today("ms", "a")
        vl_client._mark_model_dead_today("ms", "b")
        vl_client._mark_daily_exhausted("所有 VL 模型当日判死: ms", kind="model_unavailable")

        assert vl_client._daily_exhausted is True
        items = vl_client._test_delivered
        assert len(items) == 1
        it = items[0]
        assert it["category"] == "vl_all_models_dead"
        assert "全线不可用" in it["title"]
        assert it["payload"]["dead_pairs"] == 2
        assert it["dedupe_key"].startswith("vl_all_models_dead:")

    def test_all_dead_alert_not_repeated_same_day(self, vl_client):
        """A3 附带：聚合标记会被多次设置，但告警一天一条"""
        for _ in range(3):
            vl_client._mark_daily_exhausted("全判死", kind="model_unavailable")
        assert len(vl_client._test_delivered) == 1

        # 复位后能再报
        vl_client.reset_daily_state()
        vl_client._mark_daily_exhausted("全判死", kind="model_unavailable")
        assert len(vl_client._test_delivered) == 2

    def test_other_kinds_do_not_fire_all_dead_alert(self, vl_client):
        """额度/过载耗尽不是"模型全判死"，别冒充成四级事故"""
        vl_client._mark_daily_exhausted("quota exceeded", kind="quota_exhausted")
        vl_client._mark_daily_exhausted("rate limited", kind="upstream_overload")

        assert vl_client._test_delivered == []


class TestEndToEnd:
    def test_real_failover_400_lands_in_alert_channel(self, vl_client, monkeypatch):
        """A5：不接受"应该会发"——真跑一次 failover，记录必须出现在告警通路里"""
        keys = [_key("ms", "qwen-vl-dead"), _key("ms", "qwen-vl-good")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys
        calls: list[str] = []

        def fake_call(rk, messages, timeout=None):
            calls.append(rk.model)
            if rk.model == "qwen-vl-dead":
                raise VLHTTPError(400, "Bad Request",
                                  '{"error":{"message":"model does not exist"}}', "u")
            return {"choices": [{"message": {"content": "good answer"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(
            Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        # 地板：先确认这次调用真的降级过（否则"告警出现"什么也证明不了）
        assert result["status"] == "ok"
        assert calls == ["qwen-vl-dead", "qwen-vl-good"]

        items = vl_client._test_delivered
        assert len(items) == 1, "降级确实发生过，告警通路里就该有记录"
        assert items[0]["payload"]["model"] == "qwen-vl-dead"
        assert "does not exist" in items[0]["description"]

    def test_all_models_dead_end_to_end(self, vl_client, monkeypatch):
        """A5 附带：真跑到"全候选判死"时，四级告警也要落地"""
        keys = [_key("ms", "dead-a"), _key("ms", "dead-b")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys

        def fake_call(rk, messages, timeout=None):
            raise VLHTTPError(400, "Bad Request", "The model does not exist", "u")

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(
            Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result["status"] == "error"
        by_cat = {}
        for i in vl_client._test_delivered:
            by_cat.setdefault(i["category"], []).append(i)
        # 两个模型各降级一次 + 整条链路全停一次
        assert len(by_cat.get("vl_model_degraded", [])) == 2
        assert "vl_all_models_dead" in by_cat


class TestSinkSafety:
    def test_sink_exception_does_not_break_routing(self, vl_client, monkeypatch):
        """A6：告警是旁路——投递出口自己炸了，判死与选路照常"""
        def boom(_item):
            raise RuntimeError("收件箱写不进去")

        vl_client._inbox_sink = boom
        keys = [_key("ms", "dead-a"), _key("ms", "good")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys

        def fake_call(rk, messages, timeout=None):
            if rk.model == "dead-a":
                raise VLHTTPError(400, "Bad Request", "model not found", "u")
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(
            Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result["status"] == "ok"
        assert ("ms", "dead-a") in vl_client._degraded_today

    def test_lands_in_real_inbox_store_when_prod_path_active(self, vl_client, monkeypatch, tmp_path):
        """A5 后半：走生产投递路径（无 sink）时，记录真的落进收件箱库。

        用 tmp 库替换单例，避免污染真实收件箱；测完务必 reset_store() 释放。
        """
        from server import inbox as inbox_mod

        db_path = tmp_path / "inbox_test.db"
        monkeypatch.setattr(inbox_mod, "get_inbox_config", lambda: {"db_path": str(db_path)})
        inbox_mod.reset_store()
        try:
            vl_client._inbox_sink = None      # 走生产路径
            vl_client._log_dir = None         # _log_dir 是"测试替身"标记，这里要关掉
            vl_client._alert_model_degraded("ms", "qwen-vl-gone",
                                            "HTTP 404: Invalid model id", label="魔搭")

            items = inbox_mod.get_store().list()
            assert len(items) == 1
            assert items[0]["category"] == "vl_model_degraded"
            assert "qwen-vl-gone" in items[0]["title"]
        finally:
            inbox_mod.reset_store()

    def test_payload_is_json_serializable(self, vl_client):
        """收件箱 payload 字段是 JSON 列——不可序列化会在写库时才炸"""
        vl_client._alert_model_degraded("ms", "m", "HTTP 400: x", label="L")
        vl_client._mark_model_dead_today("ms", "m")
        vl_client._mark_daily_exhausted("全判死", kind="model_unavailable")

        for it in vl_client._test_delivered:
            json.dumps(it["payload"], ensure_ascii=False)
