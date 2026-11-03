"""SQLite 存储层 — 消息、摘要、事实的 CRUD"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from server.memory.schema import init_db

logger = logging.getLogger(__name__)

_REVISION_TS_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _plus_one_microsecond(ts: str) -> str:
    """把定长时间文本 +1 微秒（同刻连续写入时用来保住"区间非空且单调"）。

    为什么需要它：Windows 上 ``datetime.now()`` 的**时钟刻度约 15.6 ms**，不是 1 µs ——
    连续两次调用经常返回**完全相同**的值。若直接拿它当历史区间的上界，
    同一个刻度内的第二次覆盖就会得到 ``valid_at == invalid_at`` 的零宽度区间，
    那条历史**永远查不到**（首版实现正是如此，被 ``test_same_second_supersedes_are_both_queryable``
    抓出来）。这里不信任时钟的精度，而是按链路前一条把上界显式推后 1 µs。
    """
    try:
        dt = datetime.strptime(ts, _REVISION_TS_FMT)
    except ValueError:
        # 迁移回填的行是秒精度（来自 messages.created_at），补零到微秒再进位
        dt = datetime.strptime(ts + ".000000", _REVISION_TS_FMT)
    return (dt + timedelta(microseconds=1)).strftime(_REVISION_TS_FMT)


class MemoryStore:
    """SQLite 存储层，线程安全（WAL 模式 + check_same_thread=False + 写锁）"""

    _write_lock: threading.RLock

    def __init__(self, db_path: str):
            """初始化数据库连接管理器。

            该方法为数据库连接管理器类的构造函数，负责设置数据库路径并初始化连接对象。
            主要用于后续建立和管理SQLite数据库连接。

            Args:
                db_path (str): SQLite数据库文件路径，用于后续建立连接。

            Returns:
                None: 该构造函数不返回任何值，仅初始化实例属性。
            """
            self.db_path = db_path  # 存储数据库文件路径，供后续连接使用
            self._conn: sqlite3.Connection | None = None  # 初始化数据库连接对象为None，表示尚未建立连接
            self._write_lock = threading.RLock()  # 可重入：ButlerStore 的读路径也复用它串行化单连接读
            self._table_columns_cache: dict[str, list[str]] = {}  # 表名列名缓存（实例级）
            # Ticket 01：DB 大小采样历史（in-memory 7 天滚动，不新增 sqlite 表）
            self._db_size_history: dict[str, float] = {}

    def _commit(self) -> None:
        """线程安全的 commit（串行化写提交，避免并发 database is locked）"""
        with self._write_lock:
            self.conn.commit()

    def initialize(self) -> None:
        """初始化数据库连接和 schema（init_db 内部会按需调用 migrate_db）"""
        self._conn = init_db(self.db_path)

    @property
    def conn(self) -> sqlite3.Connection:
        """
        获取数据库连接对象。

        该方法用于获取当前实例的 SQLite 数据库连接。如果连接尚未初始化（为 None），
        则会先调用 initialize 方法进行初始化，再返回连接对象。

        参数:
            self: 类实例自身，用于访问实例属性。

        返回值:
            sqlite3.Connection - 一个 SQLite 数据库连接对象。
        """
        if self._conn is None:  # 检查数据库连接是否已存在
            self.initialize()  # 若连接为空，则初始化数据库连接
        assert self._conn is not None  # initialize() 后必有连接，收窄返回类型
        return self._conn  # 返回数据库连接对象

        # ─── 消息 ───────────────────────────────────────────────

    def insert_message(
        self,
        content: str,
        source: str = "agent",
        role: str = "assistant",
        key: str | None = None,
        session_id: str | None = None,
        metadata: dict | None = None,
        timestamp: float | None = None,
    ) -> int:
        """插入一条消息，返回 id

        T06：INSERT + commit 包入 _write_lock，避免并发写导致 lastrowid 错乱。
        用 cur.lastrowid 替代 SELECT last_insert_rowid()（防多线程串扰）。
        """
        ts = timestamp or time.time()
        with self._write_lock:
            cur = self.conn.execute(
                """INSERT INTO messages (timestamp, source, role, content, metadata, session_id, key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, source, role, content, json.dumps(metadata or {}, ensure_ascii=False), session_id, key),
            )
            self.conn.commit()
            return cur.lastrowid or 0  # T06：用 cursor.lastrowid 而非 SELECT last_insert_rowid()

    def get_message(self, msg_id: int) -> dict | None:
        """
        根据消息ID从数据库中获取消息。

        参数:
            msg_id (int): 消息的ID。

        返回:
            Optional[dict]: 如果找到消息，返回表示消息的字典；否则返回None。
        """
        # 执行SQL查询以获取指定ID的消息
        cur = self.conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
        # 从查询结果中获取一行
        row = cur.fetchone()
        # 如果没有找到行，返回None
        if not row:
            return None
        # 将行数据转换为字典并返回
        return self._row_to_dict("messages", row)

    def query_messages(
        self,
        source: str | None = None,
        key: str | None = None,
        session_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        compressed: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """按条件查询消息"""
        conditions = []
        params: list[Any] = []

        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if key is not None:
            conditions.append("key = ?")
            params.append(key)
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            conditions.append("timestamp <= ?")
            params.append(until)
        if compressed is not None:
            conditions.append("compressed = ?")
            params.append(compressed)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM messages WHERE {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cur = self.conn.execute(sql, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

    def count_uncompressed(self) -> int:
            """统计未压缩的消息数量

            查询数据库中messages表，计算compressed字段值为0（即未压缩）的记录总数。

            Args:
                self: 类实例本身，通过self.conn访问数据库连接

            Returns:
                int: 返回未压缩消息的数量
            """
            # 执行SQL查询，统计messages表中compressed=0的记录数量
            cur = self.conn.execute("SELECT COUNT(*) FROM messages WHERE compressed = 0")
            # 从查询结果中提取第一行第一列的值，即查询到的数量
            return cur.fetchone()[0]

    def mark_compressed(self, msg_ids: list[int]) -> None:
        """
        功能：将指定的消息ID列表标记为已压缩状态。

        参数：
        msg_ids (list[int]): 消息ID的列表。

        返回值：
        None
        """
        if not msg_ids:  # 如果消息ID列表为空，则直接返回
            return
        placeholders = ",".join("?" * len(msg_ids))  # 创建SQL查询的占位符字符串，用于防止SQL注入
        # T06：execute + commit 包入 _write_lock，避免并发写交叉
        with self._write_lock:
            self.conn.execute(
                f"UPDATE messages SET compressed = 1 WHERE id IN ({placeholders})",
                msg_ids,
            )
            self.conn.commit()

    def delete_message(self, msg_id: int) -> None:
        """删除数据库中指定ID的消息。

        功能：从消息表中删除给定ID的消息记录。
        参数：
        msg_id (int): 要删除的消息ID。
        返回值：无（None）。
        """
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            self.conn.commit()

        # ─── 摘要 ───────────────────────────────────────────────

    def insert_summary(
        self,
        start_time: float,
        end_time: float,
        summary: str,
        message_count: int,
        model: str | None = None,
    ) -> int:
            """向数据库中插入一条摘要记录并返回新记录的行ID。

            Args:
                start_time (float): 摘要覆盖的时间段的开始时间戳。
                end_time (float): 摘要覆盖的时间段的结束时间戳。
                summary (str): 摘要文本内容。
                message_count (int): 摘要所覆盖的消息数量。
                model (Optional[str]): 生成摘要的模型名称，可为None。

            Returns:
                int: 新插入记录在数据库中的行ID。
            """
            # T06：INSERT + commit 包入 _write_lock，用 cur.lastrowid 替代 SELECT last_insert_rowid()
            with self._write_lock:
                cur = self.conn.execute(
                    """INSERT INTO summaries (start_time, end_time, summary, message_count, model)
                       VALUES (?, ?, ?, ?, ?)""",
                    (start_time, end_time, summary, message_count, model),
                )
                self.conn.commit()
                return cur.lastrowid or 0  # T06：cursor.lastrowid 而非 SELECT last_insert_rowid()

    def query_summaries(
        self,
        since: float | None = None,
        until: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
            """根据时间范围查询数据库中的总结记录。

            参数:
                self: 类实例，用于访问数据库连接。
                since (Optional[float]): 查询开始时间的时间戳，如果提供，则筛选结束时间大于等于该值的记录。
                until (Optional[float]): 查询结束时间的时间戳，如果提供，则筛选开始时间小于等于该值的记录。
                limit (int): 查询结果的最大数量，默认为50。

            返回值:
                list[dict]: 一个字典列表，每个字典代表一条总结记录，包含数据库中的所有列。
            """
            conditions = []  # 存储SQL WHERE子句的条件列表
            params: list[Any] = []  # 存储SQL查询的参数列表，用于防止SQL注入
            if since is not None:  # 如果指定了开始时间
                conditions.append("end_time >= ?")  # 添加条件：记录结束时间必须大于等于指定时间
                params.append(since)  # 将开始时间添加到参数列表
            if until is not None:  # 如果指定了结束时间
                conditions.append("start_time <= ?")  # 添加条件：记录开始时间必须小于等于指定时间
                params.append(until)  # 将结束时间添加到参数列表
            where = " AND ".join(conditions) if conditions else "1=1"  # 如果有条件，则用AND连接；否则使用默认条件"1=1"（表示无条件）
            sql = f"SELECT * FROM summaries WHERE {where} ORDER BY start_time DESC LIMIT ?"  # 构造SQL查询语句，按开始时间降序排列，并限制结果数量
            params.append(limit)  # 将限制数量添加到参数列表
            cur = self.conn.execute(sql, params)  # 执行SQL查询，获取游标对象
            columns = [desc[0] for desc in cur.description]  # 从游标描述中提取列名，构建列名列表
            return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]  # 将查询结果转换为字典列表返回，每个字典对应一条记录

    def delete_summaries_before(self, end_time: float) -> int:
        """删除 end_time 早于给定时间戳的摘要记录，返回删除行数。

        5-9: router 此前直接 conn.execute(DELETE FROM summaries) 绕过 _write_lock
        约定，抽成 store 方法与其他写方法一致地包锁。
        """
        with self._write_lock:
            cur = self.conn.execute(
                "DELETE FROM summaries WHERE end_time < ?", (end_time,)
            )
            self.conn.commit()
            return cur.rowcount

            # ─── 事实 ───────────────────────────────────────────────

    def upsert_fact(
        self,
        key: str,
        value: str,
        source: str = "auto",
        confidence: float = 1.0,
        fact_type: str | None = None,
        occurred_at: str | None = None,
        consumption_contexts: list[str] | None = None,
        trigger_keywords: list[str] | None = None,
        *,
        title: str | None = None,
        summary: str | None = None,
        scope: str | None = None,
        kind: str | None = None,
        origin: str | None = None,
        sensitivity: str | None = None,
        evidence_refs: list[str] | None = None,
        source_quote_ids: list[int] | None = None,
        must_read: int | None = None,
        expires_at: str | None = None,
        dedup_hash: str | None = None,
    ) -> None:
        """插入或更新一条事实记录 —— **记忆的唯一写入漏斗**。

        v3 新增：fact_type / occurred_at / consumption_contexts / trigger_keywords
        v8 新增（DL3）：三轴 title/summary/scope/kind/origin/sensitivity、
                      证据 evidence_refs/source_quote_ids、红线 must_read、
                      current_state 的 expires_at、幂等键 dedup_hash。
        这些字段全部可选：不传时更新场景保留原值（COALESCE），新建场景为 NULL。
        mentioned_at 由 get_fact 自动更新，不在此处设置。

        Args:
            key: 事实的唯一键
            value: 事实的值（通常为 JSON 字符串）
            source: 'auto' 或 'manual'（**谁写进来的**，与 origin「谁说的」不同）
            confidence: 置信度 0-1
            fact_type: 旧分类 'preference'|'project'|'reference'|'experience'|'transaction'
            occurred_at: 事实发生时间 ISO 字符串
            consumption_contexts: 消费场景列表（写入时序列化为 JSON 字符串）
            trigger_keywords: 触发关键词列表（写入时序列化为 JSON 字符串）
            title: 列表/注入的短标题
            summary: 1-2 句摘要，注入只看这个
            scope: 'global' / 'project:<id>' / 'topic:<id>'
            kind: norm/fact/pitfall/preference/procedure/reference/current_state
            origin: user_quote / agent_extracted / system / tool
            sensitivity: public / private-eng / personal（N1）
            evidence_refs: 可机器核对的证据列表（path:line / 命令 / URL）
            source_quote_ids: 指向 source_quotes.id 的列表
            must_read: 1 = 必读（只允许 kind='norm'，由 wrapup 层校验）
            expires_at: 过期时间（kind='current_state' 必填）
            dedup_hash: 幂等键（sha256），唯一部分索引守着重不进去
        """
        contexts_json = json.dumps(consumption_contexts, ensure_ascii=False) if consumption_contexts is not None else None
        keywords_json = json.dumps(trigger_keywords, ensure_ascii=False) if trigger_keywords is not None else None
        refs_json = json.dumps(evidence_refs, ensure_ascii=False) if evidence_refs is not None else None
        quotes_json = json.dumps(source_quote_ids, ensure_ascii=False) if source_quote_ids is not None else None
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            # v6：覆盖前先把旧值落进历史。**必须在同一次加锁里做**，
            # 否则"落了历史但 upsert 失败"会留下一条无主历史行。
            self._record_superseded_value(key, value)
            self.conn.execute(
                """INSERT INTO facts (key, value, source, confidence, fact_type, occurred_at,
                                       consumption_contexts, trigger_keywords,
                                       title, summary, scope, kind, origin, sensitivity,
                                       evidence_refs, source_quote_ids, must_read,
                                       expires_at, dedup_hash, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           datetime('now', 'localtime'))
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       source = excluded.source,
                       confidence = excluded.confidence,
                       fact_type = COALESCE(excluded.fact_type, facts.fact_type),
                       occurred_at = COALESCE(excluded.occurred_at, facts.occurred_at),
                       consumption_contexts = COALESCE(excluded.consumption_contexts, facts.consumption_contexts),
                       trigger_keywords = COALESCE(excluded.trigger_keywords, facts.trigger_keywords),
                       title = COALESCE(excluded.title, facts.title),
                       summary = COALESCE(excluded.summary, facts.summary),
                       scope = COALESCE(excluded.scope, facts.scope),
                       kind = COALESCE(excluded.kind, facts.kind),
                       origin = COALESCE(excluded.origin, facts.origin),
                       sensitivity = COALESCE(excluded.sensitivity, facts.sensitivity),
                       evidence_refs = COALESCE(excluded.evidence_refs, facts.evidence_refs),
                       source_quote_ids = COALESCE(excluded.source_quote_ids, facts.source_quote_ids),
                       must_read = COALESCE(excluded.must_read, facts.must_read),
                       expires_at = COALESCE(excluded.expires_at, facts.expires_at),
                       dedup_hash = COALESCE(excluded.dedup_hash, facts.dedup_hash),
                       updated_at = datetime('now', 'localtime'),
                       archived_at = NULL""",
                (key, value, source, confidence, fact_type, occurred_at, contexts_json, keywords_json,
                 title, summary, scope, kind, origin, sensitivity, refs_json, quotes_json,
                 must_read, expires_at, dedup_hash),
            )
            self.conn.commit()

    # ─── v8（DL3）：原话 / 候选账 / 上下文查询 ─────────────────────

    def insert_source_quote(
        self,
        text: str,
        speaker: str = "user",
        project_id: str | None = None,
        run_id: str | None = None,
        sensitivity: str = "personal",
        captured_at: str | None = None,
    ) -> tuple[int, bool]:
        """落一条用户原话，同 hash 不重复插。返回 ``(quote_id, is_new)``。

        为什么按归一化 hash 去重：同一句话在一个 run 里被提交两次很常见
        （``note`` 一次、``wrapup`` 一次），不判重会让原话表变成重复存档。
        **原文永不 UPDATE**：命中已有 hash 直接返回旧 id，不改正文、不改 speaker。
        """
        norm = " ".join(text.split()).strip()
        if not norm:
            raise ValueError("原话为空，拒收")
        qh = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        with self._write_lock:
            row = self.conn.execute(
                "SELECT id FROM source_quotes WHERE quote_hash = ?", (qh,)
            ).fetchone()
            if row:
                return int(row[0]), False
            cur = self.conn.execute(
                "INSERT INTO source_quotes "
                "(quote_hash, text, speaker, project_id, run_id, sensitivity, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    qh,
                    text,
                    speaker,
                    project_id,
                    run_id,
                    sensitivity,
                    captured_at or datetime.now().isoformat(timespec="seconds"),
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0), True

    def get_source_quote(self, quote_id: int) -> dict | None:
        """按 id 取一条原话（脱敏后的正文照返回，调用方自己看 redacted_at）。"""
        cur = self.conn.execute("SELECT * FROM source_quotes WHERE id = ?", (quote_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row, strict=False))

    def find_fact_by_dedup(self, dedup_hash: str) -> dict | None:
        """按幂等键找已存在的事实。

        **已失效的行也算命中**：一条记忆被软删后，同内容再提交不该变回两条——
        要么显式 restore，要么换内容重写。这里返回整行（含 ``archived_at``），
        由调用方决定是 duplicate 还是提示"已失效，要恢复吗"。
        """
        cur = self.conn.execute(
            "SELECT * FROM facts WHERE dedup_hash = ? LIMIT 1", (dedup_hash,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row, strict=False))

    def list_fact_rows_by_scope_kind(self, scope: str, kind: str) -> list[dict]:
        """去重回退用：v8 之前写入的行可能没有 dedup_hash，需在 Python 侧重算。"""
        cur = self.conn.execute(
            "SELECT * FROM facts WHERE scope = ? AND kind = ?", (scope, kind)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]

    def record_memory_candidate(
        self,
        status: str,
        payload: dict,
        run_id: str | None = None,
        reason: str | None = None,
        fact_key: str | None = None,
        dedup_hash: str | None = None,
        submitted_at: str | None = None,
    ) -> int:
        """记一条候选的去向（accepted / merged / duplicate / rejected）。

        这是「写不进去也要被看见」的落点：``rejected`` 行就是 DL5 的待审候选，
        ``payload`` 原样存 JSON，人工改完可以再提交一次。
        """
        with self._write_lock:
            cur = self.conn.execute(
                "INSERT INTO memory_candidates "
                "(run_id, submitted_at, status, reason, fact_key, dedup_hash, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    submitted_at or datetime.now().isoformat(timespec="seconds"),
                    status,
                    reason,
                    fact_key,
                    dedup_hash,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def get_memory_candidate(self, candidate_id: int) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM memory_candidates WHERE id = ?", (candidate_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        item = dict(zip(cols, row, strict=False))
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except (TypeError, ValueError):
            pass
        return item

    def resolve_memory_candidate(
        self,
        candidate_id: int,
        status: str,
        *,
        reason: str | None = None,
        fact_key: str | None = None,
    ) -> bool:
        """把一条 rejected 候选置为终态；已处理过则返回 False（不覆盖）。"""
        if status not in ("accepted", "merged", "duplicate", "dismissed"):
            raise ValueError(f"非法候选终态: {status!r}")
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE memory_candidates SET status = ?, reason = COALESCE(?, reason), "
                "fact_key = ? WHERE id = ? AND status = 'rejected'",
                (status, reason, fact_key, candidate_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def list_facts_for_context(
        self,
        scope: str | None = None,
        kinds: list[str] | None = None,
        must_read_only: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        """取一批可注入的记忆（bundle 用）。

        排序：``must_read`` 优先 → ``kind='norm'`` 优先 → ``updated_at`` 新。
        只返回活跃行。**老数据 ``scope`` 为 NULL，按 scope 过滤时自然排除**——
        这是有意的：没人判过归属的老记忆不进上下文，等人工标过再进。
        """
        where = ["archived_at IS NULL"]
        params: list[Any] = []
        if scope is not None:
            where.append("scope = ?")
            params.append(scope)
        if kinds:
            where.append("kind IN (%s)" % ", ".join("?" * len(kinds)))
            params.extend(kinds)
        if must_read_only:
            where.append("COALESCE(must_read, 0) = 1")
        params.append(limit)
        cur = self.conn.execute(
            "SELECT * FROM facts WHERE "
            + " AND ".join(where)
            + " ORDER BY COALESCE(must_read, 0) DESC,"
            " CASE WHEN kind = 'norm' THEN 0 ELSE 1 END, updated_at DESC LIMIT ?",
            params,
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]

    def list_norms(
        self,
        project_id: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """取上下文红线（DL1）：``kind='norm'`` 的活跃行。

        - 只认 ``global`` 与 ``project:<id>`` 两级；``scope`` 为 NULL 的老数据
          不进（没人判过归属，不假装知道）。
        - 排序：``must_read`` 优先（安全类）→ global 先于 project → 新更新在前。
        """
        where = ["archived_at IS NULL", "kind = 'norm'"]
        params: list[Any] = []
        if project_id:
            where.append("(scope = 'global' OR scope = ?)")
            params.append(f"project:{project_id}")
        else:
            where.append("scope = 'global'")
        params.append(limit)
        cur = self.conn.execute(
            "SELECT * FROM facts WHERE "
            + " AND ".join(where)
            + " ORDER BY COALESCE(must_read, 0) DESC,"
            " CASE WHEN scope = 'global' THEN 0 ELSE 1 END,"
            " updated_at DESC, key ASC LIMIT ?",
            params,
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]

    def _update_fact_structured_fields(self, key: str, fields: dict) -> None:
        """更新 facts 表的 v3 结构化字段（fact_type/occurred_at/consumption_contexts/trigger_keywords）。

        仅更新非 None 字段，NULL 字段保留原值。用于 extract_facts_from_kv 双写策略。
        mentioned_at 不在此处更新（由 get_fact 自动维护）。
        """
        set_clauses = []
        params: list[Any] = []
        field_map = {
            "fact_type": fields.get("fact_type"),
            "occurred_at": fields.get("occurred_at"),
            "consumption_contexts": fields.get("consumption_contexts"),  # 已序列化为 JSON 字符串
            "trigger_keywords": fields.get("trigger_keywords"),  # 已序列化为 JSON 字符串
        }
        for col, val in field_map.items():
            if val is not None:
                set_clauses.append(f"{col} = ?")
                params.append(val)
        if not set_clauses:
            return
        set_clauses.append("updated_at = datetime('now', 'localtime')")
        params.append(key)
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute(
                f"UPDATE facts SET {', '.join(set_clauses)} WHERE key = ?",
                params,
            )
            self.conn.commit()

    def get_fact(self, key: str) -> dict | None:
        """根据给定的key查询事实，并更新该事实的访问计数和 mentioned_at，然后返回事实的字典形式。

        v3 新增：mentioned_at 字段在每次访问时自动更新为当前时间。
        返回的 dict 包含更新后的 mentioned_at 和 access_count 值。

        T06：SELECT + UPDATE + commit + 重新 SELECT 包入 _write_lock，保证访问计数递增原子性。
        """
        # T06：整个读-改-写序列包入 _write_lock，避免并发 access_count 丢失更新
        # C1（2026-10-18）：已失效（archived_at 非 NULL）的行视为不存在，
        # 否则"删掉的记忆还能被 GET 出来"会让删除变成假动作。
        with self._write_lock:
            cur = self.conn.execute(
                "SELECT * FROM facts WHERE key = ? AND archived_at IS NULL", (key,)
            )
            row = cur.fetchone()
            if not row:
                return None
            now_iso = datetime.now().isoformat(timespec="seconds")
            self.conn.execute(
                "UPDATE facts SET access_count = access_count + 1, mentioned_at = ? WHERE key = ?",
                (now_iso, key),
            )
            self.conn.commit()
            cur = self.conn.execute(
                "SELECT * FROM facts WHERE key = ? AND archived_at IS NULL", (key,)
            )
            row = cur.fetchone()
            return self._row_to_dict("facts", row)

    def get_fact_meta(self, key: str) -> dict | None:
        """读取 fact 元数据（不增加 access_count，用于索引构建）。

        C1（2026-10-18）：已失效的行不返回。这条决定连带影响 ``upsert_fact``：
        对已失效的 key 再写入时，`get_fact_meta` 返回 None ⇒ 调用方按"新建"处理，
        而 SQL 侧的 ``ON CONFLICT ... archived_at = NULL`` 会把该行复活。
        """
        cur = self.conn.execute(
            "SELECT * FROM facts WHERE key = ? AND archived_at IS NULL", (key,)
        )
        row = cur.fetchone()
        return self._row_to_dict("facts", row) if row else None

    # ─── 事实版本历史（v6 P2 版本模型） ──────────────────────

    _REVISION_COLUMNS = (
        "id", "key", "value", "valid_at", "invalid_at",
        "source_message_id", "migrated", "created_at",
    )

    def _record_superseded_value(self, key: str, new_value: str) -> None:
        """覆盖写入前，把旧值落进 ``fact_revisions``（调用方须已持有 ``_write_lock``）。

        判据是 **"值真的变了"才记**。这条判断不是优化而是必需：
        ``extract_facts_from_kv`` 每次 ``set()`` 都会调用 ``upsert_fact``，
        若不比较内容，同一份数据会被反复记成历史，把 ``fact_revisions``
        变成噪音日志、也会让 ``as_of`` 查询在同一时刻出现多条候选。

        **区间怎么定（踩过两个坑，别改回去）**：
        - 下界 ``valid_at`` **接在上一条历史的 ``invalid_at`` 之后**，而不是取
          ``facts.updated_at``。后者由 SQLite 的 ``datetime('now','localtime')`` 写入、
          **只有秒精度**：同一秒内连续覆盖两次时两条历史的 ``valid_at`` 会完全相同，
          较早那条的区间就变成 ``[T, T)`` —— **零宽度、永远查不到**。
        - 上界 ``invalid_at`` 取 ``datetime.now()``，但**一旦发现它不大于下界，就显式推进
          1 µs**。理由见 ``_plus_one_microsecond``：Windows 的时钟刻度约 15.6 ms，
          两次连续调用的返回值可能完全一样，"微秒格式化"并不等于"时间唯一"。
        - 该 key 第一次被覆盖时没有上一条可接，退回 ``facts.updated_at``；此时区间是
          ``[T, now_us)``，虽然窄但仍然非空。

        时间一律用定长文本（``'YYYY-MM-DD HH:MM:SS[.ffffff]'``），与
        ``messages.created_at`` / 迁移回填行可比，字符串比较即时间比较。
        """
        row = self.conn.execute(
            "SELECT value, updated_at FROM facts WHERE key = ?", (key,)
        ).fetchone()
        if row is None or row[0] == new_value:
            return
        now_us = datetime.now().strftime(_REVISION_TS_FMT)
        prev = self.conn.execute(
            "SELECT invalid_at FROM fact_revisions WHERE key = ? ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        valid_at = str(prev[0]) if prev and prev[0] else str(row[1])
        # 上界不一定信时钟：同一个 15.6 ms 刻度内连续覆盖时 now_us 会等于 valid_at，
        # 那样区间宽度为 0、该版本永远查不到 —— 此时显式推到 valid_at + 1µs。
        invalid_at = now_us if now_us > valid_at else _plus_one_microsecond(valid_at)
        self.conn.execute(
            "INSERT INTO fact_revisions (key, value, valid_at, invalid_at, migrated) "
            "VALUES (?, ?, ?, ?, 0)",
            (key, row[0], valid_at, invalid_at),
        )

    def list_fact_revisions(self, key: str) -> list[dict]:
        """列出某个 key 的版本历史（``valid_at`` 升序）。

        刻意不走 ``_row_to_dict``：``fact_revisions`` 不在 ``_ALLOWED_TABLES``
        白名单里（v5 的 ``memory_metrics_daily`` 同样没加，而那份白名单有精确
        集合断言的测试守着）。按游标列名自建 dict，避免为一张新表去改无关的
        白名单及其测试。
        """
        cur = self.conn.execute(
            f"SELECT {', '.join(self._REVISION_COLUMNS)} FROM fact_revisions "
            "WHERE key = ? ORDER BY valid_at ASC, id ASC",
            (key,),
        )
        return [dict(zip(self._REVISION_COLUMNS, r, strict=False)) for r in cur.fetchall()]

    def get_fact_as_of(self, key: str, as_of: str) -> dict | None:
        """取 ``fact_revisions`` 中在 ``as_of`` 时刻为真的那一版。

        ``as_of`` 用 ``'YYYY-MM-DD'`` 或 ``'YYYY-MM-DD HH:MM:SS'``；因为所有时间列
        都是同格式定长文本，直接字符串比较即可正确排序。命中条件：
        ``valid_at <= as_of AND (invalid_at IS NULL OR as_of < invalid_at)``。
        仅查历史表；"当前值是否也覆盖该时刻"由 ``resolve_fact_as_of`` 兜。
        """
        cur = self.conn.execute(
            f"SELECT {', '.join(self._REVISION_COLUMNS)} FROM fact_revisions "
            "WHERE key = ? AND valid_at <= ? AND (invalid_at IS NULL OR ? < invalid_at) "
            "ORDER BY valid_at DESC, id DESC LIMIT 1",
            (key, as_of, as_of),
        )
        row = cur.fetchone()
        return dict(zip(self._REVISION_COLUMNS, row, strict=False)) if row else None

    def resolve_fact_as_of(self, key: str, as_of: str) -> tuple[str, str, dict | None] | None:
        """解析 key 在 ``as_of`` 时刻"当时为真"的值。

        返回 ``(value, origin, revision)``，``origin ∈ {'revision', 'current'}``；
        没有任何版本覆盖该时刻则返回 ``None``。

        顺序不能颠倒：先查历史、再回落当前值。当前值的有效区间是
        ``[facts.updated_at, +∞)``，只有 ``as_of`` 落进该区间才轮到它——
        先查当前值会把"改了之后又查旧日期"错判成当前值。
        """
        rev = self.get_fact_as_of(key, as_of)
        if rev is not None:
            return str(rev["value"]), "revision", rev
        cur = self.conn.execute(
            "SELECT value, updated_at FROM facts WHERE key = ?", (key,)
        ).fetchone()
        if cur is not None and cur[1] is not None and str(cur[1]) <= as_of:
            return str(cur[0]), "current", None
        return None

    def list_facts(
        self,
        source: str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict]:
        """
        从数据库中获取事实（facts）记录列表。

        此方法根据给定的可选过滤条件（来源）和数量限制，查询并返回存储的事实记录。
        如果指定了来源，则仅返回该来源的记录；否则返回所有来源的记录。结果按更新时间降序排列。

        参数:
            source (Optional[str]): 可选参数，用于筛选特定来源的事实记录。默认为 None（不限制来源）。
            limit (int): 返回记录的最大数量，默认为 100。

        返回:
            list[dict]: 一个字典列表，每个字典代表一条事实记录，键为数据库表的列名。
        """
        arch = "" if include_archived else " AND archived_at IS NULL"
        if source: # 如果提供了来源参数，则执行带来源筛选的查询
            cur = self.conn.execute(
                f"SELECT * FROM facts WHERE source = ?{arch} ORDER BY updated_at DESC LIMIT ?",
                (source, limit),
            )
        else: # 未提供来源参数，查询所有记录
            cur = self.conn.execute(
                f"SELECT * FROM facts WHERE 1=1{arch} ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        # 从游标（cursor）的描述信息中提取列名，作为后续字典的键
        columns = [desc[0] for desc in cur.description]
        # 使用列表推导式，将每一行查询结果与列名配对，转换成字典列表
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

    def delete_fact(self, key: str) -> bool:
            """**失效**指定 key 的记忆（不再物理删）。

            C1（2026-10-18 用户拍板）：原实现是 ``DELETE FROM facts WHERE key = ?``，
            与两条硬约束直接冲突——「``archive ≠ 删除``」「``superseded ≠ 物理删``」。
            而且面板已成为纠正主通路，一次误点就不可逆消失。

            现在的语义：
            - 写 ``archived_at``（定长文本、微秒精度，与 ``fact_revisions`` 同一格式）
            - 行本身与 ``fact_revisions`` 全部保留 ⇒ ``restore_fact`` 可以还原
            - 所有读路径（``get_fact`` / ``get_fact_meta`` / ``list_facts`` /
              ``manager.find_consumable_memories`` / ``manager._search_facts``）
              都已排除 ``archived_at IS NOT NULL``，所以"失效 = 读不到"
            - 同 key 再写入会被 ``upsert_fact`` 复活（``archived_at = NULL``）

            返回:
                bool: 真的从"活跃"变成"失效"才返回 True（重复删除返回 False）。
            """
            with self._write_lock:
                now_us = datetime.now().strftime(_REVISION_TS_FMT)
                cur = self.conn.execute(
                    "UPDATE facts SET archived_at = ? WHERE key = ? AND archived_at IS NULL",
                    (now_us, key),
                )
                self.conn.commit()
                return cur.rowcount > 0

    def restore_fact(self, key: str) -> bool:
        """把一条已失效的记忆恢复为活跃。

        只在 ``archived_at`` 非 NULL 时生效（重复恢复返回 False），
        这样 API 层能区分"恢复成功"与"本来就是活跃的"。
        """
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE facts SET archived_at = NULL WHERE key = ? AND archived_at IS NOT NULL",
                (key,),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get_fact_raw(self, key: str) -> dict | None:
        """读任意状态的 fact（含已失效），不增加 access_count。

        给"失效列表 / 恢复前确认"用。**不要拿它当正常读路径**——
        正常读一律走 ``get_fact`` / ``get_fact_meta``（它们会过滤已失效行）。
        """
        cur = self.conn.execute("SELECT * FROM facts WHERE key = ?", (key,))
        row = cur.fetchone()
        return self._row_to_dict("facts", row) if row else None

    def list_archived_facts(self, limit: int = 100) -> list[dict]:
        """列出已失效的记忆（最近失效的在前），用于面板"回收站"视图。"""
        cur = self.conn.execute(
            "SELECT * FROM facts WHERE archived_at IS NOT NULL "
            "ORDER BY archived_at DESC LIMIT ?",
            (limit,),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

        # ─── 向量索引 ───────────────────────────────────────────

    def insert_vector(self, message_id: int, text_hash: str, vector_blob: bytes,
                      model_name: str, dim: int) -> int:
        """
        向数据库插入一条向量索引记录。

        参数：
        - message_id: 消息的唯一标识符。
        - text_hash: 文本内容的哈希值。
        - vector_blob: 向量数据的二进制表示。
        - model_name: 生成向量的模型名称。
        - dim: 向量的维度。

        返回值：
        - 插入记录的唯一ID。
        """
        # T06：INSERT + commit 包入 _write_lock，用 cur.lastrowid 替代 SELECT last_insert_rowid()
        with self._write_lock:
            cur = self.conn.execute(
                """INSERT INTO vector_index (message_id, text_hash, vector, model_name, dim)
                   VALUES (?, ?, ?, ?, ?)""",
                (message_id, text_hash, vector_blob, model_name, dim),
            )
            self.conn.commit()
            return cur.lastrowid or 0  # T06：cursor.lastrowid 而非 SELECT last_insert_rowid()

    def get_vectors_for_search(self, model_name: str | None = None) -> list[dict]:
        """获取所有向量用于搜索"""
        if model_name:
            cur = self.conn.execute(
                """SELECT v.id, v.message_id, v.vector, v.model_name, v.dim,
                          m.content, m.timestamp, m.source
                   FROM vector_index v JOIN messages m ON v.message_id = m.id
                   WHERE v.model_name = ?""",
                (model_name,),
            )
        else:
            cur = self.conn.execute(
                """SELECT v.id, v.message_id, v.vector, v.model_name, v.dim,
                          m.content, m.timestamp, m.source
                   FROM vector_index v JOIN messages m ON v.message_id = m.id""",
            )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

    def delete_vectors_by_message(self, message_id: int) -> None:
        """根据消息ID删除对应的向量索引记录。

        Args:
            message_id (int): 需要删除关联向量的消息ID。

        Returns:
            None: 此函数无返回值。
        """
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute("DELETE FROM vector_index WHERE message_id = ?", (message_id,))
            self.conn.commit()

    def update_message_content(self, msg_id: int, content: str) -> None:
        """原地更新一条消息的正文（v6：每个 key 只保留一条"当前快照"行）。

        刻意**不动 timestamp**：这一行的身份是"该 key 的记忆快照"，它的创建时间
        应该保持首次写入的时刻；"什么时候被改过"由 ``fact_revisions`` 与
        ``source='event'`` 的事件行表达。改了 timestamp 反而让 created_at 与
        timestamp 互相矛盾，也让时间线排序失去意义。

        T06：execute + commit 包入 _write_lock。
        """
        with self._write_lock:
            self.conn.execute("UPDATE messages SET content = ? WHERE id = ?", (content, msg_id))
            self.conn.commit()

    def get_kv_snapshot_id(self, key: str) -> int | None:
        """取该 key 的"当前快照"消息 id（``source='manual'`` 的最新一条）。

        v6 起每个 key 至多一条手动行 —— 它的身份从"某一次写入"变成了"当前快照"，
        每次覆盖原地更新（见 ``update_message_content``）。
        取"最新一条"是为了兼容 v6 之前留下的多行旧址：那时一个 key 有多行，
        改到最新那行即可，旧行留给 superseded 清理与指标统计。
        """
        row = self.conn.execute(
            "SELECT id FROM messages WHERE key = ? AND source = 'manual' ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        return int(row[0]) if row else None

        # ─── BM25 索引 ─────────────────────────────────────────

    def upsert_bm25_term(self, term: str, message_id: int, tf: int = 1) -> None:
        """向BM25倒排索引表中插入或更新一条记录。

        如果指定的 (term, message_id) 组合已存在，则将其词频(tf)增加传入的值；
        如果不存在，则插入一条新记录。

        Args:
            term (str): 需要索引的词汇/词项。
            message_id (int): 该词汇所属消息的唯一标识符。
            tf (int, optional): 该词项在本次操作中的词频（出现次数）。默认为1。

        Returns:
            None
        """
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO bm25_inverted (term, message_id, tf)
                   VALUES (?, ?, ?)
                   ON CONFLICT(term, message_id) DO UPDATE SET tf = tf + ?""",
                (term, message_id, tf, tf),
            )
            self.conn.commit()

    def delete_bm25_terms_by_message(self, message_id: int) -> None:
        """删除某条消息的全部 BM25 倒排词条。

        为什么必须单独有这个动作：``upsert_bm25_term`` 的冲突分支是 ``tf = tf + ?``
        （**累加**），并不替换。所以"重建索引"如果只调 upsert，旧内容独有的词会残留，
        索引就变成"新旧内容的并集" —— 那恰好是 A 项要消灭的东西。

        T06：execute + commit 包入 _write_lock。
        """
        with self._write_lock:
            self.conn.execute("DELETE FROM bm25_inverted WHERE message_id = ?", (message_id,))
            self.conn.commit()

    def get_bm25_posting(self, term: str, exclude_sources: set[str] | None = None) -> list[dict]:
            """获取指定术语的BM25倒排索引列表。

            该函数通过SQLite数据库查询给定术语的倒排索引信息，
            包括出现该术语的消息ID和词频。

            Args:
                term (str): 需要查询的目标术语。
                exclude_sources (set[str] | None): 需要排除的消息来源（如 {"tool"}）。
                    在 SQL 侧与 messages 关联过滤，避免工具日志先占满 top_k 再被滤掉。

            Returns:
                list[dict]: 包含字典的列表，每个字典包含两个字段：
                    - "message_id" (int): 消息ID。
                    - "tf" (int): 该术语在该消息中的词频。
            """
            if exclude_sources:
                placeholders = ",".join("?" for _ in exclude_sources)
                cur = self.conn.execute(
                    "SELECT i.message_id, i.tf FROM bm25_inverted i "
                    "JOIN messages m ON m.id = i.message_id "
                    f"WHERE i.term = ? AND m.source NOT IN ({placeholders})",
                    (term, *sorted(exclude_sources)),
                )
            else:
                # 使用参数化查询在bm25_inverted表中查找匹配的术语记录
                cur = self.conn.execute(
                    "SELECT message_id, tf FROM bm25_inverted WHERE term = ?",
                    (term,),
                )
            # 将查询结果转换为包含message_id和tf字段的字典列表
            return [{"message_id": r[0], "tf": r[1]} for r in cur.fetchall()]

    def get_bm25_stats(self) -> dict:
        """查询并返回 BM25 统计数据。

        通过数据库连接执行 SQL，从 bm25_stats 表中获取索引的统计数据。
        如果表中存在记录（id=1），则返回查询到的数据；否则返回默认值。

        参数:
            self: 类实例，包含数据库连接 self.conn。

        返回:
            dict: 包含统计数据的字典，键包括 'total_docs' 和 'avg_dl'。
                  如果查询无结果，返回 {"total_docs": 0, "avg_dl": 0.0}。
        """
        # 从 bm25_stats 表中查询总文档数和平均文档长度
        cur = self.conn.execute("SELECT total_docs, avg_dl FROM bm25_stats WHERE id = 1")
        row = cur.fetchone()
        if row:
            # 查询到记录，返回包含统计数据的字典
            return {"total_docs": row[0], "avg_dl": row[1]}
        # 未查询到记录，返回默认值字典
        return {"total_docs": 0, "avg_dl": 0.0}

    def update_bm25_stats(self, total_docs: int, avg_dl: float) -> None:
            """
            更新BM25统计信息。

            功能：更新数据库中的BM25统计信息，设置文档总数、平均文档长度，并自动更新修改时间为本地当前时间。

            参数：
                total_docs (int): 文档总数。
                avg_dl (float): 平均文档长度。

            返回值：
                无。
            """
            # T06：execute + commit 包入 _write_lock
            with self._write_lock:
                self.conn.execute(
                    "UPDATE bm25_stats SET total_docs = ?, avg_dl = ?, updated_at = datetime('now', 'localtime') WHERE id = 1",
                    (total_docs, avg_dl),
                )
                self.conn.commit()

    def get_message_length(self, message_id: int) -> int:
        """获取消息的词数（文档长度）"""
        cur = self.conn.execute("SELECT content FROM messages WHERE id = ?", (message_id,))
        row = cur.fetchone()
        return len(row[0]) if row else 0

    # ─── 统计 ───────────────────────────────────────────────

    def get_stats(self) -> dict:
        """
        获取数据库的统计信息。

        参数：
            无（除了self，是类实例引用）

        返回：
            dict: 包含以下键的字典：
                - "messages": 消息总数
                - "earliest": 最早消息的时间戳
                - "latest": 最新消息的时间戳
                - "summaries": 摘要总数
                - "facts": 事实总数
                - "vectors": 向量索引总数
                - "bm25_terms": BM25倒排索引的术语总数
                - "db_size_bytes": 数据库文件大小（字节）
                - "db_size_mb": 数据库文件大小（MB）
        """
        # 从messages表查询消息总数、最早和最新时间戳
        cur1 = self.conn.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM messages")
        row1 = cur1.fetchone()  # 获取查询结果

        # 从summaries表查询摘要总数
        cur2 = self.conn.execute("SELECT COUNT(*) FROM summaries")
        row2 = cur2.fetchone()

        # 从facts表查询事实总数
        cur3 = self.conn.execute("SELECT COUNT(*) FROM facts")
        row3 = cur3.fetchone()

        # 从vector_index表查询向量索引总数
        cur4 = self.conn.execute("SELECT COUNT(*) FROM vector_index")
        row4 = cur4.fetchone()

        # 从bm25_inverted表查询BM25倒排索引的术语总数
        cur5 = self.conn.execute("SELECT COUNT(*) FROM bm25_inverted")
        row5 = cur5.fetchone()

        # 检查数据库文件是否存在，若存在则获取大小，否则设为0
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        # 返回统计信息字典
        return {
            "messages": row1[0],      # 消息总数
            "earliest": row1[1],      # 最早时间戳
            "latest": row1[2],        # 最新时间戳
            "summaries": row2[0],     # 摘要总数
            "facts": row3[0],         # 事实总数
            "vectors": row4[0],       # 向量索引总数
            "bm25_terms": row5[0],    # BM25术语总数
            "db_size_bytes": db_size, # 数据库大小（字节）
            "db_size_mb": round(db_size / 1024 / 1024, 2),  # 转换为MB并保留两位小数
        }

    def query_db_size_history(self) -> list[dict]:
        """返回最近 7 天 DB 大小采样数组

        v5：采样落库（`memory_metrics_daily` 的 `db_size_mb` 指标）。
        旧实现只在内存 dict 里滚动 7 天，后端一重启历史就归零——实测
        `/memory/stats` 的 `db_size_history` 只有 1 条，而健康分的"DB 增长率"
        维度需要 ≥2 条才参与计算（`_health_score.py` 的降级分支），于是这一维
        实际上从来没被算过。

        日表读不到时（老库尚未迁移/只读连接失败）退回内存 dict，行为不劣于旧实现。

        Returns:
            list[dict]: 每条 {date: "YYYY-MM-DD", size_mb: float}，按 date 升序，
                       最少返回当天 1 条采样。
        """
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 当天采样（每次调用都更新当天值，反映最新 DB 大小）
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        size_mb = round(db_size / 1048576, 2)
        self._db_size_history[today_str] = size_mb

        persisted: list[dict] = []
        try:
            from server.memory import metrics as _metrics

            with self._write_lock:
                _metrics.put_metric(self.conn, today_str, "db_size_mb", size_mb)
                self.conn.commit()
            persisted = _metrics.query_metric_history(self.conn, "db_size_mb", days=7)
        except Exception as e:  # noqa: BLE001 - 指标持久化失败不能拖垮 /memory/status
            logger.debug(f"db_size 采样落库失败（退回内存采样）: {e}")

        if len(persisted) >= 2:
            return persisted

        # 滚动保留最近 7 天，删过期采样（内存兜底路径）
        sorted_dates = sorted(self._db_size_history.keys())
        if len(sorted_dates) > 7:
            for old_date in sorted_dates[:-7]:
                del self._db_size_history[old_date]

        # 返回 list[dict]，按 date 升序
        return [
            {"date": d, "size_mb": self._db_size_history[d]}
            for d in sorted(self._db_size_history.keys())
        ]

        # ─── 旧 KV 迁移 ────────────────────────────────────────

    def migrate_kv_memory(self, kv_dir: str) -> int:
        """从旧 KV JSON 文件迁移到 SQLite"""
        import glob
        count = 0
        for fpath in glob.glob(os.path.join(kv_dir, "*.json")):
            key = os.path.splitext(os.path.basename(fpath))[0]
            try:
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
                # 检查是否已迁移
                existing = self.conn.execute(
                    "SELECT id FROM messages WHERE key = ? AND source = 'manual' LIMIT 1",
                    (key,),
                ).fetchone()
                if existing:
                    continue
                content = json.dumps(data, ensure_ascii=False)
                self.insert_message(
                    content=content,
                    source="manual",
                    role="system",
                    key=key,
                )
                # 同时写入 facts 表
                self.extract_facts_from_kv(key, data)
                count += 1
            except Exception as e:
                logger.warning(f"迁移 {fpath} 失败: {e}")
                continue
        return count

    def extract_facts_from_kv(self, key: str, data: dict) -> None:
        """从 KV 数据中提取事实（v3 修复版）

        v2 bug：原实现只把 dict 的标量子字段拆成独立 fact，导致：
        - consumption_contexts/trigger_keywords 这种 list 字段被丢弃
        - 整个 dict 没有作为单条 fact 存入
        - find_consumable_memories 无法匹配通过 memory_set 写入的消费场景

        v3 修复：采用"整体 + 拆分"双写策略：
        1. 整体写入一条 fact（key 不变，value 是整个 dict 的 JSON）
        2. 提取结构化字段（fact_type/occurred_at/consumption_contexts/trigger_keywords）
           到 facts 表的独立列，供 find_consumable_memories 用 SQL 索引过滤

        保留旧的标量子字段拆分行为（向后兼容），但拆分时跳过 v3 结构化字段，
        避免重复写入。
        """
        # v3 结构化字段名集合（这些字段不参与旧的标量拆分）
        V3_STRUCTURED_FIELDS = {
            "fact_type", "occurred_at", "consumption_contexts",
            "trigger_keywords", "mentioned_at",
        }

        # 1. 整体写入一条 fact（key 不变，value 是整个 dict 的 JSON）
        # 这是 v3 修复的核心：把整个 dict 作为一个 fact 存入
        self.upsert_fact(
            key=key,
            value=json.dumps(data, ensure_ascii=False),
            source="manual",
        )

        # 2. 提取 v3 结构化字段到独立列
        # 注意：当 memory_set 端点做深度合并时，get() 返回的 consumption_contexts
        # 已经是 JSON 字符串（从 facts 独立列读取），merged data 中也是 JSON 字符串。
        # 此时直接使用，避免再次 json.dumps 导致双重编码。
        cc = data.get("consumption_contexts")
        if cc is None:
            cc_json = None
        elif isinstance(cc, str):
            # 已是 JSON 字符串（来自 get() 返回值的合并），直接使用
            cc_json = cc
        else:
            cc_json = json.dumps(cc, ensure_ascii=False)

        tk = data.get("trigger_keywords")
        if tk is None:
            tk_json = None
        elif isinstance(tk, str):
            tk_json = tk
        else:
            tk_json = json.dumps(tk, ensure_ascii=False)

        structured = {
            "fact_type": data.get("fact_type"),
            "occurred_at": data.get("occurred_at"),
            "consumption_contexts": cc_json,
            "trigger_keywords": tk_json,
        }
        # 仅更新非 None 字段
        non_none = {k: v for k, v in structured.items() if v is not None}
        if non_none:
            self._update_fact_structured_fields(key, non_none)

        # v4: 移除旧的标量子字段拆分逻辑（原第 3 步）。
        # 原实现把 dict 的每个标量字段拆成 key.field 的独立 fact，
        # 导致每条记忆膨胀成 N+1 条 fact（1 整体 + N 子字段），
        # 维护器 LLM 逐条验证时无法识别冗余，且 find_consumable_memories
        # 已改用 facts 表独立列（consumption_contexts/trigger_keywords）查询，
        # 不依赖子字段拆分。整体写入 + 结构化字段列已覆盖所有读取路径。

    # ─── 内部工具 ───────────────────────────────────────────

    _ALLOWED_TABLES = frozenset({
        "messages", "facts", "summaries", "vector_index", "bm25_inverted",
        "bm25_stats", "schema_info", "memory_meta",
        "evidence_ledger", "search_traces",  # v3 新增
        "context_projections",               # v4 新增（投影留痕，只写不读回 fact 列）
    })

    def _row_to_dict(self, table: str, row: tuple) -> dict:
        """
        将数据库查询返回的一行数据转换为字典。
        该函数通过查询表结构获取列名，并将列名与行数据一一对应，
        返回以列名为键、行数据为值的字典。

        参数:
            table (str): 数据库表名（必须在 _ALLOWED_TABLES 白名单内）。
            row (tuple): 一行数据，作为元组。

        返回:
            dict: 以列名为键、对应数据为值的字典。
        """
        if table not in self._ALLOWED_TABLES:
            raise ValueError(f"非法表名: {table}（不在白名单 {self._ALLOWED_TABLES}）")
        columns = self._table_columns_cache.get(table)
        if columns is None:
            cur = self.conn.execute(f"SELECT * FROM {table} LIMIT 0")
            columns = [desc[0] for desc in cur.description]
            self._table_columns_cache[table] = columns
        return dict(zip(columns, row, strict=False))

    def close(self) -> None:
        """关闭数据库连接，并释放连接资源。

        该方法用于安全地关闭当前对象持有的数据库连接。
        如果连接存在，则执行关闭操作并将连接属性重置为None，避免重复关闭。

        Args:
            无（除了self）。

        Returns:
            None
        """
        if self._conn:  # 检查连接是否存在，避免对None进行操作
            self._conn.close()  # 执行实际的连接关闭操作
            self._conn = None  # 将连接引用重置为None，防止重复关闭并帮助垃圾回收
