"""golden query 集的形状守卫（不碰生产库）。

目标 key 是否还存在由 `tools/audit/search_eval.py` 运行时校验：它先备份 DB 到 temp，
再逐条确认 gold key 落在活跃 facts 里。这里只保证文件本身可解析、不重复、样本量达标。
"""

from __future__ import annotations

import json
from pathlib import Path

GOLD = Path(__file__).with_name("search_gold.jsonl")


def _rows() -> list[dict]:
    return [
        json.loads(line)
        for line in GOLD.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_search_gold_shape() -> None:
    rows = _rows()
    assert 30 <= len(rows) <= 50, f"样本量 {len(rows)} 不在 T-N2 要求的 30-50 之间"
    assert len({r["id"] for r in rows}) == len(rows), "id 重复"
    assert len({r["query"] for r in rows}) == len(rows), "query 重复"


def test_search_gold_fields() -> None:
    for r in _rows():
        assert set(r) >= {"id", "query", "gold_keys", "query_style", "source", "note"}
        assert str(r["query"]).strip()
        assert r["gold_keys"], f"{r['id']} 没有 gold key"
        assert all(isinstance(k, str) and k.strip() for k in r["gold_keys"])
        assert r["query_style"] in {"trace", "contract"}, f"{r['id']} 风格非法"
        assert "\n" not in r["query"], f"{r['id']} 的 query 含换行"
