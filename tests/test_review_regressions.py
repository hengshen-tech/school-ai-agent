"""Regressions independently reproduced during the full grading review."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from test_jobs import image_bytes, isolated_store  # noqa: F401
from app import main, pipeline, store
from app.jobs import JobManager


def question(expression):
    return {"id": "1", "type": "calc", "page": 1, "question": f"请计算下面算式的结果：{expression} = ?"}


@pytest.mark.parametrize("old,new", [("12+8", "12-8"), ("12×8", "12÷8"),
                                     ("√(12+8)", "(12+8)"), ("(12+8)*2", "12+8*2"),
                                     ("π*12+8", "12+8"), ("12²+8", "122+8"),
                                     ("12₃+8", "123+8"), ("12!+8", "12+8"), ("x+12", "X+12")])
@pytest.mark.parametrize("path", ["library", "inflight", "alignment"])
def test_changed_math_expression_does_not_reuse_standard_answer(old, new, path):
    before, after = [question(old)], [question(new)]
    pid = store.save_paper("旧试卷", before, [{"id": "1", "answer": "old"}], "ai")
    if path == "library":
        assert store.match_paper(after)[0] is None
    elif path == "inflight":
        assert not store.same_paper(before, after)
    else:
        assert store.align_answers(after, store.get_paper(pid)) == {}


@pytest.mark.parametrize("old,new", [("12×8", r"12\times8"), ("12÷8", r"12\div8"), ("12-8", "12−8")])
def test_equivalent_operator_notation_can_reuse_paper(old, new):
    before, after = [question(old)], [question(new)]
    pid = store.save_paper("同一试卷", before, [{"id": "1", "answer": "answer"}], "ai")
    assert store.match_paper(after)[0] == pid
    assert store.same_paper(before, after)
    assert store.align_answers(after, store.get_paper(pid))["1"]["answer"] == "answer"


def test_direct_answer_comparison_preserves_superscripts():
    assert store.norm_answer("5²") != store.norm_answer("52")
    assert store.norm_answer("５２") == store.norm_answer("52")


@pytest.mark.asyncio
async def test_edited_standard_answer_does_not_accept_previous_equivalent_values(monkeypatch):
    q = question("12+8")
    q.update(student_answer="5.0", answer_state="answered")
    pid = store.save_paper("练习", [q], [{"id": "1", "answer": "5", "accepted": ["5.0"]}], "ai")
    paper = store.update_answers(pid, [{"id": "1", "answer": "20"}])
    async def model(*a, **kw):
        return {"results": [{"id": "1", "verdict": "wrong", "confidence": .99}]}
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    await pipeline.grade({}, [q], store.align_answers([q], paper), lambda *a, **kw: None)
    assert q["verdict"] == "wrong"
    assert paper["answers"][0]["accepted"] == []


def test_retry_of_failed_regrade_keeps_original_result_replacement(monkeypatch):
    async def idle(*a):
        await asyncio.Event().wait()
    manager = JobManager(idle)
    monkeypatch.setattr(main, "job_manager", manager)
    sid = "sub_failed_regrade_link"
    store.save_artifact(sid, "job.json", {"id": sid, "status": "failed", "settings": {}, "teacher_key": "",
        "meta": {"filename": "work.png", "ocr_mode": "vision", "replaces": "sub_original",
                 "prefer_saved_answers": True}})
    (store.sub_dir(sid) / "original.png").write_bytes(image_bytes())
    with TestClient(main.app) as client:
        r = client.post(f"/api/submissions/{sid}/retry")
        assert r.status_code == 200
        assert manager.record(r.json()["id"])["meta"].get("replaces") == "sub_original"
