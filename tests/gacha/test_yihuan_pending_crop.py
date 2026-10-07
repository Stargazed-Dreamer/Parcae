from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path

import pytest
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def yg(tmp_path, monkeypatch):
    """Fresh collector module; pending dir redirected so nothing touches the workspace."""
    mod = _load_module("yihuan_gacha_pending_test", "workspace/yihuan_gacha/yihuan_gacha.py")
    pending = tmp_path / "pending_rarity"
    monkeypatch.setattr(mod, "PENDING_DIR", pending)
    monkeypatch.setattr(mod, "PENDING_INDEX", pending / "_index.jsonl")
    mod._pending_saved.clear()
    mod._pending_written = 0
    return mod


def _png_b64(rows: int, cols: int, swatch, swatch_box) -> str:
    """Solid near-white canvas with a colored swatch at swatch_box (x0,y0,x1,y1)."""
    img = Image.new("RGB", (cols, rows), (240, 240, 242))
    x0, y0, x1, y1 = swatch_box
    for y in range(y0, y1):
        for x in range(x0, x1):
            img.putpixel((x, y), swatch)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _bbox(box):
    x0, y0, x1, y1 = box
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


# ── detector behaviour must survive the crop-math extraction ─────────────────

def test_detector_returns_each_band_for_known_colors(yg):
    box = (20, 20, 40, 30)
    cases = [
        ((253, 181, 11), "S"),    # SKILL 权威色：金黄
        ((231, 63, 189), "A"),    # 粉紫
        ((106, 106, 106), "B"),   # 灰
        ((170, 165, 170), "B"),   # 同时满足后面那条 A 档；排在前面才拿到 B，钉住分支顺序
    ]
    for color, expected in cases:
        b64 = _png_b64(120, 200, color, box)
        assert yg.detect_rarity_by_color(b64, _bbox(box)) == expected, color


def test_detector_returns_question_when_no_text_pixels(yg):
    box = (20, 20, 40, 30)
    b64 = _png_b64(120, 200, (250, 250, 250), box)  # 全是近白背景
    assert yg.detect_rarity_by_color(b64, _bbox(box)) == "?"


def test_detector_returns_question_on_degenerate_box(yg):
    b64 = _png_b64(120, 200, (253, 181, 11), (20, 20, 40, 30))
    assert yg.detect_rarity_by_color(b64, [[5, 5], [5, 5], [5, 5], [5, 5]]) == "?"


def test_crop_helper_pads_and_clamps(yg):
    b64 = _png_b64(120, 200, (253, 181, 11), (20, 20, 40, 30))
    crop, (x0, x1, y0, y1) = yg._rarity_crop(b64, _bbox((20, 20, 40, 30)))
    assert (x0, x1, y0, y1) == (17, 43, 17, 33)
    assert crop.shape[:2] == (16, 26)
    edge, box_edge = yg._rarity_crop(b64, _bbox((0, 0, 199, 119)))
    assert box_edge == (0, 200, 0, 120) and edge.shape[:2] == (120, 200)


# ── evidence hook: two triggers, dedupe, cap ────────────────────────────────

def _index_lines(mod):
    if not mod.PENDING_INDEX.exists():
        return []
    return [json.loads(line) for line in
            mod.PENDING_INDEX.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_unknown_name_with_clean_color_still_saves_evidence(yg):
    """09-30 那 6 项就是这么丢的：颜色判得出 S，但权威表里没有键，当时只留个 [?]。"""
    box = (20, 20, 40, 30)
    b64 = _png_b64(120, 200, (253, 181, 11), box)
    yg.note_rarity_evidence(b64, _bbox(box), "从未登记过的弧盘", "2026年10月7日 01:00:00",
                            "S", is_weapon=True)

    lines = _index_lines(yg)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["reason"] == "not_in_rarity_map"
    assert entry["rarity_key"] == "弧盘·从未登记过的弧盘"
    assert entry["detected"] == "S"
    assert entry["avg_rgb"] is not None and entry["crop_box"] == [17, 43, 17, 33]
    png = yg.PENDING_DIR / entry["png"]
    assert png.exists(), "索引记了 png 名但文件没落盘"
    assert Image.open(png).size == (26, 16)


def test_indeterminate_color_saves_evidence_even_without_a_box_match(yg):
    box = (20, 20, 40, 30)
    b64 = _png_b64(120, 200, (250, 250, 250), box)  # 检测返回 ?
    yg.note_rarity_evidence(b64, _bbox(box), "角色·某新角色", "2026年10月7日 01:00:00", "?")

    entry = _index_lines(yg)[0]
    assert entry["reason"] == "color_indeterminate"
    assert entry["avg_rgb"] is None            # 文本像素不足，均值算不出来
    assert entry["png"] is not None            # 框还在，裁片照存
    assert not any(c in entry["png"] for c in '\\/:*?"<>|')  # "?" 曾是非法文件名


def test_item_already_in_authority_map_is_not_saved(yg):
    box = (20, 20, 40, 30)
    b64 = _png_b64(120, 200, (253, 181, 11), box)
    yg.note_rarity_evidence(b64, _bbox(box), "好狗狗走四方", "2026年10月7日 01:00:00",
                            "S", is_weapon=True)
    assert _index_lines(yg) == []
    assert not (yg.PENDING_DIR / "_index.jsonl").exists()


def test_same_key_saved_once_and_unreadable_image_still_logged(yg):
    box = (20, 20, 40, 30)
    b64 = _png_b64(120, 200, (253, 181, 11), box)
    for _ in range(3):
        yg.note_rarity_evidence(b64, _bbox(box), "重复出现的新弧盘", "2026年10月7日 01:00:00",
                                "S", is_weapon=True)
    assert len(_index_lines(yg)) == 1
    assert yg._pending_written == 1

    yg.note_rarity_evidence("not-base64", _bbox(box), "解码失败的新弧盘",
                            "2026年10月7日 01:00:00", "S", is_weapon=True)
    lines = _index_lines(yg)
    assert len(lines) == 2
    assert lines[1]["png"] is None and lines[1]["rarity_key"] == "弧盘·解码失败的新弧盘"


def test_run_cap_stops_writing_and_reports_once(yg, capsys):
    yg.PENDING_MAX_PER_RUN = 2
    box = (20, 20, 40, 30)
    b64 = _png_b64(120, 200, (253, 181, 11), box)
    for i in range(5):
        yg.note_rarity_evidence(b64, _bbox(box), f"第{i}个新弧盘",
                                "2026年10月7日 01:00:00", "S", is_weapon=True)
    assert len(_index_lines(yg)) == 2
    out = capsys.readouterr().out
    assert "上限" in out and out.count("上限") == 1


def test_clean_module_failure_degrades_to_color_trigger_only(yg, monkeypatch):
    """加载不到 yihuan_clean 时，颜色判不出仍要落盘；表内判据整条跳过而不是报错。"""
    monkeypatch.setattr(yg, "_clean_mod", lambda: None)
    box = (20, 20, 40, 30)
    white = _png_b64(120, 200, (250, 250, 250), box)
    yg.note_rarity_evidence(white, _bbox(box), "某新弧盘", "2026年10月7日 01:00:00", "?")
    assert _index_lines(yg)[0]["reason"] == "color_indeterminate"

    yg.note_rarity_evidence(_png_b64(120, 200, (253, 181, 11), box),
                            _bbox(box), "另一个新弧盘", "2026年10月7日 01:00:00", "S")
    assert len(_index_lines(yg)) == 1


def test_six_items_confirmed_on_20261007_are_now_in_map(yg):
    """入表的键要能被消费方自己的清洗路径命中，否则表写了等于没写。"""
    mod = yg._clean_mod()
    assert mod is not None
    names = ["焰魂狂飙", "「最后一朵玫瑰」", "错误的门", "茶花会", "穿过胭红蜃景", "现实避难所"]
    for name in names:
        key = mod.clean_name(f"弧盘·{name}")
        assert mod.RARITY_MAP.get(key) == "S", key
        assert mod.clean_name(key) == key
