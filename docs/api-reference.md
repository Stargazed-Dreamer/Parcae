# 后端 API 速查

服务地址：`localhost:8766`（FastAPI）| OpenAPI 文档：`http://127.0.0.1:8766/docs`

> agent 优先用 MCP 工具名调用（`mcp_parcae_*`），REST 路径仅用于 curl/脚本。完整端点列表调 `agent_guide(task='...')` 或访问 `/docs`。

## 全局

| 路径 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 全局健康检查（含所有模块状态） |
| `/config` | GET/POST | 获取/更新配置 |
| `/shutdown` | POST | 优雅关闭服务器 |
| `/mcp/stats` | GET | 查询 MCP 工具调用统计 |
| `/mcp/stats/reset` | POST | 重置 MCP 工具调用统计 |

> `/health` 的 `guard` 字段是审批闸门自身状态：`command_guard_enabled`/`approval_level`/`llm_review_enabled`/`gate_open`/`note`/`recent_origins`。**`gate_open=false` 时 `note` 会明说"闸门当前关闭"**——审批端点在放行，来源分档没有记录，别把"没有可疑放行"读成"有人看过"。详见 `docs/operations-manual.md` 审批相关章节。

## OCR

| 路径 | 方法 | 说明 |
|------|------|------|
| `/ocr/status` | GET | OCR 模型状态 |
| `/ocr/file/json` | POST | 经典 OCR（JSON Body，MCP 直连） |
| `/ocr/base64` | POST | 经典 OCR（Form 上传） |
| `/ocr/path/json` | POST | 经典 OCR（本地路径） |
| `/ocr/vl/file/json` | POST | 远程 VL 文档解析（JSON Body） |
| `/ocr/vl/path/json` | POST | 远程 VL（本地路径） |
| `/ocr/models/keep/json` | POST | OCR 模型管理：保留指定模型 |
| `/ocr/models/unload/json` | POST | OCR 模型管理：卸载指定模型 |
| `/ocr/models/preload/json` | POST | OCR 模型管理：预加载指定模型 |

## Agent / Guide

| 路径 | 方法 | 说明 |
|------|------|------|
| `/agent/status` | GET | Agent 配置状态（模型层级） |
| `/guide` | GET | Agent Guide（task 参数获取任务指导） |
| `/agent/score` | POST | LLM 内容评分（6 维度） |
| `/agent/chat` | POST | LLM 通用对话 |

## 代码执行

| 路径 | 方法 | 说明 |
|------|------|------|
| `/exec/status` | GET | Exec 模块状态 |
| `/exec/python` | POST | 执行 Python 代码（万能回退） |
| `/exec/apply-patch` | POST | 应用补丁（支持 dry-run、回滚） |
| `/exec/cmd` | POST | 执行 Shell 命令 |
| `/terminals/spawn` | POST | 启动后台终端会话 |
| `/terminals/{tid}` | GET/DELETE | 查看/删除终端 |
| `/terminals/{tid}/output` | GET | 增量读取终端输出 |
| `/terminals/{tid}/input` | POST | 向终端发送输入 |
| `/terminals/{tid}/kill` | POST | 终止终端 |
| `/output` | GET | 列出缓冲的 exec 输出 |
| `/output/{exec_id}` | POST | 查看 `exec_cmd` 的一次性输出缓冲（full/range/search）；`exec_python` 完整输出走 `/terminal/{tid}/output` |

## 屏幕控制

| 路径 | 方法 | 说明 |
|------|------|------|
| `/screen/status` | GET | 屏幕状态（管理员权限、紧急停止） |
| `/screen/windows` | GET | 列出所有可见窗口 |
| `/screen/capture` | POST | 截图（inline=ImageContent / path / base64；返回 snapshot_id，帧缓存供 zoom/局部 OCR 复用） |
| `/screen/ocr` | POST | 截图+本地 OCR，返回文字 bbox（Computer Use 首选；支持 snapshot_id 帧复用 + region 局部 OCR） |
| `/screen/zoom` | POST | 最近帧局部裁剪放大（不重截图；inline=ImageContent / path） |
| `/screen/action` | POST | 键鼠操作（含安全检查；click 默认 UIA 融合 strategy=auto；支持 mouse_down/up/move 分段原语；支持 verify_prompt） |
| `/screen/focus-window` | POST | 强力激活窗口 |
| `/screen/batch-actions` | POST | 批量键鼠操作（click 步骤支持 strategy 融合） |
| `/screen/clipboard` | GET/POST | 剪贴板读（截断 2000）/写（danger block 拦截；受会话权限保护） |
| `/screen/scroll-capture` | POST | 滚动截图拼接 |
| `/screen/wait-for` | POST | 条件等待（OCR/VL 检测文字出现） |
| `/screen/snapshot` | POST | 桌面快照（窗口列表+OCR文本，无 base64） |
| `/screen/analyze` | POST | 图像特征分析（颜色/亮度/异常告警） |
| `/screen/preview/action` | POST | 点击位置预览（红点标记） |
| `/screen/overlay` | POST | 显示操作提示覆盖层（持久显示，文案/颜色/倒计时由 SessionManager.status 推送 tick 自动刷新；传 message 字段已弃用） |
| `/screen/control/request` | POST | 请求用户介导的当前任务授权（`mode=normal|watchdog`；watchdog 弹窗含时长 + 「允许关机」复选框，复选框默认勾选。只有用户新批准才改授权状态；任务需要关机时带 `needs_shutdown=true`，缺关机位才会弹第二次窗） |
| `/screen/control/release` | POST | 主动收回当前任务授权（幂等） |

> 第一次副作用 Computer Use 操作前优先调 `screen_request_control(task_description=..., source="agent")`。授权只对当前任务普通控制生效；normal 模式按 `[screen] idle_warning_seconds`+`idle_grace_seconds` 两阶段空闲降级（默认 10min 告警 + 20min 降到无权限），watchdog 模式不因空闲降级、只到硬上限降到 normal；危险操作和安全阻断不绕过。`verify_prompt` 仅在高风险/状态难判断时使用。详见 `.agents/skills/computer_use/SKILL.md`。

## 视觉AI

| 路径 | 方法 | 说明 |
|------|------|------|
| `/vision/status` | GET | 视觉模型状态 |
| `/vision/understand` | POST | VL 图像描述/状态理解（非坐标定位） |
| `/vision/locate` | POST | 无文字元素坐标兜底（自动反归一化） |
| `/vision/providers` | GET | VL provider 列表与运行时状态（含 providers[].models[] 两级视图） |
| `/vision/reset` | POST | 一键解除当日 VL 不可用标记（模型判死名单 / key 级冷却 / 聚合耗尽） |

## 视频

| 路径 | 方法 | 说明 |
|------|------|------|
| `/video/frame` | POST | 课程 packet 按秒取帧（内存解码，MCP 返回 ImageContent，不落盘；多帧拼联络表） |

## 记忆

| 路径 | 方法 | 说明 |
|------|------|------|
| `/memory/status` | GET | 记忆模块状态 |
| `/memory/list` | GET | 列出所有记忆 key |
| `/memory/{key}` | GET/POST/DELETE | 读取/写入/删除记忆 |
| `/memory/search` | POST | 语义搜索（三源召回+证据组织）；不传 `source` 时默认排除 `source='tool'` 的工具调用日志，定向查它需显式传 `source="tool"` |
| `/memory/maintain` | POST | 手动触发记忆维护 |
| `/memory/maintain/status` | GET | 维护器状态 |
| `/memory/search/traces` | GET | 检索过程追踪列表 |
| `/memory/search/traces/{trace_id}` | GET | 单条检索详情 |
| `/memory/search/traces/cleanup` | DELETE | 清理过期 trace |
| `/memory/projections` | GET | 上下文投影留痕列表（记忆真正上屏了什么） |
| `/memory/projections/{id}` | GET | 单条投影留痕详情（上屏顺序/被砍掉的 key/有效性标记） |
| `/memory/evidence/recent` | GET | 最近证据集列表 |
| `/memory/evidence/{evidence_id}` | GET | 单条证据详情 |
| `/memory/evidence/cleanup` | DELETE | 清理过期证据 |
| `/memory/wrapup` | POST | **收尾提交（DL3）**：原话 + 记忆候选一次交，服务端去重后落 `facts`；返回逐条去向 `accepted/merged/duplicate/rejected` + 原因码。被拒的也留档（`memory_candidates`），不静默丢 |
| `/memory/candidates` | GET | **待审候选（DL3/DL5）**：`status='rejected'` 的候选列表，含原 payload，供人工改后重提；可带 `run_id=` 只看某个 run |
| `/memory/candidates/{id}/review` | POST | **候选审核（DL5）**：`action=accept` 合并 `overrides` 后走 `write_memory` 唯一漏斗与去重，置 `accepted/merged/duplicate`；`action=reject` 置 `dismissed`（保留行）；非 `rejected` 状态返回 409 |
| `/memory/norms` | GET | **上下文红线（DL1）**：`kind='norm'` 的 global / project 规范；`must_read` 优先、global 先于 project；默认 5,000 字符，超预算安全类截断、效率类跳过，返回 `truncated/omitted` |

> POST `/memory/{key}` 支持 v3 结构化字段：`fact_type`/`occurred_at`/`consumption_contexts`/`trigger_keywords`。写记忆时必须填后两个字段确保消费闭环。

> **v8（DL3，2026-10-27）**：`facts` 新增 `title`/`summary`/`scope`/`kind`/`origin`/`sensitivity`/`evidence_refs`/`source_quote_ids`/`must_read`/`expires_at`/`dedup_hash` 11 列，并新增 `source_quotes`（用户原话，不可改写）与 `memory_candidates`（候选去向账）。设计见 `workspace/local_butler/30_架构与设计/30_施工与证据/17_DL1-DL5实施记录_20261027.md` §2。
>
> **DL1（2026-10-27）**：`GET /memory/norms?project_id=&budget=5000` 读取 `kind='norm'` 行；默认预算 **5,000**（用户由 2,000 上调，其余 bundle 页仍 2,000）；`kind='norm'` 不进入维护器的自动老化/自动重验。设计见 `workspace/local_butler/30_架构与设计/30_施工与证据/17_DL1-DL5实施记录_20261027.md` §4。

## 待办 / WIP

| 路径 | 方法 | 说明 |
|------|------|------|
| `/todos` | GET/POST | 列出/创建待办（T14 起底层是 `tasks` 表；status 映射回旧词） |
| `/todos/due` | GET | 获取到期周期任务 |
| `/todos/{id}` | GET/PUT/DELETE | 待办详情/更新/删除 |
| `/todos/{id}/done` | POST | 标记完成（自动算 next_due_at） |
| `/todos/{id}/check_trigger` | POST | 检查 triggered 任务触发条件 |
| `/wip` | GET/POST | 列出（GET 默认 summary 轻量模式）/创建 WIP 任务（T14 起底层是 `tasks` 表） |
| `/wip/batch` | POST | 按 ids 批量取 WIP 完整详情（`wip_get_many`） |
| `/wip/{id}` | GET/PUT/DELETE | WIP 详情/更新/删除 |
| `/tasks/queue` | GET | **事务队列（DL2）**：按 `15` §5.2 六档排序（逾期一次性 → 本周到期 → 到期的周期 → 无日期小任务 → 长期复盘 → paused/blocked），`view`/`days` 是派生值不写库 |
| `/tasks/list` | GET | **面板列表（T14）**：`tasks` 单表轻量列表，canonical 状态，`include_archived` 可关 |
| `/tasks/classification` | GET | **两轴分类报告（DL2）**：结构轴四类 + 目的轴三档计数、派生视图分布、`unmapped`（映射不上四类）与 `purpose_unset`（目的轴待标）清单 |
| `/tasks/{scope}/{id}/class` | PUT | **人工改分类（DL2）**：`scope=todos\|wip`；`task_class`/`purpose` 只收白名单值（模型层 `Literal` 拦下 422；规则层如 `todos` 传 `due_at` 拦下 400）；`todos` 不接受 `due_at`（那是 wip 的列） |
| `/tasks/{scope}/{id}/action` | POST | **统一任务动作（T14）**：`scope=todos\|wip`，`action=pause\|resume\|archive\|unarchive\|complete`；todos/wip 共用 canonical 状态词 |
| `/tasks/occurrences` | GET | 列出循环模板的 occurrence（`pending`/`done`/`missed`） |
| `/tasks/occurrences/materialize` | POST | 把到期的循环模板落成 occurrence（幂等，靠 `(template_id, due_at)` 唯一索引） |
| `/tasks/occurrences/{occ_id}/complete` | POST | 完成一次 occurrence：记证据 → 推进模板 `next_due_at` → 更早的 `pending` 判 `missed`；**模板不会被标 done** |

## 管家会话协议（DL4 + P5）

> 动作通过 4 个入口暴露，`action` 是第一个必填字段：`session.open` / `session.note` / `context.next` / `wrapup.report|task|memory|feedback|handoff` / `memory.write`；`session.inbox` 已于 2026-10-28 删除（沟通复用 `/user/message` 补充指令队列）。设计真源 `workspace/local_butler/30_架构与设计/11` + `DD-*.md`；实施记录 `30_架构与设计/17` §5。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/butler/runs` | POST | 建 run，返回 **4 位会话码** + 完整 prompt；prompt 已含页面清单（pages/cursor），agent 可跳过 `session.open` 直接并行取页；`task_title`/`done_when` 可空（agent 首轮回填） |
| `/butler/runs` | GET | run 列表：`status`（逗号分隔）/ `task_scope` / `task_id` / `limit`；返回 `stage` / `prompt_text` / `prompt_source` / `model`，不返回 `code_hash` |
| `/butler/runs/{run_id}` | GET | run 状态 + `reports` + `counters` + append-only 事件 |
| `/butler/runs/{run_id}/reports` | GET | 每轮报告列表（`attempt` 递增） |
| `/butler/runs/{run_id}/close` | POST | **仅用户/面板**关闭 run；`running` 返回 409 `run_running`；关闭后 agent 再报告 409 |
| `/butler/runs/{run_id}/prompt` | PUT | 用户改派单提示词：改**当前生效版**正文（不新增版本），`prompt_source` 记 `user_edit`；空文本 422 / 纯空白 400 `prompt_empty` |
| `/butler/runs/{run_id}/prompt` | GET | 按开关实时渲染（只读预览，不落库）：`terse=`（减少说明）/ `with_context=`（要求读入上下文），两个独立开关，不是四档 |
| `/butler/runs/{run_id}/prompts` | GET | 版本清单（v13）：`current` / `latest` / `limit`=10；每项 `version/chars/source/note/created_at/is_current`，不含正文 |
| `/butler/runs/{run_id}/prompts/{v}` | GET / PUT / DELETE | 取正文 / **改写该版正文**（改当前版时同步 `prompt_text`）/ 删该版（删当前版自动切到剩下最大版；最后一版 409 `prompt_version_last`） |
| `/butler/runs/{run_id}/prompts/{v}/activate` | POST | 回滚：把该版设为当前生效版（`prompt_source=rollback`），挪指针不复制 |
| `/butler/runs/{run_id}/handoff-prompt` | GET | 按 run 取「交接指令」（给当前执行者）；面板走下面的任务维度入口 |
| `/butler/tasks/{scope}/{task_id}/handoff-prompt` | GET | 交接按**任务**取：无有效交接 ⇒ `state=handoff`（交接指令，含会话码）；已有 `active`/`pending_archive` ⇒ `state=takeover`（接手说明，**带会话码**：已开新一轮则写该 run 的码，否则 `need_run=true`）；还没 run ⇒ `state=no_run` |
| `/butler/tasks/{scope}/{task_id}/handoff-run` | POST | 用现有交接开新一轮：建 run（写 `handoff_id`）并返回 `session_code` + 带码的接手说明；同一 handoff 已有未关闭 run 则复用（`reused=true`）；无 active handoff ⇒ 409 `no_active_handoff` |
| `/butler/session` | POST | 可选：`action=open`（手动拿 manifest；主流程已跳过）/ `note`（agent 留言，幂等） |
| `/butler/context` | POST | `action=next`：`cursor` 取单页（主流程一页一个调用、并行发出）；`cursors[]` 保留给脚本；run 为 `created` 时自动 open；同 cursor 幂等，内容变化返回 `stale=true`；返回页不含 `delivery_id` |
| `/butler/wrapup` | POST | **小步回报**：`action=report`（outcome+summary 必填，done 带 evidence，run 置 idle）/ `task`（task_updates）/ `memory`（memory_candidates+quotes）/ `feedback`（recall_feedback）/ `handoff`（handoff+blocked_on_user）；`submit` 保留为整包兼容；各小步可同批并行，返回不含 `run_id` |
| `/butler/memory` | POST | `action=write`：单条记忆写入（走 `upsert_fact` 唯一漏斗 + dedup hash）；**入参不含 run/会话码** |

> `wrapup` 小步返回只留「确认 + 异常信号」：`report` 回 `status/outcome/attempt/claimed_done/blocked_unexplained`；`task` 回 `updated`，未落地时带 `reason`（`no_changes` / `adhoc_or_no_task`）；`memory` 回 `accepted/merged/duplicate/rejected`，被拒或 `origin` 被降级时带 `issues[{index,reason,message}]`；`feedback` 回 `count`；`handoff` 只回 `created`/`ignored`（没有则空对象）。不回显完整 task、fact_key、artifacts 等面板/调试字段。

> **v9/v10（DL4 + P5，2026-10-27）**：`butler_runs`（会话码 hash、owner/心跳/manifest、`prompt_text`、`model`、`report_count`/`last_report_at`/`blocked_on_user_json`/`next_run_hint_json`）、`butler_handoffs`（四态无 TTL）、`butler_deliveries`（`UNIQUE(run_id,bundle,cursor)` 幂等 + 召回反馈三列）、`butler_reports`（每轮报告，`UNIQUE(run_id,attempt)`）、`butler_notes`、`butler_events`（append-only）。生产库迁移由 `MemoryStore.initialize()` 自动完成，无需手工脚本。
>
> **面板（P5 T10/T11，2026-10-28）**：Vue3+TS 在 `workspace/local_butler/_archive/panel_旧原型-v2/`（`npm run dev` 起 5173，`/api` 代理 8766），Tauri 2 壳在 `src-tauri/`（`tauri dev` 验收，生产构建未跑）；卡片主界面 + 6 tab + 可筛选列表；只走上面的 HTTP 接口，不直连 DB。旧单页 `panel/index.html` 降为只读回退。

## 浏览器

> **路由条数以 `server/browser/routes.py` 或运行时 `/docs` 为准，不在本文写死**。

### Session 管理

| 路径 | 方法 | 说明 |
|------|------|------|
| `/browser/status` | GET | 浏览器连接状态（page/target_count/context_count；`nav_guard` 各域冷却与额度持有者、`tab_origins` 来源分布） |
| `/browser/tabs` | GET | 列出标签页（稳定 target_id / opener_target_id / opener_url；`origin`=session/preexisting/unknown、`origin_hint`、`first_seen_at`、`open_secs`，有主页另带 `session_id`/`owner`；`created_by_session`/`created_by_owner`=最初建这个 tab 的会话，会话关闭后仍保留） |
| `/browser/close` | POST | 关闭标签页（urls 模糊匹配 + tab_ids 精确匹配，all_matches 控制单/批量） |
| `/browser/sessions` | POST | 创建持久会话（热态 <250ms，自动注入站点经验） |
| `/browser/sessions` | GET | 列出活跃 session |
| `/browser/sessions/close` | POST | 关闭 session（close_page=false 默认只释放句柄） |

### 进程管理（supervisor，2026-10-23）

> 只管浏览器进程（启动/探活/停止/重启），不碰 session 与 page。四个端点不进 `DIRECT_TOOLS`，agent 通过 `parcae_advanced_tool` 调用。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/browser/supervisor/status` | GET | supervisor 状态（state/pid/port/cdp_url/uptime/restarts/last_error），不启动浏览器 |
| `/browser/supervisor/ensure` | POST | 懒启动并等待 CDP 就绪；已就绪时直接返回。`reason` 可选 |
| `/browser/supervisor/stop` | POST | 停止浏览器：`Browser.close` → terminate → 进程树 kill；`force=true` 跳过优雅关闭 |
| `/browser/supervisor/restart` | POST | 手动重启（重置自动重启预算）；`reason` 可选 |

### 核心操作

| 路径 | 方法 | 说明 |
|------|------|------|
| `/browser/snapshot` | POST | ARIA 可访问性快照（role/name/value，返回 node_id 供 action 重放） |
| `/browser/action` | POST | 统一动作（click/double_click/fill/type/press/select/check/uncheck/hover/scroll_into_view） |
| `/browser/wait_for` | POST | 状态等待（selector/text/url/load_state/popup/filechooser/dialog） |
| `/browser/wait_and_action` | POST | 原子 wait+trigger（popup/filechooser/dialog 场景同请求完成，仅 session 模式） |
| `/browser/navigate` | POST | 导航（goto/back/forward/reload，分离 navigation_completed 与 wait_timeout；reload + force_beforeunload=true 触发 beforeunload dialog） |
| `/browser/handle_dialog` | POST | 手动处理 JS dialog（accept/dismiss + prompt 文本输入，2026-08-06 新增） |
| `/browser/set_http_credentials` | POST | 动态设置/清除 HTTP Basic Auth 凭证（context 级，2026-08-06 新增） |
| `/browser/grant_permissions` | POST | 显式预授权权限（geolocation/notifications/camera 等，避免弹原生对话框，2026-08-06 新增） |

### 辅助功能

| 路径 | 方法 | 说明 |
|------|------|------|
| `/browser/evaluate` | POST | 只读 JS 执行（正则拦截 location/cookie/fetch/XHR/eval/innerHTML= 等写操作） |
| `/browser/console_logs` | POST | 控制台日志（session 模式持久缓冲 + since_cursor 增量读；无 session 一次性 capture_ms 抓取） |
| `/browser/screenshot` | POST | 截图（shot_type=viewport/full_page/element；**`return_base64` 默认 False 只回 path**，True 才内嵌 base64 并触发 ImageContent 转换。看图优先把 path 交给 `understand_image`/`ocr_path`） |
| `/browser/match_site` | POST | 按域名匹配 `browser_lessons/sites/*.md` 站点经验库 |
| `/browser/find_url` | POST | 搜索调试浏览器书签/历史（auto_open=true 一键开 session） |
| `/browser/selector_stats` | POST | 查询选择器成功率（按 domain/target_type 聚合） |

> **关键约束**：
> - `browser_session_create(url=...)` 自动按 URL hostname 匹配 `browser_lessons/sites/*.md` 站点经验
> - snapshot 的 `node_id` 可直接传给 `browser_action`，缓存 60s 或 URL 变化后失效返回 `STALE_NODE`；agent 显式 `verify_dom_freshness=true` 可感知同 URL 下 DOM mutation
> - `dialog` 触发后由 `_on_dialog` 持久监听器存入 session 队列（不立即 dismiss）；agent 用 `browser_handle_dialog` 主动 accept/dismiss + 输入 prompt 文本；5 分钟未处理由超时兜底自动 dismiss
> - `popup`/`filechooser` 跨 HTTP 请求时序不可靠，必须用 `browser_wait_and_action` 原子 API
> - `browser_evaluate` 拦截写操作（location/cookie/fetch/XHR/eval/innerHTML= 等），写操作走 `browser_action`
> - SPA back/forward 可能 `wait_timeout=true` 但 `navigation_completed=true`（不是失败）
> - **dialog 阻塞检测双通道**（2026-08-06 新增）：`browser_status` 主动查询（聚合所有 session 的 `has_pending_dialog`/`pending_dialogs`）+ 所有操作端点预检（`browser_action`/`browser_navigate`/`browser_snapshot`/`browser_evaluate`/`browser_screenshot`/`browser_wait_for`/`browser_wait_and_action` 有 pending dialog 时返回 `blocked_by_dialog=true` + `blocking_dialog` 信息而不调 page 操作；例外：`wait_type="dialog"` 不预检）
> - **beforeunload 弹窗**：`browser_navigate(action="reload", force_beforeunload=true)` 用 `page.close(run_before_unload=True)` 触发 beforeunload dialog；agent 调 `handle_dialog(accept)` 后页面关闭，再调 `browser_navigate(action="goto", url=<原 url>)` 重开；`handle_dialog(dismiss)` 则页面保留
> - **HTTP Basic Auth**：`browser_set_http_credentials(session_id, username, password)` 预设凭证，避免 401 弹原生对话框阻塞；`clear=true` 清除（context 级，不落盘，密码不进日志）
> - **权限预授权**：`browser_grant_permissions(session_id, permissions=["geolocation", ...])` 显式预授权，避免站点请求权限弹原生对话框；`origin=None` 时用当前 session page 的 origin
> - 同 URL 多标签必须传 `tab_id`（从 `/browser/tabs` 的 `target_id` 拿），否则 `url_pattern` 静默选第一个匹配
> - **同域导航节流（2026-10-23）**：同一域名 30s 内只允许一次 `reload` / `goto`（含 `browser_session_create(url=...)` 的首次打开；`back`/`forward` 不计）。超频 `browser_navigate` 返回 `success=false` + `error_code=NAV_RATE_LIMITED` + `nav_retry_after_secs`；`/browser/sessions` 返回 429 + `Retry-After`。连续违规窗口**不翻倍**（恒定 30s，`violations` 仅作观测），localhost/内网/file: 豁免。当前各域冷却状态见 `browser_status.nav_guard`。**撞限流别重试，改复用**（`url_pattern` 复用已有 tab / 在现页面上继续操作）。配置：`[browser] nav_guard_enabled / nav_guard_window_secs`
> - **限流拒绝带归因（2026-11-01）**：被拒时 `debug_detail` 会指出**这一份额度是谁用掉的**——`holder`（caller）/`holder_session`/`holder_url`/`holder_ago_secs`，形如「8s 前被 agent-A（session e1121d84…）用掉的」。没有 caller 时明写「无法归因到具体 agent」而不编造。429 的 detail 另附当前活跃 session 数与 owner 分布、该域已开着的 tab 数。归因**只进文案与 `browser_status.nav_guard`，不参与窗口计算**（窗口恒定 30s）
> - `browser_session_create` 响应新增 `reuse_hint`（同域已有其他 tab 时提示「下次用 url_pattern 复用」）；`browser_session_close` 的目标是「任务真结束」——中途歇一下别关，session 有 idle 租约自动回收
> - `browser_action` 失败时通过 `error.error_code` 返回结构化错误（TAB_NOT_FOUND/TAB_REQUIRED/AMBIGUOUS_TAB/ELEMENT_NOT_FOUND/STALE_NODE/MULTIPLE_MATCHES/INVALID_SELECTOR/UNSUPPORTED_ACTION/TIMEOUT/BROWSER_DISCONNECTED/PAGE_BUSY/EXECUTION_ERROR），phase 标识 connect/locate/act/wait/verify 阶段
> - **页级互斥 + 版本戳（2026-11-01）**：同一**物理 page** 上的并发操作在页级锁上排队（按 session 锁不住——两个 session 本是两把不同的锁），等待超过 `wait_secs`（默认 10s）⇒ `PAGE_BUSY`，detail 点名占用者的 session / owner / action / 已占用时长。锁保护的是"同时改同一页"，不是"独占"：正常串行调用无感。`STALE_NODE` 除原有的 URL 变化 + 60s 过期外，新增**第三层**：页级版本号变了且写者不是自己 ⇒ 拒绝并写明"被哪个 session／owner 的哪个 action 改过"（多 session 共享同一 tab 时，前两层都会漏判）
> - **冷路径必须指定目标（2026-11-01，G4）**：不带 `session_id` 调 action/snapshot/evaluate/wait_for/console_logs/navigate/screenshot 时，若浏览器有多个 tab（或唯一 tab 正被别的会话持有）⇒ `TAB_REQUIRED`，`debug_detail` 给出候选（tab_id/标题/URL/持有者）。只有"恰好一个 tab 且无主"才沿用旧的单页兜底
> - **脚本开的页算「外来者」，不参与上述判定（2026-11-01）**：`workspace/`、`tools/` 下约 35 个脚本直连 CDP 9222、不建 session（`AGENTS.md` 规定如此）。用户裁决把它们当**不受约束的外来者**——不限流、不要求登记、不加归属；但它们也**不算数**：歧义判定只看 `origin != "unknown"` 的页，所以「1 个你的页 + 3 个脚本页」仍然直接放行。`/browser/tabs` 每条返回 `origin`（`session`/`preexisting`/`unknown`）+ `origin_hint` + `first_seen_at` + `open_secs`；`browser_status` 返回 `tab_origins`（含 `foreign_count`/`unknown_count`/`foreign_tabs`）。只剩外来页时仍拒绝（不自动操作别人的采集现场），提示里会说明原因
> - **`session` 来源由后端主动认领，不依赖下一次观测（2026-11-01 修复）**：`browser_session_create` 成功时立刻 `claim()` 登记，所以「建了会话但从没被查过 `/browser/tabs`」的 tab 不会被记成 `unknown`；会话关闭后它仍保持 `origin=session`（不会退化成疑似脚本页），`session_id`/`owner` 清空但 `created_by_session`/`created_by_owner` 保留最初建它的是谁
> - **多个 session 可共享同一 tab（2026-11-01，G3）**：`url_pattern` 复用 / `force=true` 后 `page_ref_count > 1`（`/browser/sessions` 每条都带 `page_ref_count` 与 `page_shared_with`）。此时关掉其中一个 session 只解绑，**不会关页**（响应 `page_kept=true` + `page_kept_by`），避免把邻居的会话一起带走
> - **`force=true` 读作 join，不是「抢过来」（2026-11-02 正名，行为未变）**：语义是**加入共享**——对方 session 原样保留、不会被踢也不会收到通知，两个 session 从此指向同一个物理 tab（DOM/登录态/表单状态只有一份，点击与输入互相可见，写冲突靠页级锁 + `STALE_NODE` 兜）。`browser_session_create` 命中他人有主 tab 且未带 `force` ⇒ 409 `TAB_OWNED`；带 `force` 后响应回 `page_ref_count`/`page_shared_with`，并在 >1 时给一句 `note` 明说「你在与谁共享」。想独占就别共享：改用 `url=` 新开一个 tab
> - `browser_selector_stats` 自动记录每次 `browser_action` 调用结果（fire-and-forget，DB 失败不阻塞 action）

## LLM 并发池

| 路径 | 方法 | 说明 |
|------|------|------|
| `/llm/pool/status` | GET | 池状态（per-key/per-model 详情，summary=true 精简） |
| `/llm/pool/models` | GET | 跨 key 聚合 per-model 统计 |
| `/llm/pool/recent-calls` | GET | 近期调用历史（上限 200 条） |
| `/llm/pool/init` | POST | 初始化池（从 keys.json 加载） |
| `/llm/pool/call` | POST | 通过池调用 LLM（完整版） |
| `/llm/pool/call-simple` | POST | 通过池调用 LLM（简化版，返回文本） |
| `/llm/pool/stats` | GET | per-project token 消耗统计 |
| `/llm/pool/health-check` | POST | 执行 key 健康检查 |
| `/llm/pool/cleanup` | POST | 清理 works=false 的 key |

> `/llm/pool/models` 和 `/llm/pool/recent-calls` 已加入 `GATEWAY_EXCLUDE`（监控面板专用），agent 用 `/health.llm_pool` 获取聚合状态。详见 `docs/llm-pool.md`。

## 入站网关（Inbound Gateway）

OpenAI 兼容本地中转：外部 harness（Cline / Cherry Studio 等）把 `base_url` 指到 `http://127.0.0.1:8766/v1`、配 `sk-la-` 本地 key，即可用整个模型池。每次调用落 `data/inbound_calls.db`（key/模型映射/上游 key/tokens/TTFT/状态码），统计全部由网关自己的 SQLite 聚合（**不复用** `pool.project_stats`——`pool.stream()` 侧零统计）。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容对话（流式与非流式），需 `sk-la-` key |
| `/v1/models` | GET | OpenAI 兼容模型列表（需 `sk-la-` key） |
| `/inbound/keys` | GET/POST | 入站 key 列表 / 创建（仅 127.0.0.1，无鉴权） |
| `/inbound/keys/{key_id}` | PATCH/DELETE | 更新（启停/改名/限额）/ 删除入站 key |
| `/inbound/calls` | GET | 调用日志（支持 key_id/model/状态筛选分页） |
| `/inbound/stats` | GET | Token 统计聚合（`days` 参数，含 TTFT p50/p95、成功率） |

> 8 个 operation_id 全部在 `server/mcp_whitelist.py` 的 `GATEWAY_EXCLUDE`（不进 MCP 工具列表）。模型解析三段：key 别名 → 池内精确匹配 → 404 带可用列表（无 tier 兜底）。面板见 `client/panels/inbound.py`（入站管理，四页签）。

## 高级工具 / 模板网关

| 路径 | 方法 | 说明 |
|------|------|------|
| `/advanced/run` | POST | 调用低频/运维工具（通过 `parcae_advanced_tool` 网关路由，可路由工具全量清单以 `GET /advanced/tools` 为准） |
| `/advanced/tools` | GET | 列出可用高级工具（含 7 段语义说明） |
| `/advanced/docs/tool` | GET | 单个工具的完整语义说明 |
| `/advanced/docs/project` | GET | 项目级文档索引 |
| `/templates/run` | POST | 执行工作流模板（通过 `parcae_template_tool`） |
| `/templates/list` | GET | 列出可用模板 |

## 其他模块

> 调用频率低，通过 `agent_guide(task='...')` 或 `/docs` 查看完整列表。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/loop/status` | GET | Loop 调度器状态 |
| `/loop/tasks` | GET | 列出所有 Loop 任务 |
| `/loop/tasks/{id}/run` | POST | 手动触发 Loop 任务 |
| `/inbox` | GET/POST | 列出/创建收件箱条目 |
| `/mindforge/status` | GET | MindForge 可用性（2026-10-27 起 MCP 工具整组摘除，仅 REST） |
| `/mindforge/search` | POST | 搜索知识库（同上，仅 REST） |
| `/docviewer/read` | POST | 读取文档内容（2026-10-27 起 MCP 网关已摘除，agent 用 Read/exec_python） |

### 对话管理探针（chat-mgmt-probe）

**是什么**：用 MockLLM 顶替真 LLM 跑一遍 v6-lite 对话引擎（EventStore + SessionRunner + 工具调用），
采集 messages / events / tool_calls / llm_requests / llm_responses / session 六份数据，供评估"自家对话管理好不好用"。
⚠️ **不是**监控/采样探针（与 `tools/disk_io_audit/probe.py`、`server/resource_probe` 无关）。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/probe/runs` | POST | 跑一次探针会话（operation_id `probe_run`，走 `parcae_advanced_tool` 网关） |

**关键参数**（`server/probe/router.py`）：
- `persist`（默认 **False**）：落盘 6 个 JSON 到 `temp/probe/runs/<sid>/`。默认**纯内存跑**，`run_dir` 返回空串
- `include_full`（默认 False）：摘要默认只给 80 字符 head；置 True 才另附全文（`request_system` / `request_messages` / `response_content` / `tool_calls[].args` 等），**纯内存验证需要它**
- `db_path`：留空用探针专属 `temp/probe/agent.db`（**不写生产库** `data/client/agent.db`）
- `mode` / `pattern` / `script` / `use_stream` / `trigger_approval` / `max_context_messages` / `auto_cleanup`

**触发方式**：`POST /probe/runs`（MCP 或 HTTP）/ 脚本 `from lib.agent_runtime.probe_runner import run_probe` /
`pytest tests/agent_runner/test_probe.py`（15 处直接调用；测试已把 `RUNS_DIR` 隔离到 `tmp_path`，不污染 `temp/probe/`）。

### 收件箱（inbox）

**inbox 是什么**：Loop 任务（后台周期任务）运行中产生的、需要用户审查/确认的条目队列。SQLite 存储（`data/inbox.db`），REST CRUD。

**典型来源**（`source` 字段）：
- `activity_tracker` — Loop 任务失败告警（首次失败 / 失败累计过半 / 连续失败 5 次自动暂停）
- `auto_shutdown:<task_id>` — 关机已触发 / 已取消通知
- `download_watcher` — 下载文件夹三分类（move/inspect/unknown）待确认
- `loop_manager` — Loop 调度器异常

**条目结构**：`id / source / category / title / description / payload(JSON) / status(pending/resolved/ignored) / resolution(JSON) / created_at / resolved_at`

**状态流转**：`pending`（待处理）→ `resolved`（已解决）/ `ignored`（已忽略）。已解决/忽略超过 10 天的条目由 `CleanupActivityAction` 自动清理（复用 `cleanup_cron`，默认每天 0:30）。

**agent 使用方式**：
- 查询待处理条目：`inbox_list`（MCP 直连，status=pending）/ `inbox_get`（单条详情）—— 走 `parcae_advanced_tool` 网关
- 批量管理（resolve/ignore/delete）：`inbox_batch` 端点，仅监控面板调用，不进 MCP（防膨胀）
- 当用户问"我的 inbox 咋回事 / 收件箱"时，调 `inbox_list(status=pending)` 按来源归类汇总

**注意**：测试 auto_shutdown 等模块时，必须 mock `_push_inbox`，否则测试会真往 inbox 写条目污染收件箱（见 `tests/approval_screen/test_auto_shutdown.py` 的 autouse fixture）。

### 数据库浏览（dbview，2026-10-31）

面板「数据库」页的只读数据源，实现在 `server/dbview.py`。**全部只读**（连接 `PRAGMA query_only=ON`），
只给面板用，不进 MCP（要读库的 agent 走 `exec_python` / 既有 memory 端点）。
设计依据见 `workspace/local_butler/30_架构与设计/20_现行设计/28_数据与记忆_面板视图与召回整改_20261030.md` §1。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/db/sources` | GET | 五个库总览：`exists / size_bytes / tables` |
| `/db/tables?source=` | GET | 表清单（`name / rows / columns`，按行数降序） |
| `/db/schema?source=&table=` | GET | 字段表（类型/非空/主键/**填充 n/m**/样例值）+ 索引 + DDL |
| `/db/rows?source=&table=&limit=&offset=` | GET | 只读分页行（`limit ≤ 500`） |
| `/db/files?group=` | GET | 文件组：`memory / logs / skills / docs / projection` |
| `/db/file?path=&lines=` | GET | 文件前 N 行预览（默认 200） |

**数据源 id 固定五个**：`memory`（走记忆配置，测试可用 `PARCAE_MEMORY_DB_PATH` 重定向）/ `agent` / `inbox` / `inbound` / `browser_stats`。
插件自有库（`data/accounting.db` 等）**不在其中**，随插件页走。

**安全约束**（每条都有用例守着，`tests/dbview/test_dbview_api.py`）：库只认注册表 id（不接受路径）；
表名先查 `sqlite_master` 再双引号转义（不接受任意 SQL）；文件路径 `resolve()` 后必须落在仓库根内 +
扩展名白名单 + 敏感路径黑名单（`config.toml` / `keys.json` / `chrome_debug` / `backups` / `private_vault`）；
行数 > 20 万的表跳过填充率统计（返回 `fill_scanned=false`，不静默给 0）。

## MCP

| 路径 | 说明 |
|------|------|
| `/mcp` | MCP 工具接口（三层：直连白名单 + 网关路由 + 预定义模板；各层数量以 `server/mcp_whitelist.py` 和 `server/templates.py` 为准） |
