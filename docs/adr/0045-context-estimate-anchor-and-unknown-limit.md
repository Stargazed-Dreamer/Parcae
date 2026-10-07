# 上下文压缩判据以「上一次调用的真实 prompt_tokens」为锚点；模型上限未知时不判压缩

状态：accepted（2026-10-07，代码在 `client/core/agent/compactor.py`、
`client/core/agent/event_store.py`、`client/core/agent/runner.py`、`server/llm_pool/`）

## Context

chat 面板 v2 的 L2 压缩长期是"配了但从没跑过"：`data/client/agent.db` 实测（26 行真实 usage）
`compactions` 表 **0 行**、`context_overflow` 事件 **0 条**，而同一段时间里单次调用的真实
`prompt_tokens` 中位数已达 5 万、峰值 **97090**。两头都对不上：

- 估算口径只有 `len(text)//4`，量的是**入库消息**。同一会话 `sess-521d458c7310` 按这个口径
  整会话估出 **27196**，而它真实单次调用到过 97090 —— system prompt 与工具 schema 那部分
  从来没被算进去，差 ~3.6 倍。
- 上限写死 `Compactor(max_context_tokens=32000)`（`client/panels/chat.py` 用默认值构造），
  阈值 27200 低于实际跑通的 97090，等于判据永远不会在正确的时机开口。

低估比不判更坏：它让"从没压缩"看起来像"不需要压缩"，而这正是把 413 留到线上才发现的形状。

## Decision

1. **估算走真实锚点**：`EventStore.get_last_usage_anchor()` 取该会话最近一次 LLM 调用的
   真实 `prompt_tokens`（按 `created_at` 取最新 —— `usage.seq` 存的是 runner 迭代轮数，
   每起一次新 run 都从 1 重计，跨 run 不可比），加上**锚点之后**新增消息的字符增量。
   结果带 `basis` 字段（`usage_anchor+delta` / `chars_only`），不把两种口径混成同一个数。
2. **同刻算增量**（`>=` 而不是 `>`）：本机 `time.time()` 粒度约 15.6ms 一个 tick，连着两次写
   常拿到同一个 float；`usage` 行写在 LLM 响应之后，所以同刻落库的消息只可能是"请求构建之后
   新增的"。用 `>` 会把这些静默丢掉 —— 低估正是本次要修的失效方向，宁可同刻多算一条。
3. **上限来自 keys.json 的 model 级 `context_window`**，经 `LLMKey.model_context_windows` →
   `GET /llm/pool/models` 每条目的 `context_window` → `ChatPanel.pick_context_window()`
   （同名多 provider 取**最小**非零值）传到 `Compactor`。
4. **没填 = 上限未知 ⇒ 不判压缩**，并打一行说明（`should_compact` 返回 False 前记 INFO），
   不再用一个编出来的默认值去比。
5. **判据修准 ≠ 接线**：本轮不接主动触发。`runner._log_context_estimate()` 每轮打
   `context estimate: ... anchor=… delta=… limit=…`，只观测不改调度；实际压缩仍只在 provider
   真返回 413（`ContextOverflow`）时被动发生。

## Considered Options

- **保守默认上限**（32000 或换成 128000）：被 97090 这条实测否证 —— 任何"先填个数再说"的默认值
  都会继续让"从没压缩"伪装成"不需要压缩"。未知就报未知，比假精确诚实。
- **给 `usage` 表加 `message_count` 列做精确配对**：能消掉同刻归属的模糊，但要 schema 迁移 +
  历史行回填；而锚点偏保守只会多算一条消息，下一轮 413 会自证，不值这个改动面。
- **按 model tokenizer 精确估算**：`ADR-0027` 定下 client 引擎不依赖 `server` 包，而 tokenizer
  在 `server/llm_pool/compression.py` 一侧；8-12 那次就是因为这个把 try-import 删掉的。
- **顺手把主动触发接进主循环**：判据修准之前接线，后果是每轮都压 —— 估算 27196 对阈值 27200
  只差 4 token，摘要会把 cheap 池配额烧穿。这一格留作独立决定。

## Consequences

- 同名模型跨 provider 时，窗口取**最小非零值**；一家填错偏小会让该模型更容易被判"接近上限"。
  `context_window` 因此是"要人负责填"的数据，不是运行时可推导的量。
- `keys.json` 的写侧（`server/llm_pool/key_store.py::_to_dict`）与 Key 表单
  （`client/widgets/key_form.py::merge_edited_model`）都必须**原样带回**加载器/表单不管辖的字段。
  这两处都是白名单式重建 dict 的写法，漏一个键就等于"用户填的值被一次 GUI 保存静默抹掉"
  —— 各有一条用例钉着（R7/R8/R12 摘改轮）。
- 想真正启用主动压缩还需要两步：给在用的 model 填 `context_window`，以及把 `should_compact()`
  接进 runner 主循环（本轮刻意留白，见 Decision 5）。
- `PruningConfig` / `ContextBudget` 两个结构仍是"已设计未接线"（`compactor.py` 里只收不读），
  本轮没有顺手启用它们 —— 精裁剪要排在判据之后，不是之前。

## References

- 代码：`client/core/agent/compactor.py`（`UsageAnchor`、`ContextEstimate`、
  `estimate_context_tokens`、`should_compact`）、`client/core/agent/event_store.py`
  （`get_last_usage_anchor`）、`client/core/agent/runner.py`（`_log_context_estimate`）、
  `server/llm_pool/key_store.py`（`context_window` 读写两侧）、`server/llm_pool/types.py`
  （`LLMKey.model_context_windows`）、`server/llm_pool/pool.py`（`list_models`）、
  `client/panels/chat.py`（`pick_context_window`）
- 测试：`tests/agent_runner/test_context_estimate.py`、
  `tests/llm_vision/test_context_window_plumbing.py`
- 相邻决策：`ADR-0027`（client 引擎不依赖 server 包，正是 tokenizer 方案被拒的理由）、
  `docs/chat-engine.md` §6.3（口径与"未接线"现状）
