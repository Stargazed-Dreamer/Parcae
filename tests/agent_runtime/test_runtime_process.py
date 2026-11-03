"""T4 验收：butler runtime 进程、端口顺延、单实例与 supervisor 自愈。

全部用真实子进程跑（uvicorn 绑定端口的行为无法在进程内可靠模拟）：
- `--port 0` 让内核分配端口，测试不依赖 18766 是否被保留
- 每个用例独立的 runtime 目录与 DB，互不干扰
- 收尾用 `taskkill /F /T` 杀进程树（supervisor 会拖一个子进程）
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DECLARATION_NAME = "butler_runtime.json"
PID_NAME = "butler_runtime.pid"


def _read_declaration(runtime_dir: Path) -> dict | None:
    try:
        return json.loads((runtime_dir / DECLARATION_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _get_json(base_url: str, path: str, *, timeout: float = 2.0) -> tuple[int, dict]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as resp:
        return int(resp.status), json.loads(resp.read().decode("utf-8"))


def _start(
    runtime_dir: Path,
    db_path: Path,
    *,
    port: int = 0,
    args: tuple[str, ...] = (),
    log_path: Path | None = None,
) -> tuple[subprocess.Popen, object]:
    log_file = open(log_path or (runtime_dir / f"child_{time.time_ns()}.log"), "w", encoding="utf-8")  # noqa: SIM115
    cmd = [
        sys.executable,
        "-m",
        "lib.agent_runtime.server",
        "--port",
        str(port),
        "--db",
        str(db_path),
        "--runtime-dir",
        str(runtime_dir),
        *args,
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
    )
    return proc, log_file


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=20,
        )
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _wait_ready(
    runtime_dir: Path,
    proc: subprocess.Popen,
    *,
    timeout: float = 40.0,
    previous_pid: int | None = None,
) -> tuple[dict, dict]:
    """等声明变成 ready 且 /health 返回 200。previous_pid 用于等 supervisor 换新子进程。"""
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"runtime exited early with {proc.returncode}; log tail:\n"
                f"{_log_tail(runtime_dir)}"
            )
        decl = _read_declaration(runtime_dir)
        if decl and decl.get("status") == "ready":
            if previous_pid is not None and decl.get("pid") == previous_pid:
                time.sleep(0.1)
                continue
            try:
                status, health = _get_json(decl["base_url"], "/health", timeout=1.0)
                if status == 200:
                    return decl, health
            except Exception as exc:  # noqa: BLE001 - 启动窗口内连接失败是正常现象
                last_error = repr(exc)
        time.sleep(0.1)
    raise AssertionError(
        f"runtime did not become ready in {timeout}s (last error: {last_error}); "
        f"log tail:\n{_log_tail(runtime_dir)}"
    )


def _log_tail(runtime_dir: Path) -> str:
    parts: list[str] = []
    for path in sorted(runtime_dir.glob("*.log"))[-2:]:
        try:
            parts.append(f"--- {path.name} ---\n{path.read_text(encoding='utf-8', errors='replace')[-3000:]}")
        except OSError:
            pass
    return "\n".join(parts) or "(no log)"


@pytest.fixture
def runtime_env(tmp_path: Path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    db_path = tmp_path / "agent.db"
    started: list[tuple[subprocess.Popen, object]] = []

    def start(*, port: int = 0, args: tuple[str, ...] = ()) -> subprocess.Popen:
        proc, log_file = _start(runtime_dir, db_path, port=port, args=args)
        started.append((proc, log_file))
        return proc

    try:
        yield runtime_dir, db_path, start
    finally:
        for proc, log_file in started:
            _kill_tree(proc)
            log_file.close()


def test_health_and_runtime_port(runtime_env) -> None:
    runtime_dir, _, start = runtime_env
    proc = start()
    decl, health = _wait_ready(runtime_dir, proc)

    assert decl["kind"] == "butler_runtime"
    assert decl["status"] == "ready"
    assert decl["degraded"] is False
    assert decl["preferred_port"] == decl["port"]
    assert decl["format_version"] == 2

    assert health["ok"] is True
    # ⚠️ 不能和 `Popen.pid` 比：`.venv\Scripts\python.exe` 是 uv 的启动器，
    # 真正跑 uvicorn 的是它 spawn 的子进程（实测差 1 个 pid）。判据取三者一致。
    assert health["pid"] == decl["pid"]
    assert health["port"] == decl["port"]
    assert health["format_version"] == 2

    status, info = _get_json(decl["base_url"], "/runtime/port")
    assert status == 200
    assert info["port"] == decl["port"]
    assert info["preferred_port"] == decl["port"]
    assert info["base_url"] == decl["base_url"]
    assert info["pid"] == decl["pid"]
    assert info["declaration"].replace("\\", "/").endswith(DECLARATION_NAME)

    pid_text = (runtime_dir / PID_NAME).read_text(encoding="utf-8").strip()
    assert pid_text == str(decl["pid"])
    assert decl["pid"] > 0


def test_bad_host_header_rejected(runtime_env) -> None:
    runtime_dir, _, start = runtime_env
    proc = start()
    decl, _ = _wait_ready(runtime_dir, proc)
    port = decl["port"]

    for bad_host in ("evil.example", f"127.0.0.2:{port}", f"127.0.0.1:{port + 1}"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET", "/health", headers={"Host": bad_host})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()
        assert resp.status == 421
        assert body["error"]["code"] == "bad_host"

    for good_host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET", "/health", headers={"Host": good_host})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()
        assert resp.status == 200
        assert body["ok"] is True


def test_second_instance_exits_zero(runtime_env) -> None:
    runtime_dir, db_path, start = runtime_env
    proc = start()
    decl, _ = _wait_ready(runtime_dir, proc)

    second = subprocess.run(
        [
            sys.executable,
            "-m",
            "lib.agent_runtime.server",
            "--port",
            "0",
            "--db",
            str(db_path),
            "--runtime-dir",
            str(runtime_dir),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=40,
    )

    assert second.returncode == 0, second.stdout + second.stderr
    assert "already running" in (second.stdout + second.stderr).lower()
    # 第二次启动不许篡改第一次的声明
    assert _read_declaration(runtime_dir)["pid"] == decl["pid"]
    _, health = _get_json(decl["base_url"], "/health")
    assert health["pid"] == decl["pid"]


def test_occupied_port_degrades_to_next(runtime_env) -> None:
    runtime_dir, _, start = runtime_env
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        occupied = blocker.getsockname()[1]
        proc = start(port=occupied)
        decl, health = _wait_ready(runtime_dir, proc)

        assert decl["preferred_port"] == occupied
        assert decl["port"] != occupied
        assert decl["degraded"] is True
        assert health["preferred_port"] == occupied
        assert health["port"] == decl["port"]
        assert health["degraded"] is True
        _, info = _get_json(decl["base_url"], "/runtime/port")
        assert info["preferred_port"] == occupied
    finally:
        blocker.close()


def test_supervisor_restarts_dead_child(runtime_env) -> None:
    runtime_dir, _, start = runtime_env
    supervisor = start(args=("--supervise",))
    decl, _ = _wait_ready(runtime_dir, supervisor)
    first_pid = decl["pid"]
    assert first_pid != supervisor.pid

    subprocess.run(["taskkill", "/F", "/PID", str(first_pid)], capture_output=True, timeout=20)

    decl2, health2 = _wait_ready(runtime_dir, supervisor, timeout=60, previous_pid=first_pid)
    assert decl2["pid"] != first_pid
    assert health2["pid"] == decl2["pid"]
    assert not decl2["base_url"].endswith(":0")


def test_fatal_ledger_exits_2(runtime_env) -> None:
    runtime_dir, db_path, start = runtime_env
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_info (key, value) VALUES ('format_version', '999')"
        )
        conn.commit()
    finally:
        conn.close()

    proc = start()
    returncode = proc.wait(timeout=40)
    assert returncode == 2, _log_tail(runtime_dir)
    assert _read_declaration(runtime_dir) is None or _read_declaration(runtime_dir)["status"] != "ready"
