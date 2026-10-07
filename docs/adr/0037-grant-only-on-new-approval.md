# 授权状态只由"新批准"写入：平级/低权限的重复 request 不改动作坊

会话已 active 时，agent 再调 `POST /screen/control/request` 会被 [overlay_client.py](file:///<project_root>/server/overlay_client.py) 的 `ensure_takeover_approved` 短路成 `status="skipped"`（确认窗根本没出现）。2026-09-30 起，[routes.py](file:///<project_root>/server/screen/routes.py) 的 `screen_request_control` **只对 `status=="confirmed"` 执行 `session.grant()`**；skipped 一律不改动作坊状态。决定性的理由是 `SessionManager.grant()` 整体替换 `SessionState`，而当时 skipped 字典里的 `shutdown_permitted` 是写死的 `False` —— 于是"没人做过任何决定"的一次重复请求，实际效果是把用户勾选的权限擦掉。

## Status

accepted (2026-09-30)，同日二次修订：Decision 2 的"关机维度必须先 release"约束被放宽，见文末 Revision

## Context

异环抽卡夜采（用户睡前批准 `watchdog` + 允许关机，要求"跑完直接关机"）实测到：`/health.screen.takeover_persistent` 先是 `mode=watchdog, shutdown_permitted=True`，采集脚本 `workspace/yihuan_gacha/yihuan_gacha.py` 一启动就自己调了一次 `mode=normal` 的 request，随后变成 `mode=watchdog, shutdown_permitted=False`，`task_description` 被换成脚本那句、`granted_at` 重置（watchdog 的 10h 硬上限被续命）。最终 `/auto_shutdown/trigger` 403 `shutdown_not_permitted`，机器没关成。

这不是"某处写错一个字段"：ADR-0011 引用的决策 17 写明"agent 调 request = 显式请求新授权，**总是覆盖**现有授权"，其正当用途是 normal→watchdog 升级、改任务描述、重置 idle 计时。但同一句话也被 used 到了 skipped 路径上，而 skipped 恰恰是"没有新授权发生"的那种情况。上一次同类事故（2026-08-06，skipped 缺 `authorized_mode` 导致 watchdog 被降级）的修法正是"skipped 里回填当前模式"，所以模式保住了；而回归测试 [test_session_manager.py](file:///<project_root>/tests/agent_runner/test_session_manager.py) 的 `test_skipped_path_preserves_existing_watchdog_mode` **只断言 mode**，同一个字典里 `shutdown_permitted=False` 无人检查——半个修复、半个盲区，留到今天。

另有一处让盲区长期不被发现：根 [conftest.py](file:///<project_root>/conftest.py) 的 `MockGUIClient.ensure_takeover_approved` 在 `force_prompt=True` 时无条件返回 `confirmed`，而生产代码会先被 `session.is_active()` 短路。也就是说**测试环境里不存在 skipped 路径**，route 层的授权覆盖行为一直失真。

## Decision

1. **写入者唯一**：授权状态（模式、`shutdown_permitted`、`max_duration_seconds`、`granted_at`、`task_description`）只能被这三类事件改变——① 用户在新确认窗里做了选择（`confirmed`）；② 显式撤销 `screen_release_control` / 紧急停止；③ 时间到期（`transition` / `expired`）。重复的平级或低权限 request 属于第 ④ 类：**只读查询，不改状态**。
2. **降级不靠请求的措辞**：`mode="normal"` 的请求不会把 watchdog 降下来，`watchdog` 的请求不会把已批准的关机权限收走。想要不同的权限组合，先 `release` 再申请。（**已修订**：关机维度改为 `needs_shutdown` 显式升级，见 Revision；其余权限组合仍遵循"要么新批准、要么 release 后重申请"。）
3. **测试替身与生产共享判据**：升级与否的判定收进会话层 `SessionManager.needs_new_approval(requested_mode, needs_shutdown)`——`OverlayClient.ensure_takeover_approved` 的 skipped 短路与根 [conftest.py](file:///<project_root>/conftest.py) 的 `MockGUIClient` 都调用**同一个函数**。之前是"两边各抄一遍条件"，抄错一次就失真（本 ADR 的 bug 能藏五周，正因为替身里没有 skipped 路径）。新增 `TestNeedsNewApprovalPredicate` 7 条把判据本身钉住；`TestSkippedRequestDoesNotMutateGrant` 三条（skipped 保留关机权限与时钟、按生产字典形状钉住 route 行为、`confirmed` 仍可覆盖）继续守住建 ADR 的那半边。
4. **skipped 的响应必须自解释**：`message` 说明"没重新弹窗、权限未变更"，并指向正确的升级姿势。**`status` 字段保持 `authorized` 不变**——调用方（含 `workspace/yihuan_gacha/yihuan_gacha.py`）判的是 `status=="authorized"`，改状态值会把这次的行为修正变成新的破坏性变更。（message 措辞随 Revision 更新，判据不变。）

## Considered Options

- **B：字段继承**（skipped 也 grant，但从旧 state 继承 `shutdown_permitted`/时长/`granted_at`）。被拒：`grant()` 的语义从"设定新状态"变成"有条件保留"，以后每加一个权限维度都要再补一次继承清单，漏一个就是下一个同类 bug。
- **C：在 `SessionManager` 加权限单调性**（低权限 grant 直接拒绝或忽略）。被拒：会一起挡掉合法路径——watchdog 到期降级到 normal、用户在新弹窗里主动不勾看门狗（`mode_downgraded`），这些都得再开豁免口子，复杂度换不到额外安全。
- **仅改 skipped 字典把 `shutdown_permitted` 改成回填真值**。被拒：那只是把"谁都能擦"换成"谁都能续"，仍然让一次无人决策的调用改动安全状态；根因在 routes 的覆盖条件，不在字典内容。

## Consequences

**得到**：睡前批准的权限不会被脚本自己的请求悄悄改写；"重复请求 = 刷新授权"这类直觉不再成立，权限变化只有一条可解释的因果链（用户点了窗）。测试替身与生产同构后，route 层的授权逻辑第一次真的被测试覆盖。

**付出**：
- 想**获得关机权限**的正当姿势是 `screen_request_control(mode="watchdog", needs_shutdown=true)`；缺位时它会弹第二次窗（**已修订**，见 Revision——本条当初写的是"必须 `release` 后重申请"）。
- 重复 request 不再刷新 `task_description`，顶栏文案停留在用户当初批准时看到的那句（更真实，但长任务换阶段时不会自动更新）。
- watchdog 的 10h 硬上限不再被"活动"续命——它是上限，不是租约。
- conftest 改动触及全套件默认授权行为，受影响目录已实测 **779 passed / 18 skipped / 0 failed**（`tests/agent_runner`、`tests/approval_screen`、GET 端点冒烟），pyright 与基线同为 0 错。

## Revision（2026-09-30 二次修订：关机位改为可升级维度）

**为什么要改**：本 ADR 的 Decision 1（skipped 不改状态）挡住了"低权限请求擦权限"，但把"脚本补申请关机权限"这条路一起堵死了——`watchdog → watchdog` 的重复请求永远短路成 `skipped`，唯一出路是先 `release`（把用户批的 watchdog 一并丢掉）再重申请。实测后果就是用户那句"看门狗老是不成，这让我很烦"：过夜任务跑到要关机时才发现缺关机位，而补位动作本身要先把授权拆掉。

**改了什么**：
1. `ControlRequest` 新增 `needs_shutdown: bool = False`（[routes.py](file:///<project_root>/server/screen/routes.py)），端点把它透传给 `ensure_takeover_approved`。
2. 短路判据从"是不是 normal→watchdog"改成"**缺不缺**"：升级 = 请求 watchdog 且（当前不是 watchdog **或** 声明了 `needs_shutdown` 而当前缺关机位）。什么都不缺时仍旧 `skipped`，不多打扰用户一次。实现落在 [manager.py](file:///<project_root>/server/screen/session/manager.py) 的 `SessionManager.needs_new_approval()`，[overlay_client.py](file:///<project_root>/server/overlay_client.py) 与 conftest 替身都只消费它（见 Decision 3）。
3. `skipped` 字典的 `shutdown_permitted` 改为**如实回填现值**，不再写死 `False`——routes 不用它改状态（Decision 1 保留），但调用方读到的必须是真话。
4. `screen_request_control` 的 `mode="watchdog"` docstring 顺手纠偏：它原先写"看门狗复选框默认不勾、用户没勾就返回 `mode_downgraded`"，两处都与代码不符——`StartConfirmDialog.shutdown_checkbox` 初始即 `setChecked(True)`（spec 决策 19），而决策 17 之后弹窗上根本没有"启用看门狗"的开关（`watchdog_group` 只有时长 + 关机两项，`authorized_mode` 在用户点允许时恒等于 `requested_mode`）。`mode_downgraded` 因此只剩 `timeout_action="proceed"` 那条超时自动放行分支能给出（config 默认 `cancel`，端点走的就是默认值），作为响应契约字段保留、GUI 面板仍在处理它。
5. `docs/computer-use-reference.md`、`.agents/skills/auto_shutdown.md`、`docs/api-reference.md`、`server/agent_guide_data.py` 的 watchdog 申请姿势同步补 `needs_shutdown`。

**同轮把状态机审计里剩下的几条一起改掉**（决策 1 的判据集中化之后，这些是"账面与实际不一致"的同类问题）：

6. **A/D：被动 `takeover_confirm` 闸门在生产路径不可达**。全部副作用端点 handler（键鼠 execute_action/batch_actions/desktop_transaction、`focus_window`、`screen_semantic_action`、窗口 minimize/restore/raise/close、`app_launch`、剪贴板读写、`scroll_capture`）都在同一 handler 里**先**调 `_enforce_session_permission()`，无授权直接 403；能走到闸门时 `session.is_active()` 必为真，于是闸门第一行就放行。原 `routes.py` 里"watchdog 单独短路"那句是**不可达分支**（is_active 已覆盖），已删除；闸门保留为纵深防御，但 docstring、`/health` 的 `takeover_confirm.description`、`computer_use/SKILL.md` 都改成"兜底闸门，正常路径弹不到；唯一授权入口是 `/control/request`"。新增 `TestPassiveGateIsDefenseInDepth`：把 conftest 对闸门的整体 stub 换回真函数后，证明"无授权 = 403 且闸门没被调用"、"已授权 = 静默放行"、"兜底分支被直接调用时仍按 confirmed+task_authorization 才 grant"。
7. **B：watchdog 下 CONFIRM 行为的文案与代码正面对撞**。代码是 `danger_level=confirm` + watchdog + 非显式确认 → `require_confirm=False`（自动跳过，ADR-0026），而 `agent_guide_data.py` 两处、`docs/computer-use-reference.md` 三处、`computer_use/SKILL.md` 三处都写着"watchdog 仍弹窗=超时 deny"。读 guide 的 agent 会以为看门狗下危险点击还会有人拦——**这条比文案本身更危险**：它让 AFK 的 agent 把安全兜底寄托在一个根本不会出现的弹窗上。全部改为"watchdog 自动跳过 CONFIRM 弹窗，AFK 时 fail-closed 纪律是唯一防线；显式 `require_confirm=true` 与 BLOCK 级仍生效"。
8. **F：紧急停止的授权撤销失败不再静默**。`security.py:_trigger` 原先 `except Exception: pass`——按了 panic 键，若 `SessionManager.release()` 抛异常，授权仍在、顶栏仍亮，而日志什么都没有。现在分三种：撤销后仍 active → ERROR；调用异常 → ERROR + 尽力 `hide_overlay()`（不留"已授权"假象）；正常撤销 → 静默。同时文档写清两条容易误读的语义：**授权撤销是永久的**（必须让用户重新批准），`emergency_cooldown_seconds`（默认 10 秒）只挡这段时间内的新操作；task_closure 的收尾 release 只是给 agent 的纪律，服务端没有"任务结束"钩子。
9. **`/health` 与监控面板的空闲时长真源**。遗留键 `takeover_idle_timeout_seconds`（默认 300）早已不决定撤销时机，却仍是 `idle_timeout_seconds` 的 fallback，导致面板"空闲超时"显示 300 秒、6 处文档写"空闲 300 秒自动撤销"，而真实策略是两阶段 600+1200=1800 秒。`server/core/health.py` 改为按 `idle_warning_seconds + idle_grace_seconds` 上报（watchdog 显式给 0），`client/panels/monitoring.py` 同改并给遗留字段留带标注的兜底；`config.example.toml` 把两个遗留键注明"仅展示、不决定时限"。新增 `TestHealthIdleTimeoutReporting` 3 条 + 监控契约断言"显示的是 1800 而不是 300"。

顺带纠正一条同样过时的表述：确认窗上**没有**"仅允许本次 / 开启任务授权"的二选一勾选项了（决策 17 之后用户点"允许接管"就是按 agent 请求的 mode 授权），`agent_guide_data.py` 的 first_action、`_index.md`、`computer_use/SKILL.md` 与 `computer-use-reference.md` 的描述都对齐了这一点。

**边界（有意不扩）**：`needs_shutdown` 只在 `mode="watchdog"` 时参与判据。关机位只存在于 watchdog（`SessionManager.grant` 对 normal 恒置 `shutdown=False`），若让 normal 请求也弹升级窗，用户会面对一个"没有关机复选框可勾"的窗——点了也拿不到权限，等于把 fail-closed 变成假象。

**代价**：升级要用户在场重新点窗（`cancelled`/超时不改现有授权，仍是 fail-closed）；这次 `grant` 会按弹窗时长**重建** watchdog 硬上限时钟（`granted_at` 重置），所以"补一个关机位"顺带重置了剩余时长——这是"用户重新批准"的自然结果，不是漏洞，但调用方不该指望它无损。

**验证**：新增 `TestShutdownBitIsUpgradable` 6 条（缺位→弹窗、平级→不弹窗、已具备→skipped 且回填真值、normal+needs_shutdown→不弹窗、端点升级落盘、skipped message 指向 `needs_shutdown`）。把判据与 message 临时回退成本 ADR 原版后 **3 failed / 3 passed**，改回后 **6 passed**。同轮审计附记再加 15 条（`TestNeedsNewApprovalPredicate` 7 / `TestPassiveGateIsDefenseInDepth` 3 / `TestHealthIdleTimeoutReporting` 3 / `TestEmergencyStopRevokesAuthorization` 2）：把 `needs_new_approval` 退回"忽略 needs_shutdown"的旧规则后 **2 failed / 11 passed**，把 `_trigger` 退回 `except: pass` 后失败路径用例 **1 failed / 1 passed**，改回后全绿。最终 `tests/agent_runner` + `tests/approval_screen` + GET 端点冒烟 **800 passed / 18 skipped / 0 failed**，`uv run pyright` **0 errors**，`drift_detector` HIGH 0（4 条 MEDIUM 与本次无关：`models.gpu_fallback_*` 说明指向已删键、recorder manifest glob 未命中）。

## 同轮审计附记（2026-09-30：状态机余下 5 条一起改掉）

Revision 之后把 `server/screen/session/` 全量读了一遍，剩下几条一起处理；逐条的对外描述见 `CHANGELOG.md` 的"权限状态机第二批"条目，这里只记决策与理由。

- **判据单点化（并入 Decision 3）**：`SessionManager.needs_new_approval(requested_mode, needs_shutdown)` 成为"要不要重新问用户"的唯一实现，渲染层与 conftest 替身都调用它。理由：本 ADR 的 bug 藏五周的机制是"生产一套条件、替身一套条件"，只要判据还能被抄第二遍，同类失真就会再来一次。
- **被动 `takeover_confirm` 定位为纵深防御，而不是第二个授权入口**：所有副作用端点 handler 都先调 `_enforce_session_permission()`（无授权 403），所以"顶栏没显示就弹窗 + confirmed 后 grant"这段在生产里走不到。选择**保留**这段兜底（未来新增端点漏掉 403 检查时它仍会问用户），但删掉其中不可达的 watchdog 分支，并把 `/health` 的 `takeover_confirm.description`、gate 的 docstring、`docs/computer-use-reference.md`、`computer_use/SKILL.md` 改成实话。被拒方案"把整段闸门删掉"：那会让"漏检的端点"从"弹窗问用户"变成"直接放行"，是把 fail-closed 换成 fail-open。
- **watchdog 下 `danger_level=confirm` 自动跳过弹窗**（[ADR-0026](0026-watchdog-skip-danger-confirm.md) 既有决策），但 guide 与两份文档写反成"仍弹窗=超时 deny"——这类文案不是措辞问题：AFK 的 agent 会把安全兜底寄托在一个不会出现的窗上。同时记下真例外：`screen_window_close` 无条件走 `_confirm_control_operation`，任何模式都弹窗。
- **时限只有一个真源**：`idle_warning_seconds` + `idle_grace_seconds`（两阶段）与 `watchdog_default_hours`。遗留键 `takeover_idle_timeout_seconds`(300) / `takeover_watchdog_max_duration_seconds`(36000) 只作展示，`/health` 与状态面板不再拿它们当"空闲超时"（旧行为把 30 分钟显示成 300 秒，是 6 处文档失真的同一处根源）。
- **紧急停止：撤销永久、冷却短时，且撤销失败必须喊出来**。`_trigger` 原先 `except: pass`——按了 panic 键而授权仍在、顶栏仍亮、日志什么都没有。现在 release 后仍 active 或调用异常都记 ERROR，异常路径尽力 `hide_overlay()`。符合"少加拦截、多加可见"：没有新增任何闸门，只是让已有闸门的状态可被发现。

## 实机验证与第三批（2026-09-30 晚：重启后实机回归 + 自我纠错）

前两批落地后用户重启后端，按五步脚本实机回归全部通过：watchdog+取消勾选得到 `shutdown_permitted=false`；生效中发 `mode=normal` 请求 → 无弹窗且 `granted_at`/`task_description`/关机位**逐字节未变**（T9 的实机版）；缺关机位带 `needs_shutdown=true` → 弹第二次窗，批准后 `shutdown_permitted=true`、`granted_at` 重建（升级有代价的实机版）；重复同一请求 → 不弹第三次窗；release 后打被把关的 `GET /screen/clipboard` → **403 无弹窗**；`Ctrl+\`` → `revoked_reason=emergency_stop` 且剪贴板探针 403。

实机同时暴露三件前两批没抓到的事，第三批一起修：

- **"5 个副作用调用点"是本 ADR 前两批自己写错的**。AST 实测 `_enforce_session_permission()` 的调用点有 **13 处**：前两批漏数了剪贴板读/写、`app_launch`、窗口 minimize/restore/raise、`scroll_capture`。教训固化成规则：涉及数量的表述必须现场测（`inspect.getsource` 扫 handler 源码），文档改为"全部副作用端点 handler + 指向 `/health` 的 `screen.takeover_confirm.protected_endpoints`"，不再手写数量与枚举。
- **`modes.py` 的 `ENDPOINTS_SIDE_EFFECT`/`ENDPOINTS_READONLY` 是零消费死名单**（全仓无 import），且内容与实际把关集合不一致：列了根本不把关的 `screen_app_wait`/`screen_preview_action`，漏了把关的剪贴板与 `scroll_capture`；ADR-0011 却把它们描述成 403 判据。已删除；把关真源就是"handler 开头显式调用"。清单由新增的 `routes.gated_side_effect_operations()` 从路由表现场扫描得出（`/health` 同步上报真值），并由 `TestGatedSideEffectListIsDerived` 钉住：把关集合扩缩时测试必红，防清单静默漂移。
- **`max_duration_hours` 从未真正生效**：`confirm_start` 消息不带时长，弹窗 spinbox 永远从默认 10 起步——实机 4 次"请求 1 小时"全部被授权成 36000 秒，agent 拿到的授权比请求的更宽。修复：请求值作为 spinbox **初值**下传（用户仍可上调），未传沿用默认 10；`TestRequestedHoursReachDialog` 2 条钉住载荷。设计上仍坚持"以用户弹窗选择为准"，改的只是"agent 的请求值要能到达用户眼前"。

## References

- 关键代码：[server/screen/routes.py](file:///<project_root>/server/screen/routes.py)（`screen_request_control` 的 grant 条件、`_ensure_takeover_approved` 的兜底定位）、[server/overlay_client.py](file:///<project_root>/server/overlay_client.py)（`ensure_takeover_approved` 的 skipped 短路）、[server/screen/session/manager.py](file:///<project_root>/server/screen/session/manager.py)（`grant()` 整体替换状态、`needs_new_approval()` 升级判据、`can_shutdown()` 校验 watchdog+`shutdown_permitted`）、[server/screen/security.py](file:///<project_root>/server/screen/security.py)（紧急停止的撤销与冷却）、[server/core/health.py](file:///<project_root>/server/core/health.py)（`takeover_persistent` / `takeover_confirm` 上报）
- 相关 ADR：[ADR-0011](0011-computer-use-session-manager.md)（SessionManager 与三档模式，其 References 里的决策 17 被本 ADR 修订）、[ADR-0012](0012-session-manager-in-memory-only.md)（纯内存不持久化）、[ADR-0015](0015-watchdog-shutdown-via-auto-shutdown.md)（关机权限双校验）、[ADR-0026](0026-watchdog-skip-danger-confirm.md)（watchdog 跳过 CONFIRM，本 ADR 附记纠正了写反的文案）
- 测试：[tests/agent_runner/test_session_manager.py](file:///<project_root>/tests/agent_runner/test_session_manager.py) 的 `TestSkippedRequestDoesNotMutateGrant`（T9）、`TestShutdownBitIsUpgradable`（T10）、`TestNeedsNewApprovalPredicate`（T11）、`TestPassiveGateIsDefenseInDepth`（T12）、`TestHealthIdleTimeoutReporting`（T13）、`TestEmergencyStopRevokesAuthorization`（T14）、`TestGatedSideEffectListIsDerived`（T15，把关清单现场派生）、`TestRequestedHoursReachDialog`（T16，请求时长进弹窗载荷），另有 `TestDialogResultPropagation` 与 [tests/approval_screen/test_monitoring_health_contract.py](file:///<project_root>/tests/approval_screen/test_monitoring_health_contract.py) 的空闲时长契约断言
- 操作文档：`docs/computer-use-reference.md`「`screen_request_control` 只在"新批准"时改状态」段
