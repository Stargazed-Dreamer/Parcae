"""浏览器进程的启动、探活、停止与进程树清理。

只负责「一个浏览器进程」；状态机/重启策略在 manager.py。所有等待都有上限，
Windows 上 kill 是异步的，重启前必须等旧进程真正退出（否则新 Chrome 会 hand-off
给旧实例并以 exit code 0 退出）。

⛔ **Popen 的 pid 不是浏览器主进程**（2026-11-01 实测）：Windows 上 Chrome 起来后会
relaunch 自身，`subprocess.Popen` 拿到的那个进程 **1 秒内以 exit code 0 退出**，真正的
browser 进程是它的子进程（实测 `popen=917228` 秒退、`917328` 才是 LISTEN 9222 的那个）。
所以：① 存活判据必须能「按端口反查监听 pid」，不能只看 `_proc.poll()`；② 绝不能把
Job Object assign 给这个会退出的启动器——新进程继承 job，`close()` 的 kill-on-close
会顺手把真正在跑的浏览器一起杀掉（这正是 `/browser/supervisor/ensure` 把浏览器弄死的
根因，见 `.agents/memory/2026-11-01.md`）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from .browser_paths import build_browser_args, detect_browser_path
from .config import SupervisorConfig

logger = logging.getLogger("parcae.browser.supervisor.process")

_IS_WINDOWS = sys.platform == "win32"
Probe = Callable[[int, float], Awaitable[dict | None]]

# 端口 → (采样时刻, 监听 pid)；只在「记录的 pid 已失效」时才查，避免每次状态轮询都全表扫。
_LISTENER_CACHE: dict[int, tuple[float, int | None]] = {}
_LISTENER_TTL_SECS = 1.0
_BROWSER_NAMES = ("chrome", "chromium", "msedge", "brave", "vivaldi")


def _is_browser_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in _BROWSER_NAMES)


def _listener_pid(port: int, *, max_age: float = _LISTENER_TTL_SECS) -> int | None:
    """谁在 LISTEN 这个端口；失败返回 None（权限/无监听/psutil 异常都当未知）。"""
    if port <= 0:
        return None
    now = time.monotonic()
    cached = _LISTENER_CACHE.get(port)
    if cached is not None and now - cached[0] < max_age:
        return cached[1]
    pid: int | None = None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            laddr = conn.laddr
            if laddr and getattr(laddr, "port", None) == port and conn.status == psutil.CONN_LISTEN and conn.pid:
                pid = int(conn.pid)
                break
    except (psutil.Error, OSError):
        pid = None
    _LISTENER_CACHE[port] = (now, pid)
    return pid


class BrowserLaunchError(RuntimeError):
    """浏览器无法启动/无法就绪。"""


class BrowserNotFound(BrowserLaunchError):
    pass


class BrowserPortInUse(BrowserLaunchError):
    pass


class BrowserStartTimeout(BrowserLaunchError):
    pass


class BrowserProcessExited(BrowserLaunchError):
    pass


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    create_time: float
    port: int
    profile: str
    browser_path: str
    owned: bool = True
    # Popen 直接拿到的 pid；Chrome 会 relaunch 自身，所以它通常 != 真正的 browser pid。
    launcher_pid: int | None = None


async def _probe_version(port: int, timeout: float) -> dict | None:
    """GET /json/version；成功返回 dict，失败/超时返回 None。"""
    from lib.async_http import get_async_client

    try:
        resp = await get_async_client().get(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout,
        )
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _chromium_product(info: dict) -> bool:
    product = str(info.get("Browser", ""))
    return bool(product) and any(name in product for name in ("Chrome", "Chromium", "Edg", "Brave", "Vivaldi"))


class BrowserProcess:
    """拥有一个浏览器根进程；adopt 的旧实例 `owned=False`，stop 仍会清理它。"""

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        probe: Probe = _probe_version,
    ) -> None:
        self._config = config
        self._popen = popen
        self._probe = probe
        self._proc: subprocess.Popen[bytes] | None = None
        self._info: ProcessInfo | None = None
        self._port_file_before: tuple[float, str] | None = None

    @property
    def info(self) -> ProcessInfo | None:
        return self._info

    async def start(self) -> ProcessInfo:
        browser_path = detect_browser_path(self._config.browser_path)
        if not browser_path:
            raise BrowserNotFound("未找到 Chromium 内核浏览器（Chrome/Edge/Brave/Vivaldi）")

        profile = self._config.profile_dir
        profile.mkdir(parents=True, exist_ok=True)

        if self._config.port > 0 and _port_open(self._config.port):
            adopted = await self.adopt(self._config.port)
            if adopted is not None:
                return adopted
            raise BrowserPortInUse(
                f"端口 {self._config.port} 已被非本 supervisor 的进程占用；"
                "请换端口或先关闭占用者"
            )

        self._port_file_before = self._snapshot_port_file(profile)
        args = build_browser_args(
            browser_path=browser_path,
            port=self._config.port,
            profile_dir=profile,
        )
        try:
            if _IS_WINDOWS:
                proc = self._popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = self._popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise BrowserLaunchError(f"启动浏览器失败: {exc}") from exc

        create_time = time.time()
        try:
            create_time = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            pass
        self._proc = proc
        self._info = ProcessInfo(
            pid=proc.pid,
            create_time=create_time,
            port=self._config.port,
            profile=str(profile),
            browser_path=browser_path,
            owned=True,
            launcher_pid=proc.pid,
        )
        # ⛔ 不建 Job Object：Chrome 会 relaunch 自身，Popen 的 pid 只是启动器；assign 后
        # 新进程继承 job，supervisor 这边一旦 close()（kill-on-close）就把真正在跑的浏览器
        # 一起杀掉。进程清理改走「按端口反查真实 pid → 杀进程树」（见 stop/_kill_tree）。
        return self._info

    async def wait_ready(self) -> ProcessInfo:
        info = self._info
        if info is None:
            raise BrowserLaunchError("浏览器尚未启动")
        deadline = time.monotonic() + self._config.startup_timeout_secs
        while time.monotonic() < deadline:
            # 先看 CDP 通不通：Chrome 常常已经起来了，只是 Popen 那个启动器进程退出了。
            port = self._resolve_port()
            if port is not None:
                version = await self._probe(port, self._config.health_timeout_secs)
                if version is not None:
                    info.port = port
                    self._adopt_listener_pid()
                    return info
            if not self.is_alive():
                raise BrowserProcessExited("浏览器在就绪前退出")
            await asyncio.sleep(0.25)
        if info.owned:
            await self.stop(force=True)
        raise BrowserStartTimeout(
            f"浏览器 {self._config.startup_timeout_secs}s 内未暴露 CDP"
        )

    async def healthy(self) -> bool:
        info = self._info
        if info is None or not self.is_alive():
            return False
        version = await self._probe(info.port, self._config.health_timeout_secs)
        return version is not None

    def _adopt_listener_pid(self) -> bool:
        """把 info.pid 修正成「真正在 LISTEN 该端口的浏览器进程」。

        Chrome 会 relaunch 自身（Popen 的 pid 秒退、exit code 0），只有端口监听者才是真身。
        修正后 `_proc` 置 None，后续一律走 psutil + pid 路径。
        """
        info = self._info
        if info is None:
            return False
        pid = _listener_pid(info.port)
        if pid is None:
            return False
        if pid == info.pid:
            return True
        try:
            proc = psutil.Process(pid)
            if not _is_browser_name(proc.name()):
                return False
            info.pid = pid
            info.create_time = proc.create_time()
            info.browser_path = proc.name()
            self._proc = None
            logger.info("supervisor: 浏览器由启动器 relaunch，pid 修正为 %s（启动器已退出）", pid)
            return True
        except psutil.Error:
            return False

    def _pid_alive(self, pid: int, create_time: float) -> bool:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return False
            return abs(proc.create_time() - create_time) < 0.01
        except psutil.Error:
            return False

    def is_alive(self) -> bool:
        info = self._info
        if info is None:
            return False
        if self._pid_alive(info.pid, info.create_time):
            return True
        # pid 对不上：自己启动的那台可能被 Chrome relaunch 换了 pid，按端口反查认回来。
        # 接管的实例（owned=False）不去认领，避免把别人的浏览器算成自己的。
        if info.owned or info.launcher_pid is not None:
            return self._adopt_listener_pid()
        return False

    def detach(self) -> None:
        """放弃托管但不碰进程（接管来的实例用：supervisor 无权杀它）。"""
        self._proc = None
        self._info = None

    async def stop(self, *, force: bool = False) -> None:
        info = self._info
        if info is None:
            return
        if not force:
            await self._send_browser_close(info.port)
            if await self._wait_exit(self._config.close_grace_secs):
                self._release()
                return
        self._terminate()
        if await self._wait_exit(self._config.exit_wait_secs):
            self._release()
            return
        self._kill_tree()
        await self._wait_exit(1.0)
        self._release()

    async def adopt(self, port: int) -> ProcessInfo | None:
        """按 profile + 端口认领一个已在运行的 Chromium（best-effort）。"""
        if not self._config.adopt_existing:
            return None
        version = await self._probe(port, self._config.health_timeout_secs)
        if version is None or not _chromium_product(version):
            return None
        profile = str(self._config.profile_dir)
        marker = f"--user-data-dir={profile}"
        port_marker = f"--remote-debugging-port={port}"
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                joined = " ".join(cmdline)
                if "--type=" in joined:
                    continue
                if marker in joined and port_marker in joined:
                    name = str(proc.info.get("name") or "")
                    if not any(x in name.lower() for x in ("chrome", "msedge", "brave", "vivaldi")):
                        continue
                    self._info = ProcessInfo(
                        pid=int(proc.info["pid"]),
                        create_time=float(proc.info["create_time"]),
                        port=port,
                        profile=profile,
                        browser_path=name,
                        owned=False,
                    )
                    logger.info("已认领同 profile 的浏览器 pid=%s port=%s", proc.info["pid"], port)
                    return self._info
            except (psutil.Error, TypeError, ValueError):
                continue
        # 兜底：Chrome 会清空/改写自己的命令行（实测日常实例 cmdline 为空），cmdline 匹配
        # 不上时按「LISTEN 该端口的浏览器进程」认领，否则端口被自己上次起的实例占着就永远
        # 变成 BrowserPortInUse（FAILED），既接管不了也起不来。
        pid = _listener_pid(port)
        if pid is not None:
            try:
                proc = psutil.Process(pid)
                if _is_browser_name(proc.name()):
                    self._info = ProcessInfo(
                        pid=pid,
                        create_time=proc.create_time(),
                        port=port,
                        profile=profile,
                        browser_path=proc.name(),
                        owned=False,
                    )
                    logger.info("按端口监听认领浏览器 pid=%s port=%s（命令行不可读）", pid, port)
                    return self._info
            except psutil.Error:
                pass
        return None

    def _snapshot_port_file(self, profile: Path) -> tuple[float, str] | None:
        path = profile / "DevToolsActivePort"
        try:
            return path.stat().st_mtime, path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _resolve_port(self) -> int | None:
        if self._config.port > 0:
            return self._config.port
        path = self._config.profile_dir / "DevToolsActivePort"
        try:
            mtime = path.stat().st_mtime
            contents = path.read_text(encoding="utf-8")
        except OSError:
            return None
        before = self._port_file_before
        if before is not None and mtime <= before[0] and contents == before[1]:
            return None
        try:
            port = int(contents.splitlines()[0].strip())
        except (IndexError, ValueError):
            return None
        return port if 0 < port <= 65535 else None

    async def _send_browser_close(self, port: int) -> None:
        version = await self._probe(port, 1.0)
        ws_url = str((version or {}).get("webSocketDebuggerUrl") or "")
        if not ws_url:
            return
        try:
            from websockets.asyncio.client import connect

            async with connect(ws_url, open_timeout=1.0) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                await asyncio.wait_for(ws.recv(), timeout=1.0)
        except Exception:
            # 关闭前询问是 best-effort；失败就走 terminate。
            pass

    async def _wait_exit(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(seconds, 0)
        while True:
            if not self.is_alive():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def _terminate(self) -> None:
        info = self._info
        if info is None:
            return
        # info.pid 可能已被修正成 relaunch 后的真身，所以先按 pid 打，再兜底 Popen 句柄。
        try:
            psutil.Process(info.pid).terminate()
        except psutil.Error:
            pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    def _kill_tree(self) -> None:
        info = self._info
        if info is None:
            return
        try:
            root = psutil.Process(info.pid)
            children = root.children(recursive=True)
        except psutil.Error:
            children = []
        for proc in children:
            try:
                proc.kill()
            except psutil.Error:
                pass
        try:
            root = psutil.Process(info.pid)
            root.kill()
        except psutil.Error:
            pass
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def _release(self) -> None:
        if self._proc is not None:
            try:
                self._proc.wait(timeout=0)
            except Exception:
                pass
        self._proc = None
        self._info = None
