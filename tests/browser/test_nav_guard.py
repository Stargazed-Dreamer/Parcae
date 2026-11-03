"""同域导航节流（nav_guard）单元测试。

不连浏览器、不 sleep：所有时间推进走注入的假时钟。
覆盖用户 2026-10-23 拍板的策略：同一域名 30s 内只允许一次刷新/初始连接，
违规硬拒绝、**窗口恒定（同日裁决删掉累犯翻倍）**，本地地址豁免。
"""

from __future__ import annotations

import pytest

from server.browser import nav_guard as ng
from server.browser.nav_guard import (
    NavRateLimited,
    NavRateLimiter,
    explain_verdict,
    is_exempt,
    nav_guard_status,
    registrable_domain,
    split_url,
)


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock: FakeClock) -> NavRateLimiter:
    return NavRateLimiter(clock=clock)


# ---------- 域名归并 ----------

@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.bilibili.com", "bilibili.com"),
        ("space.bilibili.com", "bilibili.com"),
        ("bilibili.com", "bilibili.com"),
        ("WWW.Bilibili.COM.", "bilibili.com"),
        ("www.scnu.edu.cn", "scnu.edu.cn"),
        ("a.b.com.cn", "b.com.cn"),
        ("www.bbc.co.uk", "bbc.co.uk"),
        ("192.168.1.10", "192.168.1.10"),
        ("localhost", "localhost"),
        ("", ""),
    ],
)
def test_registrable_domain(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_split_url() -> None:
    scheme, host, key = split_url("https://space.bilibili.com/123?x=1")
    assert (scheme, host, key) == ("https", "space.bilibili.com", "bilibili.com")
    # 只给 host 也认
    assert split_url("bilibili.com") == ("", "bilibili.com", "bilibili.com")


@pytest.mark.parametrize(
    ("scheme", "host"),
    [
        ("http", "localhost"),
        ("http", "127.0.0.1"),
        ("http", "192.168.1.5"),
        ("http", "10.0.0.7"),
        ("http", "172.20.3.4"),
        ("file", ""),
        ("about", ""),
        ("chrome", ""),
        ("http", "app.local"),
    ],
)
def test_local_targets_are_exempt(scheme: str, host: str) -> None:
    assert is_exempt(scheme, host) is True


@pytest.mark.parametrize("host", ["bilibili.com", "mp.weixin.qq.com", "1.2.3.4"])
def test_public_hosts_not_exempt(host: str) -> None:
    assert is_exempt("https", host) is False


# ---------- 站点级豁免（2026-10-23 砺儒云） ----------

@pytest.mark.parametrize(
    "host",
    ["scnu.edu.cn", "moodle.scnu.edu.cn", "sso.scnu.edu.cn", "MOODLE.SCNU.EDU.CN"],
)
def test_scnu_subdomains_exempt(host: str) -> None:
    """砺儒云 + 华师 SSO 都归到 scnu.edu.cn：一次任务内连续切页不该被节流。"""
    assert is_exempt("https", host, ng.DEFAULT_EXEMPT_HOSTS) is True


def test_exempt_host_never_throttled(limiter: NavRateLimiter, clock: FakeClock) -> None:
    """豁免域连续 goto 一律放行，且不占用冷却槽（reason=exempt）。"""
    for i in range(5):
        v = limiter.check(f"https://moodle.scnu.edu.cn/course/overview.php?id={i}",
                          "goto", exempt_hosts=ng.DEFAULT_EXEMPT_HOSTS)
        assert v.allowed is True
        assert v.reason == "exempt"


def test_non_exempt_still_throttled_with_exempt_list(limiter: NavRateLimiter,
                                                     clock: FakeClock) -> None:
    """豁免名单不能顺带放开别的域——风控面要还在。"""
    assert limiter.check("https://a.example.com", "goto",
                         exempt_hosts=ng.DEFAULT_EXEMPT_HOSTS).allowed is True
    clock.advance(5)
    v = limiter.check("https://b.example.com", "goto",
                      exempt_hosts=ng.DEFAULT_EXEMPT_HOSTS)
    assert v.allowed is False
    assert v.reason == "throttled"


def test_nav_guard_exempt_hosts_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配 [browser] nav_guard_exempt_hosts ⇒ 走内置默认（含 scnu.edu.cn）。"""
    monkeypatch.setattr(
        "server.browser.nav_guard.nav_guard_exempt_hosts",
        lambda: ng.DEFAULT_EXEMPT_HOSTS,
    )
    assert "scnu.edu.cn" in ng.nav_guard_exempt_hosts()


# ---------- 核心窗口行为 ----------

def test_first_navigation_always_allowed(limiter: NavRateLimiter, clock: FakeClock) -> None:
    """首次导航必须立即放行——不是令牌桶，不让第一个请求干等。"""
    v = limiter.check("https://www.bilibili.com", "goto")
    assert v.allowed is True
    assert v.reason == "ok"
    assert v.window_secs == 30.0


def test_reload_within_window_rejected(limiter: NavRateLimiter, clock: FakeClock) -> None:
    assert limiter.check("https://www.bilibili.com", "goto").allowed
    clock.advance(5)
    v = limiter.check("https://space.bilibili.com/1", "reload")
    assert v.allowed is False
    assert v.reason == "throttled"
    assert v.host == "bilibili.com"
    assert v.retry_after_secs == pytest.approx(25.0)
    assert v.violations == 1


def test_allowed_again_after_window(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    clock.advance(30)
    v = limiter.check("https://www.bilibili.com", "reload")
    assert v.allowed is True


def test_goto_same_domain_counts_against_same_budget(limiter: NavRateLimiter, clock: FakeClock) -> None:
    """同域换一个 URL 也是「初始连接」，共用同一份额度。"""
    assert limiter.check("https://www.bilibili.com/list", "goto").allowed
    clock.advance(3)
    v = limiter.check("https://www.bilibili.com/video/123", "goto")
    assert v.allowed is False
    assert v.host == "bilibili.com"


def test_subdomain_cannot_bypass(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    clock.advance(2)
    assert limiter.check("https://space.bilibili.com/1", "goto").allowed is False


def test_other_domains_independent(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    clock.advance(1)
    assert limiter.check("https://mp.weixin.qq.com/s/x", "goto").allowed is True


@pytest.mark.parametrize("action", ["back", "forward", "", "snapshot"])
def test_non_navigation_actions_not_throttled(
    limiter: NavRateLimiter, clock: FakeClock, action: str
) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    clock.advance(1)
    v = limiter.check("https://www.bilibili.com", action)
    assert v.allowed is True
    assert v.reason == "not_throttled"


def test_rejection_does_not_extend_cooldown(limiter: NavRateLimiter, clock: FakeClock) -> None:
    """被拒不算「刷新过一次」——否则反复重试会永久锁死。"""
    limiter.check("https://www.bilibili.com", "goto")
    clock.advance(5)
    limiter.check("https://www.bilibili.com", "reload")  # 拒
    clock.advance(10)
    v = limiter.check("https://www.bilibili.com", "reload")  # 距首次 15s，仍拒
    assert v.allowed is False
    clock.advance(15)  # 距首次 30s
    assert limiter.check("https://www.bilibili.com", "reload").allowed is True


# ---------- 窗口恒定（2026-10-23 删掉累犯翻倍） ----------

def test_window_never_escalates_under_repeated_violations(
    limiter: NavRateLimiter, clock: FakeClock
) -> None:
    """连续撞 10 次，窗口仍是 30s；violations 只涨，不参与窗口计算。"""
    limiter.check("https://www.bilibili.com", "goto")
    windows = []
    for _ in range(10):
        clock.advance(1)
        v = limiter.check("https://www.bilibili.com", "reload")
        assert v.allowed is False
        windows.append(v.window_secs)
    assert windows == [30.0] * 10
    assert v.violations == 10  # 仍计数，供 status 观测


def test_violations_do_not_extend_cooldown(
    limiter: NavRateLimiter, clock: FakeClock
) -> None:
    """初版翻倍会让 30s 变成 60/120s；恒定窗口下第 31 秒必须放行。"""
    limiter.check("https://a.com", "goto")
    for _ in range(8):  # 远超旧的「满 3 次翻倍」阈值
        clock.advance(1)
        assert limiter.check("https://a.com", "reload").allowed is False
    clock.advance(30)  # 距首次 38s > 30s
    v = limiter.check("https://a.com", "reload")
    assert v.allowed is True
    assert v.window_secs == 30.0


def test_successful_navigation_resets_violations(
    limiter: NavRateLimiter, clock: FakeClock
) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    for _ in range(4):
        clock.advance(1)
        limiter.check("https://www.bilibili.com", "reload")
    clock.advance(31)
    allowed = limiter.check("https://www.bilibili.com", "reload")
    assert allowed.allowed is True
    clock.advance(1)
    # 清零后重新计数
    again = limiter.check("https://www.bilibili.com", "reload")
    assert again.violations == 1
    assert again.window_secs == 30.0


# ---------- 豁免 / 开关 ----------

def test_localhost_never_throttled(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("http://localhost:3000/", "goto")
    clock.advance(0.5)
    v = limiter.check("http://localhost:3000/", "reload")
    assert v.allowed is True
    assert v.reason == "exempt"


def test_disabled_allows_everything(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://www.bilibili.com", "goto", enabled=True)
    clock.advance(1)
    v = limiter.check("https://www.bilibili.com", "reload", enabled=False)
    assert v.allowed is True
    assert v.reason == "disabled"


def test_custom_window(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://a.com", "goto", window_secs=5)
    clock.advance(3)
    assert limiter.check("https://a.com", "reload", window_secs=5).allowed is False
    clock.advance(2.5)
    assert limiter.check("https://a.com", "reload", window_secs=5).allowed is True


# ---------- 诊断 / 运维 ----------

def test_snapshot_reports_cooldown(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    clock.advance(4)
    limiter.check("https://www.bilibili.com", "reload")
    snap = limiter.snapshot()
    assert len(snap) == 1
    assert snap[0]["host"] == "bilibili.com"
    assert snap[0]["violations"] == 1
    assert snap[0]["last_nav_ago_secs"] == pytest.approx(4.0)


def test_reset(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    limiter.reset("www.bilibili.com")
    clock.advance(1)
    assert limiter.check("https://www.bilibili.com", "reload").allowed is True


def test_explain_verdict_mentions_host_and_wait(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    clock.advance(5)
    v = limiter.check("https://www.bilibili.com", "reload")
    text = explain_verdict(v)
    assert "bilibili.com" in text
    assert "25" in text
    assert "复用" in text
    assert explain_verdict(NavRateLimiter().check("https://a.com", "goto")) == ""


def test_explain_hint_does_not_claim_window_escalation(
    limiter: NavRateLimiter, clock: FakeClock
) -> None:
    """连续被拒时只提示「可能别人在用」，不得宣称窗口变大（会误导 agent 等更久）。"""
    limiter.check("https://a.com", "goto")
    for _ in range(3):
        clock.advance(1)
        v = limiter.check("https://a.com", "reload")
    text = explain_verdict(v)
    assert "翻倍" not in text
    assert "30" in text  # 窗口仍是 30s
    # 2026-11-02 正名：措辞从「接管」改为「复用/加入共享」，别让 agent 以为能独占别人的 tab
    assert "复用" in text
    assert "url_pattern" in text
    assert "重试也不会变短" in text


def test_nav_rate_limited_exception_carries_retry(limiter: NavRateLimiter, clock: FakeClock) -> None:
    limiter.check("https://www.bilibili.com", "goto")
    clock.advance(10)
    v = limiter.check("https://www.bilibili.com", "reload")
    err = NavRateLimited(v)
    assert isinstance(err, ValueError)
    assert err.retry_after_secs == pytest.approx(20.0)
    assert "NAV_RATE_LIMITED" in str(err)


def test_nav_guard_status_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """status 是给 /browser/status 消费的，字段名别随手改。"""
    monkeypatch.setattr(
        "server.browser.nav_guard.nav_guard_config", lambda: (True, 30.0)
    )
    status = nav_guard_status()
    assert status["enabled"] is True
    assert status["window_secs"] == 30.0
    # 2026-10-23 删掉累犯翻倍后，不再有可调上限
    assert "max_window_secs" not in status
    assert status["throttled_actions"] == ["goto", "reload"]
    assert isinstance(status["hosts"], list)


def test_check_nav_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_nav 应把配置传进限定器（配错窗口大小会直接影响体验）。"""
    monkeypatch.setattr(
        "server.browser.nav_guard.nav_guard_config", lambda: (True, 5.0)
    )
    from server.browser import nav_guard as ng

    ng.get_nav_limiter().reset()
    assert ng.check_nav("https://a.example.com", "goto").allowed is True
    v = ng.check_nav("https://a.example.com", "reload")
    assert v.allowed is False
    assert v.window_secs == 5.0
    ng.get_nav_limiter().reset()


def test_check_nav_off_via_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "server.browser.nav_guard.nav_guard_config", lambda: (False, 30.0)
    )
    from server.browser import nav_guard as ng

    ng.get_nav_limiter().reset()
    assert ng.check_nav("https://a.example.com", "goto").allowed is True
    assert ng.check_nav("https://a.example.com", "reload").reason == "disabled"
    ng.get_nav_limiter().reset()


# ── 2026-11-01 归因：被拒时必须说清"这一份额度是谁用掉的" ──────────────────
# 多 agent 并发抢同域时，"还需等 22s"无法区分「自己重试太急」和「别人刚刷过」，
# 而这两件的对策完全相反。没有归因，agent 只能盲等。


def test_rejection_names_holder_when_caller_given(
    limiter: NavRateLimiter, clock: FakeClock
) -> None:
    limiter.check("https://a.example.com", "goto", caller="agent-A", session_id="sess-a1b2c3d4")
    clock.advance(8)
    v = limiter.check("https://a.example.com", "reload", caller="agent-B", session_id="sess-b9")

    assert v.allowed is False
    assert v.holder == "agent-A"
    assert v.holder_session == "sess-a1b2c3d4"
    assert v.holder_url.startswith("https://a.example.com")
    assert v.holder_ago_secs == pytest.approx(8.0)

    text = explain_verdict(v)
    assert "agent-A" in text
    assert "sess-a1b" in text  # session 前缀（8 位，便于人肉对号）
    # 后到的 agent-B 不该被说成额度持有者
    assert "agent-B" not in text
    # 原有指引不能丢
    assert "复用" in text


def test_rejection_without_holder_says_unattributed(
    limiter: NavRateLimiter, clock: FakeClock
) -> None:
    """没有 caller 时不能瞎猜，明确说「无法归因」而不是编一个名字。"""
    limiter.check("https://b.example.com", "goto")
    clock.advance(3)
    v = limiter.check("https://b.example.com", "reload")
    text = explain_verdict(v)
    assert "无法归因" in text


def test_snapshot_exposes_holder(limiter: NavRateLimiter) -> None:
    # 注意：节流 key 是可注册域（c-only.com 三段会被归并），所以要取归并后的同一 key
    limiter.check("https://c-only.com", "goto", caller="agent-C", session_id="sess-cccc1111")
    row = next(h for h in limiter.snapshot() if h["host"] == "c-only.com")
    assert row["last_nav_by"] == "agent-C"
    assert row["last_nav_session"] == "sess-cccc1111"


def test_holder_does_not_change_window(
    limiter: NavRateLimiter, clock: FakeClock
) -> None:
    """归因只是观测：带上 caller 后窗口仍是 30s，不因"不同调用者"而放宽或收紧。"""
    limiter.check("https://d.example.com", "goto", caller="agent-A")
    clock.advance(29)
    assert limiter.check("https://d.example.com", "reload", caller="agent-B").allowed is False
    clock.advance(1)
    assert limiter.check("https://d.example.com", "reload", caller="agent-B").allowed is True
