"""UI 集成测试用的真 runtime（in-process uvicorn，port 0）。

面板的读走 HTTP、启动恢复归 runtime，所以恢复类用例必须有一个真 runtime：
lifespan 里跑 reconcile，`GET /admin/recovery` 把结果交给面板 banner。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from lib.agent_runtime.event_store import EventStore
from lib.agent_runtime.runtime_manager import RuntimeManager, RuntimeSettings
from lib.agent_runtime.server import create_app


async def _no_deps(*_args, **_kwargs):
    raise RuntimeError("deps_factory is not used by read-only UI tests")


@contextmanager
def runtime_for(db_path: str) -> Iterator[str]:
    store = EventStore(db_path=db_path)
    store.init()
    manager = RuntimeManager(
        store=store,
        settings=RuntimeSettings(),
        deps_factory=_no_deps,
    )
    runtime_dir = Path(db_path).parent / "runtime"
    app = create_app(
        store=store,
        manager=manager,
        host="127.0.0.1",
        port=0,
        preferred_port=0,
        degraded=False,
        runtime_dir=runtime_dir,
        started_at=time.time(),
        api_base_url="http://127.0.0.1:8766",
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="ui-test-runtime")
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "ui-test runtime did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        # lifespan 的 finally 会 close store；这里只兜底等待线程退出
        assert not thread.is_alive(), "ui-test runtime did not stop"


def run_coro(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
