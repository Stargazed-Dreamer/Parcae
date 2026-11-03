"""工具日志默认过滤（2026-10-21）。

背景：messages 里 91.7% 是 source='tool' 的工具调用日志，/memory/search 默认把
它们当"记忆"返回（10-04 实测 10 条 query 有 9 条 top1 是工具日志）。默认排除
source='tool'，显式传 source='tool' 仍可查。

关键判据：排除必须发生在 BM25/向量取 top_k **之前**，否则工具日志先占满候选、
再过滤会直接返回空。
"""

import os
import tempfile

import pytest

from server.memory.bm25 import BM25Index
from server.memory.manager import MemoryManager
from server.memory.store import MemoryStore


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for suffix in ("", "-wal", "-shm"):
        f = path + suffix
        if os.path.exists(f):
            try:
                os.unlink(f)
            except OSError:
                pass


@pytest.fixture
def store(tmp_db):
    s = MemoryStore(tmp_db)
    s.initialize()
    yield s
    s.close()


def _seed(store):
    manual = store.insert_message("虚拟机装机走 vmrun GuestOps", source="manual", role="user")
    tool = store.insert_message("调用 vmrun list 成功", source="tool", role="tool")
    index = BM25Index(store)
    index.index_message(manual, "虚拟机装机走 vmrun GuestOps")
    index.index_message(tool, "调用 vmrun list 成功")
    return manual, tool


class TestBM25ExcludeSources:
    def test_without_exclusion_both_sources(self, store):
        manual, tool = _seed(store)
        ids = {r["message_id"] for r in BM25Index(store).search("vmrun 虚拟机", 10)}
        assert {manual, tool} <= ids

    def test_exclude_tool(self, store):
        manual, tool = _seed(store)
        ids = {
            r["message_id"]
            for r in BM25Index(store).search("vmrun 虚拟机", 10, exclude_sources={"tool"})
        }
        assert manual in ids
        assert tool not in ids

    def test_exclusion_happens_before_top_k(self, store):
        """top_k=1 时也必须返回 manual，证明不是取完 top_k 再滤。"""
        index = BM25Index(store)
        manual = store.insert_message("vmrun 虚拟机 唯一人工记忆", source="manual", role="user")
        index.index_message(manual, "vmrun 虚拟机 唯一人工记忆")
        for i in range(5):
            mid = store.insert_message(f"vmrun 虚拟机 工具日志 {i}", source="tool", role="tool")
            index.index_message(mid, f"vmrun 虚拟机 工具日志 {i}")
        rows = index.search("vmrun 虚拟机", 1, exclude_sources={"tool"})
        assert [r["message_id"] for r in rows] == [manual]


class _FakeSemantic:
    def __init__(self):
        self.calls: list[dict] = []

    def search(self, query, **kwargs):
        self.calls.append(kwargs)
        return []


class _FakeConfig:
    enable_evidence_ledger = False


class TestManagerPolicy:
    """manager.search 的默认策略：source=None 排除 tool；显式 source 不额外排除。"""

    def _mgr(self):
        m = object.__new__(MemoryManager)
        m.semantic = _FakeSemantic()
        m.config = _FakeConfig()
        return m

    def test_default_excludes_tool(self):
        m = self._mgr()
        m.search("q", top_k=3)
        assert m.semantic.calls[0]["exclude_sources"] == {"tool"}

    def test_explicit_tool_source_not_excluded(self):
        m = self._mgr()
        m.search("q", top_k=3, source="tool")
        assert m.semantic.calls[0]["exclude_sources"] is None

    def test_explicit_manual_source_not_excluded(self):
        m = self._mgr()
        m.search("q", top_k=3, source="manual")
        assert m.semantic.calls[0]["exclude_sources"] is None


def test_vector_rows_carry_source(store):
    mid = store.insert_message("hello", source="tool")
    store.insert_vector(mid, "h", b"\x00" * 8, "test-model", 2)
    rows = store.get_vectors_for_search("test-model")
    assert rows and rows[0]["source"] == "tool"
