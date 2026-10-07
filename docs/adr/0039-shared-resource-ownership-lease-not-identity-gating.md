# 共享资源用「归属记录 + 租约」而不是身份验证：浏览器 tab / 屏幕授权 / 终端

一台机器上同时有多个 agent（观测最多 4 个并发）和用户 GUI 在抢同一套共享资源：同一个调试浏览器
里的 tab、同一个屏幕授权会话、同一批后端终端。2026-09-30 探针所在的 tab 被"外部"关闭且**事后无法追责**
之后，我们把它定性为**归属缺失**而不是越权——没有可验证的"合法 owner"，大家本来就共享同一个浏览器实例。
于是决定：给每类资源记 owner、按有主/无主分档租约、把静默失效变成显式 409 + 可追溯的关闭原因；
屏幕与终端这两条已有闸门的链路**只补记录、不新加拦截**。

## Status

accepted (2026-10-02)

## Context

改造前的四个洞，每一个都真实咬过人：

- `SessionManager` 的 LRU 淘汰只看 `last_used_at`、**不看归属** ⇒ 别的 agent 开得勤，就能把你的
  活跃 session 静默关掉。
- idle 超时一刀切 900s，`last_used_at` 是 per-session 的 ⇒ 没人续租的老脚本与人在用的 tab 同等对待。
- `browser_session_create(url_pattern=...)` 命中别人的 tab 就**直接抢绑**，原持有者毫不知情。
- `close` 零校验，关完只留一行 log ⇒ 09-30 事故里"谁关的、为什么关"无法回答。

同一天还量到两处不一致：`cleanup` 里兜底默认值写成 300，而配置实际是 900；全局上限 20 在有
4 个 agent 并发时不够用，但把它调大又会放大"被别人的淘汰挤掉"的概率——这两个数字彼此牵制，
所以数值必须由人拍板而不是顺着代码推。

## Decision

1. **身份判据只有一个来源**：[lib/agent_identity.get_caller_id()](file:///<project_root>/lib/agent_identity.py)
   把 `X-Agent-Caller` 头映射成 caller id——无头 ⇒ `anonymous`（内部调用/测试）；空值或显式否定值
   ⇒ `gui`（人在操作）；有头则头的原值 strip 后直接当 id（旧值 `v6-lite-agent` 向后兼容，新调用方可传
   更具体的名字）。**gui 永远拥有最高权限**（可关/可抢任何资源），反方向不成立：agent 不可动 gui 的资源。
2. **浏览器：两档租约 + 抢绑需 force + 关闭墓碑**
   （[server/browser/session/manager.py](file:///<project_root>/server/browser/session/manager.py)、
   [state.py](file:///<project_root>/server/browser/session/state.py)）。
   - `TabSession.owner` 记创建者；idle 租约有主 900s（操作即续租）/ 无主 300s；**LRU 只淘汰 anonymous**。
   - 非 owner 关他人 session、`url_pattern` 命中他人有主活跃 tab ⇒ 409 `TAB_OWNED`；确要接管须显式
     `force=true`，并记 warning 日志（**不通知被抢方**——这是有意的，见 Consequences）。
   - 单个 caller 持有上限 10（`PER_CALLER_LIMIT`，gui 豁免）；全局上限 `session_max_count` 20→32，
     超限先淘汰 idle 最久的 anonymous，仍满则 `SESSION_LIMIT_REACHED`（此时 gui 同样被拒，
     但错误信息列出当前归属分布）。数值按"最多 4 agent 并发"由用户拍板。
   - 任何关闭路径（手动/超时/LRU/页面被外部关）都写 FIFO 200 的墓碑；后续操作经 `get_closed_hint`
     在 **9 处** not-found 响应里附上"已于 HH:MM:SS 被关闭：原因，owner=…"。静默死 session 从此读作
     `SESSION_CLOSED(reason)`。
3. **屏幕授权：只记录 owner，不加 409**。ADR-0037 的决策 17 规定覆盖必须用户在**新弹窗**确认才能发生，
   弹窗本身就是人类裁决；再叠一层程序闸门等于把同一次把关做两遍，还会挡掉正当的升级路径。
   （核验过：`grant` 覆盖不是静默的，只有新弹窗确认才生效。）
4. **终端：记 `owner_caller`，维持"走审批"而不是拒绝**
   （[server/exec.py](file:///<project_root>/server/exec.py)）。`owner_token` 不匹配时照旧创建
   approval 让用户裁决（用户明确拍板不改这个口子），但审批 message 附 `owner_caller`，让
   "这是谁的终端、谁要杀它"在审批窗上就可读。`exec_python` 内部 spawn 走
   `_terminal_spawn_impl(req, None)`，记 `anonymous`。

## Considered Options

- **身份验证 / 硬隔离**（token 严格校验 + 越权直接 403，甚至每个 agent 一套独立上下文）。被拒：
  威胁模型是"误伤别人的活"，不是"防恶意"；硬隔离会一起挡掉正当的跨 agent 交接（A 探完把同一个
  tab 交给 B 接手），还得为交接再造一套授权流。
- **只加日志、不改语义**。被拒：09-30 事故的形态正是"被关了却当场不知道、事后查不到"。日志只有
  事后翻开才有效，而调用方需要的是**这一次请求的响应里就带着答案**。
- **每个 agent 独立浏览器 profile**。被拒：登录态要各自复制，而本项目的立足点是复用用户已登录的
  调试实例（AGENTS.md 浏览器段），复制 profile 既增加隐私面又破坏这个前提。
- **全局上限继续调大、靠容量兜住冲突**。被拒：调大同时放大了"被别人的活跃度挤掉"和内存占用，
  而真正导致淘汰的是 LRU 不看归属——修判据比调数字有效。

## Consequences

**得到**：谁开的、谁关的、为什么关，从响应体里就能读到；有主 session 不再被别人的活跃度挤掉；
无主的老脚本/内部调用承担最短回收（300s），额度优先留给人和正在干活的 owner。

**付出**：
- **行为变更**：不带 `X-Agent-Caller` 头的老脚本从 900s 租约掉到 300s，且成为 LRU 的首选淘汰对象。
  补救姿势是补一个 header，不是把 anon 租约调回去。
- 新增 API 面：409 的三种错误码、`force` 参数（create 与 close 各一个）、
  新配置键 `[browser].session_anon_idle_timeout_secs`（默认 300）与 `session_max_count` 默认值改 32；
  已同步 [config.example.toml](file:///<project_root>/config.example.toml)、
  Settings 面板说明与 `展示文档/architecture_analysis/07_browser_api.md`（该处原先写死"上限 20"）。
- **抢绑不通知被抢方**，跨 agent 协调仍靠人；`force` 是"我知道我在抢"的显式声明，不是协商机制。
- 上限会拒，包括 gui（全局满且无 anonymous 可淘汰时）——选"拒 + 说明分布"而不是"挤掉别人"，
  代价是极端并发下人要手动关一个。
- 数值（10 / 32 / 900 / 300）不是推导出来的，是用户按当时并发观测拍的；并发结构变了要回来重审。
- 屏幕/终端这两条链路**故意没有对称的 409**：读代码时不要以为漏了，判据见 Decision 3、4。

## References

- [lib/agent_identity.py](file:///<project_root>/lib/agent_identity.py)（`get_caller_id`）
- [server/browser/session/manager.py](file:///<project_root>/server/browser/session/manager.py)（租约、上限、墓碑）
- [server/browser/session_endpoints.py](file:///<project_root>/server/browser/session_endpoints.py)（`force` 与 409 契约）
- [server/exec.py](file:///<project_root>/server/exec.py)（`owner_caller`、审批 message）
- [server/screen/session/manager.py](file:///<project_root>/server/screen/session/manager.py)（授权 owner 展示）
- ADR-0037（决策 17：覆盖只由新批准写入——本 ADR 第 3 条的前提）
- ADR-0038（同期的 patchright 迁移，console 按需捕获与本批同属浏览器会话这条线）
