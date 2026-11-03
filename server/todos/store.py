"""待办模块存储层 — SQLite CRUD（共享 memory.db）"""

import calendar
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from server.todos.classify import (
    PURPOSES,
    STRUCT_LONG,
    STRUCT_RECURRING,
    STRUCT_TYPES,
    as_date,
    build_queue,
    classify_todo,
    classify_wip,
    summarize,
)

logger = logging.getLogger("parcae.todos.store")

TODOS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    skill TEXT,
    type TEXT NOT NULL DEFAULT 'recurring',
    status TEXT NOT NULL DEFAULT 'pending',
    frequency TEXT,
    last_done_at TEXT,
    next_due_at TEXT,
    condition TEXT,
    condition_status TEXT,
    notes TEXT,
    related_memory_keys TEXT DEFAULT '[]',
    start_date TEXT,
    end_date TEXT,
    trigger_condition TEXT,
    task_class TEXT,
    purpose TEXT,
    next_review_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);

CREATE INDEX IF NOT EXISTS idx_todos_type ON todos(type);
CREATE INDEX IF NOT EXISTS idx_todos_next_due ON todos(next_due_at);

CREATE TABLE IF NOT EXISTS wip_tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT NOT NULL DEFAULT 'medium',
    goal TEXT DEFAULT '',
    progress INTEGER DEFAULT 0,
    tags TEXT DEFAULT '[]',
    next_steps TEXT DEFAULT '[]',
    related_skills TEXT DEFAULT '[]',
    related_files TEXT DEFAULT '[]',
    current_state TEXT DEFAULT '{}',
    blocked_reason TEXT,
    related_memory_keys TEXT DEFAULT '[]',
    extra_data TEXT DEFAULT '{}',
    task_class TEXT,
    purpose TEXT,
    due_at TEXT,
    done_when TEXT,
    next_review_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_wip_status ON wip_tasks(status);
CREATE INDEX IF NOT EXISTS idx_wip_priority ON wip_tasks(priority);

-- DL2：循环模板的一次性实例。模板永不 done，occurrence 才记"这次做没做"。
-- 唯一索引 (template_id, due_at) 让 materialize 天然幂等。
CREATE TABLE IF NOT EXISTS task_occurrences (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    done_at TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_occ_template_due ON task_occurrences(template_id, due_at);
CREATE INDEX IF NOT EXISTS idx_occ_status ON task_occurrences(status);

-- T14（2026-10-27）：任务表合并。todos + wip_tasks 的超集；老表冻结为 legacy。
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT DEFAULT 'medium',
    task_class TEXT,
    purpose TEXT,
    goal TEXT DEFAULT '',
    done_when TEXT,
    progress INTEGER DEFAULT 0,
    next_steps TEXT DEFAULT '[]',
    current_state TEXT DEFAULT '{}',
    blocked_reason TEXT,
    notes TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    skill TEXT,
    related_skills TEXT DEFAULT '[]',
    related_files TEXT DEFAULT '[]',
    related_memory_keys TEXT DEFAULT '[]',
    frequency TEXT,
    start_date TEXT,
    end_date TEXT,
    condition TEXT,
    condition_status TEXT,
    trigger_condition TEXT,
    due_at TEXT,
    next_due_at TEXT,
    last_done_at TEXT,
    next_review_at TEXT,
    extra_data TEXT DEFAULT '{}',
    -- v15（2026-10-30）：面板归档。归档不覆盖 status，另存归档前那一档以便原样恢复。
    archived_at TEXT,
    archive_prev_status TEXT,
    archive_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_next_due ON tasks(next_due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_class ON tasks(task_class);

CREATE TABLE IF NOT EXISTS task_merge_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# ALTER TABLE 语句用于已有 DB 升级（幂等，列已存在时忽略错误）
_ALTER_STATEMENTS = [
    "ALTER TABLE todos ADD COLUMN condition_status TEXT",
    "ALTER TABLE wip_tasks ADD COLUMN extra_data TEXT DEFAULT '{}'",
    "ALTER TABLE todos ADD COLUMN start_date TEXT",
    "ALTER TABLE todos ADD COLUMN end_date TEXT",
    "ALTER TABLE todos ADD COLUMN trigger_condition TEXT",
    # DL2（2026-10-27）：结构轴 task_class / 目的轴 purpose / 长期任务复盘点
    "ALTER TABLE todos ADD COLUMN task_class TEXT",
    "ALTER TABLE todos ADD COLUMN purpose TEXT",
    "ALTER TABLE todos ADD COLUMN next_review_at TEXT",
    # wip 是"其余三类"的落点：一次性要 due_at/done_when，小任务靠 extra_data.one_session
    "ALTER TABLE wip_tasks ADD COLUMN task_class TEXT",
    "ALTER TABLE wip_tasks ADD COLUMN purpose TEXT",
    "ALTER TABLE wip_tasks ADD COLUMN due_at TEXT",
    "ALTER TABLE wip_tasks ADD COLUMN done_when TEXT",
    "ALTER TABLE wip_tasks ADD COLUMN next_review_at TEXT",
    # v15（2026-10-30）：面板归档三列。这里也要有一份——新库由 DDL 带，
    # 但只经 TodosStore 初始化（不经 schema.py 迁移）的库得靠这几句补齐，
    # 否则 apply_task_action("archive") 会报 no such column: archived_at。
    "ALTER TABLE tasks ADD COLUMN archived_at TEXT",
    "ALTER TABLE tasks ADD COLUMN archive_prev_status TEXT",
    "ALTER TABLE tasks ADD COLUMN archive_reason TEXT",
]

# todos 表可更新字段（排除 id、created_at、updated_at 等自动字段）
_TODO_UPDATE_FIELDS = {
    "title", "skill", "type", "status", "frequency",
    "last_done_at", "next_due_at", "condition", "condition_status",
    "notes", "related_memory_keys",
    "start_date", "end_date", "trigger_condition",
    "task_class", "purpose", "next_review_at",
}
# wip_tasks 表可更新字段
_WIP_UPDATE_FIELDS = {
    "title", "status", "priority", "goal", "progress", "tags",
    "next_steps", "related_skills", "related_files",
    "current_state", "blocked_reason", "related_memory_keys", "extra_data",
    "task_class", "purpose", "due_at", "done_when", "next_review_at",
}

# JSON 数组/对象字段，读写时需要 json.loads/dumps
_TODO_JSON_FIELDS = {"related_memory_keys"}
_WIP_JSON_FIELDS = {
    "tags", "next_steps", "related_skills", "related_files",
    "current_state", "related_memory_keys", "extra_data",
}

# 依赖 DL2 新增列的索引：必须在 _migrate_schema() 之后建，
# 否则旧库（列还没 ALTER 出来）执行建表脚本会 "no such column: task_class"。
_TASK_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_todos_task_class ON todos(task_class);
CREATE INDEX IF NOT EXISTS idx_wip_task_class ON wip_tasks(task_class);
CREATE INDEX IF NOT EXISTS idx_wip_due ON wip_tasks(due_at);
"""

# 已完结的 WIP 状态：summary 模式下这些条目只留最少的字段与最短的标题
# （active/paused/blocked 之外的历史遗留状态一并纳入，避免漏压缩）
_CLOSED_WIP_STATUSES = frozenset({"completed", "done", "archived", "cancelled"})
# summary 模式下已完结条目的标题截断长度
_CLOSED_WIP_TITLE_MAX = 24

# T14：tasks 表的 kind 分组与状态映射
_TODO_KINDS = ("recurring", "phased_recurring", "triggered")
_WIP_KINDS = ("one_shot", "long_running")
_TODO_STATUS_IN = {
    "pending": "active", "in_progress": "active",
    "done": "completed", "skipped": "completed",
    "blocked": "blocked", "archived": "archived",
    "active": "active", "paused": "paused", "completed": "completed",
}
_TODO_STATUS_OUT = {
    "active": "pending", "paused": "paused", "blocked": "blocked",
    "completed": "done", "archived": "archived",
}
_TODO_STATUS_FILTER = {
    "pending": ("active", "paused"), "in_progress": ("active",),
    "done": ("completed",), "skipped": ("completed",),
    "blocked": ("blocked",), "archived": ("archived",),
    "active": ("active",), "paused": ("paused",), "completed": ("completed",),
}
_WIP_STATUS_FILTER = {
    "active": ("active",), "paused": ("paused",), "blocked": ("blocked",),
    "completed": ("completed", "done"), "archived": ("archived",),
    "done": ("completed", "done"), "cancelled": ("archived", "cancelled"),
}


class TodosStore:
    """待办存储层（共享 memory.db）"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        # 单连接 check_same_thread=False 被多线程共享（router 读走 asyncio.to_thread、
        # loop_actions 后台轮询、事件循环线程写），同连接并发 step 会立即报
        # "database is locked"（不走 busy_timeout）。读写必须全互斥；
        # RLock 是因为 get_stats→get_due_todos→list_todos 存在同线程嵌套加锁。
        self._conn_lock = threading.RLock()

    def initialize(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(TODOS_SCHEMA_SQL)
        self._migrate_schema()
        self._conn.executescript(_TASK_INDEXES_SQL)
        self._migrate_tasks_v1()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """幂等升级已有 DB：添加新列（列已存在时忽略错误）。"""
        assert self._conn is not None  # initialize() 内调用，连接已建立
        for stmt in _ALTER_STATEMENTS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # 列已存在

    def _table_exists(self, name: str) -> bool:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _migrate_tasks_v1(self) -> None:
        """T14：把 todos / wip_tasks 的影子数据搬进 tasks（不切读写，先建影子）。

        幂等：成功搬迁后写 task_merge_meta.tasks_v1；老表不动，回滚时直接恢复。
        """
        assert self._conn is not None
        if not self._table_exists("tasks"):
            return
        done = self._conn.execute(
            "SELECT value FROM task_merge_meta WHERE key='tasks_v1'"
        ).fetchone()
        if done:
            return
        has_todos = self._table_exists("todos")
        has_wip = self._table_exists("wip_tasks")
        todos_n = self._conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] if has_todos else 0
        wip_n = self._conn.execute("SELECT COUNT(*) FROM wip_tasks").fetchone()[0] if has_wip else 0
        if not todos_n and not wip_n:
            return  # 空库：不写标志，等历史导入落库后再搬
        try:
            self._conn.execute("PRAGMA wal_checkpoint(FULL)")
            dst = f"{self.db_path}.bak.tasks_merge.{time.strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(self.db_path, dst)
            logger.info("T14 迁移前备份: %s", dst)
        except Exception as exc:  # 备份失败不阻断，但必须留痕
            logger.warning("T14 迁移前备份失败（继续迁移）: %s", exc)
        with self._conn_lock:
            if has_todos:
                self._conn.execute(
                    """INSERT OR IGNORE INTO tasks (
                           id, kind, title, status, priority, task_class, purpose,
                           goal, done_when, progress, next_steps, current_state,
                           blocked_reason, notes, tags, skill, related_skills,
                           related_files, related_memory_keys, frequency, start_date,
                           end_date, condition, condition_status, trigger_condition,
                           due_at, next_due_at, last_done_at, next_review_at,
                           extra_data, created_at, updated_at)
                       SELECT id, type, title,
                           CASE status
                               WHEN 'pending' THEN 'active'
                               WHEN 'in_progress' THEN 'active'
                               WHEN 'done' THEN 'completed'
                               WHEN 'skipped' THEN 'completed'
                               ELSE status END,
                           'medium', task_class, purpose, '', NULL, 0, '[]', '{}',
                           NULL, COALESCE(notes, ''), '[]', skill, '[]', '[]',
                           COALESCE(related_memory_keys, '[]'), frequency, start_date,
                           end_date, condition, condition_status, trigger_condition,
                           NULL, next_due_at, last_done_at, next_review_at,
                           CASE WHEN status='skipped'
                                THEN '{"legacy_status":"skipped"}' ELSE '{}' END,
                           created_at, updated_at
                       FROM todos""",
                )
            if has_wip:
                self._conn.execute(
                    """INSERT OR IGNORE INTO tasks (
                           id, kind, title, status, priority, task_class, purpose,
                           goal, done_when, progress, next_steps, current_state,
                           blocked_reason, notes, tags, skill, related_skills,
                           related_files, related_memory_keys, frequency, start_date,
                           end_date, condition, condition_status, trigger_condition,
                           due_at, next_due_at, last_done_at, next_review_at,
                           extra_data, created_at, updated_at)
                       SELECT id,
                           CASE WHEN due_at IS NOT NULL AND due_at != ''
                                THEN 'one_shot' ELSE 'long_running' END,
                           title, status, priority, task_class, purpose,
                           COALESCE(goal, ''), done_when, COALESCE(progress, 0),
                           COALESCE(next_steps, '[]'), COALESCE(current_state, '{}'),
                           blocked_reason, '', COALESCE(tags, '[]'), NULL,
                           COALESCE(related_skills, '[]'), COALESCE(related_files, '[]'),
                           COALESCE(related_memory_keys, '[]'),
                           NULL, NULL, NULL, NULL, NULL, NULL,
                           due_at, NULL, NULL, next_review_at,
                           COALESCE(extra_data, '{}'), created_at, updated_at
                       FROM wip_tasks""",
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO task_merge_meta (key, value) VALUES ('tasks_v1', ?)",
                (f"{todos_n}+{wip_n}",),
            )
            self._conn.commit()
        logger.info("T14 tasks 影子迁移完成: todos=%d wip=%d", todos_n, wip_n)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.initialize()
        assert self._conn is not None  # initialize() 后必有连接，收窄返回类型
        return self._conn

    # ─── 内部工具 ─────────────────────────────────────────────

    @staticmethod
    def _serialize_field(field: str, value, is_wip: bool) -> str:
        json_fields = _WIP_JSON_FIELDS if is_wip else _TODO_JSON_FIELDS
        if field in json_fields:
            return json.dumps(value if value is not None else [], ensure_ascii=False)
        return value if value is not None else ""

    @staticmethod
    def _deserialize_row(row, is_wip: bool) -> dict:
        d = dict(row) if hasattr(row, 'keys') else {}
        if not d:
            columns = [desc[0] for desc in row.cursor.description]
            d = dict(zip(columns, row))
        json_fields = _WIP_JSON_FIELDS if is_wip else _TODO_JSON_FIELDS
        for field in json_fields:
            if field in d and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = [] if field != "current_state" else {}
        return d

    @classmethod
    def _deserialize_task_row(cls, row, *, legacy_todo: bool = True) -> dict:
        """tasks 行的反序列化；todo 行默认把 canonical 状态映射回旧词。"""
        d = cls._deserialize_row(row, is_wip=False)  # 先按 todo 集合反序列化
        kind = str(d.get("kind") or "")
        is_wip = kind in _WIP_KINDS
        if is_wip:
            d = cls._deserialize_row(row, is_wip=True)
        d["table"] = "wip_tasks" if is_wip else "todos"
        if not is_wip:
            d["type"] = kind
            if legacy_todo:
                d["status"] = _TODO_STATUS_OUT.get(str(d.get("status")), d.get("status"))
        return d

    # ─── Todos CRUD ──────────────────────────────────────────

    def create_todo(self, todo: dict) -> dict:
        import uuid
        todo_id = todo.get("id") or f"todo_{uuid.uuid4().hex[:8]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        condition = todo.get("condition")
        condition_status = todo.get("condition_status")
        if condition and not condition_status:
            condition_status = "active"
        with self._conn_lock:
            self.conn.execute(
                """INSERT INTO tasks (id, kind, title, status, skill, frequency,
                       condition, condition_status, notes, related_memory_keys,
                       start_date, end_date, trigger_condition, task_class, purpose,
                       next_review_at, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    todo_id,
                    todo.get("type", "recurring"),
                    todo["title"],
                    todo.get("skill"),
                    todo.get("frequency"),
                    condition,
                    condition_status,
                    todo.get("notes", ""),
                    self._serialize_field("related_memory_keys",
                                          todo.get("related_memory_keys", []), is_wip=False),
                    todo.get("start_date"),
                    todo.get("end_date"),
                    todo.get("trigger_condition"),
                    todo.get("task_class"),
                    todo.get("purpose"),
                    todo.get("next_review_at"),
                    now, now,
                ),
            )
            self.conn.commit()
        result = self.get_todo(todo_id)
        assert result is not None  # 刚 INSERT 的行必存在，收窄返回类型
        return result

    def get_todo(self, todo_id: str) -> Optional[dict]:
        with self._conn_lock:
            cur = self.conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND kind IN (?, ?, ?)",
                (todo_id, *_TODO_KINDS),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._deserialize_task_row(row, legacy_todo=True)

    def list_todos(self, status: Optional[str] = None,
                   todo_type: Optional[str] = None) -> list[dict]:
        conditions = ["kind IN (?, ?, ?)"]
        params: list = list(_TODO_KINDS)
        if status:
            allowed = _TODO_STATUS_FILTER.get(status, (status,))
            conditions.append(f"status IN ({','.join('?' * len(allowed))})")
            params.extend(allowed)
        if todo_type:
            conditions.append("kind = ?")
            params.append(todo_type)
        where = " AND ".join(conditions)
        with self._conn_lock:
            cur = self.conn.execute(
                f"SELECT * FROM tasks WHERE {where} ORDER BY updated_at DESC", params
            )
            rows = cur.fetchall()
        return [self._deserialize_task_row(r) for r in rows]

    def update_todo(self, todo_id: str, updates: dict) -> Optional[dict]:
        if not updates:
            return self.get_todo(todo_id)
        set_clauses = []
        params = []
        for field, value in updates.items():
            if field not in _TODO_UPDATE_FIELDS:
                continue
            col = "kind" if field == "type" else field
            if field == "status":
                value = _TODO_STATUS_IN.get(str(value), value)
            set_clauses.append(f"{col} = ?")
            params.append(self._serialize_field(field, value, is_wip=False))
        if not set_clauses:
            return self.get_todo(todo_id)
        set_clauses.append("updated_at = datetime('now', 'localtime')")
        params.extend([todo_id, *_TODO_KINDS])
        with self._conn_lock:
            self.conn.execute(
                f"UPDATE tasks SET {', '.join(set_clauses)} "
                f"WHERE id = ? AND kind IN (?, ?, ?)",
                params,
            )
            self.conn.commit()
        return self.get_todo(todo_id)

    def delete_todo(self, todo_id: str) -> bool:
        with self._conn_lock:
            cur = self.conn.execute(
                "DELETE FROM tasks WHERE id = ? AND kind IN (?, ?, ?)",
                (todo_id, *_TODO_KINDS),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def mark_done(self, todo_id: str, done_date: Optional[str] = None) -> Optional[dict]:
        today = done_date or datetime.now().strftime("%Y-%m-%d")
        todo = self.get_todo(todo_id)
        if not todo:
            return None
        todo_type = todo.get("type", "recurring")
        next_due = None
        new_status = "active"
        if todo.get("frequency"):
            next_due = self._compute_next_due(
                todo["frequency"], today,
                start_date=todo.get("start_date"),
                end_date=todo.get("end_date"),
                todo_type=todo_type,
            )
            # phased_recurring 超出 end_date → next_due 返回 None → 置 archived
            if todo_type == "phased_recurring" and next_due is None:
                new_status = "archived"
        # triggered 类型 mark_done 时不计算 next_due（等下次事件触发）
        if todo_type == "triggered":
            next_due = None
            new_status = "active"
        with self._conn_lock:
            self.conn.execute(
                """UPDATE tasks SET status=?, last_done_at=?, next_due_at=?,
                       updated_at=datetime('now', 'localtime')
                   WHERE id=? AND kind IN (?, ?, ?)""",
                (new_status, today, next_due, todo_id, *_TODO_KINDS),
            )
            self.conn.commit()
        return self.get_todo(todo_id)

    def get_due_todos(self) -> list[dict]:
        """返回所有到期待办。

        - recurring：按 next_due_at <= today 或从未完成判断
        - phased_recurring：同 recurring，但 start_date > today 时不 due；status=archived 跳过
        - triggered：不进 due 列表（由 loop 轮询触发，触发时单独置 next_due_at=today）
        """
        today = datetime.now().strftime("%Y-%m-%d")
        # 同时查 recurring 和 phased_recurring（triggered 不进 due 列表）
        # list_todos 自带 _conn_lock（RLock 同线程重入安全）
        todos = []
        for t in self.list_todos():
            if t.get("type") not in ("recurring", "phased_recurring"):
                continue
            if t.get("status") in ("archived", "paused"):
                continue
            todos.append(t)
        due = []
        for todo in todos:
            # phased_recurring 在 start_date 之前不算 due
            start_date = todo.get("start_date")
            if start_date and start_date > today:
                continue
            if todo.get("condition"):
                if todo.get("condition_status") == "inactive":
                    continue
                due.append({**todo, "condition_check_needed": True})
                continue
            if not todo.get("last_done_at"):
                due.append(todo)
                continue
            if todo.get("next_due_at") and todo["next_due_at"] <= today:
                due.append(todo)
        return due

    @staticmethod
    def _compute_next_due(frequency: str, done_date: str,
                          start_date: Optional[str] = None,
                          end_date: Optional[str] = None,
                          todo_type: str = "recurring") -> Optional[str]:
        """计算下次到期日期。

        Args:
            frequency: daily/weekly/monthly
            done_date: 完成日期（ISO YYYY-MM-DD）
            start_date: phased_recurring 起始日期（仅用于语义校验，不影响递推）
            end_date: phased_recurring 结束日期；next_due > end_date 时返回 None
            todo_type: recurring / phased_recurring（triggered 不调此方法）

        Returns:
            ISO 日期字符串，或 None（phased_recurring 超出 end_date，调用方应置 archived）
        """
        dt = datetime.fromisoformat(done_date)
        if frequency == "daily":
            nxt = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
        elif frequency == "weekly":
            nxt = (dt + timedelta(days=7)).strftime("%Y-%m-%d")
        elif frequency == "monthly":
            # 自然月：下个月同一天；月末兜底（如 1月31日 → 2月28/29日）
            month = dt.month
            year = dt.year
            if month == 12:
                next_month, next_year = 1, year + 1
            else:
                next_month, next_year = month + 1, year
            max_day = calendar.monthrange(next_year, next_month)[1]
            day = min(dt.day, max_day)
            nxt = datetime(next_year, next_month, day).strftime("%Y-%m-%d")
        elif frequency == "quarterly":
            # 自然季度：3 个月后同一天；月末兜底（如 11月30日 → 次年2月28/29日）
            month = dt.month
            year = dt.year
            new_month = month + 3
            if new_month > 12:
                next_month, next_year = new_month - 12, year + 1
            else:
                next_month, next_year = new_month, year
            max_day = calendar.monthrange(next_year, next_month)[1]
            day = min(dt.day, max_day)
            nxt = datetime(next_year, next_month, day).strftime("%Y-%m-%d")
        else:
            logger.warning(f"未知 frequency={frequency!r}，next_due 保留为 done_date")
            nxt = done_date
        # phased_recurring：超出 end_date → 返回 None（调用方置 archived）
        if todo_type == "phased_recurring" and end_date and nxt > end_date:
            return None
        return nxt

    def check_trigger(self, todo_id: str, event_payload: dict) -> dict:
        """检查 triggered 类型任务的触发条件是否满足。

        首版支持 event=file_arrived：检查 event_payload 中的文件路径是否匹配
        trigger_condition 的 watch_dir + pattern。

        触发时更新 next_due_at=today（作为"已触发待处理"标记，agent 通过
        todos_list(todo_type='triggered') 查询 next_due_at=today 的任务）。
        agent 处理完后调 mark_done 重置 next_due_at=NULL。

        Args:
            todo_id: triggered 类型的 todo id
            event_payload: 事件数据，如
                {"event": "file_arrived", "path": "E:/test/data.csv", "filename": "data.csv"}

        Returns:
            {"triggered": bool, "reason": str, "todo_id": str}
        """
        todo = self.get_todo(todo_id)
        if not todo:
            return {"triggered": False, "reason": f"todo {todo_id} 不存在", "todo_id": todo_id}
        if todo.get("type") != "triggered":
            return {"triggered": False, "reason": f"todo 类型非 triggered", "todo_id": todo_id}
        # 已触发未处理（next_due_at=today 且非 NULL）→ 不重复触发
        today = datetime.now().strftime("%Y-%m-%d")
        if todo.get("next_due_at") and todo["next_due_at"] <= today and todo.get("last_done_at") != today:
            return {"triggered": False, "reason": "已触发待处理，等 agent mark_done", "todo_id": todo_id}

        raw_cond = todo.get("trigger_condition")
        if not raw_cond:
            return {"triggered": False, "reason": "trigger_condition 为空", "todo_id": todo_id}
        try:
            cond = json.loads(raw_cond) if isinstance(raw_cond, str) else raw_cond
        except (json.JSONDecodeError, TypeError):
            return {"triggered": False, "reason": "trigger_condition JSON 解析失败", "todo_id": todo_id}

        event_type = cond.get("event", "")
        payload_event = event_payload.get("event", "")
        if event_type != payload_event:
            return {"triggered": False, "reason": f"event 不匹配 (cond={event_type}, payload={payload_event})", "todo_id": todo_id}

        if event_type == "file_arrived":
            watch_dir = cond.get("watch_dir", "")
            pattern = cond.get("pattern", "*")
            path = event_payload.get("path", "")
            filename = event_payload.get("filename", "")
            # 路径前缀匹配 + 文件名 glob 匹配
            if watch_dir and not path.replace("\\", "/").startswith(watch_dir.replace("\\", "/")):
                return {"triggered": False, "reason": f"path 不在 watch_dir 下", "todo_id": todo_id}
            if pattern and pattern != "*":
                import fnmatch
                if not fnmatch.fnmatch(filename, pattern):
                    return {"triggered": False, "reason": f"filename 不匹配 pattern={pattern}", "todo_id": todo_id}
            reason = f"file_arrived: {filename} 匹配 {pattern} in {watch_dir}"
        else:
            return {"triggered": False, "reason": f"暂不支持 event={event_type}", "todo_id": todo_id}

        # 触发：更新 next_due_at=today（标记已触发待处理）
        with self._conn_lock:
            self.conn.execute(
                """UPDATE tasks SET next_due_at=?, condition_status='active',
                       updated_at=datetime('now', 'localtime')
                   WHERE id=? AND kind IN (?, ?, ?)""",
                (today, todo_id, *_TODO_KINDS),
            )
            self.conn.commit()
        return {"triggered": True, "reason": reason, "todo_id": todo_id}

    # ─── WipTasks CRUD ───────────────────────────────────────

    def create_wip(self, task: dict) -> dict:
        import uuid
        task_id = task.get("id") or f"wip_{uuid.uuid4().hex[:8]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn_lock:
            kind = "one_shot" if str(task.get("due_at") or "").strip() else "long_running"
            self.conn.execute(
                """INSERT INTO tasks (id, kind, title, status, priority, goal, progress,
                       tags, next_steps, related_skills, related_files,
                       current_state, blocked_reason, related_memory_keys, extra_data,
                       task_class, purpose, due_at, done_when, next_review_at,
                       created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    kind,
                    task["title"],
                    task.get("priority", "medium"),
                    task.get("goal", ""),
                    self._serialize_field("tags", task.get("tags", []), is_wip=True),
                    self._serialize_field("next_steps", task.get("next_steps", []), is_wip=True),
                    self._serialize_field("related_skills", task.get("related_skills", []), is_wip=True),
                    self._serialize_field("related_files", task.get("related_files", []), is_wip=True),
                    self._serialize_field("current_state", task.get("current_state", {}), is_wip=True),
                    task.get("blocked_reason"),
                    self._serialize_field("related_memory_keys",
                                          task.get("related_memory_keys", []), is_wip=True),
                    self._serialize_field("extra_data", task.get("extra_data", {}), is_wip=True),
                    task.get("task_class"),
                    task.get("purpose"),
                    task.get("due_at"),
                    task.get("done_when"),
                    task.get("next_review_at"),
                    now, now,
                ),
            )
            self.conn.commit()
        result = self.get_wip(task_id)
        assert result is not None  # 刚 INSERT 的行必存在，收窄返回类型
        return result

    def get_wip(self, task_id: str) -> Optional[dict]:
        with self._conn_lock:
            cur = self.conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND kind IN (?, ?)",
                (task_id, *_WIP_KINDS),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._deserialize_task_row(row)

    def get_wip_many(self, ids: list) -> dict:
        """按 id 批量取 WIP 详情（保序；不存在的 id 单独列出）。

        供 agent 在看完 wip_list(summary=True) 摘要后，只取自己真正关心的几条详情，
        替代"把 summary 关掉全量拉取"的做法。

        Returns:
            {"tasks": [...], "missing": [...]}
        """
        if not ids:
            return {"tasks": [], "missing": []}
        uniq = list(dict.fromkeys(ids))  # 去重保序
        placeholders = ",".join("?" * len(uniq))
        with self._conn_lock:
            cur = self.conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders}) AND kind IN (?, ?)",
                (*uniq, *_WIP_KINDS),
            )
            rows = cur.fetchall()
        by_id = {r["id"]: self._deserialize_task_row(r) for r in rows}
        return {
            "tasks": [by_id[i] for i in uniq if i in by_id],
            "missing": [i for i in uniq if i not in by_id],
        }

    def list_wip(self, status: Optional[str] = None, summary: bool = True) -> list[dict]:
        """列出 WIP 任务。

        summary=True（默认）只返回轻量字段，其中：
        - 未完结（active/paused/blocked）→ id/title/status/priority/progress/updated_at
        - 已完结（completed/done/archived）→ 进一步压到 id/title(截断)/status/updated_at，
          并带 closed=True 标记。这类条目基本无用，不该占据上下文。

        需要完整字段（goal/next_steps/current_state/related_*/extra_data）时，
        用 get_wip(task_id) 单条取或 get_wip_many(ids) 批量取，而不是把 summary 关掉全量拉取。
        """
        conditions = ["kind IN (?, ?)"]
        params: list = list(_WIP_KINDS)
        if status:
            allowed = _WIP_STATUS_FILTER.get(status, (status,))
            conditions.append(f"status IN ({','.join('?' * len(allowed))})")
            params.extend(allowed)
        where = " AND ".join(conditions)
        if summary:
            cols = "id, title, status, priority, progress, updated_at"
            with self._conn_lock:
                cur = self.conn.execute(
                    f"SELECT {cols} FROM tasks WHERE {where} ORDER BY updated_at DESC",
                    params,
                )
                return [self._compact_wip_summary(dict(r)) for r in cur.fetchall()]
        with self._conn_lock:
            cur = self.conn.execute(
                f"SELECT * FROM tasks WHERE {where} ORDER BY updated_at DESC",
                params,
            )
            rows = cur.fetchall()
        return [self._deserialize_task_row(r) for r in rows]

    @staticmethod
    def _compact_wip_summary(row: dict) -> dict:
        """summary 模式下压缩单条记录。

        未完结条目原样返回（6 个轻量字段）；
        已完结条目只保留 id/status/updated_at + 截断标题，并带 closed=True 标记，
        让调用方能区分"字段被压缩"与"字段缺失"。
        """
        if row.get("status") not in _CLOSED_WIP_STATUSES:
            return row
        title = row.get("title") or ""
        if len(title) > _CLOSED_WIP_TITLE_MAX:
            title = title[:_CLOSED_WIP_TITLE_MAX].rstrip() + "…"
        return {
            "id": row.get("id"),
            "title": title,
            "status": row.get("status"),
            "updated_at": row.get("updated_at"),
            "closed": True,
        }

    def update_wip(self, task_id: str, updates: dict) -> Optional[dict]:
        if not updates:
            return self.get_wip(task_id)
        set_clauses = []
        params = []
        for field, value in updates.items():
            if field not in _WIP_UPDATE_FIELDS:
                continue
            if field == "status":
                value = {"done": "completed", "cancelled": "archived"}.get(str(value), value)
            set_clauses.append(f"{field} = ?")
            params.append(self._serialize_field(field, value, is_wip=True))
        if not set_clauses:
            return self.get_wip(task_id)
        set_clauses.append("updated_at = datetime('now', 'localtime')")
        params.extend([task_id, *_WIP_KINDS])
        with self._conn_lock:
            self.conn.execute(
                f"UPDATE tasks SET {', '.join(set_clauses)} "
                f"WHERE id = ? AND kind IN (?, ?)",
                params,
            )
            self.conn.commit()
        return self.get_wip(task_id)

    def delete_wip(self, task_id: str) -> bool:
        with self._conn_lock:
            cur = self.conn.execute(
                "DELETE FROM tasks WHERE id = ? AND kind IN (?, ?)",
                (task_id, *_WIP_KINDS),
            )
            self.conn.commit()
            return cur.rowcount > 0

    # ─── 统一动作面（T14）────────────────────────────────────

    def list_tasks_view(self, include_archived: bool = True) -> list[dict]:
        """面板用：tasks 单表的轻量列表（canonical 状态，不返回正文大字段）。"""
        sql = (
            "SELECT id, kind, title, status, priority, task_class, purpose, progress, "
            "done_when, due_at, next_due_at, updated_at, created_at FROM tasks"
        )
        if not include_archived:
            sql += " WHERE status != 'archived'"
        sql += " ORDER BY updated_at DESC, id DESC"
        with self._conn_lock:
            rows = self.conn.execute(sql).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["table"] = "wip_tasks" if d["kind"] in _WIP_KINDS else "todos"
            out.append(d)
        return out

    def apply_task_action(self, scope: str, task_id: str, action: str,
                          reason: str | None = None) -> Optional[dict]:
        """统一 pause/resume/archive/unarchive/complete；todos 与 wip 同一套词。

        v15 起 archive/unarchive 额外维护归档三列（面板要有「归档于 …」和
        「归档原因」，也要能原样恢复）：

        - `archived_at`        收进归档视图的时间（面板排序/显示用）；
        - `archive_prev_status` 归档前的 status，恢复时照抄回去——不留这一列，
                                一个 done 的事务恢复后会变成 active，进度就丢了；
        - `archive_reason`     归档原因，可空。

        归档视图仍按 `status='archived'` 判断（沿用 T14，不另造判据），
        所以别的入口把状态置 archived 也会正常出现在归档里，只是没有时间戳。
        """
        status_map = {
            "pause": "paused", "resume": "active",
            "archive": "archived", "unarchive": "active", "complete": "completed",
            # 2026-10-28：面板要能直接标记「卡住」，原来 5 个状态里唯独缺这个。
            "block": "blocked",
        }
        if action not in status_map:
            raise ValueError(f"unknown action: {action}")
        if scope == "todos" and action == "complete":
            done = self.mark_done(task_id)
            if not done:
                return None
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._deserialize_task_row(row, legacy_todo=False) if row else None
        kinds = _TODO_KINDS if scope == "todos" else _WIP_KINDS
        placeholders = ",".join("?" * len(kinds))
        now = "datetime('now', 'localtime')"
        params: list = []
        if action == "archive":
            sets = (f"status = ?, updated_at = {now}, archived_at = {now}, "
                    # 重复归档时保留首次记下的原状态
                    "archive_prev_status = COALESCE(NULLIF(status, 'archived'), "
                    "archive_prev_status, status), archive_reason = ?")
            params = ["archived", reason]
        elif action == "unarchive":
            sets = (f"status = COALESCE(NULLIF(archive_prev_status, ''), 'active'), "
                    f"updated_at = {now}, archived_at = NULL, "
                    "archive_prev_status = NULL, archive_reason = NULL")
        else:
            sets = f"status = ?, updated_at = {now}"
            params = [status_map[action]]
            if action == "complete" and scope == "wip":
                sets += ", progress = 100"
        params.extend([task_id, *kinds])
        with self._conn_lock:
            cur = self.conn.execute(
                f"UPDATE tasks SET {sets} WHERE id = ? AND kind IN ({placeholders})",
                params,
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._deserialize_task_row(row, legacy_todo=False) if row else None

    # ─── 统计 ─────────────────────────────────────────────────

    # ─── 事务分类（DL2 / 2026-10-27）────────────────────────────
    #
    # 结构轴决定到期表现，目的轴决定交到哪一步；两轴都只加列，不动老字段。
    # L0-L5 不在这里 —— 那是授权刻度（40_用户标注），与目的轴不换算。

    def _task_rows(self) -> list[dict]:
        """todos + wip 统一成一种行，带上"库里已落 / 规则会判成什么"。"""
        out: list[dict] = []
        with self._conn_lock:
            rows = self.conn.execute("SELECT * FROM tasks").fetchall()
        for r in rows:
            d = self._deserialize_task_row(r, legacy_todo=False)
            if d["table"] == "todos":
                c = classify_todo(d)
            else:
                c = classify_wip(d)
            d["class_notes"] = c.notes
            d["class_source"] = "db" if d.get("task_class") else ("rule" if c.task_class else "unmapped")
            if not d.get("task_class"):
                d["task_class"] = c.task_class
            if not d.get("purpose") and c.purpose:
                d["purpose"] = c.purpose
            out.append(d)
        return out

    def classify_tasks(self, apply: bool = False) -> dict:
        """按规则给现有 todos / wip 打标。

        默认 **apply=False 只出报告**（dry run）。`unmapped` 与 `purpose_unset`
        必须原样返回：映射不上的不许挑个近的类别塞进去，目的轴判不出的不许猜。
        """
        assignments: list[dict] = []
        unmapped: list[dict] = []
        purpose_unset: list[dict] = []
        unchanged = 0

        for row in self._task_rows():
            c = classify_todo(row) if row["table"] == "todos" else classify_wip(row)
            if c.unmapped:
                unmapped.append(
                    {
                        "id": row.get("id"),
                        "table": row["table"],
                        "title": row.get("title"),
                        "status": row.get("status"),
                        "notes": c.notes,
                    }
                )
                continue
            new_purpose = row.get("purpose") or c.purpose
            if not new_purpose and str(row.get("status") or "") not in (
                "completed", "done", "archived", "cancelled", "skipped"
            ):
                purpose_unset.append(
                    {"id": row.get("id"), "table": row["table"], "title": row.get("title")}
                )
            item = {
                "id": row.get("id"),
                "table": row["table"],
                "title": row.get("title"),
                "from_class": row.get("task_class") if row.get("class_source") == "db" else None,
                "task_class": c.task_class,
                "purpose": new_purpose,
                "notes": c.notes,
            }
            if row.get("class_source") == "db" and row.get("task_class") == c.task_class \
                    and row.get("purpose") == new_purpose:
                unchanged += 1
                continue
            assignments.append(item)

        if apply:
            for a in assignments:
                self.set_task_class(
                    a["table"], str(a["id"]),
                    task_class=a["task_class"], purpose=a["purpose"],
                )

        return {
            "applied": apply,
            "planned": len(assignments),
            "unchanged": unchanged,
            "assignments": assignments,
            "unmapped": unmapped,
            "purpose_unset": purpose_unset,
        }

    def set_task_class(
        self,
        table: str,
        task_id: str,
        *,
        task_class: Optional[str] = None,
        purpose: Optional[str] = None,
        due_at: Optional[str] = None,
        done_when: Optional[str] = None,
        next_review_at: Optional[str] = None,
    ) -> Optional[dict]:
        """人工改分类。task_class/purpose 只接受白名单值，非法直接 ValueError（不静默忽略）。"""
        if table not in ("todos", "wip_tasks"):
            raise ValueError(f"未知表 {table!r}（只支持 todos / wip_tasks）")
        if task_class is not None and task_class not in STRUCT_TYPES:
            raise ValueError(f"task_class={task_class!r} 不在四类里：{list(STRUCT_TYPES)}")
        if purpose is not None and purpose not in PURPOSES:
            raise ValueError(f"purpose={purpose!r} 不在三档里：{list(PURPOSES)}")
        if table == "todos" and (due_at is not None or done_when is not None):
            raise ValueError("todos 用 next_due_at，不设 due_at / done_when（那是 wip 的列）")

        updates: dict[str, Optional[str]] = {"task_class": task_class, "purpose": purpose}
        if next_review_at is not None:
            updates["next_review_at"] = next_review_at
        if table == "wip_tasks":
            if due_at is not None:
                updates["due_at"] = due_at
            if done_when is not None:
                updates["done_when"] = done_when
        updates = {k: v for k, v in updates.items() if v is not None}
        if not updates:
            return self.get_todo(task_id) if table == "todos" else self.get_wip(task_id)

        set_clauses = ", ".join(f"{k} = ?" for k in updates)
        kinds = _TODO_KINDS if table == "todos" else _WIP_KINDS
        params = list(updates.values()) + [task_id, *kinds]
        placeholders = ",".join("?" * len(kinds))
        with self._conn_lock:
            self.conn.execute(
                f"UPDATE tasks SET {set_clauses}, updated_at = datetime('now', 'localtime') "
                f"WHERE id = ? AND kind IN ({placeholders})",
                params,
            )
            self.conn.commit()
        return self.get_todo(task_id) if table == "todos" else self.get_wip(task_id)

    def task_queue(self, now: Optional[datetime] = None) -> list[dict]:
        """按 `15` §5.2 的默认顺序出"现在该做什么"。"""
        return build_queue(self._task_rows(), now)

    def task_summary(self, now: Optional[datetime] = None) -> dict:
        """分类报告：两轴计数 + 派生视图 + 待判清单。"""
        rows = self._task_rows()
        out = summarize(rows, now)
        out["by_table"] = {
            "todos": sum(1 for r in rows if r["table"] == "todos"),
            "wip_tasks": sum(1 for r in rows if r["table"] == "wip_tasks"),
        }
        out["class_source"] = {
            "db": sum(1 for r in rows if r.get("class_source") == "db"),
            "rule": sum(1 for r in rows if r.get("class_source") == "rule"),
            "unmapped": sum(1 for r in rows if r.get("class_source") == "unmapped"),
        }
        return out

    def materialize_occurrences(self, now: Optional[datetime] = None) -> dict:
        """把到期的循环模板落成 occurrence（幂等：靠 (template_id, due_at) 唯一索引）。"""
        import uuid

        now = now or datetime.now()
        today = now.date()
        created: list[dict] = []
        existing: list[dict] = []
        skipped: list[dict] = []

        for todo in self.list_todos():
            cls = todo.get("task_class") or classify_todo(todo).task_class
            if cls != STRUCT_RECURRING:
                continue
            if todo.get("status") in ("archived", "done"):
                continue
            due = as_date(todo.get("next_due_at"))
            if due is None:
                skipped.append({"template_id": todo.get("id"), "why": "无 next_due_at（未排期）"})
                continue
            if due > today:
                skipped.append({"template_id": todo.get("id"), "why": f"未到期（{due}）"})
                continue
            occ_id = f"occ_{uuid.uuid4().hex[:10]}"
            with self._conn_lock:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO task_occurrences
                           (id, template_id, due_at, status, created_at)
                       VALUES (?, ?, ?, 'pending', ?)""",
                    (occ_id, todo.get("id"), due.isoformat(), now.strftime("%Y-%m-%d %H:%M:%S")),
                )
                self.conn.commit()
                inserted = cur.rowcount > 0
            rec = {"template_id": todo.get("id"), "due_at": due.isoformat(), "title": todo.get("title")}
            (created if inserted else existing).append(rec)

        return {"created": created, "existing": existing, "skipped": skipped}

    def list_occurrences(self, status: Optional[str] = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM task_occurrences"
        args: list = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY due_at DESC LIMIT ?"
        args.append(limit)
        with self._conn_lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def complete_occurrence(
        self, occ_id: str, evidence: Optional[str] = None, advance_template: bool = True
    ) -> Optional[dict]:
        """完成一次 occurrence：记证据 → 推进模板 → 更早的 pending 判 missed。"""
        with self._conn_lock:
            row = self.conn.execute(
                "SELECT * FROM task_occurrences WHERE id = ?", (occ_id,)
            ).fetchone()
            if not row:
                return None
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.conn.execute(
                """UPDATE task_occurrences SET status = 'done', done_at = ?, evidence = ?
                   WHERE id = ?""",
                (now_text, evidence, occ_id),
            )
            if advance_template:
                self.conn.execute(
                    """UPDATE task_occurrences SET status = 'missed'
                       WHERE template_id = ? AND status = 'pending' AND due_at < ?""",
                    (row["template_id"], row["due_at"]),
                )
            self.conn.commit()
        if advance_template:
            self.mark_done(str(row["template_id"]))
        return {
            "id": occ_id,
            "template_id": row["template_id"],
            "status": "done",
            "done_at": now_text,
            "template_advanced": bool(advance_template),
        }

    def get_stats(self) -> dict:
        kinds_todo = ",".join("?" * len(_TODO_KINDS))
        kinds_wip = ",".join("?" * len(_WIP_KINDS))
        with self._conn_lock:  # get_due_todos 内部经 list_todos 重入，RLock 安全
            todos_total = self.conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE kind IN ({kinds_todo})", _TODO_KINDS
            ).fetchone()[0]
            todos_due = len(self.get_due_todos())
            wip_total = self.conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE kind IN ({kinds_wip})", _WIP_KINDS
            ).fetchone()[0]
            wip_active = self.conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE kind IN ({kinds_wip}) AND status = 'active'",
                _WIP_KINDS,
            ).fetchone()[0]
            type_counts = {}
            for row in self.conn.execute(
                f"SELECT kind, COUNT(*) as cnt FROM tasks WHERE kind IN ({kinds_todo}) GROUP BY kind",
                _TODO_KINDS,
            ).fetchall():
                type_counts[row["kind"]] = row["cnt"]
            archived_count = self.conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE kind IN ({kinds_todo}) AND status = 'archived'",
                _TODO_KINDS,
            ).fetchone()[0]
        return {
            "available": True,
            "todos_total": todos_total,
            "todos_due": todos_due,
            "todos_by_type": type_counts,
            "todos_archived": archived_count,
            "wip_total": wip_total,
            "wip_active": wip_active,
        }

    def close(self) -> None:
        with self._conn_lock:
            if self._conn:
                self._conn.close()
                self._conn = None
