# Agent Guide keywords 编写规范

> **目标**：让 `agent_guide(task="...")` 的关键词匹配准确，避免"太泛导致多匹配"或"太窄导致漏匹配"。
> 本文是 [server/agent_guide.py](../server/agent_guide.py) 的 `match_task_candidates` 算法的配套编写指南。
> 维护 GUIDE_REGISTRY 条目（[server/agent_guide_data.py](../server/agent_guide_data.py) 或 `workspace/<module>/loop_actions.py`）时必读。

## 1. 匹配算法回顾（编写 keywords 前必须理解）

`match_task_candidates` 用五路加权打分（详见 [agent_guide.py](../server/agent_guide.py) 的 `match_task_candidates` 函数注释）：

| 路 | 信号 | 分数 | 说明 |
|---|---|---|---|
| 1 | **kw_exact** | +10/词 | entry keyword 完整出现在 task 中（最强信号） |
| 2 | **bigram_overlap** | +2/重叠 bigram，上限 16 | task 和 entry 的 2-gram 集合交集 |
| 3 | **synonym** | +3/组基础 +2/组不同词 | 同义词组命中（见 `_SYNONYM_GROUPS`） |
| 4 | **kw_fuzzy** | +3/词 | keyword 的 2-gram ≥50% 在 task 中 |
| 5 | **domain_hints** | +bonus | context 含特定 domain 时加分（C 改进） |

**strong_match 判定**：`kw_exact` 命中 OR `kw_fuzzy ratio ≥ 80%`。只有 strong_match=True 才会返回完整 TaskGuide。

**弱匹配阈值**：top-1 score < 15 走 low_confidence 精简响应（只返候选清单不返 TaskGuide）。

### 关键含义

- **kw_exact 是最强信号**（+10），一个 keyword 命中就能让 strong_match=True。所以 **keywords 必须精确，不能泛**。
- **bigram_overlap 上限 16**，纯字符重叠最多 16 分，达不到 strong_match 阈值（除非配合 kw_exact）。
- **kw_fuzzy 50% 太松**（"怎么样"3字能命中"股票怎么样"），所以 strong_match 要求 80%。

## 2. keywords 编写五大原则

### 原则 1：精确优于泛 — 避免单字/双字 keyword

**❌ 反例**：
```python
"keywords": ["存档", "重构", "排查", "原型", "抽卡"]
```

**✅ 正例**：
```python
"keywords": ["存档网页", "重构模块", "排查报错", "原型探索", "明日方舟抽卡"]
```

**为什么**：
- 单字/双字 keyword 容易被 kw_exact 命中（+10）但语义不精确
- "存档"会命中"文章存档/留档/磁盘存档/记忆存档"等多种场景
- "抽卡"4 个游戏共享，每次都平局 15:15，用户必须手动选

**经验阈值**：keyword 长度 ≥ 3 字。2 字 keyword 只在**该词在该 skill 上下文唯一**时才用（如"A股"只对应股票，"记账"只对应账单）。

### 原则 2：entry/method/support 分层 — 触发词归 entry

`dev` 桶的 skill 分四种 role（见 `_DEV_ROLES`）：
- **entry**：任务入口，**用户直接触发**
- **method**：方法论参考，**被 entry 引用**，不该用户直接触发
- **support**：辅助审查/排障，**被 entry 引用**，不该用户直接触发
- **tool**：工具型 skill，按需触发

**❌ 反例**（dev.goal_engineering vs dev.leader，已发生）：
```python
# dev.goal_engineering (entry)
"keywords": ["帮我定目标", "定义目标", "让 agent 自己跑", "目标工程", "goal engineering"]

# dev.leader (method) — 5 个共享词！
"keywords": ["帮我定目标", "写个目标", "帮我拆目标", "让 agent 自己跑", "leader", "目标工程", "goal engineering", "定义目标", "Commander's Intent", "Harness"]
```

实测：`'帮我定目标'` → dev.leader (24) 反超 dev.goal_engineering (21)，违反 entry 优先。

**✅ 正例**：
```python
# dev.goal_engineering (entry) — 独占用户触发词
"keywords": ["帮我定目标", "定义目标", "让 agent 自己跑", "目标工程", "goal engineering", "做个功能", "重构"]

# dev.leader (method) — 只留方法论标识词，不抢用户触发词
"keywords": ["Commander's Intent", "Harness", "目标七问", "五种死法", "leader 心法", "目标任务书结构"]
```

**判定方法**：问自己"用户说这个词时，应该路由到 entry 还是 method？"——如果答案是 entry，method 就不该有这个词。

### 原则 3：避免跨 skill 共享 keyword — 共享 = 平局 = 不稳定

**❌ 反例**（已发生）：
- `dev.grill_me` (entry) 和 `dev.grilling` (support) 共享 `"找漏洞"` `"打磨计划"` → 平局 17:17
- `adhoc.office_pptx` 和 `adhoc.office_xlsx` 共享 `"CAC/LTV"` → 平局 23:23
- `recurring.memory_generation` 和 `system.doc_sync` 共享 `"更新记忆"` → 平局 19:19
- 4 个 gacha skill 共享 `"抽卡"` → 平局 15:15

**平局的后果**：top-1 由 GUIDE_REGISTRY 字典顺序决定（Python 3.7+ 保插入顺序），不稳定。用户说"找漏洞"时，grill_me 和 grilling 谁先注册谁赢——这不是设计意图。

**✅ 修复策略**：
1. **entry 独占触发词**：把共享词从 method/support 移除，只留 entry 有
2. **加 skill 特异性前缀**：如"找计划漏洞"（grill_me）vs"找代码漏洞"（code_review）
3. **用 description 区分**：共享 keyword 时，description 写清楚场景，让 bigram_overlap 帮助区分

**检查方法**：
```bash
uv run python -c "
from server.agent_guide import GUIDE_REGISTRY
from collections import defaultdict
kw_to_skills = defaultdict(set)
for tt, e in GUIDE_REGISTRY.items():
    for kw in e.get('keywords', []):
        kw_to_skills[kw].add(tt)
shared = {k: v for k, v in kw_to_skills.items() if len(v) >= 2}
for kw, tts in sorted(shared.items(), key=lambda x: -len(x[1])):
    print(f'{kw!r}: {sorted(tts)}')
"
```

### 原则 4：避免泛动词 keyword — "试一下/看一下/保存一下"是语气助词不是意图

**❌ 反例**（dev.prototype，已发生）：
```python
"keywords": ["原型", "prototype", "试一下", "看看效果", ...]
```

实测：`'试一下'` → dev.prototype (10) strong_match=True。但用户说"试一下保存文章"会被 prototype 抢，实际意图是 web_archive。

**✅ 正例**：
```python
"keywords": ["原型", "prototype", "prototype this", "let me play with it", "试几种方案", "做原型", "UI 变体"]
```

**判定方法**：问自己"用户说这个词时，是否一定是要这个 skill？"——如果"试一下"可能是"试一下保存/试一下发版/试一下任何事"，就不该是 keyword。

**语气助词黑名单**（已在 `_AUX_PARTICLES` 中剥离，但 kw_exact 仍用原文）：
- `一下 / 看看 / 试试 / 弄下 / 搞下 / 下吧 / 下来`
- `试一下 / 看一下 / 弄一下 / 搞一下`

这些词**永远不该单独作为 keyword**。如果要表达"试一下"的意图，用"试几种方案/做原型"等更具体的表达。

### 原则 5：覆盖用户口语动词 — 书面动词不够

**❌ 反例**（adhoc.web_archive，已发生）：
```python
"keywords": ["存档网页", "保存网页", "网页转MD", "网页归档", "提取网页内容"]
```

用户说"爬一下文章"完全命中不了——所有 keyword 都是"存档/保存/转MD/归档/提取"这类书面动词，缺"爬/抓取/扒"等口语动词。

**✅ 正例**：
```python
"keywords": [
    # 书面动词
    "存档网页", "保存网页", "网页转MD", "网页归档", "提取网页内容",
    # 口语动词（用户最常用）
    "爬文章", "爬网页", "爬取", "抓取网页", "批量爬",
]
```

**判定方法**：列出该 skill 所有动作的同义词，**包括口语/俚语/英文**。如"保存"的同义场：保存/存档/归档/下载/采集/爬/抓取/扒/scrape/crawl/archive。

**同义词组已覆盖的**（见 `_SYNONYM_GROUPS`）：
- 屏幕/画面/桌面/截屏/截图
- 识别/分析/解析/读取/看/ocr
- 点击/鼠标/按键/单击/双击
- 输入/打字/填写/键入
- 窗口/应用/程序/软件
- 操作/控制/操控/自动化
- 文字/文本/内容
- 爬/爬取/抓取/扒 ↔ 保存/存档/归档/下载/采集（B 改进新增）

**未被同义词组覆盖的**：编 keyword 时要手动覆盖所有同义表达，不能依赖同义词组。

## 3. 已知问题清单（2026-08-05 扫描结果）

### 高优先级（已修复 2026-08-05）

| # | 问题 | 修复前 | 修复后 | 修复方式 |
|---|---|---|---|---|
| 1 | `dev.leader` (method) 抶 entry 触发词 | `'帮我定目标'` → leader(24) > goal_engineering(21) | `'帮我定目标'` → goal_engineering(21) ✓ | leader 移除 5 个用户触发词，只留方法论标识词 |
| 2 | `dev.grilling` (support) 抶 entry 触发词 | `'找漏洞'` 平局 17:17 | `'找漏洞'` → grill_me(17) ✓ | grilling 移除"找漏洞/打磨计划/拷问/追问" |
| 3 | `dev.anti_hallucination` keywords 过泛 | `'重构'` 平局 15:15 with goal_engineering | `'重构'` → goal_engineering(15) ✓ | 移除纯动作词（改代码/重构/加功能/重命名/排查/定位），保留"修 bug"+场景限定词 |
| 4 | `dev.prototype` 的"试一下"过强 | `'试一下'` strong_match=True | `'试一下'` → NO MATCH ✓ | 改为"试几种方案/做原型" |

### 中优先级（可观察）

| # | 问题 | 实测 | 备注 |
|---|---|---|---|
| 5 | 4 个 gacha skill 共享"抽卡" | 平局 15:15 strong_match=True | 用户说"抽卡"应该走 low_confidence 让用户选游戏，而非 strong_match 第一个 |
| 6 | `adhoc.office_pptx` 和 `adhoc.office_xlsx` 共享"CAC/LTV" | 平局 23:23 | 用户说"CAC/LTV"应走 low_confidence |
| 7 | `recurring.memory_generation` 和 `system.doc_sync` 共享"更新记忆" | 平局 19:19 | 需区分"生成记忆"vs"整理文档" |
| 8 | `adhoc.headless_session` 误匹配"让 agent 自己跑" | score=29 strong_match=False | bigram_overlap 误命中，需 task 更明确 |

### 低优先级（弱匹配影响小）

| # | 问题 | 实测 | 备注 |
|---|---|---|---|
| 9 | `'看一下'` → daily_summary(5) | 弱匹配 5 分，不 strong_match | bigram 偶然重叠，影响小 |
| 10 | `'保存一下'` → gkd_signin_automation(5) | 弱匹配 5 分 | 同义词组误命中，影响小 |

## 3.5 复扫记录（2026-09-15，history.json 重放）

用 `data/agent_guide/history.json` 最近 50 条真实调用重放（脚本 `temp/guide_history_recheck.py`）：

**keywords 层 0 缺口**：
- 共享 keyword 检查 = 0 个（8-05 清单 #5/#6/#7 的共享词问题确认已修复）
- 历史失配案例自愈验证：`"把…整合为项目正式 skill"` → adhoc.bat_writing(44, strong) ✓（bat_writing 注册后覆盖）；`"帮我总结一下XX群最近的群聊"` → adhoc.chat_digest(33, strong) ✓（chat_digest 注册后覆盖）
- `"做大数据库作业"` weak 属设计内：homework workspace 的 manifest.toml 注明 agent_guide 注册暂缓

**剩余观察项（embedding 层，keywords 修不了）**：

| # | 现象 | 根因 | 为什么暂不修 |
|---|---|---|---|
| O1 | `"homework工作今天更新一轮砺儒云的情况"` → cross_workspace_advisor(18, strong=True) 误路由 | 语义补强双条件（score≥15 + cosine≥0.4）拉 strong。实测 cosine 0.547，top-5 全挤在 0.53-0.55（中文短文本 embedding 基线偏高） | 设计者正例 `"我今天都干了啥"`→daily_summary cosine 仅 0.517 **低于**误匹配值——cosine 绝对阈值无法区分两者，调阈值会连正例一起杀掉。实际伤害已被下游防御化解：cross_workspace_advisor 的 first_action 第一层即"本项目维护→不走本 skill"。待 homework 注册 task_type 后自然消失 |
| O2 | `"修复 tools/check_skills.py 校验器…"` → top1=headless_session(40, strong=False) | 多个 kw_fuzzy 50-67% 松匹配堆分（8 个 ×3） | strong=False 走 GeneralGuide，其 dev_entry_points 决策树（small_fix→anti_hallucination）兜底正确；anti_hallucination 语义 cosine 0.665 排第 3，方向信号存在 |

**结论**：keywords 编写层面无需变更。若未来 embedding 更换或 homework 注册，复跑 `temp/guide_history_recheck.py` 验证 O1 是否消失。

> 本节的 `temp/guide_history_recheck.py` 已随 10-02 的 temp 清理丢失（连同 `guide_semantic_debug.py`）；复扫请重写探针，做法与踩坑见 §3.6。

## 3.6 复扫记录（2026-10-07，history.json 最近 50 条 + 进程内归因）

样本口径：`records=50`（`mode` 计数 task 39 / general 11）。扣掉 2 条空参调用与 2 条本会话自放的反向守卫，剩 7 条逐条归因，脚本 `temp/20261006_guide_attrib3.py`（取 `matched` 明细，能看见分数是从哪一路来的）。

先记两条机件坑，比结论更容易复发：

- **重放会吃掉被审样本**。`GET /guide?task=` 每次调用都往那 50 格环形缓冲追加：本轮 8 次 HTTP 重放之后，10-05 15:26 那条图库句已被驱逐、原文不可复原。⇒ 归因走进程内 `server.agent_guide.match_task_candidates`（同一条代码路径、零写入）；确实要走 HTTP 就先 dump 一份 `history.json`。
- **样本不许手打**。第一版探针里我把一条曾被 `[:60]` 截断的原话**自己补了后半截**，而该中文字面量经工具往返又变成乱码 —— 有一行数字是拿"虚构 + 乱码"文本量出来的，整版作废重做。⇒ 句子一律从 `history.json` 读原文，并把"送进去的原文 + 字符数 + 是否含 U+FFFD"回显到输出里。

| # | 句子（截断） | 现 top1 | matched 来源 | 判定 |
|---|---|---|---|---|
| 1 | 检查 …`workspace\windows_forensics\data\rootcause_…md` 的根因调查报告 | `adhoc.headless_session`=37 | bigram 25 + 7 条 kw_fuzzy「* agent」50–67% | 误拉面，见 C2/C3 |
| 2 | 复核 Windows 高内存不回收的三份新增调查报告 | `dev.computer_use`=17 | bigram 7 + synonym 1 | 误拉，见 C2 |
| 3 | 制定 Windows 已退出进程对象引用未释放…根因测试路线 | `dev.wayfinder`=17 | bigram 7 + kw_fuzzy:wayfinder(50%) | **不是缺口**：wayfinder 正是这件事，只是分数不到 strong 阈值 |
| 4 | 清理一批零调用的 REST 端点，顺便把 data 里没人读的统计文件也删掉 | `dev.impeccable`=14 | bigram 7（`system.temp_cleanup` 未进 top4） | 见 C1 |
| 5 | 后端刚重启，复验三处 server 改动是否已加载生效… | `dev.cross_workspace_advisor`=9 | bigram 3 + kw_fuzzy:写后端(50%) | 元任务句，落 general 是正确行为 |
| 6 | 帮我把这个文件夹改名 | `adhoc.video_digest`=8 | bigram 4 | 反向守卫仍守得住（`cases.jsonl` 里 `expected_top1=null`） |
| 7 | 10-05 15:28 一条含 U+FFFD 的语句 | 9 | `semantic_only` | **样本已坏**（当时被 shell 按 GBK 重编码），不参与判定 |

三条缺口的处置：

- **C1「仓库死代码 / 无人读产物」没有归宿 —— 用户 2026-10-07 裁决：不建路由。** 决策当时的现状：`system.temp_cleanup` 整篇对象锁在 `temp/`（第一句就是「对 `temp/` 下每一项依次问 Q1/Q2/Q3」），而且**动作也不匹配**——仓库死代码要改的是 `server/` 源码、`check_hard_rules` 白名单与文档引用，不是"移进回收站"；`system.project_audit` 管发现不管摘除，第一步还要求你先选 12 大类方向。⇒ 这类活继续由人发起（10-06 那次就没走任何 skill）。**要复活这件事，先定动作边界，别从加 keywords 开始。**
- **C2 `workspace/windows_forensics` 未注册**：实测没有 `manifest.toml`，该线句子只能落 general 并被误拉。**未决，等点单。**
- **C3 kw_fuzzy 的英文通道噪声底**：最小实验 `temp/20261006_kwfuzzy_path_experiment.py` —— 一句不含任何关键词的纯英文句子给 `adhoc.headless_session` **26 分**；同句加一段含 `localAgent` 的绝对路径 → **42**（+16，其中 +6 来自 kw_fuzzy「判定 agent(50%)」，因为项目名里嵌着 `agent`）。缓解事实：这类候选一律 `strong_match=False`，走 GeneralGuide，只污染候选清单、不误派 TaskGuide（与 §3.5 的 O2 同族）。**未决，等点单。**

## 4. 编写 keywords 的标准流程

新增或修改 GUIDE_REGISTRY 条目时，按以下流程：

### 步骤 1：列出用户可能的说法

头脑风暴 5-10 种用户表达该意图的自然语言，**包括口语/俚语/英文**：
```
意图：把网页保存成本地 MD
用户可能说：
- 爬一下文章
- 爬取这些网页
- 保存网页
- 把这个链接存档
- 批量抓取
- 网页转 MD
- archive this page
```

### 步骤 2：筛选精确 keyword

从用户说法中筛选：
- ✅ 长度 ≥ 3 字（除非该词在该 skill 上下文唯一）
- ✅ 语义精确（"爬文章"而非"爬"）
- ✅ 不与其他 skill 共享（跑步骤 5 检查）
- ✅ 不是语气助词（"试一下"❌ → "试几种方案"✅）

### 步骤 3：检查 entry/method/support 分层

如果该 skill 是 method/support role：
- ❌ 不要放用户触发词（"帮我定目标"等）
- ✅ 只放方法论标识词（"Commander's Intent"等）

如果是 entry role：
- ✅ 独占用户触发词
- ✅ 检查 method/support skill 是否有重复，有则移除

### 步骤 4：跑共享检查脚本

```bash
uv run python -c "
from server.agent_guide import GUIDE_REGISTRY
from collections import defaultdict
kw_to_skills = defaultdict(set)
for tt, e in GUIDE_REGISTRY.items():
    for kw in e.get('keywords', []):
        kw_to_skills[kw].add(tt)
shared = {k: v for k, v in kw_to_skills.items() if len(v) >= 2}
for kw, tts in sorted(shared.items(), key=lambda x: -len(x[1])):
    print(f'{kw!r}: {sorted(tts)}')
print(f'共 {len(shared)} 个共享词')
"
```

新加的 keyword 不应该出现在共享清单里。如果共享了，调整 keyword 或与对方 skill 协调。

### 步骤 5：跑实际匹配测试

```bash
uv run python -c "
from server.agent_guide import match_task_candidates
for q in ['用户说法1', '用户说法2', ...]:
    r = match_task_candidates(q)
    if r:
        t = r[0]
        print(f'{q!r:30s} -> {t[\"task_type\"]:35s} score={t[\"score\"]:3d} strong={t[\"strong_match\"]}')
"
```

验证：
- top-1 是预期的 task_type
- score ≥ 15（避免弱匹配）
- top-1 与 top-2 分差 ≥ 5（避免平局）

### 步骤 6：如果是 web 类 skill，加 domain_hints

```python
"domain_hints": {
    "xiaoheihe.cn": 10,
    "mp.weixin.qq.com": 10,
}
```

这样 `agent_guide(task="爬一下文章", context="tabs.json")` 能通过 context 文件内的 URL domain 加分。

## 5. 维护清单

每次修改 GUIDE_REGISTRY 时检查：

- [ ] 新 keyword 长度 ≥ 3 字（或该词在 skill 上下文唯一）
- [ ] 新 keyword 不在语气助词黑名单（一下/看看/试试/弄下/搞下）
- [ ] 新 keyword 不与其他 skill 共享（跑共享检查脚本）
- [ ] 如果是 method/support role，没放用户触发词
- [ ] 如果是 entry role，独占用户触发词（检查 method/support 没有重复）
- [ ] 跑实际匹配测试：top-1 是预期、score ≥ 15、分差 ≥ 5
- [ ] 如果是 web 类 skill，加 domain_hints
- [ ] 更新 [CHANGELOG.md](../CHANGELOG.md) 记录变更

## 6. 相关文件

- [server/agent_guide.py](../server/agent_guide.py) — 匹配算法实现
- [server/agent_guide_data.py](../server/agent_guide_data.py) — 内置 GUIDE_REGISTRY
- `workspace/<module>/loop_actions.py` — 可选组件 GUIDE_REGISTRY_ENTRIES
- [tests/guide_loops/test_agent_guide_matching.py](../tests/guide_loops/test_agent_guide_matching.py) — 匹配改进测试
- [tests/guide_loops/test_agent_guide_recording.py](../tests/guide_loops/test_agent_guide_recording.py) — recording 路由测试
- [tests/guide_loops/test_agent_guide_memory_index.py](../tests/guide_loops/test_agent_guide_memory_index.py) — memory_index 测试
