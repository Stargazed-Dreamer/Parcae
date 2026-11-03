"""P5 T8：MCP 直连面收敛 + 并行调用契约。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from server.butler.service import next_context, open_session
from server.butler.store import ButlerStore
from server.mcp_whitelist import DIRECT_TOOLS, GATEWAY_EXCLUDE
from server.memory.store import MemoryStore
from server.todos.store import TodosStore


def test_direct_surface_is_slim():
    # 常驻：core + browser + 记忆/会话协议 + 有调用计数证据的高频工具
    for name in ("agent_guide", "parcae_advanced_tool", "browser_snapshot",
                 "memory_get", "butler_session", "butler_context", "butler_wrapup",
                 "exec_python", "exec_cmd", "exec_inspect", "exec_terminal_detail",
                 "exec_status", "capture_screen", "execute_action", "video_frame"):
        assert name in DIRECT_TOOLS
    # 下沉到网关：其余 exec / perception / llm / state
    for name in ("exec_terminal_spawn", "screen_snapshot", "understand_image",
                 "agent_chat", "todos_due", "wip_list"):
        assert name not in DIRECT_TOOLS
        assert name not in GATEWAY_EXCLUDE  # 仍在网关可调


def test_parallel_context_calls(tmp_path):
    db = str(tmp_path / "mem.db")
    mem = MemoryStore(db)
    mem.initialize()
    todos = TodosStore(db)
    todos.initialize()
    butler = ButlerStore(mem)
    runs = []
    for i in range(4):
        task = todos.create_wip({
            "title": f"并发任务{i}", "status": "active", "done_when": "完成",
        })
        created = butler.create_run(
            f"并发{i}", task_scope="wip", task_id=task["id"],
            project_id="parcae", harness="pytest",
        )
        open_session(butler, mem, todos, created["session_code"])
        runs.append(created["session_code"])

    def fetch(code: str):
        return next_context(butler, mem, todos, code, None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(fetch, runs))
    assert len(pages) == 4
    assert all(p.get("digest") and not p.get("error") for p in pages)
    mem.close()
    try:
        todos.conn.close()
    except Exception:
        pass
