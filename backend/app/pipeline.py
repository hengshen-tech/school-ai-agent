"""批改流水线：解析 → 标准答案 → 判分 → OCR 坐标检查 / 视觉复核 → 标注。"""
import asyncio
import json
import re
import threading
import time
import weakref
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool
from PIL import Image

from . import imaging, materials, ocr, prompts, store
from .agents import AgentError, run_json_agent
from .config import MODEL_EFFORT

# 同卷共享正在生成的答案；全局锁只保护查库与登记，不覆盖模型调用。
_PAPER_STATES = weakref.WeakKeyDictionary()
REVIEW_CHUNK = 12
GRADE_CHUNK = 40
ANNOTATION_VERSION = 2
_ANNOTATION_LOCK = threading.Lock()
_QUESTION_NUMBER = re.compile(r"^\s*(\d+)\s*(?:[.．](?!\d)|[、)）])\s*")


async def _parallel(coroutines):
    tasks = [asyncio.create_task(c) for c in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _page_block(cfg, path):
    cache = cfg.setdefault("_page_blocks", {})
    key = str(path)
    if key not in cache:
        cache[key] = await asyncio.to_thread(imaging.page_block, path)
    return cache[key]


def _f(v, default=0.5) -> float:
    """模型给的数字不一定规范（"0.9"、"高"、None），转换失败时用默认值而不是让整份试卷失败。"""
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return default


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def summarize(questions: list[dict]) -> dict:
    c = {k: sum(1 for q in questions if q["status"] == k) for k in ("correct", "wrong", "partial", "review")}
    judged = c["correct"] + c["wrong"] + c["partial"]
    return {"total": len(questions), **c,
            "accuracy": round((c["correct"] + 0.5 * c["partial"]) / judged, 4) if judged else None}


def render_annotations(sid: str, result: dict):
    d = store.sub_dir(sid)
    for i in range(1, result["page_count"] + 1):
        items = [q for q in result["questions"] if q["page"] == i]
        imaging.annotate(d / "pages" / f"page-{i}.png", d / "annotated" / f"page-{i}.png", items)


# ---------------- Step 0.5：DeepSeek OCR（可选） ----------------
async def _ocr_page(cfg, i, path, emit):
    emit("ocr", "info", f"第 {i} 页 OCR 处理中")
    try:
        data = await asyncio.to_thread(imaging.jpeg_bytes, path)
        obj = await ocr.recognize(cfg, data, f"page-{i}.jpg")
        text = "\n".join(pg.get("text") or "" for pg in obj["pages"])
        blocks = ocr.parse_blocks(text)
        emit("ocr", "info", f"第 {i} 页：{len(blocks)} 个文字块")
        if text:
            emit("ocr", "output", kind="output", label=f"第 {i} 页", call_id=f"ocr-{i}", text=text)
        return {"blocks": blocks, "raw": text}
    except Exception as e:
        emit("ocr", "info", f"第 {i} 页 OCR 未成功，使用模型直接识图（{str(e)[:100]}）")
        return {"blocks": [], "raw": ""}


def _ocr_hint(blocks: list[dict]) -> str:
    rows = [json.dumps({"id": b["id"], "box": b["box"], "text": b["text"]}, ensure_ascii=False)
            for b in blocks if b.get("text")]
    return "OCR 识别结果（编号、坐标、文字）：\n" + ("\n".join(rows) or "无可用文字块")


def _question_blocks(question, blocks):
    selected = ocr.answer_blocks(question.get("question_block_ids") or question.get("ocr_question_block_ids"), blocks)
    if selected:
        return selected
    if imaging.canon_bbox(question.get("question_bbox") or question.get("question_bbox_norm")):
        return []  # 已有明确题目框时，不用旧记录的启发式匹配覆盖它。
    # 兼容旧记录：只按本页明确匹配的题干找位置，不把相似的另一道题当作本题。
    def stem_text(value):
        value = _QUESTION_NUMBER.sub("", str(value or ""))
        value = store.norm_answer(value)
        value = re.sub(r"^(?:请计算|计算|口算|算一算)[:：]?", "", value)
        value = re.sub(r"(?:写出计算结果|写出得数)$", "", value)
        return value.rstrip("?？")

    text = stem_text(question.get("question"))
    matches = []
    for block in blocks:
        printed_number = _QUESTION_NUMBER.match(block.get("text", ""))
        qid = str(question.get("id", "")).split("#", 1)[0]
        if printed_number and qid.isdigit() and int(printed_number[1]) != int(qid):
            continue
        stem = stem_text(block.get("text"))
        # 完整匹配，不截取子算式或维护符号白名单；所有未知数学符号也必须一致。
        if len(text) >= 4 and stem == text:
            matches.append(block)
    return ocr.answer_blocks([matches[0].get("id")], blocks) if len(matches) == 1 else []


def _ocr_position(ids, blocks, student_answer, state, question=None, force_question=False):
    selected = ocr.answer_blocks(ids, blocks)
    question = question or {}
    result = {"position_source": "ocr_blocks", "ocr_block_ids": [b["id"] for b in selected],
              "bbox_norm": None, "bbox_ok": False}
    anchor = "answer"
    if not selected or force_question:
        selected = _question_blocks(question, blocks)
        anchor = "question"
    if not selected:
        # OCR 未识别题干时，允许解析阶段提供题目框；仍不估算学生答案位置。
        box = imaging.canon_bbox(question.get("question_bbox") or question.get("question_bbox_norm"))
        if box:
            result.update(bbox_norm=box, bbox_ok=True, annotation_anchor="question",
                          question_position_source="parser", question_bbox_norm=box)
        else:
            result["position_note"] = "未能定位答案或题目，请人工确认位置"
        return result
    boxes = [b["box"] for b in selected]
    result.update(bbox_norm=[min(b[0] for b in boxes), min(b[1] for b in boxes),
                             max(b[2] for b in boxes), max(b[3] for b in boxes)], bbox_ok=True,
                  annotation_anchor=anchor)
    if anchor == "question":
        result.update(ocr_question_block_ids=[b["id"] for b in selected], question_position_source="ocr")
    return result


def _resolve_ocr_positions(questions, ocr_pages, pages):
    owners = {}
    for q in questions:
        for ident in set(q.get("ocr_block_ids") or []):
            owners.setdefault((q["page"], ident), []).append(q)
    shared = {id(q) for group in owners.values() if len(group) > 1 for q in group}
    sizes = {}
    for q in questions:
        page = q["page"]
        if page not in sizes:
            with Image.open(pages[page - 1]) as image:
                sizes[page] = image.size
        blocks = (ocr_pages.get(page) or {}).get("blocks") or []
        position = _ocr_position(q.get("ocr_block_ids"), blocks, q.get("student_answer"),
                                 q.get("answer_state"), q, force_question=id(q) in shared)
        # 已有的有效 OCR 框不依赖 OCR 附件始终存在。
        if not blocks and q.get("position_source") == "ocr_blocks" and q.get("bbox_ok") and q.get("bbox_norm") and id(q) not in shared:
            position.update(bbox_norm=q["bbox_norm"], bbox_ok=True,
                            annotation_anchor=q.get("annotation_anchor", "answer"))
            position.pop("position_note", None)
        q.pop("position_note", None)
        q.update(position)
        q["bbox_px"] = imaging.norm_to_px(q["bbox_norm"], *sizes[page]) if q["bbox_norm"] else None


def ensure_ocr_annotations(sid, result):
    """旧 OCR 结果打开时补绘标记，不重新 OCR、解题或调用模型。"""
    if (result.get("ocr_mode") != "deepseek_ocr" and result.get("position_method") != "ocr_blocks") or result.get("annotation_version", 0) >= ANNOTATION_VERSION:
        return result
    with _ANNOTATION_LOCK:
        result = store.get_result(sid) or result
        if result.get("annotation_version", 0) >= ANNOTATION_VERSION:
            return result
        d = store.sub_dir(sid)
        pages = [d / "pages" / f"page-{i}.png" for i in range(1, result.get("page_count", 0) + 1)]
        if not pages or not all(p.exists() for p in pages):
            return result
        raw = store._read(d / "ocr.json", {})
        ocr_pages = {int(k): v for k, v in raw.items()}
        for page in ocr_pages.values():
            # 最早的 OCR 工件只有文字和坐标，补上页内编号后即可复用题干位置。
            page["blocks"] = [{**b, "id": b.get("id") or f"b{i}"}
                              for i, b in enumerate(page.get("blocks") or [], 1)]
        questions = result.get("questions", [])
        _resolve_ocr_positions(questions, ocr_pages, pages)
        for q in questions:
            q["reasons"] = [r for r in q.get("reasons", []) if not r.startswith(
                ("OCR 在答案位置读到", "复核未完成", "复核辨认结果不一致"))]
            q.pop("ocr_confirmed", None)
            if not q.get("overridden"):
                q["status"] = "review" if q["reasons"] or not q.get("verdict") else q["verdict"]
            q["draw"] = bool(q.get("bbox_px")) and bool(q.get("bbox_ok"))
        render_annotations(sid, result)
        result.update(annotation_version=ANNOTATION_VERSION, summary=summarize(questions))
        store.save_result(sid, result)
        return result


# ---------------- Step 1：解析 ----------------
async def _bundle_layouts(cfg, pages, emit, ocr_pages):
    manifest = cfg["_material_manifest"]
    if ocr.enabled(cfg):
        semaphore = asyncio.Semaphore(3)
        async def read(i, path):
            async with semaphore:
                ocr_pages[i] = await _ocr_page(cfg, i, path, emit)
        await _parallel(read(i, path) for i, path in enumerate(pages, 1) if i not in ocr_pages)
        count = sum(len(v["blocks"]) for v in ocr_pages.values())
        emit("ocr", "done" if count else "skip", f"已识别全部材料，共 {count} 个文字块")
    blocks = [_text("这一组文件属于同一次批改。全局页码与文件来源：\n" + json.dumps(manifest, ensure_ascii=False))]
    ocr_positions = cfg.get("ocr_mode") == "deepseek_ocr"
    for source, path in zip(manifest, pages):
        blocks.extend([_text(f"全局第 {source['page']} 页；文件「{source['filename']}」第 {source['source_page']} 页；"
                             f"用户指定用途：{source['requested_role']}"), await _page_block(cfg, path)])
        if ocr_positions:
            blocks.append(_text(_ocr_hint((ocr_pages.get(source["page"]) or {}).get("blocks", []))))
    data = await run_json_agent(cfg, system=prompts.PARSER + (prompts.OCR_PARSE_HINT if ocr_positions else "") +
                               prompts.MATERIAL_BUNDLE_HINT, blocks=blocks, emit=emit, stage="parse", label="整组材料")
    try:
        metadata, work_pages, refs = materials.resolve_bundle(data, manifest, bool(cfg.get("_manual_teacher_key")))
    except ValueError as e:
        raise AgentError(str(e)) from e
    cfg["_materials"], cfg["_graded_pages"], cfg["_reference_answers"] = metadata, work_pages, refs
    cfg["_solver_pages"] = [pages[m["page"] - 1] for m in metadata if m["role"] == "question_paper"] or [pages[i - 1] for i in work_pages]
    for item in metadata:
        emit("parse", "info", f"{item['filename']} · 第 {item['source_page']} 页 → {materials.ROLE_LABELS[item['role']]}")
    return [{"title": data.get("title"), "student_name": data.get("student_name") if i in work_pages else "",
             "student_no": data.get("student_no") if i in work_pages else "",
             "questions": [q for q in data["questions"] if q["page"] == i]}
            for i in range(1, len(pages) + 1)]


async def parse_pages(cfg, pages: list[Path], emit, ocr_pages: dict) -> tuple[str, list[dict], dict]:
    sem, ocr_sem = asyncio.Semaphore(3), asyncio.Semaphore(3)
    ocr_completed = 0
    ocr_positions = cfg.get("ocr_mode", "deepseek_ocr" if ocr.enabled(cfg) else "vision") == "deepseek_ocr"

    async def one(i, p):
        nonlocal ocr_completed
        if ocr.enabled(cfg):
            if i not in ocr_pages:
                async with ocr_sem:
                    ocr_pages[i] = await _ocr_page(cfg, i, p, emit)
            ocr_completed += 1
            if ocr_completed == len(pages):
                n = sum(len(v["blocks"]) for v in ocr_pages.values())
                emit("ocr", "done" if n else "skip", f"识别到 {n} 个文字块" if n else "OCR 未返回可用坐标，使用模型直接识图")
        async with sem:
            blocks = (ocr_pages.get(i) or {}).get("blocks") or []
            extra = [_text(_ocr_hint(blocks))] if ocr_positions else []
            data = await run_json_agent(cfg, system=prompts.PARSER + (prompts.OCR_PARSE_HINT if ocr_positions else ""),
                                        emit=emit, stage="parse", label=f"第 {i} 页",
                                        blocks=[await _page_block(cfg, p), *extra,
                                                _text(f"这是试卷的第 {i} 页，请按要求输出 JSON。")])
            emit("parse", "info", f"第 {i} 页：识别到 {len(data.get('questions', []))} 道题")
            return data

    results = await _bundle_layouts(cfg, pages, emit, ocr_pages) if cfg.get("_material_bundle") else await _parallel(
        one(i, p) for i, p in enumerate(pages, 1))
    title, questions, seen, who = "", [], {}, {"name": "", "no": ""}
    for i, (p, data) in enumerate(zip(pages, results), 1):
        title = title or (data.get("title") or "").strip()
        who["name"] = who["name"] or str(data.get("student_name") or "").strip()
        who["no"] = who["no"] or str(data.get("student_no") or "").strip()
        with Image.open(p) as image:
            w, h = image.size
        qs = data.get("questions")
        for q in qs if isinstance(qs, list) else []:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id") or len(questions) + 1).strip()
            seen[qid] = seen.get(qid, 0) + 1
            if seen[qid] > 1:
                qid = f"{qid}#{seen[qid]}"
            state = q.get("answer_state")
            if state not in ("answered", "blank", "unreadable"):
                state = "answered" if str(q.get("student_answer") or "").strip() else "blank"
            if state == "answered" and not str(q.get("student_answer") or "").strip():
                state = "unreadable"  # 说有作答却没抄出内容 → 不能当成空白直接判错
            position = _ocr_position(q.get("answer_block_ids"), (ocr_pages.get(i) or {}).get("blocks", []),
                                     str(q.get("student_answer") or "").strip(), state, q) if ocr_positions else {
                "position_source": "visual_review", "bbox_norm": imaging.canon_bbox(q.get("answer_bbox"))}
            bbox = position["bbox_norm"]
            questions.append({
                "id": qid, "page": i, "type": q.get("type") or "blank",
                "question": (q.get("question") or "").strip(),
                "student_answer": str(q.get("student_answer") or "").strip(),
                "answer_state": state,
                "bbox_norm": bbox,
                "bbox_px": imaging.norm_to_px(bbox, w, h) if bbox else None,
                "parse_confidence": _f(q.get("confidence")),
                "ocr_question_block_ids": q.get("question_block_ids") or [],
                "question_bbox_norm": imaging.canon_bbox(q.get("question_bbox")),
                **position,
            })
    if ocr_positions:
        _resolve_ocr_positions(questions, ocr_pages, pages)
    return title, questions, who


# ---------------- Step 2：标准答案（命中试卷库则跳过） ----------------
async def solve(cfg, title, questions, pages, teacher_key: str, emit=None) -> list[dict]:
    qlist = [{"id": q["id"], "type": q["type"], "question": q["question"]} for q in questions]
    blocks = [_text(f"试卷：{title or '未命名'}\n题目列表：\n{json.dumps(qlist, ensure_ascii=False)}")]
    if teacher_key.strip():
        blocks.append(_text(f"老师提供的标准答案（以此为准）：\n{teacher_key.strip()}"))
    blocks += [await _page_block(cfg, p) for p in cfg.get("_solver_pages", pages[:6])]
    data = await run_json_agent(cfg, system=prompts.SOLVER, blocks=blocks, emit=emit, stage="solve", label="标准答案")
    ids = {q["id"] for q in questions}
    out = []
    ans = data.get("answers")
    for a in ans if isinstance(ans, list) else []:
        if isinstance(a, dict) and str(a.get("id")) in ids:
            out.append({"id": str(a["id"]), "answer": str(a.get("answer") or "").strip(),
                        "accepted": [str(x) for x in (a.get("accepted") or [])],
                        "explanation": a.get("explanation") or "",
                        "knowledge": str(a.get("knowledge") or "").strip(),
                        "confident": bool(a.get("confident", True))})
    return out


async def paper_answers(cfg, title, questions, pages, teacher_key, emit):
    loop = asyncio.get_running_loop()
    if loop not in _PAPER_STATES:
        _PAPER_STATES[loop] = (asyncio.Lock(), [])
    lock, in_flight = _PAPER_STATES[loop]
    while True:
        async with lock:
            pending = next((p for p in in_flight if store.same_paper(questions, p["questions"])), None)
            if not pending:
                pid, ratio = store.match_paper(questions)
                if pid and (not teacher_key.strip() or cfg.get("_prefer_saved_answers")):
                    paper = store.get_paper(pid)
                    emit("paper", "done", f"命中试卷库「{paper['title']}」（相似度 {ratio:.0%}）")
                    emit("solve", "skip", "已有标准答案，跳过解题")
                    return paper, True, ratio
                reservation = {"questions": questions, "teacher_key": teacher_key,
                               "future": loop.create_future()}
                in_flight.append(reservation)
                break
        emit("paper", "info", "相同试卷正在生成标准答案，等待后复用")
        resolved = await asyncio.shield(pending["future"])
        if resolved and teacher_key == pending["teacher_key"]:
            emit("paper", "done", "已复用同卷刚生成的标准答案")
            emit("solve", "skip", "已有标准答案，跳过解题")
            return store.get_paper(resolved), True, 1.0

    resolved = None
    try:
        emit("paper", "done", "更新老师标准答案" if pid else "新试卷，将生成标准答案")
        emit("solve", "start", "正在对齐老师答案" if teacher_key.strip() else "标准答案 Agent 正在解题")
        answers = await solve(cfg, title, questions, pages, teacher_key, emit)
        source = "teacher" if teacher_key.strip() else "ai"
        if source == "teacher":
            for answer in answers:
                answer["edited"] = True
        async with lock:
            resolved = store.save_paper(title, questions, answers, source, pid)
        emit("solve", "done", f"已生成 {len(answers)} 道题的标准答案并存入试卷库")
        return store.get_paper(resolved), False, ratio
    finally:
        async with lock:
            in_flight.remove(reservation)
            reservation["future"].set_result(resolved)


# ---------------- Step 3：批改 ----------------
async def grade(cfg, questions, key: dict, emit):
    todo = []
    for q in questions:
        a = key.get(q["id"])
        q["standard_answer"] = a["answer"] if a else None
        q["knowledge"] = (a or {}).get("knowledge") or ""
        q["error_type"] = ""
        q["reasons"] = []
        if q["answer_state"] == "unreadable":
            q.update(verdict=None, reason="学生答案无法辨认")
            q["reasons"].append("学生答案无法辨认")
        elif not a or not a.get("answer"):
            q.update(verdict=None, reason="试卷库中没有这道题的标准答案")
            q["reasons"].append("缺少标准答案")
        elif q["answer_state"] == "blank" or not q["student_answer"]:
            q.update(verdict="wrong", reason="未作答", grade_confidence=1.0, error_type="未作答")
        elif store.norm_answer(q["student_answer"]) in {store.norm_answer(x) for x in [a["answer"], *a.get("accepted", [])]}:
            q.update(verdict="correct", reason="与标准答案一致", grade_confidence=1.0)  # 确定性比对，不调模型
        else:
            todo.append(q)
        if a and not a.get("confident", True) and not a.get("edited"):
            q["reasons"].append("标准答案由 AI 生成且把握不足")
    emit("grade", "info", f"{len(questions) - len(todo)} 道题直接比对完成，{len(todo)} 道题交给批改 Agent")
    async def one(i):
        chunk = todo[i:i + GRADE_CHUNK]
        label = f"第 {i // GRADE_CHUNK + 1} 批（{len(chunk)} 题）"
        payload = [{"id": q["id"], "type": q["type"], "question": q["question"],
                    "standard_answer": key[q["id"]]["answer"], "accepted": key[q["id"]].get("accepted", []),
                    "student_answer": q["student_answer"]} for q in chunk]
        data = await run_json_agent(cfg, system=prompts.GRADER,
                                    blocks=[_text(json.dumps(payload, ensure_ascii=False))], emit=emit, stage="grade", label=label)
        rs = data.get("results")
        rmap = {str(r.get("id")): r for r in (rs if isinstance(rs, list) else []) if isinstance(r, dict)}
        for q in chunk:
            r = rmap.get(q["id"])
            if not r or r.get("verdict") not in ("correct", "wrong", "partial"):
                q.update(verdict=None, reason="批改 Agent 未给出结论")
                q["reasons"].append("批改 Agent 未给出结论")
                continue
            conf = _f(r.get("confidence"))
            q.update(verdict=r["verdict"], reason=r.get("reason") or "", grade_confidence=conf,
                     error_type=(str(r.get("error_type") or "其他") if r["verdict"] != "correct" else ""))
            if conf < 0.7:
                q["reasons"].append("批改把握不足")
            if r.get("key_doubt"):
                q["reasons"].append("批改 Agent 对标准答案有疑问")

        emit("grade", "info", f"{label}判分完成")
    await _parallel(one(i) for i in range(0, len(todo), GRADE_CHUNK))


# ---------------- Step 4：复核 ----------------
def _review_tools(pages: list[Path], ocr_pages: dict, with_zoom: bool):
    """复核 Agent 的工具箱（进程内 MCP）：zoom_region 看放大图，ocr_blocks 查 OCR 文字与坐标。"""
    region = {"page": int, "x1": int, "y1": int, "x2": int, "y2": int}

    @tool("zoom_region", "查看试卷原图某一区域的放大图。坐标为相对整页的 0–1000 归一化坐标。", region)
    async def zoom_region(args):
        try:
            def encode():
                page = int(args["page"])
                if not 1 <= page <= len(pages):
                    raise ValueError("页码无效")
                p = pages[page - 1]
                with Image.open(p) as image:
                    box = imaging.norm_to_px([args["x1"], args["y1"], args["x2"], args["y2"]], *image.size)
                img = imaging.crop(p, box, pad=0.05, min_side=900, outline=False)
                return imaging.image_block(img)["source"]
            b = await asyncio.to_thread(encode)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"无法裁剪：{e}"}], "is_error": True}
        return {"content": [{"type": "image", "data": b["data"], "mimeType": b["media_type"]}]}

    @tool("ocr_blocks", "查询 DeepSeek OCR 在原图某一区域识别到的文字块及其坐标（0–1000 归一化）。"
                        "OCR 对手写可能漏识别，返回为空不代表该处没有字。", region)
    async def ocr_blocks(args):
        try:
            blocks = (ocr_pages.get(int(args["page"])) or {}).get("blocks") or []
            hit = ocr.blocks_in(blocks, [args["x1"], args["y1"], args["x2"], args["y2"]])
            rows = [{"box": b["box"], "text": b["text"][:120]} for b in hit[:40]]
        except Exception as e:
            return {"content": [{"type": "text", "text": f"查询失败：{e}"}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(rows, ensure_ascii=False) if rows else "该区域没有 OCR 文字块"}]}

    tools, names = [], []
    if with_zoom:
        tools.append(zoom_region); names.append("mcp__grader__zoom_region")
    if any(v.get("blocks") for v in ocr_pages.values()):
        tools.append(ocr_blocks); names.append("mcp__grader__ocr_blocks")
    if not tools:
        return None, []
    return create_sdk_mcp_server(name="grader", version="1.0.0", tools=tools), names


async def review(cfg, questions, pages, emit, ocr_pages: dict):
    targets = [q for q in questions if q["bbox_px"]]
    for q in questions:
        if not q["bbox_px"]:
            q["bbox_ok"] = False
    use_zoom = bool(cfg.get("agentic_zoom", True))
    has_ocr = any(v.get("blocks") for v in ocr_pages.values())
    crop_cache = {}

    async def call(chunk, zoom):
        blocks = [_text("以下是待复核的题目。")]
        for q in chunk:
            shown = {"answered": f"「{q['student_answer']}」", "blank": "（判定为未作答）",
                     "unreadable": "（判定为无法辨认）"}[q["answer_state"]]
            blocks.append(_text(f"题号 {q['id']}（第 {q['page']} 页，蓝框归一化坐标 {q['bbox_norm']}）"
                                f"，解析阶段抄录的学生答案：{shown}"))
            cache_key = (q["page"], tuple(q["bbox_px"]))
            if cache_key not in crop_cache:
                crop_cache[cache_key] = await asyncio.to_thread(
                    lambda q=q: imaging.image_block(imaging.crop(pages[q["page"] - 1], q["bbox_px"])))
            blocks.append(crop_cache[cache_key])
        kw, names = {}, []
        if zoom is not None:
            server, names = _review_tools(pages, ocr_pages, with_zoom=zoom)
            if server:
                kw = dict(mcp_server=server, allowed_tools=names, max_turns=8,
                          on_tool=lambda n, i: emit("review", "info", ("复核 Agent 查询 OCR：" if n.endswith("ocr_blocks")
                                                    else "复核 Agent 放大查看：") + f"第 {i.get('page')} 页局部"))
        hints = [h for h, n in ((prompts.ZOOM_HINT, "mcp__grader__zoom_region"),
                                (prompts.OCR_TOOL_HINT, "mcp__grader__ocr_blocks")) if n in names]
        system = prompts.REVIEWER.replace("{ZOOM}", "\n".join(hints))
        return await run_json_agent(cfg, system=system, blocks=blocks, emit=emit, stage="review",
                                    label=f"题 {chunk[0]['id']}–{chunk[-1]['id']}", **kw)

    async def one(i):
        chunk = targets[i:i + REVIEW_CHUNK]
        data = None
        # 依次降级：放大工具 + OCR 工具 → 只带 OCR 工具（纯文本返回）→ 不带工具
        modes = ([True] if use_zoom else []) + ([False] if has_ocr else []) + [None]
        for zoom in modes:
            try:
                data = await call(chunk, zoom)
                break
            except AgentError as e:
                if zoom is not None and tool_unsupported(e):
                    emit("review", "info", f"带工具的复核失败，降级重试（{str(e)[:80]}）")
                else:
                    emit("review", "info", f"复核失败：{str(e)[:120]}")
                    break
        rv = (data or {}).get("reviews")
        rmap = {str(r.get("id")): r for r in (rv if isinstance(rv, list) else []) if isinstance(r, dict)}
        for q in chunk:
            r = rmap.get(q["id"])
            if not r:
                q["bbox_ok"] = False
                q["reasons"].append("复核未完成")
                continue
            q["bbox_ok"] = bool(r.get("bbox_ok"))
            corrected = imaging.canon_bbox(r.get("answer_bbox"))
            # The reviewer sees a crop; only accept a tightened box within the
            # original confirmed region, preventing crop-relative coordinates.
            old_box = q["bbox_norm"]
            if q["bbox_ok"] and corrected and all((corrected[0] >= old_box[0], corrected[1] >= old_box[1],
                                                  corrected[2] <= old_box[2], corrected[3] <= old_box[3])):
                q["bbox_norm"] = corrected
                with Image.open(pages[q["page"] - 1]) as img:
                    q["bbox_px"] = imaging.norm_to_px(corrected, *img.size)
            q["review_read"] = str(r.get("read_answer") or "")
            q["review_note"] = r.get("note") or ""
            if not r.get("matches") and q["answer_state"] != "unreadable":
                q["reasons"].append(f"复核辨认结果不一致（复核读到「{q['review_read']}」）")

        emit("review", "info", f"第 {i // REVIEW_CHUNK + 1} 批复核完成（{len(chunk)} 题）")
    await _parallel(one(i) for i in range(0, len(targets), REVIEW_CHUNK))


def tool_unsupported(error):
    message = str(error).lower()
    return any(word in message for word in ("unsupported", "not supported", "does not support", "不支持")) and any(
        word in message for word in ("tool", "image", "工具", "图片"))


def ocr_crosscheck(questions, ocr_pages: dict):
    """仅对视觉复核定位的答案做文字核对；OCR 直接定位模式不做此检查。"""
    for q in questions:
        if q.get("position_source") == "ocr_blocks":
            continue  # OCR 坐标直接使用，文字差异不再影响落笔或判分状态。
        blocks = (ocr_pages.get(q["page"]) or {}).get("blocks") or []
        if not blocks or q["answer_state"] != "answered":
            continue
        if q.get("bbox_norm"):
            inside = [b for b in ocr.blocks_in(blocks, q["bbox_norm"], min_cover=0.7) if b["text"]]
        else:
            continue
        if not inside:
            continue
        seen = "".join(b["text"] for b in sorted(inside, key=lambda b: (b["box"][1], b["box"][0])))
        q["ocr_text"] = seen[:80]
        q["ocr_confirmed"] = store.norm_answer(seen) == store.norm_answer(q["student_answer"])
        if not q["ocr_confirmed"]:
            q["reasons"].append(f"OCR 在答案位置读到「{seen[:30]}」，与抄录不一致")


def finalize(questions):
    for q in questions:
        if q["parse_confidence"] < 0.6 and q["answer_state"] == "answered":
            q["reasons"].append("解析把握不足")
        q["draw"] = bool(q["bbox_px"]) and bool(q.get("bbox_ok"))
        if q.get("position_source") == "ocr_blocks":
            if not q["draw"]:
                q.setdefault("position_note", "未能定位答案或题目，请人工确认位置")
        elif q["bbox_px"] and not q.get("bbox_ok"):
            q["position_note"] = "答案位置未通过复核，未在卷面落笔"
        elif not q["bbox_px"]:
            q["position_note"] = "未能定位答案，未在卷面落笔"
        q["status"] = "review" if (q["reasons"] or not q.get("verdict")) else q["verdict"]
        q["overridden"] = False


# ---------------- 主流程 ----------------
async def run_pipeline(sid: str, cfg: dict, meta: dict, emit, teacher_key: str = ""):
    d = store.sub_dir(sid)
    t0 = time.time()
    mode = meta.get("ocr_mode") or ("deepseek_ocr" if ocr.enabled(cfg) else "vision")
    cfg = {**cfg, "ocr_mode": mode, "use_ocr": mode == "deepseek_ocr"}
    ocr_positions = mode == "deepseek_ocr"

    emit("preprocess", "start", "转换为逐页图片")
    inputs = meta.get("inputs") or [{"original": meta["original"], "filename": meta["filename"], "role": "auto"}]
    pages, manifest = await asyncio.to_thread(materials.to_pages, inputs, d)
    cfg.update(_material_manifest=manifest, _manual_teacher_key=teacher_key,
               _prefer_saved_answers=bool(meta.get("prefer_saved_answers")),
               _material_bundle=len(pages) > 1 or meta.get("input_version", 1) >= 2 or any(x.get("role", "auto") != "auto" for x in inputs))
    emit("preprocess", "done", f"共 {len(pages)} 页", page_count=len(pages))

    ocr_pages = {}
    if ocr.enabled(cfg):
        emit("ocr", "start", "逐页获取文字与坐标，完成一页即开始解析")
    else:
        emit("ocr", "skip", "使用模型直接识图")
    emit("parse", "start", "解析 Agent 正在阅读试卷")
    title, questions, who = await parse_pages(cfg, pages, emit, ocr_pages)
    material_pages = cfg.get("_materials") or [{**m, "role": "student_work"} for m in manifest]
    graded_pages = cfg.get("_graded_pages") or list(range(1, len(pages) + 1))
    refs = cfg.get("_reference_answers") or []
    if refs:
        reference_text = "标准答案材料（由解析 Agent 忠实提取；未列出的题目没有老师答案，不能自行补齐）：\n" + json.dumps(refs, ensure_ascii=False)
        teacher_key = reference_text + ("\n老师手动填写的标准答案（有冲突时优先）：\n" + teacher_key if teacher_key.strip() else "")
    store.save_artifact(sid, "materials.json", material_pages)
    if ocr_pages:
        store.save_artifact(sid, "ocr.json", ocr_pages)
    ocr_used = any(v["blocks"] for v in ocr_pages.values())
    if not questions:
        raise AgentError("没有在图片中识别到题目，请确认上传的是试卷。")
    store.save_artifact(sid, "layout.json", {"title": title, "questions": questions})
    emit("parse", "done", f"共 {len(questions)} 道题")

    emit("paper", "start", "在试卷库中查找这套试卷")
    paper, paper_hit, ratio = await paper_answers(cfg, title, questions, pages, teacher_key, emit)
    pid = paper["id"]
    key = store.align_answers(questions, paper)
    store.bump_paper(pid)

    emit("grade", "start", "批改 Agent 正在判分")
    await grade(cfg, questions, key, emit)
    store.save_artifact(sid, "grading.json", questions)
    emit("grade", "done", "判分完成")

    review_title = "OCR 位置检查" if ocr_positions else "复核 Agent"
    emit("review", "start", "使用 OCR 答案文字块坐标，不调用复核 Agent" if ocr_positions else "复核 Agent 正在逐题放大核对",
         title=review_title)
    if not ocr_positions:
        await review(cfg, questions, pages, emit, ocr_pages)
    ocr_crosscheck(questions, ocr_pages)
    finalize(questions)
    store.save_artifact(sid, "verified.json", questions)
    n_review = sum(1 for q in questions if q["status"] == "review")
    missing_positions = sum(not q["draw"] for q in questions)
    emit("review", "done", f"OCR 位置检查完成，{missing_positions} 道题未定位，{n_review} 道题需要人工确认" if ocr_positions
         else f"复核完成，{n_review} 道题需要人工确认", title=review_title)

    student_id, how = meta.get("student_id") or None, "manual"
    if not student_id and meta.get("class_id"):
        student_id, how = store.match_student(meta["class_id"], who["name"], who["no"]), "auto"
    emit("annotate", "start", "绘制对错号" + (f"；按卷面姓名「{who['name'] or who['no']}」归档" if student_id and how == "auto" else ""))
    result = {"id": sid, "class_id": meta.get("class_id") or None, "task_id": meta.get("task_id") or None,
              "student_id": student_id, "student_match": how if student_id else None,
              "detected_name": who["name"], "detected_no": who["no"], "paper_rev": paper.get("rev", 0),
              "file_key": meta["file_key"], "filename": meta["filename"], "created_at": meta["created_at"], "status": "done",
              "inputs": inputs, "input_version": meta.get("input_version", 1), "materials": material_pages, "graded_pages": graded_pages,
              "page_count": len(pages), "paper_id": pid, "paper_title": paper["title"],
              "paper_cache_hit": paper_hit, "paper_similarity": round(ratio, 3),
              "answer_source": paper.get("answer_source"), "model": cfg["model"], "ocr_used": ocr_used,
              "ocr_mode": meta.get("ocr_mode", "deepseek_ocr" if cfg.get("use_ocr") else "vision"), "effort": MODEL_EFFORT,
              "position_method": "ocr_blocks" if ocr_positions else "visual_review",
              "annotation_version": ANNOTATION_VERSION,
              "elapsed_s": round(time.time() - t0, 1), "questions": questions,
              "summary": summarize(questions)}
    await asyncio.to_thread(render_annotations, sid, result)
    store.save_result(sid, result)
    store.index_key(meta["file_key"], sid)
    if meta.get("task_id"):  # 任务第一次有人交卷时，绑定这套试卷
        r = store.roster()
        t = next((t for t in r["tasks"] if t["id"] == meta["task_id"]), None)
        if t and not t.get("paper_id"):
            t["paper_id"], t["paper_title"] = pid, paper["title"]
            store.save_roster(r)
    old = store.get_result(meta["replaces"]) if meta.get("replaces") else None
    if old:
        old["superseded"] = True
        store.save_result(old["id"], old)
    emit("annotate", "done", "已生成批改后的图片")
    return result
