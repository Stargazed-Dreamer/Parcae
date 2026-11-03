"""页级互斥 + 版本戳（page_guard）单元测试。

背景（2026-11-01 实测）：`url_pattern` 接管 / `force=true` 会把多个 session 绑到
**同一个** Playwright Page 上（实测 tab_id 相同）。执行层原来既没有互斥，也没有
"快照后被别人改过"的感知——A 拿着几秒前的 css_path 在 B 改过的 DOM 上照样执行，
静默点错元素。这正是"闭眼执行"的真实形态。

这里不连浏览器：page 用最普通的 object 顶替（page_guard 只要求可哈希、
可被弱引用）。时间不 sleep，全部走假时钟/极小超时。
"""

from __future__ import annotations

import asyncio

import pytest

from server.browser.page_guard import PageBusy, PageGuard, get_page_guard


class _FakePage:
    """最小替身：只需可哈希 + 可弱引用。"""


class _FakeClock:
    def __init__(self, t: float = 500.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_generation_starts_at_zero_and_bump_records_writer() -> None:
    guard = PageGuard()
    page = _FakePage()

    assert guard.generation(page) == 0

    guard.bump(page, session_id="sess-A", owner="agent-A", action="click")
    guard.bump(page, session_id="sess-B", owner="agent-B", action="fill")

    meta = guard.meta(page)
    assert meta.gen == 2
    assert meta.last_writer == "sess-B"
    assert meta.last_writer_owner == "agent-B"
    assert meta.last_action == "fill"
    assert meta.writers == {"sess-A", "sess-B"}


def test_generation_is_per_page() -> None:
    """版本号必须跟页面走，不能全局累加——否则两个无关 tab 会互相判过期。"""
    guard = PageGuard()
    p1, p2 = _FakePage(), _FakePage()

    guard.bump(p1, session_id="s", action="click")

    assert guard.generation(p1) == 1
    assert guard.generation(p2) == 0


def test_exclusive_serializes_two_sessions_on_same_page() -> None:
    """同一 page 上的两个会话必须排队，不能交叉执行中间被对方插一脚。"""
    guard = PageGuard()
    page = _FakePage()
    order: list[str] = []

    async def worker(name: str, delay: float) -> None:
        async with guard.exclusive(page, session_id=f"sess-{name}", owner=name, action="click"):
            order.append(f"{name}:enter")
            await asyncio.sleep(delay)
            order.append(f"{name}:exit")

    async def main() -> None:
        await asyncio.gather(worker("A", 0.05), worker("B", 0.0))

    asyncio.run(main())

    # 不管谁先拿到锁，enter/exit 必须成对相邻，不能出现 enter A → enter B
    assert order in (
        ["A:enter", "A:exit", "B:enter", "B:exit"],
        ["B:enter", "B:exit", "A:enter", "A:exit"],
    )
    assert guard.generation(page) == 2


def test_page_busy_after_wait_timeout_reports_holder() -> None:
    """排队超时要明确报"谁在占"，不能静默放行，也不能无限等。"""
    guard = PageGuard()
    page = _FakePage()

    async def main() -> PageBusy | None:
        release = asyncio.Event()

        async def holder() -> None:
            async with guard.exclusive(
                page, session_id="sess-holder-1", owner="agent-H", action="click"
            ):
                await release.wait()

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.02)  # 让 holder 先拿到锁
        try:
            async with guard.exclusive(
                page, session_id="sess-waiter", owner="agent-W", action="fill", wait_secs=0.05
            ):
                pass
        except PageBusy as e:
            return e
        finally:
            release.set()
            await task
        return None

    busy = asyncio.run(main())

    assert busy is not None
    assert busy.holder_session == "sess-holder-1"
    assert busy.holder_owner == "agent-H"
    assert busy.holder_action == "click"
    text = busy.describe()
    assert "PAGE_BUSY" in text
    assert "sess-hol" in text  # 8 位前缀，便于对号
    assert "另一个会话" in text


def test_exception_inside_exclusive_does_not_bump_but_releases() -> None:
    """操作失败通常没改 DOM，不推进版本；但锁必须释放，否则页面被永久占住。"""
    guard = PageGuard()
    page = _FakePage()

    async def main() -> None:
        with pytest.raises(RuntimeError):
            async with guard.exclusive(page, session_id="sess-A", action="click"):
                raise RuntimeError("boom")
        # 锁已释放：第二个请求能立刻拿到
        async with guard.exclusive(page, session_id="sess-B", action="click", wait_secs=0.2):
            pass

    asyncio.run(main())

    # 只有成功的那次推进了版本
    assert guard.generation(page) == 1
    assert guard.meta(page).last_writer == "sess-B"


def test_mark_false_keeps_generation_unchanged() -> None:
    """只读操作（如 snapshot 基线）不该改变版本，否则会把自己判成过期。"""
    guard = PageGuard()
    page = _FakePage()

    async def main() -> None:
        async with guard.exclusive(page, session_id="sess-A", action="snapshot", mark=False):
            pass

    asyncio.run(main())
    assert guard.generation(page) == 0


def test_describe_reports_busy_holder_then_clears() -> None:
    guard = PageGuard()
    page = _FakePage()

    async def main() -> None:
        async with guard.exclusive(page, session_id="sess-X", owner="agent-X", action="fill"):
            info = guard.describe(page)
            assert info["busy"] is True
            assert info["held_by"] == "sess-X"
        assert guard.describe(page)["busy"] is False

    asyncio.run(main())


def test_singleton_is_stable() -> None:
    assert get_page_guard() is get_page_guard()
