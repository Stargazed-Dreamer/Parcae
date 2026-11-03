# 固定库增量入库（默认操作指引）

> 触发：用户说「`<data_drive>:/<media_inbox>\xxx` 更新了，分别更新到对应文件夹」「按默认操作入库」。
> **这是两个互相独立的库，命名体系和来源形态都不同，绝不能混放。**
> 2026-10-24 已实际犯过一次错：把 G 库的 `new2` 放进了 D 库，事后搬回。

## 0. 先判库（第一步，别跳过）

| 库 | 现有命名 | 目录结构 | 来源形态 | 判据 |
|---|---|---|---|---|
| `<data_drive>:/<bilibili_videos>作品` | `角色X.Y.mp4`（版本序列，**无日期**） | 月份子目录 `2026.6月`…`2026.10月` | 平铺 `角色X.Y.7z`（双层加密） | 包名里**没有** `M-D` 日期 |
| `<data_drive>:/<media_staging>\new` | `M-D 角色 版本.mp4`（**日期在前**） | **平铺**（无月份子目录） | 按日期分的子目录：`*.zip` + 直出 mp4 + txt | 包/文件名里**有** `10-13` 这类日期 |

⛔ **不能靠角色名判库**：两边都出现若娜瓦、小葱等角色（D 库是同一角色的版本序列，G 库是按天收录）。
只认「命名形态 + 目录结构 + 来源包型」。

## 1. 两种来源形态与密码

| 来源形态 | 结构 | 密码 | 典型批次 |
|---|---|---|---|
| 平铺 `角色X.Y.7z` | 外层普通 7z → 内层 `.z7`（头加密）→ 1 个 mp4 | **`qwe123`** | `<data_drive>:/<media_inbox>\10月补1` |
| 日期子目录 | `*.zip`（ZipCrypto）→ 包内 `.exe`（**实为 mp4**）+ 说明 txt；同目录另有直出 mp4 | **`123`** | `<data_drive>:/<media_inbox>\new2` |

细节：

- 外层 7z 不加密，只有内层要密码；内层扩展名 `.z7` 少一个 7，文件头仍是标准 7z；
- zip 包内 `.exe` 的文件头是 `ftyp`，**改后缀为 `.mp4` 即可**，不要运行；
- 包内/包外的 txt 有提示：「解压密码123，exe后缀改mp4」。

## 2. 标准四步

```
plan（只读生成映射表） → 人工复核 target 列 → run（按表执行） → 验收 + 回收
```

### 2.1 D 库（平铺 7z，版本序列）

```bash
cd /f/<project_root>
./.venv/Scripts/python.exe .agents/skills/daily/media-file-organizer/scripts/ingest_media_batch.py \
  plan --source "<data_drive>:/<media_inbox>\10月补1" --target "<data_drive>:/<bilibili_videos>作品\2026.10月" \
  --style flat --out temp/plan_oct_supp.csv

BZ_7Z_PW=qwe123 \
./.venv/Scripts/python.exe .agents/skills/daily/media-file-organizer/scripts/ingest_media_batch.py \
  run --mapping temp/plan_oct_supp.csv --target "<data_drive>:/<bilibili_videos>作品\2026.10月"
```

- 目标名沿用包名：`女皇1.1.7z` → `女皇1.1.mp4`；
- 按素材日期选月份目录（这批是 10 月 → `2026.10月`）。

### 2.2 G 库（日期子目录，平铺入库）

```bash
cd /f/<project_root>
./.venv/Scripts/python.exe .agents/skills/daily/media-file-organizer/scripts/ingest_media_batch.py \
  plan --source "<data_drive>:/<media_inbox>\new2" --target "<data_drive>:/<media_staging>\new" \
  --style dated --out temp/plan_new2.csv

BZ_ZIP_PW=123 \
./.venv/Scripts/python.exe .agents/skills/daily/media-file-organizer/scripts/ingest_media_batch.py \
  run --mapping temp/plan_new2.csv --target "<data_drive>:/<media_staging>\new"
```

- 目标名：`M-D 角色 版本.mp4`，如 `10-13 小葱 原画.mp4`；
- G 库是平铺的，**不建月份子目录**。

### 2.3 复核（不可跳过）

打开映射表逐项看 `note` 列：

- `zip名=X 与包内=Y 不符` → **一律以包内 entry 名为准**。本批三处：
  - `若娜瓦10-16.zip` → 包内 `10-16瓦学妹差分.exe`
  - `10-17祀.zip` → 包内 `10-17小葱差分.exe`
  - `茜特拉莉差分.zip` → 包内 `10-28奶奶.exe`
- `角色名缺失` → 看同目录另一项能否继承（`原画.mp4` 继承 `耀嘉音`；`10-14平台` 继承 `若娜瓦`）；
- `目标已存在` / `重名` → 必须停下人工处理；
- 拿不准的保留原名（如 `10-16 炸死你 原画`），不要硬猜。

## 3. 验收

```bash
# D 库：清单 = 该月原有文件 + 本次新增，逐项比大小
./.venv/Scripts/python.exe .agents/skills/daily/media-file-organizer/scripts/verify_media_folder.py \
  "<data_drive>:/<bilibili_videos>作品/2026.10月" --csv temp/d_oct_final.csv --no-name-check

# G 库：核对本次新增的每个目标（存在/大小/头），再看总数 = 原数量 + 本次数量
```

必须看到 `全部通过`、`MP4 头 N/N`、`0 个子目录`。没有数字对比不算验收。

## 4. 回收源文件（默认全送回收站）

```bash
./.venv/Scripts/python.exe tools/disk/recycle.py --list temp/recycle_xxx.txt
```

顺序：

1. **D 库 7z 批次**：列出所有 `.7z` → 回收 → 确认批次内 0 个 7z；
2. **G 库 zip 批次**：先回收所有 `.zip` 和 `.txt` → 再回收空的日期子目录；
3. 回收 `<data_drive>:/<bilibili_videos>作品_extract_tmp`（staging，可能为空）；
4. 批次顶层目录按需保留（`new2` 留作下次投放点），一次性目录可整体回收。

⛔ 不用 `rm`/`del`/`shutil.rmtree`；只走 `tools/disk/recycle.py`。
⚠ D/G 盘回收站不会立即释放空间，需要用户自行清空。

## 5. 命名规则

### D 库

| 来源 | 目标 |
|---|---|
| `女皇1.1.7z` | `女皇1.1.mp4` |
| `若娜瓦7.5.7z` | `若娜瓦7.5.mp4` |

保持版本序列，不加日期，不改角色名。

### G 库

| 来源 | 目标 |
|---|---|
| 日期 + zip | `M-D 角色 差分.mp4`（zip 默认差分） |
| 日期 + 直出 mp4 | `M-D 角色 原画.mp4`（直出默认原画） |
| 直出但带版本词 | 保留版本词：`平台版`、`无黑条版` |
| 无角色线索 | 保留原名做角色位，如 `10-16 炸死你 原画.mp4` |

角色名先按包内/文件名原样；要核官方名另走
`references/naming-and-entity-verification.md`。

## 6. 实测坑

| 坑 | 事实 | 处置 |
|---|---|---|
| **判错库** | G 的日期批次被放进 D（2026-10-24） | 先看命名形态：有日期 → G，`角色X.Y` → D |
| 样本外推 | 首批 445 个里 443 双层 + 2 单层直出 | 先全量扫 `7z l` 条目再分派 |
| 内层 `.z7` | 不是笔误，是标准 7z | 按文件头判断，不按扩展名 |
| zip 名骗人 | 3 个包包名与内容角色不符 | 一律看 entry |
| zip 内 `.exe` | 文件头是 `ftyp`，就是 mp4 | 改名即可 |
| 密码噪音 | `10-18冰神密码123.zip` | 命名时剥掉 `密码123` |
| 短名/长名 | zip 名 `小葱` vs 包内 `葱` | 取更完整的一方 |
| 直出无角色 | `原画.mp4`、`10-14平台...` | 同目录继承唯一角色 |
| 日期后缀 | `_2026.10.13-19.49.22` | 命名时剥掉时间戳 |
| 跨卷移动 | D ↔ G 不同盘 | 先复制+校验，再删源；不能直接 rename |

## 7. 批次记录

| 日期 | 批次 | 结果 |
|---|---|---|
| 2026-10-24 | `<data_drive>:/<bilibili_videos>作品` 全量（445 个 7z） | 445 个 mp4，15.60 GB |
| 2026-10-24 | `<data_drive>:/<media_inbox>\10月补1`（35 个双层 7z）→ **D 库** | 35 个 mp4；`2026.10月` 42 → **77** |
| 2026-10-24 | `<data_drive>:/<media_inbox>\new2`（16 天）→ **G 库** | 33 个 mp4；G 142 → **175** |
| 2026-10-24 | 修正：new2 误入 D 后搬回 G | D 回到 77，G 到 175 |

## 8. 下次检查清单

- [ ] **先判库**：有日期前缀 → G；`角色X.Y` → D
- [ ] 来源路径、目标目录、月份（仅 D）已确认
- [ ] `plan` 行数 = 源里的包/视频数
- [ ] `note` 列逐条看过，zip 名不符的已按包内名
- [ ] `run` 全绿，无 `[BAD]`
- [ ] 验收：数量 = 原 + 新，字节对得上，头有效，无子目录/多余文件
- [ ] 源包与 txt 已回收，staging 已回收
- [ ] 更新本文件第 7 节批次记录
