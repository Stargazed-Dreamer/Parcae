"""X10 验收：事件域注册表 + live 默认不回放。

覆盖 tickets.md T2：
- 注册表覆盖 runner/event_store 全部 append_event 字面量类型（静态扫描，自报命中数）
- 写入 live + persistent + capability → 默认回放只见后两者，include_live=True 见全部
- live 仍落库；未登记类型默认 persistent
"""

from __future__ import annotations

import ast
import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from lib.agent_runtime.event_domains import (
    EVENT_DOMAIN_CAPABILITY,
    EVENT_DOMAIN_LIVE,
    EVENT_DOMAIN_PERSISTENT,
    EVENT_DOMAINS,
    EVENT_TYPE_DOMAINS,
)
from lib.agent_runtime.event_store import EventStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = PROJECT_ROOT / "lib" / "agent_runtime"

LIVE_TYPES = (
    "streaming_text_delta",
    "streaming_thinking_delta",
    "streaming_tool_call",
)


def _literal_append_event_types() -> dict[str, list[str]]:
    """静态扫描 append_event 的第二个位置参数；不发现在运行时，扫盘上源码。"""
    found: dict[str, list[str]] = {}
    for path in sorted(RUNTIME_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append_event"
            ):
                continue
            arg = node.args[1] if len(node.args) >= 2 else next(
                (kw.value for kw in node.keywords if kw.arg == "event_type"), None
            )
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.setdefault(arg.value, []).append(f"{path.name}:{node.lineno}")
    return found


@pytest.fixture
def store(tmp_db_path) -> Iterator[EventStore]:
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


def test_registry_covers_all_append_event_call_sites():
    found = _literal_append_event_types()
    assert len(found) >= 20, f"static scan matched only {len(found)} types: {sorted(found)}"
    missing = sorted(set(found) - set(EVENT_TYPE_DOMAINS))
    assert not missing, (
        f"未登记的事件类型 {missing}；调用点: "
        + "; ".join(f"{t} -> {found[t]}" for t in missing)
    )
    assert set(EVENT_TYPE_DOMAINS.values()) <= EVENT_DOMAINS


def test_registry_marks_streaming_types_live():
    for event_type in LIVE_TYPES:
        assert EVENT_TYPE_DOMAINS[event_type] == EVENT_DOMAIN_LIVE


def test_message_event_types_registered_persistent():
    for role in ("user", "assistant", "system", "synthetic", "tool"):
        event_type = f"{role}_message_appended"
        assert EVENT_TYPE_DOMAINS[event_type] == EVENT_DOMAIN_PERSISTENT


def test_default_replay_excludes_live_but_keeps_capability(store):
    async def run():
        await store.create_session(session_id="s-domains")
        await store.append_event("s-domains", "assistant_message_appended", {"seq": 1})
        await store.append_event("s-domains", "streaming_text_delta", {"delta": "x"})

        # capability 目前还没有真实写入方；按契约直接落一条，验证默认回放包含它
        seq = store.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE session_id='s-domains'"
        ).fetchone()["n"]
        store.conn.execute(
            "INSERT INTO events(seq, session_id, type, domain, payload_json, created_at) "
            "VALUES(?, 's-domains', 'tool_catalog_snapshot', ?, '{}', 0)",
            (seq, EVENT_DOMAIN_CAPABILITY),
        )
        store.conn.commit()

        default_types = [e.type for e in await store.load_events("s-domains")]
        all_types = [e.type for e in await store.load_events("s-domains", include_live=True)]

        assert default_types == [
            "session_created",
            "assistant_message_appended",
            "tool_catalog_snapshot",
        ], default_types
        assert "streaming_text_delta" in all_types
        assert len(all_types) == 4

        domain_by_type = {
            r["type"]: r["domain"]
            for r in store.conn.execute(
                "SELECT type, domain FROM events WHERE session_id='s-domains'"
            ).fetchall()
        }
        assert domain_by_type["streaming_text_delta"] == EVENT_DOMAIN_LIVE
        assert domain_by_type["assistant_message_appended"] == EVENT_DOMAIN_PERSISTENT
        assert domain_by_type["tool_catalog_snapshot"] == EVENT_DOMAIN_CAPABILITY

        # Event 对象也要带 domain，供上层决定怎么消费
        default_events = await store.load_events("s-domains")
        assert {e.type: e.domain for e in default_events}["tool_catalog_snapshot"] == (
            EVENT_DOMAIN_CAPABILITY
        )

    asyncio.run(run())


def test_live_is_still_persisted_in_ledger(store):
    async def run():
        await store.create_session(session_id="s-live-kept")
        await store.append_event(
            "s-live-kept", "streaming_thinking_delta", {"delta": "why"}
        )
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE session_id='s-live-kept' AND type='streaming_thinking_delta'"
        ).fetchone()
        assert int(row["n"]) == 1

    asyncio.run(run())


def test_unknown_event_type_defaults_persistent(store):
    async def run():
        await store.create_session(session_id="s-unknown")
        await store.append_event("s-unknown", "brand_new_event", {"x": 1})
        row = store.conn.execute(
            "SELECT domain FROM events WHERE session_id='s-unknown' AND type='brand_new_event'"
        ).fetchone()
        assert row["domain"] == EVENT_DOMAIN_PERSISTENT
        default_types = [e.type for e in await store.load_events("s-unknown")]
        assert "brand_new_event" in default_types

    asyncio.run(run())


def test_init_repairs_legacy_streaming_domain(tmp_db_path):
    """旧库 domain 列按 DEFAULT 落到 persistent 的 streaming 行，重启时回填为 live。"""
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        asyncio.run(store.create_session(session_id="s-legacy-live"))
        asyncio.run(
            store.append_event(
                "s-legacy-live", "streaming_text_delta", {"delta": "old"}
            )
        )
    finally:
        store.close()

    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute(
            "UPDATE events SET domain = 'persistent' "
            "WHERE session_id='s-legacy-live' AND type='streaming_text_delta'"
        )
        conn.commit()
    finally:
        conn.close()

    store2 = EventStore(db_path=tmp_db_path)
    store2.init()
    try:
        row = store2.conn.execute(
            "SELECT domain FROM events WHERE session_id='s-legacy-live' "
            "AND type='streaming_text_delta'"
        ).fetchone()
        assert row["domain"] == EVENT_DOMAIN_LIVE
        default_types = [
            e.type for e in asyncio.run(store2.load_events("s-legacy-live"))
        ]
        assert "streaming_text_delta" not in default_types
    finally:
        store2.close()


def test_replay_events_follows_same_contract(store):
    async def run():
        await store.create_session(session_id="s-replay")
        await store.append_event("s-replay", "streaming_text_delta", {"delta": "d"})
        default_replay = await store.replay_events("s-replay")
        full_replay = await store.replay_events("s-replay", include_live=True)
        assert [e["type"] for e in default_replay] == ["session_created"]
        assert [e["type"] for e in full_replay] == [
            "session_created",
            "streaming_text_delta",
        ]

    asyncio.run(run())
