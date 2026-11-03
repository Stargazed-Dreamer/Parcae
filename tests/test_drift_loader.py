"""drift_detector 组件加载器的回归测试。

锁定 2026-09-23 修掉的**假 HIGH**：`import_module_from_path` 原用
`spec_from_file_location` 按文件路径加载，模块内只要有相对导入
（`from .sibling import x`）就必然 `ImportError: attempted relative import
with no known parent package` —— 于是 `workspace/email_source/loop_actions.py`
被误报成"模块加载失败（真实断链）"，并连带把它的 GUIDE_REGISTRY_ENTRIES 检查
也打成假 HIGH（一个 bug 报两条）。现在改为以父包身份导入。

同时锁定不能"一律降级掩盖"：真实的 ModuleNotFoundError 必须仍然是 HIGH。
"""
import sys
import uuid
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools" / "audit"
sys.path.insert(0, str(TOOLS_DIR))

import drift_detector as dd  # noqa: E402


def _make_pkg(files: dict[str, str]) -> Path:
    """在 ROOT/temp/tests 下造一个临时包（必须在 ROOT 内：加载器用 relative_to(ROOT) 报错）。

    统一落在 temp/tests/（测试专属 scratch 区），不再散在 temp/ 根目录。
    """
    tests_dir = dd.ROOT / "temp" / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    pkg = tests_dir / f"_drift_loader_{uuid.uuid4().hex[:8]}"
    pkg.mkdir(parents=True, exist_ok=True)
    for fn, content in files.items():
        (pkg / fn).write_text(content, encoding="utf-8")
    return pkg


def _clean(pkg: Path) -> None:
    """删掉临时包，**含 import 时生成的 `__pycache__`**（项目审批规则拦 shutil.rmtree，故手写）。

    旧实现只删顶层文件，`__pycache__` 让 `pkg.rmdir()` 必然抛 OSError，又被 `except: pass` 吞掉，
    于是每轮全量在 temp/ 漏下 2 个"看着空、其实装着 pyc"的目录。这里不吞错：删不干净就该红。
    """
    for f in sorted(pkg.rglob("*"), reverse=True):   # 倒序 = 先子后父，深度优先目录能被删
        if f.is_file():
            f.unlink()
        else:
            f.rmdir()
    pkg.rmdir()


def test_relative_import_module_loads_without_findings():
    """带相对导入的模块必须加载成功，且不产生任何 D 节发现（修复前的假 HIGH）。"""
    pkg = _make_pkg({"sibling.py": "V = 42\n", "entry.py": "from .sibling import V\n"})
    try:
        dd.FINDINGS.clear()
        dd.REPORT_LINES.clear()
        mod = dd.import_module_from_path("probe", pkg / "entry.py")
        assert mod is not None, f"相对导入模块应加载成功，实得发现：{dd.FINDINGS.get('D')}"
        assert mod.V == 42, "应真正执行了模块体并拿到兄弟模块内容"
        assert not dd.FINDINGS.get("D"), f"不该报任何 D 节发现：{dd.FINDINGS.get('D')}"
    finally:
        _clean(pkg)


def test_missing_module_still_reported_as_high():
    """组件 import 了不存在的包 = 真实断链，必须仍是 HIGH（不能被降级掩盖）。"""
    pkg = _make_pkg({"broken.py": f"import no_such_module_{uuid.uuid4().hex[:8]}\n"})
    try:
        dd.FINDINGS.clear()
        dd.REPORT_LINES.clear()
        mod = dd.import_module_from_path("probe", pkg / "broken.py")
        assert mod is None
        findings = dd.FINDINGS.get("D", [])
        assert len(findings) == 1, findings
        assert findings[0].startswith("[HIGH]"), f"真实断链必须是 HIGH：{findings}"
        assert "真实断链" in findings[0]
    finally:
        _clean(pkg)


def test_missing_file_reported_as_high():
    """路径不是文件 → HIGH（旧实现在 spec 为 None 时静默返回，会漏报）。"""
    pkg = _make_pkg({})
    try:
        dd.FINDINGS.clear()
        dd.REPORT_LINES.clear()
        mod = dd.import_module_from_path("probe", pkg / "not_exist.py")
        assert mod is None
        findings = dd.FINDINGS.get("D", [])
        assert len(findings) == 1 and findings[0].startswith("[HIGH]"), findings
        assert "不存在" in findings[0]
    finally:
        _clean(pkg)
