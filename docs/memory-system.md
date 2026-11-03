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
- **工具日志默认排除**：`POST /memory/search` 不传 `source` 时排除 `source='tool'`（MCP 中间件自动记录的调用日志，占 `messages` 的绝大多数）；要定向查工具日志得显式传 `source="tool"`，传 `"manual"` / `"event"` 时不做排除。排除发生在 `top_k` 截断**之前**（`bm25.search` 在 SQL 层带 `exclude_sources`、`_vector_search` 在候选层过滤）——若放在截断之后，工具日志会先占满名额再把结果滤空
- **降级模式**：嵌入模型不可用时自动降级为 BM25-only

> **检索通道评测**：`tests/memory_eval/search_gold.jsonl`（40 条 golden query，gold = fact key）+ `tools/audit/search_eval.py`。只判 messages 的 BM25 与 facts 的词项覆盖率，不判 summaries 与端到端融合排序；方法与基线见 `workspace/local_butler/20_决策过程/20_记忆第二轮/10_检索通道golden_query基线.md`。
>
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
| `/memory/maintain` | POST | 手动触发记忆维护（`force=true` 忽略时间检查；2026-10-31 起不再接受 `force_revalidate`） |
| `/memory/maintain/status` | GET | 记忆维护器状态（上次运行 / stale 数 / 已归档数 / `validator: "removed_2026-10-31"`） |
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
| `validation_flags` | 列名为历史命名；2026-10-31 起内容只剩老化告警（`staleness_warning`），不再含 LLM 验证判决 |
| `injected_text_digest` / `injected_chars` | 实际注入文本的 sha256 与长度，用于比对"留痕说注入的"与"真注入的"是否同一份 |
| `trace_id` | 有检索 trace 时回填；`agent_guide` 的 must_read 走 facts 定向召回、不产生 trace，此处为空 |

当前写入方是 `agent_guide` 的【必读记忆】块（`server/agent_guide.py`），一个 surface 常量 `SURFACE_AGENT_GUIDE_MUST_READ = "agent_guide.must_read"`。GUI 侧在记忆面板的"检索过程"页切到"上下文投影"来源即可回放（`client/panels/memory/search_traces_page.py`）。

消费时点的两个配套行为：
- **有效性存疑不拦截、但降位 + 显式警告**：带 staleness 警告（老化评分超阈值）的记忆排在必读块最后，并附一条"引用前必须先验证"的可见警告（不做静默丢弃）。LLM 验证判决那一路已于 2026-10-31 随验证器一起删除。
- **摘要类证据不冒充有分**：`Evidence.scored=False` 表示来源本身不产生相似度（如摘要窗口命中），排序时一律压在有分证据之后（`server/memory/evidence.py`）。

保留期由 `[memory] projection_retention_days`（默认 30）控制，维护器每轮调 `projection.cleanup_old()`，删除条数以 `projection_cleanup_deleted` 出现在维护结果里。

## 记忆维护器（maintainer）

后端内置 `MemoryMaintainer`，由 `[loops.memory]` 定时循环自动运行（间隔见该配置段，默认每 6 小时），另有记忆 API 调用时的惰性触发兜底。职责：

### aging（老化）

- 查询 `facts` 表中 `updated_at` 早于 `now - [memory.maintain].stale_days` 的记忆
- 每轮都更新 `memory_meta.stale_score`（距上次更新的天数，staleness 警告要用它）
- **只算分，不改标签**（2026-10-31 起）：旧实现每轮把 stale 记忆的 `validation_status`
  重置回 `pending` 等 LLM 重判；验证器删除后这段翻转逻辑一并去掉，`run_aging` 现在只写 `stale_score`
- `memory_meta` 的四个验证列（`validation_status` / `last_validated_at` / `validation_note` /
  `validate_attempts`）**保留但冻结**（SQLite 删列代价大）：值是 v15 最后一批 LLM 判决，此后不再变化，
  面板按「历史字段」显示，不参与任何排序或拦截

### ~~validate（LLM 验证）~~ 已于 2026-10-31 删除

原设计每 6 小时取 ≤25 条 `pending` 记忆让 cheap 模型判 keep/update/archive。删除依据是实测：
162 次判定里 **157 keep / 4 update / 1 archive**，而唯一可执行的 archive **从不执行**——
产出只有日志，成本却是每天最多 20 万输入 token。

形态类问题（超长 / 超版 / 频繁改 / 坏 scope / 死链 / 孤儿）本来就是确定性查询，改由中枢 L7 巡检
（九条 `G-*` 判据，见 `workspace/local_butler/30_架构与设计/20_现行设计/30_中枢模块_职责与执行链路_20261030.md` §1.6
与 `28_数据与记忆_面板视图与召回整改_20261030.md` §2.6）和面板打开即算；内容真伪没有程序判得了，交给人看。

> 历史沿革（代码已全删，别再照它改）：该验证器曾打过三轮补丁——① 原 `value[:800]` 常被元数据信封
> 占满，改为剥信封 + 窗口放大到 2400 字符；② 超窗不采纳 archive，连续 3 次改判 update；
> ③ 分 fact_type 判据、去项目中心偏见、拒绝「知识截止」类无效依据。2026-10-04 还做过一次存量重判
> （旧 archive 72 → keep 69 / update 2 / 仍 archive 1）。细节见 `CHANGELOG.md` 与 git 历史。
>

### 触发机制

- **定时循环**：`[loops.memory]` 注册为 Loop 定时任务（APScheduler `IntervalTrigger`），按 `maintain_interval` 周期直接调 `maintainer.run()`
- **惰性兜底**：每次记忆 API 调用时检查 `schema_info['maintainer_last_run']`，距上次超过间隔则在后台 `_async_maintenance` 中触发
- **手动**：`POST /memory/maintain`（`force=true` 忽略时间检查）

`maintainer.run()` 的固定顺序：垃圾预过滤 → 老化评分 → **指标日聚合** → 三张明细表的
retention 清理。聚合必须排在清理之前，否则每天先删后算、指标永远是空表。全程 **零 LLM 调用**。

### 配置

```toml
[memory.maintain]
interval_hours = 6              # 运行间隔（→ maintain_interval_hours）
stale_days = 7                  # 过期阈值（→ maintain_stale_days）
```

> 2026-10-31 随验证器删除四个键：`enable_validate` / `preview_chars` / `batch_limit` / `revalidate_days`。
> 旧配置文件里留着这四个键不会报错，但**不再被解析**（`server/memory/config.py` 已无对应字段），
> 别以为写成 `enable_validate = true` 能把验证器开回来。`model_tier` 字段更早（v15）就已移除，
> `USE_CASE_REGISTRY.memory_validate` 也一并从 18 个 use_case 里删掉了。

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

### 静态钉 key 与必读块判卷（2026-10-21）

`_MUST_READ_RANK`（`server/agent_guide.py`）决定必读块内的排序：`matched_by=None`（skill 条目静态声明的 `memory_key` / `related_memory_keys`）优先于 `context+keyword` → `keyword` → `context`。**要给某条记忆保底上屏，就在 `GUIDE_REGISTRY` 条目（或 workspace 的 `loop_actions.py` / `guide_entries.py`）里把它写进 `related_memory_keys`**——静态项排在动态命中之前，超限截断也先砍动态项。

⛔ 不要反过来把 `context` 档整体提前：人工盲评显示，精确声明 `consumption_contexts` 的条目里同样有大量与任务无关的项，整体提权会把噪声一起拉上来。噪声侧的正确修法是**收窄 `consumption_contexts`**（去掉 `"*"` 或过宽的 `<scope>.*`），不是调排序。

判卷集与评测器：人工盲评 gold 落 `tests/memory_eval/gold.jsonl`（追加写，同一 `(task_type, key)` 最后一行为准，撤销写 `unset`），`tools/audit/recall_eval.py` 据此算 A 通道（task_type → 必读块）的分档上屏率、逐 task_type 的漏召与噪声清单。⚠️ gold 由**定向抽样**产生（高特异但从未注入 / 高注入但通配低相关），**率不能外推成全库召回率**。方法与本轮结论见 `workspace/local_butler/20_决策过程/20_记忆第二轮/09_焦点20判卷与召回修复.md`。


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
> 属消费侧问题：没有 fact_type 就进不了按类型的 staleness 判据；
> 写入端已改为回显提醒（见上节），存量 25 条未动。（此处数字是当时的实测，现查用 SQL。）

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
staleness 阈值表、中枢 L7 的形态巡检都不会命中）"。**写入照常成功**——这是提醒，不是拒绝。

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

> **准入规矩真源（2026-10-14 起）**：`workspace/local_butler/40_盘点与证据/10_上下文供给/02_记忆准入规矩_什么该进记忆.md`
> 本节只回答「**不写什么**」；「**写哪里**（L0 事件流 / L1 检索层 / L2 常驻层）+ 超配额怎么置换降级」在那份文件（§1 三层落点、§2 准入四问、§4 L2 准入、§5 配额与置换）。起因是两份注入层文件长期顶格（全局 3999/4000、项目 7998/8000），而 facts 检索层 161 条本就装得下——问题不在"记太多"，在"什么都往每会话注入的文件里塞"。
> ⚠ **一处待收紧**：上面第 1 条把「文件路径」整类列为禁存，括注理由却是「代码本身可推导」——禁的其实是**仓库内**路径。**本机物理布局**（免安装软件区、模拟器安装位等）不进任何仓库、grep 不到、不因重构失效，属误伤，应落 `.agents/memory/machine/路径台账.md`。
> ⚠ **一处分类缺口**：本机环境事实在 v6.1 五类 closed taxonomy 里没有对应类，硬派 `reference`（30 天）会让它挂上"可能已过时"，而物理布局不会过时；已提议新增 `fact_type=environment`（见那份文件 §8 待定第 4 条）。

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
## 记忆写入漏斗（v8 / DL3，2026-10-27）

设计真源：`workspace/local_butler/30_架构与设计/30_施工与证据/17_DL1-DL5实施记录_20261027.md` §2

**三个轴**（`facts` 表新列，全部可空）

| 轴 | 取值 | 用途 |
|---|---|---|
| `scope` | `global` / `project:<id>` / `topic:<id>` | 全局规范 vs 分项目 |
| `kind` | `norm` / `fact` / `pitfall` / `preference` / `procedure` / `reference` / `current_state` | 记忆种类 |
| `origin` | `user_quote` / `agent_extracted` / `system` / `tool` | 谁说的（**与 `source` 的 auto/manual 不是一回事**） |

配套列：`title` / `summary`（注入只看 summary）/ `evidence_refs` / `source_quote_ids` / `must_read` / `expires_at` / `dedup_hash`。
`must_read=1` 只允许 `kind='norm'`；`kind='current_state'` 必须带 `expires_at`（否则会膨胀成永久 fact）。

**两张新表**

- `source_quotes`：用户原话，**原文永不 UPDATE**；`quote_hash` 唯一，同一句话只存一份；`speaker='agent_relay'` 表示「agent 转述」。
- `memory_candidates`：每条候选的去向账（`accepted`/`merged`/`duplicate`/`rejected` + 原因码 + 原 payload）。**`rejected` 行就是面板的待审候选**。

**唯一的收尾入口**：`POST /memory/wrapup`（服务端实现 `server/memory/wrapup.py::submit_wrapup`）

```
payload = {run_id, project_id, quotes:[...], candidates:[...]}
  → 先落 quotes（去重靠 quote_hash）
  → 逐条候选：必填校验 → dedup 查重 → 同 key 覆盖(merged) 或新建(accepted)
  → 全部走 upsert_fact（唯一漏斗，修订史与写锁自动继承）
  → 返回逐条去向
```

**幂等**：`dedup_hash = sha256(scope|kind|title 或 key|content)`，归一化只做「空白折叠 + 小写」。同一份 payload 连交两次，第二次全部 `duplicate`，`facts` 与 `source_quotes` 行数都不变。

**拒收码**：`empty_content` / `over_limit`（单次 >10）/ `unknown_kind` / `must_read_not_norm` / `missing_expires_at` / `bad_scope` / `sensitivity_invalid`。
被拒的候选同样进 `memory_candidates`（同内容 + 同原因只留一条，重试不刷屏）。

**取上下文**：`MemoryStore.list_facts_for_context(scope=, kinds=, must_read_only=)`，
排序 `must_read` → `kind='norm'` → `updated_at`。**老数据 `scope` 为 NULL，按 scope 查不到**——没人判过归属就不进上下文。
