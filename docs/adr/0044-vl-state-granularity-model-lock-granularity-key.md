# VL 的模型级失效状态按 `(key_id, model)` 记，执行锁、并发上限与冷却记账按 `key_id` 记 —— 两种粒度故意不一致

状态：accepted（2026-10-07 追加第 5 条即 Ticket 02(a) 的正式裁决、第 6 条即 Ticket 04 的聚合判据；
主体决策落地于 2026-10-06，代码在 `server/vl/remote_vl.py`）

## Context

VL 降级链把候选粒度从 `key_id` 升到 `(key_id, model)`（同 key 内一个模型撞 400/404/422 就换同
token 的下一个模型，见 0043 的相邻链条）。落地后同一份代码里出现了**两种索引粒度**：

- 失效状态按 `(key_id, model)`：`_dead_models_today: dict[key_id, set[model]]`（`server/vl/remote_vl.py`），
  判死时 `_mark_model_dead_today()` 登记、`_check_daily_exhausted_reset()` 跨日整表作废，
  下一轮调用开头由 `_call_with_failover` 把标记**播种**进本轮排除集。
- 执行锁与并发上限按 `key_id`：`_get_per_key_lock(rk.key_id)`、
  `_pick_provider` 里的 key 级 `state.active_count >= max_concurrency`、
  `ProviderRuntimeState` 整个类只以 `key_id` 为键。

（本文一律用符号名而不是行号指位：往这个文件里加一行日志就会让所有行号整体漂移，
按行号写的锚点会静默变成假引用。）

这不是漏改，而是两条约束各自的作用域本来不同：

1. **"这个模型 ID 今天不能用"是模型级事实**——按 key 记会让一个下架 ID 连坐同 token 的其他模型。
2. **上游限流记在账号/token 上，不记在模型上**：`vision.max_concurrency` 挂在 keys.json 的
   **key 记录**上（schema v9，`docs/environment-constraints.md`「远程 VL」段），
   模型级并发**没有配置位**；把锁降到 model 级等于给同一 token 放开并发，
   而这条链路的限流余量是个位数 RPM（`max_concurrency=1` 就是为了序列化调用）。

危险恰恰在于"看起来像疏漏"：合理读者的默认动作是**统一粒度**，而统一成 model 级会静默放松串行。

## Decision

1. **状态按 `(key_id, model)`**，且标记只在**当天**有效——次日 00:00 整表解除，给上游补回同名 ID
   一次重新证明的机会（永久拉黑仍归 keys.json 的 `enabled`/`tier`，那是人工配置，不是运行时状态）。
2. **执行锁与并发上限按 `key_id`**，模型级降级不得改变"同一 token 同时只有一个在飞请求"。
   为"不断路"可以付出"多打一次注定失败的探测请求"；反过来，**绝不**为提高吞吐放松 token 级串行。
3. **两道 key 级门都保留，各挡不同的洞**（不是重复实现）：
   `_pick_provider` 里的 `active_count` 上限让选路阶段就知道"这家满了"，于是走"等待后重试"而不是
   排队在锁上；但**检查与自增跨两次调用、本身有竞态**，真正的串行由 `plock` 保证
   （`active_count += 1` 在 `plock.acquire` 之后，`finally` 里递减并释放——见 `_call_with_failover`）。
   摘掉任一道都会红一条用例，见 Consequences。
4. **模型级失效不并进 `_provider_exhausted`**：那张表是额度/429 语义（冷却时长、连续耗尽、
   跨日止损都按 provider 算），混进模型级条目会让"一个模型下架"看起来像"这个 token 额度用完了"。
5. **`cooldown / last_error / last_success / consecutive_server_errors` 保持 key 级连坐**
   （2026-10-07 用户点单，即 Ticket 02(a) 的正式裁决）。判据是错误类别而不是候选粒度：
   5xx 走的是 `_call_with_failover` 的 `is_server_error` 分支，它表达的是**上游服务/账号侧不可用**，与"这次选了哪个
   model"无关；按 model 拆开计数等于假设同 token 下不同 model 的后端彼此独立，而实测上游并非如此。
   永久失败（400/404/422）不归此项——那类早按 `(key_id, model)` 记（第 1 条）。
6. **聚合判据 `daily_exhausted` 把"整条 key 的模型当日全判死"也算不可用**（2026-10-07 Ticket 04）。
   判据是 `_key_unavailable_today(rk)` = key 级额度/5xx 冷却 **或** 该 key 的 VL 候选模型全在
   `_dead_models_today`。只判前者的话，"上游把模型 ID 全下架"这一档**永远翻不起**聚合标记——
   永久失败按第 1、4 条故意不进 `_provider_exhausted`，于是聚合看不见它，`loop_actions` 也就拿不到
   OCR 兜底入口，业务整天报错。成因跟着写进 `kind`：额度类 `quota_exhausted`，模型类
   `model_unavailable`。**后者不是配额信号**——`vl_quota.should_call` 的 deny 文案按 kind 分岔，
   写成"ModelScope 返回 429"会把人引向下调 `vl_daily_quota`，而正确动作是换模型 ID 或复位。

## Considered Options

- **锁与并发上限降到 `(key_id, model)`**（直觉上的"统一粒度"）：被拒。上游按 token 计限流，
  同 token 两个模型并行直接换 429；而 429 会被 0043 的"距上次调用 < 60s ⇒ 并发"判定吃下，
  变成整 key 短冷却重试——**一个模型的问题又放大成整条 key 的问题**，与本次改造目的相反。
  另外 keys.json 没有 model 级 `max_concurrency` 字段，真要走这条路得先改 schema。
- **死模型按模型名全局记（不分 key）**：被拒。同名模型在不同 key/不同 base_url 上可用性不同，
  全局记会误伤另一条 token 上好好的同名模型。用例专门断"同名模型在另一条 key 上不受波及"。
- **只留 `plock`、摘掉 key 级并发上限**：被拒。选路会一直"选中"已满载的 key，
  调用方拿着锁排队而不是走"并发已满→按 backoff 等待重试"，`status()` 里的 `active_count`/
  `max_concurrency` 也失去意义。
- **只留并发上限、摘掉 `plock`**：被拒。上面第 3 条的竞态——两个线程可以都在自增前读到 `active_count=0`。
- **连败计数与冷却降到 `(key_id, model)`**（Ticket 02(a) 的另一答案）：被拒（2026-10-07）。量级摆在这：
  `ice_9c1e4bf8` 一条 key 有 5 个 VL 模型（keys.json 里 `max_concurrency=5`，是实测最大的一条），
  按 model 各算 3 次才止损 ⇒ 单轮最多 **15 次 5xx** 才会走 `daily_exhausted → OCR 兜底`，而代码注释
  担心的正是"ice 等 ~80% 可用率 provider 503 持续失败导致无限短冷却循环"（`is_server_error` 分支上方的注释原文）。另外 ADR 0043
  的"距上次调用 < 60s ⇒ 并发，否则 ⇒ 额度"是 **provider 级时钟**，`all_exhausted → daily_exhausted`
  的聚合判据也是 key 级——计数一旦按 model 拆，这两处判据都得重定义（正撞 Ticket 04）。
- **折中：计数按 model 记账、冷却仍按 key 连坐**：未采纳。它换来的只是"哪个 model 触发的"这一行信息，
  而这条信息用**日志 + 把触发 model 存进 `_provider_exhausted[key_id]`** 就能拿到，不必多养一张表。
- **聚合判据维持"只看 key 级冷却"，模型级判死不参与**（Ticket 04 之前的实际行为）：被拒（2026-10-07）。
  它不是"更保守"，而是**漏了一整档**：全池模型被下架时 `daily_exhausted` 永远 False，
  `should_call` 不 deny、`loop_actions` 不走 OCR 兜底，每轮都重新撞 400。
- **模型级判死直接写进 `_provider_exhausted`（省掉一个新判据）**：被拒。那张表按第 4 条是额度/429 语义，
  混进去之后 `kind` 与冷却时长都失去含义，且 `/health` 会把"某个 token 额度用完"报给一个纯下架故障。

## Consequences

- 正面：一个下架模型只烧一次 400（当天内），不再每轮重新烧满 6 档退避；同 token 的串行保证没有被
  降级链顺手改掉。
- 代价 1：**双粒度本身反直觉**，只能靠本 ADR + 用例挡"顺手统一"。守卫是
  `tests/llm_vision/test_vl_dead_model.py::TestSameTokenSerialization`（三例分别钉
  "两线程不同时落在两个 model" / "key 级在飞数到上限后换 model 也选不到" /
  "换过 model 后锁表仍只有一条"）。摘改矩阵实测：只拆锁→锁表例红；只拆并发上限→上限例红；
  两道一起拆→串行例红。**第一版串行例是恒绿的坏判据**（两线程从同一个 model 起步，
  "按 model 分锁"的摘改照样撞同一把锁）——改判据时别只看它绿不绿。
- 代价 2：串行是 key 级 ⇒ 同 token 换模型**不会更快**，降级链上的多个模型仍然排队。
  这是有意的：换来的是"不断路"，不是吞吐。
- 代价 3：**A 模型的 5xx 会把整条 key 停掉，同 key 的 B 模型跟着等**。02(a) 已按第 5 条裁决为
  "保持连坐"，所以这条代价是**接受下来的**，配套做的是把它变成看得见的：触发 model 存进
  `_provider_exhausted[key_id].trigger_model`，冷却日志同时打"因哪个 model、停到几点、
  同 key 其余 model 一并不可选"。反过来的错法是把日志写成"这个 token 额度用完了"——那是 kind
  字段的事，不是措辞的事。
- 代价 4：**双粒度必须有两级视图，否则读的人只能猜**。`status().providers[]` 一行一条 key（key 级
  冷却/并发字段一个不少），其下 `models[]` 一行一个模型（`name/current/dead_today`）；代表模型
  优先取当日没判死的那个，全死才退回 tier 首。`available` 与 `vl_usable_for_sensitive` 都按当日
  判死排除，`providers[].available` 在模型全死时是 False——否则面板说"可用"而选路取不到候选。
  展示口径与聚合判据共用同一个谓词 `_all_models_dead_today`，避免两处各写一遍而漂移。
- 代价 5（第 6 条带来的新问题）：**"整条 key 只有一个 VL 模型 + 该 ID 下架"会一天不通**。
  缓解是两条，都不靠提前探测（10-05 已裁决不给死模型做活体探针）：次日 00:00 自动解除，
  以及 `POST /vision/reset` 手动复位。复位只解三处当日标记（判死名单 / key 级冷却 / 聚合标记，
  配额侧走 `vl_quota.clear_daily_exhausted`），**不动限流冷却与 `used_today`**——
  复位是"再给一次机会"，不是把账擦了。

## References

- 代码：`server/vl/remote_vl.py` 的 `ProviderRuntimeState`（含 `consecutive_server_errors`）、
  `_dead_models_today` / `_dead_models_date`、`_check_daily_exhausted_reset`、`_mark_model_dead_today`、
  `_get_per_key_lock`、`_pick_provider`（key 级并发上限那道门）、`available`（当日判死排除）、
  `_call_with_failover`（播种排除集 → `plock.acquire` → `active_count += 1` → `finally` 递减并释放；内含
  `is_server_error` 分支与非并发 429 分支，两处都调 `_mark_provider_exhausted`，末尾是
  `_key_unavailable_today` 聚合）、
  `_mark_provider_exhausted`（`trigger_model` 记账与两处冷却日志）、
  `_all_models_dead_today` / `_key_unavailable_today` / `reset_daily_state`（第 6 条与代价 5）
- 复位与视图：`server/vl/vision.py` 的 `POST /vision/reset`（`vision_reset`）与
  `GET /vision/providers`（暴露 `providers[].models[]` + `provider_exhausted`）、
  `server/core/health.py` 的 `_get_vision_status`；配额侧 `server/activity_tracker/vl_quota.py` 的
  `clear_daily_exhausted` 与 `should_call` 的 `deny_exhausted` 分成因文案
- 测试：`tests/llm_vision/test_vl_dead_model.py`（死模型跨调用 + `TestSameTokenSerialization` +
  模型级判死翻起 `daily_exhausted`）、
  `tests/llm_vision/test_vl_ticket04_visibility_reset.py`（两级视图、复位范围、成因文案）、
  `tests/llm_vision/test_vl_provider_fallback.py`（`TestProviderExhaustedMark` 两条可见性用例 +
  `TestServerErrorExhausted` 的 `trigger_model` 接缝断言；四轮摘改矩阵各判红，没有一轮
  "摘掉守卫仍全绿"，矩阵细节见 `CHANGELOG.md` 2026-10-07 那条）
- 相关 ADR：**0043**（provider 级额度耗尽记账；本文与它的分工是"0043 管额度/429 按 provider，
  本文管模型级失效按 `(key, model)` 而串行按 key"）、0032（keys.json 热重载守卫）
- 配置口径：`data/llm/keys.json` 中 VL key 的 `vision.max_concurrency`（key 记录级，schema v9）、
  `docs/environment-constraints.md`「远程 VL」段
- 变更记录：`CHANGELOG.md` 2026-10-05「400/404/422 判永久失败」、2026-10-06「`(key_id, model)` 级
  今日永久失效标记」、2026-10-06「D8 同 token 串行守卫单测」
