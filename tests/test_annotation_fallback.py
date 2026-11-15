"""Regression coverage for direct OCR marking and question-side fallback."""
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from test_jobs import isolated_store  # noqa: F401
from app import main, pipeline, store


def blocks():
    return [{"id": "b1", "text": "1. 12 + 8 =", "box": [100, 200, 300, 240]},
            {"id": "b2", "text": r"$$ \frac{680}{785} $$", "box": [310, 200, 390, 250]}]


def question(**extra):
    return {"id": "1", "page": 1, "question": "12 + 8 =", "student_answer": "680/785",
            "answer_state": "answered", "parse_confidence": .99, "position_source": "ocr_blocks",
            "ocr_block_ids": ["b2"], "reasons": [], "verdict": "correct", "status": "correct", **extra}


def test_latex_ocr_difference_does_not_remove_coordinate_or_require_review():
    position = pipeline._ocr_position(["b2"], blocks(), "680/785", "answered")
    assert position["bbox_norm"] == [310, 200, 390, 250]
    q = question(**position, bbox_px=[310, 200, 390, 250])
    pipeline.ocr_crosscheck([q], {1: {"blocks": blocks()}})
    pipeline.finalize([q])
    assert q["status"] == "correct" and q["draw"] and q["reasons"] == []


@pytest.mark.asyncio
async def test_no_answer_block_marks_next_to_question_block(tmp_path, monkeypatch):
    page = tmp_path / "page.png"
    Image.new("RGB", (1000, 1000), "white").save(page)
    async def model(*args, **kwargs):
        return {"questions": [{"id": "1", "question": "12 + 8 =", "student_answer": "20", "confidence": .99,
                              "answer_state": "answered", "answer_block_ids": [], "question_block_ids": ["b1"],
                              "answer_bbox": None}]}
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    _, qs, _ = await pipeline.parse_pages({"ocr_mode": "deepseek_ocr", "use_ocr": False}, [page], lambda *a, **k: None,
                                         {1: {"blocks": blocks()}})
    q = qs[0]
    assert q["bbox_norm"] == [100, 200, 300, 240] and q["annotation_anchor"] == "question"
    q.update(verdict="wrong", reasons=[])
    pipeline.finalize(qs)
    assert q["status"] == "wrong" and q["draw"]


@pytest.mark.asyncio
async def test_ocr_failure_uses_question_bbox_without_calling_review(tmp_path, monkeypatch):
    page = tmp_path / "page.png"
    Image.new("RGB", (2000, 1000), "white").save(page)
    async def model(*args, **kwargs):
        return {"questions": [{"id": "1", "question": "12+8=", "student_answer": "20", "confidence": .99,
                              "answer_block_ids": [], "question_block_ids": [],
                              "answer_bbox": [800, 800, 900, 900], "question_bbox": [100, 200, 300, 240]}]}
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    _, qs, _ = await pipeline.parse_pages({"ocr_mode": "deepseek_ocr", "use_ocr": False}, [page], lambda *a, **k: None, {})
    assert qs[0]["bbox_px"] == [200, 200, 600, 240]
    assert qs[0]["annotation_anchor"] == "question" and qs[0]["question_position_source"] == "parser"


@pytest.mark.asyncio
async def test_shared_answer_block_falls_back_to_each_own_question(tmp_path, monkeypatch):
    page = tmp_path / "page.png"
    Image.new("RGB", (1000, 1000), "white").save(page)
    source = blocks() + [{"id": "b3", "text": "2. 15 - 6 =", "box": [100, 400, 300, 440]}]
    async def model(*args, **kwargs):
        return {"questions": [{"id": str(i), "question": "题干", "student_answer": "20", "confidence": .99,
                              "answer_block_ids": ["b2"], "question_block_ids": [stem]}
                             for i, stem in ((1, "b1"), (2, "b3"))]}
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    _, qs, _ = await pipeline.parse_pages({"ocr_mode": "deepseek_ocr", "use_ocr": False}, [page], lambda *a, **k: None,
                                         {1: {"blocks": source}})
    assert [q["bbox_norm"] for q in qs] == [[100, 200, 300, 240], [100, 400, 300, 440]]
    assert all(q["annotation_anchor"] == "question" for q in qs)


def test_no_question_match_does_not_use_another_questions_coordinates():
    position = pipeline._ocr_position([], blocks(), "9", "answered", {"question": "15 - 6 ="})
    assert position["bbox_norm"] is None


@pytest.mark.parametrize("question_text,other_stem", [
    ("1.2 + 3 =", "2. 12 + 3 ="), ("2 + 3 =", "2. 12 + 3 ="),
    ("2 + 3 =", "2. 1.2 + 3 ="),
    ("12 + 3 =", "2. -12 + 3 ="), ("a + b =", "2. -a + b ="),
    ("2 + 3 =", "2. -(2 + 3) ="),
    ("2 + 3 =", "2. √2 + 3 ="), ("2 + 3 =", "2. π2 + 3 ="),
    ("2 + 3 =", "2. ∛2 + 3 ="), ("2 + 3 =", "2. 2 + 3 的相反数 ="),
])
def test_question_match_preserves_decimal_and_number_boundaries(question_text, other_stem):
    source = [{"id": "b1", "text": other_stem, "box": [600, 700, 800, 750]}]
    position = pipeline._ocr_position([], source, "", "blank", {"question": question_text})
    assert position["bbox_norm"] is None


def test_saved_question_bbox_takes_priority_over_heuristic_text_match():
    position = pipeline._ocr_position([], blocks(), "20", "answered", {
        "question": "12+8=", "question_bbox_norm": [100, 100, 300, 150]})
    assert position["bbox_norm"] == [100, 100, 300, 150]


def test_repeated_question_text_does_not_override_different_printed_question_number():
    position = pipeline._ocr_position([], blocks(), "20", "answered", {"id": "2", "question": "12+8="})
    assert position["bbox_norm"] is None


def seed_old_result(sid="sub_mark_repair", **changes):
    d = store.sub_dir(sid)
    (d / "pages").mkdir(parents=True)
    Image.new("RGB", (1000, 1000), "white").save(d / "pages/page-1.png")
    old = dict(bbox_norm=None, bbox_px=None, bbox_ok=False, draw=False, status="review",
               reasons=[r"OCR 在答案位置读到「$$ \frac{680}{785} $$」，与抄录不一致"],
               position_note="OCR 文字与学生答案不一致，未在卷面落笔")
    old.update(changes)
    result = {"id": sid, "filename": "test.png", "ocr_mode": "deepseek_ocr", "position_method": "ocr_blocks",
              "page_count": 1, "questions": [question(**old)]}
    store.save_result(sid, result)
    store.save_artifact(sid, "ocr.json", {"1": {"blocks": blocks()}})
    return result


def red_pixels(path):
    with Image.open(path) as image:
        return sum(1 for pixel in image.convert("RGB").get_flattened_data() if pixel[0] > 150 and pixel[1] < 100 and pixel[2] < 100)


def test_open_existing_result_repairs_marks_and_removes_only_old_ocr_reason():
    result = seed_old_result()
    with TestClient(main.app) as client:
        r = client.get(f"/api/submissions/{result['id']}")
        assert r.status_code == 200
        data = r.json()
        q = data["questions"][0]
        assert q["draw"] and q["status"] == "correct" and q["reasons"] == []
        assert not q.get("position_note")
        assert data["summary"]["correct"] == 1
        assert data["annotation_version"] >= 2
        assert red_pixels(store.sub_dir(result["id"]) / "annotated/page-1.png") > 0
        assert client.get(f"/api/submissions/{result['id']}/pdf").content.startswith(b"%PDF")


def test_annotated_page_request_alone_repairs_old_result():
    result = seed_old_result()
    with TestClient(main.app) as client:
        r = client.get(f"/api/submissions/{result['id']}/pages/1")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        saved = store.get_result(result["id"])
        assert saved["annotation_version"] >= 2 and saved["questions"][0]["draw"]


def test_legacy_ocr_blocks_without_ids_can_recover_question_position():
    result = seed_old_result(ocr_block_ids=[], student_answer="20")
    source = [{k: v for k, v in b.items() if k != "id"} for b in blocks()]
    store.save_artifact(result["id"], "ocr.json", {"1": {"blocks": source}})
    with TestClient(main.app) as client:
        q = client.get(f"/api/submissions/{result['id']}").json()["questions"][0]
        assert q["draw"] and q["annotation_anchor"] == "question"


@pytest.mark.parametrize("status", ["correct", "wrong"])
def test_manual_override_recovers_question_position_and_renders_mark(status):
    result = seed_old_result(ocr_block_ids=[], student_answer="20", reasons=[], status="correct")
    with TestClient(main.app) as client:
        r = client.post(f"/api/submissions/{result['id']}/override", json={"qid": "1", "status": status})
        assert r.status_code == 200
        q = r.json()["questions"][0]
        assert q["overridden"] and q["status"] == status and q["draw"]
        assert q["annotation_anchor"] == "question" and q["bbox_norm"] == [100, 200, 300, 240]
        assert red_pixels(store.sub_dir(result["id"]) / "annotated/page-1.png") > 0


def test_repair_preserves_other_review_reasons_and_manual_decision():
    result = seed_old_result(reasons=["OCR 在答案位置读到「x」，与抄录不一致", "批改把握不足"])
    with TestClient(main.app) as client:
        q = client.get(f"/api/submissions/{result['id']}").json()["questions"][0]
        assert q["reasons"] == ["批改把握不足"] and q["status"] == "review"
        r = store.get_result(result["id"])
        r["annotation_version"] = 0
        r["questions"][0].update(overridden=True, status="wrong")
        store.save_result(result["id"], r)
        q = client.get(f"/api/submissions/{result['id']}").json()["questions"][0]
        assert q["overridden"] and q["status"] == "wrong"
