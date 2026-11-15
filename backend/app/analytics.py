"""学情统计：全部由已保存的批改结果实时计算，不调用模型。

计分口径：正确 = 1，部分正确 = 0.5，错误 = 0；「待复核」的题不计入（老师改判后才计入）。
"""
from collections import Counter, defaultdict

from . import store

SCORE = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}
TIERS = [("提高组", 0.85, 1.01), ("巩固组", 0.60, 0.85), ("基础组", -1, 0.60)]
BANDS = [("优秀 ≥90%", 0.90, 1.01), ("良好 75–90%", 0.75, 0.90), ("合格 60–75%", 0.60, 0.75), ("待提高 <60%", -1, 0.60)]


def _acc(right, total):
    return round(right / total, 4) if total else None


def _tally(results):
    """汇总一批结果 → (得分, 已判定题数, 待复核数, 知识点, 错因, 错题列表)。"""
    right = total = review = 0
    kp = defaultdict(lambda: [0.0, 0])
    errs = Counter()
    wrong = []
    for r in results:
        for q in r["questions"]:
            st = q["status"]
            if st == "review":
                review += 1
                continue
            sc = SCORE[st]
            right += sc
            total += 1
            k = q.get("knowledge") or "未标注"
            kp[k][0] += sc
            kp[k][1] += 1
            if st != "correct":
                errs[q.get("error_type") or "其他"] += 1
                wrong.append({"sid": r["id"], "date": r["created_at"][:10], "paper_title": r["paper_title"],
                              "qid": q["id"], "question": q["question"], "student_answer": q["student_answer"],
                              "answer_state": q["answer_state"], "standard_answer": q.get("standard_answer"),
                              "reason": q.get("reason", ""), "knowledge": k, "status": st,
                              "error_type": q.get("error_type") or "其他"})
    knowledge = sorted(({"name": k, "mastery": _acc(v[0], v[1]), "total": v[1]} for k, v in kp.items()),
                       key=lambda x: (x["mastery"], -x["total"]))
    return right, total, review, knowledge, [{"name": k, "count": n} for k, n in errs.most_common()], wrong


def student_brief(results):
    right, total, review, *_ = _tally(results)
    accs = [r["summary"]["accuracy"] for r in results if r["summary"]["accuracy"] is not None]
    return {"count": len(results), "accuracy": _acc(right, total), "review": review,
            "last": accs[-1] if accs else None,
            "delta": round(accs[-1] - accs[-2], 4) if len(accs) >= 2 else None}


def student_report(student_id: str):
    ro = store.roster()
    stu = next((s for s in ro["students"] if s["id"] == student_id), None)
    if not stu:
        return None
    cls = next((c for c in ro["classes"] if c["id"] == stu["class_id"]), {})
    tasks = {t["id"]: t["title"] for t in ro["tasks"]}
    results = [r for r in store.all_results() if r.get("student_id") == student_id]
    right, total, review, knowledge, errs, wrong = _tally(results)
    return {"student": stu, "class_name": cls.get("name", ""), **student_brief(results),
            "judged": total,
            "trend": [{"sid": r["id"], "date": r["created_at"][:10], "title": tasks.get(r.get("task_id")) or r["paper_title"],
                       "accuracy": r["summary"]["accuracy"], **{k: r["summary"][k] for k in ("correct", "wrong", "partial", "review", "total")}}
                      for r in results],
            "knowledge": knowledge, "error_types": errs, "wrong_book": wrong[::-1]}


def class_report(class_id: str, task_id: str | None = None):
    ro = store.roster()
    cls = next((c for c in ro["classes"] if c["id"] == class_id), None)
    if not cls:
        return None
    task = next((t for t in ro["tasks"] if t["id"] == task_id), None) if task_id else None
    students = [s for s in ro["students"] if s["class_id"] == class_id]
    results = [r for r in store.all_results() if r.get("class_id") == class_id and (not task_id or r.get("task_id") == task_id)]
    by_stu = defaultdict(list)
    for r in results:
        if r.get("student_id"):
            by_stu[r["student_id"]].append(r)
    ids = {s["id"] for s in students}
    assigned = [r for r in results if r.get("student_id") in ids]  # 已从花名册移除的学生不计入
    right, total, review, knowledge, errs, _ = _tally(assigned)

    rows = []
    for s in students:
        b = student_brief(by_stu.get(s["id"], []))
        rows.append({"id": s["id"], "name": s["name"], "no": s.get("no", ""), **b})
    scored = [r for r in rows if r["accuracy"] is not None]
    dist = [{"name": n, "count": sum(1 for r in scored if lo <= r["accuracy"] < hi)} for n, lo, hi in BANDS]
    tiers = [{"name": n, "students": [r["name"] for r in scored if lo <= r["accuracy"] < hi]} for n, lo, hi in TIERS]

    # 逐题正确率与典型错误答案（按试卷分组）
    qstat = {}
    for r in assigned:
        for q in r["questions"]:
            if q["status"] == "review":
                continue
            d = qstat.setdefault((r["paper_id"], q["id"]), {"paper_title": r["paper_title"], "qid": q["id"],
                                                             "question": q["question"], "knowledge": q.get("knowledge") or "未标注",
                                                             "standard_answer": q.get("standard_answer"),
                                                             "right": 0.0, "total": 0, "wrong_answers": Counter(), "wrong_students": []})
            d["right"] += SCORE[q["status"]]
            d["total"] += 1
            if q["status"] != "correct":
                d["wrong_answers"][q["student_answer"] or "（未作答）"] += 1
                who = next((s["name"] for s in students if s["id"] == r["student_id"]), "?")
                if who not in d["wrong_students"]:
                    d["wrong_students"].append(who)
    questions = sorted(({**{k: v for k, v in d.items() if k not in ("right", "wrong_answers")},
                         "rate": _acc(d["right"], d["total"]),
                         "wrong_answers": [{"answer": a, "count": n} for a, n in d["wrong_answers"].most_common(3)]}
                        for d in qstat.values()), key=lambda x: (x["rate"], x["qid"]))

    attention = []
    for r in rows:
        why = []
        if task_id and not r["count"]:
            why.append("未交")
        if r["accuracy"] is not None and r["accuracy"] < 0.6:
            why.append(f"正确率 {r['accuracy']:.0%}")
        if r["delta"] is not None and r["delta"] <= -0.15:
            why.append(f"较上次下降 {abs(r['delta']):.0%}")
        if why:
            attention.append({"id": r["id"], "name": r["name"], "why": "，".join(why)})

    submitted = sum(1 for r in rows if r["count"])
    return {"class": cls, "task": task, "student_count": len(students), "submitted": submitted,
            "submit_rate": _acc(submitted, len(students)), "submission_count": len(results),
            "unassigned": len(results) - len(assigned), "accuracy": _acc(right, total), "judged": total,
            "review": review, "distribution": dist, "tiers": tiers, "questions": questions,
            "knowledge": knowledge, "error_types": errs, "attention": attention, "students": rows}
