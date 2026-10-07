# 审批严格性做成四级并与 dcg 的 enabled 解耦，拦截判定改两道门

状态：accepted（2026-10-04 追记；决策本身落地于 2026-07 的 `command_guard.approval_level`）

## Context

审批链路原本只有一个开关 `command_guard.enabled`，而它的语义是混的：

- `enabled=false` 关掉的是 **dcg 二进制的危险命令预检查**；
- 但 HTTP 端点审批（`server/core/middleware.py`）和 MCP 网关审批（`server/core/mcp_gateway.py`）
  走的是 `route_tags` 的 safety 分类，**完全不读这个 enabled**。

结果就是用户实测到的那句困惑："dcg=false 为什么还审批"——开关关掉了一层，另一层照拦，
而代码里没有任何地方说明"这是两层"。同时按场景精细控制拦截范围的粒度也太粗：
要么全按 strict 清单拦，要么整体关掉，没有"只拦代码执行类"这一档。

## Decision

1. **两个概念拆成两个配置**：`enabled` 只管 dcg 二进制预检查；新增
   `[command_guard].approval_level` 管端点审批面，四级
   （`server/route_tags.py:24-29`）：
   `strict`（完整清单）/ `moderate`（strict 减低风险运维端点）/ `loose`（只拦代码执行类）/
   `none`（不拦任何端点，dcg 预检查仍独立生效）。
2. **两道门，不是一道**：静态分类先算出 `safety`，中间件与网关在
   `safety == 'approval_required'` 时**再调一次运行时判定**确认——
   `server/core/middleware.py:413-414` 用 `classify_safety_runtime(method, path)`，
   `server/core/mcp_gateway.py:162-163` 用 `classify_safety_runtime_by_op(op)`
   （网关侧只有 `operation_id` 没有 path，故启动时建 `_op_to_path` 反查表）。
   运行时判定把当前 level 下不该拦的端点降级为 safe 放行。
3. **改 level 不必重启**：`build_safety_lookup()` / `build_operation_safety_map()`
   **按当前 `approval_level` 过滤**，是 GUI 改档后即时生效的那一侧。
   ⚠️ 这里有一条常见的误读（曾写进项目记忆）：`auto_tag_routes()` 才是
   "按 strict 完整清单写 OpenAPI 元数据"的那个——它展示的是**理论最大审批面**，
   故意不随 level 变，两者别混（见 `server/route_tags.py:31-33`）。
4. **降级清单是显式的两份**：`_MODERATE_EXCLUDE_PATTERNS`（`server/route_tags.py:93`）与
   `_LOOSE_APPROVAL_PATTERNS`（同文件 102），改哪一档少拦什么，直接读代码可得。

## Considered Options

- **只留一个 enabled 布尔**：被拒。它把"dcg 预检查"和"端点审批"两件事绑死，
  关一层必须连带关另一层，正是用户困惑的来源。
- **按 level 重建整套 safety 数据结构**：被拒。`auto_tag_routes` 写入的 OpenAPI 元数据
  跟着 level 漂，`/docs` 里看到的审批面会随配置变，排查时无法判断"是没拦还是没显示"。
  展示面固定为理论最大、生效面按 level 过滤，是把这两件事分开。
- **改 level 后要求重启后端**：被拒。这是日常会调的旋钮，重启代价高于收益。

## Consequences

- 正面：`dcg=false 为什么还审批`有确定答案（两层解耦）；按场景收放拦截面不再靠改代码。
- 代价 1：**排查"这个端点为什么没拦"必须看两处**——静态 safety 与运行时二次确认。
  只读 `safety_lookup` 会得到"应该拦"的错误结论。
- 代价 2：两份降级清单（moderate/loose）要随端点增删人工维护；漏更新的表现是
  **静默少拦**，不是报错。
- 代价 3：`none` 档下只剩 dcg 一层。这是有意留的口子（用户自决），不是防御缺失。
- 已知移出：loop 控制（pause/resume/run）已于 2026-08-25 移出 strict 清单，
  理由是低风险运维且有自动熔断兜底（`server/route_tags.py:26` 的注记）。

## References

- 代码：`server/route_tags.py`（四级说明 24-29、清单 93/102、`get_approval_level` 112）、
  `server/core/middleware.py:413`、`server/core/mcp_gateway.py:162`
- 相关 ADR：0018（route_tags 保持 fail-open + 二级防护）、0005（审批领域文件整合）、
  0037（授权状态只由"新批准"写入）
- 相关文档：`SECURITY-RISKS.md` §14、`docs/operations-manual.md`
- 本 ADR 的起因：清理记忆库存量时复核 `fact_type='architecture_decision'` 的 5 条，
  其中 `approval_strictness_levels` 的正文把第 3 条**说反了**（把"按 level 过滤"的
  对象安到了 `auto_tag_routes` 上）。按项目规矩（架构决策的真源是 ADR），
  这条决策从记忆升级到本文，记忆侧改为指向本文。
