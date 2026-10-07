---
name: auto_shutdown
description: >
  挂机监控自动关机：agent 自写检测 loop（轮询进程/日志/API/文件/时间等条件），
  条件满足后调 POST /auto_shutdown/trigger 触发 120s 倒计时关机（可 /auto_shutdown/cancel 中止），
  关机不走 computer use。当用户说"挂机到 XX 完成自动关机"、"游戏挂机关机"、"等下载完关机"、
  "定时关机"、"X 点关机"、"条件关机"、"睡前关机"、"后台关机"时触发。
task_type: adhoc.auto_shutdown
---

# auto_shutdown — 自动关机（挂机监控）

> task_type: `adhoc.auto_shutdown`
>
> 关联端点：`POST /auto_shutdown/trigger` + `POST /auto_shutdown/cancel`（[server/auto_shutdown.py](file:///<project_root>/server/auto_shutdown.py)）
>
> 历史背景：原 `workspace/auto_shutdown/monitor.py` 独立 CLI 子进程已于 2026-08-03 删除，关机流程收进后端端点。agent 现在在 Trae 会话里自己跑检测 loop，不依赖独立子进程。

## 何时使用

用户说以下任一时：
- "挂机到 XX 完成自动关机" / "游戏挂机关机" / "等下载完关机"
- "23:30 自动关机" / "定时关机" / "X 点关机"
- "等 Trae 里另一个 agent 完成后关机" / "条件关机"
- "睡前关机" / "后台关机"

## 工作流（agent 自己跑 loop + 调端点）

### 第 0 步：注册看门狗授权（⛔ 强制前置，任何自动关机任务通用，跳过必失败）

> 2026-09-22 事故教训：过夜探针没有 watchdog 授权，真触发时被 403 拦截，
> 且 403 发生在 inbox 推送之前 ⇒ 失败完全静默，机器没关机卡死到中午。
> **没有这一步，后面全部白做。**

```
0a. 调 POST /screen/control/request（MCP 工具 screen_request_control）
    参数：mode="watchdog", task_description="挂机关机：<触发条件>",
          max_duration_hours=预计挂机时长向上取整（不传则默认 10h 上限！）,
          needs_shutdown=true
    ⚠ 弹窗里"允许关机"复选框**默认勾选**，用户取消勾选 ⇒ shutdown_permitted=false
      ⇒ /auto_shutdown/trigger 返回 403 shutdown_not_permitted。
    ⚠ 已经有 watchdog 但缺关机位：带 needs_shutdown=true 再请求属于**权限升级**，
      会弹第二次确认窗（不必先 screen_release_control 把 watchdog 丢掉）；不带该参数
      则短路成 status="skipped"，现有授权一律不改。升级这次会按弹窗时长重建 watchdog
      硬上限时钟（granted_at 重置），不是无损补位。
    ⚠ 返回 status="cancelled" ⇒ 用户拒绝或 30s 超时（fail-closed）：
      不得继续，也不得反复重发施压，明确告知用户"未获关机授权，自动关机任务无法保证执行"。
    ⚠ max_duration_hours 作为弹窗时长 spinbox 的**初值**下传，用户可上调；
      用户没动就按请求值授权（2026-09-30 之前该值没传进弹窗，一律按默认 10h 授权）。

0b. 验证（不看返回值猜，必须复查）：
    GET /health → screen.takeover_persistent
    要求 mode == "watchdog" 且 shutdown_permitted == true。
    （注意：GET /screen/status 不含会话权限字段，别用它验证）
    两个条件缺一个 ⇒ 回到 0a 重新请求或停任务。

0c. watchdog 硬上限（机制说明，非隐患）：
    watchdog 有 max_duration_seconds 硬上限，到期自动降级为 NORMAL
    且 shutdown_permitted 归零。默认 10h 是有意设计（过夜足够），
    弹窗里用户可自行设置时长；agent 也可用 max_duration_hours 显式传。
    挂机时长明显超过上限时（如挂机 14h）传对应值即可。
```

### 第 1-5 步：检测 + 触发

```
1. 确认触发条件 + task_id
   - task_id 用于截屏目录隔离（temp/auto_shutdown/<task_id>/final_screenshot.png）
   - reason 描述触发原因（agent 描述检测到什么）

2. agent 自己写检测 loop
   - 用 time.sleep 或 asyncio.sleep 控制检测间隔（60s 常用）
   - 检测函数 agent 自行编写（识图 / 查日志 / 查 API / 查文件 / 查时间）
   - 不依赖任何内置条件检测（旧 6 种条件已删除）
   - ⛔ 独立探针脚本必须：启动时预检 0b 的权限；触发失败要重试 + 落告警文件，
     禁止"单发即退"（参考 workspace/windows_forensics/tools/probes/mem_watch.py 的 2026-09-23 修正版）

3. 条件满足 → 调 POST /auto_shutdown/trigger
   - 端点内部自动：截屏 + 推 inbox + 调 shutdown /s /t 120
   - 120s 倒计时（不是 60s，多 60s 宽限用户刹停）
   - ⚠ dry_run 默认 True，生产必须显式传 dry_run=false，否则只推 inbox 不关机
   - 返回 {success, task_id, trigger_count, shutdown_scheduled, dry_run, screenshot_path, cancel_command}

4. 用户反悔 → 调 POST /auto_shutdown/cancel
   - 端点调 shutdown /a 中止倒计时
   - 幂等：重复调不报错

5. 关机完成 / 用户中止
```

## 关键约束（强制）

### 1. 调 trigger 前先关闭有窗口的程序

**模拟器、全屏应用、有未保存数据的程序在系统直接关机时会阻碍关机流程**（弹窗阻止 / 资源占用 / 文件锁）。agent 调 trigger 前应：

- 用 `list_windows` 查当前所有窗口
- 识别需要关闭的程序（模拟器如 MuMu/雷电/Nox、游戏客户端、IDE、有未保存文档的程序）
- 用 `screen_window_close` 或 `execute_action`（Alt+F4）逐个关闭
- **不要用 taskkill 强杀**（可能丢失用户数据），优先 graceful close
- 模拟器类程序关闭可能需要确认弹窗，agent 要处理

端点不强制此约束（端点只负责关机本身，关窗口是 agent 的 loop 责任）。

### 2. 120s 倒计时

`shutdown /s /t 120` 触发后 Windows 弹窗 120s 倒计时。用户可在 120s 内：
- 喊"取消关机" → agent 调 `/auto_shutdown/cancel`
- 或自行执行 `shutdown /a`

### 3. 关机命令绕过 command_guard

用户睡前无法审批 command_guard 弹窗，端点内部直接调 `subprocess.Popen(["shutdown", "/s", "/t", "120"])`，绕过 command_guard。这是设计取舍，已明确同意关机。

### 4. 端点幂等

- 重复调 trigger：递增 trigger_count，不报错
- 重复调 cancel：返回 success=True，不报错

### 5. 副作用容错

- 截屏失败（mss 异常）→ screenshot_path=None，仍触发关机
- inbox 推送失败（后端不可用）→ 记日志，仍触发关机
- 用户睡前 inbox 不重要，关机本身重要

## 端点参数

### POST /auto_shutdown/trigger

```json
{
  "task_id": "game_afk_20260803",  // 必填，截屏目录隔离用
  "reason": "游戏完成",             // 可选，默认 ""
  "screenshot": true,               // 可选，默认 true（关机瞬间截屏存档）
  "dry_run": false                  // 可选，⚠️ 默认 True（T04 安全优先：不显式传 false 就不会真关机，只推 inbox）
}
```

返回：
```json
{
  "success": true,
  "task_id": "game_afk_20260803",
  "trigger_count": 1,
  "shutdown_scheduled": true,
  "dry_run": false,
  "screenshot_path": "temp/auto_shutdown/game_afk_20260803/final_screenshot.png",
  "cancel_command": "shutdown /a"
}
```

### POST /auto_shutdown/cancel

```json
{"dry_run": false}  // 可选，⚠️ 默认 True（同 trigger，T04 安全优先）
```

返回：
```json
{
  "success": true,
  "dry_run": false,
  "cancel_command": "shutdown /a"
}
```

## MCP 工具调用

```
# 触发关机
localagent_advanced_tool(
    tool="auto_shutdown_trigger",
    params={"task_id": "game_afk", "reason": "游戏完成", "screenshot": true, "dry_run": false}
)

# 取消关机
localagent_advanced_tool(
    tool="auto_shutdown_cancel",
    params={"dry_run": false}
)
```

## 示例检测脚本（agent 自行编写，仅供参考）

### 示例 1：游戏完成检测（识图）

```python
import time
import urllib.request
import json

def check_game_done():
    """截图 + OCR 判断游戏是否完成"""
    # 用 MCP 工具：capture_screen + screen_ocr
    # 或用 localagent_advanced_tool(tool='screen_ocr', params={...})
    # 返回 True/False
    pass

task_id = "game_afk_20260803"
while not check_game_done():
    time.sleep(60)  # 60s 检测间隔

# 条件满足，先关窗口程序（模拟器等），再触发关机
# ... list_windows + screen_window_close ...

# 触发关机
data = json.dumps({"task_id": task_id, "reason": "游戏完成", "screenshot": True}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8766/auto_shutdown/trigger",
    data=data, headers={"Content-Type": "application/json"}, method="POST"
)
urllib.request.urlopen(req, timeout=10)
```

### 示例 2：定时关机（处理跨日边界）

```python
import time
from datetime import datetime, timedelta

def get_next_target(today_now: datetime, target_hhmm: str) -> datetime:
    """处理跨日边界：23:35 启动 target="23:30" → 明天 23:30"""
    h, m = map(int, target_hhmm.split(":"))
    target_today = today_now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target_today <= today_now:
        # 目标时间已过，定为明天
        return target_today + timedelta(days=1)
    return target_today

target = get_next_target(datetime.now(), "23:30")
while datetime.now() < target:
    time.sleep(30)

# 触发关机
# ... 调 /auto_shutdown/trigger ...
```

### 示例 3：文件出现检测

```python
import time
from pathlib import Path

flag_file = Path("temp/agent_done.flag")
while not flag_file.exists():
    time.sleep(30)

# 触发关机
# ... 调 /auto_shutdown/trigger ...
```

## 常见陷阱

1. **不要找 monitor.py**：旧独立子进程已删除，agent 自己跑 loop
2. **不要用 subprocess 直接调 shutdown**：必须走端点（端点内部统一处理截屏 + inbox + 命令）
3. **dry_run 测试**：测试时显式传 `dry_run=true`，生产传 `dry_run=false`
4. **关机前关窗口**：模拟器/全屏应用会阻碍关机，agent 调 trigger 前先关
5. **120s 不是 60s**：倒计时 120s，多 60s 宽限用户刹停
6. **inbox 失败正常**：用户睡前 inbox 不重要，关机本身重要
7. **trigger_count 不持久化**：后端重启归零，inbox 有历史记录可查

## 排查：「inbox 有关机条目但实际没关机」（2026-09-22 实战）

inbox 条目标题恒为「关机已触发」，**即使 dry_run=True 也一样**（inbox 推送在 dry_run 分支之前）——标题不可信，要查 payload：

1. **查 payload 的 `dry_run`**：只读查 `data/inbox.db`（表 `inbox_items`，SQLite，`?mode=ro` 打开）。`dry_run: true` ⇒ 端点根本没执行 shutdown 命令（设计如此，T04 安全网）。
2. **查 `temp/auto_shutdown/*.log`**：watchdog 探针脚本（如 `workspace/windows_forensics/tools/probes/mem_watch.py`）自己落日志。真触发被拒的签名：`TRIGGER FAILED HTTPError: HTTP Error 403: Forbidden`。
3. **403 = 关机权限未激活**：端点 T19 权限检查 `SessionManager.can_shutdown()`，需 **watchdog 模式 + 勾选允许关机**（`shutdown_permitted=True`）才放行。过夜无人值守时若没先开审批面板授权，必然 403 fail-closed。403 发生在 inbox 推送之前 ⇒ 权限被拒时**连 inbox 条目都不会有**。
4. **交叉验证真没关过机**：`wevtutil qe System "/q:*[System[(EventID=1074)]]" /rd:true /f:text`（只读查 1074 关机发起事件，时间为 UTC，本地 +8）。真执行过 `shutdown /s /t 120` 一定有 User32 1074 事件。
5. **探针脚本自身缺陷要补**：单发即退脚本触发失败后就退出（不重试、不推送告警）⇒ 无人值守场景会静默失效。已修正：见 `workspace/windows_forensics/tools/probes/mem_watch.py`（预检权限 + 重试 + 告警文件 + inbox 推送）。

## 看门狗模式联动（自然行为，不单独 spec）

- 看门狗授权的申请/查询端点：`POST /screen/control/request`（申请，mode="watchdog"）/ `GET /health` 的 `screen.takeover_persistent`（查询 mode/shutdown_permitted/max_duration_seconds）。~~`/screen/grant/status`~~ 端点不存在，是过时引用；`GET /screen/status` 也不含会话权限字段
- agent 每次 screen 操作成功会自动续期看门狗授权（仅对 idle 计时；**watchdog 的 max_duration 硬上限不因续期重置**，到点照样降级）
- 用户撤销看门狗授权 → agent 下次 screen 操作被拒 → agent 应识别此信号并停止 / 询问用户
- agent 触发关机时，看门狗授权自然失效（系统关机），无需显式撤销

## 相关文件

- [server/auto_shutdown.py](file:///<project_root>/server/auto_shutdown.py) — 端点实现
- [tests/test_auto_shutdown.py](file:///<project_root>/tests/test_auto_shutdown.py) — 测试（含 autouse 安全网 fixture 防真关机）
- `temp/sdd/auto-shutdown-refactor/spec.md` — SDD 规约（含危险操作测试铁律）
- `docs/dev-workflow.md` "危险操作测试铁律" 章节 — 测试安全规范
