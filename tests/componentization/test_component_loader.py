"""lib/component_loader 单测：外部组件的错误不得影响核心启动。

事故背景（2026-10-20）：``workspace/chat_digest/loop_actions.py:30`` 一个未转义
的半角引号触发 SyntaxError；原加载器只捕获 ImportError，SyntaxError 直接穿透
``server/main.py:81`` 的顶层 import，后端启动即退出。本测试锁住三条防线：

1. 语法预检（零副作用、带文件:行号）；
2. 失败登记（组件名 / 类别 / 位置，供 /health 与启动日志）；
3. 坏组件只降级自己，同批其它组件照常加载。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lib import component_loader as cl

GOOD_SOURCE = '''GUIDE_REGISTRY_ENTRIES = {
    "adhoc.good_comp": {
        "skill": "good_comp",
        "name": "好组件",
        "skill_file": "workspace/good_comp/SKILL.md",
        "keywords": ["good_comp"],
        "description": "正常组件",
    }
}
'''

# 复刻事故：外层双引号字符串里出现裸半角引号
BAD_SOURCE = '''GUIDE_REGISTRY_ENTRIES = {
    "adhoc.broken_comp": {
        "skill": "broken_comp",
        "first_action": "若用户说「默认」（含"默认那套"），照做",
    }
}
'''


def _snapshot_workspace_modules() -> dict:
    """把真实 workspace.* 模块暂时移出 sys.modules（测试期间替换成临时包）。"""
    snapshot = {}
    for name in list(sys.modules):
        if name == "workspace" or name.startswith("workspace."):
            snapshot[name] = sys.modules.pop(name)
    return snapshot


def _restore_workspace_modules(snapshot: dict) -> None:
    for name in list(sys.modules):
        if (name == "workspace" or name.startswith("workspace.")) and name not in snapshot:
            del sys.modules[name]
    sys.modules.update(snapshot)


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    """把 _WORKSPACE_DIR 指向 tmp_path/workspace，并把 workspace 包改道过去。"""
    from lib import component_manifest

    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    (ws_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(component_manifest, "_WORKSPACE_DIR", ws_dir)
    monkeypatch.setattr(component_manifest, "_PROJECT_ROOT", tmp_path)
    component_manifest.reset_cache()

    snapshot = _snapshot_workspace_modules()
    module = type(sys)("workspace")
    module.__path__ = [str(ws_dir)]
    sys.modules["workspace"] = module

    cl.reset_failures()
    try:
        yield ws_dir
    finally:
        cl.reset_failures()
        _restore_workspace_modules(snapshot)
        component_manifest.reset_cache()


def _make_component(ws_dir, name: str, source: str) -> Path:
    comp = ws_dir / name
    comp.mkdir()
    (comp / "__init__.py").write_text("", encoding="utf-8")
    src = comp / "loop_actions.py"
    src.write_text(source, encoding="utf-8")
    return src


class TestSyntaxPrecheck:
    """import 之前的语法预检：零副作用，能报出文件:行。"""

    def test_ok_file_passes(self, tmp_path):
        f = tmp_path / "ok.py"
        f.write_text("X = 1\n", encoding="utf-8")
        assert cl.check_syntax(f) is None

    def test_broken_file_reports_position(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text(BAD_SOURCE, encoding="utf-8")
        err = cl.check_syntax(f)
        assert err is not None
        assert "SyntaxError" in err
        assert "bad.py:" in err

    def test_missing_file_reports_oserror(self, tmp_path):
        err = cl.check_syntax(tmp_path / "nope.py")
        assert err is not None
        assert "OSError" in err


class TestFailureRegistry:
    """失败必须可查：组件名 + 类别 + 位置。"""

    def setup_method(self):
        cl.reset_failures()

    def teardown_method(self):
        cl.reset_failures()

    def test_record_and_summary(self):
        cl.record_failure(
            "comp",
            "workspace.comp.loop_actions",
            cl.KIND_SYNTAX,
            "SyntaxError: invalid syntax (a.py:3)",
            "a.py",
        )
        fails = cl.get_failures()
        assert len(fails) == 1
        assert fails[0]["component"] == "comp"
        assert fails[0]["kind"] == cl.KIND_SYNTAX
        lines = cl.failure_summary_lines()
        assert len(lines) == 1
        assert "comp" in lines[0] and "a.py:3" in lines[0]

    def test_reset_clears(self):
        cl.record_failure("c", "m", cl.KIND_IMPORT, "boom")
        assert cl.has_failures() is True
        cl.reset_failures()
        assert cl.get_failures() == []


class TestLoadIsolation:
    """坏组件只降级自己，加载入口永不抛异常。"""

    def test_broken_component_registers_syntax_and_returns_none(self, temp_workspace):
        src = _make_component(temp_workspace, "broken_comp", BAD_SOURCE)

        mod = cl.load_component_module(
            "workspace.broken_comp.loop_actions", component="broken_comp", path=src
        )

        assert mod is None, "语法坏的组件不该被加载"
        fails = cl.get_failures()
        assert len(fails) == 1
        assert fails[0]["kind"] == cl.KIND_SYNTAX
        assert fails[0]["component"] == "broken_comp"

    def test_broken_does_not_block_good_in_same_batch(self, temp_workspace):
        from server import agent_guide

        _make_component(temp_workspace, "good_comp", GOOD_SOURCE)
        _make_component(temp_workspace, "broken_comp", BAD_SOURCE)

        entries = agent_guide._load_optional_guide_entries()

        assert "adhoc.good_comp" in entries, "同批的好组件必须照常加载"
        assert "adhoc.broken_comp" not in entries, "坏组件不该进注册表"
        assert [f["component"] for f in cl.get_failures()] == ["broken_comp"]

    def test_component_without_loop_actions_is_not_a_failure(self, temp_workspace):
        """组件只有 SKILL.md、没有 loop_actions.py 是正常情况，不算加载失败。"""
        from server import agent_guide

        only_skill = temp_workspace / "skill_only"
        only_skill.mkdir()
        (only_skill / "__init__.py").write_text("", encoding="utf-8")

        entries = agent_guide._load_optional_guide_entries()

        assert entries == {}
        assert cl.get_failures() == [], "没有入口文件的组件不该被登记为失败"

    def test_runtime_error_during_import_is_caught(self, temp_workspace):
        """模块级运行时异常（非语法）同样只降级，不上升。"""
        src = _make_component(
            temp_workspace,
            "raiser",
            'raise RuntimeError("模块级炸了")\n',
        )

        mod = cl.load_component_module(
            "workspace.raiser.loop_actions", component="raiser", path=src, probe=False
        )

        assert mod is None
        fails = cl.get_failures()
        assert len(fails) == 1
        assert fails[0]["kind"] == cl.KIND_IMPORT
        assert "RuntimeError" in fails[0]["detail"]
        assert "模块级炸了" in fails[0]["detail"]


class TestProbeDefaults:
    """子进程探测默认关闭（实测拖慢 20s 且误杀重依赖组件）。"""

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(cl.PROBE_ENV, raising=False)
        assert cl.probe_enabled() is False

    def test_can_be_enabled_by_env(self, monkeypatch):
        monkeypatch.setenv(cl.PROBE_ENV, "1")
        assert cl.probe_enabled() is True

    def test_warm_probe_is_noop_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv(cl.PROBE_ENV, raising=False)
        src = tmp_path / "x.py"
        src.write_text("X = 1\n", encoding="utf-8")
        module = "workspace.__loader_probe_noop_test__.loop_actions"

        assert cl.warm_probe({module: src}) == {module: None}
