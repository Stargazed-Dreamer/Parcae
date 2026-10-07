# ask_user 用「worker 同步阻塞 + 主线程卡片」桥；等待时长只归配置，中断必须绕过 worker 事件循环

状态：accepted（2026-10-07，代码在 `client/core/ask_user_bridge.py`、
`client/core/agent/builtin_tools/ask_user.py`、`client/panels/chat.py` 的 `_AskUserBlock`）

## Context

`ask_user` 工具自 v6-lite 起就在清单里（[ADR 0010](0010-tool-list-four-categories.md)），但 GUI
从未注入 `ask_callback`，于是它一直走 fail-closed：agent 提问 = 拿到一句"没有可用的提问通道"。
要把这张卡接上，有三个决定各自都有一条看起来更省事、实际会把"问你"变成"替你拍板"的路。

约束来自既有架构，不是本次新造的：

- v6-02 §3.1 禁止 worker 线程直接触发 widget；runner 跑在 `_ChatWorker(QThread)` 自己的
  asyncio loop 里（`client/panels/chat.py`），主线程只经 Signal 收事件。
- `BuiltinTool.execute()` 的返回形态是 `ToolResult`（[ADR 0009](0009-toolresult-five-variant-enum.md)
  的 5 变体），工具没有"挂起以后再说"的生命周期。
- 审批链路已经有一套同形状的先例：`client/core/http_worker.py` 的 `ApprovalBridge`
  （Signal 投主线程 + `threading.Event` 唤醒 worker）。

## Decision

1. **同步阻塞桥，复用 `ApprovalBridge` 的形状**：`AskUserBridge.ask()` 在 worker 线程登记
   `_pending[rid]` 后 `_request.emit(...)`（跨线程自动 QueuedConnection ⇒ 槽在主线程），
   然后 `event.wait()`。主线程把卡片画进当前会话时间线（`_AskUserBlock`），用户点「提交回答」
   或「跳过，你自己定」时调 `resolve(rid, outcome, answers)` 填 slot 并 set Event。
   渲染器缺失或建卡异常 ⇒ 立刻 `no_host` 唤醒，**绝不让 worker 干等到超时**（那会把
   "GUI 没就绪"伪装成"用户没回答"）。

2. **等待时长只归配置**：`[runner] ask_user_timeout_secs`（0 或缺失 = 永不超时），由
   `ChatPanel._load_ask_user_timeout_secs()` 在构造 bridge 时注入。`ask()` 的签名没有 timeout
   形参，工具 schema 的 `properties` 里也没有这一格 —— 有测试钉住这两处。

3. **中断走 Event，不走事件循环**：`cancel_pending()` 由主线程在中断（`interrupt_active_session`
   第 0 步）和 `_clear_messages()` 里调用，只 set `threading.Event`。条目的清除归 `ask()` 的
   `finally`（谁等待谁清理）；"迟到点击不算数"由 `resolve()` 里"slot 已带 `outcome` 就返回
   False"那道保证。

4. **五种 outcome 各占一档文案**（`describe_outcome`）：`answered`→SUCCESS、`timeout`→TIMEOUT、
   `skipped`/`cancelled`/`no_host`→USER_DENIED，且三条"没有回答"的文本都写明**这不是反对**。

## Considered Options

- **await 一个 future（不占线程）**：`execute()` 是协程，理论上可以 `await` 主线程置位的
  `asyncio.Future`，省掉一条阻塞线程。被拒：runner 的取消/超时逻辑建立在"工具调用要么返回
  要么已返回"上，把一个 tool call 变成可长期悬挂的 await，就得同时给 `wall_clock_budget_secs`
  和中断路径加"半截工具"的分支——为一个本地单人 GUI 换来的是主循环语义变重。而且
  Future 必须在 worker 的 loop 里创建，跨线程 `set_result` 仍要走 `call_soon_threadsafe`，
  正好撞上下面第 3 条那个送不达的问题。
- **让 agent 传 timeout（或给个默认 30s）**：被拒，且是点单里明确的一条。agent 能传就等于能传
  1 秒，超时从"没人应答"退化成"我替你决定了等多久算你没回答"。反过来写死一个非 0 默认值，
  会让"卡片挂在那儿等你"这种用户预期静默变成"过会儿它自己没了"。默认值因此取 0（永不超时），
  时长只有用户在自己机器上改配置这一条来路。
- **`worker.interrupt()` 的 `call_soon_threadsafe` 路径足够**：被实测推翻。`AskUserTool.execute`
  正占着那条 loop，`call_soon_threadsafe` 排不进去，"永不超时"就是线程死等 —— 所以中断必须
  绕过 loop 直接 set Event，这条不写下来下一个人一定会"顺手统一成 interrupt()"。
- **在 `cancel_pending()` 里把条目摘走**（而不是在 `resolve()` 里判"已定局"）：被拒。反向探针
  实测——`ask()` 的 `finally` 本来就会清理，摘与不摘在判据上分不出来（改坏守卫照样绿），
  却把"谁负责清理"变成两处。真正挡得住窗口的是"已定局不可再写"，它有一条确定性用例
  （同线程直连：渲染器里先 cancel 再 resolve，此刻 worker 必然还没清理）。
- **卡片失效后留着 `resolve` 也返回 True**（只改 UI 文案）：被拒，会出现卡片显示"已回答"、
  agent 早已读走"被中断"的两边各说各话。

## Consequences

- 多一条阻塞线程换的是"工具语义不变"：`ask_user` 对 runner 来说和普通同步工具没区别，
  runner 不需要知道半截工具的存在。
- 永不超时是**默认**而非例外，代价是卡片可能长期挂着。因此"唤醒的入口"不止一个：超时、
  中断、切会话、关窗都会 cancel；这几条路径各有用例钉住
  （`tests/client_ui/test_ask_user_card.py` 的
  `test_cancel_pending_wakes_a_waiting_caller_from_another_thread`、
  `test_without_handler_falls_back_immediately`、
  `test_settled_request_cannot_be_resolved_before_cleanup`）。
- 时长归属写死在配置后，agent 侧的行为差异（"催一次" vs "放弃"）只能靠 outcome 文本传达，
  不能靠调参 —— 这是有意的：把旋钮交给用户，把解释交给文案。
- `no_host` 与 `cancelled` 都落 USER_DENIED 但文案不同；将来若要把它们分档，得同时改
  `describe_outcome` 与按变体分流的逻辑，不要只改一处。
