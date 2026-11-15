"""批改工作台 API。"""
import base64
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, imaging, materials, ocr, school, store
from .agents import AgentError, run_agent
from .pipeline import ensure_ocr_annotations, render_annotations, run_pipeline, summarize
from .jobs import ACTIVE, JobManager, valid_id

job_manager = JobManager(run_pipeline, max_parallel=max(1, int(os.environ.get("MAX_PARALLEL_JOBS", "2"))))


@asynccontextmanager
async def lifespan(app):
    job_manager.recover()
    yield
    await job_manager.close()


app = FastAPI(title="A道工作台", lifespan=lifespan)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.middleware("http")
async def auth(request: Request, call_next):
    """可选的访问口令（环境变量 APP_PASSWORD）：HTTP Basic，用户名随意。"""
    if APP_PASSWORD and request.url.path != "/api/health":
        try:
            scheme, _, cred = request.headers.get("authorization", "").partition(" ")
            pw = base64.b64decode(cred).decode().partition(":")[2] if scheme.lower() == "basic" else ""
        except Exception:
            pw = ""
        if not secrets.compare_digest(pw.encode(), APP_PASSWORD.encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="A-Dao"'})
    return await call_next(request)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_BYTES = 40 * 1024 * 1024



# ---------- 模型设置 ----------
class SettingsIn(BaseModel):
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    agentic_zoom: bool | None = None
    use_ocr: bool | None = None


@app.get("/api/settings")
def get_settings():
    return config.public_settings(config.load_settings())


@app.put("/api/settings")
async def put_settings(body: SettingsIn):
    return config.public_settings(config.save_settings(body.model_dump()))


@app.post("/api/settings/test")
async def test_settings():
    cfg = config.load_settings()
    t = time.time()
    try:
        out = await run_agent(cfg, system="你是连通性测试助手。", blocks=[{"type": "text", "text": "只回复：OK"}])
        return {"ok": True, "reply": out.strip()[:80], "elapsed_s": round(time.time() - t, 1)}
    except AgentError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ocr/text")
async def ocr_text(file: UploadFile = File(...)):
    """用 DeepSeek OCR 读取老师上传的答案文件（PDF / Word / PPT / 图片），返回纯文字供老师确认。"""
    cfg = config.load_settings()
    if not ocr.available(cfg):
        raise HTTPException(400, "读取答案文件需要 DeepSeek 接口（模型设置里填 DeepSeek 的地址和 Key）")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(400, "文件超过 40MB")
    try:
        obj = await ocr.recognize(cfg, data, Path(file.filename or "answer").name, "text_recognition")
    except ocr.OcrError as e:
        raise HTTPException(502, str(e))
    return {"text": ocr.plain_text(obj), "page_count": obj.get("page_count")}


# ---------- 批改 ----------
@app.post("/api/submissions")
async def create_submission(file: UploadFile | None = File(None), answer_key: str = Form(""), force: bool = Form(False),
                            class_id: str = Form(""), task_id: str = Form(""), student_id: str = Form(""),
                            ocr_mode: str = Form(""), files: list[UploadFile] | None = File(None),
                            file_roles: str = Form("")):
    if file and files:
        raise HTTPException(400, "请使用 file 或 files 中的一种方式上传")
    try:
        loaded = await materials.read_uploads(files or ([file] if file else []), file_roles)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    cfg = config.load_settings()
    ocr_mode = ocr_mode or ("deepseek_ocr" if ocr.available(cfg) else "vision")
    if ocr_mode not in ("vision", "deepseek_ocr"):
        raise HTTPException(400, "识别方式无效")
    if ocr_mode == "deepseek_ocr" and not ocr.available(cfg):
        raise HTTPException(400, "当前接口不支持 DeepSeek OCR，请选择模型直接识图")
    cfg = {**cfg, "use_ocr": ocr_mode == "deepseek_ocr"}
    ro = store.roster()
    if task_id:
        t = next((t for t in ro["tasks"] if t["id"] == task_id), None)
        if not t:
            raise HTTPException(400, "任务不存在")
        class_id = t["class_id"]
    if student_id:
        st = next((x for x in ro["students"] if x["id"] == student_id), None)
        if not st or (class_id and st["class_id"] != class_id):
            raise HTTPException(400, "学生不存在或不属于该班级")
        class_id = st["class_id"]
    if class_id and not any(c["id"] == class_id for c in ro["classes"]):
        raise HTTPException(400, "班级不存在")
    fh = materials.bundle_hash(loaded, modern=bool(files))
    if not force and not answer_key.strip():
        hit = store.find_by_file(fh, task_id, student_id, ocr_mode, class_id)
        if hit:  # 文件级缓存：同一个文件直接返回上次结果
            return {"id": hit, "cached": True}
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key，请先到「模型设置」填写。")
    sid = store.new_id("sub")
    d = store.sub_dir(sid)
    d.mkdir(parents=True)
    inputs = materials.save_inputs(d, loaded)
    meta = {"filename": loaded[0]["filename"] + (f" 等 {len(loaded)} 份材料" if len(loaded) > 1 else ""),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "inputs": inputs, "input_version": 2 if files else 1,
            "original": inputs[0]["original"], "file_hash": fh, "class_id": class_id, "task_id": task_id,
            "student_id": student_id, "ocr_mode": ocr_mode, "file_key": store.file_key(fh, task_id, student_id, ocr_mode, class_id)}
    job_manager.start(sid, cfg, meta, answer_key)
    return {"id": sid, "cached": False}


@app.get("/api/submissions/{sid}/status")
def submission_status(sid: str):
    status = job_manager.status(sid)
    if not status:
        raise HTTPException(404, "批改任务不存在")
    return status


@app.get("/api/submissions/{sid}/events")
async def events(sid: str, request: Request, after: int = 0):
    submission_status(sid)
    try:
        cursor = max(0, after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "事件序号无效")
    return StreamingResponse(job_manager.stream(sid, cursor), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/submissions")
def list_submissions():
    return store.list_submissions()


def _result(sid):
    if not valid_id(sid):
        raise HTTPException(404)
    r = store.get_result(sid)
    if not r:
        raise HTTPException(404, "批改结果不存在")
    return ensure_ocr_annotations(sid, r)


@app.get("/api/submissions/{sid}")
def get_submission(sid: str):
    return _result(sid)


@app.get("/api/submissions/{sid}/pages/{n}")
def page_image(sid: str, n: int, kind: str = "annotated"):
    submission_status(sid)
    if kind not in ("raw", "annotated") or n < 1:
        raise HTTPException(400, "页面或图片类型无效")
    if kind == "annotated" and store.get_result(sid):
        _result(sid)
    p = store.sub_dir(sid) / ("annotated" if kind == "annotated" else "pages") / f"page-{n}.png"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


def _restart(sid, retry=False):
    previous = submission_status(sid)
    if previous["status"] in ACTIVE:
        raise HTTPException(409, "任务仍在运行，请等待完成")
    if retry and previous["status"] not in ("failed", "interrupted"):
        raise HTTPException(409, "只有失败或中断的任务可以重试")
    cfg = config.load_settings()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key，请先到「模型设置」填写。")
    record = job_manager.record(sid)
    result = store.get_result(sid) or {}
    original_meta = record["meta"] if retry and record else result
    mode = previous["ocr_mode"]
    # Retry keeps input/model choices, but always uses the current credential.
    if retry and record:
        cfg.update(record.get("settings", {}))
    cfg["use_ocr"] = mode == "deepseek_ocr"
    if mode == "deepseek_ocr" and not ocr.available(cfg):
        raise HTTPException(400, "当前配置无法使用此任务的 DeepSeek OCR 模式，请先恢复接口设置")
    new = store.new_id("sub")
    d = store.sub_dir(new)
    d.mkdir(parents=True)
    try:
        inputs = materials.copy_inputs(store.sub_dir(sid), d, original_meta)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    loaded = [{**item, "data": Path(item["original"]).read_bytes()} for item in inputs]
    meta = {k: original_meta.get(k) or "" for k in ("class_id", "task_id", "student_id")}
    meta.update(filename=previous["filename"], created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                original=inputs[0]["original"], inputs=inputs, input_version=original_meta.get("input_version", 1),
                file_hash=materials.bundle_hash(loaded, modern=original_meta.get("input_version", 1) >= 2), ocr_mode=mode)
    meta["file_key"] = store.file_key(meta["file_hash"], meta["task_id"], meta["student_id"], mode, meta["class_id"])
    if result:
        meta["replaces"] = sid
    elif original_meta.get("replaces"):
        meta["replaces"] = original_meta["replaces"]
    if not retry or original_meta.get("prefer_saved_answers"):
        meta["prefer_saved_answers"] = True  # 重批应采用试卷库中老师最新修订的答案。
    teacher_key = record.get("teacher_key", "") if record else ""
    job_manager.start(new, cfg, meta, teacher_key if retry else "")
    return {"id": new, "cached": False}


@app.post("/api/submissions/{sid}/regrade")
async def regrade(sid: str):
    _result(sid)
    return _restart(sid)


@app.post("/api/submissions/{sid}/retry")
async def retry_submission(sid: str):
    return _restart(sid, retry=True)


@app.get("/api/jobs")
async def jobs(task_id: str = "", status: str = ""):
    if status not in ("", "all", "queued", "running", "done", "failed", "interrupted"):
        raise HTTPException(400, "任务状态无效")
    return job_manager.list(task_id, status)


@app.get("/api/submissions/{sid}/pdf")
def download_pdf(sid: str):
    """下载批改后的试卷 PDF（所有页，含对错号）。"""
    r = _result(sid)
    d = store.sub_dir(sid) / "annotated"
    paths = [d / f"page-{i}.png" for i in r.get("graded_pages", range(1, r["page_count"] + 1))]
    if not all(p.exists() for p in paths):
        render_annotations(sid, r)  # 标注图丢失时按结果重新生成
    data = imaging.pages_to_pdf(paths)
    name = Path(r["filename"]).stem + "-已批改.pdf"
    from urllib.parse import quote
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=graded.pdf; filename*=UTF-8''{quote(name)}"})


class AssignIn(BaseModel):
    student_id: str | None = None


@app.post("/api/submissions/{sid}/assign")
async def assign(sid: str, body: AssignIn):
    """把一份批改结果归到某个学生名下（或取消归档）。"""
    r = _result(sid)
    if body.student_id:
        st = next((x for x in store.roster()["students"] if x["id"] == body.student_id), None)
        if not st:
            raise HTTPException(404, "学生不存在")
        r.update(student_id=st["id"], class_id=st["class_id"], student_match="manual")
    else:
        r.update(student_id=None, student_match=None)
    store.save_result(sid, r)
    return r


class OverrideIn(BaseModel):
    qid: str
    status: str


@app.post("/api/submissions/{sid}/override")
async def override(sid: str, body: OverrideIn):
    """老师人工改判：更新结论并重新绘制标注（不调用模型）。"""
    r = _result(sid)
    if body.status not in ("correct", "wrong", "partial"):
        raise HTTPException(400, "status 无效")
    q = next((q for q in r["questions"] if q["id"] == body.qid), None)
    if not q:
        raise HTTPException(404, "题目不存在")
    q.update(status=body.status, overridden=True, draw=bool(q.get("bbox_px")))
    r["summary"] = summarize(r["questions"])
    render_annotations(sid, r)
    store.save_result(sid, r)
    return r


# ---------- 试卷库 ----------
def _pid(pid):
    if not pid.startswith("paper_") or "/" in pid or ".." in pid:
        raise HTTPException(404)
    p = store.get_paper(pid)
    if not p:
        raise HTTPException(404, "试卷不存在")
    p.pop("norm_text", None)
    return p


@app.get("/api/papers")
def list_papers():
    return store.list_papers()


@app.get("/api/papers/{pid}")
def get_paper(pid: str):
    return _pid(pid)


class AnswerPatch(BaseModel):
    answers: list[dict]


@app.put("/api/papers/{pid}/answers")
async def put_answers(pid: str, body: AnswerPatch):
    _pid(pid)
    p = store.update_answers(pid, body.answers)
    p.pop("norm_text", None)
    return p


@app.get("/api/health")
def health():
    return {"ok": True}


app.include_router(school.router)
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
