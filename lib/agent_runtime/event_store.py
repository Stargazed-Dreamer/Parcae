"""v6-lite EventStore：SQLite 4 表持久化（T02: sessions/messages/events/tool_calls）

设计依据：
- v6-01 §1.1：表 schema（lite 砍到 4 表）
- v6-01 §1.2：events.seq 单 session 内单调递增；状态变更先写 event 再更新 projection
- v6-01 §3：启动恢复（reconciliation）—— T06 实现，T01 只留 status 字段
- v6-lite §3 W1：DB 路径 data/client/agent.db
- v6-lite §4.10：tool_calls 表先写 pending 再执行（副作用前落 durable record）

T01 范围（已完成）：
- sessions / messages / events 三表（含预留字段，只留列不写逻辑）
- SessionRunner 主循环所需的最小 CRUD

T02 范围（新增）：
- tool_calls 表（id/session_id/seq/name/args_json/safety/status/started_at/ended_at/trace_id）
- append_tool_call / update_tool_call_status / load_tool_calls CRUD
- append_tool_result_message（role=tool 消息，关联 tool_call_id）

T04 范围（新增）：
- SQLite WAL 模式（PRAGMA journal_mode=WAL + synchronous=NORMAL，v6-01 §1）
- events 表预留字段已在 T01 schema 中就位（prompt_index/invalidated_seq/parent_trace_id/depth），T04 验证可读写

T06+ 范围（未实现）：
- 启动恢复 reconciliation（T06）

接口为 async（匹配 v6-02 主循环 await store.xxx() 设计），内部 sqlite3 同步执行。
T01/T02 阶段 sqlite 本地文件 I/O 极快，async def 内直接同步调用即可；
真实 I/O 场景（W2+ 接 server）可用 asyncio.to_thread 包装而不改接口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid as _uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lib.agent_runtime.event_domains import (
    EVENT_DOMAIN_LIVE,
    EVENT_DOMAIN_PERSISTENT,
    EVENT_TYPE_DOMAINS,
    REPLAY_DOMAINS,
    domain_for_event,
)
from lib.agent_runtime.types import (
    TOOL_STATUS_PENDING,
    TOOL_STATUS_TERMINAL,
    Event,
    Message,
    Session,
    ToolCall,
    ToolResult,
)

logger = logging.getLogger("parcae.agent.event_store")

# 默认 DB 路径（相对项目根）—— v6-01 §1
DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "client" / "agent.db")

# 连接池默认大小（ADR-0021：每个 worker 独立 connection，WAL 允许并发读 + 串行写）
DEFAULT_POOL_SIZE = 8
# busy_timeout（ms）：并发写等待而非立即抛 SQLITE_BUSY
_BUSY_TIMEOUT_MS = 5000

# schema 版本（迁移用：T01=1，T02=2 加 tool_calls 表，T02.1=3 加 messages.tool_calls_json，
# v6-lite-streaming-gui T01=4 加 messages.thinking_json，
# chat-panel-v2 T01=5 加 sessions.group_name/pinned + messages.model，
# chat-engine-safety-fixes D5=6 加 usage 表，
# chat-engine-safety-fixes E3=7 加 client_message_ids 表，
# chat-engine-safety-fixes E6=8 加 runner_state 表，
# chat-engine-safety-fixes E7=9 加 compactions 表）
_SCHEMA_VERSION = 9

# 账本格式版本（X8）：与 _SCHEMA_VERSION 正交——前者是迁移分派依据，后者只用于建表。
# 现存所有 schema v1..v9 库一律视为 format 1；format 2 = events.domain + 三张投影表。
LEDGER_BASELINE_FORMAT = 1
LEDGER_FORMAT_VERSION = 2
_LEDGER_FORMAT_KEY = "format_version"


class LedgerFormatError(RuntimeError):
    """账本格式不可用：版本号畸形、迁移链不完整或迁移步骤失败。"""


class LedgerFormatTooNew(LedgerFormatError):
    """账本由更新版本的代码写过，当前代码拒绝降级打开。"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_ledger_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1→v2：事件域列 + 域索引 + 三张读侧投影表（只增不删）。

    注意：不能在此用 executescript（它会先 COMMIT，破坏每代单事务的回滚语义）。
    """
    if "domain" not in _table_columns(conn, "events"):
        conn.execute(
            "ALTER TABLE events ADD COLUMN domain TEXT NOT NULL DEFAULT 'persistent'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_session_domain_seq "
        "ON events(session_id, domain, seq)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proj_stats (
            session_id TEXT PRIMARY KEY,
            message_count INTEGER NOT NULL DEFAULT 0,
            event_count INTEGER NOT NULL DEFAULT 0,
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            last_event_at REAL,
            status TEXT,
            updated_seq INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proj_turn_outline (
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            step_count INTEGER NOT NULL DEFAULT 0,
            tool_count INTEGER NOT NULL DEFAULT 0,
            status TEXT,
            PRIMARY KEY (session_id, turn_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proj_title (
            session_id TEXT PRIMARY KEY,
            title TEXT,
            source_seq INTEGER,
            updated_at REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
        """
    )


# 有序迁移表：key = 起始格式版本；启动时校验 [baseline, target) 相邻完备。
_LEDGER_MIGRATIONS: dict[int, tuple[str, Callable[[sqlite3.Connection], None]]] = {
    1: ("v1->v2: events.domain + proj_stats/proj_turn_outline/proj_title", _migrate_ledger_v1_to_v2),
}


_SCHEMA_SQL = """
-- sessions 表（v6-01 §1.1，含 v6 预留字段，T01 只留列不写逻辑）
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    mode TEXT DEFAULT 'dialogue',
    status TEXT DEFAULT 'idle',
    created_at REAL,
    updated_at REAL,
    -- v6 预留字段（T04+ 补逻辑，T01 只留列）
    prompt_index INTEGER DEFAULT 0,
    last_compaction_prompt_index INTEGER,
    -- chat-panel-v2 T01 新增（D10：分组 + 置顶）
    group_name TEXT,           -- NULL = 未分组
    pinned INTEGER DEFAULT 0   -- 0=否，1=是
);

-- messages 表（v6-01 §1.1，T02 加 tool_calls_json 列）
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    tool_call_id TEXT,
    source TEXT DEFAULT 'user',
    visible INTEGER DEFAULT 1,
    created_at REAL,
    tool_calls_json TEXT,  -- T02 新增：assistant 携带的 tool_calls（OpenAI 格式 list[dict]）
    thinking_json TEXT,    -- v6-lite-streaming-gui T01 新增：LLM thinking 内容（streaming 合并后落库）
    model TEXT,            -- chat-panel-v2 T01 新增：assistant 消息的模型名（D31，气泡下方小字显示）
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);

-- events 表（append-only，v6-01 §1.1，含 v6 预留字段）
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT,
    trace_id TEXT,
    -- v6 预留字段（T04+ 补逻辑，T01 只留列）
    prompt_index INTEGER,
    invalidated_seq INTEGER,
    parent_trace_id TEXT,
    depth INTEGER DEFAULT 0,
    created_at REAL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);

-- tool_calls 表（T02 新增，v6-01 §1.1 + v6-lite §4.10）
-- 副作用前先落 durable record：tool_calls 表先写 pending 再执行
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,            -- 幂等键（来自 LLM tool_call.id 或自生成）
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,           -- 与 assistant 消息同 seq（一轮可含多个 tool_call）
    name TEXT NOT NULL,
    args_json TEXT,
    safety TEXT DEFAULT 'read_only',
    status TEXT DEFAULT 'pending',  -- pending/running/completed/failed/interrupted/unknown
                                     -- 后四者（含 unknown）为终态，禁止原地回写
    started_at REAL,
    ended_at REAL,
    trace_id TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session_seq ON tool_calls(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_tool_calls_status ON tool_calls(status);

-- usage 表（chat-engine-safety-fixes D5 新增，spec D20）
-- 每次 LLM 调用的 token usage 持久化（失败/断流也落库，跨 session SUM 全局统计）
CREATE TABLE IF NOT EXISTS usage (
    id TEXT PRIMARY KEY,             -- uuid
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,            -- 与 LLM 调用轮次对应
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    model TEXT,                      -- 调用的模型名
    created_at REAL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_usage_session_seq ON usage(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_usage_session_id ON usage(session_id);

-- client_message_ids 表（chat-engine-safety-fixes E3 新增）
-- client_message_id 幂等去重：facade.start() 传入 client_message_id 时，
-- 先查此表，已存在则返回上次结果，不重复触发 LLM 调用。
CREATE TABLE IF NOT EXISTS client_message_ids (
    client_message_id TEXT PRIMARY KEY,   -- 客户端生成的幂等键
    session_id TEXT NOT NULL,             -- 关联的 session
    outcome_status TEXT,                  -- RunOutcome.status (completed/interrupted/failed)
    outcome_stop_reason TEXT,             -- RunOutcome.stop_reason
    created_at REAL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- E6（spec D23）：runner 关键状态持久化（每 session 一行，最新状态）
-- 用于审计 / 重启恢复（reconciler 启动时读取，避免 has_attempted_reactive_compact 丢失）
CREATE TABLE IF NOT EXISTS runner_state (
    session_id TEXT PRIMARY KEY,                       -- 关联 session
    iterations INTEGER NOT NULL DEFAULT 0,             -- runner 已执行的迭代轮数
    has_attempted_reactive_compact INTEGER NOT NULL DEFAULT 0,  -- bool: 是否已尝试过 L2 压缩重试
    total_tokens INTEGER NOT NULL DEFAULT 0,           -- 累计 token（与 usage 表 SUM 可互验）
    wall_clock_exceeded INTEGER NOT NULL DEFAULT 0,    -- bool: 最后一次 LLM 响应是否触发 wall_clock 超时
    last_updated_at REAL NOT NULL,                     -- 最后更新时间戳
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- E7（spec D24）：compactions 表持久化压缩历史
-- 每次 reactive_compact 成功后写一条记录，用于审计（统计压缩命中率、回溯压缩内容）
CREATE TABLE IF NOT EXISTS compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,              -- 自增主键
    session_id TEXT NOT NULL,                          -- 关联 session
    source_seq INTEGER NOT NULL,                       -- 触发压缩时的 runner 迭代轮数
    summary TEXT,                                      -- LLM 生成的摘要文本
    recent_json TEXT,                                  -- 压缩后保留的 tail messages（JSON）
    prompt_hash TEXT,                                  -- 压缩前 messages 的 hash（去重/审计）
    created_at REAL NOT NULL,                          -- 创建时间戳
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_compactions_session_id ON compactions(session_id);

-- schema 版本记录
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class EventStore:
    """SQLite 持久化的对话事件存储。

    生命周期：
        store = EventStore(db_path)
        store.init()              # 建表（幂等）+ WAL + 连接池
        store.create_session(...) # 创建会话
        await store.append_message(...)
        store.close()

    ADR-0021（连接池 + WAL）：每个 worker 独立 connection，WAL 允许并发读 + 串行写。
    WAL 不可用时（如网络盘）fallback 到 DELETE 模式 + _write_lock 串行化写。
    """

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        *,
        pool_size: int = DEFAULT_POOL_SIZE,
    ):
        self._db_path = db_path
        self._pool_size = max(1, pool_size)
        # init_conn：用于 schema 初始化 + 迁移 + conn property 兼容外部只读访问
        # 不参与池化，但与池连接共享同一 DB 文件（WAL 模式下并发安全）
        self._init_conn: sqlite3.Connection | None = None
        # 连接池：每个 _run_sync 借一个连接，执行完归还
        self._pool: queue.Queue[sqlite3.Connection] | None = None
        # 线程局部：_run_sync 上下文内 self.conn 自动指向池中借来的连接
        self._thread_conn = threading.local()
        # 实际生效的 journal_mode（init 后更新为 'wal' 或 'delete' 等）
        self._journal_mode: str = "delete"
        # 账本格式版本（init 成功后为 LEDGER_FORMAT_VERSION）
        self._format_version: int | None = None
        # WAL 不可用时的写锁 fallback（WAL 可用时为 None）
        self._write_lock: threading.Lock | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init(self) -> None:
        """初始化数据库连接 + 建表（幂等）+ 迁移 + WAL 模式 + 连接池。

        ADR-0021：
        - 启用 WAL（PRAGMA journal_mode=WAL），写入不阻塞读、崩溃后 journal 自动恢复
        - WAL 不可用时（如网络盘/in-memory DB）fallback 到 DELETE 模式 + _write_lock 串行化写
        - 创建连接池（DEFAULT_POOL_SIZE 个 worker connection），每个 _run_sync 借一个
        - 所有连接 check_same_thread=False + busy_timeout=5s（并发写等待而非抛 BUSY）

        迁移策略：CREATE TABLE IF NOT EXISTS 建新表；对已存在的旧表用 ALTER TABLE ADD COLUMN 补列。
        """
        # 确保目录存在
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            Path(db_dir).mkdir(parents=True, exist_ok=True)

        # init_conn：用于 schema 初始化 + 迁移 + conn property 兼容外部只读访问
        self._init_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._init_conn.row_factory = sqlite3.Row

        try:
            self._init_conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")

            # 先判账本代数再做任何写操作：更高版本/畸形版本必须在改动 DB 之前拒绝。
            existing_format = self._read_existing_format()
            if existing_format > LEDGER_FORMAT_VERSION:
                raise LedgerFormatTooNew(
                    f"ledger {self._db_path} has format_version={existing_format}, "
                    f"but this code supports at most {LEDGER_FORMAT_VERSION}; refusing to downgrade"
                )
            self._validate_migration_chain(existing_format)

            self._init_journal_mode()

            # synchronous=NORMAL：WAL/DELETE 模式下兼顾安全和性能（v6-01 §1 推荐）
            self._init_conn.execute("PRAGMA synchronous=NORMAL")

            # 建表 + 旧版补列（_SCHEMA_VERSION 只负责把表建到当前形状）
            self._init_conn.executescript(_SCHEMA_SQL)
            self._migrate_messages_tool_calls_json()
            self._migrate_messages_thinking_json()
            self._migrate_sessions_mode()
            self._migrate_sessions_group_name_pinned()
            self._migrate_messages_model()
            self._init_conn.execute(
                "INSERT OR REPLACE INTO schema_info(key, value) VALUES('version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            # 现存库（含新库）先落基线代数，再由迁移链逐代推进
            self._stamp_format_version(existing_format)
            self._init_conn.commit()

            self._format_version = self._run_ledger_migrations(existing_format)
            # v1→v2 的 domain 列带 DEFAULT 'persistent'，旧库里的 streaming 行需要按
            # 注册表回填为 live，否则历史增量会被默认回放重新带出来（只改新列，不动旧列）
            self._backfill_event_domains()

            # 创建连接池：每个 worker 独立 connection，配置 check_same_thread=False + busy_timeout
            # WAL 模式持久化在 DB 文件中，池连接自动继承，无需重设 journal_mode
            self._pool = queue.Queue(maxsize=self._pool_size)
            for _ in range(self._pool_size):
                self._pool.put(self._new_pool_connection())
        except BaseException:
            # 初始化失败不许留下半开连接（Windows 下会锁住 DB 文件）
            self._init_conn.close()
            self._init_conn = None
            raise

        logger.info(
            "EventStore initialized: %s (schema v%d, format v%d, journal=%s, pool_size=%d)",
            self._db_path, _SCHEMA_VERSION, self._format_version,
            self._journal_mode, self._pool_size,
        )

    def _init_journal_mode(self) -> None:
        """WAL 不可用时 fallback 到 DELETE 模式 + 写锁。"""
        assert self._init_conn is not None
        # PRAGMA journal_mode=WAL 返回实际生效的模式（'wal' 或 'delete'）；
        # in-memory DB 返回 'memory'，网络盘可能返回 'delete' 或抛 OperationalError
        try:
            cur = self._init_conn.execute("PRAGMA journal_mode=WAL")
            row = cur.fetchone()
            mode = str(row[0]).lower() if row else "delete"
        except sqlite3.Error as e:
            # 网络盘等场景 PRAGMA 可能抛 OperationalError
            logger.warning("PRAGMA journal_mode=WAL failed: %s; fallback to DELETE", e)
            mode = "delete"

        if mode == "wal":
            self._journal_mode = "wal"
            # ADR-0021 修正：WAL 模式下也用 _write_lock 串行化进程内写操作。
            # 原设计靠 BEGIN IMMEDIATE + busy_timeout 串行化，但 Python sqlite3
            # 多线程（asyncio.to_thread + 连接池）下 BEGIN IMMEDIATE 锁等待不可靠，
            # 多线程同时 BEGIN IMMEDIATE 会立即抛 "database is locked" 而非等待。
            # 修复：统一用 _write_lock 串行化进程内写，WAL 仍保留作为并发读优化。
            self._write_lock = threading.Lock()
        else:
            # fallback：DELETE 模式下读写互斥，必须串行化所有写操作
            self._journal_mode = mode if mode and mode != "memory" else "delete"
            self._write_lock = threading.Lock()
            logger.warning(
                "WAL not supported (got %r), fallback to %s mode with _write_lock",
                mode, self._journal_mode,
            )

    def _read_existing_format(self) -> int:
        """判定 DB 当前账本代数；判定不了就拒绝，不许猜。

        - 无 schema_info 表：全新库 → 基线 1
        - 有 format_version：解析整数
        - 无 format_version 但有旧版 version（v1..v9）：按基线 1 处理
        - 两者都无但三张账本锚表齐全（早期 init 中断的库）：按基线 1 处理
        - 两者都无且不是账本：无法判定 → LedgerFormatError
        """
        assert self._init_conn is not None
        has_schema_info = self._init_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_info'"
        ).fetchone()
        if has_schema_info is None:
            return LEDGER_BASELINE_FORMAT

        rows = {
            str(r[0]): r[1]
            for r in self._init_conn.execute("SELECT key, value FROM schema_info").fetchall()
        }
        raw_format = rows.get(_LEDGER_FORMAT_KEY)
        if raw_format is not None:
            try:
                value = int(str(raw_format).strip())
            except (TypeError, ValueError) as e:
                raise LedgerFormatError(
                    f"ledger {self._db_path} has malformed format_version={raw_format!r}"
                ) from e
            if value < LEDGER_BASELINE_FORMAT:
                raise LedgerFormatError(
                    f"ledger {self._db_path} has unsupported format_version={value}"
                )
            return value

        raw_schema = rows.get("version")
        if raw_schema is None:
            ledger_tables = {
                str(r[0])
                for r in self._init_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if {"sessions", "messages", "events"} <= ledger_tables:
                return LEDGER_BASELINE_FORMAT
            raise LedgerFormatError(
                f"ledger {self._db_path} has schema_info but neither format_version nor "
                "legacy version; cannot determine baseline"
            )
        try:
            schema_version = int(str(raw_schema).strip())
        except (TypeError, ValueError) as e:
            raise LedgerFormatError(
                f"ledger {self._db_path} has malformed legacy version={raw_schema!r}"
            ) from e
        if not LEDGER_BASELINE_FORMAT <= schema_version <= _SCHEMA_VERSION:
            raise LedgerFormatError(
                f"ledger {self._db_path} has unknown legacy version={schema_version}"
            )
        return LEDGER_BASELINE_FORMAT

    @staticmethod
    def _validate_migration_chain(current: int) -> None:
        """校验 [current, LEDGER_FORMAT_VERSION) 每一代都有步骤，且注册表无越界项。"""
        for version in range(current, LEDGER_FORMAT_VERSION):
            if version not in _LEDGER_MIGRATIONS:
                raise LedgerFormatError(
                    f"ledger migration chain incomplete: missing v{version}->v{version + 1} "
                    f"(code target v{LEDGER_FORMAT_VERSION})"
                )
        for version in _LEDGER_MIGRATIONS:
            if not LEDGER_BASELINE_FORMAT <= version < LEDGER_FORMAT_VERSION:
                raise LedgerFormatError(
                    f"ledger migration registry has out-of-range step v{version} "
                    f"(baseline v{LEDGER_BASELINE_FORMAT}, target v{LEDGER_FORMAT_VERSION})"
                )

    def _backfill_event_domains(self) -> None:
        """把旧库中按 DEFAULT 落到 persistent 的非 persistent 类型改回注册表域。"""
        assert self._init_conn is not None
        repaired = 0
        for event_type, domain in EVENT_TYPE_DOMAINS.items():
            if domain == EVENT_DOMAIN_PERSISTENT:
                continue
            cur = self._init_conn.execute(
                "UPDATE events SET domain = ? WHERE type = ? AND domain = ?",
                (domain, event_type, EVENT_DOMAIN_PERSISTENT),
            )
            repaired += int(cur.rowcount or 0)
        self._init_conn.commit()
        if repaired:
            logger.info("Backfilled event domains for %d legacy rows", repaired)

    def _stamp_format_version(self, version: int) -> None:
        assert self._init_conn is not None
        self._init_conn.execute(
            "INSERT OR REPLACE INTO schema_info(key, value) VALUES(?, ?)",
            (_LEDGER_FORMAT_KEY, str(version)),
        )

    def _run_ledger_migrations(self, current: int) -> int:
        """逐代执行迁移；每代单独事务，失败回滚且版本号不推进。"""
        assert self._init_conn is not None
        version = current
        while version < LEDGER_FORMAT_VERSION:
            label, step = _LEDGER_MIGRATIONS[version]
            logger.info("Ledger migration %s: starting (format v%d -> v%d)", label, version, version + 1)
            try:
                self._init_conn.execute("BEGIN IMMEDIATE")
                step(self._init_conn)
                self._stamp_format_version(version + 1)
                self._init_conn.commit()
            except Exception as e:
                self._init_conn.rollback()
                raise LedgerFormatError(
                    f"ledger migration {label} failed on {self._db_path}: {e}; "
                    f"format stays at v{version}"
                ) from e
            version += 1
            logger.info("Ledger migration %s: done (format v%d)", label, version)
        return version

    # ------------------------------------------------------------------
    # X9 三投影（proj_stats / proj_turn_outline / proj_title）
    # 投影只是缓存；append_event / append_usage 在同一事务内同步更新，
    # rebuild_projections 清空后按账本重放，两边走同一个 apply 函数。
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_stats_row(conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO proj_stats("
            "session_id, message_count, event_count, tool_call_count, total_tokens, "
            "last_event_at, status, updated_seq) "
            "VALUES(?, 0, 0, 0, 0, NULL, 'idle', 0)",
            (session_id,),
        )

    @staticmethod
    def _finalize_active_turn(
        conn: sqlite3.Connection,
        session_id: str,
        ended_at: float,
        status: str,
        end_reason: str | None = None,
    ) -> None:
        conn.execute(
            "UPDATE proj_turn_outline SET ended_at = ?, status = ?, "
            "end_reason = COALESCE(?, end_reason) "
            "WHERE session_id = ? AND ended_at IS NULL",
            (ended_at, status, end_reason, session_id),
        )

    def _apply_projection_event(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        event_type: str,
        payload: dict,
        seq: int,
        created_at: float,
        domain: str,
    ) -> None:
        """把一条事件同步进三投影；live 事件不参与统计/轮次。"""
        if event_type == "session_created":
            self._ensure_stats_row(conn, session_id)
            conn.execute(
                "INSERT OR REPLACE INTO proj_title(session_id, title, source_seq, updated_at) "
                "VALUES(?, ?, ?, ?)",
                (session_id, payload.get("title") or "", seq, created_at),
            )
        if domain == EVENT_DOMAIN_LIVE:
            return

        self._ensure_stats_row(conn, session_id)
        conn.execute(
            "UPDATE proj_stats SET event_count = event_count + 1, "
            "last_event_at = ?, updated_seq = ? WHERE session_id = ?",
            (created_at, seq, session_id),
        )

        if event_type.endswith("_message_appended"):
            conn.execute(
                "UPDATE proj_stats SET message_count = message_count + 1 WHERE session_id = ?",
                (session_id,),
            )
            if event_type == "user_message_appended" and payload.get("source") != "steer":
                self._finalize_active_turn(conn, session_id, created_at, "completed")
                turn_id = str(payload.get("message_id") or f"seq-{seq}")
                conn.execute(
                    "INSERT OR REPLACE INTO proj_turn_outline("
                    "session_id, turn_id, started_at, ended_at, end_reason, "
                    "step_count, tool_count, status) "
                    "VALUES(?, ?, ?, NULL, NULL, 0, 0, 'running')",
                    (session_id, turn_id, created_at),
                )
            elif event_type == "assistant_message_appended":
                conn.execute(
                    "UPDATE proj_turn_outline SET step_count = step_count + 1 "
                    "WHERE session_id = ? AND ended_at IS NULL",
                    (session_id,),
                )
        elif event_type == "tool_call_pending":
            conn.execute(
                "UPDATE proj_stats SET tool_call_count = tool_call_count + 1 "
                "WHERE session_id = ?",
                (session_id,),
            )
            conn.execute(
                "UPDATE proj_turn_outline SET tool_count = tool_count + 1 "
                "WHERE session_id = ? AND ended_at IS NULL",
                (session_id,),
            )
        elif event_type == "session_status_changed":
            status = str(payload.get("status") or "")
            if status:
                conn.execute(
                    "UPDATE proj_stats SET status = ? WHERE session_id = ?",
                    (status, session_id),
                )
            if status in ("idle", "completed", "interrupted", "failed"):
                self._finalize_active_turn(
                    conn, session_id, created_at,
                    "completed" if status == "idle" else status, status,
                )
        elif event_type == "session_title_changed":
            conn.execute(
                "INSERT OR REPLACE INTO proj_title(session_id, title, source_seq, updated_at) "
                "VALUES(?, ?, ?, ?)",
                (session_id, payload.get("title") or "", seq, created_at),
            )
        elif event_type == "session_finalized":
            self._finalize_active_turn(
                conn, session_id, created_at, "completed", "completed"
            )
        elif event_type == "transition":
            if payload.get("reason") == "user_interrupted":
                self._finalize_active_turn(
                    conn, session_id, created_at, "interrupted", "user_interrupted"
                )

    async def rebuild_projections(self, session_id: str | None = None) -> dict[str, int]:
        """清空并从账本重建投影（persistent + capability；live 不参与）。

        幂等：重复执行结果一致。返回重建的 session 数与事件数。
        """
        def _sync() -> dict[str, int]:
            conn = self.conn
            with conn:
                if session_id is not None:
                    exists = conn.execute(
                        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()
                    targets = [session_id] if exists else []
                    conn.execute("DELETE FROM proj_stats WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM proj_turn_outline WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM proj_title WHERE session_id = ?", (session_id,))
                else:
                    targets = [
                        r["id"] for r in conn.execute("SELECT id FROM sessions").fetchall()
                    ]
                    conn.execute("DELETE FROM proj_stats")
                    conn.execute("DELETE FROM proj_turn_outline")
                    conn.execute("DELETE FROM proj_title")

                event_count = 0
                placeholders = ",".join("?" for _ in REPLAY_DOMAINS)
                for sid in targets:
                    rows = conn.execute(
                        f"SELECT seq, type, domain, payload_json, created_at FROM events "
                        f"WHERE session_id = ? AND domain IN ({placeholders}) ORDER BY seq ASC",
                        (sid, *REPLAY_DOMAINS),
                    ).fetchall()
                    for r in rows:
                        self._apply_projection_event(
                            conn, sid, r["type"],
                            json.loads(r["payload_json"]) if r["payload_json"] else {},
                            r["seq"], r["created_at"], r["domain"],
                        )
                        event_count += 1

                    srow = conn.execute(
                        "SELECT title, status, updated_at FROM sessions WHERE id = ?", (sid,)
                    ).fetchone()
                    if srow is not None:
                        # 分支会话等没有 session_created 事件的会话也要有投影行
                        conn.execute(
                            "INSERT OR IGNORE INTO proj_stats("
                            "session_id, message_count, event_count, tool_call_count, total_tokens, "
                            "last_event_at, status, updated_seq) "
                            "VALUES(?, 0, 0, 0, 0, ?, ?, 0)",
                            (sid, srow["updated_at"], srow["status"] or "idle"),
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO proj_title(session_id, title, source_seq, updated_at) "
                            "VALUES(?, ?, NULL, ?)",
                            (sid, srow["title"] or "", srow["updated_at"] or 0.0),
                        )
                    # message_count 以 messages 表为准：分支复制、压缩替换、单条删除
                    # 都不写逐条事件，只有当前行数能给出正确值
                    conn.execute(
                        "UPDATE proj_stats SET message_count = ("
                        "SELECT COUNT(*) FROM messages WHERE session_id = ?"
                        ") WHERE session_id = ?",
                        (sid, sid),
                    )
                    conn.execute(
                        "UPDATE proj_stats SET total_tokens = ("
                        "SELECT COALESCE(SUM(total_tokens), 0) FROM usage WHERE session_id = ?"
                        ") WHERE session_id = ?",
                        (sid, sid),
                    )
            return {"sessions": len(targets), "events": event_count}

        return await self._run_write_sync(_sync)

    def _new_pool_connection(self) -> sqlite3.Connection:
        """创建一个池连接（与 init_conn 共享 DB 文件，独立连接对象）。"""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # synchronous + busy_timeout 需每个连接单独设
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _migrate_messages_tool_calls_json(self) -> None:
        """T02.1 迁移：messages 表加 tool_calls_json 列（若不存在）。

        旧 DB（schema v1/v2）的 messages 表没有此列，需 ALTER TABLE ADD COLUMN。
        新 DB 在 _SCHEMA_SQL 中已含此列，此方法 no-op。
        """
        # 初始化不变量：migrate 仅在 init() 中 _init_conn 赋值后调用
        assert self._init_conn is not None
        cols = {r[1] for r in self._init_conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "tool_calls_json" not in cols:
            self._init_conn.execute("ALTER TABLE messages ADD COLUMN tool_calls_json TEXT")
            logger.info("Migrated messages table: added tool_calls_json column")

    def _migrate_messages_thinking_json(self) -> None:
        """v6-lite-streaming-gui T01 迁移：messages 表加 thinking_json 列（若不存在）。

        旧 DB（schema v3）的 messages 表没有此列，需 ALTER TABLE ADD COLUMN。
        新 DB 在 _SCHEMA_SQL 中已含此列，此方法 no-op。
        """
        # 初始化不变量：migrate 仅在 init() 中 _init_conn 赋值后调用
        assert self._init_conn is not None
        cols = {r[1] for r in self._init_conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "thinking_json" not in cols:
            self._init_conn.execute("ALTER TABLE messages ADD COLUMN thinking_json TEXT")
            logger.info("Migrated messages table: added thinking_json column")

    def _migrate_sessions_mode(self) -> None:
        """headless-agent-session Ticket 01 迁移：sessions 表加 mode 列（若不存在）。

        旧 DB（v6-01 之前）的 sessions 表没有此列，需 ALTER TABLE ADD COLUMN，
        已有行自动获得 DEFAULT 'dialogue'。新 DB 在 _SCHEMA_SQL 中已含此列，此方法 no-op。

        headless 主会话写 mode="headless"，judge 会话写 mode="headless_judge"，
        普通 chat 会话保持默认 mode="dialogue"。
        """
        # 初始化不变量：migrate 仅在 init() 中 _init_conn 赋值后调用
        assert self._init_conn is not None
        cols = {r[1] for r in self._init_conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "mode" not in cols:
            self._init_conn.execute("ALTER TABLE sessions ADD COLUMN mode TEXT DEFAULT 'dialogue'")
            logger.info("Migrated sessions table: added mode column (default 'dialogue')")

    def _migrate_sessions_group_name_pinned(self) -> None:
        """chat-panel-v2 T01 迁移：sessions 表加 group_name + pinned 列（若不存在）。

        旧 DB（schema v4 及之前）的 sessions 表没有这两列，需 ALTER TABLE ADD COLUMN。
        - group_name TEXT（NULL = 未分组）
        - pinned INTEGER DEFAULT 0（0=否，1=是，已有行自动取默认值 0）
        新 DB 在 _SCHEMA_SQL 中已含这两列，此方法 no-op。
        """
        # 初始化不变量：migrate 仅在 init() 中 _init_conn 赋值后调用
        assert self._init_conn is not None
        cols = {r[1] for r in self._init_conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "group_name" not in cols:
            self._init_conn.execute("ALTER TABLE sessions ADD COLUMN group_name TEXT")
            logger.info("Migrated sessions table: added group_name column")
        if "pinned" not in cols:
            self._init_conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0")
            logger.info("Migrated sessions table: added pinned column (default 0)")

    def _migrate_messages_model(self) -> None:
        """chat-panel-v2 T01 迁移：messages 表加 model 列（若不存在）。

        旧 DB（schema v4 及之前）的 messages 表没有此列，需 ALTER TABLE ADD COLUMN。
        新 DB 在 _SCHEMA_SQL 中已含此列，此方法 no-op。
        """
        # 初始化不变量：migrate 仅在 init() 中 _init_conn 赋值后调用
        assert self._init_conn is not None
        cols = {r[1] for r in self._init_conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "model" not in cols:
            self._init_conn.execute("ALTER TABLE messages ADD COLUMN model TEXT")
            logger.info("Migrated messages table: added model column")

    def close(self) -> None:
        """关闭连接池 + init_conn。幂等（多次调用安全）。"""
        # 关闭池中所有连接
        if self._pool is not None:
            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except queue.Empty:
                    break
                except sqlite3.Error:
                    pass
            self._pool = None
        # 关闭 init_conn
        if self._init_conn is not None:
            self._init_conn.close()
            self._init_conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程绑定的连接（_run_sync 上下文内）或 init_conn（外部只读访问）。

        - _run_sync 内：返回 thread-local 绑定的池连接（业务方法内 self.conn 自动用池连接）
        - 外部直接访问（如 reconciler / 测试断言）：返回 init_conn
        - 兼容 C7 旧 API：reconciler.py 用 store.conn.execute() 只读查询 events 表
        """
        # 优先返回线程局部连接（_run_sync 上下文）
        tc = getattr(self._thread_conn, "conn", None)
        if tc is not None:
            return tc
        if self._init_conn is None:
            raise RuntimeError("EventStore not initialized; call init() first")
        return self._init_conn

    @property
    def journal_mode(self) -> str:
        """实际生效的 journal_mode（'wal' / 'delete' 等，init 后可用）。"""
        return self._journal_mode

    @property
    def format_version(self) -> int:
        """账本格式版本（init 成功后可用）。"""
        if self._format_version is None:
            raise RuntimeError("EventStore not initialized; call init() first")
        return self._format_version

    @property
    def pool_size(self) -> int:
        """连接池大小（init 后可用）。"""
        return self._pool_size

    # ------------------------------------------------------------------
    # ADR-0021：连接池借用 + asyncio.to_thread 包装
    # ------------------------------------------------------------------

    @contextmanager
    def _borrow_conn(self):
        """从池中借一个连接，with 块结束自动归还。

        借用期间通过 thread-local 把连接绑定到当前线程，使 self.conn 自动指向该连接。
        业务方法内的 self.conn.execute() / self.conn.commit() 透明地用池连接。
        """
        if self._pool is None:
            raise RuntimeError("EventStore not initialized; call init() first")
        conn = self._pool.get()  # 阻塞等待可用连接
        # 设置线程局部连接（保存前值以便嵌套调用）
        prev = getattr(self._thread_conn, "conn", None)
        self._thread_conn.conn = conn
        try:
            yield conn
        finally:
            # 恢复前值（支持嵌套 _borrow_conn）
            self._thread_conn.conn = prev
            self._pool.put(conn)  # 归还

    async def _run_sync(self, fn, *args, **kwargs):
        """读操作：从池中借连接执行，无锁并发（WAL 模式下读不阻塞写）。

        ADR-0021：
        - asyncio.to_thread：避免阻塞 event loop
        - _borrow_conn：从池中借一个 connection，业务方法内 self.conn 透明使用
        - WAL 模式：直接执行（读不阻塞写、写不阻塞读）
        - DELETE 模式（fallback）：用 _write_lock 串行化所有操作（读+写互斥）

        check_same_thread=False 允许池连接跨线程使用（asyncio.to_thread 线程池）；
        每次调用借独立连接，避免 'bad parameter or other API misuse'。

        注意：本方法不保证 read-modify-write 原子性。写操作（含 SELECT MAX(seq)+1
        后 INSERT 的 read-modify-write 模式）必须用 _run_write_sync 串行化。
        """
        def _pool_exec():
            # 初始化不变量：init() 后 _write_lock 已赋值（WAL/DELETE 均非 None）
            assert self._write_lock is not None
            with self._borrow_conn():
                # WAL 模式：直接执行（读不阻塞写，并发读无锁）
                # DELETE 模式：_write_lock 串行化所有操作（读+写互斥）
                if self._journal_mode != "wal":
                    with self._write_lock:
                        return fn(*args, **kwargs)
                return fn(*args, **kwargs)
        return await asyncio.to_thread(_pool_exec)

    async def _run_write_sync(self, fn, *args, **kwargs):
        """写操作：_write_lock 串行化进程内 read-modify-write。

        ADR-0021（修正后）：
        - WAL + DELETE 模式统一用 _write_lock 串行化进程内写操作
        - 原设计用 BEGIN IMMEDIATE + busy_timeout 串行化，但 Python sqlite3
          多线程下 BEGIN IMMEDIATE 锁等待不可靠，多线程同时 BEGIN IMMEDIATE
          立即抛 "database is locked" 而非等待 busy_timeout
        - WAL 仍保留作为并发读优化（读不阻塞写、写不阻塞读）
        - 跨进程保护靠 SQLite 内置文件锁（WAL 模式下多进程写串行）
        """
        def _pool_exec():
            # 初始化不变量：init() 后 _write_lock 已赋值
            assert self._write_lock is not None
            with self._borrow_conn(), self._write_lock:
                # _write_lock 串行化进程内写操作（WAL + DELETE 都需要）。
                # ADR-0021 修正：原设计用 BEGIN IMMEDIATE + busy_timeout 串行化，
                # 但 Python sqlite3 多线程（asyncio.to_thread + 连接池）下不可靠，
                # 多线程同时 BEGIN IMMEDIATE 立即抛 "database is locked" 而非等待。
                # 修复：统一用 _write_lock 串行化进程内写，WAL 仍保留作为并发读优化。
                return fn(*args, **kwargs)
        return await asyncio.to_thread(_pool_exec)

    # ------------------------------------------------------------------
    # sessions 表 CRUD
    # ------------------------------------------------------------------

    async def create_session(
        self,
        session_id: str | None = None,
        title: str = "",
        mode: str = "dialogue",
    ) -> Session:
        """创建会话并写 session_created event。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        sid = session_id or str(_uuid.uuid4())
        now = time.time()
        def _sync():
            self.conn.execute(
                "INSERT INTO sessions(id, title, mode, status, created_at, updated_at) "
                "VALUES(?, ?, ?, 'idle', ?, ?)",
                (sid, title, mode, now, now),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)
        await self.append_event(sid, "session_created", {"title": title, "mode": mode})
        return Session(
            id=sid, title=title, mode=mode, status="idle",
            created_at=now, updated_at=now,
        )

    async def get_session(self, session_id: str) -> Session | None:
        """C7（spec D11）：DB 操作包装到 asyncio.to_thread。"""
        def _sync():
            return self.conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        row = await self._run_sync(_sync)
        if row is None:
            return None
        return Session(
            id=row["id"], title=row["title"] or "", mode=row["mode"],
            status=row["status"], created_at=row["created_at"],
            updated_at=row["updated_at"],
            prompt_index=row["prompt_index"],
            last_compaction_prompt_index=row["last_compaction_prompt_index"],
            group_name=row["group_name"],
            pinned=bool(row["pinned"]) if row["pinned"] is not None else False,
        )

    async def list_sessions_by_status(self, status: str) -> list[Session]:
        """列出指定状态的所有会话（T06d reconciliation 用）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            return self.conn.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY updated_at ASC",
                (status,),
            ).fetchall()
        rows = await self._run_sync(_sync)
        return [
            Session(
                id=r["id"], title=r["title"] or "", mode=r["mode"],
                status=r["status"], created_at=r["created_at"],
                updated_at=r["updated_at"],
                prompt_index=r["prompt_index"],
                last_compaction_prompt_index=r["last_compaction_prompt_index"],
                group_name=r["group_name"],
                pinned=bool(r["pinned"]) if r["pinned"] is not None else False,
            )
            for r in rows
        ]

    async def list_sessions(self) -> list[Session]:
        """列出所有会话（按 updated_at 倒序，T07 ChatPanel 左侧列表用）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            return self.conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC",
            ).fetchall()
        rows = await self._run_sync(_sync)
        return [
            Session(
                id=r["id"], title=r["title"] or "", mode=r["mode"],
                status=r["status"], created_at=r["created_at"],
                updated_at=r["updated_at"],
                prompt_index=r["prompt_index"],
                last_compaction_prompt_index=r["last_compaction_prompt_index"],
                group_name=r["group_name"],
                pinned=bool(r["pinned"]) if r["pinned"] is not None else False,
            )
            for r in rows
        ]

    async def update_session_status(self, session_id: str, status: str) -> None:
        """更新会话状态并写 event。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        now = time.time()
        def _sync():
            self.conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, session_id),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)
        await self.append_event(session_id, "session_status_changed", {"status": status})

    async def update_session_title(self, session_id: str, title: str) -> None:
        """更新会话标题并写 event（v6-lite-chat-fix T06，spec D7）。

        首句入库时 facade.start() 调用，自动取首句前 20 字写 sessions.title，
        让会话列表能区分会话（不再显示 sid 前 8 位）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        now = time.time()
        def _sync():
            self.conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, session_id),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)
        await self.append_event(session_id, "session_title_changed", {"title": title})

    async def update_session_group(
        self, session_id: str, group_name: str | None
    ) -> None:
        """chat-panel-v2 T06（D6/D10）：设置/取消会话分组。

        - group_name 非空：设为该分组（若与现有同名视为同一分组）
        - group_name=None 或 ""：取消分组（移回未分组列表）
        - 同步刷新 updated_at（影响置顶/分组内排序）

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        normalized = (group_name or "").strip() or None
        now = time.time()
        def _sync():
            self.conn.execute(
                "UPDATE sessions SET group_name = ?, updated_at = ? WHERE id = ?",
                (normalized, now, session_id),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)
        await self.append_event(
            session_id, "session_group_changed", {"group_name": normalized}
        )

    async def update_session_pinned(self, session_id: str, pinned: bool) -> None:
        """chat-panel-v2 T06（D6/D10/D13）：置顶/取消置顶会话。

        pinned=True → sessions.pinned=1（移入置顶区，按 updated_at 倒序展示）
        pinned=False → sessions.pinned=0（移回原分组或未分组）
        同步刷新 updated_at（置顶区/分组内排序锚点）

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        now = time.time()
        def _sync():
            self.conn.execute(
                "UPDATE sessions SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, now, session_id),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)
        await self.append_event(
            session_id, "session_pinned_changed", {"pinned": bool(pinned)}
        )

    async def list_distinct_group_names(self) -> list[str]:
        """chat-panel-v2 T06（D6）：列出所有非空分组名（UTF-8 顺序排序）。

        用于"移动到分组"对话框的候选清单 + 侧边栏分组列表渲染顺序。
        不含 NULL（未分组）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            return self.conn.execute(
                "SELECT DISTINCT group_name FROM sessions "
                "WHERE group_name IS NOT NULL AND group_name != '' "
                "ORDER BY group_name ASC"
            ).fetchall()
        rows = await self._run_sync(_sync)
        return [r["group_name"] for r in rows]

    async def finalize_session(self, session_id: str) -> None:
        """标记会话完成。"""
        await self.update_session_status(session_id, "completed")
        await self.append_event(session_id, "session_finalized", {})

    async def delete_session(self, session_id: str) -> bool:
        """删除会话 + 级联删 messages / events / tool_calls（chat-mgmt-probe Ticket 01）。

        事务包裹四表删除，保证原子性。返回值：
        - True：session 存在并已删
        - False：session 不存在或 session_id 为空（不抛异常）

        用途：探针 auto_cleanup / 用户手动删会话 / 测试隔离。
        不写 event（会话本身都没了，event 也没意义）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        if not session_id:
            return False
        # 先检查 session 是否存在（不存在直接返回 False，避免无谓删除）
        existing = await self.get_session(session_id)
        if existing is None:
            return False
        # 事务级联删：messages → events → tool_calls → sessions
        # 注：四表之间无 FK 约束，删除顺序不强制，但按依赖关系先删子表再删父表
        def _sync() -> bool:
            try:
                with self.conn:  # 上下文管理器自动 commit/rollback
                    self.conn.execute(
                        "DELETE FROM messages WHERE session_id = ?", (session_id,)
                    )
                    self.conn.execute(
                        "DELETE FROM events WHERE session_id = ?", (session_id,)
                    )
                    self.conn.execute(
                        "DELETE FROM tool_calls WHERE session_id = ?", (session_id,)
                    )
                    self.conn.execute(
                        "DELETE FROM proj_stats WHERE session_id = ?", (session_id,)
                    )
                    self.conn.execute(
                        "DELETE FROM proj_turn_outline WHERE session_id = ?", (session_id,)
                    )
                    self.conn.execute(
                        "DELETE FROM proj_title WHERE session_id = ?", (session_id,)
                    )
                    self.conn.execute(
                        "DELETE FROM sessions WHERE id = ?", (session_id,)
                    )
                return True
            except sqlite3.Error:
                # 事务失败时 with self.conn 已自动 rollback
                return False
        return await self._run_write_sync(_sync)

    # ------------------------------------------------------------------
    # messages 表 CRUD
    # ------------------------------------------------------------------

    async def append_message(self, session_id: str, message: Message) -> Message:
        """追加消息并写 event。自动分配 seq。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = row["next_seq"]
            now = time.time()
            msg_id = message.id or str(_uuid.uuid4())
            tool_calls_json = (
                json.dumps(message.tool_calls, ensure_ascii=False) if message.tool_calls else None
            )
            thinking_json = message.thinking if message.thinking else None
            model = message.model or None
            self.conn.execute(
                "INSERT INTO messages(id, session_id, seq, role, content_json, tool_call_id, source, visible, created_at, tool_calls_json, thinking_json, model) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg_id, session_id, seq, message.role,
                    json.dumps(message.content, ensure_ascii=False),
                    message.tool_call_id, message.source,
                    1 if message.visible else 0, now, tool_calls_json, thinking_json, model,
                ),
            )
            self.conn.commit()
            self.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
            self.conn.commit()
            return msg_id, seq, now

        msg_id, seq, now = await self._run_write_sync(_sync)
        # 写 event（append_event 已自带 asyncio.to_thread 包装）
        event_type = {
            "user": "user_message_appended",
            "assistant": "assistant_message_appended",
            "system": "system_message_appended",
            "synthetic": "synthetic_message_appended",
        }.get(message.role, f"{message.role}_message_appended")
        await self.append_event(session_id, event_type, {
            "message_id": msg_id, "seq": seq, "source": message.source,
            "visible": message.visible, "role": message.role,
        })
        # 返回填充后的 message
        message.id = msg_id
        message.seq = seq
        message.session_id = session_id
        message.created_at = now
        return message

    async def load_messages(self, session_id: str, include_invisible: bool = False) -> list[Message]:
        """加载会话消息（按 seq 升序）。默认跳过 visible=0 的 synthetic 消息。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            if include_invisible:
                sql = "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC"
                rows = self.conn.execute(sql, (session_id,)).fetchall()
            else:
                sql = "SELECT * FROM messages WHERE session_id = ? AND visible = 1 ORDER BY seq ASC"
                rows = self.conn.execute(sql, (session_id,)).fetchall()
            return [Message.from_db(dict(r)) for r in rows]
        return await self._run_sync(_sync)

    async def delete_message_by_id(self, message_id: str) -> bool:
        """chat-panel-v2 T09: 按 id 删除单条消息（用于清理持久化的排队消息）。

        返回 True 表示删除成功（行存在），False 表示无此 id。
        不写 event（排队消息是 UI 辅助状态，非对话内容）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        if not message_id:
            return False
        def _sync() -> bool:
            row = self.conn.execute(
                "SELECT session_id FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return False
            self.conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            self.conn.execute(
                "UPDATE proj_stats SET message_count = MAX(message_count - 1, 0) "
                "WHERE session_id = ?",
                (row["session_id"],),
            )
            self.conn.commit()
            return True
        return await self._run_write_sync(_sync)

    async def branch_session(
        self, source_session_id: str, upto_seq: int, title: str | None = None
    ) -> str:
        """分支会话（2026-09-13 用户需求）：复制源会话历史（seq ≤ upto_seq）为新会话。

        - 复制 user/assistant 且 visible=1 的消息，seq 在新会话内从 1 重新编号
        - tool 消息不复制，assistant 的 tool_calls_json 剥离——避免新会话出现
          "有 tool_calls 却无对应 tool_result"的 provider 400 隐患
        - 新会话继承源会话 group_name，置顶状态不复用；标题默认加 " (分支)" 后缀
        - 在源会话写一条 session_branched 审计事件

        返回新 session id。C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        if upto_seq <= 0:
            raise ValueError(f"upto_seq must be positive, got {upto_seq}")

        def _sync() -> str:
            src = self.conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (source_session_id,)
            ).fetchone()
            if src is None:
                raise ValueError(f"source session not found: {source_session_id}")
            new_id = f"sess-{_uuid.uuid4().hex[:12]}"
            now = time.time()
            final_title = title if title else f"{src['title'] or '对话'} (分支)"
            self.conn.execute(
                "INSERT INTO sessions(id, title, mode, status, created_at, updated_at, group_name, pinned) "
                "VALUES(?, ?, 'dialogue', 'idle', ?, ?, ?, 0)",
                (new_id, final_title, now, now, src["group_name"]),
            )
            rows = self.conn.execute(
                "SELECT role, content_json, thinking_json, model FROM messages "
                "WHERE session_id = ? AND seq <= ? AND visible = 1 AND role IN ('user', 'assistant') "
                "ORDER BY seq ASC",
                (source_session_id, upto_seq),
            ).fetchall()
            for new_seq, r in enumerate(rows, start=1):
                self.conn.execute(
                    "INSERT INTO messages(id, session_id, seq, role, content_json, tool_call_id, "
                    "source, visible, created_at, tool_calls_json, thinking_json, model) "
                    "VALUES(?, ?, ?, ?, ?, NULL, ?, 1, ?, NULL, ?, ?)",
                    (
                        str(_uuid.uuid4()), new_id, new_seq, r["role"], r["content_json"],
                        r["role"], now, r["thinking_json"], r["model"],
                    ),
                )
            self.conn.commit()
            return new_id

        new_id = await self._run_write_sync(_sync)
        await self.append_event(source_session_id, "session_branched", {
            "new_session_id": new_id,
            "upto_seq": upto_seq,
        })
        # 分支复制的是 messages 行、没有逐条 message 事件，投影只能从账本重建
        await self.rebuild_projections(new_id)
        return new_id


    async def find_pending_queue_messages(self) -> list[Message]:
        """chat-panel-v2 T09: 查找所有 source='queue' 的未发送排队消息。

        跨所有会话扫描，按 created_at 升序（最老的在前）。
        用于启动恢复：重启后检查是否有排队消息未发送，banner 提示用户。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE source = 'queue' ORDER BY created_at ASC"
            ).fetchall()
            return [Message.from_db(dict(r)) for r in rows]
        return await self._run_sync(_sync)

    async def replace_messages(self, session_id: str, new_messages: list[Message]) -> None:
        """T08: 用压缩后的消息列表替换当前会话所有 visible 消息。

        v6-lite §3 W5 L2 压缩调用：
        - 删除当前 session 所有 visible=1 的 messages
        - 重新插入 new_messages（seq 从 1 重新分配）
        - 写一条 context_compacted event（payload 含 original_count/new_count）

        注意：invisible 的 synthetic 消息保留（v6-02 §4 不污染用户视图，
        压缩只重建 visible transcript）。

        此操作不可逆，调用前应已 append 一条 context_compacted event 作为审计。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        now = time.time()
        def _sync() -> int:
            # 获取当前 visible 消息数（审计用）
            row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE session_id = ? AND visible = 1",
                (session_id,),
            ).fetchone()
            original_count = row["cnt"] if row else 0

            # 删除当前所有 visible 消息
            self.conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND visible = 1",
                (session_id,),
            )

            # 重新插入 new_messages（seq 从 1 重新分配）
            for i, msg in enumerate(new_messages, start=1):
                msg_id = msg.id or str(_uuid.uuid4())
                tool_calls_json = (
                    json.dumps(msg.tool_calls, ensure_ascii=False) if msg.tool_calls else None
                )
                thinking_json = msg.thinking if msg.thinking else None
                model = msg.model or None
                self.conn.execute(
                    "INSERT INTO messages(id, session_id, seq, role, content_json, tool_call_id, source, visible, created_at, tool_calls_json, thinking_json, model) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        msg_id, session_id, i, msg.role,
                        json.dumps(msg.content, ensure_ascii=False),
                        msg.tool_call_id, msg.source,
                        1 if msg.visible else 0, now, tool_calls_json, thinking_json, model,
                    ),
                )
                msg.id = msg_id
                msg.seq = i
                msg.session_id = session_id
                msg.created_at = now

            # 更新 session updated_at + last_compaction_prompt_index
            self.conn.execute(
                "UPDATE sessions SET updated_at = ?, last_compaction_prompt_index = COALESCE(last_compaction_prompt_index, 0) + 1 WHERE id = ?",
                (now, session_id),
            )
            self.conn.commit()
            return original_count

        original_count = await self._run_write_sync(_sync)

        # 写 context_compacted event（审计用）
        await self.append_event(session_id, "context_compacted", {
            "original_count": original_count,
            "new_count": len(new_messages),
            "compacted_at": now,
        })
        # 压缩直接替换 messages 行，投影按账本重算一次
        await self.rebuild_projections(session_id)

    # ------------------------------------------------------------------
    # events 表（append-only）
    # ------------------------------------------------------------------

    async def append_event(
        self, session_id: str, event_type: str, payload: dict,
        trace_id: str | None = None,
    ) -> int:
        """追加事件（append-only），返回 seq。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread，避免阻塞 event loop。
        """
        def _sync():
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = row["next_seq"]
            now = time.time()
            domain = domain_for_event(event_type)
            self.conn.execute(
                "INSERT INTO events(seq, session_id, type, domain, payload_json, trace_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (seq, session_id, event_type, domain,
                 json.dumps(payload, ensure_ascii=False), trace_id, now),
            )
            self._apply_projection_event(
                self.conn, session_id, event_type, payload, seq, now, domain
            )
            self.conn.commit()
            return seq
        return await self._run_write_sync(_sync)

    async def load_events(
        self, session_id: str, *, include_live: bool = False, after_seq: int = 0,
    ) -> list[Event]:
        """加载会话事件（按 seq 升序，含 invalidated 的，供回放）。

        X10 契约：默认只返回 persistent + capability；`include_live=True`
        才带 streaming 增量。live 仍落库（排障/合并用），只是默认不重放。
        `after_seq` 用于 WS 续传：只返回 seq > after_seq 的事件。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            if include_live:
                rows = self.conn.execute(
                    "SELECT * FROM events WHERE session_id = ? AND seq > ? ORDER BY seq ASC",
                    (session_id, int(after_seq)),
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in REPLAY_DOMAINS)
                rows = self.conn.execute(
                    f"SELECT * FROM events WHERE session_id = ? AND seq > ? "
                    f"AND domain IN ({placeholders}) ORDER BY seq ASC",
                    (session_id, int(after_seq), *REPLAY_DOMAINS),
                ).fetchall()
            return [
                Event(
                    seq=r["seq"], session_id=r["session_id"], type=r["type"],
                    payload=json.loads(r["payload_json"]) if r["payload_json"] else {},
                    created_at=r["created_at"], trace_id=r["trace_id"],
                    prompt_index=r["prompt_index"],
                    invalidated_seq=r["invalidated_seq"],
                    parent_trace_id=r["parent_trace_id"],
                    depth=r["depth"],
                    domain=r["domain"] or EVENT_DOMAIN_PERSISTENT,
                )
                for r in rows
            ]
        return await self._run_sync(_sync)

    async def get_last_event_time(self, session_id: str) -> float | None:
        """获取会话最新 runner-activity 事件的 created_at（C2 spec D7/D11：reconcile 活跃性判断）。

        Returns:
            最新 runner-activity 事件的 created_at（float），无活跃事件时返回 None。
            用于 reconcile 判断 streaming/awaiting_tools session 是否还活跃：
            - 近期有 runner-activity 事件 → runner 可能还在跑，跳过不标 interrupted
            - 长期无活跃事件 / 无活跃事件 → 视为残留，标 interrupted

        注：只统计 runner 活跃事件（白名单），不统计状态标记/审计事件
            （session_created / session_status_changed / session_reconciled /
            streaming_events_merged / session_title_changed / session_finalized），
            因为这些事件不是 runner 持续工作的证据。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        # runner 活跃事件白名单：
        # - streaming_*: LLM 流式增量
        # - transition: runner 主循环迭代间转换
        # - context_compacted: 压缩发生
        # - tool_call_pending / tool_call_status_changed / tool_result_appended: 工具执行
        # - *_message_appended: 消息追加（assistant/user/system/synthetic/tool）
        ACTIVITY_TYPES = (
            "streaming_text_delta",
            "streaming_thinking_delta",
            "streaming_tool_call",
            "transition",
            "context_compacted",
            "tool_call_pending",
            "tool_call_status_changed",
            "tool_result_appended",
            "assistant_message_appended",
            "user_message_appended",
            "system_message_appended",
            "synthetic_message_appended",
            "tool_message_appended",
        )
        placeholders = ",".join("?" * len(ACTIVITY_TYPES))
        def _sync() -> float | None:
            row = self.conn.execute(
                f"SELECT MAX(created_at) AS last_ts FROM events "
                f"WHERE session_id = ? AND type IN ({placeholders})",
                (session_id, *ACTIVITY_TYPES),
            ).fetchone()
            if row is None or row["last_ts"] is None:
                return None
            return float(row["last_ts"])
        return await self._run_sync(_sync)

    async def invalidate_events(self, session_id: str, seqs: list[int]) -> None:
        """标记事件为 invalidated（v6-lite-streaming-gui T01 D06）。

        streaming 事件合并为完整 Message 后，调此方法标记 invalidated_seq，
        使 load_messages / 回放不再返回这些临时增量事件。
        invalidated_seq 设为当前 MAX(seq) + 1（表示"在第 N 步被合并"）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        if not seqs:
            return
        def _sync():
            # 获取当前最大 seq 作为 invalidated_seq 标记
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            mark_seq = row["max_seq"]
            placeholders = ", ".join("?" for _ in seqs)
            self.conn.execute(
                f"UPDATE events SET invalidated_seq = ? WHERE session_id = ? AND seq IN ({placeholders})",
                (mark_seq, session_id, *seqs),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)

    async def purge_invalidated_events(self, before_ts: float | None = None) -> int:
        """物理删除已 invalidated 的事件行（2026-09-13 code review 8-11 维护方法）。

        streaming 每条 delta 一行 events 插入，会话结束后只标记 invalidated_seq
        永不物理删除，agent.db 随使用量单调膨胀。本方法 DELETE 这些行并返回删除数。

        Args:
            before_ts: 只删 created_at < before_ts 的已 invalidated 行（Unix 秒）；
                       None = 不设时间下限，删除全部已 invalidated 行。

        只删 invalidated_seq IS NOT NULL 的行；未 invalidated 的事件（活跃 streaming
        增量 + 全部审计事件）不受影响。

        建议调用时机（本类没有自然的自动挂点，按需显式调用）：
        - GUI 启动或会话删除后的低频维护任务：
          ``n = await store.purge_invalidated_events(time.time() - 7 * 86400)``
        - 不建议在 runner 运行路径中调用（与写路径抢 _write_lock）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread + _write_lock。
        """
        def _sync() -> int:
            if before_ts is None:
                cur = self.conn.execute(
                    "DELETE FROM events WHERE invalidated_seq IS NOT NULL"
                )
            else:
                cur = self.conn.execute(
                    "DELETE FROM events "
                    "WHERE invalidated_seq IS NOT NULL AND created_at < ?",
                    (before_ts,),
                )
            self.conn.commit()
            return cur.rowcount
        deleted = await self._run_write_sync(_sync)
        return int(deleted or 0)

    async def load_streaming_events(self, session_id: str) -> list[Event]:
        """加载未合并的 streaming 事件（v6-lite-streaming-gui T01 D06）。

        返回 invalidated_seq IS NULL 且 type 属于 streaming 增量类型的事件，
        按 seq 升序。reconciler 用此方法找需要合并的 streaming 增量。
        注意：精确匹配 3 种增量类型，避免误捞审计事件 `streaming_events_merged`。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            rows = self.conn.execute(
                "SELECT * FROM events WHERE session_id = ? AND invalidated_seq IS NULL "
                "AND type IN ('streaming_text_delta', 'streaming_thinking_delta', 'streaming_tool_call') "
                "ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
            return [
                Event(
                    seq=r["seq"], session_id=r["session_id"], type=r["type"],
                    payload=json.loads(r["payload_json"]) if r["payload_json"] else {},
                    created_at=r["created_at"], trace_id=r["trace_id"],
                    prompt_index=r["prompt_index"],
                    invalidated_seq=r["invalidated_seq"],
                    parent_trace_id=r["parent_trace_id"],
                    depth=r["depth"],
                    domain=r["domain"] or EVENT_DOMAIN_PERSISTENT,
                )
                for r in rows
            ]
        return await self._run_sync(_sync)

    # ------------------------------------------------------------------
    # 回放（W1 验收：所有 events 可按 seq 回放整个会话）
    # ------------------------------------------------------------------

    async def replay_events(
        self, session_id: str, *, include_live: bool = False,
    ) -> list[dict]:
        """按 seq 回放会话事件序列（每条含 type + payload + ts）。

        X10：默认与 load_events 一致排除 live；需要排障时显式 include_live=True。
        """
        events = await self.load_events(session_id, include_live=include_live)
        return [
            {"seq": e.seq, "type": e.type, "payload": e.payload, "ts": e.created_at}
            for e in events
        ]

    # ------------------------------------------------------------------
    # tool_calls 表 CRUD（T02 新增）
    # ------------------------------------------------------------------

    async def append_tool_call(self, session_id: str, tool_call: ToolCall) -> ToolCall:
        """追加工具调用记录（status=pending，v6-lite §4.10 副作用前先落 durable record）。

        幂等：tool_call.id 已存在则直接返回已有记录（不重复插入）。
        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            existing = self.conn.execute(
                "SELECT * FROM tool_calls WHERE id = ?", (tool_call.id,)
            ).fetchone()
            if existing is not None:
                return ToolCall.from_db(dict(existing)), None

            seq = tool_call.seq
            if seq == 0:
                row = self.conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM tool_calls WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                seq = row["next_seq"]

            self.conn.execute(
                "INSERT INTO tool_calls(id, session_id, seq, name, args_json, safety, status, started_at, ended_at, trace_id) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tool_call.id, session_id, seq, tool_call.name,
                    json.dumps(tool_call.args, ensure_ascii=False),
                    tool_call.safety, TOOL_STATUS_PENDING,
                    None, None, tool_call.trace_id,
                ),
            )
            self.conn.commit()
            return None, seq

        existing_tc, new_seq = await self._run_write_sync(_sync)
        if existing_tc is not None:
            return existing_tc
        seq = new_seq
        # 写 event（tool_call_pending，durable record 已落）
        await self.append_event(session_id, "tool_call_pending", {
            "tool_call_id": tool_call.id, "seq": seq,
            "name": tool_call.name, "safety": tool_call.safety,
        })
        tool_call.session_id = session_id
        tool_call.seq = seq
        tool_call.status = TOOL_STATUS_PENDING
        return tool_call

    async def update_tool_call_status(
        self, tool_call_id: str, status: str,
        started_at: float | None = None, ended_at: float | None = None,
    ) -> bool:
        """更新工具调用状态（pending → running → completed/failed/interrupted/unknown）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。

        unknown-outcome-evidence T01：**终态不可回写**。已落入 completed/failed/
        interrupted/unknown 的记录，任何改向其他终态或退回非终态的写入都被拒绝，
        只追加一条 tool_call_status_write_rejected 审计事件——要修正只能追加事实，
        不能覆盖事实。返回 False 表示本次写入被拒绝或记录不存在。
        """
        def _sync() -> tuple[str | None, str | None, str]:
            row = self.conn.execute(
                "SELECT session_id, status FROM tool_calls WHERE id = ?", (tool_call_id,)
            ).fetchone()
            if row is None:
                return (None, None, "missing")
            session_id = row["session_id"]
            current = row["status"]

            if current in TOOL_STATUS_TERMINAL:
                if current == status and started_at is None and ended_at is None:
                    # 同值且不带时间字段：幂等 no-op，不写库也不造审计噪音
                    return (session_id, current, "noop")
                return (session_id, current, "reject")

            # 构造更新字段
            sets = ["status = ?"]
            params: list[Any] = [status]
            if started_at is not None:
                sets.append("started_at = ?")
                params.append(started_at)
            if ended_at is not None:
                sets.append("ended_at = ?")
                params.append(ended_at)
            params.append(tool_call_id)

            self.conn.execute(
                f"UPDATE tool_calls SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            self.conn.commit()
            return (session_id, current, "write")

        session_id, prior_status, mode = await self._run_write_sync(_sync)
        if session_id is None:
            logger.warning("update_tool_call_status: tool_call %s not found", tool_call_id)
            return False
        if mode == "reject":
            await self.append_event(session_id, "tool_call_status_write_rejected", {
                "tool_call_id": tool_call_id,
                "attempted_status": status,
                "terminal_status": prior_status,
                "started_at": started_at,
                "ended_at": ended_at,
            })
            logger.warning(
                "update_tool_call_status: refused to overwrite terminal status %s → %s "
                "for tool_call %s (terminal records are append-only)",
                prior_status, status, tool_call_id,
            )
            return False
        if mode == "noop":
            return True
        # 写 event（状态流转审计）
        await self.append_event(session_id, "tool_call_status_changed", {
            "tool_call_id": tool_call_id, "status": status,
            "started_at": started_at, "ended_at": ended_at,
        })
        return True

    async def tool_call_outcome_summary(self, session_id: str) -> dict[str, Any]:
        """本会话工具调用的终态计数 + 非成功项清单。

        unknown-outcome-evidence T13：给无人值守 judge 当**结构化证据**用。
        此前 judge 只看主 agent 说了什么，"说完成了"就算完成；现在必须同时看到
        执行账本里有多少 completed / failed / interrupted / unknown。
        """
        def _sync() -> dict[str, Any]:
            rows = self.conn.execute(
                "SELECT id, name, status FROM tool_calls WHERE session_id = ? "
                "ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
            counts: dict[str, int] = {}
            problems: list[dict[str, str]] = []
            for r in rows:
                status = r["status"] or TOOL_STATUS_PENDING
                counts[status] = counts.get(status, 0) + 1
                if status not in ("completed",):
                    problems.append({
                        "id": r["id"], "name": r["name"], "status": status,
                    })
            rejected_row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM events WHERE session_id = ? "
                "AND type = 'tool_call_status_write_rejected'",
                (session_id,),
            ).fetchone()
            return {
                "total": len(rows),
                "counts": counts,
                "non_success": problems,
                "rejected_terminal_writes": int(rejected_row["cnt"] or 0),
            }

        return await self._run_sync(_sync)

    async def load_tool_calls(
        self, session_id: str, status: str | None = None,
    ) -> list[ToolCall]:
        """加载会话工具调用（按 seq 升序，可选按 status 过滤）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync():
            if status is None:
                sql = "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY seq ASC, id ASC"
                rows = self.conn.execute(sql, (session_id,)).fetchall()
            else:
                sql = "SELECT * FROM tool_calls WHERE session_id = ? AND status = ? ORDER BY seq ASC, id ASC"
                rows = self.conn.execute(sql, (session_id, status)).fetchall()
            return [ToolCall.from_db(dict(r)) for r in rows]
        return await self._run_sync(_sync)

    async def list_tool_calls_by_status(
        self, statuses: list[str],
    ) -> list[ToolCall]:
        """跨所有会话列出指定状态的 tool_calls（T06d reconciliation 用）。

        用于启动恢复时扫描 pending/running 的悬挂工具调用。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        if not statuses:
            return []
        def _sync():
            placeholders = ", ".join("?" for _ in statuses)
            sql = (
                f"SELECT * FROM tool_calls WHERE status IN ({placeholders}) "
                f"ORDER BY session_id ASC, seq ASC, id ASC"
            )
            rows = self.conn.execute(sql, statuses).fetchall()
            return [ToolCall.from_db(dict(r)) for r in rows]
        return await self._run_sync(_sync)

    async def has_tool_result(self, tool_call_id: str) -> bool:
        """检查指定 tool_call 是否已有对应的 role=tool 结果消息（T06d reconciliation 用）。

        OpenAI 协议要求每个 tool_call 必须配一条 role=tool 消息；
        重启后若 tool_call 状态为 pending/running 但无 tool_result 消息，
        需补 is_error=true 的 tool_result（yieldMissingToolResultBlocks，防 provider 400）。

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync() -> bool:
            row = self.conn.execute(
                "SELECT 1 FROM messages WHERE tool_call_id = ? AND role = 'tool' LIMIT 1",
                (tool_call_id,),
            ).fetchone()
            return row is not None
        return await self._run_sync(_sync)

    async def append_tool_result_message(
        self, session_id: str, result: ToolResult,
    ) -> Message:
        """把工具结果作为 role=tool 消息回灌到 messages 表。

        OpenAI 协议：assistant 携带 tool_calls → 每个 tool_call 必须有一条 role=tool 消息配对。
        source=tool_result；content 是结果文本（或 error 描述）。
        """
        msg = Message(
            role="tool",
            content=result.content,  # OpenAI 协议：role=tool 的 content 是字符串
            source="tool_result",
            tool_call_id=result.tool_call_id,
            visible=True,  # 工具结果可见（transcript 渲染为工具卡）
        )
        appended = await self.append_message(session_id, msg)
        # 写 event（tool_result_appended，便于回放与启动恢复）
        await self.append_event(session_id, "tool_result_appended", {
            "tool_call_id": result.tool_call_id,
            "message_id": appended.id,
            "message_seq": appended.seq,
            "is_error": result.is_error,
            "artifact_path": result.artifact_path,
        })
        # 更新 ToolResult 时间戳
        result.created_at = appended.created_at
        return appended

    # ------------------------------------------------------------------
    # usage 表 CRUD（chat-engine-safety-fixes D5 新增，spec D20）
    # ------------------------------------------------------------------

    async def append_usage(
        self,
        session_id: str,
        seq: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        model: str = "",
    ) -> str:
        """追加一次 LLM 调用的 usage 记录（D5，spec D20）。

        每次 LLM 返回后调此方法，usage 持久化到 SQLite。
        失败/断流场景也调（runner 在中断路径仍记录已收到的 usage）。

        Args:
            session_id: 会话 ID
            seq: LLM 调用轮次（与 iterations 对应）
            prompt_tokens: 输入 token 数
            completion_tokens: 输出 token 数
            total_tokens: 总 token 数（通常 = prompt + completion）
            model: 调用的模型名

        Returns:
            usage_id（uuid 字符串）

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        usage_id = str(_uuid.uuid4())
        created_at = time.time()

        def _sync():
            self.conn.execute(
                "INSERT INTO usage(id, session_id, seq, prompt_tokens, completion_tokens, "
                "total_tokens, model, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (usage_id, session_id, seq, prompt_tokens, completion_tokens,
                 total_tokens, model, created_at),
            )
            self.conn.execute(
                "UPDATE proj_stats SET total_tokens = total_tokens + ? WHERE session_id = ?",
                (total_tokens, session_id),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)
        return usage_id

    async def load_usage(self, session_id: str) -> list[dict]:
        """加载会话的所有 usage 记录（按 seq 升序，D5，spec D20）。

        Returns:
            list[dict]：每条记录含 id/session_id/seq/prompt_tokens/
            completion_tokens/total_tokens/model/created_at

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync() -> list[dict]:
            rows = self.conn.execute(
                "SELECT * FROM usage WHERE session_id = ? ORDER BY seq ASC, id ASC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        return await self._run_sync(_sync)

    async def get_last_usage_anchor(self, session_id: str) -> dict | None:
        """取该会话最近一次 LLM 调用的真实 usage，作为压缩判据的锚点（2026-10-07）。

        为什么按 created_at 而不是 seq：usage.seq 存的是 runner 迭代轮数
        （runner.py `append_usage(seq=iterations)`），每起一次新 run 都从 1 重计，
        跨 run 不可比；created_at 才是全局时序。

        一并返回该会话最近一次压缩的时间戳 last_compaction_at：压缩会重写
        messages，比它更早的锚点算的是"压缩前那份上下文"，不能当增量基准。

        Returns:
            None（该会话从未有过 LLM 调用）或
            {prompt_tokens, created_at, model, last_compaction_at}
        """
        def _sync() -> dict | None:
            row = self.conn.execute(
                "SELECT prompt_tokens, created_at, model FROM usage "
                "WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            anchor = dict(row)
            comp = self.conn.execute(
                "SELECT MAX(created_at) AS at FROM compactions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            comp_at = comp["at"] if comp is not None else None
            anchor["last_compaction_at"] = float(comp_at) if comp_at else 0.0
            return anchor

        return await self._run_sync(_sync)

    async def get_total_tokens(self, session_id: str | None = None) -> int:
        """查询 total_tokens 总和（D5，spec D20）。

        Args:
            session_id: 指定 session 时只 SUM 该 session；None 时跨所有 session SUM

        Returns:
            total_tokens 总和（无记录返回 0）

        C7（spec D11）：DB 操作包装到 asyncio.to_thread。
        """
        def _sync() -> int:
            if session_id is None:
                row = self.conn.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) AS s FROM usage"
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) AS s FROM usage WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            return int(row["s"]) if row is not None else 0
        return await self._run_sync(_sync)

    # ========================================================================
    # E3（spec）：client_message_id 幂等去重
    # ========================================================================

    async def has_client_message_id(self, client_message_id: str) -> bool:
        """检查 client_message_id 是否已处理过（E3 幂等去重）。

        Args:
            client_message_id: 客户端生成的幂等键

        Returns:
            True 如果已存在（重复提交），False 如果首次提交
        """
        def _sync() -> bool:
            row = self.conn.execute(
                "SELECT 1 FROM client_message_ids WHERE client_message_id = ?",
                (client_message_id,),
            ).fetchone()
            return row is not None
        return await self._run_sync(_sync)

    async def get_outcome_for_client_message_id(
        self, client_message_id: str
    ) -> dict | None:
        """查询 client_message_id 关联的上次 outcome（E3 幂等返回缓存结果）。

        Args:
            client_message_id: 客户端生成的幂等键

        Returns:
            {"session_id": str, "outcome_status": str, "outcome_stop_reason": str}
            或 None（不存在）
        """
        def _sync() -> dict | None:
            row = self.conn.execute(
                "SELECT session_id, outcome_status, outcome_stop_reason "
                "FROM client_message_ids WHERE client_message_id = ?",
                (client_message_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "session_id": row["session_id"],
                "outcome_status": row["outcome_status"],
                "outcome_stop_reason": row["outcome_stop_reason"] or "",
            }
        return await self._run_sync(_sync)

    async def record_client_message_id(
        self,
        client_message_id: str,
        session_id: str,
        outcome_status: str,
        outcome_stop_reason: str,
    ) -> None:
        """记录 client_message_id → outcome 映射（E3 幂等去重）。

        在 facade.start() 完成后调用，记录本次提交的 outcome。
        下次相同 client_message_id 提交时，has_client_message_id 返回 True，
        facade 返回缓存的 outcome 而不重复触发 LLM。

        Args:
            client_message_id: 客户端生成的幂等键
            session_id: 关联的 session ID
            outcome_status: RunOutcome.status (completed/interrupted/failed)
            outcome_stop_reason: RunOutcome.stop_reason
        """
        created_at = time.time()

        def _sync():
            self.conn.execute(
                "INSERT OR REPLACE INTO client_message_ids "
                "(client_message_id, session_id, outcome_status, outcome_stop_reason, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (client_message_id, session_id, outcome_status, outcome_stop_reason, created_at),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)

    # ------------------------------------------------------------------
    # runner_state 表（E6，spec D23：runner 关键状态持久化）
    # ------------------------------------------------------------------

    async def upsert_runner_state(
        self,
        session_id: str,
        *,
        iterations: int,
        has_attempted_reactive_compact: bool,
        total_tokens: int,
        wall_clock_exceeded: bool,
    ) -> None:
        """E6（spec D23）：upsert runner 关键状态到 runner_state 表。

        runner.py 在主循环每轮关键状态变更后调用，把当前快照写入 DB。
        用途：
        1. 审计：检查中断/失败时的 runner 状态（iterations / total_tokens / wall_clock）
        2. 重启恢复：reconciler 启动时读取，避免 has_attempted_reactive_compact 丢失
           导致二次压缩（spec D23 明确目标）
        3. 跨表对账：total_tokens 可与 usage 表 SUM(total_tokens) 互验

        Args:
            session_id: 关联的 session ID
            iterations: runner 已执行的迭代轮数
            has_attempted_reactive_compact: 是否已尝试过 L2 压缩重试
            total_tokens: 累计 token
            wall_clock_exceeded: 最后一次 LLM 响应是否触发 wall_clock 超时
        """
        now = time.time()
        arc_int = 1 if has_attempted_reactive_compact else 0
        wc_int = 1 if wall_clock_exceeded else 0

        def _sync():
            self.conn.execute(
                "INSERT OR REPLACE INTO runner_state "
                "(session_id, iterations, has_attempted_reactive_compact, "
                " total_tokens, wall_clock_exceeded, last_updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (session_id, iterations, arc_int, total_tokens, wc_int, now),
            )
            self.conn.commit()
        await self._run_write_sync(_sync)

    async def get_runner_state(self, session_id: str) -> dict | None:
        """E6（spec D23）：读取 runner 状态快照。

        Returns:
            dict with keys: session_id / iterations / has_attempted_reactive_compact
                                 / total_tokens / wall_clock_exceeded / last_updated_at
            None if no record exists for session_id
        """
        def _sync():
            return self.conn.execute(
                "SELECT session_id, iterations, has_attempted_reactive_compact, "
                "       total_tokens, wall_clock_exceeded, last_updated_at "
                "FROM runner_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        row = await self._run_sync(_sync)
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "iterations": row["iterations"],
            "has_attempted_reactive_compact": bool(row["has_attempted_reactive_compact"]),
            "total_tokens": row["total_tokens"],
            "wall_clock_exceeded": bool(row["wall_clock_exceeded"]),
            "last_updated_at": row["last_updated_at"],
        }

    # ------------------------------------------------------------------
    # compactions 表（E7，spec D24：压缩历史持久化）
    # ------------------------------------------------------------------

    async def append_compaction(
        self,
        session_id: str,
        *,
        source_seq: int,
        summary: str,
        recent_json: str,
        prompt_hash: str,
    ) -> int:
        """E7（spec D24）：追加一条压缩历史记录。

        runner.py 在 reactive_compact 成功后（compaction_occurred=True）调用。
        用途：审计压缩命中率、回溯压缩内容、统计压缩频次。

        Args:
            session_id: 关联的 session ID
            source_seq: 触发压缩时的 runner 迭代轮数
            summary: LLM 生成的摘要文本
            recent_json: 压缩后保留的 tail messages（JSON 字符串）
            prompt_hash: 压缩前 messages 的 hash（去重/审计）

        Returns:
            compaction_id: 新插入记录的 id
        """
        now = time.time()

        def _sync():
            cur = self.conn.execute(
                "INSERT INTO compactions "
                "(session_id, source_seq, summary, recent_json, prompt_hash, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (session_id, source_seq, summary, recent_json, prompt_hash, now),
            )
            self.conn.commit()
            return cur.lastrowid
        return await self._run_write_sync(_sync)

    async def load_compactions(self, session_id: str) -> list[dict]:
        """E7（spec D24）：读取 session 的压缩历史记录（按 id 升序）。

        Returns:
            list[dict] with keys: id / session_id / source_seq / summary
                                  / recent_json / prompt_hash / created_at
            空 list if no records
        """
        def _sync():
            return self.conn.execute(
                "SELECT id, session_id, source_seq, summary, "
                "       recent_json, prompt_hash, created_at "
                "FROM compactions WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        rows = await self._run_sync(_sync)
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "source_seq": row["source_seq"],
                "summary": row["summary"],
                "recent_json": row["recent_json"],
                "prompt_hash": row["prompt_hash"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
