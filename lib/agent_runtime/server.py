"""butler runtime：独立常驻进程（R-A / Y1）。

职责（第一切片）：
- 持有账本（EventStore）与会话循环，不依赖前端存活
- 端口预探测（18766 起顺延，跳过 WinNAT 保留区）并写独立声明
  `data/runtime/butler_runtime.json`，绝不覆盖 8766 的 `server_port.json`
- 提供 `/health` 与 `/runtime/port`；Host 只允许 127.0.0.1/localhost + 实际端口
- `--supervise`：子进程非零退出按 1s→2s→5s→30s 退避重启；退出 2 视为致命不重启
- 手工启动：`start_butler_runtime.bat`；**不做开机自启**（用户 2026-10-21 裁决）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from lib.agent_runtime.event_store import DEFAULT_DB_PATH, EventStore, LedgerFormatError
from lib.agent_runtime.runtime_api import create_runtime_router, error_payload
from lib.agent_runtime.runtime_deps import build_runtime_deps, settings_from_config
from lib.agent_runtime.runtime_manager import RuntimeApiError, RuntimeManager
from lib.agent_runtime.session_query import ProjectionMissing
from lib.port_binding import bind_first_available, diagnose_commands
from lib.runtime_endpoint import (
    RUNTIME_DIR,
    STATUS_BINDING,
    STATUS_READY,
    STATUS_STOPPED,
    clear_pid_file,
    declared_base_url,
    mark_declaration_status,
    read_declaration,
    resolve_base_url,
    write_declaration,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18766
DECLARATION_NAME = "butler_runtime.json"
PID_NAME = "butler_runtime.pid"
LOG_NAME = "butler_runtime.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("parcae.butler_runtime")


def _paths(runtime_dir: Path) -> tuple[Path, Path, Path]:
    return (
        runtime_dir / DECLARATION_NAME,
        runtime_dir / PID_NAME,
        runtime_dir / LOG_NAME,
    )


def _setup_logging(log_path: Path, *, to_file: bool = True) -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if to_file:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            # 日志文件不可写不能阻止 runtime 启动
            pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def _probe_health(base_url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=timeout) as resp:
            return 200 <= int(resp.status) < 300
    except Exception:
        return False


def _host_allowed(host_header: str, actual_port: int) -> bool:
    if not host_header or actual_port <= 0:
        return False
    host, sep, port_text = host_header.rpartition(":")
    if not sep or not port_text.isdigit():
        return False
    return host.lower() in ("127.0.0.1", "localhost") and int(port_text) == actual_port


async def _prepare_store(db_path: str) -> EventStore:
    store = EventStore(db_path=db_path)
    store.init()
    result = await store.rebuild_projections()
    logger.info(
        "ledger ready: %s (format v%d, projections %s)",
        db_path, store.format_version, result,
    )
    return store


async def _active_sessions(store: EventStore) -> int:
    def _sync() -> int:
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM sessions "
            "WHERE status IN ('streaming', 'awaiting_tools', 'running')"
        ).fetchone()
        return int(row["n"])

    return await store._run_sync(_sync)


def create_app(
    *,
    store: EventStore,
    manager: RuntimeManager,
    host: str,
    port: int,
    preferred_port: int,
    degraded: bool,
    runtime_dir: Path,
    started_at: float,
    api_base_url: str,
) -> FastAPI:
    declaration_path, pid_path, _ = _paths(runtime_dir)
    # 进程内测试（TestClient）用 port=0：无法得知真实监听端口，只在该哨兵下放行 Host
    allow_any_host = port <= 0

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动恢复归 runtime：此时本进程还没有任何活跃 run，不会误伤在跑的会话。
        # GUI 重启不再自己 reconcile（8-3：客户端侧重跑会把活跃 run 补成第二份）。
        try:
            from dataclasses import asdict

            from lib.agent_runtime.reconciler import reconcile

            result = await reconcile(store)
            manager.recovery = asdict(result)
            if result.sessions_interrupted:
                logger.info(
                    "startup recovery: %d session(s) interrupted: %s",
                    len(result.sessions_interrupted), result.sessions_interrupted,
                )
        except Exception:
            logger.exception("startup recovery failed (runtime will continue)")
        mark_declaration_status(STATUS_READY, declaration_path=declaration_path)
        logger.info(
            "runtime ready: http://%s:%d (preferred=%d degraded=%s pid=%d)",
            host, port, preferred_port, degraded, os.getpid(),
        )
        try:
            yield
        finally:
            await manager.shutdown()
            mark_declaration_status(
                STATUS_STOPPED, reason="shutdown", declaration_path=declaration_path
            )
            clear_pid_file(pid_path=pid_path)
            store.close()
            logger.info("runtime stopped")

    app = FastAPI(title="Parcae Butler Runtime", lifespan=lifespan)
    app.state.store = store
    app.state.manager = manager
    app.state.port = port
    app.state.preferred_port = preferred_port
    app.state.degraded = degraded
    app.state.started_at = started_at

    app.include_router(
        create_runtime_router(
            manager,
            port=port,
            host_allowed=_host_allowed,
            allow_any_host=allow_any_host,
        )
    )

    @app.exception_handler(RuntimeApiError)
    async def _runtime_api_error(request: Request, exc: RuntimeApiError) -> JSONResponse:
        return JSONResponse(
            error_payload(exc.code, exc.message, exc.detail), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            error_payload(
                "invalid_argument", "request validation failed",
                {"errors": jsonable_encoder(exc.errors())},
            ),
            status_code=422,
        )

    @app.exception_handler(ProjectionMissing)
    async def _projection_missing(request: Request, exc: ProjectionMissing) -> JSONResponse:
        return JSONResponse(
            error_payload(
                exc.code,
                str(exc),
                {"session_id": exc.session_id, "projection": exc.projection},
            ),
            status_code=500,
        )

    @app.middleware("http")
    async def _host_guard(request: Request, call_next):
        if allow_any_host:
            return await call_next(request)
        server = request.scope.get("server") or (None, None)
        actual_port = int(server[1] or port)
        host_header = request.headers.get("host", "")
        if not _host_allowed(host_header, actual_port):
            return JSONResponse(
                {
                    "error": {
                        "code": "bad_host",
                        "message": (
                            f"Host must be 127.0.0.1:{actual_port} or "
                            f"localhost:{actual_port}"
                        ),
                        "detail": {"host": host_header},
                    }
                },
                status_code=421,
            )
        return await call_next(request)

    @app.get("/health")
    async def health(request: Request) -> dict:
        server = request.scope.get("server") or (None, None)
        actual_port = int(server[1] or port)
        return {
            "ok": True,
            "pid": os.getpid(),
            "port": actual_port,
            "preferred_port": preferred_port,
            "degraded": degraded,
            "uptime": round(time.time() - started_at, 3),
            "format_version": store.format_version,
            "active_sessions": await _active_sessions(store),
        }

    @app.get("/runtime/port")
    async def runtime_port(request: Request) -> dict:
        server = request.scope.get("server") or (None, None)
        actual_port = int(server[1] or port)
        return {
            "ok": True,
            "host": host,
            "port": actual_port,
            "preferred_port": preferred_port,
            "base_url": f"http://{host}:{actual_port}",
            "pid": os.getpid(),
            "degraded": degraded,
            "format_version": store.format_version,
            "declaration": str(declaration_path),
            "api_base_url": api_base_url,
        }

    return app


def _existing_runtime(declaration_path: Path) -> str | None:
    """已有一个健康的 runtime → 返回 base_url（第二次启动应直接退出 0）。"""
    base = declared_base_url(require_alive=True, declaration_path=declaration_path)
    if base and _probe_health(base):
        return base
    return None


async def _serve(args: argparse.Namespace, declaration_path: Path, pid_path: Path) -> None:
    store = await _prepare_store(args.db)
    settings = settings_from_config(resolve_base_url(probe_health=False))
    manager = RuntimeManager(
        store=store,
        settings=settings,
        deps_factory=lambda interrupt_event, provider: build_runtime_deps(
            store, settings, interrupt_event, provider
        ),
    )
    try:
        bind = bind_first_available(args.host, args.port)
        if bind.sock is None:
            lines = [
                f"runtime 启动失败：从 {args.port} 起的 {len(bind.attempts)} 个端口全部不可绑定。",
                f"  {bind.failure_summary}",
                "诊断命令：",
            ]
            lines += [f"  {cmd}" for cmd in diagnose_commands(args.port)]
            msg = "\n".join(lines)
            logger.error(msg)
            print(msg, file=sys.stderr, flush=True)
            raise SystemExit(2)

        sock = bind.sock
        try:
            write_declaration(
                host=args.host,
                port=bind.port,
                preferred_port=bind.preferred_port,
                degraded=bind.degraded,
                reason=bind.reason,
                status=STATUS_BINDING,
                extra={
                    "kind": "butler_runtime",
                    "format_version": store.format_version,
                    "log_file": str(_paths(args.runtime_dir)[2]),
                    "api_base_url": settings.base_url,
                },
                declaration_path=declaration_path,
                pid_path=pid_path,
            )
            if bind.degraded:
                logger.warning(
                    "port degraded: preferred %d unavailable (%s), using %d",
                    args.port, bind.reason, bind.port,
                )
            app = create_app(
                store=store,
                manager=manager,
                host=args.host,
                port=bind.port,
                preferred_port=bind.preferred_port,
                degraded=bind.degraded,
                runtime_dir=args.runtime_dir,
                started_at=time.time(),
                api_base_url=settings.base_url,
            )
            config = uvicorn.Config(
                app,
                host=args.host,
                port=bind.port,
                log_config=None,
                access_log=False,
                lifespan="on",
            )
            server = uvicorn.Server(config)
            logger.info("uvicorn serving on %s:%d", args.host, bind.port)
            await server.serve(sockets=[sock])
        finally:
            try:
                sock.close()
            except OSError:
                pass
    finally:
        store.close()


def _run_once(args: argparse.Namespace) -> int:
    declaration_path, pid_path, log_path = _paths(args.runtime_dir)
    _setup_logging(log_path)
    existing = _existing_runtime(declaration_path)
    if existing:
        msg = f"butler runtime already running at {existing}; nothing to do"
        logger.info(msg)
        print(msg, flush=True)
        return 0
    try:
        asyncio.run(_serve(args, declaration_path, pid_path))
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except LedgerFormatError as exc:
        logger.error("ledger refused to open: %s", exc)
        print(f"runtime 启动失败：{exc}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.exception("runtime crashed")
        return 1
    finally:
        # 只清理自己写的声明：别把另一个正在启动的 runtime 的声明改成 stopped
        decl = read_declaration(declaration_path=declaration_path)
        if decl is None or decl.get("pid") == os.getpid():
            mark_declaration_status(
                STATUS_STOPPED, reason="exit", declaration_path=declaration_path
            )
            clear_pid_file(pid_path=pid_path)


def _supervise(args: argparse.Namespace) -> int:
    _, _, log_path = _paths(args.runtime_dir)
    _setup_logging(log_path, to_file=False)
    cmd = [
        sys.executable,
        "-m",
        "lib.agent_runtime.server",
        "--host", args.host,
        "--port", str(args.port),
        "--db", args.db,
        "--runtime-dir", str(args.runtime_dir),
    ]
    delays = [1, 2, 5, 30]
    attempt = 0
    while True:
        logger.info("supervisor starting child: %s", " ".join(cmd))
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))
            returncode = proc.wait()
        except KeyboardInterrupt:
            logger.info("supervisor interrupted")
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            return 0
        if returncode == 0:
            logger.info("child exited 0; supervisor exits")
            return 0
        if returncode == 2:
            logger.error("child exited 2 (fatal); supervisor exits")
            return 2
        delay = delays[min(attempt, len(delays) - 1)]
        attempt += 1
        logger.warning("child exited %s; restarting in %ss", returncode, delay)
        time.sleep(delay)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parcae butler runtime")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="首选端口；0 = 测试用内核分配")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--runtime-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--supervise", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        raise SystemExit("--port must be between 0 and 65535")
    args.runtime_dir = Path(args.runtime_dir).resolve()
    if args.supervise:
        return _supervise(args)
    return _run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
