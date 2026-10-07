# 浏览器自动化库换用 patchright：反检测只做协议层减法，不做 JS 加法伪装

2026-09-30 红蓝对抗实测到原版 Playwright 的 `connect_over_cdp` 会**逐 page/frame 发 `Runtime.enable`**，
而 console 序列化预览的构建走对象原型链枚举——页内 JS 用 `ownKeys` 陷阱就能测出来，这是 V8 2025-05
修掉 stack getter 陷阱之后**残余的协议层检测面**，JS 伪装治不了。决定全量迁到 API 兼容 fork
[patchright](file:///<project_root>/pyproject.toml)（`patchright>=1.63.0`，不发 `Runtime.enable`），
并把 `console_logs` 改成**按需**开启捕获：用一次可解释的功能取舍（首次读取之前的日志拿不到），
换检测暴露面从"每个 tab 永久"缩到"agent 显式要日志的那几个 tab / 单页单窗口"。

## Status

accepted (2026-09-30)

## Context

本项目的浏览器链路连的是**用户自己已经登录的调试浏览器**（CDP 9222，`connect_over_cdp`，
不启动新实例）。这条前提决定了暴露面的性质：不是"我们注入过什么"，而是"我们的 CDP 会话
在页面看来长什么样"。页面能观察到的协议行为，伪装补丁改不掉。

当日红蓝对抗的做法是同页对照——一个 tab 由 agent 驱动、一个作干净基线，两边跑同一套检测向量。
结果：原版 Playwright 在**主 frame 与 iframe 均触发** `ownKeys` 陷阱；patchright 与干净基线一致、
检测向量清零。相关实验脚本已归档在 `temp/browser_mgmt_audit_20260930/`（temp 会被清，**不作长期引用**；
留在仓库里的复现手段见 Consequences 的"付出"最后一条）。

`docs/browser-anti-detection.md` 早就定死了一条铁律：**不注入任何 JS 伪装补丁**（实测无收益，
且每个补丁本身新增可检测面）。所以当时能走的路只剩"减法"——把库自己制造的那部分流量减掉。

## Decision

1. **全量迁移到 patchright**：29 个文件 31 处 import，包括两处容易被漏掉的**内嵌命令模板字符串**
   （[server/templates.py](file:///<project_root>/server/templates.py) 的浏览器脚本模板、
   [playwright_executor.py](file:///<project_root>/server/browser/playwright_executor.py) 的
   `_build_playwright_header()`）。全仓 `chromium.launch()` 0 使用，因此**不需要**下载 patchright 版
   浏览器内核，仍然连用户自己的调试实例。
2. **`console_logs` 改为按需 `Runtime.enable`**（先承认原理性代价：主 world 不发 `Runtime.enable`
   ⇒ `page.on("console")` 收不到 `consoleAPICalled`，实测 9 → 0）：
   - 持久 session：首次调用 `browser_console_logs` 即对该 session **粘性开启**捕获
     （[SessionManager.enable_console_capture()](file:///<project_root>/server/browser/session/manager.py)
     幂等，直到 session 关闭）。**开启之前产生的日志不可见**。
   - 无 session 的一次性抓取：窗口式 enable——只在 `capture_ms` 窗口内开，抓完立即
     `Runtime.disable`（[routes.py](file:///<project_root>/server/browser/routes.py) 的子进程模板）。
   - 两条路径的取舍都写在端点 docstring 里，调用方读得到"为什么我少了前一段日志"。

## Considered Options

- **注入 JS 伪装补丁**（navigator/webdriver 属性那一类）。被拒：违反
  [`docs/browser-anti-detection.md`](file:///<project_root>/docs/browser-anti-detection.md) 铁律，
  且这条痕迹根本不在 JS 属性层——它来自 CDP 会话本身，补丁改不到。
- **CDP `Log.enable` 兜底 console**。被拒：Chrome 140 实测证伪——不发 `Runtime.enable` 时
  console-api 与 network 源的 `entryAdded` 均**零事件**。这条曾经是本迁移的默认方案，写下来是为了
  阻止下一个人再试一遍。
- **保持官方库、每个 session 常开 `Runtime.enable`**。被拒：功能一点不减，但恰好保留了被实测抓到的
  那个永久暴露面，等于本次迁移没发生。
- **直接砍掉 `console_logs`**。被拒：它有真实消费方（页面报错排障），砍掉省下的暴露面与付出的
  能力不对称。

## Consequences

**得到**：同页对照下检测向量清零、与干净基线不可区分；console 排障能力保留，只是变成需要显式动作。

**付出**：
- `browser_console_logs` 的**语义变了**：首次调用之前的日志不可见；一次性抓取只在窗口期内有效。
  这是刻意的，端点 docstring 与本文件是唯一的解释来源。
- 依赖第三方 fork：patchright 需跟随上游 Playwright 版本演进，`pyproject.toml` 只给下限
  （`>=1.63.0`），升级由 uv 决定；上游断更时要能随时退回官方库（代码面只有 import 与 console 两处差异）。
- 迁移中发现并一起修掉一处**与库无关**的既有缺陷：`browser_navigate` 的 back/forward 属 bfcache
  恢复型导航，Chrome 不重发 `domcontentloaded`/`load`，原先仍会 `wait_for_load_state` 吃满 15s
  超时——两库 A/B 行为一致，证明不是迁移引入的。恢复型导航现跳过第二段等待（e2e 导航组 47.47s → 3.43s）。
- 反检测相关的改动**必须先看** `docs/browser-anti-detection.md`；想加任何"伪装/反爬"补丁前先回到
  本文件的路由：做减法，不做加法。

## References

- [pyproject.toml](file:///<project_root>/pyproject.toml)（依赖与迁移原因注释）
- [server/browser/session/manager.py](file:///<project_root>/server/browser/session/manager.py)（`enable_console_capture`）
- [server/browser/routes.py](file:///<project_root>/server/browser/routes.py)（console_logs 按需/窗口式、back/forward 等待修复）
- [docs/browser-anti-detection.md](file:///<project_root>/docs/browser-anti-detection.md)（禁加法伪装铁律与实测证据）
- [tools/browser/fingerprint_selfcheck.py](file:///<project_root>/tools/browser/fingerprint_selfcheck.py)（仓库内的指纹自检复现件）
- ADR-0039（同期落地的共享资源归属/租约，与本迁移同属多 agent 并发浏览器会话这条线）
