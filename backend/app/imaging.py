"""图片工具：PDF 转图、送模型前压缩、局部裁剪、绘制对错号。全部是确定性代码，不经过模型。"""
import base64
import io
import math
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import MODEL_IMAGE_MAX_SIDE

RED = (214, 48, 39)
YELLOW = (240, 180, 20)


def _density(image):
    value = image.info.get('dpi', (200, 200))
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return (200, 200)
    return tuple(v if type(v) in (int, float) and math.isfinite(v) and v > 0 else 200 for v in value[:2])


def to_pages(src: Path, out_dir: Path) -> list[Path]:
    """PDF / JPG / PNG → 逐页 PNG（按 EXIF 方向矫正，保留原始分辨率）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    if src.suffix.lower() == ".pdf":
        doc = pymupdf.open(src)
        for i, page in enumerate(doc, 1):
            pix = page.get_pixmap(dpi=200, alpha=False)
            p = out_dir / f"page-{i}.png"
            pix.save(p)
            pages.append(p)
    else:
        original = Image.open(src)
        density = _density(original)
        img = ImageOps.exif_transpose(original).convert("RGB")
        p = out_dir / "page-1.png"
        img.save(p, dpi=density)
        pages.append(p)
    return pages


def _b64_jpeg(img: Image.Image, quality=88) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def jpeg_bytes(path: Path, max_side: int = 3200) -> bytes:
    """整页转 JPEG（用于上传 OCR；坐标是归一化的，缩放不影响定位）。"""
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def image_block(img: Image.Image) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64_jpeg(img)}}


def page_block(path: Path) -> dict:
    img = Image.open(path)
    img.thumbnail((MODEL_IMAGE_MAX_SIDE, MODEL_IMAGE_MAX_SIDE))
    return image_block(img)


def canon_bbox(bbox):
    """把模型输出的边框规整为 0–1000 归一化的 [x1, y1, x2, y2]；非法则返回 None。"""
    try:
        if isinstance(bbox, list) and len(bbox) == 1 and isinstance(bbox[0], list):
            bbox = bbox[0]  # 容错：[[x1,y1,x2,y2]]
        x1, y1, x2, y2 = [float(v) for v in bbox]
    except Exception:
        return None
    if max(x1, y1, x2, y2) <= 1.0:  # 容错：模型给了 0–1
        x1, y1, x2, y2 = x1 * 1000, y1 * 1000, x2 * 1000, y2 * 1000
    if max(x1, y1, x2, y2) > 1000 or min(x1, y1, x2, y2) < 0:
        return None  # 超出范围说明模型给的多半是像素坐标，不能硬截断成一个错误的位置
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)]


def norm_to_px(bbox, w: int, h: int):
    """0–1000 归一化坐标 → 原图像素坐标。非法则返回 None。"""
    b = canon_bbox(bbox)
    if not b:
        return None
    return [round(b[0] / 1000 * w), round(b[1] / 1000 * h), round(b[2] / 1000 * w), round(b[3] / 1000 * h)]


def crop(path: Path, box, pad=0.6, min_side=640, outline=True) -> Image.Image:
    """裁出答题区域（带上下文边距并放大），可选用细框标出模型声称的位置。"""
    img = Image.open(path).convert("RGB")
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    px, py = max(bw * pad, 40), max(bh * pad, 40)
    cx1, cy1 = max(0, int(x1 - px)), max(0, int(y1 - py))
    cx2, cy2 = min(img.width, int(x2 + px)), min(img.height, int(y2 + py))
    c = img.crop((cx1, cy1, cx2, cy2))
    if outline:
        ImageDraw.Draw(c).rectangle((x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1), outline=(40, 110, 220), width=2)
    scale = min_side / max(c.size)
    if scale > 1:
        c = c.resize((round(c.width * min(scale, 4)), round(c.height * min(scale, 4))), Image.LANCZOS)
    return c


def _mark(draw: ImageDraw.ImageDraw, kind: str, x: int, y: int, s: int):
    w = max(3, s // 9)
    def stroke(points, color):
        draw.line(points, fill='white', width=w + 1, joint='curve')
        draw.line(points, fill=color, width=w, joint='curve')
    if kind in ("correct", "partial"):
        stroke([(x + 0.08 * s, y + 0.55 * s), (x + 0.38 * s, y + 0.86 * s), (x + 0.98 * s, y + 0.08 * s)], RED)
        if kind == "partial":  # 半对：对号上加一撇
            stroke([(x + 0.50 * s, y + 0.30 * s), (x + 0.86 * s, y + 0.60 * s)], RED)
    elif kind == "wrong":
        stroke([(x + 0.1 * s, y + 0.1 * s), (x + 0.9 * s, y + 0.9 * s)], RED)
        stroke([(x + 0.9 * s, y + 0.1 * s), (x + 0.1 * s, y + 0.9 * s)], RED)
    else:  # review
        draw.ellipse((x, y, x + s, y + s), outline=YELLOW, width=max(2, w // 2))
        try:
            font = ImageFont.load_default(size=int(s * 0.7))
        except TypeError:
            font = ImageFont.load_default()
        draw.text((x + s / 2, y + s / 2), "?", fill=YELLOW, font=font, anchor="mm")


def annotate(page_path: Path, out_path: Path, items: list[dict]):
    """位置确认后优先在答案右侧落笔，右侧无空间则画在答案区域上。"""
    img = Image.open(page_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    short = min(img.size)
    gap = max(2, round(short * .004))
    occupied = [it["bbox_px"] for it in items if it.get("bbox_px")]
    marks = []

    def intersects(a, b):
        return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])

    for it in items:
        box = it.get("bbox_px")
        if not box or not it.get("draw", True):
            continue
        x1, y1, x2, y2 = box
        s = max(8, round(min(max((y2 - y1) * .8, short * .018), short * .035)))
        cy = min(max(0, (y1 + y2 - s) // 2), img.height - s)
        cx = min(max(0, (x1 + x2 - s) // 2), img.width - s)
        x, y = x2 + gap, cy
        area = [x, y, x + s, y + s]
        if x < 0 or y < 0 or x + s > img.width or y + s > img.height or any(
            intersects(area, other) for other in occupied + marks
        ):
            x, y = cx, cy
        _mark(draw, it["status"], int(x), int(y), s)
        marks.append([x, y, x + s, y + s])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, dpi=_density(img))


def pages_to_pdf(paths: list[Path]) -> bytes:
    """Losslessly embed page images at their original density and page order."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    if not paths:
        raise ValueError('没有可导出的答卷页')
    buf = io.BytesIO()
    document = canvas.Canvas(buf)
    for path in paths:
        with Image.open(path) as original:
            xdpi, ydpi = _density(original)
            width, height = original.width * 72 / xdpi, original.height * 72 / ydpi
            document.setPageSize((width, height))
            document.drawImage(ImageReader(original.convert('RGB')), 0, 0, width=width, height=height)
            document.showPage()
    document.save()
    return buf.getvalue()
