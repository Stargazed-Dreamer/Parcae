# 周期任务与空闲额度跑批台账（workspace 触发真源）

> **用途**：一次性回答三个问题——① 哪些 workspace 任务本质上需要周期性跑；② 现在分别由谁提醒；③ 用户说"我额度很多 / 想跑长期自动化"时，agent 该核对哪些、能提议什么。
> **下次别再重新探**：本表就是探完的结果。新增/停用 workspace 时同步改本表（见「维护」段）。
> 判据一律指向代码或运行时端点，不写会漂移的计数。

## 三种触发机制（先分清，再谈"谁该提醒我"）

| 机制 | 谁动 | 消费入口 | 特征 |
|------|------|----------|------|
| ① 自动 loop | loop 引擎自己 | `GET /loop/tasks`（间隔来自 `config.toml` 的 `[loops.*]`） | 完全不用人管；`workspace/*/manifest.toml` 的 `[loop_tasks]` 声明候选动作 |
| ② 到期看板 | **用户开口** | `GET /todos` → `GET /todos/due`，判据 `server/todos/store.py` 的 `get_due_todos()`；展示在 client DueTodos 面板 + Dashboard | 到点了只会"挂在看板上"，**没有推送**，agent 不会因此自主跑 |
| ③ 机会型 | 用户主动问 | 本文件「空闲额度自查流程」 | 不注册任何定时器；只在用户表示额度有余时由 agent 核对后提议 |

`frequency` 只支持 `daily / weekly / monthly / quarterly`（`server/todos/models.py` 的 `TodoFrequency`）。`condition` 是自由文本，其自动化判定依赖 `todos_trigger_check` action——该 action 已注册但**没有实例化任务**，所以"只有条件成立才提醒"这件事系统做不到，只能靠 `condition_status` 人工置位（`niuke_review` 就是这么用的）。这正是第 ③ 类存在的理由。

### 第 ③ 类怎么被路由到（2026-09-20 落地，别再重新探）

`agent_guide` 决定返不返完整 TaskGuide 的门是 **`strong_match`**，不是分数：`is_weak = score < WEAK_MATCH_THRESHOLD or not strong_match`（`server/agent_guide.py` 的 `get_agent_guide`），而 `strong_match` 只认 `kw_exact`、`kw_fuzzy ratio≥80%`、或语义补强（`cosine≥0.6`，或 `score≥阈值 且 cosine≥0.4`）。**分数再高但 `strong_match=False` 照样走 `low_confidence`**（实测：一次弱匹配 score=33 仍 `low_confidence`）。`low_confidence` 分支刻意不注入任何记忆与项目结构（省 token，且此时 task_type 未定）。

要让"额度很多"这类口语进得来，做的是**把词注册进 `system.task_reminder.keywords`**（`server/agent_guide_data.py`），并把台账指针直接写进该条目的 `first_action`——这样 TaskGuide 一返回就带着指针，不依赖记忆注入的排序。已注册词：额度很多/额度有余/额度多余/额外额度/空闲额度/长期自动化/顺便跑/能跑什么。**实测**四句原话全部靠 `kw_exact` 强匹配（score 19-42，与 top-2 分差 ≥17），且把语义补强整体关掉后分数一字不变——即命中不依赖 embedding 可用；"起个后端自主会话"仍归 `adhoc.headless_session`（`tests/guide_eval/cases.jsonl` 已加防回归用例）。

同批修掉的两个消费侧缺陷（`server/agent_guide.py` 的 `_build_task_guide`）：

- **`trigger_keywords` 此前是死字段**：调用 `find_consumable_memories` 时传的是候选 skill 的**显示名**（如"任务提醒"），不是用户原话，所以按名字做子串匹配的关键词永远命中不上——而 AGENTS.md 与 `展示文档/architecture_analysis/04_skill_routing.md` 都写作"任务描述出现这些词时应读取"。现改为透传 `task_query=task`，无文本时（`task_type=` 精确调用）退回旧的显示名行为。
- **【必读记忆】此前恒取最旧 5 条**：`find_consumable_memories` 没有 ORDER BY（返回≈写入序），`first_action` 却直接 `[:5]`，导致必读块长期被几十天前的通配记忆占据、新写的记忆永远排不进。现按 `_MUST_READ_RANK`（静态 `memory_key` > `context+keyword` > `keyword` > 仅 `context`）+ 越新越前排序。回归测试在 `tests/memory/test_find_consumable_memories.py`。

> **生效前提**：`GUIDE_REGISTRY` 与 `_build_task_guide` 都在后端进程里，改完要**重启后端**；registry 变更后 embedding 缓存（`data/agent_guide/embeddings_cache.json`）按 hash 自动重建，首次匹配会慢一次。

## 空闲额度自查流程（agent 必读）

**触发语**："我现在额度很多"、"想跑一下长期自动化"、"有什么可以顺便跑的"、"空着的额度能干啥"。

**动作**：逐项取下面台账的"上次活动"信号 → 与阈值比较 → 一次性报告**超期项**，每项附一句"跑它要什么、大概多久、有什么风险" → 等用户点单。**禁止**不核对就断言"都新鲜"，也**禁止**用户没点就开跑。

| 任务 | 阈值 | 上次活动核对信号 | 怎么跑 | 注意 |
|------|------|------------------|--------|------|
| homework 作业巡检 | 额度有余且 >1 天没动；无条件 >1 周没动 | `workspace/homework/records/journal.md`、`records/todos.md` 的 mtime | 按 `workspace/homework/AGENTS.md` 的五段流水线（获取/检查/完成/汇总/人工提交） | 砺儒云统一身份认证需人工登录，agent 不代填密码；提交环节恒为人工 |
| web_archive 超链接群导出+跑 | >1 周没动 | `workspace/web_archive/reports/`、`state/` 内最新文件 mtime | 供源 `wechat_source.py`（**必须用 wechatDecode 的 venv** 跑）→ 出 `state/wechat_source/pending_links.json` → 对话确认要抓哪条 → 按站点 `extract_web.py` / `extract_feishu.py` / `extract_tencent_doc.py` | 抓取需调试浏览器（CDP 9222）+ 步骤 0 登录态预检，未登录时评论数偏少且不报错。⚠️ "每天限额 20 篇、每篇间隔 15~30s" 是 **`extract_xiaolin.py`（小林coding）** 的约束，不是 weflow；该慢爬已于 2026-09-16 全量完成（见 `reports/report_xiaolin.md`），weflow 的一次性脚本已回收、**当前无可运行入口** |
| gh_mirror 告警处理 | 额度有余且 >1 周没人工处理 | `GET /loop/tasks` 里 `gh_mirror.monitor` 的 `last_result.alerts`（**每轮重复计数，不是去重后的仓库数**）；明细在 `GET /inbox?status=pending`（⚠️ 只有 `pending` 是合法状态值，传 `new` 会静默返回空列表 → 假绿灯） | `workspace/gh_mirror/health_check.py`；上游被删走 `RUNBOOK_wiped_upstream.md` | 监控本身每 24h 自动。⚠️ 系统性误报：`alert_lag_threshold_days = 7`（`config.toml` `[loops.gh_mirror]`）与镜像同步 cron `0 3 * * 1`（周频）同量级，且 monitor 每日跑在当天同步**之前**，于是每周必然刷出一批 `mirror_lag`；判据里的 `upstream_sha != mirror_sha` 也恒成立（同步流程会打 strip commit 删上游 workflow 文件，SHA 永远不相等）。2026-09-29 实测：pending 告警对应 10 个仓库，全部 `dmca-backup.yml` 最近一次 run `conclusion=success`，**无需人工 dispatch**。**判"镜像是否真落后"的口径**（2026-09-30 定）：不比 tip SHA，比"镜像分支 tip 若是 `chore: strip upstream CI workflows` 提交，其 `parents[0]` 是否等于上游该分支 tip"；`workspace/gh_mirror/workflow_template.yml` 的同步循环带"上游无新提交即 skip"，所以一轮下来 tip 变化数少是正常的。**`sync_failed` 不可当误报直接关**：09-28 那次是单个 ref 被 `remote rejected` + 循环 `set -e` 中断后续分支。另一层坑：cron 只跑**默认分支**上那份 workflow，09-30 普查发现 `mirror-SteamTools`/`mirror-lux` 的 GitHub `default_branch` 是上游 `dependabot/*` 分支（`repos.json` 的 `branch` 同样记着它），且这两个仓的脚本落后于现役模板（无清库守护）——已升级并把默认分支改回 `main`/`master`；但"远端脚本版本 vs 模板"的漂移检查直到 2026-10-05 才落地：只读体检 `workspace/gh_mirror/workflow_drift_check.py`（口径与按设计豁免见 `workspace/gh_mirror/README.md`「Workflow 模板更新」），首跑即证明 `rollout_update.py` 的 `PILOT_DONE` 手抄清单失真（记为已下发的仓，其 `release-backup.yml` 在远端从未有过提交历史）；未处理的缺口与后续动作见 `wip_5950f631` |
| official_gacha 官方抽奖扫描 | 无自动；有空可跑 | `workspace/official_gacha/state/latest_scan.json` 的 mtime | `workspace/official_gacha/SKILL.md`（Playwright 连 CDP 9222 扫官方账号近 7 天动态） | **故意不注册提醒**（风控顾虑）。参与抽奖是键鼠+OCR 实操，弹窗需用户确认 |
| modelscope 模型库更新 | 每月；额度有余可提前 | 最新报告文件名日期：`workspace/modelscope_model_update/report_*.md`、`推荐报告_*.md` | `workspace/modelscope_model_update/SKILL.md` | 结果会**覆盖写 `data/llm/keys.json`**，必须用户确认后再写；魔搭会不定期下架模型 ID，拖太久等于模型池悄悄失效 |
| model_data 大模型数据集增量轮（D 轮） | >1 周没动即可提议（2026-09-30 实测：历史轮间最大间隔 4 天，D51 前空窗 16 天、库内最新 release_date 停在 09-13） | `workspace/model_data/docs/` 最新 `D<NN>_REPORT.md` 文件名日期 + 该子仓 `git log -1`（嵌套独立 git，不在主仓历史里） | 按 `workspace/model_data/docs/增量更新工作流.md` S0→S5；S0 唯一清单入口 `docs/跟踪源清单.md`（§B2 有各台账页实测坑：DeepSeek 走 sitemap、阿里台账必须 zh/en 页） | **用户 2026-09-10 拍板不设 todo/loop，别顺手补提醒**；⚠️ `agent_guide` 会把该工作区任务误路由到 `adhoc.office_xlsx`（2026-09-30 实测），入口直接读该工作区 `docs/README.md`；门禁基线 ERROR 0/WARN 0、qa_outliers r1/r2 须保持 0；派采集 subagent 并发≤3；变体口径按 D51「独立测量身份」分判 |
| API Key 全量健康检查 | 随时可加跑 | `GET /apikey/status`；每条 key 的 `status.last_health_check`（在 `data/llm/keys.json`，只看时间字段别回显密钥） | `POST /apikey/keys/health-check-all` | ⚠️ `[loops.key_health] check_interval` 只是 loop 的 **tick 间隔**；真正的检查受 `HEALTH_CHECK_INTERVAL_DAYS`（`server/llm_pool/key_store.py`）节流。未到间隔时每轮返回 `skipped=true, skip_reason=未到检查间隔`，`last_run_at` 因此会一直刷新而看起来"刚查过"——**这是正常状态，不是故障**，额度多时补跑才有意义。补跑会写 `fail_count`，`fail_count >= HEALTH_CHECK_MAX_FAILS` 即置 `works=false` 出池 |
| 记忆压缩/维护 | 长会话后、或批量写记忆后 | `GET /memory/stats` | `POST /memory/compress`、`POST /memory/maintain` | `memory.maintain` 已每 6h 自动 |

## 已注册周期任务台账

以 `GET /todos` 为准，下列是当前注册项与其真实语义：

| todo id | 频率 | workspace | 语义 |
|---------|------|-----------|------|
| `accounting` | 每周 | `accounting` | 导出账单 + GUI 审核 + 导出 Obsidian |
| `bilibili_gacha` | 每周 | `bilibili_gacha` | 查开奖、清理未中奖动态与关注 |
| `niuke_review` | 每周 | `niuke_review` | 带 `condition="找工作期间"`；`condition_status=inactive` 时才不挂看板 |
| `wuwa_gacha` | 每月 | `arknights_gacha` + `yihuan_gacha` | **伞形条目**：一条 todo 覆盖两个仍在玩的游戏增量采集（方舟官网只留 90 天，必须按月采） |
| `todo_251d1433` | 每季度 | `life_design` | 被动展示型：到期只挂看板，用户主动说"做人生设计"agent 才读 `workspace/life_design/prompt.txt` 开对话 |
| `todo_20c74a86` | 每月 | `modelscope_model_update` | 2026-09-20 新增，替代"全靠用户记得开口" |
| `todo_df6b969a` | 每周 | `homework` | 2026-09-20 新增；"额度有余且一天没动"那档走上面机会型台账 |
| `todo_955d1de3` | 每周 | `web_archive` | 2026-09-20 新增；对应"超链接群导出+跑一周没动" |

> **已知行为，不要再当 bug 报**：`get_due_todos()` 有两条独立的判据（`server/todos/store.py`）：① `next_due_at` 有值且 `<= today` 时判到期，而 `next_due_at` 仅由 `mark_done()` 写入——所以 `last_done_at` 有值、`next_due_at` 为 null 的条目**永远不会再进到期名单**（当前 `community_review` / `arknights_gacha` / `yihuan_gacha` 属此类，均为有意保留，要恢复提醒就正常跑一次并 `todos_mark_done`）；② **`last_done_at` 为空则恒判到期**——因此从未 `mark_done` 过的条目（如 `accounting`、`bilibili_gacha`、`todo_20c74a86`）会一直挂在 due 名单里，它们出现在名单中**不等于"本周新到期"**，别据此推断"有活要干"。

## 自动 loop 台账（不用你管的那些）

以 `GET /loop/tasks` 为准。当前实例与间隔（间隔值取自 `config.toml` 对应 `[loops.*]` 段）：

| task_id | 间隔 | 作用 |
|---------|------|------|
| `activity_tracker.collect_windows` | 60s | 活动追踪窗口采样 |
| `activity_tracker.screen_vl` | 300s | 屏幕 VL 摘要 |
| `activity_tracker.hourly_summarize` | 每小时 | 小时级活动总结 |
| `activity_tracker.daily_summarize` | 每天 | 日总结（写 `private_vault/activity/daily/`） |
| `activity_tracker.cleanup` | 按配置 | 活动数据清理 |
| `download_watcher.scan` | 300s | 下载目录新文件监视 |
| `gh_mirror.monitor` | 86400s | 镜像 4 类状态检查，告警推 inbox |
| `memory.maintain` | 21600s | 记忆老化+验证维护 |
| `key_health.check` | 43200s | LLM 密钥可用性与余额 |
| `secret_backup.run` | 432000s | 密钥文件备份 |
| `workspace_backup.run` | 432000s | 各 workspace 声明的 `[backup]` 数据文件备份 |
| `stock_advisor.opening` / `.closing` / `.evening` | cron `35 9 * * 1-5` / `5 15 * * 1-5` / `0 21 * * 1-5` | 股市三时段分析（evening 带 catchup） |
| `ue_learning_copilot.log_watch` | 20s（`[loops.ue_learning_copilot] poll_interval`） | 轮询已配置的 UE 工程目录日志，追加可核实事件到 `data/ue_learning_copilot/events/`（只落文本、7 天滚动、对工程目录零写操作） |

> **已知行为，不要再当 bug 报**：loop action 必须返回 `success`/`skipped` 两个**布尔键**（`server/activity_tracker/loop_manager.py` 的三态判断），返回 `{"status": "ok"}` 这类形状会被逐次判失败，跑满 `fail_threshold` 即 `auto_failed` 暂停且不报错。暂停状态持久化在 `data/loops/tasks.json`。**重启是否自动恢复要看计数**（`_load_state()`，2026-09-30 改）：`auto_failed` 且 `fail_count < fail_threshold` 才自动解除；`fail_count` **不再在重启时清零**，攒满阈值的任务跨重启也不会复活，必须人工 `POST /loop/tasks/{id}/resume`。`paused=true` 但 `paused_reason` 缺失的历史条目按 `manual` 保留（不再猜成 `auto_failed` 直接放行），并推一条 inbox 说明分不清。

### 一个周期只烧一次：run_key 与「结局不明」（`unknown-outcome-evidence` T07/T08）

每个触发周期算一个 `run_key = "{task_id}@{period_ts}"`，动作执行前写-ahead 落盘（`inflight_run_key`），返回后按结果分类（`RESULT_STATE_RULES`）处理：

| 结果 | fail_count | 是否可自动暂停 | 该周期 |
|------|-----------|----------------|--------|
| `success` | 清零 | — | 从 inflight 解除 |
| `skipped` | 不计数 | — | 不占用 |
| `failed` | +1 | 是（达阈值即 `auto_failed`） | 可重跑 |
| `unknown` | +1 | **否**（不自动暂停，也不自动重跑） | 记进 `unknown_run_keys` 等人裁决 |

- **结局不明的来源**：进程被杀时 `inflight_run_key` 仍有值（`_load_state` 检测）；或动作返回体带 `"outcome_unknown": True`（无人值守 judge 判定为 `uncertain` / `run_judge_agent` 异常时会打这个标记，见 `server/activity_tracker/loop_actions.py`）。
- **重跑是否允许由副作用档位决定**（`lib/side_effect_tier.py`，见 `docs/chat-engine.md`）：只读/本地可逆 → 交给 catch-up 正常补；外部不可逆 →  held 住不再自动烧这个周期，推 inbox 要人裁决。任务档位可被 `workspace/*/manifest.toml` 的 `[side_effect_tier]` 覆盖。
- **catch-up 有上限**：`CronTrigger.CATCHUP_RUN_CAP = 24`，超出部分不再补跑，累计到 `missed_periods_dropped` 并推 inbox（避免重启后一次性打爆 200 轮）。
- **inbox 去重是存储级的**：带 `dedupe_key` 的推送（`loop_unknown:<run_key>`、`loop_catchup_dropped:<task_id>:<小时槽>`、`memory:maintain_result:<ISO 周>`）由 `server/inbox.py` 的部分唯一索引保证同 key 只落一条，第二次创建返回既有条目并标 `deduplicated=True`——不依赖调用方自己记得"先查再推"。键的**粒度**要按"什么变了才算新事件"选：记忆维护告警原先在调用方比对 `stale_count`，而老化数每天都在漂，等于永不合并（实测 8 条 pending 里 4 条是同一类告警的不同天副本），2026-10-06 改按 ISO 周切。
- `GET /loop/tasks` 的每条任务因此多出 `side_effect_tier` / `inflight_run_key` / `unknown_run_keys` / `missed_periods_dropped` 四个可核对字段。

`[loop_tasks]` 声明了候选动作但**未注册实例**的：多数 workspace 的 `loop_actions.py`——那里只放 `GUIDE_REGISTRY_ENTRIES`（agent_guide 路由条目），不是 loop 任务。

⚠️ `email_source` **现在是启用的**（`config.toml` `[loops.email_source] enabled = true`，2026-09-29 实测；本文档曾长期记为"故意关着"）。它的 poll 会周期性刷 `task_failure_warning` 进 inbox，同时在列的还有真实邮件通知（`category=email`）——这两类要分开看待，别把失败告警当新邮件。

## 有意不做周期化（下次别顺手补提醒）

| workspace | 为什么不补 |
|-----------|------------|
| `mindforge` | SKILL.md 明写"低频模块，agent 不主动推荐" |
| `endfield_gacha`、`wuwa`（单游戏采集） | 已退坑，不采集（见记忆 `gacha_games_status`） |
| `official_gacha` | 怕风控，故意不注册提醒；有空跑走上面机会型台账 |
| `task_room`、`chat_digest`、`yefeng_group4`、`ai_club_recruit` | 用户裁决"我问再说"；后两者本身有明确时间窗 |
| `exam_prep` | 期末季节性，非全年周期 |
| `disk_manager` | 事件驱动（空间不足才跑）；全面备份功能标注未测试 |
| `stock_advisor` 月度简评 | 用户裁决不提醒；`valid_until` 月底自动失效已有机制 |
| `gh_mirror`、`stock_advisor` | 有 `recurring.*` skill 条目但由 loop 全自动驱动，**不需要**再建 todo |
| `recorder`、`windows_forensics`、`yihuan_simulator`、`dev_toolkit`、`apikey_test`、`image_organize_remote_vl_sample`、`web_archive/dev` | 工具型/取证型，被调用才动 |

## 维护

- 新增 workspace：判断它属于"自动 loop / 到期看板 / 机会型 / 有意不周期"四类中的哪一类，写进对应表；机会型必须给**可核对的上次活动信号**（文件路径或端点），否则不进台账。
- 改周期判据逻辑时同步本文件：到期判据在 `server/todos/store.py`，频率枚举在 `server/todos/models.py`，loop 实例在 `config.toml` `[loops.*]`。
- 相关系统文档：`docs/todos-wip.md`（todo/WIP 系统本身）、`docs/operations-manual.md`（loop 与终端 API）、`AGENTS.md`（`agent_guide` 路由与收尾闭环）。
