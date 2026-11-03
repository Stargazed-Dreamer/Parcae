"""组件可选模块的安全加载器（扩展点加载加固）。

问题背景
--------
``workspace/<component>/loop_actions.py`` 是**可选**扩展点，由
``server/activity_tracker/loop_manager.py``、``server/agent_guide.py``、
``server/activity_tracker/loop_actions.py`` 在**模块 import 期**动态加载，
而 ``server/main.py`` 顶层 import 这三个模块。原实现只捕获 ``ImportError``：

    except ImportError:
        continue

``ImportError`` 只表示「模块找不到」。``SyntaxError`` 是与之**并列**的
``Exception`` 子类，不在其继承链上，因此 workspace 里任意一个文件的语法错误
都会击穿加载器，让整个后端启动即退出。2026-10-20 实测：``chat_digest/
loop_actions.py`` 一个未转义的半角引号，导致 ``server/main.py:81`` 直接崩，
后端完全起不来。

本模块把「加载外部代码」收敛为唯一入口，四道防线：

1. **语法预检**：import 前先 ``compile()``，语法错零副作用拦下；
2. **子进程探测**（默认关闭，``PARCAE_COMPONENT_PROBE=1`` 开启）：带超时批量
   试 import，模块级阻塞（连网 / 等锁 / 加载模型）不会卡住主进程；结果按文件签名
   缓存。默认关的原因见 :func:`probe_enabled`（实测拖慢 20s 且误杀重依赖组件）；
3. **主进程兜底**：``except BaseException``（``KeyboardInterrupt`` 除外），
   加载期任何异常都只降级不上升；
4. **失败登记**：组件名 / 模块 / 类别 / 位置摘要，供启动日志与 ``/health`` 消费。

设计原则：**加载失败绝不上抛**。宁可少一个 skill，不可倒整个后端；但也绝不
静默 —— 每次降级都必须能从日志或 ``/health`` 查到原因与位置。

环境变量
--------
- ``PARCAE_COMPONENT_PROBE=0``：关闭子进程探测（只保留语法预检 + 兜底）；
- ``PARCAE_COMPONENT_PROBE_MODULE_TIMEOUT``：单个组件试 import 超时秒数（默认 5）。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

# lib/component_loader.py → parents[1] = Parcae/
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CACHE_PATH = _PROJECT_ROOT / "data" / "runtime" / "component_probe.json"

# 降级类别
KIND_SYNTAX = "syntax"          # 语法预检不过（静态可判，永远不会执行）
KIND_IMPORT = "import"          # 执行/导入期异常
KIND_TIMEOUT = "probe_timeout"  # 子进程试 import 超时（疑似模块级阻塞）

PROBE_ENV = "PARCAE_COMPONENT_PROBE"
PROBE_MODULE_TIMEOUT_ENV = "PARCAE_COMPONENT_PROBE_MODULE_TIMEOUT"
_DEFAULT_MODULE_TIMEOUT = 5.0   # 单个组件的试 import 上限（秒）

# 子进程里报这两个错，说明是「探测进程自己解析不到模块」（测试会 monkeypatch
# workspace.__path__、临时目录组件等），不代表组件坏 → 不可判定，放行给主进程定夺。
# 真依赖缺失时主进程 import 同样会抛，由兜底分支登记。
_UNDETERMINED_TYPES = frozenset({"ImportError", "ModuleNotFoundError"})


# ========== 失败登记 ==========

@dataclass(frozen=True)
class LoadFailure:
    """一次组件模块加载失败的记录。"""

    component: str          # 组件名（workspace/<component>）
    module: str             # 完整模块名（workspace.x.loop_actions）
    kind: str               # KIND_* 之一
    detail: str             # 一行摘要：异常类型 + 消息 + 文件:行
    path: str = ""          # 源文件路径（已知时）
    at: float = 0.0         # 记录时间戳

    def to_dict(self) -> dict:
        return {
            "component": self.component,
            "module": self.module,
            "kind": self.kind,
            "detail": self.detail,
            "path": self.path,
            "at": self.at,
        }


_lock = threading.RLock()
_failures: dict[str, LoadFailure] = {}


def _default_component(module_name: str) -> str:
    """从模块名推组件名：workspace.chat_digest.loop_actions → chat_digest"""
    parts = module_name.split(".")
    if len(parts) >= 3 and parts[0] == "workspace":
        return parts[1]
    return module_name


def record_failure(
    component: str | None,
    module: str,
    kind: str,
    detail: str,
    path: str | Path | None = None,
) -> LoadFailure:
    """登记一次加载失败（幂等：同模块覆盖为最新一次）。"""
    import time as _time

    failure = LoadFailure(
        component=component or _default_component(module),
        module=module,
        kind=kind,
        detail=(detail or "").strip() or kind,
        path=str(path) if path else "",
        at=_time.time(),
    )
    with _lock:
        _failures[module] = failure
    return failure


def _clear_failure(module: str) -> None:
    with _lock:
        _failures.pop(module, None)


def reset_failures() -> None:
    """清空失败登记（测试隔离用）。"""
    with _lock:
        _failures.clear()


def get_failures() -> list[dict]:
    """返回全部失败记录（按组件名排序，便于稳定输出）。"""
    with _lock:
        items = sorted(_failures.values(), key=lambda f: (f.component, f.module))
    return [f.to_dict() for f in items]


def has_failures() -> bool:
    with _lock:
        return bool(_failures)


def failure_summary_lines() -> list[str]:
    """一行一个失败组件，供启动日志与 /health 直接展示。"""
    return [
        f"- {f['component']} → {f['module']} [{f['kind']}] {f['detail']}"
        for f in get_failures()
    ]


def log_failures(prefix: str = "组件加载降级") -> int:
    """把当前失败清单打到日志（ERROR 级，避免被 INFO 淹没）。返回条数。"""
    items = get_failures()
    if not items:
        return 0
    logger.error(
        "%s：%d 个组件模块未能加载（后端仍可运行，对应 skill / loop 不可用）",
        prefix,
        len(items),
    )
    for line in failure_summary_lines():
        logger.error("  %s", line)
    logger.error("修复后需重启后端生效；详见 /health 的 component_load 段")
    return len(items)


# ========== 语法预检 ==========

def check_syntax(path: str | Path) -> str | None:
    """编译源码做语法预检。通过返回 None，否则返回一行错误摘要。

    用 ``compile(bytes)`` 而非 import：零副作用，代价约毫秒级，且能覆盖
    SyntaxError / IndentationError / TabError / 编码声明错误。
    """
    p = Path(path)
    try:
        source = p.read_bytes()
    except OSError as exc:
        return f"OSError: {exc}"
    try:
        compile(source, str(p), "exec")
    except SyntaxError as exc:
        return f"SyntaxError: {exc.msg} ({p.name}:{exc.lineno})"
    except (ValueError, UnicodeDecodeError) as exc:
        return f"{type(exc).__name__}: {exc} ({p.name})"
    return None


# ========== 子进程试 import（防模块级阻塞） ==========

# 探测脚本：逐个 import，任何异常只记录不抛出，最后一次性输出 JSON。
# 用 BaseException 兜住 SystemExit —— 组件里一个 sys.exit() 不该终止探测。
_PROBE_CODE = r"""
import contextlib, io, json, os, sys, threading
specs = json.loads(sys.stdin.read() or "{}")
per = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
real_stdout = sys.stdout
out = {}

def _run(module, box):
    buf = io.StringIO()
    try:
        # 组件模块级 print 不许污染本脚本的 JSON 输出
        with contextlib.redirect_stdout(buf):
            __import__(module)
        box[module] = None
    except BaseException as exc:
        msg = "%s: %s" % (type(exc).__name__, exc)
        lines = msg.splitlines()
        head = lines[0] if lines else type(exc).__name__
        box[module] = {"type": type(exc).__name__, "error": head[:300]}

# 每模块独立超时：一个模块卡住不影响同批其它模块（批次级总超时会把整批误判）
for module in specs:
    box = {}
    th = threading.Thread(target=_run, args=(module, box), daemon=True)
    th.start()
    th.join(per)
    if th.is_alive():
        out[module] = {
            "type": "ProbeTimeout",
            "error": "试 import 超过 %.0fs（疑似模块级阻塞）" % per,
        }
    else:
        out[module] = box.get(module)

real_stdout.write(json.dumps(out))
real_stdout.flush()
# 卡住的 daemon 线程不许拖住探测进程退出
os._exit(0)
"""

_verdicts: dict[str, str | None] = {}   # module → None(通过) / 错误摘要
_file_cache: dict[str, dict] | None = None


def probe_enabled() -> bool:
    """子进程试 import 探测是否开启。**默认关闭**，``PARCAE_COMPONENT_PROBE=1`` 开启。

    为什么默认关（2026-10-20 实测，本机 29 个 workspace 组件）：
    - 冷启动从 ~2s 涨到 **23.5s**（每个组件一个子进程试 import + 重依赖真实加载）；
    - 5s/模块的阈值会把 stock_advisor / email_source / gh_mirror /
      ue_learning_copilot 这类「import 就要拉 pandas / akshare」的重依赖组件
      误判成「疑似阻塞」而整体剔除 —— 误杀比漏防更糟。
    所以它只作为**排查用**的可选加固（怀疑某组件 import 卡死时临时开），
    常规启动只走语法预检 + 异常兜底，两者零成本且已能挡住本次事故。
    """
    return os.environ.get(PROBE_ENV, "0").strip().lower() in {"1", "true", "on", "yes"}


def probe_module_timeout() -> float:
    """单个组件的试 import 超时（秒），默认 5；``PARCAE_COMPONENT_PROBE_MODULE_TIMEOUT`` 可改。"""
    raw = os.environ.get(PROBE_MODULE_TIMEOUT_ENV, "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MODULE_TIMEOUT
    return value if value > 0 else _DEFAULT_MODULE_TIMEOUT


def _signature(component_dir: Path) -> str:
    """组件目录内 *.py 的 (文件名, mtime, 大小) 指纹，用于缓存失效判断。"""
    digest = hashlib.sha1()
    try:
        files = sorted(component_dir.glob("*.py"))
    except OSError:
        return "no-dir"
    for f in files[:200]:
        try:
            st = f.stat()
        except OSError:
            continue
        digest.update(f"{f.name}:{st.st_mtime_ns}:{st.st_size};".encode("utf-8", "replace"))
    return digest.hexdigest()


def _load_disk_cache() -> dict[str, dict]:
    global _file_cache
    if _file_cache is not None:
        return _file_cache
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        _file_cache = raw if isinstance(raw, dict) else {}
    except Exception:
        _file_cache = {}
    return _file_cache


def _save_disk_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps(_file_cache or {}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception as exc:  # 缓存写失败不影响加载
        logger.debug("component_loader 缓存写入失败: %s", exc)


def clear_probe_cache() -> None:
    """清空内存 + 磁盘探测缓存（改完组件、想强制重探时用）。"""
    global _file_cache
    with _lock:
        _verdicts.clear()
        _file_cache = None
    try:
        _CACHE_PATH.unlink()
    except OSError:
        pass


def _normalize_verdict(raw: object) -> str | None:
    """把探测子进程的返回归一成 None（放行）或一行错误摘要。

    ``ImportError`` / ``ModuleNotFoundError`` 只说明**探测进程**解析不到模块
    （测试会 monkeypatch workspace.__path__、组件也可能依赖运行期注入），不足以
    判定组件坏 → 放行，交给主进程 import 定夺（那里有兜底与登记）。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        etype = str(raw.get("type") or "")
        if etype in _UNDETERMINED_TYPES:
            return None
        detail = str(raw.get("error") or etype or "unknown")
        if etype == "ProbeTimeout":
            return f"probe_timeout: {detail}"
        return detail
    return str(raw)


def warm_probe(specs: dict[str, Path], *, timeout: float | None = None) -> dict[str, str | None]:
    """批量试 import（起**一个**子进程），返回 {module: None | 错误摘要}。

    调用点应在循环加载之前把整批 specs 一次传进来，避免每个模块起一次子进程。
    已缓存且文件未变的模块不会重跑 —— 组件模块的 import 副作用只发生一次
    （探测子进程一次 + 主进程一次，这是隔离阻塞的代价）。

    参数 ``specs``: {完整模块名: 组件入口源文件路径}。
    """
    if not probe_enabled():
        # 默认路径：不探测。语法预检已在 load_component_module 里单独做过，
        # 这里连签名计算都省掉，保证启动零额外成本。
        with _lock:
            for module in specs:
                _verdicts.setdefault(module, None)
            return {m: _verdicts.get(m) for m in specs}

    pending: dict[str, tuple[Path, str]] = {}
    disk = _load_disk_cache()
    with _lock:
        for module, path in specs.items():
            if module in _verdicts:
                continue
            src = Path(path)
            sig = _signature(src.parent)
            rec = disk.get(module)
            if isinstance(rec, dict) and rec.get("sig") == sig and "error" in rec:
                _verdicts[module] = rec["error"]
                continue
            pending[module] = (src, sig)

    if pending:
        per = timeout if timeout and timeout > 0 else probe_module_timeout()
        # 外层总超时 = 每模块上限 × 个数 + 解释器启动余量。
        # 探测进程整体挂死时**放行**（不判组件坏）：主进程 import 还有兜底，
        # 误杀一个正常组件比多等一会儿更糟。
        total = per * len(pending) + 60
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _PROBE_CODE, str(per)],
                input=json.dumps(list(pending), ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=total,
                cwd=str(_PROJECT_ROOT),
            )
            results = json.loads(proc.stdout or "{}")
            if not isinstance(results, dict):
                raise ValueError("probe 输出不是 dict")
        except subprocess.TimeoutExpired:
            logger.warning("组件试 import 探测整体超时（%.0fs），本批放行交主进程加载", total)
            results = dict.fromkeys(pending)
        except Exception as exc:
            # 探测自身失败（Python 起不来 / 输出损坏）→ 不放行阻塞，但也不冤枉组件：
            # 记为 None 让它走主进程 import（有 except BaseException 兜底）
            logger.warning("组件试 import 探测失败，降级为直接加载: %s", exc)
            results = dict.fromkeys(pending)

        cache = _load_disk_cache()
        with _lock:
            for module, (src, sig) in pending.items():
                verdict = _normalize_verdict(results.get(module))
                _verdicts[module] = verdict
                cache[module] = {"sig": sig, "error": verdict, "src": str(src)}
        _save_disk_cache()

    with _lock:
        return {m: _verdicts.get(m) for m in specs}


def describe_exc(exc: BaseException, path: Path | None = None) -> str:
    """把异常压成一行「类型: 消息 (文件:行)」，供日志与 /health 展示。"""
    if isinstance(exc, SyntaxError):
        if exc.filename:
            where = f"{Path(exc.filename).name}:{exc.lineno}"
        elif path is not None:
            where = path.name
        else:
            where = "?"
        return f"{type(exc).__name__}: {exc.msg} ({where})"
    lines = str(exc).strip().splitlines()
    head = lines[0] if lines else ""
    suffix = f" ({path.name})" if path is not None else ""
    return f"{type(exc).__name__}: {head}{suffix}" if head else f"{type(exc).__name__}{suffix}"


# ========== 统一加载入口 ==========

def load_component_module(
    module_name: str,
    *,
    component: str | None = None,
    path: str | Path | None = None,
    probe: bool = True,
) -> ModuleType | None:
    """加载一个可选组件模块。成功返回模块，失败返回 None 并登记失败。

    绝不向上抛异常（``KeyboardInterrupt`` 除外）。三道检查顺序：
    语法预检 → 子进程探测结论（需先 ``warm_probe``）→ 主进程 import 兜底。

    ``path`` 是组件入口源文件，给了才能做语法预检与探测；不给则只做 import 兜底。
    """
    component_name = component or _default_component(module_name)
    src = Path(path) if path is not None else None

    if src is not None and src.exists():
        syntax_error = check_syntax(src)
        if syntax_error:
            record_failure(component_name, module_name, KIND_SYNTAX, syntax_error, src)
            return None
        if probe:
            verdict = warm_probe({module_name: src}).get(module_name)
            if verdict:
                kind = KIND_TIMEOUT if verdict.startswith("probe_timeout") else KIND_IMPORT
                record_failure(component_name, module_name, kind, verdict, src)
                return None

    try:
        module = importlib.import_module(module_name)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # SystemExit 也要隔离，组件不能决定后端生死
        record_failure(component_name, module_name, KIND_IMPORT, describe_exc(exc, src), src)
        return None

    _clear_failure(module_name)
    return module
