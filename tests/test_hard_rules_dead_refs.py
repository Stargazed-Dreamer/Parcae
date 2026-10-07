"""C6 死链闸门的专项用例（真源 tools/check_hard_rules.py）。

三条豁免各钉一条，外加两条"名单不是橡皮图章"的接缝：
  - 基线只挡"已知存量"，从基线里摘掉一条就必须立刻判红（证明闸门真在看这些引用）
  - 基线里的条目一旦不再构成死链 → 全量跑必须报"基线陈旧"（逼着修完删行，
    否则 backlog 数字会和现实脱钩）

`find_dead_doc_refs` 的 `exists` 是注入的，所以豁免类用例不碰真盘。
"""
# pyright: reportMissingImports=false
# 走 sys.path 装载 tools/ 下的模块（同 tests/test_hard_rules.py 的做法）；
# 这里要拿模块对象本身，才能 monkeypatch 它的全局基线常量。
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_hard_rules as hr  # noqa: E402


def _nothing_exists(_p: Path) -> bool:
    return False


def _only_root(*existing: str) -> Callable[[Path], bool]:
    """只承认"仓库根下这几个相对路径"存在，其余一律不存在。"""
    wanted = {hr.PROJECT_ROOT / p for p in existing}
    return lambda p: p in wanted


class TestExemptions:
    def test_history_surface_is_not_scanned_at_all(self):
        # CHANGELOG/归档/WIP/日志 提已删除路径是正当职责，整份不扫
        for md in ("CHANGELOG.md", "docs/changelog-archive.md", ".agents/wip/wip_x.md",
                   "planning_notes/n.md", "workspace/gh_mirror/records/r.md"):
            assert hr.find_dead_doc_refs("见 `server/gone.py`。", md, _nothing_exists) == [], md

    def test_placeholder_and_wildcard_are_exempt(self):
        # 通配/占位在任何基准下都判不了死活，不能算进 backlog
        text = (
            "跑 `workspace/*/loop_actions.py` 里的动作\n"
            "样例见 `docs/<你的文件>.md` 与 `scripts/collect_*.py`\n"
            "模板是 `.agents/skills/browser_lessons/sites/_template.md`\n"
            "举个例 `tools/example_tool.py`\n"
        )
        assert hr.find_dead_doc_refs(text, "docs/x.md", _nothing_exists) == []

    def test_ref_resolving_at_repo_root_is_ok(self):
        assert hr.find_dead_doc_refs(
            "见 `server/vl/remote_vl.py`。", "docs/y.md", _only_root("server/vl/remote_vl.py")
        ) == []

    def test_ref_resolving_against_a_subproject_root_is_ok(self):
        """子项目文档按**自己根**写路径是合法风格，不能只认 md 自己那一格。

        实测救回的就是这一类：workspace/homework/subjects/arch/conventions.md 写
        `tools/hw_docx_build.py`，文件真实存在于 workspace/homework/tools/。
        """
        md = "workspace/homework/subjects/arch/conventions.md"
        exists = _only_root("workspace/homework/tools/hw_docx_build.py")
        assert hr.find_dead_doc_refs("跑 `tools/hw_docx_build.py`。", md, exists) == []
        # 地板：同一个路径若哪一级都不存在，必须判死（否则上面那条是恒绿）
        assert hr.find_dead_doc_refs(
            "跑 `tools/hw_docx_build.py`。", md, _nothing_exists
        ) == [(1, "tools/hw_docx_build.py")]

    def test_dead_ref_is_reported_with_line_number(self):
        text = "第一行没问题\n看 `server/gone_module.py` 的实现\n"
        assert hr.find_dead_doc_refs(text, "docs/z.md", _nothing_exists) == [
            (2, "server/gone_module.py")
        ]


class TestBaselineIsNotARubberStamp:
    def test_entry_outside_baseline_blocks_the_commit(self, monkeypatch):
        """把一条已知存量从基线里摘掉 → 必须立刻判红，证明闸门真的在看这份文件。"""
        assert hr.DEAD_DOC_PATH_BASELINE, "基线为空则本用例测的是空气"
        pair = sorted(hr.DEAD_DOC_PATH_BASELINE)[0]
        monkeypatch.setattr(hr, "DEAD_DOC_PATH_BASELINE",
                            set(hr.DEAD_DOC_PATH_BASELINE) - {pair})
        violations = hr.check_dead_doc_paths([pair[0]])
        hits = [v for v in violations if v.startswith("C6 死链") and pair[1] in v]
        assert len(hits) >= 1, violations

    def test_every_baseline_entry_is_still_real(self):
        """基线里每条现在都必须仍构成死链——修好了就删行，别让名单比现实宽松。"""
        stale = [p for p in sorted(hr.DEAD_DOC_PATH_BASELINE)
                 if not hr._baseline_pair_still_dead(p)]
        assert not stale, f"陈旧基线条目（请删掉）: {stale}"

    def test_stale_entry_is_reported_as_baseline_stale(self, monkeypatch):
        fake = {("docs/definitely_not_a_real_doc.md", "server/whatever.py")}
        monkeypatch.setattr(hr, "DEAD_DOC_PATH_BASELINE", fake)
        violations = hr.check_dead_doc_paths(["README.md"])
        assert any(v.startswith("C6 基线陈旧") for v in violations), violations

    def test_inbox_surface_is_exempt_end_to_end(self, monkeypatch):
        """CHANGELOG 里的已删除路径不该被拦：整份历史面在豁免里。"""
        monkeypatch.setattr(hr, "DEAD_DOC_PATH_BASELINE", set())
        assert hr.check_dead_doc_paths(["CHANGELOG.md"]) == []


class TestCorpusInvariants:
    def test_c6_is_registered_in_all_checks(self):
        """注册本身要钉住：只直连调用各 check_* 的用例挡不住"忘了加进 ALL_CHECKS"。

        摘改矩阵实测：把 check_dead_doc_paths 从 ALL_CHECKS 里摘掉，其余用例照样全绿
        （它们各自直接调 hr.check_dead_doc_paths），只有走 run_checks 的那条才该红——
        所以这条注册断言不是冗余，是那道闸门的唯一看守。
        """
        assert hr.check_dead_doc_paths in hr.ALL_CHECKS

    def test_run_checks_reaches_c6(self, monkeypatch):
        """接缝：把基线清空后走 run_checks，必须能看见 C6 报出来的死链。

        光断言"ALL_CHECKS 里有这个函数"挡不住函数内部提前 return/异常吞掉；
        这条要求死链**经由 run_checks**冒出来，才是端到端可达。
        """
        monkeypatch.setattr(hr, "DEAD_DOC_PATH_BASELINE", set())
        violations = hr.run_checks(staged=False)
        assert any(v.startswith("C6 死链") for v in violations), (
            "run_checks 没走到 C6 ⇒ 注册或调用链断了，基线外的新死链会静默放行"
        )

    def test_gate_scans_a_meaningful_number_of_docs(self):
        """防"扫描器坏了自己不知道"：tracked .md 数量与死链总数都要有量级地板。"""
        tracked = hr._tracked_files()
        mds = [f for f in tracked if f.endswith(".md")]
        assert len(mds) > 300, len(mds)
        dead = 0
        for f in mds:
            p = hr.PROJECT_ROOT / f
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            dead += len(hr.find_dead_doc_refs(text, f, hr._path_exists))
        assert dead > 0, "在用面一条死链都扫不出来 ⇒ 正则或豁免失效，闸门在测空气"
        # 存量都在基线里：扫出的死链数 == 基线条数（按 (file, ref) 去重）
        pairs = {(f, r) for f in mds if (hr.PROJECT_ROOT / f).is_file()
                 for _ln, r in hr.find_dead_doc_refs(
                     (hr.PROJECT_ROOT / f).read_text(encoding="utf-8", errors="replace"),
                     f, hr._path_exists)}
        assert pairs == set(hr.DEAD_DOC_PATH_BASELINE), (
            f"扫出 {len(pairs)} 条 vs 基线 {len(hr.DEAD_DOC_PATH_BASELINE)} 条：名单与现实脱钩"
        )
