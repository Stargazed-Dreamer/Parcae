"""冷路径目标消歧（G4）单元测试。

原行为：没传 session_id / tab_id / url_pattern 时，`_find_page` 直接
``return context.pages[0]`` —— 谁排第一就操作谁。多 agent 或用户自己也开着页时，
这是"静默点了别人页面"的来源。

新行为：0 个 tab ⇒ TAB_NOT_FOUND；1 个且无主 ⇒ 允许；1 个但有主 / ≥2 个 ⇒
TAB_REQUIRED + 候选清单（含持有者）。
"""

from __future__ import annotations

from server.browser.playwright_executor import _build_playwright_header
from server.browser.target_resolver import ColdTargetIssue, decide


def _page(tab: str, url: str = "https://example.com/", title: str = "Example") -> dict:
    return {"tab_id": tab, "url": url, "title": title}


def test_no_pages_says_open_one_first() -> None:
    issue = decide([], [])
    assert isinstance(issue, ColdTargetIssue)
    assert issue.error_code == "TAB_NOT_FOUND"
    assert "browser_session_create" in issue.detail


def test_single_unowned_page_falls_back() -> None:
    """只有一个 tab 且没有会话持有：放行，并把这个 tab_id 交回调用方。

    返回值从 ``None`` 改成 ``str``（2026-11-01）：后端要告诉调用方"我替你选了哪个"，
    否则子进程 `_find_page` 会再按物理 tab 数拦一次，形成双重拦截。
    """
    assert decide([_page("t1")], []) == "t1"


def test_single_page_owned_by_session_requires_explicit_target() -> None:
    issue = decide(
        [_page("t1", "https://a.example/", "A 页")],
        [{"session_id": "sess-aaaaaaaa", "tab_id": "t1", "owner": "agent-A"}],
    )
    assert isinstance(issue, ColdTargetIssue)
    assert issue.error_code == "TAB_REQUIRED"
    assert "sess-aaa" in issue.detail   # 8 位前缀，够人肉对号          # 点名持有者
    assert "agent-A" in issue.detail
    assert "t1" in issue.detail                 # 给出可以显式传的 tab_id


def test_multiple_pages_lists_candidates_with_holders() -> None:
    issue = decide(
        [
            _page("t1", "https://a.example/", "A 页"),
            _page("t2", "https://b.example/", "B 页"),
        ],
        [{"session_id": "sess-bbbbbbbb", "tab_id": "t2", "owner": "agent-B"}],
    )
    assert isinstance(issue, ColdTargetIssue)
    assert issue.error_code == "TAB_REQUIRED"
    assert "2 个可信标签页" in issue.detail
    assert "t1" in issue.detail and "t2" in issue.detail
    assert "sess-bbb" in issue.detail
    assert len(issue.candidates) == 2
    # 候选里要能直接看到"哪个被人占着"
    holders = {c["tab_id"]: c["holders"] for c in issue.candidates}
    assert holders["t1"] == []
    assert holders["t2"][0]["owner"] == "agent-B"


def test_multiple_pages_without_sessions_still_requires_target() -> None:
    issue = decide([_page("t1"), _page("t2"), _page("t3")], [])
    assert isinstance(issue, ColdTargetIssue)
    assert issue.error_code == "TAB_REQUIRED"


# ── 子进程侧兜底：模板里不能再出现"取第一个 tab" ────────────────────────

def test_playwright_header_never_returns_first_page_blindly() -> None:
    header = _build_playwright_header()
    assert "TAB_REQUIRED" in header
    assert "if len(context.pages) == 1" in header
    # 唯一允许的 pages[0] 用法必须被 len==1 守住
    assert "return context.pages[0], None" in header
    assert "(context.pages[0] if context.pages else None)" not in header


# ── 2026-11-01：脚本直连 CDP 开的页算「外来者」，不参与歧义判定 ──────────────
# 用户裁决：脚本当不受约束的外来者，不能因为"现在有 2 个 tab"就把 agent 卡住。

def test_foreign_tabs_do_not_block_single_trusted_page() -> None:
    """1 个可信页 + 2 个外来页 ⇒ 放行，选可信那个。"""
    pages = [
        _page("mine", "https://a.example/", "我的"),
        {**_page("script1", "https://b.example/", "脚本甲"), "origin": "unknown"},
        {**_page("script2", "https://c.example/", "脚本乙"), "origin": "unknown"},
    ]
    assert decide(pages, []) == "mine"


def test_only_foreign_tabs_requires_explicit_target() -> None:
    """全是外来页 ⇒ 仍然拒绝，但要说清"这些是未知来源"，不能闷头操作。"""
    pages = [
        {**_page("script1", "https://b.example/", "脚本甲"), "origin": "unknown"},
        {**_page("script2", "https://c.example/", "脚本乙"), "origin": "unknown"},
    ]
    issue = decide(pages, [])
    assert isinstance(issue, ColdTargetIssue)
    assert issue.error_code == "TAB_REQUIRED"
    assert "未知来源" in issue.detail
    assert "脚本" in issue.detail
    assert "script1" in issue.detail and "script2" in issue.detail


def test_foreign_tabs_are_excluded_from_candidate_list() -> None:
    """2 个可信页触发歧义时，候选清单只列可信的，外来者单独提一句。"""
    pages = [
        _page("t1", "https://a.example/", "A 页"),
        _page("t2", "https://b.example/", "B 页"),
        {**_page("s1", "https://c.example/", "脚本页"), "origin": "unknown"},
    ]
    issue = decide(pages, [])
    assert isinstance(issue, ColdTargetIssue)
    assert len(issue.candidates) == 2
    assert {c["tab_id"] for c in issue.candidates} == {"t1", "t2"}
    assert "另有 1 个未知来源" in issue.detail


def test_preexisting_counts_as_trusted() -> None:
    """观测开始时就存在的页（用户自己开的）算可信，只有一个时直接放行。"""
    pages = [{**_page("user", "https://u.example/", "用户页"), "origin": "preexisting"}]
    assert decide(pages, []) == "user"


def test_missing_origin_treated_as_trusted() -> None:
    """没经过来源标注的老调用方：保持原行为，不能全被当成外来者。"""
    assert decide([_page("t1")], []) == "t1"
