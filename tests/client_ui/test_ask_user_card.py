"""ask_user 卡片与桥接（2026-10-07，wip_chat_3directions 点单①③④）。

覆盖两层：
- `_AskUserBlock`：单选 / 多选 / 自由输入三态，答完即锁，超时或"没人等了"要转失效态
- `AskUserBridge`：worker 线程阻塞 ↔ 主线程渲染；**中断必须能唤醒**（`AskUserTool.execute`
  正占着 worker 的事件循环线程，`call_soon_threadsafe` 那套路此刻送不达，只有直接
  set `threading.Event` 才醒得来）；超时值只来自构造参数（= config），不来自提问入参
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QRadioButton  # noqa: E402

from client.core.ask_user_bridge import (  # noqa: E402
    OUTCOME_ANSWERED,
    OUTCOME_CANCELLED,
    OUTCOME_NO_HOST,
    OUTCOME_SKIPPED,
    OUTCOME_TIMEOUT,
    AskUserBridge,
)
from client.panels.chat import _AskUserBlock  # noqa: E402

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _single_q() -> dict:
    return {
        "question": "要不要现在跑全量？", "header": "全量",
        "options": [{"label": "跑", "description": ""}, {"label": "先不跑", "description": ""}],
        "multiSelect": False, "allow_free_text": True,
    }


def _multi_q() -> dict:
    q = _single_q()
    q["question"] = "挑哪几条要归档？"
    q["multiSelect"] = True
    return q


def _card(questions: list[dict], calls: list) -> _AskUserBlock:
    def on_resolve(outcome, answers):
        calls.append((outcome, answers))
        return True
    return _AskUserBlock(questions, on_resolve=on_resolve)


def _buttons(card: _AskUserBlock):
    return {
        "radio": card.findChildren(QRadioButton),
        "check": card.findChildren(QCheckBox),
        "free": card.findChildren(QLineEdit),
    }


def _click(widget) -> None:
    widget.click()


def _pump_until(qapp, predicate, secs: float = 2.0) -> bool:
    """泵主线程事件直到 predicate 成立。

    worker 线程 `emit` 之后事件要排队才投递得到主线程槽，而 `_pending` 的登记发生在
    emit **之前**——只 `processEvents()` 一次会撞上"登记已可见、事件还没入队"的窗口，
    用例就会偶发假红。跨线程投递本身要断言，但要给它到达的机会。
    """
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ============================================================================
# 卡片三态
# ============================================================================


class TestAskUserCardShapes:
    def test_single_choice_uses_exclusive_radio_buttons(self):
        calls: list = []
        card = _card([_single_q()], calls)
        w = _buttons(card)
        assert w["radio"] and not w["check"]
        w["radio"][0].setChecked(True)
        w["radio"][1].setChecked(True)
        assert not w["radio"][0].isChecked(), "单选题必须互斥"

    def test_multi_choice_allows_several_checks(self):
        card = _card([_multi_q()], [])
        checks = card.findChildren(QCheckBox)
        assert checks and not card.findChildren(QRadioButton)
        checks[0].setChecked(True)
        checks[1].setChecked(True)
        answers, error = card.collect_answers()
        assert error == ""
        assert sorted(answers[0]["selected"]) == sorted([
            checks[0].text(), checks[1].text(),
        ])

    def test_free_text_input_is_always_present_by_default(self):
        card = _card([_single_q()], [])
        assert card.findChildren(QLineEdit), "点单①：卡片末尾恒挂一条自由输入框"

    def test_allow_free_text_false_hides_the_input(self):
        q = _single_q()
        q["allow_free_text"] = False
        card = _card([q], [])
        assert not card.findChildren(QLineEdit)

    def test_open_question_with_no_options_still_answers_via_free_text(self):
        q = _single_q()
        q["options"] = []
        calls: list = []
        card = _card([q], calls)
        card.findChildren(QLineEdit)[0].setText("我自己写的名字")
        _click(card._submit_btn)
        assert calls and calls[0][0] == OUTCOME_ANSWERED
        assert calls[0][1][0]["free_text"] == "我自己写的名字"


class TestAskUserCardSubmission:
    def test_empty_answer_is_blocked_in_place(self):
        """没选也没写：不许静默提交空答案，也不许回调桥接层。"""
        calls: list = []
        card = _card([_single_q()], calls)
        _click(card._submit_btn)
        assert calls == []
        assert card._hint.text(), "必须原地写明哪一题还没答"
        assert "还没选" in card._hint.text()

    def test_submit_locks_the_card_and_shows_what_was_chosen(self):
        calls: list = []
        card = _card([_single_q()], calls)
        card.findChildren(QRadioButton)[0].setChecked(True)
        _click(card._submit_btn)
        assert calls == [(OUTCOME_ANSWERED, [{"question": "要不要现在跑全量？",
                                             "selected": ["跑"], "free_text": ""}])]
        assert not card._submit_btn.isEnabled()
        assert not card._skip_btn.isEnabled()
        assert "已回答" in card._hint.text()

    def test_card_is_locked_after_answering(self):
        """答完即锁：整卡控件真的禁用（不是只靠 flag），再点提交不会二次回调。

        原先这用例把 `card._answered = False` 再调 `_on_submit()`，等于"把守卫拆掉
        再验证守卫"——测的是空气。改成断言锁的机制本身（enabled=False）+ 后果。
        """
        calls: list = []
        card = _card([_single_q()], calls)
        card.findChildren(QRadioButton)[0].setChecked(True)
        _click(card._submit_btn)
        assert len(calls) == 1
        assert not card._submit_btn.isEnabled()
        assert not card._skip_btn.isEnabled()
        assert not any(rb.isEnabled() for rb in card.findChildren(QRadioButton))
        assert all(not f.isEnabled() for f in card.findChildren(QLineEdit))
        _click(card._submit_btn)          # 禁用态下 Qt 不会发 clicked
        _click(card._skip_btn)
        assert len(calls) == 1, "锁卡后又回调了一次"

    def test_skip_hands_the_decision_back(self):
        calls: list = []
        card = _card([_single_q()], calls)
        _click(card._skip_btn)
        assert calls and calls[0][0] == OUTCOME_SKIPPED and calls[0][1] is None
        assert "跳过" in card._hint.text()

    def test_resolve_refused_means_card_goes_expired(self):
        """桥接层已经不认这次回答（超时/中断后用户才点）⇒ 卡片如实转失效态。"""
        def on_resolve(outcome, answers):
            return False

        card = _AskUserBlock([_single_q()], on_resolve=on_resolve)
        card.findChildren(QRadioButton)[0].setChecked(True)
        _click(card._submit_btn)
        assert "已失效" in card._hint.text()
        assert not card._submit_btn.isEnabled()

    def test_mark_expired_disables_and_says_nobody_answered(self):
        calls: list = []
        card = _card([_single_q()], calls)
        card.mark_expired()
        assert "未回答" in card._hint.text()
        assert not any(rb.isEnabled() for rb in card.findChildren(QRadioButton))
        # 失效之后再点提交也不许回调
        card._on_submit()
        assert calls == []

    def test_multiple_questions_numbered_and_all_required(self):
        calls: list = []
        card = _card([_single_q(), _multi_q()], calls)
        radios = card.findChildren(QRadioButton)
        radios[0].setChecked(True)
        answers, error = card.collect_answers()
        assert error and "第 2 题" in error, "第二题没答就不许整体提交"
        _click(card._submit_btn)
        assert calls == []
        card.findChildren(QCheckBox)[0].setChecked(True)
        _click(card._submit_btn)
        assert len(calls[0][1]) == 2


# ============================================================================
# 桥接
# ============================================================================


class TestAskUserBridge:
    def test_answer_from_handler_wakes_the_caller(self):
        bridge = AskUserBridge(timeout_secs=0)

        def render(payload):
            assert bridge.resolve(payload["rid"], OUTCOME_ANSWERED,
                                  [{"question": "q", "selected": ["跑"]}]) is True

        bridge.set_handlers(render)
        result = bridge.ask([_single_q()])
        assert result["outcome"] == OUTCOME_ANSWERED
        assert result["answers"][0]["selected"] == ["跑"]
        assert bridge.has_pending() is False

    def test_without_handler_falls_back_immediately(self):
        """没有渲染器（GUI 没就绪）时必须立刻回落，不许让 worker 干等到超时。"""
        bridge = AskUserBridge(timeout_secs=0)
        bridge.set_handlers(None)
        started = time.monotonic()
        result = bridge.ask([_single_q()])
        assert result["outcome"] == OUTCOME_NO_HOST
        assert time.monotonic() - started < 1.0

    def test_handler_exception_does_not_hang_the_caller(self):
        bridge = AskUserBridge(timeout_secs=0)

        def explode(payload):
            raise RuntimeError("卡片建不起来")

        bridge.set_handlers(explode)
        assert bridge.ask([_single_q()])["outcome"] == OUTCOME_NO_HOST

    def test_timeout_expires_the_card_and_clears_pending(self):
        expired: list[str] = []
        bridge = AskUserBridge(timeout_secs=0.05)
        bridge.set_handlers(lambda payload: None, expire_handler=expired.append)
        result = bridge.ask([_single_q()])
        assert result["outcome"] == OUTCOME_TIMEOUT
        assert result["waited_secs"] == pytest.approx(0.05)
        assert bridge.has_pending() is False
        assert len(expired) == 1, "超时后必须把卡片标失效，不能留一张还能点的假入口"

    def test_late_answer_after_timeout_is_refused(self):
        bridge = AskUserBridge(timeout_secs=0.05)
        bridge.set_handlers(lambda payload: None)
        captured: dict = {}

        def render(payload):
            captured["rid"] = payload["rid"]

        bridge.set_handlers(render)
        assert bridge.ask([_single_q()])["outcome"] == OUTCOME_TIMEOUT
        assert bridge.resolve(captured.get("rid", "nope"), OUTCOME_ANSWERED, []) is False

    def test_cancel_pending_wakes_a_waiting_caller_from_another_thread(self):
        """这就是"永不超时"的那条逃生门：中断只能靠直接 set Event 送达。"""
        bridge = AskUserBridge(timeout_secs=0)   # 0 = 永不超时
        bridge.set_handlers(lambda payload: None)  # 卡片挂上去但没人点
        out: dict = {}
        done = threading.Event()

        def worker():
            out["result"] = bridge.ask([_single_q()])
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        # 等注册完成（注册在 emit 之前，比渲染可靠）
        for _ in range(200):
            if bridge.has_pending():
                break
            time.sleep(0.01)
        assert bridge.has_pending(), "worker 没真的进入等待，这条测试就是空的"
        assert done.wait(0.2) is False, "没人唤醒时它必须还在等（永不超时）"

        woke = bridge.cancel_pending(OUTCOME_CANCELLED)
        assert woke == 1
        assert done.wait(2.0) is True
        assert out["result"]["outcome"] == OUTCOME_CANCELLED
        t.join(2.0)

    def test_cancel_pending_is_idempotent(self):
        bridge = AskUserBridge(timeout_secs=0)
        assert bridge.cancel_pending() == 0

    def test_settled_request_cannot_be_resolved_before_cleanup(self):
        """已经定局（被中断/超时唤醒）的请求，在 worker 清理之前也不能再写。

        同线程调用 ⇒ Signal 是 DirectConnection，渲染器就在 `emit` 里内联跑完，此刻
        worker 还没执行到清理，条目一定仍在 `_pending`。所以这条是"已定局不可再写"
        那道守卫的**确定性**证明；上面那条跨线程用例只能证明"清理之后被拒"，
        抓不到清理之前的窗口（探针实测：只靠清理来拒，改坏守卫照样绿）。
        """
        bridge = AskUserBridge(timeout_secs=0)
        seen: dict = {}

        def render(payload):
            assert bridge.cancel_pending(OUTCOME_CANCELLED) == 1
            seen["refused"] = bridge.resolve(
                payload["rid"], OUTCOME_ANSWERED, [{"question": "q"}]
            )

        bridge.set_handlers(render)
        result = bridge.ask([_single_q()])
        assert seen["refused"] is False, "slot 已带 outcome，这次回答不该被算成用户已答"
        assert result["outcome"] == OUTCOME_CANCELLED
        assert bridge.has_pending() is False

    def test_late_answer_after_cancel_is_refused(self, qapp):
        """中断后用户才点提交：桥接层必须说"没人等了"，卡片才会转失效态。

        否则卡片显示"已回答"、agent 那边读走"被中断"——两边各说各话。
        这条还顺带验证跨线程 Signal 真的能在主线程投递（processEvents 之后渲染器
        才被调到）；不泵事件的话它永远排不进去，正是生产里"worker 阻塞 + 主线程空转"
        的反面。
        """
        bridge = AskUserBridge(timeout_secs=0)
        captured: dict = {}

        def render(payload):
            captured["rid"] = payload["rid"]

        bridge.set_handlers(render)
        out: dict = {}
        done = threading.Event()

        def worker():
            out["result"] = bridge.ask([_single_q()])
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        for _ in range(200):
            if bridge.has_pending():
                break
            time.sleep(0.01)
        assert bridge.has_pending(), "worker 没进入等待，这条测试是空的"
        assert _pump_until(qapp, lambda: "rid" in captured), (
            "跨线程 emit 始终没投递到主线程——卡片根本画不出来"
        )

        assert bridge.cancel_pending(OUTCOME_CANCELLED) == 1
        assert done.wait(2.0) is True
        assert out["result"]["outcome"] == OUTCOME_CANCELLED
        assert bridge.has_pending() is False
        assert bridge.resolve(captured["rid"], OUTCOME_ANSWERED,
                              [{"question": "q", "selected": ["跑"]}]) is False, (
            "唤醒后条目还留在 _pending，迟到的回答就会被当成成功")
        t.join(2.0)

    def test_ask_takes_no_timeout_argument(self):
        """超时只能来自构造参数（config）。给提问入参开一个 timeout 就是让 agent
        自己决定"等多久算你没回答"——那正是点单④禁止的形状。"""
        params = inspect.signature(AskUserBridge.ask).parameters
        assert list(params) == ["self", "questions"], params


# ============================================================================
# 时长只来自 config（点单④：agent 传不了，用户改得了）
# ============================================================================


class TestTimeoutOnlyFromConfig:
    """`[runner] ask_user_timeout_secs` 仍是唯一入口；T7 起由 runtime 读取。

    面板不再持有 ask_user 等待逻辑（卡片只负责把选择 POST 回 runtime），
    所以这里验两段：config 解析口径不变 + runtime 把该值传给 provider。
    """

    @staticmethod
    def _patch(monkeypatch, runner_table):
        import lib.config_reader as cr
        real = cr.load_config()
        merged = dict(real)
        merged["runner"] = dict(runner_table)
        monkeypatch.setattr(cr, "load_config", lambda: merged)
        return merged

    def test_positive_value_is_read(self, monkeypatch):
        from lib.agent_runtime.runtime_deps import settings_from_config

        self._patch(monkeypatch, {"ask_user_timeout_secs": 25})
        assert settings_from_config("http://127.0.0.1:18766").ask_user_timeout_secs == 25.0

    @pytest.mark.parametrize("table", [
        {},
        {"ask_user_timeout_secs": 0},
        {"ask_user_timeout_secs": -5},
        {"ask_user_timeout_secs": "abc"},
        {"ask_user_timeout_secs": None},
    ])
    def test_missing_zero_junk_all_mean_never_timeout(self, monkeypatch, table):
        """缺失/0/负数/写错类型 ⇒ 0.0（永不超时），绝不因为读失败就变成一个短超时"""
        from lib.agent_runtime.runtime_deps import settings_from_config

        self._patch(monkeypatch, table)
        assert settings_from_config("http://127.0.0.1:18766").ask_user_timeout_secs == 0.0

    def test_runtime_passes_configured_timeout_to_provider(self, monkeypatch, tmp_path):
        """配置值必须真的进到 provider 的等参数里，否则就是死配置。"""
        import types

        from lib.agent_runtime.event_store import EventStore
        from lib.agent_runtime.runtime_deps import settings_from_config
        from lib.agent_runtime.runtime_manager import RuntimeApiError, RuntimeManager

        self._patch(monkeypatch, {"ask_user_timeout_secs": 33})
        recorded = {}

        def fake_provider(**kwargs):
            recorded.update(kwargs)
            return types.SimpleNamespace(
                cancel_pending=lambda *a, **k: 0,
                resolve=lambda *a, **k: False,
            )

        monkeypatch.setattr(
            "lib.agent_runtime.runtime_manager.RuntimeAskUserProvider", fake_provider,
        )
        store = EventStore(db_path=str(tmp_path / "timeout_wiring.db"))
        store.init()

        async def _boom(*_a, **_k):
            raise RuntimeError("deps not needed for this contract test")

        manager = RuntimeManager(
            store=store,
            settings=settings_from_config("http://127.0.0.1:18766"),
            deps_factory=_boom,
        )

        async def _submit():
            with pytest.raises(RuntimeApiError):
                await manager.submit("s-timeout", "hi")
            return recorded

        try:
            loop = asyncio.new_event_loop()
            try:
                got = loop.run_until_complete(_submit())
            finally:
                loop.close()
        finally:
            store.close()

        assert got["timeout_secs"] == 33.0
