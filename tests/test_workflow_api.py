"""New workflow API contracts and durable answer review, without paid requests."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from test_jobs import image_bytes, isolated_store  # noqa: F401
from app import config, main, store
from app.jobs import JobManager


def idle_manager(monkeypatch):
    async def idle(*args):
        await asyncio.Event().wait()
    manager = JobManager(idle)
    monkeypatch.setattr(main, "job_manager", manager)
    return manager


def upload(client, **data):
    return client.post("/api/submissions", data=data,
                       files=[("files", ("paper.png", image_bytes(), "image/png"))])


def test_prepare_job_preserves_mode_and_has_no_student_archive(monkeypatch):
    manager = idle_manager(monkeypatch)
    with TestClient(main.app) as client:
        response = upload(client, mode="prepare", file_roles='["question_paper"]')
        assert response.status_code == 200
        record = manager.record(response.json()["id"])
        assert record["meta"]["mode"] == "prepare"
        assert record["meta"]["workflow_version"] == 2
        assert client.get(f"/api/submissions/{record['id']}/status").json()["mode"] == "prepare"
        assert upload(client, mode="unknown").status_code == 400
        assert upload(client, mode="prepare", student_id="someone").status_code == 400


def test_new_grading_requires_ocr_and_rejects_vision(monkeypatch):
    idle_manager(monkeypatch)
    with TestClient(main.app) as client:
        assert upload(client, ocr_mode="vision").status_code == 400
        cfg = {**config.load_settings(), "base_url": "https://example.com/anthropic"}
        monkeypatch.setattr(config, "load_settings", lambda: cfg)
        assert upload(client).status_code == 400
        # Preparing a paper does not run OCR, but still needs model credentials.
        assert upload(client, mode="prepare").status_code == 200


def test_selected_paper_is_validated_and_preserved_on_retry(monkeypatch):
    manager = idle_manager(monkeypatch)
    pid = store.save_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "20"}], "ai")
    with TestClient(main.app) as client:
        assert upload(client, paper_id="../../outside").status_code == 404
        response = upload(client, paper_id=pid)
        sid = response.json()["id"]
        assert manager.record(sid)["meta"]["paper_id"] == pid
    with TestClient(main.app) as client:
        response = client.post(f"/api/submissions/{sid}/retry")
        assert response.status_code == 200
        meta = manager.record(response.json()["id"])["meta"]
        assert meta["paper_id"] == pid and meta["workflow_version"] == 2
        assert meta["retry_of"] == sid


def test_manual_confirmation_without_answer_edit_increases_revision():
    qs = [{"id": "1", "question": "12+8="}]
    pid = store.save_paper("Quiz", qs, [{"id": "1", "answer": "20", "review_status": "review",
                                        "review_reason": "答案有争议", "teacher_answer": "21"}], "ai")
    with TestClient(main.app) as client:
        response = client.put(f"/api/papers/{pid}/answers", json={"answers": [{"id": "1", "answer": "20", "confirmed": True}]})
        assert response.status_code == 200
        paper = response.json()
        assert paper["rev"] == 1
        assert paper["answers"][0]["review_status"] == "manual_confirmed"
        assert paper["answers"][0]["teacher_answer"] == "21"
        assert paper["answers"][0]["edited"] is True
        assert client.put(f"/api/papers/{pid}/answers", json={"answers": [{"id": "1", "confirmed": True}]}).json()["rev"] == 1


def test_reviewed_paper_archives_reference_images_and_keeps_them_after_edit(tmp_path):
    page = tmp_path / "page.png"
    page.write_bytes(image_bytes())
    qs = [{"id": "1", "question": "12+8=", "source_page": 4, "reference_pages": [1]}]
    pid = store.save_reviewed_paper("Quiz", qs, [{"id": "1", "answer": "20", "review_status": "approved"}],
                                    "ai", reference_pages=[{"page": 4, "path": page}])
    paper = store.get_paper(pid)
    assert paper["workflow_version"] == 2
    assert paper["questions"][0]["source_page"] == 4
    assert paper["questions"][0]["reference_pages"] == [1]
    assert paper["reference_pages"][0]["page"] == 4
    archived = store.PAPERS_DIR / pid / "reference-pages" / "page-4.png"
    assert archived.read_bytes() == page.read_bytes()
    store.update_answers(pid, [{"id": "1", "answer": "21"}])
    changed = store.get_paper(pid)
    assert changed["reference_pages"] == paper["reference_pages"]
    assert changed["workflow_version"] == 2
    assert changed["answers"][0]["review_status"] == "manual_confirmed"


def test_preparation_results_do_not_enter_student_history():
    for sid, mode in (("sub_prep", "prepare"), ("sub_grade", "grade")):
        store.save_result(sid, {"id": sid, "mode": mode, "created_at": "2026-11-16", "filename": "a.png"})
    assert [r["id"] for r in store.all_results()] == ["sub_grade"]


def test_v2_cache_checks_workflow_paper_revision_and_archive():
    pid = store.save_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "20"}], "ai")
    key = store.workflow_file_key("hash", mode="grade", paper_id=pid)
    sid = "sub_cache2"
    result = {"id": sid, "mode": "grade", "paper_id": pid, "paper_rev": 0,
              "workflow_version": 2, "file_key": key, "class_id": None}
    store.save_result(sid, result)
    store.index_key(key, sid)
    assert store.find_workflow_result(key, mode="grade", paper_id=pid) == sid
    assert store.find_workflow_result(key, mode="prepare", paper_id=pid) is None
    store.save_result(sid, {**result, "workflow_version": 1})
    assert store.find_workflow_result(key, mode="grade", paper_id=pid) is None
    store.save_result(sid, {**result, "class_id": "other"})
    assert store.find_workflow_result(key, mode="grade", paper_id=pid) is None
    store.save_result(sid, result)
    store.update_answers(pid, [{"id": "1", "answer": "21"}])
    assert store.find_workflow_result(key, mode="grade", paper_id=pid) is None


def test_workflow_cache_key_can_pin_the_actually_graded_revision():
    pid = store.save_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "20"}], "ai")
    original = store.workflow_file_key("hash", paper_id=pid)
    store.update_answers(pid, [{"id": "1", "answer": "21"}])
    assert store.workflow_file_key("hash", paper_id=pid) != original
    assert store.workflow_file_key("hash", paper_id=pid, paper_rev=0) == original


def test_no_ocr_position_is_created_by_manual_override(monkeypatch):
    sid = "sub_override2"
    store.save_result(sid, {"id": sid, "workflow_version": 2, "page_count": 0,
                           "questions": [{"id": "1", "status": "review", "bbox_px": None, "bbox_ok": False, "draw": False}]})
    with TestClient(main.app) as client:
        response = client.post(f"/api/submissions/{sid}/override", json={"qid": "1", "status": "correct"})
        assert response.status_code == 200
        assert response.json()["questions"][0]["draw"] is False


def test_manual_reassignment_invalidates_cache_even_without_student_selection():
    store.save_roster({"classes": [{"id": "A", "name": "A"}],
                       "students": [{"id": "one", "class_id": "A"}, {"id": "two", "class_id": "A"}], "tasks": []})
    pid = store.save_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "20"}], "ai")
    key = store.workflow_file_key("hash", class_id="A")
    sid = "sub_reassigned"
    store.save_result(sid, {"id": sid, "mode": "grade", "workflow_version": 2, "file_key": key,
                           "paper_id": pid, "paper_rev": 0, "class_id": "A", "student_id": "one"})
    store.index_key(key, sid)
    assert store.find_workflow_result(key, class_id="A") == sid
    with TestClient(main.app) as client:
        assert client.post(f"/api/submissions/{sid}/assign", json={"student_id": "two"}).status_code == 200
    assert store.find_workflow_result(key, class_id="A") is None


def test_concurrent_paper_publications_keep_all_revisions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    pid = store.save_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "20"}], "ai")
    def publish(number):
        store.save_reviewed_paper("Quiz", [{"id": "1", "question": "12+8="}],
                                 [{"id": "1", "answer": str(number), "review_status": "approved"}], "ai", pid)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(publish, range(12)))
    paper = store.get_paper(pid)
    assert paper["rev"] == 12
    assert paper["reviewed_rev"] == 12


def test_paper_reader_never_observes_a_mixed_revision(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    import threading
    pid = store.save_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "20"}], "ai")
    metadata_written, release, reader_started = threading.Event(), threading.Event(), threading.Event()
    write = store._write
    def paused_write(path, value):
        write(path, value)
        if path.name == "paper.json" and value.get("rev") == 1:
            metadata_written.set()
            assert release.wait(2)
    monkeypatch.setattr(store, "_write", paused_write)
    def read():
        reader_started.set()
        return store.get_paper(pid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(store.save_paper, "Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "21"}], "ai", pid)
        assert metadata_written.wait(2)
        reading = pool.submit(read)
        assert reader_started.wait(2)
        try:
            snapshot = reading.result(timeout=.05)
        except TimeoutError:
            snapshot = None
        finally:
            release.set()
        publication.result(timeout=2)
        snapshot = snapshot or reading.result(timeout=2)
    assert snapshot["rev"] == 1 and snapshot["answers"][0]["answer"] == "21"


def test_invalid_reference_does_not_publish_answers(tmp_path):
    pid = store.save_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "20"}], "ai")
    with pytest.raises(ValueError):
        store.save_reviewed_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "21"}],
                                  "ai", pid, reference_pages=[{"page": 0, "path": tmp_path / "missing.png"}])
    paper = store.get_paper(pid)
    assert paper["rev"] == 0 and paper["answers"][0]["answer"] == "20"


def test_empty_answer_cannot_be_confirmed_or_reused_as_a_manual_key():
    pid = store.save_paper("Quiz", [{"id": "1", "question": "12+8="}], [{"id": "1", "answer": "20"}], "ai")
    with TestClient(main.app) as client:
        response = client.put(f"/api/papers/{pid}/answers", json={"answers": [{"id": "1", "answer": "", "confirmed": True}]})
        assert response.status_code == 400
    store.update_answers(pid, [{"id": "1", "answer": ""}])
    answer = store.get_paper(pid)["answers"][0]
    assert answer["review_status"] == "review" and answer["edited"] is False


@pytest.mark.parametrize('grade_confidence', [.99, .4])
def test_prepare_then_answer_card_uses_reviewed_paper_and_file_cache(monkeypatch, grade_confidence):
    import time
    from app import workflow, ocr
    calls = []
    async def agent(cfg, state):
        calls.append(state.mode)
        selected = state.paper
        question = {"id": "1", "type": "calc", "question": "12+8=",
                    "source_pages": [] if selected else [1], "reference_pages": [1] if selected else []}
        state.submit_source({"title": "Quiz", "materials": [{"page": 1, "role": "question_paper" if state.mode == "prepare" else "student_work"}],
                             "questions": [question]})
        if not state.review:
            state.submit_draft({"answers": [{"id": "1", "answer": "20", "confident": True}]})
            state.submit_review({"source_version": state.source_version, "draft_version": state.draft_version,
                                 "reviews": [{"id": "1", "status": "approved", "independent_answer": "20"}]})
        if state.mode == "grade":
            state.submit_grades({"source_version": state.source_version, "draft_version": state.draft_version,
                                 "results": [{"id": "1", "page": 1, "answer_state": "answered", "student_answer": "20",
                                              "verdict": "correct", "confidence": grade_confidence, "reason": "与已审核答案一致"}]})
            await state.ocr_page({"page": 1})
            state.submit_positions({"positions": [{"id": "1", "page": 1, "block_ids": ["p1:b1"]}]})
    async def recognize(*args, **kwargs):
        return {"pages": [{"finish_reason": "stop", "text": "<|ref|>text<|/ref|><|box|>[[300,300,400,340]]<|/box|>20"}]}
    monkeypatch.setattr(workflow, "run_workflow_agent", agent)
    monkeypatch.setattr(ocr, "recognize", recognize)
    monkeypatch.setattr(main, "job_manager", JobManager(workflow.run_workflow))
    def finish(client, sid):
        for _ in range(200):
            status = client.get(f"/api/submissions/{sid}/status").json()
            if status["done"]:
                assert status["status"] == "done", status
                return client.get(f"/api/submissions/{sid}").json()
            time.sleep(.005)
        pytest.fail("workflow did not finish")
    with TestClient(main.app) as client:
        prepared = finish(client, upload(client, mode="prepare", file_roles='["question_paper"]').json()["id"])
        pid = prepared["paper_id"]
        original_rev = store.get_paper(pid)["rev"]
        graded = finish(client, upload(client, paper_id=pid, file_roles='["student_work"]').json()["id"])
        if grade_confidence < .7:
            assert graded["questions"][0]["status"] == "review" and not graded["questions"][0]["draw"]
            graded = client.post(f"/api/submissions/{graded['id']}/override", json={"qid": "1", "status": "correct"}).json()
        assert graded["questions"][0]["draw"] and graded["summary"]["correct"] == 1
        assert graded["paper_cache_hit"] is True
        assert store.get_paper(pid)["rev"] == original_rev
        assert client.get(f"/api/submissions/{graded['id']}/pdf").content.startswith(b"%PDF")
        assert client.get(f"/api/papers/{pid}/pages/1").status_code == 200
        assert upload(client, paper_id=pid, file_roles='["student_work"]').json() == {"id": graded["id"], "cached": True}
        assert calls == ["prepare", "grade"]
        assert [r["id"] for r in store.all_results()] == [graded["id"]]
