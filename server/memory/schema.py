"""SQLite 数据库 schema 和迁移"""

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 15

# v8（DL3，2026-10-27）：facts 的三轴与证据列。**只加列、不迁表**——
# 160+ 行老数据的 scope/kind 没人判过，一律留 NULL（不假装知道）。
# 设计见 workspace/local_butler/30_架构与设计/12_记忆写入漏斗_DL3实施设计_20261027.md
V8_FACT_COLUMNS: list[tuple[str, str]] = [
    ("title", "TEXT"),                    # 列表/注入的短标题
    ("summary", "TEXT"),                  # 1-2 句，注入只看这个
    ("scope", "TEXT"),                    # global / project:<id> / topic:<id>；NULL=未分类
    ("kind", "TEXT"),                     # norm/fact/pitfall/preference/procedure/reference/current_state
    ("origin", "TEXT"),                   # user_quote / agent_extracted / system / tool
    ("sensitivity", "TEXT"),              # public / private-eng / personal（N1）
    ("evidence_refs", "TEXT"),            # JSON 数组：可机器核对的证据
    ("source_quote_ids", "TEXT"),         # JSON 数组：指向 source_quotes.id
    ("must_read", "INTEGER DEFAULT 0"),   # 只允许 kind='norm'；DL1 用它取红线
    ("expires_at", "TEXT"),               # kind='current_state' 必填（Q15）
    ("dedup_hash", "TEXT"),               # sha256，幂等键（唯一部分索引）
]

# v8 新表：原话不可改写；候选不静默丢（拒收也留档给 DL5「待审候选」）
V8_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS source_quotes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_hash   TEXT NOT NULL UNIQUE,
    text         TEXT NOT NULL,
    speaker      TEXT NOT NULL DEFAULT 'user',
    project_id   TEXT,
    run_id       TEXT,
    sensitivity  TEXT NOT NULL DEFAULT 'personal',
    captured_at  TEXT NOT NULL,
    redacted_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_quotes_project ON source_quotes(project_id);
CREATE INDEX IF NOT EXISTS idx_quotes_run ON source_quotes(run_id);

CREATE TABLE IF NOT EXISTS memory_candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    submitted_at TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT,
    fact_key     TEXT,
    dedup_hash   TEXT,
    payload      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cand_status ON memory_candidates(status);
CREATE INDEX IF NOT EXISTS idx_cand_run ON memory_candidates(run_id);
"""

# v9（DL4，2026-10-27）：会话码 / run / handoff / 投递 / 轻量事件。
# 与任务/记忆同库（todos/wip 已共享 memory.db），一份备份、一次迁移。
# 设计见 workspace/local_butler/30_架构与设计/15_外部循环接入_DL4实施设计_20261027.md
V9_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS butler_runs (
    run_id       TEXT PRIMARY KEY,
    code_hash    TEXT NOT NULL UNIQUE,
    code_prefix  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'created',
    task_scope   TEXT,                     -- 'todos' / 'wip' / 'adhoc'
    task_id      TEXT,
    project_id   TEXT,
    harness      TEXT,
    owner        TEXT,
    prompt       TEXT NOT NULL DEFAULT '',
    handoff_id   TEXT,
    manifest_json TEXT,
    outcome      TEXT,
    summary      TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    opened_at    TEXT,
    heartbeat_at TEXT,
    closed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_butler_runs_status ON butler_runs(status);
CREATE INDEX IF NOT EXISTS idx_butler_runs_task ON butler_runs(task_scope, task_id);
CREATE INDEX IF NOT EXISTS idx_butler_runs_handoff ON butler_runs(handoff_id);

CREATE TABLE IF NOT EXISTS butler_handoffs (
    handoff_id   TEXT PRIMARY KEY,
    task_scope   TEXT,
    task_id      TEXT,
    project_id   TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    title        TEXT,
    payload_json TEXT NOT NULL,
    created_by   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    archived_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_butler_handoffs_status ON butler_handoffs(status);
CREATE INDEX IF NOT EXISTS idx_butler_handoffs_task ON butler_handoffs(task_scope, task_id);

CREATE TABLE IF NOT EXISTS butler_deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    bundle       TEXT NOT NULL,
    cursor       TEXT NOT NULL,
    digest       TEXT NOT NULL,
    chars        INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    acked_at     TEXT,
    UNIQUE(run_id, bundle, cursor)
);
CREATE INDEX IF NOT EXISTS idx_butler_deliveries_run ON butler_deliveries(run_id);

CREATE TABLE IF NOT EXISTS butler_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    event_type   TEXT NOT NULL,
    payload_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_butler_events_run ON butler_events(run_id, seq);
"""

# v10（P5，2026-10-27）：长会话 + 每轮报告；召回反馈落投递行。
# 只加表/加列，不迁表；老 run 的 report_count 读取按 0 处理。
V10_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS butler_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    outcome         TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    payload_json    TEXT NOT NULL DEFAULT '{}',
    result_json     TEXT,
    idempotency_key TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_butler_reports_run ON butler_reports(run_id, attempt);
CREATE UNIQUE INDEX IF NOT EXISTS idx_butler_reports_idem
    ON butler_reports(run_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS butler_notes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    client_note_id TEXT,
    direction      TEXT NOT NULL DEFAULT 'agent',   -- 'agent' / 'user'
    kind           TEXT NOT NULL DEFAULT 'note',
    text           TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    consumed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_butler_notes_run
    ON butler_notes(run_id, direction, consumed_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_butler_notes_client
    ON butler_notes(run_id, client_note_id) WHERE client_note_id IS NOT NULL;
"""

# v11（2026-10-28 用户拍板）：派单提示词版本化。
# v12 曾被用户第三批裁定「只保留当前这一个版本」而停用；v13（2026-10-29）用户
# 重新要求「有历史、可回滚、可看可改可删」，本表**重新启用**，并由
# butler_runs.prompt_version 指向当前生效的那一版。
V11_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS butler_prompt_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    prompt_text TEXT NOT NULL,
    chars       INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,          -- initial / user_edit / auto_next / manual
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, version)
);
CREATE INDEX IF NOT EXISTS idx_butler_prompt_versions_run
    ON butler_prompt_versions(run_id, version DESC);
"""

V10_RUN_COLUMNS: list[tuple[str, str]] = [
    ("blocked_on_user_json", "TEXT"),
    ("next_run_hint_json", "TEXT"),
    ("report_count", "INTEGER DEFAULT 0"),
    ("last_report_at", "TEXT"),
    ("domain_id", "TEXT"),
    # 2026-10-27 用户裁定：会话码不按秘密处理，存完整提示词以便面板随时重发
    ("prompt_text", "TEXT"),
    # 执行 agent 在 session.open 时自报；外部 harness 可能不提供，允许为空
    ("model", "TEXT"),
]

V10_DELIVERY_COLUMNS: list[tuple[str, str]] = [
    ("used", "INTEGER"),
    ("useful", "INTEGER"),
    ("feedback_note", "TEXT"),
]

V10_REPORT_COLUMNS: list[tuple[str, str]] = [
    ("result_json", "TEXT"),
]

# v12（2026-10-28 用户第三批裁定）：派单提示词只留当前一份，不再存多版本。
# prompt_source 记录这份正文是谁写的：
#   initial   建 run 时生成
#   auto_next 一轮报告后系统重渲染（遇到 user_edit 不覆盖，以用户手改版为准）
#   user_edit 用户在面板上改并保存
# 无版本表 ⇒ 保存即覆盖；想留旧内容由用户自己复制走。
V12_RUN_COLUMNS: list[tuple[str, str]] = [
    ("prompt_source", "TEXT"),
]

# v13（2026-10-29）：当前生效的提示词版本号。空 = 老数据，读侧按最大版本兜底。
V13_RUN_COLUMNS: list[tuple[str, str]] = [
    ("prompt_version", "INTEGER"),
]

# v14（2026-10-29 用户拍板）：run 补一层「执行域状态」。
#
# 为什么加在 run 上：`progress / next_steps / current_state / blocked_reason`
# 原来全写在 tasks 行（目标域），一个事务开三路时后写覆盖先写，而且
# 「一路 done」会被当成「全部 done」（见 30_架构与设计/10_边界与契约/DD-run级状态与并发.md §2）。
# 状态字段归执行域之后，每路各写自己那一行，天然无冲突。
#
# 同时给 butler_handoffs 挂 run_id：交接是「这一路交给下一任」，不是「整个事务」。
V14_RUN_COLUMNS: list[tuple[str, str]] = [
    ("label", "TEXT"),            # 这一路在做什么（≤40 字）；空则退回会话码
    ("progress", "INTEGER"),      # 本路进度 0-100，NULL = 未报
    ("next_steps_json", "TEXT"),  # 本路下一步（JSON 数组）
    ("state_note", "TEXT"),       # 本路现状一句话
]

V14_HANDOFF_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "TEXT"),           # 写这份交接的那一路；空 = v13 及以前的老数据
]

# v15（2026-10-30 用户拍板）：面板要真归档，交接提示词要能存住。
#
# 为什么归档不靠 status='archived'：那是任务状态机的一档（T14 动作面），
# 归档一个已经 done 的事务会把 done 抹掉，恢复时也无从知道该回哪一档。
# 所以归档只加时间戳 + 原文存一份 prev_status，恢复时原样还原。
#
# 交接提示词的 override 只在「还没交接」这一态生效：一旦开出交接单，
# 正文来自交接单本身，override 自然不参与（不需要额外清理动作）。
V15_TASK_COLUMNS: list[tuple[str, str]] = [
    ("archived_at", "TEXT"),            # 收进归档视图的时间；空 = 在事务面板
    ("archive_prev_status", "TEXT"),    # 归档前的 status，恢复时照抄回去
    ("archive_reason", "TEXT"),
]

V15_RUN_COLUMNS: list[tuple[str, str]] = [
    ("archived_at", "TEXT"),            # 这一路被收走的时间；空 = 还在事务面板
    ("archive_reason", "TEXT"),
    ("handoff_text_override", "TEXT"),  # 面板手改的交接提示词；空 = 用实时渲染
]


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _ensure_columns(conn: sqlite3.Connection, table: str,
                    columns: list[tuple[str, str]]) -> list[str]:
    """幂等加列（ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS）。

    表不存在时直接返回空：迁移链是按版本逐段执行的，手工造的半截库
    （或某个版本跳过了建表）不该在这里炸掉整条迁移。
    """
    if not _has_table(conn, table):
        return []
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    added: list[str] = []
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added


SCHEMA_SQL = """
-- 原始消息（时间索引）
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent',   -- 'agent', 'user', 'tool', 'system', 'manual'
    role TEXT NOT NULL DEFAULT 'assistant', -- 'user', 'assistant', 'tool', 'system'
    content TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    session_id TEXT,
    key TEXT,                               -- 向后兼容 KV 记忆的 key
    compressed INTEGER DEFAULT 0,           -- 是否已被压缩
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source);
CREATE INDEX IF NOT EXISTS idx_messages_key ON messages(key);
CREATE INDEX IF NOT EXISTS idx_messages_compressed ON messages(compressed);

-- 压缩摘要
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    summary TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_summaries_time_range ON summaries(start_time, end_time);

-- 事实（提取的关键信息）
-- v3 新增列：fact_type / occurred_at / mentioned_at / consumption_contexts / trigger_keywords
-- 这些列允许 NULL，向后兼容 v2 数据（迁移时不回填，新写入自动填）
-- v6.1 T29：fact_type 收敛为 5 类 closed（user/feedback/project/reference/experience）
--   - 旧值 'preference' 由 migrate_fact_type_v6_1.py 迁移为 'user'
--   - 旧值 'transaction' 归档后删除（业务数据走 todos/wip）
--   - 写入端 Pydantic 加 Literal 约束（君子协议：未知类型落 NULL + log warning，不强制拒绝）
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',    -- 'auto', 'manual'
    confidence REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    fact_type TEXT,                          -- v6.1 closed: 'user' | 'feedback' | 'project' | 'reference' | 'experience'（NULL=未知/旧数据）
    occurred_at TEXT,                       -- ISO 时间，事实发生时间（区别于写入时间）
    mentioned_at TEXT,                       -- ISO 时间，最近被引用时间
    consumption_contexts TEXT,              -- JSON 数组字符串，如 ["recurring.accounting", "adhoc.web_archive"]
    trigger_keywords TEXT,                  -- JSON 数组字符串，如 ["退款", "curl"]
    archived_at TEXT,                       -- C1（v7）：NULL=活跃；非 NULL=已失效（定长微秒时间戳）
    -- v8（DL3，2026-10-27）：三轴 + 证据列，见 30_架构与设计/12
    title TEXT,
    summary TEXT,
    scope TEXT,
    kind TEXT,
    origin TEXT,
    sensitivity TEXT,
    evidence_refs TEXT,
    source_quote_ids TEXT,
    must_read INTEGER DEFAULT 0,
    expires_at TEXT,
    dedup_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(fact_type);
CREATE INDEX IF NOT EXISTS idx_facts_mentioned ON facts(mentioned_at);
CREATE INDEX IF NOT EXISTS idx_facts_archived ON facts(archived_at);
CREATE INDEX IF NOT EXISTS idx_facts_scope_kind ON facts(scope, kind);
CREATE INDEX IF NOT EXISTS idx_facts_must_read ON facts(must_read) WHERE must_read = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_dedup ON facts(dedup_hash) WHERE dedup_hash IS NOT NULL;

-- 向量索引（元数据 + 向量 BLOB）
CREATE TABLE IF NOT EXISTS vector_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    text_hash TEXT NOT NULL,
    vector BLOB NOT NULL,                   -- numpy 数组序列化
    model_name TEXT NOT NULL,
    dim INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_vector_message ON vector_index(message_id);
CREATE INDEX IF NOT EXISTS idx_vector_hash ON vector_index(text_hash);

-- BM25 倒排索引
CREATE TABLE IF NOT EXISTS bm25_inverted (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    tf INTEGER NOT NULL DEFAULT 1,          -- 词频
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_bm25_term ON bm25_inverted(term);
CREATE INDEX IF NOT EXISTS idx_bm25_message ON bm25_inverted(message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bm25_term_message ON bm25_inverted(term, message_id);

-- BM25 文档统计
CREATE TABLE IF NOT EXISTS bm25_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id = 1),
    total_docs INTEGER NOT NULL DEFAULT 0,
    avg_dl REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Schema 版本
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 记忆元数据（维护器写入：只写 stale_score；后四列是 2026-10-31 前 LLM 验证器的遗留）
-- ⚠ 2026-10-31 起 LLM 验证器已删除（用户 D7，见 `30_架构与设计/28` §2.6）：
--    last_validated_at / validation_status / validation_note / validate_attempts
--    四列**冻结为历史字段**，不再有生产者；保留是为了不丢已有判决与免去 SQLite 删列。
--    新代码不要再读它们判断现状，形态问题看中枢 L7 的 G-* 判据 + facts.archived_at。
CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT PRIMARY KEY REFERENCES facts(key) ON DELETE CASCADE,
    stale_score REAL DEFAULT 0,           -- 距上次更新的天数（唯一仍在写的列）
    last_validated_at TEXT,               -- [冻结] 上次 LLM 验证时间 (ISO)
    validation_status TEXT DEFAULT 'pending',  -- [冻结] pending/keep/update/archive
    validation_note TEXT DEFAULT '',       -- [冻结] LLM 给出的原因
    validate_attempts INTEGER DEFAULT 0    -- [冻结] 本条累计被验证的次数
);
CREATE INDEX IF NOT EXISTS idx_memory_meta_status ON memory_meta(validation_status);

-- Evidence Ledger (v3) — 证据组织层
-- 记录每次 memory_search 返回的证据集，用于事后审计
-- organize_evidence() 把多源召回（messages + facts + summaries）组织成结构化证据
CREATE TABLE IF NOT EXISTS evidence_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,                       -- 触发查询
    search_trace_id INTEGER,                   -- 关联 search_traces.id
    source_type TEXT NOT NULL,                 -- 'message' | 'fact' | 'summary'
    source_id INTEGER NOT NULL,                -- 对应表的主键 id
    content_preview TEXT NOT NULL,             -- 内容前 500 字符
    timestamp REAL,                            -- 消息时间戳 / fact.updated_at epoch
    retrieval_score REAL NOT NULL,             -- 检索打分 0-1
    source_tag TEXT,                           -- 来源标签 (vl_summary/bill_summary 等)
    confidence REAL DEFAULT 1.0,
    corroboration_count INTEGER DEFAULT 0,    -- 佐证数（其他证据支持此条）
    conflicting_ids TEXT,                     -- JSON 数组：冲突证据 id 列表
    matched_by TEXT,                           -- 'bm25' | 'vector' | 'both' | 'fact_kv' | 'summary'
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_query ON evidence_ledger(query);
CREATE INDEX IF NOT EXISTS idx_evidence_ts ON evidence_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_evidence_trace_id ON evidence_ledger(search_trace_id);

-- Search Traces (v3) — 检索可观测性
-- 记录每次 search 的完整过程（query/params/子检索结果/scores/latency/error）
-- 便于事后诊断：为什么某条记忆没被召回？为什么某条无关记忆被排在前？
CREATE TABLE IF NOT EXISTS search_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                          -- ISO 时间
    query TEXT NOT NULL,
    params TEXT NOT NULL,                      -- JSON: top_k/since/until/source
    mode TEXT NOT NULL,                        -- 'hybrid' | 'bm25_only' | 'vector_only' | 'error'
    latency_ms INTEGER NOT NULL,
    bm25_count INTEGER DEFAULT 0,
    vector_count INTEGER DEFAULT 0,
    final_count INTEGER NOT NULL,
    candidates TEXT NOT NULL,                  -- JSON: 前 N 条候选（含子检索分数）
    final_results TEXT NOT NULL,              -- JSON: 最终返回的结果
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON search_traces(ts);

-- 上下文投影记录 (v4) — "召回之后真正进了上下文的是哪些"
-- search_traces 只到"检索返回了什么"为止；记忆注入是检索的下游，它会重排、会截断、
-- 会跳过失效项，而这些动作此前完全不落盘。于是"记忆抽风"查不出是哪一环：
-- 检索？排序？失效？还是投影？这张表只做事后留痕——不参与 ranking，不产生 authority。
CREATE TABLE IF NOT EXISTS context_projections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    trace_id INTEGER,                       -- 关联 search_traces.id（静态声明的记忆可为空）
    task_type TEXT,                         -- 消费场景（如 recurring.accounting）
    surface TEXT NOT NULL,                  -- 注入面，如 agent_guide.must_read
    selected_keys TEXT NOT NULL,            -- JSON: 最终上屏的 key，按上屏顺序
    dropped_keys TEXT,                      -- JSON: 被上限砍掉的 key
    rank_before TEXT,                       -- JSON: 上游召回给出的顺序
    rank_after TEXT,                        -- JSON: 实际上屏顺序（与 before 不同即显式记录偏离）
    validation_flags TEXT,                  -- JSON: key → 验证状态/时效警告
    injected_text_digest TEXT NOT NULL,     -- 实际注入文本的 sha256
    injected_chars INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_projection_ts ON context_projections(ts);
CREATE INDEX IF NOT EXISTS idx_projection_trace ON context_projections(trace_id);

-- 记忆指标日表 (v5) — 明细留痕被 retention 删掉之前的聚合快照
-- 背景：search_traces / evidence_ledger / context_projections 各按 30 天硬删，
-- 删之前没有任何聚合，于是"召回质量随时间怎么变"永远算不出来（累计写过 1094 条
-- trace、10165 行 evidence，如今两张表都是 0 行）。这张表存按日聚合的标量指标，
-- 是上面三张表的**替代物**，因此不参与维护器的 retention 清理。
-- 与 context_projections 同一纪律：只做事后度量，不参与 ranking，不产生 authority。
-- 窄表（date, metric, value）：新增指标不必再动 schema。
CREATE TABLE IF NOT EXISTS memory_metrics_daily (
    date TEXT NOT NULL,                    -- 'YYYY-MM-DD'（本地日）
    metric TEXT NOT NULL,                  -- 指标名，如 'facts_surfaced'
    value REAL,                            -- 标量值；无法量化时留 NULL，细节进 extra
    extra TEXT,                            -- JSON：样本 key 清单、窗口说明等
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (date, metric)
);
CREATE INDEX IF NOT EXISTS idx_metrics_metric ON memory_metrics_daily(metric, date);

-- 事实版本历史 (v6) — P2 版本模型
-- 背景：同一 key 被反复覆盖时，旧值此前靠 messages(source='manual') 的全文副本"顺带"留痕，
-- 但那份副本同时被 BM25/向量索引吃掉（实测 183 份旧副本、约 6.9 万行倒排词条仍可被召回），
-- 于是"当前真相"与"曾经为真"混在同一条召回通道里，没有显式语义。
-- 本表把历史显式化：facts 只存 key 的**当前值**（key UNIQUE 不变，所有按 key 读 facts 的代码零改动），
-- 被取代的旧值进本表，支持 as_of 查"当时为真的是什么"。
-- ⚠️ key 刻意**不加** REFERENCES facts(key)：facts 行会被 memory_delete 删除，
--    若此处级联，历史会跟着消失——与"失效 ≠ 消失"（ADR-0040 同源纪律）冲突。删 key 时历史保留。
CREATE TABLE IF NOT EXISTS fact_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,                      -- 逻辑关联 facts.key，但不设外键（见上）
    value TEXT NOT NULL,                    -- 被取代时的完整 value 原文
    valid_at TEXT NOT NULL,                 -- 该值开始为真；与 facts.occurred_at / messages.created_at 同格式（可字符串排序）
    invalid_at TEXT,                        -- 被取代时刻；NULL = 仍为真（仅可能出现在迁移边界）
    source_message_id INTEGER,              -- 溯源：迁移行来自哪条 messages（索引清理按它定位）
    migrated INTEGER NOT NULL DEFAULT 0,    -- 1 = v6 迁移回填，0 = 运行期写入
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_fact_rev_key ON fact_revisions(key, valid_at);

-- 初始化 BM25 统计行
INSERT OR IGNORE INTO bm25_stats (id, total_docs, avg_dl) VALUES (1, 0, 0.0);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """初始化数据库，创建表和索引。

    新库：执行 SCHEMA_SQL 后直接写入当前版本号。
    旧库：先 migrate_db() 升级 schema（添加新列/新表/新索引），再执行 SCHEMA_SQL 幂等补建。
    顺序很重要：旧库的 facts 表可能缺 v3 新列，SCHEMA_SQL 中的 CREATE INDEX 会因列不存在而失败，
    所以必须先 migrate_db 添加列，再 executescript(SCHEMA_SQL)。
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    # 检查 schema_info 是否存在（区分新库 vs 旧库）
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_info'"
    )
    schema_info_exists = cur.fetchone() is not None

    if schema_info_exists:
        # 旧库：先迁移（添加新列、创建新表、创建新索引）
        # 注意：此时旧库的 facts 表可能缺 v3 新列，不能直接 executescript(SCHEMA_SQL)
        # 因为 SCHEMA_SQL 中的 CREATE INDEX 会因列不存在而失败
        version_row = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'"
        ).fetchone()
        existing_version = int(version_row[0]) if version_row else 0
        if existing_version < SCHEMA_VERSION:
            migrate_db(conn)

    # 执行 SCHEMA_SQL（幂等：新库创建所有表，旧库补建缺失部分）
    # 此时旧库的新列已存在，CREATE INDEX 不会因列缺失而失败
    conn.executescript(SCHEMA_SQL)
    # v8 新表与 V8_TABLES_SQL 共用同一份 DDL（避免两处漂移）
    conn.executescript(V8_TABLES_SQL)
    # v9 表（butler_*）同理，新库旧库都建（DDL 幂等）
    conn.executescript(V9_TABLES_SQL)
    # v10：每轮报告表 + 老表加列（新库旧库都执行，幂等）
    conn.executescript(V10_TABLES_SQL)
    # v11：派单提示词版本表（新库旧库都执行，幂等）
    conn.executescript(V11_TABLES_SQL)
    _ensure_columns(conn, "butler_runs", V10_RUN_COLUMNS)
    _ensure_columns(conn, "butler_deliveries", V10_DELIVERY_COLUMNS)
    _ensure_columns(conn, "butler_reports", V10_REPORT_COLUMNS)
    # v12：提示词只留一份，按来源标记（新库也要补，否则 SELECT 直接报列不存在）
    _ensure_columns(conn, "butler_runs", V12_RUN_COLUMNS)
    # v13：提示词版本重新启用，记当前生效版本号
    _ensure_columns(conn, "butler_runs", V13_RUN_COLUMNS)
    # v14：执行域状态归位（run 自带进度/下一步/现状/标签）+ 交接挂 run
    _ensure_columns(conn, "butler_runs", V14_RUN_COLUMNS)
    _ensure_columns(conn, "butler_handoffs", V14_HANDOFF_COLUMNS)
    # v15：面板归档（事务 / 会话都记时间戳）+ 交接提示词手改留存
    _ensure_columns(conn, "tasks", V15_TASK_COLUMNS)
    _ensure_columns(conn, "butler_runs", V15_RUN_COLUMNS)
    if _has_table(conn, "butler_handoffs"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_butler_handoffs_run "
            "ON butler_handoffs(run_id)"
        )

    # 新库写入版本号（旧库已在 migrate_db 中更新）
    if not schema_info_exists:
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        conn.commit()
    return conn


def migrate_db(conn: sqlite3.Connection) -> None:
    """版本迁移入口"""
    cur = conn.execute("SELECT value FROM schema_info WHERE key = 'version'")
    row = cur.fetchone()
    version = int(row[0]) if row else 0

    if version < 2:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_meta (
                key TEXT PRIMARY KEY REFERENCES facts(key) ON DELETE CASCADE,
                stale_score REAL DEFAULT 0,
                last_validated_at TEXT,
                validation_status TEXT DEFAULT 'pending',
                validation_note TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_memory_meta_status ON memory_meta(validation_status);
        """)
        logger.info("迁移到 schema v2: 添加 memory_meta 表（记忆维护器）")

    if version < 3:
        # 幂等添加 facts 新列（ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS）
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
        new_cols = [
            ("fact_type", "TEXT"),
            ("occurred_at", "TEXT"),
            ("mentioned_at", "TEXT"),
            ("consumption_contexts", "TEXT"),
            ("trigger_keywords", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {col_name} {col_type}")
                logger.info(f"迁移到 schema v3: 添加 facts.{col_name} 列")

        # 新增 Evidence Ledger 和 Search Traces 表
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS evidence_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                search_trace_id INTEGER,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                content_preview TEXT NOT NULL,
                timestamp REAL,
                retrieval_score REAL NOT NULL,
                source_tag TEXT,
                confidence REAL DEFAULT 1.0,
                corroboration_count INTEGER DEFAULT 0,
                conflicting_ids TEXT,
                matched_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_query ON evidence_ledger(query);
            CREATE INDEX IF NOT EXISTS idx_evidence_ts ON evidence_ledger(timestamp);
            CREATE INDEX IF NOT EXISTS idx_evidence_trace_id ON evidence_ledger(search_trace_id);

            CREATE TABLE IF NOT EXISTS search_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                query TEXT NOT NULL,
                params TEXT NOT NULL,
                mode TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                bm25_count INTEGER DEFAULT 0,
                vector_count INTEGER DEFAULT 0,
                final_count INTEGER NOT NULL,
                candidates TEXT NOT NULL,
                final_results TEXT NOT NULL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_traces_ts ON search_traces(ts);

            CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(fact_type);
            CREATE INDEX IF NOT EXISTS idx_facts_mentioned ON facts(mentioned_at);
        """)
        logger.info("迁移到 schema v3: facts 结构化字段 + evidence_ledger + search_traces")

    if version < 4:
        # v4: context_projections —— 记忆进入上下文那一段的留痕（T15）
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS context_projections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                trace_id INTEGER,
                task_type TEXT,
                surface TEXT NOT NULL,
                selected_keys TEXT NOT NULL,
                dropped_keys TEXT,
                rank_before TEXT,
                rank_after TEXT,
                validation_flags TEXT,
                injected_text_digest TEXT NOT NULL,
                injected_chars INTEGER NOT NULL DEFAULT 0,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_projection_ts ON context_projections(ts);
            CREATE INDEX IF NOT EXISTS idx_projection_trace ON context_projections(trace_id);
        """)
        logger.info("迁移到 schema v4: 添加 context_projections 表（上下文投影留痕）")

    if version < 5:
        # v5: memory_metrics_daily（指标日聚合，先于 retention 删除落盘）
        #     + memory_meta.validate_attempts（验证器超窗延判计数）
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_metrics_daily (
                date TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL,
                extra TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (date, metric)
            );
            CREATE INDEX IF NOT EXISTS idx_metrics_metric ON memory_metrics_daily(metric, date);
        """)
        meta_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_meta)").fetchall()}
        if "validate_attempts" not in meta_cols:
            conn.execute("ALTER TABLE memory_meta ADD COLUMN validate_attempts INTEGER DEFAULT 0")
        logger.info("迁移到 schema v5: memory_metrics_daily 表 + memory_meta.validate_attempts 列")

    if version < 6:
        # v6: fact_revisions（P2 版本模型 —— facts 存当前值，本表存"曾经为真"）
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fact_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                valid_at TEXT NOT NULL,
                invalid_at TEXT,
                source_message_id INTEGER,
                migrated INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_fact_rev_key ON fact_revisions(key, valid_at);
        """)
        backfilled = backfill_fact_revisions(conn)
        logger.info("迁移到 schema v6: fact_revisions 表（回填 %d 行历史）", backfilled)

    if version < 7:
        # v7: facts.archived_at —— 删除语义从物理删改为"失效"（C1，2026-10-18）
        #   为什么加在 facts 而不是另建 tombstone 表：
        #   key 是 UNIQUE，本行就是"这个 key 曾经是什么"的唯一锚点；
        #   另建表要处理双写与恢复时的一致性，成本高于收益。
        #   NULL = 活跃；非 NULL = 已失效（时间戳，定长文本微秒精度，与 fact_revisions 一致）。
        fact_cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "archived_at" not in fact_cols:
            conn.execute("ALTER TABLE facts ADD COLUMN archived_at TEXT")
            logger.info("迁移到 schema v7: facts.archived_at 列（删除改失效）")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_archived ON facts(archived_at)"
        )

    if version < 8:
        # v8（DL3，2026-10-27）：记忆三轴 + 证据列；原话表与候选账。
        # 只加列不迁表：老数据 scope/kind 留 NULL（没人判过，不假装知道）。
        fact_cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
        added: list[str] = []
        for col, decl in V8_FACT_COLUMNS:
            if col not in fact_cols:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {col} {decl}")
                added.append(col)
        conn.executescript(V8_TABLES_SQL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_scope_kind ON facts(scope, kind)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_must_read ON facts(must_read) "
            "WHERE must_read = 1"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_dedup ON facts(dedup_hash) "
            "WHERE dedup_hash IS NOT NULL"
        )
        logger.info("迁移到 schema v8: facts 加 %d 列（%s）+ source_quotes + memory_candidates",
                    len(added), ", ".join(added))

    if version < 9:
        # v9（DL4，2026-10-27）：会话码 / run / handoff / 投递 / 轻量事件。
        conn.executescript(V9_TABLES_SQL)
        logger.info("迁移到 schema v9: butler_runs / butler_handoffs / "
                    "butler_deliveries / butler_events")

    if version < 10:
        # v10（P5，2026-10-27）：长会话 + 每轮报告；召回反馈落投递行。
        conn.executescript(V10_TABLES_SQL)
        added_runs = _ensure_columns(conn, "butler_runs", V10_RUN_COLUMNS)
        added_deliv = _ensure_columns(conn, "butler_deliveries", V10_DELIVERY_COLUMNS)
        _ensure_columns(conn, "butler_reports", V10_REPORT_COLUMNS)
        logger.info("迁移到 schema v10: butler_reports + butler_runs 加 %d 列 + "
                    "butler_deliveries 加 %d 列",
                    len(added_runs), len(added_deliv))

    if version < 11:
        # v11（2026-10-28）：派单提示词版本化（每 run 最多 10 版，历史只读）
        conn.executescript(V11_TABLES_SQL)
        logger.info("迁移到 schema v11: 新增 butler_prompt_versions")

    if version < 12:
        # v12（2026-10-28 用户第三批裁定）：提示词只留当前一份，按来源标记。
        # 老 run 的 prompt_text 是建 run 时生成的，标 initial；空值保持 NULL。
        added_prompt = _ensure_columns(conn, "butler_runs", V12_RUN_COLUMNS)
        conn.execute(
            "UPDATE butler_runs SET prompt_source = 'initial' "
            "WHERE prompt_source IS NULL AND COALESCE(prompt_text, '') <> ''"
        )
        logger.info("迁移到 schema v12: butler_runs 加 %d 列（%s）+ 回填 prompt_source",
                    len(added_prompt), ", ".join(added_prompt))

    if version < 13:
        # v13（2026-10-29 用户重新裁定）：派单提示词恢复「有历史、可回滚、可看可改可删」。
        # v12 期间是覆盖式写入，版本表里的旧行可能已经和 prompt_text 不一致，
        # 所以以 prompt_text 为准：没有版本行的补 v1，最新行对不上的再追加一条。
        added_ver = _ensure_columns(conn, "butler_runs", V13_RUN_COLUMNS)
        runs = conn.execute(
            "SELECT run_id, prompt_text, prompt_source FROM butler_runs "
            "WHERE COALESCE(prompt_text, '') <> ''"
        ).fetchall()
        for run_id, prompt_text, source in runs:
            latest = conn.execute(
                "SELECT version, prompt_text FROM butler_prompt_versions "
                "WHERE run_id = ? ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if latest is None:
                conn.execute(
                    "INSERT INTO butler_prompt_versions "
                    "(run_id, version, prompt_text, chars, source, note) "
                    "VALUES (?, 1, ?, ?, ?, ?)",
                    (run_id, prompt_text, len(prompt_text),
                     source or "initial", "v13 迁移：按现存 prompt_text 回填"),
                )
                current = 1
            elif (latest[1] or "") != prompt_text:
                current = int(latest[0]) + 1
                conn.execute(
                    "INSERT INTO butler_prompt_versions "
                    "(run_id, version, prompt_text, chars, source, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, current, prompt_text, len(prompt_text),
                     source or "manual", "v13 迁移：v12 覆盖式写入的正文"),
                )
            else:
                current = int(latest[0])
            conn.execute(
                "UPDATE butler_runs SET prompt_version = ? WHERE run_id = ?",
                (current, run_id),
            )
        logger.info("迁移到 schema v13: butler_runs 加 %d 列（%s）+ 回填 %d 个 run 的版本",
                    len(added_ver), ", ".join(added_ver), len(runs))

    if version < 14:
        # v14（2026-10-29 用户拍板）：状态字段从「目标域」搬到「执行域」。
        # 老 run 的新列全是空 ⇒ 面板上历史 run 会显示成「未报」。
        # 回填口径：进度 / 下一步从所属事务行拷一份（那时它们本来就写在那儿），
        # 现状优先用 run 自己最近一次报告的 summary（它天然是「这一路」的）。
        added = _ensure_columns(conn, "butler_runs", V14_RUN_COLUMNS)
        added_hof = _ensure_columns(conn, "butler_handoffs", V14_HANDOFF_COLUMNS)
        if _has_table(conn, "butler_handoffs"):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_butler_handoffs_run "
                "ON butler_handoffs(run_id)"
            )
        has_tasks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        backfilled = 0
        if has_tasks:
            rows = conn.execute(
                "SELECT run_id, task_scope, task_id, summary FROM butler_runs "
                "WHERE progress IS NULL OR state_note IS NULL"
            ).fetchall()
            for run_id, scope, task_id, summary in rows:
                # progress / next_steps 只有能从事务行取到才有得补；
                # state_note 是这一路自己的 summary，跟事务在不在没关系——
                # 早先的冒烟 run 没有 task_id，不该因此丢掉自己的现状。
                task = None
                if scope and task_id:
                    task = conn.execute(
                        "SELECT progress, next_steps FROM tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                progress, next_steps = (task[0], task[1]) if task else (None, None)
                conn.execute(
                    "UPDATE butler_runs SET progress = COALESCE(progress, NULLIF(?, 0)), "
                    "next_steps_json = COALESCE(next_steps_json, ?), "
                    "state_note = COALESCE(state_note, ?) WHERE run_id = ?",
                    (progress, next_steps, summary, run_id),
                )
                backfilled += 1
        logger.info(
            "迁移到 schema v14: butler_runs 加 %d 列（%s）+ butler_handoffs 加 %d 列（%s）"
            "+ 回填 %d 个 run 的状态",
            len(added), ", ".join(added) or "-",
            len(added_hof), ", ".join(added_hof) or "-", backfilled,
        )

    if version < 15:
        # v15（2026-10-30 用户拍板）：面板归档 / 交接提示词手改。
        # 老数据全空 = 未归档、未手改，行为与升级前一致，不需要回填。
        added_task = _ensure_columns(conn, "tasks", V15_TASK_COLUMNS)
        added_run = _ensure_columns(conn, "butler_runs", V15_RUN_COLUMNS)
        logger.info("迁移到 schema v15: tasks 加 %d 列（%s）+ butler_runs 加 %d 列（%s）",
                    len(added_task), ", ".join(added_task) or "-",
                    len(added_run), ", ".join(added_run) or "-")

    if version < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        conn.commit()


def backfill_fact_revisions(conn: sqlite3.Connection) -> int:
    """把每个 key 除最新一条外的 manual 全文副本灌入 fact_revisions（v6 迁移）。

    为什么需要回填：v6 之前"某个 key 以前是什么值"这件事只存在于
    ``messages(source='manual')`` 的全文副本里，靠 GET /memory/timeline 顺带显示，
    没有显式语义。本函数把它固化成带有效区间（valid_at / invalid_at）的历史行。

    两条实现判据（都不是随手选的）：
    - **按 ``messages.id`` 升序定序，不按 ``created_at``**：``created_at`` 只有秒级精度，
      同一秒内被覆盖两次时无法定序，而 id 是 AUTOINCREMENT，写入顺序严格单调。
    - **最新一条不进历史**：它对应的 ``facts`` 行就是"当前真相"，重复存一份会让
      "历史"与"当前"同内容两份，as_of 查询还会在边界上歧义。

    幂等：只要表里已有 ``migrated=1`` 的行就直接返回 0（重跑安全）。
    返回：本次插入的 history 行数。
    """
    already = conn.execute(
        "SELECT 1 FROM fact_revisions WHERE migrated = 1 LIMIT 1"
    ).fetchone()
    if already:
        logger.info("fact_revisions 已有迁移数据，跳过回填")
        return 0

    keys = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT key FROM messages WHERE source = 'manual' AND key IS NOT NULL"
        ).fetchall()
    ]

    inserted = 0
    for key in keys:
        rows = conn.execute(
            "SELECT id, content, created_at FROM messages "
            "WHERE source = 'manual' AND key = ? ORDER BY id ASC",
            (key,),
        ).fetchall()
        if len(rows) < 2:
            continue
        # 除最后一条（= 当前值）外，全部转为历史；invalid_at 取下一条的写入时间
        for i in range(len(rows) - 1):
            msg_id, content, created_at = rows[i]
            conn.execute(
                "INSERT INTO fact_revisions "
                "(key, value, valid_at, invalid_at, source_message_id, migrated) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (key, content, created_at, rows[i + 1][2], msg_id),
            )
            inserted += 1
    conn.commit()
    return inserted
