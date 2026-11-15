"""班级管理、任务中心、学情报告的 API。"""
import csv
import hashlib
import io
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from . import analytics, config, prompts, store
from .agents import AgentError, run_json_agent

router = APIRouter(prefix="/api")
REPORTS_DIR = config.DATA_DIR / "reports"


def _find(items, id_, what):
    x = next((i for i in items if i["id"] == id_), None)
    if not x:
        raise HTTPException(404, f"{what}不存在")
    return x


# ---------- 班级与学生 ----------
class NameIn(BaseModel):
    name: str


@router.get("/classes")
def list_classes():
    ro = store.roster()
    return [{**c, "student_count": sum(1 for s in ro["students"] if s["class_id"] == c["id"]),
             "task_count": sum(1 for t in ro["tasks"] if t["class_id"] == c["id"])} for c in ro["classes"]]


@router.post("/classes")
async def create_class(body: NameIn):
    if not body.name.strip():
        raise HTTPException(400, "请填写班级名称")
    ro = store.roster()
    c = {"id": store.new_id("cls"), "name": body.name.strip()[:40], "created_at": store.now()}
    ro["classes"].append(c)
    store.save_roster(ro)
    return c


@router.delete("/classes/{cid}")
async def delete_class(cid: str):
    """删除班级及其学生、任务；已批改的记录保留在「最近批改」里。"""
    ro = store.roster()
    _find(ro["classes"], cid, "班级")
    for k in ("students", "tasks"):
        ro[k] = [x for x in ro[k] if x["class_id"] != cid]
    ro["classes"] = [c for c in ro["classes"] if c["id"] != cid]
    store.save_roster(ro)
    return {"ok": True}


@router.get("/classes/{cid}")
def get_class(cid: str):
    ro = store.roster()
    c = _find(ro["classes"], cid, "班级")
    results = [r for r in store.all_results() if r.get("class_id") == cid]
    students = [{**s, **analytics.student_brief([r for r in results if r.get("student_id") == s["id"]])}
                for s in ro["students"] if s["class_id"] == cid]
    return {**c, "students": students}


class BulkIn(BaseModel):
    text: str


@router.post("/classes/{cid}/students")
async def add_students(cid: str, body: BulkIn):
    """批量添加：一行一个，「姓名」或「学号 姓名」。同班重名 / 重学号的行会跳过。"""
    ro = store.roster()
    _find(ro["classes"], cid, "班级")
    have = {(s["name"], s.get("no", "")) for s in ro["students"] if s["class_id"] == cid}
    added = 0
    for line in body.text.splitlines():
        parts = [p for p in re.split(r"[\s,，\t]+", line.strip()) if p]
        if not parts:
            continue
        no, name = (parts[0], parts[1]) if len(parts) >= 2 and re.fullmatch(r"[\w-]*\d[\w-]*", parts[0]) else ("", parts[0])
        if (name, no) in have or len(name) > 30:
            continue
        have.add((name, no))
        ro["students"].append({"id": store.new_id("stu"), "class_id": cid, "name": name, "no": no,
                               "created_at": store.now()})
        added += 1
    store.save_roster(ro)
    return {"added": added}


@router.get("/students")
def list_students():
    ro = store.roster()
    cname = {c["id"]: c["name"] for c in ro["classes"]}
    return [{"id": s["id"], "name": s["name"], "no": s.get("no", ""), "class_id": s["class_id"],
             "class_name": cname.get(s["class_id"], "")} for s in ro["students"]]


@router.delete("/students/{sid}")
async def delete_student(sid: str):
    ro = store.roster()
    _find(ro["students"], sid, "学生")
    ro["students"] = [s for s in ro["students"] if s["id"] != sid]
    store.save_roster(ro)
    return {"ok": True}


@router.get("/students/{sid}")
def get_student(sid: str):
    rep = analytics.student_report(sid)
    if not rep:
        raise HTTPException(404, "学生不存在")
    rep["ai"] = _cached_ai("student", rep)
    return rep


# ---------- 任务中心 ----------
class TaskIn(BaseModel):
    class_id: str
    title: str
    note: str = ""


@router.get("/tasks")
def list_tasks(class_id: str | None = None):
    ro = store.roster()
    results = store.all_results()
    cname = {c["id"]: c["name"] for c in ro["classes"]}
    out = []
    for t in ro["tasks"]:
        if class_id and t["class_id"] != class_id:
            continue
        rs = [r for r in results if r.get("task_id") == t["id"]]
        n = sum(1 for s in ro["students"] if s["class_id"] == t["class_id"])
        out.append({**t, "class_name": cname.get(t["class_id"], ""), "student_count": n,
                    "submitted": len({r["student_id"] for r in rs if r.get("student_id")}),
                    "unassigned": sum(1 for r in rs if not r.get("student_id")),
                    "review": sum(r["summary"]["review"] for r in rs)})
    return out[::-1]


@router.post("/tasks")
async def create_task(body: TaskIn):
    ro = store.roster()
    _find(ro["classes"], body.class_id, "班级")
    if not body.title.strip():
        raise HTTPException(400, "请填写任务名称")
    t = {"id": store.new_id("task"), "class_id": body.class_id, "title": body.title.strip()[:60],
         "note": body.note.strip()[:200], "created_at": store.now(), "paper_id": None, "paper_title": None}
    ro["tasks"].append(t)
    store.save_roster(ro)
    return t


@router.delete("/tasks/{tid}")
async def delete_task(tid: str):
    ro = store.roster()
    _find(ro["tasks"], tid, "任务")
    ro["tasks"] = [t for t in ro["tasks"] if t["id"] != tid]
    store.save_roster(ro)
    return {"ok": True}


@router.get("/tasks/{tid}")
def get_task(tid: str):
    ro = store.roster()
    t = _find(ro["tasks"], tid, "任务")
    rs = [r for r in store.all_results() if r.get("task_id") == tid]
    latest = {}
    for r in rs:  # 升序，后者覆盖前者 → 每个学生取最近一次
        if r.get("student_id"):
            latest[r["student_id"]] = r
    rows = []
    for s in ro["students"]:
        if s["class_id"] != t["class_id"]:
            continue
        r = latest.get(s["id"])
        rows.append({"id": s["id"], "name": s["name"], "no": s.get("no", ""),
                     "sid": r["id"] if r else None, "summary": r["summary"] if r else None,
                     "auto": bool(r and r.get("student_match") == "auto")})
    unassigned = [{"sid": r["id"], "filename": r["filename"], "detected_name": r.get("detected_name", ""),
                   "summary": r["summary"]} for r in rs if not r.get("student_id")]
    cname = next((c["name"] for c in ro["classes"] if c["id"] == t["class_id"]), "")
    return {**t, "class_name": cname, "rows": rows, "unassigned": unassigned}


# ---------- 学情报告 ----------
def _class_or_404(class_id, task_id):
    rep = analytics.class_report(class_id, task_id or None)
    if not rep:
        raise HTTPException(404, "班级不存在")
    return rep


@router.get("/reports/class")
def report_class(class_id: str, task_id: str | None = None):
    rep = _class_or_404(class_id, task_id)
    rep["ai"] = _cached_ai("class", rep)
    return rep


@router.get("/reports/class.csv")
def report_csv(class_id: str, task_id: str | None = None):
    rep = _class_or_404(class_id, task_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["学号", "姓名", "批改次数", "正确率", "最近一次", "待复核题数"])
    pct = lambda v: "" if v is None else f"{v:.1%}"
    for r in rep["students"]:
        # 防止表格软件把以 = + - @ 开头的内容当公式执行
        safe = lambda v: ("'" + v) if isinstance(v, str) and v and v[0] in "=+-@" else v
        w.writerow([safe(r["no"]), safe(r["name"]), r["count"], pct(r["accuracy"]), pct(r["last"]), r["review"]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=report.csv"})


def _ai_payload(scope: str, rep: dict) -> dict:
    """喂给学情分析 Agent 的精简数据（同时作为缓存键：数据没变就不重新生成）。"""
    if scope == "class":
        return {"class": rep["class"]["name"], "task": (rep["task"] or {}).get("title"),
                "student_count": rep["student_count"], "submitted": rep["submitted"], "accuracy": rep["accuracy"],
                "judged_questions": rep["judged"], "pending_review": rep["review"],
                "tiers": {t["name"]: len(t["students"]) for t in rep["tiers"]},
                "questions": [{k: q[k] for k in ("qid", "question", "knowledge", "standard_answer", "rate", "total",
                                                 "wrong_answers")} for q in rep["questions"][:20]],
                "knowledge": rep["knowledge"][:20], "error_types": rep["error_types"]}
    return {"accuracy": rep["accuracy"], "judged_questions": rep["judged"],
            "trend": [{k: t[k] for k in ("date", "title", "accuracy")} for t in rep["trend"][-10:]],
            "knowledge": rep["knowledge"][:20], "error_types": rep["error_types"],
            "recent_wrong": [{k: w[k] for k in ("question", "student_answer", "standard_answer", "knowledge",
                                                "error_type")} for w in rep["wrong_book"][:12]]}


def _ai_path(scope, rep):
    payload = _ai_payload(scope, rep)
    key = rep["class"]["id"] + str((rep["task"] or {}).get("id")) if scope == "class" else rep["student"]["id"]
    h = hashlib.sha256(json.dumps([scope, key, payload], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    return REPORTS_DIR / f"{scope}_{h}.json", payload


def _cached_ai(scope, rep):
    return store._read(_ai_path(scope, rep)[0])


class AiIn(BaseModel):
    scope: str
    class_id: str | None = None
    task_id: str | None = None
    student_id: str | None = None


@router.post("/reports/ai")
async def report_ai(body: AiIn):
    """学情分析 Agent：把统计数据写成讲评建议 / 成长评语。统计数据不变时直接返回上次生成的内容。"""
    if body.scope == "class":
        rep = _class_or_404(body.class_id or "", body.task_id)
    elif body.scope == "student":
        rep = analytics.student_report(body.student_id or "")
        if not rep:
            raise HTTPException(404, "学生不存在")
    else:
        raise HTTPException(400, "scope 无效")
    if not rep["judged"]:
        raise HTTPException(400, "还没有可分析的批改数据")
    path, payload = _ai_path(body.scope, rep)
    hit = store._read(path)
    if hit:
        return hit
    system = prompts.CLASS_REPORT if body.scope == "class" else prompts.STUDENT_REPORT
    try:
        data = await run_json_agent(config.load_settings(), system=system,
                                    blocks=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])
    except AgentError as e:
        raise HTTPException(502, str(e))
    data["generated_at"] = store.now()
    store._write(path, data)
    return data
