# disk_io_audit —— 前后端磁盘读写审计

审计 Parcae 后端（`server.main`, FastAPI :8766）与前端（`client.main`, PySide6）
在**日常运行**中自身产生的磁盘读写量，用来发现「不合理的高频 / 过量落盘」。

## 脚本一览

| 脚本 | 作用 | 需要多久 |
|------|------|----------|
| `daily_run.py` | **全日检查的一键入口**（start / status / stop / report） | 一天 |
| `probe.py` | 持续采样进程 I/O + 目录快照 diff；`--report` 聚合成报告 | 5 分钟~几天 |
| `microbench.py` | 测「**一次操作**」的成本（一次 /guide、一次 MCP 调用…） | ~3 分钟 |
| `selftest_io_channels.py` | 校验计数器口径（哪些通道会被计入） | ~10 秒 |
| `global_io_attrib.py` | **整机层面**谁在读/写：全进程两次采样求差 + 整盘物理 I/O 对照 | ~30 秒 |
| `fix_port_and_start.py` | 重启后 8766 被 WinNAT 抢占时修复并拉起后端+探针（**需管理员**） | ~30 秒 |
| `restart_and_track.py` | 「重启后端 → 起追踪」一键（两级 detach，防被后端 Job 带走） | ~1 分钟 |
| `srum_attrib.py` | 回答「**某段时期内**谁在高强度读写」：读 Windows SRUM 账本 `SRUDB.dat`，出每应用总量 + 每小时时间线 + Top 榜 | ~1 分钟 |
| `ssd_wear_snapshot.py` | **跨周/跨月耐久监测**：读 NVMe SMART 的「数据单位写入」，按窗口记真实落盘字节 + harness 强度指纹（**已接入 loop 任务，每 30 分钟一行**，见下） | ~2 秒 |
| `ssd_wear_report.py` | 把上面的 CSV 折成「写入 × 活跃度」证据表：分桶中位数、相关性、异常窗口、散点图 | ~1 秒 |

> `srum_attrib.py` 有两个前置动作绕不过去：① SRUDB 被 SruService 独占锁定，须**管理员**先
> `esentutl /y C:\Windows\System32\sru\SRUDB.dat /d <副本> /o` 拷出；② 解析库 `dissect.esedb`
> **不在项目 venv 里**（只有这一个工具用得到，约 3 MB），装到任意独立目录后用 `--lib` 传入：
> `python -m pip install --target <lib目录> dissect.esedb`，缺库时脚本会打印这条命令并以 rc=1 退出。
> 本机这份已固化在 `tools/disk_io_audit/lib/`（`.gitignore` 已排除，4 MB），所以直接跑
> `uv run python tools/disk_io_audit/srum_attrib.py --db <SRUDB副本> --lib tools/disk_io_audit/lib`；
> ⛔ 别把依赖留在 `temp/` 下 —— temp 会被定期清理，脚本会「昨天能跑今天就崩」。
> 更完整的坑（TimeStamp 双编码、账本可能整体冻结、GMT+8 分桶）见
> `.agents/skills/dev/windows-process-disk-io-audit/SKILL.md`。

> `global_io_attrib.py` 的口径：进程级计数器是**逻辑 I/O**（含缓存命中），求和**可能大于**整盘物理 I/O
> ⇒ 排名可信，总量只能当近似；「整盘 − 进程之和」的正差额通常是内核/页文件/缓存回写。
>
> `fix_port_and_start.py` 的适用判据：后端日志出现 `[Errno 13] ... winerror 10013`，且
> `netsh int ipv4 show excludedportrange protocol=tcp` 有一段覆盖 8766，同时注册表
> `HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters` **没有** `PersistentPortExclusions`
> （有的话就不是 WinNAT 动态保留，停服务没用）。WinNAT 的保留区**每次开机重新随机分配**，
> 所以「昨天能起今天起不来」是正常现象，不是代码问题。

## 方法论（先读这段，否则会读错数字）

> 方法论的**真源**是 `.agents/skills/dev/windows-process-disk-io-audit/SKILL.md`（含八个必踩坑、
> 判读基准、长跑四件事、从审计到修复的解法表）。本节只留这套脚本的口径摘要，两处冲突以 skill 为准。

1. **被测对象是「进程自身」的计数器，不是进程树。**
   本探针多半由后端自己派生的终端启动，子进程（`_exec_runner.py`、探针自己的
   日志）会把噪声算到后端头上。所以 `--match` 只匹配 `server.main` / `client.main`，
   `--exclude` 排除 `probe.py` / `disk_io_audit` / `_exec_runner.py`。

2. **`GetProcessIoCounters` 是「逻辑 I/O」，不是「物理落盘」。**
   实测口径（`selftest_io_channels.py`）：
   - 文件写入 → **计入**
   - 回环 socket → **不计入**
   - stdout / 管道 → **计入**（⚠️ 这条最容易踩：测量脚本千万别用后端的
     `exec_python` 跑，控制台输出会被算进被测进程）
   另外它统计的是「交给 OS 的字节」，真正落盘发生在刷页时，Windows 会合并；
   所以它适合做**相对比较**（谁在写、写多频繁、量级差多少），不适合直接
   换算成 SSD 磨损。

3. **文件归因靠目录快照 diff**，不靠计数器 —— 计数器只给总量。
   `os.stat` 只读元数据，不产生 read transfer，对被审计对象几乎无污染。

4. **全量重写型文件看「改写上界」列，不要看「净增」列。**
   `tmp + os.replace` 这类原子写的净增恒为 0，真实写入量 ≈ 文件大小 × 次数。

## 跑一轮短审计（约 10 分钟）

```bash
# 1. 空转基线（期间尽量别用 agent，否则 MCP 调用会混进去）
.venv/Scripts/python.exe tools/disk_io_audit/probe.py \
    --duration 600 --interval 5 \
    --dirs data,logs,private_vault \
    --match server.main,client.main \
    --out temp/disk_io_audit_YYYYMMDD/probe_baseline.jsonl

# 2. 聚合
.venv/Scripts/python.exe tools/disk_io_audit/probe.py \
    --report temp/disk_io_audit_YYYYMMDD/probe_baseline.jsonl

# 3. 单次操作成本
python tools/disk_io_audit/microbench.py --idle 60 --n 5
```

## 全日检查（跑一整天，收集真实「每日写入面」）

用一键入口 `daily_run.py`：它自己建目录、起**脱离终端**的探针，并且**等首次心跳确认**
才返回 —— 长跑最怕「以为在采、其实没采」。

```bash
# 1) 早上开机后：启动全天采样（默认 --duration 0 = 一直跑到手动停）
.venv/Scripts/python.exe tools/disk_io_audit/daily_run.py start

# 2) 白天随时：探针还活着吗？跑到哪了？
.venv/Scripts/python.exe tools/disk_io_audit/daily_run.py status

# 3) 收工：出报告（可直接对还没跑完的文件出）
.venv/Scripts/python.exe tools/disk_io_audit/daily_run.py report

# 4) 需要提前结束
.venv/Scripts/python.exe tools/disk_io_audit/daily_run.py stop
```

产物全在 `temp/disk_io_audit_<YYYYMMDD>/`：
`probe_daily.jsonl`（采样）、`probe.heartbeat`（心跳）、`probe.log`（stdout）、
`RUN_INFO.json`（本次启动参数，含探针真实 pid）。

### 三条铁律（否则一天白跑）

1. **必须从后端终端启动，不要从 agent 的 bash 沙箱里起。**
   实测（2026-10-01）：沙箱里起的进程会被反复冻结 —— 第一次采样隔了 **94 秒**才出现，
   而且终止进程会被**静默拦下**（进程实际已死，`wait()` 却因权限读不到而报超时）。
   后端终端（`exec_terminal_spawn` / `POST /terminals/spawn`）不受此限：实测
   `--interval 10` 连续 4 个 tick 全部准点。
2. **中途挂了，直接再 `start` 一次即可。** jsonl 以追加模式写，探针重启后首 tick
   增量为 0（**不会重复计数**）；报告已能识别多段并正确计算跨度，口径会显示
   「文件含多次续跑」。所以「掉了」= 数据断一段时间，不是数据损坏。
3. **别跳过 `start` 返回的那行「启动确认」。** 8 秒内拿不到心跳 = 没跑起来，
   它会自动把 `probe.log` 末尾打出来。补一句：`start` 报告的 pid 是**启动器**的，
   `.venv` 的 python 会再 exec 一次，权威 pid 在心跳里（`stop` 用的就是它）。

### 默认参数

`--interval 30`（一天 2880 tick、JSONL 约 10~20 MB）、
`--dirs data,logs,private_vault`、`--match server.main,client.main`。

要覆盖更多面可以加 `--dirs data,logs,private_vault,temp` —— 但会把**其它工具**在
`temp/` 的写入掺进来，解读时要注意区分。

### 观察期结束后

- **把 `temp/disk_io_audit_<日期>/` 整目录留好或搬走**（`temp/` 会被清理）。
- `report` 里：【一】按进程、【三】按文件（区分「追加型 / 重写型」两种口径）、
  【三-a】SQLite 附属文件单列、【三-b】折算每天、【四】高频重写信号、
  【五】占全盘物理写入的百分比。

> ⚠️ **读【三】前先知道两个口径陷阱**：
> ① **`.db-shm` / `.db-wal` / `.db-journal` 已从主榜剔除、单列在【三-a】**。
> 它们的 mtime 一动不代表内容全量写入 —— `-shm` 是 32 KiB 的 mmap 共享索引（改几字节
> 也 bump mtime），`-wal` 被 checkpoint 时会**变小**（净增为负 ⇒ 会被人为判成"整文件
> 重写"）。早期版本把两者按"重写"记，于是每次 tick 凭空多出 2×32 KiB 幽灵写入，
> 把真实写手挤出 Top 榜。
> ② **冷启动（后端重启）的读量是数量级尖峰**：实测一次冷启动读 **176~192 MiB**
> （模型加载），而写入只有 **44 KiB**。所以报告同时给「含一次性尖峰」与「剔除最高
> 2% 尖峰」两个外推 —— **看稳态请用后者**，否则 24h 读量会被外推成 7 GiB+。
> 另注：被测进程 PID 变化（后端重启）后的**第一个 tick 增量恒为 0**（新 PID 无历史
> 计数器基线），该 tick 的进程级 I/O 会漏计；此时以【三】的文件 diff 为准。

> ⚠️ `--dirs` 不要包含**探针自己输出所在的目录**（脚本会自动忽略 `--out` 与
> `--heartbeat` 的父目录、以及 `temp/terminals/`），否则探针自己写的 jsonl 会冒充
> 被审计文件。

## 直接用 probe.py 跑长采样（不走 daily_run）

```bash
.venv/Scripts/python.exe tools/disk_io_audit/probe.py \
    --duration 0 --interval 30 \
    --dirs data,logs,private_vault \
    --out temp/disk_io_audit_YYYYMMDD/probe_long.jsonl \
    --heartbeat temp/disk_io_audit_YYYYMMDD/probe.heartbeat
```

## 跨周/跨月耐久监测（`ssd_wear_snapshot.py` + `ssd_wear_report.py`）

### 挂在哪跑：loop 任务为主，开机启动兜底

| 入口 | 节奏 | 作用 |
|---|---|---|
| loop 任务 `disk_manager.ssd_wear` | 每 30 分钟一行 | **主力**：能分出「高强度时段 / 空闲时段」 |
| `programs.json` 条目 `SSD写入监测` | 每次登录 +120 s | 兜底：后端很久没起时仍有数据（`--skip-if-recent 900` 防重复行） |

配置在 `config.toml` 的 `[loops.disk_manager]`（`ssd_wear_interval` / `ssd_wear_first_run_delay` /
`skip_if_recent_secs`），action 实现在 `workspace/disk_manager/loop_actions.py`。
改完要**重启后端**才生效（`TASK_BUILDERS` 在 `loop_manager` 模块导入时合并）。

**为什么挂在 loop 上可以容忍后端重启/崩溃**：SMART `Data Units Written` 是**盘级累计**，
间隔多久都能正确相减 —— 断档只会让窗口变宽，不会丢数据。这与「进程计数器」类方案
（重启即清零）本质不同，所以不需要为了采数去养一个常驻进程。

**为什么不能只挂开机**：开机那一条确实能拿到「上一次会话总共写了多少」，但它同时记的
强度指纹是**开机 2 分钟后的空闲态**（进程刚起、CPU 很低）⇒ 写入量与活跃度**配对错位**，
永远回答不了「高写入是不是发生在高强度时段」。这是把采样挪进 loop 的真正理由。

### 为什么不能用 SRUM / psutil 回答「一共写了多少 TB」

| 尺子 | 性质 | 能否当落盘字节 |
|---|---|---|
| NVMe SMART `Data Units Written` | 盘控制器自己数、**单调递增**、跨重启持续 | ✅ **真值** |
| SRUM `BytesWritten` | 进程逻辑写（含 IPC、页缓存重复脏化） | ❌ 本机实测可虚高数倍 |
| psutil `io_counters()` / 任务管理器 | 同上，Electron 多进程下更极端 | ❌ 实测虚高过 **36×** |
| 物理盘 `Win32_PerfRawData_PerfDisk` | 自开机累计的块设备字节 | ⚠ 可信但**开机清零**，只适合同会话窗口 |

2026-11-03 校准（同一 45 s 窗口、两边同时取差分）：SMART **0.136 GB** vs psutil `PhysicalDrive0`
**0.135 GB**，差 0.7% ⇒ 单位换算 `Data Units × 1000 × 512` 没问题，SMART 差分可以当落盘量用。

### 「是不是高强度使用」怎么判 —— 三个观测量，看关系不设阈值

每行 CSV 记同一窗口内的三个量（`win_secs` / `dw_bytes` / `cpu_s` / `cores` / `harness_json`）：

| 观测量 | 含义 | 已知限界 |
|---|---|---|
| `dw_bytes` → GB/h | 该窗口真实落盘字节 | 真值（设备计数器） |
| `cores` = `cpu_s / win_secs` | 窗口内 harness 进程平均占用几个核 | **下限**：进程中途退出，最后一段 CPU 结算不到（首轮 `new_pids` 全高，属正常） |
| `log_n` / `log_wr_mb` | 被写日志文件数 / 日志新增字节 | 只在**上一轮有基线**时才算增量；无基线且非本窗口新建 ⇒ 记 0（宁可少算，不虚报） |

CPU 用 `pid:create_time` 做键与上一轮快照求差（防 pid 复用）。三个都已实测过的坑：
**系统累计 CPU 开机清零**（跨重启差分为负）、**日志目录总体积会被轮转清掉**
（WorkBuddy 曾 1088 MB → 204 MB，不能拿目录体积做增量）、**harness 累计 CPU 随进程退出丢失**
（所以必须落基线求差，不能只读当前值）。

判读方式（`ssd_wear_report.py` 直接输出）：
- **按核数分桶**看写入中位数：若「≥2 核」桶的 GB/h 明显高于「≈0 核」桶 ⇒ 写入确实跟着负载走；
- **核数≈0 但写入 > 中位数 2 倍**的窗口单独列出来 —— 这些**不是 agent 写的**，要回头用
  `srum_attrib.py` 找别的来源（系统更新 / 索引 / 备份 / 其它进程）；
- 相关系数（Pearson/Spearman）只说明同向，**不说明因果**，幅度是否成比例要看分桶表。

### v3 加的第四个观测量：内存压力（`commit_gb` / `pf_used_mb` / `pf_peak_mb`）

**这是 2026-11-03 查那个 9.79 GB 突发逼出来的。** 那次实测结论：大额写入**不是**任何 app 的数据写入，
而是**内存压力下的分页写回落（pagefile）** —— 而分页写入有一个致命性质：

> `pagefile.sys` 的 mtime **冻结在开机时刻**（本机 10:17:41 开机后再也没变过）。
> 往页文件写字节**不产生任何文件时间戳** ⇒ 全盘 mtime 扫描（实测扫过 2,676,975 个文件）
> 只会找到 0.07 GB，让人误以为"没人写"。**别拿"扫不到文件"当"没有写入"。**

当时的硬数字（开机 4.54 h 内）：disk0 写 **61.15 GB**，其中 `\Memory\Pages Output` 累计
**43.05 GB = 70%**；SRUM `System` 条目当日累计 36.69 GiB 独立佐证（差 9%）。

所以每个窗口现在同时记提交电荷与页文件用量。判别方式：

| 观测 | 结论 |
|---|---|
| 设备增量大，页文件已用/提交也同步涨 | **分页写回落**（`System` 名下，内核工作线程记账）——对 SSD 是纯损耗、零信息留存 |
| 设备增量大，页文件净增 ≈ 0、提交平稳 | 另有落盘路径：某应用真写文件 / 写后即删的临时文件 / 缓存写回 |

⚠ 两个必须一起看的限界：
- `pf_used_mb` 是**水位**不是流量 —— 页被读回释放时水位会降，所以只能看「同向且同量级」，不能当流水账；
- `Pages Output` 只有「自开机累计」这一个原始计数器（`Win32_PerfRawData_PerfOS_Memory.PagesOutputPersec`
  是 **PERF_COUNTER_COUNTER 的累计原始值**，`Formatted` 版本才是 /s），也切不到单分钟。

```bash
# 文本汇总（默认只吃 ≥10 分钟窗口，避免 30 秒窗口的瞬时速率冒充结论）
uv run python tools/disk_io_audit/ssd_wear_report.py
# 另存散点图（x=核数, y=GB/h, 点大小=窗口写入量）
uv run python tools/disk_io_audit/ssd_wear_report.py --html temp/ssd_report.html
```

### 读结果

```bash
# 看存量（不追加）
cut -d, -f1,3,4,6 workspace/disk_manager/ssd_wear_log.csv
```

对 `2026-10-30` 这类尖峰日（SRUM 报 1216 GiB 写）要先怀疑口径：
**只有 SMART 的日增量才是当天真的落了多少字节。**

旧 schema（v1，无 `win_secs`/`dw_bytes`/`cores` 列）会在首次运行时自动升级，
原文件留一份 `.bak-schema`。

⚠ 不要用「寿命百分比没变」当安全证据：该字段粒度 1%，按本机当前速率约 82 天通电才动一格。

## 参数速查

```
daily_run.py
  start | status | stop | report
  --outdir DIR          产物目录（默认 temp/disk_io_audit_<YYYYMMDD>）
  --interval SEC        采样间隔（默认 30）      --duration SEC  0=一直跑（默认）
  --dirs / --match      透传给 probe.py          --force        忽略「已有探针在跑」
  --extra ...           其余参数透传给 probe.py

probe.py
  --out PATH            JSONL 输出（默认 temp/disk_io_audit/probe.jsonl）
  --interval SEC        采样间隔（默认 10）
  --duration SEC        总时长，0=无限（默认 0）
  --match  A,B          被测进程 cmdline 关键字（默认 server.main,client.main,_exec_runner）
  --exclude A,B         排除的进程 cmdline 关键字（默认排除探针自身与其传输外壳）
  --dirs   A,B          监视目录（默认 data,logs,temp,private_vault；相对路径按项目根解析）
  --ignore A,B          额外跳过的路径前缀
  --heartbeat PATH      心跳文件：每 tick 原子写入存活状态与当前速率（长跑必备）
  --report FILE         聚合模式
  --verbose             每个 tick 都打印

ssd_wear_snapshot.py
  --csv PATH            累计记录（默认 workspace/disk_manager/ssd_wear_log.csv）
  --state PATH          CPU / 日志基线（默认 workspace/disk_manager/ssd_intensity_state.json）
  --log PATH            pythonw 下的输出日志（stdout 不可写时自动重定向）
  --tbw TB              标称耐久，用于算剩余年限（默认 600）
  --delay-secs SEC      先等 N 秒再采集（开机启动用，等磁盘活动平息）
  --skip-if-recent SEC  距上次采集不足 N 秒则跳过（loop 用 600，开机兜底用 900）
  --json                输出单行 JSON（loop 任务的 action 解析用）
  --quiet               只写 CSV，不打印分析

ssd_wear_report.py
  --csv PATH            累计记录
  --disk N              看哪块盘（默认 0 = 系统盘）
  --min-win SEC         只用窗口 ≥N 秒的行（默认 600）
  --since YYYY-MM-DD    只看该日期之后
  --html PATH           另存散点图 HTML
  --quiet               只写 CSV，不打印分析
```
