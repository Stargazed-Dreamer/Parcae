"""截图失败健壮性测试（2026-10-17 BitBlt 偶发失败事故后补）

事故背景：凌晨 `POST /screen/capture` 偶发 500
（`ScreenShotError: Windows graphics function failed (no error provided): BitBlt`，
GetLastError()==0）。同一进程失败前 18 秒、失败后 4 分 50 秒均抓取成功 ⇒ 判定为
**源 DC 瞬时失效的抖动**，但暴露两条防线都没兜住：

- L1 `capture._grab_virtual_screen`：没有自愈手段。mss 的 GDI 后端把源 DC 长期挂在
  实例上，失效后该线程必然持续失败 ⇒ 失败必须丢弃实例、重建换 DC、重试。
- L2 HTTP 层 `routes._capture_http_error`：原先对截图异常不做处理，一次环境抖动
  就＝500 + 两份各约 100 行栈（middleware 一遍、uvicorn 一遍）。

本文件用假 mss 实例把这三条钉死，防止回退到"抖一次炸一次"。
"""

import pytest
from mss.exception import ScreenShotError

import server.screen.capture as cap
from server.screen import routes

_BITBLT_ERR = ScreenShotError("Windows graphics function failed (no error provided): BitBlt")
_MONITORS = [{"left": -1920, "top": 0, "width": 8, "height": 8}]


@pytest.fixture(autouse=True)
def _clean_thread_local_mss():
    """每个用例后清掉线程本地 mss 实例，避免假实例泄漏到其他测试。"""
    yield
    cap._mss_local.sct = None


class _FakeShot:
    size = (8, 8)
    rgb = bytes(8 * 8 * 3)


class _FakeSct:
    """假 mss 实例：记录被创建/关闭/grab 次数，按配置抛 BitBlt 错误。"""

    instances: list["_FakeSct"] = []

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.grab_count = 0
        self.closed = False
        self.monitors = _MONITORS
        _FakeSct.instances.append(self)

    def grab(self, monitor):
        self.grab_count += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise _BITBLT_ERR
        return _FakeShot()

    def close(self):
        self.closed = True


def _install_fake_get_mss(monkeypatch, *, instances_before_recovery: int = 1):
    """替换 _get_mss：前 N 个实例会 BitBlt 失败一次，之后的实例恢复正常。"""
    _FakeSct.instances.clear()
    cap._mss_local.sct = None

    def fake_get_mss():
        sct = getattr(cap._mss_local, "sct", None)
        if sct is None:
            fail = 1 if len(_FakeSct.instances) < instances_before_recovery else 0
            cap._mss_local.sct = _FakeSct(fail_times=fail)
        return cap._mss_local.sct

    monkeypatch.setattr(cap, "_get_mss", fake_get_mss)
    monkeypatch.setattr(cap, "_CAPTURE_RETRY_DELAY", 0.0)  # 测试里不真的等待


class TestGrabVirtualScreenSelfHealing:
    """L1：BitBlt 失败后的自愈重试"""

    def test_rebuilds_instance_and_succeeds(self, monkeypatch):
        """首个实例失败 → 丢弃并重建 → 第二次抓取成功"""
        _install_fake_get_mss(monkeypatch, instances_before_recovery=1)

        img, left, top = cap._grab_virtual_screen()

        assert img.size == (8, 8)
        assert (left, top) == (-1920, 0)
        assert len(_FakeSct.instances) == 2, "应重建出第二个实例"
        assert _FakeSct.instances[0].closed is True, "失效实例必须被 close（释放源 DC）"
        assert [s.grab_count for s in _FakeSct.instances] == [1, 1]

    def test_raises_original_error_after_retries_exhausted(self, monkeypatch):
        """持续失败：重试 _CAPTURE_RETRY 次后抛出原始异常类型（调用方无需适配）"""
        _install_fake_get_mss(monkeypatch, instances_before_recovery=99)

        with pytest.raises(ScreenShotError):
            cap._grab_virtual_screen()

        assert cap._CAPTURE_RETRY >= 1, "自愈重试必须开着，否则一次抖动就炸"
        assert len(_FakeSct.instances) == cap._CAPTURE_RETRY + 1
        assert all(s.closed for s in _FakeSct.instances[:-1]), "每次重试前都应丢弃上一个实例"

    def test_fullscreen_png_path_uses_same_entrypoint(self, monkeypatch):
        """_capture_fullscreen 走同一咽喉（窗口截图的最终兜底也由此受益）"""
        _install_fake_get_mss(monkeypatch, instances_before_recovery=1)
        assert cap._capture_fullscreen().startswith(b"\x89PNG")


class TestCaptureEndpointFailureSemantics:
    """L2：HTTP 层不再把环境抖动伪装成 500"""

    def _boom(self):
        raise ScreenShotError("Windows graphics function failed (no error provided): BitBlt")

    def test_fullscreen_failure_returns_503_with_reason(self, client, monkeypatch):
        """非锁屏的截图失败：503 + 原始错因原样回传（不掩盖）"""
        monkeypatch.setattr(routes, "_capture_fullscreen", self._boom)
        monkeypatch.setattr(routes, "is_desktop_locked", lambda: False)

        resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})

        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "BitBlt" in detail, f"错因必须在 detail 里，实际={detail}"

    def test_locked_desktop_is_reported_as_environment(self, client, monkeypatch):
        """桌面锁定：明确归因为环境问题并给出可行动提示（与 screen_vl loop 的
        skipped_locked 第三态同口径）"""
        monkeypatch.setattr(routes, "_capture_fullscreen", self._boom)
        monkeypatch.setattr(routes, "is_desktop_locked", lambda: True)

        resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})

        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "桌面已锁定" in detail and "解锁" in detail, f"实际={detail}"
