"""页级互斥 + 版本戳：让执行层"按会话"操作共享页面，而不是闭眼执行。

背景（2026-11-01 实测，探针见 `temp/browser_multiagent_probe_20261101/`）
--------------------------------------------------------------------
`url_pattern` 复用或 `force=true` 加入共享之后，多个 `TabSession` 会指向**同一个**
Playwright Page（实测两个 session 的 `tab_id` 完全相同）。此时执行层有两个盲区：

1. **没有互斥**：A 还在等 selector，B 已经 `goto` 走了，A 的定位落到新页面上。
2. **快照缓存按 session 存、DOM 却是共享的**：A 拿着几秒前的 `node_id`→`css_path`，
   在 B 改过 DOM 之后照样执行，静默点到/填到别的元素。这正是"闭眼执行"的真实形态
   ——不是"多个匹配里挑第一个"（那条路已有 `MULTIPLE_MATCHES` 严格拒绝），
   而是"用旧快照的坐标去打新页面的元素"。
   注：`TabSession.last_snapshot_dom_version` / `_dom_version_probe` 这两个字段
   从定义起就没有任何代码使用，设计好的 MutationObserver 防线从未接上。

本模块只做两件事（**不做归属校验**，那属于 session 层 `manager.py`）：
- **串行化**：同一物理 page 上的操作排队；排队超时返回占用者信息，而不是干等或硬闯。
- **版本戳**：每次操作后 `gen += 1` 并记录操作者。session 在 snapshot 时记下当时的
  gen；真正操作时若 gen 变了且写者不是自己 ⇒ 判为快照已失效，要求重拍。

为什么按 page 而不是按 session 加锁：要防的是"同一个页面被两个会话同时改"，
按 session 加锁恰好防不住这个——两个 session 本来就是两把不同的锁。
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("parcae.browser_page_guard")

# 同一 page 上排队的最长等待。超过就明确报错，不无限阻塞 HTTP 请求。
DEFAULT_WAIT_SECS = 10.0


class PageBusy(RuntimeError):
    """同一页面正被另一个会话操作，且排队超时。"""

    def __init__(self, page_key: str, holder_session: str, holder_owner: str,
                 holder_action: str, held_secs: float, wait_secs: float) -> None:
        self.page_key = page_key
        self.holder_session = holder_session
        self.holder_owner = holder_owner
        self.holder_action = holder_action
        self.held_secs = held_secs
        self.wait_secs = wait_secs
        super().__init__(self.describe())

    def describe(self) -> str:
        who = self.holder_session[:8] if self.holder_session else "?"
        owner = self.holder_owner or "?"
        return (
            f"PAGE_BUSY: 该页面正被 session {who}…（owner={owner}）执行 "
            f"{self.holder_action}，已占用 {self.held_secs:.1f}s，"
            f"本请求排队 {self.wait_secs:.0f}s 仍未轮到。"
            f"这不是你的页面出问题——是**另一个会话正在操作同一个 tab**，"
            f"等它结束后重试，或改用你自己的 session/tab。"
        )


@dataclass
class PageMeta:
    """单个物理页面的操作台账。"""

    gen: int = 0
    last_writer: str = ""  # 最近一次操作的 session_id
    last_writer_owner: str = ""  # 该 session 的 owner
    last_action: str = ""
    last_at: float = 0.0
    # 累计被几个不同会话操作过（诊断用）
    writers: set[str] = field(default_factory=set)

    def describe(self) -> dict[str, Any]:
        return {
            "gen": self.gen,
            "last_writer": self.last_writer,
            "last_writer_owner": self.last_writer_owner,
            "last_action": self.last_action,
            "last_at": self.last_at,
            "writers_count": len(self.writers),
        }


def page_key(page: Any) -> str:
    """稳定的页面标识（用于日志与报错，不作为字典键）。"""
    tab_id = getattr(page, "_la_tab_id", "") or ""
    if tab_id:
        return str(tab_id)
    return f"page@{id(page):x}"


class PageGuard:
    """页面级互斥锁 + 版本戳登记表。

    `page` 对象用弱引用做键，页面被回收后条目自动消失，避免 `id()` 复用导致
    版本戳串台。
    """

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
        self._metas: weakref.WeakKeyDictionary[Any, PageMeta] = weakref.WeakKeyDictionary()
        self._fallback_locks: dict[int, asyncio.Lock] = {}
        self._fallback_metas: dict[int, PageMeta] = {}
        self._hold: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
        self._fallback_hold: dict[int, dict[str, Any]] = {}

    # ---- 内部：弱引用字典在个别对象上可能不可用，退回 id 键 ----

    def _lock_for(self, page: Any) -> asyncio.Lock:
        try:
            lock = self._locks.get(page)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[page] = lock
            return lock
        except TypeError:
            lock = self._fallback_locks.get(id(page))
            if lock is None:
                lock = asyncio.Lock()
                self._fallback_locks[id(page)] = lock
            return lock

    def meta(self, page: Any) -> PageMeta:
        """取页面台账（不存在则建）。"""
        try:
            m = self._metas.get(page)
            if m is None:
                m = PageMeta()
                self._metas[page] = m
            return m
        except TypeError:
            m = self._fallback_metas.get(id(page))
            if m is None:
                m = PageMeta()
                self._fallback_metas[id(page)] = m
            return m

    def _set_hold(self, page: Any, info: dict[str, Any] | None) -> None:
        try:
            if info is None:
                self._hold.pop(page, None)
            else:
                self._hold[page] = info
        except TypeError:
            if info is None:
                self._fallback_hold.pop(id(page), None)
            else:
                self._fallback_hold[id(page)] = info

    def _get_hold(self, page: Any) -> dict[str, Any]:
        try:
            return self._hold.get(page, {}) or {}
        except TypeError:
            return self._fallback_hold.get(id(page), {}) or {}

    # ---- 对外查询 ----

    def generation(self, page: Any) -> int:
        return self.meta(page).gen

    def describe(self, page: Any) -> dict[str, Any]:
        info = self.meta(page).describe()
        hold = self._get_hold(page)
        info["busy"] = bool(hold)
        info["held_by"] = hold.get("session_id", "")
        info["held_secs"] = round(self._clock() - hold["since"], 1) if hold else 0.0
        return info

    def bump(
        self, page: Any, *, session_id: str = "", owner: str = "", action: str = ""
    ) -> PageMeta:
        """记一次操作：版本前进一格并记录操作者。"""
        m = self.meta(page)
        m.gen += 1
        m.last_writer = session_id
        m.last_writer_owner = owner
        m.last_action = action
        m.last_at = self._clock()
        if session_id:
            m.writers.add(session_id)
        return m

    # ---- 串行执行 ----

    @asynccontextmanager
    async def exclusive(
        self,
        page: Any,
        *,
        session_id: str = "",
        owner: str = "",
        action: str = "",
        wait_secs: float = DEFAULT_WAIT_SECS,
        mark: bool = True,
    ):
        """占用该页面直到代码块结束。

        - 排队超时抛 `PageBusy`（带占用者信息），不静默放行；
        - `mark=True` 时块正常结束后 `gen += 1` 并记录操作者；
          块内抛异常则不推进版本（失败的操作没改变 DOM 的把握更大）。
        """
        lock = self._lock_for(page)
        key = page_key(page)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=wait_secs)
        except TimeoutError:
            hold = self._get_hold(page)
            raise PageBusy(
                page_key=key,
                holder_session=str(hold.get("session_id", "")),
                holder_owner=str(hold.get("owner", "")),
                holder_action=str(hold.get("action", "?")),
                held_secs=(self._clock() - hold["since"]) if hold.get("since") else 0.0,
                wait_secs=wait_secs,
            ) from None
        self._set_hold(page, {
            "session_id": session_id,
            "owner": owner,
            "action": action,
            "since": self._clock(),
        })
        try:
            yield self.meta(page)
        except BaseException:
            raise
        else:
            if mark:
                self.bump(page, session_id=session_id, owner=owner, action=action)
        finally:
            self._set_hold(page, None)
            lock.release()


_guard: PageGuard | None = None


def get_page_guard() -> PageGuard:
    global _guard
    if _guard is None:
        _guard = PageGuard()
    return _guard
