"""video_frame 端点：内存取帧、拼图、路径边界与 MCP 图片块形状。"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest

pytest.importorskip("av")
pytest.importorskip("PIL")

import av
import numpy as np
from PIL import Image


def _make_video(path: Path, seconds: int = 6, fps: int = 10, w: int = 640, h: int = 480) -> None:
    container = av.open(str(path), mode="w")
    stream = container.add_stream("mpeg4", rate=fps)
    stream.width = w
    stream.height = h
    stream.pix_fmt = "yuv420p"
    for i in range(seconds * fps):
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        arr[:, :, 0] = (i * 4) % 256
        arr[:, :, 1] = 80
        arr[:, :, 2] = (255 - i * 4) % 256
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode():
        container.mux(pkt)
    container.close()


@pytest.fixture()
def packet(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    pkt = out_root / "demo" / "packets" / "l01_g01"
    pkt.mkdir(parents=True)
    video = tmp_path / "source" / "lesson.mp4"
    video.parent.mkdir()
    _make_video(video)
    (pkt / "meta.json").write_text(json.dumps({
        "video": str(video), "duration_sec": 6,
    }), encoding="utf-8")
    (pkt / "frames.json").write_text("[]", encoding="utf-8")
    import server.video as video_mod
    monkeypatch.setattr(video_mod, "OUT_ROOT", out_root)
    return pkt


def test_single_frame_is_inline_jpeg(client, packet):
    r = client.post("/video/frame", json={"packet": str(packet), "at": 3, "width": 1600})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mcp_image_block"] is True
    assert data["mime_type"] == "image/jpeg"
    assert data["frames"] == 1 and data["sheet"] is False
    assert data["width"] == 640 and data["height"] == 480
    raw = base64.b64decode(data["image"])
    assert raw[:2] == b"\xff\xd8"
    im = Image.open(io.BytesIO(raw))
    assert im.size == (640, 480)


def test_downscale_keeps_aspect_ratio(client, packet):
    r = client.post("/video/frame", json={"packet": str(packet), "at": 3, "width": 320})
    assert r.status_code == 200, r.text
    assert (r.json()["width"], r.json()["height"]) == (320, 240)


def test_multi_frame_sheet(client, packet):
    r = client.post("/video/frame", json={"packet": str(packet), "times": [1, 3, 5]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["frames"] == 3 and data["sheet"] is True
    assert data["times"] == ["00:00:01", "00:00:03", "00:00:05"]
    im = Image.open(io.BytesIO(base64.b64decode(data["image"])))
    assert im.width > 320 and im.height > 240


def test_span_mode(client, packet):
    r = client.post("/video/frame", json={"packet": str(packet), "at": 3, "span": 2, "step": 1})
    assert r.status_code == 200, r.text
    assert r.json()["times"] == ["00:00:01", "00:00:02", "00:00:03", "00:00:04", "00:00:05"]


def test_packet_outside_out_root_rejected(client, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "meta.json").write_text("{}", encoding="utf-8")
    r = client.post("/video/frame", json={"packet": str(other), "at": 1})
    assert r.status_code == 400
    assert "OUT_ROOT" in r.text or "out" in r.text


def test_env_root_is_allowed(client, tmp_path, monkeypatch):
    """产物落到 COURSE_DIGEST_OUT（如课程库所在盘）时也要能取帧。"""
    env_root = tmp_path / "g_root"
    pkt = env_root / "demo" / "packets" / "l02_g01"
    pkt.mkdir(parents=True)
    video = tmp_path / "source2" / "lesson.mp4"
    video.parent.mkdir()
    _make_video(video, seconds=3)
    (pkt / "meta.json").write_text(json.dumps({
        "video": str(video), "duration_sec": 3,
    }), encoding="utf-8")
    monkeypatch.setenv("COURSE_DIGEST_OUT", str(env_root))
    r = client.post("/video/frame", json={"packet": str(pkt), "at": 1})
    assert r.status_code == 200, r.text
    assert r.json()["width"] == 640


def test_limit_and_range(client, packet):
    r = client.post("/video/frame", json={"packet": str(packet), "times": list(range(13))})
    assert r.status_code == 400
    r = client.post("/video/frame", json={"packet": str(packet), "at": 99})
    assert r.status_code == 400


def test_no_frame_files_written(client, packet):
    before = {p.relative_to(packet) for p in packet.rglob("*")}
    r = client.post("/video/frame", json={"packet": str(packet), "times": [1, 2, 3]})
    assert r.status_code == 200
    after = {p.relative_to(packet) for p in packet.rglob("*")}
    assert before == after


def test_image_block_is_scrubbed_from_text(client, packet):
    from server.core.mcp_gateway import _scrub_image_block

    data = client.post("/video/frame", json={"packet": str(packet), "at": 2}).json()
    summary, image, mime = _scrub_image_block(data)
    assert image == data["image"] and mime == "image/jpeg"
    assert "image" not in summary and "mcp_image_block" not in summary


def test_registered_as_direct_mcp_tool(client):
    """2026-10-27 用户裁定：video_frame 保持直连（先观察一段时间）。"""
    from server.mcp_whitelist import DIRECT_TOOLS, GATEWAY_EXCLUDE, TOOL_ANNOTATIONS

    assert "video_frame" in DIRECT_TOOLS
    assert "video_frame" not in GATEWAY_EXCLUDE
    assert TOOL_ANNOTATIONS["video_frame"]["readOnlyHint"] is True
    spec = client.get("/openapi.json").json()
    assert spec["paths"]["/video/frame"]["post"]["operationId"] == "video_frame"
