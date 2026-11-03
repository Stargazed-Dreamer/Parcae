"""标签页来源登记（tab_origins）单元测试。

背景：`workspace/`、`tools/` 下约 35 个脚本直连 CDP 9222（`AGENTS.md:225` 规定如此），
不经后端、不建 session、不登记归属。用户裁决把它们当**不受约束的外来者**——
不拦截、不登记、不限流，但查询时要能认出「这几张是未知来源，可能是脚本」。

所以这里的判据只有两个：
1. 观测开始时就存在的 ⇒ preexisting（可能是用户自己开的，也可能是更早的脚本残留）
2. 观测期间新冒出来又没人认领的 ⇒ unknown（**最可能是脚本**）
"""

from __future__ import annotations

import pytest

from server.browser.tab_origins import (
    ORIGIN_PREEXISTING,
    ORIGIN_SESSION,
    ORIGIN_UNKNOWN,
    TabOriginRegistry,
)


def _page(tab: str, url: str = "https://example.com/", title: str = "Example") -> dict:
    return {"tab_id": tab, "url": url, "title": title}


@pytest.fixture()
def reg() -> TabOriginRegistry:
    return TabOriginRegistry()


def test_first_observation_marks_preexisting(reg: TabOriginRegistry) -> None:
    """第一次观测时就在的页，算 preexisting（不能冤枉成脚本）。"""
    rows = reg.observe([_page("t1"), _page("t2")], [])
    assert [r["origin"] for r in rows] == [ORIGIN_PREEXISTING, ORIGIN_PREEXISTING]


def test_tab_appearing_later_is_unknown(reg: TabOriginRegistry) -> None:
    """观测期间新冒出来、又没人认领 ⇒ unknown（脚本开的就是这种）。"""
    reg.observe([_page("t1")], [])
    rows = reg.observe([_page("t1"), _page("t2")], [])
    by_tab = {r["tab_id"]: r for r in rows}
    assert by_tab["t1"]["origin"] == ORIGIN_PREEXISTING
    assert by_tab["t2"]["origin"] == ORIGIN_UNKNOWN
    # unknown 的提示语要能让人想到脚本
    assert "脚本" in by_tab["t2"]["origin_hint"]


def test_session_claim_upgrades_unknown(reg: TabOriginRegistry) -> None:
    """先被记成 unknown 的页，一旦被会话认领就升级为 session。"""
    reg.observe([_page("t1")], [])
    reg.observe([_page("t1"), _page("t2")], [])
    rows = reg.observe(
        [_page("t1"), _page("t2")],
        [{"session_id": "sess-1111", "tab_id": "t2", "owner": "agent-A"}],
    )
    t2 = next(r for r in rows if r["tab_id"] == "t2")
    assert t2["origin"] == ORIGIN_SESSION
    assert t2["session_id"] == "sess-1111"
    assert t2["owner"] == "agent-A"


def test_closed_session_keeps_trace(reg: TabOriginRegistry) -> None:
    """会话关了但页还活着：不退回 unknown，免得被误当成脚本页。"""
    reg.observe([_page("t1")], [])
    reg.observe(
        [_page("t1")], [{"session_id": "sess-2222", "tab_id": "t1", "owner": "agent-A"}]
    )
    rows = reg.observe([_page("t1")], [])  # 会话已关
    assert rows[0]["origin"] == ORIGIN_SESSION
    assert not rows[0].get("session_id")


def test_open_secs_from_first_seen(reg: TabOriginRegistry) -> None:
    """open_secs 从"后端第一次看到它"算起。"""
    reg.observe([_page("t1")], [], now=1000.0)
    rows = reg.observe([_page("t1")], [], now=1060.0)
    assert rows[0]["first_seen_at"] == pytest.approx(1000.0)
    assert rows[0]["open_secs"] == 60


def test_url_and_title_follow_latest_observation(reg: TabOriginRegistry) -> None:
    reg.observe([_page("t1", "https://a.example/", "旧标题")], [], now=1.0)
    rows = reg.observe([_page("t1", "https://a.example/next", "新标题")], [], now=2.0)
    assert rows[0]["url"] == "https://a.example/next"
    assert rows[0]["title"] == "新标题"
    assert rows[0]["first_seen_at"] == pytest.approx(1.0)  # 时间不被覆盖


def test_summary_counts_and_foreign_list(reg: TabOriginRegistry) -> None:
    reg.observe([_page("u1")], [])
    reg.observe(
        [_page("u1"), _page("s1"), _page("s2")],
        [{"session_id": "sess-3333", "tab_id": "s1", "owner": "agent-B"}],
    )
    stat = reg.summary()
    assert stat["counts"][ORIGIN_SESSION] == 1
    assert stat["counts"][ORIGIN_PREEXISTING] == 1
    assert stat["counts"][ORIGIN_UNKNOWN] == 1
    assert stat["foreign_count"] == 2  # preexisting + unknown
    assert stat["unknown_count"] == 1
    assert {t["tab_id"] for t in stat["foreign_tabs"]} == {"u1", "s2"}


def test_prune_drops_long_gone_tabs() -> None:
    """消失超过 TTL 的记录要清掉，别无限增长。"""
    reg = TabOriginRegistry(ttl_secs=10)
    reg.observe([_page("gone")], [], now=100.0)
    reg.observe([_page("alive")], [], now=200.0)  # gone 已消失且超 TTL
    stat = reg.summary()
    assert sum(stat["counts"].values()) == 1
    assert {t["tab_id"] for t in stat["foreign_tabs"]} == {"alive"}


def test_max_entries_evicts_oldest() -> None:
    reg = TabOriginRegistry(max_entries=3)
    for i in range(3):
        reg.observe([_page(f"t{i}")], [], now=float(i + 1))
    reg.observe([_page("t0"), _page("t1"), _page("t2"), _page("t3")], [], now=10.0)
    assert reg.summary()["counts"][ORIGIN_PREEXISTING] + reg.summary()["counts"][ORIGIN_UNKNOWN] <= 3


def test_reset_clears_baseline(reg: TabOriginRegistry) -> None:
    reg.observe([_page("t1")], [])
    reg.reset()
    rows = reg.observe([_page("t1")], [])
    assert rows[0]["origin"] == ORIGIN_PREEXISTING  # 重置后又算基线


# ── 2026-11-01 实测补：后端建会话时必须主动认领，别等下一次观测 ──────────────
# 现象：X 建 session 后到关闭之间没人查过 /browser/tabs ⇒ 登记表里没这条记录
# ⇒ 关闭后该 tab 被当成 unknown（疑似脚本），冷路径于是拒绝操作后端自己开的页面。

def test_claim_without_observe_marks_session() -> None:
    reg = TabOriginRegistry()
    reg.claim("t1", "sess-1", "agent-X", url="https://a.example/")
    rows = reg.observe([_page("t1", "https://a.example/", "A")], [])
    assert rows[0]["origin"] == ORIGIN_SESSION


def test_claimed_tab_survives_session_close() -> None:
    """会话关了、tab 还在：保留 session 痕迹，不退回 unknown。"""
    reg = TabOriginRegistry()
    reg.claim("t1", "sess-1", "agent-X")
    reg.observe([_page("t1")], [{"session_id": "sess-1", "tab_id": "t1", "owner": "agent-X"}])
    rows = reg.observe([_page("t1")], [])  # session 已关闭
    assert rows[0]["origin"] == ORIGIN_SESSION
    assert rows[0]["origin"] != ORIGIN_UNKNOWN


def test_claim_records_owner_and_session() -> None:
    """会话还活着时显示持有者；关闭后仍保留"当初是谁建的"。"""
    reg = TabOriginRegistry()
    reg.claim("t1", "sess-xyz", "agent-Q", url="https://q.example/")

    live = reg.observe([_page("t1")], [{"session_id": "sess-xyz", "tab_id": "t1", "owner": "agent-Q"}])
    assert live[0]["session_id"] == "sess-xyz"
    assert live[0]["owner"] == "agent-Q"

    after = reg.observe([_page("t1")], [])  # 会话关了
    assert "session_id" not in after[0]          # 不再假装还持有
    assert after[0]["created_by_session"] == "sess-xyz"
    assert after[0]["created_by_owner"] == "agent-Q"


def test_claim_ignores_empty_tab_id() -> None:
    """tab_id 没解析出来时不能凭空造条目（否则会污染统计）。"""
    reg = TabOriginRegistry()
    reg.claim("", "sess-1", "agent-X")
    reg.claim("   ", "sess-1", "agent-X")
    assert reg.summary()["counts"][ORIGIN_SESSION] == 0
