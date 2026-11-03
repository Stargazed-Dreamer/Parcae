# VL 降级链的遍历方向按 use_case 分策略；链的观察口与告警必须与选路同源

状态：accepted（2026-10-11，Ticket 03/06/07 落地时追记）

## Context

Ticket 02 把 VL 的候选粒度从"一个 key 一个模型"升到"(key, model)"，同 key 内多模型降级链
从此存在。但这条链有三个洞，三个都是"代码里对、人看不见/看不懂"的形态：

1. **方向被硬编码成"便宜优先"**。`_pick_model_for_tier` 一律 tier 升序，于是
   `vl_vision`（兜底定位，输出坐标会直接变成点击位置）也只能拿最便宜的模型——
   而这一档恰恰是"错的比贵的更糟"。方向在一个地方写死，其他 use_case 无从表达自己的取舍。
2. **观察口与选路不同源**。`GET /vision/providers` 的 `models[]` 恒按 tier 升序展示，
   代表模型取升序首个未判死；而 `vl_vision` 真实选路走的是 tier 高端 ⇒
   **面板首行的"当前模型"不是接下来会调的模型**。状态口说 A、选路走 B，且没人知道。
3. **故障没有任何主动告知**。2026-10 的事故是"上游下架模型 ID → 5000 次调用全部
   落到 OCR 兜底 → **坏了四天零人知晓**"。判死标记、聚合 `daily_exhausted`、
   降级日志全都在，但没有任何一条通路把"正在降级/已全停"送到人眼前。

三者共同的问题：链只活在代码与日志里，谁也没法靠它做判断。

## Decision

1. **方向是 use_case 的属性，不是全局常量**（`server/llm_pool/key_store.py`）。
   新增用户 `model_order_for_use_case()` → `"asc" | "desc"`；降序白名单**只有一个**
   `vl_vision`，其余（含**未注册 use_case 与不传**）一律升序。落点是三个公有助手：
   `model_order_for_use_case` / `ordered_models_for_use_case`（返回顺序**即降级链顺序**）/
   `pick_model_for_use_case`（首个 + `preferred_model` 软偏好）。
   `_pick_model_for_tier` 的过滤+排序上移后变薄包装（不传 use_case ⇒ 恒升序），
   **签名与返回形状零改动**；`resolve_keys(use_case)` 把 use_case 传下去即可，
   VL 侧 `_pick_provider` 一行没改就吃到新方向。
   ⛔ tier 语义（成本/质量档）不被重新定义——这里只决定同一批候选的**遍历方向**。

2. **观察口必须能表达"在看哪条链"**（`server/vl/vision.py` + `server/vl/remote_vl.py`）。
   `GET /vision/providers?use_case=…`（默认 `vl_vision`）把 use_case 透传给
   `status(use_case=…)`，由 `_all_vl_candidates(use_case=…)` 定顺序；
   `current` = **该链首个未判死模型**，于是它才真的是"接下来会调的那个"。
   **不传 use_case 时逐字保持升序**（`/health`、`/vision/status` 与既有测试全走这条）。
   `models[]` 每项补齐 `{tier, enabled, dead_reason}`：
   - 未启用的 vl 模型只以"未启用"追加在链尾**展示**，**不进候选链**
     （进去了会让 `_all_models_dead_today` 与聚合判据多算）；
   - 判死原因按 `(key_id, model)` 单独记账（`_dead_model_reasons`，与 `_dead_models_today`
     同生共死）；同日重复登记**保留首个非空原因**（第一次才是真病因）。
   解冻倒计时由服务端算一次（`dead_reset_in_s`，距本地 00:00），不让客户端各推各的口径。

3. **告警与聚合标记同源，且必须去重**（`server/vl/remote_vl.py`）。
   - 一次调用内发生过模型降级 ⇒ 收件箱 `category=vl_model_degraded`（含被跳过的模型 + 原因摘要）；
   - 全部 `(key,model)` 候选被标死 ⇒ `category=vl_all_models_dead`，**挂在
     `_mark_daily_exhausted(kind="model_unavailable")` 同一个触发点**——
     否则就会长出"面板说全停、告警说还好"的第二套真相。额度/过载耗尽
     （`quota_exhausted` / `upstream_overload`）**不触发**全停告警，不把配额问题冒充成四级事故；
   - 去重粒度：降级按 (当天, key_id, model)、全停按天；每条另带 `dedupe_key`，
     由收件箱的 partial unique index 做**结构性兜底**（进程重启也拦得住）；
   - **复位 = 再给一次机会**：`reset_daily_state()` 一并清掉去重标记，于是"复了又坏"能再报一次；
   - **告警是旁路**：投递失败只留一行 WARNING，绝不阻断判死与选路。

## Considered Options

- **给 `vl_vision` 加一条 `preferred_models` 白名单配置**（Ticket 03 票面给的备选）：**拒**。
  它引入**第二真源**（候选集与优先级各说各话），且破坏票面自己那条
  "不新增配置项也能工作"——用 tier 表达方向的代价只是"同 tier 内顺序"，
  而白名单的代价是"keys.json 与代码里各有一份优先级"。
- **状态口保持恒升序，只加一句"这是全 tier 视图，实际按窗口再筛"的标注**：**拒**。
  缺口是"面板首行指错模型"，标注不会让那一行变对；而 `current` 的本义就是
  "现在会调的那个"，让它长期说谎比缺一个视图更糟。
- **统一成一个 use_case（只保留一条链）**：**拒**。`vl_ocr`（高频截图 OCR、便宜优先）、
  `vl_vision`（定位兜底、质量优先）、`vl_activity_tracker`（活动追踪）是三条**真实并存**的
  调用路径（`server/screen/routes.py` / `server/vl/feedback_vl.py` / 
  `server/activity_tracker/loop_actions.py`），砍成一条等于让其中两条拿错取舍。
- **为告警另建一条通路（专门的告警表 / 通知通道）**：**拒**。收件箱
  （`server/inbox.py`）已经在客户端有面板、有状态机、有去重键，新建一套只会多一份"哪里看告警"的歧义。
- **告警不做去重，交给调用方自觉**：**拒**。降级会被 loop 反复撞到，
  实测同一 `(key,model)` 一天能撞几十次——不去重等于把收件箱变成刷屏器，
  而"刷屏"的自然结果是再次"没人看"。

## Consequences

- **收益**：`vl_vision` 拿质量档模型（坐标错了比贵了更糟）；面板首行的"在服务"与真实选路同源；
  "正在降级 / 已全停"不再需要人主动翻日志。
- **代价 1**：`/vision/providers` 的 `models[]` 顺序**依赖 use_case 参数**，
  不再是一个恒定视图——读它的人必须知道自己在看哪条链。缓解：响应回显 `use_case`，
  面板顶部的链路视角下拉与 Provider/Model 摘要跟随该视角。
- **代价 2**：`_dead_models_today`（set）之外多了一张 `_dead_model_reasons`（dict）——
  两表必须同生共死（跨日换血、`reset_daily_state`、`_mark_model_dead_today` 的换日期分支
  三处都要一起清）。保持 set 形状是因为它被 failover 直接当排除集合用（另有 4 处消费者）。
- **代价 3**：告警会成为收件箱的**持续来源**。去重键与复位清空是必须的配对；
  若将来出现"天天报同一条"，先查是不是去重键没有按天分段。
- **不做的事**：不新增配置项；不改 tier 语义；不给模型级加"失效时间戳"
  （失效只有"当天"这一档，00:00 整表解除）；不把告警做成会阻断选路的强依赖。

## References

- Ticket 真源：`temp/sdd/remote-vl-model-fallback/tickets.md`（Ticket 02/03/06/07）与 `spec.md`
- 代码：`server/llm_pool/key_store.py`（`model_order_for_use_case` / `ordered_models_for_use_case` /
  `pick_model_for_use_case` / `_pick_model_for_tier`）、
  `server/vl/remote_vl.py`（`_all_vl_candidates(use_case)` / `status(use_case)` /
  `_build_models_info` / `_dead_reset_in_s` / `_dead_model_reasons` /
  `_push_inbox` / `_alert_model_degraded` / `_alert_all_candidates_dead`）、
  `server/vl/vision.py`（`GET /vision/providers?use_case=`）、
  `client/panels/llm_pool.py`（VL 子面板 key→model 两层树 + 链路视角 + 行内复位）
- 告警通路：`server/inbox.py`（`get_store().create`、`dedupe_key` partial unique index）
- 测试：`tests/llm_vision/test_vl_ticket03_order.py`、`test_vl_ticket06_chain_visibility.py`、
  `test_vl_ticket07_alerts.py`、`tests/client_ui/test_model_pool_vl_chain.py`；
  摘改矩阵 `temp/20261011_vl_t03_mutation.py` / `_t06_mutation.py` / `_t07_mutation.py`
- 相关 ADR：**0043**（额度/429 按 provider 记账）、**0044**（模型级失效按 `(key_id, model)`、
  锁与并发按 key）——本文与它们的分工是"0043 管额度、0044 管粒度、本文管方向与可见性"
- 变更记录：`CHANGELOG.md` 2026-10-11 三条
