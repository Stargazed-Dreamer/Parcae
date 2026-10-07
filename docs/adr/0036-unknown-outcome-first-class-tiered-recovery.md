# 「结果未知」成为一等终态，恢复行为由静态副作用档位决定

## Context

v6-lite 引擎与 loop 调度器原来只在两格里记账：**成功**或**失败**。凡是"不知道成没成"都被塞进失败，或者干脆抹掉现场。这不是理论问题，2026-09-30 对照朋友的 Project Akari 交流包逐项实测时，每一格都撞出了具体后果：

1. **重启后系统敢于盲目重跑外部不可逆动作**。Reconciler 把所有悬空 `tool_call` 一律补成 `is_error=true` 的 tool_result，文案写着 "Please retry or choose a different approach"（[reconciler.py](file:///<project_root>/client/core/agent/reconciler.py)）——而 `running` 态意味着请求可能已经发到后端并执行完了，只是回包时进程死了。发出去的消息、提交表单、`git push` 这类动作被"重试"就是重复执行。
2. **同一个 loop 周期可以被烧第二次**。`data/loops/tasks.json` 里 `inflight_run_key` 这个概念根本不存在，进程死在动作执行中，重启后 catch-up 只看 `last_run_at`，把那一轮当成"没跑过"重跑一遍。
3. **失败预算每次重启发一张新的**。`_load_state()` 旧实现把 `fail_count` 清零并自动解除 `auto_failed`——一个永远失败的任务可以无限 `pause → 重启 → resume → pause` 循环，而这是"重试预算有界"这个机制唯一的意义。
4. **崩溃现场在启动时被抹掉**。`server/exec.py` 的孤儿清理对上一进程遗留的 `temp/exec_*.py` / `term_*.log` 直接 `unlink()`，正好删掉"上次崩在哪条命令上"的唯一证据。
5. **终态可被原地改写**。晚一步复活的旧进程可以把已经落定的 `completed` 覆盖成 `failed`，两条因果链在库里合成一条，事后无法归因。

要紧的区分是：**"确定没执行"与"不知道执行没执行"不是一回事**。前者重跑安全，后者重跑可能重复扣款。而旧模型里只有前者。

## Decision

1. **`unknown` 成为第四个终态**（[types.py](file:///<project_root>/client/core/agent/types.py) 的 `TOOL_STATUS_UNKNOWN` / `TOOL_STATUS_TERMINAL`）。它不由模型叙述产生，只由两种可核实情形产生：
   - 活路径：HTTP 执行器在**请求阶段**超时（请求可能已被后端执行）→ `ToolResult.outcome_unknown=True`。**等审批超时不算**——审批没通过，请求根本没发出，那是可证明的"未执行"，记 `failed`/`interrupted`。
   - 重启路径：Reconciler 按悬挂时的状态分档——`pending → interrupted`（还没开始跑）、`running → unknown`（副作用可能已落地）。
2. **终态不可回写**。`EventStore.update_tool_call_status()` 检测到当前已是终态即拒绝原地改写，改为追加一条 `tool_call_status_write_rejected` 审计事件并返回 `False`（[event_store.py](file:///<project_root>/client/core/agent/event_store.py)）。要修正已落定的事实只能**追加**，不能覆盖。同值且不带时间戳的写入按幂等 no-op 处理，不产生审计噪音。
3. **恢复行为由静态副作用档位决定，模型没有裁量权**。`lib/side_effect_tier.py`（[lib/side_effect_tier.py](file:///<project_root>/lib/side_effect_tier.py)）把工具分三档：`read_only` / `local_reversible` / `external_irreversible`，精确名单 + 前缀（`screen_` / `desktop_` / `vm_` / `git_` / `net_` 等），**未命中一律按保守档**。补给的 tool_result 文案按档位分岔：外部不可逆明写"禁止直接重做，先查证"，只读/可逆才说可以重试。workspace 可用 `manifest.toml` 的 `[side_effect_tier]` 段覆盖具体工具——因为"这个脚本是否幂等"只有 owner 知道。
4. **一个周期只烧一次，且失败预算跨重启有效**。loop 每轮算 `run_key = task_id@period_ts`，动作执行前 write-ahead 落盘；结局语义集中在 `RESULT_STATE_RULES`（[loop_manager.py](file:///<project_root>/server/activity_tracker/loop_manager.py)）：`success` 清零、`skipped` 不计数、`failed` +1 且可自动暂停、**`unknown` +1 但既不自动暂停也不自动重跑**（失去信息不等于动作本身坏了，把它判成失败会招来无意义重试）。遗留的 `inflight_run_key` 由档位裁决去向。`CronTrigger.CATCHUP_RUN_CAP = 24` 给补跑加上限，超出累计到 `missed_periods_dropped` 并推 inbox，不再静默丢弃。
5. **崩溃证据归档而不是删除**。启动孤儿清理把遗留文件移到 `temp/exec_evidence/`（每类留最近 3 份）后才裁剪，绝不碰用户目录。

## Considered Options

- **一律按失败处理并允许自动重试**（原状）。被拒：在只读工具上没问题，在外部不可逆工具上是重复执行副作用——本项目有发微信、提交表单、`git push`、盘操作这类动作。
- **让模型自己判断"要不要重试"**。被拒：模型看到的是"上次超时了"这句话，它无法知道请求有没有到达后端；把不可逆性交给一个信息不足的判断者裁量，正是本项目 ADR-0035「确定性证据路永不过模型」反对的那类授权。档位表是代码里的静态属性，模型只能读、不能改。
- **完整 fencing/租约体系**（Akari 那侧的跨进程所有权令牌）。被拒且明确出局：威胁模型是本机自用、**防误判不防越权**（与 [ADR-0018](file:///<project_root>/docs/adr/0018-route-tags-fail-open-secondary-defense.md) 的 fail-open + 二级防护一致）。租约体系要为"另一个进程抢同一个会话"付费，而那个场景在本机不存在；真正存在的场景是"同一个进程重启后忘了自己在干什么"，用 run_key + 档位就够。
- **只加 `unknown` 状态、不改读路径**。被拒：状态没人读就等于没有——本 spec 的 Anti-Cheat 节点名禁止过这个偷懒姿势，所以同批必须落 Reconciler 文案分岔、judge 的结构化账本、`/loop/tasks` 的可查询字段。

## Consequences

**得到**：
- "禁止盲目重做不可逆副作用"从一句提示词变成机器属性——文案由 `lib/side_effect_tier` 生成，模型改不了。
- 已落定的事实不可被覆盖，事后归因不会遇到两条因果链合成一条。
- 永远失败的任务不再能靠重启续命；重试预算跨进程有效。
- 崩溃现场可查（`temp/exec_evidence/`、`temp/write_backups/`）。

**付出**：
- **多了"需要人来裁决"的停顿**。`unknown` 不自动重跑，意味着外部不可逆动作卡住时要用户点一下。这是有意的：把代价从"可能重复执行"换成"可能要等一次确认"。缓解是 inbox 推送带 `dedupe_key`（存储级去重，同一轮只催一次），以及 `GET /loop/tasks` 的 `unknown_run_keys` 可一眼看到攒了几条。
- **档位表是要维护的清单**。新工具没进名单就落保守档，行为是"该重跑的也不敢自动重跑"——安全方向，但有摩擦。workspace 覆盖机制（`[side_effect_tier]`）是为这条缺口准备的正规出口，不是绕过。
- **`fail_count` 不再重启清零，历史上有依赖旧行为的工作流会变**：以前"重启一下 loop 就恢复了"，现在攒满阈值的任务需要显式 `resume`。
- 只改了行为、没新增测试（用户点单），守护靠 `temp/sdd/unknown-outcome-evidence/` 里那 54 项针对性探针一次性验证，长期回归仍缺。

## References

- SDD 产物：`temp/sdd/unknown-outcome-evidence/`（spec.md 的 D-01…D-20 与 Anti-Cheat 节、tickets.md、design-decisions.md 的 D1–D8）
- 代码：[types.py](file:///<project_root>/client/core/agent/types.py) · [event_store.py](file:///<project_root>/client/core/agent/event_store.py) · [reconciler.py](file:///<project_root>/client/core/agent/reconciler.py) · [runner.py](file:///<project_root>/client/core/agent/runner.py) · [http_client_tool_executor.py](file:///<project_root>/client/core/agent/http_client_tool_executor.py) · [lib/side_effect_tier.py](file:///<project_root>/lib/side_effect_tier.py) · [loop_manager.py](file:///<project_root>/server/activity_tracker/loop_manager.py) · [exec.py](file:///<project_root>/server/exec.py)
- 文档：`docs/chat-engine.md` §6.2 / §6.4 / §6.5、`docs/periodic-task-inventory.md`「一个周期只烧一次」
- 相关 ADR：[0018](file:///<project_root>/docs/adr/0018-route-tags-fail-open-secondary-defense.md)（fail-open + 二级防护的威胁模型）、[0035](file:///<project_root>/docs/adr/0035-evidence-gate-deterministic-lanes-no-model.md)（确定性判据不给模型裁量）、[0027](file:///<project_root>/docs/adr/0027-client-server-dependency-boundary.md)（档位表放 `lib/` 的原因）
