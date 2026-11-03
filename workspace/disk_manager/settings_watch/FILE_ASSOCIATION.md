# Windows 文件关联：机制、陷阱与修复路径

> 2026-10-07~09 三天实测沉淀（用户报「所有文件默认打开方式都丢了」→「媒体全被抢成系统默认工具」）。
> 与 `settings_watch/items.py` 的 `FileAssocItem`、`scripts/assoc_*.py` 配套：那套脚本负责**备份/恢复**，
> 本文档负责**解释 Windows 到底怎么判定**，以及**哪些修复手段是无效的**（避免重复踩坑）。
>
> ★ **2026-10-12 重大补充（见第九节）**：查清了「图标已解析、双击仍弹选择框」的真因——两个独立的注册表条件；
> 并给出两条修复路与**影响面核算**。此前「经典层写法/自动接管」相关的若干说法在本节被更正或作废。

---

## 一、四层判定链与「自动接管」（核心机制）

Windows 决定「双击用什么打开」时，按优先级从上往下找，**但第 1 层为空时不是往下走，而是自动补位**：

| 层 | 位置 | 说明 |
|---|---|---|
| 1（最高） | `HKCU\...\Explorer\FileExts\<ext>\UserChoice` | 带防篡改 Hash；**Hash 有效才生效，无效则弹「你要如何打开这个文件」** |
| 2 | `HKCU\Software\Classes\<ext>`（用户级经典层） | 不需要 Hash，但**只要第 1 层有有效值就被盖住** |
| 3 | `HKLM\Software\Classes\<ext>`（系统级经典层） | 音视频类在本机多数为空 |
| 4 | 按类别推荐：视频→电影和电视、音乐→媒体播放器、图片→照片 | **第 1 层空了，Windows 会把这层的 AppX 写进第 1 层** |

**★ 铁律 1：UserChoice 一旦缺失，Windows 会自动写入一个带全新有效 Hash 的 AppX UserChoice。**
- 实测：删掉 `.mp4` 的 UserChoice 后 **+15 秒**它自己回来了，`ProgId` = 电影和电视的 AppX。
- Hash 每次都不同且都有效：`1OisV6hK9jI=` → `nzvF5pXP+rs=` → `3P7q5Df6QT8=`。
- 反证：`.wav` 的 UserChoice 是 `pps_wav`（爱奇艺，有效 Hash）→ **从未被抢**。
- ⇒ 判据：**第 1 层有有效值 = 稳；为空 = 被系统用 AppX 接管。**

**★ 铁律 2：Hash 无效 ≠ 回退到经典层，而是弹窗。**
- `.docx` 实证：HKCR 下层本来就是 `Word.Document.12`、exe 也在，但因 UserChoice Hash 不被认可，双击照样弹选择框。
- ⇒ 修复必须**删除** UserChoice，光设经典层没用；但删了又会被系统用 AppX 补位（铁律 1）——这就是死结。

**★ 铁律 3：Hash 由 Windows 私有算法生成（用户 SID + ProgID + 键写入时间等），第三方算不出来。**
- 备份里的旧 `(ProgID, Hash)` 原样写回 → **2 秒内被覆盖**（Win10 19045 = build 19045 UBR 7548）。
- 官方 COM `IApplicationAssociationRegistration::SetAppAsDefault` → **CLSID `{591209c7-767b-42b2-9f20-ef6d75d6997a}` 在本机未注册**（0x80040154，HKCR\CLSID 里根本没有），走不通。
- ⇒ **唯一出路：让 Windows 自己写一次有效 Hash，或让它没得可写。**

---

## 二、已证伪的路（别再试）

| 手段 | 结果 |
|---|---|
| 只删 UserChoice，靠经典层接管 | ❌ +15s 被自动重写为 AppX |
| 删 `HKCU\Software\Classes\<ext>\OpenWithProgids` 里的 AppX | ❌ 无效（接管不来自这里） |
| 清空 `FileExts\<ext>\OpenWithList` 的 AppX 推荐位 + 重排 MRUList | ❌ 仍被重写 |
| `SHChangeNotify(SHCNE_ASSOCCHANGED)` 强制刷新 | ❌ 无效，不是缓存问题 |
| 官方 COM `SetAppAsDefault` | ❌ 类未注册（0x80040154） |
| 备份里的 (ProgID, Hash) 原样写回 | ❌ 2s 内被改写 |
| `reg import` 导入含 Hash 的 .reg | ❌ Hash 必被判无效（同铁律 3） |
| 爱奇艺 `GeePlayer.exe` 命令行关联参数 | ❌ 二进制里没有任何 assoc/default 类开关 |
| 「经典层 ProgID 写成 `Applications\<exe>` 形式 Shell 不认」 | ❌ **结论作废（10-12 隔离实验）**：`.zzapp`（HKCU-only = `Applications\notepad.exe`）双击**直接打开记事本**。10-07 那次修复失败的真因不是写法，而是它同时制造了第九节的触发条件 ① 和 ② |
| `FindExecutableW` / `AssocQueryString(ASSOCF=0)` 判「会不会弹窗」 | ❌ **不可用（10-12）**：二者都拿经典层兜底，对 `.py/.md/.json` 一律报「会直接打开」，与实际相反。唯一可靠 = **真双击 + 数 `OpenWith.exe` 进程** |

---

## 三、可行修复路径（三选一）

| | 做法 | 后果 | 可逆性 | 耗时 |
|---|---|---|---|---|
| **A 卸载系统播放器** | 移除「电影和电视」`*ZuneVideo*` +「媒体播放器」`*ZuneMusic*` AppX | 系统无推荐可写 → 音视频自动落回经典层（爱奇艺）；代价是两个系统应用消失 | 可从 Store / PowerShell 重装 | ~1 分钟 |
| **B 屏幕自动化点选** | 每个后缀弹「打开方式」→ 选目标程序 → 勾「始终」 | Windows 自己写有效 Hash，最正统；但要弹十几次窗占屏 | 可逐个改回 | 3–5 分钟 |
| **C 让播放器自己关联** ✅ **已验证有效（2026-10-11）** | 爱奇艺 → 设置 → 文件关联 → 全选 → 确定 | 官方途径，一次覆盖其 Capabilities 里声明的全部后缀；**写出的 UserChoice 是 `pps_*` + 全新有效 Hash（30 个后缀同批写入，实测 20s 内零回归）** | 同 B | ~1 分钟 |

⚠ **C 的坑**：`%APPDATA%\IQIYI Video\GeePlayer\FileAssoc.ini` 里 128 个后缀都记着 `=1`（爱奇艺自认为已关联）。
若点了「全选→确定」没反应，**先备份并把该 ini 的值改成 `0`（或删掉文件）**，再让爱奇艺重新关联。

---

## 四、本机基线（用户原本的分配，09-26 与 10-07 修复前两次快照完全一致、零变动）

- **33 个音视频后缀 → `pps_*` = 爱奇艺万能联播**（`<data_drive>:\<apps_root_x86>\iqiyi video\geeplayer\geeplayer.exe`）
- **8 个图片后缀 → 照片 AppX**（`.jpg/.jpeg/.png/.bmp/.webp/.heic` = `AppX4mntx…`；`.gif/.tif` = `AppX43hnxt…`）
- `.tiff` → Photoshop、`.svg` → Chrome、RAW（`.dng/.cr2` 等）→ Photoshop
- 播放器现状：**PotPlayer 已卸载**（`<data_drive>:/<apps_root>\PotPlayer\` 不存在，`HKLM\SOFTWARE\Clients\Media\PotPlayerMini64` 是残留，会误导）；VLC 在（`<data_drive>:\<apps_root_x86>\VLC\vlc.exe`）
- 爱奇艺在 `HKLM\SOFTWARE\Clients\Media\GeePlayer\Capabilities\FileAssociations` 声明了 128 条，但**没进 `SOFTWARE\RegisteredApplications`** ⇒ 系统「设置→默认应用」列表里看不到爱奇艺，这是当初只能选到「电影和电视」的原因之一

---

## 五、备份与脚本

| 用途 | 位置 |
|---|---|
| 修复前全量（10-07 23:44） | `<data_drive>:/<backup_root>\SystemBackup\file_associations\prerepair_20261007_234416\`（FileExts.reg + HKCU_Classes.reg） |
| phase4 备份（10-09 04:42） | `temp/assoc_loss_probe_20261007/backup_p4_20261009_044207/FileExts.reg` |
| 09-26 基线快照 | `<data_drive>:/<backup_root>\SystemBackup\file_associations\20260926_050437\inventory.json` |
| 探针/修复脚本 | `temp/assoc_loss_probe_20261007/`（probe8 前后对比、probe10 `AssocQueryString` 权威查询、probe13/14 机制排查、probe15 UserChoice 回归、probe18 Hash 复写试验、repair_phase3/phase4） |
| **复验脚本（推荐复用）** | `probe19_verify_final.py`（全量 786 后缀 vs 09-26 基线比对）、`probe20_stability.py`（图像按友好名比对 + UserChoice 归属层 + 20s 稳定复测）、`probe21_summary.py`（写入时间 + 还剩多少会弹窗 + 常用后缀抽查）、`probe22_leftovers.py`（残余异常逐条定性） |

回滚：`reg import "<备份>\FileExts.reg"`（Hash 会被忽略，但经典层可完整还原）。

---

## 六、工具坑（本机专属）

1. ⛔ **删 UserChoice 的两个坑**（2026-10-11 在 `.db` 上修订，推翻此前说法）：
   - 不能用 `reg delete`（返回「拒绝访问」）；
   - ⚠ **也不能用 `winreg` + `KEY_ALL_ACCESS` 打开**——Windows 给自己写的 UserChoice 加了一条 `Deny <user> SetValue`，而 `KEY_ALL_ACCESS` 的权限集**含 `SET_VALUE`** ⇒ **`OpenKey` 直接被拒（WinError 5）**，根本轮不到删。
   - ✅ **正解**：该 DENY **只挡「写值」，不挡「删除」**（FullControl 里 DELETE 仍在）。以 `KEY_READ` 判存在，再用 `winreg.DeleteKey(HKEY_CURRENT_USER, 完整路径)` 直接删即可（`DeleteKey` 只需 DELETE 权限）。
   - 实测掩码证据：`KEY_ALL_ACCESS` 打开**失败**；`KEY_READ`、`READ|DELETE`、`DELETE` 单独 均打开**成功**（`temp/assoc_loss_probe_20261007/probe26_mask.py`）。
   - 附注：旧版说「winreg KEY_ALL_ACCESS 能删」当时之所以成立，是因为那 101 个键是我方/旧流程写的、**没有那条 DENY**；而 Adobe 写的 `.pdf`、Windows「打开方式」写的键**有** DENY，所以当时删不掉。正解对两类键都有效。
2. ⛔ **reg.exe 输出是 GBK**：`subprocess.run(..., text=True)` 会 UnicodeDecodeError 并吞掉 stdout，误把成功判成失败。必须 `capture_output=True` + `.decode('gbk','replace')`。
3. ⚠ **判定「实际生效」要用 `shlwapi.AssocQueryStringW`**（`ASSOCSTR_FRIENDLYAPPNAME`=4 / `ASSOCSTR_EXECUTABLE`=2），`assoc` 命令和 winreg 读到的层可能不一致；AppX 查不到 exe 是正常现象（返回 0x80070483），要看友好名。
4. 注册表键名**不分大小写**，用 dict 比对会把 `.M2V` / `.m2v` 误报成丢失。
5. ⛔ **判「会不会弹窗」只看友好名是否等于 `选取应用`，不能用 exe 是否为空** —— AppX 应用（照片等）的 `ASSOCSTR_EXECUTABLE` 恒为空，用 exe 判会把「有默认应用」误报成「会弹选取应用」（实测 `.jpg/.png` 全部误报，实际 friendly=照片）。正确写法：`is_prompt = friendly.startswith('选取应用') or exe.endswith('OpenWith.exe')`。
6. 比对脚本别用「基线 exe 文件名」当唯一期望值：基线里 13 个后缀的 exe 被记录成 `program`（未加引号路径解析失败），会被误判成「程序被改掉」。**优先比友好名，exe 只作参考。**
7. ⛔ **UCPD（User Choice Protection Driver，Windows 11 24H2 / 2024-03 补丁起）只放行微软签名进程写 `UserChoice`**：
   非 MS 签名的脚本/工具直接写 UC 会被拦或写完即被还原 ⇒ 想自写 UC 要么确认 UCPD 未启用，要么走路 ①（由系统自己的
   `OpenWith.exe` 写），否则脚本会"静默失效"。⛔ 与之无关的是：UCPD 不改变「Hash 算不出来」（见 §一 铁律 3）。
8. ⛔ **官方 API 自 Win8 起只剩只读查询**：`AssocQueryStringW` 能读当前默认，但**没有任何官方 API 能写/判定"会不会弹"** ⇒
   唯一可靠判据 = **真双击 + 数 `OpenWith.exe`**（见 §9.1）。

---

## 七、2026-10-11 复验结果（用户执行方案 C 后）

**结论：目标达成，30 个音视频后缀全部稳在爱奇艺万能联播，未被系统抢回。**

| 项目 | 结果 |
|---|---|
| 音视频 30 个后缀 | 全部 `UserChoice = pps_<ext>` + 全新有效 Hash，实际生效 = **爱奇艺万能联播** |
| 写入时间 | **2026-10-10 20:48:04** 同批写入（`.wav` 例外，2026-10-09 01:51 用户单独设的） |
| 20 秒稳定性复测 | **0 个变化**（对比 10-09 时 +15s 就被抢回 15 个） |
| 指向非 `pps_*` 的 UserChoice | **0 个** |
| 图像 8 个后缀 | 全部 = 照片 AppX；`.tiff` → Photoshop；`.svg` → Chrome（与基线一致） |
| 常用办公 | `.docx/.doc`→Word、`.xlsx/.xls`→Excel、`.pptx/.ppt`→PowerPoint、`.pdf`→Adobe Acrobat DC、`.txt`→记事本、`.zip/.rar/.7z`→7-Zip、`.psd`→Photoshop —— 全部正常，与基线一致 |
| 全量 786 后缀 | 532 有明确默认程序；254 仍会弹「选取应用」，其中 **237 在基线就是无关联**（属正常，从未设过） |

**残余 17 个「基线有、现在反而会弹」——全部是目标程序没了，不是关联问题：**

| 类别 | 后缀 | 目标 | 现状 |
|---|---|---|---|
| 迅雷 XMP（10 个） | `.csf .dvd .rpm .rt .smi .smil .sup .usf .xlmv`（+`.psb`已落到 PS） | `<data_drive>:\<apps_root_x86>\Thunder\Program\XMP\Program\` | 迅雷 XMP 已不存在 |
| TeX Live（2 个） | `.dvi .sty`（`.cls` 已回落 VSCode） | `<data_drive>:/<apps_root>\texlive\2025\bin\windows\` | **texlive 整个目录已不在** |
| Steam 盘缺失（1 个） | `.spl` | `<data_drive>:/<game_library_root>\steamapps\common\Soundpad\` | **D 盘当前不存在** |
| 软件被清理（3 个） | `.drawio`→`<data_drive>:/<apps_root>\drawio-…`（已清空）、`.ggb`（GeoGebra）、`.RMP`（RAMMap 在 Downloads 已挪走） | — | 软件不在原位 |
| IE 失效（1 个） | `.xsl` → `iexplore.exe` | — | IE 本身不可用，可忽略 |

**另外两个小不一致（非故障，供参考）：**
- `.htm` → Microsoft Edge（`UserChoice=MSEdgeHTM`），而 `.html` → Google Chrome（`UserChoice=ChromeHTML`）。两者被分开指代，Edge 自己写入的 UserChoice（依然有效）。想要统一到 Chrome，可删 `HKCU\...\FileExts\.htm\UserChoice`（现��第 1 层有有效值，删除后大概率回落 `HKCU\Software\Classes\.htm = ChromeHTML`，但 Edge 有重抢历史，需复验）。
- `.md` → Sublime Text 而 `.markdown` → calibre（基线也是这样分的，保持原样）。

---

## 八、2026-10-11 往返测试：改造后的 `assoc_restore.py` 验证

目的：检验「verify 紧跟 apply 会造成假成功」这个问题是否真被修掉。方法 = 往返测试（先故意改坏，再用脚本恢复，再独立复核）。

以 `.db` 为例（非类别竞争、目标程序存在、极少被双击）：

| 阶段 | 生效程序（`AssocQueryString` 官方判定） | HKCU 经典层 `\Software\Classes\.db` | UserChoice |
|---|---|---|---|
| 原始 | `dbfile` | （无） | （无） |
| 改坏（脚本外手工写 `txtfile`） | `txtfile`（记事本） | `txtfile` | （无） |
| `assoc_restore.py --ext .db --apply --yes` 后 | **`dbfile`** ✅ | `dbfile` | （无） |
| +25 秒独立复测 | **`dbfile`** ✅ | `dbfile` | （无） |

**结论**：脚本正确识别差异 → 归类「可自动修复」→ 恢复到位；且延时复核证实未被抢回。**假成功问题已解决**。

残留（预期终态，无需处理）：`HKCU\Software\Classes\.db` 现在由无变显式 `= dbfile`，这是哈希无关两步法的正常产物（把用户级 pin 写实）；删掉该键生效仍是 `dbfile`。

### 八-2、第二轮：补测「删 UserChoice」半程（并修掉一个真 bug）

第一轮 `.db` 本没有 `UserChoice`，只走了「写经典层」半程。随后用户手动把 `.db` 默认程序设成**记事本**，Windows 因此写入了一个**带有效 Hash 的 UserChoice**（`Applications\notepad.exe`），正好补上这半程。

**第一次重跑 → 失败（有价值）**：
```
[v] .db: 经典层已设；删 UserChoice 失败: [WinError 5] 拒绝访问。
[!] 未达目标 1 项: .db            ← 复核诚实报失败，未制造假成功
```
根因（`Get-Acl` 实测）：系统写的 UserChoice 带一条 `Deny <dev_machine>\admin SetValue`，而旧实现用 `KEY_ALL_ACCESS` 打开（权限集含 `SET_VALUE`）⇒ 打开即被拒。

**修正**：`delete_userchoice_key` 改为 `KEY_READ` 判存在 + 直接 `winreg.DeleteKey`（只依赖 DELETE 权限，DENY 不拦）。

**第二次重跑 → 通过**：脚本报 `删 UserChoice 成功` + 复核 OK；独立探针（立即 & +25 秒）均确认 `.db` 生效 = `dbfile`、UserChoice 已消失、稳定未被抢回。

**结论**：两半程（写经典层 / 删受保护 UserChoice）均已实测通过；并且"失败时如实报错"这一行为也在第一轮失败里得到了验证。

测试脚本：`temp/assoc_loss_probe_20261007/rt_db.py`（`--break` 改坏 / 无参 dump）；掩码探测 `probe26_mask.py`。

---

## 九、2026-10-12：双击弹「你要如何打开这个文件?」的真因与影响面

**背景**：10-07 修复后，`.py`/`.toml`/`.json` 等**图标已正确解析**（`.toml`→记事本、`.py`→Python 解释器），
`AssocQueryString` 也报「有默认程序」，但**双击照样弹选择框**。本节是这一现象的根因结论。

### 9.1 真因：两个独立条件（单变量对照实验）

方法：**真双击（`os.startfile`）+ 数 `OpenWith.exe` 进程增量**；白鼠用 `.ps1` / `.py`。

| # | 触发条件 | 含义 |
|---|---|---|
| **①** | `HKCU\Software\Classes\<ext>` 与 `HKLM\Software\Classes\<ext>` **都存在且值不同** | 用户级与系统级**声明冲突**，Shell 要求用户裁决 |
| **②** | `HKCU\...\Explorer\FileExts\<ext>\OpenWithProgids` 含**指向另一个不同 exe** 的 ProgID | 该类型**对应到多于一个不同程序**，存在真实歧义 |

**两条任一成立就弹；两条都不成立（哪怕 ProgID 写法很怪）都不弹。与 ProgID 的写法、与文件类型都无关。**

**⚠ 两个条件都还要求「指向的程序真的可用」**（10-12 复验补充）：

- **条件 ① 追加要求：HKLM 侧那个 ProgID 必须能解析出可用的 `shell\open\command`。**
  若 HKLM 侧只是"值不同、却根本没有 open 命令"，**冲突实际不成立 ⇒ 不弹**。
  反例：`.json`（HKCU=`Applications\sublime_text.exe`，HKLM=`Adobe.AfterEffects.json.17`，**无 open 命令**）⇒ **实测不弹**。
- 条件 ② 同理：OWP 里的异项必须是**能解析出 exe 的项**，只有名字不同但无 open 命令的不算（见下反例校验）。

⇒ 判"会不会弹"时，**光看「HKCU≠HKLM」会误报**；必须再确认 HKLM 侧有效。本机 687 个后缀里就有 **8 个**这类假阳性。

关键实验（完整记录见 `temp/assoc_probe_20261012/`）：

- **实验 1（只动 HKCU 一个值，白鼠 `.ps1`）**：SAME（HKCU==HKLM）→ 直接打开；HKLM-only（删 HKCU 值）→ 直接打开；**DIFF（HKCU≠HKLM）→ 弹窗**。
- **实验 2（均先消除 ①，白鼠 `.py`）**：OWP=`{Python.File}` → 不弹；OWP=`{Python.File, Applications\python.exe}`（后者指向 Miniconda 的另一个 exe）→ **弹窗**。
- **反例校验**：`.ini` 的 OWP 含异项 `INI`（但该项**没有 open 命令**）→ 不弹；`.ps1` 的 OWP 含 `Applications\notepad.exe`（与该类型默认同为记事本）→ 不弹。⇒ **条件 ② 要"真的指向另一个可用的 exe"才算**，只有名字不同但无 open 命令的不算。

**★ 第 1 层仍然是最高优先级**：一旦存在**有效 UserChoice**，就直接命中，条件 ①② 都不再触发。
旁证：`.jpg/.png/.gif/.htm/.html` 等 **10 个后缀同时有 UC + 经典层 DIFF**，用户从未报告它们弹窗。

### 9.2 两条修复路

| | 做法 | 优点 | 代价 |
|---|---|---|---|
| **① 官方路（推荐）** | 双击该类型文件 → 选目标应用 → 勾「始终使用此应用打开 .xxx」→ 确定 | Windows 自写有效 UserChoice；**立即生效**；**不动经典层、不动 OWP**；只影响这一个后缀 | 要逐个弹窗点选，无法批量 |
| **② 注册表批量路** | 删 `HKCU\Software\Classes\<ext>` 默认值（消除 ①、回落 HKLM 标准关联）+ 把 `OpenWithProgids` 清成「单一项且与默认同 exe」（消除 ②） | 可脚本化、无需逐个弹窗 | **会改变生效程序**（见 9.3）；减少右键「打开方式」候选 |

**首次触发路径决定弹窗长什么样**：双击 → `OpenWith.exe` 浮层**带**「始终使用…」勾选框（默认已勾）；
右键「打开方式」/ `OpenAs_RunDLL` → **不带**。⇒ 想一步到位就别用右键路径。（旧说「Win11 取消勾选框」作废。）

**已实测**：`.toml` 10-12 15:38 走路 ① 恢复（`ProgId=notepad.exe`），再双击不再弹；
`.py` 在「无 ① + OWP 单一项」组合下走路 ②，实测不弹（但程序从 Miniconda 变成系统 `py.exe`）。

### 9.3 影响面核算（同口径：`FileExts` 下 687 个后缀）

> 脚本 `temp/assoc_probe_20261012/impact_scan.py`（**只读注册表**），原始数据 `impact_scan.json`。

| 分组 | 数量 | 脚本是否涉及 |
|---|---|---|
| 已有 UserChoice（第 1 层胜出，不会弹） | **147** | ❌ 不碰 |
| 无 UC 且**根本没有常规程序**（陌生素材，**本来就该弹**） | **341** | ❌ 不碰（弹窗是正常行为） |
| **命中 ①② 且「目标程序真的可用」的异常弹窗** | **25** | ✅ 候选对象 |
| ├─ 其中「有常规 exe 可回落」（含 4 个已修） | 14 | 可修（走路 ①） |
| ├─ 其中「HKCU 目标 exe 已不存在」（`.psb` 迅雷 XMP） | 1 | 只能换程序 |
| └─ 其中「基类是 AppX / 解析不出常规 exe」 | 10 | ⚠ 弹窗里可能要手工挑程序 |

> 注：「147」是 10-12 16:02 快照值。此后走路 ① 修好 4 个（+4），
> 另有 4 个 CameraRaw 类型（`.erf/.nef/.nrw/.rwl`）被「照片」AppX 自注册 UC（+4），
> 复测时实测已达 **156**。⇒ **UC 数量会自己漂移**（App 会自注册），不是静止值。

> **口径两次修正**：
> ① 旧口径「383 会弹」把上面 341 个**本来就无关联**的也算进去了（它们弹窗是正常的，不是故障）；
> ② 初版「33 个」仍**偏宽** —— 它只看「HKCU≠HKLM」，没要求「HKLM 侧能解析出 open 命令」。
> **按 10-12 复验修正后，真实异常弹窗 = 25 个**，剔除了 **8 个假阳性**：
> `.crtx / .ico / .jpe / .json / .mht / .mhtml / .pdb / .svg`（HKCU≠HKLM 但 HKLM 侧无 open 命令 ⇒ 实测本就不弹）。

**⚠ 路 ② 的真实副作用：治疗条件 ① 只能「二选一」，删 HKCU 值会让 13 个后缀的生效程序改变：**

| 后缀 | 现在（HKCU 指定） | 删 HKCU 后回落（HKLM） | 评价 |
|---|---|---|---|
| `.py` | Miniconda `python.exe` | 系统 `py.exe` | 变了 |
| `.zip` | 7-Zip | 资源管理器 | 变了 |
| `.url` | Chrome | `rundll32.exe` | 变差 |
| `.xml` | Chrome | msoxmled（IE 内核） | 变差 |
| `.sh` | 记事本 | git-bash | 变了 |
| `.tiff` | Photoshop | `rundll32.exe`（老式照片查看器） | 变差 |
| `.pub` | 记事本 | MS Publisher | 变了 |
| `.svg` | Chrome | 无可用程序（`svgfile` 解析不出） | 变差 |
| `.psb` | 迅雷 XMP（已不存在） | Photoshop | 算修好 |
| `.crtx / .mht / .mhtml / .pdb` | 各自程序 | 无可用程序 | 变差 |
| `.psd` | Photoshop | Photoshop | 不变 |

（同时命中 ①② 的后缀已并入上表。）

> ⚠ 上表是**仅路 ② 假设下**的风险；且其中 `.svg / .crtx / .mht / .mhtml / .pdb` **属 8 个假阳性**（实测本就不弹）
> —— 也就是说，即便走 ②，这 5 个后缀**根本不需要动**。用户在 10-12 已裁决**不用路 ②**，此表保留仅作机理记录。

**条件 ② 的副作用（重要澄清）**：「右键『打开方式』候选变少」**只来自"清 `OpenWithProgids`"这一步**，
**不是走 ② 就一定触发**：

- 只命中**条件 ①**（HKCU/HKLM 冲突）的后缀，治它**只需删 `HKCU\Classes\<ext>` 默认值，完全不碰 OWP** ⇒ **候选不变**。
- 只有命中**条件 ②**（OWP 含指向另一 exe 的异项）的后缀，才需要清 OWP ⇒ 才会掉候选。

本次 **25 个**真实异常弹窗里：**只命中 ① 的 3 个**（`.psd / .sh / .tiff`，走 ② 也无候选副作用）；
**需要动 OWP 的 22 个**（② only 15 + ①②同时 7）。
⇒ 若脚本"图省事对所有后缀一律清 OWP"，副作用会被无谓放大；**正确的实现只对命中 ② 的后缀清**。

### 9.4 结论

- **路 ① 影响面 ≈ 0**（只写 1 个 UserChoice 键，不动其他任何东西）—— 常用后缀首选。
- **路 ② 影响面是真实的**：会让 13 个后缀的默认程序被改（含 `.py/.zip/.url/.xml` 等高频），其中若干是会**变差**的。
  仅当「当前 HKCU 值本身就是误配 / 目标程序已不存在」时才适合用路 ②。
- 两条路**混合**最稳：对 `.py/.zip/.url/.xml` 这类要保程序的用 ①；对 `.psb` 这类「目标已没了」的用 ②。

**★ 用户裁决（2026-10-12）**：**不用路 ②，一律走路 ①**（避免任何"生效程序被改"的风险）。
⇒ 修复动作 = 对「会弹的异常后缀」逐个执行：双击该类型文件 → 选**现状生效的那个程序** → 勾「始终使用…」→ 确定。
目标程序取「现状生效 exe」（即 `HKCU` 经典层当前指定的），保证修复前后**用的是同一个程序**，只是不再弹窗。

### 9.5 本次一并更正/作废的旧结论

1. ⛔ 「`Applications\<exe>` 形式不被 Shell 认」→ **作废**（隔离实验 `.zzapp` 双击直接打开记事本）。
2. ⛔ 「10-07 修复失败是因为把 `Applications\<exe>` 搬进经典层」→ 真因是**同时制造了 ① 和 ②**。
3. ⛔ 「Python 安装器能自动、脚本不能」→ 安装器写的是 **HKLM 标准关联（`Python.File`）**，只要不踩 ①② 就能直接打开；
   我方脚本之所以失败，是因为在 HKCU 另写了不同值并往 OWP 塞了异项。

### 9.6 10-12 试跑记录（5 个高频后缀，走路 ①）

用户裁决「不用路 ②」后，先挑 5 个高频后缀试跑（`.py .json .zip .xml .url`）：

| 后缀 | 结果 | UserChoice.IsProgId | 生效 exe |
|---|---|---|---|
| `.py` | ✅ 修好 | `python.exe` | Miniconda `python.exe`（未被换） |
| `.json` | ⚪ **本就不弹**（8 个假阳性之一） | 无（未动） | Sublime Text |
| `.zip` | ✅ 修好 | `7zFM.exe` | 7-Zip |
| `.xml` | ✅ 修好 | `chrome.exe` | Chrome |
| `.url` | ✅ 修好 | `chrome.exe` | Chrome |

- 方法：`os.startfile` 真双击样例文件 → `screen_accessibility_snapshot` 读 `OpenWith.exe` 浮层 UIA 树 →
  `screen_semantic_action(select/invoke)` 选**现状那个程序**并确定 → 回读 `UserChoice` 校验 → 再双击复验不弹。
- 全部**走在路 ①**（只写 `UserChoice`），生效程序与修复前一致，经典层与 OWP **均未改动**。
- ⚠ 操作坑：`OpenWith.exe` 浮层**不是独立顶层窗口**，会被抢前台的 App（Chrome/Sublime）挡住且不出现在常规窗口枚举里；
  须先把前台还给普通窗口再触发，浮层成为前台后才能按其 hwnd 读 UIA。
  另：`invoke` 确定按钮会**直接关窗**，若选中项不对就白跑一次（`.zip` 第一次即如此，重做才成功）。

**试跑后剩余**：25 个真实异常中 4 个已修、4 个（CameraRaw）被 AppX 自注册 UC 变为不弹 ⇒ **实际待修 17 个**，
其中「有常规 exe 目标」的 10 个可走路 ① 修（`.aifc/.docx/.godot/.odt/.psd/.pub/.rtf/.sh/.snd/.tiff`），
`.psb` 目标（迅雷 XMP）已不存在需换程序，其余 6 个（`.ec3/.obj/.oga/.srw/.tex/.tif`）解析不出常规 exe。

### 9.7 收尾与留档（2026-10-14）

**★ 用户最终裁决：不再批量修，遇到弹窗自己点。**
理由与代价都清楚了（路①影响面≈0、路②会改程序），剩下 17 个异常的边际收益不值得再抢一次屏幕焦点；
且**走路 ① 的弹窗本身就带「始终使用此应用打开 .xxx」且默认已勾选**——
用户自己点一次，等价于脚本替他做的那一步，**一次点完永久生效**，还完全可控。

**留档（下次想批量再做时直接照着跑）**：

| 资产 | 位置 | 作用 |
|---|---|---|
| 走①清单（25 个 + 状态列） | `temp/assoc_probe_20261012/走①清单.md` | 谁待修、目标程序是哪个 |
| 修正版判据脚本（只读） | `temp/assoc_probe_20261012/refined_scan.py` → `refined_scan.json` | **重跑即可刷新清单**（UC 会漂移，旧清单会过期） |
| 初版核算脚本 | `temp/assoc_probe_20261012/impact_scan.py` → `impact_scan.json` | 687 后缀全量原始数据 |
| 修复前备份（含 248 个原始 UserChoice + Hash） | `<data_drive>:/<backup_root>\SystemBackup\file_associations\prerepair_20261007_234416\FileExts.reg` | 兜底回滚 |

**⚠ 用户自己点时的三个注意点**（这几条是脚本踩过的坑，手动同样适用）：

1. **必须走「双击」路径**才有「始终使用」勾选框；**右键「打开方式」没有**（9.2）。
2. 目标程序不在列表里时（`.ec3/.obj/.oga/.srw/.tex/.tif` 这类解析不出常规 exe 的），
   要展开「更多应用 ↓」，甚至「在这台电脑上查找应用」手动指定 exe。
3. **清单会过期**：App 会自注册 UC（本次就抓到「照片」给 CameraRaw 四个后缀写 UC），
   所以"待修数"不是固定值；想知道当下还剩几个，**重跑 `refined_scan.py`** 而不是翻旧清单。

**遗留未做（待用户发话）**：
`workspace/disk_manager/scripts/assoc_common.py` 的 docstring 里「恢复对文档类后缀有效」的说法**已过时**
（按 9.1，恢复是否生效取决于有没有踩 ①②，与"是否文档类"无关），**本次未改代码**。

**本轮未提交 git**（只动了 `workspace/disk_manager/settings_watch/FILE_ASSOCIATION.md` 一个文件）。

原始证据：`temp/assoc_probe_20261012/`（`VERIFY_FINDINGS.md`、`single_var_exp.txt`、`py_second_factor.txt`、`owp_rule_test.txt`、`final_factor.txt`、`full_scan.json`、`impact_scan.json`、`refined_scan.json`、`refined_scan.py`、`check_uc.py`、`verify_no_prompt.py`、`走①清单.md`）。


