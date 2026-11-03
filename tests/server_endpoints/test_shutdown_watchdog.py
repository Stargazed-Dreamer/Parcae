"""关停兜底单测（server/core/lifecycle.py + server/config.py 的两个关停旋钮）

背景：清理链任一步阻塞都会让进程永久滞留（曾经的实例是 uvicorn 无限等「在飞连接/
任务结束」，见 docs/operations-manual.md「关停卡住的定位」）。这里只测兜底契约：

1. 看门狗到点确实强制退出 —— ``os._exit`` 被 monkeypatch，**绝不真退出**
   （危险操作测试铁律：能 mock 就不实操；本文件的 autouse 兜底见下方 fixture）；
2. 看门狗幂等 —— 重复调用不会再起第二个线程（否则关停时多个线程同时 os._exit）；
3. 两个配置旋钮存在且有下限（1s / 5s）。

不触真实清理链（screen/system/视觉/记忆/Loop/浏览器全不参与）。
"""

import threading
import time

import pytest

from server.core import lifecycle


@pytest.fixture(autouse=True)
def _never_really_exit(monkeypatch):
    """机制级安全网：本模块任何用例都不可能真的把测试进程 os._exit 掉。

    即便某条用例忘了自己 mock，真实 os._exit 也被替换成记录器；
    退出码记录在 ``_exit_calls`` 里供断言，teardown 由 monkeypatch 自动还原。
    """
    calls: list[int] = []
    monkeypatch.setattr(lifecycle.os, "_exit", lambda code: calls.append(code))
    # 幂等标志是模块级全局，逐条重置，保证每条用例都拿到「首次调用」语义
    monkeypatch.setattr(lifecycle, "_watchdog_started", False)
    return calls


def _watchdog_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "shutdown-watchdog")


def test_watchdog_fires_once_and_is_idempotent(_never_really_exit):
    lifecycle.start_shutdown_watchdog(source="pytest", deadline_s=1.0)
    assert _watchdog_threads() == 1

    # 第二次调用不得再起线程
    lifecycle.start_shutdown_watchdog(source="pytest", deadline_s=1.0)
    assert _watchdog_threads() == 1

    deadline = time.time() + 10
    while not _never_really_exit and time.time() < deadline:
        time.sleep(0.05)
    assert _never_really_exit == [0], "看门狗到点必须调用 os._exit(0) 强制退出"


def test_cleanup_deadline_reads_config_with_floor():
    # 配置项 server.shutdown_cleanup_deadline_secs（默认 30，下限 5）
    assert lifecycle.shutdown_cleanup_deadline() >= 5.0


def test_server_config_exposes_shutdown_knobs():
    from server.config import get_server_config

    cfg = get_server_config()
    # graceful 下限 1（uvicorn 收到 None 会无限等待，故代码里夹掉 0/负数）
    assert cfg["graceful_shutdown_timeout_secs"] >= 1
    assert cfg["shutdown_cleanup_deadline_secs"] >= 5
