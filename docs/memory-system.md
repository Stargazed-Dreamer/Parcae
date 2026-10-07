# 记忆系统详解
> 本文档是 `AGENTS.md` 中记忆系统的详细展开。日常使用原则见 AGENTS.md "会话启动检查清单"。

后端提供三层记忆系统（`/memory/*`），供 agent 跨会话保存任务状态和用户偏好。
旧 KV 记忆（`/memory/{key}`）只读兼容，新数据写入 SQLite（`data/memory/memory.db`）。旧 KV 目录已清理，迁移已完成。

## 三层架构

| 层级 | 存储 | 用途 | 查询方式 |
|------|------|------|----------|
| L1 Recent | 内存 deque + JSON | 近期对话滑动窗口 | 顺序读取 |
| L2 Time-indexed | SQLite | 按时间范围查询消息 | 时间线查询 |
| L3 Semantic | 向量索引 + BM25 倒排 | 语义检索历史记忆 | 混合搜索 |

> ⚠️ **L1 Recent 目前只有写入方，没有生产读取方**：`server/memory/recent.py` 的
> `get_recent` / `get_context_for_prompt` / `search_recent` 全仓无调用点（`/memory/status`
> 的 `recent_count` 也只是它的长度）。"三层"在文档里成立，在消费链路上还有一层没接上——
> 讨论记忆架构时别把它当成已经在用的检索路径。

## 语义检索

- **向量嵌入**：ONNX Runtime + `BAAI/bge-small-zh-v1.5`（本地推理，无需 API Key）
- **BM25 关键词**：jieba 中文分词 + Okapi BM25 评分
- **混合召回**：alpha 加权融合（`hybrid_alpha=0.5`，0=纯BM25，1=纯向量）
- **HONEST_EMPTY 下限**：纯向量命中的候选还要过一道绝对相似度下限 `hybrid_min_vector_similarity`（默认 0.30，`[memory]` 段可配），低于下限且 BM25 也没命中的直接丢弃——宁可返回空，不返回"看着像"的假命中。被丢条数由 `semantic.py` 的 `_last_honest_empty_dropped` 记录并写进日志
- **降级模式**：嵌入模型不可用时自动降级为 BM25-only

> **禁用语义检索**：在 `config.toml` 的 `[memory]` 段设置 `enable_semantic = false` 可完全跳过嵌入模型下载与加载。
> - **不受影响**：KV 存储（`/memory/{key}`）、时间线查询（`/memory/timeline`）、Recent 滑动窗口、记忆压缩、自动记录、BM25 关键词检索
> - **不可用**：向量语义搜索（`/memory/search` 退化为 BM25-only）、语义索引构建（`/memory/reindex` 跳过向量部分）
> - **适用场景**：网络受限无法下载模型、计划改用简单文本存储替代向量检索

## 记忆压缩

- 定期用 cheap 模型压缩旧消息为摘要（无 API Key 时用规则提取关键点）
- 压缩触发条件：未压缩消息数超过 `compress_threshold`（默认 200）
- 手动触发：`POST /memory/compress`

## 自动记录

- MCP 中间件自动记录每次工具调用到记忆系统
- 每 N 次交互（`maintenance_interval`，默认 10）触发一次维护（保存待写入、压缩、清理）
- 排除低价值工具（如 `memory_status`、`exec_status`）

## 使用原则

- **任务完成时写记忆**：每个 Skill 工作流结束后，将任务进度、关键结果写入记忆（如 `bilibili_gacha` 记录上次扫描时间）
- **会话开始时读记忆**：处理用户请求前，先 `GET /memory/{key}` 了解上次做到哪了
- **语义搜索**：用 `POST /memory/search` 按语义查找相关记忆，而非仅靠 key
- **记忆要清洗**：定期检查记忆是否过期，手动调用 `POST /memory/cleanup/orphaned` 清理超过 `orphan_cleanup_days`（默认30天）的已压缩消息和超过 `summary_cleanup_days`（默认180天）的旧摘要

## 记忆 key 规范

- 每个 Skill 对应一个 key，如 `bilibili_gacha`、`yihuan_gacha`、`accounting`
- 跨 Skill 的用户偏好用 `preferences` key
- key 只允许字母、数字、下划线、连字符

## API

| 路径 | 方法 | 说明 |
|------|------|------|
| `/memory/status` | GET | 记忆系统状态（含三层统计） |
| `/memory/list` | GET | 列出所有记忆 key |
| `/memory/{key}` | GET | 读取记忆（兼容旧 KV）；带 `?as_of=YYYY-MM-DD` 时查历史视图，见「记忆版本历史」 |
| `/memory/{key}` | POST | 写入/更新记忆（`merge=true` 时只深合并 `data` 原文，不并入元数据信封）；落库后 `fact_type` 仍为空时响应带 `guidance` 字段回显后果，见「写入端缺 fact_type 的回显」 |
| `/memory/{key}` | DELETE | 删除记忆 |
| `/memory/search` | POST | 语义搜索（向量+BM25混合） |
| `/memory/timeline` | GET | 时间线查询 |
| `/memory/compress` | POST | 手动触发记忆压缩 |
| `/memory/record` | POST | 手动记录交互 |
| `/memory/stats` | GET | 详细记忆统计 |
| `/memory/reindex` | POST | 重建语义索引 |
| `/memory/cleanup/orphaned` | POST | 清理超过 orphan_cleanup_days 的已压缩消息 + 超过 summary_cleanup_days 的旧摘要 |
| `/memory/maintain` | POST | 手动触发记忆维护（`force=true` 忽略时间检查；`force_revalidate=true` 先把所有已验证标签重置为 pending，见「验证器改版后重判存量」） |
| `/memory/maintain/status` | GET | 记忆维护器状态（上次运行/stale数量/验证分布） |
| `/memory/projections` | GET | 列出上下文投影留痕（最近 N 条，按时间倒序） |
| `/memory/projections/{id}` | GET | 单条投影留痕详情（含上屏顺序、被砍掉的 key、召回原序） |

## 上下文投影留痕（context_projections，schema v4）

> 目的：把"记忆进入推理之前的链路"做成可检查的——不只是"模型想起了这条"，而是这条从哪来、为什么被召回、当时排第几、最后有没有真的上屏。

`server/memory/projection.py` 写读 `context_projections` 表（`server/memory/schema.py` 的 `SCHEMA_VERSION` 为准，v4 新增；迁移只建表，不动旧数据）：

| 列 | 含义 |
|------|------|
| `surface` | 注入面（当前只有 `agent_guide.must_read`）；`task_type` 记消费场景 |
| `selected_keys` | 最终上屏的 key，**按上屏顺序** |
| `dropped_keys` | 召回到了但被条数上限砍掉的 key |
| `rank_before` / `rank_after` | 上游召回给出的顺序 vs 实际上屏顺序，两列并排存，排序偏离可回查 |
| `validation_flags` | 每个 key 在写入时点的有效性状态快照（来自 `memory_meta`） |
| `injected_text_digest` / `injected_chars` | 实际注入文本的 sha256 与长度，用于比对"留痕说注入的"与"真注入的"是否同一份 |
| `trace_id` | 有检索 trace 时回填；`agent_guide` 的 must_read 走 facts 定向召回、不产生 trace，此处为空 |

当前写入方是 `agent_guide` 的【必读记忆】块（`server/agent_guide.py`），一个 surface 常量 `SURFACE_AGENT_GUIDE_MUST_READ = "agent_guide.must_read"`。GUI 侧在记忆面板的"检索过程"页切到"上下文投影"来源即可回放（`client/panels/memory/search_traces_page.py`）。

消费时点的两个配套行为：
- **有效性存疑不拦截、但降位 + 显式警告**：`validation_status` 为 `archive`/`update` 或带 staleness 警告的记忆，排在必读块最后，并附一条"引用前必须先验证"的可见警告（不做静默丢弃）。
- **摘要类证据不冒充有分**：`Evidence.scored=False` 表示来源本身不产生相似度（如摘要窗口命中），排序时一律压在有分证据之后（`server/memory/evidence.py`）。

保留期由 `[memory] projection_retention_days`（默认 30）控制，维护器每轮调 `projection.cleanup_old()`，删除条数以 `projection_cleanup_deleted` 出现在维护结果里。

## 记忆维护器（maintainer）

后端内置 `MemoryMaintainer`，由 `[loops.memory]` 定时循环自动运行（间隔见该配置段，默认每 6 小时），另有记忆 API 调用时的惰性触发兜底。职责：

### aging（老化）

- 查询 `facts` 表中 `updated_at` 早于 `now - [memory.maintain].stale_days` 的记忆
- 每轮都更新 `memory_meta.stale_score`（距上次更新的天数，staleness 警告要用它）
- **状态重置按时间门控（v5）**：只有 `validation_status='pending'`、或从未验证过
  （`last_validated_at IS NULL`）、或距上次验证超过 `validate_revalidate_days` 的记忆，
  才被重置回 `pending`。旧行为是每轮把 `keep`/`update` 一律重置回 `pending`，
  等于对每条 stale 记忆每 6 小时重验一次（白烧 cheap 池配额）
- **`archive` 也纳入可重验范围（v5）**：旧代码的 CASE 只翻转 `keep`/`update`，
  `archive` 是**永久标签**——一次误判就永远留在账上，修好验证器也救不回来。
  存量 archive 想尽快按新判据重判，用 `POST /memory/maintain {"force": true, "force_revalidate": true}`
  （显式动作：会按 `batch_limit` 逐轮消耗 cheap 池并改写标签，所以不自动跑）

> **存量标签已于 2026-10-04 全量重判（洗存量那一档，此前只修了机制）**。按重判前的库备份逐 key
> 对照：旧 `archive` 72 → `keep` 69 / `update` 2 / 仍 `archive` 1（98.6% 翻案），旧 `keep` 53 全部
> 维持；archive 占比从 0.46 降到 0.006。留下的那条 archive 是"截止日期已过"这类**内容本身**过期的，
> 不是误判残留。两个操作坑：① `force_revalidate` 重置的是**所有非 pending 标签**（129 条，不止
> archive），且**只能调一次**——再调会把已判完的重新打回 pending 重烧配额；② 要做前后对比必须
> 先留一份重判前的备份并按 key 对照，**别只数最终条数**（WAL 库直接复制主文件会缺行，见上方注）。

### validate（验证）

- 对 `validation_status='pending'` 的记忆调用 cheap 模型，单轮最多 `validate_batch_limit` 条
  （按 `stale_score` 从旧到新取）
- LLM 做选择题（不做摘要），返回 JSON：

```json
{"is_accurate": true, "reason": "仍然准确", "suggested_action": "keep"}
```

- `suggested_action` 取值：`keep`（保留）/ `update`（需更新）/ `archive`（归档）
- LLM 失败时记忆保持 `pending`，下个周期重试（不确定时保留）

### 验证器喂给模型什么（v5 重写，`maintainer.build_validation_preview`）

旧实现是 `value[:800]`。体检实测：多数 fact 超过 800 字符，且相当一部分的**前 800 字符
就是写入时抄进正文的元数据信封**（`{"source": "manual", "updated_at": ...}` 这类），
于是 cheap 模型给出的 archive 理由是"内容被截断/乱码/无实际语义/技术细节"——
它看到的是信封，不是信。现在的规则：

> **存量信封已于 2026-10-03 清洗**（67/155 条 fact 的顶层 6–11 个信封键，连同
> `messages(source='manual')` 当前快照一起洗，并逐行 `reindex_message` 重建索引；
> 备份 `data/memory/memory.db.bak_before_envelope_strip_220804_REPAIRED`；
> ⚠️ 库在 WAL 模式，`shutil.copy2` 复制主文件会漏掉未 checkpoint 的行——快照要用
> `sqlite3.Connection.backup()` 或先 `PRAGMA wal_checkpoint(TRUNCATE)`，并复验行数）。
> 上面那条"剥信封"规则**不是历史包袱**，它现在服务两件事：① `fact_revisions` 里的历史副本
> 仍带信封（114/185 行，属"当时正文长什么样"的存档，故意不洗）；② 防回归——万一写入侧
> 再次把列抄进 value，验证器不受影响。
>
> **②已经拿到真证据了（2026-10-04）**：洗完之后一直没写过 fact，所以"不会再长回来"当时
> 只是推断。10-04 当天有 **18 条 fact 走线上 `POST /memory/{key}` 重写过**（改死链 + 改正文），
> 其中 `approval_strictness_levels` / `release_phase2_gh_mirror_isolation` 两条此前**根本没有**
> `messages(source='manual')` 快照行，是当天 00:37 新建的——正是"新写入会不会带信封"最干净的样本。
> 按昨天那套三重守卫指纹（顶层信封键 + `source=="manual"` + `confidence==1.0`）复扫，
> 全库与这 18 行**都命中 0**，新建那 2 条的顶层键里也没有 `source`。写入侧不复发，这下是实测不是推测。
> 顺带扫出一个**当时故意没碰的轻形态**（2026-10-04 记录）：3 条 fact 顶层有 `source="manual"` 但没有 `confidence`
> （`reference_agents_md_keywords_pollute_mock_llm_judge_call` 与
> `reference_sqlite_wal_pragma_exclusive_lock_concurrent_init` 写在 2026-08-03，
> `reference_memory_write_offline` 写在 2026-09-18，都在信封清洗之前），落在那道"宁可少洗"的窄判据之外。
> **已补洗干净（2026-10-05）**：先核对这三条的信封值在 `facts` 列里都有同等值才剥（防止把路由元数据剥丢），
> 走 `POST /memory/{key}` `merge=false` 整坨替换，落库后快照与 `facts.value` 逐字相等、`fact_revisions` 有当日行；
> 复扫全库顶层 `source=="manual"` 命中 **0**。其中前两条的 `fact_type` 列本来是 NULL，
> 按其自身 `type="reference"` 补成 `reference`——这属于"值里有明确类型"的那档，不是猜的。
> 另有 4 条 fact 的顶层 `source` 是**正经业务键**（值是"用户口述复盘"这类出处，分别是
> `oracle_vm_stage3_pitfalls_20260915` / `reference_guide_semantic_matching_baseline` /
> `reference_chromium_occluded_rendering` / `project_gamejam_lessons`），补洗时逐条验证过没被误删；
> 统计时按 shape 数会把它们误算成信封，**要按指纹数**（这条判据在洗完后依然成立）。

1. **剥元数据信封**：`source/updated_at/created_at/access_count/confidence/mentioned_at/
   fact_type/consumption_contexts/trigger_keywords/source_task/type/date` 一律不进 prompt；
   其余业务键（正文、rules、problem/solution 等）全部保留，**不静默丢**——丢了就等于"看不全"。
2. **窗口放大**到 `[memory.maintain].validate_preview_chars`。
3. **超窗不采纳 archive**：正文仍超出窗口时，本轮不给死刑，留在 `pending` 并在
   `validation_note` 写明"第 N 次未采纳"（计数在 `memory_meta.validate_attempts`）。
   连续 `3` 次仍超窗 → 改判 `update`（"臃肿到无法判读"是**重写信号**，不是过时信号）。
4. **判据分 fact_type**：状态类几天就变、偏好类几年不变，旧 prompt 用同一把尺子量五类记忆。
   分档判据见 `server/memory/maintainer.py` 的 `_FACT_TYPE_GUIDANCE`。
5. **去项目中心偏见**：旧 prompt 明文要求把"与当前项目无关的个人数据（如游戏抽卡记录、
   非项目相关的个人统计）"判 archive。验证的问题被改成"这条记忆所说的内容**是否仍然成立**"，
   并禁止以"属于个人生活域 / 与项目无关"为理由归档——生活事务恰恰是个人管家最需要长期记住的部分。
6. 超窗时 prompt 末尾追加可见标记「（正文过长，已截断）」并明文禁止把它读成"内容不完整所以判死"。
7. **拒绝"知识截止"类无效依据**（2026-10-03 重判存量时实测到，`is_invalid_archive_reason`）：
   cheap 模型会把晚于自己训练截止时间的时间戳读成"未来未发生 ⇒ 内容无效"，据此判 archive。
   这条偏见的杀伤面很专一——**优先杀最新写入的记忆**（实测中招的有某条同期上屏 283 次的高频记忆）。
   处理与"超窗"同构：prompt 里把时间线钉在调用方给出的「今天」，并声明"你的知识截止与本库无关"；
   命中该类理由的 archive **本轮不采纳**（`memory_meta` 计数 + note 写明"无效依据"），
   连续 3 次只给这种理由则落 `keep`——反复只有无效依据，等于没有成立的失效证据。
   计数出口区分两类：`deferred_invalid_reason`（依据不成立）与 `deferred_truncated`（没看全）。

### 触发机制

- **定时循环**：`[loops.memory]` 注册为 Loop 定时任务（APScheduler `IntervalTrigger`），按 `maintain_interval` 周期直接调 `maintainer.run()`
- **惰性兜底**：每次记忆 API 调用时检查 `schema_info['maintainer_last_run']`，距上次超过间隔则在后台 `_async_maintenance` 中触发
- **手动**：`POST /memory/maintain`（`force=true` 忽略时间检查，`force_revalidate=true` 先重置全部已验证标签）

`maintainer.run()` 的固定顺序：垃圾预过滤 → 老化 → 验证 → **指标日聚合** → 三张明细表的
retention 清理。聚合必须排在清理之前，否则每天先删后算、指标永远是空表。

### 配置

`[memory.maintain]` 段的键名不带 `validate_` 前缀（与 dataclass 字段 `validate_*` 一一对应，
映射在 `server/memory/config.py`）：

```toml
[memory.maintain]
interval_hours = 6              # 运行间隔（→ maintain_interval_hours）
stale_days = 7                  # 过期阈值（→ maintain_stale_days）
enable_validate = true          # 启用 LLM 验证（→ maintain_enable_validate）
preview_chars = 2400            # 验证器可见正文字数上限（→ validate_preview_chars）
batch_limit = 25                # 单轮最多验证条数（→ validate_batch_limit）
revalidate_days = 30            # 同一条记忆两次验证的最小间隔（→ validate_revalidate_days）
```

> `model_tier` 字段已移除；验证用 key 由 `USE_CASE_REGISTRY.memory_validate` 解析
> （`memory_maintainer` 这个 use_case 在 v15 就废弃了）。

## 记忆指标日表（memory_metrics_daily，v5）

> 动机：`search_traces` / `evidence_ledger` / `context_projections` 三张明细表各按 retention
> 硬删，**删之前没有任何聚合**。结果是"记忆库召回质量随时间怎么变"在代码里根本问不出来——
> 明细表累计写入过上千行，到期就成了空表。本表把按日可聚合的标量指标固化下来，
> 是那些明细表的**替代物**，因此不参与维护器的 retention 清理。

- 实现：`server/memory/metrics.py`；写入时机：`MemoryMaintainer.run()` 里**排在三张明细表
  的清理之前**（顺序错了就永远是零，这是硬约束，别挪到后面）
- 表形状是窄表 `(date, metric, value, extra)`：加指标不必再动 schema；
  `extra` 存样本 key 清单与窗口说明（例如 `surfaced_ratio` 会带上"观测窗口=投影表留存天数，
  非全生命周期"，防止被读成"这些记忆永远不会被用"）
- 指标种类以代码为准（`compute_metrics` 里每个 `_safe(...)` 调用），文档不抄数量
- 计算失败**不静默**：失败项汇总成 `_errors` 指标并由维护器 log warning，
  审计报告把它判红——"指标算不出来"和"指标是 0"必须能区分开
- `db_size_history` 从内存 dict 改为落库（`store.query_db_size_history` 读写本表的
  `db_size_mb`）。此前后端一重启采样就归零，而健康分的"DB 增长率"维度需要 ≥2 个采样才参与，
  于是那一维实际上从没被算过
- 读回入口：`tools/audit/memory_health_report.py`（只读体检报告，见 `tools/README.md`）

**索引覆盖率的口径（v5 修正）**：`vector_coverage` 的分子**只数能 JOIN 回 `messages` 活行的向量**。
旧口径把孤儿行也算进去，实测虚报 15% 覆盖，按可解析算只有 ~6%。成对的 `index_orphan_rows`
报的是反向事实——**指向已不存在 message 的索引行数**（向量/BM25 分别给数，并按产生日分组，
用来区分"封口的一批历史遗留"和"还在长"）。

> 孤儿是怎么来的（2026-08-18 那 419 行的根因）：`vector_index` / `bm25_inverted` 都写了
> `REFERENCES messages(id) ON DELETE CASCADE`，级联也确实生效——但 SQLite 的
> `PRAGMA foreign_keys` 是**按连接**生效且**默认关闭**的。生产连接在 `init_db` 里开了它
> （实测 `PRAGMA foreign_keys`=1，删除消息后两张索引表零残留），
> 而**任何裸 `sqlite3.connect()` 的一次性批量脚本都不会有**。那批孤儿正是这么留下的。
> 回归测试 `TestIndexHygieneMetrics` 把两侧都钉住了：生产连接插指向不存在消息的向量行会
> 被 `IntegrityError` 拒绝，`delete_message` 不留孤儿。
>
> **那批孤儿已于 2026-10-03 23:19 清掉**（`vector_index` 683 → 264，`index_orphan_rows` 归 0）。
> 清前用 `sqlite3.Connection.backup()` 做快照并**逐表复验行数**，删除只按
> `message_id NOT IN (SELECT id FROM messages)` 这一个条件。副作用要说清：覆盖率数字从
> 虚高的 15% 掉到真实的 **6.4%** —— 变差的是账面，不是能力。

**判卷纪律**：行为类指标（消费漏斗、归档率、召回精度）只报数、给趋势，**不设红黄线**——
没有基线就不设阈值，攒够两周日表后再按实测分布定线。只有结构上无争议的缺陷才判色
（指标算不出来、明细被删而日表不在、superseded 旧全文仍在索引、**索引行指向已删除的 message**、
`fact_type` 越出 closed taxonomy、投影层与检索层因 `trace_id` 全空而无法 join）。被评对象不能自己出题自己判卷，
所以"前 N 条到底有没有用"要另行人工标注，不由这套指标自称。

## 记忆正文里的路径会腐烂（死链审计）

`do_not_store` 禁清单写着"文件路径不存"，但 `reference` 类记忆的本质就是指针，禁不掉。
实测 156 条 facts 里 **102 条正文带项目内路径**，首轮扫描其中 54 条已经是死链——文件一搬家它们
就腐烂，而这条腐烂**不会让任何现有指标变红**（`index_orphan_rows` 只管索引行指向已删 message，
不管正文里的路径字符串）。

审计判据（四步，全部实测，不接受"看起来像"）：

1. 从 `facts.value` 与 `messages` 的**当前快照行**（召回真读的那份）里正则抽出项目内路径 token。
2. 每个 token 做双向判定：`os.path.exists`（本机在不在）+ `git ls-files`（入库没有）。
3. 死链按性质分档，**不是一刀切都改**：
   - `temp/` `data/` 这类易失目录 → 属预期腐烂，不改（改了下次照样被清）；
   - 有唯一后继（全仓 `find` 同名文件只有一个，且 `git log` 能对上搬迁提交）→ 改指针；
   - basename 也变了（拆包/改名）→ 只认 `git log -M --follow --diff-filter=R` 里的重命名记录放行，
     ⚠️ 按**旧路径**查 `--diff-filter=R` 实测返回空（旧路径改名后不再被跟踪），方向要反过来查；
   - 找不到后继 → 逐条读上下文再判，其中两类**不是失效指针**：跨项目引用（正文写的是别的仓里
     的同名文件）、以及"已删除"论断的内容本身（漂移清单里"让用户跑已删除的 X"就是 X 不存在才对）。
4. 替换必须幂等：`docs/x.md` 是 `workspace/model_data/docs/x.md` 的**子串**，直接 `str.replace`
   跑第二遍会套娃。判据是"改完再跑一遍必须逐字不变"，不是"第二遍没报错"。

> **为什么不能拿裸 SQL 改 `facts.value`**：召回读的是 `messages` 的 `source='manual'` 当前快照行
> 及其 BM25/向量索引，**不是 facts**。只 UPDATE facts 的结果是"账面改了、召回照旧"——本项第一版
> 就是这么错过一次，靠逐字比对 `facts.value == messages.content` 才发现。走线上
> `POST /memory/{key}`（`manager.set`）会连带做 `update_message_content` +
> `semantic.reindex_message`，并且覆盖前把旧值记进 `fact_revisions`，**那张表就是批量改的撤销通道**，
> 不必另做文件快照。被取代的历史快照行不用洗：`recorder._cleanup_superseded_index`
> 已清空它们的 BM25/向量（实测 183 份旧副本 0 行被索引），旧内容进不了召回，留着只作历史。

2026-10-04 首轮执行结果：持久目录死链 26 → 6，剩余 6 条经逐条读上下文判定**都不是失效指针**
（跨项目同名文件 3：wechatDecode / melodymark / 外部 widgets 仓；"这脚本已删除"论断的内容本身 2；
拆包沿革说明 1——`server/browser_session.py` 已改写成 `server/browser/session/manager.py:404`，
旧名只作为"a5395c2 拆分前"的说明留在句子里）。实际改指针 **21 条路径 / 14 条 fact**。
`temp/` 那类易失目录引用（76 个目标 / 37 条 fact）按第 3 档**没动**——它们本来就该随清理消失。

## 必读记忆块的全场景通配限槽（v5）

`consumption_contexts=["*"]` 的语义是"对所有任务永远相关"。实测这类记忆占 facts 的两成上下，
于是任何 `task_type` 的候选集都被灌到数十条，而 `find_consumable_memories` 没有 ORDER BY、
没有阈值、没有 cap——`agent_guide` 切前 `_MUST_READ_MAX` 条实际等于"谁写得最晚谁上屏"。

现在（`server/agent_guide.py` 的 `_select_must_read`，配额常量 `_MUST_READ_WILDCARD_QUOTA`）：

- `find_consumable_memories` 为每条命中项返回 `via_wildcard`（**只**靠 `"*"` 命中、
  本轮没有具体场景或关键词命中）
- 必读块里通配最多占 `_MUST_READ_WILDCARD_QUOTA` 个名额，其余让给具体命中项
- **不拦阻**：具体命中项坐不满名额时仍用通配补位（宁少勿空），被让位的通配项进
  `context_projections.dropped_keys` 留痕，`note` 里写明"通配限槽让位 N 条"——事后可查是谁把它挤下去的


## 记忆版本历史（fact_revisions，schema v6）

> 动机：一个 key 只在 `facts` 里留**一条当前真相**，但"曾经为真"必须查得到——管家场景里
> "你上次说的止损线是 12%"和"现在是 15%"是同一件事的两面。旧实现把每次写入的全文副本堆在
> `messages(source='manual')` 里，靠时间线页顺带承担历史、没有显式语义：这些副本**同时进了
> BM25/向量索引**，整改前实测积出 183 份冗余副本、约 6.9 万行 BM25 词条仍可被召回——"当前真相"和
> "曾经为真"混在同一条召回通道里（71 个 key 有多版本，`stock_advisor` 一个 key 存了 20 份全文）。
> **现已摘除**：`recorder._cleanup_superseded_index()` 按 `source_message_id` 清掉旧副本的
> BM25/向量行（只清索引不删行），复测 `bm25_term_rows_on_superseded = 0`。

- 表结构（`server/memory/schema.py` 的 `SCHEMA_VERSION` 为准，v6 新增）：
  `fact_revisions(id, key, value, valid_at, invalid_at, source_message_id, migrated, created_at)`，
  索引 `idx_fact_rev_key(key, valid_at)`。
- **撤销通道只覆盖 `value`，不覆盖信封路由列**：这张表没有 `fact_type` / `consumption_contexts` /
  `trigger_keywords` 三列，所以路由列写错不能用 `GET /memory/{key}?as_of=` 回滚，只能再写一次正确值。
  其中 `consumption_contexts` 写成一个不存在的 task_type 是**静默失效**（该记忆从此不再被 `agent_guide`
  召回，本身仍是绿写入）。相邻的一条行为：`merge=true` 时**没传**的路由列保持原值（2026-10-06 实测，
  只传 `consumption_contexts` 时 `trigger_keywords` 未被清）。
- **`facts` 保持 `key UNIQUE`**——它就是当前值。所有按 key 读 `facts` 的既有代码
  （`memory_meta` 外键、`access_count` 递增、维护器 join、`get_fact`）**一行都不动**，
  这是选"单行当前值 + 历史表"而非"`facts` 去 UNIQUE 多行共存"的核心理由（后者把回归风险
  铺到写入侧，写坏一次就是静默毁数据）。
- **区间是双时态**：`valid_at` = 该值开始为真，`invalid_at` = 被取代时刻；查询条件
  `valid_at <= as_of AND (invalid_at IS NULL OR as_of < invalid_at)`。时间列统一用
  `datetime('now','localtime')` 同格式文本（与 `facts.occurred_at` / `messages.created_at` 一致），
  可直接字符串比较排序，别混入 REAL epoch。
- **`key` 不设外键**：`memory_meta` 已是 `ON DELETE CASCADE`，历史表若跟着级联，
  `DELETE /memory/{key}` 会把历史一起抹掉——与"失效 ≠ 消失"（ADR-0040 同源纪律）冲突。
  删 key 时历史**保留**。
- **写入捕获落在唯一漏斗**：`store.upsert_fact()` 的 `ON CONFLICT` 之前调
  `_record_superseded_value()`（同一把 `_write_lock`、同一事务，避免"落了历史但当前值没更新"）。
  判据是**"值真的变了"**（`old != new`）——否则 `extract_facts_from_kv` 每次 set 都调它，
  同一份内容会被反复灌成日志垃圾。区间用**链式下界**（接上一条 `invalid_at`）+ 微秒上界
  （`now_us if now_us > valid_at else _plus_one_microsecond(valid_at)`），规避 Windows 约 15.6 ms
  时钟刻度导致同秒两次覆盖产生**零宽度区间**（`valid_at == invalid_at`）而永远查不到。
- **迁移回填**：`migrate_db` 的 `if version < 6` 分支调 `backfill_fact_revisions()`——按
  `messages.id`（**不是 `created_at`**，同秒多次写入时只有 id 顺序可靠）升序，每个 key 除最新
  一条（它即 `facts` 的当前值）外灌入历史，`invalid_at` 取下一条 `created_at`，`migrated=1`；
  **幂等**（表里已有 `migrated=1` 的行即跳过重跑）。
- **`messages` 改存"每 key 一条当前快照"**（`manager.set()`）：不再每写一次追加一行全文副本，
  改为**原地更新**那条快照行（`update_message_content` + `semantic.reindex_message`——因 BM25 的
  `tf` 是累加、向量按 `(message_id, text_hash)` 去重，只建不删会退化成新旧内容的并集）；
  另追一条 `source='event'` 的事件行（`{event:"kv_updated", key, chars, digest}`，digest 为
  sha256 前 12 位）供时间线阅读，**不建索引、不进 recent**（它是"某时改过"的痕迹，正文含 key 名，
  建索引会让搜 key 命中它）。
  ⚠️ 为什么保留一条快照而非彻底不存全文：KV 内容的召回目前由 messages 的 BM25+向量承担，而
  `facts` 那条替代路径 `_search_facts` 是 `LIKE '%query%'` **子串匹配、不分词**（"止损阈值"
  不会被切成"止损"+"阈值"），直接砍掉会造成召回退化；"每 key 恰好一条、且只索引这一条"之后，
  召回与改动前逐字等价，被取代的版本则天然进不了索引。
- **索引清理**：`recorder._cleanup_superseded_index()`（挂维护循环，与 `_cleanup_old_vectors`
  同批）按 `fact_revisions.source_message_id` 删这些消息的 `bm25_inverted` / `vector_index` 行
  并置 `compressed = 1`，**只清索引不删行**（`metrics._superseded` 正是靠这些行算
  `superseded_message_versions`，本项判据是 `bm25_term_rows_on_superseded == 0`、不是行数归零）。
  运行期新写入的历史行 `source_message_id` 为 NULL，故本方法天然只作用于存量、越跑越空。
- **读侧 `as_of`**：`GET /memory/{key}?as_of=YYYY-MM-DD`（`_normalize_as_of` 补成当日 23:59:59）
  走 `store.resolve_fact_as_of()`，返回该时刻**为真**的版本（附 `as_of` / `revision_id` 元数据）；
  未命中返回 404 并说明"早于本 key 的全部历史"。**默认路径（不带 `as_of`）完全不变**，零回归面。
- **顺手修信封污染**：`memory_set(merge=true)` 原先是先 `mgr.get(key)`（它把 `facts.value` 平铺后
  **挂上 `source/updated_at/access_count/confidence/…` 十个信封键**）再深合并写回——这正是实测
  66/151 条 fact"元数据信封被抄进 value"的**生产源头**。现改为直接解析 `store.get_fact_meta(key)`
  的**原文**再合并，天然无信封；`_deep_merge` 本身逻辑正确，未动。

## 记忆生成（memory_generation）

**用户与 agent 的对话不存在后端**，记忆生成是**用户触发、agent 执行**的工作流。维护器只做老化+验证。

### 触发方式

用户说"生成记忆"、"记忆生成"、"保存经验"等关键词时，agent 调用 `agent_guide(task_type='recurring.memory_generation')`，返回 `memory_generation_workflow`（5步）+ `do_not_store` 清单，agent 按步骤执行。

### 结构化记忆格式

```json
{
  "fact_type": "user|feedback|project|reference|experience",
  "name": "简短名称",
  "description": "一句话描述",
  "content": "实际内容",
  "why": "为什么重要",
  "how_to_apply": "如何在未来应用",
  "consumption_contexts": ["recurring.accounting", "adhoc.web_archive"],
  "trigger_keywords": ["退款", "账单对冲", "curl"],
  "created_at": "YYYY-MM-DD",
  "source_task": "触发本次记忆生成的任务名"
}
```

> **v6.1 closed taxonomy**：`fact_type` 收敛为 5 类 closed（user/feedback/project/reference/experience）。
> 旧值 `transaction` 已归档删除（业务数据走 todos/wip）。
> 君子协议：未知类型不强制拒绝，Pydantic validator 落 NULL + log warning。
>
> **两处与上面这句不完全相符的实测事实（2026-10-03 直读活库，别照注释信）**：
> ① `preference → user` 的迁移**当年没跑全**，库里还留着 3 条 `fact_type='preference'`
> （`preferences` / `preferences_gacha_login_handling` / `stock_advisor_workflow_feedback`），
> 已于 10-03 23:48 按同一映射补齐（快照 `memory.db.bak_before_fact_type_fix_20261003_234835`，
> 反向句柄 `temp/audit/fact_type_undo.json`）；执行迁移的 `tools/migrate_fact_type_v6_1.py`
> **现已不在仓库**，所以这条映射不会再被自动重放——要复现就照上面两个文件的路子做。
> ② 另有 5 条 `fact_type='architecture_decision'`，10-03 那轮**有意留着没动**（当时的理由是：
> 5 类里没有"决策记录"这一类，硬派成 `project`（7 天阈值）或 `reference`（30 天）都会让一条
> ADR 立刻挂上"可能已过时"，而"架构决策会不会过时"是架构决定，不由洗数据的会话代拍）。
> **10-04 已按该路径收口**：先判断这三条决策值不值得进 ADR（都满足 hard-to-reverse / surprising /
> real-trade-off），决策本体升格为 ADR-0041/0042/0043，记忆侧只留"指针 + 操作细节"——
> 性质就是 `reference`，归类不再勉强，30 天的阈值也不再错位（配 `decision_source` 指向 ADR，
> 读到"可能已过时"时该核的是 ADR 而非这条记忆）。
> 剩下越界的只有 `fact_type=NULL` **25** 条（`taxonomy_violations` 现=25）。
> NULL 那 25 条是**写入方没传 `fact_type`**（validator 只在收到非法值时才落 NULL 并 warning），
> 属消费侧问题：没有 fact_type 就进不了按类型的 staleness 判据与验证器分型判据；
> 写入端已改为回显提醒（见上节），存量 25 条未动。

### 记忆消费闭环（核心约束）

写入记忆时必须填 `consumption_contexts` + `trigger_keywords`，确保未来任务能消费：

- `consumption_contexts`：哪些 task_type 应读取此记忆
  - 通用偏好用 `["*"]`
  - scope 通配用 `["recurring.*"]`
  - 具体任务用 `["recurring.accounting", "adhoc.web_archive"]`
- `trigger_keywords`：任务描述/用户消息出现这些词时应读取

下次调 `agent_guide(task_type='recurring.accounting')` 时，后端通过 `find_consumable_memories()` 反向查询所有 `consumption_contexts` 匹配的记忆，并在 `first_action` 前注入"【必读记忆】"指令——agent 不需要记得读哪些记忆，guide 会主动推送。

无法明确消费场景的候选 → 跳过不写入（存了没人用=垃圾）。

### 写入端缺 `fact_type` 的回显（可见，不拦阻）

`POST /memory/{key}` 写完会按**落库后的行**读一次：若 `fact_type` 仍为空，响应里多带一个
`guidance` 字段，写明"这条按类型过滤不到它（`find_consumable_memories` 的 SQL、
staleness 阈值表、验证器分型判据都不会命中）"。**写入照常成功**——这是提醒，不是拒绝。

- 为什么按行读而不是按请求参数判断：`merge=true` 的更新往往不重复传 `fact_type`，
  旧行本来有类型；按参数猜会把正常的 merge 更新误报成"缺类型"。
- 为什么不加拦：`fact_type` 为 NULL 的成因是**调用方压根没传**
  （`_validate_fact_type` 只在收到非法值时才落 NULL + warning），属写入方习惯问题；
  写入时硬拒是被否过的方向（ADR-0040）。回归测试
  `tests/memory/test_memory.py::TestMemorySetFactTypeGuidance` 里专设一条
  `test_guidance_is_not_a_rejection` 钉住"提醒 ≠ 拦截"。
- **存量收口（2026-10-05）**：只标两类——① key 前缀本身就是类型命名（`reference_*` /
  `preference_*`+`preferences_*` / `project_*` / `feedback_*`，这是本库一直在用的约定）；
  ② value 里自己声明过 `type`/`fact_type` 且与前缀不冲突（如 `v6_lite_safety_fixes_summary`
  自证 `project`）。标类型走 `POST /memory/{key}` `merge=true` + 只传顶层 `fact_type`，
  脚本断言"value 正文逐字未变"。剩 4 条**前缀与自证冲突或压根没有类型线索**的
  （`feedback_commit_before_public_release`、`gacha_games_status`、`task_room_workflow`、
  `web_archive_xiaolin`）留在 NULL 等用户点单，不按目录名猜内容层。
- **这 4 条已于 2026-10-06 逐条点单落定**（`feedback` / `project` / `reference` / `project`），
  NULL 存量归零。两条附带处置：`web_archive_xiaolin` 同时剥掉 `progress_log`/`final_status`/
  `automation_20260916` 三个进度字段（进度真源是 `workspace/web_archive/reports/index_xiaolin.json`，
  写进记忆必然过期）；`reference_e2e_test_window_coord_uia_realclient` 的 value 顶层是
  `{key, value, merge}`（当年某次调用把**请求体本身**当成 data 写进去了），拆包后内层正文逐字不变，
  并把内层的 `consumption_contexts`/`trigger_keywords` 提升到 facts 列——此前这两列一直是 NULL，
  等于该条记忆从没能被 `find_consumable_memories` 的 SQL 过滤命中。
  实际数量现查：`SELECT COALESCE(fact_type,'<NULL>'), COUNT(*) FROM facts GROUP BY 1`。

### fact_type 分类（v6.1 closed taxonomy）

| 类型 | 含义 | staleness 阈值 | 示例 |
|------|------|----------------|------|
| user | 用户偏好/身份 | 90 天 | 用户喜欢用 curl.exe 而非 PowerShell curl |
| feedback | 用户反馈 | 30 天 | "你这报告太啰嗦了，下次精简点" |
| project | 项目状态/进度/约定 | 7 天 | 异环抽卡清洗脚本 v3 新增道具映射 |
| reference | 参考资料/链接/命令 | 30 天 | ModelScope VL API 限流较严，需低频串行调用 |
| experience | debug 解决方案/坑点/最佳实践 | 14 天 | sqlite WAL 模式下并发写需开 shared_cache |

> **staleness 警告**：`memory_get` / `memory_search` / `agent_guide` 返回的记忆会自动追加 `memory_age`（相对时间 today/yesterday/N days ago）+ `staleness_warning`（超阈值才填）+ `trust_recall_hint`（常驻"引用前请验证"）。
> 模型读到 `staleness_warning` 时应优先用 `file_read` / `file_grep` 验证文件/函数仍存在再引用（TRUSTING_RECALL 机制）。
> `fact_type=NULL`（旧数据或未知类型）不算 staleness（君子协议，不强制拒绝）。

### do_not_store 清单（WHAT_NOT_TO_SAVE）

以下内容不存储为记忆（代码本身可推导 / 可 grep / 已过时）：

- **代码模式 / 架构 / 文件路径 / git history**（代码本身可推导，存了反而过时）
- **AGENTS.md / project_rules.md 已有内容**（重复存储）
- **ephemeral task details**（临时任务细节，会话内解决）
- 代码结构（可 grep 代码库获取）
- git 历史（可 `git log` 获取）
- 已修复的 bug（修复后不再需要）
- 临时调试信息（如某次 OCR 返回的坐标）
- config.toml 中已配置的值

**可以写入的**：debug 解决方案 / 坑点 / 最佳实践（experience 类）、用户偏好 / 身份（user 类）、用户反馈（feedback 类）。
即使看似相关也不要保存上述禁清单内容（君子协议，后端不强制拒绝，但 prompt 强约束）。

### memory_generation vs maintainer

| 维度 | memory_generation（用户触发） | maintainer（后端自动） |
|------|-------------------------------|----------------------|
| 触发 | 用户说"生成记忆" | 每 6 小时自动 / 手动 `POST /memory/maintain` |
| 职责 | 提取+写入新记忆 | 老化+验证已有记忆 |
| LLM | agent 自己 | cheap 模型做选择题 |
| 输出 | 新记忆写入 facts 表 | memory_meta 表标记 stale + 验证结果 |

> 完整工作流见 `.agents/skills/memory_generation.md` 和 `agent_guide(task_type='recurring.memory_generation')`。

## 待办模块（todos）

独立模块 `server/todos/`，与记忆系统共享同一 SQLite 数据库但使用独立表。从旧记忆 key（`task_reminders`、`wip_index`）自动迁移。

### 表结构

**todos 表**（周期任务）：

| 字段 | 说明 |
|------|------|
| id / title / skill | 标识 + 关联 skill |
| type | recurring / oneshot |
| frequency | daily / weekly / monthly |
| last_done_at / next_due_at | 完成时间 + 下次到期（mark_done 时自动计算） |
| condition | 生效条件（如"找工作期间"） |

**wip_tasks 表**（未完成工作）：

| 字段 | 说明 |
|------|------|
| id / title | 标识 |
| status | active / paused / blocked / completed |
| priority | high / medium / low |
| goal / progress | 目标 + 进度百分比 |
| next_steps | 下一步操作（JSON 数组） |
| current_state | 状态快照（JSON 对象） |

### API

| 路径 | 方法 | operation_id | MCP 直连 |
|------|------|-------------|---------|
| `/todos` | GET | todos_list | ✓ |
| `/todos` | POST | todos_create | 网关 |
| `/todos/due` | GET | todos_due | ✓ |
| `/todos/{id}` | GET/PUT/DELETE | todos_get/update/delete | 网关 |
| `/todos/{id}/done` | POST | todos_mark_done | 网关 |
| `/wip` | GET/POST | wip_list/wip_create | ✓/网关 |
| `/wip/{id}` | GET/PUT/DELETE | wip_get/update/delete | 网关 |

> **wip_list summary 模式**：`GET /wip?summary=true` 只返回 `id/title/status/priority/progress/updated_at` 轻量字段，跳过 `goal/next_steps/current_state/related_*/extra_data` 等大字段和 JSON 反序列化。适用于 agent 会话启动或用户问"上次没做完什么"时快速浏览，避免长输出污染上下文。需要详情再用 `wip_get(id)` 单独取。

### 迁移

启动时自动执行（幂等，检查 `schema_info['todos_migrated']`）：
- `task_reminders` → `todos` 表
- `wip_index` → `wip_tasks` 表（基本字段）
- `.agents/wip/*.json` → `wip_tasks` 表（完整字段，更新已有记录）

> 旧 KV 记忆目录和 `.agents/wip/` 的 .json 文件已清理（备份在 `temp/memory_cleanup_backup_20260716/`，`.agents/wip/*.json` 迁移到 `temp/wip_migrated_backup/`）。所有数据以 SQLite 为唯一真源。
