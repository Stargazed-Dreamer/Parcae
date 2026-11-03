"""/runtime/port 端点：端口顺延后必须如实自报。

核心回归点：**声明不属于本进程时不能回退 config.toml**。

config.toml 里的 `[server] port` 是**首选**端口，不是实际监听端口。顺延场景下
回退它会张冠李戴 —— 2026-10-20 实测：9808 上跑着的实例自报 8766（因为它的
运行时声明被另一个测试实例覆盖过），任何按 `/runtime/port` 找后端的消费方都会
连到错误端口。修法是取 ASGI `scope["server"]` 里的实际监听地址。
"""

from __future__ import annotations

import pytest

from lib import runtime_endpoint as rt
from server.core import runtime_routes as rr


@pytest.fixture()
def tmp_runtime(tmp_path, monkeypatch):
    """把声明/pid/登记表指到 tmp，让"读不到属于自己的声明"成为可控状态。"""
    monkeypatch.setattr(rt, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(rt, "DECLARATION_PATH", tmp_path / "server_port.json")
    monkeypatch.setattr(rt, "PID_PATH", tmp_path / "server.pid")
    monkeypatch.setattr(rt, "CONSUMERS_PATH", tmp_path / "consumers.json")
    return tmp_path


# ==================== 实际端口提取 ====================


def test_actual_listen_port_reads_scope():
    assert rr._actual_listen_port({"server": ("127.0.0.1", 9808)}) == 9808


@pytest.mark.parametrize("scope", [{}, {"server": None}, {"server": ("127.0.0.1",)}])
def test_actual_listen_port_returns_none_when_unavailable(scope):
    """拿不到就返回 None，由调用方决定回退（不要猜）。"""
    assert rr._actual_listen_port(scope) is None


# ==================== 端点自报行为 ====================


def test_port_is_not_claimed_from_config_when_declaration_missing(client, tmp_runtime):
    """⛔ 无声明时不得把首选端口当实际端口报出去。"""
    resp = client.get("/runtime/port")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "self-scope"
    # TestClient 的监听地址是 ("testclient", 80)：关键是它**不等于** config 的首选端口
    assert data["port"] == 80
    assert data["preferred_port"] == 8766
    assert data["degraded"] is True, "实际端口 != 首选端口，必须标 degraded"
    assert data["port"] != data["preferred_port"]


def test_declaration_of_another_process_is_ignored(client, tmp_runtime):
    """声明属于别的进程（pid 不匹配）⇒ 同样走实际监听地址，不能拿别人的端口。"""
    rt.write_declaration(
        host="127.0.0.1",
        port=9808,
        preferred_port=8766,
        degraded=True,
        pid=999999,  # 别人的进程
        status=rt.STATUS_READY,
    )
    data = client.get("/runtime/port").json()
    assert data["source"] == "self-scope"
    assert data["port"] != 9808, "别人的端口不能拿来当自己的"


def test_own_declaration_wins(client, tmp_runtime):
    """属于自己的声明仍是第一优先（含 port_env 等额外字段）。"""
    rt.write_declaration(
        host="127.0.0.1",
        port=9808,
        preferred_port=8766,
        degraded=True,
        pid=__import__("os").getpid(),
        status=rt.STATUS_READY,
        extra={"port_env": {"healthy": False}},
    )
    data = client.get("/runtime/port").json()
    assert data["source"] == "declaration"
    assert data["port"] == 9808
    assert data["port_env"]["healthy"] is False
