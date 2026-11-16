"""DeepSeek OCR 接口（POST /v1/tools/ocr）的封装。

只有当用户配置的是 DeepSeek 官方接口时才可用：同一个 Key 即可调用，按页计费。
逐页识别的结果里带有文字块及其 0–1000 归一化坐标，与本项目使用的坐标系一致，
新工作流先识别全部上传材料，主 Agent 与子 Agent 共享文字块，批改时关联答案块，程序校验后绘图。
模块同时保留历史记录的文字块查询与定位辅助函数。
"""
import json
import math
import os
import re
from urllib.parse import urlparse

import httpx

TAG = re.compile(r"<[|｜]ref[|｜]>(.*?)<[|｜]/ref[|｜]>\s*<[|｜]box[|｜]>\s*(\[\[.*?\]\])\s*<[|｜]/box[|｜]>", re.S)
ANY_TAG = re.compile(r"<[|｜]/?(?:ref|box|grounding)[|｜]>")
LAYOUT_LABELS = {"text", "title", "sub_title", "subtitle", "image", "figure", "table", "table_caption",
                 "image_caption", "formula", "equation", "header", "footer", "list", "caption"}


class OcrError(RuntimeError):
    pass


def endpoint(cfg: dict) -> str | None:
    """根据用户填写的接口地址判断是不是 DeepSeek；是则返回 OCR 接口地址。"""
    override = os.environ.get("DEEPSEEK_OCR_URL")  # 测试或走代理时手动指定
    if override:
        return override
    try:
        u = urlparse(cfg.get("base_url") or "")
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme == "https" and (host == "deepseek.com" or host.endswith(".deepseek.com")):
        return f"https://{u.netloc}/v1/tools/ocr"
    return None


def available(cfg: dict) -> bool:
    return bool(endpoint(cfg) and cfg.get("api_key"))


def enabled(cfg: dict) -> bool:
    return bool(cfg.get("use_ocr", True)) and available(cfg)


async def recognize(cfg: dict, data: bytes, filename: str, purpose: str = "rich_layout_recognition") -> dict:
    """调用 OCR。返回接口的原始 JSON（pages[].text 为每页的识别结果）。"""
    url = endpoint(cfg)
    if not url:
        raise OcrError("当前配置的不是 DeepSeek 接口，OCR 不可用")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(190, connect=15)) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {cfg['api_key']}"},
                                  data={"purpose": purpose}, files={"file": (filename, data)})
    except httpx.HTTPError as e:
        raise OcrError(f"OCR 请求失败：{type(e).__name__}")
    body = r.text.strip()  # 处理期间服务端会先发换行保活
    try:
        obj = json.loads(body) if body else {}
    except ValueError:
        obj = {}
    if r.status_code != 200 or "error" in obj:
        msg = (obj.get("error") or {}).get("message") if isinstance(obj.get("error"), dict) else None
        raise OcrError(f"OCR 失败（HTTP {r.status_code}）：{msg or body[:120] or '无返回内容'}")
    if not isinstance(obj.get("pages"), list):
        raise OcrError("OCR 返回格式异常")
    return obj


def valid_box(box):
    """OCR coordinates are already 0–1000; never infer a different scale."""
    if not isinstance(box, list) or len(box) != 4 or not all(
        type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1000 for v in box
    ):
        return None
    x1, x2 = sorted((box[0], box[2]))
    y1, y2 = sorted((box[1], box[3]))
    return [x1, y1, x2, y2] if x2 - x1 >= 2 and y2 - y1 >= 2 else None


def parse_blocks(text: str) -> list[dict]:
    """把一页的原始输出拆成带页内编号 b1、b2 的文字块（坐标 0–1000）。

    标记形如 <｜ref｜>…<｜/ref｜><｜box｜>[[x1,y1,x2,y2]]<｜/box｜>，其后跟着该区域的文字；
    ref 里有时是版面类型（text / title / image…），有时就是文字本身，两种都兼容。
    """
    blocks = []
    ms = list(TAG.finditer(text or ""))
    for i, m in enumerate(ms):
        ref = m.group(1).strip()
        tail = ANY_TAG.sub("", text[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(text)]).strip()
        is_label = ref in LAYOUT_LABELS  # 版面类型标签是小写英文；其他情况 ref 就是文字本身
        content = tail or ("" if is_label else ref)
        try:
            boxes = json.loads(m.group(2))
        except ValueError:
            continue
        for b in boxes:
            box = valid_box(b)
            if box:
                blocks.append({"id": f"b{len(blocks) + 1}", "label": ref if is_label else "text", "text": content, "box": box})
    return blocks


def answer_blocks(ids, blocks):
    """Resolve only actual, text-bearing blocks; reject an entire partial selection."""
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        return []
    by_id = {b.get("id"): b for b in blocks}
    selected = []
    for ident in dict.fromkeys(ids):
        block = by_id.get(ident)
        if not block or not block.get("text", "").strip() or not valid_box(block.get("box")):
            return []
        selected.append(block)
    # Keep the OCR engine's reading order. Sorting by top coordinate would
    # reverse adjacent handwriting fragments with slightly different heights.
    selected_ids = {b["id"] for b in selected}
    return [b for b in blocks if b.get("id") in selected_ids]


def block_text(blocks):
    return "\n".join(b["text"] for b in blocks)


def plain_text(obj: dict) -> str:
    """去掉定位标记，得到纯文字（用于读取老师上传的答案文件）。"""
    out = []
    for p in obj.get("pages", []):
        t = TAG.sub(lambda m: "" if m.group(1).strip() in LAYOUT_LABELS else m.group(1) + "\n", p.get("text") or "")
        out.append(ANY_TAG.sub("", t).strip())
    return "\n\n".join(x for x in out if x)


def _inter(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def blocks_in(blocks: list[dict], box, min_cover: float = 0.0) -> list[dict]:
    """与 box 相交的文字块；min_cover 为文字块自身被 box 覆盖的最小比例。"""
    out = []
    for b in blocks:
        area = max(1.0, (b["box"][2] - b["box"][0]) * (b["box"][3] - b["box"][1]))
        cover = _inter(b["box"], box) / area
        if cover > min_cover:
            out.append({**b, "cover": round(cover, 2)})
    return out
