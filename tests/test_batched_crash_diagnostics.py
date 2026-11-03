"""分批器/运行器的崩溃现场诊断（quick 层）。

背景（2026-10-24）：`tests/client_ui` 合跑硬崩 0xC0000409，分批器只报
`crashed_3221226505` + 一句"可能是 STATUS_ACCESS_VIOLATION"，日志里只剩 2 字节
（pytest stdout 是块缓冲，最后几 KB 随进程一起没了）。这三件事一起让"死在哪"无从查起。

这里钉住修完的四条：
1. 崩溃退出码要翻成正确的 Windows 异常名（0xC0000409 不是 ACCESS_VIOLATION）；
2. 日志末尾要取得到（tail_lines）；
3. 崩溃日志末行能定位到具体测试（last_test_line）；
4. 报告里真的把这些写出来了（write_report 的渲染结果，不是只看函数返回值）。
"""
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import run_tests_batched as B  # noqa: E402
import test_runner as T  # noqa: E402

RC_FASTFAIL = 3221226505       # 0xC0000409
RC_ACCESS_VIOLATION = 3221225477  # 0xC0000005


# --- 退出码分类 ---

def test_fastfail_is_not_misreported_as_access_violation():
    status, detail = T.classify_exit(RC_FASTFAIL)
    assert status == f"crashed_{RC_FASTFAIL}"
    assert "0xC0000409" in detail
    assert "__fastfail" in detail
    # 旧文案把任何未知崩溃都推给 ACCESS_VIOLATION，这条是防它退回去
    assert "ACCESS_VIOLATION" not in detail


def test_access_violation_still_named_correctly():
    _, detail = T.classify_exit(RC_ACCESS_VIOLATION)
    assert "0xC0000005" in detail and "ACCESS_VIOLATION" in detail


def test_unknown_fatal_code_keeps_hex():
    _, detail = T.classify_exit(0xC0009999)
    assert "0xC0009999" in detail, "未知码至少要给出十六进制，否则没法拿去搜"


def test_pytest_exit_codes_still_classified():
    assert T.classify_exit(0) == ("passed", "全部通过")
    assert T.classify_exit(1)[0] == "failed"
    assert T.classify_exit(5)[0] == "pytest_error_5"


def test_build_pytest_cmd_runs_child_unbuffered():
    """`-u` 不是可选项：少了它，崩溃时最后几 KB 输出随缓冲一起丢。"""
    cmd = T.build_pytest_cmd(include_workspace_tests=False)
    assert cmd[1] == "-u", f"pytest 子进程必须无缓冲: {cmd[:4]}"


# --- 日志末尾 ---

def test_tail_lines_returns_last_n(tmp_path):
    p = tmp_path / "batch.log"
    p.write_text("\n".join(f"line-{i}" for i in range(100)) + "\n", encoding="utf-8")
    got = T.tail_lines(p, limit=5)
    assert got == [f"line-{i}" for i in range(95, 100)]


def test_tail_lines_missing_file_is_empty(tmp_path):
    assert T.tail_lines(tmp_path / "nope.log") == []


def test_tail_lines_drops_partial_first_line_when_seeking(tmp_path):
    """从文件中部切进来时首行是半截——不能把它当成完整行报出去。"""
    p = tmp_path / "big.log"
    filler = "x" * 200
    lines = [f"{filler}-{i}" for i in range(400)]   # 远超 16KB
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    got = T.tail_lines(p, limit=400)
    assert got, "长文件也应能取到末尾"
    assert all(len(ln) == len(filler) + 1 + len(str(400 - 1)) or ln.startswith(filler + "-")
               for ln in got), f"首行可能是半截: {got[0][:40]!r}"


# --- 定位崩在哪个测试 ---

def test_last_test_line_prefers_nodeid_of_crashing_test():
    lines = [
        "collecting ... collected 4 items",
        "tests/x/test_a.py::test_one PASSED   [ 25%]",
        "tests/x/test_a.py::test_two PASSED   [ 50%]",
        "tests/x/test_a.py::test_three ",      # 崩溃：这行没有结果后缀
    ]
    assert T.last_test_line(lines) == "tests/x/test_a.py::test_three"


def test_last_test_line_falls_back_to_file_progress():
    lines = ["tests/x/test_a.py ..F", "  ", ""]
    assert T.last_test_line(lines) == "tests/x/test_a.py"


def test_last_test_line_empty_when_nothing_looks_like_test():
    assert T.last_test_line(["random log line", "another"]) == ""


# --- 报告渲染：崩溃批要把现场写进 SUMMARY.md ---

def _crash_result(tail: list[str], at: str) -> dict:
    return {
        "label": "tests/demo", "rc": RC_FASTFAIL,
        "status": f"crashed_{RC_FASTFAIL}", "detail": T.classify_exit(RC_FASTFAIL)[1],
        "seconds": 3.0, "waited": 0.0, "avail_before": 2.0, "avail_after": 1.8,
        "mem_warn": None, "summary": None, "failures": [],
        "log": "temp/batch_tests/tests_demo.log", "tail": tail, "at": at, "slow": False,
    }


def test_report_includes_crash_location_and_log_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    monkeypatch.setattr(B, "MEM_LOG", tmp_path / "mem_min.log")
    tail = ["collected 4 items", "tests/demo/test_a.py::test_boom "]
    out = B.write_report([_crash_result(tail, "tests/demo/test_a.py::test_boom")],
                         None, 1, partial=True)
    text = out.read_text(encoding="utf-8")
    assert "__fastfail" in text, "退出码要翻成人话"
    assert "最后停在" in text and "tests/demo/test_a.py::test_boom" in text
    assert "tests/demo/test_a.py::test_boom " in text, "日志末尾要真的贴出来"
    assert "--only tests/demo" in text, "要给出单批复现命令"


def test_normal_failure_report_skips_log_tail(tmp_path, monkeypatch):
    """有 XML 的普通失败：失败清单已够，再贴日志末尾是噪声。"""
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    monkeypatch.setattr(B, "MEM_LOG", tmp_path / "mem_min.log")
    r = {
        "label": "tests/demo", "rc": 1, "status": "failed", "detail": "有测试失败",
        "seconds": 3.0, "waited": 0.0, "avail_before": 2.0, "avail_after": 1.8,
        "mem_warn": None,
        "summary": {"tests": 2, "passed": 1, "failures": 1, "errors": 0, "skipped": 0,
                    "time": 0.1},
        "failures": [{"classname": "tests.demo.test_a", "name": "test_b",
                      "message": "AssertionError: 故意失败", "type": "AssertionError"}],
        "log": "temp/batch_tests/tests_demo.log", "tail": ["SENTINEL-TAIL"],
        "at": "tests/demo/test_a.py::test_b", "slow": False,
    }
    text = B.write_report([r], None, 1, partial=True).read_text(encoding="utf-8")
    assert "最后一条测试" in text          # 有 XML 时是"最后一条"，不是"最后停在"
    assert "SENTINEL-TAIL" not in text
