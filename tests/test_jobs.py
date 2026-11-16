"""Background jobs use temporary files and a controllable pipeline, never a paid model."""
import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="grader-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from app import config, store, main


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    for name in ("SUBS_DIR", "PAPERS_DIR"):
        p = tmp_path / name.lower()
        p.mkdir()
        monkeypatch.setattr(store, name, p)
    monkeypatch.setattr(store, "FILE_INDEX", tmp_path / "index.json")
    monkeypatch.setattr(store, "ROSTER", tmp_path / "roster.json")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "load_settings", lambda: {
        "model": "deepseek-flash", "base_url": "https://api.deepseek.com/anthropic",
        "api_key": "test-secret", "agentic_zoom": False, "use_ocr": True})


def image_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), "white").save(buf, "PNG")
    return buf.getvalue()


def test_raw_image_is_available_before_final_result():
    sid = "sub_preview"
    d = store.sub_dir(sid)
    (d / "pages").mkdir(parents=True)
    store.save_artifact(sid, "job.json", {"id": sid, "status": "running", "meta": {"filename": "a.png"}})
    (d / "pages/page-1.png").write_bytes(image_bytes())
    with TestClient(main.app) as client:
        response = client.get(f"/api/submissions/{sid}/pages/1?kind=raw")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert client.get(f"/api/submissions/{sid}/pages/1?kind=bad").status_code == 400
        assert client.get(f"/api/submissions/{sid}/pages/0?kind=raw").status_code == 400


@pytest.mark.asyncio
async def test_detached_job_replays_progress_and_does_not_store_credentials():
    from app.jobs import JobManager
    entered, release = asyncio.Event(), asyncio.Event()

    async def pipeline(sid, cfg, meta, emit, teacher_key):
        emit("preprocess", "done", "1 页", page_count=1)
        entered.set()
        await release.wait()
        store.save_result(sid, {"id": sid, "filename": meta["filename"]})

    manager = JobManager(pipeline, max_parallel=1)
    sid = "sub_detached"
    manager.start(sid, config.load_settings(), {"filename": "a.png", "ocr_mode": "vision"}, "1. 20")
    await entered.wait()
    assert manager.status(sid)["status"] == "running"
    assert manager.status(sid)["page_count"] == 1
    events = manager.events_after(sid, 0)
    cursor = events[-1]["id"]
    release.set()
    await manager.tasks[sid]
    assert manager.status(sid)["status"] == "done"
    assert all(e["id"] > cursor for e in manager.events_after(sid, cursor))
    persisted = (store.sub_dir(sid) / "job.json").read_text()
    assert "test-secret" not in persisted and '"api_key"' not in persisted
    assert json.loads(persisted)["teacher_key"] == "1. 20"
    restored = JobManager(pipeline)
    restored.recover()
    assert restored.status(sid)["status"] == "done"
    assert restored.events_after(sid, 0) == manager.events_after(sid, 0)


@pytest.mark.asyncio
async def test_queue_and_restart_mark_interrupted_without_reexecution():
    from app.jobs import JobManager
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def pipeline(sid, cfg, meta, emit, teacher_key):
        calls.append(sid)
        entered.set()
        await release.wait()

    manager = JobManager(pipeline, max_parallel=1)
    for sid in ("sub_first", "sub_second"):
        manager.start(sid, config.load_settings(), {"filename": sid}, "")
    await entered.wait()
    assert manager.status("sub_second")["status"] == "queued"
    recovered = JobManager(pipeline)
    recovered.recover()
    assert {j["status"] for j in recovered.list(status="all")} == {"interrupted"}
    assert calls == ["sub_first"]
    release.set()
    await asyncio.gather(*list(manager.tasks.values()))


def test_ocr_mode_separates_file_cache():
    qs = [{"id": "1", "question": "12 + 8 = ?"}]
    pid = store.save_paper("paper", qs, [{"id": "1", "answer": "20"}], "ai")
    sid = "sub_cached"
    store.save_result(sid, {"id": sid, "paper_id": pid, "paper_rev": 0, "ocr_mode": "vision"})
    store.index_key(store.file_key("hash", "", "", "vision"), sid)
    assert store.find_by_file("hash", "", "", "vision") == sid
    assert store.find_by_file("hash", "", "", "deepseek_ocr") is None


def test_submission_mode_and_interrupted_retry(monkeypatch):
    from app.jobs import JobManager
    async def noop(*args):
        await asyncio.sleep(60)
    manager = JobManager(noop)
    monkeypatch.setattr(main, "job_manager", manager)
    with TestClient(main.app) as client:
        r = client.post("/api/submissions", files={"file": ("a.png", image_bytes(), "image/png")},
                        data={"ocr_mode": "deepseek_ocr", "answer_key": "1. 20"})
        assert r.status_code == 200
        sid = r.json()["id"]
        status = client.get(f"/api/submissions/{sid}/status").json()
        assert status["ocr_mode"] == "deepseek_ocr" and status["effort"] == "high"
        assert client.post(f"/api/submissions/{sid}/retry").status_code == 409
    # Lifespan shutdown cancels owned jobs and marks them interrupted.
    with TestClient(main.app) as client:
        assert client.get(f"/api/submissions/{sid}/status").json()["status"] == "interrupted"
        r = client.post(f"/api/submissions/{sid}/retry")
        assert r.status_code == 200 and r.json()["id"] != sid
        new = manager.record(r.json()["id"])
        assert new["teacher_key"] == "1. 20" and new["meta"]["ocr_mode"] == "deepseek_ocr"


def test_sse_cursor_and_finished_history(monkeypatch):
    from app.jobs import JobManager
    manager = JobManager(None)
    monkeypatch.setattr(main, "job_manager", manager)
    sid = "sub_events"
    store.save_artifact(sid, "job.json", {"id": sid, "status": "interrupted", "meta": {"filename": "x"},
                                         "created_at": 1, "finished_at": 2, "last_event_id": 2})
    (store.sub_dir(sid) / "events.jsonl").write_text(
        '\n'.join(json.dumps({"id": i, "stage": "parse" if i == 1 else "interrupted", "status": "info"}) for i in (1, 2)) + '\n')
    with TestClient(main.app) as client:
        r = client.get(f"/api/submissions/{sid}/events?after=1")
        assert "id: 2\n" in r.text and "id: 1\n" not in r.text
        assert client.get("/api/jobs?status=all").json()[0]["id"] == sid


def test_restart_preserves_cursor_after_unsnapshotted_output_and_partial_line():
    from app.jobs import JobManager
    sid = "sub_crash"
    manager = JobManager(None)
    store.save_artifact(sid, "job.json", {"id": sid, "status": "running", "meta": {"filename": "x"},
                                         "created_at": 1, "last_event_id": 1})
    path = store.sub_dir(sid) / "events.jsonl"
    path.write_text(json.dumps({"id": 2, "stage": "parse", "kind": "output", "text": "partial"}) + '\n{"id":3')
    manager.recover()
    tail = manager.events_after(sid, 2)
    assert len(tail) == 1 and tail[0]["stage"] == "interrupted"
    assert tail[0]["id"] > 2


def test_legacy_pipeline_finishes_after_preview_and_can_reopen(monkeypatch):
    import time
    from app import pipeline, prompts
    from app.jobs import JobManager
    gate = {}
    async def model(cfg, *, system, blocks, **kwargs):
        if system.startswith(prompts.PARSER):
            gate["release"] = asyncio.Event()
            await gate["release"].wait()
            return {"title": "数学测验", "questions": [{"id": "1", "question": "12 + 8 = ? Please calculate.",
                     "student_answer": "20", "answer_state": "answered", "answer_bbox": [300, 300, 400, 340], "confidence": .99}]}
        if system == prompts.SOLVER:
            return {"answers": [{"id": "1", "answer": "20", "confident": True}]}
        return {"reviews": [{"id": "1", "bbox_ok": True, "matches": True, "read_answer": "20"}]}
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    monkeypatch.setattr(pipeline.ocr, "enabled", lambda cfg: False)
    manager = JobManager(pipeline.run_pipeline)
    monkeypatch.setattr(main, "job_manager", manager)
    with TestClient(main.app) as client:
        sid = client.post("/api/submissions", files={"file": ("a.png", image_bytes(), "image/png")},
                          data={"ocr_mode": "deepseek_ocr"}).json()["id"]
        for _ in range(100):
            if "release" in gate:
                break
            time.sleep(.01)
        assert client.get(f"/api/submissions/{sid}/pages/1?kind=raw").status_code == 200
        assert client.get(f"/api/submissions/{sid}").status_code == 404
        assert client.get("/api/jobs").json()[0]["id"] == sid
        client.portal.call(gate["release"].set)
        for _ in range(100):
            if client.get(f"/api/submissions/{sid}/status").json()["done"]:
                break
            time.sleep(.01)
        result = client.get(f"/api/submissions/{sid}").json()
        assert result["summary"]["correct"] == 1 and result["effort"] == "high"
        assert result["timings"]["review"] >= 0 and result["ocr_mode"] == "deepseek_ocr"
        assert client.get(f"/api/submissions/{sid}/pdf").content.startswith(b"%PDF")
        assert client.get(f"/api/submissions/{sid}/pages/1").status_code == 200
        assert client.get("/api/jobs").json() == []
        assert '"stage": "done"' in client.get(f"/api/submissions/{sid}/events").text
        cached = client.post("/api/submissions", files={"file": ("a.png", image_bytes(), "image/png")},
                             data={"ocr_mode": "deepseek_ocr"}).json()
        # Legacy output remains readable but cannot bypass native answer review.
        assert cached["cached"] is False and cached["id"] != sid


@pytest.mark.asyncio
async def test_stream_drains_terminal_appended_while_consumer_reads():
    from app.jobs import JobManager
    sid = "sub_live_tail"
    manager = JobManager(None)
    store.save_artifact(sid, "job.json", {"id": sid, "status": "running", "meta": {"filename": "x"}, "last_event_id": 0})
    manager.emit(sid, "parse", "start", "读取中")
    stream = manager.stream(sid)
    assert "id: 1" in await anext(stream)
    manager.record(sid)["status"] = "done"
    manager.emit(sid, "done", "done", "批改完成")
    assert '"stage": "done"' in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


def test_retry_restores_saved_ocr_provider_before_checking_availability(monkeypatch):
    from app.jobs import JobManager
    async def noop(*args):
        await asyncio.sleep(60)
    manager = JobManager(noop)
    monkeypatch.setattr(main, "job_manager", manager)
    sid = "sub_saved_provider"
    store.save_artifact(sid, "job.json", {"id": sid, "status": "failed", "created_at": 1,
        "meta": {"filename": "x.png", "ocr_mode": "deepseek_ocr"}, "teacher_key": "1. 20",
        "settings": {"model": "deepseek-flash", "base_url": "https://api.deepseek.com/anthropic", "use_ocr": True}})
    (store.sub_dir(sid) / "original.png").write_bytes(image_bytes())
    monkeypatch.setattr(config, "load_settings", lambda: {"model": "other", "api_key": "current-secret", "base_url": "https://other.example.com"})
    with TestClient(main.app) as client:
        response = client.post(f"/api/submissions/{sid}/retry")
        assert response.status_code == 200
        new = manager.record(response.json()["id"])
        assert new["settings"]["base_url"] == "https://api.deepseek.com/anthropic"
        assert "current-secret" not in str(new)


def test_same_file_uploaded_to_different_class_does_not_reuse_other_class(monkeypatch):
    from app.jobs import JobManager
    async def noop(*args):
        await asyncio.sleep(60)
    monkeypatch.setattr(main, "job_manager", JobManager(noop))
    store.save_roster({"classes": [{"id": "A", "name": "A班"}, {"id": "B", "name": "B班"}],
                       "students": [], "tasks": []})
    pid = store.save_paper("试卷", [{"id": "1", "question": "12 + 8 = ?"}], [{"id": "1", "answer": "20"}], "ai")
    sid, file_hash = "sub_classA", store.sha256_bytes(image_bytes())
    cache_key = store.workflow_file_key(file_hash, class_id="A")
    store.save_result(sid, {"id": sid, "filename": "a.png", "class_id": "A", "ocr_mode": "deepseek_ocr",
                           "mode": "grade", "workflow_version": 2, "file_key": cache_key,
                           "paper_id": pid, "paper_rev": 0, "superseded": False})
    store.index_key(cache_key, sid)
    with TestClient(main.app) as client:
        hit = client.post("/api/submissions", files={"file": ("a.png", image_bytes(), "image/png")},
                          data={"class_id": "A", "ocr_mode": "deepseek_ocr"}).json()
        assert hit == {"id": sid, "cached": True}
        other = client.post("/api/submissions", files={"file": ("a.png", image_bytes(), "image/png")},
                            data={"class_id": "B", "ocr_mode": "deepseek_ocr"}).json()
        assert other["cached"] is False and other["id"] != sid
        status = client.get(f"/api/submissions/{other['id']}/status").json()
        assert status["class_id"] == "B"


def test_new_cache_keys_keep_both_class_results_and_validate_reassignment():
    pid = store.save_paper("试卷", [{"id": "1", "question": "12 + 8 = ?"}], [], "ai")
    for cid in ("A", "B"):
        sid = f"sub_class{cid}"
        store.save_result(sid, {"id": sid, "class_id": cid, "ocr_mode": "vision", "paper_id": pid, "paper_rev": 0})
        store.index_key(store.file_key("hash", "", "", "vision", cid), sid)
    assert store.find_by_file("hash", "", "", "vision", "A") == "sub_classA"
    assert store.find_by_file("hash", "", "", "vision", "B") == "sub_classB"
    moved = store.get_result("sub_classA")
    moved["class_id"] = "B"
    store.save_result("sub_classA", moved)
    assert store.find_by_file("hash", "", "", "vision", "A") is None


def test_regrade_preserves_current_manual_student_assignment(monkeypatch):
    from app.jobs import JobManager
    async def noop(*args):
        await asyncio.sleep(60)
    manager = JobManager(noop)
    monkeypatch.setattr(main, "job_manager", manager)
    sid = "sub_manually_assigned"
    store.save_roster({"classes": [{"id": "A", "name": "A"}, {"id": "B", "name": "B"}],
                       "students": [{"id": "studentB", "class_id": "B", "name": "乙"}], "tasks": []})
    store.save_artifact(sid, "job.json", {"id": sid, "status": "done", "created_at": 1,
        "meta": {"filename": "x.png", "class_id": "A", "student_id": "", "ocr_mode": "vision"}})
    store.save_result(sid, {"id": sid, "filename": "x.png", "class_id": "A", "student_id": None,
                           "ocr_mode": "vision"})
    (store.sub_dir(sid) / "original.png").write_bytes(image_bytes())
    with TestClient(main.app) as client:
        assigned = client.post(f"/api/submissions/{sid}/assign", json={"student_id": "studentB"}).json()
        assert assigned["class_id"] == "B"
        response = client.post(f"/api/submissions/{sid}/regrade")
        assert response.status_code == 200
        new = manager.record(response.json()["id"])
        assert new["meta"]["class_id"] == "B" and new["meta"]["student_id"] == "studentB"
