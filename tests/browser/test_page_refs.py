"""页级引用表（G3）单元测试：一个 tab 多个 session 时，关闭不能连坐。

实测背景（2026-11-01，探针 E5）：A `browser_session_close(close_page=True)` 之后，
B **用自己的 session 操作也报"不存在或已失效"**——page 被 A 关掉，B 的 session 在
`get_page()` 探活时被判 `page_closed_external` 清掉。一人收工，同 tab 所有人陪葬。

这里不连真浏览器：page 用最小替身（只需 is_closed / async close）。
"""

from __future__ import annotations

import asyncio

import pytest

from server.browser.page_refs import PageRefTable, get_page_refs
from server.browser.session.manager import SessionManager
from server.browser.session.state import TabSession


class _FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _mk_session(sid: str, page: _FakePage, owner: str = "agent-A") -> TabSession:
    return TabSession(
        session_id=sid, tab_id="tab-1", page=page,  # type: ignore[arg-type]
        url="https://example.com/x", owner=owner,
    )


@pytest.fixture(autouse=True)
def _clean_table():
    get_page_refs().reset()
    yield
    get_page_refs().reset()


# ── 引用表本身 ────────────────────────────────────────────────────────────

def test_attach_detach_counts() -> None:
    table = PageRefTable()
    page = _FakePage()

    assert table.attach(page, "s1") == 1
    assert table.attach(page, "s2") == 2
    assert table.is_shared(page) is True
    assert table.others(page, "s1") == {"s2"}

    assert table.detach(page, "s1") == 1
    assert table.refs(page) == {"s2"}
    assert table.detach(page, "s2") == 0
    assert table.count(page) == 0
    # 幂等：重复解绑不报错
    assert table.detach(page, "s2") == 0


def test_distinct_pages_are_independent() -> None:
    table = PageRefTable()
    a, b = _FakePage(), _FakePage()
    table.attach(a, "s1")
    assert table.attach(b, "s2") == 1
    assert table.others(a, "s1") == set()
    assert table.count(b) == 1


# ── 关闭行为 ──────────────────────────────────────────────────────────────

def test_close_keeps_page_when_other_session_still_holds_it() -> None:
    """G3 的核心断言：还有人挂着 ⇒ 只解绑，不关 page。"""
    mgr = SessionManager()
    page = _FakePage()
    mgr._sessions["A"] = _mk_session("A", page, owner="agent-A")
    mgr._sessions["B"] = _mk_session("B", page, owner="agent-B")
    refs = get_page_refs()
    refs.attach(page, "A")
    refs.attach(page, "B")

    closed = asyncio.run(mgr.close_session("A", close_page=True, closed_reason="manual:agent-A"))

    assert closed is True
    assert page.close_calls == 0, "还有 B 挂着，不应关页"
    assert page.is_closed() is False
    # B 仍然可用（这正是实测里失效的那条路径）
    assert asyncio.run(mgr.get_page("B")) is page
    assert "A" not in mgr._sessions
    # 墓碑要写清"为什么没关页"
    info = mgr.get_closed_info("A")
    assert info is not None
    assert info["page_kept_by"] == ["B"]


def test_close_closes_page_when_last_holder() -> None:
    mgr = SessionManager()
    page = _FakePage()
    mgr._sessions["A"] = _mk_session("A", page)
    get_page_refs().attach(page, "A")

    assert asyncio.run(mgr.close_session("A", close_page=True)) is True
    assert page.close_calls == 1
    assert page.is_closed() is True


def test_close_page_false_never_closes_even_if_last() -> None:
    mgr = SessionManager()
    page = _FakePage()
    mgr._sessions["A"] = _mk_session("A", page)
    get_page_refs().attach(page, "A")

    assert asyncio.run(mgr.close_session("A", close_page=False)) is True
    assert page.close_calls == 0


def test_list_sessions_reports_sharing() -> None:
    mgr = SessionManager()
    page = _FakePage()
    mgr._sessions["A"] = _mk_session("A", page)
    mgr._sessions["B"] = _mk_session("B", page)
    refs = get_page_refs()
    refs.attach(page, "A")
    refs.attach(page, "B")

    rows = {r["session_id"]: r for r in mgr.list_sessions()}
    assert rows["A"]["page_ref_count"] == 2
    assert rows["A"]["page_shared_with"] == ["B"]
    assert rows["B"]["page_shared_with"] == ["A"]


def test_get_page_detaches_when_page_closed_externally() -> None:
    """外部把 tab 关了：session 清掉的同时，引用表也不能留死条目。"""
    mgr = SessionManager()
    page = _FakePage()
    page.closed = True
    mgr._sessions["A"] = _mk_session("A", page)
    refs = get_page_refs()
    refs.attach(page, "A")

    assert asyncio.run(mgr.get_page("A")) is None
    assert refs.count(page) == 0
