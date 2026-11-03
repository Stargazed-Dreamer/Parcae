"""运行时端点发现 + 下游消费方登记（跨 client / server / 外部前端共用）

## 为什么需要这个模块

后端首选端口可能被 **Windows 端口保留区** 占用（`WinError 10013`，端口没被任何
进程监听、`netstat` 查不到，但内核拒绝 bind）。此时启动会按序顺延到后续端口
（见 `lib/port_binding.py`）。

问题在于：所有「写死 8766」的消费方会静默失联 —— 客户端死连旧端口直到 offline、
IDE 的 MCP 配置直接 421。所以必须有一份**运行时声明**告诉所有人"这一轮实际是哪个端口"。

## 三层职责

1. **声明**（写）：后端绑定成功后把 `{port, pid, base_url, ...}` 写进
   `data/runtime/server_port.json`；lifespan 就绪/关闭时更新 `status`。
2. **发现**（读）：客户端、外部前端（未来的本地管家）读声明文件，可选 `/health`
   探活校验，失败再回退 `config.toml`。
3. **登记与同步**：记住哪些外部配置文件里写死了我们的 URL（下游消费方），
   端口变更时把旧端口号改成新端口（改前备份、只改匹配到的 URL、逐条出报告）。

## 双向注册

- **出向**（我们记下游）：`load_consumers()` / `register_consumer()`；
  首次调用会自动探测已知 harness 的 MCP 配置并登记。
- **入向**（下游问我们）：`GET /runtime/port` 返回当前端口；
  下游也可 `POST /runtime/consumers` 自报配置文件路径。

## 硬约束（不要试图绕过）

IDE 的 MCP 客户端只认自己配置里的 URL，**不会读声明文件**。所以端口顺延时
要么改写它的配置（本模块的 `rewrite_consumers`），要么人工改 + 重连。
改写只在 `auto_rewrite=True` 且内容里确实出现旧端口 URL 时才动手。

设计原则：lib 是共享底层层，不 import server/ 或 client/（ADR-0027）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = _PROJECT_ROOT / "data" / "runtime"
DECLARATION_PATH = RUNTIME_DIR / "server_port.json"
# 纯文本 pid，给 start.bat 用（cmd 解析 JSON 不可靠，`set /p` 读单行最稳）
PID_PATH = RUNTIME_DIR / "server.pid"
CONSUMERS_PATH = RUNTIME_DIR / "consumers.json"

DECLARATION_SCHEMA = 1

# 状态机：binding（已绑定端口、服务尚未 listen）→ ready → stopped
STATUS_BINDING = "binding"
STATUS_READY = "ready"
STATUS_STOPPED = "stopped"


# ==================== 基础 IO ====================


def _ensure_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再 os.replace（避免读到半个 JSON）。"""
    _ensure_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # 失败时清掉临时文件，别在 runtime 目录里留垃圾
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


# ==================== 声明文件（写 / 读） ====================


def _resolve_path(path: Path | None, default: Path) -> Path:
    """默认值在调用时解析：测试会 monkeypatch 模块级路径，def 时绑定会漏改。"""
    return default if path is None else path


def write_declaration(
    *,
    host: str,
    port: int,
    preferred_port: int,
    degraded: bool,
    reason: str = "",
    pid: int | None = None,
    status: str = STATUS_BINDING,
    extra: dict[str, Any] | None = None,
    declaration_path: Path | None = None,
    pid_path: Path | None = None,
) -> dict[str, Any]:
    """写运行时端口声明。返回落盘的 dict。

    绑定成功后立刻调用（此时 status=binding，服务还未 listen），
    lifespan 起来后再 `mark_declaration_status(STATUS_READY)`。
    """
    declaration_path = _resolve_path(declaration_path, DECLARATION_PATH)
    pid_path = _resolve_path(pid_path, PID_PATH)
    scheme_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    data: dict[str, Any] = {
        "schema": DECLARATION_SCHEMA,
        "host": host,
        "port": int(port),
        "preferred_port": int(preferred_port),
        "base_url": f"http://{scheme_host}:{int(port)}",
        "degraded": bool(degraded),
        "reason": reason or "",
        "pid": int(pid if pid is not None else os.getpid()),
        "started_at": _now_iso(),
        "status": status,
    }
    if extra:
        data.update(extra)
    _atomic_write_text(declaration_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    # bat 用的 pid 文件（单行，无 BOM）
    _atomic_write_text(pid_path, f"{data['pid']}\n")
    return data


def read_declaration(*, declaration_path: Path | None = None) -> dict[str, Any] | None:
    """读运行时端口声明。文件不存在 / 损坏 → None（调用方应回退 config）。"""
    declaration_path = _resolve_path(declaration_path, DECLARATION_PATH)
    try:
        raw = declaration_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("port"), int):
        return None
    return data


def mark_declaration_status(
    status: str,
    *,
    reason: str | None = None,
    declaration_path: Path | None = None,
) -> dict[str, Any] | None:
    """就地更新状态（ready / stopped），保留其余字段。失败不抛（启动/关停路径不能崩）。"""
    declaration_path = _resolve_path(declaration_path, DECLARATION_PATH)
    data = read_declaration(declaration_path=declaration_path)
    if data is None:
        return None
    data["status"] = status
    data["status_at"] = _now_iso()
    if reason is not None:
        data["reason"] = reason
    try:
        _atomic_write_text(declaration_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        return None
    return data


def clear_pid_file(*, pid_path: Path | None = None) -> None:
    """关闭时删掉 pid 文件（声明文件保留，status=stopped 供事后排查）。"""
    try:
        _resolve_path(pid_path, PID_PATH).unlink()
    except OSError:
        pass


# ==================== 端点解析（客户端 / 外部前端入口） ====================


def declared_base_url(
    *,
    require_alive: bool = False,
    statuses: tuple[str, ...] = (STATUS_BINDING, STATUS_READY),
    declaration_path: Path | None = None,
) -> str | None:
    """从声明文件取 base_url。

    require_alive=True 时校验进程存活（pid 判断），避免拿到上次崩溃残留的端口
    去死连。注意 pid 可能被系统复用，所以这只是**降低**误判，最终判据应是
    HTTP 探活（见 resolve_base_url）。
    """
    data = read_declaration(declaration_path=declaration_path)
    if data is None:
        return None
    if statuses and data.get("status") not in statuses:
        return None
    if require_alive:
        pid = data.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            return None
    base = data.get("base_url")
    return base if isinstance(base, str) and base else None


def _pid_alive(pid: int) -> bool:
    """跨平台判活；不真正发信号。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        # 权限不足等情况无法判定 → 保守认为活着（宁可多试一次 HTTP 探活）
        return True


def _config_base_url() -> str:
    """回退：从 config.toml [server] 派生（与 client/core/constants.py 原实现一致）。"""
    try:
        from lib.config_reader import load_config

        server = load_config().get("server", {})
        host = server.get("host", "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{int(server.get('port', 8766))}"
    except Exception:
        return "http://127.0.0.1:8766"


def _env_base_url() -> str | None:
    """显式环境变量覆盖（最高优先级）。

    `PARCAE_API` 由 `server/exec.py` 注入给子进程（agent 跑 workspace 脚本时
    自动正确）；`PARCAE_BASE` 供直接跑 workspace 脚本时手工指定，两者等价。
    裸 `127.0.0.1:9808` 这种写法补 http:// 前缀，其余原样。
    """
    for name in ("PARCAE_API", "PARCAE_BASE"):
        raw = os.environ.get(name, "").strip().rstrip("/")
        if not raw:
            continue
        if "://" not in raw:
            raw = "http://" + raw
        return raw
    return None


def resolve_base_url(*, probe_health: bool = True, timeout: float = 1.5) -> str:
    """解析当前后端 base_url —— **所有消费方都该走这里**，不要自己拼端口。

    顺序：环境变量（显式指定）→ 运行时声明（可选 HTTP 探活）→ config.toml 回退。

    probe_health=True 会发一次 GET /health；代价约 1-2ms（本机），换来的是
    「后端顺延到 8767 后客户端不会死连 8766」。探活失败**不抛异常**。

    probe_health=False 是给「只管拿地址、连接失败自己会报错」的场景用的
    （批量脚本 / workspace 工具），零网络开销。
    """
    explicit = _env_base_url()
    if explicit:
        # 显式指定的地址不再探活/回退：用户或上层既然点名了，就按它走
        return explicit

    candidates: list[str] = []
    declared = declared_base_url(require_alive=False)
    if declared:
        candidates.append(declared)
    fallback = _config_base_url()
    if fallback not in candidates:
        candidates.append(fallback)

    if not probe_health:
        return candidates[0]

    for base in candidates:
        if _probe_health(base, timeout):
            return base
    # 都探不通 → 返回声明优先的那个：让调用方按正常失败路径处理
    # （比直接回退 config 更贴近"服务其实在顺延端口上"的事实）
    return candidates[0]


def _probe_health(base_url: str, timeout: float) -> bool:
    """GET {base}/health，2xx 视为活。任何异常都当失败，不抛。"""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= int(resp.status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False
    except Exception:
        return False


# ==================== 下游消费方登记 ====================


@dataclass
class RewriteReport:
    """一次下游配置改写的结局（供日志/API 逐条回报）。"""

    consumer_id: str
    path: str
    status: str  # rewritten / skipped_missing / skipped_no_match / skipped_disabled / failed
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "consumer_id": self.consumer_id,
            "path": self.path,
            "status": self.status,
            "detail": self.detail,
        }


def _default_consumers() -> list[dict[str, Any]]:
    """已知 harness 的 MCP 配置（存在才登记）。

    这里是「出向注册」的起点：换 harness / 装新 IDE 时只要把它的配置文件路径
    加进来，端口顺延就能自动同步。⛔ 不要在这里写死单一 harness 的路径。
    """
    home = Path.home()
    appdata = os.environ.get("APPDATA", "")
    candidates = [
        ("workbuddy-mcp", home / ".workbuddy" / "mcp.json", "mcp-json"),
        ("codebuddy-mcp", home / ".codebuddy" / "mcp.json", "mcp-json"),
        ("qoder-mcp", home / ".qoder" / "mcp.json", "mcp-json"),
        ("claude-desktop", Path(appdata) / "Claude" / "claude_desktop_config.json", "claude-json"),
        ("claude-code", home / ".claude.json", "claude-json"),
    ]
    out: list[dict[str, Any]] = []
    for cid, path, kind in candidates:
        if path.exists():
            out.append(
                {
                    "id": cid,
                    "kind": kind,
                    "path": str(path),
                    "auto_rewrite": True,
                    "note": "首次自动探测登记（存在才登记）",
                }
            )
    return out


def load_consumers(*, seed_if_missing: bool = True) -> list[dict[str, Any]]:
    """读下游消费方登记表；文件不存在且 seed_if_missing 时探测并落盘。"""
    try:
        raw = json.loads(CONSUMERS_PATH.read_text(encoding="utf-8"))
        items = raw.get("consumers")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict) and i.get("path")]
    except (OSError, ValueError):
        pass

    if not seed_if_missing:
        return []
    seeded = _default_consumers()
    if seeded:
        _write_consumers(seeded)
    return seeded


def _write_consumers(items: list[dict[str, Any]]) -> None:
    payload = {"schema": DECLARATION_SCHEMA, "consumers": items}
    _atomic_write_text(CONSUMERS_PATH, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def register_consumer(
    consumer_id: str,
    path: str,
    *,
    kind: str = "mcp-json",
    auto_rewrite: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """登记（或更新）一个下游消费方。同 id 覆盖，不重复登记。"""
    items = load_consumers()
    entry = {
        "id": consumer_id,
        "kind": kind,
        "path": path,
        "auto_rewrite": bool(auto_rewrite),
        "note": note,
        "registered_at": _now_iso(),
    }
    for idx, item in enumerate(items):
        if item.get("id") == consumer_id:
            items[idx] = {**item, **entry}
            _write_consumers(items)
            return entry
    items.append(entry)
    _write_consumers(items)
    return entry


def _port_url_pattern(old_port: int) -> re.Pattern[str]:
    """匹配「本机回环地址 + 指定端口」的 URL 端口段。

    只替换端口号本身，保留 scheme/host/路径；`(?!\\d)` 防止 8766 命中 87660。
    """
    return re.compile(rf"(https?://(?:127\.0\.0\.1|localhost))(?:):{int(old_port)}(?!\d)")


def plan_rewrite_sources(preferred_port: int, prev_port: int | None, actual_port: int) -> list[int]:
    """端口顺延时，下游配置文件里可能出现的「旧端口」有哪些。

    为什么要两个候选而不是只用首选端口：下游文件里写的是**上一次被同步进去的端口**。
    连续两次顺延（如 8766→9808→8908）时，第二次启动时文件里是 9808，若只按首选端口
    8766 去匹配，`rewrite_consumers` 会全部返回 skipped_no_match，下游静默停在死端口上
    （2026-10-20 实际事故：4 个 MCP 配置全部漏改）。

    返回顺序：首选端口在前（最常见的情形），上次实际端口在后（仅当它是第三个不同值）。
    """
    sources: list[int] = [int(preferred_port)]
    if prev_port is None:
        return sources
    prev = int(prev_port)
    if prev != int(preferred_port) and prev != int(actual_port) and prev not in sources:
        sources.append(prev)
    return sources


def rewrite_consumers(old_port: int, new_port: int, *, dry_run: bool = False) -> list[RewriteReport]:
    """把已登记下游配置里的 `127.0.0.1:<old_port>` 改成 `<new_port>`。

    安全边界（很重要，这是个会写项目外文件的操作）：
    - 只处理登记表里 `auto_rewrite=True` 的项
    - 只替换**形如回环地址 + 旧端口**的内容，不认识的格式一律不动
    - 改动前备份为 `<path>.bak_portrewrite`（只保留一份）
    - 无匹配 → 不写文件（避免无谓改动 IDE 配置）
    - 任何异常都收敛成 failed 报告，不中断启动
    """
    reports: list[RewriteReport] = []
    if old_port == new_port:
        return reports
    pattern = _port_url_pattern(old_port)

    for item in load_consumers():
        cid = str(item.get("id", "?"))
        raw_path = str(item.get("path", ""))
        if not raw_path:
            continue
        if not item.get("auto_rewrite", True):
            reports.append(RewriteReport(cid, raw_path, "skipped_disabled", "auto_rewrite=False"))
            continue
        path = Path(os.path.expanduser(raw_path))
        if not path.exists():
            reports.append(RewriteReport(cid, str(path), "skipped_missing", "配置文件不存在"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            reports.append(RewriteReport(cid, str(path), "failed", f"读取失败: {exc}"))
            continue

        new_text, count = pattern.subn(rf"\g<1>:{int(new_port)}", text)
        if count == 0:
            reports.append(RewriteReport(cid, str(path), "skipped_no_match", f"未见 127.0.0.1:{old_port}"))
            continue
        if dry_run:
            reports.append(RewriteReport(cid, str(path), "rewritten", f"dry-run：将替换 {count} 处"))
            continue
        try:
            backup = path.with_suffix(path.suffix + ".bak_portrewrite")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
            _atomic_write_text(path, new_text)
            reports.append(RewriteReport(cid, str(path), "rewritten", f"替换 {count} 处（已备份 {backup.name}）"))
        except OSError as exc:
            reports.append(RewriteReport(cid, str(path), "failed", f"写入失败: {exc}"))
    return reports


def consumer_ports_in_use() -> list[dict[str, str]]:
    """扫描登记表，报告每个下游配置里写的是哪个端口（排查用，只读）。"""
    out: list[dict[str, str]] = []
    pat = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)")
    for item in load_consumers():
        path = Path(os.path.expanduser(str(item.get("path", ""))))
        if not item.get("path") or not path.exists():
            continue
        try:
            ports = sorted({m.group(1) for m in pat.finditer(path.read_text(encoding="utf-8"))})
        except (OSError, UnicodeDecodeError):
            continue
        out.append({"consumer_id": str(item.get("id", "?")), "path": str(path), "ports": ",".join(ports)})
    return out
