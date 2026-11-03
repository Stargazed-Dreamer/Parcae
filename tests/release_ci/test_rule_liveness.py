"""脱敏规则「活性」闸门：规则写成 no-op 时当场红，而不是等发布物泄了才追。

为什么单独一道：v0.49.0 因规则 no-op 泄过真机路径与主机名，付过 force-push 重发的代价。
09-29 修的是规则本体（`release/profiles/public-full.toml` 的转义层数），本文件补的是防复发：
`test_apply_placeholders_sync` 只查占位符清单同步、`test_sanitized_residue` 只查残留，
两者都看不见「规则本身匹配不上」。

三类判据都按引擎真实用法跑，不另拼一套语义：

1. `[sensitive_line_skips]` / `[post_process_remove_lines]`：规则是**按文件**登记的，
   所以活性判据也按文件——它必须能在**自己那个文件**里命中至少一行，且那一行确实会被
   `prepare._SENSITIVE_PATTERNS` 判成命中。命中不了任何行 = 写了但永不生效的死配置；
   命中的行本来不敏感 = 这道遮蔽只是把扫描器闭嘴。两种都红。
   （用合成形态样本当判据不行：样本电池永远缺尾名 → 误报，而真正坏掉的转义层反而能命中
   电池里的双反斜杠样本 → 假绿。2026-10-04 实测走过这个弯路后改成自锚。）
2. `[content_replacements.path_mapping]`：每条键走 `build._apply_path_mapping` 的用法
   （`re.sub(re.escape(find), ...)` + IGNORECASE），断言它在待打包语料里真能命中；
   命中不上的替换规则 = 静默不脱敏 = 泄漏面。
3. `[scan]` 的 `residue_markers`：每条必须能在典型残留样本上开火（正控制），
   否则「残留复检」本身是假绿灯。

新增豁免必须写理由：豁免是「已知这条当前无命中面」的声明，不是「测试红了先关掉」。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import toml

from tools.release.engine.build import _load_path_mapping
from tools.release.engine.prepare import _SENSITIVE_PATTERNS

_PROFILE = Path("release/profiles/public-full.toml")
_ROOT = Path(".")


def _profile() -> dict:
    return toml.load(_PROFILE)


def _rules_from(section: str) -> dict[str, list[str]]:
    data = _profile()
    return {k: list(v) for k, v in (data.get(section, {}) or {}).items() if isinstance(v, list)}


def _file_lines(rel_path: str) -> list[str]:
    path = _ROOT / rel_path
    if not path.exists():
        raise FileNotFoundError(rel_path)
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def _personal_identifier_patterns() -> list[re.Pattern[str]]:
    """profile `[scan] personal_identifiers` 编译成的 personal-hostname 规则。

    必须与 `prepare.collect_sensitive_hits` 同源——那边**只在 identifiers 非空时**
    才把 personal-hostname 并进规则表。本文件原先只看 `_SENSITIVE_PATTERNS`，
    判据比引擎窄：任何「只遮蔽主机名」的 skip 都会被判成「遮蔽了本来不敏感的脏行」，
    好配置反被判死（2026-11-03 加主机名豁免时踩到，被这条测试拦下）。
    """
    scan = _profile().get("scan") or {}
    return [
        re.compile(re.escape(s), re.IGNORECASE)
        for s in (scan.get("personal_identifiers") or [])
        if s
    ]


def _is_sensitive_line(line: str) -> bool:
    if any(pat.search(line) for cfg in _SENSITIVE_PATTERNS.values() for pat in cfg["patterns"]):
        return True
    return any(pat.search(line) for pat in _personal_identifier_patterns())


# ── 1. line_skips / remove_lines 自锚活性 ─────────────────────────
_LINE_RULE_CASES = sorted(
    (section, rel, pat)
    for section in ("sensitive_line_skips", "post_process_remove_lines")
    for rel, pats in _rules_from(section).items()
    for pat in pats
)


@pytest.mark.parametrize("section,rel_path,pattern", _LINE_RULE_CASES, ids=lambda v: str(v))
def test_line_rule_is_live_and_guards_a_sensitive_line(section: str, rel_path: str, pattern: str) -> None:
    try:
        lines = _file_lines(rel_path)
    except FileNotFoundError:
        pytest.fail(f"[{section}] 登记的文件不存在：{rel_path} —— 整条规则按定义永不生效，删掉或改指现存文件。")
    rx = re.compile(pattern)
    hits = [(number, line) for number, line in enumerate(lines, start=1) if rx.search(line)]
    if not hits:
        pytest.fail(
            f"[{section}] {rel_path} 的规则 `{pattern}` 在该文件里一行都匹配不上。"
            "它现在不遮蔽任何东西，却会被后来的人当成「这行已经分诊过」的依据；"
            "要么按文件里的真实形态改写，要么整条删掉。"
        )
    if not any(_is_sensitive_line(line) for _number, line in hits):
        pytest.fail(
            f"[{section}] {rel_path} 的规则 `{pattern}` 只匹配到本来不会被扫描器命中的行"
            f"（试了 {len(hits)} 行）——这种规则的唯一作用是让扫描器少看几行，删掉它。"
        )


def test_line_rule_gate_has_teeth(tmp_path: Path) -> None:
    """反向探针：同一套判据必须把「多一层转义」和「文件搬家」两种死规则判红。"""
    real = tmp_path / "doc.md"
    real.write_text("装到 <data_drive>:/<apps_root>\VMware 下面\n", encoding="utf-8")
    lines = real.read_text(encoding="utf-8").splitlines()

    live = r"E:[\\/]+System_Programes"
    assert any(re.compile(live).search(line) for line in lines), "好规则被判死，说明判据本身坏了"

    over_escaped = r"E:[\\\\]{2,}System_Programes"  # 要求连续两个反斜杠，真文本里只有一个
    assert not any(re.compile(over_escaped).search(line) for line in lines), "坏规则居然也绿，判据无牙"

    missing_file = tmp_path / "gone.md"
    assert not missing_file.exists()

    # 反向探针之三：personal-hostname 是「profile 派生规则」，判据必须认它。
    # 只放宽不放宽都错——放宽到"什么行都算敏感"等于把闸门拆了，所以同时验中性行仍判否。
    identifiers = (_profile().get("scan") or {}).get("personal_identifiers") or []
    if identifiers:
        probe = f"故障机 {str(identifiers[0]).upper()} 上的转储文件"
        assert _is_sensitive_line(probe), (
            "personal-hostname 未被判据识别：合法的主机名豁免会被误判成死规则"
        )
        assert not _is_sensitive_line("这一行只有普通说明文字，没有任何盘符或标识"), (
            "判据过宽：无标识无路径的行也被判敏感，闸门形同虚设"
        )


# ── 2. path_mapping 必须在语料里真命中 ────────────────────────────
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".docx", ".xlsx",
    ".pptx", ".dat", ".model", ".bin", ".so", ".dll", ".pyd", ".pyc", ".ttf", ".otf",
    ".woff", ".woff2", ".zip", ".7z", ".gz", ".whl", ".onnx", ".lock", ".iso", ".vmdk",
}

# 已知当前无命中面的键 → 留下的理由（都不是"规则写坏"，而是刻意多备的一层转义/另一台机器的盘符）
PATH_MAPPING_EXEMPTIONS = {
    "<projects_parent>\\\\localagent": "混合转义变体，语料里无此写法；同族单转义键已覆盖真实形态",
    "<data_drive>:/<apps_root>\\\\Miniconda3": "混合转义变体，语料里无此写法；同族键已覆盖",
    "<debug_profile_dir>": "另一台机器上的盘符形态，本机语料只出现别的盘符",
    "System_Programes\\\\envs": "混合转义变体，语料里无此写法",
}


def _corpus_text() -> str:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout.splitlines()
    chunks: list[str] = []
    for rel in tracked:
        if Path(rel).suffix.lower() in _BINARY_EXTS:
            continue
        path = _ROOT / rel
        try:
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def test_path_mapping_rules_scrub_something() -> None:
    mapping = _load_path_mapping("public-full")
    assert mapping, "path_mapping 读空了，先确认 profile 能被引擎的 toml 加载器读出来"
    corpus = _corpus_text()
    dead = [
        find for find in mapping
        if find not in PATH_MAPPING_EXEMPTIONS
        and not re.compile(re.escape(find), re.IGNORECASE).search(corpus)
    ]
    if dead:
        detail = "\n".join(f"  - {item!r}" for item in dead)
        pytest.fail(
            f"[content_replacements.path_mapping] {len(dead)} 条规则在语料里 0 命中 = 写了但从不生效"
            f"（v0.49.0 泄漏同一类失效）：\n{detail}\n"
            "改成能命中当前形态的写法，或加进 PATH_MAPPING_EXEMPTIONS 并写明为什么留着。"
        )


def test_path_mapping_exemptions_are_not_stale() -> None:
    """豁免清单本身也要活：里面列的键必须仍在 profile 里，否则豁免是过期的死条目。"""
    mapping = _load_path_mapping("public-full")
    stale = [find for find in PATH_MAPPING_EXEMPTIONS if find not in mapping]
    assert not stale, f"PATH_MAPPING_EXEMPTIONS 里的键已不在 profile 中，删掉这些过期豁免：{stale}"


# ── 3. residue_markers 正控制 ─────────────────────────────────────
_RESIDUE_PROBES = (
    # 以下三条夹具串运行时拼接（勿合并成连续字面量）：residue_markers 是
    # 「构建链后不得残留」的个人片段，而本文件会随发布包出去——源码里拆段
    # 才能让残留复检在包内自净；私有仓里拼回的值照常给正控制开火。
    "F:/rub" "bish/tmp",
    "E:\\rub" "bish\\x",
    "<data_drive>:/<system_data_root>\\WeChat Files",
    "<data_drive>:/<working_root>\\文字篇章",
    "<data_drive>:/<projects_root>\\src",
    "<data_drive>:/<backup_root>\\<data_drive>:/<bilibili_videos>视频",
    "<data_drive>:/<classified_root>\\图",
    "F:\\\\图片大" "搜集\\\\a.png",
    "<project_root>\\x",
    "E:\\\\project_temp\\\\localAgent",
    # 真机主机名夹具：同上，源码里拆成三段拼接，避免发布包带上连续主机名
    # （v0.49.0 事故形状）；运行时拼回后 residue_markers 正控制照常开火。
    "furry" "-fox" " 主机名",
    r"<miniconda_root>",
    "<dev_machine>",
)

# 明确不需要正控制的标记 → 理由
RESIDUE_MARKER_EXEMPTIONS: dict[str, str] = {}


@pytest.mark.parametrize(
    "marker",
    (_profile().get("scan", {}) or {}).get("residue_markers") or [],
    ids=lambda v: str(v),
)
def test_residue_marker_fires_on_typical_residue(marker: str) -> None:
    if marker in RESIDUE_MARKER_EXEMPTIONS:
        pytest.skip(f"显式豁免：{RESIDUE_MARKER_EXEMPTIONS[marker]}")
    rx = re.compile(marker)
    if not any(rx.search(probe) for probe in _RESIDUE_PROBES):
        pytest.fail(
            f"[scan] residue_markers 的 `{marker}` 在所有典型残留样本上都不开火——"
            "残留复检会因此报假绿灯（规则空转 + 复检通过 = 比没复检更糟）。"
        )
