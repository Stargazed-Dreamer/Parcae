"""Gate 账本三方一致性：引擎真产出 × audience 登记 × profile 自述。

为什么单独一道（原 wip_fe6da6fa 遗留 B）：`release/profiles/*.toml` 的 `[verification].required`
只是"这个 profile 声明了哪些 gate"的**自述清单，引擎不消费它**——真源是 `release/audience/*.toml`
的 `[audience.gates]` 加上 build 无条件跑的构建期 gate。此前没有任何测试断言这份自述与实际执行一致，
09-28 靠人工对齐过一次；结果 2026-10-06 把名不副实的 `no-key-startup` 改名成 `compile-check` 时，
就得靠人记得同改三处。本测试把"记得改三处"换成机器判据：任一处漂移即红。

三方各自从哪读：

- 引擎侧：`tools/release/engine/{prepare,build}.py` 里所有 `GateResult(name="…")` 的字面量
  （正则提取，不 import 引擎，免得把 release 的依赖链拖进单测）。
- 登记侧：`release/audience/*.toml` 的 `[audience.gates].static` / `.build_time`。
- 自述侧：`release/profiles/*.toml` 中 `build_enabled = true` 且带 `[verification].required` 的 profile。

地板（防"恒绿"）：`test_engine_names_are_actually_discovered` 断言已知 gate 名确实在提取结果里
（正则一坏，三方比对就变成空集比空集）；`test_synthetic_drift_is_detected` 用合成账本验每种漂移
都能被 `diff_gate_ledger` 报出来。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_FILES = (
    _ROOT / "tools" / "release" / "engine" / "prepare.py",
    _ROOT / "tools" / "release" / "engine" / "build.py",
)
_AUDIENCE_DIR = _ROOT / "release" / "audience"
_PROFILE_DIR = _ROOT / "release" / "profiles"

# 已知应当存在的 gate（引擎侧地板：少一个就说明提取正则或构造形状变了）
_KNOWN_ENGINE_GATES = {
    "policy-schema",
    "tracked-file-inventory",
    "sensitive-content-scan",
    "manifest-audit",
    "license-clearance",
    "no-agpl-import",
    "compile-check",
    "activity-task-safe-stop",
    "focused-tests",
    "archive-verification",
    "sbom-generated",
}

_GATE_NAME_RE = re.compile(r'GateResult\(\s*name="([a-z0-9][a-z0-9-]*)"')


def engine_gate_names() -> set[str]:
    """引擎实际会产出的 gate 名（来自 GateResult 构造的字面量）。"""
    found: set[str] = set()
    for path in _ENGINE_FILES:
        found |= set(_GATE_NAME_RE.findall(path.read_text(encoding="utf-8")))
    return found


def audience_gates() -> dict[str, set[str]]:
    """audience 名 → 该 audience 登记的 gate 全集（static ∪ build_time）。"""
    out: dict[str, set[str]] = {}
    for path in sorted(_AUDIENCE_DIR.glob("*.toml")):
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        gates = data.get("audience", {}).get("gates", {})
        name = data.get("audience", {}).get("name") or path.stem
        out[name] = set(gates.get("static", [])) | set(gates.get("build_time", []))
    return out


def live_profile_claims() -> list[tuple[str, str, set[str]]]:
    """build_enabled 的 profile → (profile_id, audience, 自述 required 清单)。"""
    claims: list[tuple[str, str, set[str]]] = []
    for path in sorted(_PROFILE_DIR.glob("*.toml")):
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        if not data.get("build_enabled"):
            continue
        required = data.get("verification", {}).get("required")
        if required is None:
            continue
        claims.append((data.get("profile_id", path.stem), str(data.get("audience", "")), set(required)))
    return claims


def diff_gate_ledger(engine: set[str], registered: set[str],
                     declared: set[str] | None = None) -> dict[str, set[str]]:
    """三方比对的纯函数。返回四类漂移，空集=该类一致。

    - claimed_but_not_in_engine：audience 登记或 profile 自述里写了，引擎根本没这个 gate（吹出来的清单）
    - engine_not_registered：引擎产出了，任何 audience 都没登记（加了 gate 忘了接线，静默不跑）
    - declared_missing：audience 实际会跑，但 profile 自述漏登记（对外说少了）
    - declared_extra：profile 自述里多出的项（对外承诺了没要跑的东西）
    """
    drift = {
        "claimed_but_not_in_engine": registered - engine,
        "engine_not_registered": engine - registered,
        "declared_missing": set(),
        "declared_extra": set(),
    }
    if declared is not None:
        drift["claimed_but_not_in_engine"] |= declared - engine
        drift["declared_missing"] = registered - declared
        drift["declared_extra"] = declared - registered
    return {k: v for k, v in drift.items() if v}


def test_engine_names_are_actually_discovered():
    """地板：先证明提取正则真的抓到了已知 gate，否则下面的比对全是空集比空集。"""
    found = engine_gate_names()
    missing = _KNOWN_ENGINE_GATES - found
    assert not missing, f"提取正则失效或 GateResult 构造形状变了，漏识别：{sorted(missing)}"
    assert len(found) >= len(_KNOWN_ENGINE_GATES), "引擎侧 gate 名集合不应小于已知清单"


def test_registered_and_declared_gates_exist_in_engine():
    """audience 登记与 profile 自述里的每个名字，都必须是引擎真会产出的 gate。"""
    engine = engine_gate_names()
    offenders: dict[str, set[str]] = {}
    for aud, gates in audience_gates().items():
        if bad := gates - engine:
            offenders[f"audience:{aud}"] = bad
    for pid, _aud, required in live_profile_claims():
        if bad := required - engine:
            offenders[f"profile:{pid}"] = bad
    assert not offenders, f"清单里写了引擎不存在的 gate：{ {k: sorted(v) for k, v in offenders.items()} }"


def test_every_engine_gate_is_wired_into_some_audience():
    """引擎产出的 gate 若没有任何 audience 登记，等于加了却不跑（静默失踪）。"""
    engine = engine_gate_names()
    wired = set().union(*audience_gates().values()) if audience_gates() else set()
    assert engine - wired == set(), f"这些 gate 没有被任何 audience 登记：{sorted(engine - wired)}"


@pytest.mark.parametrize("pid,audience,declared", live_profile_claims())
def test_profile_self_description_equals_audience_registration(pid: str, audience: str, declared: set[str]):
    """build_enabled 的 profile：[verification].required 必须等于其 audience 的 static ∪ build_time。"""
    regs = audience_gates()
    assert audience in regs, f"profile {pid} 的 audience='{audience}' 没有对应登记文件"
    drift = diff_gate_ledger(engine_gate_names(), regs[audience], declared)
    assert not drift, (
        f"profile {pid}（audience={audience}）gate 账本漂移：{ {k: sorted(v) for k, v in drift.items()} }"
    )


def test_live_profile_claims_are_not_all_empty():
    """地板：至少有一个 build_enabled profile 带自述清单，否则上面的参数化用例一条都没跑。"""
    assert live_profile_claims(), "没有任何 build_enabled 且带 [verification].required 的 profile"


def test_synthetic_drift_is_detected():
    """反向地板：合成一份漂了的账本，四类漂移必须各被报出来（否则这检查是恒等式）。"""
    engine = {"a", "b", "c", "d"}
    registered = {"a", "b", "c", "ghost"}            # ghost：登记了但引擎没有
    declared = {"a", "c", "ghost", "spoken"}         # 漏登记 b；多承诺 spoken
    drift = diff_gate_ledger(engine, registered, declared)
    # d 只在引擎里（没被任何 audience 接线）
    assert drift["engine_not_registered"] == {"d"}
    # ghost 在两边都算"账上写了、引擎没有"；spoken 是自述独有
    assert drift["claimed_but_not_in_engine"] == {"ghost", "spoken"}
    assert drift["declared_missing"] == {"b"}
    assert drift["declared_extra"] == {"spoken"}

    # 一致时必须是空 dict（否则任何账本都会红，等于没有判据）
    assert diff_gate_ledger({"a", "b"}, {"a", "b"}, {"a", "b"}) == {}
