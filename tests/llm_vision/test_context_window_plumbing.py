"""keys.json 的 model 级 `context_window` 一路透到压缩判据（2026-10-07，点单②）。

这条链上有两处会静默丢数据，各配一条守卫：
- `_to_dict`（keys.json 的写侧）：白名单式重建 model dict，漏一个键等于把用户填的值抹掉
- `key_form._BatchEditModelsDialog`：改一次 tier 就把表单不管辖的字段丢掉（现走 merge_edited_model）

以及两处"值必须活着到达消费方"：
- `load_llm_keys_for_pool()` → `LLMKey.model_context_windows`（只收 enabled 的 llm scope）
- `LLMPool.list_models()` → 每个条目带 `context_window`（0 = 未填），chat 面板据此设上限
"""

from __future__ import annotations

import json

import pytest

from client.widgets.key_form import merge_edited_model
from server.llm_pool import LLMKey, LLMPool, ProviderPolicy, key_store
from server.llm_pool.key_store import _to_dict, _to_record, load_llm_keys_for_pool


@pytest.fixture(autouse=True)
def _no_cache():
    key_store._invalidate_cache()
    yield
    key_store._invalidate_cache()


def _key_dict(models: list[dict]) -> dict:
    return {
        "id": "k1",
        "label": "test",
        "key": "sk-x",
        "base_url": "https://api.test/v1",
        "models": models,
        "max_concurrency": 3,
        "enabled": True,
    }


# ============================================================================
# 读写两侧都不许丢字段
# ============================================================================


class TestRecordRoundTrip:
    def test_positive_int_window_survives_read_then_write(self):
        """读侧留、写侧也得留：_to_dict 是 keys.json 的写侧，漏键 = 抹掉用户填的值。"""
        rec = _to_record(_key_dict([{"name": "m1", "scope": ["llm"], "context_window": 128000}]))
        assert rec.models[0]["context_window"] == 128000
        assert _to_dict(rec)["models"][0]["context_window"] == 128000

    @pytest.mark.parametrize("bad", [0, -5, "128k", None, True, False, [], {}])
    def test_illegal_values_are_dropped_not_stored(self, bad):
        """非法值一律当"未知"，不能带着脏值进 LLMKey（判据会把脏值当真）。"""
        rec = _to_record(_key_dict([{"name": "m1", "scope": ["llm"], "context_window": bad}]))
        assert "context_window" not in rec.models[0]

    def test_float_window_is_coerced_to_int(self):
        rec = _to_record(_key_dict([{"name": "m1", "scope": ["llm"], "context_window": 32768.0}]))
        assert rec.models[0]["context_window"] == 32768
        assert isinstance(rec.models[0]["context_window"], int)


class TestLLMKeyNormalization:
    def test_non_dict_becomes_empty(self):
        bogus: object = None  # 故意喂错类型验规范化，不是调用方真会传这个
        key = LLMKey(key="sk", base_url="u", models=["m1"],
                     model_context_windows=bogus)  # pyright: ignore[reportArgumentType]
        assert key.model_context_windows == {}

    def test_junk_entries_filtered_keeps_positive_ints(self):
        key = LLMKey(
            key="sk", base_url="u", models=["a", "b", "c"],
            model_context_windows={"a": 64000, "b": -1, "c": True},
        )
        assert key.model_context_windows == {"a": 64000}

    def test_bool_is_not_a_window(self):
        """True == 1 是 int 子类，不挡就会当成"这模型只有 1 token 上下文"。"""
        key = LLMKey(key="sk", base_url="u", models=["a"], model_context_windows={"a": True})
        assert key.model_context_windows == {}


# ============================================================================
# 到 LLMKey（运行时池）
# ============================================================================


def test_load_keys_collects_windows_only_for_enabled_llm_scope(monkeypatch, tmp_path):
    test_file = tmp_path / "keys.json"
    test_file.write_text(json.dumps({
        "version": 6,
        "keys": [_key_dict([
            {"name": "chat-a", "scope": ["llm"], "tier": 3, "context_window": 131072},
            {"name": "chat-off", "scope": ["llm"], "tier": 3, "enabled": False,
             "context_window": 8192},
            {"name": "vl-a", "scope": ["vl"], "tier": 2, "context_window": 4096},
        ])],
    }), encoding="utf-8")
    monkeypatch.setattr(key_store, "_get_unified_keys_path", lambda: test_file)

    keys = load_llm_keys_for_pool()
    assert len(keys) == 1
    # 只收 enabled 的 llm scope：关掉的模型与 vl 模型都不进这张表
    assert keys[0].model_context_windows == {"chat-a": 131072}


def test_load_keys_without_the_field_is_all_unknown(monkeypatch, tmp_path):
    """没填 context_window（现状：42 个 model 全没填）必须是"未知"，不是 0 以外的假值。"""
    test_file = tmp_path / "keys.json"
    test_file.write_text(json.dumps({
        "version": 6,
        "keys": [_key_dict([{"name": "chat-a", "scope": ["llm"], "tier": 3}])],
    }), encoding="utf-8")
    monkeypatch.setattr(key_store, "_get_unified_keys_path", lambda: test_file)

    assert load_llm_keys_for_pool()[0].model_context_windows == {}


# ============================================================================
# 到面板（/llm/pool/models 条目）
# ============================================================================


def _pool_with(keys: list[LLMKey], stats_file: str) -> LLMPool:
    return LLMPool(
        keys=keys, stats_file=stats_file,
        default_policy=ProviderPolicy(name="test"),
    )


def test_list_models_entries_carry_context_window(tmp_path):
    pool = _pool_with([
        LLMKey(key="sk-a", base_url="u", name="p-a", models=["m1", "m2"],
               model_tiers={"m1": 3, "m2": 3},
               model_context_windows={"m1": 32768}),
    ], str(tmp_path / "pool_stats.json"))
    grouped = pool.list_models()["grouped_by_tier"]
    entries = [e for group in grouped.values() for e in group]
    by_model = {e["model"]: e["context_window"] for e in entries}
    assert by_model == {"m1": 32768, "m2": 0}


# ============================================================================
# 表单侧：改 tier 不许顺手抹掉别的字段
# ============================================================================


class TestMergeEditedModel:
    def test_preserves_fields_the_form_does_not_own(self):
        original = {"name": "m1", "scope": ["llm"], "tier": 2, "enabled": True,
                    "context_window": 131072}
        merged = merge_edited_model(
            original, name="m1", scope=["llm", "vl"], tier=4,
            enabled=True, display_name="",
        )
        assert merged["tier"] == 4
        assert merged["scope"] == ["llm", "vl"]
        assert merged["context_window"] == 131072, "改一次 tier 就把窗口抹掉 = 丢用户数据"
        assert "display_name" not in merged, "空输入不该凭空造一个展示名"

    def test_cleared_display_name_is_removed(self):
        original = {"name": "m1", "display_name": "旧名", "context_window": 10}
        merged = merge_edited_model(
            original, name="m1", scope=["llm"], tier=3, enabled=True, display_name="  ",
        )
        assert "display_name" not in merged
        assert merged["context_window"] == 10

    def test_display_name_equal_to_model_name_is_not_written(self):
        merged = merge_edited_model(
            {"name": "m1"}, name="m1", scope=["llm"], tier=3, enabled=True, display_name="m1",
        )
        assert "display_name" not in merged

    def test_original_dict_is_not_mutated(self):
        original = {"name": "m1", "context_window": 10}
        merge_edited_model(original, name="m1", scope=["llm"], tier=5,
                           enabled=False, display_name="")
        assert original == {"name": "m1", "context_window": 10}
