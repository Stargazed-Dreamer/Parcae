"""记忆系统管理器 — 统一入口

初始化并协调三层记忆系统，提供全局单例。
"""

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from server.memory.compress import CompressionPipeline
from server.memory.config import MemoryConfig, get_memory_config
from server.memory.embeddings import EmbeddingEngine
from server.memory.evidence import EvidenceLedger
from server.memory.maintainer import MemoryMaintainer
from server.memory.recent import RecentMemory
from server.memory.recorder import InteractionRecorder
from server.memory.search_tracer import SearchTracer
from server.memory.semantic import SemanticSearch
from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# 记忆索引标题的取值顺序：信封里的标题字段 → 正文首行 → 顶层字段名清单 → 原始截断
_TITLE_FIELDS = ("name", "description", "title", "summary")
_BODY_FIELDS = ("content", "text", "lesson", "note", "conclusion")
# 只用于"这条记忆长得像信封"的判断：这些键不构成可读标题，也不该出现在字段名清单里
_ENVELOPE_KEYS = frozenset({
    "source", "updated_at", "created_at", "access_count", "confidence", "mentioned_at",
    "fact_type", "consumption_contexts", "trigger_keywords", "source_task", "type", "date",
    "key", "value",
})
_MAX_SUMMARY_CHARS = 120


def _structured_shape(parsed: dict) -> str:
    """顶层只有嵌套 dict/list（没有任何字符串标题/正文）时，用字段名清单代替 JSON 前缀。

    真库实测这类有 36 条（`preferences`、`stock_advisor` 这种业务键），旧兜底是
    `value[:120]`，上屏就是 `{"stock_review_output": {"no_html": true, "format":`。
    """
    names = [k for k in parsed if isinstance(k, str) and k not in _ENVELOPE_KEYS]
    if not names and isinstance(parsed.get("value"), dict):
        # 双层信封 {"key": ..., "value": {...}}：内层才是内容
        names = [k for k in parsed["value"] if isinstance(k, str)]
    if not names:
        return ""
    head = "、".join(names[:3])
    more = f"…共 {len(names)} 项" if len(names) > 3 else ""
    return f"（结构化：{head}{more}）"[:_MAX_SUMMARY_CHARS]


def index_summary_from_value(value: str) -> str:
    """把 facts.value 压成一行可读标题，供 agent_guide 的【必读记忆】清单上屏。

    直接 `value[:80]` 会把 `{"source": "manual", "content": "## 现象..."}` 这类
    元数据信封整坨 JSON 打到屏幕上，同清单里别的条目却是人话标题，读者无法对齐。
    取值顺序：标题字段 → 正文首行 → 顶层字段名清单 → 非 JSON 才原样截断。
    """
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return value[:_MAX_SUMMARY_CHARS]
    if not isinstance(parsed, dict):
        return value[:_MAX_SUMMARY_CHARS]
    for field in _TITLE_FIELDS:
        got = parsed.get(field)
        if isinstance(got, str) and got.strip():
            return got.strip()[:_MAX_SUMMARY_CHARS]
    for field in _BODY_FIELDS:
        got = parsed.get(field)
        if not isinstance(got, str):
            continue
        for line in got.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:_MAX_SUMMARY_CHARS]
    return _structured_shape(parsed)


# ---------------------------------------------------------------------------
# fact 检索用词项切分（2026-10-10 工具体检 B1：把整串 LIKE 改成分词 OR 召回）
# ---------------------------------------------------------------------------

# 与 messages 侧 BM25 用同一套过滤口径（bm25.py:85）：ASCII 单字符丢弃，中文单字保留。
# 刻意不去发明停用词表 —— 两侧口径必须一致，否则同一句 query 在 facts 和 messages
# 上召回的语义对不齐，排序就没法比较。
_MAX_FACT_TERMS = 12  # 词项数上限，兜住超长 query 生成的 SQL 长度


def _escape_like(s: str) -> str:
    """转义 LIKE 通配符。用户 query 里带 `%` / `_` 时，不转义会退化成全表匹配。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fact_query_terms(query: str) -> list[str]:
    """把 query 切成用于 fact 检索的词项（已去重、已保序、已限长）

    分词不到任何词项时（query 全是单字符 ASCII 或纯空白）回退成整串一项，
    保持老行为 —— 至少 `LIKE '%query%'` 还能查到东西，而不是静默返回空。
    """
    from server.memory.bm25 import _tokenize

    raw = (query or "").strip()
    if not raw:
        return []
    terms = [
        t.strip().lower()
        for t in _tokenize(raw)
        if t.strip() and (len(t.strip()) > 1 or "\u4e00" <= t.strip() <= "\u9fff")
    ]
    if not terms:
        return [raw.lower()]
    seen: set[str] = set()
    deduped = [t for t in terms if not (t in seen or seen.add(t))]
    return deduped[:_MAX_FACT_TERMS]


# 全局单例
_instance: Optional["MemoryManager"] = None
_instance_lock = threading.Lock()


class MemoryManager:
    """记忆系统管理器"""

    def __init__(self, config: MemoryConfig | None = None):
        """初始化内存管理器的实例。

        参数:
            config (Optional[MemoryConfig], optional): 配置对象，用于设置内存管理器的各项参数。默认为None，将使用默认配置。

        返回:
            None
        """
        self.config = config or get_memory_config()  # 如果config为None，则获取默认配置
        # 相对路径相对于项目根目录解析（不依赖 cwd；manager.py 在 server/memory/，3 级 parent 到项目根）
        db_path = Path(self.config.db_path)
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent.parent.parent / db_path
        self.store = MemoryStore(str(db_path))  # 初始化内存存储对象，使用配置中的数据库路径
        self.recent = RecentMemory(  # 初始化最近记忆模块
            window_size=self.config.recent_window_size,  # 设置最近记忆的窗口大小
            save_dir=self.config.recent_dir,  # 设置保存目录
            save_interval=self.config.recent_save_interval,  # 设置保存间隔
        )
        self.embedding = EmbeddingEngine(  # 初始化嵌入引擎
            model_name=self.config.embedding_model,  # 设置嵌入模型名称
            cache_dir=self.config.embedding_cache_dir,  # 设置缓存目录
            hf_mirror=self.config.hf_mirror,  # 设置Hugging Face镜像地址
        )
        # v3: SearchTracer — 检索可观测性
        self.search_tracer = SearchTracer(
            store=self.store,
            retention_days=self.config.search_tracer_retention_days,
            sample_rate=self.config.search_tracer_sample_rate,
            candidates_to_store=self.config.search_tracer_candidates_to_store,
            enabled=self.config.enable_search_tracer,
        )
        self.semantic = SemanticSearch(  # 初始化语义搜索模块
            store=self.store,  # 传入存储对象
            embedding_engine=self.embedding,  # 传入嵌入引擎
            alpha=self.config.hybrid_alpha,  # 设置混合搜索的alpha参数
            vector_top_k=self.config.vector_top_k,  # 设置向量搜索的top_k值
            bm25_top_k=self.config.bm25_top_k,  # 设置BM25搜索的top_k值
            tracer=self.search_tracer,  # v3: 注入 SearchTracer
            # T18 HONEST_EMPTY：纯向量命中的绝对相似度门槛
            min_vector_similarity=self.config.hybrid_min_vector_similarity,
        )
        # v3: EvidenceLedger — 证据组织层
        self.evidence_ledger = EvidenceLedger(store=self.store, config=self.config)
        self.compress = CompressionPipeline(  # 初始化压缩管道
            store=self.store,  # 传入存储对象
            threshold=self.config.compress_threshold,  # 设置压缩阈值
            batch_size=self.config.compress_batch_size,  # 设置批处理大小
        )
        self.maintainer = MemoryMaintainer(  # 初始化记忆维护器
            store=self.store,  # 传入存储对象
            config=self.config,  # 传入配置对象
            # v3/5-14: SearchTracer 和 EvidenceLedger 正常传参注入（此前 Any 动态挂属性）
            search_tracer=self.search_tracer,
            evidence_ledger=self.evidence_ledger,
        )
        self.recorder = InteractionRecorder(  # 初始化交互记录器
            store=self.store,  # 传入存储对象
            recent=self.recent,  # 传入最近记忆模块
            semantic=self.semantic,  # 传入语义搜索模块
            compress=self.compress,  # 传入压缩管道
            config=self.config,  # 传入配置对象
            maintainer=self.maintainer,  # 传入维护器
        )
        self._initialized = False  # 初始化完成标志，设置为False表示未初始化

    def initialize(self) -> None:
        """初始化所有组件"""
        if self._initialized:
            return

        logger.info("正在初始化三层记忆系统...")

        # 1. 初始化 SQLite 存储
        self.store.initialize()
        logger.info("SQLite 存储层就绪")

        # 2. 初始化近期记忆
        self.recent.initialize()
        logger.info(f"近期记忆层就绪 (窗口={self.config.recent_window_size})")

        # 3. 初始化嵌入引擎（可能失败，降级为 BM25-only）
        if self.config.enable_semantic:
            try:
                success = self.embedding.initialize()
                if success:
                    logger.info(f"嵌入引擎就绪: {self.config.embedding_model}")
                else:
                    logger.info("嵌入引擎不可用，使用 BM25-only 模式")
            except Exception as e:
                logger.warning(f"嵌入引擎初始化失败: {e}，使用 BM25-only 模式")
        else:
            logger.info("语义检索已禁用")

        self._initialized = True
        logger.info("三层记忆系统初始化完成")

    # ─── 向后兼容 KV 接口 ──────────────────────────────────

    def get(self, key: str) -> dict | None:
        """读取记忆（兼容旧 KV 接口）

        查找顺序：facts 表 → messages 表（key 匹配）

        v3 行为：
        - facts 表的 value 是 JSON 字符串，解析后合并到返回 dict
          （客户端可直接访问 data["name"]，同时附带 source/updated_at 元数据）
        - 同时返回 v3 结构化字段（fact_type/occurred_at/mentioned_at/
          consumption_contexts/trigger_keywords），如果有值
        """
        import json

        # 先查 facts 表
        fact = self.store.get_fact(key)
        if fact:
            result: dict = {
                "source": fact["source"],
                "updated_at": fact["updated_at"],
            }
            # v3: 解析 value JSON，如果是 dict 则合并到 result（保持向后兼容）
            # 这样客户端可以直接访问 data["name"]，同时也能访问元数据 data["source"]
            value_str = fact.get("value", "")
            try:
                value_parsed = json.loads(value_str)
                if isinstance(value_parsed, dict):
                    result.update(value_parsed)
                else:
                    result["value"] = value_str
            except (json.JSONDecodeError, TypeError):
                result["value"] = value_str
            # v3: 附带结构化字段（如果有值）
            for field in (
                "fact_type", "occurred_at", "mentioned_at",
                "consumption_contexts", "trigger_keywords",
                "access_count", "confidence", "created_at",
            ):
                val = fact.get(field)
                if val is not None:
                    result[field] = val
            return result

        # 再查 messages 表
        # v6：跳过 source='event' 的时间线事件行 —— 它只是"某时改过"的痕迹，正文是
        # {event,key,chars,digest} 而**不是**记忆值；当记忆返回会让详情页显示一个摘要哈希。
        # （只有 facts 缺行时才会走到这里；实测 kv-only = 0，正常路径不触发。）
        for msg in self.store.query_messages(key=key, limit=5):
            if msg.get("source") == "event":
                continue
            try:
                import json
                data = json.loads(msg["content"])
                if isinstance(data, dict):
                    data["_id"] = msg["id"]
                    # source 统一字段名（原 _source 与 facts 分支 source 不一致导致客户端详情页该行缺失）
                    data["source"] = msg["source"]
                    return data
            except (json.JSONDecodeError, TypeError):
                return {"value": msg["content"], "_id": msg["id"],
                        "source": msg["source"]}

        return None

    def set(self, key: str, data: dict, structured: dict | None = None) -> dict:
        """写入记忆（兼容旧 KV 接口）

        同时写入 facts 表和 messages 表。

        v6（P2 版本模型）起有两处行为变化：

        1. **历史显式化**：``extract_facts_from_kv`` → ``upsert_fact`` 在覆盖前会把旧值
           落进 ``fact_revisions``（判据是"值真的变了"）。
        2. **messages 里每个 key 只保留一条"当前快照"行**，不再每写一次追加一行全文副本。
           旧行为实测积出 183 行被取代的全文副本、约 6.9 万行 BM25 词条仍可被召回 ——
           "当前真相"和"曾经为真"混在同一条召回通道里，没有显式语义。

        ⚠️ 为什么是"原地更新快照"而不是"干脆不存全文"：KV 内容的召回目前由 messages 的
        BM25+向量索引承担，而 facts 那条替代路径 ``_search_facts`` 用的是
        ``LIKE '%query%'`` 子串匹配、**不分词**（"止损阈值"不会被切成"止损"+"阈值"），
        直接砍掉会造成召回退化。改成"每 key 恰好一条、且只索引这一条"之后，
        召回与改动前逐字等价，被取代的版本则天然进不了索引 —— 正是本项要的效果。

        另外追加一条 ``source='event'`` 的事件行供时间线阅读；它**不建索引、不进 recent**
        （只是"某时改过"的痕迹，不是记忆内容；且正文含 key 名，建索引会让搜 key 命中它）。

        Args:
            key: 记忆 key
            data: 记忆数据（dict）
            structured: v3 结构化字段，可选。
                支持的键：fact_type / occurred_at / consumption_contexts / trigger_keywords
                如果提供，会覆盖 data 中相同字段提取的值。
                mentioned_at 不在此处设置（由 get_fact 自动维护）。
        """
        import hashlib
        import json

        content = json.dumps(data, ensure_ascii=False)

        # 提取事实（内部经 upsert_fact：覆盖前把旧值记入 fact_revisions）
        # 如果 data 内部含 fact_type/occurred_at/consumption_contexts/trigger_keywords，
        # extract_facts_from_kv 会自动提取到独立列
        self.store.extract_facts_from_kv(key, data)

        # v3: 显式传入的 structured 字段优先（覆盖 data 内部提取的值）
        if structured:
            prepared: dict = {}
            for k, v in structured.items():
                if v is None:
                    continue
                # list 字段需要序列化为 JSON 字符串（与 facts 表存储格式一致）
                if k in ("consumption_contexts", "trigger_keywords") and isinstance(v, list):
                    prepared[k] = json.dumps(v, ensure_ascii=False)
                else:
                    prepared[k] = v
            if prepared:
                self.store._update_fact_structured_fields(key, prepared)

        # 每个 key 只维护一条当前快照行：有则原地更新 + 重建索引，无则新建 + 建索引
        snapshot_id = self.store.get_kv_snapshot_id(key)
        if snapshot_id is not None:
            msg_id = snapshot_id
            self.store.update_message_content(msg_id, content)
            # 必须走 reindex（先删旧词/旧向量再建）：BM25 的 upsert 是 tf 累加、
            # 向量按 (message_id, text_hash) 去重，只建不删会退化成新旧内容的并集
            self.semantic.reindex_message(msg_id, content)
        else:
            msg_id = self.store.insert_message(
                content=content,
                source="manual",
                role="system",
                key=key,
            )
            self.semantic.index_message(msg_id, content)

        # 时间线事件行（不建索引、不进 recent）
        self.store.insert_message(
            content=json.dumps(
                {
                    "event": "kv_updated",
                    "key": key,
                    "chars": len(content),
                    "digest": hashlib.sha256(content.encode("utf-8")).hexdigest()[:12],
                },
                ensure_ascii=False,
            ),
            source="event",
            role="system",
            key=key,
        )

        # 写入 Recent 层
        self.recent.add(content, source="manual", role="system", key=key)

        return {"key": key, "id": msg_id, "status": "ok"}

    def delete(self, key: str) -> bool:
        """删除记忆"""
        # 删除 facts
        fact_deleted = self.store.delete_fact(key)

        # 删除 messages（通过 key 匹配）；vector_index/bm25_inverted 由 ON DELETE CASCADE 自动清理
        messages = self.store.query_messages(key=key)
        for msg in messages:
            self.store.delete_message(msg["id"])

        return fact_deleted or len(messages) > 0

    def list_keys(self) -> list[dict]:
        """列出所有记忆 key

        v3 行为：facts 表是主源（v3 extract_facts_from_kv 写入整个 dict 到 facts），
        messages 表只列出 facts 表中不存在的 key（避免重复）。
        """
        results = []
        seen_keys: set[str] = set()

        # 从 facts 表（主源）
        facts = self.store.list_facts(limit=200)
        for f in facts:
            # v3: 解析 value 取 summary（name/description/title 优先，content 兜底）
            summary = ""
            try:
                import json as _json
                _parsed = _json.loads(f.get("value", ""))
                if isinstance(_parsed, dict):
                    summary = (
                        _parsed.get("name")
                        or _parsed.get("title")
                        or _parsed.get("description")
                        or _parsed.get("summary")
                        or ""
                    )
                    # reference 类 fact 只有 content（markdown 正文），取前 80 字作摘要
                    if not summary and _parsed.get("content"):
                        summary = str(_parsed["content"])[:80].replace("\n", " ")
            except (ValueError, TypeError):
                pass
            results.append({
                "key": f["key"],
                # source 统一为 'auto' | 'manual'（谁写入的），与 get 端点 facts 分支对齐
                # 客户端用 fact_type 字段是否存在判断是 fact 还是 message（messages 分支不返回 fact_type）
                "source": f.get("source"),
                "fact_type": f.get("fact_type"),        # preference/project/reference/...
                "summary": summary or f["value"][:60],  # 解析后的可读摘要，非裸 JSON
                "value_preview": f["value"][:100],      # 向后兼容
                "updated_at": f["updated_at"],
            })
            seen_keys.add(f["key"])

        # 从 messages 表（补充：只列 facts 中没有的 key）
        # C1（2026-10-18）：facts 里已失效的 key，不能再从 messages 兜底列出来。
        # 否则"删了还在列表里"——失效等于没失效。
        cur = self.store.conn.execute(
            """SELECT key, source, content, created_at FROM messages
               WHERE id IN (SELECT MAX(id) FROM messages WHERE key IS NOT NULL GROUP BY key)
                 AND key NOT IN (SELECT key FROM facts WHERE archived_at IS NOT NULL)"""
        )
        for row in cur.fetchall():
            if row[0] in seen_keys:
                continue
            results.append({
                "key": row[0],
                "source": row[1],
                "value_preview": row[2][:100],
                "updated_at": row[3],
            })
            seen_keys.add(row[0])

        return results

    def get_memory_index(self, keys: list) -> list:
        """返回记忆索引信息（不含完整内容，不增加 access_count）。

        供 agent_guide 增强：返回 [{key, summary, updated_at, stale, days_since_update}, ...]
        """
        from datetime import datetime as _dt

        results = []
        for key in keys:
            meta = self.store.get_fact_meta(key)
            if meta:
                updated_at = meta.get("updated_at")
                days_since = None
                stale = False
                try:
                    if isinstance(updated_at, str):  # 收窄为 str 再 strptime，规避 Optional
                        updated_dt = _dt.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                        days_since = (_dt.now() - updated_dt).days
                        stale = days_since >= self.config.maintain_stale_days
                except (ValueError, TypeError):
                    pass

                value = meta.get("value", "")
                summary = index_summary_from_value(value)

                results.append({
                    "key": key,
                    "summary": summary,
                    "updated_at": updated_at,
                    "stale": stale,
                    "days_since_update": days_since,
                })
            else:
                results.append({
                    "key": key,
                    "summary": None,
                    "updated_at": None,
                    "stale": False,
                    "days_since_update": None,
                })
        return results

    def find_consumable_memories(self, task_type: str, task_query: str | None = None) -> list[dict]:
        """反向查询：找出所有声明了 consumption_contexts 匹配 task_type 的记忆。

        v3 优化：优先用 SQL 索引过滤 consumption_contexts 列（LIKE 模式匹配），
        SQL 异常时回退到全表扫描 + JSON 解析。

        候选集必须同时包含三类记忆（缺一即漏召回，2026-09-13 code review 5-1）：
        - contexts 命中 task_type 的记忆（SQL LIKE 过滤）
        - 纯 trigger_keywords 记忆（contexts 列为 NULL 或 '[]'）
        - v3 旧数据（两列均 NULL，从 value JSON 解析）

        遍历候选 facts，解析 value JSON，检查：
        1. consumption_contexts 是否含 task_type（支持 "*" 通配和 "scope.*" 通配）
        2. trigger_keywords 是否出现在 task_query 中

        返回 [{key, summary, updated_at, stale, days_since_update, matched_by}, ...]
        matched_by: "context" | "keyword" | "context+keyword"
        不增加 access_count（用 get_fact_meta 而非 get_fact）。
        """
        import json as _json
        from datetime import datetime as _dt

        results: list[dict] = []
        task_query_lower = task_query.lower() if task_query else ""
        task_scope = task_type.split(".", 1)[0] if "." in task_type else task_type

        # v3 优化路径：用 SQL LIKE 过滤 consumption_contexts 列
        # 匹配三种模式：
        #   1. '"*"' — 全场景通配
        #   2. '"task_type"' — 精确匹配（如 "recurring.accounting"）
        #   3. '"scope.*"' — scope 通配（如 "recurring.*"）
        # 注意：LIKE 模式中的 % 匹配任意字符，所以查询字符串中的特殊字符需转义
        # 但 task_type 通常只含字母、数字、点、下划线、连字符，无需转义
        sql_patterns = [
            '%"*"%',                       # 全场景通配
            f'%"{task_type}"%',            # 精确匹配
            f'%"{task_scope}.*"%',         # scope 通配
        ]
        where_clause = " OR ".join(["consumption_contexts LIKE ?"] * len(sql_patterns))
        # 候选集除 context 命中行外，还必须含"纯 keyword"（contexts 为 NULL/'[]'）
        # 与"v3 旧数据"（两列均 NULL）行，交给下方 Python 逐条匹配。
        # 历史缺陷：候选集只含 LIKE 命中行且回退条件写成 `if not rows`（仅 SQL 空结果触发），
        # 导致只要存在任一 context 命中行，keyword-only/旧数据记忆永远不被召回。
        # C1（2026-10-18）：已失效行永不召回。括号必须包住整个 OR 组，
        # 否则 `AND archived_at IS NULL` 只会约束最后一个 OR 分支。
        candidate_clause = (
            f"(({where_clause}) OR consumption_contexts IS NULL OR consumption_contexts = '[]')"
            " AND archived_at IS NULL"
        )

        try:
            cur = self.store.conn.execute(
                f"""SELECT key, value, updated_at, fact_type, occurred_at, mentioned_at,
                          consumption_contexts, trigger_keywords
                   FROM facts
                   WHERE {candidate_clause}""",
                sql_patterns,
            )
            rows = cur.fetchall()
        except Exception as e:
            logger.debug(f"find_consumable_memories: SQL 索引查询失败，回退全表扫描: {e}")
            rows = None

        # v3 优化路径未命中或旧数据：回退到全表扫描 + JSON 解析
        if not rows:
            try:
                all_facts = self.store.list_facts(limit=500)
            except Exception as e:
                logger.debug(f"find_consumable_memories: list_facts 失败: {e}")
                return results
            rows = [
                {
                    "key": f.get("key"),
                    "value": f.get("value", ""),
                    "updated_at": f.get("updated_at"),
                    "fact_type": f.get("fact_type"),
                    "occurred_at": f.get("occurred_at"),
                    "mentioned_at": f.get("mentioned_at"),
                    "consumption_contexts": f.get("consumption_contexts"),
                    "trigger_keywords": f.get("trigger_keywords"),
                }
                for f in all_facts
            ]
        else:
            # 把 SQL 结果转成 dict 列表
            new_rows = []
            for row in rows:
                # row 是 tuple，按 SELECT 顺序对应
                new_rows.append({
                    "key": row[0],
                    "value": row[1] or "",
                    "updated_at": row[2],
                    "fact_type": row[3],
                    "occurred_at": row[4],
                    "mentioned_at": row[5],
                    "consumption_contexts": row[6],  # JSON 字符串
                    "trigger_keywords": row[7],  # JSON 字符串
                })
            rows = new_rows

        for fact in rows:
            key = fact.get("key")
            value_str = fact.get("value", "")
            if not value_str:
                continue

            # 优先用独立列的 consumption_contexts/trigger_keywords，回退到 value JSON 解析
            contexts_raw = fact.get("consumption_contexts")
            keywords_raw = fact.get("trigger_keywords")

            contexts: list = []
            keywords: list = []

            if contexts_raw:
                try:
                    parsed = _json.loads(contexts_raw)
                    if isinstance(parsed, list):
                        contexts = parsed
                except (ValueError, TypeError):
                    pass

            if keywords_raw:
                try:
                    parsed = _json.loads(keywords_raw)
                    if isinstance(parsed, list):
                        keywords = parsed
                except (ValueError, TypeError):
                    pass

            # 旧数据无独立列，回退到 value JSON 解析
            if not contexts and not keywords:
                try:
                    value = _json.loads(value_str)
                    if isinstance(value, dict):
                        contexts = value.get("consumption_contexts") or []
                        keywords = value.get("trigger_keywords") or []
                except (ValueError, TypeError):
                    continue
            else:
                # 有独立列数据，但可能还需要从 value 解析 summary
                try:
                    value = _json.loads(value_str)
                    if not isinstance(value, dict):
                        value = {}
                except (ValueError, TypeError):
                    value = {}

            if not contexts and not keywords:
                continue

            # 检查 consumption_contexts 匹配
            matched_context = False
            matched_specific = False   # 由具体 task_type / scope.* 命中（不是 "*"）
            matched_wildcard = False   # 仅由 "*" 命中
            for ctx in contexts:
                if ctx == "*":
                    matched_context = True
                    matched_wildcard = True
                    continue
                if ctx == task_type:
                    matched_context = True
                    matched_specific = True
                    break
                if ctx.endswith(".*"):
                    ctx_scope = ctx[:-2]
                    if ctx_scope == task_scope:
                        matched_context = True
                        matched_specific = True
                        break

            # 检查 trigger_keywords 匹配
            matched_keyword = False
            if task_query_lower and keywords:
                for kw in keywords:
                    if kw.lower() in task_query_lower:
                        matched_keyword = True
                        break

            if not matched_context and not matched_keyword:
                continue

            # 构建索引项（复用 get_memory_index 的摘要逻辑）
            updated_at = fact.get("updated_at")
            days_since = None
            stale = False
            try:
                if isinstance(updated_at, str):  # 收窄为 str 再 strptime，规避 Optional
                    updated_dt = _dt.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                    days_since = (_dt.now() - updated_dt).days
                    stale = days_since >= self.config.maintain_stale_days
            except (ValueError, TypeError):
                pass

            summary = index_summary_from_value(value_str)

            if matched_context and matched_keyword:
                matched_by = "context+keyword"
            elif matched_context:
                matched_by = "context"
            else:
                matched_by = "keyword"

            results.append({
                "key": key,
                "summary": summary,
                "updated_at": updated_at,
                "stale": stale,
                "days_since_update": days_since,
                "matched_by": matched_by,
                "fact_type": fact.get("fact_type"),
                "occurred_at": fact.get("occurred_at"),
                # v5：只靠 "*" 命中的标记，供 agent_guide 给全场景通配限槽。
                # 实测 35/151 条 fact 声明 "*"（=对所有任务永远相关），任何 task_type
                # 都被灌进 ~37 条候选；调用方需要能区分"为这个任务写的"和"对所有人说的"。
                "via_wildcard": matched_wildcard and not matched_specific and not matched_keyword,
            })

        return results

    # ─── 新接口 ─────────────────────────────────────────────

    def search(self, query: str, top_k: int = 10,
               since: float | None = None, until: float | None = None,
               source: str | None = None) -> list[dict]:
        """语义搜索

        v3 改进：启用 EvidenceLedger 时，组织三源召回（messages + facts + summaries）
        为结构化证据集返回；未启用时回退到原 messages-only 行为。

        source=None 时默认排除 `source='tool'` 的工具调用日志（实测占 messages
        的 91.7%，不排除会把真记忆挤掉）；要查工具日志显式传 source="tool"。
        """
        # 1. 原 messages 搜索（SemanticSearch 内部已记录 trace）
        exclude_sources = {"tool"} if source is None else None
        msg_results = self.semantic.search(
            query, top_k=top_k * 2, since=since, until=until, source=source,
            exclude_sources=exclude_sources,
        )

        # 2. 启用 EvidenceLedger 时，从 facts 和 summaries 也搜一遍，然后组织证据
        if self.config.enable_evidence_ledger:
            fact_results = self._search_facts(query, top_k=top_k)
            summary_results = self._search_summaries(query, top_k=top_k)
            # 拿到 trace_id 用于审计关联（单线程安全）
            trace_id = getattr(self.semantic, "_last_trace_id", None)
            evidence = self.evidence_ledger.organize_evidence(
                query=query,
                message_results=msg_results,
                fact_results=fact_results,
                summary_results=summary_results,
                trace_id=trace_id,
            )
            return evidence[:top_k]

        # 未启用 EvidenceLedger，回退原行为
        return msg_results[:top_k]

    def _search_facts(self, query: str, top_k: int = 10) -> list[dict]:
        """从 facts 表搜索匹配 query 的事实

        v2（2026-10-10，工具体检 B1）：原来是 `LIKE '%整串 query%'`，中文一加空格
        就零命中 —— 查「抽卡 游戏 状态」时三个词都在 value 里、整串却不连续，
        fact 全漏；而 messages 走向量+BM25 永远能凑满 top_k 把位置占掉，于是这个
        端点对外表现成「只搜得到 message」，且是**静默**的。

        现在：按 BM25 侧的同款分词切成词项 → SQL 侧 OR 召回候选 → Python 侧按
        「命中词项数 / 词项总数」算 match_score 排序。整串的每个词项都是它的子串，
        所以原来能精确子串命中的场景排序不变（覆盖率满分），只是不再因为差一个空格
        就整条消失。

        ⛔ match_score 只是**召回质量**，不是可信度：下游 `EvidenceLedger` 会把它
        乘到 confidence 上当 retrieval_score（fact 的 confidence 实测恒为 1.0，
        不乘的话任何一条 fact 命中都稳压所有 message）。
        不调 LLM；facts 表 161 条量级，全表 LIKE 无压力。
        """
        terms = _fact_query_terms(query)
        if not terms:
            return []

        try:
            # OR 召回：任一词项命中即进候选（召回宁宽）；排序交给下面的覆盖率
            cond = " OR ".join(["key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\'"] * len(terms))
            params: list[str] = []
            for t in terms:
                pat = f"%{_escape_like(t)}%"
                params.extend([pat, pat])
            cur = self.store.conn.execute(
                f"""SELECT id, key, value, source, confidence, fact_type, occurred_at,
                          updated_at
                    FROM facts
                    WHERE ({cond}) AND archived_at IS NULL""",
                params,
            )
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]
        except Exception as e:
            logger.warning(f"_search_facts 失败: {e}")
            return []

        scored: list[dict] = []
        for r in rows:
            hay = f"{r.get('key') or ''}\n{r.get('value') or ''}".lower()
            hits = sum(1 for t in terms if t in hay)
            if not hits:
                # SQL LIKE 对 ASCII 大小写不敏感、Python 的 `in` 敏感，兜掉这层差异
                continue
            r["matched_terms"] = hits
            r["match_score"] = round(hits / len(terms), 4)
            scored.append(r)

        # 命中词项数降序；同分用 updated_at 降序（ISO 字符串可直接比）
        scored.sort(
            key=lambda r: (r["matched_terms"], str(r.get("updated_at") or "")),
            reverse=True,
        )
        return scored[:top_k]

    def _search_summaries(self, query: str, top_k: int = 10) -> list[dict]:
        """从 summaries 表搜索匹配 query 的摘要

        简单实现：用 SQL LIKE 匹配 summary 字段，按 end_time DESC 排序。
        """
        try:
            pattern = f"%{query}%"
            cur = self.store.conn.execute(
                """SELECT id, start_time, end_time, summary, message_count, model,
                          created_at
                   FROM summaries
                   WHERE summary LIKE ?
                   ORDER BY end_time DESC
                   LIMIT ?""",
                (pattern, top_k),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]
        except Exception as e:
            logger.warning(f"_search_summaries 失败: {e}")
            return []

    def timeline(self, since: float | None = None, until: float | None = None,
                 source: str | None = None, limit: int = 50) -> list[dict]:
        """时间线查询"""
        return self.store.query_messages(
            since=since, until=until, source=source, limit=limit
        )

    def get_status(self) -> dict:
        """获取记忆系统状态"""
        stats = self.store.get_stats()
        stats["recent_count"] = self.recent.count
        stats["embedding_ready"] = self.embedding.ready
        stats["embedding_model"] = self.config.embedding_model if self.embedding.ready else "unavailable"
        stats["interaction_count"] = self.recorder.interaction_count
        stats["pending_messages"] = len(self.recorder._pending_messages)
        stats["initialized"] = self._initialized
        stats["maintainer"] = self.maintainer.get_status()
        # Ticket 01：DB 大小历史采样（7 天滚动，in-memory，向后兼容字段）
        stats["db_size_history"] = self.store.query_db_size_history()
        return stats

    def shutdown(self) -> None:
        """关闭记忆系统"""
        self.recorder.force_flush()
        self.recent.flush()
        self.store.close()
        self._initialized = False
        logger.info("记忆系统已关闭")


def get_memory_manager() -> MemoryManager:
    """获取全局记忆管理器实例"""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MemoryManager()
            _instance.initialize()
        return _instance


def reset_memory_manager() -> None:
    """重置全局实例（测试用）"""
    global _instance
    with _instance_lock:
        if _instance:
            _instance.shutdown()
        _instance = None
