"""页级引用表：一个物理 page 可能被多个 session 共享，关闭时必须看引用数。

背景（2026-11-01 实测，探针 `temp/browser_multiagent_probe_20261101/probe2.py`）
------------------------------------------------------------------------------
`url_pattern` 复用或 `force=true` 加入共享后，两个 `TabSession` 会指向**同一个** Page
（实测 E3：新 session 的 `tab_id` 与对方的完全相同，对方 session 仍在列表里）。
旧 `close_session(close_page=True)` 不看有没有别人，直接 `page.close()`：

- 实测 E5：A 关掉自己的 session 之后，B **用自己的 session 操作也报
  "session 不存在或已失效"**——因为 page 没了，`get_page()` 探活命中
  `is_closed()`，把 B 一并判成 `page_closed_external` 清掉。
- 也就是说：一人收工，同 tab 的所有人陪葬。这在多 agent 协作里是最难排查的
  一类故障：B 什么都没做错，只是邻居关了个门。

本表只回答一个问题：**这个 page 上现在还挂着哪些 session**。
`close_session` 据此决定关不关 page（还有人就只解绑，不关页）。

⚠ 不做归属校验——owner 语义在 `session/manager.py`，两者不要混。
⚠ 用弱引用做键：page 被浏览器回收后条目自动消失，避免 id() 复用造成串台
（与 `page_guard.py` 同一手法）。
"""

from __future__ import annotations

import logging
import weakref
from typing import Any

logger = logging.getLogger("parcae.browser_page_refs")


def page_label(page: Any) -> str:
    """稳定的页面标识（仅用于日志/报错，不作为字典键）。"""
    tab_id = getattr(page, "_la_tab_id", "") or ""
    if tab_id:
        return str(tab_id)
    return f"page@{id(page):x}"


class PageRefTable:
    """page → 持有它的 session_id 集合。"""

    def __init__(self) -> None:
        self._refs: weakref.WeakKeyDictionary[Any, set[str]] = weakref.WeakKeyDictionary()
        self._fallback: dict[int, set[str]] = {}

    # ---- 内部：个别对象不支持弱引用时退回 id 键 ----

    def _get(self, page: Any) -> set[str] | None:
        try:
            return self._refs.get(page)
        except TypeError:
            return self._fallback.get(id(page))

    def _put(self, page: Any, sessions: set[str]) -> None:
        try:
            self._refs[page] = sessions
        except TypeError:
            self._fallback[id(page)] = sessions

    def _drop(self, page: Any) -> None:
        try:
            self._refs.pop(page, None)
        except TypeError:
            self._fallback.pop(id(page), None)

    # ---- 对外 ----

    def attach(self, page: Any, session_id: str) -> int:
        """登记一条引用，返回登记后该页面上的 session 数。"""
        if not session_id:
            return self.count(page)
        sessions = self._get(page)
        if sessions is None:
            sessions = set()
            self._put(page, sessions)
        sessions.add(session_id)
        return len(sessions)

    def detach(self, page: Any, session_id: str) -> int:
        """移除一条引用，返回**剩余**在该页面上的 session 数。

        幂等：不认识这个 session 也照样返回当前计数。
        """
        sessions = self._get(page)
        if sessions is None:
            return 0
        sessions.discard(session_id)
        if not sessions:
            self._drop(page)
            return 0
        return len(sessions)

    def refs(self, page: Any) -> set[str]:
        """该页面上挂着的 session_id 集合（副本，可安全遍历）。"""
        return set(self._get(page) or ())

    def count(self, page: Any) -> int:
        return len(self._get(page) or ())

    def others(self, page: Any, session_id: str) -> set[str]:
        """除自己以外还挂着谁（close 决策用）。"""
        return self.refs(page) - {session_id}

    def is_shared(self, page: Any) -> bool:
        return self.count(page) > 1

    def describe(self, page: Any) -> dict[str, Any]:
        sessions = self.refs(page)
        return {
            "page": page_label(page),
            "ref_count": len(sessions),
            "sessions": sorted(sessions),
            "shared": len(sessions) > 1,
        }

    def snapshot(self) -> list[dict[str, Any]]:
        """所有登记中的页面（诊断用；注意弱引用字典随时可能缩小）。"""
        rows: list[dict[str, Any]] = []
        try:
            for page, sessions in list(self._refs.items()):
                rows.append({
                    "page": page_label(page),
                    "ref_count": len(sessions),
                    "sessions": sorted(sessions),
                    "closed": bool(getattr(page, "is_closed", lambda: False)()),
                })
        except RuntimeError:  # 迭代中字典被 GC 改动
            pass
        for pid, sessions in self._fallback.items():
            rows.append({
                "page": f"page@{pid:x}",
                "ref_count": len(sessions),
                "sessions": sorted(sessions),
                "closed": None,
            })
        return rows

    def reset(self) -> None:
        """仅测试用：清空登记。"""
        self._refs = weakref.WeakKeyDictionary()
        self._fallback.clear()


_table: PageRefTable | None = None


def get_page_refs() -> PageRefTable:
    global _table
    if _table is None:
        _table = PageRefTable()
    return _table
