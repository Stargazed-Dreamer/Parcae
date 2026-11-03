"""BrowserSupervisor：浏览器进程的状态机、健康检查与重启。

生命周期：懒启动（第一次 ensure）→ ready → 健康检查 → degraded/restarting。
不碰 SessionManager 的 page 级连接；它只管浏览器进程是否存在、CDP 是否可达。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Protocol

from .config import SupervisorConfig
from .process import BrowserProcess, ProcessInfo
from .state import BrowserStatus, SupervisorState

logger = logging.getLogger("parcae.browser.supervisor")

_UNHEALTHY_BEFORE_RESTART = 3


class ManagedProcess(Protocol):
    """manager 需要的浏览器进程契约（测试用 FakeProcess 按结构实现它）。"""

    @property
    def info(self) -> ProcessInfo | None: ...

    async def start(self) -> ProcessInfo: ...

    async def wait_ready(self) -> ProcessInfo: ...

    async def healthy(self) -> bool: ...

    def is_alive(self) -> bool: ...

    async def stop(self, *, force: bool = False) -> None: ...


def _detach_process(process: object) -> None:
    """放弃托管但不碰进程（BrowserProcess 有 detach；测试里的 FakeProcess 没有）。"""
    fn = getattr(process, "detach", None)
    if callable(fn):
        fn()


class BrowserSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        *,
        process_factory: Callable[[SupervisorConfig], ManagedProcess] = BrowserProcess,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._process_factory = process_factory
        self._clock = clock
        self._process: ManagedProcess | None = None
        self._state = SupervisorState.STOPPED
        self._last_error: str | None = None
        self._started_at: float | None = None
        self._restarts = 0
        self._unhealthy = 0
        self._lock = asyncio.Lock()
        self._health_task: asyncio.Task[None] | None = None
        if not config.enabled:
            self._state = SupervisorState.STOPPED

    @property
    def config(self) -> SupervisorConfig:
        return self._config

    async def ensure(self, reason: str = "") -> BrowserStatus:
        if not self._config.enabled:
            raise RuntimeError("browser supervisor 已在配置中禁用")
        async with self._lock:
            if self._state is SupervisorState.READY and self._process is not None:
                if await self._process.healthy():
                    return self._status()
            if self._process is not None and self._process.is_alive():
                if self._process_owned():
                    logger.info("supervisor: 现有浏览器不健康，先停止再启动（reason=%s）", reason)
                    await self._process.stop(force=False)
                else:
                    # 接管来的实例（adopt/按端口认领）：只放弃托管，绝不杀别人的浏览器。
                    logger.info("supervisor: 接管的浏览器不健康，放弃托管而不杀进程（reason=%s）", reason)
                    _detach_process(self._process)
                    self._process = None
            elif self._process is not None:
                self._process = None
            return await self._start_locked(reason)

    async def stop(self, *, force: bool = False) -> BrowserStatus:
        async with self._lock:
            await self._stop_locked(force=force)
            return self._status()

    async def restart(self, reason: str = "") -> BrowserStatus:
        """手动重启：不消耗崩溃预算（max_restarts 只约束健康检查触发的自动重启）。"""
        async with self._lock:
            await self._stop_locked(force=False)
            self._restarts = 0
            self._unhealthy = 0
            return await self._start_locked(reason or "manual restart")

    async def status(self) -> BrowserStatus:
        return self._status()

    async def detach(self) -> BrowserStatus:
        """放弃托管但不杀进程（adopt 的旧实例）：停健康检查，状态置 stopped。"""
        async with self._lock:
            await self._cancel_health_task()
            self._state = SupervisorState.STOPPED
            self._write_declaration()
            return self._status()

    def owns_process(self) -> bool:
        """当前托管的进程是否由本 supervisor 启动（adopt 的旧实例不算）。"""
        info = self._process.info if self._process is not None else None
        return bool(info is not None and info.owned)

    def _process_owned(self) -> bool:
        return self.owns_process()

    async def _start_locked(self, reason: str) -> BrowserStatus:
        self._state = SupervisorState.STARTING
        self._last_error = None
        self._write_declaration()
        process = self._process_factory(self._config)
        self._process = process
        try:
            info = await process.start()
            info = await process.wait_ready()
            self._started_at = self._clock()
            self._unhealthy = 0
            self._state = SupervisorState.READY
            self._ensure_health_task()
            self._write_declaration(info)
            logger.info(
                "supervisor: 浏览器就绪 pid=%s port=%s reason=%s",
                info.pid, info.port, reason,
            )
            return self._status(info)
        except Exception as exc:
            self._last_error = str(exc) or type(exc).__name__
            self._state = SupervisorState.FAILED
            try:
                info = process.info
                if info is not None and not info.owned:
                    # 启动是「接管」失败：只放弃托管，别把别人的浏览器一起关掉。
                    _detach_process(process)
                else:
                    await process.stop(force=True)
            except Exception:
                pass
            self._process = None
            self._write_declaration()
            raise

    async def _stop_locked(self, *, force: bool) -> None:
        self._state = SupervisorState.STOPPING
        await self._cancel_health_task()
        if self._process is not None:
            await self._process.stop(force=force)
            self._process = None
        self._started_at = None
        self._state = SupervisorState.STOPPED
        self._write_declaration()

    def _ensure_health_task(self) -> None:
        if self._health_task is not None and not self._health_task.done():
            return
        self._health_task = asyncio.create_task(self._health_loop())

    async def _cancel_health_task(self) -> None:
        task = self._health_task
        if task is None or task is asyncio.current_task():
            self._health_task = None
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("supervisor: 健康检查任务退出异常", exc_info=True)
        self._health_task = None

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.health_interval_secs)
            if self._state not in (SupervisorState.READY, SupervisorState.DEGRADED):
                continue
            process = self._process
            if process is None:
                continue
            try:
                healthy = await process.healthy()
            except Exception as exc:
                # 探活本身抛异常（连接被重置/事件循环竞争）按一次失败计，不许杀掉健康循环。
                logger.debug("supervisor: 健康检查异常: %s", exc)
                healthy = False
            if healthy:
                self._unhealthy = 0
                if self._state is SupervisorState.DEGRADED:
                    self._state = SupervisorState.READY
                    self._write_declaration()
                continue
            self._unhealthy += 1
            self._state = SupervisorState.DEGRADED
            self._last_error = f"连续 {self._unhealthy} 次健康检查失败"
            self._write_declaration()
            if self._unhealthy >= _UNHEALTHY_BEFORE_RESTART:
                await self._restart_from_health()

    async def _restart_from_health(self) -> None:
        failed_process = self._process
        if failed_process is not None and not self._process_owned():
            # ⛔ 接管的实例不归我们处置：连续不健康就放弃托管，绝不 stop(force=True)。
            # 旧行为会在这里把浏览器整个杀掉，是 `/browser/supervisor/ensure` 弄死浏览器的
            # 直接触发点之一（2026-11-01 实测）。
            logger.warning("supervisor: 接管的浏览器连续不健康，放弃托管而不杀进程")
            self._unhealthy = 0
            self._state = SupervisorState.STOPPED
            self._started_at = None
            _detach_process(failed_process)
            self._process = None
            self._write_declaration()
            return
        self._restarts += 1
        if self._restarts > self._config.max_restarts:
            self._state = SupervisorState.FAILED
            self._last_error = f"超过最大重启次数 {self._config.max_restarts}"
            self._write_declaration()
            return
        delay = min(2 ** (self._restarts - 1), self._config.backoff_max_secs)
        self._state = SupervisorState.RESTARTING
        self._write_declaration()
        await asyncio.sleep(delay)
        async with self._lock:
            # 退避期间可能已被手动 ensure/restart/stop 接管：只重启「原来那个」失败进程。
            if self._state is not SupervisorState.RESTARTING or self._process is not failed_process:
                return
            if self._process is not None:
                await self._process.stop(force=True)
                self._process = None
            try:
                await self._start_locked("health check")
            except Exception:
                # _start_locked 已把状态置 failed；保留给下一次手动 restart/ensure。
                logger.warning("supervisor: 健康检查触发的重启失败: %s", self._last_error)

    def _status(self, info: ProcessInfo | None = None) -> BrowserStatus:
        info = info or (self._process.info if self._process is not None else None)
        alive = self._process.is_alive() if self._process is not None else False
        uptime = None
        if alive and self._started_at is not None:
            uptime = max(0.0, self._clock() - self._started_at)
        port = info.port if info is not None else None
        cdp_url = f"http://127.0.0.1:{port}" if port else None
        return BrowserStatus(
            state=self._state.value,
            enabled=self._config.enabled,
            alive=alive,
            pid=info.pid if info is not None else None,
            port=port,
            cdp_url=cdp_url,
            profile=info.profile if info is not None else str(self._config.profile_dir),
            browser_path=info.browser_path if info is not None else "",
            owned=info.owned if info is not None else False,
            started_at=self._started_at,
            uptime_secs=uptime,
            restarts=self._restarts,
            last_error=self._last_error,
        )

    def _write_declaration(self, info: ProcessInfo | None = None) -> None:
        payload = self._status(info).as_dict()
        path = self._config.declaration_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{id(self)}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.debug("supervisor: 写声明文件失败 %s: %s", path, exc)


_supervisor: BrowserSupervisor | None = None
_supervisor_lock = asyncio.Lock()


async def get_browser_supervisor() -> BrowserSupervisor:
    global _supervisor
    if _supervisor is not None:
        return _supervisor
    async with _supervisor_lock:
        if _supervisor is None:
            from server.config import get_supervisor_config

            _supervisor = BrowserSupervisor(
                SupervisorConfig.from_mapping(get_supervisor_config()),
            )
    return _supervisor


async def reset_browser_supervisor(*, close_adopted: bool = False) -> None:
    """回收 supervisor；默认只关闭自己启动的浏览器，adopt 的旧实例留给自裁。"""
    global _supervisor
    supervisor = _supervisor
    if supervisor is None:
        return
    try:
        if close_adopted or supervisor.owns_process():
            await supervisor.stop(force=True)
        else:
            await supervisor.detach()
    finally:
        _supervisor = None
