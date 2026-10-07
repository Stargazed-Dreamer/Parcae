# -*- coding: utf-8 -*-
"""审核界面误触防护回归测试（2026-09-23 用户反馈）。

锁定两个行为：
1. _NoWheelCombo：滚轮**只在下拉弹窗展开时**生效 —— 未聚焦、或仅聚焦
   但弹窗未展开（用户点选行时点到下拉框后的常见状态），滚轮一律忽略
   （透传给表格滚动）；
2. 审核表 item:hover 高亮已关（静态检查 panel.py 的样式表覆盖存在），
   防止"悬浮看着像选中"导致确认错行。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance() or QApplication([])
    assert isinstance(instance, QApplication)
    return instance


def _wheel_event() -> QWheelEvent:
    return QWheelEvent(
        QPointF(4, 4),
        QPointF(4, 4),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def test_no_wheel_combo_ignores_wheel_without_focus(app: QApplication):
    """未聚焦：滚轮不切换值，事件忽略 → 交给表格滚动。"""
    from workspace.accounting.panel_widgets import _NoWheelCombo

    combo = _NoWheelCombo()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(0)
    assert not combo.hasFocus()
    ev = _wheel_event()
    combo.wheelEvent(ev)
    assert not ev.isAccepted(), "未聚焦时滚轮事件必须 ignore（透传给父级滚动）"
    assert combo.currentIndex() == 0, "未聚焦时滚轮不得切换选中值"


def test_no_wheel_combo_allows_wheel_only_when_popup_open(app: QApplication):
    """只有弹窗展开才放行滚轮；仅聚焦（弹窗未开）必须拦截。

    2026-09-23 用户实测复现：点选行时点到下拉框使其持有焦点，随后
    想滚动表格时又把值切掉 —— 因此 v2 收紧为"弹窗展开才响应滚轮"。
    """
    from workspace.accounting.panel_widgets import _NoWheelCombo

    combo = _NoWheelCombo()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(0)
    combo.hasFocus = lambda: True  # 即便持有焦点……
    assert not combo.view().isVisible()  # ……弹窗未展开
    ev = _wheel_event()
    combo.wheelEvent(ev)
    assert not ev.isAccepted(), "仅聚焦而弹窗未展开时，滚轮必须 ignore（用户实测复现的洞）"
    assert combo.currentIndex() == 0, "弹窗未展开时滚轮不得切换选中值"

    # 弹窗展开时才放行（isVisible 桩为 True）
    combo.view().isVisible = lambda: True  # type: ignore[method-assign]
    ev2 = _wheel_event()
    combo.wheelEvent(ev2)
    assert ev2.isAccepted(), "弹窗展开时滚轮应被接受（下拉列表内滚动/换值）"


def test_review_table_hover_highlight_disabled():
    """审核表必须覆盖全局主题的 item:hover 底色（悬浮不再看起来像选中）。"""
    import workspace.accounting.panel as panel_mod

    src = Path(panel_mod.__file__).read_text(encoding="utf-8")
    assert "QTableWidget::item:hover" in src, "审核表缺少 item:hover 样式覆盖"
    # 覆盖值必须是透明/无色，而不是换个颜色继续高亮
    hover_rule = src.split("QTableWidget::item:hover", 1)[1].split("}", 1)[0]
    assert "transparent" in hover_rule.lower()


def test_reload_guard_exists_and_wired():
    """整表刷新收口处必须有"有未暂存修改先询问"的守卫。"""
    import workspace.accounting.panel as panel_mod

    src = Path(panel_mod.__file__).read_text(encoding="utf-8")
    assert "_maybe_guard_pending_before_reload" in src
    # 守卫必须在 _load_review_data 内部被调用（收口），且提供暂存/放弃/取消三选项
    load_body = src.split("def _load_review_data", 1)[1].split("\n    def ", 1)[0]
    assert "_maybe_guard_pending_before_reload()" in load_body
    guard_body = src.split("def _maybe_guard_pending_before_reload", 1)[1].split(
        "\n    def ", 1
    )[0]
    for token in ("暂存并刷新", "放弃修改", "取消"):
        assert token in guard_body, f"守卫对话框缺少选项：{token}"
