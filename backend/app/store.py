"""文件存储：试卷库（题目 Markdown + 标准答案，可复用）与批改记录。

两层缓存：
1. 文件级 —— 上传的文件字节完全相同：直接返回上次的批改结果，不调用模型。
2. 试卷级 —— 同一套试卷（不同学生 / 不同扫描件）：题干指纹或文本相似度命中试卷库，
   复用已保存的标准答案，跳过"解题"这一步。
"""
import hashlib
import json
import re
import shutil
import threading
import time
import unicodedata
import uuid
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path

from .config import DATA_DIR, PAPER_MATCH_THRESHOLD, PAPERS_DIR, SUBS_DIR

FILE_INDEX = DATA_DIR / "file_index.json"
_STORE_LOCK = threading.RLock()
_FULLWIDTH_ASCII = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}


def _serialized(fn):
    @wraps(fn)
    def locked(*args, **kwargs):
        with _STORE_LOCK:
            return fn(*args, **kwargs)
    return locked


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


@_serialized
def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"


def _width_normalize(s):
    # NFKC would turn 12² into 122, losing the exponent. Fold fullwidth ASCII
    # without flattening superscripts, subscripts or mathematical alphabets.
    return unicodedata.normalize("NFC", str(s or "")).translate(_FULLWIDTH_ASCII)


def _canonical_math(s):
    text = _width_normalize(s).replace("−", "-")
    for command, symbol in (("times|cdot", "×"), ("div", "÷"), ("sqrt", "√"), ("pi", "π")):
        text = re.sub(r"\\(?:" + command + r")(?![a-zA-Z])", lambda _: symbol, text)
    text = re.sub(r"\\(?:left|right)(?![a-zA-Z])", "", text)
    return text.replace("*", "×").replace("·", "×").replace("/", "÷")


def _math_signature(s):
    """Cache guard: preserve operands, symbols, grouping, variables and unknown LaTeX commands."""
    tokens = re.findall(r"\d+(?:\.\d+)?|\\[a-zA-Z]+|[a-zA-Z]+|.", _canonical_math(s), re.S)
    # Preserve unknown symbols too (e.g. factorial and Unicode scripts). Only
    # layout/question punctuation and non-numeric Chinese prose are excluded.
    return tuple(t for t in tokens if not t.isspace() and t not in {"?", "？", ".", "。", "$"}
                 and not (len(t) == 1 and "\u3400" <= t <= "\u9fff" and not t.isnumeric()))


def norm(s) -> str:
    """文本归一化：全角转半角、去空白和标点、小写。用于答案比对与试卷指纹。"""
    s = _canonical_math(s).lower()
    return re.sub(r"[\s,.;:!?，。；：！？、'\"`()（）\[\]【】{}$\\_=]+", "", s)


def norm_answer(s) -> str:
    """答案比对用的归一化：只做全角转半角、去空白、小写、去掉首尾的标点和 $。

    不能像 norm 那样删掉小数点和逗号，否则 1.5 会等于 15、"1,2" 会等于 "12"。
    """
    s = _width_normalize(s).lower()
    s = re.sub(r"\s+", "", s)
    s = s.strip("$。,，;；:：、!！?？").rstrip(".")
    # A leading decimal point changes the value: .5 is 1/2, never 5.
    return s if re.match(r"^\.\d", s) else s.lstrip(".")


def _nums(s) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", str(s or "")))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------- 文件级缓存 ----------
def _fkey(file_hash, task_id="", student_id="", ocr_mode=None, class_id=None):
    key = f"{file_hash}|{task_id or ''}|{student_id or ''}"
    if ocr_mode is not None:
        key += f"|{ocr_mode}"
    return key if class_id is None else f"{key}|class:{class_id}"


def find_by_file(file_hash: str, task_id="", student_id="", ocr_mode=None, class_id=None):
    """同一文件与归档上下文的有效结果；旧索引也须验证当前班级与识别方式。"""
    idx = _read(FILE_INDEX, {}) or {}
    keys = dict.fromkeys((_fkey(file_hash, task_id, student_id, ocr_mode, class_id),
                          _fkey(file_hash, task_id, student_id, ocr_mode),
                          _fkey(file_hash, task_id, student_id)))
    for key in keys:
        sid = idx.get(key)
        r = get_result(sid) if sid else None
        if not r or r.get("superseded"):
            continue
        if class_id is not None and (r.get("class_id") or "") != class_id:
            continue
        if student_id and r.get("student_id") != student_id:
            continue
        if ocr_mode is not None and r.get("ocr_mode", "deepseek_ocr" if r.get("ocr_used") else "vision") != ocr_mode:
            continue
        if ocr_mode == "deepseek_ocr" and r.get("position_method") != "ocr_blocks":
            continue
        meta = _read(PAPERS_DIR / str(r.get("paper_id")) / "paper.json") or {}
        if meta.get("rev", 0) == r.get("paper_rev", 0):
            return sid
    return None


def file_key(file_hash: str, task_id="", student_id="", ocr_mode=None, class_id=None) -> str:
    return _fkey(file_hash, task_id, student_id, ocr_mode, class_id)


@_serialized
def index_key(key: str, sid: str):
    idx = _read(FILE_INDEX, {}) or {}
    idx[key] = sid
    _write(FILE_INDEX, idx)


@_serialized
def invalidate_result_cache(sid):
    index = _read(FILE_INDEX, {}) or {}
    _write(FILE_INDEX, {key: value for key, value in index.items() if value != sid})


def workflow_file_key(file_hash, *, mode="grade", paper_id="", class_id="", task_id="", student_id="", paper_rev=None):
    """Separate new results from legacy caches and pin an explicitly selected key."""
    paper = get_paper(paper_id) if paper_id else None
    revision = paper_rev if paper_rev is not None else paper.get("rev", 0) if paper else None
    context = {"workflow_version": 2, "ocr_stage": "input-v1", "file_hash": file_hash, "mode": mode,
               "paper_id": paper_id or "", "paper_rev": revision,
               "class_id": class_id or "", "task_id": task_id or "", "student_id": student_id or ""}
    return "workflow:2:" + sha256_bytes(json.dumps(context, sort_keys=True).encode())


def find_workflow_result(key, *, mode="grade", paper_id="", class_id="", task_id="", student_id=""):
    sid = (_read(FILE_INDEX, {}) or {}).get(key)
    result = get_result(sid) if sid else None
    if not result or result.get("superseded") or result.get("workflow_version") != 2:
        return None
    if result.get("mode", "grade") != mode or result.get("file_key") != key:
        return None
    if (result.get("class_id") or "") != class_id or (result.get("task_id") or "") != task_id:
        return None
    if student_id and result.get("student_id") != student_id:
        return None
    if paper_id and result.get("paper_id") != paper_id:
        return None
    paper = get_paper(result.get("paper_id", ""))
    if paper and paper.get("rev", 0) == result.get("paper_rev", 0):
        return sid
    return None


# ---------- 试卷库 ----------
def paper_text(questions: list[dict]) -> str:
    return "|".join(norm(q.get("question")) for q in questions)


def fingerprint(questions: list[dict]) -> str:
    return hashlib.sha256(paper_text(questions).encode()).hexdigest()[:16]


def list_papers() -> list[dict]:
    out = []
    for d in PAPERS_DIR.iterdir():
        meta = _read(d / "paper.json")
        if meta:
            out.append({k: meta.get(k) for k in ("id", "title", "created_at", "question_count", "used_count",
                                                 "answer_source", "rev", "workflow_version", "review_summary")})
    return sorted(out, key=lambda m: m["created_at"], reverse=True)


@_serialized
def get_paper(pid: str):
    d = PAPERS_DIR / pid
    meta = _read(d / "paper.json")
    if not meta:
        return None
    meta["answers"] = _read(d / "answers.json", [])
    meta["markdown"] = (d / "paper.md").read_text("utf-8") if (d / "paper.md").exists() else ""
    return meta


def match_paper(questions: list[dict]):
    """返回 (paper_id, 相似度)；未命中返回 (None, 最高相似度)。"""
    text, fp = paper_text(questions), fingerprint(questions)
    if len(text.replace("|", "")) < 12:
        return None, 0.0  # 题干几乎是空的（例如只有答题卡），没法可靠判断是不是同一套试卷
    signature = [_math_signature(q.get("question")) for q in questions]
    best, best_r = None, 0.0
    for d in PAPERS_DIR.iterdir():
        meta = _read(d / "paper.json")
        if not meta:
            continue
        # Even fingerprint hits must preserve mathematical structure: norm()
        # removes decimal points and grouping, which can change the answer.
        if signature != [_math_signature(q.get("question")) for q in meta.get("questions", [])]:
            continue
        if meta.get("fingerprint") == fp:
            return meta["id"], 1.0
        n = meta.get("question_count", 0)
        if abs(n - len(questions)) > max(1, 0.15 * max(n, len(questions))):
            continue  # 题量差得多，不是同一套
        r = SequenceMatcher(None, text, meta.get("norm_text", ""), autojunk=False).ratio()
        if r > best_r:
            best, best_r = meta["id"], r
    return (best, best_r) if best_r >= PAPER_MATCH_THRESHOLD else (None, best_r)


def same_paper(a, b):
    """Use the library's matching rules for concurrent, not-yet-saved papers."""
    ta, tb = paper_text(a), paper_text(b)
    if min(len(ta.replace("|", "")), len(tb.replace("|", ""))) < 12:
        return False
    if abs(len(a) - len(b)) > max(1, .15 * max(len(a), len(b))):
        return False
    na = [_math_signature(q.get("question")) for q in a]
    nb = [_math_signature(q.get("question")) for q in b]
    if na != nb:
        return False
    return SequenceMatcher(None, ta, tb, autojunk=False).ratio() >= PAPER_MATCH_THRESHOLD


def render_markdown(title: str, questions: list[dict], answers: list[dict]) -> str:
    amap = {a["id"]: a for a in answers}
    lines = [f"# {title or '未命名试卷'}", ""]
    for q in questions:
        a = amap.get(q["id"], {})
        lines += [f"## 第 {q['id']} 题" + (f"　〔{a['knowledge']}〕" if a.get("knowledge") else ""), "",
                  q.get("question", ""), "",
                  f"**标准答案：** {a.get('answer', '（缺失）')}", ""]
        if a.get("explanation"):
            lines += [f"> {a['explanation']}", ""]
    return "\n".join(lines)


@_serialized
def save_paper(title, questions, answers, source: str, pid: str | None = None) -> str:
    pid = pid or new_id("paper")
    d = PAPERS_DIR / pid
    qs = [{k: q[k] for k in ("id", "type", "question", "page", "source_page", "source_pages", "reference_page", "reference_pages") if k in q}
          for q in questions]
    old = _read(d / "paper.json", {}) or {}
    _write(d / "paper.json", {
        **old,
        "id": pid, "title": title or "未命名试卷",
        "created_at": old.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": fingerprint(qs), "norm_text": paper_text(qs), "questions": qs,
        "question_count": len(qs), "used_count": old.get("used_count", 0), "answer_source": source,
        "rev": old.get("rev", 0) + (1 if old else 0)})
    _write(d / "answers.json", answers)
    (d / "paper.md").write_text(render_markdown(title, qs, answers), "utf-8")
    return pid


@_serialized
def save_reviewed_paper(title, questions, answers, source, pid=None, reference_pages=None):
    """Publish reviewed per-question keys and durable source images (including disputes)."""
    sources = []
    for number, item in enumerate(reference_pages or [], 1):
        item = dict(item) if isinstance(item, dict) else {"page": number, "path": item}
        page = item.get("page")
        if type(page) is not int or page < 1 or any(r["page"] == page for r in sources):
            raise ValueError("试卷原图页码无效或重复")
        if not Path(item["path"]).is_file():
            raise ValueError("试卷原图不存在")
        sources.append(item)
    pid = save_paper(title, questions, answers, source, pid)
    folder = PAPERS_DIR / pid
    meta = _read(folder / "paper.json")
    references = []
    for item in sources:
        page = item["page"]
        source_path = Path(item["path"])
        target = folder / "reference-pages" / f"page-{page}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != target.resolve():
            shutil.copyfile(source_path, target)
        references.append({**{k: v for k, v in item.items() if k != "path"}, "path": str(target)})
    if reference_pages is not None:
        meta["reference_pages"] = references
    meta.update(workflow_version=2, reviewed_rev=meta["rev"], review_summary={
        status: sum(a.get("review_status") == status for a in answers)
        for status in ("approved", "review", "manual_confirmed")})
    _write(folder / "paper.json", meta)
    return pid


@_serialized
def update_answers(pid: str, patch: list[dict]):
    p = get_paper(pid)
    if not p:
        return None
    amap = {a["id"]: a for a in p["answers"]}
    qids = {q["id"] for q in p["questions"]}
    answer_changed = changed = False
    for it in patch:
        qid = str(it.get("id", ""))
        if qid not in qids:
            continue
        a = amap.setdefault(qid, {"id": qid, "answer": "", "accepted": [], "explanation": "", "knowledge": ""})
        if "answer" in it and str(it["answer"]).strip() != str(a.get("answer", "")):
            # 只有真正被老师改过的题才标记为"已人工确认"，其余题保留 AI 的把握度提示
            a["answer"] = str(it["answer"]).strip()
            a["confident"] = a["edited"] = bool(a["answer"])
            a["accepted"] = []  # 等价答案属于旧答案，老师修改后不能继续据此判对。
            a["review_status"] = "manual_confirmed" if a["answer"] else "review"
            answer_changed = changed = True
        if it.get("confirmed") is True and a.get("answer") and a.get("review_status") != "manual_confirmed":
            a.update(review_status="manual_confirmed", edited=True, confident=True)
            answer_changed = changed = True
        if "knowledge" in it and str(it["knowledge"]).strip() != str(a.get("knowledge", "")):
            a["knowledge"] = str(it["knowledge"]).strip()
            changed = True
    if changed:  # 没有实际改动就不保存，避免无谓地让文件缓存失效
        answers = [amap[q["id"]] for q in p["questions"] if q["id"] in amap]
        save_paper(p["title"], p["questions"], answers, "teacher" if answer_changed else p.get("answer_source", "ai"), pid)
        meta = _read(PAPERS_DIR / pid / "paper.json")
        meta["review_summary"] = {status: sum(a.get("review_status") == status for a in answers)
                                  for status in ("approved", "review", "manual_confirmed")}
        _write(PAPERS_DIR / pid / "paper.json", meta)
    return get_paper(pid)


@_serialized
def bump_paper(pid: str):
    f = PAPERS_DIR / pid / "paper.json"
    meta = _read(f)
    if meta:
        meta["used_count"] = meta.get("used_count", 0) + 1
        _write(f, meta)


def align_answers(questions: list[dict], paper: dict) -> dict:
    """把本次解析出的题目对齐到试卷库里的题目，返回 {本次题号: 答案}。

    题号相同且题干基本一致才算同一题；否则按题干相似度找。两种情况都要求数学结构一致，
    对不上的题宁可没有答案（转人工复核），也不套用别的题的答案。
    """
    amap = {a["id"]: a for a in paper["answers"]}
    pq = {q["id"]: (norm(q["question"]), _math_signature(q["question"])) for q in paper["questions"]}
    out = {}
    for q in questions:
        t, n = norm(q.get("question")), _math_signature(q.get("question"))
        same = lambda k: pq[k][1] == n
        sim = lambda k: SequenceMatcher(None, t, pq[k][0]).ratio() if t and pq[k][0] else 0.0
        hit = None
        if q["id"] in pq and same(q["id"]) and (sim(q["id"]) > 0.6 or not t):
            hit = q["id"]
        else:
            cands = [(sim(k), k) for k in pq if same(k)]
            if cands and max(cands)[0] >= 0.8:
                hit = max(cands)[1]
        if hit and hit in amap:
            out[q["id"]] = amap[hit]
    return out


# ---------- 批改记录 ----------
def sub_dir(sid: str) -> Path:
    return SUBS_DIR / sid


def save_result(sid: str, result: dict):
    _write(sub_dir(sid) / "result.json", result)


def get_result(sid: str):
    return _read(sub_dir(sid) / "result.json")


def save_artifact(sid: str, name: str, obj):
    _write(sub_dir(sid) / name, obj)


def all_results() -> list[dict]:
    """所有有效的批改结果（不含已被重新批改取代的），按时间升序。"""
    out = []
    for d in SUBS_DIR.iterdir():
        r = _read(d / "result.json")
        if r and not r.get("superseded") and r.get("mode") != "prepare":
            out.append(r)
    return sorted(out, key=lambda r: (r["created_at"], r["id"]))


def list_submissions() -> list[dict]:
    names = {s["id"]: s["name"] for s in roster()["students"]}
    out = [{**{k: r.get(k) for k in ("id", "filename", "created_at", "paper_id", "paper_title", "summary",
                                     "status", "student_id", "task_id", "class_id")},
            "student_name": names.get(r.get("student_id"))} for r in all_results()]
    return out[::-1][:100]


# ---------- 班级 / 学生 / 任务 ----------
ROSTER = DATA_DIR / "roster.json"


def roster() -> dict:
    r = _read(ROSTER, {}) or {}
    for k in ("classes", "students", "tasks"):
        r.setdefault(k, [])
    return r


def save_roster(r: dict):
    _write(ROSTER, r)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def match_student(class_id: str, name: str, no: str):
    """用解析 Agent 从卷面读到的姓名 / 学号在班级花名册里找学生；有歧义时不匹配。"""
    studs = [s for s in roster()["students"] if s["class_id"] == class_id]
    no, name = norm(no), norm(name)
    if no:
        hit = [s for s in studs if norm(s.get("no")) == no]
        if len(hit) == 1:
            return hit[0]["id"]
    if name:
        hit = [s for s in studs if norm(s["name"]) == name]
        if len(hit) == 1:
            return hit[0]["id"]
    return None
