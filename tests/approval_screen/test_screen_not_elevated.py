"""提权闸门测试：未提权时注入类端点直接拒绝，且**不弹**接管确认窗。

背景（2026-10-22 用户要求）：「屏幕控制在不可用时应该直接拒绝并明确未提权，而不是弹窗
让用户允许之后才让 agent 发现点击无效」。

被测契约：
- 未提权（_ADMIN_STATUS=False）时，注入类端点（execute_action / batch_actions /
  desktop_transaction / scroll_capture / focus_window）与授权入口 screen_request_control
  一律返回 status="not_elevated"，且**早于** takeover 弹窗与 403 授权引导。
- 例外 1：execute_action(strategy="uia_only", action="click") 走纯 UIA invoke（零键鼠事件），
  不被闸门拦截。
- 例外 2：screen_request_control(needs_input_injection=false) 照常弹窗授权，供只做
  UIA 语义动作 / 窗口操作的任务使用。
- 只读端点（/screen/status 等）不受影响。

主 seam：REST API 层。conftest 的 mock_overlay_client 已 mock GUI 并把 SessionManager
默认 grant normal，本文件只额外把提权状态置 False + 用 spy 记录弹窗是否被调用。
"""

import pytest

# 拥有 _ADMIN_STATUS 模块级绑定的端点模块（各模块 import 时拿的是自己的副本，
# 所以必须逐个 patch，只改 routes 不会影响它们）
_ADMIN_MODULES = (
    "action_endpoints",
    "batch_endpoints",
    "desktop_transaction_endpoints",
    "scroll_wait_analyze_endpoints",
)


@pytest.fixture
def not_elevated(monkeypatch):
    """模拟"后端以非管理员身份启动"（_ADMIN_STATUS=False）。"""
    from server.screen import routes

    monkeypatch.setattr(routes, "_ADMIN_STATUS", False)
    for mod_name in _ADMIN_MODULES:
        mod = __import__(f"server.screen.{mod_name}", fromlist=["_ADMIN_STATUS"])
        monkeypatch.setattr(mod, "_ADMIN_STATUS", False, raising=False)


@pytest.fixture
def dialog_spy(monkeypatch):
    """记录"接管确认弹窗"是否被触达，返回调用记录列表。

    两条 seam 都要接：
    1. routes._ensure_takeover_approved（注入类端点的统一入口；conftest 已把它 mock 成
       恒 True，这里换成记账版，语义不变）
    2. overlay_client.ensure_takeover_approved（screen_request_control 直接调 overlay）
    """
    calls: list[str] = []

    def _spy_gate(screen_cfg, task_description):
        calls.append(task_description or "")
        return True, ""

    from server.screen import routes

    monkeypatch.setattr(routes, "_ensure_takeover_approved", _spy_gate)
    for mod_name in ("action_endpoints", "batch_endpoints", "desktop_transaction_endpoints"):
        mod = __import__(f"server.screen.{mod_name}", fromlist=["_ensure_takeover_approved"])
        monkeypatch.setattr(mod, "_ensure_takeover_approved", _spy_gate, raising=False)

    import server.overlay_client as _oc

    def _spy_overlay(*args, **kwargs):
        calls.append(kwargs.get("requested_mode", "normal"))
        return {
            "status": "confirmed",
            "reason": "",
            "task_authorization": True,
            "requested_mode": kwargs.get("requested_mode", "normal"),
            "authorized_mode": "normal",
            "max_duration_hours": None,
            "shutdown_permitted": False,
        }

    monkeypatch.setattr(_oc.overlay_client, "ensure_takeover_approved", _spy_overlay)
    return calls


class TestInjectionEndpointsRefuse:
    """注入类端点：未提权 → not_elevated，且不弹窗"""

    def test_execute_action(self, client, not_elevated, dialog_spy):
        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 100, "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "not_elevated"
        assert "未以管理员权限运行" in data["message"]
        assert data["transport_status"] == "not_sent"
        assert dialog_spy == [], "未提权时不允许弹接管确认窗"

    def test_execute_action_keyboard(self, client, not_elevated, dialog_spy):
        resp = client.post("/screen/action", json={
            "action": "type", "text": "hello", "require_confirm": False,
        })
        assert resp.json()["status"] == "not_elevated"
        assert dialog_spy == []

    def test_batch_actions(self, client, not_elevated, dialog_spy):
        resp = client.post("/screen/batch-actions", json={
            "actions": [{"action": "click", "x": 100, "y": 100}],
        })
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "not_elevated"
        assert dialog_spy == []

    def test_desktop_transaction(self, client, not_elevated, dialog_spy):
        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 1},
            "actions": [{"action": "click", "x": 100, "y": 100}],
            "expected": {"type": "ocr_contains", "text": "已保存"},
        })
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "not_elevated"
        assert dialog_spy == []

    def test_scroll_capture(self, client, not_elevated, dialog_spy):
        resp = client.post("/screen/scroll-capture", json={"hwnd": 1})
        data = resp.json()
        assert data["success"] is False
        assert "未以管理员权限运行" in data["message"]

    def test_focus_window(self, client, not_elevated, dialog_spy):
        resp = client.post("/screen/focus-window", json={"hwnd": 1})
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "not_elevated"
        assert dialog_spy == []


class TestAuthorizationEntryRefuses:
    """授权入口：未提权时不做无意义的授权询问"""

    def test_request_control_no_dialog(self, client, not_elevated, dialog_spy):
        resp = client.post("/screen/control/request", json={"task_description": "点一下按钮"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "not_elevated"
        assert data["admin_privileges"] is False
        assert "未以管理员权限运行" in data["message"]
        assert dialog_spy == [], "未提权时不应弹接管确认窗"
        assert data["task_authorization"]["active"] in (True, False)  # 只是快照，不因请求而授权

    def test_request_control_uia_only_escape(self, client, not_elevated, dialog_spy):
        """只做 UIA/窗口类任务时显式声明不需要注入 → 照常走授权流程。"""
        resp = client.post("/screen/control/request", json={
            "task_description": "只做 UIA 语义动作",
            "needs_input_injection": False,
        })
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "authorized"
        assert dialog_spy == ["normal"]  # 弹窗确实发生了
        assert data["admin_privileges"] is False  # 但不谎报已提权


class TestReadOnlyUnaffected:
    """只读端点不受提权闸门影响"""

    def test_screen_status_reports_admin_false(self, client, not_elevated):
        resp = client.get("/screen/status")
        assert resp.status_code == 200
        assert resp.json()["admin_privileges"] is False


class TestUiaOnlyExemption:
    """execute_action + strategy=uia_only 不走闸门（纯 UIA invoke，零键鼠事件）"""

    def test_uia_only_click_not_blocked_by_gate(self, client, not_elevated, dialog_spy, monkeypatch):
        # 不真的去 hit-test 桌面：命中失败 → 端点自己返回 blocked（不是 not_elevated）
        from server.screen import uia

        monkeypatch.setattr(uia, "try_uia_click_fusion", lambda x, y: None)
        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 100,
            "require_confirm": False, "strategy": "uia_only",
        })
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert "uia_only" in data["message"]
