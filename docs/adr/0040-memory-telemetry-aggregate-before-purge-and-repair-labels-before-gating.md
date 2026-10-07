# 记忆库判据整改：遥测先聚合后删、判据不可信时禁止加拦截、观测窗口不完整就不给死刑

**Status**: accepted（2026-10-03）
**主题域**: 证据与检索 / 记忆系统

## Context

对 `data/memory/memory.db` 的自查（152 条 fact / 4032 行 message）暴露出三个互相咬合的判据缺陷，
而不是容量问题：

1. **遥测先删后算**。`search_traces` / `evidence_ledger` / `context_projections` 三张明细表按
   retention 30 天硬删，删除前**没有任何聚合**，也没有任何代码汇总过它们。实测累计写入 1094 条
   trace、10,165 行 evidence，而两张表当时都是 **0 行**——于是"记忆库召回质量随时间怎么变"
   在代码里根本问不出来。`db_size_history` 同样只在内存 dict 里滚动（`store.py:39`），
   重启即归零，导致健康分里"DB 增长率"那一维（需要 ≥2 个采样才参与）**从来没被算过**。
2. **72% 的 fact 被维护器判了 archive，而这个标签本身是测量假象**。旧验证器只喂 `value[:800]`；
   实测 120/151 条超 800 字符，**81 条的前 800 字符正是被抄进正文的元数据信封**
   （66/151 条 fact 的 value 里带 `source/updated_at/access_count/confidence`）——模型看到信封不是信，
   于是写下"内容被截断／乱码／无实际语义"。被判死的包括 `ui_design_system`（全项目 GUI 真源约定）、
   `approval_strictness_levels`（审批架构决策）、以及同期上屏 283 次的
   `reference_opencode_zen_free_tier`。另有一处旧 prompt 明文要求把
   "与当前项目无关的个人数据（如游戏抽卡记录）"判 archive——项目中心偏见，
   而个人管家恰恰最依赖生活域记忆。
3. **标签既不摘除召回、又不可重验**。`archive` 只让条目排在必读块最后并附警告
   （`find_consumable_memories` / `_search_facts` 都不 join `memory_meta`），
   而旧 `run_aging` 的 CASE 只翻转 `keep`/`update` ⇒ **archive 是永久标签**，一次误判永远留在账上。

三者合起来的效果是"账面 72% 已死、实际照常上屏、且没有任何一处能证明它有用过"。

## Decision

三条互相约束的原则，按顺序执行：

1. **聚合先于删除**：`MemoryMaintainer.run()` 的顺序固定为
   垃圾预过滤 → 老化 → 验证 → **指标日聚合** → 三张明细表的 retention 清理。
   窄表 [`memory_metrics_daily(date, metric, value, extra)`](file:///<project_root>/server/memory/metrics.py)
   作为被删明细的**替代物**，本身不参与 retention；新增指标不必再迁移 schema。
   单指标计算失败汇总成 `_errors` 指标 + log warning，体检脚本判红——
   **"算不出来"必须能与"值是 0"区分**。
2. **判据不可信时禁止加拦截，先修产生判据的那一环**（用户裁决）。本轮**不改** `archive` 的语义
   （照旧不摘除召回），只让标签可信：`build_validation_preview` 剥元数据、窗口放大到
   `validate_preview_chars`、判据按 `fact_type` 分五档、删除个人域偏见、
   并把 `archive` 纳入可重验范围（老化按 `validate_revalidate_days` 时间门控，
   同时止住"keep/update 每 6 小时被无限重验"的配额浪费）。
   理由：在一个已知 72% 假阳的标签上挂否决权，等于让坏判据决定什么进不了上下文。
3. **观测窗口不完整 ⇒ 只能延判，不能否决**。正文仍超窗时**不采纳** archive，
   留在 `pending` 并计 `memory_meta.validate_attempts`；连续 3 次仍超窗改判 `update`
   （"臃肿到无法判读"是重写信号，不是过时信号）。
   同日重判存量时又抓到同族第二条：**判决依据本身不成立也不能否决**——cheap 模型把晚于自己
   训练截止（2026-07）的时间戳读成"未来未发生 ⇒ 内容无效"，这条偏见的杀伤面专一地落在
   **最新写入的记忆**上。现由 `is_invalid_archive_reason` 识别该类理由，同样走延判
   （`deferred_invalid_reason`），连续 3 次只给无效依据则落 `keep`（没有成立的失效证据就等于保留）。
   实测一轮重判后 `keep` 15→52、archive 占比 72%→56%、`archive_high_freq_conflicts` 2→1。

同族原则：`"context_projections"` 的投影留痕只度量、不判卷，
行为类指标（消费漏斗、归档率）**只报数不设阈值**——没有基线就不设线，
攒够两周实测分布再定；只有结构上无争议的缺陷（superseded 旧全文仍被索引、
`fact_type` 越出 closed taxonomy、`trace_id` 全空导致无法归因）才判红/黄。
这与 [ADR-0035](0035-evidence-gate-deterministic-lanes-no-model.md)（证据门确定性路不过模型）、
[ADR-0036](0036-unknown-outcome-first-class-tiered-recovery.md)（结果未知为一等终态）是同一条线：
**被评对象不得自己出题自己判卷**。

## Considered Options

- ❌ *archive 默认不注入必读块*（硬拦）：被拒——会把 109 条按坏判据直接移出日常召回。
- ❌ *只加可见（带理由上屏）*：被拒——假正常仍在，根因（喂料与偏见）没动。
- ❌ *两次独立判决一致才归档*：被拒——同 prompt、temperature 0.1，第二次只是重复第一次的错误，
  是**假独立性**；改成"观测完整性"这一条真判据。
- ❌ *延长明细表 retention*：被拒——明细体积随流量线性涨（evidence 每轮 ≤50 行×500 B，
  投影三天 465 行），而且没人会逐行读明细；要的是聚合后的时间序列。
- ❌ *宽表（一指标一列）*：被拒——指标清单本来会一直长，每加一个就要迁移 schema。
- ❌ *一次性迁移脚本清空 archive 标签*：被拒——清完就没有"曾被判死"的痕迹，也无法新旧判据对比；
  改成显式入口 `POST /memory/maintain {"force_revalidate": true}`，由用户点单。

## Consequences

- 正面：消费漏斗第一次可量化（实测窗口 2.7 天 / 467 次注入：上过屏 33、只被召回后裁掉 88、
  从未进候选 31）；`archive × 高频上屏` 成为可自动检测的矛盾信号（基线 2 条）；
  趋势从"重启就归零"变成落库可比。
- **已发生的数据变化**：验证链路时对活库跑过一次 `force=true`，按新门控把 35 条
  `last_validated_at` 超 30 天的 archive 重置为 pending（archive 109→74，pending 1→36）。
  这些条目在重判前**不再带**"有效性存疑"警告——方向是撤销一个坏判决，不是丢数据。
- 代价：验证配额（每轮 `validate_batch_limit` 条，重判存量要跑数轮）；
  指标聚合多一次全表扫描（facts/messages 规模内可忽略）；
  行为类阈值暂时"只报数不判色"，看起来不够自动化——这是刻意的，避免自判卷。
- 下游必须一起做（**同日已由 `d83d756` 完成**，原样保留下面这段以说明当时的判据）：
  `facts` 双时态化（`fact_revisions` 的 `valid_at/invalid_at`，失效而非删除）与索引只指 current——
  当时实测 182 份 superseded 旧全文仍有 68,480 行 BM25 词条可被召回；以及取消 `memory_set`
  的 merge 路径把元数据信封写进 value（原 `router.py:474`）。**现状复核（10-03 晚间直读活库）**：
  superseded 副本上的 BM25 词条与向量行均为 **0**；merge 改从 `store.get_fact_meta()` 取原文，
  新增污染已封住；**存量脏 value 已于 10-03 22:08 洗完**（67/155 条，连同 67 条
  `messages(source='manual')` 当前快照 + 逐行 `reindex_message`；索引里的信封词元
  1327 → 219，剩下的都是正文里真提到这些词的合法内容）。
  **回滚归档**：`data/memory/memory.db.bak_before_envelope_strip_220804_REPAIRED`
  （复验 facts=156 / 带信封=67 / messages=4105 / integrity ok）。
  ⚠️ 第一版归档是 `shutil.copy2` 复制主库文件得到的，**少 1 条 fact + 3 行 messages**：
  库在 WAL 模式下，未 checkpoint 的提交只活在 `-wal` 里，按文件复制必然漏。已按行补回，
  原件改名 `..._220804.INCOMPLETE-wal-uncheckpointed-do-not-restore`。
  ⇒ 给这个库做快照要用 `sqlite3.Connection.backup()`，或先 `PRAGMA wal_checkpoint(TRUNCATE)`
  再复制，并且**复验行数**——`integrity_check` 通过不代表内容完整。
  仍带信封的是 `fact_revisions` 历史副本 114/185 行——**故意不洗**：那记录的是"当时正文
  长什么样"，洗它就是篡改历史；这些行不进 BM25/向量、无 `memory_meta`，不会被召回或上屏。
- 洗数据时顺带撞出的旧问题（**同日 23:19 已处理**）：`vector_index` 曾有 **419/683 行（61%）指向
  已不存在的 `message_id`**（`created_at` 全集中在 2026-08-18 21:54–23:19 一次批写），
  BM25 侧孤儿 0。这些向量永远解析不到内容，语义召回只能靠 `min_vector_similarity` 和
  "honest empty" 丢弃（`semantic.py:204`）——等于向量通道六成名额白烧，也解释了体检里
  "向量比 BM25 少 6.4 倍"里有一部分是**假覆盖**（`vector_coverage` 把孤儿算进了分子）。
  **根因不是级联失效**：两张索引表都带 `ON DELETE CASCADE`，生产连接也确实开了
  `PRAGMA foreign_keys`（在快照副本上走 `store.delete_message` 实测：消息、向量、BM25 三处
  零残留）。但该 pragma **按连接生效且默认关闭**，所以 2026-08-18 那次**裸连接的批量清理**
  删得出 messages 却删不出级联；按天分组是决定性的——当晚写的 419 行全是孤儿，其余 7 天写的
  160 行零孤儿，属一次性遗留而非活跃泄漏。处理：先 `Connection.backup()` 快照并逐表复验，
  再按"源行不存在"单条件删（683 → 264，`index_orphan_rows` 归 0）；覆盖率诚实值从虚高 15%
  降到 **6.4%**，变差的是账面不是能力。

## References

- 体检方法与实测数字：`temp/sdd/memory-audit/00-audit-findings.md`（含复现命令）
- 决策与拒绝方案逐条：`temp/sdd/memory-audit/design-decisions.md`
- v2 设计（分层真源 / 检索合流 / 消费记账 / 命名空间 / 隐私分级 / 分期）：
  `temp/sdd/memory-audit/01-architecture-v2.md`
- 代码：[server/memory/metrics.py](file:///<project_root>/server/memory/metrics.py)、
  [server/memory/maintainer.py](file:///<project_root>/server/memory/maintainer.py)、
  [server/memory/schema.py](file:///<project_root>/server/memory/schema.py)（v5）、
  [server/agent_guide.py](file:///<project_root>/server/agent_guide.py)（`_select_must_read`）、
  [tools/audit/memory_health_report.py](file:///<project_root>/tools/audit/memory_health_report.py)
- 文档：`docs/memory-system.md`（「记忆指标日表」「验证器喂给模型什么」「必读记忆块的全场景通配限槽」）
- 测试：`tests/memory/test_memory_audit_v5.py`（28 例，断言中间量而非最终条数）
