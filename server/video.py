"""视频取帧路由：从课程 packet 按秒解码画面，直接以 MCP ImageContent 返回。

不落盘：解码、拼图、JPEG 编码全部在内存完成。leaf agent 需要回看视频时用它，
`workspace/course_digest/seek.py` 只作为后端/盘不可用时的离线兜底。

依赖 PyAV（可选 inference 组），缺失时端点返回 503 而不是让后端起不来。
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, HTTPException
from pydantic import Field

from lib.schema import BaseSchema

router = APIRouter(prefix="/video", tags=["Video"])

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = (PROJECT_ROOT / "workspace" / "course_digest" / "out").resolve()
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv"}
MAX_FRAMES = 12
MAX_SPAN = 60.0
MAX_SHEET_WIDTH = 2400
_CACHE_MAX = 48
_CACHE: OrderedDict[tuple[str, int, int, int], tuple[bytes, int, int]] = OrderedDict()
_CACHE_LOCK = Lock()


class VideoFrameRequest(BaseSchema):
    packet: str = Field(..., description="packet 目录（必须在 course_digest 输出根下）")
    at: float | None = Field(default=None, description="单帧时刻（秒）")
    times: list[float] = Field(default_factory=list, description="多帧时刻（秒），优先于 at")
    span: float = Field(default=0.0, description="以 at 为中心前后取多少秒")
    step: float = Field(default=2.0, description="span 模式下相邻帧间隔（秒）")
    width: int = Field(default=1600, description="目标宽度，源更小则不放大")


class VideoFrameResponse(BaseSchema):
    ok: bool
    packet: str
    video: str
    times: list[str]
    frames: int
    width: int
    height: int
    sheet: bool
    mcp_image_block: bool
    image: str
    mime_type: str
    elapsed_ms: int


def _hms(t: float) -> str:
    s = int(max(0.0, t))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _reroot(video: Path) -> Path:
    """课程库盘符会随重启变化：按 课程库/ 后缀在当前已挂载盘上重定位。"""
    if video.exists() or "课程库" not in video.parts:
        return video
    tail = Path(*video.parts[video.parts.index("课程库") + 1:])
    drives = getattr(os, "listdrives", None)
    roots = drives() if drives else [f"{c}:/" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    for root in roots:
        cand = Path(root) / "课程库" / tail
        if cand.exists():
            return cand
    return video


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_roots() -> list[Path]:
    """course_digest 的输出根：项目内默认 + COURSE_DIGEST_OUT + 各盘 课程库/_course_digest。

    产物会随用户要求落在课程库所在的盘上，盘符又随开机变化，所以按目录名重建根列表，
    而不是只认项目内的 out。
    """
    roots = [OUT_ROOT]
    env = os.environ.get("COURSE_DIGEST_OUT", "").strip()
    if env:
        roots.append(Path(env).expanduser().resolve())
    drives = getattr(os, "listdrives", None)
    for drive in (drives() if drives else []):
        cand = Path(drive) / "课程库" / "_course_digest"
        if cand.is_dir():
            roots.append(cand.resolve())
    return roots


def _packet_dir(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()
    roots = _allowed_roots()
    if not any(_is_under(p, r) for r in roots):
        raise HTTPException(status_code=400,
                            detail="packet 必须在 course_digest 输出根（OUT_ROOT）下，"
                                   f"拒绝任意路径读取: {p}")
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"packet 不存在: {p}")
    if not (p / "meta.json").is_file():
        raise HTTPException(status_code=404, detail=f"packet 缺 meta.json: {p}")
    return p


def _video_of(pkt: Path) -> tuple[Path, float]:
    try:
        meta = json.loads((pkt / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"meta.json 不可读: {e}") from e
    raw = str(meta.get("video") or "")
    if not raw:
        raise HTTPException(status_code=400, detail="meta.json 缺 video 字段")
    video = _reroot(Path(raw))
    if video.suffix.lower() not in VIDEO_EXT:
        raise HTTPException(status_code=400, detail=f"不是支持的视频扩展名: {video.suffix}")
    if not video.is_file():
        raise HTTPException(status_code=404,
                            detail=f"视频不可读（盘未挂载或路径失效）: {video}")
    return video, float(meta.get("duration_sec") or 0)


def _times_of(req: VideoFrameRequest, duration: float) -> list[float]:
    if req.times:
        times = [float(t) for t in req.times]
    elif req.at is not None:
        if req.span > 0:
            if req.span > MAX_SPAN:
                raise HTTPException(status_code=400, detail=f"span 最大 {MAX_SPAN:.0f}s")
            times = []
            t = max(0.0, req.at - req.span)
            while t <= min(duration, req.at + req.span) + 1e-6:
                times.append(round(t, 1))
                t += max(0.2, float(req.step))
        else:
            times = [float(req.at)]
    else:
        raise HTTPException(status_code=400, detail="需要 at 或 times")
    if not times:
        raise HTTPException(status_code=400, detail="没有取帧时刻")
    if len(times) > MAX_FRAMES:
        raise HTTPException(status_code=400, detail=f"一次最多 {MAX_FRAMES} 帧，收到 {len(times)}")
    if any((t < 0 or t > duration + 1) for t in times):
        raise HTTPException(status_code=400, detail=f"时刻必须在 0..{duration:.0f}s")
    return [round(t, 1) for t in times]


def _cache_get(key: tuple[str, int, int, int]) -> tuple[bytes, int, int] | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
        return hit


def _cache_put(key: tuple[str, int, int, int], val: tuple[bytes, int, int]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = val
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def _decode(video: Path, times: list[float], width: int) -> list[tuple[bytes, int, int]]:
    try:
        import av
    except ImportError as e:
        raise HTTPException(status_code=503,
                            detail="后端未安装 PyAV（av），无法内存解码视频；请改用 seek.py") from e
    mtime = video.stat().st_mtime_ns
    frames: list[tuple[bytes, int, int] | None] = []
    miss: list[int] = []
    for i, t in enumerate(times):
        hit = _cache_get((str(video), mtime, int(round(t * 10)), width))
        frames.append(hit)
        if hit is None:
            miss.append(i)

    if miss:
        try:
            container = av.open(str(video), metadata_errors="ignore")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"视频无法打开/解码: {e}") from e
        try:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                raise HTTPException(status_code=400, detail="文件里没有视频流")
            tb = stream.time_base
            if tb is None:
                raise HTTPException(status_code=400, detail="视频流缺 time_base")
            src_w = int(getattr(stream, "width", 0) or width)
            src_h = int(getattr(stream, "height", 0) or 0)
            w = min(int(width), src_w) if src_w else int(width)
            h = round(src_h * w / src_w) if src_w and src_h else 0
            for i in miss:
                t = times[i]
                container.seek(int(max(0.0, t) / tb), stream=stream)
                frame = next((x for x in container.decode(stream)
                              if isinstance(x, av.VideoFrame)), None)
                if frame is None:
                    raise HTTPException(status_code=400, detail=f"{t:.1f}s 处没有画面")
                img = (frame.reformat(width=w, height=h) if h
                       else frame.reformat(width=w)).to_image()
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=90)
                val = (buf.getvalue(), img.width, img.height)
                frames[i] = val
                _cache_put((str(video), mtime, int(round(t * 10)), width), val)
        finally:
            container.close()

    out = [f for f in frames if f is not None]
    if len(out) != len(times):
        raise HTTPException(status_code=500, detail="取帧数量不匹配")
    return out


def _sheet(frames: list[tuple[bytes, int, int]], times: list[float], width: int) -> tuple[bytes, int, int]:
    from PIL import Image, ImageDraw, ImageFont

    n = len(frames)
    cols = 2 if n == 2 else min(3, max(1, math.ceil(math.sqrt(n))))
    rows = math.ceil(n / cols)
    tile_w = min(width, max(480, MAX_SHEET_WIDTH // cols))
    gap = 8
    label_h = 30
    imgs = []
    for raw, _w, _h in frames:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.width != tile_w:
            im = im.resize((tile_w, max(1, int(im.height * tile_w / im.width))),
                           Image.Resampling.LANCZOS)
        imgs.append(im)
    tile_h = max(im.height for im in imgs) + label_h
    sheet_w = cols * tile_w + (cols + 1) * gap
    sheet_h = rows * tile_h + (rows + 1) * gap
    canvas = Image.new("RGB", (sheet_w, sheet_h), (28, 28, 30))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=20)
    except TypeError:
        font = ImageFont.load_default()
    for i, (im, t) in enumerate(zip(imgs, times, strict=False)):
        r, c = divmod(i, cols)
        x = gap + c * (tile_w + gap)
        y = gap + r * (tile_h + gap)
        draw.rectangle([x, y, x + tile_w, y + label_h - 1], fill=(0, 0, 0))
        draw.text((x + 8, y + 4), f"{i + 1}/{n}  {_hms(t)}", fill=(255, 255, 255), font=font)
        canvas.paste(im, (x, y + label_h))
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), canvas.width, canvas.height


@router.post("/frame", response_model=VideoFrameResponse, operation_id="video_frame")
def video_frame(req: VideoFrameRequest):
    """按秒取课程视频画面，内存编码后作为 MCP ImageContent 返回（不落盘）。

    - packet 必须位于 course_digest 输出根下（项目 out / COURSE_DIGEST_OUT / 课程库/_course_digest）；
      视频路径取自 packet 的 meta.json
    - 单帧返回原图；多帧拼成一张联络表（烧入 序号/时间）
    - 响应含 `mcp_image_block=true` + base64 image，MCP 网关会转成 ImageContent，
      摘要文本里不留 base64
    """
    t0 = time.perf_counter()
    pkt = _packet_dir(req.packet)
    video, duration = _video_of(pkt)
    times = _times_of(req, duration)
    width = max(320, min(int(req.width), 3840))

    if len(times) == 1:
        raw, w, h = _decode(video, times, width)[0]
        sheet = False
    else:
        cols = 2 if len(times) == 2 else min(3, max(1, math.ceil(math.sqrt(len(times)))))
        tile_w = min(width, max(480, MAX_SHEET_WIDTH // cols))
        raw, w, h = _sheet(_decode(video, times, tile_w), times, tile_w)
        sheet = True

    return VideoFrameResponse(
        ok=True, packet=pkt.name, video=str(video),
        times=[_hms(t) for t in times], frames=len(times),
        width=w, height=h, sheet=sheet,
        mcp_image_block=True,
        image=base64.b64encode(raw).decode("ascii"),
        mime_type="image/jpeg",
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )
