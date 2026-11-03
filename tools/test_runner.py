#!/usr/bin/env python3
"""共享测试运行器：流式 tee 输出 + JUnit XML 解析 + 崩溃检测 + 报告生成。

被 tools/run_tests_collect.py 和 tests/run_all.py 共用，消除"查不到错误"的根因：

1. 流式 tee：subprocess.Popen 实时读 stdout，边写 log 文件边 echo 控制台。
   跑测试时能实时看到进度和失败，跑完有完整 log（替代 capture_output=True 黑盒）。
2. JUnit XML：--junit-xml 结构化输出，解析 XML 而非文本（pytest-xdist 并行时
   文本输出顺序乱，正则解析易漏；XML 是结构化真源）。
3. 崩溃检测：检查 returncode，非 0/1/2/3/4/5 时标记"进程崩溃"并把 Windows 异常码
   翻成人话（如 0xC0000409 = __fastfail，abort()/Qt qFatal 走它；0xC0000005 =
   ACCESS_VIOLATION），不假装"0 失败"假绿。
4. 崩溃现场：XML 是 pytest 收尾时才写的，硬崩就没有 XML。此时把**日志末尾**和
   "最后停在哪个测试"直接摊进报告/控制台——否则报告里只剩一个裸退出码。
5. 报告生成：解析 XML 生成 test_failure_report.md，含崩溃状态 + 失败表格。

退出码分类（pytest 官方 + Windows 崩溃）：
  0  = 全过
  1  = 有失败
  2  = 中断（KeyboardInterrupt）
  3  = 内部错误
  4  = 用法错误
  5  = 没收集到测试
  其他 = 进程崩溃（STATUS_ACCESS_VIOLATION 等致命错误，XML 可能不完整或缺失）

详见 docs/dev-workflow.md "测试运行与问题定位流程" 段。
"""
import re
import subprocess
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / "temp" / "test_full_log.txt"
XML_FILE = PROJECT_ROOT / "temp" / "test_results.xml"
REPORT_FILE = PROJECT_ROOT / "temp" / "test_failure_report.md"

# 排除需要外部资源的 marker，只跑纯单元测试
# （e2e/gui/migration 也排除：它们依赖 CDP/后端/GUI 渲染或真实数据迁移，
#   在 quick 层跑会因环境不满足而连坐失败；full 层不传 markers 自然全跑）
QUICK_MARKERS = (
    "not gpu and not browser and not real_backend and not network "
    "and not chaos and not e2e and not gui and not migration"
)


IHAVEREAD_FLAG = "--ihaveread"

# 闸门提示语只说"去读手册"——不点名是哪一份手册、也不点名缺哪个参数。
# AGENTS.md「测试」节已经写明测试手册是哪一份，照着做即可；说破了就退化成"照抄参数"。
IHAVEREAD_REFUSAL = "拒绝执行：请先阅读测试手册，按手册里的测试使用方法补齐参数后再运行。"


def require_ihaveread(argv: list[str]) -> int | None:
    """测试入口的「读过手册」闸门：不带 --ihaveread 就拒绝执行。

    返回 None = 放行；返回非 0 = 调用方直接 return 该退出码。
    ⚠️ 这不是 pytest 参数：调用方必须把它从下传给 pytest 的参数里剔除。
    """
    if IHAVEREAD_FLAG in argv:
        return None
    print("❌ " + IHAVEREAD_REFUSAL, file=sys.stderr)
    return 2


# Windows 致命异常码（低 32 位）→ 人话。
# ⚠️ 0xC0000409 名字叫 STACK_BUFFER_OVERRUN，实践中它就是 `__fastfail` 的通用码：
# MSVC 的 abort()/terminate()、Qt 的 qFatal() 都走它。进程被内核直接终止，
# Python 层不会有 traceback（faulthandler 也来不及打印）——所以崩溃现场只能靠日志末尾。
WINDOWS_FATAL_CODES = {
    0xC0000409: "0xC0000409 __fastfail（abort()/Qt qFatal 一类快速失败，无 Python traceback）",
    0xC0000602: "0xC0000602 FAIL_FAST_EXCEPTION（RaiseFailFastException）",
    0xC0000005: "0xC0000005 ACCESS_VIOLATION（野指针/越界访问）",
    0xC0000374: "0xC0000374 HEAP_CORRUPTION（堆被写坏）",
    0xC00000FD: "0xC00000FD STACK_OVERFLOW（栈溢出）",
    0x80000003: "0x80000003 BREAKPOINT（断言/断点）",
    0xC000001D: "0xC000001D ILLEGAL_INSTRUCTION（非法指令）",
    0xC0000135: "0xC0000135 DLL_NOT_FOUND（缺 DLL）",
    0xC0000139: "0xC0000139 ENTRYPOINT_NOT_FOUND（DLL 入口缺失）",
}


def classify_exit(rc: int) -> tuple[str, str]:
    """分类 pytest 退出码。

    返回 (status, detail)：
    - passed: 全过
    - failed: 有失败（正常失败，XML 应完整）
    - pytest_error_N: pytest 自身错误（中断/内部/用法/无测试）
    - crashed_N: 进程崩溃（致命错误，XML 可能不完整或缺失，禁止假装"0 失败"）
    """
    if rc == 0:
        return "passed", "全部通过"
    if rc == 1:
        return "failed", "有测试失败"
    error_map = {
        2: "中断（KeyboardInterrupt 或 collection 错误）",
        3: "内部错误",
        4: "用法错误",
        5: "没收集到测试",
    }
    if rc in error_map:
        return f"pytest_error_{rc}", error_map[rc]
    # 非 pytest 退出码 = Windows 致命异常。翻成人话，别再写"可能是 ACCESS_VIOLATION"
    # ——0xC0000409 是最常遇到的那个，而它的名字（STACK_BUFFER_OVERRUN）也具有误导性。
    code = rc & 0xFFFFFFFF   # 有的调用方传负数形式，统一按低 32 位查表
    known = WINDOWS_FATAL_CODES.get(code)
    if known:
        return f"crashed_{rc}", f"进程崩溃（exit={rc} = {known}）"
    return f"crashed_{rc}", (f"进程崩溃（exit={rc} = 0x{code:08X}，不是 pytest 退出码；"
                             f"看日志末尾停在哪个测试）")


LOG_TAIL_LINES = 25        # 报告里摊几行日志末尾
LOG_TAIL_BYTES = 16 * 1024  # 只读文件尾这么多字节，不整读（红批日志可能几十 MB）

# -vv 下 pytest 一行一条：`tests/x.py::TestY::test_z PASSED [ 12%]`
# 崩溃时那一行没有结果后缀 —— 它就是把进程带崩的测试。
_NODEID_RE = re.compile(r"^\s*(\S+\.py::\S+)")
# 默认 verbosity 只有文件级进度：`tests/x/test_y.py ..F`
_FILEPROG_RE = re.compile(r"^\s*(\S+\.py)\s+[.sFEx]{2,}")


def tail_lines(path: Path, limit: int = LOG_TAIL_LINES) -> list[str]:
    """取日志末尾若干非空行（只读尾部 16KB，不整读）。

    存在的理由：JUnit XML 是 pytest `sessionfinish` 才写的，进程硬崩时根本没有 XML，
    报告里就只剩一个裸退出码。把日志末尾摊出来，才不用人去开文件猜死在哪。
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - LOG_TAIL_BYTES)
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return []
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if start and lines:
        lines = lines[1:]   # 从中间切进来的第一行是半截
    return lines[-limit:]


def last_test_line(lines: list[str]) -> str:
    """从日志末尾找"最后停在哪条测试"。

    优先 nodeid（`-vv` 一行一条，崩溃时最后一条是没有结果的半行）；退回文件级进度行
    （默认 verbosity 的 `tests/x/test_y.py ..F`）。都没有就返回空串，不瞎猜。
    """
    for ln in reversed(lines):
        m = _NODEID_RE.match(ln)
        if m:
            return m.group(1)
    for ln in reversed(lines):
        m = _FILEPROG_RE.match(ln)
        if m:
            return m.group(1)
    return ""


def parse_junit_xml(xml_path: Path) -> tuple[dict, list[dict]] | tuple[None, list]:
    """解析 JUnit XML，返回 (summary, failures)。

    summary: {tests, failures, errors, skipped, passed, time}
    failures: [{name, classname, file, message, type}, ...]

    XML 不存在或解析失败时返回 (None, [])，调用方应据此标记"XML 未生成"。
    """
    if not xml_path.exists():
        return None, []

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        return None, [{"parse_error": str(e)}]

    root = tree.getroot()
    # pytest junit-xml: <testsuites><testsuite ...><testcase ...><failure/></testcase></testsuite></testsuites>
    # 兼容 root 是 <testsuite> 或 <testsuites>
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    total = failures = errors = skipped = 0
    total_time = 0.0
    failure_list: list[dict] = []

    for suite in suites:
        total += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        try:
            total_time += float(suite.get("time", 0))
        except (ValueError, TypeError):
            pass

        for tc in suite.findall("testcase"):
            # failure 节点 = 测试断言失败
            fail_node = tc.find("failure")
            err_node = tc.find("error")
            if fail_node is not None or err_node is not None:
                node = fail_node if fail_node is not None else err_node
                assert node is not None  # 由上方 or 分支保证（pyright Optional 收窄）
                kind = "failure" if fail_node is not None else "error"
                name = tc.get("name", "<unknown>")
                classname = tc.get("classname", "")
                file = tc.get("file", "")
                message = node.get("message", "") or (node.text or "").strip()
                # 截断长 message
                if len(message) > 200:
                    message = message[:197] + "..."
                failure_list.append({
                    "kind": kind,
                    "name": name,
                    "classname": classname,
                    "file": file,
                    "message": message,
                    "type": node.get("type", ""),
                })

    passed = total - failures - errors - skipped
    return {
        "tests": total,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "passed": passed,
        "time": total_time,
    }, failure_list


def generate_report(
    xml_path: Path,
    rc: int,
    elapsed: float,
    log_path: Path,
    report_path: Path,
    cmd: list[str],
) -> str:
    """生成 test_failure_report.md，返回报告内容。"""
    status, detail = classify_exit(rc)
    summary, failures = parse_junit_xml(xml_path)

    report = ["# 测试报告\n"]
    report.append("## 运行状态\n")
    report.append(f"- **状态**：`{status}` — {detail}")
    report.append(f"- **退出码**：{rc}")
    report.append(f"- **耗时**：{elapsed:.1f}s")
    report.append(f"- **命令**：`{' '.join(cmd)}`\n")

    if summary is None:
        report.append("## 结果摘要\n")
        report.append("> JUnit XML 未生成或解析失败（进程崩溃或被中断）。")
        report.append("> 无法解析结构化结果，**不能假设\"0 失败\"**。")
        report.append(f"> 完整输出见 `{log_path.relative_to(PROJECT_ROOT)}`。\n")
        # 别把"自己去看 log"当交付：XML 缺失时报告里本来什么都没有，这里直接摊现场。
        tail = tail_lines(log_path)
        at = last_test_line(tail)
        if at or tail:
            report.append("## 崩溃现场\n")
            if at:
                report.append(f"- 最后停在：`{at}`（XML 是 pytest 收尾才写的，"
                              f"硬崩时拿不到计数）")
            if tail:
                report.append(f"\n### 日志末尾（末 {len(tail)} 行）\n")
                report.append("```")
                report.extend(tail)
                report.append("```\n")
    else:
        report.append("## 结果摘要\n")
        report.append(
            f"```\n{summary['passed']} passed, {summary['failures']} failed, "
            f"{summary['errors']} errors, {summary['skipped']} skipped, "
            f"{summary['tests']} total in {summary['time']:.1f}s\n```"
        )

        report.append(f"\n## 失败/错误测试 ({len(failures)} 个)\n")
        if failures:
            report.append("| # | 类型 | 测试 | 错误摘要 |")
            report.append("|---|------|------|----------|")
            for i, f in enumerate(failures, 1):
                # 测试全名：classname::name（classname 含模块路径）
                full = f"{f['classname']}::{f['name']}" if f["classname"] else f["name"]
                msg = f["message"] if f["message"] else f["type"] or "(无消息)"
                # 转义 markdown 表格分隔符
                msg = msg.replace("|", "\\|").replace("\n", " ")
                report.append(f"| {i} | {f['kind']} | `{full}` | {msg} |")
        else:
            if status == "passed":
                report.append("（无失败）")
            else:
                report.append(
                    f"> 状态={status} 但 XML 解析出 0 失败。"
                    f"可能是崩溃发生在 pytest 汇总阶段（conftest.py:95-122 已知问题），"
                    f"XML 未记录崩溃。完整输出见 log。"
                )

    report.append(f"\n## 完整 log\n见 `{log_path.relative_to(PROJECT_ROOT)}`\n")
    if xml_path.exists():
        report.append(f"## JUnit XML\n见 `{xml_path.relative_to(PROJECT_ROOT)}`\n")

    # 失败子集复现：让 agent 不必整轮重跑就能验证修复。
    # 按 file 而非 nodeid 聚合——junit XML 的 classname 是点分模块路径，参数化测试的
    # name 带 [params]，反向拼 nodeid 不可靠；文件路径属性稳定且足够定位。
    if failures:
        files = []
        for f in failures:
            fp = f.get("file", "")
            if fp and fp not in files:
                files.append(fp)
        if files:
            file_args = " ".join(files)
            report.append("## 失败子集复现（验证修复先跑这些，别整轮重跑）\n")
            report.append("```powershell")
            report.append(f".venv\\Scripts\\python.exe -m pytest {file_args} --tb=short")
            report.append("```")
            report.append("\n或只重跑上次失败的测试（依赖 .pytest_cache）：`pytest --lf --tb=short`")
            report.append("全部通过后，再跑一次整轮 quick 收尾。\n")

    report.append("\n## 分类建议\n")
    report.append("- **真实 bug**：单独跑该测试文件也失败 → 改实现代码")
    report.append("- **测试隔离问题**：单独跑通过，全量跑失败 → 改测试（加 cleanup/隔离 fixture）或标记已知问题")
    report.append("- **环境问题**：缺依赖/权限/路径 → 修环境")
    report.append("- **过时测试**：测试期望的行为已不存在 → 删/改测试")
    report.append("- **进程崩溃**：returncode 非 0-5，多为 Qt access violation → 看 log 末尾，分段跑定位\n")
    report.append("单独跑确认：")
    report.append("```powershell")
    report.append(".venv\\Scripts\\python.exe -m pytest tests/test_xxx.py -v --tb=short")
    report.append("```\n")

    content = "\n".join(report)
    report_path.write_text(content, encoding="utf-8")
    return content


def _print_failure_list(
    summary: dict | None,
    failures: list[dict],
    status: str,
    xml_path: Path,
    report_path: Path,
    log_path: Path | None = None,
) -> None:
    """把完整失败/错误清单打印到 console（默认强制，调用方显式关才不打印）。

    背景（2026-09-03）：失败表格原本只写进 test_failure_report.md，控制台只打一行
    [result] 计数。agent 若不主动读报告文件，就拿不到完整 error 清单，只能反复
    翻 log / 重跑，形成"测→修→再测"循环。此函数把同一份 JUnit XML 驱动的清单以
    紧凑文本打上 stdout，一行一个失败（测试全名 + 错误摘要），不依赖 pytest 的
    文本输出（-q dots 进度会淹没在长输出里），也不依赖 agent 主动读文件。

    summary=None（XML 缺失/解析失败）时给出显式提示，不假装"0 失败"。
    """
    if summary is None:
        if failures and "parse_error" in failures[0]:
            print(
                f"[list] XML 解析失败：{failures[0]['parse_error']}，"
                "无法生成结构化失败清单；完整输出见 log 末尾",
                flush=True,
            )
        else:
            print(
                "[list] XML 未生成（进程崩溃或被中断），无法生成结构化失败清单；"
                "下面是日志末尾",
                flush=True,
            )
        # 控制台也把现场摊出来：调用方常常只看 stdout，不读报告文件。
        if log_path is not None:
            tail = tail_lines(log_path, limit=15)
            at = last_test_line(tail)
            if at:
                print(f"[list] 最后停在：{at}", flush=True)
            for ln in tail:
                print(f"[list]   {ln}", flush=True)
        return

    print(f"[list] ===== 失败/错误清单（共 {len(failures)} 个，来自 JUnit XML）=====", flush=True)
    for i, f in enumerate(failures, 1):
        full = f"{f['classname']}::{f['name']}" if f["classname"] else f["name"]
        msg = (f["message"] or f["type"] or "(无消息)").replace("\n", " ")
        print(f"[list] {i:>3}. [{f['kind']}] {full} — {msg}", flush=True)
    if failures:
        print("[list] ===== 清单结束 =====", flush=True)
        print(
            f"[list] 完整表格 + 每个失败的单独复现命令见 {report_path.relative_to(PROJECT_ROOT)}。"
            "修复后先跑失败子集/`pytest --lf` 验证，全部通过再整轮收尾。",
            flush=True,
        )
    elif status == "passed":
        print("[list] （无失败）", flush=True)
    else:
        print(
            f"[list] 状态={status} 但 XML 解析出 0 失败：崩溃可能发生在 pytest 汇总阶段"
            f"（conftest.py:95-122 已知问题），XML 未记录崩溃，见 log 末尾。",
            flush=True,
        )


def run_pytest_streaming(
    cmd: list[str],
    log_path: Path = LOG_FILE,
    xml_path: Path | None = None,
    cwd: Path = PROJECT_ROOT,
) -> tuple[int, float]:
    """流式 tee 运行 pytest。

    - subprocess.Popen + stdout=PIPE + stderr=STDOUT（合并）
    - 实时逐行读：边写 log 文件边 echo 控制台（用户能看到进度和失败）
    - 不用 capture_output=True（黑盒），不用 timeout=600（卡死等 10 分钟）

    返回 (returncode, elapsed_seconds)。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 清空旧 log（避免上次残留混淆）
    log_path.write_text("", encoding="utf-8")

    print(f"[run] {' '.join(cmd)}", flush=True)
    print(f"[run] log -> {log_path}", flush=True)
    if xml_path:
        print(f"[run] xml -> {xml_path}", flush=True)
    print("[run] 实时输出中（同时写 log 文件）...\n", flush=True)

    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # 合并 stderr 到 stdout，避免顺序错乱
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # 行缓冲，实时输出
    )
    assert proc.stdout is not None  # stdout=PIPE 时必有（pyright Optional 收窄）

    # 主线程逐行读（Popen.stdout.readline 阻塞，读到 EOF 返回 ""）
    with open(log_path, "a", encoding="utf-8") as log_f:
        for line in iter(proc.stdout.readline, ""):
            log_f.write(line)
            sys.stdout.write(line)
            sys.stdout.flush()

    proc.stdout.close()
    proc.wait()
    elapsed = time.monotonic() - start
    print(f"\n[done] exit={proc.returncode}, elapsed={elapsed:.1f}s", flush=True)
    return proc.returncode, elapsed


def build_pytest_cmd(
    extra_args: list[str] | None = None,
    markers: str | None = None,
    xml_path: Path | None = XML_FILE,
    tb_mode: str = "line",
    python_exe: str | None = None,
    include_workspace_tests: bool = True,
) -> list[str]:
    """构造 pytest 命令。

    - markers: -m 表达式（如 QUICK_MARKERS），None 表示不筛选
    - xml_path: --junit-xml 路径，None 表示不生成 XML
    - tb_mode: --tb 模式（line/short/long），默认 line（全量收集最简）
    - python_exe: python 路径，None 用 sys.executable
    - include_workspace_tests: 追加各组件 manifest.toml [tests] 段声明的测试目录
      （lib.component_manifest.collect_test_dirs()）。收集失败不阻断主套件。

    注意：不在此加 -q/-ra/--maxfail/--timeout 等——这些走 pyproject.toml addopts，
    避免重复。脚本层只补 --tb（覆盖 addopts 的 --tb=short）和 --junit-xml。

    `-u`（子进程 stdout 无缓冲）是**崩溃现场的唯一保障**：默认块缓冲下进程被硬崩
    （Qt qFatal / abort）时，缓冲区里最后几 KB 输出根本不落盘，日志只剩半截。
    2026-10-24 实测同一崩溃命令：不加 -u 日志只留 2 字节，加了能留到崩溃那条测试。
    """
    if python_exe is None:
        python_exe = sys.executable
    cmd = [python_exe, "-u", "-m", "pytest", "tests/"]
    if include_workspace_tests:
        try:
            # 直接以脚本方式跑（如 python tests/run_all.py）时项目根不在 sys.path
            if str(PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))
            from lib.component_manifest import collect_test_dirs

            workspace_dirs = collect_test_dirs()
            if workspace_dirs:
                cmd.extend(workspace_dirs)
                print(
                    f"[test_runner] workspace tests: {len(workspace_dirs)} dirs "
                    f"(from manifest.toml [tests])",
                    flush=True,
                )
        except Exception as e:
            # manifest 系统不可用时不阻断主测试套件
            print(f"[test_runner][WARN] collect workspace test dirs failed: {e}", flush=True)
    if markers:
        cmd.extend(["-m", markers])
    cmd.extend(["--tb", tb_mode])
    if xml_path:
        cmd.extend(["--junit-xml", str(xml_path)])
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def run_and_report(
    cmd: list[str],
    log_path: Path = LOG_FILE,
    xml_path: Path = XML_FILE,
    report_path: Path = REPORT_FILE,
    cwd: Path = PROJECT_ROOT,
    show_failure_list: bool = True,
) -> int:
    """完整流程：流式运行 → 解析 XML → 生成报告 → 打印失败清单 → 返回退出码。

    show_failure_list（默认 True）：跑完总是把完整失败清单打印到 console。
    只有调用方显式传 False（入口脚本 --no-list）才关闭——这是"一次拿全量失败"
    的机械防线，禁止默认关闭（agent 不读 report 文件就拿不到清单，会陷入
    "测→修→再测"循环）。
    """
    rc, elapsed = run_pytest_streaming(cmd, log_path, xml_path, cwd)
    generate_report(xml_path, rc, elapsed, log_path, report_path, cmd)   # 报告由它自己写盘
    print(f"[done] report -> {report_path}", flush=True)
    # 打印报告关键摘要到控制台
    status, detail = classify_exit(rc)
    summary, failures = parse_junit_xml(xml_path)
    print(f"[result] status={status} ({detail})", flush=True)
    if summary:
        print(
            f"[result] {summary['passed']} passed, {summary['failures']} failed, "
            f"{summary['errors']} errors, {summary['skipped']} skipped",
            flush=True,
        )
    elif failures and "parse_error" in failures[0]:
        print(f"[result] XML 解析失败：{failures[0]['parse_error']}", flush=True)
    else:
        print("[result] XML 未生成（进程崩溃或被中断），见 log 末尾", flush=True)
    if show_failure_list:
        _print_failure_list(summary, failures, status, xml_path, report_path, log_path)
    return rc

