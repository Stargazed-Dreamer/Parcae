"""UI 单测接缝：把 ChatPanel 的 store 工厂换成本地账本。

生产路径是 `RuntimeStore`（HTTP → butler runtime）；UI 单测只验面板逻辑，
用同一鸭子接口的本地 EventStore 注入，避免每个用例都起一个 runtime 进程。
"""

from __future__ import annotations


def local_store_factory(db_path: str):
    def _make():
        from lib.agent_runtime import EventStore

        store = EventStore(db_path=db_path)
        store.init()
        return store

    return _make
