"""Ticket 06：模型池 VL 子面板的降级链渲染与行内复位（wip_c3716c0b）

面板此前是"一行一 key 的 8 列表"，看不到同 key 的多个模型、也看不到谁被判死。
本文件钉住新形状：key→model 两层树，子行顺序=后端给的降级链顺序（不许重排），
角色分四档（在服务/今日失效/待命/未启用），今日失效带原因与解冻倒计时，
行内复位按钮走后端唯一入口 POST /vision/reset。

覆盖：
- U1 子行数量/顺序 = payload 的 models[]（重排即骗人）
- U2 四档角色语义 + 颜色只用 tokens
- U3 tooltip 全文（长文本 elide 上屏、全文进 tooltip）
- U4 行内复位：按钮存在性 + 请求路径 + 置灰/恢复
- U5 无判死模型的 key 不给复位按钮并说明原因
- U6 降级链视角切换会带 use_case 重拉
"""

import pytest
from PySide6.QtWidgets import QApplication

from client.panels.llm_pool import ModelPoolPanel
from lib.ui import tokens

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None, **kwargs):
        self.calls.append(("get", path, params))
        return None

    def post(self, path, json=None, **kwargs):
        self.calls.append(("post", path, json))
        return {"dead_models_cleared": 2, "dead_model_keys_cleared": 1}


def _payload(models, *, available=True, label="魔搭", key_id="ms", last_error=""):
    return {
        "enabled": True,
        "use_case": "vl_vision",
        "dead_reset_in_s": 3 * 3600 + 12 * 60,
        "providers": [{
            "key_id": key_id,
            "label": label,
            "model": "vl-q",
            "base_url": "https://api.example.cn/v1",
            "available": available,
            "cooldown_remaining": 0.0,
            "active_count": 0,
            "max_concurrency": 2,
            "last_error": last_error,
            "privacy_warning": "",
            "models": models,
        }],
    }


def _m(name, tier, *, current=False, dead=False, enabled=True, reason=""):
    return {"name": name, "tier": tier, "current": current, "dead_today": dead,
            "enabled": enabled, "dead_reason": reason}


def _render(panel, payload):
    panel._vl_providers = payload
    panel._vl_dead_reset_in_s = payload.get("dead_reset_in_s", 0)
    panel._update_vl_providers_tree()
    return panel._vl_providers_tree


@pytest.fixture
def panel(qapp):
    p = ModelPoolPanel()
    p._http = FakeHttp()
    yield p
    p.close()


class TestChainRendering:
    def test_child_rows_follow_backend_order(self, panel):
        """U1：子行顺序必须逐字等于 payload 的 models[]（后端已按 use_case 排好）"""
        tree = _render(panel, _payload([
            _m("vl-quality", 5, current=True),
            _m("vl-a", 2),
            _m("vl-late", 3),
        ]))

        assert tree.topLevelItemCount() == 1
        key_item = tree.topLevelItem(0)
        assert key_item.childCount() == 3
        assert [key_item.child(i).text(0) for i in range(3)] == \
            ["└ vl-quality", "└ vl-a", "└ vl-late"]

    def test_key_row_shows_label_and_concurrency(self, panel):
        tree = _render(panel, _payload([_m("vl-q", 5, current=True)]))
        key_item = tree.topLevelItem(0)
        assert "魔搭" in key_item.text(0)
        assert key_item.text(3) == "0/2"

    def test_placeholder_when_vl_disabled(self, panel):
        tree = _render(panel, {"enabled": False, "providers": []})
        assert tree.topLevelItemCount() == 1
        assert "未启用" in tree.topLevelItem(0).text(0)

    def test_placeholder_when_no_provider(self, panel):
        tree = _render(panel, {"enabled": True, "providers": []})
        assert tree.topLevelItemCount() == 1
        assert "无 VL provider" in tree.topLevelItem(0).text(0)


class TestRoles:
    def test_four_roles_are_semantic(self, panel):
        """U2：四档角色 + 语义色（在服务=成功、今日失效=危险、未启用=三级文字）"""
        tree = _render(panel, _payload([
            _m("vl-q", 5, current=True),
            _m("vl-dead", 4, dead=True, reason="HTTP 404: Invalid model id"),
            _m("vl-standby", 3),
            _m("vl-off", 2, enabled=False),
        ]))
        key_item = tree.topLevelItem(0)
        roles = [key_item.child(i).text(1) for i in range(4)]
        assert roles == ["在服务", "今日失效", "待命", "未启用"]

        def color(i):
            return key_item.child(i).foreground(1).color().name().lower()

        assert color(0) == tokens.SUCCESS_TEXT.lower()
        assert color(1) == tokens.DANGER_TEXT.lower()
        assert color(2) == tokens.TEXT_SECONDARY.lower()
        assert color(3) == tokens.TEXT_TERTIARY.lower()

    def test_dead_row_shows_reason_and_countdown(self, panel):
        """今日失效行必须把"为什么"和"什么时候解冻"写在同一行"""
        tree = _render(panel, _payload([
            _m("vl-q", 5, current=True),
            _m("vl-dead", 4, dead=True, reason="HTTP 404: Invalid model id"),
        ]))
        note = tree.topLevelItem(0).child(1).text(4)
        assert "Invalid model id" in note
        assert "3h12m 后解冻" in note

    def test_tier_column_present(self, panel):
        tree = _render(panel, _payload([_m("vl-q", 5, current=True)]))
        assert tree.topLevelItem(0).child(0).text(2) == "T5"

    def test_key_status_when_all_dead(self, panel):
        """一个能用的模型都不剩 ⇒ key 行不能再写"可用" """
        tree = _render(panel, _payload(
            [_m("a", 5, dead=True, reason="HTTP 404: 下架"), _m("b", 4, dead=True, reason="HTTP 404: 下架")],
            available=False,
        ))
        assert tree.topLevelItem(0).text(1) == "全判死"


class TestTooltips:
    def test_long_reason_full_text_in_tooltip(self, panel):
        """U3：列表 elide，全文进 tooltip（对齐 last_error 的既有规范偏差）"""
        long_reason = "HTTP 404: " + ("Invalid model id; " * 20).strip()
        tree = _render(panel, _payload([
            _m("vl-q", 5, current=True),
            _m("vl-dead", 4, dead=True, reason=long_reason),
        ]))
        dead = tree.topLevelItem(0).child(1)
        assert dead.toolTip(4) == long_reason
        assert dead.toolTip(1) == f"判死原因: {long_reason}"

    def test_key_row_tooltip_lists_dead_models_and_reset_hint(self, panel):
        tree = _render(panel, _payload([
            _m("vl-q", 5, current=True),
            _m("vl-dead", 4, dead=True, reason="HTTP 404: x"),
        ]))
        tip = tree.topLevelItem(0).toolTip(0)
        assert "今日判死: vl-dead" in tip
        assert "3h12m 后自动解冻" in tip


class TestInlineReset:
    def test_reset_button_present_and_posts_vision_reset(self, panel, monkeypatch):
        """U4：行内复位走后端唯一入口 POST /vision/reset，且按钮先置灰"""
        shown = []
        monkeypatch.setattr(
            "client.panels.llm_pool.QMessageBox.information",
            lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""),
        )
        monkeypatch.setattr(
            "client.panels.llm_pool.QMessageBox.warning",
            lambda *a, **k: shown.append("WARN"),
        )
        tree = _render(panel, _payload([
            _m("vl-q", 5, current=True),
            _m("vl-dead", 4, dead=True, reason="HTTP 404: x"),
        ]))
        key_item = tree.topLevelItem(0)
        btn = tree.itemWidget(key_item, 5)
        assert btn is not None, "有判死模型的 key 行必须给复位动作"

        btn.click()
        assert btn.isEnabled() is False, "请求在飞时必须置灰（幂等范式）"

        worker = panel._http_workers.pop() if panel._http_workers else None
        assert worker is not None
        assert worker.wait(3000) is True
        qapp = QApplication.instance()
        qapp.processEvents()

        assert panel._http.calls[0][0] == "post"
        assert panel._http.calls[0][1] == "/vision/reset"
        assert btn.isEnabled() is True, "完成后必须恢复可点"
        assert shown and "2" in str(shown[0]), "成功后要有可见反馈（清了几条）"

    def test_no_button_when_nothing_dead(self, panel):
        """U5：今日没有判死模型的 key 没有复位按钮，并说明为什么"""
        tree = _render(panel, _payload([_m("vl-q", 5, current=True)]))
        key_item = tree.topLevelItem(0)
        assert tree.itemWidget(key_item, 5) is None
        assert key_item.text(5) == "—"
        assert "无需复位" in key_item.toolTip(5)

    def test_reset_failure_surfaces_warning(self, panel, monkeypatch):
        """后端为空响应（离线/被拒）必须弹告警，不能静默"""
        warned = []
        monkeypatch.setattr(
            "client.panels.llm_pool.QMessageBox.information", lambda *a, **k: None)
        monkeypatch.setattr(
            "client.panels.llm_pool.QMessageBox.warning",
            lambda *a, **k: warned.append(a[2] if len(a) > 2 else ""),
        )
        panel._http.post = lambda path, json=None, **kw: None  # 模拟失败
        tree = _render(panel, _payload([_m("vl-dead", 4, dead=True, reason="x")]))
        btn = tree.itemWidget(tree.topLevelItem(0), 5)
        btn.click()
        worker = panel._http_workers.pop()
        assert worker.wait(3000) is True
        QApplication.instance().processEvents()
        assert warned, "失败必须弹警告"
        assert btn.isEnabled() is True


class TestUseCaseSwitch:
    def test_switching_view_triggers_refetch_with_use_case(self, panel):
        """U6：换视角必须重拉该视角的链（否则显示的还是旧顺序）"""
        assert panel._vl_use_case == "vl_vision"
        panel._vl_use_case_combo.setCurrentIndex(1)  # 视觉定位 → 截图 OCR
        assert panel._vl_use_case == "vl_ocr"
        assert panel._vl_thread is not None, "换视角必须重拉（否则显示的还是旧顺序）"
        assert panel._vl_thread.wait(3000) is True
        QApplication.instance().processEvents()
        gets = [c for c in panel._http.calls if c[0] == "get"]
        assert gets and gets[-1][2] == {"use_case": "vl_ocr"}
