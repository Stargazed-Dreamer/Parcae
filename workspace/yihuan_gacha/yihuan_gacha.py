"""异环抽卡记录收集器 v4 - 骰子点数识别+集点赠礼检测

改进点（相比 v3）：
- 骰子点数识别：通过图像处理检测骰子图标中的亮色圆点数量(1-6)
- 集点赠礼检测：OCR文字+颜色+形状三重检测，0=赠礼
- 优化stdout输出：展示页数/总页数、点数、名称、数量、时间
- 页码跟踪：记录已读页码，检测翻页是否真正生效
- 重复检测：如果翻页后页码未变化，自动重试
- 连续失败重试：连续3次翻页失败后停止，避免死循环
- 颜色稀有度检测：通过裁切文本区域像素判断 S/A/B 稀有度
"""
import requests
import time
import json
import random
import re
import base64
import io
import os
import sys
from pathlib import Path
from collections import deque, Counter
from datetime import datetime
from typing import Any
from PIL import Image

# ---- 后端地址动态解析：端口可能被 Windows 保留区顶走而顺延（docs/environment-constraints.md）
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "lib" / "runtime_endpoint.py").exists():
        _sys.path.insert(0, str(_p))
        break

from lib.runtime_endpoint import resolve_base_url as _resolve_base_url  # noqa: E402

API = _resolve_base_url(probe_health=False)
WINDOW = "异环  "
OUTPUT_DIR = Path("workspace/yihuan_gacha")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 调试模式：YIYUAN_DEBUG=1 开启后打印每步耗时（截图/OCR/翻页）
DEBUG = os.environ.get("YIYUAN_DEBUG") == "1"

# OCR 超时（秒）：CPU 推理大图约 15-40s/页，留足余量；超时说明后端异常，避免脚本永久挂死
OCR_TIMEOUT = 180

# 下面这些像素常量都是在 REF_WIDTH 宽的样本图上量的（test_samples/ 5 张 2560×1440）。
# 窗口尺寸或 DPI 一变，绝对坐标就整体错位（2026-10-30 实测：窗口 1938 宽时骰子实际
# 中心 x≈462，而旧的固定 610 完全没覆盖到）。运行时按 scale = 截图宽/REF_WIDTH 归一化，
# 骰子列优先用表头「投掷点数」的 OCR 锚点，见 detect_layout()。
REF_WIDTH = 2560

# 骰子图标中心x坐标（v2 修正：通过5张样本图分析确认实际中心x=610，bbox [584,636]）
# 原 v1 用的 629 也落在骰子区域内但非中心，导致 y 裁切范围偏移截断骰子下半部分
DICE_CX = 610
DICE_HALF_W = 40          # 骰子裁切半宽（覆盖宽 53 + 余量）
DICE_SIZE_RANGE = (45, 60)  # 骰子边长范围（正方形）
DICE_X_TOL = 15           # 骰子中心 x 相对列锚点的容差

# 稀有度待复核区：判不出或权威表里没有的物品，落文本裁片 + 一行索引。
# 2026-10-07 之前的版本只留文字结果不留像素，09-30 那 6 个新物品因此无源可查。
PENDING_DIR = OUTPUT_DIR / "pending_rarity"
PENDING_INDEX = PENDING_DIR / "_index.jsonl"
PENDING_MAX_PER_RUN = 40
_pending_saved: set[str] = set()
_pending_written = 0


_window_bbox_cache = None
_window_hwnd_cache = None
_vscreen_offset_cache = None


def _get_window_info():
    """获取异环窗口的 bbox 与 hwnd（一次查询，两项都缓存）

    2026-10-30：点击只传 window_title 时，后端要按标题模糊匹配一次窗口；
    标题带尾随空格、又可能同时存在多个相似窗口，显式带 hwnd 最稳。
    """
    global _window_bbox_cache, _window_hwnd_cache
    if _window_bbox_cache and _window_hwnd_cache:
        return {"bbox": _window_bbox_cache, "hwnd": _window_hwnd_cache}
    try:
        resp = requests.get(f"{API}/screen/windows", timeout=10)
        for w in resp.json().get("windows", []):
            if (w.get("title") or "").strip() == WINDOW.strip():
                _window_bbox_cache = w.get("bbox")
                _window_hwnd_cache = w.get("hwnd")
                return {"bbox": _window_bbox_cache, "hwnd": _window_hwnd_cache}
    except Exception as e:
        print(f"  [!] 取窗口信息失败: {e}")
    return None


def _get_window_bbox():
    """获取异环窗口的bbox（带缓存）"""
    info = _get_window_info()
    return info["bbox"] if info else None


def ensure_foreground(reason=""):
    """把游戏窗口拉到前台，并确认真的到了前台。

    后端 /screen/action 自带 activate_window（默认 True），但有两种情况会让它白给：
    - UIA 融合命中时按设计「后台 invoke 不抢焦点」，会被 skip —— 游戏没有 UIA 元素，
      真正命中的往往是盖在上面的客户端面板，点了等于点空；
    - 有模态/弹窗抢走前台时，后端只打一条 focus_warning 就继续发事件。
    所以这里做一次显式前置 + 前台校验，作为点击前的兜底。
    """
    info = _get_window_info()
    payload = {"window_title": WINDOW}
    if info and info.get("hwnd"):
        payload["hwnd"] = info["hwnd"]
    try:
        data = requests.post(f"{API}/screen/focus-window", json=payload, timeout=30).json()
    except Exception as e:
        print(f"  [!] 前置窗口失败({reason}): {e}")
        return False
    fg_title = (data.get("foreground_title") or "").strip()
    ok = bool(data.get("success")) and fg_title == WINDOW.strip()
    if not ok:
        print(f"  [!] 前置窗口未生效({reason}): success={data.get('success')} 前台={fg_title!r}")
    return ok


def _get_vscreen_offset():
    """获取虚拟屏幕左上角偏移（带缓存）"""
    global _vscreen_offset_cache
    if _vscreen_offset_cache is not None:
        return _vscreen_offset_cache
    import ctypes
    _vscreen_offset_cache = (
        ctypes.windll.user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        ctypes.windll.user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
    )
    return _vscreen_offset_cache


def capture_window():
    """截取异环窗口，返回 base64 图片（含重试）

    用 PrintWindow（force_fullscreen_crop=False）：不要求游戏在前台。
    此前用「全屏截图+裁剪」，一旦窗口被别的程序盖住就会截到遮挡物
    （2026-10-30 实测：窗口切到后台后截到的是微信界面）。
    若把游戏改成独占全屏，PrintWindow 可能返回错误内容——那时改回 True。"""
    for attempt in range(3):
        try:
            resp = requests.post(f"{API}/screen/capture",
                json={"mode": "window", "window_title": WINDOW, "format": "base64",
                      "force_fullscreen_crop": False}, timeout=30)
            data = resp.json()
            if not data.get("success"):
                print(f"截图失败(重试{attempt+1}/3): {data.get('detail', data)}")
                time.sleep(2)
                continue
            return data["image"]
        except Exception as e:
            print(f"截图异常(重试{attempt+1}/3): {e}")
            time.sleep(2)
    print(f"截图失败，已重试3次")
    return None


def ocr_image(image_b64):
    """OCR识别图片（带超时，防止后端挂死时脚本永久阻塞）"""
    try:
        resp = requests.post(f"{API}/ocr/base64", data={"data": image_b64}, timeout=OCR_TIMEOUT)
    except requests.exceptions.Timeout:
        print(f"[!] OCR 超时（{OCR_TIMEOUT}s），后端可能异常（GPU 推理挂死？），放弃本页")
        return None, []
    except requests.exceptions.RequestException as e:
        print(f"[!] OCR 请求异常: {e}")
        return None, []
    data = resp.json()
    if not data.get("success"):
        print(f"OCR失败: {data}")
        return None, []
    return data.get("text", ""), data.get("details", [])


def capture_and_ocr():
    """截图+OCR一步完成（调试模式下打印各步耗时）"""
    t0 = time.perf_counter()
    image = capture_window()
    if not image:
        return None, []
    t1 = time.perf_counter()
    text, details = ocr_image(image)
    t2 = time.perf_counter()
    if DEBUG:
        print(f"    [debug] 截图 {t1-t0:.1f}s | OCR {t2-t1:.1f}s | {len(details)} 项")
    return image, details


def _decode_frame(image_b64):
    """base64 → ndarray。"""
    import numpy as np

    return np.array(Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB"))


def _encode_frame(arr):
    """ndarray → base64 PNG。"""
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def learn_page_roi(details, img_w, img_h, margin=16, bottom_pad=60):
    """从 OCR 结果里只留「后续页真正要用」的文本，取它们的外接矩形当 ROI。

    要的：表头（投掷点数/道具名称/数量/获得时间，武器池是弧盘列表/研募类型/研募时间）、
    数据行（角色·xx 这类名称、时间、页码）、翻页按钮。
    不要的：窗口标题、关闭按钮、「棋盘详情」标题、tab、提示语、底部开关、UID——
    它们会把 ROI 撑到整屏（实测：全文本范围 70%，最小集约 27%，OCR 4.3s → 2.2s）。

    y 上沿从表头起（棋盘类型只在首帧用全图验证，后续页不需要）；
    下沿留 bottom_pad 余量，防止行数变化把「下一页」挤到框外。
    """
    keep_words = ("投掷点数", "道具名称", "数量", "获得时间",
                  "弧盘列表", "研募类型", "研募时间", "上一页", "下一页")
    keep_regex = (r"^\d+\s*/\s*\d+$", r"\d{4}年\d{1,2}月\d{1,2}日",
                  r"^(角色|道具|弧盘|滑翔翼)·")
    head_words = ("投掷点数", "弧盘列表", "道具名称", "研募类型")

    boxes = []
    head_cy = None
    for it in details or []:
        text = (it.get("text") or "").strip()
        box = it.get("box") or []
        if not box:
            continue
        if not (any(k in text for k in keep_words) or any(re.search(rx, text) for rx in keep_regex)):
            continue
        boxes.append(box)
        if head_cy is None and any(k in text for k in head_words):
            head_cy = sum(p[1] for p in box) / 4

    if not boxes:
        return None
    pts = [p for box in boxes for p in box]
    x0 = max(0, int(min(p[0] for p in pts)) - margin)
    x1 = min(img_w, int(max(p[0] for p in pts)) + margin)
    y0 = max(0, int(head_cy - 40) if head_cy is not None else int(min(p[1] for p in pts)) - margin)
    y1 = min(img_h, int(max(p[1] for p in pts)) + bottom_pad)
    if x1 <= x0 or y1 <= y0:
        return None
    if (x1 - x0) * (y1 - y0) > 0.90 * img_w * img_h:
        return None
    return (x0, y0, x1, y1)


def roi_anchors_ok(details):
    """裁剪页的结果必须还认得出表头和页码，否则说明切掉了关键内容。"""
    texts = [it.get("text", "") for it in (details or [])]
    has_head = any(k in t for t in texts for k in ("投掷点数", "道具名称", "弧盘列表", "研募类型"))
    has_page = any(re.fullmatch(r"\d+\s*/\s*\d+", t.strip()) for t in texts)
    return has_head and has_page


def ocr_roi(image_b64, roi):
    """按 ROI 裁剪后 OCR，返回的 bbox 已换算回全图坐标。

    ROI 内的结果缺表头或页码时自动退回全图重识别（宁可慢，不要漏）。
    """
    if not roi:
        return ocr_image(image_b64)
    try:
        arr = _decode_frame(image_b64)
        x0, y0, x1, y1 = roi
        sub = arr[y0:y1, x0:x1]
        text, details = ocr_image(_encode_frame(sub))
        if details and roi_anchors_ok(details):
            for it in details:
                it["box"] = [[p[0] + x0, p[1] + y0] for p in (it.get("box") or [])]
            return text, details
        if DEBUG:
            print("    [debug] ROI 结果缺表头/页码，回退全图 OCR")
    except Exception as e:
        print(f"  [!] ROI OCR 失败，回退全图: {e}")
    return ocr_image(image_b64)


def ensure_authorization():
    """申请屏幕控制授权（采集前主动调用，防后端重启/授权过期导致点击 403）

    POST /screen/control/request 会弹窗阻塞等待用户批准（后端默认 30s 超时），
    响应 status="authorized" 表示成功。已授权时重新请求也会覆盖刷新（幂等）。
    """
    try:
        resp = requests.post(f"{API}/screen/control/request", json={
            "source": "yihuan_gacha",
            "task_description": "异环抽卡记录采集：需要自动翻页读取掷骰记录",
        }, timeout=60)
        data = resp.json()
    except Exception as e:
        print(f"[!] 授权申请失败: {e}")
        return False
    if data.get("status") == "authorized":
        print("屏幕控制授权: 已通过")
        return True
    print(f"[!] 屏幕控制授权未通过: status={data.get('status')} {data.get('message', '')}")
    return False


def reset_window_bbox_cache() -> None:
    """窗口可能被移动/缩放/重建，采集开始前清一次偏移与 hwnd 缓存。"""
    global _window_bbox_cache, _window_hwnd_cache
    _window_bbox_cache = None
    _window_hwnd_cache = None


def click_at(x, y, element_text="", require_confirm=False):
    """点击指定坐标；x/y 传窗口内坐标，内部换算成屏幕绝对坐标。

    /screen/action 的 x/y 是屏幕绝对坐标，而 OCR bbox 是窗口截图内的坐标。
    游戏全屏时窗口偏移为 (0,0)，两者相等，所以以前没加偏移也能跑；
    窗口化后偏移非零（2026-10-30：left=311 top=117），不加就整片点偏、翻页永远不生效。

    稳健性（2026-10-30 第一次采集失败后加的）：
    - 显式带 hwnd：不让后端按带尾随空格的标题模糊匹配；
    - strategy="event" 关掉 UIA 融合：融合命中时会「后台 invoke 不抢焦点」，
      对游戏无效且把点击吞掉（同一坐标上的客户端面板就是这种情况）；
    - activate_window=True 用后端自带的前置，再核对 target_match_before，
      前置没成（被弹窗盖住）就先 focus-window 重试一次，不把点击发到错误窗口。
    """
    info = _get_window_info()
    if info and info.get("bbox"):
        x = int(x) + int(info["bbox"]["left"])
        y = int(y) + int(info["bbox"]["top"])

    def _payload():
        p = {
            "action": "click", "x": int(x), "y": int(y),
            "window_title": WINDOW, "element_text": element_text,
            "require_confirm": require_confirm,
            "strategy": "event",      # 强制真实键鼠，别走 UIA 后台 invoke
            "activate_window": True,  # 后端原生：先把目标窗口拉到前台再点
        }
        if info and info.get("hwnd"):
            p["hwnd"] = info["hwnd"]
        return p

    def _post():
        r = requests.post(f"{API}/screen/action", json=_payload())
        if r.status_code == 403:
            # 授权丢失/过期（后端重启或 idle 超时撤销）→ 申请后重试一次
            print("  [!] 点击被拒（403 未授权），尝试申请屏幕控制授权...")
            if ensure_authorization():
                r = requests.post(f"{API}/screen/action", json=_payload())
        return r

    resp = _post()
    if resp.status_code == 422:
        errors = resp.json().get("detail", [])
        err_info = "; ".join(
            f"{e.get('loc',['?'][-1])}: {e.get('msg','')}" for e in errors
        )
        print(f"  点击({x},{y}) '{element_text}': 参数验证失败 - {err_info}")
        return False
    data = resp.json()

    # 前置校验：点击发生时前台若已不是游戏，这一下多半打空/打到弹窗上，重来一次
    if data.get("success") and data.get("target_match_before") is False:
        fg = (data.get("foreground_before") or {}).get("title") or "未知"
        print(f"  [!] 点击前前台是 {fg!r}（非游戏），强制前置后重试")
        if ensure_foreground("点击前前台漂移"):
            data = _post().json()

    success = data.get("success", False)
    if not success:
        print(f"  点击({x},{y}) '{element_text}': FAILED status={data.get('status')} msg={data.get('message', '')}")
    return success


def find_text_center(details, target_text, y_min=0, y_max=9999):
    """在OCR结果中查找指定文本的中心坐标"""
    for item in details:
        text = item.get("text", "")
        box = item.get("box", [])
        if target_text in text and box:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = sum(xs) / 4 + random.randint(-3, 3)
            cy = sum(ys) / 4 + random.randint(-3, 3)
            if y_min <= cy <= y_max:
                return cx, cy, text
    return None


def _rarity_crop(image_b64, bbox):
    """bbox 外扩 3px 并夹到图内，返回 (裁片, (x0,x1,y0,y1))；退化框返回 None。"""
    import numpy as np

    img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
    arr = np.array(img)
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    x_min = max(0, int(min(xs)) - 3)
    x_max = min(arr.shape[1], int(max(xs)) + 3)
    y_min = max(0, int(min(ys)) - 3)
    y_max = min(arr.shape[0], int(max(ys)) + 3)
    if x_max <= x_min or y_max <= y_min:
        return None
    return arr[y_min:y_max, x_min:x_max], (x_min, x_max, y_min, y_max)


def _rgb_to_hsv(px):
    """(N,3) 0-255 → hue(0-360) / sat(0-1) / val(0-1)，向量化。"""
    import numpy as np

    f = px.astype(np.float64) / 255.0
    mx = f.max(axis=1)
    mn = f.min(axis=1)
    diff = mx - mn
    sat = np.where(mx > 1e-6, diff / np.maximum(mx, 1e-6), 0.0)
    hue = np.zeros_like(mx)
    nz = diff > 1e-6
    r, g, b = f[:, 0], f[:, 1], f[:, 2]
    m = nz & (mx == r)
    hue[m] = ((g[m] - b[m]) / diff[m]) % 6
    m = nz & (mx == g) & ~(mx == r)
    hue[m] = (b[m] - r[m]) / diff[m] + 2
    m = nz & (mx == b) & ~(mx == r) & ~(mx == g)
    hue[m] = (r[m] - g[m]) / diff[m] + 4
    return hue * 60, sat, mx


def _text_stats(crop):
    """剔掉近白背景与近黑边框后，返回文本像素的颜色统计。

    - rgb_all: 全部文本像素均值（沿用旧口径，写进待复核索引供事后复核）
    - hue/sat/val: 核心口径的中位数，只留高饱和且不太暗的笔画中心，
      把抗锯齿边缘和黑边过渡像素排除在外
    有效像素不足 5 个返回 None。
    """
    import numpy as np

    r, g, b = crop[:, :, 0], crop[:, :, 1], crop[:, :, 2]
    is_text = ~((r > 200) & (g > 200) & (b > 200)) & ~((r < 40) & (g < 40) & (b < 40))
    px = crop[is_text]
    if len(px) < 5:
        return None
    rgb_all = (float(px[:, 0].mean()), float(px[:, 1].mean()), float(px[:, 2].mean()))

    hue, sat, val = _rgb_to_hsv(px)
    core = (sat > 0.55) & (val > 0.35)
    n_core = int(core.sum())
    if n_core >= 10:
        idx = np.where(core)[0]
        hue_c = float(np.median(hue[idx]))
        sat_c = float(np.median(sat[idx]))
        val_c = float(np.median(val[idx]))
        rgb_core = (float(px[idx, 0].mean()), float(px[idx, 1].mean()), float(px[idx, 2].mean()))
    else:
        hue_c = float(np.median(hue))
        sat_c = float(np.median(sat))
        val_c = float(np.median(val))
        rgb_core = rgb_all
    return {
        "rgb_all": rgb_all,
        "rgb_core": rgb_core,
        "hue": hue_c,
        "sat": sat_c,
        "val": val_c,
        "n": int(len(px)),
        "n_core": n_core,
    }


def _classify_rarity(stats):
    """按 HSV 归档：先看饱和度分「灰 / 彩色」，再用色相分金 / 粉。

    2026-10-30 实测（2560 样本，核心口径）：
      S 金黄 核心 RGB(228,163,12) hue≈42°  sat≈0.96
      A 粉紫 核心 RGB(210,58,172) hue≈315° sat≈0.73
      B 灰   核心 RGB(122,122,122)          sat≈0.00
    旧的「RGB 均值 > 200」会被抗锯齿拉低到 197~199，整批 S 判不出
    （明音凛/灵可/失纬棋子/质实骰子 5 条实测全卡在门槛下）。
    """
    if stats is None:
        return "?"
    if stats["sat"] < 0.25:
        return "B"
    hue = stats["hue"]
    if 25 <= hue <= 75:
        return "S"
    if 280 <= hue <= 350:
        return "A"
    return "?"


def detect_rarity_by_color(image_b64, bbox):
    """通过裁切文本区域检测颜色来判断稀有度
    S级=金黄色(253,181,11), A级=粉紫色(231,63,189), B级=灰色(106,106,106)
    """
    try:
        cropped = _rarity_crop(image_b64, bbox)
        if cropped is None:
            return "?"
        return _classify_rarity(_text_stats(cropped[0]))
    except Exception as e:
        print(f"  颜色检测失败: {e}")
        return "?"


def resolve_rarity(image_b64, bbox, raw_name, is_weapon=False):
    """先查权威表，查不到再用颜色判。

    权威表是用户确认过的真值，优先级高于图像；颜色只用来判断表里没有的新物品。
    返回 (rarity, source)，source ∈ {"rarity_map", "color"}。
    """
    mod = _clean_mod()
    if mod is not None:
        prefixed = f"弧盘·{raw_name}" if (is_weapon and not raw_name.startswith("弧盘·")) else raw_name
        hit = mod.RARITY_MAP.get(mod.clean_name(prefixed))
        if hit:
            return hit, "rarity_map"
    return detect_rarity_by_color(image_b64, bbox), "color"


_clean_module = None


def _clean_mod() -> Any:
    """按需加载同目录的 yihuan_clean（权威表 RARITY_MAP 与名称清洗在它那一侧）。

    用 importlib 而不是 import：本脚本既可以 `python workspace/.../yihuan_gacha.py`
    跑，也可以被 tests 用 spec_from_file_location 装载，后一种情况下同目录不在 sys.path。
    """
    global _clean_module
    if _clean_module is None:
        try:
            import importlib.util

            path = Path(__file__).resolve().parent / "yihuan_clean.py"
            spec = importlib.util.spec_from_file_location("yihuan_clean_sidecar", path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load spec for {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _clean_module = module
        except Exception as e:
            print(f"  [!] 无法加载 yihuan_clean.py，权威表判据不可用: {e}")
            _clean_module = False
    return _clean_module if _clean_module else None


def _safe_filename(name):
    """Windows 文件名安全化：非法字符换下划线，限长 48。"""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return (cleaned or "unnamed")[:48]


def note_rarity_evidence(image_b64, bbox, raw_name, time_str, rarity, is_weapon=False):
    """稀有度要人复核时，把文本裁片和一行索引落到 PENDING_DIR。

    触发两条：颜色判不出；或颜色判得出但权威表里没有这个键（09-30 那 6 项就是这条，
    当时只留了个 [?] 而没有像素，事后无从复核）。同一键只存一张，单次运行封顶。
    """
    global _pending_written

    mod = _clean_mod()
    key = raw_name
    if mod is not None:
        prefixed = f"弧盘·{raw_name}" if (is_weapon and not raw_name.startswith("弧盘·")) else raw_name
        key = mod.clean_name(prefixed)
    if rarity == "?":
        reason = "color_indeterminate"
    elif mod is not None and key not in mod.RARITY_MAP:
        reason = "not_in_rarity_map"
    else:
        return

    if key in _pending_saved:
        return
    if _pending_written >= PENDING_MAX_PER_RUN:
        if _pending_written == PENDING_MAX_PER_RUN:
            _pending_written += 1
            print(f"  [!] 待复核裁片已达上限 {PENDING_MAX_PER_RUN} 张，本进程内不再落盘")
        return

    entry = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "record_time": time_str,
        "raw_name": raw_name,
        "rarity_key": key,
        "detected": rarity,
        "reason": reason,
        "bbox": [[int(p[0]), int(p[1])] for p in bbox] if bbox else None,
        "crop_box": None,
        "avg_rgb": None,
        "hsv": None,
        "png": None,
    }

    try:
        cropped = _rarity_crop(image_b64, bbox) if image_b64 and bbox else None
        if cropped is not None:
            crop, box = cropped
            entry["crop_box"] = list(box)
            st = _text_stats(crop)
            if st is not None:
                entry["avg_rgb"] = [round(v, 1) for v in st["rgb_all"]]
                entry["hsv"] = {
                    "hue": round(st["hue"], 1),
                    "sat": round(st["sat"], 2),
                    "val": round(st["val"], 2),
                    "n": st["n"],
                    "n_core": st["n_core"],
                }
            PENDING_DIR.mkdir(parents=True, exist_ok=True)
            # rarity 可能是 "?"，而它在 Windows 文件名里非法，所以整段再过一遍安全化
            stem = (f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{rarity}_{_safe_filename(key)}")
            png_name = _safe_filename(stem)[:80] + ".png"
            Image.fromarray(crop).save(PENDING_DIR / png_name, format="PNG")
            entry["png"] = png_name
    except Exception as e:
        print(f"  [!] 待复核裁片落盘失败（仍记索引）: {e}")

    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        with io.open(PENDING_INDEX, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"  [!] 待复核索引写入失败: {e}")
        return

    _pending_saved.add(key)
    _pending_written += 1
    print(f"  [待复核] {key} 检测={rarity} 原因={reason} → {entry['png'] or '(无裁片，仅索引)'}")



def detect_layout(details, img_width=0):
    """从 OCR 表头锚点推导各列位置，窗口尺寸变化时自动适应。

    骰子列优先取表头「投掷点数」的中心 x；表头没识别出来时由
    detect_dice_points 按 scale = 截图宽/REF_WIDTH 对参考常量做归一化兜底。
    img_width 未知时 scale 留空，由调用方（能拿到图的那侧）补。
    """
    scale = (img_width / REF_WIDTH) if img_width else None
    layout = {
        "scale": scale,
        "dice_cx": None,
        "dice_source": "fallback",
    }
    for item in details or []:
        text = item.get("text", "")
        box = item.get("box") or []
        if "投掷点数" in text and box:
            xs = [p[0] for p in box]
            layout["dice_cx"] = sum(xs) / len(xs)
            layout["dice_source"] = "header"
            break
    return layout


def detect_dice_points(image_b64, name_cy, name_h, ocr_details=None, layout=None):
    """检测骰子点数（图像处理，v2 算法）

    v2 改进（2026-07-09）：定向裁切 + 封闭圆角方形强约束
    - 修正 DICE_CX=610（原 629 非中心）
    - 定向裁切：x 围绕骰子列锚点（±40×scale），y 围绕 name_cy（-0.5*name_h ~ +1.5*name_h）
      y 搜索范围 2*name_h ≈ 70 < 骰子间距 80，不会匹配到相邻骰子
    - 强约束筛选：尺寸 45-60、宽高比 0.8-1.2、填充率>0.5、cx 贴近列锚点
    - 解决了 v1 y 裁切范围太窄截断骰子下半部分导致点数偏少的问题

    2026-10-30：列锚点与尺寸常量改为按布局自适应（layout 由 detect_layout 产出），
    不再假定窗口一定和样本图同宽。

    算法：
    1. OCR检查"集点赠礼"/"沉眠地"文字
    2. 定向裁切骰子区域
    3. 区分骰子底色(RGB~82,83,82,G略高)和赠礼文字(RGB~74,74,74,RGB几乎相等)
    4. 找所有骰子底色连通域，用强约束筛选"封闭圆角方形"
    5. 选 y 偏移最接近 name_h*0.5 的候选（骰子通常在 name 下方半个 name_h）
    6. 在骰子内部找亮色像素(RGB~224,224,226) → 连通域计数(过滤<30px噪点) = 点数

    返回: 1-6=点数, 0=集点赠礼, -1=无法识别, -2=沉眠地

    注: -2 是采集阶段的内部信号(保留 raw 数据区分信息)，清洗时会归为
        dice_points=0 + action="沉眠地"(与集点赠礼同属额外奖励类，供模拟器使用)
    """
    try:
        from PIL import Image
        import numpy as np

        img_data = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_data))
        img_array = np.array(img)

        # Step 1: OCR检查"集点赠礼"和"沉眠地"
        if ocr_details:
            for item in ocr_details:
                text = item.get("text", "")
                box = item.get("box", [])
                if "集点赠礼" in text and box:
                    item_cy = sum(p[1] for p in box) / 4
                    if abs(item_cy - name_cy) < name_h:
                        return 0
                if "沉眠地" in text and box:
                    item_cy = sum(p[1] for p in box) / 4
                    if abs(item_cy - name_cy) < name_h:
                        return -2

        # Step 2: 定向裁切区域（列锚点来自表头或分辨率归一化，见 detect_layout）
        # x: 列锚点 ± DICE_HALF_W*scale
        # y: name_cy - 0.5*name_h 到 name_cy + 1.5*name_h
        #    骰子在 name 下方约 +5~+26 像素，y 搜索范围 2*name_h ≈ 70 < 骰子间距 80
        lay = layout or {}
        scale = float(lay.get("scale") or (img_array.shape[1] / REF_WIDTH))
        dice_cx = lay.get("dice_cx")
        dice_cx = float(dice_cx) if dice_cx is not None else DICE_CX * scale
        dice_left = max(0, int(dice_cx - DICE_HALF_W * scale))
        dice_right = min(img_array.shape[1], int(dice_cx + DICE_HALF_W * scale))
        dice_top = max(0, int(name_cy - name_h * 0.5))
        dice_bot = min(img_array.shape[0], int(name_cy + name_h * 1.5))

        if dice_right <= dice_left or dice_bot <= dice_top:
            return -1

        crop = img_array[dice_top:dice_bot, dice_left:dice_right]
        h, w = crop.shape[:2]
        total = h * w

        r, g, b = crop[:,:,0].astype(int), crop[:,:,1].astype(int), crop[:,:,2].astype(int)

        # Step 3: 区分骰子底色和赠礼文字
        # 骰子底色(82,83,82): G通道略高(G-R>=0)
        # 赠礼文字(74,74,74): RGB几乎相等(|R-G|<5, |G-B|<5)
        is_dice_base = (np.abs(r - 82) < 15) & (np.abs(g - 83) < 15) & (np.abs(b - 82) < 15) & \
                       ((g - r) >= 0)
        is_gift_text = (np.abs(r - 74) < 15) & (np.abs(g - 74) < 15) & (np.abs(b - 74) < 15) & \
                       (np.abs(r - g) < 5) & (np.abs(g - b) < 5)

        dice_base_ratio = is_dice_base.sum() / total
        gift_text_ratio = is_gift_text.sum() / total

        # 赠礼文字显著且骰子底色少 → 集点赠礼
        if gift_text_ratio > 0.005 and dice_base_ratio < 0.05:
            return 0

        if dice_base_ratio < 0.02:
            return -1

        # Step 4: 找所有骰子底色连通域，用强约束筛选"封闭圆角方形"（v2 改进）
        visited = np.zeros((h, w), dtype=bool)
        components = []

        for y in range(h):
            for x in range(w):
                if is_dice_base[y, x] and not visited[y, x]:
                    queue = deque([(y, x)])
                    visited[y, x] = True
                    count = 0
                    min_y2, max_y2, min_x2, max_x2 = y, y, x, x
                    while queue:
                        cy2, cx2 = queue.popleft()
                        count += 1
                        min_y2 = min(min_y2, cy2)
                        max_y2 = max(max_y2, cy2)
                        min_x2 = min(min_x2, cx2)
                        max_x2 = max(max_x2, cx2)
                        for dy, dx in [(-1,0),(1,0),(0,-1),(0,1)]:
                            ny, nx = cy2+dy, cx2+dx
                            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and is_dice_base[ny, nx]:
                                visited[ny, nx] = True
                                queue.append((ny, nx))
                    components.append((count, min_y2, max_y2, min_x2, max_x2))

        if not components:
            return -1

        # 强约束筛选：骰子近似正方形，尺寸与 x 位置都按 scale 归一化
        expected_cx_rel = dice_cx - dice_left  # 骰子中心 x 相对 crop
        size_lo = DICE_SIZE_RANGE[0] * scale
        size_hi = DICE_SIZE_RANGE[1] * scale
        x_tol = DICE_X_TOL * scale
        candidates = []
        for comp in components:
            count, min_y2, max_y2, min_x2, max_x2 = comp
            bbox_h = max_y2 - min_y2 + 1
            bbox_w = max_x2 - min_x2 + 1
            fill_ratio = count / (bbox_h * bbox_w)
            aspect_ratio = bbox_w / bbox_h if bbox_h > 0 else 0

            # 尺寸约束（样本图 53x53 → 按 scale 缩放）
            if bbox_h < size_lo or bbox_h > size_hi:
                continue
            if bbox_w < size_lo or bbox_w > size_hi:
                continue
            # 宽高比约束：0.8-1.2（正方形）
            if aspect_ratio < 0.8 or aspect_ratio > 1.2:
                continue
            # 填充率约束：封闭圆角方形内部基本填满
            if fill_ratio < 0.5:
                continue
            # x 中心约束：必须贴近列锚点
            center_x = (min_x2 + max_x2) / 2
            if abs(center_x - expected_cx_rel) > x_tol:
                continue

            # y 偏移（相对 name_cy）
            center_y = (min_y2 + max_y2) / 2
            name_cy_rel = name_cy - dice_top
            y_offset = center_y - name_cy_rel

            candidates.append({
                "count": count, "bbox": (min_y2, max_y2, min_x2, max_x2),
                "y_offset": y_offset,
            })

        if not candidates:
            # 没找到符合条件的骰子方形
            if gift_text_ratio > 0.003:
                return 0
            return -1

        # Step 5: 选最佳候选——y 偏移最接近 name_h*0.5（骰子通常在 name 下方半个 name_h）
        expected_y_offset = name_h * 0.5
        candidates.sort(key=lambda c: abs(c["y_offset"] - expected_y_offset))
        best = candidates[0]

        min_y2, max_y2, min_x2, max_x2 = best["bbox"]

        # Step 6: 在骰子内部找亮色点
        inner_y1 = min_y2 + 2
        inner_y2 = max_y2 - 1
        inner_x1 = min_x2 + 2
        inner_x2 = max_x2 - 1

        if inner_y2 <= inner_y1 or inner_x2 <= inner_x1:
            return -1

        inner_crop = crop[inner_y1:inner_y2+1, inner_x1:inner_x2+1]
        ir, ig, ib = inner_crop[:,:,0].astype(int), inner_crop[:,:,1].astype(int), inner_crop[:,:,2].astype(int)

        is_dot = (np.abs(ir - 224) < 20) & (np.abs(ig - 224) < 20) & (np.abs(ib - 226) < 20)
        dot_px = is_dot.sum()

        ih, iw = inner_crop.shape[:2]

        if dot_px < 2:
            return -1

        # 连通域计数
        visited2 = np.zeros((ih, iw), dtype=bool)
        dot_sizes = []

        for y in range(ih):
            for x in range(iw):
                if is_dot[y, x] and not visited2[y, x]:
                    queue = deque([(y, x)])
                    visited2[y, x] = True
                    cnt = 0
                    while queue:
                        cy2, cx2 = queue.popleft()
                        cnt += 1
                        for dy, dx in [(-1,0),(1,0),(0,-1),(0,1)]:
                            ny, nx = cy2+dy, cx2+dx
                            if 0 <= ny < ih and 0 <= nx < iw and not visited2[ny, nx] and is_dot[ny, nx]:
                                visited2[ny, nx] = True
                                queue.append((ny, nx))
                    dot_sizes.append(cnt)

        # 过滤噪点：真实点数圆点≥30px，角落噪点<25px
        MIN_DOT_SIZE = 30
        real_dots = [s for s in dot_sizes if s >= MIN_DOT_SIZE]
        num_real = len(real_dots)

        if 1 <= num_real <= 6:
            return num_real
        else:
            return -1

    except Exception as e:
        print(f"  骰子检测异常: {e}")
        return -1


def check_layout_health(details, records):
    """首页布局自检：骰子列定位是否可靠。

    窗口尺寸/DPI 一变，定点裁切会整体错位。静默产出一屏 -1 比直接报错更糟，
    所以在第一页就把锚点来源和是否全军覆没说出来。
    """
    if not records:
        return
    layout = detect_layout(details)
    src = "表头锚点" if layout["dice_source"] == "header" else "分辨率归一化"
    anchor = layout.get("dice_cx")
    anchor_txt = f"x={anchor:.0f}" if anchor is not None else "x=按比例推算"
    dice_vals = [r.get("dice") for r in records]
    miss = sum(1 for d in dice_vals if d == -1)
    print(f"  布局自检: 骰子列={src} {anchor_txt}, 本页点数未识别 {miss}/{len(dice_vals)}")
    if len(dice_vals) >= 3 and miss == len(dice_vals):
        print("  [!] 本页投掷点数全部未识别——骰子列定位可能失效（窗口尺寸/DPI 变化？）")


def parse_page_records(details, image_b64=None):
    """解析当前页的抽卡记录（含颜色稀有度+骰子点数检测）"""
    # 列位置从本页表头锚点推导（窗口尺寸变化时无需改常量）
    layout = detect_layout(details)
    items = []
    for item in details:
        text = item.get("text", "").strip()
        box = item.get("box", [])
        if not text or not box:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = sum(xs) / 4
        cy = sum(ys) / 4
        items.append({"text": text, "cx": cx, "cy": cy, "box": box})

    # 按y坐标分组
    items.sort(key=lambda x: x["cy"])
    rows = []
    current_row = []
    current_y = -999

    for item in items:
        if abs(item["cy"] - current_y) > 25:
            if current_row:
                rows.append(current_row)
            current_row = [item]
            current_y = item["cy"]
        else:
            current_row.append(item)
    if current_row:
        rows.append(current_row)

    for row in rows:
        row.sort(key=lambda x: x["cx"])

    # 识别数据行
    name_prefixes = ["弧盘", "角色", "道具", "滑翔翼"]
    time_pattern = re.compile(r'\d{4}年\d{1,2}月\d{1,2}日')
    time_suffix_pattern = re.compile(r'\d{2}:\d{2}:\d{2}')

    records = []
    header_found = False

    for row in rows:
        row_text = " ".join(item["text"] for item in row)
        if "道具名称" in row_text or "获得时间" in row_text:
            header_found = True
            continue

        if not header_found:
            continue

        if any(t in row_text for t in ["上一页", "下一页"]):
            continue
        if re.match(r'^\d+/\d+$', row_text.strip()):
            continue

        has_name = any(
            prefix in item["text"]
            for item in row
            for prefix in name_prefixes
        )

        if not has_name:
            continue

        # 解析行
        row_texts = [item["text"] for item in row]
        full_text = " ".join(row_texts)

        # 提取道具名称
        name = ""
        name_box = None
        for item in row:
            for prefix in name_prefixes:
                if prefix in item["text"]:
                    idx = row.index(item)
                    name_parts = [item["text"]]
                    name_box = item["box"]
                    if "·" not in item["text"] and "「" not in item["text"]:
                        for j in range(idx + 1, len(row)):
                            next_text = row[j]["text"]
                            name_parts.append(next_text)
                            if "·" in next_text or "」" in next_text:
                                break
                    else:
                        if item["text"].endswith("。") or item["text"].endswith("「"):
                            for j in range(idx + 1, len(row)):
                                next_text = row[j]["text"]
                                if next_text in ["」", "」"] or len(next_text) <= 2:
                                    name_parts.append(next_text)
                                else:
                                    break

                    name = "".join(name_parts)
                    break
            if name:
                break

        # 清理名称
        name = name.replace('"', '·').replace(""", '·').replace(""", '·')
        if not re.match(r'^(弧盘|角色|道具|滑翔翼)·', name):
            for prefix in name_prefixes:
                if prefix in name and "·" not in name:
                    name = name.replace(prefix, prefix + "·", 1)
                    break

        # 提取时间
        date_part = ""
        time_part = ""
        for text in row_texts:
            m = time_pattern.search(text)
            if m:
                date_part = m.group()
            m2 = time_suffix_pattern.search(text)
            if m2:
                time_part = m2.group()
        time_str = f"{date_part} {time_part}".strip()

        # 提取数量：在行文本中找独立整数（非日期/时间部分）
        quantity = 1
        date_time_texts = set()
        for text in row_texts:
            if time_pattern.search(text) or time_suffix_pattern.search(text):
                date_time_texts.add(text)
            # "X月X日" 这种部分日期文本也排除
            if re.search(r'\d{1,2}月\d{1,2}日', text):
                date_time_texts.add(text)
        for text in row_texts:
            t = text.strip()
            if t in date_time_texts:
                continue
            # 独立整数（1-99），不是日期/时间的一部分
            if re.match(r'^\d{1,2}$', t):
                q = int(t)
                if q >= 1:
                    quantity = q
                    break

        # 如果名称末尾粘着数字（OCR合并），尝试提取
        # 先剥离名称中的时间信息，再提取数量
        # 例如 "道具·-迷迭棋子302026年5月7日13:56:47" → 先去时间 → "道具·-迷迭棋子30" → 提取30
        if quantity == 1:
            # 剥离名称中的时间信息
            name_no_time = re.sub(r'\d*\d{4}年\d{1,2}月\d{1,2}日.*$', '', name)
            name_no_time = re.sub(r'\d{2,4}\d?月\d{1,2}日.*$', '', name_no_time)
            # 从剥离时间后的名称末尾提取数字
            name_qty_match = re.search(r'^(.*[^\d])(\d{1,2})$', name_no_time)
            if name_qty_match:
                candidate_name = name_qty_match.group(1)
                candidate_qty = int(name_qty_match.group(2))
                # 确认名称去掉数字后仍是合法名称（以已知前缀开头）
                if any(candidate_name.startswith(p) for p in name_prefixes) and candidate_qty > 1:
                    name = candidate_name
                    quantity = candidate_qty

        # 检测稀有度
        rarity = "?"
        name_cy = 0
        name_h = 0
        if name_box and image_b64:
            rarity, _src = resolve_rarity(image_b64, name_box, name)
            note_rarity_evidence(image_b64, name_box, name, time_str, rarity)
            name_ys = [p[1] for p in name_box]
            name_cy = sum(name_ys) / 4
            name_h = max(name_ys) - min(name_ys)

        # 检测骰子点数（图像处理）
        dice = -1
        if image_b64 and name_cy and name_h:
            dice = detect_dice_points(image_b64, name_cy, name_h, details, layout)

        if name:
            records.append({
                "name": name,
                "quantity": quantity,
                "time": time_str,
                "rarity": rarity,
                "dice": dice,
            })

    return records


def weapon_column_bounds(details, image_b64=None):
    """武器池三列分界（名称 | 研募类型 | 时间）。

    优先用表头「弧盘列表 / 研募类型 / 研募时间」的中心算相邻中点；
    表头认不全时按 scale = 截图宽/REF_WIDTH 缩放参考值 (1100, 1600)。
    """
    scale = 1.0
    if image_b64:
        try:
            from PIL import Image
            scale = Image.open(io.BytesIO(base64.b64decode(image_b64))).size[0] / REF_WIDTH
        except Exception:
            scale = 1.0
    fallback = (1100 * scale, 1600 * scale)

    def cx_of(keys):
        for item in details or []:
            text = item.get("text", "")
            box = item.get("box") or []
            if any(k in text for k in keys) and box:
                xs = [p[0] for p in box]
                return sum(xs) / len(xs)
        return None

    name_cx = cx_of(("弧盘列表", "研募列表"))
    rt_cx = cx_of(("研募类型",))
    time_cx = cx_of(("研募时间",))
    if name_cx is None or rt_cx is None or time_cx is None:
        return fallback

    b1 = (name_cx + rt_cx) / 2
    b2 = (rt_cx + time_cx) / 2
    if b2 <= b1:
        return fallback
    return (b1, b2)


def parse_weapon_page_records(details, image_b64=None):
    """解析弧盘研募页面的记录（三列：弧盘列表/研募类型/研募时间）

    与角色池 parse_page_records 的区别：
    - 无骰子列、无数量列（每条记录 count=1, dice_points=-1）
    - 名称无"弧盘·"前缀（武器池全是弧盘，UI不显示前缀）
    - 新增 research_type 字段（研募类型，如"奇迹盒盒"）
    - 三列分界由表头锚点推导（见 weapon_column_bounds），不写死像素
    """
    col_name_max, col_rt_max = weapon_column_bounds(details, image_b64)
    items = []
    for item in details:
        text = item.get("text", "").strip()
        box = item.get("box", [])
        if not text or not box:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = sum(xs) / 4
        cy = sum(ys) / 4
        items.append({"text": text, "cx": cx, "cy": cy, "box": box})

    # 按y坐标分组（同角色池逻辑）
    items.sort(key=lambda x: x["cy"])
    rows = []
    current_row = []
    current_y = -999
    for item in items:
        if abs(item["cy"] - current_y) > 25:
            if current_row:
                rows.append(current_row)
            current_row = [item]
            current_y = item["cy"]
        else:
            current_row.append(item)
    if current_row:
        rows.append(current_row)

    for row in rows:
        row.sort(key=lambda x: x["cx"])

    # 识别数据行
    time_pattern = re.compile(r'\d{4}年\d{1,2}月\d{1,2}日')
    time_suffix_pattern = re.compile(r'\d{2}:\d{2}:\d{2}')

    # 武器池页面噪音文本（需跳过）
    skip_texts = {"上一页", "下一页", "研募详情", "研募列表", "规则说明",
                  "研募记录", "弧盘列表", "研募类型", "研募时间"}

    records = []
    header_found = False

    for row in rows:
        row_text = " ".join(item["text"] for item in row)
        # 表头行（弧盘列表/研募类型/研募时间）
        if "弧盘列表" in row_text or "研募类型" in row_text:
            header_found = True
            continue

        if not header_found:
            continue

        # 跳过导航/说明文本
        if any(t in row_text for t in ["上一页", "下一页", "研募详情", "研募列表",
                                        "规则说明", "研募记录", "可在本页面查询"]):
            continue
        # 跳过页码 "10/14"
        if re.match(r'^\d+/\d+$', row_text.strip()):
            continue

        # 按列提取（分界由 weapon_column_bounds 依表头推导）
        name_parts = []
        name_box = None
        research_type = ""
        date_part = ""
        time_part = ""

        for item in row:
            cx = item["cx"]
            text = item["text"]
            if cx < col_name_max:
                name_parts.append(text)
                if name_box is None:
                    name_box = item["box"]
            elif cx < col_rt_max:
                research_type = text
            else:
                m = time_pattern.search(text)
                if m:
                    date_part = m.group()
                m2 = time_suffix_pattern.search(text)
                if m2:
                    time_part = m2.group()

        name = "".join(name_parts)
        time_str = f"{date_part} {time_part}".strip()

        # 必须有名称和时间才算有效记录
        if not name or not date_part:
            continue

        # 检测稀有度（先查权威表，表里没有才用颜色判）
        rarity = "?"
        if name_box and image_b64:
            rarity, _src = resolve_rarity(image_b64, name_box, name, is_weapon=True)
            note_rarity_evidence(image_b64, name_box, name, time_str, rarity, is_weapon=True)

        records.append({
            "name": name,
            "quantity": 1,
            "time": time_str,
            "rarity": rarity,
            "dice": -1,  # 武器池无骰子
            "research_type": research_type,
        })

    return records


def get_current_page_info(details):
    """获取当前页码"""
    for item in details:
        text = item.get("text", "")
        m = re.search(r'(\d+)/(\d+)', text)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def switch_board_type(details, target_type, max_retries=5):
    """切换棋盘类型（含重试逻辑，多次点击+验证）"""
    for attempt in range(max_retries):
        # 重新获取当前状态（第2次起需要重新截图）
        if attempt > 0:
            print(f"  切换重试第{attempt}次...")
            time.sleep(2)
            _, details = capture_and_ocr()

        # 查找当前棋盘名称
        current_board = None
        for item in details:
            text = item.get("text", "")
            if text in ["限定棋盘", "常驻棋盘", "标准棋盘"]:
                current_board = text
                break

        if current_board == target_type:
            print(f"  已在目标棋盘: {target_type}")
            return True

        if not current_board:
            print("  未找到当前棋盘类型")
            continue

        # 点击当前棋盘名称打开下拉框（尝试多次点击）
        result = find_text_center(details, current_board, y_min=300, y_max=600)
        if not result:
            print("  未找到棋盘类型下拉框")
            continue

        cx, cy, _ = result
        print(f"  当前棋盘: {current_board}, 点击 ({cx:.0f},{cy:.0f})")
        # 多点几次确保打开下拉框
        for click_try in range(3):
            click_at(cx, cy, "打开下拉框", require_confirm=False)
            time.sleep(1.5)
            # 重新截图检查下拉框是否打开
            _, check_details = capture_and_ocr()
            # 检查是否出现了目标选项（说明下拉框已打开）
            for item in check_details:
                text = item.get("text", "")
                if text == target_type or (text in ["限定棋盘", "常驻棋盘", "标准棋盘"] and text != current_board):
                    # 下拉框已打开，跳出点击循环
                    break
            else:
                # 没找到其他选项，继续尝试点击
                continue
            break

        # 重新截图找选项
        _, new_details = capture_and_ocr()
        target = find_text_center(new_details, target_type)
        if target:
            cx, cy, _ = target
            print(f"  点击 {target_type} ({cx:.0f},{cy:.0f})")
            click_at(cx, cy, target_type, require_confirm=False)
            time.sleep(2.5)

            # 验证是否切换成功（多次验证）
            verified = False
            for verify_try in range(3):
                _, verify_details = capture_and_ocr()
                for item in verify_details:
                    text = item.get("text", "")
                    if text == target_type:
                        print(f"  切换成功: {target_type}")
                        verified = True
                        break
                if verified:
                    break
                print(f"  验证第{verify_try+1}次未检测到 {target_type}，等待重试...")
                time.sleep(1.5)

            if verified:
                return True
            print(f"  切换验证失败，未检测到 {target_type}")
        else:
            print(f"  未找到 {target_type} 选项")
            # 尝试按ESC或点击空白区域关闭下拉框
            click_at(500, 300, "关闭下拉框", require_confirm=False)
            time.sleep(1)

    print(f"  切换棋盘失败，已重试{max_retries}次")
    return False


def _print_record(r):
    """打印单条记录（紧凑格式）"""
    rarity = r.get("rarity", "?")
    dice = r.get("dice", -1)
    name = r.get("name", "?")
    qty = r.get("quantity", 1)
    t = r.get("time", "")
    # 点数显示: 0=赠礼, 1-6=点数, -1=???, -2=沉眠地
    if dice == 0:
        dice_str = "赠礼"
    elif dice == -2:
        dice_str = "沉眠地"
    elif 1 <= dice <= 6:
        dice_str = f" {dice}点"
    else:
        dice_str = " ???"
    print(f"  [{rarity}]{dice_str} {name} x{qty}  {t}")


def _print_weapon_record(r):
    """打印武器池单条记录（紧凑格式，无骰子）"""
    rarity = r.get("rarity", "?")
    name = r.get("name", "?")
    t = r.get("time", "")
    research_type = r.get("research_type", "")
    print(f"  [{rarity}] {name} ({research_type})  {t}")


def load_existing_database():
    """加载已有数据库，用于重复检测"""
    db_path = OUTPUT_DIR / "yihuan_gacha_database.json"
    if not db_path.exists():
        return {}
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_MATCH_NAME_NOISE = str.maketrans({
    '：': '·', '■': '·',
    '"': '', '\u201c': '', '\u201d': '', '\u201e': '', '\u201f': '', '\uff02': '',
})


def _norm_name_for_match(name):
    """判重用的名称归一化：只统一分隔符与引号噪音，再剥类型前缀。

    必须与 yihuan_clean.py 的 clean_name 前几步保持一致 —— 采集侧比对的是 OCR
    原名，库里存的是 clean_name 之后的名字。不归一化就会把「弧盘·：「电音」狂欢」
    和「「电音」狂欢」当成两条记录，白翻好几页。

    ⛔ 这里不许加模糊匹配：B 级重名极密，一放宽就是丢卡。
    """
    if not name:
        return ""
    name = name.translate(_MATCH_NAME_NOISE)
    name = name.replace('·-', '·')
    while '··' in name:      # 折叠连续分隔符（「弧盘·：x」-> 「弧盘··x」-> 「弧盘·x」）
        name = name.replace('··', '·')
    for prefix in ["弧盘", "角色", "道具", "滑翔翼"]:
        if name.startswith(prefix + "·"):
            return name[len(prefix) + 1:]
        if name.startswith(prefix):      # 少了分隔符的变体（「弧盘「x」」）
            return name[len(prefix):]
    return name


def record_signature(record):
    """内容指纹 = (纯名称, 稀有度, Unix 时间)；第二个返回值表示时间是否可用。

    一次十连的 10 条在游戏里共享同一个时间（精确到秒），同名同稀有度的记录
    （B 级重名极密）只能靠"出现次数"区分，所以库侧必须用多重集而不是集合
    —— 与鸣潮 wuwa_gacha.py 的 _record_key / Counter 合并是同一套做法。
    """
    name = _norm_name_for_match(record.get("name", ""))

    ts = 0
    raw_time = record.get("time", "")
    if isinstance(raw_time, (int, float)):
        ts = int(raw_time)
    elif raw_time:
        m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2}):(\d{2})', str(raw_time))
        if m:
            try:
                ts = int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                  int(m.group(4)), int(m.group(5)), int(m.group(6))).timestamp())
            except ValueError:
                ts = 0
    return (name, str(record.get("rarity") or "?"), ts), ts > 0


class PoolMatcher:
    """按内容多重集判断"这条记录库里有没有"。

    为什么不用时间水位线：同一次十连跨了两轮采集（采到一半断了），水位线会把
    同组里剩下的记录全判成"已采"而丢掉；多重集按出现次数比对，库里已有 6 条、
    本轮采到 10 条 ⇒ 差额 4 条照样收进来。

    时间解析不出来的记录一律算"新"：宁可多采一条，也不能把没采过的丢掉。
    """

    def __init__(self, records=None):
        self.remaining = Counter()
        for r in records or []:
            sig, _ = record_signature(r)
            self.remaining[sig] += 1

    def __bool__(self):
        return bool(self.remaining)

    def classify_page(self, records):
        """返回 (本页新记录列表, 本页已采条数)。

        判据：页内同内容已出现次数 < 库内该内容条数 ⇒ 这条已采。
        用「固定计数 + 页内累计」而不是全局消耗余额：翻页失败重读同一页时结果
        幂等，不会因为上一页消耗掉余额就把已采记录又算成新的。
        """
        seen = Counter()
        fresh = []
        for r in records:
            sig, ts_ok = record_signature(r)
            if ts_ok and seen[sig] < self.remaining[sig]:
                seen[sig] += 1
            else:
                fresh.append(r)
        return fresh, len(records) - len(fresh)


def db_latest_time(db_data, board_name):
    """库内该池最新记录的时间（仅用于日志展示，不再参与判重）。"""
    records = (db_data or {}).get(board_name) or []
    ts_list = []
    for r in records:
        t = r.get("time")
        if isinstance(t, (int, float)) and t > 0:
            ts_list.append(int(t))
        elif str(t).strip().isdigit():
            ts_list.append(int(str(t).strip()))
    return max(ts_list) if ts_list else 0


def _sync_retry_page(details, last_page, roi, max_retries=5, turn_delay=1.5):
    """流水线失配时的同步兜底：点一页、截图、OCR，直到页码真的前进。

    返回 (details, image, page)；彻底失败返回 (None, None, None)。
    """
    for retry in range(max_retries):
        res = find_text_center(details, "下一页")
        if not res:
            return None, None, None
        if retry > 0:
            time.sleep(1.5)
            # 页码不动最常见的原因就是前台被抢（弹窗/客户端面板盖上来），重试前先抢回
            ensure_foreground(f"翻页重试{retry}")
            _, details = capture_and_ocr()
            if not details:
                continue
            res = find_text_center(details, "下一页")
            if not res:
                return None, None, None
        if not click_at(res[0], res[1], "下一页", require_confirm=False):
            print(f"  翻页点击失败(重试{retry+1}/{max_retries})")
            time.sleep(2)
            continue
        time.sleep(turn_delay)
        image = capture_window()
        if not image:
            print("截图失败，等待重试...")
            time.sleep(2)
            continue
        _, fresh = ocr_roi(image, roi)
        if not fresh:
            continue
        page, _ = get_current_page_info(fresh)
        if page is None:
            print(f"  无法识别页码(重试{retry+1}/{max_retries})")
            continue
        if page <= last_page:
            print(f"  页码{page}未前进(重试{retry+1}/{max_retries})")
            time.sleep(1)
            continue
        return fresh, image, page
    return None, None, None


def iter_pages_pipelined(first_details, first_page, total, on_page, roi=None,
                         max_retries=5, turn_delay=1.5):
    """流水线翻页：上一张截图的 OCR 与「点下一页 + 等刷新 + 截图」重叠。

    点击用上一页 OCR 得到的按钮位置（同一面板跨页位置稳定），每页 OCR 回来会刷新；
    页码没前进就退回同步重试。翻页失败时已经截好的那张新图会先被用掉，不会丢页。

    on_page(details, image, page) -> bool：处理这一页，返回 False 表示停止。
    """
    from concurrent.futures import ThreadPoolExecutor

    def _prime(details, page):
        """点一次下一页并截好图，返回 (待 OCR 截图, 按钮位置)。"""
        if total and page and page >= total:
            return None, None
        nb = find_text_center(details, "下一页")
        if not nb:
            return None, None
        if not click_at(nb[0], nb[1], "下一页", require_confirm=False):
            return None, None
        time.sleep(turn_delay)
        return capture_window(), nb

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pending, next_btn = _prime(first_details, first_page)
        last_page = first_page or 0
        while pending and next_btn:
            fut = pool.submit(ocr_roi, pending, roi)

            # 与 OCR 并行：翻下一页并截图
            turned = click_at(next_btn[0], next_btn[1], "下一页", require_confirm=False)
            time.sleep(turn_delay)
            nxt_shot = capture_window() if turned else None

            _, details = fut.result()
            if not details:
                # OCR 失败不等于翻页失败：先把同一张图重识别一次，别把这一页整页丢掉
                print("  OCR 失败，重试同一张图")
                _, details = ocr_roi(pending, roi)
            page, _ = get_current_page_info(details) if details else (None, None)

            if page is not None and total and page >= total and page == last_page:
                print(f"已到达最后一页({total})")
                return

            cur_image = pending
            page_behind = page is None or page <= last_page
            primed_btn = None

            # pending 是重复页时，本轮已经重新点过并截了 nxt_shot，先看这张新图
            if page_behind and nxt_shot and turned:
                _, d2 = ocr_roi(nxt_shot, roi)
                p2, _ = get_current_page_info(d2) if d2 else (None, None)
                if d2 and p2 is not None and p2 > last_page:
                    details, cur_image, page = d2, nxt_shot, p2
                    page_behind = False
                    nxt_shot, primed_btn = _prime(details, page)

            if not details or page_behind or not turned:
                reason = "OCR 失败" if not details else f"页码{page}未前进"
                print(f"  {reason}，退回同步重试")
                details, cur_image, page = _sync_retry_page(
                    details or first_details, last_page, roi, max_retries, turn_delay)
                if not details:
                    print(f"翻页失败，已重试{max_retries}次，停止")
                    return
                nxt_shot, primed_btn = _prime(details, page)

            if on_page(details, cur_image, page) is False:
                return

            last_page = page if page is not None else last_page
            next_btn = primed_btn or find_text_center(details, "下一页")
            pending = nxt_shot
    finally:
        pool.shutdown(wait=False)


def collect_board(board_name, max_pages=None, existing_records=None):
    """收集指定棋盘类型的所有页面（含页码跟踪+增量判停+连续失败重试）

    existing_records: 库内该池已有记录，交给 PoolMatcher 按内容多重集判重；
    空 = 全量翻到最后一页。连续 2 页全部已在库即停止翻页。
    """
    matcher = PoolMatcher(existing_records)
    print(f"\n{'='*60}")
    print(f"开始收集: {board_name}")
    if matcher:
        latest = db_latest_time({board_name: existing_records}, board_name)
        print(f"增量模式: 库内已有 {sum(matcher.remaining.values())} 条"
              f"（最新 {datetime.fromtimestamp(latest):%Y-%m-%d %H:%M:%S}）"
              f"，连续 2 页全部已在库即停止翻页")
    else:
        print("全量模式: 库内无该池记录，翻到最后一页")
    print(f"{'='*60}")

    # 翻页全靠真实鼠标，先确认游戏真在前台（被客户端面板盖住时点击会全打空）
    ensure_foreground(f"{board_name}开始前")

    all_records = []
    seen_pages = set()  # 页码跟踪：防止重复读取同一页
    consecutive_failures = 0  # 连续失败重试计数器

    image, details = capture_and_ocr()
    if not image:
        return []

    # 验证当前棋盘类型是否匹配
    current_board = None
    for item in details:
        text = item.get("text", "")
        if text in ["限定棋盘", "常驻棋盘", "标准棋盘"]:
            current_board = text
            break
    if current_board and current_board != board_name:
        print(f"[!] 警告: 期望棋盘'{board_name}'，实际'{current_board}'，切换可能失败!")
        print(f"[!] 跳过收集以避免重复数据")
        return []

    page, total = get_current_page_info(details)
    print(f"当前页: {page}/{total}")

    # 切换棋盘后游戏自动将页面置1，如果不在第1页说明棋盘切换失败
    if page and page > 1:
        print(f"[!] 警告: 切换棋盘后不在第1页(当前第{page}页)，棋盘切换可能失败!")
        print(f"[!] 当前仍在之前的棋盘，跳过收集以避免重复数据")
        return []

    if max_pages and total:
        total = min(total, max_pages)

    if total is None:
        print("无法确定总页数，只读取当前页")
        total = 1

    # 读取当前页
    records = parse_page_records(details, image)

    # 增量判重：内容多重集（同名同稀有度靠出现次数区分，一次十连共享同一时间）
    stale_pages = 0
    fresh, skipped = matcher.classify_page(records)
    if records and not fresh:
        stale_pages = 1

    all_records.extend(fresh)
    if page:
        seen_pages.add(page)
    print(f"第{page}/{total}页: {len(records)}条"
          + (f"（其中 {skipped} 条已在库，只收 {len(fresh)} 条新记录）" if skipped else ""))
    if page == 1:
        check_layout_health(details, records)
    for r in fresh:
        _print_record(r)

    # 面板 ROI：按首帧内容范围裁掉窗口标题栏和两侧背景（实测省 ~16% OCR 时间）
    board_roi = None
    try:
        frame = _decode_frame(image)
        board_roi = learn_page_roi(details, frame.shape[1], frame.shape[0])
        if board_roi:
            x0, y0, x1, y1 = board_roi
            print(f"  面板 ROI: x={x0}..{x1} y={y0}..{y1}"
                  f"（占全图 {100*(x1-x0)*(y1-y0)/(frame.shape[1]*frame.shape[0]):.0f}%）")
        else:
            print("  面板 ROI: 不裁剪（内容几乎铺满全图）")
    except Exception as e:
        print(f"  [!] ROI 学习失败，用全图: {e}")

    # 翻页读取（流水线：上一页 OCR 与「点下一页 + 等刷新 + 截图」重叠）
    def _handle_page(page_details, page_image, cur_page):
        nonlocal stale_pages
        records = parse_page_records(page_details, page_image)
        fresh, skipped = matcher.classify_page(records)
        all_records.extend(fresh)
        if cur_page:
            seen_pages.add(cur_page)
        print(f"第{cur_page}/{total}页: {len(records)}条"
              + (f"（其中 {skipped} 条已在库，只收 {len(fresh)} 条新记录）" if skipped else ""))
        for r in fresh:
            _print_record(r)
        if records and not fresh:
            stale_pages += 1
        else:
            stale_pages = 0
        if stale_pages >= 2:
            print(f"[增量判停] 连续 {stale_pages} 页全部已在库中，停止翻页"
                  f"（已到第 {cur_page}/{total} 页）")
            return False
        return True

    iter_pages_pipelined(details, page, total, _handle_page, roi=board_roi)

    print(f"\n{board_name}汇总: 共 {len(all_records)} 条记录")
    return all_records


def _ocr_texts(details):
    return [(it.get("text") or "").strip() for it in (details or [])]


def _image_size(image_b64, default=(1938, 1127)):
    """窗口截图的像素尺寸；解不开就用本机实测默认值。"""
    try:
        with Image.open(io.BytesIO(base64.b64decode(image_b64))) as im:
            return im.size
    except Exception:
        return default


def is_weapon_records_page(details):
    """当前是不是 弧盘研募 → 研募记录 页（表头三列特征）。"""
    texts = _ocr_texts(details)
    return any("弧盘列表" in t for t in texts) and any("研募" in t for t in texts)


def _click_text_center(details, needle):
    res = find_text_center(details, needle)
    if not res:
        return False
    cx, cy, hit = res
    # 红线：抽卡动作按钮「研募」单独成词时绝不点（会消耗用户资源）
    if hit.strip() == "研募" or hit.strip().startswith("研募×"):
        print(f"  [!] 拒绝点击：{hit!r} 是消耗资源的研募按钮")
        return False
    print(f"  点击 {hit!r} @({cx:.0f},{cy:.0f})")
    return click_at(cx, cy, hit, require_confirm=False)


def navigate_to_weapon_records(max_steps=8):
    """自动走到 弧盘研募 → 研募记录 页，成功返回 True。

    路径（2026-10-30 用户口述 + 实测）：
    左下角「弧盘研募」→ 弧盘商店里「××特刊 限时开启」横幅 → 弹窗右上角书页图标
    → 详情页最右「研募记录」页签。

    红线：全程只点上面这几个锚点，绝不点弹窗里的「研募」按钮——那是真花钱。
    书页图标没有文字可锚，用窗口内相对坐标（本机实测 1938×1127 下 1750,332），
    点完必须核到「研募记录」页签才算成功，否则放弃、交回人工。
    """
    print("\n[导航] 自动前往 弧盘研募 → 研募记录 ...")
    for step in range(max_steps):
        image, details = capture_and_ocr()
        if not image:
            return False
        texts = _ocr_texts(details)
        if is_weapon_records_page(details):
            print("[导航] 已到达研募记录页")
            return True

        # 1) 已在研募详情：直接切到最右页签
        if "研募记录" in texts:
            print(f"[导航] 步骤{step+1}: 切换研募记录页签")
            if _click_text_center(details, "研募记录"):
                time.sleep(2.5)
                continue

        # 2) 特刊弹窗（有奇迹盒盒 + 研募按钮）：点右上角书页图标
        if any("奇迹盒盒" in t for t in texts) and any(t == "研募" for t in texts):
            w, h = _image_size(image)
            print(f"[导航] 步骤{step+1}: 在特刊弹窗，点右上角书页图标")
            click_at(int(w * 0.903), int(h * 0.295), "book_icon", require_confirm=False)
            time.sleep(3)
            continue

        # 3) 弧盘商店：点「××特刊」横幅
        banner = next((t for t in texts if "特刊" in t and "剩余" not in t), None)
        if banner and any("限时开启" in t for t in texts):
            print(f"[导航] 步骤{step+1}: 点击特刊横幅 {banner!r}")
            if _click_text_center(details, banner):
                time.sleep(3.5)
                continue

        # 4) 主界面：点左下角「弧盘研募」入口
        if "弧盘研募" in texts:
            print(f"[导航] 步骤{step+1}: 点击左下角弧盘研募入口")
            if _click_text_center(details, "弧盘研募"):
                time.sleep(6)  # 商城要加载
                continue

        # 5) 有面板挡着（棋盘详情/研募详情）：关掉再试
        if any(t in ("棋盘详情", "研募详情") for t in texts):
            w, h = _image_size(image)
            print(f"[导航] 步骤{step+1}: 关闭挡路的面板")
            click_at(int(w * 0.816), int(h * 0.208), "panel_close", require_confirm=False)
            time.sleep(2)
            continue

        print(f"[导航] 步骤{step+1}: 认不出当前界面（文字: {texts[:8]}），放弃自动导航")
        return False
    print("[导航] 步数用尽，仍未到达研募记录页")
    return False


def collect_weapon_pool(max_pages=None, existing_records=None):
    """收集弧盘研募池的所有页面（无棋盘切换/无骰子，普通抽卡）

    与 collect_board 的区别：
    - 无棋盘类型切换（武器池只有一个池，无需切换）
    - 无棋盘类型验证（不检查"限定棋盘"等文本）
    - 不要求从第1页开始（武器池不切换，页码可能任意）
    - 使用 parse_weapon_page_records 解析三列布局

    existing_records: 库内已有记录，交给 PoolMatcher 按内容多重集判重；
    连续 2 页全部已采就停止翻页（记录按时间倒序，再往后只会更旧）。
    """
    board_name = "弧盘研募"
    matcher = PoolMatcher(existing_records)
    print(f"\n{'='*60}")
    print(f"开始收集: {board_name}")
    if matcher:
        latest = db_latest_time({board_name: existing_records}, board_name)
        print(f"增量模式: 库内已有 {sum(matcher.remaining.values())} 条"
              f"（最新 {datetime.fromtimestamp(latest):%Y-%m-%d %H:%M:%S}）"
              f"，连续 2 页全部已在库即停止翻页")
    else:
        print("全量模式: 库内无该池记录，翻到最后一页")
    print(f"{'='*60}")

    ensure_foreground("弧盘研募开始前")

    all_records = []
    seen_pages = set()

    image, details = capture_and_ocr()
    if not image:
        return []

    # 验证当前页面是弧盘研募记录页（检查表头特征）；不在就自动导航过去
    is_weapon_page = any(
        "弧盘列表" in item.get("text", "") or "研募类型" in item.get("text", "")
        for item in details
    )
    if not is_weapon_page:
        print("[!] 当前页面不是弧盘研募记录页，尝试自动导航...")
        if not navigate_to_weapon_records():
            print("[!] 自动导航失败：请在游戏里手动打开 弧盘研募 → 研募记录 后重跑 --weapon")
            return []
        image, details = capture_and_ocr()
        if not image:
            return []

    page, total = get_current_page_info(details)
    print(f"当前页: {page}/{total}")

    if max_pages and total:
        total = min(total, max_pages)

    if total is None:
        print("无法确定总页数，只读取当前页")
        total = 1

    # 读取当前页
    records = parse_weapon_page_records(details, image)

    # 增量判重：内容多重集。旧版这里"第1页有2条重复就 return [] 整池跳过"，
    # 一是只判第1页，二是判中就把整池丢弃 —— 抽过新卡以后第1页必有新记录，
    # 于是永远判不中，每次都退化成全量。
    stale_pages = 0
    fresh, skipped = matcher.classify_page(records)
    if records and not fresh:
        stale_pages = 1

    all_records.extend(fresh)
    if page:
        seen_pages.add(page)
    print(f"第{page}/{total}页: {len(records)}条"
          + (f"（其中 {skipped} 条已在库，只收 {len(fresh)} 条新记录）" if skipped else ""))
    for r in fresh:
        _print_weapon_record(r)

    # 面板 ROI：按首帧内容范围裁掉标题栏和两侧背景。
    # 2026-10-30 加：全图反复 OCR 让 Paddle 在跑到第 7 页时崩了（RuntimeError: Unknown exception
    # → 后端进程直接没了），裁到 ~40% 面积后单帧像素量大幅下降，也更抗内存压力。
    weapon_roi = None
    try:
        frame = _decode_frame(image)
        weapon_roi = learn_page_roi(details, frame.shape[1], frame.shape[0])
        if weapon_roi:
            x0, y0, x1, y1 = weapon_roi
            print(f"  面板 ROI: x={x0}..{x1} y={y0}..{y1}"
                  f"（占全图 {100*(x1-x0)*(y1-y0)/(frame.shape[1]*frame.shape[0]):.0f}%）")
        else:
            print("  面板 ROI: 不裁剪（内容几乎铺满全图）")
    except Exception as e:
        print(f"  [!] ROI 学习失败，用全图: {e}")

    # 翻页读取（流水线：上一页 OCR 与「点下一页 + 等刷新 + 截图」重叠）
    partial_path = OUTPUT_DIR / "raw" / "_partial_weapon.json"

    def _handle_page(page_details, page_image, cur_page):
        nonlocal stale_pages
        records = parse_weapon_page_records(page_details, page_image)
        fresh, skipped = matcher.classify_page(records)
        all_records.extend(fresh)
        if cur_page:
            seen_pages.add(cur_page)
        print(f"第{cur_page}/{total}页: {len(records)}条"
              + (f"（其中 {skipped} 条已在库，只收 {len(fresh)} 条新记录）" if skipped else ""))
        for r in fresh:
            _print_weapon_record(r)
        # 增量落盘：OCR 崩过一次（后端整进程没了），已采的页不能跟着一起丢
        try:
            partial_path.parent.mkdir(parents=True, exist_ok=True)
            with open(partial_path, "w", encoding="utf-8") as f:
                json.dump({board_name: all_records}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  [!] 增量落盘失败: {e}")
        # 整页全是库里已有的旧记录 ⇒ 再往后只会更旧，连续 2 页就收工
        if records and not fresh:
            stale_pages += 1
        else:
            stale_pages = 0
        if stale_pages >= 2:
            print(f"[增量判停] 连续 {stale_pages} 页全部已在库中，停止翻页"
                  f"（已到第 {cur_page}/{total} 页）")
            return False
        return True

    iter_pages_pipelined(details, page, total, _handle_page, roi=weapon_roi)

    print(f"\n{board_name}汇总: 共 {len(all_records)} 条记录")
    try:
        partial_path.unlink(missing_ok=True)
    except Exception:
        pass
    return all_records


def print_summary(result):
    """打印汇总统计"""
    print("\n" + "=" * 70)
    print("异环抽卡记录汇总")
    print("=" * 70)

    for board_type, records in result.items():
        print(f"\n{'='*50}")
        print(f"【{board_type}】共 {len(records)} 条记录")
        print(f"{'='*50}")

        is_weapon = (board_type == "弧盘研募")

        by_rarity = {"S": [], "A": [], "B": [], "?": []}
        for r in records:
            rarity = r.get("rarity", "?")
            if rarity not in by_rarity:
                by_rarity[rarity] = []
            by_rarity[rarity].append(r)

        for rarity in ["S", "A", "B", "?"]:
            items = by_rarity.get(rarity, [])
            if not items:
                continue
            print(f"\n  --- {rarity}级 ({len(items)}条) ---")
            for i, r in enumerate(items, 1):
                name = r.get("name", "?")
                t = r.get("time", "?")
                if is_weapon:
                    research_type = r.get("research_type", "")
                    print(f"  {i:3d}. {name} ({research_type})  {t}")
                else:
                    dice = r.get("dice", -1)
                    qty = r.get("quantity", 1)
                    if dice == 0:
                        dice_str = "[赠礼]"
                    elif dice == -2:
                        dice_str = "[沉眠地]"
                    elif 1 <= dice <= 6:
                        dice_str = f"[{dice}点]"
                    else:
                        dice_str = "[???]"
                    print(f"  {i:3d}. {dice_str:6s} {name} x{qty}  {t}")

        s_count = len(by_rarity.get("S", []))
        a_count = len(by_rarity.get("A", []))
        b_count = len(by_rarity.get("B", []))

        print(f"\n  统计: S级{s_count} | A级{a_count} | B级{b_count}")

        if is_weapon:
            # 武器池：显示研募类型分布 + 额外奖励统计
            research_types = {}
            for r in records:
                rt = r.get("research_type", "")
                research_types[rt] = research_types.get(rt, 0) + 1
            rt_str = " | ".join(f"{k}:{v}" for k, v in research_types.items() if k)
            if rt_str:
                print(f"  研募类型: {rt_str}")
            # 额外奖励：S=40失纬棋子, A=4失纬, B=20迷迭棋子
            shiwei = s_count * 40 + a_count * 4
            midie = b_count * 20
            print(f"  额外奖励: 失纬棋子{shiwei} | 迷迭棋子{midie}")
        else:
            # 角色池：骰子统计
            gift_count = sum(1 for r in records if r.get("dice") == 0)
            dice_count = sum(1 for r in records if isinstance(r.get("dice"), int) and 1 <= r.get("dice") <= 6)
            chenmian_count = sum(1 for r in records if r.get("dice") == -2)
            unknown_dice = sum(1 for r in records if r.get("dice", -1) == -1)
            dice_dist = {}
            for r in records:
                d = r.get("dice", -1)
                if isinstance(d, int) and 1 <= d <= 6:
                    dice_dist[d] = dice_dist.get(d, 0) + 1
            dice_dist_str = " ".join(f"{k}点:{v}" for k, v in sorted(dice_dist.items()))
            parts = [f"掷骰: {dice_count}"]
            if chenmian_count:
                parts.append(f"沉眠地: {chenmian_count}")
            parts.append(f"集点赠礼: {gift_count}")
            parts.append(f"未识别: {unknown_dice}")
            print(f"  {' | '.join(parts)}")
            if dice_dist_str:
                print(f"  点数分布: {dice_dist_str}")

        print(f"  总抽数: {len(records)}")


if __name__ == "__main__":
    # 行缓冲输出：后台运行（RunCommand/终端重定向）时 print 立即可见，不再被块缓冲吞掉
    sys.stdout.reconfigure(line_buffering=True)
    weapon_mode = "--weapon" in sys.argv
    force_mode = "--force" in sys.argv

    print("异环抽卡记录收集器 v5")
    reset_window_bbox_cache()  # 窗口位置可能变过，重新取一次偏移
    if DEBUG:
        print("  [调试模式] 每步打印截图/OCR 耗时（YIYUAN_DEBUG=1）")
    if weapon_mode:
        print("  [武器池模式] 仅采集弧盘研募（请先在游戏中打开 弧盘研募→研募记录 页面）")
    if force_mode:
        print("  [强制模式] 跳过增量判重，全量翻到最后一页")

    # 检查后端
    resp = requests.get(f"{API}/health")
    health = resp.json()
    admin = health.get("screen", {}).get("admin_privileges", False)
    print(f"后端状态: {health.get('status')}, 管理员权限: {admin}")

    if not admin:
        print("警告: 没有管理员权限，键鼠操作可能失败！")

    # 采集前申请屏幕控制授权（后端重启后授权会丢失；弹窗会等你批准一次）
    if not ensure_authorization():
        print("无法获得屏幕控制授权，退出。请在弹窗中批准后重试。")
        sys.exit(1)

    # 加载已有数据库（--force 跳过）：判重靠内容多重集，不再依赖库里的时间绝对值
    db_data = load_existing_database()
    existing = {}
    if db_data and not force_mode:
        total_existing = sum(len(v) for v in db_data.values())
        print(f"已有数据库: {total_existing} 条记录（{', '.join(f'{k}:{len(v)}' for k, v in db_data.items())}）")
        for board, records in db_data.items():
            existing[board] = records
            latest = db_latest_time(db_data, board)
            if latest:
                print(f"  {board}: {len(records)} 条，最新 {datetime.fromtimestamp(latest):%Y-%m-%d %H:%M:%S}")
            else:
                print(f"  {board}: {len(records)} 条（缺时间字段，仍可按内容判重）")
    else:
        print("无已有数据库（或强制模式），全量采集")

    # 显示覆盖层
    requests.post(f"{API}/screen/overlay", json={"action": "show", "message": "Agent正在读取抽卡记录..."})

    result = {}

    if weapon_mode:
        # 武器池模式：仅采集弧盘研募（无棋盘切换/无骰子）
        result["弧盘研募"] = collect_weapon_pool(existing_records=existing.get("弧盘研募"))
    else:
        # 角色池模式：采集限定棋盘 + 标准棋盘（现有逻辑）
        # 限定棋盘（先确认当前棋盘类型，必要时切换）
        _, init_details = capture_and_ocr()
        current_board = None
        if init_details:
            for item in init_details:
                text = item.get("text", "")
                if text in ["限定棋盘", "常驻棋盘", "标准棋盘"]:
                    current_board = text
                    break

        if current_board == "限定棋盘":
            result["限定棋盘"] = collect_board("限定棋盘", existing_records=existing.get("限定棋盘"))
        elif current_board and init_details:
            print(f"当前在{current_board}，切换到限定棋盘...")
            if switch_board_type(init_details, "限定棋盘"):
                time.sleep(2)
                result["限定棋盘"] = collect_board("限定棋盘", existing_records=existing.get("限定棋盘"))
            else:
                print("切换到限定棋盘失败，跳过")
                result["限定棋盘"] = []
        else:
            print("无法确定当前棋盘类型，尝试直接收集限定棋盘")
            result["限定棋盘"] = collect_board("限定棋盘", existing_records=existing.get("限定棋盘"))

        # 切换到标准棋盘
        _, details = capture_and_ocr()
        if details and switch_board_type(details, "标准棋盘"):
            time.sleep(2)
            result["标准棋盘"] = collect_board("标准棋盘", existing_records=existing.get("标准棋盘"))
        else:
            result["标准棋盘"] = []

    # 隐藏覆盖层
    requests.post(f"{API}/screen/overlay", json={"action": "hide"})

    # === 第一步：保存原始数据到 raw/（永不删除；清洗成功后由 yihuan_clean.py 归档到 backup/raw/） ===
    total_new = sum(len(v) for v in result.values())
    raw_dir = OUTPUT_DIR / "raw"
    if total_new:
        raw_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_path = raw_dir / f"yihuan_gacha_raw_{timestamp}.json"
        n = 1
        while raw_path.exists():  # 同一秒跑两次不覆盖原始件
            raw_path = raw_dir / f"yihuan_gacha_raw_{timestamp}_{n}.json"
            n += 1
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n原始数据已保存到 {raw_path}（{total_new} 条新记录）")
        print("  下一步跑 yihuan_clean.py 清洗；清洗成功后原始件会自动归档到 backup/raw/")
    else:
        print("\n本轮没有新记录，不生成 raw 文件")

    # === 第二步：保存工作文件（单次覆盖） ===
    output_path = OUTPUT_DIR / "yihuan_gacha_summary.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"工作文件已保存: {output_path}")

    # 打印汇总
    print_summary(result)
