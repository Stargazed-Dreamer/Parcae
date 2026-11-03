"""从账本重建会话投影（X9）。

用法：
    uv run python tools/runtime/rebuild_projections.py
    uv run python tools/runtime/rebuild_projections.py --session sess-xxxx
    uv run python tools/runtime/rebuild_projections.py --db path/to/agent.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.agent_runtime.event_store import DEFAULT_DB_PATH, EventStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重建 proj_stats / proj_turn_outline / proj_title")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="账本路径（默认 data/client/agent.db）")
    parser.add_argument("--session", default=None, help="只重建一个会话；缺省重建全部")
    args = parser.parse_args()

    store = EventStore(db_path=args.db)
    store.init()
    try:
        result = asyncio.run(store.rebuild_projections(args.session))
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
