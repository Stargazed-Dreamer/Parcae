"""后端启动外壳（supervisor）—— 把「进程怎么死的」变成可读证据。

背景：`data/logs/server.log` 只记录 logging 模块的输出。进程被打断时，Python 的
traceback 走的是 **stderr**，而 stderr 原本直接打在 `start.bat` 的控制台窗口里，
窗口一关/一滚屏就永久丢失 ⇒ 事后只能看到「日志在某一行断了」，不知道 exit code、
不知道有没有 traceback、不知道当时内存多少。

本外壳补上四件事：
1. 出生登记：启动时刻、子进程 PID、**父进程链**、命令行 → data/runtime/server_birth.json
2. 控制台全量落盘：子进程 stdout+stderr 边回显边写 data/logs/backend_console_YYYYMMDD.log
3. 周期采样：每 N 秒记本进程 private commit / working set / 句柄 / 线程 + 系统提交水位
   → data/logs/backend_mem_YYYYMMDD.jsonl
4. 退出判决：exit code（十进制 + 十六进制 + 释义）、存活时长、峰值内存、
   本次运行窗口内 server.log 有无优雅关闭标记、stderr 有无 Traceback
   → data/logs/backend_exit.jsonl，并在窗口打印摘要

用法：
    .venv\\Scripts\\python.exe tools\\runtime\\run_backend.py
    .venv\\Scripts\\python.exe tools\\runtime\\run_backend.py --sample-interval 10
    .venv\\Scripts\\python.exe tools\\runtime\\run_backend.py --self-test   # 不碰 8766，验逻辑

⚠ 本文件不修改后端任何行为：它只是 `python -m server.main` 的父进程，stdin 仍接控制台，
   Ctrl+C 照旧同时发给两边。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# Windows 常见退出码释义（只列会遇到的）
EXIT_HINTS: dict[int, str] = {
    0: "正常退出",
    1: "通用错误（MemoryError / 未捕获异常 / taskkill /F 都会是 1，需看 stderr 有无 Traceback）",
    2: "命令行参数错误（argparse）",
    3: "被 Ctrl+Break 中断",
    3221225477: "0xC0000005 访问冲突（原生崩溃，通常伴随 WER 报告）",
    3221225495: "0xC0000017 STATUS_NO_MEMORY —— 分配内存失败",
    3221225869: "0xC000012D STATUS_COMMITMENT_LIMIT —— 超出提交上限（虚拟内存耗尽）",
    3221226505: "0xC0000409 栈溢出/快速失败（fail-fast）",
    3221225786: "0xC000013A 被 Ctrl+C 终止",
}


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _human_seconds(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _u32(code: int) -> int:
    """把 Python 返回的退出码规整成 32 位无符号，便于查表。"""
    return code & 0xFFFFFFFF


def _exit_hint(code: int) -> str:
    u = _u32(code)
    return EXIT_HINTS.get(u, EXIT_HINTS.get(code, "未收录，按原值排查"))


@dataclass
class Paths:
    """所有落盘位置集中一处，self-test 可整体重定向，避免污染真实日志。"""

    root: Path = ROOT
    log_dir: Path = field(default_factory=lambda: ROOT / "data" / "logs")
    runtime_dir: Path = field(default_factory=lambda: ROOT / "data" / "runtime")

    @property
    def console_log(self) -> Path:
        return self.log_dir / f"backend_console_{datetime.now():%Y%m%d}.log"

    @property
    def shell_log(self) -> Path:
        """外壳自己的输出单独一份。

        ⛔ 不能和子进程共用 `console_log`：两个句柄同时 append 同一文件时，
        子进程把文件写大之后外壳的写入会整段丢掉（2026-11-01 实测：banner /
        接管提示写进去了，此后每一行 `_echo` 全部消失，连「子进程已启动」
        都没留下）。分开之后还顺带解决「tee 写进文件 → tail 读回来 → 窗口
        再显示一遍」的重复回显。
        """
        return self.log_dir / f"backend_shell_{datetime.now():%Y%m%d}.log"

    @property
    def mem_log(self) -> Path:
        return self.log_dir / f"backend_mem_{datetime.now():%Y%m%d}.jsonl"

    @property
    def exit_log(self) -> Path:
        return self.log_dir / "backend_exit.jsonl"

    @property
    def birth_file(self) -> Path:
        return self.runtime_dir / "server_birth.json"

    @property
    def server_log(self) -> Path:
        return self.log_dir / "server.log"


class ConsoleTee:
    """把外壳自己的输出追加进**外壳专属**日志（`shell_log`）。

    ⛔ 两条都不许改回去：① 不写窗口——窗口那一路走 `Supervisor._write_console`，
    它出问题只把自己永久停用，绝不会回头把子进程的输出路径拖住；
    ② 不和子进程共用 `console_log`——见 `Paths.shell_log` 的注释。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "ab", buffering=0)
        self.bytes_written = 0

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        try:
            self._fh.write(chunk)
            self.bytes_written += len(chunk)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def _parent_chain(limit: int = 6) -> list[str]:
    """父进程链（名字 + PID）。这就是下次「谁起的我」的唯一线索。"""
    try:
        import psutil  # 项目依赖，已在 .venv 里
    except Exception:
        return ["<psutil 不可用>"]
    chain: list[str] = []
    try:
        for proc in psutil.Process().parents():
            try:
                chain.append(f"{proc.name()}({proc.pid})")
            except Exception:
                chain.append(f"?({proc.pid})")
            if len(chain) >= limit:
                break
    except Exception:
        pass
    return chain


def _mem_snapshot(proc: object) -> dict:
    """采样子进程内存/句柄。psutil 的 private == Windows PrivateUsage（任务管理器的提交大小）。"""
    out: dict = {}
    try:
        info = proc.memory_info()  # type: ignore[attr-defined]
        out["private_mb"] = round(getattr(info, "private", 0) / 1048576, 1)
        out["ws_mb"] = round(info.rss / 1048576, 1)
    except Exception:
        out["private_mb"] = None
        out["ws_mb"] = None
    try:
        out["handles"] = proc.num_handles()  # type: ignore[attr-defined]
    except Exception:
        out["handles"] = None
    try:
        out["threads"] = proc.num_threads()  # type: ignore[attr-defined]
    except Exception:
        out["threads"] = None
    # 记下采的是谁：采样对象搞错是这个工具栽过的坑，不写 pid 就没法事后自证
    try:
        out["pid"] = proc.pid  # type: ignore[attr-defined]
        out["name"] = proc.name()  # type: ignore[attr-defined]
    except Exception:
        out["pid"] = None
    return out


def _system_mem() -> dict:
    try:
        import psutil
    except Exception:
        return {}
    try:
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        return {
            "sys_avail_mb": round(vm.available / 1048576, 1),
            "sys_used_pct": vm.percent,
            "pagefile_used_mb": round(sm.used / 1048576, 1),
            "pagefile_pct": sm.percent,
        }
    except Exception:
        return {}


def _load_prev_exits(path: Path, limit: int = 3) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_from(path: Path, offset: int, limit: int = 200_000) -> bytes:
    """读 path 从 offset 起的新内容（只取尾部 limit 字节，防日志暴涨）。"""
    if not path.exists():
        return b""
    try:
        size = path.stat().st_size
        if size <= offset:
            return b""
        start = max(offset, size - limit)
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read()
    except OSError:
        return b""


# ------------------------------------------------------------ 存量实例接管
#
# 以前 start.bat 直接 taskkill /F 旧进程：端口可能没释放、临时文件没落盘、子进程残留，
# 事后也完全看不出这次是"被重启"还是"自己死了"。这里改成优雅优先、逐级降级：
#   POST /shutdown（走完整清理链） → 等待退出 → taskkill（软，WM_CLOSE） → taskkill /F

DEFAULT_GRACEFUL_TIMEOUT = 30.0


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _declared_port(paths: Paths) -> int:
    """端口优先级：server_port.json（上次实际用的，可能已漂移）> config.toml [server] port。"""
    declared = _read_json(paths.runtime_dir / "server_port.json").get("port")
    if isinstance(declared, int) and 1 <= declared <= 65535:
        return declared
    try:
        with open(paths.root / "config.toml", "rb") as fh:
            port = tomllib.load(fh).get("server", {}).get("port")
        if isinstance(port, int) and 1 <= port <= 65535:
            return port
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return 8766


def _declared_pid(paths: Paths) -> int | None:
    pid = _read_json(paths.runtime_dir / "server_port.json").get("pid")
    if isinstance(pid, int) and pid > 0:
        return pid
    try:
        pid = int((paths.runtime_dir / "server.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
    except Exception:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _listener_pid(port: int) -> int | None:
    """端口还被占但 pid 文件不可信时，反查监听者；查不到就 None。"""
    try:
        import psutil
    except Exception:
        return None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid:
                return int(conn.pid)
    except Exception:
        return None
    return None


def _http_shutdown(port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """POST /shutdown：后端回 OK 后延迟 0.5s 走完整生命周期清理链（自带 30s 看门狗）。"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/shutdown", method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定回环地址
            body = resp.read(200).decode("utf-8", errors="replace").strip()
            return 200 <= resp.status < 300, f"HTTP {resp.status} {body}".strip()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _taskkill(pid: int, force: bool) -> tuple[bool, str]:
    args = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
    try:
        run = subprocess.run(args, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    # taskkill 在中文 Windows 上输出 GBK，不能按 UTF-8 解（否则 reader 线程抛 UnicodeDecodeError）
    raw = (run.stdout or b"") + (run.stderr or b"")
    codec = "mbcs" if os.name == "nt" else "utf-8"
    detail = raw.decode(codec, errors="replace").strip().replace("\r\n", " ")
    return run.returncode == 0, detail[:200]


GRACEFUL_MARKERS = (    "Parcae API 已关闭",
    "关闭清理完成",
    "session manager is shutting down",
    "记忆系统已关闭",
)


def _judge_graceful(new_bytes: bytes, exit_code: int | None = None) -> tuple[bool, str]:
    """优雅关闭 = 退出码为 0 **且** 本次运行窗口内 server.log 出现生命周期收尾标记。

    exit_code 优先：非 0 一律判异常。否则原生崩溃（如 0xC0000005）在崩溃途中
    恰好打到 "记忆系统已关闭" 这类收尾行时会被误判成优雅——2026-10-30 18:10 那次
    就是这么错判的，必须靠退出码把这条腿钉死。
    """
    if exit_code is not None and exit_code != 0:
        hit = ""
        if new_bytes:
            text = new_bytes.decode("utf-8", errors="replace")
            hit = next((m for m in GRACEFUL_MARKERS if m in text), "")
        extra = f"（日志里出现了收尾标记「{hit}」，但进程是被异常终止的，不算优雅）" if hit else ""
        return False, f"退出码非 0 ⇒ 崩溃/被终止{extra}"
    if not new_bytes:
        return False, "本次运行窗口内 server.log 无任何新内容"
    text = new_bytes.decode("utf-8", errors="replace")
    for marker in GRACEFUL_MARKERS:
        if marker in text:
            return True, f"命中收尾标记：{marker}"
    return False, "未命中任何收尾标记 ⇒ 硬杀 / 异常退出"


def _last_traceback(tail: bytes, lines: int = 15) -> str:
    """取「最后一段 traceback 的**尾部**」。

    2026-10-30 19:23 那次暴露：原实现取 `[:lines]`，拿到的是最外层的 h11/starlette 帧，
    真正有用的 `server/ocr.py:746 → paddleocr.predict → runner.py:265 → RuntimeError`
    全在最末几行，被整段截掉 ⇒ 摘要里看着像"starlette 崩了"。
    """
    text = tail.decode("utf-8", errors="replace")
    idx = text.rfind("Traceback (most recent call last)")
    if idx < 0:
        return ""
    block = text[idx:].splitlines()
    # 截到异常行（traceback 之后第一个顶格行）为止，别把后面的日志混进来
    end = len(block)
    for i in range(1, len(block)):
        ln = block[i]
        if ln and not ln.startswith((" ", "\t")):
            end = i + 1
            break
    block = block[:end]
    if len(block) > lines:
        block = [f"...（前面 {len(block) - lines} 层已省略，只留最内层）", *block[-lines:]]
    return "\n".join(block)


def _tail_lines(tail: bytes, n: int = 10, width: int = 150) -> list[str]:
    """崩溃前最后 n 行有效输出——这才是真正的现场。

    `has_traceback` 容易骗人：2026-10-30 18:10 那次 stderr 里唯一一段 traceback 是
    17:49 的 MCP 历史异常，真正致命的是原生崩溃，Python 根本没机会打 traceback。
    所以必须同时给出「进程死前最后输出了什么」。
    """
    text = tail.decode("utf-8", errors="replace")
    # 只留「本次运行真正开始之后」的输出，别把启动横幅混进现场
    marker = "子进程已启动 PID="
    pos = text.rfind(marker)
    if pos >= 0:
        nl = text.find("\n", pos)
        text = text[nl + 1 :] if nl >= 0 else text[pos:]
    skip = ("Parcae 后端启动外壳", "后端已退出", " exit code:", " 退出时刻", " 存活时长")
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("=====") or s.startswith("-----"):
            continue
        if any(k in s for k in skip):
            continue
        out.append(s[:width])
    # 多线程同时崩时 faulthandler 会并发写同一个 stderr，转储互相穿插成一堆
    # 「Windows fatal exception: access violation」（2026-10-30 19:23 那次 10+ 线程）。
    # 那段对定位没帮助，反而把「死前在干什么」淹掉了 ⇒ 截断并注明。
    fatal = ("Windows fatal exception", "Fatal Python error")
    cut = next((i for i, s in enumerate(out) if any(k in s for k in fatal)), None)
    if cut is not None and cut >= 2:
        dropped = len(out) - cut
        out = out[:cut] + [f"[此后还有 {dropped} 行多线程崩溃转储（faulthandler 交错，全文见控制台日志）]"]
    return out[-n:]


class Supervisor:
    def __init__(
        self,
        child_cmd: list[str],
        paths: Paths,
        interval: float = 30.0,
        takeover: bool = True,
        graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT,
    ) -> None:
        self.child_cmd = child_cmd
        self.paths = paths
        self.interval = max(0.2, interval)
        self.takeover_enabled = takeover
        self.graceful_timeout = max(1.0, graceful_timeout)
        self.takeover_notes: list[str] = []
        self.peak_private = 0.0
        self.peak_ws = 0.0
        self.last_snapshot: dict = {}
        self.samples = 0
        self._stop = threading.Event()
        self._tee: ConsoleTee | None = None
        self._mem_fh = None
        self._tailer: threading.Thread | None = None
        self._sampler: threading.Thread | None = None
        self._console_ok = True
        self.bytes_seen = 0
        self._tail_pos = 0
        self.console_bytes = 0

    # ---------- 存量实例接管（优雅优先） ----------
    def _drop_stale_pidfile(self, pids: list[int]) -> None:
        """pid 文件指向的进程已不在 ⇒ 清掉，免得下次又把它当"存量实例"。"""
        path = self.paths.runtime_dir / "server.pid"
        try:
            if not path.exists():
                return
            current = int(path.read_text(encoding="utf-8").strip())
            if current in pids or not _pid_alive(current):
                path.unlink()
                self._echo(f"[{_stamp()}] 已清理陈旧 pid 文件（PID={current} 不在了）\n")
        except (OSError, ValueError):
            pass

    def _wait_gone(self, pids: list[int], port: int, timeout: float) -> bool:
        deadline = time.time() + max(0.0, timeout)
        while True:
            alive = [p for p in pids if _pid_alive(p)]
            up = _port_listening(port)
            if not alive and not up:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.3)

    def takeover(self) -> list[str]:
        """把已有实例请走：优先 POST /shutdown，失败才软杀/硬杀，全过程记账。"""
        notes: list[str] = []
        port = _declared_port(self.paths)
        pid = _declared_pid(self.paths)
        if pid is not None and pid == os.getpid():
            return ["跳过接管：pid 文件指向本进程"]

        alive = bool(pid and _pid_alive(pid))
        up = _port_listening(port)
        if not alive and not up:
            notes.append(f"无存量实例（端口 {port} 未监听，pid 文件 PID={pid} 已失效）")
            self._drop_stale_pidfile([pid] if pid else [])
            return notes

        notes.append(
            f"发现存量实例：PID={pid}（{'存活' if alive else '已不存在'}）"
            f"，端口 {port}（{'在监听' if up else '未监听'}）"
        )

        # 第 1 档：优雅关闭
        graceful_pids: list[int] = [pid] if (pid is not None and alive) else []
        ok, detail = _http_shutdown(port)
        if ok:
            notes.append(f"[1/3] 已发 POST /shutdown → {detail}")
            if self._wait_gone(graceful_pids, port, self.graceful_timeout):
                notes.append("[1/3] 存量实例已优雅退出（走完整清理链，端口已释放）")
                self._drop_stale_pidfile([pid] if pid else [])
                return notes
            notes.append(f"[1/3] 发了 /shutdown 但 {self.graceful_timeout:g}s 内未退出（清理链可能卡住）")
        else:
            notes.append(f"[1/3] 优雅关闭不可用 → {detail}")

        # 第 2 档：软杀（taskkill 不带 /F，发 WM_CLOSE），仍不行才第 3 档硬杀
        target = pid if alive else _listener_pid(port)
        if target is None:
            notes.append("[2/3] 端口在监听但反查不到监听者 PID，不再强行动手")
            return notes
        ok, detail = _taskkill(target, force=False)
        notes.append(f"[2/3] 降级软杀 taskkill /PID {target} → {'成功' if ok else '失败：' + detail}")
        if self._wait_gone([target], port, 8.0):
            notes.append("[2/3] 软杀后已退出")
            self._drop_stale_pidfile([target])
            return notes

        ok, detail = _taskkill(target, force=True)
        notes.append(f"[3/3] 降级硬杀 taskkill /F /PID {target} → {'成功' if ok else '失败：' + detail}")
        if self._wait_gone([target], port, 10.0):
            notes.append("[3/3] 硬杀后已退出（这次不是优雅收尾，日志里不会有 shutdown 三件套）")
        else:
            notes.append("[3/3] 硬杀后端口仍被占用，启动可能失败（检查端口保留区 / 其它进程）")
        self._drop_stale_pidfile([target])
        return notes

    # ---------- 采样 ----------
    def _pick_backend_proc(self, root: Any) -> Any:
        """从 Popen 句柄沿进程树选出「真正干活的后端进程」。

        实测（2026-10-30）：`.venv\\Scripts\\python.exe` 是 uv 的转发壳，自己只占
        1MB private / 57 句柄 / 1 线程，真正跑 server.main 的是它派生的孙子进程
        （uv base python）。原实现直接采 Popen.pid，于是 68 个样本全是 0.8MB，
        等于没采——这次崩溃的核心数据就是这么丢的。
        """
        best = root
        try:
            tree = [root, *root.children(recursive=True)]
        except Exception:
            return root

        scored: list[tuple[float, int, Any]] = []
        named: list[tuple[float, int, Any]] = []
        for p in tree:
            try:
                if not p.is_running():
                    continue
            except Exception:
                continue
            snap = _mem_snapshot(p)
            entry = (
                float(snap.get("private_mb") or 0),
                int(snap.get("handles") or 0),
                p,
            )
            try:
                cmd = " ".join(p.cmdline()).lower()
            except Exception:
                cmd = ""
            # ⛔ 只按「命令行带 server.main」取第一个是无解的：uv 的转发壳
            # （.venv\Scripts\python.exe）同样带 `-u -m server.main`，而且排在树序前头。
            # 2026-10-31 06:47 那场 1890 个样本全打在 0.8MB / 57 句柄的壳上，
            # 真身（2081MB / 1064 句柄）一次都没采到 —— 坑就在这里。
            if "server.main" in cmd or "server\\main.py" in cmd:
                named.append(entry)
            scored.append(entry)
        # ① 命令行命中的一组里，挑 private 最大的（真身后端加载完是 GB 级，壳只有 0.8MB）
        # ② 全都拿不到命令行时，退化成整棵树按 private 排序
        for pool in (named, scored):
            if not pool:
                continue
            pool.sort(key=lambda x: (x[0], x[1]), reverse=True)
            if pool[0][0] > 0:
                return pool[0][2]
        return best

    def _sample_loop(self, proc: subprocess.Popen) -> None:
        psutil = None
        root = None
        target = None
        try:
            import psutil as _ps

            psutil = _ps
            root = psutil.Process(proc.pid)
        except Exception:
            pass
        while not self._stop.is_set():
            if psutil is not None and root is not None:
                try:
                    if not root.is_running():
                        root = None
                except Exception:
                    root = None
            if psutil is not None and root is not None:
                # 每次采样都重新解析目标：第一个样本往往打在 uv 转发壳上（孙进程还没起来），
                # 只在「目标已死」时才重解析，会把这个错目标固定整场
                # ——2026-10-30 18:26 那场 114 个样本全是 0.8MB / 1 线程，等于没采。
                try:
                    picked = self._pick_backend_proc(root)
                    if picked is not None:
                        target = picked
                except Exception:
                    pass  # 解析失败就沿用上一次的目标，别让一次异常把采样线程整个送走
            if target is not None:
                snap = _mem_snapshot(target)
                snap.update(_system_mem())
                self.last_snapshot = snap
                self.samples += 1
                priv = snap.get("private_mb") or 0
                ws = snap.get("ws_mb") or 0
                if isinstance(priv, (int, float)):
                    self.peak_private = max(self.peak_private, float(priv))
                if isinstance(ws, (int, float)):
                    self.peak_ws = max(self.peak_ws, float(ws))
                self._write_mem(snap)
                # 每 10 个样本（默认 5 分钟）在窗口打一行，避免刷屏
                # 带上 pid/线程，采错进程时一眼能看出来（以前只有 0.8MB 也没人知道是谁）
                if self.samples % 10 == 0:
                    self._echo(
                        f"[{_stamp()}] mem private={priv} MB  ws={ws} MB  "
                        f"handles={snap.get('handles')}  threads={snap.get('threads')}  "
                        f"pid={snap.get('pid')}  sys_avail={snap.get('sys_avail_mb')} MB\n"
                    )
            self._stop.wait(self.interval)

    def _write_mem(self, snap: dict) -> None:
        row = {"ts": _stamp(), **snap}
        try:
            if self._mem_fh is None:
                self.paths.mem_log.parent.mkdir(parents=True, exist_ok=True)
                self._mem_fh = open(self.paths.mem_log, "a", encoding="utf-8", newline="\n")
            self._mem_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._mem_fh.flush()
        except OSError:
            pass

    # ---------- 输出 ----------
    def _echo(self, text: str) -> None:
        """本外壳自己的输出：外壳日志 + 窗口，两路互不牵连。"""
        data = text.encode("utf-8", errors="replace")
        if self._tee is not None:
            self._tee.write(data)
        self._write_console(data)

    def _write_console(self, chunk: bytes) -> None:
        """只往窗口写。控制台被关掉/被选中卡住 ⇒ 永久停用这一路，绝不回压子进程。"""
        if not chunk or not self._console_ok:
            return
        out = getattr(sys.stdout, "buffer", None)
        if out is None:
            self._console_ok = False
            return
        try:
            out.write(chunk)
            out.flush()
        except (OSError, ValueError):
            self._console_ok = False

    def _tail_once(self, pos: int) -> int:
        """把自 pos 以来的新增内容取出来回显，返回新的 pos。"""
        try:
            size = self.paths.console_log.stat().st_size
            if size > pos:
                with open(self.paths.console_log, "rb") as fh:
                    fh.seek(pos)
                    data = fh.read(size - pos)
                pos = size
                if data:
                    self.bytes_seen += len(data)
                    self._write_console(data)
        except OSError:
            pass
        return pos

    def _tail_loop(self, start_offset: int) -> None:
        """跟着控制台日志文件追赶子进程输出。

        ⛔ 子进程 stdout 直接写文件，**不再走管道**。2026-10-31 22:33 那次全进程冻死，
        形态是「92 个线程全在等、日志停在半行、端口还在 LISTENING 但没人 accept」，
        指向的正是「外壳读管道的线程停摆 → 子进程写 stdout 阻塞 → logging 持锁死等」。
        改成文件之后，这个中继线程哪怕彻底挂掉，也只是窗口看不到新行，后端照跑。
        """
        self._tail_pos = start_offset
        while not self._stop.is_set():
            self._tail_pos = self._tail_once(self._tail_pos)
            self._stop.wait(0.25)

    # ---------- 主流程 ----------
    def run(self) -> int:
        self.paths.log_dir.mkdir(parents=True, exist_ok=True)
        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        server_log_offset = self.paths.server_log.stat().st_size if self.paths.server_log.exists() else 0

        started = time.time()
        birth = {
            "started_at": _stamp(),
            "supervisor_pid": os.getpid(),
            "cwd": str(self.paths.root),
            "child_cmd": self.child_cmd,
            "parent_chain": _parent_chain(),
            "console_log": str(self.paths.console_log),
            "shell_log": str(self.paths.shell_log),
            "mem_log": str(self.paths.mem_log),
        }
        try:
            self.paths.birth_file.write_text(
                json.dumps(birth, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

        self._tee = ConsoleTee(self.paths.shell_log)
        self._print_banner(birth)

        # 存量实例：先请它优雅退场，再起新的（以前是直接 taskkill /F）
        if self.takeover_enabled:
            self.takeover_notes = self.takeover()
        else:
            self.takeover_notes = ["接管已禁用（--no-takeover），直接启动"]
        self._echo(f"[{_stamp()}] 存量实例处理：\n")
        for note in self.takeover_notes:
            self._echo(f"    {note}\n")
        self._echo("\n")
        birth["takeover"] = self.takeover_notes
        try:
            self.paths.birth_file.write_text(
                json.dumps(birth, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # 原生崩溃（0xC0000005 这类 SEH 异常）Python 层来不及打 traceback，窗口里什么都留不下
        # ——2026-10-30 18:10 那次就是这么只剩「日志断在某行」。faulthandler 在 Windows 上
        # 装 VEH，能在访问冲突时打印所有线程的 Python 栈，直接落到本外壳的 stderr 日志里。
        env["PYTHONFAULTHANDLER"] = "1"

        # ⛔ 子进程 stdout 直接落文件，不接管道：管道会让「外壳不读」变成「后端写不动」，
        # 再被 logging 的 handler 锁放大成整进程冻结（2026-10-31 22:33）。文件没有这个回压路径。
        tail_from = self.paths.console_log.stat().st_size if self.paths.console_log.exists() else 0
        child_out = open(self.paths.console_log, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                self.child_cmd,
                cwd=str(self.paths.root),
                stdin=subprocess.DEVNULL,
                stdout=child_out,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
            )
        finally:
            child_out.close()  # 子进程已继承句柄，父进程这份可以放掉
        self._echo(f"[{_stamp()}] 子进程已启动 PID={proc.pid}\n\n")

        self._tailer = threading.Thread(target=self._tail_loop, args=(tail_from,), daemon=True)
        self._tailer.start()
        self._sampler = threading.Thread(target=self._sample_loop, args=(proc,), daemon=True)
        self._sampler.start()

        try:
            code = proc.wait()
        except KeyboardInterrupt:
            self._echo(f"\n[{_stamp()}] 收到 Ctrl+C，等待子进程自行收尾（最多 60s）...\n")
            try:
                code = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self._echo(f"[{_stamp()}] 子进程未在 60s 内退出，执行 terminate\n")
                proc.terminate()
                try:
                    code = proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait()

        self._stop.set()
        if self._tailer is not None:
            self._tailer.join(timeout=10)
        # 子进程退出那一瞬的输出最容易带崩溃尾巴，停在 _stop 之后就再也看不到 ⇒ 补追一次
        try:
            self._tail_pos = self._tail_once(self._tail_pos)
        except Exception:
            pass
        if self._sampler is not None:
            self._sampler.join(timeout=5)
        if self._mem_fh is not None:
            try:
                self._mem_fh.close()
            except OSError:
                pass

        new_log = _read_from(self.paths.server_log, server_log_offset)
        graceful, graceful_why = _judge_graceful(new_log, code)
        tail = _read_from(self.paths.console_log, max(0, self.paths.console_log.stat().st_size - 60_000)
                          if self.paths.console_log.exists() else 0)
        tb = _last_traceback(tail)
        crash_tail = _tail_lines(tail)

        record = {
            "exit_at": _stamp(),
            "exit_code": code,
            "exit_code_u32": _u32(code),
            "exit_hex": f"0x{_u32(code):08X}",
            "exit_hint": _exit_hint(code),
            "started_at": birth["started_at"],
            "uptime_sec": round(time.time() - started, 1),
            "graceful": graceful,
            "graceful_why": graceful_why,
            "peak_private_mb": round(self.peak_private, 1) if self.peak_private else None,
            "last_private_mb": self.last_snapshot.get("private_mb"),
            "samples": self.samples,
            "has_traceback": bool(tb),
            "crash_tail": crash_tail,
            "console_log": str(self.paths.console_log),
            "shell_log": str(self.paths.shell_log),
            "mem_log": str(self.paths.mem_log),
            "parent_chain": birth["parent_chain"],
            "takeover": self.takeover_notes,
        }
        self._append_exit(record)
        self._print_summary(record, tb)
        # 摘要也要进外壳日志，所以 tee 留到最后一刻才关
        self.console_bytes = self._tee.bytes_written if self._tee else 0
        if self._tee is not None:
            self._tee.close()
        return code

    def _append_exit(self, record: dict) -> None:
        try:
            self.paths.exit_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.paths.exit_log, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------- 窗口输出 ----------
    def _print_banner(self, birth: dict) -> None:
        chain = " <- ".join(birth["parent_chain"]) or "(取不到)"
        lines = [
            "=" * 78,
            " Parcae 后端启动外壳（退出监控已开启）",
            "=" * 78,
            f" 启动时刻   : {birth['started_at']}",
            f" 外壳 PID   : {birth['supervisor_pid']}",
            f" 父进程链   : {chain}",
            f" 控制台全量 : {birth['console_log']}",
            f" 外壳日志   : {birth['shell_log']}",
            f" 内存采样   : {birth['mem_log']}（每 {self.interval:g}s 一条）",
            f" 退出台账   : {self.paths.exit_log}",
        ]
        prev = _load_prev_exits(self.paths.exit_log)
        lines.append("-" * 78)
        if prev:
            lines.append(" 最近几次退出：")
            for row in prev:
                flag = "优雅" if row.get("graceful") else "硬杀/异常"
                lines.append(
                    f"   {row.get('exit_at')} | exit={row.get('exit_code')}"
                    f"({row.get('exit_hex')}) | {flag} | 存活 {_human_seconds(row.get('uptime_sec') or 0)}"
                    f" | 峰值 {row.get('peak_private_mb')} MB"
                )
                lines.append(f"     └ {row.get('exit_hint')}")
        else:
            lines.append(" 最近几次退出：(暂无记录，这是第一次带监控运行)")
        lines.append("=" * 78)
        lines.append("")
        self._echo("\n".join(lines) + "\n")

    def _print_summary(self, rec: dict, tb: str) -> None:
        lines = [
            "",
            "=" * 78,
            " 后端已退出",
            "=" * 78,
            f" 退出时刻 : {rec['exit_at']}",
            f" 存活时长 : {_human_seconds(rec['uptime_sec'])}",
            f" exit code: {rec['exit_code']}  ({rec['exit_hex']})",
            f" 释义     : {rec['exit_hint']}",
            f" 收尾判定 : {'优雅关闭' if rec['graceful'] else '硬杀 / 异常退出'} —— {rec['graceful_why']}",
            f" 峰值内存 : private {rec['peak_private_mb']} MB"
            f" | 最后一次采样 private {rec['last_private_mb']} MB（共 {rec['samples']} 个样本）",
            f" 控制台日志: {rec['console_log']}",
            f" 外壳日志  : {rec['shell_log']}",
            f" 内存采样  : {rec['mem_log']}",
            "-" * 78,
        ]
        tail = rec.get("crash_tail") or []
        if tail:
            lines.append(" 死前最后输出了什么（最能定位现场）：")
            lines.extend("   " + line for line in tail)
            lines.append("-" * 78)
        if tb:
            lines.append(" stderr 里的 Traceback（⚠ 可能是运行期的历史异常，不一定是这次的死因）：")
            lines.extend("   " + line for line in tb.splitlines())
        else:
            lines.append(" stderr 里没有 Traceback ⇒ 不是 Python 层未捕获异常（原生崩溃 / 被硬杀都是这种形态）")
        lines.extend(
            [
                "-" * 78,
                " 复盘：python tools/runtime/backend_postmortem.py",
                "=" * 78,
                "",
            ]
        )
        self._echo("\n".join(lines) + "\n")


# ---------------------------------------------------------------- self test


def _self_test() -> int:
    """不启动后端、不碰 8766，用假子进程验证「exit code / traceback / 采样 / 台账」四条链路。

    两个用例互为反向腿：
      A 崩掉：exit 1 + stderr 有 Traceback + 判定「硬杀/异常」
      B 优雅：exit 0 + 本次窗口内 server.log 出现收尾标记 + 判定「优雅关闭」
    """
    import tempfile

    base = ROOT / "temp" / "run_backend_selftest"
    base.mkdir(parents=True, exist_ok=True)
    checks: list[tuple[str, bool, str]] = []

    def new_paths(tag: str) -> Paths:
        log_dir = Path(tempfile.mkdtemp(prefix=f"logs_{tag}_", dir=str(base)))
        runtime_dir = Path(tempfile.mkdtemp(prefix=f"rt_{tag}_", dir=str(base)))
        return Paths(root=ROOT, log_dir=log_dir, runtime_dir=runtime_dir)

    # ---- 用例 A：未捕获异常 ----
    paths_a = new_paths("a")
    crash_child = (
        "import sys, time\n"
        "print('fake-child: hello', flush=True)\n"
        "print('fake-child: warn line', file=sys.stderr, flush=True)\n"
        "time.sleep(0.4)\n"
        "raise RuntimeError('boom from self-test')\n"
    )
    Supervisor([sys.executable, "-u", "-c", crash_child], paths_a, interval=0.2, takeover=False).run()

    exits_a = _load_prev_exits(paths_a.exit_log, limit=5)
    rec_a = exits_a[-1] if exits_a else {}
    console_a = paths_a.console_log.read_text(encoding="utf-8", errors="replace")
    shell_a = paths_a.shell_log.read_text(encoding="utf-8", errors="replace")
    checks += [
        ("A 退出台账有 1 条记录", len(exits_a) == 1, f"got {len(exits_a)}"),
        ("A exit_code == 1", rec_a.get("exit_code") == 1, str(rec_a.get("exit_code"))),
        ("A 识别到 Traceback", bool(rec_a.get("has_traceback")), str(rec_a.get("has_traceback"))),
        ("A 判定为非优雅", rec_a.get("graceful") is False, str(rec_a.get("graceful"))),
        ("A 采样数 >= 1", (rec_a.get("samples") or 0) >= 1, str(rec_a.get("samples"))),
        ("A 峰值 private 有值", rec_a.get("peak_private_mb") is not None, str(rec_a.get("peak_private_mb"))),
        ("A 控制台日志含子进程输出", "fake-child: hello" in console_a, "缺少 hello"),
        ("A 控制台日志含 stderr 行", "fake-child: warn line" in console_a, "缺少 warn line"),
        ("A 外壳日志含启动横幅", "退出监控已开启" in shell_a, "缺少横幅"),
        ("A 外壳日志含退出摘要", "后端已退出" in shell_a, "缺少摘要"),
        ("A 外壳日志含子进程已启动", "子进程已启动 PID=" in shell_a, "缺 PID 行（Popen 之后 tee 失效的老 bug）"),
        # 两个文件必须各管一边：混用会让「子进程写大文件之后外壳写入整段丢失」
        ("A 控制台日志不含外壳横幅", "退出监控已开启" not in console_a, "控制台日志混进了外壳输出"),
        ("A 控制台日志不含退出摘要", "后端已退出" not in console_a, "控制台日志混进了摘要"),
        ("A 出生登记已写", paths_a.birth_file.exists(), "缺 server_birth.json"),
        ("A 内存采样文件非空", paths_a.mem_log.exists() and paths_a.mem_log.stat().st_size > 0, "空文件"),
    ]

    # ---- 用例 B：优雅关闭（子进程往本次窗口内的 server.log 写收尾标记）----
    paths_b = new_paths("b")
    marker = "2026-10-30 00:00:00,000 [parcae.lifecycle] INFO: 关闭清理完成，耗时 1ms"
    ok_child = (
        "import sys\n"
        f"open({str(paths_b.server_log)!r}, 'a', encoding='utf-8').write({marker!r} + '\\n')\n"
        "print('fake-child: bye', flush=True)\n"
        "sys.exit(0)\n"
    )
    Supervisor([sys.executable, "-u", "-c", ok_child], paths_b, interval=0.2, takeover=False).run()
    exits_b = _load_prev_exits(paths_b.exit_log, limit=5)
    rec_b = exits_b[-1] if exits_b else {}
    checks += [
        ("B exit_code == 0", rec_b.get("exit_code") == 0, str(rec_b.get("exit_code"))),
        ("B 判定为优雅关闭", rec_b.get("graceful") is True, str(rec_b.get("graceful_why"))),
        ("B 无 Traceback", rec_b.get("has_traceback") is False, str(rec_b.get("has_traceback"))),
    ]

    # ---- 用例 C：硬杀形态（等价 taskkill /F：exit 1、无 Traceback、无收尾标记）----
    paths_c = new_paths("c")
    kill_child = (
        "import ctypes\n"
        "print('fake-child: about to be hard-killed', flush=True)\n"
        "k32 = ctypes.windll.kernel32\n"
        "k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]\n"
        "k32.TerminateProcess(ctypes.c_void_p(-1), 1)\n"  # -1 = GetCurrentProcess 伪句柄
    )
    Supervisor([sys.executable, "-u", "-c", kill_child], paths_c, interval=0.2, takeover=False).run()
    rec_c = (_load_prev_exits(paths_c.exit_log, limit=5) or [{}])[-1]
    checks += [
        ("C 硬杀形态 exit_code == 1", rec_c.get("exit_code") == 1, str(rec_c.get("exit_code"))),
        ("C 硬杀形态无 Traceback", rec_c.get("has_traceback") is False, str(rec_c.get("has_traceback"))),
        ("C 硬杀形态判为非优雅", rec_c.get("graceful") is False, str(rec_c.get("graceful_why"))),
    ]

    # ---- 用例 D：接管存量实例（假旧后端收到 POST /shutdown 后自行退出）----
    paths_d = new_paths("d")
    d_port = 18991
    if _port_listening(d_port):
        checks.append((f"D 端口 {d_port} 被占用，跳过接管用例", True, "skipped"))
    else:
        old_code = (
            "import http.server, socketserver, threading\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_POST(self):\n"
            "        self.send_response(200); self.end_headers()\n"
            "        self.wfile.write(b'{\"ok\": true}')\n"
            "        threading.Thread(target=self.server.shutdown, daemon=True).start()\n"
            "    def log_message(self, *a):\n"
            "        pass\n"
            f"srv = socketserver.TCPServer(('127.0.0.1', {d_port}), H)\n"
            "srv.serve_forever()\n"
        )
        old = subprocess.Popen([sys.executable, "-u", "-c", old_code])
        for _ in range(50):  # 等假旧实例端口就绪（最多 5s）
            if _port_listening(d_port):
                break
            time.sleep(0.1)
        paths_d.runtime_dir.mkdir(parents=True, exist_ok=True)
        (paths_d.runtime_dir / "server_port.json").write_text(
            json.dumps({"schema": 1, "port": d_port, "pid": old.pid, "status": "ready"}), encoding="utf-8"
        )
        (paths_d.runtime_dir / "server.pid").write_text(str(old.pid), encoding="utf-8")

        Supervisor(
            [sys.executable, "-u", "-c", "print('new instance up', flush=True)"],
            paths_d,
            interval=0.2,
            takeover=True,
            graceful_timeout=10.0,
        ).run()
        rec_d = (_load_prev_exits(paths_d.exit_log, limit=5) or [{}])[-1]
        notes_d = " | ".join(rec_d.get("takeover") or [])
        checks += [
            ("D 判定走了优雅关闭路径", "已优雅退出" in notes_d, (notes_d[:160] or "无接管记录")),
            ("D 旧实例进程已消失", not _pid_alive(old.pid), f"PID {old.pid} 仍存活"),
            ("D 旧实例端口已释放", not _port_listening(d_port), f"{d_port} 仍在监听"),
            ("D 陈旧 pid 文件已清理", not (paths_d.runtime_dir / "server.pid").exists(), "server.pid 未删"),
        ]
        if _pid_alive(old.pid):
            old.kill()

    # ---- 用例 E：降级能力本身可用（taskkill /F 能真的收掉一个进程）----
    dummy = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(120)"])
    time.sleep(0.3)
    tk_ok, tk_detail = _taskkill(dummy.pid, force=True)
    gone = not _pid_alive(dummy.pid)
    checks.append(("E taskkill /F 降级可用", tk_ok and gone, f"ok={tk_ok} gone={gone} {tk_detail[:80]}"))
    if not gone:
        dummy.kill()

    # F：退出码优先于日志标记——2026-10-30 18:10 真实踩过的误判。
    # 那次后端原生崩溃 0xC0000005，但崩溃途中打到了「记忆系统已关闭」，
    # 旧逻辑据此判成「优雅关闭」，把最关键的结论写反了。
    fake_shutdown_log = (
        "2026-10-30 18:10:28,100 [parcae.lifecycle] INFO: 关闭步骤: [POST /shutdown] 关闭记忆系统\n"
        "2026-10-30 18:10:28,120 [server.memory.manager] INFO: 记忆系统已关闭\n"
    ).encode()
    g_crash, why_crash = _judge_graceful(fake_shutdown_log, 3221225477)
    checks.append(("F 原生崩溃不被收尾标记骗过", g_crash is False, why_crash))
    g_ok, why_ok = _judge_graceful(fake_shutdown_log, 0)
    checks.append(("F 退出码 0 + 收尾标记 ⇒ 优雅", g_ok is True, why_ok))
    g_nc, why_nc = _judge_graceful(fake_shutdown_log, None)
    checks.append(("F 无退出码时退回标记判定", g_nc is True, why_nc))
    g_empty, _why_empty = _judge_graceful(b"", 0)
    checks.append(("F 无日志内容 ⇒ 非优雅", g_empty is False, str(g_empty)))

    # G：采样必须抓到「真身」而不是转发壳
    shell = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(8)"])
    try:
        time.sleep(1.5)
        import psutil as _p

        picked = Supervisor._pick_backend_proc(  # type: ignore[arg-type]
            Supervisor.__new__(Supervisor), _p.Process(shell.pid)
        )
        picked_pid = picked.pid if picked is not None else None
        checks.append(
            (
                "G 采样抓到真身而非转发壳",
                picked_pid is not None and picked_pid != shell.pid,
                f"picked={picked_pid} shell={shell.pid}",
            )
        )
        _snap = _mem_snapshot(picked) if picked is not None else {}
        checks.append(
            (
                "G 真身句柄数高于壳",
                (_snap.get("handles") or 0) > 0,
                str(_snap.get("handles")),
            )
        )
    finally:
        shell.kill()
        try:
            shell.wait(timeout=10)
        except Exception:
            pass

    # ---- 用例 H/I：崩溃现场留不留得下栈 ----
    # 这是「等下次崩溃再定位」（选项 C）的唯一依据：栈落不下来，那次崩溃照样白等。
    # H = 原生代码里的访问冲突（18:10 那次的形态，SEH），I = 进程被真的打死（信号路径）。
    fault_head = (
        "import ctypes, faulthandler\n"
        "ctypes.windll.kernel32.SetErrorMode(0x0003)\n"  # 不弹系统崩溃框，别干扰桌面
        "def dive(n):\n"
        "    if n == 0:\n"
        "        {CALL}\n"
        "        return 0\n"
        "    return dive(n - 1)\n"
        "dive(3)\n"
    )

    paths_h = new_paths("h")
    av_child = fault_head.replace("{CALL}", "ctypes.string_at(0)")
    Supervisor([sys.executable, "-u", "-c", av_child], paths_h, interval=0.2, takeover=False).run()
    exits_h = _load_prev_exits(paths_h.exit_log, limit=5)
    rec_h = exits_h[-1] if exits_h else {}
    try:
        console_h = paths_h.console_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        console_h = ""
    checks += [
        (
            "H 访问冲突被 faulthandler 记下",
            "Windows fatal exception: access violation" in console_h,
            "控制台无 fatal exception 行（PYTHONFAULTHANDLER 没生效）",
        ),
        (
            "H 栈里指到调用的那一帧",
            "in dive" in console_h,
            "控制台无 dive 帧",
        ),
        ("H 未误判为优雅关闭", rec_h.get("graceful") is False, str(rec_h.get("graceful_why"))),
    ]

    paths_i = new_paths("i")
    seg_child = fault_head.replace("{CALL}", "faulthandler._sigsegv()")
    Supervisor([sys.executable, "-u", "-c", seg_child], paths_i, interval=0.2, takeover=False).run()
    exits_i = _load_prev_exits(paths_i.exit_log, limit=5)
    rec_i = exits_i[-1] if exits_i else {}
    try:
        console_i = paths_i.console_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        console_i = ""
    checks += [
        (
            "I 进程确实崩了（退出码不是 0/1）",
            _u32(int(rec_i.get("exit_code") or 0)) not in (0, 1),
            f"{rec_i.get('exit_code')} / {rec_i.get('exit_hex')}",
        ),
        (
            "I 致命错误也留下了栈",
            "Fatal Python error" in console_i and "in dive" in console_i,
            "控制台无 Fatal Python error / dive 帧",
        ),
        ("I 未误判为优雅关闭", rec_i.get("graceful") is False, str(rec_i.get("graceful_why"))),
    ]

    # ---- 用例 J/K/L：摘要把「最没用的部分」当结论的三处（19:23 那次暴露）----
    fake_tb = (
        b"INFO: something\n"
        b"Traceback (most recent call last):\n"
        b'  File "h11_impl.py", line 415, in run_asgi\n'
        b"    result = await app()\n"
        b'  File "server/ocr.py", line 746, in _run_classic_ocr\n'
        b"    res = ocr.predict(img)\n"
        b'  File "runner.py", line 265, in __call__\n'
        b"    self.predictor.run()\n"
        b"RuntimeError: Unknown exception\n"
        b'INFO:     127.0.0.1:62665 - "POST /x HTTP/1.1" 200 OK\n'
    )
    tb_j = _last_traceback(fake_tb, lines=6)
    checks += [
        (
            "J traceback 取到最内层异常行",
            "RuntimeError: Unknown exception" in tb_j,
            tb_j.replace("\n", " / ")[:70],
        ),
        ("J 不再只给最外层 h11 帧", "h11_impl" not in tb_j, tb_j.replace("\n", " / ")[:70]),
    ]

    tail_k = (
        b"INFO: a\nINFO: b\nWindows fatal exception: access violation\n"
        b"access violation\naccess violation\n"
    )
    tl_k = _tail_lines(tail_k, n=10)
    checks.append(
        ("K 死前输出截在多线程转储之前", any("此后还有" in x for x in tl_k), str(tl_k)[:80])
    )

    try:
        import psutil as _ps3

        snap_self = _mem_snapshot(_ps3.Process())
        checks.append(("L 采样记录带 pid", snap_self.get("pid") == os.getpid(), str(snap_self.get("pid"))))
    except Exception as exc:  # pragma: no cover - psutil 缺失时跳过
        checks.append(("L 采样记录带 pid", False, f"psutil 不可用: {exc}"))

    ok = True
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + ("" if passed else f"  ({detail})"))
        ok = ok and passed
    print(f"\nself-test {'全部通过' if ok else '有失败项'}（{len(checks)} 项）；产物目录 {base}")
    return 0 if ok else 1

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parcae 后端启动外壳（退出监控）")
    parser.add_argument("--sample-interval", type=float, default=30.0, help="内存采样间隔秒，默认 30")
    parser.add_argument("--self-test", action="store_true", help="用假子进程自检，不启动后端")
    parser.add_argument("--no-takeover", action="store_true", help="不处理已有实例（默认先 POST /shutdown 优雅关闭）")
    parser.add_argument(
        "--graceful-timeout",
        type=float,
        default=DEFAULT_GRACEFUL_TIMEOUT,
        help=f"等待存量实例优雅退出的秒数，默认 {DEFAULT_GRACEFUL_TIMEOUT:g}",
    )
    parser.add_argument("--no-supervise", action="store_true", help="只做自检/调试用：直接 exec 后端")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    child = [sys.executable, "-u", "-m", "server.main"]
    if args.no_supervise:
        os.execv(sys.executable, child)  # 理论上不会走到（Windows 上 execv 会替换进程）
        return 0
    return Supervisor(
        child,
        Paths(),
        interval=args.sample_interval,
        takeover=not args.no_takeover,
        graceful_timeout=args.graceful_timeout,
    ).run()


if __name__ == "__main__":
    sys.exit(main())
