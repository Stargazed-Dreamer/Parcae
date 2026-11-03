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

    def test_third_party_surfaces_are_not_scanned_at_all(self):
        """上游 toolkit 存档 / 外部项目踩坑记录：里面的 `scripts/`、`tests/` 指的是
        **蓝图用户自己项目**和 maa 仓库的路径，不是本仓的，按仓库根判必然误报。"""
        for md in ("workspace/dev_toolkit/team_playbook.md",
                   "workspace/maa-patch/LESSONS.md",
                   "workspace/dev_toolkit/.agents/skills/spec/SKILL.md"):
            assert hr.find_dead_doc_refs(
                "见 `docs/gamejam-lessons.md`。", md, _nothing_exists
            ) == [], md

    def test_third_party_exemption_is_not_a_free_pass(self):
        """地板：同一句话放在**本仓**文档里必须判死——否则上一条是恒绿，等于全库免检。"""
        assert hr.find_dead_doc_refs(
            "见 `docs/gamejam-lessons.md`。", "docs/somewhere.md", _nothing_exists
        ) == [(1, "docs/gamejam-lessons.md")]

    def test_evidence_raw_surface_is_not_scanned_at_all(self):
        """66 事务盘点的**原始抓取件**：卡片正文是从用户其它项目的会话里逐字导出的，
        里面的 `tools/check_arch.py`、`tests/conftest.py` 是被调研现场的路径。
        改它们等于改证据 ⇒ 整棵不扫（2026-11-03 补 local_butler 入库时实测 4923 条）。"""
        raw = ("workspace/local_butler/40_盘点与证据/20_事务盘点/"
               "20_分源产出/07_ai/01_cards/workbuddy/fac77/card.md")
        assert hr.find_dead_doc_refs(
            "改动涉及 `tools/check_arch.py` 与 `tests/conftest.py`。", raw, _nothing_exists
        ) == []

    def test_evidence_exemption_stops_at_the_raw_layer(self):
        """豁免只到 `20_分源产出/` 为止：同一盘点的设计文档与汇总仍受管。

        两处兄弟路径各钉一条，免得把整棵 `40_盘点与证据/` 或整个 `local_butler/`
        一刀切成免检区（那样这一层的设计文档出事就没人报了）。"""
        for md in ("workspace/local_butler/40_盘点与证据/20_事务盘点/00_调查设计.md",
                   "workspace/local_butler/40_盘点与证据/20_事务盘点/30_汇总/H6_汇总.md",
                   "workspace/local_butler/30_架构与设计/10_边界与契约/DD-流转契约.md"):
            assert hr.find_dead_doc_refs(
                "见 `tools/check_arch.py`。", md, _nothing_exists
            ) == [(1, "tools/check_arch.py")], md

    def test_evidence_raw_surface_is_exempt_from_c5_too(self, monkeypatch, tmp_path):
        """C5 与 C6 共用同一个豁免面：卡片里的 `mcp__localagent__X` 是被调研现场的
        调用名（实测 701 条），不该按本仓的第一层直连表去判。"""
        raw_rel = ("workspace/local_butler/40_盘点与证据/20_事务盘点/"
                   "20_分源产出/07_ai/01_cards/codex/card.md")
        body = "调 `mcp__localagent__exec_terminal_spawn` 起终端\n"
        for rel in (raw_rel, "docs/borrowed.md"):
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        monkeypatch.setattr(hr, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hr, "_load_direct_tools", lambda: set())
        assert hr.check_mcp_tool_refs([raw_rel]) == []
        # 地板：同一句话写在 docs/ 下必须照旧判红
        assert len(hr.check_mcp_tool_refs(["docs/borrowed.md"])) == 1

    def test_history_line_exempts_that_ref_anywhere_in_the_file(self):
        """同一路径只要在**某一行**带沿革动词，全文件都放行。

        实测原型 ADR 0011：`server/gui_process.py` 在 L20 是平铺直叙"根因是它"，
        L28 才写"重命名为 overlay_client.py"。只按行豁免会把 L20 判死，逼作者去改
        一句本来正确的叙述。
        """
        text = (
            "根因：`GUIClient`（`server/gui_process.py`，1053 行）单类一身五职。\n"
            "——文件从 `server/gui_process.py` 重命名为 `server/overlay_client.py`。\n"
        )
        assert hr.find_dead_doc_refs(text, "docs/adr/0011-x.md", _nothing_exists) == []

    def test_data_migration_wording_does_not_exempt_file_paths(self):
        """地板：`迁移到` 讲的是**数据**迁移（密钥 config→secrets、格式 v8→v9），
        不能顺带把文件路径也豁免掉——钉住 `(?<!迁)移到` 那个子串坑。"""
        # ⛔ 这句**不能**写"历史迁移"——那是真沿革（一次性迁移脚本跑完就删），
        # 是被有意豁免的；这里要测的是纯数据迁移措辞不豁免。
        text = "逐级迁移到当前版本（其中一步需手动跑 `tools/migrate_x.py`）\n"
        assert hr.find_dead_doc_refs(text, "docs/llm-pool.md", _nothing_exists) == [
            (1, "tools/migrate_x.py")
        ]

    def test_example_line_exempts_placeholder_filenames(self):
        """`x.md`/`yyy.py` 这类占位名不是真路径，举例子时不该判死。"""
        text = "替换必须幂等：`docs/x.md` 是子串\n| 配套脚本 | scripts/yyy.py | ... |\n"
        assert hr.find_dead_doc_refs(text, "docs/a.md", _nothing_exists) == []

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
        """基线外的死链必须判红——证明闸门真的在看这些引用，不是摆设。

        ⚠️ 2026-10-08 存量已全部修完（`DEAD_DOC_PATH_BASELINE` 清零），所以**不能再从
        名单里挑一条来测**（名单空了这条会退化成跳过）。改成把 `_path_exists` 换成
        「什么都不存在」，让 README.md 里真实存在的路径引用**全部**变成死链——基线是空的，
        于是每一条都得判红。这样测的还是同一条代码路径，且不依赖"仓库里留着坏引用"。
        """
        monkeypatch.setattr(hr, "_path_exists", _nothing_exists)
        violations = hr.check_dead_doc_paths(["README.md"])
        hits = [v for v in violations if v.startswith("C6 死链")]
        assert hits, (
            "基线为空 + 路径全部判不存在，却一条 C6 死链都没报 ⇒ 闸门失效（或豁免吃光了）"
        )

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

        ⚠️ 存量清零后语料里已经没有真死链了，所以把 `_path_exists` 换成「什么都不存在」
        造出死链，否则这条会退化成恒绿（同 test_entry_outside_baseline_blocks_the_commit）。
        """
        monkeypatch.setattr(hr, "DEAD_DOC_PATH_BASELINE", set())
        monkeypatch.setattr(hr, "_path_exists", _nothing_exists)
        violations = hr.run_checks(staged=False)
        assert any(v.startswith("C6 死链") for v in violations), (
            "run_checks 没走到 C6 ⇒ 注册或调用链断了，基线外的新死链会静默放行"
        )

    def test_gate_scans_a_meaningful_number_of_docs(self):
        """防"扫描器坏了自己不知道"：tracked .md 数量与死链总数都要有量级地板。"""
        tracked = hr._tracked_files()
        mds = [f for f in tracked if f.endswith(".md")]
        assert len(mds) > 300, len(mds)
        # ⚠️ 地板原来是 `dead > 0`（语料里必须扫得出死链），2026-10-08 存量修完之后它不再
        # 成立——继续要求它等于逼着"故意留一条死链不修"来养地板。这条地板真正要看的是
        # **扫描器还没坏**（正则没失效、豁免没把什么都吃掉），用合成样例判即可。
        assert hr.find_dead_doc_refs(
            "看 `server/definitely_gone.py` 的实现。", "docs/synthetic.md", _nothing_exists
        ) == [(1, "server/definitely_gone.py")], "扫描器判不出死链 ⇒ 正则或豁免失效"
        # 存量都在基线里：扫出的死链数 == 基线条数（按 (file, ref) 去重）
        pairs = {(f, r) for f in mds if (hr.PROJECT_ROOT / f).is_file()
                 for _ln, r in hr.find_dead_doc_refs(
                     (hr.PROJECT_ROOT / f).read_text(encoding="utf-8", errors="replace"),
                     f, hr._path_exists)}
        assert pairs == set(hr.DEAD_DOC_PATH_BASELINE), (
            f"扫出 {len(pairs)} 条 vs 基线 {len(hr.DEAD_DOC_PATH_BASELINE)} 条：名单与现实脱钩"
        )
