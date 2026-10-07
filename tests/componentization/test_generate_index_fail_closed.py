"""generate_index 的 fail-closed 回归：加载失败绝不能静默清空组件段。

背景（2026-09-28 实测）：`python tools/generate_index.py` 因 sys.path 无仓库根而
`No module named 'server'`，旧实现把异常吞成"0 个组件"，于是用空段覆盖已有段
（实测 -155 行）且退出码 0——典型假绿灯。现在两条路都拒绝写回。
"""

from __future__ import annotations

import sys

import pytest

from tools import generate_index

HEADER = generate_index.COMPONENT_SECTION_HEADER
END = generate_index.COMPONENT_SECTION_END

SAMPLE = (
    "# Index\n\n"
    f"{HEADER}\n\n"
    "### alpha (alpha) [adhoc]\n\n"
    "- **描述**：A\n\n"
    "### beta (beta) [recurring]\n\n"
    "- **描述**：B\n\n"
    f"{END}\n"
)


@pytest.fixture
def index_file(tmp_path, monkeypatch):
    p = tmp_path / "_index.md"
    p.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(generate_index, "INDEX_FILE", p)
    return p


@pytest.fixture
def broken_import(monkeypatch):
    """让 `from server.component_manifest import load_manifests` 抛 ImportError。

    sys.modules 里置 None 是标准的"导入即失败"桩；两个键都要置，否则同目录里
    先跑过的用例已经把子模块缓存住，本用例就模拟不出故障了（实测顺序依赖）。
    """
    monkeypatch.setitem(sys.modules, "server", None)
    monkeypatch.setitem(sys.modules, "server.component_manifest", None)


def test_import_failure_refuses_and_leaves_file(index_file, broken_import):
    """server 包不可导入（真实故障形态）→ 抛错且 _index.md 一个字节没动。"""
    with pytest.raises(generate_index.ManifestLoadError):
        generate_index.generate()

    assert index_file.read_text(encoding="utf-8") == SAMPLE


def test_zero_entries_with_existing_section_refuses(index_file, monkeypatch):
    """加载"成功"但 0 条，而已有段里 2 条 → 同样判失败，不清空。"""
    monkeypatch.setattr(generate_index, "_collect_component_entries", lambda: [])

    with pytest.raises(generate_index.ManifestLoadError):
        generate_index.generate()

    assert index_file.read_text(encoding="utf-8") == SAMPLE


def test_allow_empty_is_the_only_way_to_wipe(index_file, monkeypatch):
    """显式承认组件为零时，才允许写空段。"""
    monkeypatch.setattr(generate_index, "_collect_component_entries", lambda: [])

    report = generate_index.generate(allow_empty=True)

    assert report["written"] is True
    body = index_file.read_text(encoding="utf-8")
    assert "### alpha" not in body
    assert "暂无组件化模块" in body


def test_dry_run_still_refuses(index_file, monkeypatch):
    """dry-run 不写文件，但也不该把失败伪装成"0 个组件"的正常输出。"""
    monkeypatch.setattr(generate_index, "_collect_component_entries", lambda: [])

    with pytest.raises(generate_index.ManifestLoadError):
        generate_index.generate(dry_run=True)


def test_main_exits_1_without_traceback(index_file, broken_import, monkeypatch, capsys):
    """CLI 层：退出码 1（让 CI/闸门变红），消息里给出正确姿势。"""
    monkeypatch.setattr(sys, "argv", ["generate_index"])

    rc = generate_index.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "拒绝" in err and "tools.generate_index" in err
    assert index_file.read_text(encoding="utf-8") == SAMPLE


def test_main_json_reports_error(index_file, monkeypatch, capsys):
    """--json 形态（CI 友好）也要带 error 字段，而不是 entries=0 的绿报。"""
    monkeypatch.setattr(generate_index, "_collect_component_entries", lambda: [])
    monkeypatch.setattr(sys, "argv", ["generate_index", "--json"])

    rc = generate_index.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert '"error"' in out
