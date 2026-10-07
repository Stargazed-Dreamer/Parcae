# AGENTS.md - LocalAgent 项目指引

## 项目定位

LocalAgent 是一个个人 AI Agent 项目，集合了电脑操控、Agent 工具和日常可自动化工作流。
所有 Skill 定义在 `.agents/skills/` 目录，索引文件为 `_index.md`。新 skill 按 scope 分桶：`dev/`（工程化开发，借鉴 mattpocock/skills）、`daily/`（日常事务）、`_vendor/`（上游 fork 存档）、`_deprecated/`（弃用占位）；历史扁平 `.md` skill 保持原位。

后端提供 OCR、LLM 对话、浏览器控制、代码执行、屏幕操控、文档处理与知识库搜索（MindForge 集成）等能力，通过 FastAPI 暴露 REST API 和 MCP 工具接口。

## 技术栈

- **后端**: FastAPI + Uvicorn，端口 8766
- **OCR**: 经典 PaddleOCR 3.7（PP-OCRv6）+ PaddlePaddle-GPU 3.2.2（CUDA 12.6，PIR 懒加载禁用）；截图管线关闭 UVDoc 去畸变，bbox 三档分辨率回归误差 1-3px，可作为 Computer Use 文字定位主路径；远程 VL 主要提供文档解析和图像描述
- **视觉AI**: 远程 VL（ModelScope API-Inference；默认模型 = `data/llm/keys.json` 中 `vl` scope 里 tier 最低的启用模型，运行时看 `GET /vision/status` 的 `vl_model`，魔搭会不定期下架模型 ID），负责文档解析、图像描述与无文字元素定位兜底
- **LLM**: 多提供商配置（DeepSeek/OpenAI/智谱等），v8 tier 系统（model.tier 1-5 由 keys.json 定义，use_case.default_tier 决定调用层级，tier 硬匹配 + model 软偏好）
- **记忆**: 三层记忆系统 v3（Recent 滑动窗口 + SQLite 时间索引 + 向量语义检索 + BM25 + HMS 风格三源召回/EvidenceLedger/SearchTracer）
- **浏览器**: Chromium 内核浏览器 CDP 协议（端口 9222，独立调试实例，支持 Chrome/Edge/Brave/Vivaldi 等）
- **MCP**: fastapi-mcp，Streamable HTTP 协议，端点 `/mcp`
- **GUI**: PySide6 子进程（覆盖层/确认窗口）

> 环境兼容性约束（PaddlePaddle/Playwright/调试浏览器实例）详见 `docs/environment-constraints.md`。

## 文档导航

| 文档 | 内容 | 何时查阅 |
|------|------|----------|
| `AGENTS.md`（本文件） | 项目指引、会话规范、核心陷阱、关键约束 | 每次会话必读 |
| `docs/project-intro.md` | 深度项目导览（比喻主干、由浅入深、章→模块映射表、FAQ；人类读者入口，AI 可选读） | 新维护者上手 / 想系统理解项目时 |
| `docs/architecture.md` | 架构与设计决策（架构总览、后端模块清单、一次 agent 任务的完整数据流、关键设计决策、ADR 索引） | 理解整体架构 / 查"为什么这样设计"时 |
| `docs/operations-manual.md` | 后端重启、终端 API、日总结、挂机关机、浏览器经验、记忆系统、工具脚本、PySide6 性能 | 触发特定任务或运维时 |
| `docs/deployment.md` | 部署与其他机器迁移（依赖安装、模型下载、部署常见故障） | 新机器部署 / 部署故障排查时 |
| `docs/dev-workflow.md` | 功能变更检查清单、CHANGELOG 维护、发版流程、MCP 工具原则、MCP 接入、用户补充指令 | 工程开发/发版/MCP 配置时 |
| `docs/changelog-archive.md` | 历史 release 条目归档（按版本倒序，工具自动维护） | 查完整变更历史时 |
| `docs/release-policy.md` | 源码分发策略（public-full profile、Apache-2.0） | 打包/分发源码时 |
| `.agents/skills/_index.md` | Skill 索引 + API 快速参考 + 项目结构 | 需查看 skill 完整信息/API 接口表时（任务路由走 `agent_guide`） |
| `docs/api-reference.md` | 后端 API 速查（按功能分桶、浏览器分三层、精简描述） | 查 REST 端点/MCP 工具名时 |
| `docs/browser-anti-detection.md` | **浏览器反检测（伪装）规范**：为什么本项目不注入任何 JS 伪装补丁（实测证据）、禁止 playwright-stealth、指纹自检工具 | 改动浏览器链路前；想加"反爬/伪装"补丁时**必读** |
| `docs/computer-use-reference.md` | UIA 语义层、桌面事务、DPI、API 速查、注意事项 | 屏幕操控深入参考时 |
| `docs/vm-ops.md` | **虚拟机操作方案（全项目）**：通道决策表（vmrun GuestOps vs VNC 后台直连）、无窗口驱动装机、快照回滚纪律、实测坑清单 | 任何要开/装/控制虚拟机的任务**先读通道决策表** |
| `docs/mcp-reference.md` | MCP 三层架构、工具排除清单、REST→MCP 映射规则、统计 | 需要查 MCP 工具名/网关时 |
| `docs/tools-guide.md` | 所有工具脚本的详细用法、参数、安全机制 | 运行工具脚本时 |
| `docs/memory-system.md` | 三层记忆系统架构、API、压缩、自动记录 | 读写记忆时 |
| `docs/llm-pool.md` | LLM 并发池架构 | 脚本调用 LLM 时 |
| `docs/environment-constraints.md` | PaddlePaddle 兼容性、**资源采样中枢（常驻 NVML；GPU 侧只支持 NVIDIA，别的卡走扩展点）**、Model Lifecycle Manager（含"无 GPU→CPU 降级"缺口）、Playwright、调试浏览器、**网络与环境查询技巧**（HF 访问/查 wheel/验证 GPU/测挂死） | OCR/Vision/浏览器操作时；改资源采样/压力监控链路时；查环境类通用技巧时 |
| `docs/model-paths.md` | 模型路径管理（`[models]` 配置、外部目录回退） | 配置模型权重路径时 |
| `docs/recorder-guide.md` | 操作录制器用户指南（L0-L4 五层架构） | 使用录制器/采集操作时 |
| `docs/recorder-test-guide.md` | Recorder 模块端到端测试说明 | 测试录制器功能时 |
| `docs/ui/` | UI 设计系统（风格 token/组件/图标/交互模式/agent 实操手册，配套 `lib/ui/`） | **写任何 PySide6 GUI 前必读** |
| `tools/README.md` | 工具目录总览 | 查找工具脚本时 |
| `docs/adr/` | ADR（架构决策记录）索引 + 决策文档（覆盖 LLM 池/对话引擎/记忆系统/屏幕操控/审批/视觉/发布工具等主题域） | 遇到"为什么这样设计"或做架构决策时 |
| `docs/agent-guide-keywords.md` | agent_guide 关键词编写规范（match_task_candidates 五路加权打分） | 维护 GUIDE_REGISTRY 时必读 |
| `docs/chat-engine.md` | v6-lite 对话引擎架构（SessionRunner/EventStore/Compactor 等多组件 + EventStore schema（版本以 `client/core/agent/event_store.py` 的 `_SCHEMA_VERSION` 为准）+ chat-panel-v2 关键设计 + QThread 桥接） | 理解对话引擎工作原理时 |
| `docs/agent-guide.md` | 任务路由系统（GUIDE_REGISTRY 结构/scope 分类/consumption_contexts 消费闭环/first_action 注入/workspace 动态扩展） | 维护 skill 或理解任务路由时 |
| `docs/todos-wip.md` | 待办与 WIP 系统（三类型 todo/WIP 生命周期/task_closure 整合/API 端点表） | 使用 todos 或 WIP 功能时 |
| `docs/periodic-task-inventory.md` | **周期任务与空闲额度跑批台账**（哪些 workspace 本质上要周期性跑、分别由谁提醒、用户说"额度很多"时该核对哪些并提议跑什么、哪些有意不周期化） | 用户问周期任务/"额度多能跑啥"、新增 workspace 判断触发方式、改 todo 到期判据时 |
| `.agents/rules/project_rules.md` | 功能变更检查清单 | 新增/修改功能后 |

## 知识沉淀位置

**两类内容分开存放，不要混：**

| 类型 | 存放位置 | 说明 |
|------|----------|------|
| **日志**（今天做了什么、排查过程、因果链） | 各端要求的位置 | WorkBuddy 写 `.workbuddy/memory/YYYY-MM-DD.md`；其他 agent 按各自约定。日志是流水账，不需要进项目文档 |
| **通用知识**（环境技巧、网络访问姿势、部署坑、可复用的方法论） | 项目专题文档 | 见下表。这类知识与具体某次任务无关，下次还会用到 |

通用知识的推荐落点：

| 知识类型 | 落到哪 |
|----------|--------|
| 网络访问、依赖版本查询、硬件/驱动、推理设备验证 | `docs/environment-constraints.md`（「网络与环境查询技巧」章节） |
| 其他机器部署故障、模型下载与路径、依赖缺失 | `docs/deployment.md`（「其他机器部署常见问题」章节） |
| 架构决策与其原因 | `docs/adr/` |
| 功能变更的对外描述 | `CHANGELOG.md`（按 `docs/dev-workflow.md` 流程） |

> 判断标准：**下次换个任务还用得上吗？** 用得上 → 项目专题文档；只是这次干了什么 → 日志。

## 提交前机械防线（强制）

对 `server/` / `client/` / `lib/` 的任何代码变更，声称"完成"前**必须**跑两道检查：① `uv run python -m pytest tests/server_endpoints/test_smoke_endpoints.py -q`（GET 端点冒烟，≥500 即失败）；② `uv run pyright` 与基线 0 错误对比（不允许新增）。⚠️ `pyrightconfig.json` 的 `include` 只列这三个目录，`workspace/` / `tools/` 脚本仓库级运行**从不被检查**，改这类脚本必须显式 `uv run pyright workspace/.../xxx.py`。注册表类改动（GUIDE_REGISTRY/manifest/白名单/config）另跑 `tools/audit/drift_detector.py`（该脚本自写报告到 `temp/audit/drift_report.md`）。详见 `docs/dev-workflow.md` "提交前机械防线"章节。

## 会话启动检查清单（每次新会话第一步，必须执行）

> ## ⛔ 最高优先级铁律：每个用户任务都必须先调 `agent_guide`，禁止跳过直接干活
>
> **反复出问题、必须强制强调**：用户给出具体任务时（如"今日股票分析""帮我记账""整理下载文件夹"），agent 常常觉得"这我懂，直接搜/直接做就行"，**跳过 `agent_guide` 直接执行——这是错误**。
>
> `agent_guide(task='用户原始任务描述')` 是**任务路由入口**，不是可选的"启动检查"：
> - 它会匹配对应的 skill（如 `recurring.stock_analysis` / `recurring.accounting`）并给出 `first_action`
> - 它会**自动注入相关记忆**（上次分析到哪、用户关注哪些标的、用户偏好）——跳过 = 失忆
> - 它会给出该 task_type 的工具优先级（`mcp_tools_priority`）和环境提示
> - **跳过 agent_guide = 丢弃项目积累的所有 skill / 记忆 / 偏好，退化成无记忆的通用 ChatGPT**
>
> **自检规则**：如果你接下来要直接执行任何动作（WebSearch / 写代码 / 调 MCP 工具 / 读文件 / 截图），而本回合还没有调过 `agent_guide` → **停下来，先调 `agent_guide(task='用户原始任务描述')`**。
>
> **唯一例外**：纯事实查询（如"Python 版本是多少"）、闲聊、或用户明确说"别走流程直接做"——这些可以跳过。拿不准时，宁可调。
>
> 具体调用方式见下方"第 1 步"第 5 条。

### 第 1 步：确认 MCP 后端可用

**在执行任何任务之前，先确认 MCP 工具是否可用。** 如果 MCP 不可用，所有后端功能都无法使用。

1. 尝试调用任意 MCP 工具（如 `memory_status` 或 `exec_status`）
2. 如果调用失败或工具列表中没有 `mcp_localagent_*` 工具：
   - **不要用 curl/Invoke-RestMethod 替代**——后端也没启动，curl 会卡住
   - 提醒用户启动后端：`start.bat` 或 `.venv\Scripts\python.exe -m server.main`
   - 等用户确认启动后再继续
3. MCP 可用后，用 `exec_status` 确认后端正常
4. **MCP 工具名前缀**：两个 IDE 中工具名均显示为 `mcp_localagent_<operation_id>`（如 `mcp_localagent_exec_python`），调用时使用完整名称或简称均可
5. **调 `agent_guide` 获取任务指导（强制前置，禁止跳过！见上方最高优先级铁律）**：`agent_guide(task='用户任务描述')` 返回决策摘要 + 候选清单，无需读 _index.md 全量索引。无参调用返回全量分类清单（scope：recurring/adhoc/system/dev/daily/recording；内置 task_type 见 `server/agent_guide_data.py` GUIDE_REGISTRY，workspace 动态扩展见 `workspace/*/manifest.toml`）。返回中含 `environment_notes`（双屏/管理员权限/Computer Use 铁律）和 `session_startup`（含新会话首次任务的 skill 多选询问机制）
   - **为什么不能跳过（即使任务看起来很简单）**：以"今日股票分析"为例，agent 直觉是"搜一下行情就行"——但 `agent_guide(task='今日股票分析')` 会返回 `recurring.stock_analysis` skill 的 `first_action`、注入上次分析的股票清单和用户偏好、给出数据源工具优先级。跳过它你就丢了这些，做出的分析和上次完全脱节。**任何具体任务都必须先调 guide，再按 guide 的 `first_action` 执行。**
   - **可选 `include_structure=true`**：若任务涉及项目目录布局（如新增模块/查找文件位置），传此参数获取当前项目结构映射（替代下方"项目结构"章节的静态描述）
   - **开发任务先看 `dev_entry_points` 决策树**：GeneralGuide 响应含 `dev_entry_points` 字段，按目标清晰度选入口（`goal_clear_full_flow`→`dev.goal_engineering` 三阶段编排 / `goal_unclear`→`dev.grill_me` 追问 / `small_fix`→`dev.anti_hallucination`）。dev 桶 skill 按 role 分层（entry/method/support/tool），entry 是第一步。**禁止跳过入口直接走执行类 skill**（implement/to_tickets 响应会附 `prerequisite_check` 前置提示）
6. **屏幕操控前置检查（涉及截图/点击/输入时必读）**：
   - **管理员权限**：调 `/health` 检查 `screen.admin=true`。无权限时键鼠操作被 Windows UIPI 阻止（`SetCursorPos`/`SetForegroundWindow` 静默失败），用 `start.bat` 重启（自动 UAC 提权）
   - **双屏配置**：用户有时双屏有时单屏。`mode=fullscreen` 截图默认拼接所有显示器（mss.monitors[0]），OCR 可能返回多屏内容。需要窗口内容时用 `mode=window` + `force_fullscreen_crop=True`（DirectX 全屏游戏尤其重要，PrintWindow 会假成功返回错误内容）
   - **先读 Computer Use 说明**：`.agents/skills/computer_use/SKILL.md` 包含窗口操作铁律、DPI 缩放、坐标系统、同名窗口冲突处理。任何屏幕操控任务必须先读此文件
   - **定位优先级**：浏览器 DOM locator > UIA 语义层 > 桌面/截图 OCR bbox > `vision_locate`。OCR bbox 已修复，可直接承担文字定位；远程 VL 默认只做布局/状态描述，避免普通文字点击消耗昂贵且慢的 VL 配额
   - **OCR 坐标转换**：`screen_ocr(mode="window", engine="ocr")` 返回窗口截图内 bbox；中心点加 `list_windows` 返回的窗口 `left/top` 后再传 `execute_action`。`screen_snapshot` 只返回 OCR 文本摘要，不返回 bbox
7. **浏览器（调试浏览器实例）自主启动（涉及浏览器操作时必读）**：
   - **项目有专用调试浏览器**，agent 应自主启动和管理，**不要问用户"要我来开还是你来开"**——直接开
   - **检查**：`browser_status` 看调试浏览器（CDP 9222）是否已运行
   - **未运行时启动**：`exec_python` 执行 `tools/browser/start_debug_browser.py`（非交互模式，自动用空白配置或已有 `chrome_debug/` 数据）。启动脚本支持 `--copy-user-data`（从源浏览器复制登录态）、`--no-copy`（默认，非交互）、`--browser-path`（显式指定浏览器路径，默认自动探测 Chrome/Edge/Brave/Vivaldi）、`--port` 和 `--user-data-dir`
   - **隔离**：调试浏览器用独立 `chrome_debug/` 用户数据目录（可在 config.toml `[browser] user_data_dir` 配置），**绝不影响用户的工作浏览器**
   - **禁止**：不要打开用户的工作浏览器；不要执行 `playwright install`（已装好）；所有浏览器操作必须 `connect_over_cdp("http://127.0.0.1:9222")` 连接已运行实例，禁止启动新浏览器
   - 详见 `docs/environment-constraints.md`「调试浏览器实例」章节

### 第 2 步：读取相关记忆（按需）

如果用户的第一条消息已经明确了任务（如"帮我记账"），则：
1. 读取对应 skill 的记忆（如 `memory_get("accounting")`）了解上次做到哪了
2. 读取 `preferences` 了解用户偏好（按需，轻量）

**不要一次性读取所有记忆**——只读取与当前任务相关的记忆，避免浪费上下文。

> 记忆系统完整 API 和架构详见 `docs/operations-manual.md` 的"记忆系统"章节和 `docs/memory-system.md`。

### 关于到期任务与待办（用户问时再查，不主动检查）

**不要在会话开始时主动调 `todos_due` / `wip_list` 检查到期任务或未完成工作。** 这些查询与用户当前任务可能无关，主动检查会污染上下文。

- 用户问"有什么任务"/"待办"/"上次没做完的" → 调 `agent_guide(task_type='system.task_reminder')` 获取指导
- 用户问"上次做到哪了"/"未完成的工作" → 调 `wip_list`（**默认已是轻量模式**，未完结条目给 id/title/status/priority/progress，已完结条目压成 id/截断标题/状态 + `closed=true`），需要详情再 `wip_get(id)` 单条或 `wip_get_many(ids=[...])` 批量；**不要为了看详情把 `summary` 关掉整表拉取**
- 用户确认要做某到期任务 → 按 task_reminder 指导执行，完成后调 `todos_mark_done`

**todo 类型**：`recurring`（周期任务，按 frequency 到期）、`phased_recurring`（阶段性周期，`[start_date, end_date]` 区间内递推，超出后 `archived`）、`triggered`（触发式，loop 轮询 `trigger_condition` 自动触发，不进 `todos_due`）。triggered 任务触发后，消息进入 `server/user_message.py` 的内存队列，在 agent **下次调用非排除端点时**被注入其响应（拉取式注入，**没有主动推送**——agent 不调工具就收不到），处理后 `todos_mark_done` 重置。

## 交互规范（agent 提问与文档存档）

### 提问/确认优先用 AskUserQuestion 工具

- agent 需要向用户提问、确认决策、征求选项时，**优先用 `AskUserQuestion` 工具**，不要用纯文本问完就结束对话
- 结束对话（turn end）后用户回答不便；用 AskUserQuestion 工具能在客户端渲染交互卡片，用户点选即可
- 多选项场景用 `multiSelect: true` 允许多选
- 每个问题附推荐答案：推荐项放第一位，label 末尾标注"（推荐）"
- 单次调用可批量 1-4 个问题，减少往返

### 对话延续性（提问即改进意图）

用户在对话中提出实质性问题时（非纯事实查询/闲聊），**默认假设有改进意图**——用户大概率想改进项目，而非只是问问。比如用户问"项目里 skill 入口决策树清晰吗"，大概率是想改进入口治理，而非单纯了解现状。

**规则**：
- 回答完问题后，**必须**用 `AskUserQuestion` 问下一步，固定提供两类选项：
  - ①「仅了解，暂不行动」—— 用户只是想搞清楚情况，保持现状不动
  - ②「继续推进改进」—— 附具体计划选项（如"出方案""开始实现""深入分析某模块"），保证对话不断
- **决策点必须在情况清晰后确认**，不在问答中途拍板——先给充分信息，再让用户决策
- 这保证对话不断：用户想停就停（选①），想继续有明确路径（选②）
- 触发范围：实质性提问都触发。纯事实查询（如"Python 版本是多少"）可不触发，但拿不准时宁可触发

### 功能规划文档存档路径（强制）

**任何功能相关文档（构想/计划/路线图/SDD 产物 spec/tickets/checklist）→ `temp/sdd/<feature-slug>/`；HTML 展示文件 → `temp/html/<feature-name>.html` 或子目录。**

**禁止**在项目根目录、`planning_notes/`、`docs/`、`workspace/`、`tools/`、`server/`、`client/` 创建 SDD 文档或 HTML 展示文件。⚠️ `planning_notes/` 仅允许用户手动存放规划/路线图文档，SDD 流程产物默认走 `temp/sdd/`。

> 路径规范、SDD 文件夹内文件组织表、与 SDD 流程的衔接、`planning_notes/` 现状、历史教训详见 `docs/dev-workflow.md` 的"功能规划文档存档路径"章节。

## 工作结束后的动作（任务完成/中断/会话结束前，必须执行）

**任务结束时不等用户说"收尾"，agent 主动调 `agent_guide(task_type='system.task_closure')` 获取收尾指引。** 这与开始时的 `agent_guide(task='...')` 对称——开始有 guide，结束也有 guide。

### 何时触发

- **任务完成**：目标达成，有代码/文档/数据产出
- **任务中断**：用户切换话题或会话结束前，工作未完成
- **无实质产出**：纯对话/简单查询 → 跳过收尾（避免无意义记忆）

### 收尾 6 步流程（task_closure skill）

1. **评估任务状态**：完成？中断？无实质产出？决定后续步骤集
2. **WIP 处理**：完成→`wip_update(status=completed)`；中断→`wip_create` 或 `wip_update` 留档（next_steps 必须可执行）。⚠️ **写操作不是直接 MCP 工具**：MCP 侧只有 `wip_get` / `wip_list` / `wip_get_many`，`wip_create` / `wip_update` / `wip_delete` 与 `todos_create` / `todos_update` / `todos_mark_done` 要走 `localagent_advanced_tool(tool="wip_create", params={...})`（REST `POST /wip`）。按 `mcp__localagent__wip_create` 调会直接 not found
3. **经验提炼 + 可消费性自检**：识别值得保留的经验（preference/project/reference），**必须填 `consumption_contexts`（哪些 task_type 应读取）+ `trigger_keywords`（任务描述出现什么词时读取）**，无法明确消费场景的候选跳过——避免"存了没人用"
4. **查重 + 写入**：`memory_list` 查重 → `memory_set` 写入结构化记忆。参数形状是**平铺**的：`memory_set(key=..., data={...}, merge=true, fact_type=..., consumption_contexts=[...], trigger_keywords=[...])`——`fact_type` / `consumption_contexts` / `trigger_keywords` 是**顶层字段**（写进 facts 表独立列供 SQL 过滤），不是塞在 `data` 里；同名时顶层覆盖 `data` 内的值
5. **文档自查（不询问用户）**：agent 自行对照 `.agents/rules/project_rules.md` 检查清单，检查本轮变更是否需要文档同步（路由注册/Pydantic模型/`/health`/硬编码/`_index.md`/`AGENTS.md`/`CHANGELOG.md`/`config.example.toml`/`tools_manifest.json`）。**ADR 评估（强制）**：本轮若有架构层变更或 SDD 产出 design-decisions，按 `project_rules.md` "ADR 评估"段三项标准打分，3/3 通过则写 ADR 并更新 `docs/adr/README.md` 索引（详见"关键约束 → ADR 维护"段）。完整 neat-freak 审查由用户主动触发。**若是浏览器操作任务**，额外检查"浏览器经验记录"（见 `docs/operations-manual.md` "浏览器操作经验记录"章节）：本次是否遇到非显然行为？若是，写入 `.agents/skills/browser_lessons/sites/<domain>.md`，无对应文件时按 `sites/_template.md` 创建，并同步更新 `.agents/skills/browser_lessons/references/site_index.md`
6. **收尾报告**：向用户报告 WIP 处理 + 经验提炼（含消费场景）+ 文档自查结果（含浏览器经验记录变更）

### 记忆消费闭环（核心约束）

写入记忆时必须填的两个字段，确保未来任务能消费：

```json
{
  "consumption_contexts": ["recurring.accounting", "adhoc.web_archive"],
  "trigger_keywords": ["退款", "账单对冲", "curl"]
}
```

- `consumption_contexts`：哪些 task_type 应读取此记忆（通用偏好用 `["*"]`，scope 通配用 `["recurring.*"]`）
- `trigger_keywords`：任务描述/用户消息出现这些词时应读取

下次调 `agent_guide(task_type='recurring.accounting')` 时，后端会自动返回所有 `consumption_contexts` 匹配的记忆清单，并在 `first_action` 前注入"【必读记忆】"指令——agent 不需要记得读哪些记忆，guide 会主动推送。

### 与现有 skill 的关系

| skill | 触发 | 职责 |
|-------|------|------|
| task_closure | agent 自主/用户说"收尾" | 编排入口，整合 WIP/经验/文档/报告 |
| memory_generation | 用户说"生成记忆" | 仅做经验提取+写入（可独立触发） |
| neat-freak | 用户说"整理文档" | 完整文档审查（跨文件一致性/监控面板/测试） |
| wip_tracker | 用户说"留档" | 仅做 WIP 留档/查询/恢复 |

> task_closure 详见 `.agents/skills/task_closure.md`。

## 关键约束

### 密钥统一管理（强制，2026-08-06 lib/secret 改造起）

**核心原则：禁止将密钥硬编码到代码或写入 config.toml，所有密钥读取必须经过 lib/secret 中转。**

- **LLM/VL/AIGC 密钥** → `data/llm/keys.json`，通过 `lib.secret.get_llm_keys_path()` 获取路径，`server/llm_pool/key_store.py` 统一加载
- **非 LLM 密钥**（tushare_token/github_token/example_token 等）→ `data/secret/secrets.toml` `[tokens]` 段，通过 `lib.secret.get_secret(key)` 读取
- **禁止**：硬编码密钥到代码、写入 config.toml、直接 `open()` 密钥文件、在日志/错误信息中回显密钥值
- **密钥恢复**：`tools/restore_secrets.py`（从备份找回密钥文件）
- 详见 `.agents/rules/project_rules.md` "密钥统一管理"段

### ADR 维护（架构决策记录，强制评估）

ADR 是项目架构决策的真源，位于 `docs/adr/`（索引见 `docs/adr/README.md`，覆盖 LLM 池/对话引擎/记忆系统/屏幕操控/审批/视觉/发布工具等主题域）。**agent 几乎不主动维护 ADR** 是历史问题，本节强制要求：

**何时评估 ADR**（三个触发点，均强制）：
1. **架构层变更时**：新增模块/重构边界/技术选型/移除模块/改变集成模式 → 收尾时按 `project_rules.md` 的"ADR 评估"段三项标准打分
2. **用户问"为什么这样设计"时**：先查 `docs/adr/README.md` 索引，若有对应 ADR 则引用并解释；若无 ADR 且决策符合三项标准，主动提议补 ADR
3. **SDD 流程产出 design-decisions 时**：SDD 的 `design-decisions.md` 是临时决策记录，收尾时评估其中是否有符合三项标准的决策值得提升为 ADR（SDD 进 gitignore 会丢失，ADR 是持久真源）

**三项标准**（详见 `.agents/skills/dev/domain-modeling/ADR-FORMAT.md`）：
- Hard to reverse（反转成本有意义）
- Surprising without context（未来读者会困惑）
- Real trade-off（确实有被拒绝的替代方案）

3/3 通过 → 写 ADR；2/3 → 边缘案例优先撤销或调整设计；<2/3 → CHANGELOG 足矣。

**写 ADR 流程**：扫 `docs/adr/` 最高编号递增一 → 按 ADR-FORMAT.md 模板 → 参考 0001/0002 风格 → 代码路径用 `file:///` 链接 → 在 `docs/adr/README.md` 索引追加一行。

**禁止行为**：
- ❌ 把 bug 修复/性能优化/可逆调整写成 ADR（CHANGELOG 足矣）
- ❌ SDD 的 design-decisions 写完不评估是否值得提升为 ADR
- ❌ 重大架构决策落地后不留 ADR，导致未来读者无法理解"为什么"

### 上下文压缩防护（长程任务硬规则）

长程任务（`dev.goal_engineering` 执行阶段、多 ticket 实现）极易触发上下文压缩。压缩后 agent 容易误判"压缩的历史是次要的，当前要求是主要的"，导致**重新拆任务或重写计划书**——这是严重错误，会丢弃已审核的方案和已完成的工作。

**真源层级**（从高到低）：
1. `temp/sdd/<slug>/spec.md` + `tickets.md` —— 方案与任务真源（最高权威，工作记忆，不进 git）
2. 压缩历史中的用户明确要求 —— 用户意图权威
3. 压缩历史中的 agent 中间产物 —— 可重建，非权威

**硬规则**：
- 上下文压缩后，agent 醒来**第一步必须读** `temp/sdd/<slug>/spec.md` 和 `tickets.md`，对照当前 ticket 状态续跑
- **禁止**因压缩而重新拆任务（重写 `tickets.md`）或重写方案（重写 `spec.md`），除非用户明确要求"重新规划"
- 执行阶段遇到不确定，读 spec 的 Anti-Cheat 节和 Bounds 节自行决策，**不要回头问用户**（执行阶段不再询问是硬规则，见 `dev.goal_engineering`）
- 每完成一个 ticket，**立刻更新** `tickets.md` 的 Status 字段（ready→in_progress→completed），作为压缩后的恢复锚点
- 拿不准的写进 `temp/sdd/<slug>/BLOCKED.md`，跳过继续做别的，最后随交付提交

### 文档防过时规范（写/改任何文档前必读，2026-09-16 集中整改教训）

本项目曾集中出现"文档说的和代码不一致"（面板数、schema 版本、fail-open/fail-closed 方向、DoomLoop 重试行为、健康检查间隔等十几处），整改后固化以下规则，**新写或修改任何 `.md` / docstring 都适用**：

1. **禁写易漂移数字**：不要写"X 个面板 / X 个工具 / X 个 ADR / X 条风险 / schema vN / 每 N 天"这类随迭代必然变化的值。替代方案：① 指向代码常量（如 `SCHEMA_VERSION`、`HEALTH_CHECK_INTERVAL_DAYS`、`_SCHEMA_VERSION`）；② 指向运行时清单（`/docs`、`GET /advanced/tools`、`PanelRegistry` 发现结果、目录实际文件）；③ 定性描述（"按 main/monitor/advanced 分组的多面板"）。例外：事实性配置值（如 tier 区间、表格里的必要阈值）可写，但**必须对照当前代码实测后**再写。
2. **每个行为论断锚定代码路径**：描述结构/行为时给出对应 `.py` 文件，读者能自行验证；引用其他文件/文档前先确认存在（`ls`/`Test-Path`）；移动或删除文件时全仓 grep 引用并同步清理（walkthroughs.md 死链接教训）。
3. **行为重构必须同步所有描述处**：改掉某个旧行为时（如"重试改不重试"），同一变更内 grep 旧行为关键词，把 docstring、`docs/*.md`、config 注释全部同步（DoomLoop B1 重构漏改 docstring 教训）。docstring 描述旧行为 = 未来的人被带偏。
4. **数字冷静期**：写"当前是 X"之前先实测（import 常量 / 数文件 / 跑发现逻辑），不照抄其他文档的数字；发现两份文档冲突时以代码为准，顺手把另一份也修了。
5. **rules 双镜像纪律**：改 `.agents/rules/project_rules.md` 与 `.trae/rules/project_rules.md` 必须双向同步，改完 `diff` 确认一致（出现过单侧修复、另一侧留旧值的情况）。
6. **三种必删的废话（写文档/注释时主动警惕）**：
   - **「原 X 已删除/已改掉 vN」式考古**：东西已从代码里删掉，留一句"原来是什么"只占篇幅。**只有"不写就会让读者去找/去用已消失的东西"时才写**（如旧路径仍被外部广泛引用、旧行为照抄会踩坑）；其余直接删，只描述现状。
   - **易漂移的统计数字**：见第 1 条。"有 13 个文件""共 6 个面板"每次迭代都变，写了必然过时。改为指向代码常量 / 运行时清单 / 定性描述。
   - **事无巨细的细节**：只在**本就该写细节**（ADR 的 trade-off、算法判据、表格里的必要阈值）或**细节非常值得**（防复发的一次性教训）时展开；否则压成"结论 + 代码路径锚点"，让读者按需读代码。默认假设"能删一半"。

### API 常见陷阱

> 其它陷阱详见 `docs/operations-manual.md` 的"API 常见陷阱"章节。

- **PowerShell 中 `curl` 是 `Invoke-WebRequest` 的别名（重要！）**：在 PowerShell 终端运行 `curl -s http://127.0.0.1:8766/health` 会触发 `Invoke-WebRequest`，它把 `-s` 解析为 `-Session`，然后等待用户输入 `Uri:` 参数，导致终端卡住，必须 Ctrl+C 才能中断。**避免方法**：
  - 用 `curl.exe` 强制调用真正的 curl：`curl.exe -s http://127.0.0.1:8766/health`
  - 或用 `Invoke-RestMethod`：`Invoke-RestMethod -Uri http://127.0.0.1:8766/health`
  - **绝对不要**在 PowerShell 中直接用 `curl -s ...`，会卡住终端
- **PowerShell 不支持 `&&` 串联命令语句（反复踩坑！）**：Windows PowerShell 5.1 不支持 `&&`（管道链运算符），用 `cmd1 && cmd2` 会报 `The token '&&' is not a valid statement separator`。**避免方法**：
  - 用 `;` 顺序执行（不管前一个是否成功）：`cmd1 ; cmd2`
  - 用 `$LASTEXITCODE` 显式判断：`cmd1 ; if ($LASTEXITCODE -eq 0) { cmd2 }`
  - 或改用 PowerShell 7（`pwsh.exe`，支持 `&&` 和 `||`）
  - **Shell 工具链式命令时不要用 `&&`**，用 `;` 或拆成多次调用
- **PowerShell 5.1 写 UTF-8 文件必带 BOM（跨平台坑，agent 写文件必读！）**：`Set-Content -Encoding UTF8` / `Out-File -Encoding UTF8` 在 Windows PowerShell 5.1 下写出的文件**必带 BOM**（U+FEFF），多次写会**累积**（yihuan_clean.py 曾堆 3 个 BOM 直接 `SyntaxError`；早期误归因"harness 平台写入叠加"，对照实验已推翻）。**避免方法**：
  - agent 写/改文件**优先用 IDE 的 Write/Edit 工具**（Trae 已对照实验验证不加 BOM；其他平台同理），不要走 shell 重定向
  - 必须 PowerShell 写 UTF-8 时用 `[IO.File]::WriteAllText($path, $content)`（无 BOM）；PS 7 的 `-Encoding utf8` 也不带 BOM，但项目终端默认 PS 5.1，别赌版本
  - 遇 `SyntaxError: invalid non-printable character U+FEFF` → `uv run python tools/clean_bom.py --fix`（默认 dry-run 只扫描）
- **写/改 .bat/.cmd 必须遵循 bat_writing skill（含中文的批处理反复"写了跑不了"，强制！）**：中文 Windows（ACP=936）下 bat 的铁律是 **GBK 编码 + CRLF 行尾 + 无 BOM**，任意一条破坏就以"编码问题"的方式炸（最常见：LF-only 行尾让 cmd 按字符数错位定位，报错全是半截汉字）。任何新建/修改 bat、排查"bat 跑不了/乱码/不是内部或外部命令"的任务，**先读 `.agents/skills/bat_writing/SKILL.md`**；写完/改完必须跑 `uv run python .agents/skills/bat_writing/scripts/fix_bat_encoding.py <文件> --check` 自检，全 OK 才算完成，不过则 `--inplace` 修复后复检；交付前实际运行一次（只在 IDE 里看过源码不算验证）
- **Git 命令默认走 pager 会卡住终端（agent 自动执行必读！）**：`git log` / `git diff` / `git show` 等命令在 Windows 上默认使用 `less` 作为 pager，输出超过一屏时进入分页模式，等待用户按键（空格/q/回车）才显示后续或退出。在 agent 自动执行场景下命令会一直挂起，直到被用户手动跳过或 Ctrl+C，外观像"命令在运行但没输出"。**避免方法**：
  - 用 `git --no-pager log ...` / `git --no-pager diff ...` 强制单次关闭 pager（推荐，最稳）
  - 或显式重定向 pager：`git -c core.pager=cat log ...`（输出直送 cat，不暂停）
  - 或限制输出长度：`git log -n <N>`、`git diff --stat`、`git diff <ref>~1 <ref> -- <path>`
  - 全局禁用 pager：`git config --global core.pager cat`（影响所有仓库，需用户授权，不要擅自改全局配置）
  - **绝对不要**在 agent 自动执行场景下直接用 `git log` / `git diff` 不加 `--no-pager` 或长度限制，会卡住终端
- **不要根据文档注释判断文件是否存在（反复踩坑！）**：AGENTS.md / _index.md / skill 文件中的注释可能过时（如"XX 尚未创建"、"XX 在 wip 待办中"），直接采信会导致错误结论。**避免方法**：
  - 文件是否存在 → 用 `Read` / `Glob` / `Test-Path` 实际验证，不要信文档注释
  - 文件内容 → 用 `Read` 实际读取，不要根据文档描述推断
  - 接口是否存在 → 用 `curl.exe` / `Invoke-RestMethod` 实际调用验证
  - **典型案例**：AGENTS.md 曾写"CHANGELOG.md 尚未创建"，但实际文件已存在（40KB），agent 没读就下结论导致 CHANGELOG 漏更新
- **禁止大量返回 Base64 污染上下文（强制规则）**：所有代码、脚本、MCP 工具调用都不得返回大段 base64 数据（截图、文件、音频等）到 LLM 上下文。遇到 base64 立即改代码或调用方式：
  - 截图给多模态 LLM：用 `capture_screen(format="inline")`（返回 ImageContent，由 MCP 协议处理，不进文本上下文）
  - **浏览器截图**：`browser_screenshot` 默认已改为只返回 `path`（`return_base64=False`）。要看图就把 path 交给 `understand_image` / `ocr_path`；**不要**为了省事传 `return_base64=True`——2026-09-21 曾因网关只认顶层/`result` 两种形状、而本端点把标记放在 `data` 下，一次调用往上下文塞了 181424 字符
  - 截图特征分析：用 `screen_analyze`（返回纯文本的颜色/亮度/异常分析，无 base64）
  - 截图+OCR：优先 `localagent_advanced_tool(tool="screen_ocr", params={...})`，直接返回文字和 bbox，不经过 base64
  - 文件读取：用 `docviewer_read` 或 `exec_python` 提取文本，绝不返回 base64
  - **违反此规则会导致上下文窗口被数 MB 的 base64 字符串撑爆，LLM 无法继续工作**
- **截图+OCR 的正确流程（重要！）**：优先调用高级工具 `screen_ocr`，一步截图并返回 `text + details[].box`，不返回图片数据：
  ```
  localagent_advanced_tool(
      tool="screen_ocr",
      params={"mode":"window", "hwnd":123, "engine":"ocr"}
  )
  ```
  需要已有图片文件时用 `ocr_file` / `ocr_path`。需要把截图交给其他工具时可用 `capture_screen(format="path")` 获取临时路径，再传给 `ocr_path`。**绝对不要**直接调用 `capture_screen(format="base64")` 把数 MB base64 返回上下文。
- **坐标参数必须整数**：`/screen/action` 的 `x`/`y` 传浮点会 422；Pydantic 验证失败返回 422，`detail` 数组含 `loc` + `msg`

### IDE 平台使用注意

本项目同时支持 **CatPaw** 和 **Trae** 两个 IDE；各客户端的接入配置、桥接方式与排查顺序见 `docs/dev-workflow.md` 的「MCP 接入」章节。

**CatPaw 注意事项**：
- **语义搜索优先**：CatPaw 的 `codebase_search` 工具支持语义搜索，优先使用语义搜索探索代码库
- **工具调用并行化**：CatPaw 支持在单次回复中并行调用多个工具，尽量批量调用以提高效率
- **Glob 搜索用绝对路径**：搜索 gitignore 目录下的文件时，使用绝对路径作为 `target_directory` 参数

**Trae 注意事项**：
- **Glob 工具在 gitignore 目录下需用绝对路径**：`workspace/` 下含敏感数据的子目录、`temp/` 等被 gitignore 的目录，用相对路径会找不到文件，需要用绝对路径
- **LS 和 Grep 不受此限制**：LS 和 Grep 用绝对路径即可正常访问 gitignore 目录

### 临时文件规则
- **所有临时脚本必须写到 `temp/` 目录**，不要在项目根目录、`tools/`、`server/` 或其他位置创建临时文件
- 临时文件命名建议带前缀或时间戳，如 `temp/debug_ocr_test.py`、`temp/20260614_scan.py`
- **临时脚本必须写到 `temp/` 目录**这一条不等于"`temp/` 整体被 gitignore"：`.gitignore` 用 `temp/*` + 按名 `!` 豁免，归档目录（如 `temp/planning_archive/`）**是随仓库跟踪的**，豁免清单以 `.gitignore` 为准。判断某产物在不在库里用 `git ls-files temp/<子路径>`（`docs/dev-workflow.md`「与 SDD 流程的衔接」同理）
- `exec_python` 工具内部会写 `temp/exec_<uuid>.py` 起后台子进程执行（统一 terminal 机制，详见 `docs/mcp-reference.md`），手动写脚本仍应放在 `temp/`
- 任务完成后应清理不再需要的临时文件

### 磁盘清理规则（强制！）

**任何形式的磁盘清理——包括但不限于删除模型缓存、pip/uv 缓存、临时文件、旧环境、下载残留——必须先拉取清单给用户审核，获得明确同意后才可执行。**

1. **清理前**：列出所有待删除文件的完整路径和大小，生成清单表格
2. **审核**：将清单提交给用户，等待用户逐项确认或整体批准
3. **执行**：只删除用户确认的文件，删除完成后报告释放的空间
4. **例外**：Agent 自己临时创建的脚本文件（`temp/` 目录下的临时脚本）无需审核，可直接删除

**删除执行纪律（回收站，禁止物理删除）**：任何"可恢复删除"一律走项目自带的回收站工具
`tools/disk/recycle.py`（`uv run python tools/disk/recycle.py <path>...` 或库用法
`from tools.disk.recycle import send_to_recycle`）。**禁止 `rm`/`del` 物理删除用户文件**。
为什么必须用它：本机 PowerShell 的 `Add-Type` 与 `Shell.Application` COM 均被 WorkBuddy
安全策略拦截（2026-09-03 实测，`Microsoft.VisualBasic.FileIO` SendToRecycleBin 与 COM
两条常规路线都不可用），recycle.py 用纯 ctypes 调 Win32 `SHFileOperationW`（`FOF_ALLOWUNDO`）
实现，是本机唯一可靠的回收站路线。**先翻 `tools/README.md` 找现成工具，不要自己 pip
install 绕路**（2026-09-03 教训：为删测试垃圾先装 send2trash 被用户纠正）。

**回收站删目录失败怎么定位**（2026-09-17 实测）：`SHFileOperationW` 删目录树时聚合错误码
`124`（`ERROR_INVALID_LEVEL`）不含信息量，必须**对子项逐个 `send_to_recycle`** 才能拿到真实原因——
`32`（`ERROR_SHARING_VIOLATION`）意味着有进程把该目录当 cwd 持有（此时子文件全部可写、单独删除都能成功，
容易误判成"路径权限问题"）。找持有者：遍历 `psutil.process_iter()` 取 `cwd()`（本机约半数进程无权限读取，
静默跳过即可），比对目标路径。**锁定属于其他产品会话进程时先报告给用户，不要擅自 kill**；
也**绝不因为回收站这条路受阻就退回物理删除**。

**违反此规则可能导致用户重要数据/模型被误删，是不可接受的错误。**

### 输出截断规则
- `/exec/python` 返回的 stdout/stderr 超过 8000 字符时自动截断，保留头部+尾部
- 截断时返回 `exec_id`、`stdout_truncated=true`、`stdout_total_chars` 等字段
- **查看完整输出（exec_python）**：走终端日志——`exec_terminal_detail`（tail 截取）或 `GET /terminal/{tid}/output` 游标读取
- **`POST /output/{exec_id}`（full/range/search）仅覆盖 `exec_cmd`** 的一次性输出缓冲；缓冲区保留最近少量执行输出，超出自动淘汰

> 长时间运行命令的终端会话 API 详见 `docs/operations-manual.md` 的"终端会话 API"章节。

## 项目结构

项目分一二级目录，**完整映射由程序维护**，避免静态文档过时：

- **后端**：`server/`（FastAPI + MCP，模块详见 `_index.md` 后端模块表）
- **GUI 客户端**：`client/`（PySide6 面板式架构：主窗口内置面板 + workspace manifest 声明的组件面板，**数量与清单由 PanelRegistry 扫描 `client/panels/` 得到，不在这里写死**；另有独立进程审批面板 `client/approval_panel/`（`python -m client.approval_panel`，托盘驻留，不在主窗口侧边栏）；`client/core/agent/` 是 v6-lite 对话引擎的**纯 Python 核心库，不依赖 Qt**，ChatPanel 经 SessionFacade 通过 QThread 调引擎。引擎与 chat-panel-v2 的实现细节见 `docs/chat-engine.md`，UI 规范见 `docs/ui/`）
- **Skill/规则/文档**：`.agents/`（`skills/` skill 定义、`wip/` 任务留档、`rules/` 规则文件；**无 documents/ 子目录**，文档走 `docs/`）
- **通用工具脚本**：`tools/`（browser/debug/deploy/llm/file_classifier 等，详见 `tools/README.md`；**任务专属脚本写到 `workspace/<task>/`，不写到 `tools/`**）
- **任务工作区**：`workspace/`（按任务分目录，**子目录数量随工作增减，实际清单看 `workspace/` 目录本身或 `data/project_structure.json`**；新建子目录**按需**——`SKILL.md` + `manifest.toml` + `loop_actions.py` 三件套不是强制要求，临时/一次性脚本可省；常驻/loop 任务建议有 `manifest.toml` + `loop_actions.py` 让 agent_guide 路由）。**特殊子目录**：`dev_toolkit/`（独立 dev toolkit 项目，有自己的 `.agents/` `specs/` `wip/`，写入前看其 `AGENTS.md`）、`maa-patch/`（自写文件 `MAINTENANCE.md`/`LESSONS.md`/`mumu-keepalive.patch`/`sync-maa-patch.ps1`/`UPSTREAM_ISSUE_RECORD.md` 进仓库（白名单见 .gitignore），`maa-upstream/` 是上游 MAA 仓库副本不碰）
- **二级文档**：`docs/`（mcp-reference/tools-guide/memory-system/llm-pool/environment-constraints/operations-manual/dev-workflow + `adr/` 架构决策记录 + `ui/` UI 设计系统文档）
- **运行时数据**：`data/`（LLM池/记忆DB/活动追踪/inbox，整体 gitignore，仅 `project_structure.json` 进仓库）
- **测试**：`tests/`（`fixtures/` 测试夹具；危险操作测试必须 mock，详见 `project_rules.md` 测试铁律）
- **临时脚本与 SDD/HTML 产物**：`temp/`（`exec 临时脚本`、`temp/sdd/<slug>/` SDD 流程产物、`temp/html/<name>/` HTML 展示文件、`temp/planning_archive/` 历史归档；**所有临时脚本必须写这里，不写根目录/`tools/`/`server/`**）。⚠️ `temp/` **不是"整体 gitignore"**：`.gitignore` 用 `temp/*` + 按名 `!` 豁免（`planning_archive/`、`html/`、`architecture_analysis/` 随仓库跟踪；`temp/sdd/` 被豁免之后又按名重新挡掉，故 SDD 当轮产物不进 git、但归档里的旧副本在）。**豁免清单以 `.gitignore` 为准，别在这里抄条目数**；判断某文件在不在库里用 `git ls-files temp/<子路径>`，不要凭"temp 被忽略"下结论。

### 其他一级目录（按类别分组）

**配置 / CI**：
- `.dcg/` + `.dcg.toml` — dcg（destructive command guard）危险命令拦截规则包；加新危险命令模式时改 `.dcg/packs/localagent.yaml`
- `scripts/hooks/` — **git 钩子（已启用，`core.hooksPath` 指向这里）**：pre-commit 跑 `tools/check_hard_rules.py --staged`，提交前强制检查硬化规则（BOM / 硬编码密钥 / 文档路径越界 / import 边界 / MCP 工具名 / 死链），违反即拦截提交；配套 `tests/test_hard_rules.py` 与 `tests/test_hard_rules_dead_refs.py` 全量层（C5 另有直连/反例用例，C6 另有豁免/基线接缝用例）。改钩子必须保持 LF 行尾（见 .gitattributes）
- `.github/workflows/` — GitHub Actions。`release-public.yml` 目前**不产生任何 CI 运行**：它是 `workflow_dispatch` 手动触发、文件头注明依赖条件不成立（需 profile + Ubuntu 装不上 win 依赖），且被排除出发布包 → `publish` 推出去的公开仓里没有 `.github/`。所以发布物由本机 `tools/release/cli.py build` 产出；CI 流程变化时改这里
- `.trae/` — Trae IDE 配置。`rules/project_rules.md` 是 `.agents/rules/project_rules.md` 的镜像（前者改了同步后者，反之亦然）；`documents/` 是设计/规划文档（如 `content_aware_file_classification.md`、`mcp-response-truncation-plan.md`），定位类似 `planning_notes/`，新文档由用户决定是否纳入

**代码共享库**：
- `lib/` — 共享库。`lib/ui/`（UI 设计系统：tokens/theme/controls/icons，GUI 代码 import 它，**不要在 `client/` 重复造组件**）、`lib/recorder/`（录制器核心：L0 sensors + L1 processor + L2 timeline + L3 editor，**与项目共用的代码**）、`lib/uia.py`（仅录制器键盘传感器用的焦点控件值快照；**UIA 语义层在 `server/screen/uia.py`，不要在这里找**）
- `references/` — 参考项目（destructive_command_guard/neko-review/opencode 等，整体 gitignore；clone 的第三方代码不进仓库）

**录制器归属**（模块化分层）：
- `lib/recorder/` — 与项目共用的核心代码（sensors/processor/timeline/editor/consumer，**与项目共用的代码**）
- `workspace/recorder/` — 录制器的入口/工具/录制包（入口 `python -m workspace.recorder.tools.main`；`consumer/` agent 公用库；`recordings/` 大体积本地数据 gitignore）

**发布 / 分发**：
- `release/` — 源码分发。`profiles/`（发布配置 toml）、`audience/`（受众定义）、`plans/` `dist/` `staging/`（构建产物，gitignore 仅留 .gitkeep）+ `policy.toml`；通过 `tools/release/cli.py prepare/compute-digest/build` 操作，不手动改 `dist/` `staging/`。依赖映射表/审计报告（`dependency_map.toml` / `dependency_audit.json`）由 `tools/release/generate_dependency_map.py` 生成到 `temp/release_deps/`，**不入库也不进发布包**（产物含未进包的私有模块路径，且引擎无消费方）

**私有 / 运行时（gitignore，不进仓库不进 release）**：
- `private_vault/` — 私有文档仓库（obsidian vault）。子目录：`life_design/`（人生设计对话存档+蓝图）、`accounting/`（账单核对产出）、`stock_advisor/`（持仓配置+收盘报告）、`disk_manager/`（磁盘清理评估）、`activity/daily/`（每日工作总结）。agent 运行时读写私有产出
- `chrome_debug/` — 调试浏览器用户数据目录（CDP 9222，名字沿用历史默认；路径可在 config.toml `[browser] user_data_dir` 配置）；运行时生成，**绝不影响用户的工作浏览器**
- `config.toml` — 实际配置（含密钥）；运行时由用户填充，`config.example.toml` 是模板
- `temp/` — 见上方
- `data/*` — 见上方（除 `project_structure.json`）

**规划笔记**：
- `planning_notes/` — 用户手动存放的规划/路线图与历史设计文档（含历史设计文档系列、公开发布、外部文章与提示词参考等），按主题分子目录。**仅用户手动存放**；SDD 流程产物走 `temp/sdd/`，不写这里

**展示与归档文档**：
- `展示文档/` — 架构分析长文与可视化展示（`architecture_analysis/` 下按主题拆分的多篇专题、`architecture_showcase.html`、`walkthroughs.md`、`code-knowledge-graph.md`），随仓库跟踪。定位是给架构读者的展示材料，与 `docs/`（agent/维护者手册）分工不同；新展示产物仍按规则写 `temp/html/`，不往这里加

**用户专用文件**（agent 禁止读写）：
- `USER_ONLY_PROJECT_OPERATIONS_AGENT_DO_NOT_READ_OR_EDIT.md` — 用户专用操作手册；agent 仅为完成任务时无需读取，禁止修改/删除/绕过其指明的 `.dcg.toml`、`.dcg/`、`config.toml`、`workspace/` 用户数据

**根目录其他文件**：
- `todo.txt` — 根目录的旧待办笔记（非项目内维护的产物）
- `start.bat` / `start_client.bat` — 后端启动脚本（含 UAC 提权+杀端口）/ GUI 启动脚本
- `tools_manifest.json` — 工具清单（GUI 自动读取）
- `LICENSE` / `NOTICE.md` — Apache-2.0 版权 + 版权声明
- `SECURITY-RISKS.md` — 已知安全风险登记
- `CHANGELOG.md` — 变更日志
- `.gitignore` / `.python-version` / `pyproject.toml` / `uv.lock` — 标准 Python 项目文件

### baseline 与漂移检测注意事项

- **Glob 工具受 gitignore 过滤**：`Glob` / `LS` 看不到 gitignored 文件/目录（如 `chrome_debug/` `config.toml` `private_vault/` `temp/` `USER_ONLY_*.md` `file_sample.json` `.venv/`），验证存在性时用 PowerShell `Test-Path` 或 `Get-ChildItem -Force`

### 程序化映射与漂移检测

完整的一二级目录映射存在 `data/project_structure.json`（baseline），由 `server/project_structure.py` 维护。

- **会话开始需要目录布局**：调 `agent_guide(task='...', include_structure=true)`，响应中 `project_structure` 字段含当前扫描 + baseline 描述
- **task_closure 自动检测漂移**：调 `agent_guide(task_type='system.task_closure')` 时响应自动含 `structure_diff` 字段（unknown_paths / missing_paths），agent 在收尾报告中提示用户并按需补全 baseline
- **手动补全 baseline**：`exec_python` 调 `server.project_structure.sync_baseline(add_descriptions={"新路径/": "描述"})`（幂等，一级/二级目录与已失效路径一次归位）。⚠️ `update_baseline_descriptions()` 只写 `top_level`，用它补二级目录**静默无效**
- **/health 查看状态**：`project_structure.baseline_exists` / `baseline_entries` / `baseline_updated`

## Skill 编写

**先判断该不该建，再谈怎么写。默认不建**——只有**同时**满足下面两条，才值得沉淀成 skill：

1. **高频**：这类任务会反复发生，不是一次性的。
2. **有坑**：是没有指导就会反复踩坑、试错多次才走通的经验。一次就成功、照着通用知识就能做的，不记。

只满足一条、或两条都不满足 → 记到 `docs/` 专题文档或 `.workbuddy/memory/YYYY-MM-DD.md` 日志，或干脆留在对话里，**不要建 skill**。已有 skill 能覆盖 → 先路由到它，不重复造轮子（`agent_guide(task='...')` 或 `.agents/skills/_index.md`）。

Skill 编写规范（目录结构、frontmatter、同步清单）见 `.agents/skills/skill-creator.md`，创建新功能后的必做清单见 `.agents/rules/project_rules.md`（Trae 镜像副本在 `.trae/rules/project_rules.md`）。

## 测试

**测试运行入口（禁止乱调用，详见 `docs/dev-workflow.md` "测试运行入口" 段）**：

- **一次性收集全部失败**（推荐）：`uv run python tools/run_tests_collect.py`
- **分层跑**（quick 排除外部资源 / full 全跑）：`uv run python tests/run_all.py --quick`
- **发版全量**（按目录分批、慢目录 2 路并行、真动桌面的批走独占段、批间内存护栏）：`uv run python tools/run_tests_batched.py`
- **单文件调试**：`uv run python -m pytest tests/test_xxx.py -v --tb=short`

前三个入口参数已统一（pyproject.toml `addopts` + `tools/test_runner.py` 共享逻辑），
产出 `temp/test_full_log.txt` + `temp/test_results.xml` + `temp/test_failure_report.md`；
分批器每批一个独立 pytest 进程，产出在 `temp/batch_tests/`（`SUMMARY.md` + `mem_min.log`
内存采样；每批 `.log`/`.xml` **只在该批非零时保留**，全绿跑完不堆中间产物），实测口径见
`docs/dev-workflow.md` "发版全量为什么用分批器"。

**禁止**：
- ❌ `pytest tests/ -v`（全量刷屏，看不到失败摘要）
- ❌ 单进程跑整套发版全量（本机 RSS 会到 11-12 GB，被系统保护打断）——用分批器
- ❌ 不加 `-m` 排除外部资源跑全量（会跑 E2E，需 CDP:9222 + 后端:8766）
- ❌ 手动拼 `--tb`/`--maxfail` 等参数（addopts 已配，脚本层按需覆盖）

测试铁律（危险操作 mock / 测试修复 / 隔离处理）见 `docs/dev-workflow.md` 对应章节。
