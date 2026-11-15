"""OCR positions must come from selected source blocks, never model coordinates."""
import pytest
from PIL import Image

from test_jobs import image_bytes, isolated_store  # noqa: F401
from app import config, ocr, pipeline, prompts, store


def ocr_page():
    return {"pages": [{"text": (
        "<|ref|>text<|/ref|><|box|>[[100,200,300,240]]<|/box|>1. 12 + 8 =\n"
        "<|ref|>text<|/ref|><|box|>[[310,200,360,240]]<|/box|>20\n"
    )}]}


def parsed_question(**changes):
    return {"id": "1", "type": "calc", "question": "请计算：12 + 8 = ? 写出计算结果。",
            "student_answer": "20", "answer_state": "answered", "confidence": .99,
            "answer_bbox": [850, 850, 950, 950], "answer_block_ids": ["b2"], **changes}


@pytest.mark.asyncio
async def test_parser_references_numbered_ocr_blocks_and_ignores_generated_bbox(tmp_path, monkeypatch):
    page = tmp_path / "page.png"
    Image.new("RGB", (1000, 1000), "white").save(page)
    async def recognize(*args):
        return ocr_page()
    async def model(cfg, **kwargs):
        payload = str(kwargs["blocks"])
        assert "b2" in payload and "answer_block_ids" in kwargs["system"]
        return {"questions": [parsed_question()]}
    monkeypatch.setattr(ocr, "recognize", recognize)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    pages = {}
    _, questions, _ = await pipeline.parse_pages(config.load_settings(), [page], lambda *a, **k: None, pages)
    q = questions[0]
    assert q["bbox_norm"] == [310, 200, 360, 240]
    assert q["bbox_px"] == [310, 200, 360, 240]
    assert q["position_source"] == "ocr_blocks" and q["ocr_block_ids"] == ["b2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"answer_block_ids": []}, {"answer_block_ids": ["missing"]},
    {"answer_block_ids": "b2"}, {"answer_block_ids": ["b2", "missing"]},
    {"answer_block_ids": ["b1"]}, {"answer_state": "blank", "student_answer": ""},
    {"answer_state": "unreadable", "student_answer": ""},
])
async def test_missing_ocr_answer_uses_question_block_instead_of_model_answer_bbox(tmp_path, monkeypatch, changes):
    page = tmp_path / "page.png"
    Image.new("RGB", (1000, 1000), "white").save(page)
    async def recognize(*args):
        return ocr_page()
    async def model(*args, **kwargs):
        return {"questions": [parsed_question(**changes)]}
    monkeypatch.setattr(ocr, "recognize", recognize)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    _, questions, _ = await pipeline.parse_pages(config.load_settings(), [page], lambda *a, **k: None, {})
    expected = [310, 200, 360, 240] if changes.get("answer_state") else [100, 200, 300, 240]
    assert questions[0]["bbox_norm"] == expected
    assert questions[0]["bbox_px"] == expected
    assert not questions[0].get("position_note")


@pytest.mark.asyncio
async def test_one_ocr_block_cannot_anchor_two_questions(tmp_path, monkeypatch):
    page = tmp_path / "page.png"
    Image.new("RGB", (1000, 1000), "white").save(page)
    async def recognize(*args):
        return ocr_page()
    async def model(*args, **kwargs):
        return {"questions": [parsed_question(id="1"), parsed_question(id="2")]}
    monkeypatch.setattr(ocr, "recognize", recognize)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    _, questions, _ = await pipeline.parse_pages(config.load_settings(), [page], lambda *a, **k: None, {})
    assert questions[0]["bbox_px"] == [100, 200, 300, 240] and questions[0]["annotation_anchor"] == "question"
    assert questions[1]["bbox_px"] is None  # 本页没有第 2 题的题干，不借用第 1 题位置。


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,ocr_fails", [("deepseek_ocr", False), ("deepseek_ocr", True), ("vision", False)])
async def test_pipeline_ocr_skips_review_but_vision_keeps_it(monkeypatch, mode, ocr_fails):
    sid = "sub_ocr_position"
    d = store.sub_dir(sid)
    d.mkdir()
    orig = d / "original.png"
    orig.write_bytes(image_bytes())
    stages, events = [], []
    async def recognize(*args):
        if ocr_fails:
            raise ocr.OcrError("timeout")
        return ocr_page()
    async def model(cfg, *, system, blocks, **kwargs):
        stages.append(kwargs.get("stage"))
        if system.startswith(prompts.PARSER):
            return {"questions": [parsed_question()]}
        if system == prompts.SOLVER:
            return {"answers": [{"id": "1", "answer": "20", "confident": True}]}
        return {"reviews": [{"id": "1", "bbox_ok": True, "matches": True}]}
    monkeypatch.setattr(ocr, "recognize", recognize)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    meta = {"original": str(orig), "filename": "a.png", "ocr_mode": mode, "created_at": "now", "file_key": "key"}
    result = await pipeline.run_pipeline(sid, config.load_settings(), meta,
        lambda stage, status, message="", **extra: events.append({"stage": stage, "status": status, "message": message, **extra}))
    q = result["questions"][0]
    assert q["verdict"] == "correct" and q["status"] == "correct"
    if mode == "deepseek_ocr":
        assert "review" not in stages
        assert result["position_method"] == "ocr_blocks"
        assert any(e.get("title") == "OCR 位置检查" for e in events)
        assert q["draw"] is (not ocr_fails)
        if not ocr_fails:
            assert q["bbox_norm"] == [310, 200, 360, 240] and not q.get("ocr_confirmed")
        else:
            assert q["bbox_px"] is None and "复核" not in q["position_note"]
    else:
        assert "review" in stages and result["position_method"] == "visual_review"
        assert q["draw"] and q["bbox_norm"] == [850, 850, 950, 950]
    assert (d / "annotated/page-1.png").exists()


def test_selected_ocr_text_crosscheck_ignores_unselected_overlapping_print():
    blocks = [{"id": "b1", "text": "题干文字", "box": [310, 200, 360, 240]},
              {"id": "b2", "text": "20", "box": [310, 200, 360, 240]}]
    q = {"id": "1", "page": 1, "position_source": "ocr_blocks", "ocr_block_ids": ["b2"],
         "bbox_norm": [310, 200, 360, 240], "student_answer": "20", "answer_state": "answered", "reasons": []}
    pipeline.ocr_crosscheck([q], {1: {"blocks": blocks}})
    assert not q.get("ocr_confirmed") and q["reasons"] == []
    q["student_answer"] = "21"
    pipeline.ocr_crosscheck([q], {1: {"blocks": blocks}})
    assert not q.get("ocr_confirmed") and q["reasons"] == []


def test_old_ocr_results_do_not_bypass_new_position_pipeline():
    pid = store.save_paper("试卷", [], [], "ai")
    result = {"id": "sub_old_ocr", "paper_id": pid, "paper_rev": 0, "ocr_mode": "deepseek_ocr"}
    store.save_result(result["id"], result)
    store.index_key(store.file_key("hash", ocr_mode="deepseek_ocr"), result["id"])
    assert store.find_by_file("hash", ocr_mode="deepseek_ocr") is None
    result["position_method"] = "ocr_blocks"
    store.save_result(result["id"], result)
    assert store.find_by_file("hash", ocr_mode="deepseek_ocr") == result["id"]


def test_numbered_ocr_blocks_reject_invalid_coordinates():
    text = ''.join(f"<|ref|>text<|/ref|><|box|>[[{box}]]<|/box|>answer\n"
                   for box in ("10,20,30,40", "-1,20,30,40", "10,20,1001,40", "NaN,20,30,40", "0,0,1,1"))
    blocks = ocr.parse_blocks(text)
    assert len(blocks) == 1 and blocks[0]["id"] == "b1"
    assert blocks[0]["box"] == [10, 20, 30, 40]


def test_running_and_completed_job_status_exposes_position_method():
    from app.jobs import JobManager
    manager = JobManager(None)
    sid = "sub_position_status"
    store.save_artifact(sid, "job.json", {"id": sid, "status": "running", "created_at": 1,
        "meta": {"filename": "x.png", "ocr_mode": "deepseek_ocr", "position_method": "ocr_blocks"}})
    assert manager.status(sid)["position_method"] == "ocr_blocks"
    store.save_result("sub_legacy_status", {"id": "sub_legacy_status", "filename": "old.png", "position_method": "ocr_blocks"})
    assert manager.status("sub_legacy_status")["position_method"] == "ocr_blocks"


def test_position_stage_title_survives_snapshot_and_restart():
    from app.jobs import JobManager
    sid = "sub_stage_title"
    store.save_artifact(sid, "job.json", {"id": sid, "status": "done", "meta": {"filename": "x.png"}})
    manager = JobManager(None)
    manager.emit(sid, "review", "done", "OCR 坐标检查完成", title="OCR 位置检查")
    restored = JobManager(None)
    restored.recover()
    assert restored.status(sid)["stages"]["review"]["title"] == "OCR 位置检查"


@pytest.mark.asyncio
async def test_multiple_ocr_blocks_union_and_page_local_ids(tmp_path, monkeypatch):
    pages = [tmp_path / f"page-{i}.png" for i in (1, 2)]
    for page in pages:
        Image.new("RGB", (1000, 1000), "white").save(page)
    async def recognize(*args):
        return {"pages": [{"text": (
            "<|ref|>text<|/ref|><|box|>[[300,200,320,240]]<|/box|>2\n"
            "<|ref|>text<|/ref|><|box|>[[325,200,345,240]]<|/box|>0\n")}]}
    async def model(*args, **kwargs):
        return {"questions": [parsed_question(answer_block_ids=["b2", "b1", "b2"])]}
    monkeypatch.setattr(ocr, "recognize", recognize)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    _, questions, _ = await pipeline.parse_pages(config.load_settings(), pages, lambda *a, **k: None, {})
    assert len(questions) == 2 and questions[0]["id"] != questions[1]["id"]
    assert all(q["bbox_norm"] == [300, 200, 345, 240] and q["bbox_ok"] for q in questions)
    assert all(q["ocr_block_ids"] == ["b1", "b2"] for q in questions)


def test_ocr_upload_api_keeps_unlocated_grade_and_exports_pdf(monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from app import main
    from app.jobs import JobManager
    calls = []
    async def recognize(*args):
        return ocr_page()
    async def model(*args, **kwargs):
        calls.append(kwargs["stage"])
        if kwargs["stage"] == "parse":
            return {"questions": [parsed_question(), parsed_question(id="2", question="请计算：15 - 6 = ? 写出计算结果。",
                                                                     student_answer="8", answer_block_ids=[])]}
        if kwargs["stage"] == "solve":
            return {"answers": [{"id": "1", "answer": "20"}, {"id": "2", "answer": "9"}]}
        return {"results": [{"id": "2", "verdict": "wrong", "confidence": .99}]}
    monkeypatch.setattr(ocr, "recognize", recognize)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    monkeypatch.setattr(main, "job_manager", JobManager(pipeline.run_pipeline))
    with TestClient(main.app) as client:
        sid = client.post("/api/submissions", files={"file": ("a.png", image_bytes(), "image/png")},
                          data={"ocr_mode": "deepseek_ocr"}).json()["id"]
        for _ in range(100):
            status = client.get(f"/api/submissions/{sid}/status").json()
            assert status["position_method"] == "ocr_blocks"
            if status["done"]:
                break
            time.sleep(.01)
        assert status["status"] == "done"
        result = client.get(f"/api/submissions/{sid}").json()
        first, second = result["questions"]
        assert first["draw"] and first["status"] == "correct"
        assert second["status"] == "wrong" and not second["draw"] and second["position_note"]
        assert "review" not in calls
        assert status["stages"]["review"]["title"] == "OCR 位置检查"
        assert client.get(f"/api/submissions/{sid}/pdf").content.startswith(b"%PDF")
        cached = client.post("/api/submissions", files={"file": ("a.png", image_bytes(), "image/png")},
                             data={"ocr_mode": "deepseek_ocr"}).json()
        assert cached == {"id": sid, "cached": True}


def test_mock_model_selects_actual_ocr_blocks(monkeypatch):
    import json
    import mock_llm
    from fastapi.testclient import TestClient
    monkeypatch.setitem(mock_llm.STATE, "n", 0)
    with TestClient(mock_llm.app) as client:
        obj = client.post("/v1/tools/ocr", files={"file": ("a.png", image_bytes(), "image/png")},
                          data={"purpose": "rich_layout_recognition"}).json()
    blocks = ocr.parse_blocks(obj["pages"][0]["text"])
    data = json.loads(mock_llm.reply_for({"system": prompts.PARSER + prompts.OCR_PARSE_HINT, "messages": []})["text"])
    for q in data["questions"]:
        selected = ocr.answer_blocks(q["answer_block_ids"], blocks)
        assert selected and store.norm_answer(ocr.block_text(selected)) == store.norm_answer(q["student_answer"])
        assert q["answer_bbox"] is None


def test_adjacent_answer_fragments_keep_ocr_reading_order_despite_different_tops():
    blocks = [{"id": "b1", "text": "2", "box": [300, 205, 320, 240]},
              {"id": "b2", "text": "0", "box": [325, 200, 345, 240]}]
    position = pipeline._ocr_position(["b2", "b1"], blocks, "20", "answered")
    assert position["bbox_norm"] == [300, 200, 345, 240]
    q = {"id": "1", "page": 1, "answer_state": "answered", "student_answer": "20", "reasons": [], **position}
    pipeline.ocr_crosscheck([q], {1: {"blocks": blocks}})
    assert not q.get("ocr_confirmed") and q["reasons"] == []
