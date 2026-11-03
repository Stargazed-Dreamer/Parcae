# 操作手册（Operations Manual）

从 AGENTS.md 拆出的操作性指南：后端运维、终端 API、触发型任务（日总结/挂机关机/浏览器经验）、记忆系统、工具脚本、GUI 性能。AGENTS.md 仅保留每次会话必读的核心 agent 行为规范。

## 后端重启流程

### 一句话

**改代码不等于要重启。** 默认不动后端，让改动等下一次自然重启生效。

### 一、先判断：这次改动不重启会不会「不生效」

只有「进程启动时读一次、之后不再读」的东西，不重启就永远是旧值：

| 类型 | 例子 |
|---|---|
| 注册表 / 常量表 | `server/todos/*`、`mcp_whitelist.py`、`browser/site_lessons.py`、`_WIPE_ALERT_KINDS` |
| 启动时装配的映射 | 路由表、safety 清单、`init_op_path_map` 建的反查表 |

判断方法：看这个值是不是在模块 import 或 startup 事件里就读进了内存。
**是 → 需要重启才生效；否 → 不用管**。请求进来才执行的代码（绝大多数 endpoint、
service、工具函数）下一次重启自然带上新逻辑，不需要为它专门重启。

### 二、即使需要重启，也不等于「现在就重启」

先看两件事：

1. **手上有没有活**：在途接口调用、批量任务、调试浏览器 / 终端、用户正在跑的东西。
2. **旧定义会不会先闯祸**：注册表类改动仍在内存里按旧 cron 走，查 `next_run_at`，
   看它会不会在你打算重启的时刻之前触发。

两件都不急 ⇒ **改动落盘、记为「待生效」**，等自然边界（用户点头 / 活干完 /
下次本来就要重启时合并）。回报时写明「已落盘、未生效、需重启」，不要描述成已修好。

### 三、谁来决定重启

**用户。** agent 不得自行决定重启后端（真源 `.agents/memory/TOPIC_backend.md` 第十三节）：

- 三类**许可**（不是义务）：① 注册表类改动且确实需要生效；② 用户点名；③ 端口/环境治理且已安排替身。
- ⛔「收工了 / 测试完了 / 我想验证一下」都**不是**理由。后端是常驻服务，默认留在原地。
- ⛔⛔ **已有实例在跑时不要起测试实例**：两者共用 `data/runtime/server_port.json`，后起的会覆盖前者的
  端口声明 ⇒ 前者 `GET /runtime/port` 回退成 `config.toml` 的首选端口，所有按该端点找后端的消费方连错端口（10-20 实测）。
  收尾按 PID 精确 `taskkill`，并用 `netstat` + `server_port.json` 的 `pid` **双确认**存量实例还在。

> 下面是「确实要重启时」的操作步骤。**先过完上面三节再往下读**，不要把它当成改完代码的默认下一步。

### 四、操作步骤（仅在确实要重启时）

顺序不能颠倒，否则会关不掉旧进程或打不开新进程。

#### 1. 优雅关闭旧后端

> 用 `start.bat` 启动时，这一步已**自动化**：外壳会先 `POST /shutdown` 再启动新实例
> （见 §2 的「重复启动先优雅关闭」）。下面是手动场景仍要用的原始步骤。

调用 `POST /shutdown` 接口，后端收到后会自动退出（0.5秒延迟后 `os._exit(0)`）：

```bash
# 方式一：curl（推荐）
curl -X POST http://127.0.0.1:8766/shutdown

# 方式二：PowerShell
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8766/shutdown

# 方式三：MCP 工具（如果 MCP 还能用）
mcp_parcae_exec_python 执行: import urllib.request; urllib.request.urlopen("http://127.0.0.1:8766/shutdown")
```

**注意**：
- 不要用 `taskkill /F` 强杀进程，可能导致端口未释放、临时文件未清理
- 如果 `/shutdown` 无响应（后端已卡死），才用 `taskkill /PID <pid> /F` 强杀
- 关闭后等 2-3 秒再启动新进程，确保端口释放

#### 2. 启动新后端

**用户手动启动**：运行根目录的 `start.bat`，它会自动完成：

1. **检测管理员权限** → 无权限则自动 UAC 提权（弹窗申请管理员权限）
2. **杀掉占用 8766 端口的旧进程** → `netstat` 查找 + `taskkill /F`
3. **启动后端** → `.venv\Scripts\python.exe -m server.main`（经下面这个外壳）

> 📋 **退出监控（2026-10-30 新增，`tools/runtime/run_backend.py`）**
> 第 3 步现在走一层外壳。外壳**只记录、不改后端行为**（stdin 仍接控制台，Ctrl+C 照旧两边都收）：
> - `data/logs/backend_console_YYYYMMDD.log` —— 子进程 stdout+**stderr 全量**（以前 traceback 只打在窗口里，窗口一关就丢）
> - `data/logs/backend_shell_YYYYMMDD.log` —— **外壳自己**的输出：启动横幅、接管过程、每 5 分钟一行采样、退出摘要
>   （⛔ 与上一份**必须分开**：两个写入方共用一个文件时，子进程把文件写大之后外壳的写入会**整段丢掉**，
>   而且 `ConsoleTee` 的 `except OSError: pass` 会把它吞成静默——2026-11-01 实测连「子进程已启动」都没留下）
> - `data/logs/backend_mem_YYYYMMDD.jsonl` —— 每 30s 一条：**真正干活的后端进程**的 private commit /
>   working set / 句柄 / 线程 + 系统可用内存与页面文件水位；窗口里每 5 分钟打一行
>   （⚠ 采的是**进程树里真正跑 `server.main` 的那个**，不是 `Popen` 句柄——见下面那条坑）
> - `data/logs/backend_exit.jsonl` —— 退出时追加：exit code（十进制 + `0x…` + **释义**）、存活时长、
>   峰值内存、**「优雅关闭 vs 硬杀 vs 原生崩溃」判定**、`crash_tail`（**死前最后 10 行输出**，现场就在这）、
>   有无 Traceback
> - `data/runtime/server_birth.json` —— 出生登记：启动时刻、子进程 PID、**父进程链**、命令行
>
> 窗口里启动时会先打印**最近 3 次退出记录**，退出时打印摘要 + 死前输出 + stderr 里的 Traceback。
> 下次再"莫名其妙死了"：先看 `backend_exit.jsonl` 最后一行（exit code 能直接区分
> `0xC0000005 访问冲突（原生崩溃）` / `0xC000012D 超提交上限` / `0xC0000017 分配失败` / `1 通用错误`），
> 再跑 `python tools/runtime/backend_postmortem.py`。
> 自检（不碰 8766）：`python tools/runtime/run_backend.py --self-test` —— **35 项**，含
> 「崩掉必判非优雅」「优雅关闭必判优雅」「硬杀形态 exit=1 无 traceback」「优雅接管存量实例」
> 「taskkill 降级真能收掉进程」「**原生崩溃不被收尾标记骗过**」「**采样抓到真身而非转发壳**」
> 「**访问冲突时 faulthandler 记下栈并指到调用帧**」「**致命信号下进程真死且仍留栈**」九条腿。
> 后两条是用真崩溃验的（不是断言）：`ctypes.string_at(0)` 触发纯 AV（形态与 18:10 那次一致），
> `faulthandler._sigsegv()` 触发致命信号（进程真死）。
>
> ⛔ **两个实测踩过的坑（2026-10-30 18:10 那次暴露，都已修）**
> 1. **退出码必须优先于日志标记**。那次后端原生崩溃 `0xC0000005`，但崩溃途中打到了
>    「记忆系统已关闭」这行收尾日志，旧逻辑据此判成**优雅关闭**，把最关键的结论写反了。
>    现在 `_judge_graceful()` 先看 exit code：非 0 一律判异常，不再被日志骗。
> 2. **`Popen` 句柄是转发壳，不是后端**。`.venv\Scripts\python.exe` 是 uv 的 launcher（实测
>    1MB private / 57 句柄 / 1 线程），真正跑 `server.main` 的是它派生的孙子进程
>    （`<user_home>\AppData\Roaming\uv\python\cpython-3.12.12-...\python.exe`）。
>    旧实现直接采 `proc.pid`，68 个样本全是 0.8MB，等于没采。现在沿进程树按
>    「命令行含 `server.main` → private 最大」两档选真身。
> 3. **原生崩溃时 stderr 里通常没有本次的 traceback**。`has_traceback=True` 可能只是运行期
>    的历史异常（那次是 17:49 的 MCP `Task group is not initialized`）。所以摘要里同时给
>    `crash_tail`——**死前最后输出了什么**才是真现场。
>
> 🔬 **`PYTHONFAULTHANDLER=1` 已默认注入子进程环境**（`run_backend.py:680`）。原生崩溃
> （`0xC0000005` 这类 SEH 异常）Python 层来不及打 traceback，窗口里什么都留不下；
> faulthandler 在 Windows 上装 VEH，能在访问冲突时打印**所有线程的 Python 栈**，
> 直接落到 `backend_console_*.log`。**这条链路已用真崩溃验过**，输出长这样：
>
> ```
> Windows fatal exception: access violation          ← 原生 AV
> Current thread 0x00075344 (most recent call first):
>   File "...\ctypes\__init__.py", line 525 in string_at
>   File "<string>", line 6 in dive                  ← 崩在哪一帧，看这里
> ```
>
> 另一种机制会给 `Fatal Python error: Segmentation fault`（CRT abort 路径，退出码 3）。
> **搜这两个关键词任意一个**：`Windows fatal exception` / `Fatal Python error`。
>
> 📖 **下次崩了按这个顺序读**（选项 C 的全部动作，不用改代码）：
> 1. `data/logs/backend_exit.jsonl` 最后一行 → `exit_code`。`3221225477` = `0xC0000005` 原生崩溃
>    ⇒ 往下走第 2 步；其他码先查 `exit_hint`。
> 2. 同一行看 `crash_tail`（死前最后 10 行）→ 崩之前最后一件事是什么。
> 3. `grep -a -A 12 "Windows fatal exception" data/logs/backend_console_YYYYMMDD.log`
>    → **最顶那一帧就是调用点**（例如 `paddle ... predict` / `torch ...`），这就是凶手。
> 4. 若 3 什么也没搜到（faulthandler 没赶上，比如崩在非 Python 线程或栈已破坏），
>    退回去看 `backend_mem_*.jsonl` 崩前内存曲线 + WER 的 `故障模块`（`mkldnn.dll` /`torch_cpu.dll` 等）。
>
> 🔁 **重复启动先优雅关闭（2026-10-30，替代 start.bat 里的直杀）**
> 外壳在起自己之前，先读 `data/runtime/server_port.json` 的 `port` / `pid`，对存量实例发
> `POST /shutdown`（走后端完整清理链：Screen/System/VL/记忆/Loop/浏览器 session…），等它自己退出；
> 逐级降级判定与记账全在窗口和 `backend_exit.jsonl` 的 `takeover` 字段里：
> `[1/3] POST /shutdown 成功 → 等待退出（默认 30s）` → `[2/3] taskkill /PID`（软，WM_CLOSE）
> → `[3/3] taskkill /F`。pid 文件指向的进程已不在时会顺手清掉陈旧 `server.pid`，不再误杀。
> `start.bat` 里原有的 `/F` 硬杀块**只在外壳文件缺失时**才执行（保底路径不变）。
> 关掉接管：`--no-takeover`；改等待时长：`--graceful-timeout 60`。
> 注意：`/shutdown` 有 30s 看门狗，若清理链卡住它自己会强退，外壳这时只会看到"等了 30s 没退" ⇒ 再降级。

```bash
# 直接双击或命令行运行
start.bat
```

**管理员权限说明**：键鼠操控（`SetCursorPos`）需要管理员权限，否则 Windows UIPI 会阻止。`start.bat` 会自动检测并申请提权——但这是**用户手动启动**的入口。

**⚠️ agent 自启动不要用 `start.bat`**：它会在无管理员权限时弹 UAC，agent 不应替用户点提权。agent 自主启动后端一律用非提权命令：

```bash
env -u PYTHONPATH PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m server.main
```

（非沙箱 shell 可去掉 `env -u PYTHONPATH`。）非提权后端的 MCP / REST / 浏览器 / 截图 / OCR 正常，但 `screen.admin=false`，键鼠与 UIA 激活窗口会被 UIPI 拦截；需要这些能力时请用户手动运行 `start.bat`，agent 不替用户触发 UAC。

#### 3. 验证启动成功

```bash
curl http://127.0.0.1:8766/health
```

返回 `"status": "ok"` 即启动成功。

#### 4. 从 WorkBuddy 沙箱 shell 内启动后端的陷阱（2026-09-03 实测）

**症状**：从 WorkBuddy（CodeBuddy）的沙箱 bash 里启动后端，进程会在**运行一段时间后或启动瞬间被 `SystemExit(1)` 击杀**——日志末尾是 `sitecustomize.py` 的批量删除守卫报错，不是后端自己的异常。2026-09-03 实测：v2 跑了 ~10 分钟后暴毙，v3 启动即失败。

**根因**：WorkBuddy 沙箱经 `PYTHONPATH` 注入了一个 `sitecustomize.py` shim（内含批量删除文件守卫）。后端自己的 `_terminal_ttl_cleanup_loop`（终端 TTL 过期清理）会对临时文件执行 `path.unlink()`，该调用被 shim 的删除守卫误判为"批量删除"，直接 `SystemExit(1)` 把后端进程杀掉。

**解法**：启动命令必须**剥掉 PYTHONPATH**，且在**非沙箱 shell**（沙箱外执行）里运行：

```bash
env -u PYTHONPATH PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m server.main
```

- `env -u PYTHONPATH` 是关键——去掉沙箱注入的 sitecustomize 钩子
- 不要用沙箱化的后台任务方式拉后端；`start.bat` 双击/命令行直启不受此问题影响
- agent 自启动走上面的非提权命令即可，同样不需要也不应该触发 UAC
- 排查辅助：`ps -W | grep python` 看进程存活时长；后端日志写 `temp/backend_restart_*.log` 可定位死亡时刻

### 关停卡住的定位（2026-10-16 整改）

后端有**两条关停路径**，行为不同，别混：

| 路径 | 触发 | uvicorn 侧 | 收尾方式 |
|------|------|-----------|----------|
| 优雅关停 | 控制台 `Ctrl+C`（SIGINT） | 关监听 → 等在飞连接/任务结束 → 跑 lifespan 清理 | 进程正常退出，之后 `start.bat` 的 `pause` 会停住窗口 |
| 硬关停（文档推荐） | `POST /shutdown` | **完全绕过** | 0.5s 后手动清理子进程 → `os._exit(0)`，无完成回执 |

**曾经的卡点**：uvicorn 的 `timeout_graceful_shutdown` 默认 `None`，即"等待连接的关闭"这一步**无限等**；而本机未装 httptools ⇒ 走 h11，h11 的 `Connection.shutdown()` 对**有在飞请求**的连接只置 `keep_alive=False`、不关连接 ⇒ 只要有 1 个请求不返回（LLM 池排队 / 未断开的 SSE / IDE 的 MCP 长连接），进程就永久停在 `Waiting for connections to close. (CTRL+C to force quit)`。特征：**请求还能继续被服务**（访问日志照刷），但 `data/logs/server.log` 里一条清理日志都没有——uvicorn 自己的关停日志只进控制台（`uvicorn.error` 挂在 `uvicorn` 父 logger 上，`propagate=False`）。

**现在有三层保护**：

1. `[server] graceful_shutdown_timeout_secs`（默认 15s）：uvicorn 到点打 `Cancel N running task(s), timeout graceful shutdown exceeded`、取消挂起任务，再照常执行清理；`[server] shutdown_cleanup_deadline_secs`（默认 30s）：清理链总兜底，超时强制退出。两条路径都挂了兜底。
2. 清理链**逐步打点**：文件日志里最后一条 `关闭步骤: xxx` 就是被阻塞的那一步（写入 `data/logs/server.log`）。
3. 想立刻通关：**再按一次 `Ctrl+C`** —— uvicorn 收到第二次 SIGINT 会置 `force_exit`，直接跳过等待阶段。

**卡住时的三分支判据**（在另一个终端跑 `netstat -ano | findstr :8766` 与 `curl.exe -s -m 3 http://127.0.0.1:8766/health`）：

- 无 `LISTENING` 但有 `ESTABLISHED`、health 连不上 ⇒ 卡在 uvicorn 等待阶段；那几条 ESTABLISHED 就是"谁在拽着连接"
- 仍有 `LISTENING` 且 health 正常 ⇒ 关停信号还没生效
- 两者都无、health 拒绝 ⇒ 已进清理链，看日志最后一条 `关闭步骤`

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 端口 8766 被占用 | 旧进程未完全退出 | 等 2-3 秒，或手动 `netstat -ano \| findstr :8766` 找 PID 杀掉 |
| 端口 8766 **绑不上**（报 `WinError 10013`，且 `netstat` 里**查不到任何占用**） | ⛔ **不是"被占用"**，是 WinNAT/Hyper-V 把它划进了**端口保留区**（本机真凶 `Tcpip\Parameters\MaxUserPort=15000`） | ⛔ **别找 PID 杀——没有进程可杀**。用 `netsh int ipv4 show excludedportrange protocol=tcp` 确认，然后走 `tools/disk_io_audit/fix_port_and_start.py`（`net stop winnat` → 起后端 → `net start winnat`）。机制、判据与三档对策见 `docs/environment-constraints.md`「端口保留区」章；⚠️ 只做临时解堵，**重启后仍会复发** |
| `start.bat` 闪退 | UAC 被拒绝 | 右键 → 以管理员身份运行 |
| MCP 工具不可用 | 后端未启动 | agent 用非提权命令自启动；需要键鼠权限时请用户运行 `start.bat` |
| 沙箱里起的后端莫名暴毙（`SystemExit(1)`） | PYTHONPATH 里沙箱注入的 sitecustomize 删除守卫击杀 | 见上文「第 4 节」：`env -u PYTHONPATH` 非沙箱启动 |

## butler runtime（对话循环宿主）

`butler runtime` 是独立进程：持有对话账本（SQLite）与所有会话循环。桌面面板、headless 作业、probe 都是它的客户端；8766 后端只提供 LLM/工具/审批能力，**不再持有对话循环**。架构见 [chat-engine.md](file:///<project_root>/docs/chat-engine.md)。

### 启动

```powershell
# 双击或命令行运行（手工启动；开机自启未做）
.\start_butler_runtime.bat

# 拿实际端口（首选 18766，被占或落进 WinNAT 保留区会顺延）
Get-Content data\runtime\butler_runtime.json
curl.exe -s http://127.0.0.1:18766/health        # 端口以声明里的 port 为准
curl.exe -s http://127.0.0.1:18766/runtime/port
```

- 启动脚本用 `--supervise`：子进程非零退出按 1s→2s→5s→30s 退避重启。
- 已有健康 runtime 时再启动一次：第二个进程打印 `already running` 并**退出 0**，不改声明。
- 账本 `format_version` 比代码新 → 退出码 2（致命），supervisor 不重启；先升级代码，**不要**手工改库里的版本号。
- 日志：`data/runtime/butler_runtime.log`（滚动）。

### 停止

- **推荐**：关掉启动 runtime 的那个窗口（Ctrl+C）。supervisor 会 terminate 子进程后退出 0。
- 需要强制停：先找 supervisor（不是子进程）再整树结束，否则只杀子进程会被 1 秒后拉起：
  ```powershell
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*lib.agent_runtime.server --supervise*' }
  taskkill /PID <supervisor_pid> /T /F
  ```
- 声明文件里的 `pid` 是**子进程** pid；`taskkill` 掉它只会触发 supervisor 自愈。
- **不要用 `start.bat` 停 runtime**：它管理的是 8766 后端，停了后端只会让 LLM/工具调用失败，runtime 进程还在。

### 迁移与备份

- 首次用新版本启动会按迁移链升级账本（v1→v2：事件域 + 三投影）；失败整批回滚，进程退出。
- 备份**必须用 SQLite backup API**，WAL 库按文件 copy 会缺行：
  ```python
  import sqlite3
  src = sqlite3.connect(r"data/client/agent.db")
  dst = sqlite3.connect(r"data/client/agent.db.bak")
  src.backup(dst)
  dst.close(); src.close()
  ```

### 重建投影

读路径报 `projection_missing`（或手工改过库）后，从账本重建：

```powershell
curl.exe -s -X POST http://127.0.0.1:<runtime_port>/admin/projections/rebuild -H "Content-Type: application/json" -d "{}"
# 单会话：{"session_id":"sess-xxx"}
```

### 启动恢复

runtime 启动时（对外 ready 前）自动跑 reconcile：残留 `streaming/awaiting_tools` 会话转 `interrupted`、补缺失 tool_result、合并未完成 streaming 事件。结果可查：

```powershell
curl.exe -s http://127.0.0.1:<runtime_port>/admin/recovery
```

桌面面板的"检测到 N 个异常退出的会话"banner 就来自这里；GUI 不再自己 reconcile。

## API 常见陷阱

> 从 AGENTS.md 拆出的详细 API 调用陷阱和避免方法。AGENTS.md 仅保留核心铁律指针。

### PowerShell 中 `curl` 是 `Invoke-WebRequest` 的别名（重要！）

在 PowerShell 终端运行 `curl -s http://127.0.0.1:8766/health` 会触发 `Invoke-WebRequest`，它把 `-s` 解析为 `-Session`，然后等待用户输入 `Uri:` 参数，导致终端卡住，必须 Ctrl+C 才能中断。

**避免方法**：
- 用 `curl.exe` 强制调用真正的 curl：`curl.exe -s http://127.0.0.1:8766/health`
- 或用 `Invoke-RestMethod`：`Invoke-RestMethod -Uri http://127.0.0.1:8766/health`
- **绝对不要**在 PowerShell 中直接用 `curl -s ...`，会卡住终端

### Git Bash 里 `curl.exe --data-urlencode` 传中文会把查询变成 GBK 字节（假故障，2026-09-28 实测）

**症状**：`curl.exe -G ... --data-urlencode "task=开虚拟机装系统"` 得到的响应像"功能没生效"，
而同一时刻用 ASCII 参数（`task=VMware`）却正常命中。极易误判成"后端注册了条目但匹配逻辑坏了"。

**根因**：Git Bash 的 locale 不是 UTF-8，`--data-urlencode` 按当前 locale 把中文编成 **GBK 字节**再百分号编码，
服务端按 UTF-8 解出来的是一串乱码，自然匹配不到任何中文关键词。ASCII 字符两种编码一致，所以看起来"一半能用"。

**同一条请求的对照实测**（同一个后端、同一时刻、同一句话）：

```
curl.exe -s "http://127.0.0.1:8766/guide?task=%E5%BC%80%E8%99%9A%E6%8B%9F%E6%9C%BA%E8%A3%85%E7%B3%BB%E7%BB%9F"
→ {"mode":"task","task_type":"adhoc.vm_ops", ...}                  # UTF-8 预编码：正常命中
curl.exe -s -G "http://127.0.0.1:8766/guide" --data-urlencode "task=开虚拟机装系统"
→ {"mode":"low_confidence", ...}                                    # 经 shell 编码：判为不匹配
```

**避免方法**：
- 判协议层行为优先走 **MCP 工具**（`mcp_parcae_agent_guide` 等），JSON 传输不经 shell 编码
- 或 `python -c` / `urllib` 自己拼 UTF-8 查询
- 必须用 curl 时：**自己按 UTF-8 预先把中文百分号编码好**写进 URL（如上面那串 `%E5%BC%80...`），
  不要用 `--data-urlencode` 让 shell 编码
- 与 POST 侧同族：中文 JSON body 走 `--data-binary @文件`（文件用 Write 工具落盘，UTF-8 无 BOM），
  不要在命令行里内联中文
- **推论**：任何"中文参数不生效而 ASCII 生效"的现象，先怀疑探针编码，再怀疑服务端

### PowerShell 不支持 `&&` 串联命令语句（反复踩坑！）

Windows PowerShell 5.1 不支持 `&&`（管道链运算符），用 `cmd1 && cmd2` 会报 `The token '&&' is not a valid statement separator`。

**避免方法**：
- 用 `;` 顺序执行（不管前一个是否成功）：`cmd1 ; cmd2`
- 用 `$LASTEXITCODE` 显式判断：`cmd1 ; if ($LASTEXITCODE -eq 0) { cmd2 }`
- 或改用 PowerShell 7（`pwsh.exe`，支持 `&&` 和 `||`）
- **Shell 工具链式命令时不要用 `&&`**，用 `;` 或拆成多次调用

### PowerShell 5.1 写 UTF-8 文件必带 BOM（跨平台坑，agent 写文件必读！）

Windows PowerShell 5.1 的 `Set-Content -Encoding UTF8` / `Out-File -Encoding utf8` 写出的文件**必带 BOM**（U+FEFF，字节 `EF BB BF`），且多次写会**累积**。2026-08-26 实测：yihuan_clean.py 曾堆 3 个 BOM、SKILL.md 2 个、`__init__.py` 3 个，Python 脚本直接 `SyntaxError: invalid non-printable character U+FEFF`。

agent 为写中文不乱码常恰好选中 `-Encoding UTF8` 这种写法，是本项目 BOM 的**唯一来源**——IDE 的 Write/Edit 工具不加 BOM（Trae 已对照实验验证；早期归因"harness 平台写入叠加"已被推翻，详见 `tools/clean_bom.py` 文件头注释）。

**避免方法**：
- agent 写/改文件**优先用 IDE 的 Write/Edit 工具**，不走 shell 重定向（跨平台通用：凡在 shell 里用重定向/`Set-Content` 写 UTF-8 都可能踩 BOM）
- 必须 PowerShell 写 UTF-8 时用 .NET API：`[IO.File]::WriteAllText($path, $content)`（UTF-8 无 BOM）
- PS 7（`pwsh`）的 `-Encoding utf8` 不带 BOM，但项目终端默认 PS 5.1，不要赌版本
- 遇 `SyntaxError: invalid non-printable character U+FEFF` → `uv run python tools/clean_bom.py --fix`（默认 dry-run 只扫描报告，`--fix` 清头部 BOM，`--fix-all` 连中部零宽字符一起清，慎用）

### Git 命令默认走 pager 会卡住终端（agent 自动执行必读！）

`git log` / `git diff` / `git show` 等命令在 Windows 上默认使用 `less` 作为 pager，输出超过一屏时进入分页模式，等待用户按键（空格/q/回车）才显示后续或退出。在 agent 自动执行场景下命令会一直挂起，直到被用户手动跳过或 Ctrl+C，外观像"命令在运行但没输出"。

**避免方法**：
- 用 `git --no-pager log ...` / `git --no-pager diff ...` 强制单次关闭 pager（推荐，最稳）
- 或显式重定向 pager：`git -c core.pager=cat log ...`（输出直送 cat，不暂停）
- 或限制输出长度：`git log -n <N>`、`git diff --stat`、`git diff <ref>~1 <ref> -- <path>`
- 全局禁用 pager：`git config --global core.pager cat`（影响所有仓库，需用户授权，不要擅自改全局配置）
- **绝对不要**在 agent 自动执行场景下直接用 `git log` / `git diff` 不加 `--no-pager` 或长度限制，会卡住终端

### 不要根据文档注释判断文件是否存在（反复踩坑！）

AGENTS.md / _index.md / skill 文件中的注释可能过时（如"XX 尚未创建"、"XX 在 wip 待办中"），直接采信会导致错误结论。

**避免方法**：
- 文件是否存在 → 用 `Read` / `Glob` / `Test-Path` 实际验证，不要信文档注释
- 文件内容 → 用 `Read` 实际读取，不要根据文档描述推断
- 接口是否存在 → 用 `curl.exe` / `Invoke-RestMethod` 实际调用验证
- **典型案例**：AGENTS.md 曾写"CHANGELOG.md 尚未创建"，但实际文件已存在（40KB），agent 没读就下结论导致 CHANGELOG 漏更新

### 禁止大量返回 Base64 污染上下文（强制规则）

所有代码、脚本、MCP 工具调用都不得返回大段 base64 数据（截图、文件、音频等）到 LLM 上下文。遇到 base64 立即改代码或调用方式：
- 截图给多模态 LLM：用 `capture_screen(format="inline")`（返回 ImageContent，由 MCP 协议处理，不进文本上下文）
- 截图特征分析：用 `screen_analyze`（返回纯文本的颜色/亮度/异常分析，无 base64）
- 截图+OCR：优先 `parcae_advanced_tool(tool="screen_ocr", params={...})`，直接返回文字和 bbox，不经过 base64
- 文件读取：用 `docviewer_read` 或 `exec_python` 提取文本，绝不返回 base64
- **违反此规则会导致上下文窗口被数 MB 的 base64 字符串撑爆，LLM 无法继续工作**

### REST 读写 `/wip` 的三个形状坑（做"读—改—写"合并更新前必读）

`workspace/homework` 那条线常用脚本直接打 REST 更新 WIP（MCP 不可用时的通路），四个坑实测确认：

- **`GET /wip/{id}` 返回的是扁平对象**，顶层就是 `title/status/current_state/...`，**没有 `task` 包装**。写 `json.load(r).get("task", {})` 会静默拿到 `{}`，于是"读旧值→合并→写回"变成"用空底座写回"，**旧 `current_state` 键、`next_steps`、`related_files` 全被覆盖丢失**（2026-09-28 实测踩过，一次覆盖两条 WIP，靠事先 dump 的原始快照才并回去）。列表接口 `GET /wip` 才有 `tasks` 数组，别把两者的形状混用。
- **`PUT/POST /wip` 的 `current_state` 必须是 dict**，传字符串直接 422。
- **`POST /wip`（新建）必须带 `goal`**，且**不接受 `progress` / `status`**（`extra_forbidden`）；这两个字段只在 `WipTaskUpdate`（PUT）里有。
- **`GET /wip?status=...` 只认 `active|paused|blocked|completed`**，传 `open` 之类非法值不报错、返回 `total=0` 空表（看着像"没有待办"）。
- **合并式写回的纪律**：先 `GET` 落到临时 JSON 文件留底，脚本里断言 `读到的旧键数 > 0` 再写；写完立刻回读一次核对键数（`steps=N(期望N)`），别信"PUT 返回 200"等于写对了。

### 截图+OCR 的正确流程（重要！）

优先调用高级工具 `screen_ocr`，一步截图并返回 `text + details[].box`，不返回图片数据：

```
parcae_advanced_tool(
    tool="screen_ocr",
    params={"mode":"window", "hwnd":123, "engine":"ocr"}
)
```

需要已有图片文件时用 `ocr_file` / `ocr_path`。需要把截图交给其他工具时可用 `capture_screen(format="path")` 获取临时路径，再传给 `ocr_path`。**绝对不要**直接调用 `capture_screen(format="base64")` 把数 MB base64 返回上下文。

### 截图特征分析（远程 VL 不可用时的图像辅助）

当远程 VL（ModelScope）不可用（限流冷却中或网络异常）、OCR 只能识别文字时，用 `screen_analyze` 获取图像特征（主色/亮度/异常检测/网格分区），全部返回文本，无 base64：

```
screen_analyze(params={"mode":"fullscreen"})  # 全屏分析
screen_analyze(params={"mode":"window","window_title":"异环","grid_size":4})
```

返回：主色列表（hex+名称+占比）、亮度统计、颜色多样性、边缘密度、4x4 网格分区主色、异常告警（蓝屏/黑屏/白屏/单色统治/警告色等）、一句话总结。**用于快速判断画面是否异常**（如游戏卡加载、应用崩溃、蓝屏等），比 `capture_screen` 更有信息量，又不会像 base64 那样污染上下文。

### `ocr_file` / `ocr_path` 的适用场景

识别已有图片路径；实时屏幕定位用 `screen_ocr`。`screen_snapshot(with_ocr=true)` 只返回文字摘要，不返回 bbox。

### 远程 VL 状态有三个出口，字段不会自动同步（改 VL 可观测性前必读）

同一份远程 VL 状态从三个口出来，行为各不相同（2026-09-19 排查 400 事故时实测确认）：

- `GET /health` 的 `vision` 段与 `GET /vision/providers` —— 原样透传 dict，加字段立即生效
- `GET /vision/status` —— 走 `VisionStatusResponse`（`lib/schema.py` 的 `extra='forbid'`），且端点**按 schema 字段名白名单过滤**响应：只在 `remote_vl.vl_status_fields()` 里新增 key 会被**静默丢弃**，必须同时给响应模型声明字段
- **客户端面板不请求 `/vision/status`**：模型池面板读 `/health.vision` + `/vision/providers`，监控面板只渲染 `/health` 里的几个 key 级字段。所以"给 `/vision/status` 加字段"对 GUI 毫无效果

**连带判卷**：`tests/llm_vision/test_vision.py` 断言 `/vision/status` 字段集合；
`tests/approval_screen/test_monitoring_health_contract.py` 规定状态卡任何字段显示 `—` 即失败——
**给状态卡加新字段必须同步它的 fixture**，否则要么红要么静默显示占位符。
`tests/client_ui/` 目前对 VL 渲染零覆盖，改面板不会有任何测试拦你，需要自己补冒烟。

### 坐标参数必须为整数

`/screen/action` 的 `x`/`y` 类型为 `int`，传浮点数会返回 422 验证错误。脚本中应使用 `int(x)` 转换。

### 422 验证错误

Pydantic 验证失败时返回 422，响应体包含 `detail` 数组，每项有 `loc`（出错字段路径）和 `msg`（错误描述），可用于快速定位问题。

## 终端会话 API（长时间运行命令）

对于模型下载、pip 安装、批量处理等长时间运行命令，直接使用终端会话 API。`/exec/python` 自 v15 起**也走同一套 terminal 机制**（写 `temp/exec_<uuid>.py` → spawn），它默认只内联等 `[server] exec_python_inline_wait_secs`（默认 10）秒：子进程在该窗口内结束就返回 `status="done"` + 完整 stdout/exit_code，否则返回 `status="running"` + `terminal_id`，由调用方自己 `exec_inspect`——**不存在"到点被杀"的超时**（spawn 侧 `timeout=0`）。

> **注意**：本项目的终端 API 是**后端常驻进程的执行面**，不是"agent 平台终端不好用时才考虑的替代"——启动调试浏览器（CDP 9222）这类必须长过 harness 会话的进程，**只能**走 `exec_terminal_spawn`（见 `docs/environment-constraints.md`「调试浏览器实例」回收机制小节），harness 侧 Bash / 后台任务起的进程会随会话清理被回收。运行中终端数量有硬上限（常量 `_MAX_RUNNING_TERMINALS`，真源在 `server/exec.py`），不是无界创建。

| MCP 工具 | 用途 |
|---------|------|
| `exec_terminal_spawn` | 启动后台终端（返回 tid，不阻塞） |
| `exec_terminals_list` | 列出所有终端（状态/PID/输出字符数） |
| `exec_terminal_detail` | 查看终端详情（stdout/stderr，支持 tail 截取） |
| `exec_terminal_output` | 按字节游标增量读取完整 stdout/stderr（单次最多 256 KiB） |
| `exec_terminal_input` | 向运行中终端发送输入（交互式命令/Ctrl+C） |
| `exec_terminal_kill` | 终止运行中终端 |
| `exec_terminal_delete` | 删除已结束终端记录 |

**典型流程**：
1. `exec_terminal_spawn(cmd="python download.py", label="download_model")` → 获取 tid
2. 轮询 `exec_terminal_detail(tid, tail=3000)` 查看近期进度（`tail=0` 默认仅返回 8000 字符安全预览）
3. 如需交互：`exec_terminal_input(tid, text="y")` 回应提示
4. 需要完整日志时，用 `exec_terminal_output(tid, channel="stdout", offset=0, limit=65536)` 按 `next_offset` 分页读取
5. 完成后 `exec_terminal_delete(tid)` 清理会话和磁盘日志

终端输出以 `temp/terminals/` 下的日志文件为真源，内存只保留每通道最近 64 KiB；stdout/stderr 会并发排空，避免高输出命令因管道写满死锁。运行中终端默认上限为 10 个，防止 Agent 无界创建后台进程。

**崩溃证据不再被启动清理抹掉**（`unknown-outcome-evidence` T04，`server/exec.py`）：后端启动时的孤儿清理（`cleanup_orphan_temp_files()`）过去对上一进程遗留的 `temp/exec_*.py` / `temp/terminals/term_*.log` 直接 `unlink()`——那正好抹掉"上次崩在哪条命令上"的唯一现场。现改为**归档**到 `temp/exec_evidence/`（`EXEC_EVIDENCE_DIR`，每类保留最近 `EXEC_EVIDENCE_KEEP=3` 份，超出才裁剪），统计键相应为 `archived_exec_files` / `archived_term_logs` / `pruned` / `skipped` / `errors`（旧名 `deleted_*` 已不存在，别照抄）。这不是磁盘清理，也不涉及用户目录。

**终端监控**：通过 GUI 客户端（`start_client.bat`）的 Monitoring 面板查看终端管理标签页，实时查看所有终端输出

## 日总结功能（用户问"总结今日"时触发）

**"今天"的定义（关键，agent 易误解）**：用户说"总结今日" / "今天的日报" 时，"今天" = **从上一个 05:00 到当前时刻**，不是"等到下一个 05:00"。用户要日报时通常是要睡觉或结束工作，**不会期待更多记录**——立即基于已有 hourly 文件汇总生成，不要因"今天还没到 05:00 结束"而推迟或询问。文件名由当前时间决定：当前 ≥ 05:00 用当天日期，当前 < 05:00 用前一天日期（如 07-14 04:00 请求 → `20260713.md`）。详见 `.agents/skills/daily_summary.md` 的"日期划分规则"。

**数据源优先级（必读）**：用户说"总结今日工作" / "今日做了什么" / "总结一下" 时，按以下顺序收集信息：

1. **首选：[data/activity/hourly/](file:///<project_root>/data/activity/hourly) 目录下的 `YYYYMMDD_HH.md` 文件** — 这是本功能的本源数据，由 `hourly_summarize` loop 任务每整点自动生成。**先读这些文件，不要先查 git log**。
2. **补全缺失时段**：用 `git log --since="YYYY-MM-DD 05:00" --until="now"` 补全 hourly summary 缺失或失败的时段（注意 since 用 05:00 不是 00:00，与日期划分规则一致）。
3. **memory 仅作参考**：`<user_home>\.trae-cn\memory\projects\-f-project-temp-Parcae\YYYYMMDD\topics.md` 提供会话级上下文，但**不是主要数据源**。

**易踩的坑**：
- hourly summary 文件可能整小时缺失（LLM 失败、后端重启）— 此时必须用 git log 补全
- 若 hourly summary 标注 "VL 屏幕描述数据整小时缺失" → `screen_vl` loop 任务可能被暂停，查 `loop_list_tasks`（走 `parcae_advanced_tool` 网关调 `loop_status`）确认
- 若 hourly summary 显示"LLM 返回空内容"占位文件 → `hourly_summarize` 任务最近失败，可手动 `loop_run_task` 重试
- **不要在日报中加入 agent 的 task_closure 工作报告** — 日报是用户的活动记录，不是 agent 的收尾报告，两者是不同的东西

**判断偏差反馈**：agent 容易下意识把 git log + memory 当成主要数据源，而忽略本功能的内部数据源 hourly summary。这是错误的——必须先查 hourly summary，再外求。

## 挂机监控 + 条件关机（用户说"我要睡觉了，XX 条件就关机"时触发）

**触发词**：「我要睡觉了」「挂机」「跑完就关机」「XX 完成就关机」「条件达成后关机」「定时关机」「23:30 关机」等表达睡前/离开后让 agent 监控并关机的意图。

> **2026-08-03 重构**：原 `workspace/auto_shutdown/monitor.py` 独立 CLI 子进程已删除。关机流程收进后端 REST 端点（`server/auto_shutdown.py`）。agent 现在在 Trae 会话里自己跑检测 loop，不依赖独立子进程。原因：80% 真实场景复杂（识图/查日志/多条件组合），简单条件检测覆盖不了，必须 agent 在场判断；agent 在场 = 后端必须活着 + Trae 必须开着，"独立子进程持续运行"价值大打折扣。

### 工作流（agent 自己跑 loop + 调端点）

1. **确认触发条件 + task_id**：复述用户给的条件，确认理解无误；定一个 task_id（用于截屏目录隔离）
2. **agent 自己写检测 loop**：用 `time.sleep` 或 `asyncio.sleep` 控制检测间隔（60s 常用），检测函数 agent 自行编写（识图 / 查日志 / 查 API / 查文件 / 查时间）
3. **条件满足 → 关窗口程序 → 调 `POST /auto_shutdown/trigger`**：
   - **关窗口程序（强制）**：模拟器/全屏应用/有未保存数据的程序会阻碍关机。agent 调 `list_windows` 查窗口，用 `screen_window_close` 或 `execute_action`（Alt+F4）逐个关闭。不要用 `taskkill` 强杀（可能丢失用户数据）
   - **调端点**：端点内部自动截屏 + 推 inbox + 调 `shutdown /s /t 120`（120s 倒计时，多 60s 宽限用户刹停）
4. **用户反悔 → 调 `POST /auto_shutdown/cancel`**：端点调 `shutdown /a` 中止倒计时，幂等
5. **关机完成 / 用户中止**

### 端点参数

#### POST /auto_shutdown/trigger

```json
{
  "task_id": "game_afk_20260803",  // 必填，截屏目录隔离用
  "reason": "游戏完成",             // 可选，默认 ""
  "screenshot": true,               // 可选，默认 true（关机瞬间截屏存档）
  "dry_run": false                  // 可选，默认 false（true 跳过 shutdown 调用，测试用）
}
```

返回：`{success, task_id, trigger_count, shutdown_scheduled, dry_run, screenshot_path, cancel_command}`

#### POST /auto_shutdown/cancel

```json
{"dry_run": false}  // 可选，默认 false
```

返回：`{success, dry_run, cancel_command}`

### MCP 工具调用

```
# 触发关机
parcae_advanced_tool(
    tool="auto_shutdown_trigger",
    params={"task_id": "game_afk", "reason": "游戏完成", "screenshot": true, "dry_run": false}
)

# 取消关机
parcae_advanced_tool(
    tool="auto_shutdown_cancel",
    params={"dry_run": false}
)
```

### 关键约束（强制）

1. **调 trigger 前先关闭有窗口的程序**：模拟器（MuMu/雷电/Nox）、游戏客户端、IDE、有未保存文档的程序在系统直接关机时会阻碍关机流程（弹窗阻止 / 资源占用 / 文件锁）。agent 调 trigger 前应先关这些窗口，优先 graceful close，不要 taskkill 强杀
2. **120s 倒计时**：`shutdown /s /t 120` 触发后 Windows 弹窗 120s 倒计时，用户可在 120s 内喊"取消关机"或自行 `shutdown /a`
3. **关机命令绕过 command_guard**：用户睡前无法审批弹窗，端点内部直接调 `subprocess.Popen(["shutdown", "/s", "/t", "120"])`，绕过 command_guard（设计取舍，已明确同意关机）
4. **端点幂等**：重复调 trigger 递增 trigger_count，重复调 cancel 不报错
5. **副作用容错**：截屏失败（mss 异常）→ screenshot_path=None 仍触发关机；inbox 推送失败（后端不可用）→ 记日志仍触发关机。用户睡前 inbox 不重要，关机本身重要

### 状态记录与通知

- **状态目录**：`temp/auto_shutdown/<task_id>/final_screenshot.png`（关机瞬间截屏，失败时无此文件）
- **inbox 推送**：关机已触发（含 task_id/reason/截屏路径/取消提示）+ 关机已取消
- **trigger_count**：内存中不持久化，后端重启归零，inbox 有历史记录可查
- **/health**：响应含 `auto_shutdown` 字段（trigger_count / last_trigger_at / last_trigger_task_id）

### 示例检测脚本（agent 自行编写，仅供参考）

完整示例见 [.agents/skills/auto_shutdown.md](file:///<project_root>/.agents/skills/auto_shutdown.md)，含 3 种场景：
- 游戏 AFK 识图检测（capture_screen + screen_ocr）
- 定时关机（处理跨日边界：23:35 启动 target="23:30" → 明天 23:30）
- 文件出现检测（标记文件协调跨 agent 任务）

### 受控托管模式联动（自然行为，不单独 spec）

- agent loop 跑 screen 操作前，应确认看门狗授权 active（调 `/screen/grant/status`）
- agent 每次 screen 操作成功会自动续期看门狗授权
- 用户撤销看门狗授权 → agent 下次 screen 操作被拒 → agent 应识别此信号并停止 / 询问用户
- 用户 AFK 时若遇到无法通过已授权、安全路径解决的问题 → agent 必须 fail-closed，立即停止，不得绕过确认、切换未授权通道或无限重试
- agent 触发关机时，看门狗授权自然失效（系统关机），无需显式撤销

> 相关文件：[server/auto_shutdown.py](file:///<project_root>/server/auto_shutdown.py) + [tests/approval_screen/test_auto_shutdown.py](file:///<project_root>/tests/approval_screen/test_auto_shutdown.py) + [.agents/skills/auto_shutdown.md](file:///<project_root>/.agents/skills/auto_shutdown.md)

## command_guard 双开关语义（审批排查必读）

`config.toml [command_guard]` 段有**两个独立开关**，语义不同，排查"为什么被拦/没被拦"时必须分清：

| 开关 | 控制范围 | 当前默认 |
|------|----------|----------|
| `enabled = false` | **仅 dcg 二进制预检查**（shell 命令拦截）。关掉后 dcg 不生效 | false |
| `approval_level = "none"` | **HTTP 中间件 + MCP 网关的端点审批拦截**（strict/moderate/loose/none 四级） | none |

常见误区：
- "我把 `enabled` 关了怎么还弹审批？" → 端点审批由 `approval_level` 控制，两者独立。
- "GUI 点按钮也被拦？" → GUI 直连请求不带 `X-Agent-Caller` 头，天然放行；被拦的是 agent 链路（chat 引擎直连 REST / MCP 网关调用）。
- **"审批日志里没有可疑放行" ≠ "有人看过"** → 见下方「放行来源分档」。闸门关闭时日志本就是空的。

### 调用方身份判据（`lib/agent_identity.py` 是唯一真源）

客户端发 `X-Agent-Caller: v6-lite-agent`，服务端判据是 `is_agent_caller(headers)`：**头存在且值不是明确否定词（`""`/`false`/`0`/`no`/`gui`/`user`）即算 agent 发起**。GUI 从不设置这个头，所以"有值即 agent"是安全方向。

> ⚠️ **2026-09-30 修正的错判**：此前 `server/core/middleware.py` 与 `server/exec.py` 把判据写成 `== "true"`，而客户端发的值是 `v6-lite-agent`——于是 **agent 流量一直被判成 GUI 用户流量**：审批中间件跳过它、terminal 的 `owner_token` 检查也免掉。改成统一走 `is_agent_caller()` 后，agent 链路会真正进入审批/归属判定路径。**如果哪天你把 `approval_level` 从 `none` 打开，agent 会开始弹出以前不弹的审批**——那是修复生效，不是新 bug。
>
> 两端都不要再写字符串字面量：客户端从 `lib.agent_identity` 引 `AGENT_CALLER_HEADER(_VALUE)`，服务端引 `is_agent_caller`（`lib/` 是 C4 允许的双向共享层）。

### 放行来源分档（谁批准的，可查）

`server/approval_review.py` 的每条 `data/approvals.jsonl` 记录都带 `origin`，区分放行是谁给的：

| origin | 含义 |
|--------|------|
| `human` | GUI 审批面板真人点的（`command_guard` 与 `user_review_for_llm_deny` 两条路径都记） |
| `llm_approved` | LLM 复核放行 |
| `low_risk_degraded` | LLM 审查不可用时按 AST 低风险判定降级放行 |
| `approval_cache` | 复用近期人审指纹（TTL 内不再问人） |
| `denied` | 拒绝 |
| `unknown` | 无法归类的历史/异常记录 |

- 聚合计数：`approval_origin_counts()`，也从 `GET /health` 的 `guard.recent_origins` 直接看。
- **闸门自身状态**：`GET /health` 的 `guard.gate_open`（= `enabled && approval_level != "none"`）与 `guard.note`。本机默认 `gate_open=false`，此时 `approval_required` 端点与命令守卫都在放行、日志无记录——排查时先看这一格，别从"没有告警"推"安全"。

### 审批清单（2026-08-25 调整）

- `/loop/tasks*` 的 POST（pause/resume/run）**已从审批清单移除**——低风险运维操作，有 fail_threshold 自动熔断兜底；auto_failed 能否重启后复活取决于 `fail_count` 是否还没攒满阈值（见 `docs/periodic-task-inventory.md`）。任何 approval_level 下均 safe。
- strict 仍拦：exec 类（python/cmd/apply-patch/terminal spawn）、shutdown/config、模型管理、activity/daily、browser/close、PUT/DELETE 默认等。真源见 `server/route_tags.py`。

### loop 手动触发语义

- 手动触发（面板按钮 / `loop_run_task`）**不推进 last_run_at**，不影响正常调度周期。
- 任务正在执行中再手动触发 → 返回 409 task_busy（不是错误，提示"稍后再试"即可）。
- MCP 网关人审批准后的 token 用 `_approval_body(arguments)` 指纹验证（签发/验证一致），批准即可重试成功。

> 相关文件：[server/route_tags.py](file:///<project_root>/server/route_tags.py) + [server/core/mcp_gateway.py](file:///<project_root>/server/core/mcp_gateway.py) + [server/http_guard.py](file:///<project_root>/server/http_guard.py) + 回归测试 tests/approval_screen/test_mcp_gateway_approval_fingerprint.py

## 模型生命周期管理（Model Lifecycle Manager）

**Model Lifecycle Manager（`server/model_manager/`）** 统一管理后端进程内 4 个本地模型的生命周期——PaddleOCR（GPU，可逐出）/ 记忆 embedding（CPU，默认钉住）/ guide embedding（CPU，默认钉住）/ MindForge 搜索引擎（CPU，可逐出）。

设计原则：**管理器是决策权威、模块是执行者**（薄编排不做所有权重构）。框架通用、策略分资源（GPU 主动逐出 + CPU 框架预留默认关闭）。环境约束与显存释放实测详见 `docs/environment-constraints.md` 的 "Model Lifecycle Manager 显存释放实测" 段；端点审批级别与 MCP 调用方式详见 `docs/mcp-reference.md` 的 "Model Lifecycle Manager 统一控制面" 段。

### 何时需要手动操作

正常情况下 ModelLifecycleManager 自动运行——按 GPU 压力态自动逐出 OCR（VRAM 高压时）+ OCR 懒恢复（下次真实请求自愈）。**只在以下场景需要手动操作**：

| 场景 | 操作 | 端点 |
|------|------|------|
| 显存异常高 + 怀疑 OCR 占用 → 手动卸载释放 | 卸载 OCR | `POST /models/ocr/unload`（approval_required） |
| 调试时需要预加载 OCR 模型 | 加载 OCR | `POST /models/ocr/load`（approval_required） |
| 排查问题时绕过压力态约束（仅手动超驰） | 暂停压力监控 | `POST /models/pause`（approval_required） |
| 调试完恢复自动监控 | 恢复压力监控 | `POST /models/resume`（approval_required） |
| 排查 MindForge 状态（1-2GB 大对象） | 卸载 MindForge 搜索引擎 | `POST /models/mindforge_searcher/unload`（approval_required） |
| 调试记忆 embedding 卸载后果（**高风险**） | 卸载 memory_embedding | `POST /models/memory_embedding/unload`（approval_required） |
| 查询当前所有模型状态 + 压力态 | 列模型 + 查压力 | `GET /models` + `GET /models/pressure`（read_only，免审批） |

### 手动卸载记忆 embedding 的恢复步骤（高风险，必读）

**默认 `evictable=False`（钉住）规避此场景**：记忆 embedding 在每个 MCP 请求路径上（`/mcp` tools/call → recorder.record_tool_call → semantic.index_message → embed），逐出后必然立即被下一请求拉回形成"逐出→拉回→60s 后再逐出"的慢循环，且**卸载窗口内写入的消息永久缺失向量索引**——逐出收益为负。

如果确实需要手动卸载调试（已通过审批），**必须按以下步骤恢复**：

1. `POST /models/memory_embedding/load` — 重新加载 embedding engine
2. `POST /memory/rebuild_vector_index` — 补卸载窗口内缺失的向量索引（关键，否则语义检索永久降级）

**卸载后果（静默降级）**：记忆语义检索自动降级为 BM25-only（无 503，消费者都先查 `ready`，不会写入零向量）。不调 `rebuild_vector_index` 会导致卸载窗口内写入的消息永久缺失向量索引。

### MindForge 搜索引擎卸载已知局限

- 搜索端点持局部引用（mindforge.py），`unload()` 只删属性，**1-2GB 实际要等在途搜索结束才释放**
- 无 OCR 那样的 `forced` + 延迟补释放语义
- `load()` 无参协议与 `get_searcher(index_dir)` 必填参数冲突的解决：`unload()` 前自留 `last_index_dir` 快照；`load()` 优先用快照，从未加载过则复刻 `/mindforge/preload` 解析逻辑

### 状态查询

**`GET /models`**（read_only，免审批）— 列出全部注册模型状态：

```json
{
  "models": [
    {
      "model_id": "ocr",
      "resource": "gpu",
      "loaded": true,
      "footprint_mb": 600,
      "priority": 80,
      "evictable": true,
      "in_flight": 0,
      "loaded_at": 1234567890.0,
      "last_load_ms": 1850
    },
    {
      "model_id": "memory_embedding",
      "resource": "cpu",
      "loaded": true,
      "footprint_mb": 90,
      "priority": 80,
      "evictable": false,
      "in_flight": 0,
      "loaded_at": null,
      "last_load_ms": null
    }
  ]
}
```

**`GET /models/pressure`**（read_only，免审批）— 各资源压力态：

```json
{
  "gpu": {
    "state": "NORMAL",
    "used_mb": 1234,
    "total_mb": 8192,
    "ratio": 0.15,
    "probe_source": "hub",
    "probe_id": "gpu_mem",
    "probe_status": "ok",
    "probe_absent": false,
    "probe_age_sec": 2.4,
    "last_failure": null,
    "last_transition_ts": 1234567890.0
  },
  "cpu": {
    "state": "disabled",
    "used_mb": null,
    "total_mb": null
  }
}
```

**`/health.model_manager`** — 聚合到 `/health` 端点（低频枚举）：

```json
{
  "enabled": true,
  "gpu_state": "NORMAL",
  "cpu_state": "disabled",
  "loaded_ids": ["memory_embedding", "ocr"],
  "reload_degraded_ids": [],
  "last_transition_ts": 1234567890.0
}
```

`reload_degraded_ids` 非空时提示该模型连续 ≥3 次加载失败，建议重启后端。

### keep_models 写回链（OCR + MindForge）

原 OCR 与 MindForge 的 `keep_models` 死旋钮已统一迁移到 ModelLifecycleManager per-model `restore_preload` 配置（语义所有权迁移，单例所有权不变）：

- **写入口**：`POST /ocr/models/keep`（OCR）/ MindForge 同款旋钮 → 写入 `[model_manager.models.<id>].restore_preload`
- **读出口**：`/health.ocr`、`/ocr/status`、`/mindforge/status`、GUI `client/panels/llm_pool.py` → 全部回读管理器生效值
- 配置位置：`[model_manager.models.ocr]` 段（覆盖默认值）

向后兼容：调用方零改动，`keep_models=true` 等价于 `restore_preload=true`（默认值已开启）。

#### 空闲回收（`idle_unload_sec`，2026-10-23 补）

`restore_preload` 单独存在时有个缺口：它只管「被逐出后要不要自动重载」，**不管「用完了什么时候释放」**，
而 OCR 的加载发生在请求路径里（`_run_classic_ocr → models.get_ocr()` 懒加载），所以关掉常驻后
任何一次 OCR 调用都会把模型重新拉回内存并一直挂着——表现为「关了常驻还是常驻」。

补法是在管理器里加空闲回收，**门闸仍是 `keep_models`**（不新增第二个开关）：

| 配置 | 位置 | 含义 |
|------|------|------|
| `idle_unload_sec` | `[model_manager]`（全局，默认 `0`=禁用） | 模型空闲超过 N 秒且常驻关闭时自动卸载 |
| `idle_unload_sec` | `[model_manager.models.<id>]`（覆盖全局） | 本机 OCR 配 10 秒 |
| `idle_tick_sec` | `[model_manager]`（默认 `1.0`） | 空闲判定循环周期（独立于 5s 压力采样） |

- 判据是 **`last_active_at`**（`get_ocr()` 进入 + 每次推理结束刷新），不是 `loaded_at`；
  否则「加载很久但一直在用」会被误判为空闲。
- 跳过条件：未加载 / `in_flight>0` / 冷却中 / 常驻开启。回收与压力逐出共用串行锁，锁内复查。
- 可观测：`/ocr/status` 与 `/health.ocr` 的 `idle_unload_sec`、`idle_remaining_sec`（倒计时，`null`=不适用）、
  `last_active_at`。GUI「模型池 → OCR」显示倒计时，并提供「加载模型 / 卸载模型 / 常驻开关」三个按钮。
- 想恢复旧行为（永不自动卸）：把 `idle_unload_sec` 删掉或设 `0`。

### 配置参考

`config.example.toml` 的 `[model_manager]` 段（详见 `temp/sdd/model-lifecycle-manager/design.md` §9）：

```toml
[model_manager]
enabled = true                    # 总开关（false=纯透传，各模块行为回退现状）
poll_interval_sec = 5.0           # 状态机推进周期（实际采样节奏见 [resource_probe]）
reload_fail_cooldown_sec = 30      # per-model 加载失败冷却默认值（秒）

[model_manager.gpu]
# GPU 显存压力监控（主动逐出唯一资源维度，design D2）
enabled = true
high_watermark = 0.90              # 高压阈值（触发 REFUSING）
high_sustain = 3                  # 高压持续周期数
critical_watermark = 0.97         # 逃生门单样本阈值
low_watermark = 0.70              # 低压阈值（触发 ARMED）
low_sustain = 6                   # 低压持续周期数
min_loaded_seconds = 60           # 加载保护期（秒）
unload_timeout_sec = 15           # 卸载超时（秒，含排空 in_flight）

[model_manager.cpu]
# CPU 内存压力监控（默认关闭：8GB+ 主机内存下 ~2GB 搜索引擎不构成压力）
enabled = false
# 字段同 [model_manager.gpu]，启用时同款状态机生效

# [model_manager.models.<id>]：每模型覆盖
# 字段：evictable / priority / reload_fail_cooldown_sec / restore_preload
# 示例：
#   [model_manager.models.ocr]
#   evictable = true
#   priority = 80
#   reload_fail_cooldown_sec = 30
#
#   [model_manager.models.memory_embedding]
#   evictable = false   # 默认钉住（热路径，每个 MCP 请求都会用）
```

采样节奏与探针开关在 `config.example.toml` 的 `[resource_probe]` 段（常驻采样中枢：NVML 显存/利用率 + psutil 内存/CPU；无 NVIDIA 卡机器上 GPU 探针判 `absent`，属正常降级）。中枢结构与扩展点见 [docs/environment-constraints.md](environment-constraints.md) 的「资源采样中枢」。

### 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| `POST /models/ocr/load` 返回 503 | GPU 压力态 REFUSING/PROBE_DEGRADED | 等压力下降到 ARMED/NORMAL 后重试；或调 `/models/pause` 手动超驰（注意：PAUSED 期间逐出也暂停） |
| `POST /models/ocr/load` 返回 503 reason="reload_degraded" | OCR 连续 ≥3 次加载失败 | 重启后端（`POST /shutdown` 后由用户运行 `start.bat`，或 agent 用非提权命令重启） |
| `GET /models` 返回空 models 列表 | ModelLifecycleManager 启动失败 | 查后端日志 "ModelLifecycleManager 启动失败"；管理器降级为透传模式（模块行为回退现状，无自动监控） |
| `GET /models/pressure` 的 `gpu.probe_status = "absent"` | 该机没有 NVIDIA 卡/驱动（NVML 库由驱动提供） | 正常降级，不是故障：GPU 监控失效、准入放行。CPU-only 机器另把 `[models] paddle_device` 设 `"cpu"` 让推理真跑 CPU |
| `/health.model_manager` 字段缺失 | HealthResponse schema 漏声明 | 已修复（design §10 v2 修订），如仍缺失查 `server/core/health.py` 是否含 `model_manager: dict = {}` 字段 |
| MindForge 卸载后内存未释放 | 在途搜索持局部引用 | 等在途搜索结束自动释放；无 forced + 延迟补释放语义（已知局限） |
| 记忆语义检索突然变差（仅 BM25 命中） | memory_embedding 被手动卸载 | 调 `POST /models/memory_embedding/load` + `POST /memory/rebuild_vector_index` 补缺口 |

## 浏览器操作经验记录（强制）

**任何浏览器操作任务**（含 `browser_*` MCP 工具调用、Playwright、`connect_over_cdp`、`page.goto` 的任务，含 `web_archive`/`community_review`/`bilibili_gacha`/`arknights_gacha`/`endfield_gacha`/`official_gacha`/`html-dev-debug` 等浏览器相关 skill）必须遵守：

### 任务开始前（必做）

1. **识别目标网站主域**：从 URL 提取 eTLD+1（如 `https://www.xiaoheihe.cn/community/123` → `xiaoheihe.cn`）
2. **查网站索引**：`Read .agents/skills/browser_lessons/references/site_index.md` 看该主域是否已记录
3. **若有**：`Read .agents/skills/browser_lessons/sites/<domain>.md`，把"已知坑"和"DOM 结构"段读入上下文，**避免重复踩坑**
4. **若无**：任务中遇到非显然行为时按 `.agents/skills/browser_lessons/sites/_template.md` 创建新文件

### 任务收尾时（强制规则，已并入 task_closure step 5）

收尾 6 步流程的 step 5（文档自查）必须额外检查：本次浏览器操作是否遇到非显然行为？若是，写入对应 `sites/<domain>.md`，并同步更新 `.agents/skills/browser_lessons/references/site_index.md`。

**"非显然行为"判定标准**（满足任一即记录）：
- DOM 选择器与"看起来对"的不一致（如小黑盒作者应该是 `.info-box__username` 但实际取到评论者，正确是 `.link-user__username`）
- URL 有特殊参数携带真实数据（如小黑盒 `redirect_data` 含标题 JSON）
- 同一网站有多种页面布局（如小黑盒图文帖 vs 文章帖）
- 反爬/风控机制（如微信图片防盗链需 Referer）
- 登录态特殊处理（如必须用调试浏览器而非无头）
- 懒加载/异步渲染导致选择器失效（如微信图片 `data-src` 而非 `src`）
- 已知遗留问题（如微信评论 Vue 异步未渲染，需调 API）

**不需要记录的**：通用 Playwright/CDP 用法、一次性 bug（已修复）、skill 工作流内部决策（按点赞倒序等）。

### 与 key_pitfalls 的分工

| 类型 | 存放位置 |
|------|---------|
| 网站通用踩坑（DOM 结构、反爬、URL 规则、登录态、防盗链） | `sites/<domain>.md` |
| skill 特定踩坑（换网站就不适用的） | `agent_guide.py` 的 `key_pitfalls` |
| 跨网站通用浏览器踩坑（Playwright/CDP 通用问题） | AGENTS.md "API 常见陷阱" + `browser_lessons/SKILL.md` 末尾 |

> 详见 [`.agents/skills/browser_lessons/SKILL.md`](file:///<project_root>/.agents/skills/browser_lessons/SKILL.md)。

### 知识老化规则（防陈旧经验腐化任务）

站点经验文件含"最后更新"日期，`browser_match_site` / `browser_session_create` 响应含 `staleness_level`：
- **fresh**（<90 天）：可直接参考
- **warn**（90-180 天）：网站可能已改版，验证后再用；验证通过后用 `browser_write_lesson` 更新"最近验证日期"列
- **critical**（>180 天）：高度可能过时，必须验证关键坑是否仍适用后再参考

陈旧的"坑"记录反而会阻碍任务（如网站已修复某 bug 但经验仍标注为坑）。`browser_write_lesson` 写入会自动重置 staleness 为 fresh。

## 桌面操作经验记录（强制）

**任何桌面软件操作任务**（含 `execute_action`/`batch_actions`/`screen_accessibility_snapshot`/`screen_semantic_action`/`focus_window` 等 screen_* 工具调用）必须遵守：

### 任务开始前（必做）

1. **识别目标软件 process_name**：从 `list_windows` 或 `screen_app_list` 获取目标窗口的 `process_name`（如 `qbittorrent.exe`、`Code.exe`、`WINWORD.EXE`）
2. **查软件经验**：调 `screen_match_app(process_name=...)` 查询 `.agents/skills/computer_use/apps/<process_name>.md`
3. **若有经验**：把"UIA 友好度"、"已知坑"、"常用快捷键"、"菜单路径"读入上下文，**避免重复踩坑**
4. **若无经验**：任务中遇到非显然行为时用 `screen_write_lesson` 创建新文件
5. **list_windows/screen_app_list 响应含 `app_lessons_hint`** 时说明已有经验文件，应主动查询

### 任务收尾时（强制规则，已并入 task_closure step 5）

收尾 6 步流程的 step 5（文档自查）必须额外检查：本次桌面操作是否踩坑或发现最佳实践？若是，用 `screen_write_lesson` 写入对应 `apps/<process_name>.md`。

**"非显然行为"判定标准**（满足任一即记录）：
- UIA 不可用或部分不可用（如 Qt 表格控件 UIA 读不到，需 OCR bbox 定位）
- 快捷键与预期不一致（如某些软件 Ctrl+S 不是保存而是其他功能）
- 菜单路径不直观（如代理设置藏在非 obvious 的子菜单）
- 对话框处理有特殊坑（如某些对话框只能用快捷键关闭不能用点击）
- 窗口标题含版本号/动态内容导致标题匹配不稳定
- 配置文件被界面覆盖（如直接改 ini 被界面保存覆盖）
- DPI 缩放导致坐标偏移

**不需要记录的**：通用键鼠操作、一次性 bug（已修复）、通用 Windows 行为。

### 知识老化规则（防陈旧经验腐化任务）

软件经验文件含"最后更新"日期，`screen_match_app` / `list_windows` / `screen_app_list` / `screen_accessibility_snapshot` 响应含 `staleness_level`：
- **fresh**（<90 天）：可直接参考
- **warn**（90-180 天）：软件可能已更新/界面改版，验证后再用；验证通过后用 `screen_write_lesson` 更新"最近验证日期"列
- **critical**（>180 天）：高度可能过时，必须验证关键坑是否仍适用后再参考

陈旧的"坑"记录反而会阻碍任务（如软件已修复某 bug 但经验仍标注为坑）。`screen_write_lesson` 写入会自动重置 staleness 为 fresh。

### 与 key_pitfalls 的分工

| 类型 | 存放位置 |
|------|---------|
| 软件通用踩坑（UIA 友好度、快捷键、菜单路径、对话框处理） | `apps/<process_name>.md` |
| skill 特定踩坑（换软件就不适用的） | `agent_guide.py` 的 `key_pitfalls` |
| 通用桌面操作踩坑（DPI/焦点安全/坐标绑定） | `docs/computer-use-reference.md` + AGENTS.md |

> 详见 [`.agents/skills/computer_use/SKILL.md`](file:///<project_root>/.agents/skills/computer_use/SKILL.md)。

## PySide6 GUI 性能踩坑（client/ 开发必读）

**核心原则：`on_show()` 是高频调用，不能做重活。** 用户每次切换面板都会触发 `on_show()`，如果其中做同步 DB 查询 + 大量 widget 重建，会导致面板切换卡顿。

| 踩坑 | 后果 | 正确做法 |
|------|------|---------|
| `on_show()` 每次都重新读文件 + 销毁重建所有 widget | 工具面板每次切换卡 200-500ms（29 个 ToolDetailWidget 重建） | 用 mtime 检测：文件没变就不重建（[tools.py](file:///<project_root>/client/panels/tools.py) 的 `_read_manifest_mtime`） |
| `on_show()` 每次都重新查 DB + 填充表格 | 记账面板每次切换卡 300-800ms | 加首次加载标志：`_data_loaded` 为 True 后跳过，用户点刷新按钮才重新加载 |
| 循环中每行创建多个 `QComboBox` 作为 `setCellWidget` | 512 行 × 3 个 QComboBox = 1536 个控件，主线程卡 500ms+ | 懒加载（双击才创建）或分页（只加载前 N 条） |
| N+1 查询：循环中每行单独查 DB | 512 次 SQLite 查询（虽然每次快，但累积开销大） | 一次性预取：`{cat: descriptions for cat in all_categories}` |
| `QCalendarWidget` 在 `__init__` 中创建 | QCalendarWidget 是 Qt 最重控件之一，2 个同时创建卡 100ms+ | 延迟到 tab 首次切换时才创建（懒加载） |
| 主线程同步 DB 查询 | 查询期间 UI 完全冻结 | DB 查询放 `QThread`，通过 `Signal` 回主线程填充 widget |

**异步加载模式（QThread + Signal）**：
```python
class _DataLoader(QThread):
    data_ready = Signal(dict)
    def run(self):  # 后台线程
        data = store.query(...)  # DB 查询
        self.data_ready.emit(data)

def _load(self):  # 主线程
    self._loader = _DataLoader(store)
    self._loader.data_ready.connect(self._on_data)
    self._loader.start()  # 不阻塞

def _on_data(self, data):  # 主线程，填充 widget
    ...
```

**SQLite 跨线程**：`AccountingStore` 用 `check_same_thread=False` 创建连接，可在 QThread 中安全读取（写操作有 `_write_lock` 保护）。

**防重复加载**：异步加载时用 `_loading` 标志拦截重复请求（用户快速切换过滤时），避免多个 loader 竞争。

> 详见 `.agents/skills/client_dev.md` 的性能规范章节。

## 记忆系统

后端提供三层记忆系统 v3（`/memory/*`）：Recent 滑动窗口 + SQLite 时间索引 + 向量语义检索 + BM25 + HMS 风格"存取解耦 + 全息重构"。旧 KV 记忆已兼容，首次启动时自动迁移到 SQLite。Schema v2→v3 自动迁移（幂等）。

**v3 新增（参考 Shadow-Weave/HMS）**：
- **EvidenceLedger 中间层**：`memory_search` 改为 messages（BM25/向量）+ facts（SQL LIKE）+ summaries（SQL LIKE）三源召回，经证据组织器做 Jaccard 交叉佐证 + 时间窗口冲突检测。原原始事实不变，但推理结论可被审视和修正（HMS 核心）
- **facts 结构化字段**：`memory_set` 顶层支持 `fact_type`/`occurred_at`/`consumption_contexts`/`trigger_keywords`，写入 `facts` 表独立列。`consumption_contexts` 支持 SQL LIKE 索引优化匹配 task_type
- **SearchTracer 检索可观测性**：自动记录 query/子检索延迟/融合结果，支持 `sample_rate` 采样率。新增 `GET /memory/search/traces` 等端点（走 `parcae_advanced_tool` 网关）

**使用原则**：
- **任务完成时写记忆**：每个 Skill 工作流结束后，将任务进度、关键结果写入记忆（含 `consumption_contexts` + `trigger_keywords` 确保未来可消费）
- **会话开始时读记忆**：处理用户请求前，先 `GET /memory/{key}` 了解上次做到哪了
- **语义搜索**：用 `POST /memory/search` 按语义查找相关记忆（v3 自动三源召回 + 证据组织）
- **记忆 key 规范**：每个 Skill 对应一个 key（如 `bilibili_gacha`、`accounting`），跨 Skill 偏好用 `preferences`

**记忆生成**（用户触发）：用户说"生成记忆"/"保存经验"/"记忆总结"时 → 调 `agent_guide(task_type='recurring.memory_generation')` 获取 5 步工作流（recall→classify→dedup→write→optional_maintain）。结构化记忆含 `type`/`consumption_contexts`/`trigger_keywords` 字段，确保未来任务能消费。

**记忆维护器**（后端自动）：每 6 小时运行一次（下次记忆 API 调用时触发），流程为「垃圾预过滤 → 老化评分（>7 天未更新记 stale） → 指标日聚合 → 清理过期明细」，**零 LLM 调用**（LLM 验证器已于 2026-10-31 删除）。同时自动清理过期 `search_traces` 和 `evidence_ledger` 记录。手动触发：`POST /memory/maintain`（force=true）。

> 三层架构、语义检索、记忆压缩、自动记录、完整 API 表、记忆生成工作流详解、维护器配置详见 `docs/memory-system.md`。设计文档见 `.agents/wip/memory_system_hms_improvement.md`。

## 工具脚本与 LLM 池

项目包含大量工具脚本（抽卡采集、社群爬取、媒体分类、图片整理、批量注释等），通过 `exec_python` 或直接命令行运行。完整脚本列表和用法详见 `docs/tools-guide.md`，工具目录总览见 `tools/README.md`，机器可读清单见 `tools_manifest.json`。

`client/panels/tools.py`（ToolsPanel）+ `client/widgets/tool_runner.py` 是 GUI 工具面板，默认只读取 `tools_manifest.json` 来展示工具。新增工具时通常先改清单；只有 GUI 的布局、分类、队列或运行逻辑需要变化时，才改 `client/widgets/tool_runner.py` 本身。

脚本中调用 LLM 时，统一通过后端 LLM 并发池代理（`call_via_backend`），支持多 key 并发、拉链排队、per-project token 统计。

> LLM 并发池架构、API、使用方式详见 `docs/llm-pool.md`。
