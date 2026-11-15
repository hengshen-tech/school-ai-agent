"""Material storage and Agent output contracts; document meaning is decided by the Agent."""
import json
import shutil
from pathlib import Path

import pymupdf

from . import imaging, store

ROLES = {"auto", "question_paper", "answer_key", "student_work"}
ROLE_LABELS = {"question_paper": "试卷", "answer_key": "标准答案", "student_work": "学生作答"}
MAX_FILES = 20
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_PAGES = 40
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


async def read_uploads(uploads, file_roles):
    if not uploads or len(uploads) > MAX_FILES:
        raise ValueError(f"请选择 1–{MAX_FILES} 份材料")
    try:
        roles = json.loads(file_roles) if file_roles else ["auto"] * len(uploads)
    except ValueError:
        raise ValueError("材料用途格式无效") from None
    if not isinstance(roles, list) or len(roles) != len(uploads) or any(not isinstance(r, str) or r not in ROLES for r in roles):
        raise ValueError("请为每份材料指定有效用途")
    loaded, total = [], 0
    for upload, role in zip(uploads, roles):
        filename = Path(upload.filename or "").name
        if Path(filename).suffix.lower() not in ALLOWED:
            raise ValueError(f"{filename}：只支持 PDF / PNG / JPG / WEBP")
        data = await upload.read(MAX_FILE_BYTES + 1)
        total += len(data)
        if not data:
            raise ValueError(f"{filename}：文件为空")
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("单个文件不能超过 40MB，全部材料合计不能超过 200MB")
        loaded.append({"filename": filename, "role": role, "data": data})
    return loaded


def bundle_hash(loaded, modern=True):
    if not modern and len(loaded) == 1 and loaded[0]["role"] == "auto":
        return store.sha256_bytes(loaded[0]["data"])
    manifest = [{"filename": x["filename"], "role": x["role"], "hash": store.sha256_bytes(x["data"])} for x in loaded]
    return store.sha256_bytes(json.dumps({"version": 2, "inputs": manifest}, ensure_ascii=False, sort_keys=True).encode())


def save_inputs(folder, loaded):
    inputs = []
    for i, item in enumerate(loaded, 1):
        target = folder / (f"original{Path(item['filename']).suffix.lower()}" if len(loaded) == 1 else
                           f"input-{i}{Path(item['filename']).suffix.lower()}")
        target.write_bytes(item["data"])
        inputs.append({"original": str(target), "filename": item["filename"], "role": item["role"]})
    return inputs


def copy_inputs(source_folder, target_folder, meta):
    inputs = meta.get("inputs")
    if not inputs:
        source = next(iter(source_folder.glob("original.*")), None)
        if not source:
            raise ValueError("原件不存在")
        inputs = [{"original": str(source), "filename": meta.get("filename") or source.name, "role": "auto"}]
    copied = []
    for item in inputs:
        source = Path(item["original"])
        if not source.is_file() or source.parent.resolve() != source_folder.resolve():
            raise ValueError(f"材料原件不存在：{item['filename']}")
        target = target_folder / source.name
        shutil.copyfile(source, target)
        copied.append({**item, "original": str(target)})
    return copied


def to_pages(inputs, folder):
    # 只读取页数，整组预检通过后才开始栅格化，避免小体积超长 PDF 先占满转换资源。
    total = 0
    for item in inputs:
        source = Path(item["original"])
        if source.suffix.lower() == ".pdf":
            with pymupdf.open(source) as document:
                total += document.page_count
        else:
            total += 1
        if total > MAX_PAGES:
            raise ValueError(f"同一次批改最多支持 {MAX_PAGES} 页材料，请分批添加")
    pages, manifest = [], []
    for index, item in enumerate(inputs, 1):
        # PDF 转换在本文件的临时目录内完成，再编号到统一的页序列。
        source_dir = folder / "source-pages" / str(index)
        try:
            converted = imaging.to_pages(Path(item["original"]), source_dir)
            for source_page, path in enumerate(converted, 1):
                target = folder / "pages" / f"page-{len(pages) + 1}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
                pages.append(target)
                manifest.append({"page": len(pages), "filename": item["filename"], "source_page": source_page,
                                 "input_index": index, "requested_role": item.get("role", "auto")})
        finally:
            shutil.rmtree(source_dir, ignore_errors=True)
    return pages, manifest


def resolve_bundle(data, manifest, manual_key=False):
    """Validate the Agent's page selection without inferring roles or matching question IDs."""
    roles = data.get("materials")
    if not isinstance(roles, list) or len(roles) != len(manifest):
        raise ValueError("Agent 未完整识别每页材料用途，请在文件列表中指定用途后重试")
    by_page = {}
    for item in roles:
        if not isinstance(item, dict) or type(item.get("page")) is not int or not 1 <= item["page"] <= len(manifest) or item["page"] in by_page:
            raise ValueError("Agent 返回了无效或重复的材料页码，请重试")
        by_page[item["page"]] = item
    materials = []
    for source in manifest:
        item = by_page[source["page"]]
        requested = source["requested_role"]
        role = requested if requested != "auto" else item.get("role")
        try:
            confidence = float(item.get("confidence", 0))
        except (ValueError, TypeError):
            confidence = 0
        if role not in ROLE_LABELS or (requested == "auto" and not .7 <= confidence <= 1):
            raise ValueError(f"无法确认「{source['filename']}」第 {source['source_page']} 页用途，请在文件列表中指定用途后重试")
        materials.append({**source, "role": role, "confidence": confidence})
    work_pages = [m["page"] for m in materials if m["role"] == "student_work"]
    if not work_pages:
        raise ValueError("没有识别到学生作答材料，请添加学生作答图片或指定用途")
    qs = data.get("questions")
    if not isinstance(qs, list) or not qs:
        raise ValueError("没有在学生作答材料中识别到可批改的题目")
    ids = set()
    for q in qs:
        if not isinstance(q, dict) or type(q.get("page")) is not int or q["page"] not in work_pages:
            raise ValueError("Agent 返回的学生答案页码不属于学生作答材料，请确认文件用途后重试")
        ident = str(q.get("id") or "").strip()
        if not ident or ident in ids:
            raise ValueError("Agent 未区分重复题号，请重试；同题跨页作答需要合并为一道题")
        ids.add(ident)
        if not str(q.get("question") or "").strip():
            raise ValueError(f"第 {ident} 题缺少题干，请补充试卷材料后重试")
    if data.get("multiple_students") is True:
        raise ValueError("这组材料包含多位学生，请将每位学生的作答分别开始批改")
    refs = data.get("reference_answers") or []
    key_pages = any(m["role"] == "answer_key" for m in materials)
    if not isinstance(refs, list):
        raise ValueError("Agent 未正确提取标准答案，请重试")
    if refs and not key_pages:
        raise ValueError("Agent 将非标准答案材料用作参考答案，请确认文件用途后重试")
    if key_pages and not refs and not manual_key:
        raise ValueError("标准答案材料未提取到答案，请上传清晰版本或手动填写标准答案")
    seen = set()
    for answer in refs:
        if not isinstance(answer, dict) or not str(answer.get("id") or "").strip() or not str(answer.get("answer") or "").strip():
            raise ValueError("标准答案存在缺失或无法辨认的内容，请补充清晰材料")
        if answer.get("confident") is False or str(answer["id"]) in seen:
            raise ValueError("标准答案存在歧义或冲突，请核对材料后重试")
        seen.add(str(answer["id"]))
    return materials, work_pages, refs
