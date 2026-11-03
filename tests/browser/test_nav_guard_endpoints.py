"""同域导航节流在 HTTP 端点上的行为（session 模式 / session_create）。

与 test_nav_guard.py 的分工：那边测纯策略（假时钟、无 IO），这里测**埋点**——
确认两条入口真的会拦、拦截响应里的字段/状态码能让 agent 照做，
以及豁免路径（localhost）不会误伤。

不连真实浏览器：navigate 用 mock page，session_create 用 mock manager。
"""

from __future__ import annotations

import pytest


class _MockPage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.reload_calls = 0
        self.goto_calls: list[str] = []

    async def reload(self, **kw):
        self.reload_calls += 1

    async def goto(self, url: str, **kw):
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_load_state(self, *a, **kw):
        pass


def _install_mock_session(monkeypatch, page: _MockPage, session_id: str = "s_nav_guard"):
    from server.browser.session import manager as _mgr_mod
    from server.browser.session.state import TabSession

    session = TabSession(
        session_id=session_id, tab_id="t1", page=page, url=page.url
    )

    class _MockMgr:
        def get_session(self, sid):
            return session

        async def get_page(self, sid):
            return page

    monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())


def test_second_reload_within_window_is_rejected(client, monkeypatch):
    """同一域名 30s 内第二次 reload → success=false + NAV_RATE_LIMITED。"""
    page = _MockPage("https://www.example.com/list")
    _install_mock_session(monkeypatch, page)

    first = client.post("/browser/navigate", json={"session_id": "s_nav_guard", "action": "reload"})
    assert first.json()["success"] is True

    second = client.post("/browser/navigate", json={"session_id": "s_nav_guard", "action": "reload"})
    data = second.json()
    assert data["success"] is False
    assert data["error"]["error_code"] == "NAV_RATE_LIMITED"
    assert data["nav_guard_host"] == "example.com"
    assert 0 < data["nav_retry_after_secs"] <= 30.0
    # 被拒时不该真的碰页面
    assert page.reload_calls == 1


def test_goto_same_domain_shares_budget(client, monkeypatch):
    """goto 到同域另一个 URL 也算初始连接，共用同一额度。"""
    page = _MockPage("https://www.example.com/list")
    _install_mock_session(monkeypatch, page)

    assert client.post(
        "/browser/navigate",
        json={"session_id": "s_nav_guard", "action": "goto", "url": "https://www.example.com/a"},
    ).json()["success"]

    data = client.post(
        "/browser/navigate",
        json={"session_id": "s_nav_guard", "action": "goto", "url": "https://www.example.com/b"},
    ).json()
    assert data["success"] is False
    assert data["error"]["error_code"] == "NAV_RATE_LIMITED"


def test_other_domain_not_blocked(client, monkeypatch):
    page = _MockPage("https://www.example.com/list")
    _install_mock_session(monkeypatch, page)

    client.post("/browser/navigate", json={"session_id": "s_nav_guard", "action": "reload"})
    data = client.post(
        "/browser/navigate",
        json={"session_id": "s_nav_guard", "action": "goto", "url": "https://other.test/x"},
    ).json()
    assert data["success"] is True


def test_localhost_exempt(client, monkeypatch):
    page = _MockPage("http://localhost:3000/")
    _install_mock_session(monkeypatch, page)

    for _ in range(3):
        data = client.post(
            "/browser/navigate", json={"session_id": "s_nav_guard", "action": "reload"}
        ).json()
        assert data["success"] is True


def test_error_detail_tells_agent_to_reuse(client, monkeypatch):
    """拦截文案必须给出「复用当前页 + url_pattern 复用已有 tab」的做法，否则 agent 只会重试。"""
    page = _MockPage("https://www.example.com/list")
    _install_mock_session(monkeypatch, page)

    client.post("/browser/navigate", json={"session_id": "s_nav_guard", "action": "reload"})
    detail = client.post(
        "/browser/navigate", json={"session_id": "s_nav_guard", "action": "reload"}
    ).json()["error"]["debug_detail"]
    assert "复用" in detail
    assert "url_pattern" in detail
    assert "example.com" in detail


def test_session_create_rate_limited_returns_429(client, monkeypatch):
    """session_create(url=...) 撞限流 → 429 + Retry-After（不是 500/409）。"""
    from server.browser.nav_guard import NavRateLimited, NavRateLimiter
    from server.browser.session import manager as _mgr_mod

    clock = _FakeClock()
    rejected = NavRateLimiter(clock=clock)
    rejected.check("https://www.example.com", "goto")
    clock.advance(1)
    v = rejected.check("https://www.example.com", "reload")
    assert v.allowed is False

    class _MockMgr:
        async def create_session(self, **kw):
            raise NavRateLimited(v)

    monkeypatch.setattr(_mgr_mod, "_manager", _MockMgr())

    resp = client.post("/browser/sessions", json={"url": "https://www.example.com"})
    assert resp.status_code == 429
    assert "NAV_RATE_LIMITED" in resp.json()["detail"]
    assert "retry_after_secs" in resp.json()["detail"]
    assert int(resp.headers["Retry-After"]) >= 1


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


@pytest.fixture(autouse=True)
def _clean_guard():
    """端点测试共用全局单例，每个用例前后清零。"""
    from server.browser import nav_guard as ng

    ng.get_nav_limiter().reset()
    yield
    ng.get_nav_limiter().reset()
