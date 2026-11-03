"""标签页来源登记：把"后端建的"和"外来者开的"分开，但不干预外来者。

背景（2026-11-01）
------------------
项目里有约 35 个 `workspace/`、`tools/` 脚本直连 `connect_over_cdp("http://127.0.0.1:9222")`
（`AGENTS.md:225` 就是这么规定的），它们不经后端、不建 session、不登记归属。
后端在 `/browser/sessions` 里看不见它们，但它们的 tab 会出现在 CDP 的 page target 列表里。

用户裁决（2026-11-01 原话意）：
  ✅ 脚本当**不受约束的外来者**——新建就新建、关掉就关掉，不限流、不要求登记、不加归属
  ✅ 但要能在查询时认出「这几张是未知来源，可能是脚本」
  ✅ 并且**不能污染 agent 侧的判定**（比如"现在有 2 个 tab 所以要求指定目标"）

所以本模块只做**观测与标注**，一行拦截逻辑都没有，也不改任何脚本。

三类来源
--------
- ``session``     后端 SessionManager 建的，有 session_id + owner，最可信
- ``preexisting`` 本进程首次观测浏览器时就已经在的（可能是用户自己开的，也可能是更早的脚本残留）
- ``unknown``     观测期间新冒出来的、又没人认领的 —— **最可能是脚本**

``unknown`` 和 ``preexisting`` 都不是"坏人"，只是"不是我们建的"。区别在于：
``unknown`` 是后端**亲眼看着它出现**的，所以 ``first_seen_at`` 近似等于它的打开时间。

关于时间
--------
``first_seen_at`` 是**后端第一次看到它**的时间，不是 tab 的真实创建时间——CDP 不提供后者。
后端不是常驻轮询的，只在有人查 tabs/status 或走冷路径时才扫一 次，所以这个时间是
"不晚于某刻"的近似值：tab 在两次观测之间创建又关闭，就完全记录不到。这是可接受的代价，
换的是零轮询开销。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("parcae.browser_tab_origins")

# 消失多久后从登记表里清掉（tab 关了就别一直占着）
DEFAULT_TTL_SECS = 6 * 3600.0
# 登记表上限，防止长时间运行无限增长
DEFAULT_MAX_ENTRIES = 500

ORIGIN_SESSION = "session"
ORIGIN_PREEXISTING = "preexisting"
ORIGIN_UNKNOWN = "unknown"

_HINTS = {
    ORIGIN_SESSION: "后端会话创建，已有归属",
    ORIGIN_PREEXISTING: "观测开始时就存在，可能是用户自己开的",
    ORIGIN_UNKNOWN: "未知来源，可能是脚本或外部进程打开的（本项目脚本直连 CDP 不登记）",
}


@dataclass
class TabOrigin:
    """一张标签页的来源与时间档案。"""

    tab_id: str
    first_seen_at: float
    last_seen_at: float
    url: str = ""
    title: str = ""
    origin: str = ORIGIN_UNKNOWN
    session_ids: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    # 是否被观测到"曾经是 session"（session 关闭后不降级回 unknown，保持可追溯）
    ever_claimed: bool = False
    # 首次认领者：会话关闭后 session_ids/owners 会清空（避免误读成"还活着"），
    # 但"这页当初是谁建的"要留得住，否则事后没法答"这个 tab 哪来的"。
    created_by_session: str = ""
    created_by_owner: str = ""

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        ts = time.time() if now is None else now
        row: dict[str, Any] = {
            "tab_id": self.tab_id,
            "url": self.url,
            "title": self.title,
            "origin": self.origin,
            "first_seen_at": round(self.first_seen_at, 3),
            "open_secs": max(0, int(ts - self.first_seen_at)),
            "origin_hint": _HINTS.get(self.origin, ""),
        }
        if self.session_ids:
            row["session_id"] = self.session_ids[0]
            row["session_ids"] = list(self.session_ids)
        if self.owners:
            row["owner"] = self.owners[0]
        if self.created_by_session:
            row["created_by_session"] = self.created_by_session
        if self.created_by_owner:
            row["created_by_owner"] = self.created_by_owner
        return row


class TabOriginRegistry:
    """按 tab_id 记录"第一次被看到的时间"和来源分类。线程安全。"""

    def __init__(
        self,
        ttl_secs: float = DEFAULT_TTL_SECS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._tabs: dict[str, TabOrigin] = {}
        self._baseline_done = False
        self._ttl = float(ttl_secs)
        self._max = int(max_entries)
        self._lock = threading.Lock()

    def observe(
        self,
        pages: list[dict[str, Any]],
        sessions: list[dict[str, Any]] | None = None,
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """记录一次观测，返回带来源标注的标签页列表（顺序与入参一致）。

        pages:    [{tab_id, url, title}]，来自 CDP /json/list
        sessions: [{session_id, tab_id, owner}]，来自 SessionManager.list_sessions()
        """
        ts = time.time() if now is None else now
        sessions = sessions or []

        by_tab: dict[str, list[dict[str, Any]]] = {}
        for s in sessions:
            tid = s.get("tab_id") or ""
            if tid:
                by_tab.setdefault(tid, []).append(s)

        with self._lock:
            # 首次观测：此刻已存在的一律算 preexisting（可能是用户开的）
            baseline_now = not self._baseline_done
            seen: set[str] = set()

            for page in pages:
                tid = page.get("tab_id") or ""
                if not tid:
                    continue
                seen.add(tid)
                holders = by_tab.get(tid, [])
                rec = self._tabs.get(tid)

                if rec is None:
                    rec = TabOrigin(
                        tab_id=tid,
                        first_seen_at=ts,
                        last_seen_at=ts,
                        url=page.get("url") or "",
                        title=page.get("title") or "",
                        origin=ORIGIN_PREEXISTING if baseline_now else ORIGIN_UNKNOWN,
                    )
                    self._tabs[tid] = rec
                else:
                    rec.last_seen_at = ts
                    rec.url = page.get("url") or rec.url
                    rec.title = page.get("title") or rec.title

                # session 认领优先级最高：即使先前记成 unknown，也升级为 session
                if holders:
                    rec.origin = ORIGIN_SESSION
                    rec.ever_claimed = True
                    rec.session_ids = [h.get("session_id", "") for h in holders]
                    rec.owners = [h.get("owner", "") for h in holders if h.get("owner")]
                    if not rec.created_by_session and rec.session_ids:
                        rec.created_by_session = rec.session_ids[0]
                    if not rec.created_by_owner and rec.owners:
                        rec.created_by_owner = rec.owners[0]
                elif rec.ever_claimed:
                    # 会话关了但 tab 还活着：保留 session 痕迹，别退回 unknown 造成误解
                    rec.origin = ORIGIN_SESSION
                    rec.session_ids = []
                    rec.owners = []

            self._baseline_done = True
            self._prune(ts, seen)
            return [self._tabs[p["tab_id"]].to_dict(ts) for p in pages if p.get("tab_id") in self._tabs]

    def claim(
        self,
        tab_id: str,
        session_id: str,
        owner: str = "",
        *,
        url: str = "",
        title: str = "",
        now: float | None = None,
    ) -> None:
        """后端新建/复用 tab 时主动登记（不依赖下一次观测）。

        为什么需要：`observe` 只在有人查 tabs/status 或走冷路径时才跑。若一个会话
        从建立到关闭期间没有任何人观测过，它关掉后留下的 tab 在登记表里根本不存在，
        下次观测会被当成 `unknown`（疑似脚本），冷路径于是拒绝操作——把后端自己开的
        页面误判成外来者。2026-11-01 实测复现。
        """
        tid = (tab_id or "").strip()
        if not tid:
            return
        ts = time.time() if now is None else now
        with self._lock:
            rec = self._tabs.get(tid)
            if rec is None:
                rec = TabOrigin(tab_id=tid, first_seen_at=ts, last_seen_at=ts)
                self._tabs[tid] = rec
            rec.last_seen_at = ts
            if url:
                rec.url = url
            if title:
                rec.title = title
            rec.origin = ORIGIN_SESSION
            rec.ever_claimed = True
            if session_id and session_id not in rec.session_ids:
                rec.session_ids.append(session_id)
            if owner and owner not in rec.owners:
                rec.owners.append(owner)
            # 只写一次：保留"最初是谁建的"，后续复用者不覆盖
            if not rec.created_by_session and session_id:
                rec.created_by_session = session_id
            if not rec.created_by_owner and owner:
                rec.created_by_owner = owner
            if len(self._tabs) > self._max:
                ordered = sorted(self._tabs.items(), key=lambda kv: kv[1].last_seen_at)
                for tid_old, _ in ordered[: len(self._tabs) - self._max]:
                    self._tabs.pop(tid_old, None)

    def _prune(self, now: float, seen: set[str]) -> None:
        """清掉消失太久的条目，并压在上限内（调用方已持锁）。"""
        stale = [
            tid for tid, rec in self._tabs.items()
            if tid not in seen and (now - rec.last_seen_at) > self._ttl
        ]
        for tid in stale:
            self._tabs.pop(tid, None)
        if len(self._tabs) > self._max:
            # 按最后出现时间淘汰最旧的
            ordered = sorted(self._tabs.items(), key=lambda kv: kv[1].last_seen_at)
            for tid, _ in ordered[: len(self._tabs) - self._max]:
                self._tabs.pop(tid, None)

    def summary(self) -> dict[str, Any]:
        """不触发观测，只读当前登记表的统计（给 /browser/status 用，保持轻量）。"""
        with self._lock:
            counts = {ORIGIN_SESSION: 0, ORIGIN_PREEXISTING: 0, ORIGIN_UNKNOWN: 0}
            for rec in self._tabs.values():
                counts[rec.origin] = counts.get(rec.origin, 0) + 1
            foreign = [
                rec.to_dict()
                for rec in self._tabs.values()
                if rec.origin in (ORIGIN_UNKNOWN, ORIGIN_PREEXISTING)
            ]
            return {
                "counts": counts,
                "foreign_count": counts[ORIGIN_UNKNOWN] + counts[ORIGIN_PREEXISTING],
                "unknown_count": counts[ORIGIN_UNKNOWN],
                "foreign_tabs": foreign[:20],
                "note": (
                    "foreign = 不是后端建的页面（脚本/用户自己开的）。"
                    "它们不计入 agent 侧的目标歧义判定，也不受 nav_guard / 会话额度约束。"
                ),
            }

    def reset(self) -> None:
        """清空（测试用；后端重启时基线也会自然重置）。"""
        with self._lock:
            self._tabs.clear()
            self._baseline_done = False


_registry: TabOriginRegistry | None = None
_registry_lock = threading.Lock()


def get_tab_origins() -> TabOriginRegistry:
    """进程级单例。"""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = TabOriginRegistry()
    return _registry
