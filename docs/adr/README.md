# Architecture Decision Records (ADR)

本目录记录 LocalAgent 项目的架构决策。每个 ADR 是一个不可变的历史文档，捕获"为什么做出了某个决定"——不是 spec，不是 CHANGELOG，而是未来读者会问"为什么这样设计"时可以查阅的真源。

## 索引

| 编号 | 标题 | 主题域 | 摘要 |
|------|------|--------|------|
| [0001](0001-auto-scoring-routing.md) | Auto-Scoring Routing | LLM 池 | 6 因子加权评分替代 round-robin 主选路（health/quota/latency/cost/tier/lkgp） |
| [0002](0002-llm-pool-module-reorganization.md) | llm_pool 模块重组 | LLM 池 | USE_CASE_REGISTRY 迁入 types.py 打破循环依赖，删除 key_manager.py compat shim |
| [0003](0003-vendor-skill-absorption-criteria.md) | 外部 skill 吸收判断标准 | Skill 体系 | 4 项标准（方法论普适性/未来场景/dev_toolkit 适配/融合成本）避免"前端不紧密就放弃" |
| [0004](0004-prefilter-llm-max-tokens.md) | 预检 LLM max_tokens 必须给足 | 内容审核 | 思考模型 `<think>` 吃光 300 token 导致预检失效，max_tokens=16384 与主调用对齐 |
| [0005](0005-approval-domain-consolidation.md) | 审批领域文件整合 | 审批/守卫 | approval_log → approval_review，command_guard_router → command_guard，5 文件减为 3 |
| [0006](0006-v6-lite-scope-cut.md) | v6-lite 砍范围开工 | 对话引擎 | v6 全量 13 文档降级为参考库，v6-lite.md 唯一开工依据，EventStore 砍到 4 表 |
| [0007](0007-omniparser-removal.md) | OmniParser 移除 | 视觉/OCR | 2026-07-31 移除 OmniParser，文字定位主路径改 PaddleOCR bbox + 远程 VL 兜底 |
| [0008](0008-memory-sqlite-gentlemans-agreement.md) | 记忆系统沿用 SQLite + 君子协议 | 记忆系统 | 不重写为 markdown 文件系统，fact_type closed taxonomy 但写入端君子协议 |
| [0009](0009-toolresult-five-variant-enum.md) | ToolResult 5 变体 enum | 对话引擎 | 从二态 is_error 改为 SUCCESS/ERROR/USER_DENIED/APPROVAL_REQUIRED/TIMEOUT |
| [0010](0010-tool-list-four-categories.md) | 工具清单分四类呈现 | 对话引擎 | 内置基本工具 + 核心 MCP 直暴露 + MCP 子桶 + REST 物化，避免 116 工具平铺 |
| [0011](0011-computer-use-session-manager.md) | computer use 会话管理层独立 | 屏幕操控 | SessionManager 替代 ControlGrantManager，GUIClient 拆为 OverlayClient |
| [0012](0012-session-manager-in-memory-only.md) | 会话管理层纯内存不持久化 | 屏幕操控 | 后端重启重置为无权限，避免"幽灵授权"风险 |
| [0013](0013-release-engine-compiler-architecture.md) | release engine 编译器式架构 | 发布工具 | prepare_release() → build_release() 双接口，审计与构建消费同一 PreparedRelease |
| [0014](0014-digest-bound-approval.md) | digest 绑定 > 人工审批 | 发布工具 | SHA-256 digest 绑定审批，digest 不匹配 fail closed 拒绝构建 |
| [0015](0015-watchdog-shutdown-via-auto-shutdown.md) | watchdog 关机预授权走 auto_shutdown | 屏幕操控 | agent 不持有 shutdown 权限，走 auto_shutdown 模块 + SessionManager 双重校验 |
| [0016](0016-chat-engine-bookkeeping-only.md) | chat 引擎只记账不控制 | 对话引擎 | 删 cost 字段，wall_clock_budget_secs 是唯一预算控制 |
| [0017](0017-secret-unified-gateway.md) | lib/secret 密钥读取统一中转层 | 密钥管理 | 路径真源 + 非 LLM 密钥集中到 secrets.toml，保留 key_store.py 委托路径获取 |
| [0018](0018-route-tags-fail-open-secondary-defense.md) | route_tags 保持 fail-open + 二级防护 | 审批/守卫 | 默认暴露给 agent，靠 classify_safety/approval_level/中间件三层防护，不靠 x-agent-callable 标志 |
| [0019](0019-pydantic-extra-forbid-policy.md) | Pydantic extra='forbid' 全项目策略 | 数据校验 | BaseModel 继承 BaseSchema，传错字段名立即报错避免静默吞导致误报成功 |
| [0020](0020-async-http-httpx-asyncclient.md) | async HTTP 客户端选型 httpx.AsyncClient | HTTP 客户端 | 后端 httpx.AsyncClient 单例 + Qt QThread + httpx.Client，弃 requests/aiohttp |
| [0021](0021-eventstore-connection-pool-wal.md) | EventStore 连接池 + SQLite WAL 模式 | 对话引擎 | 连接池 + WAL 替代单连接 Lock，并发读 + 串行写，为多 agent 并发预留 |
| [0022](0022-memory-db-single-connection-global-lock.md) | memory.db 单连接 + 全局写锁 | 记忆系统 | 6 模块共享单连接 + MemoryStore._write_lock 全局写锁串行化所有写操作 |
| [0023](0023-advanced-tool-dangerous-tools-permission.md) | 网关端点级权限检查清单 DANGEROUS_TOOLS | 审批/守卫 | DANGEROUS_TOOLS 集合，confirm 级强制走 command_guard 审批，其他放行 |
| [0024](0024-multi-process-stats-atomic-write.md) | 多进程并发写 stats 原子写 + last-writer-wins | 统计/并发 | tmp + os.replace 原子写，last-writer-wins 不留半截文件 |
| [0025](0025-uia-semantic-action-focus-exemption.md) | UIA 语义动作豁免焦点校验 | 屏幕操控 | UIA COM 隔离窗口焦点，invoke pattern 不需要 SetForegroundWindow |
| [0026](0026-watchdog-skip-danger-confirm.md) | watchdog 模式跳过 danger=confirm | 屏幕操控 | watchdog 授权即同意 confirm 范围，跳过弹窗保持自动执行体验 |
| [0027](0027-client-server-dependency-boundary.md) | Client-Server 模块依赖边界 | 分层架构 | 提取 lib/config_reader + lib/component_manifest，物理隔离 + 单一真源 |
| [0028](0028-agent-guide-semantic-embedding.md) | Agent Guide 语义向量化匹配 | Agent Guide | 复用 EmbeddingEngine 对 GUIDE_REGISTRY 摘要嵌入，弱匹配时余弦加分，双条件 strong_match |
| [0029](0029-workspace-backup-manifest-decoupled.md) | workspace 数据备份方案 | 备份/恢复 | 新增 [backup] 段独立于 [watch] 段，与 [secret_backup] 对称设计但独立配置目录和保留策略 |
| [0030](0030-model-lifecycle-manager-architecture-decisions.md) | Model Lifecycle Manager 架构决策（5 项） | 模型生命周期 | 薄编排 vs 所有权重构 / 进程内优先 / VRAM 主动 + RAM 预留 / 守护内部卸载绕过审批 / 热路径嵌入默认钉住 |
| [0031](0031-inbound-gateway-own-sqlite-stats.md) | 入站网关统计自持 | 入站网关 | 网关 SQLite 是调用统计唯一真源，不复用池统计；TTFT 网关层计时。⚠️ 2026-09-02 修订：流式 `upstream_key` 缺失限制已解除、池子树只读边界纯增量放宽（见文末 Amendment）。⚠️ 原决策语境「`pool.stream()` 池侧零统计」已被 [ADR-0033](0033-stream-usage-collector-not-token-counter.md) 改变（流式现已记池侧 token），但「网关不复用池统计」核心结论不变 |
| [0032](0032-keys-json-hot-reload-guard.md) | keys.json 热重载守护 | LLM 池 | 20s 轮询 mtime + 在途让位重建池，替代"手动 reload"；启动/重载自检 `inbound_gateway` 授权覆盖，独立于池子树 |
| [0033](0033-stream-usage-collector-not-token-counter.md) | 流式 token 记账 = usage 采集器（非计数器） | 统计/并发 | 流式 token 只采 provider 权威 usage，缺失诚实标 `provider_missing` 记 0 + 精确字符数，**绝不本地估算**；补齐 `pool.stream()` 池侧记账 + Anthropic 原生 SSE；升格 ADR-0031 决策 #4「不估算」为跨池/网关全局原则。**Correction**：曾一度基于"聚合代理丢 SSE usage"的错误前提给网关加了默认关的「缓冲伪流式」开关，后原始 SSE dump 推翻该前提——真因是 `_stream_openai_sse` 看到 `finish_reason` 就 break、漏读其后的尾部 usage chunk（本端 bug，已修：读到 `[DONE]` 再统一收尾，仿 Anthropic）；据此缓冲开关已**彻底移除**，真流式**同时**得到逐字直播思维 + provider 精确 token（实测 total=395 / reasoning 529 字），网关真流式并把 provider 的 usage 尾 chunk 转发给外部客户端、透传真实 `finish_reason` |
| [0034](0034-gh-mirror-wipe-guard.md) | gh_mirror 同步脚本清库守护 | 发布工具 | 上游 force-push 清库实锤（CipherTalk）后加三层防护：每分支滚动锚点 tag、提交数悬崖检测（阻断 + 永久快照）、retag 保护旧值另存；分叉不阻断只告警（否则 37 仓永久卡死） |
| [0035](0035-evidence-gate-deterministic-lanes-no-model.md) | 证据门：检索/事件流两路刻意不过模型 | 证据与检索 | ue_learning_copilot 的出处必须机器可核实：确定性证据路永不过模型（模型回忆的行号不可核实），LLM 只消费证据集；被拒方案"四路全 agent"会击穿"可当场核实"承诺；代价是检索质量瓶颈转移到代码（T04/T06 各修过一个真检索缺陷），换来检索缺陷留下机器可查痕迹 |
| [0036](0036-unknown-outcome-first-class-tiered-recovery.md) | 「结果未知」成为一等终态，恢复行为由静态副作用档位决定 | 未知结局与恢复 | `unknown` 与 `failed` 分开（`running` 悬挂时副作用可能已落地）；终态不可回写只能追加；重跑与否由 `lib/side_effect_tier` 静态三档决定、模型无裁量；loop 一个周期只烧一次（run_key）且 `fail_count` 跨重启有效。被拒方案：让模型自己判断要不要重试、上跨进程租约 fencing（威胁模型是防误判不防越权）。代价：不可逆动作卡住时要人裁决一次，档位表要维护 |
| [0037](0037-grant-only-on-new-approval.md) | 授权状态只由"新批准"写入：平级/低权限的重复 request 不改动作坊 | 屏幕操控 | `screen_request_control` 只在 `status=="confirmed"` 才 grant；skipped（会话已 active、窗没弹）不改模式/`shutdown_permitted`/时钟/描述——因 `grant()` 整体替换状态而 skipped 字典写死 `shutdown_permitted=False`，2026-09-30 睡前批准的关机权限被采集脚本自己的请求擦掉。二次修订：关机位改为显式升级维度（`needs_shutdown`，缺位才弹第二次窗，代价是重建 watchdog 时钟），判据收进 `SessionManager.needs_new_approval`；同轮附记：被动 takeover_confirm 定位为纵深防御（生产路径 403 在前，弹不到）、watchdog 下 CONFIRM 自动跳过的文案纠偏、空闲时限以两阶段策略为唯一真源、紧急停止撤销失败不再静默 |

| [0038](0038-playwright-to-patchright-protocol-subtraction.md) | 浏览器自动化库换用 patchright：反检测只做协议层减法，不做 JS 加法伪装 | 浏览器自动化 | 原版 Playwright `connect_over_cdp` 逐 page/frame 发 `Runtime.enable`，console 序列化预览走原型链枚举可被页内 `ownKeys` 陷阱测到（V8 修 stack getter 后残余的协议层面，JS 伪装治不了）。迁 API 兼容 fork patchright（29 文件 31 处 import，含两处内嵌命令模板），`console_logs` 改按需 `Runtime.enable`（session 粘性开启 / 一次性窗口式，抓完 disable）。被拒方案：注入 JS 伪装补丁（违反 `docs/browser-anti-detection.md` 且改不到协议层）、CDP `Log.enable` 兜底（Chrome 140 实测零事件证伪）、常开 Runtime.enable（保留被实测抓到的永久暴露面）、砍掉 console_logs。代价：首次读取之前的日志不可见；依赖第三方 fork 需跟版本 |
| [0039](0039-shared-resource-ownership-lease-not-identity-gating.md) | 共享资源用「归属记录 + 租约」而不是身份验证：浏览器 tab / 屏幕授权 / 终端 | 共享资源归属 | 09-30 探针 tab 被静默关闭无法追责，定性为归属缺失（无合法 owner 可验证，大家共享同一浏览器实例）。身份判据单一来源 `lib.agent_identity.get_caller_id`；浏览器两档租约（有主 900s 操作即续租 / 无主 300s）、LRU 只淘汰 anonymous、抢绑与关他人 session 报 409 `TAB_OWNED` 需显式 `force`、单 caller 上限 10、全局 32、关闭写 FIFO 200 墓碑并在 9 处 not-found 响应附原因；屏幕授权**只记 owner 不加 409**（ADR-0037 决策 17：覆盖须新弹窗确认，弹窗即人类裁决）；终端记 `owner_caller` 且维持"走审批"而非拒绝（用户裁决）。被拒方案：身份验证/硬隔离（威胁模型是误伤不是防恶意，且挡掉正当交接）、只加日志（事故形态正是"当场不知道、事后查不到"）、每 agent 独立 profile（破坏"复用用户已登录实例"前提）。代价：无头老脚本租约 900→300 且优先被淘汰；抢绑不通知被抢方；数值由用户按并发观测拍板不是推导 |
| [0040](0040-memory-telemetry-aggregate-before-purge-and-repair-labels-before-gating.md) | 记忆库判据整改：遥测先聚合后删、判据不可信时禁止加拦截 | 记忆系统 | 三张明细表按 30 天硬删且删前无聚合（实测累计 1094 条 trace / 10165 行 evidence，两表现均为 0 行）⇒ `maintainer.run()` 顺序固定为「老化→验证→**指标日聚合**→retention 清理」，窄表 `memory_metrics_daily` 不参与 retention，单指标失败汇成 `_errors`（「算不出来」必须区别于「值是 0」）。实测 72% fact 被维护器判 archive，而旧验证器只喂 `value[:800]`——120/151 条超 800 字符、81 条前 800 字符正是被抄进正文的元数据信封，于是理由写成「内容被截断/乱码」；且 archive 既**不摘除召回**又**永久不可重验**。裁决：先修产生判据的环节（剥信封/放大窗口/分 fact_type 判据/删「个人数据应判 archive」的项目中心偏见/archive 纳入可重验），**不在坏判据上加拦截**；正文仍超窗时只延判不判死（连续 3 次改判 update）。行为类指标只报数不设阈值（无基线不设线），与 ADR-0035/0036 同族：被评对象不得自己出题自己判卷 |
| [0041](0041-approval-level-four-tier-runtime-second-gate.md) | 审批严格性做成四级并与 dcg 的 enabled 解耦，拦截判定改两道门 | 审批/守卫 | 原 `enabled` 只管 dcg 二进制预检查、端点审批另走一层且不读它 ⇒ "dcg=false 为什么还审批"。现 `[command_guard].approval_level` 四级（strict/moderate/loose/none）+ 运行时二次确认（中间件 `classify_safety_runtime`、网关 `_by_op` 走 `_op_to_path` 反查）。关键区分：`auto_tag_routes` 按 strict 写 OpenAPI（展示理论最大面），`build_safety_lookup`/`build_operation_safety_map` 才按当前 level 过滤（故改档免重启）；无效值回退 strict。代价：查"为什么没拦"必须看两处；moderate/loose 两份降级清单漏更新是静默少拦 |
| [0042](0042-workspace-component-discovery-convention-and-manifest.md) | workspace 组件用目录约定 + manifest 双轨发现，主代码库不 import 任何组件 | Skill 体系 | 私有组件目录排掉了、主库里的 import 与注册条目还在 ⇒ 改为发现式：扫 `workspace/*/loop_actions.py` 取约定符号，manifest.toml 优先、旧约定兜底且已声明的组件跳过旧机制防重复。**故意不用 `pkgutil.iter_modules`**（只枚举带 `__init__.py` 的包，而组件目录按约定不带，失败形态是静默零发现）。删除目录即自动注销。代价：发现失败静默；`GUIDE_REGISTRY` 仅启动时合并、无热重载端点。插入点清单不在本文写死，看 `list-components` 或 grep `load_manifests()` |
| [0043](0043-vl-provider-level-quota-fallback.md) | VL 池的额度耗尽按 provider 记账，并用"距上次调用间隔"区分并发 429 与额度 429 | 视觉/OCR | 旧实现一个全局 `daily_exhausted` + 全局 `last_call_finished_ts`：一家 429 全家停摆，且 A 刚调完 fallback 到 B 时 B 的"额度到顶"会被误判成"并发"。现 per-provider 时间戳与冷却（默认 3600s），连续 3 次耗尽或连续 5xx 达阈值推到次日 00:00，**所有**候选都在冷却才置全局（向后兼容且是 `loop_actions` 切 OCR 兜底的路由开关）。代价：60s 是写死的启发式，上游改限流节奏会系统性误分类；判定状态进程内、重启清空 |
| [0044](0044-vl-state-granularity-model-lock-granularity-key.md) | VL 的模型级失效状态按 `(key_id, model)` 记，执行锁、并发上限与冷却记账按 `key_id` 记 —— 两种粒度故意不一致 | 视觉/OCR | 降级链把候选升到 `(key, model)` 后，"哪个模型死了"必须 model 级，但上游限流记在 token 上（`vision.max_concurrency` 挂在 keys.json 的 key 记录上，model 级并发没有配置位），于是同一份代码里两种索引并存且**看起来像疏漏**——合理读者的默认动作是"统一粒度"，统一成 model 级就静默放松了串行。锁才真正保证串行（`active_count` 在 acquire 之后自增，只靠 key 级检查有竞态），并发上限只是选路的早退信号。代价：同 token 换模型不会更快；Ticket 02(a) 已裁决 —— 5xx 连败计数与冷却**保持 key 级连坐**（拆到 model 级会让 5 模型那条 key 单轮最多烧 15 次 5xx 才落 OCR 兜底），代价靠 `_provider_exhausted[key_id].trigger_model` 记账 + 日志指名变可见。守卫 `TestSameTokenSerialization` 三例 + 摘改矩阵，第一版判据曾恒绿。Ticket 04 补聚合判据：`daily_exhausted` 也算"整条 key 的模型当日全判死"（kind=`model_unavailable`，**不是**配额信号，deny 文案按成因分岔），配套 `providers[].models[]` 两级视图与 `POST /vision/reset` 手动复位 |
| [0045](0045-context-estimate-anchor-and-unknown-limit.md) | 上下文压缩判据以「上一次调用的真实 prompt_tokens」为锚点；模型上限未知时不判压缩 | 对话引擎 / LLM 池 | 实测 `compactions` 表 0 行、`context_overflow` 事件 0 条，而真实 `prompt_tokens` 峰值 97090 —— 判据两头都对不上：`len(text)//4` 只数入库消息（同一会话估出 27196，差 ~3.6 倍，量不到 system prompt 与工具 schema），上限又写死 32000（低于实际跑通的值）。现走 `EventStore.get_last_usage_anchor()` 真值 + 锚点之后消息的字符增量，`basis` 把两种口径写明；上限来自 keys.json 的 model 级 `context_window`，**没填就报"上限未知"且不判压缩**，不再用编出来的默认值比。代价：同刻归属（`>=` 而非 `>`，本机时钟 ~15.6ms 一个 tick）会多算一条，偏保守；`_to_dict` 与 Key 表单必须原样带回该字段否则被 GUI 保存静默抹掉。⚠️ 判据修准 ≠ 接线：主动触发仍不在主循环里，runner 每轮只打 `context estimate` 观测行，压缩还是 413 被动触发 |
| [0046](0046-ask-user-blocking-bridge-and-timeout-ownership.md) | ask_user 用「worker 同步阻塞 + 主线程卡片」桥；等待时长只归配置，中断必须绕过 worker 事件循环 | 对话引擎 | 工具 `ask_user` 自 v6-lite 就在清单里却从未接线（无 `ask_callback` ⇒ 一直 fail-closed）。三处省事的路都被拒：① 用 `await` Future 不占线程——会把 runner 的"工具要么返回要么已返回"语义改成可长期悬挂，且跨线程 `set_result` 仍要走此刻送不达的 `call_soon_threadsafe`；② 让 agent 传 timeout（或写死默认 30s）——能传 1 秒就是能替你拍板，写死非 0 默认会把"等你点"变成"过会儿自己没了"，故时长只在 `[runner] ask_user_timeout_secs`，schema 里没这格且有测试钉住；③ 在 `cancel_pending` 里摘条目——反向探针实测改坏照样绿（`ask()` 的 `finally` 本就清理），真正挡窗口的是 `resolve()` 的"已定局不可再写"。五种 outcome 各占一档文案，`timeout`→TIMEOUT、`skipped`/`cancelled`/`no_host`→USER_DENIED 且都写明"这不是反对" |

## 主题域分组

- **LLM 池**：0001, 0002, 0032, 0045
- **对话引擎**：0006, 0009, 0010, 0016, 0021, 0045, 0046
- **记忆系统**：0008, 0022
- **屏幕操控**：0011, 0012, 0015, 0025, 0026, 0037
- **审批/守卫**：0005, 0018, 0023, 0041
- **视觉/OCR**：0007, 0043, 0044
- **内容审核**：0004
- **Skill 体系**：0003, 0042
- **发布工具**：0013, 0014
- **密钥管理**：0017
- **数据校验**：0019
- **HTTP 客户端**：0020
- **统计/并发**：0024, 0033
- **Agent Guide**：0028
- **分层架构**：0027
- **备份/恢复**：0029
- **模型生命周期**：0030
- **入站网关**：0031
- **证据与检索**：0035
- **记忆系统**：0040
- **未知结局与恢复**：0036
- **浏览器自动化**：0038
- **共享资源归属**：0039

## 何时写 ADR

只有以下三项**全部**成立时才写 ADR（详见 [ADR-FORMAT.md](../../.agents/skills/dev/domain-modeling/ADR-FORMAT.md)）：

1. **Hard to reverse** - 反转成本有意义（改一个 if-block 不算）
2. **Surprising without context** - 未来读者会问"为什么这样设计"
3. **Real trade-off** - 确实有被拒绝的替代方案

反模式（**不写 ADR**，CHANGELOG 足矣）：bug 修复、安全补丁、性能优化、可逆代码调整、字段语义显式化、内存泄漏修复、单点焦点校验。

## 如何写 ADR

1. 扫描本目录最高编号，递增一作为新编号
2. 文件名格式：`NNNN-kebab-case-slug.md`
3. 遵循 [ADR-FORMAT.md](../../.agents/skills/dev/domain-modeling/ADR-FORMAT.md) 模板
4. 参考现有 ADR（如 0001、0002）的结构：标题 + 摘要 + Context + Decision + Considered Options + Consequences + References
5. 代码路径用 `file:///` 绝对路径链接
6. 写完后在本 README 索引表格追加一行

## 编号冲突历史

2026-08-06 修复了一次编号冲突：`0001-approval-domain-consolidation.md` 与 `0001-auto-scoring-routing.md` 同号。因 `auto-scoring-routing` 被 18 处代码/文档引用为 ADR-0001（config.example.toml / server/config.py / server/llm_pool/pool.py / scoring.py 等），保留其编号不变；`approval-domain-consolidation` 重编号为 0005。新 ADR 从 0017 起编号。
