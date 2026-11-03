"""硬化规则检查（"说过三遍的规则"→"会变红的构建"）。

六个检查项（C1–C5 详见 temp/sdd/agent-trust-guardrails/spec.md 2.1；C6 见
wip_fe6da6fa 的死链口径实测 temp/20261007_dead_ref_scan.py）：
  C1 BOM：tracked 的 .py/.md/.toml/.json 文件头不得为 UTF-8 BOM（EF BB BF）
  C2 硬编码密钥：源码中 api_key/token/secret/password 等字面量赋值即违规
  C3 文档路径越界：tracked .html / 根目录 .md 必须在白名单内
  C4 import 边界：client/** 不许 import server/**；server/** 不许 import client/**（白名单除外）
  C5 MCP 工具名：文档里写的 mcp__localagent__X 必须在第一层直连工具表内（反例语境豁免）
  C6 死链：在用文档面引用的仓库内路径必须解析得到（历史面/通配占位/上级目录写法豁免，
     存量走 DEAD_DOC_PATH_BASELINE 名单，名单里的条目修好必须删行——全量跑会反查陈旧项）

用法：
  uv run python tools/check_hard_rules.py            # 全量检查（CI/测试语义）
  uv run python tools/check_hard_rules.py --staged   # 只查 staged 文件（pre-commit 语义）
  uv run python tools/check_hard_rules.py install    # git config core.hooksPath scripts/hooks
  uv run python tools/check_hard_rules.py uninstall  # 恢复默认 hooksPath

设计约束：stdlib-only，不引入新依赖；文件清单以 git ls-files 为准（天然排除
gitignored 的 temp/ 等）；白名单变更必须显式改本文件并 commit（这就是"硬化"）。
"""
from __future__ import annotations

import ast
import fnmatch
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 文件头 3 字节为 UTF-8 BOM
BOM = b"\xef\xbb\xbf"
# C1 检查的扩展名
BOM_EXTENSIONS = {".py", ".md", ".toml", ".json"}

# C2 白名单（相对路径，支持 fnmatch 通配）：这些文件合法接触密钥形态文本
SECRET_ALLOW_FILES = [
    "lib/secret/*",
    "server/llm_pool/key_store.py",
    "tools/check_hard_rules.py",  # 本文件自带检测模式字符串
    "tests/*",
    "docs/*",
    "workspace/*",
    "planning_notes/*",
    "展示文档/*",
]
# C2 占位值（不视为泄漏）
SECRET_PLACEHOLDER_SUBSTRINGS = [
    "xxx", "your", "placeholder", "example", "dummy", "changeme",
    "todo", "<", ">", "test", "sample", "redacted",
]
# C2 检测的密钥形字段名（用于 .py 行内赋值/字典键；需词边界防 "backups/secrets" 这类路径误报）
SECRET_KEY_PATTERN = r"(?:api[_-]?key|api[_-]?secret|token|secret|password|passwd|access[_-]?key)"
# 赋值形态：key = "字面量"（≥8 字符才算，避免空串/短占位误报）；(?<![\w]) 防字段名误中字符串内容
RE_SECRET_ASSIGN = re.compile(
    r"(?<![\w])" + SECRET_KEY_PATTERN + r"\s*[\"']?\s*=\s*[\"']([^\"']{8,})[\"']", re.IGNORECASE
)
# 字典形态："key": "字面量"
RE_SECRET_DICT = re.compile(
    "[\"']" + SECRET_KEY_PATTERN + r"[\"']\s*:\s*[\"']([^\"']{8,})[\"']", re.IGNORECASE
)
# config.example.toml：字段名（锚定在 = 前）含密钥形字段 且 值为 8+ 字符字面量 → 违规
RE_TOML_SECRET = re.compile(
    r"^\s*[\w*]*" + SECRET_KEY_PATTERN + r"[\w*]*\s*=\s*[\"']([^\"']{8,})[\"']", re.IGNORECASE
)

# C3：tracked .html 白名单
# ⚠ 2026-09-23 补：这里原写"temp/html/ 因 gitignore 天然不入清单"，前提是 `temp/` 整体被忽略。
#   .gitignore 改成 `temp/*` + `!temp/html/` 等四条显式豁免后（见其注释：豁免=有意纳入版本
#   跟踪），temp 下的 html 开始进入 tracked 清单，前提失效 ⇒ 按名补进白名单。
HTML_ALLOW = [
    "tools/release/templates/*",
    "tests/*",       # 测试夹具（browser fixtures 等）
    "workspace/*",   # workspace 组件自带资源（gacha/accounting/dashboard 等）
    "展示文档/*",
    "temp/html/*",
    "temp/planning_archive/*",
]
# C3：根目录 tracked .md 白名单（新增顶级 .md 必须显式改这里并 commit）
ROOT_MD_ALLOW = [
    "AGENTS.md",
    "CHANGELOG.md",
    "NOTICE.md",
    "README.md",
    "README_public.md",
    "SECURITY-RISKS.md",
    "USER_ONLY_PROJECT_OPERATIONS_AGENT_DO_NOT_READ_OR_EDIT.md",
]

# C4：import 边界。双向各有白名单例外（现存合法跨层复用，新增例外必须显式改这里并 commit）
# T0（butler-runtime）后循环已迁 lib/agent_runtime：server 不再 import client，白名单清零。
IMPORT_ALLOW_SERVER_TO_CLIENT: list[str] = []
IMPORT_ALLOW_CLIENT_TO_SERVER: list[str] = []

# C5：MCP 工具名引用真实性。
# 分层事实（真源 server/core/mcp_gateway.py setup_mcp）：FastApiMCP 只以
# `include_operations=DIRECT_TOOLS` 挂载，因此 **只有 DIRECT_TOOLS 那 55 个**会以
# `mcp__localagent__X` 形式存在；其余 117 个网关层工具只能走
# `parcae_advanced_tool(tool="X")`，写成 `mcp__localagent__X` 调会 not found。
RE_MCP_REF = re.compile(r"mcp__localagent__([a-z0-9_]+)")
# ⛔ 反例语境必须豁免，否则立刻误报：AGENTS.md:190 与 CHANGELOG.md:67 是**刻意写的
# 教学反例**（原文含"按 mcp__localagent__wip_create 调会直接 not found"）。同行出现
# 这些否定/禁令词即判定为反例而非推荐用法。
MCP_REF_NEGATIVE_HINTS = (
    "not found", "不存在", "不可用", "不生效", "不要", "禁止", "别用",
    "不是", "无法", "已删除", "已移除", "失效", "错误", "反例",
)
# 扫描扩展名（比 C1 的 BOM 面宽，因为引用写在 .md/.txt 里也要抓到）
MCP_REF_SCAN_EXTS = {".py", ".md", ".toml", ".json", ".txt", ".yaml", ".yml"}
# 自带模式字符串 / 测试夹具：这些文件不指导 agent 怎么调工具，误报代价大于收益
MCP_REF_ALLOW_FILES = [
    "tools/check_hard_rules.py",      # 本文件自带 RE_MCP_REF 模式字符串
    "tools/audit/drift_detector.py",
    "tests/*",
    # 带日期的工具体检记录：正文是"某天某探针**实际**调用了哪个工具名"的原始轨迹，
    # 改它就是改证据（与下面 EVIDENCE_RAW_SURFACES 同一逻辑）。2026-11-03 工具改名
    # parcae_* → parcae_* 后，历史报告里的旧名**必然**不在当前直连表里。
    "workspace/tool_dojo/reports/*",
]

# C6：在用文档面的"仓库内路径引用"必须解析得到（死链闸门）。
# 口径来自 2026-10-07 的三基准实测（temp/20261007_dead_ref_scan.py）：
#   全 tracked .md 共 956 个、路径引用 3590 处，其中 1532 处在**仓库根**解析不到。
#   但 1378 处落在 CHANGELOG/归档/WIP/日志/planning_notes 这类"历史与流水账"面上——
#   它们提到已删除的路径是**正当职责**（记的是"曾经有过"），拦它们等于逼文档说谎。
#   真正该管的在用文档面只有 154 处，其中还要再豁免两类：
#     - 通配/占位（`models/*`、`<file>`、`_template.md`、"示例"…）：任何基准都判不了；
#     - 按 md 自身目录能解析的（skill/ADR 里写相对路径是合法风格）：40 处。
#   ⇒ 闸门 = 仓库根为基准 + 只看在用面 + 上述两类豁免 + 存量走基线名单。
DOC_REF_TOPS = (r"docs|server|client|lib|tools|workspace|release|scripts|tests|planning_notes|"
                r"\.agents|展示文档")
DOC_REF_EXTS = (r"py|md|toml|json|txt|bat|ps1|html|pyi|cfg|yaml|yml|csv|xml|ini|sh")
RE_DOC_PATH_REF = re.compile(
    r"(?<![\w/.\-])((?:" + DOC_REF_TOPS + r")/[\w一-鿿\-.<>*…]+?\.(?:" + DOC_REF_EXTS + r"))(?![\w])"
)
# 通配/占位：路径里带这些片段就不是"某一条具体路径"，无法判死活
DOC_REF_PLACEHOLDER = re.compile(
    r"[*<>]|\.\.\.|…|XXX|xxx|\{|path/to|example|示例|模板|_template", re.IGNORECASE
)
# 历史/流水账面：合法提及已删除路径，不进闸门（清单与 temp/20261007_dead_ref_scan.py 同一份）
DOC_REF_HISTORY_SURFACES = re.compile(
    r"^(?:CHANGELOG\.md|SECURITY-RISKS\.md|docs/changelog-archive\.md|temp/|"
    r"\.agents/wip/|\.workbuddy/|planning_notes/|workspace/[^/]+/(?:records|logs?)/)"
)
# 第三方/上游存档：整棵不扫（2026-10-08 用户认可）。
#   workspace/dev_toolkit/ 是**上游 toolkit 蓝图的存档**（不是本仓代码，里面的 `scripts/`、
#   `tests/` 指的是蓝图用户自己项目里的东西）；workspace/maa-patch/ 是**外部项目（MAA）的
#   踩坑记录**，提的是 maa 仓库的路径。这两棵按仓库根判必然误报，且里面没有本仓风险内容。
DOC_REF_THIRD_PARTY_SURFACES = re.compile(r"^workspace/(?:dev_toolkit|maa-patch)/")
# 证据原样抓取层：整棵不扫（C5 + C6 共用，2026-11-03 补）。
#   `workspace/local_butler/40_盘点与证据/20_事务盘点/20_分源产出/` 是 66 事务盘点的
#   **原始抓取件**：卡片正文是从用户其它项目/工具（WorkBuddy、codex rollout、Obsidian…）
#   的会话里**逐字**导出的，里面的 `tools/check_arch.py`、`tests/conftest.py`、`mcp__localagent__X`
#   都是**被调研现场**的路径与调用名，不是本仓的（本仓对应物不同名或根本不存在）。
#   改它们是改证据，删它们等于毁掉结论的可核对性 ⇒ 与 temp/、dev_toolkit/ 同理，整棵不扫。
#   ⚠️ 只豁免「分源产出」这一层；同一盘点下的 `00_调查设计.md`、`15_跨轮次不变量`、`30_汇总/`
#   都是本仓自己的文档，照旧受闸门管。实测豁免前 C6 4923 条、C5 701 条全落在这棵树里。
EVIDENCE_RAW_SURFACES = re.compile(
    r"^workspace/local_butler/40_盘点与证据/20_事务盘点/20_分源产出/"
)
# 沿革语境（行级）：这一行在说"这个文件曾经叫/曾经在 X，后来删了/改了/搬了"。
#   写"原 X 已删"是文档的正当职责，判它死链等于逼文档说谎（与豁免 CHANGELOG 同理）。
#   ⛔ 只收**搬迁/删除类强动词**：`迁移到`/`迁移至` 在本仓语料里多指**数据**迁移
#   （密钥 config→secrets、格式 v8→v9），收进来会误伤一堆正常句子。
#   ⛔ `移到` 必须写 `(?<!迁)移到`：否则"逐级迁移到当前版本"里的 **移到** 子串会命中，
#   等于把"迁移到"又放回来了（2026-10-08 实测踩过）。
DOC_REF_HISTORY_LINE = re.compile(
    r"已删|已移除|已废弃|已下线|已迁|已改名|已重命名|已改写|已重写|"
    r"移入|移出|(?<!迁)移到|"
    r"重命名为|改名为|改写成|重写成|"
    r"重构前|迁移前|改版前|拆分前|重写前|改名后|拆分后|"
    r"已于.{0,24}(?:删除|移除|废弃)|不再(?:存在|使用|维护|需要)|"
    # 一次性迁移脚本：跑完就删是常态，文档留它是为了说明"版本怎么演进过来的"。
    # 这三种措辞是实测三条（`tools/migrate_secrets.py`「已完成使命并删除」、
    # `tools/migrate_keys_v8_to_v9.py`「历史迁移中有一步需手动跑」、
    # `tools/migrate_fact_type_v6_1.py`「执行迁移的」）的实际形状，不是拍脑袋加的。
    r"已完成使命|历史迁移|迁移期|执行迁移|"
    # "原 `xxx.py`"：反引号紧跟在"原"后面，几乎只可能是"原来的某某"；
    # 裸 `原` 会把"原因…"读进来，所以必须带反引号锚。
    r"原\s*`"
)
# 占位语境（行级）：这一行是示例/模板/示意，路径名本身就是占位的（`x.md`、`yyy.py`…）。
#   ⛔ 不收裸 `{`/`}`：本仓大量行含 `{'status': ...}` 这类字典字面量，收进来会误伤。
DOC_REF_EXAMPLE_LINE = re.compile(
    r"示例|例如|比如|举例|样例|示意|如[:：]\s*[`\"]|模板|占位|placeholder|"
    r"\b(?:x|xxx|yyy|foo|bar)\.(?:py|md|json)\b|path/to|"
    r"\{(?:如|例|示例|例如|比如|占位|placeholder)",
    re.IGNORECASE,
)
# 跨子项目相对路径白名单（2026-10-08 用户认可）：仓库根的文档引用**子项目内部**路径时，
#   作者按子项目根写（`disk_manager: ["scripts/scan_disk.py", ...]`），这些文件真实存在于
#   `workspace/disk_manager/scripts/`。⚠️ 只放**逐个认可**过的子项目，不要写成
#   `workspace/*` 全量——那样 `tests/conftest.py` 会被 `workspace/ue_learning_copilot/tests/`
#   里那份同名文件救下来，而作者真正指的是根 `conftest.py`，读者照样扑空。
DOC_REF_SUBPROJECT_ROOTS = ("workspace/disk_manager",)
# 本文件自带上面那些模式字符串，扫自己必然命中
DOC_REF_ALLOW_FILES = ["tools/check_hard_rules.py"]

# C6 存量基线（(文件, 引用) 成对；2026-10-07 由 temp/20261007_c6_baseline_gen.py
# 走闸门同一条代码路径扫出来，不是手抄）。
# ⚠️ 这份名单只用于"挡新增"，每一条都还是**待修的死链**，不是"已豁免的正确写法"：
#    修好一条就删一行（全量跑会反查陈旧条目）。加新条目要写清为什么不能改路径。
# 2026-10-08 已清零：49 条存量里 18 条属三类豁免（第三方存档整棵 / 沿革语境 / 占位语境）、4 条属一次性迁移脚本的沿革记录、3 条属跨子项目相对路径（disk_manager 白名单），剩下 24 条全部按「新路径 + 符号锚点」修好了（详见 CHANGELOG 2026-10-08 那条）。
# ⚠️ 清零不是放行：闸门照旧扫在用文档面，**新增**死链一律判红。名单空了只说明存量修完，任何新写的坏引用都会立刻被挡下。
# 2026-10-14 新增 12 条：`workspace/local_butler/` 是**第三方仓库调研存档**，报告正文里引用的
# 是被研究对象（OpenClaw / Hermes / super-agent-party 等上游仓库）的**真实**文件路径
# （`server/outbox.py` / `server/loop_manager.py` / `server/activity_signal.py` / `tools/memory_tool.py`
# / `.agents/config.json`），以及上游↔Parcae 对比表里对上游路径的复述——它们不是本仓库路径
# （本仓库对应物是 `server/activity_tracker/loop_manager.py` 等）。改写成仓库内路径会破坏研究结论
# 的可核对性，故入基线：**不是「已豁免的正确写法」**，这些报告若重做/删除，同名条目必须同删。
# 2026-10-15 新增 6 条：`30_架构与设计/90_历史/05_调研产出/` 派单回执（R*/U*）同理——正文引用的
# `docs/tool-execution-pipeline.md` / `docs/agent-lifecycle.md`（DSH 上游）、
# `docs/permission-project-approval.md` / `docs/conversation-session-case-catalog.md`（ZCode 上游）、
# `docs/chronos-managed-cron-contract.md`（Hermes 上游）、`docs/module-graph.md` 均为**被调研上游
# 仓库**的文档路径（本仓库没有这些文件）。同样：不是「已豁免的正确写法」，报告删除则同名条目同删。
DEAD_DOC_PATH_BASELINE: set[tuple[str, str]] = {
    ('workspace/local_butler/10_调研/10_早期调研/planning.md', 'server/loop_manager.py'),
    ('workspace/local_butler/10_调研/10_早期调研/survey.md', 'server/loop_manager.py'),
    ('workspace/local_butler/10_调研/20_六家比对/00_横向汇总/0_总报告_四维横向对比与改造建议.md', 'server/outbox.py'),
    ('workspace/local_butler/10_调研/20_六家比对/00_横向汇总/1_交付链横向对比.md', 'server/activity_signal.py'),
    ('workspace/local_butler/10_调研/20_六家比对/00_横向汇总/1_交付链横向对比.md', 'server/outbox.py'),
    ('workspace/local_butler/10_调研/20_六家比对/00_横向汇总/2_唤醒横向对比.md', 'server/loop_manager.py'),
    ('workspace/local_butler/10_调研/20_六家比对/00_横向汇总/3_记忆横向对比.md', 'tools/memory_tool.py'),
    ('workspace/local_butler/10_调研/20_六家比对/00_横向汇总/4_数据整合横向对比.md', 'tools/memory_tool.py'),
    ('workspace/local_butler/10_调研/20_六家比对/02_Hermes比对/13_hermes_memory.md', 'tools/memory_tool.py'),
    ('workspace/local_butler/10_调研/20_六家比对/02_Hermes比对/Hermes比对报告.md', 'tools/memory_tool.py'),
    ('workspace/local_butler/10_调研/50_主循环/主循环调研/03_super-agent-party_主循环调研.md', '.agents/config.json'),
    ('workspace/local_butler/10_调研/50_主循环/主循环调研/05_Hermes_主循环调研.md', 'tools/memory_tool.py'),
    ('workspace/local_butler/30_架构与设计/90_历史/05_调研产出/R4_ZCode相位机异常路径.md', 'docs/conversation-session-case-catalog.md'),
    ('workspace/local_butler/30_架构与设计/90_历史/05_调研产出/R5_ZCode授权跨重启恢复.md', 'docs/permission-project-approval.md'),
    ('workspace/local_butler/30_架构与设计/90_历史/05_调研产出/U1_DSH工具管线与取消恢复三篇.md', 'docs/agent-lifecycle.md'),
    ('workspace/local_butler/30_架构与设计/90_历史/05_调研产出/U1_DSH工具管线与取消恢复三篇.md', 'docs/tool-execution-pipeline.md'),
    ('workspace/local_butler/30_架构与设计/90_历史/05_调研产出/U10_Hermes_cron六层看护复验.md', 'docs/chronos-managed-cron-contract.md'),
    ('workspace/local_butler/30_架构与设计/90_历史/05_调研产出/U17_Host服务层与CLI自检.md', 'docs/module-graph.md'),
    # 2026-10-23 新增 2 条：DSH 浏览器插件源码勘察报告引用的是被调研上游
    # `wqty123/dsh-builtin-browser` 的 `docs/why-browser.md` / `docs/limitations.md`
    # （本仓库没有这些文件；改写会破坏研究结论的可核对性）。报告删除则同名条目同删。
    ('workspace/agent_browser/01_调研_已有项目/2026-10-23_dsh浏览器插件实现勘察.md', 'docs/why-browser.md'),
    ('workspace/agent_browser/01_调研_已有项目/2026-10-23_dsh浏览器插件实现勘察.md', 'docs/limitations.md'),
    # 2026-10-31 新增 4 条：面板设计跑到 v3.6 这一轮，两类引用点的是本仓库没有的文件。
    # ① `21`（已归档到 _panel_archive/）通篇按「外部 skill 规范文档」给几何样式依据
    #   （同段还有 ui-primitives / ui-trajectory 的 README.zh.md），本仓库没有这几个文件；
    #   改写会把「哪条数字出自哪份规范」的依据抹掉，别忘了它是追溯用文档。
    # ② `27`/`28` 里的 `docs/db-field-notes.md` 是 28 号台账 A3 排好队还没开工的产物，
    #   正文自己就写着「待建」——它不是写错路径，是提前把落点钉在这儿。
    #   ⇒ A3 一旦落地该条即为陈旧项，全量跑会反查到，届时连同本条注释一起删。
    ('workspace/local_butler/30_架构与设计/_panel_archive/21_面板v2信息架构与DSH寄居评估_20261029.md', 'docs/ui-radius.zh.md'),
    ('workspace/local_butler/30_架构与设计/_panel_archive/21_面板v2信息架构与DSH寄居评估_20261029.md', 'docs/web-styling.zh.md'),
    ('workspace/local_butler/30_架构与设计/20_现行设计/27_面板现行设计_v3.6_20261030.md', 'docs/db-field-notes.md'),
    ('workspace/local_butler/30_架构与设计/20_现行设计/28_数据与记忆_面板视图与召回整改_20261030.md', 'docs/db-field-notes.md'),
}


def _git(args: list[str]) -> str:
    r = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, check=True
    )
    return r.stdout.decode("utf-8", errors="replace")


def _tracked_files() -> list[str]:
    out = _git(["ls-files", "-z"])
    return [f for f in out.split("\0") if f]


def _staged_files() -> list[str]:
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"])
    return [f for f in out.split("\0") if f]


def _fnmatch_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def _is_secret_placeholder(value: str) -> bool:
    low = value.lower()
    return any(s in low for s in SECRET_PLACEHOLDER_SUBSTRINGS)


# ---------- C1 BOM ----------

def check_bom(files: list[str]) -> list[str]:
    violations = []
    for f in files:
        if Path(f).suffix.lower() not in BOM_EXTENSIONS:
            continue
        p = PROJECT_ROOT / f
        if not p.is_file():
            continue
        try:
            with open(p, "rb") as fh:
                if fh.read(3) == BOM:
                    violations.append(f"C1 BOM: {f}")
        except OSError:
            continue
    return violations


# ---------- C2 硬编码密钥 ----------

def check_secrets(files: list[str]) -> list[str]:
    violations = []
    for f in files:
        if _fnmatch_any(f, SECRET_ALLOW_FILES):
            continue
        p = PROJECT_ROOT / f
        if not p.is_file():
            continue
        suffix = Path(f).suffix.lower()
        try:
            if suffix == ".py":
                text = p.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    for m in (*RE_SECRET_ASSIGN.finditer(line), *RE_SECRET_DICT.finditer(line)):
                        if not _is_secret_placeholder(m.group(1)):
                            violations.append(f"C2 硬编码密钥: {f}:{lineno}（字段形如 {SECRET_KEY_PATTERN}）")
                            break
            elif f == "config.example.toml":
                text = p.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    m = RE_TOML_SECRET.match(line)
                    if m and not _is_secret_placeholder(m.group(1)):
                        violations.append(f"C2 硬编码密钥: {f}:{lineno}（token/key 字段不得有真实值）")
                        break
        except OSError:
            continue
    return violations


# ---------- C3 文档路径越界 ----------

def check_path_boundary(files: list[str]) -> list[str]:
    violations = []
    for f in files:
        low = f.lower()
        if low.endswith(".html") and not _fnmatch_any(f, HTML_ALLOW):
            violations.append(f"C3 路径越界: {f}（.html 展示文件只允许白名单位置）")
        elif "/" not in f and low.endswith(".md") and f not in ROOT_MD_ALLOW:
            violations.append(f"C3 路径越界: {f}（根目录新增 .md 必须显式加入 ROOT_MD_ALLOW）")
    return violations


# ---------- C4 import 边界 ----------

def _iter_import_modules(tree: ast.AST) -> list[str]:
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.append(node.module)
            # 相对 import（level>0）不跨包，忽略
    return modules


def check_import_boundary(files: list[str]) -> list[str]:
    violations = []
    py_files = [f for f in files if f.startswith(("client/", "server/")) and f.endswith(".py")]
    for f in py_files:
        p = PROJECT_ROOT / f
        if not p.is_file():
            continue
        try:
            source = p.read_bytes().decode("utf-8-sig", errors="replace")
            tree = ast.parse(source)
        except SyntaxError:
            continue  # C1/C2 已覆盖内容问题，这里只管边界
        in_client = f.startswith("client/")
        for mod in _iter_import_modules(tree):
            if in_client and mod.startswith("server."):
                if not _fnmatch_any(f, IMPORT_ALLOW_CLIENT_TO_SERVER):
                    violations.append(
                        f"C4 import 边界: {f} import {mod}（client 不许 import server，"
                        f"例外需加入 IMPORT_ALLOW_CLIENT_TO_SERVER）"
                    )
            elif not in_client and mod.startswith("client."):
                if not _fnmatch_any(f, IMPORT_ALLOW_SERVER_TO_CLIENT):
                    violations.append(
                        f"C4 import 边界: {f} import {mod}（server 不许 import client，"
                        f"例外需加入 IMPORT_ALLOW_SERVER_TO_CLIENT）"
                    )
    return violations


# ---------- C5 MCP 工具名引用真实性 ----------

def _load_direct_tools() -> set[str]:
    """加载第一层直连工具名集合（真源 server/mcp_whitelist.py 的 DIRECT_TOOLS）。

    ⛔ 刻意按文件路径 spec 加载，而不是 `import server.mcp_whitelist`：后者会执行
    server 包的导入链，在 pre-commit 钩子里既慢又可能因环境缺失而失败。
    mcp_whitelist.py 是纯数据模块（零 import），spec 加载无副作用。
    """
    p = PROJECT_ROOT / "server" / "mcp_whitelist.py"
    spec = importlib.util.spec_from_file_location("_hard_rules_mcp_whitelist", p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法为 {p} 构造 import spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.DIRECT_TOOLS)


def _scan_mcp_refs(text: str, direct_tools: set[str]) -> list[tuple[int, str]]:
    """扫描文本，返回 [(lineno, tool_name), ...]：不在直连表且非反例语境的引用。

    纯函数（不碰文件系统），便于 pytest 直接喂样例覆盖两个陷阱。
    """
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if any(hint in low for hint in MCP_REF_NEGATIVE_HINTS):
            continue  # 反例/禁令语境：不是推荐用法
        for m in RE_MCP_REF.finditer(line):
            name = m.group(1)
            if name in direct_tools:
                continue
            # ⛔ 判定只看 DIRECT_TOOLS 成员资格：GATEWAY_EXCLUDE **不代表不存在**
            # （parcae_advanced_tool 同时属于 DIRECT_TOOLS 与 GATEWAY_EXCLUDE——
            # 它是直连工具，只是不重复收进网关）。拿 GATEWAY_EXCLUDE 当"不存在"必误报。
            found.append((lineno, name))
    return found


def check_mcp_tool_refs(files: list[str]) -> list[str]:
    try:
        direct_tools = _load_direct_tools()
    except Exception as exc:  # 加载失败必须显式报出来，不能静默放行
        return [f"C5 MCP 工具名: 无法加载 DIRECT_TOOLS（{type(exc).__name__}: {exc}）"]

    violations = []
    for f in files:
        if _fnmatch_any(f, MCP_REF_ALLOW_FILES):
            continue
        if EVIDENCE_RAW_SURFACES.match(f.replace("\\", "/")):
            continue  # 证据原样抓取层：卡片刻的是被调研现场的调用名（见常量处注释）
        if Path(f).suffix.lower() not in MCP_REF_SCAN_EXTS:
            continue
        p = PROJECT_ROOT / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, name in _scan_mcp_refs(text, direct_tools):
            violations.append(
                f"C5 MCP 工具名: {f}:{lineno} `mcp__localagent__{name}` 不在第一层直连表"
                f"（该工具只能走 parcae_advanced_tool(tool=\"{name}\")；"
                f"若这行是刻意写的反例，请在同行加 'not found' 等否定语境）"
            )
    return violations


# ---------- C6 在用文档面的死路径引用 ----------

def _path_exists(p: Path) -> bool:
    return p.is_file() or p.is_dir()


def find_dead_doc_refs(text: str, md_rel: str, exists) -> list[tuple[int, str]]:
    """扫一份 md，返回"该管且已死"的 [(lineno, ref), ...]。

    三条豁免的顺序是固定的（先豁免后判死），换顺序就会把通配算进 backlog：
      1. 历史/流水账面整份不扫（CHANGELOG/归档/WIP/日志/planning_notes）；
      2. 第三方/上游存档整棵不扫（`workspace/dev_toolkit/`、`workspace/maa-patch/`）；
      3. 通配/占位（`models/*`、`<file>`、`_template.md`、"示例"）——任何基准都判不了；
      4. 沿革语境（该行在说"原 X 已删/已改名/已移走"）与占位语境（`x.md`、`yyy.py`…）；
      5. **沿 md 的各级祖先目录**能解析的相对写法。

    第 4 条按 **(文件, 路径) 成对**生效，而不是按行：同一个路径在这份 md 里只要在
    **任何一行**出现在沿革/占位语境里，它在整份文件里就都放行。为什么要跨行——同一份
    ADR 里 `server/gui_process.py` 出现在 L20（平铺直叙"根因是它"）和 L28（"文件从它
    重命名为 overlay_client.py"），只按行豁免会把 L20 判成死链，逼着作者去改一句本来
    正确的叙述。这是有意放宽：宁可漏报一条，也不要把"记录历史"或"举例子"的正确写法
    判成缺陷——反过来改文档去迁就闸门是错的。

    第 3 条为什么要走到"每一级祖先"而不只是 md 自己那格：`workspace/` 下是**各自独立的
    子项目**（homework 有自己的 `tools/hw_*.py`、dev_toolkit 有自己的 `scripts/` 与 `.agents/`、
    gh_mirror 有自己的 `scripts/`），它们的文档按**子项目根**写路径是正常风格；只看 md 自己那格
    会把这一整片读成死链。2026-10-07 实测：只按 md 目录判是 59 条，走到每级祖先之后 **49 条**，
    被救的 10 条里就包括 `workspace/homework/subjects/*/conventions.md → tools/hw_*.py`
    （那些文件真实存在于 `workspace/homework/tools/`）。

    `exists` 注入是为了让 pytest 喂合成样例（不碰真盘）也能覆盖这些豁免。
    """
    if DOC_REF_HISTORY_SURFACES.match(md_rel):
        return []
    if DOC_REF_THIRD_PARTY_SURFACES.match(md_rel):
        return []
    if EVIDENCE_RAW_SURFACES.match(md_rel):
        return []  # 证据原样抓取层，路径属于被调研现场（见常量处注释）
    md_path = PROJECT_ROOT / md_rel
    ancestors: list[Path] = []
    cur = md_path.parent
    while True:
        ancestors.append(cur)
        if cur == PROJECT_ROOT or cur.parent == cur:
            break
        cur = cur.parent
    lines = text.splitlines()
    # 第一遍：收集"在这份 md 里出现过沿革/占位语境"的路径（详见 docstring 第 4 条）
    contextual: set[str] = set()
    for line in lines:
        if not (DOC_REF_HISTORY_LINE.search(line) or DOC_REF_EXAMPLE_LINE.search(line)):
            continue
        for m in RE_DOC_PATH_REF.finditer(line):
            if not DOC_REF_PLACEHOLDER.search(m.group(1)):
                contextual.add(m.group(1))
    # 第二遍：判死
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, 1):
        for m in RE_DOC_PATH_REF.finditer(line):
            ref = m.group(1)
            if DOC_REF_PLACEHOLDER.search(ref):
                continue
            if ref in contextual:
                continue  # 别处有沿革/占位语境，这条路径全文件放行
            if exists(PROJECT_ROOT / ref):
                continue
            if any(exists(base / ref) for base in ancestors):
                continue  # 相对某个上级目录能解析（子项目根/同目录写法）
            if any(exists(PROJECT_ROOT / root / ref) for root in DOC_REF_SUBPROJECT_ROOTS):
                continue  # 跨子项目引用，按该子项目根写（白名单内，见常量处注释）
            out.append((lineno, ref))
    return out


def _baseline_pair_still_dead(pair: tuple[str, str]) -> bool:
    """基线里这一条 (文件, 引用) 现在还是"该管且已死"吗。

    逐条只读它自己那份文件（不靠"本次传进来的清单"）——否则 staged 模式永远看不到
    别的文件，整份基线会被误报成陈旧。
    """
    f, ref = pair
    p = PROJECT_ROOT / f
    if not p.is_file():
        return False  # 文件都不在了（改名/删除），基线条目该摘
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(r == ref for _lineno, r in find_dead_doc_refs(text, f, _path_exists))


def check_dead_doc_paths(files: list[str]) -> list[str]:
    """在用文档面的死路径引用：存量走基线名单，**新增**一律判红。

    同时反查基线里的陈旧条目——名单只会因"没人回头删"而烂掉，
    修好一条就必须把它从名单里摘掉，否则 backlog 数字是假的。
    """
    violations: list[str] = []
    for f in files:
        if _fnmatch_any(f, DOC_REF_ALLOW_FILES):
            continue
        if Path(f).suffix.lower() != ".md":
            continue
        p = PROJECT_ROOT / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, ref in find_dead_doc_refs(text, f, _path_exists):
            if (f, ref) in DEAD_DOC_PATH_BASELINE:
                continue
            violations.append(
                f"C6 死链: {f}:{lineno} 引用 `{ref}` 在仓库根解析不到"
                f"（不是通配/占位，也不能按该 md 自身目录解析）。"
                f"改成现存路径；确属历史遗留才加进 tools/check_hard_rules.py 的 "
                f"DEAD_DOC_PATH_BASELINE 并写明理由"
            )
    for pair in sorted(DEAD_DOC_PATH_BASELINE):
        if not _baseline_pair_still_dead(pair):
            violations.append(
                f"C6 基线陈旧: {pair[0]} → `{pair[1]}` 现在已不构成死链，"
                f"请把它从 DEAD_DOC_PATH_BASELINE 删掉（backlog 数字要跟着现实走）"
            )
    return violations


# ---------- 主入口 ----------

ALL_CHECKS = [
    check_bom,
    check_secrets,
    check_path_boundary,
    check_import_boundary,
    check_mcp_tool_refs,
    check_dead_doc_paths,
]


def run_checks(staged: bool = False) -> list[str]:
    files = _staged_files() if staged else _tracked_files()
    violations: list[str] = []
    for check in ALL_CHECKS:
        violations.extend(check(files))
    return sorted(set(violations))


def cmd_check(staged: bool) -> int:
    violations = run_checks(staged=staged)
    scope = "staged" if staged else "全量 tracked"
    if violations:
        print(f"[check_hard_rules] {scope} 检查发现 {len(violations)} 处违规：")
        for v in violations:
            print(f"  {v}")
        print("修复或显式更新 tools/check_hard_rules.py 白名单（白名单变更必须 commit）")
        return 1
    print(f"[check_hard_rules] {scope} 检查通过"
          f"（C1 BOM / C2 密钥 / C3 路径 / C4 import / C5 MCP 工具名 / C6 死链）")
    return 0


def cmd_install() -> int:
    subprocess.run(
        ["git", "config", "core.hooksPath", "scripts/hooks"],
        cwd=PROJECT_ROOT, check=True,
    )
    print("[check_hard_rules] 已设置 core.hooksPath=scripts/hooks，pre-commit 生效")
    return 0


def cmd_uninstall() -> int:
    r = subprocess.run(
        ["git", "config", "--unset", "core.hooksPath"],
        cwd=PROJECT_ROOT, capture_output=True,
    )
    if r.returncode == 0:
        print("[check_hard_rules] 已恢复默认 hooksPath")
    else:
        print("[check_hard_rules] 本来就没有自定义 hooksPath")
    return 0


def main(argv: list[str]) -> int:
    if "--staged" in argv:
        return cmd_check(staged=True)
    if "install" in argv:
        return cmd_install()
    if "uninstall" in argv:
        return cmd_uninstall()
    return cmd_check(staged=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
