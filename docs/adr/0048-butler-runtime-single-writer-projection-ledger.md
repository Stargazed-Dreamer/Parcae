# 对话循环迁出独立 butler runtime：单一写者 + 版本化账本 + 读侧投影

状态：accepted（2026-10-22，代码在 `lib/agent_runtime/`、`lib/port_binding.py`、
`start_butler_runtime.bat`；桌面侧在 `client/panels/chat.py` 与 `client/core/runtime_store.py`）

## Context

v6-lite 的对话循环住在 GUI 进程里（`client/core/agent/`，由 `_ChatWorker(QThread)` 持有），
带来三个结构性后果：

- **关前端即断会话**：`closeEvent` 只能中断 runner，长任务无法在无人看窗口时跑完。
- **依赖倒挂**：8766 后端的 headless / probe 要反向 import 前端循环库（`server → client`），
  检查脚本里靠一张 `IMPORT_ALLOW_SERVER_TO_CLIENT` 白名单兜着。
- **账本没有代数和边界**：`_SCHEMA_VERSION=9` 只写不读、迁移全是幂等 `ALTER`，没有可判定的
  格式版本 —— 升级只能"尽力打开"，旧数据未来还能不能读全靠运气；事件是自由 JSON，读侧重放
  会把流式增量一起吐出来；UI 每 200ms 轮询全量事件并临时聚合；同一个 SQLite 谁都能写。

第一切片的目标不是做完整管家，而是把地基换成：**循环有独立宿主、账本有代数、读写有边界**。

## Decision

1. **宿主独立**：循环跑在单独的 uvicorn 进程（`python -m lib.agent_runtime.server`），首选
   `127.0.0.1:18766`。端口复用项目 `bind_first_available`（先绑 socket 再交给 uvicorn，避免
   TOCTOU）；顺延后把 preferred/actual/pid/status 写进**独立声明** `data/runtime/butler_runtime.json`，
   绝不复用 8766 的 `server_port.json`（10-20 已被第二实例覆盖出过事故）。客户端只读声明 +
   `/health` 探活，不写死端口。
2. **常驻契约**：`start_butler_runtime.bat` 手动拉起，可选 `--supervise`（子进程非零退出按
   1s→2s→5s→30s 退避重启；退出码 2 视为致命错误不再重启；正常退出 0 不重启）。已有健康
   runtime 时第二次启动直接退出 0。仅绑回环，Host 不是 `127.0.0.1:<actual>` / `localhost:<actual>`
   一律 421（WS 同校验）。**开机自启本切片不做**：本机自启链路已实证不可靠，先用手动脚本把
   常驻契约跑通。
3. **代码归位 `lib/agent_runtime/`**：循环库不依赖 Qt，也不依赖 `server`；headless / probe /
   桌面面板统统退化为 runtime 客户端。`tools/check_hard_rules.py` 的 `server→client` 白名单清零，
   依赖方向变成 `client/server → lib.agent_runtime → lib`。
4. **账本格式版本与建表版本正交**：新增 `ledger_format_version`（存 `schema_info`），
   `_SCHEMA_VERSION` 只管建表。现存库标记 v1，本切片目标 v2；迁移链登记在有序表，启动时校验
   **相邻完备**，逐代单独事务执行。迁移步骤只许 `CREATE TABLE/INDEX`、`ALTER TABLE ADD COLUMN`
   （带常量默认）与回填，**不得重写或删除既有行**。`format_version > code`、版本缺失无法判定、
   链不完整 —— 一律**拒绝启动**（退出 2），不许"尽力打开"。
5. **三层事件域**：`events.domain` ∈ `persistent` / `live` / `capability`，类型到域的映射只在
   单一注册表（未登记一律 `persistent`）。`load_events` / `GET events` 默认只给
   `persistent + capability`，`include_live=true` 才带流式增量；WS 实时订阅默认三类都推。
   `live` 仍落库（排障用），但默认不重放；TTL 留给后续切片。
6. **单一写者 + 读侧投影**：只有 runtime 写账本；`append` 事务内同步更新三张投影
   `proj_stats` / `proj_turn_outline` / `proj_title`。所有列表/统计/标题/轮次摘要只走投影，
   不再在 UI 侧临时聚合。投影缺失时读路径**明确报错**，不给一份看起来正常的错数；
   `POST /admin/projections/rebuild` 与 CLI 都能从账本重放重建，重复执行结果一致。
7. **桌面端 = runtime 客户端**：WS 按 `after_seq` 游标增量收事件（断线重连续游标），HTTP 负责
   提交/引导/中断/审批/答题与元数据写。关闭前端只**脱钩**（断开 WS、停本地 worker），不再
   中断 runtime 会话。`ask_user` 改为 runtime 发 `ask_user_request` 事件、面板回
   `POST /ask_user/{rid}`；**ADR 0046 的同步阻塞桥实现由此取代**，但其中两条结论继续有效：
   等待时长只归 `[runner] ask_user_timeout_secs`（agent 无权传），"没回答"必须区别于"反对"。

## Considered Options

- **只给进程内循环加持久化**（保留 QThread，只补账本版本）：被拒。关前端仍然断会话，多前端
  共享同一会话无从谈起，写者仍是"谁都能写"。持久化救得了崩溃恢复，救不了宿主生命周期。
- **把 runtime 并进 8766 后端**：被拒。8766 是工具箱后端，改工具/配置就要重启；把长任务
  绑在它上面等于换个理由杀会话。而且 8766 已有自己的端口声明语义，混用就是 10-20 那次覆盖
  事故的复现路径。
- **只开 WebSocket**：被拒。提交、答题、元数据写需要请求-响应与幂等重试语义，WS 只适合
  事件推送；两者混在一路会让"重连后哪些写已经生效"变得不可判定。
- **继续用 `_SCHEMA_VERSION` 当迁移依据**：被拒。它只写不读、没有版本分派，且历史迁移是
  幂等 `ALTER`，无法回答"这个库比代码新还是旧"。代数必须独立声明才能拒绝降级。
- **投影异步更新**：被拒（本切片）。异步会引入"账本已写、投影滞后"的窗口和额外失败路径；
  单一写者下同步更新已经足够简单，投影坏了还有"清空 + 重放"这条确定性修复路。
- **开机自启（计划任务/启动目录）**：推迟。本机 AutoStartManager 已实证不可靠；先验收手动
  脚本 + supervisor 的常驻契约，自启作为远景单独立项。

## Consequences

- 多一个常驻进程与一份声明文件：客户端必须先解析声明再探活，端口不是常量；声明丢失时按
  配置回退（已知残留）。
- 前端关闭不再等于"停止任务" —— 这是有意的语义变化，旧用户预期需要改；代价是失控会话只能
  显式调 `POST /interrupt`。
- 无客户端在线时 `ask_user` 直接 `no_host`（fail-closed），带审批/提问的长任务需要保持前端
  或 headless 订阅在线。
- 单一写者把所有写操作串行化；吞吐上限取决于 runtime 单进程，换来的是没有跨进程写冲突。
- 投影是缓存不是真源：坏了的修复动作是重建，而不是手工改表。
- T4/T7 的 9 项人工操作（重启、杀进程自愈、关前端不中断等）仍待用户在场签字；自动化验收
  已全部通过。
