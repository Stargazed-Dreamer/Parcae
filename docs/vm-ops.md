# 虚拟机操作方案（全项目）

适用范围：**任何**需要"开 / 控制 / 装 / 取证一台虚拟机"的任务，不局限于某个作业组件。
本机既定策略是"能放进 VMware 虚拟机的就放虚拟机"（见项目记忆 `project-hci-jsd-autonomy-decisions`），
所以这份方案是通用基础设施，不是 homework 私有约定。

工具实现：[workspace/vm_vnc_ctl/](../workspace/vm_vnc_ctl/README.md)（VNC 通道）+ `<data_drive>:/<apps_root>\VMware\vmrun.exe`（GuestOps 通道）。
路由入口：`agent_guide(task="开虚拟机 / 装虚拟机 / 虚拟机快照 …")` → `adhoc.vm_ops`。

---

## 0. 一句话决策

**guest 里有系统且 open-vm-tools 在跑 → 用 `vmrun`；guest 里还没系统（装机）或以 `nogui` 无窗口运行却要看画面 → 用 VNC 通道。**

选错通道是本轮装机最大的时间黑洞：装机期 `vmrun` 的 GuestOps 会直接报 `The VMware Tools are not running`，
而 `nogui` 期 `vmrun captureScreen` 会**假成功**（见 §5 坑 3）。

## 1. 通道决策表

| 我要做什么 | 用什么 | 为什么不是另一个 |
|---|---|---|
| 在装好的系统里执行命令、取输出、传文件 | `vmrun` GuestOps（`-gu/-gp` + `runProgramInGuest` / `copyFileFrom*To*`） | VNC 要模拟键盘敲命令，还会被 Shift 坑（§5 坑 1） |
| 装操作系统 / 走 GUI 安装向导 | VNC 通道 | 此时 guest 没有系统、没有 tools，`vmrun` 全部不可用 |
| VM 以 `nogui` 跑，但需要看/点图形界面 | VNC 通道 | `vmrun captureScreen` 在 nogui 下只给 1×1 空图 |
| 需要人在场实时观看 | `vmware.exe` + `vmrun start <vmx> gui` | 无 |
| 回滚一次做坏的环境实验 | `vmrun revertToSnapshot` | 无（前提：事前打过快照，见 §4） |
| 让 agent 抢宿主鼠标键盘去点 VM 窗口 | **都不要** | 屏幕接管会占用户输入设备，且长空等会把任务授权熬到撤销；VNC 完全无感 |

坐标语义差异（很容易搞错）：VNC 通道的 x/y **就是 guest 原生像素**（`vnc_ctl.py info` 打印分辨率），
不需要宿主 DPI / 窗口偏移换算；而屏幕接管路线（`screen_ocr(mode="window")`）拿到的是窗口截图内坐标，必须再加窗口 `left/top`。

## 2. `vmrun` 通道标准姿势

```bash
VMR="<data_drive>:/<apps_root>/VMware/vmrun.exe"
VMX="<data_drive>:/<vm_store>\<VM>\<VM>.vmx"

"$VMR" -T ws start "$VMX" nogui              # 或 gui
"$VMR" -T ws -gu <user> -gp <pass> runProgramInGuest "$VMX" headless /bin/bash -c "<cmd> > /tmp/out.txt 2>&1"
"$VMR" -T ws -gu <user> -gp <pass> copyFileFromGuestToHost "$VMX" /tmp/out.txt "F:\...\out.txt"
"$VMR" stop "$VMX" soft                        # 有 tools 才会秒回；挂住=tools 没起来
"$VMR" list                                    # 判"到底在不在跑"的唯一可信来源
```

要点：
- `runProgramInGuest` 必须带 `headless`/`interactive` 位置参数，漏了会被当成 program 名。
- **返回码不可信**：涉及"是否真的拿到了东西"时，去量产物（文件尺寸 / 像素数），别只看 `rc=0`。
- 中文输出可能变 `?`（非交互环境缺 locale），脚本内 `export` 好再跑（此坑由 `workspace/homework/subjects/large_db/conventions.md` 记录）。
- `vmrun` 没有 `setParam` 子命令；改 `.vmx` 只能在**虚拟机电源关闭时**编辑文件，运行中改会被回写覆盖。

## 3. VNC 通道（装机期 / nogui 期）

虚拟机关机后往 `.vmx` 追加（键名从 `vmware-vmx.exe` 二进制里提取核实，不是抄来的）：

```
RemoteDisplay.vnc.enabled = "TRUE"
RemoteDisplay.vnc.ip = "127.0.0.1"      # 只绑回环：别把 guest 屏幕暴露到网络
RemoteDisplay.vnc.port = "5901"
RemoteDisplay.vnc.password = "<=8字符"   # VNC 口令只有前 8 字节有效
```

然后 `vmrun -T ws start <vmx> nogui`，用 [workspace/vm_vnc_ctl/vnc_ctl.py](../workspace/vm_vnc_ctl/vnc_ctl.py) 驱动：

```bash
export VM_VNC_PASSWORD=...              # 故意不给默认值，也不写进仓库
python workspace/vm_vnc_ctl/vnc_ctl.py info      # 1280x800 desktop='...' bpp=32
python workspace/vm_vnc_ctl/vnc_ctl.py ocr       # 抓帧 + 本机 OCR，输出 guest 像素坐标
python workspace/vm_vnc_ctl/vnc_ctl.py click 711 642
python workspace/vm_vnc_ctl/vnc_ctl.py type "hostname"
```

## 4. 快照纪律

环境会被做坏（装驱动、跑实验、改库），**动手前先打基线快照**，坏了 `vmrun revertToSnapshot` 回滚，
而不是重装（一次全新装机实测 ~20 分钟）。命名建议带日期与状态，例如 `baseline-20260928-clean`。

**基线类快照一律关机态。** `vmrun snapshot` 在运行中的虚拟机上**必带内存态且没有开关**：
8GB RAM 实测写 ~10GB 宿主磁盘、耗时 ~3 分钟，而且 `vmrun` 进程在快照完成后迟迟不返回
（占住 `.lck`，后续 `vmrun` 全部挂起）。关机态快照 `rc=0` 秒回，`.vmsn` 里没有 `type` 字段，
也不产生 `.vmem`（判据：`grep 'type =' <vmx目录>.vmsd` 与 `ls *.vmem`，别只看返回码）。
内存态只在"要冻住跑到一半的进程状态"（登录中、安装中）时才特意用——环境基线不需要。
删掉带内存态的快照走 `vmrun deleteSnapshot`，会触发一次 vmdk 合并。

## 5. 坑清单（1–6 为 2026-09-28 实测，7–11 为 2026-09-29 实测，12–14 为 2026-09-30 实测，15–20 为 2026-10-05 实测；Workstation 16.2.3 + Hyper-V 共存走 WHP）

1. **VNC 的 KeyEvent 丢 Shift。** `$ | > & _ { } !` 和所有大写会退化成未移位基键（`$2,$3`→`42,43`、`&&`→`77`、`>`→`.`、`_`→`-`、`NET_OK`→`net-ok`），显式发 `Shift_L` 也救不回来。
   对策：`vnc_ctl.py type` **默认拒绝**含这些字符的输入并列出罪证；往 guest 塞复杂命令时改成"先在 guest 落一个脚本文件，再执行它"。
2. **RFB `ServerInit` 的宽高是 `u16` 不是 `u32`。** 按 u32 读会整体错位 4 字节，症状是"连上、认证过、然后永远等不到帧"直到超时。
3. **`vmrun captureScreen` 在 `nogui` 虚拟机上假成功**：`rc=0` 无报错，产出 PNG 是 **1×1 像素**。
4. **VNC 服务端会在 guest 重启若干轮后"接了 TCP 但不发 RFB 问候"**。处置：**电源循环一次**（`vmrun stop soft` → `start nogui`）即恢复，实测有效，不用改配置。
5. **手写 `.vmx` 缺 `pciBridge0/4/5/6/7`（`virtualDev = "pcieRootPort"`）与显式 `pciSlotNumber`** → 开机报 `[msg.pci.noslotavail] No PCIe slot available for Ethernet0`，`DevicePowerOn` 失败，并在 UI 挂一个模态错误框**占住 `.lck`**，之后所有 `vmrun start` 都变成"操作被取消"。
   对策：**抄一台本机已能跑的 `.vmx` 当设备骨架模板**，别手写。
6. **Ubuntu 桌面选"默认集合"不含 `open-vm-tools`** → GuestOps 全不可用。补 `sudo apt install open-vm-tools open-vm-tools-desktop`。旁证：装好后 `vmrun stop soft` 从"挂死"变成"秒回"。
7. **`runScriptInGuest` 以 `-gu` 那个普通用户身份执行，不是 root。** 2026-09-29 实测 `id` → `uid=1000`，脚本里 `apt-get update` 因 `/var/lib/apt/lists` 加锁失败仍打印 `PHASE-update-OK`（假绿灯）。要 root 就在 guest 脚本里显式 `sudo`；并建议配 `/etc/sudoers.d/NN-<user>-nopasswd`，免得口令出现在每条命令行与被推送的脚本里（只在设置那一条 sudoers 时用一次口令）。
8. **从 GuestOps 起 guest 的 GUI 程序必须走 `systemd-run --user`。** 直接 `nohup /usr/bin/<app>` 缺 `DISPLAY`/`WAYLAND_DISPLAY`/`XAUTHORITY`：症状是主进程活着、渲染子进程全变 `<defunct>`，日志 `zygote_linux.cc(673) write: Broken pipe`。瞬时用户服务自动继承登录会话的环境块，比手传可靠（手传容易写出 `DISPLAY=:-0` 这种值）。`gdm-wayland-session` 的 `/proc/<pid>/environ` **不含** `WAYLAND_DISPLAY`，别拿它当环境来源；权威来源是 `systemctl --user show-environment`。
9. **同一个环境缺口也会挡住 `systemctl --user <子命令>` 本身。** 2026-09-29 实测：从 GuestOps 直接跑 `systemctl --user is-active wla-wechat` 报 `Failed to connect to user scope bus via local transport: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined`，但**同一时刻目标 unit 是 active 的**（`pgrep` 能看到进程）。也就是说这个报错**不代表服务没在跑**，别拿它当"服务挂了"的证据。要么显式补两个变量（`XDG_RUNTIME_DIR=/run/user/<uid>`、`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus`，uid 从 `id -u` 拿），要么改用不依赖总线的手段（`pgrep` / 读 `/sys/fs/cgroup/user.slice/...` 目录）。
10. **往 guest 推 `.bt`（bpftrace）脚本时，正文里不要出现 `/*/`。** bpftrace 脚本用 C 风格注释，`/sys/block/*/device/serial` 这种 sysfs 通配写法里的 `*/` 会**提前终止注释**，把后半句当代码解析 → `ERROR: syntax error, unexpected ","`。这类失败的坏味道是"脚本跑了、报告照样打印一张整齐的阴性表"，所以执行侧必须有 `Attached <N> probes` 门禁（见 `workspace/wechat_linux_audit/guest/08_lookout_probe.sh`）。
11. **guest 脚本里用不带引号的 heredoc 生成 `.bt`，正文中的 `$name` 与反引号都是活的 shell 展开。** 2026-09-29 实测两次自伤：① 注释里给 `write` 加了反引号 → bash 真去执行 `` `write` ``，报告里冒出一行 `write: 未找到命令`；② bpftrace 的局部变量写成裸 `$fd`，在 `set -u` 下是未绑定变量 → **`cat` 整段中止，生成一个 0 字节的 `.bt`**，bpftrace 只回 `No probes to attach`。第 ② 次更贵的地方在**失败时机**：attach 门禁当时写在 `wait` 之后，于是采样器照样跑满 600s 才报 INVALID——整整 10 分钟在观测一个根本没在被观测的对象。纪律三条：bpftrace 局部变量写 `\$name`、`.bt` 正文（含注释）不出现裸 `$xxx` 与反引号、**开窗前先 `bpftrace --dry-run <script>` 预检**（编译+挂探针后立刻退出，秒级暴露这类问题），并把 attach 门禁放在 `wait` 之前、失败时 `kill` 掉后台采样器。参见 `workspace/wechat_linux_audit/guest/07_tls_lib_census.sh` 的 PRE-FLIGHT 段。

12. **从长时间挂起态恢复后，VNC 可能"认证全过、ServerInit 永不到达"。** 症状与坑 2 逐字相同（连上、DES 挑战应答 `security-result=0`、然后等不到帧），但成因完全不同：这台 VM 挂起约 14 小时后 `vmrun start` 恢复，guest 显示面已经死了（VMware 窗口里那片是**纯黑**），VNC 代理没有帧可描述，于是扣住 ServerInit。
    排查顺序（别反复重试同一个客户端，那只会重复超时）：① 用分段握手探针把 `greeting / sec-types / challenge / security-result / ServerInit` 各自打印出来，先确定**卡在第几步**——`vnc_ctl.py` 的报错栈看不出这个，现成工具是 `workspace/vm_vnc_ctl/vnc_handshake_probe.py <port> <password>`；② 确认不是 `nogui` 造成的（换 `start <vmx> gui` 再试一次）；③ 都不行就**冷启动**：`vmrun stop <vmx> hard` → `start nogui`，实测一次就恢复。
    ⚠ 代价要说清：`stop hard` 与冷启动都会清掉 guest 的 `tmpfs`（`/tmp` 里的 pcap、脚本副本全没）。所以**凡是产物落在 `/tmp` 的探针，跑完当场拉回宿主**，别指望下次开机还在——2026-09-30 就是这么丢了一份 8080 抓包，而它本来在挂起镜像里还活着。
13. **忘了 guest 口令时的自救路：`NOPASSWD sudo` + VNC 开终端跑 `passwd`，不需要旧口令。** 2026-09-30 实测（口令丢了，`ubuntu01` 与空口令都被 GuestOps 拒）。两个坑叠在一起：
    - **PAM 会拒绝"包含用户名"的口令**：给 `ubuntu` 设 `ubuntu01` 直接报 `无效的密码：密码包含用户名在某些地方`，然后要求重输——所以"随手用用户名+数字"这种好记的口令在这类机器上根本设不进去。
    - **别用 `echo 'user:pw' | sudo chpasswd` 这种一行式**：坑 1 的丢 Shift 会把 `:` 打成 `;`、`|` 打成 `\`（实测复现，落点变成 `echo ubuntu;ubuntu01 \ sudo -n chpasswd`），而 VNC 里你看不见自己打错了。改成**交互式 `sudo passwd <user>`**：提示不回显，只需小写字母+数字就过得去质量检查。
    顺序：`key Super` → `type "terminal"` → 点第一个结果 → 先 `shot` 看清落点再 `key Return`。
14. **`captureScreen` 不是"免凭据的观测手段"**：它和 `runScriptInGuest` 一样要求先 `LoginInGuest`。口令丢了的时候，屏幕上唯一还能用的通道就是 VNC（口令在 `.vmx` 里明文）。


15. **Vagrant 转换来的 `.vmx` 里的 `ethernet0.vnet = ""` 会让「改桥接」变成「打不开虚拟机」。** 2026-10-05 实测（Metasploitable3-win2k8 的 Vagrant 版）：把 `ethernet0.connectionType` 由 `nat` 改成 `bridged` 后，`vmrun start` 报 `Cannot open VM: ...，无法读取虚拟机的配置文件`。Kali 与 Metasploitable2 的 vmx 都没有这一行，故无此问题。
    对策：**删掉 `ethernet0.vnet` 行**再改 connectionType。判据：把原始 `.vmx.bak`（nat 版）换回去能正常启动 ⇒ 是**内容**问题不是环境问题——这条判据要先于任何环境排查。
16. **Metasploitable 系靶机在 guest 内是硬编码静态 IP `192.168.10.x/24`（作者环境的 NAT 网段），不是 DHCP。** 2026-10-05 实测：两台桥接到 `192.168.4.0/24` 后**完全不可达**，症状是「nmap 扫全网段也看不到靶机」，极易误判成「桥接没生效」。
    对策：进 guest 改地址——Linux 改 `/etc/network/interfaces` 的 `address`/`gateway` 后 `/etc/init.d/networking restart`；Windows 用 `netsh interface ipv4 set address name=<索引> source=static address=... mask=... gateway=...`，**用接口索引代替接口名**以绕开坑 1 打不了引号的问题。另：MS2 在该内核上要把网卡 `ethernet0.virtualDev` 定为 `e1000`（默认 vlance 会刷 `VLANCE: Ethernet0 skipped N time(s)`）。
17. **VNC 在 WinPE / 恢复环境（WinRE）里鼠标点击无效，键盘有效。** 2026-10-05 实测：win2k8 进恢复环境后 `click` 点任何按钮都没反应，改用 `key Tab` / `key Return` 能走完菜单；进了正常 Windows 桌面后鼠标恢复可用。
18. **`vmrun stop <vmx> hard` 连续两次会让 Windows 靶机进 WinRE。** 2026-10-05 实测：win2k8 因 `checkToolsState` 返回 `unknown` 被硬停两次，再开机直接落到「系统恢复选项」；**冷启动一次（`stop hard` → `start`）即恢复正常引导**，不必做引导修复。与坑 12 的「`stop hard` 清 tmpfs」叠加 ⇒ 产物别留 guest `/tmp`。
19. **Kali 的 `vmrun` GuestOps 也可能「只有查询类可用」。** 2026-10-05 实测（Kali / Debian 64 位，**已装 tools**）：`listProcessesInGuest` / `checkToolsState` / `getGuestIPAddress` 正常，而 `runProgramInGuest`（**带不带 `headless` / `interactive` 都一样**）/ `runScriptInGuest` / `copyFileFromHostToGuest` / `copyFileFromGuestToHost` **全部报 `未找到文件` / `文件名无效`**。故 §2 的「装了 tools 就能全套」要补一句：**先用一次查询类操作探活，再决定执行/传文件是否必须改走 SSH**。另 `-interactive` 还会因「交互登录用户（这里是 starry）与 `-gu` 身份不匹配」而失败。
    本轮实际用的通道：**host → Kali 走 SSH**（key 免密 + `NOPASSWD` sudo）；**看画面走 VNC**（两台靶机没装 tools，`captureScreen` 也报需 `LoginInGuest`，见坑 14）。
20. **宿主桥接网段的 IP 会漂，别把宿主自己的开放端口当成靶机在线。** 2026-10-05 实测：`skill vmware-bridged-networking-this-host` 记的以太网地址 `192.168.4.105` 已变成 **`192.168.4.104`**，而 **`.105` 变成了宿主 WLAN 网卡**的地址——从宿主扫到 `192.168.4.105:445 开放` 其实是宿主自己（Windows 本机 445），据此误判过「靶机已在网」。判据：先 `ipconfig` 认清宿主各网卡地址，再谈靶机。
## 6. 通用纪律

- **每步先读回画面/产物再下结论。** 命令返回 0 只说明事件发出去了，不说明界面变成了你要的样子。
  本轮装机有两次把"命令成功"当成"结果已确认"汇报，读图后都不成立——这条是这份文档存在的首要理由。
- **凭据不进仓库**：VM 口令写在会话里、不写进 `docs/`、`workspace/` 或提交历史；密钥类一律走 `lib/secret`。
- **Hyper-V 共存代价**：本机启用 Hyper-V 后 Workstation 16.2.3 走 WHP，guest 会变慢（`workspace/homework/subjects/mobile_dev/conventions.md` 已记，并有"作业结束后回退 Hyper-V"的 WIP）。判断当前是否共存激活看 `HypervisorPresent` 与 `hvservice`，不能只看可选功能 `State=Enabled`（见项目记忆 `reference_hyperv_launchtype_gotcha`）。

## 7. 维护规则

改这份方案时同步：
- **本方案是全项目入口，不是某组件私有**：`AGENTS.md` 文档导航表有一行指向本文件，新会话靠它发现这条路；
- `docs/environment-constraints.md` 的「系统 ISO / 大文件的镜像选源」是本方案的前置（先拿到可信 ISO 才谈装机），改选源结论时两边都要对得上；
- 工具行为变了 → 同步 [workspace/vm_vnc_ctl/README.md](../workspace/vm_vnc_ctl/README.md) 与 `vnc_ctl.py` 的 docstring；
- 新增可路由的触发词 → 改 [workspace/vm_vnc_ctl/guide_entries.py](../workspace/vm_vnc_ctl/guide_entries.py) 的 `keywords`，并按 [agent-guide-keywords.md](agent-guide-keywords.md) 的规则跑 `generate_index` / `check_skills` / `drift_detector`；
- 又踩到新坑 → 追加到 §5，**必须带实测日期与症状原文**，不写"据说/应该"。
