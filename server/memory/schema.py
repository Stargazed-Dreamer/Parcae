"""SQLite 数据库 schema 和迁移"""

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 6

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
    trigger_keywords TEXT                   -- JSON 数组字符串，如 ["退款", "curl"]
);

CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(fact_type);
CREATE INDEX IF NOT EXISTS idx_facts_mentioned ON facts(mentioned_at);

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

-- 记忆元数据（维护器写入：老化标记 + LLM 验证结果）
CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT PRIMARY KEY REFERENCES facts(key) ON DELETE CASCADE,
    stale_score REAL DEFAULT 0,           -- 距上次更新的天数
    last_validated_at TEXT,               -- 上次 LLM 验证时间 (ISO)
    validation_status TEXT DEFAULT 'pending',  -- pending/keep/update/archive
    validation_note TEXT DEFAULT '',       -- LLM 给出的原因
    validate_attempts INTEGER DEFAULT 0    -- v5：本条累计被验证的次数（超窗延判用）
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
