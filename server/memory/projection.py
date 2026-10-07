"""上下文投影留痕（Context Projection trace）

`search_traces` 记录到"检索返回了什么"为止。但记忆真正进入模型上下文，还要经过
注入面的一段独立处理：按业务档位重排、砍到前 N 条、跳过没有 updated_at 的占位项。
这些动作过去**一行都没落盘**，于是"这次为什么想起 A 没想起 B"只能查到召回，
查不到"召回了却没上屏"，也查不到"上屏的顺序被人改过"。

本模块只补这一段留痕，且刻意保持无权：
- 不参与 ranking（不读它来决定谁上位）
- 不产生 authority（不因为它存在就放行任何动作）
- 写失败绝不阻断注入（留痕是观测，不是闸门）

对应 `temp/sdd/unknown-outcome-evidence/spec.md` 的 D-14 / D-15 / D-16。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any

logger = logging.getLogger("localagent.memory.projection")

# 注入面标识（surface）——新增注入点时在这里登记，别散着写字符串
SURFACE_AGENT_GUIDE_MUST_READ = "agent_guide.must_read"

_JSON_LIST_FIELDS = ("selected_keys", "dropped_keys", "rank_before", "rank_after")
_JSON_OBJ_FIELDS = ("validation_flags",)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def record_projection(
    conn: sqlite3.Connection,
    write_lock,
    *,
    surface: str,
    selected_keys: list[str],
    injected_text: str,
    dropped_keys: list[str] | None = None,
    rank_before: list[str] | None = None,
    rank_after: list[str] | None = None,
    trace_id: int | None = None,
    task_type: str | None = None,
    validation_flags: dict[str, Any] | None = None,
    note: str | None = None,
) -> int | None:
    """写一条投影记录，返回自增 id；失败返回 None（不抛，调用方不必 try）。

    `rank_before` / `rank_after` 允许相等——相等本身就是"下游没有偷偷重排"的证据，
    值得留下来；不等时说明注入面按业务档位改了顺序，两个顺序都要在。
    """
    digest = _sha256(injected_text)
    try:
        with write_lock:
            cur = conn.execute(
                """INSERT INTO context_projections
                       (trace_id, task_type, surface, selected_keys, dropped_keys,
                        rank_before, rank_after, validation_flags,
                        injected_text_digest, injected_chars, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trace_id,
                    task_type,
                    surface,
                    _dumps(list(selected_keys)),
                    _dumps(list(dropped_keys or [])),
                    _dumps(list(rank_before)) if rank_before is not None else None,
                    _dumps(list(rank_after)) if rank_after is not None else None,
                    _dumps(validation_flags or {}),
                    digest,
                    len(injected_text),
                    note,
                ),
            )
            conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else None
    except Exception as e:
        # 留痕失败不能让记忆注入整段停摆；但要吵——静默失败等于没有这条防线
        logger.warning("上下文投影留痕失败 (surface=%s): %s", surface, e)
        return None


def _fetch_dicts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """按列名取行。

    ⚠ memory 库的连接**没有**设 row_factory=sqlite3.Row（store 里一律用
    `_row_to_dict` 手工转），所以这里靠 cursor.description 自己配列名，
    不能 `dict(row)`。
    """
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def _hydrate(d: dict) -> dict:
    for field in _JSON_LIST_FIELDS:
        raw = d.get(field)
        if isinstance(raw, str):
            try:
                d[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[field] = []
    for field in _JSON_OBJ_FIELDS:
        raw = d.get(field)
        if isinstance(raw, str):
            try:
                d[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[field] = {}
    return d


def list_projections(
    conn: sqlite3.Connection,
    *,
    limit: int = 30,
    since_id: int | None = None,
    task_type: str | None = None,
    surface: str | None = None,
) -> list[dict]:
    """倒序取投影记录（轻量列，不含大 JSON），供面板列表用。"""
    sql = (
        "SELECT id, ts, trace_id, task_type, surface, injected_text_digest, "
        "injected_chars, note FROM context_projections WHERE 1=1"
    )
    params: list[Any] = []
    if since_id is not None:
        sql += " AND id > ?"
        params.append(since_id)
    if task_type:
        sql += " AND task_type = ?"
        params.append(task_type)
    if surface:
        sql += " AND surface = ?"
        params.append(surface)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    try:
        return _fetch_dicts(conn, sql, tuple(params))
    except Exception as e:
        logger.warning("读取投影记录列表失败: %s", e)
        return []


def get_projection(conn: sqlite3.Connection, projection_id: int) -> dict | None:
    """单条投影详情（含 JSON 字段还原），回答"这一条到底上了什么"。"""
    try:
        rows = _fetch_dicts(
            conn, "SELECT * FROM context_projections WHERE id = ?", (projection_id,)
        )
    except Exception as e:
        logger.warning("读取投影详情失败 id=%s: %s", projection_id, e)
        return None
    return _hydrate(rows[0]) if rows else None


def cleanup_old(conn: sqlite3.Connection, write_lock, retention_days: int) -> int:
    """按保留天数清理投影记录（与 search_traces 同一套纪律，避免无限增长）。

    写操作照 ADR-0022 走 store 的全局写锁，不依赖 `with conn:` 的隐式事务。
    """
    try:
        with write_lock:
            cur = conn.execute(
                "DELETE FROM context_projections WHERE ts < "
                "datetime('now', 'localtime', ?)",
                (f"-{int(retention_days)} days",),
            )
            conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except Exception as e:
        logger.warning("清理过期投影记录失败: %s", e)
        return 0
