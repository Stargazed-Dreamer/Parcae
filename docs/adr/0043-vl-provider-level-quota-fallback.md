# VL 池的额度耗尽按 provider 记账，并用"距上次调用间隔"区分并发 429 与额度 429

状态：accepted（2026-10-04 追记；决策落地于 2026-08-01，代码在 2026-09 后又有二次修订）

## Context

远程视觉（ModelScope 等）的 429 有两种性质完全不同的成因：

- **并发限流**：同一 provider 被并发/紧邻调用，属临时性，冷却一会儿就该重试；
- **当日额度耗尽**：真到顶了，当天再试也没用。

HTTP 状态码本身不区分这两者。早期实现只有一个**全局** `_daily_exhausted` 布尔：
任一 provider 报 429 就可能把整个 VL 池标成当日耗尽，于是一家限流 ⇒ 全家停摆。
更糟的是判据字段 `last_call_finished_ts` 当时也是全局的，
provider A 刚调用完就 fallback 到 provider B 时，B 的 429 会因为
"距上次调用 < 60s"被**误判成并发**——而 B 其实是当日额度用完了。

## Decision

1. **耗尽状态做成 per-provider，全局只做聚合**。
   `self._provider_exhausted: dict[key_id, {cooldown_until, reason, kind, source,
   consecutive_count, date}]`（`server/vl/remote_vl.py:115`）。
   单 provider 判为额度耗尽 → 只冷却该 provider（默认 3600 秒，
   `vl_provider_exhausted_cooldown_seconds`，`server/config.py:788`），**不阻止其他 provider**；
   只有**所有候选 provider 都在耗尽冷却**时才 `vl_quota.mark_daily_exhausted()`
   （`server/activity_tracker/vl_quota.py:725`）标全局，对外仍返回 `daily_exhausted` 向后兼容。
2. **并发 vs 额度的判据：时间间隔，而不是错误文本**。
   规则（`server/vl/remote_vl.py:560-571`）：同一 provider 距**它自己**上次调用结束
   `< 60s` 又出 429 ⇒ 并发（短冷却，稍后可重试）；`>= 60s` ⇒ 额度到顶。
   因此 `last_call_finished_ts` **必须 per-provider**（`ProviderRuntimeState`，同文件 65 行），
   全局那份只保留作向后兼容（670 行注释写明）。
3. **连续耗尽要止损**：同一 provider 连续耗尽达
   `vl_provider_exhausted_max_consecutive`（默认 3，`server/config.py:789`）→
   冷却直接推到**次日 00:00**，当日不再试它。
4. **5xx 走同一条止损路**：`consecutive_server_errors`（500/502/503/504）达阈值也标 exhausted，
   避免像 ice 这类不稳定 provider 在"短冷却—再失败"之间无限循环把业务拖出错
   （`server/vl/remote_vl.py:67-69`）。

## Considered Options

- **全局单一 `_daily_exhausted`**（原方案）：被拒。一家限流让整池停摆，
  且并发/耗尽两种成因被压成同一个布尔。
- **按 429 响应体文案分类**：被拒。上游文案会变且容易在正文里误命中
  （同文件 122-125 行专门留了"子串启发式连续命中计数"的日志，说明这条路的误判是被观测到的）。
- **保留全局时间戳 + 额外加 per-provider 标志**：被拒。判据与状态分居两处必然漂移，
  直接让判据读 per-provider 字段。

## Consequences

- 正面：单家 429 不再连坐整池；额度真耗尽的 provider 有明确冷却时长与当日止损点。
- 代价 1：**60 秒这个判据是启发式**，写死在逻辑里。上游若改限流节奏
  （比如额度类也在 <60s 返回），会被系统性误分类为"并发"→ 反复短冷却重试而不是冷却到明天。
- 代价 2：`status()` 的语义变宽——除 `daily_exhausted`（聚合、向后兼容）外，
  真正的排查入口是 `provider_exhausted` dict（每家的 cooldown_until / reason / kind /
  source / consecutive_count）。看聚合布尔判断"VL 还能不能用"会漏掉"只有某家活着"这种状态。
- 代价 3：两个配置项 + 一个硬编码 60s 分散在 `server/config.py` 与 `server/vl/remote_vl.py`，
  改冷却策略要同时看两处。

## References

- 代码：`server/vl/remote_vl.py`（`ProviderRuntimeState` 58-77、`_provider_exhausted` 115、
  并发/额度判定 560-571、向后兼容字段 670）、
  `server/activity_tracker/vl_quota.py:725`、`server/config.py:788-789`
- 设计原件：`temp/planning_archive/sdd/vl-provider-fallback/`（`design-decisions.md` /
  `spec.md` / `tickets.md`，随仓库跟踪）
- 相关 ADR：0002（LLM 池模块重组）、0007（OmniParser 移除，附带提到 provider 级 fallback）、
  0032（keys.json 热重载守卫）、0033（流式 token 记账）
- 本 ADR 的起因：记忆库 `architecture_decision` 存量复核时发现
  `vl_provider_fallback_architecture` 指向的 `temp/sdd/vl-provider-fallback/` **已不存在**
  （内容已搬进 `temp/planning_archive/sdd/`）——死链，且这条决策满足 ADR 三项标准，故升级进本文。
