# 调试浏览器 profile 数据安全（铁律）

> 适用范围：`chrome_debug/`（以及任何 `[browser] user_data_dir` 指向的调试 profile）。
> 违反下面的条例**不会报错**，只会静默删数据——所以必须当成红线，不是建议。
> 配套工具：`tools/browser/profile_guard.py`（backup / inspect / restore）。

## 0. 为什么有这份文档

这个 profile 里装的是**用户真实的日常浏览数据**：6872 条访问历史（最早 2026-07-27）、300+ 条登录态 cookie、34 个扩展、38MB 扩展配置、34MB localStorage。它是从用户工作 profile 克隆来的，**不是一次性沙盒**。

在此之前已经发生过三次同类事故，全部同一个病根：**没搞清楚这个目录的来历与校验机制就动手**。

| 时间 | 事故 | 真因 |
|---|---|---|
| 09-05 | 旧版 Chrome 打开新 profile 当场闪退，被写成「必须用 Edge」 | 降级方向（新版写、旧版读），被误包装成生态结论 |
| 10-24 18:38 | `Default/Extensions` 下 13 个扩展**文件被删光**，只留空壳目录 | 用 Chrome 打开 Edge 写过的 profile，扩展条目 MAC 校验不过 |
| 10-24 19:0x | 我把整个后端挂在 harness 会话下，会话清理时后端被硬杀 → 关光 session page → Chrome 退出、9222 消失 | 起进程的通道错了（见 `environment-constraints.md`） |

## 1. 命根子清单（这些丢了就是真丢）

| 路径 | 里面是什么 | 丢了什么后果 |
|---|---|---|
| `Local State` | `os_crypt.encrypted_key`（cookie/密码的加密密钥）、版本标记 | **所有登录态解不开** |
| `Default/Secure Preferences` | 扩展条目 + MAC 签名 | 扩展全部消失 |
| `Default/Extensions/` | 扩展实体文件 | 同上 |
| `Default/Network/Cookies` | 登录态 | 全部站点重新登录 |
| `Default/Local Storage/` | 站点 localStorage（大量 token 在这里） | 部分站点掉登录 |
| `Default/Local Extension Settings/` | 扩展自己的数据（KeePassXC 配置等） | 扩展要重新配 |
| `Default/Login Data` / `Web Data` | 站内密码、表单 | 自动填充失效 |

## 2. 铁律

### R1 一个 profile 只许一个浏览器打开
`chrome_debug/` **只许 Chrome 打开**，且 `config.toml [chrome] browser_path` 钉死 Chrome。
⛔ 禁止 `--browser-path` 指向 Edge / Brave / Vivaldi。

**机制（2026-10-24 实测确认）**：扩展条目的 MAC 用**每个浏览器各自的种子**校验，种子存在注册表：

```
HKCU\Software\Google\Chrome\PreferenceMACs\Default      ← Chrome 的种子
HKCU\Software\Microsoft\Edge\PreferenceMACs\Default     ← Edge 的种子
```

所以「同一份 profile 换浏览器打开」= 用别人的种子验自己的签名 ⇒ **校验不过的条目被删除，连 `Extensions/<id>/` 下的文件一起删**。这是浏览器的自我保护，不是 bug，也不会弹窗，不会通知。

### R2 动 profile 之前必须先备份，且自检通过才动手
```bash
python tools/browser/profile_guard.py backup --profile chrome_debug
```
它会：复制 9 项命根子 → 写 `manifest.json`（体积/文件数/sha1/扩展 ID/cookie 条数）→ **立刻重算自检**，不一致 `exit 1`。
⛔ 自检不过就**停手**，不要「先改了再说」。备份落 `backups/browser_profile/<profile>_<时间戳>/`。

### R3 先停实例，再备份或替换
浏览器运行时会**独占锁定** `Cookies`（sqlite 打不开，报 `unable to open database file`），并且 `Local State` / `Preferences` 在内存里有未落盘状态，退出时会覆盖你刚写进去的文件。
停实例走后端通道（R8），不要在浏览器跑着的时候替换 profile 文件。

### R4 不许随手造测试 profile
⛔ 默认**禁止**为验证某个想法新建 profile。
确需时必须：① 起名 `temp/bp_<用途>_<时间戳>`；② 只用一次；③ **收尾立即删除并自检**：
```bash
ls -d temp/*profile* chrome_debug*      # 只应出现已知项
```
留一个 12MB 的 `temp/edge_goldset_profile` 在仓库里，和「把用户数据搞乱」是同一种邋遢。

### R5 报告「数据丢了」之前，先查备份、先试还原
顺序永远是：**查 `manifest.json` → 查 `backups/browser_profile/` → 试 `restore` → 确认救不回来 → 才允许说「丢了」**。
⛔ 禁止先对外宣布损失、再回头想办法（10-24 就是这么干的：先报「13 个扩展被清空」，其实扩展本体在用户的 工作 profile 里好好的）。

### R6 扩展缺失走「克隆」，不是让用户手点
用户的工作 profile 就是最佳源：
```bash
# 停调试实例后（R3），从工作 profile 克隆扩展三件套
cp -r "<Chrome User Data>/Default/Extensions/." chrome_debug/Default/Extensions/
cp -p "<Chrome User Data>/Default/Secure Preferences" chrome_debug/Default/
cp -p "<Chrome User Data>/Default/Preferences"        chrome_debug/Default/
```
**不要动 `Local State` 与 `Cookies`**（那会换掉 cookie 密钥，把现有登录态全废掉）。
实测（2026-10-24 19:10）：这样克隆后 Chrome 140 启动，**34 个扩展全部加载**（含 KeePassXC-Browser `oboonakemofpalcgghocfoadofidjkkk`），306 条 cookie 一条没丢。
⛔ 也不许把「chrome://extensions → 加载已解压的扩展程序」当成默认方案丢给用户点——那是 137+ 之后 `--load-extension` 失效时的**最后手段**，不是第一选择。

### R7 浏览器选型与路径必须本机实测，禁止凭记忆断言
⛔ 不许出现「Chrome 没装 / 找不到在哪 / 只能用 Edge」这类**没核实就下结论**的话。
核实三步，缺一不可：
1. 文件系统：`C:\Program Files\Google\Chrome\Application\chrome.exe`、`C:\Program Files (x86)\...`
2. 注册表：`HKCU\Software\Google\Chrome\BlBeacon` 或 `App Paths\chrome.exe`（拿真实安装路径与版本）
3. 运行证据：`GET http://127.0.0.1:9222/json/version` 的 `Browser` 字段

文档结论与实机冲突时：**以实机为准，当场改文档**，并把冲突记进 `CHANGELOG.md`。

### R8 起停浏览器一律走后端，不走 harness
见 `docs/environment-constraints.md`「调试浏览器实例」：`POST /terminals/spawn` + `cmd_list` 数组。
harness 侧 Bash / `run_in_background` 起的进程会随会话清理被回收（后端自己也是，见同文档 2026-10-24 补丁）。

## 3. 标准动作（照着做，不要临场发挥）

```
① 现状盘点：profile_guard.py inspect --profile chrome_debug
② 备份    ：profile_guard.py backup  --profile chrome_debug     # 必须自检通过
③ 停实例  ：后端 terminals/spawn → taskkill /T /F /PID <9222 监听 pid>
④ 改数据  ：按 R6 克隆，或改 config，不动 Local State / Cookies
⑤ 起实例  ：后端 terminals/spawn → start_debug_browser.py（cmd_list 数组）
⑥ 验收    ：/json/version 有 Browser 字段 + cookie 条数没掉 + chrome://extensions 卡片数没掉
⑦ 失败    ：profile_guard.py restore --profile chrome_debug --backup <步骤②目录> --yes
```

**验收判据（缺一条不算通过）**：
- `GET /json/version` 返回 `Chrome/<版本>`
- cookie 条数 ≥ 动手前（用 CDP `ctx.cookies()` 数，不要读 sqlite——运行中打不开）
- `chrome://extensions` 的 `extensions-item` 数量 ≥ 动手前（注意卡片在 **shadow DOM** 里，`document.querySelectorAll` 数不到，要穿 `extensions-manager` → `extensions-item-list` 的 shadowRoot）

## 4. 常见误判对照表

| 现象 | 别急着说 | 实际是 |
|---|---|---|
| 9222 没了，进程也没了 | 「浏览器神秘死亡」 | 先查后端还活着吗、日志末行有无 shutdown；后端死 → `close_all()` 关光 tab → Chrome 退出 |
| 扩展目录空壳 | 「扩展被我删了」 | 跨浏览器打开触发 MAC 校验失败的自删；实体在源 profile 里，R6 克隆即可 |
| cookie 数变少 | 「登录态丢了」 | 先确认密钥（`Local State`）有没有被替换过；替换过 = 全废 |
| `--load-extension` 不生效 | 「参数写错了」 | Chrome 137+ 品牌版禁用该开关；只能靠 profile 里已注册的扩展 |
| 扫描 profile 得到 0 条 cookie | 「数据没了」 | 浏览器在跑 → sqlite 被独占锁；停实例再数，或用 CDP 数 |

## 5. 相关

- `docs/environment-constraints.md`「调试浏览器实例」——启动通道、回收机制、端口保留区
- `tools/browser/profile_guard.py`——备份/盘点/还原
- `tools/browser/start_debug_browser.py`——启动脚本（`--copy-user-data` 用于**首次**克隆整份 profile）
- `AGENTS.md` 第 1 步第 7 条——agent 必读入口
