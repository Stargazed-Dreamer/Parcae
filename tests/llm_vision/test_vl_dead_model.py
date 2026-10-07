"""远程 VL 的"死模型"处置（2026-10-05，wip_c3716c0b）。

此前 VL 链路不经 pool.py 的重试机件，400 被当瞬时错误：每调重试 6 次、不记账、
`_pick_provider` 每次都返回同一个模型，而响应体从未被读（`str(HTTPError)` 里只有
`HTTP Error 400: Bad Request`）。魔搭不定期下架模型 ID，撞上的就是这一档。

覆盖：
- D1 `_call_chat` 把状态码与响应体截断带出来
- D2 分类器按码判永久（400/404/422），429/5xx 绝不算
- D3 无状态码时退回消息形态
- D4 永久失败 → 同 key 换下一个模型，且不退避（中间量断言）
- D5 全部模型永久失败 → 提前停止，不把 6 档退避跑满
- D6 回归护栏：429 仍走冷却，没被永久分支抢走
- D7 (key_id, model) 级今日失效标记：下一次独立调用不再入选、跨日自动解除、同名模型不跨 key 波及
- D8 同 token 串行守卫：候选粒度升到 (key, model) 后，锁与并发计数仍在 key 级（Ticket 02 的"不得放松"项）
"""

import io
import threading
import time
import urllib.error
from http.client import HTTPMessage

import pytest
from PIL import Image

from server.llm_pool.key_store import ResolvedKey
from server.vl.remote_vl import (
    RemoteVLClient,
    VLHTTPError,
    classify_permanent_model_error,
)


@pytest.fixture
def vl_client(monkeypatch, tmp_path):
    """隔离的 RemoteVLClient：短退避 + sleep 记账（D4/D5 要断言"有没有退避"）"""
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
            "vl_retry_backoffs": (0.01, 0.01, 0.01, 0.01, 0.01, 0.01),
        },
    )
    monkeypatch.setattr(
        "server.config.get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    sleeps: list[float] = []
    monkeypatch.setattr("server.vl.remote_vl.time.sleep", lambda x: sleeps.append(x))
    client.reload_config()
    client._test_sleeps = sleeps  # type: ignore[attr-defined]
    return client


def _key(key_id: str, model: str) -> ResolvedKey:
    return ResolvedKey(
        key_id=key_id,
        api_key=f"sk-{key_id}",
        base_url="https://api.example.cn/v1",
        model=model,
        models=[model],
        label=key_id,
        privacy_warning="",
        max_concurrency=1,
        source="test",
        vision_params={"max_concurrency": 1, "timeout": 60, "rate_limit_cooldown": 60},
    )


def _patch_resolver(monkeypatch, keys):
    """按 exclude_models 过滤的假 resolve_keys——模拟 key_store 的真行为"""

    def fake_resolve(use_case, tier=None, exclude_models=None):
        excluded = exclude_models or {}
        out = []
        for rec in keys:
            skip = excluded.get(rec.key_id, set())
            if rec.model in skip:
                continue
            out.append(rec)
        return out

    monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", fake_resolve)


# ==================== D1 响应体与状态码 ====================

class TestCallChatCarriesEvidence:
    def test_body_and_code_survive_to_the_caller(self, monkeypatch):
        client = RemoteVLClient()
        body = b'{"error": {"message": "The model `qwen-vl-gone` does not exist."}}'

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                "https://api.example.cn/v1/chat/completions",
                400, "Bad Request", HTTPMessage(), io.BytesIO(body),
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(VLHTTPError) as got:
            client._call_chat(_key("ms", "qwen-vl-gone"), [{"role": "user", "content": "x"}])

        err = got.value
        assert err.code == 400
        # 判据是"证据还在不在"，不是"有没有报错"：旧实现这里只剩 'HTTP Error 400: Bad Request'
        assert "does not exist" in err.body
        assert "does not exist" in str(err)

    def test_body_read_failure_does_not_swallow_original_error(self, monkeypatch):
        client = RemoteVLClient()

        class BrokenBody(io.BytesIO):
            def read(self, size=-1):  # noqa: D102
                raise OSError("connection reset")

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError("https://x/v1", 404, "Not Found", HTTPMessage(), BrokenBody(b""))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(VLHTTPError) as got:
            client._call_chat(_key("ms", "m"), [{"role": "user", "content": "x"}])
        assert got.value.code == 404
        assert got.value.body == ""


# ==================== D2/D3 分类器 ====================

class TestClassifier:
    @pytest.mark.parametrize("code", [400, 404, 422])
    def test_permanent_codes(self, code):
        assert classify_permanent_model_error(code, f"HTTP Error {code}: Bad Request") is True

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_transient_codes_are_never_permanent(self, code):
        """限流与过载该退避冷却，换模型不解决问题——被永久分支抢走就是行为回归"""
        assert classify_permanent_model_error(code, f"HTTP Error {code}") is False

    def test_body_hint_alone_counts_when_no_code(self):
        assert classify_permanent_model_error(None, "boom", "model not found") is True

    def test_message_fallback_for_plain_exceptions(self):
        assert classify_permanent_model_error(None, "HTTP Error 400: Bad Request") is True
        assert classify_permanent_model_error(None, "429 Too Many Requests") is False
        assert classify_permanent_model_error(None, "timed out") is False

    def test_unrelated_number_in_message_is_not_read_as_status(self):
        # 裸子串判 400 会把 "max_tokens=400" 这类读成死模型
        assert classify_permanent_model_error(None, "payload too large, n=400 items") is False


# ==================== D4/D5 failover 行为 ====================

class TestFailoverOnDeadModel:
    def test_400_switches_to_next_model_without_backoff(self, vl_client, monkeypatch):
        keys = [_key("ms", "qwen-vl-dead"), _key("ms", "qwen-vl-good")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys
        calls: list[str] = []

        def fake_call(rk, messages, timeout=None):
            calls.append(rk.model)
            if rk.model == "qwen-vl-dead":
                raise VLHTTPError(400, "Bad Request", '{"error":{"message":"model does not exist"}}', "u")
            return {"choices": [{"message": {"content": "good answer"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result["status"] == "ok"
        assert calls == ["qwen-vl-dead", "qwen-vl-good"]
        # 中间量：永久失败不该退避（旧实现每档 sleep 一次，6 档跑满才换 key）
        assert vl_client._test_sleeps == []
        # 死模型不该把整个 key 拖进冷却/耗尽
        assert vl_client._get_runtime_state("ms").cooldown_until == 0.0
        assert "ms" not in vl_client._provider_exhausted

    def test_all_models_dead_stops_early_with_actionable_detail(self, vl_client, monkeypatch):
        keys = [_key("ms", "dead-a"), _key("ms", "dead-b")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys
        calls: list[str] = []

        def fake_call(rk, messages, timeout=None):
            calls.append(rk.model)
            raise VLHTTPError(400, "Bad Request", "The model does not exist", "u")

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result["status"] == "error"
        # 地板：只尝试了两个模型各一次，而不是 6 档退避循环
        assert calls == ["dead-a", "dead-b"]
        # Ticket 04 的聚合判据：候选模型当日全判死也算"不可用"，detail 要说清成因并
        # 点名是哪些 ID（此前只回一句 "[no usable model] …"，读的人得翻日志才知道死的是谁）
        assert "模型当日判死" in result["detail"]
        assert "dead-a" in result["detail"] and "dead-b" in result["detail"]

    def test_all_models_dead_triggers_daily_exhausted_for_ocr_fallback(self, vl_client, monkeypatch):
        """模型级判死也要翻起 daily_exhausted——否则 loop_actions 拿不到 OCR 兜底入口。

        这是 Ticket 04 补的那一格：聚合判据原先只看 key 级 _provider_exhausted，
        而 400 永久失败按设计**不**标 key 级冷却（ADR 0044），于是"所有模型都被下架"
        这一档永远走不到 daily_exhausted。
        """
        keys = [_key("ms", "dead-a"), _key("ms", "dead-b")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys
        monkeypatch.setattr(vl_client, "_call_chat", lambda *a, **kw: (_ for _ in ()).throw(
            VLHTTPError(400, "Bad Request", "The model does not exist", "u")
        ))

        result = vl_client._call_with_failover(Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result.get("daily_exhausted") is True
        assert vl_client._daily_exhausted is True
        # 成因写的是模型判死，不是额度耗尽——kind 决定下游怎么读这条（见 vl_quota 文档）
        assert vl_client._daily_exhausted_reason.startswith("所有 VL 模型当日判死")
        # 判死是模型级：key 级冷却一个都没被标记（连坐只属于 429/5xx 路径）
        assert vl_client._provider_exhausted == {}

    def test_other_key_tried_after_dead_model_on_first_key(self, vl_client, monkeypatch):
        keys = [_key("ms", "dead"), _key("ice", "alive")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys
        calls: list[str] = []

        def fake_call(rk, messages, timeout=None):
            calls.append(f"{rk.key_id}/{rk.model}")
            if rk.key_id == "ms":
                raise VLHTTPError(400, "Bad Request", "model not found", "u")
            return {"choices": [{"message": {"content": "ice"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result["answer"] == "ice"
        assert calls == ["ms/dead", "ice/alive"]
        assert "ms" not in vl_client._provider_exhausted


class TestDeadModelMarksAcrossCalls:
    """Ticket 02 未落的那半格：判死必须跨调用成立，并且次日自动解除。

    单次调用内换模型（D4）早就有了，但票面要的是"今天不再被选中、
    次日 00:00 重新证明"——那是 (key_id, model) 级的持久标记，不是本轮局部变量。
    """

    def _two_model_keys(self):
        return [_key("ms", "qwen-vl-dead"), _key("ms", "qwen-vl-good")]

    def _patch_dead_calls(self, vl_client, monkeypatch, keys, calls):
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys

        def fake_call(rk, messages, timeout=None):
            calls.append(rk.model)
            if rk.model == "qwen-vl-dead":
                raise VLHTTPError(
                    400, "Bad Request", '{"error":{"message":"model does not exist"}}', "u"
                )
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)

    def test_next_independent_call_skips_the_dead_model(self, vl_client, monkeypatch):
        keys = self._two_model_keys()
        calls: list[str] = []
        self._patch_dead_calls(vl_client, monkeypatch, keys, calls)
        img = Image.new("RGB", (8, 8), "white")

        first = vl_client._call_with_failover(img, "t", use_case="vl_ocr")
        assert first["status"] == "ok"
        # 地板：第一次调用得真的撞过死模型，否则第二次"没烧它"是空的
        assert calls == ["qwen-vl-dead", "qwen-vl-good"]

        calls.clear()
        second = vl_client._call_with_failover(img, "t", use_case="vl_ocr")
        assert second["status"] == "ok"
        assert calls == ["qwen-vl-good"], "第二次独立调用还在烧同一个死模型 = 标记没有跨调用生效"
        assert vl_client._dead_models_today == {"ms": {"qwen-vl-dead"}}
        # 跨调用标记不能顺手把整个 key 冷却掉（额度语义与模型语义两回事）
        assert "ms" not in vl_client._provider_exhausted
        assert vl_client._get_runtime_state("ms").cooldown_until == 0.0

    def test_expiry_lets_the_model_be_tried_again(self, vl_client, monkeypatch):
        """把登记日改成昨天 → 下一次调用前自动解除 → 死模型重新入选一次"""
        keys = self._two_model_keys()
        calls: list[str] = []
        self._patch_dead_calls(vl_client, monkeypatch, keys, calls)
        img = Image.new("RGB", (8, 8), "white")

        vl_client._mark_model_dead_today("ms", "qwen-vl-dead")
        assert vl_client._dead_models_date == time.strftime("%Y%m%d")
        vl_client._dead_models_date = "20260101"

        calls.clear()
        result = vl_client._call_with_failover(img, "t", use_case="vl_ocr")
        assert result["status"] == "ok"
        assert calls == ["qwen-vl-dead", "qwen-vl-good"], "跨日解除后仍跳过 = 标记根本不会过期"

    def test_mark_is_key_scoped_not_model_name_only(self, vl_client, monkeypatch):
        """同名模型在另一条 key 上不该被连带排除（索引是 (key_id, model)）"""
        keys = [_key("ms", "shared-name"), _key("ice", "shared-name")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys
        calls: list[str] = []

        def fake_call(rk, messages, timeout=None):
            calls.append(f"{rk.key_id}/{rk.model}")
            if rk.key_id == "ms":
                raise VLHTTPError(400, "Bad Request", "model not found", "u")
            return {"choices": [{"message": {"content": "ice"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result["answer"] == "ice"
        assert vl_client._dead_models_today == {"ms": {"shared-name"}}

        calls.clear()
        again = vl_client._call_with_failover(Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")
        assert again["status"] == "ok"
        assert calls == ["ice/shared-name"], "ms 上的判死不该波及 ice 上的同名模型"

    def test_marking_twice_same_day_does_not_grow(self, vl_client):
        vl_client._mark_model_dead_today("ms", "a")
        vl_client._mark_model_dead_today("ms", "a")
        vl_client._mark_model_dead_today("ms", "b")
        assert vl_client._dead_models_today == {"ms": {"a", "b"}}
        # 换日期重新登记 = 旧表整张作废（不能让昨天的集合顶着今天的日期继续排除）
        vl_client._dead_models_date = "20260101"
        vl_client._mark_model_dead_today("ms", "c")
        assert vl_client._dead_models_today == {"ms": {"c"}}


class TestRateLimitUnaffected:
    def test_429_still_takes_cooldown_path(self, vl_client, monkeypatch):
        """反向护栏：永久分支插在 429 判定之前，不能把限流也吞成『换模型』"""
        keys = [_key("ms", "m1")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys
        calls: list[str] = []

        def fake_call(rk, messages, timeout=None):
            calls.append(rk.model)
            raise Exception("429 Too Many Requests")

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result["status"] == "error"
        state = vl_client._get_runtime_state("ms")
        assert state.cooldown_until > time.time() - 1  # 已冷却
        assert calls == ["m1"]  # 冷却后不再被选中，而不是被当死模型排除


@pytest.fixture
def serial_client(monkeypatch, tmp_path):
    """D8 专用：**不** patch time.sleep —— 那条 patch 改的是 stdlib time 模块本身，
    会让"第二个线程退避后重试"变成空转，串行窗口就测不到了。退避给真实秒数。"""
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
            "vl_retry_backoffs": (0.02, 0.05, 0.1, 0.2, 0.3, 0.5),
        },
    )
    monkeypatch.setattr(
        "server.config.get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    client.reload_config()
    return client


class TestSameTokenSerialization:
    """D8：候选粒度升到 (key, model) 后，同一个 token 仍不许有两个请求同时在飞。

    机件有两条、作用不对称，各钉一条测试（粒度取舍的"为什么"见 `docs/adr/0044`）：
    - per-key 锁：remote_vl.py `_get_per_key_lock(rk.key_id)`（model 不参与取锁）—— 真正保证串行的一道
    - key 级并发计数：`_pick_provider` 用 `state.active_count >= max_concurrency` 挡第二个 ——
      只是选路阶段的早退信号：`active_count` 在 acquire 之后才自增，检查与自增跨两次调用，单靠它有竞态

    摘改矩阵（temp/20261006_d8_mutation.py 实测，两轮才做对）：
    只拆锁 → D8-3 红；只拆计数 → D8-2 红；两道一起拆 → D8-1 红。
    D8-1 是第一版写坏的判据：两个线程都从 m-a 起步，"按 model 分锁"的摘改照样撞在同一把锁上，
    于是它恒绿。改成按 use_case 发不同首选项，两个线程才真的落在不同 model 上。
    """

    def test_two_threads_on_different_models_do_not_overlap(self, serial_client, monkeypatch):
        a = _key("ms", "m-a")
        b = _key("ms", "m-b")
        order = {"vl_ocr": [a, b], "vl_vision": [b, a]}

        def fake_resolve(use_case, tier=None, exclude_models=None):
            return list(order[use_case])

        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", fake_resolve)
        serial_client._all_vl_candidates = lambda: [a, b]

        guard = threading.Lock()
        inside: dict[str, int] = {"m-a": 0, "m-b": 0}
        live_models = {"max": 0}
        finished: list[str] = []

        def fake_call(rk, messages, timeout=None):
            with guard:
                inside[rk.model] += 1
                # 中间量：此刻"同时在飞"的 model 集合，正是票面要挡的东西
                live_models["max"] = max(live_models["max"], sum(1 for v in inside.values() if v > 0))
            time.sleep(0.15)  # 真 sleep：给第二个线程撞进来的窗口
            with guard:
                inside[rk.model] -= 1
                finished.append(rk.model)
            return {"choices": [{"message": {"content": f"ans-{rk.model}"}}]}

        monkeypatch.setattr(serial_client, "_call_chat", fake_call)
        results: list[dict] = []
        threads = [
            threading.Thread(
                target=lambda uc=uc: results.append(serial_client._call_with_failover(
                    Image.new("RGB", (8, 8), "white"), "t", use_case=uc)))
            for uc in ("vl_ocr", "vl_vision")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert [t.is_alive() for t in threads] == [False, False], "有线程没跑完，这台子本身不可信"
        assert sorted(r["status"] for r in results) == ["ok", "ok"], results
        # 地板：两个线程确实各走了一个 model，否则"没重叠"只是"没来过"
        assert sorted(finished) == ["m-a", "m-b"], finished
        assert live_models["max"] == 1, f"同 token 同时在飞的 model 数={live_models['max']}，串行被放松了"

    def test_capacity_limit_is_counted_per_key_not_per_model(self, vl_client, monkeypatch):
        keys = [_key("ms", "m-a"), _key("ms", "m-b")]
        _patch_resolver(monkeypatch, keys)
        state = vl_client._get_runtime_state("ms")
        state.active_count = 1  # m-a 正在飞

        assert vl_client._pick_provider(use_case="vl_ocr", exclude_models={"ms": {"m-a"}}) is None

        # 中间量：清零在飞数后同一个排除集仍能选到 m-b ⇒ 上一条的 None 来自并发上限，不是排除集
        state.active_count = 0
        picked = vl_client._pick_provider(use_case="vl_ocr", exclude_models={"ms": {"m-a"}})
        assert picked is not None
        assert picked[0].model == "m-b"

    def test_switching_model_reuses_the_same_key_lock(self, vl_client, monkeypatch):
        keys = [_key("ms", "dead"), _key("ms", "alive")]
        _patch_resolver(monkeypatch, keys)
        vl_client._all_vl_candidates = lambda: keys

        def fake_call(rk, messages, timeout=None):
            if rk.model == "dead":
                raise VLHTTPError(400, "Bad Request", "The model does not exist", "u")
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", fake_call)
        result = vl_client._call_with_failover(
            Image.new("RGB", (8, 8), "white"), "t", use_case="vl_ocr")

        assert result["status"] == "ok"
        # 换过一次 model 也不该开出第二把锁：锁表按 key_id 索引
        assert list(vl_client._per_key_lock) == ["ms"]
