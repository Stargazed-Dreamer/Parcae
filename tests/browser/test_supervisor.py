"""浏览器 supervisor：状态机、健康检查、声明文件与 HTTP 端点。

不启动真实 Chrome：管理器测试用 FakeProcess，端口/参数测试只碰纯函数与临时目录。
真实进程生命周期（Job Object / 杀树 / adopt）留给集成用例。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.browser.supervisor.browser_paths import build_browser_args
from server.browser.supervisor.config import SupervisorConfig
from server.browser.supervisor.manager import BrowserSupervisor
from server.browser.supervisor.process import (
    BrowserPortInUse,
    BrowserProcess,
    BrowserStartTimeout,
    ProcessInfo,
)
from server.browser.supervisor.state import BrowserStatus, SupervisorState


class FakeProcess:
    def __init__(
        self,
        config: SupervisorConfig,
        *,
        fail_start: Exception | None = None,
        owned: bool = True,
    ) -> None:
        self.config = config
        self.fail_start = fail_start
        self.owned = owned
        self.info: ProcessInfo | None = None
        self.stop_calls: list[bool] = []
        self.healthy_flag = True
        self.fail_health_calls: int | None = None
        self._alive = False

    async def start(self) -> ProcessInfo:
        if self.fail_start is not None:
            raise self.fail_start
        self._alive = True
        self.info = ProcessInfo(
            pid=4000,
            create_time=1.0,
            port=self.config.port or 55000,
            profile=str(self.config.profile_dir),
            browser_path="fake-browser",
            owned=self.owned,
        )
        return self.info

    async def wait_ready(self) -> ProcessInfo:
        assert self.info is not None
        return self.info

    async def healthy(self) -> bool:
        if self.fail_health_calls is not None and self.fail_health_calls > 0:
            self.fail_health_calls -= 1
            return False
        return self.healthy_flag and self._alive

    def is_alive(self) -> bool:
        return self._alive

    async def stop(self, *, force: bool = False) -> None:
        self.stop_calls.append(force)
        self._alive = False
        self.info = None


def _config(tmp_path: Path, **overrides) -> SupervisorConfig:
    base = {
        "port": 9222,
        "port_file": str(tmp_path / "agent_browser.json"),
        "user_data_dir": str(tmp_path / "profile"),
        "health_interval_secs": 0.01,
        "health_timeout_secs": 0.05,
        "startup_timeout_secs": 1.0,
        "close_grace_secs": 0.0,
        "exit_wait_secs": 0.0,
        "max_restarts": 3,
        "backoff_max_secs": 0.0,
    }
    base.update(overrides)
    return SupervisorConfig(**base)


def _factory(
    made: list[FakeProcess],
    *,
    fail_start: Exception | None = None,
    owned: bool = True,
):
    def make(config: SupervisorConfig) -> FakeProcess:
        proc = FakeProcess(config, fail_start=fail_start, owned=owned)
        made.append(proc)
        return proc

    return make


def _declaration(config: SupervisorConfig) -> dict:
    return json.loads(Path(config.port_file).read_text(encoding="utf-8"))


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def test_ensure_starts_once_and_writes_declaration(tmp_path):
    config = _config(tmp_path)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made))

    async def scenario():
        try:
            status = await supervisor.ensure("test")
            assert status.state == SupervisorState.READY.value
            assert status.alive is True
            assert status.port == 9222
            assert status.cdp_url == "http://127.0.0.1:9222"
            assert len(made) == 1

            declaration = _declaration(config)
            assert declaration["state"] == "ready"
            assert declaration["pid"] == 4000
            assert declaration["port"] == 9222

            again = await supervisor.ensure("test")
            assert again.pid == status.pid
            assert len(made) == 1
        finally:
            await supervisor.stop(force=False)

    asyncio.run(scenario())
    assert made[0].stop_calls == [False]
    assert _declaration(config)["state"] == "stopped"


def test_detach_stops_health_checks_but_keeps_adopted_process(tmp_path):
    config = _config(tmp_path)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made, owned=False))

    async def scenario():
        status = await supervisor.ensure("test")
        assert status.owned is False
        assert supervisor.owns_process() is False
        assert len(made) == 1

        detached = await supervisor.detach()
        assert detached.state == SupervisorState.STOPPED.value
        assert detached.alive is True
        assert made[0].stop_calls == []
        # 健康任务已停：再改健康标志也不会触发重启
        made[0].healthy_flag = False
        await asyncio.sleep(0.1)
        assert len(made) == 1

    asyncio.run(scenario())


def test_restart_resets_crash_budget(tmp_path):
    config = _config(tmp_path, max_restarts=1)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made))

    async def scenario():
        try:
            await supervisor.ensure("test")
            status = await supervisor.restart("manual")
            assert status.state == SupervisorState.READY.value
            assert status.restarts == 0
            assert len(made) == 2
        finally:
            await supervisor.stop(force=True)

    asyncio.run(scenario())


def test_start_failure_marks_failed_and_cleans_up(tmp_path):
    config = _config(tmp_path)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(
        config, process_factory=_factory(made, fail_start=BrowserStartTimeout("boom"))
    )

    async def scenario():
        with pytest.raises(BrowserStartTimeout):
            await supervisor.ensure("test")
        status = await supervisor.status()
        assert status.state == SupervisorState.FAILED.value
        assert status.alive is False
        assert status.last_error == "boom"
        assert made[0].stop_calls == [True]
        assert _declaration(config)["state"] == "failed"

    asyncio.run(scenario())


def test_three_unhealthy_checks_trigger_restart(tmp_path):
    config = _config(tmp_path)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made))

    async def scenario():
        try:
            await supervisor.ensure("test")
            assert len(made) == 1
            made[0].healthy_flag = False
            assert await _wait_until(lambda: len(made) == 2)
            status = await supervisor.status()
            assert status.state == SupervisorState.READY.value
            assert status.restarts == 1
            assert made[0].stop_calls == [True]
        finally:
            await supervisor.stop(force=True)

    asyncio.run(scenario())


def test_no_health_loop_means_no_restart(tmp_path, monkeypatch):
    """反向验证：摘掉健康循环后，进程被杀不会触发任何重启。"""
    config = _config(tmp_path)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made))
    monkeypatch.setattr(supervisor, "_ensure_health_task", lambda: None)

    async def scenario():
        try:
            await supervisor.ensure("test")
            made[0].healthy_flag = False
            await asyncio.sleep(0.2)
            assert len(made) == 1
            assert made[0].stop_calls == []
            status = await supervisor.status()
            assert status.state == SupervisorState.READY.value
        finally:
            await supervisor.stop(force=True)

    asyncio.run(scenario())


def test_manual_ensure_during_backoff_cancels_pending_restart(tmp_path):
    config = _config(
        tmp_path, health_interval_secs=0.01, backoff_max_secs=0.3, max_restarts=3
    )
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made))

    async def scenario():
        try:
            await supervisor.ensure("test")
            made[0].healthy_flag = False
            assert await _wait_until(
                lambda: _declaration(config)["state"] == "restarting"
            )
            await supervisor.ensure("manual")
            assert len(made) == 2
            await asyncio.sleep(0.5)
            assert len(made) == 2
        finally:
            await supervisor.stop(force=True)

    asyncio.run(scenario())


def test_restart_limit_marks_failed(tmp_path):
    config = _config(tmp_path, max_restarts=0)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made))

    async def scenario():
        try:
            await supervisor.ensure("test")
            made[0].healthy_flag = False
            assert await _wait_until(
                lambda: _declaration(config)["state"] == "failed"
            )
            status = await supervisor.status()
            assert status.state == SupervisorState.FAILED.value
            assert "最大重启次数" in (status.last_error or "")
            assert len(made) == 1
        finally:
            await supervisor.stop(force=True)

    asyncio.run(scenario())


def test_recovery_from_degraded_returns_to_ready(tmp_path):
    # 间隔放大到 50ms，让 degraded 状态在两次探活之间有可观测窗口。
    config = _config(tmp_path, health_interval_secs=0.05, max_restarts=99)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made))

    async def scenario():
        try:
            await supervisor.ensure("test")
            made[0].fail_health_calls = 2
            assert await _wait_until(
                lambda: _declaration(config)["state"] == "degraded"
            )
            assert await _wait_until(
                lambda: _declaration(config)["state"] == "ready", timeout=1.0
            )
            assert len(made) == 1
        finally:
            await supervisor.stop(force=True)

    asyncio.run(scenario())


def test_browser_args_are_real_chrome_without_headless(tmp_path):
    args = build_browser_args(
        browser_path="chrome.exe", port=9222, profile_dir=tmp_path
    )
    joined = " ".join(args)
    assert "--headless" not in joined
    assert "--remote-debugging-port=9222" in args
    assert f"--user-data-dir={tmp_path}" in args
    assert args[-1] == "about:blank"


def test_dynamic_port_ignores_stale_devtools_active_port(tmp_path):
    config = _config(tmp_path, port=0)
    profile = Path(config.user_data_dir)
    profile.mkdir()
    port_file = profile / "DevToolsActivePort"
    port_file.write_text("55001\n/devtools/browser/old", encoding="utf-8")

    async def probe(port: int, timeout: float) -> dict | None:
        return None

    process = BrowserProcess(config, probe=probe)
    process._port_file_before = process._snapshot_port_file(profile)
    assert process._resolve_port() is None

    port_file.write_text("55002\n/devtools/browser/new", encoding="utf-8")
    assert process._resolve_port() == 55002


class FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def _status(self) -> BrowserStatus:
        return BrowserStatus(
            state=SupervisorState.READY.value,
            enabled=True,
            alive=True,
            pid=4000,
            port=9222,
            cdp_url="http://127.0.0.1:9222",
            profile="C:/tmp/profile",
            browser_path="chrome.exe",
            owned=True,
            started_at=1.0,
            uptime_secs=2.0,
            restarts=0,
        )

    async def status(self) -> BrowserStatus:
        return self._status()

    async def ensure(self, reason: str = "") -> BrowserStatus:
        self.calls.append(("ensure", reason))
        return self._status()

    async def stop(self, *, force: bool = False) -> BrowserStatus:
        self.calls.append(("stop", force))
        return self._status()

    async def restart(self, reason: str = "") -> BrowserStatus:
        self.calls.append(("restart", reason))
        return self._status()


class PortInUseSupervisor(FakeSupervisor):
    async def ensure(self, reason: str = "") -> BrowserStatus:
        raise BrowserPortInUse("端口 9222 已被占用")


def _install_supervisor(monkeypatch, supervisor) -> None:
    from server.browser import supervisor_endpoints as endpoints

    async def _get():
        return supervisor

    monkeypatch.setattr(endpoints, "get_browser_supervisor", _get)


def test_status_endpoint(client, monkeypatch):
    supervisor = FakeSupervisor()
    _install_supervisor(monkeypatch, supervisor)

    response = client.get("/browser/supervisor/status")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["state"] == "ready"
    assert data["port"] == 9222
    assert data["restarts"] == 0


def test_ensure_endpoint_passes_reason(client, monkeypatch):
    supervisor = FakeSupervisor()
    _install_supervisor(monkeypatch, supervisor)

    response = client.post("/browser/supervisor/ensure", json={"reason": "smoke"})
    assert response.status_code == 200
    assert supervisor.calls == [("ensure", "smoke")]


def test_stop_endpoint_passes_force(client, monkeypatch):
    supervisor = FakeSupervisor()
    _install_supervisor(monkeypatch, supervisor)

    response = client.post("/browser/supervisor/stop", json={"force": True})
    assert response.status_code == 200
    assert supervisor.calls == [("stop", True)]


def test_restart_endpoint_passes_reason(client, monkeypatch):
    supervisor = FakeSupervisor()
    _install_supervisor(monkeypatch, supervisor)

    response = client.post("/browser/supervisor/restart", json={"reason": "manual"})
    assert response.status_code == 200
    assert supervisor.calls == [("restart", "manual")]


def test_port_in_use_maps_to_409(client, monkeypatch):
    _install_supervisor(monkeypatch, PortInUseSupervisor())

    response = client.post("/browser/supervisor/ensure")
    assert response.status_code == 409
    assert "BROWSER_PORT_IN_USE" in response.json()["detail"]


# --- 浏览器被 supervisor 弄死的回归（2026-11-01 实测）--------------------------------


def test_is_alive_follows_relaunched_pid(monkeypatch, tmp_path):
    """Popen 的启动器秒退、真身换了 pid：alive 必须按端口反查认回来。

    实测：`popen=917228` 1s 内 exit 0，`917328` 才是 LISTEN 9222 的进程。旧实现只看
    `_proc.poll()` ⇒ alive 恒 False ⇒ 健康循环连判 3 次不健康 ⇒ 重启并杀掉浏览器。
    """
    from server.browser.supervisor import process as process_mod

    browser = BrowserProcess(_config(tmp_path))
    browser._info = ProcessInfo(
        pid=111,
        create_time=1.0,
        port=9222,
        profile="p",
        browser_path="chrome.exe",
        owned=True,
        launcher_pid=111,
    )

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def is_running(self) -> bool:
            return self.pid == 222

        def create_time(self) -> float:
            return 7.5

        def name(self) -> str:
            return "chrome.exe"

    monkeypatch.setattr(process_mod.psutil, "Process", FakeProc)
    monkeypatch.setattr(process_mod, "_listener_pid", lambda port, **kw: 222)

    assert browser.is_alive() is True
    assert browser.info is not None
    assert browser.info.pid == 222
    assert browser.info.create_time == 7.5


def test_is_alive_does_not_claim_for_adopted(monkeypatch, tmp_path):
    """接管的实例 pid 对不上就是真死了，不许把端口上别人的浏览器认成自己的。"""
    from server.browser.supervisor import process as process_mod

    browser = BrowserProcess(_config(tmp_path))
    browser._info = ProcessInfo(
        pid=111, create_time=1.0, port=9222, profile="p", browser_path="chrome.exe", owned=False
    )

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def is_running(self) -> bool:
            return False

        def create_time(self) -> float:
            return 7.5

        def name(self) -> str:
            return "chrome.exe"

    monkeypatch.setattr(process_mod.psutil, "Process", FakeProc)
    monkeypatch.setattr(process_mod, "_listener_pid", lambda port, **kw: 222)

    assert browser.is_alive() is False


def test_adopt_falls_back_to_port_listener(monkeypatch, tmp_path):
    """Chrome 会清空自己的命令行，cmdline 匹配不上时按端口监听者认领。

    否则「上次起的实例还活着但 cmdline 不可读」⇒ 每次 ensure 都 BrowserPortInUse。
    """
    from server.browser.supervisor import process as process_mod

    browser = BrowserProcess(_config(tmp_path))

    async def fake_probe(port: int, timeout: float) -> dict:
        return {"Browser": "Chrome/156.0.0.0"}

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return 3.0

        def name(self) -> str:
            return "chrome.exe"

    monkeypatch.setattr(browser, "_probe", fake_probe)
    monkeypatch.setattr(process_mod.psutil, "process_iter", lambda *a, **k: [])
    monkeypatch.setattr(process_mod.psutil, "Process", FakeProc)
    monkeypatch.setattr(process_mod, "_listener_pid", lambda port, **kw: 333)

    info = asyncio.run(browser.adopt(9222))
    assert info is not None
    assert info.pid == 333
    assert info.owned is False


def test_health_failure_detaches_adopted_instead_of_killing(tmp_path):
    """接管的实例连续不健康 ⇒ 放弃托管，绝不 stop(force=True) 杀掉别人的浏览器。"""
    config = _config(tmp_path)
    made: list[FakeProcess] = []
    supervisor = BrowserSupervisor(config, process_factory=_factory(made, owned=False))

    async def scenario():
        await supervisor.ensure("test")
        assert len(made) == 1
        made[0].healthy_flag = False
        assert await _wait_until(lambda: supervisor._process is None)
        assert made[0].stop_calls == []
        status = await supervisor.status()
        assert status.state == SupervisorState.STOPPED.value
        assert len(made) == 1
        await supervisor.stop(force=False)

    asyncio.run(scenario())
