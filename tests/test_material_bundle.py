"""Multi-file inputs remain one job; reference pages never become student grades."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from test_jobs import image_bytes, isolated_store  # noqa: F401
from app import config, main, materials, pipeline, prompts, store
from app.jobs import JobManager


def idle_manager(monkeypatch):
    async def idle(*args):
        await asyncio.Event().wait()
    manager = JobManager(idle)
    monkeypatch.setattr(main, "job_manager", manager)
    return manager


def test_multiple_files_default_to_ocr_and_retry_keeps_all_inputs(monkeypatch):
    manager = idle_manager(monkeypatch)
    with TestClient(main.app) as client:
        r = client.post("/api/submissions", files=[("files", ("paper.png", image_bytes(), "image/png")),
                                                 ("files", ("work.png", image_bytes(), "image/png"))],
                        data={"file_roles": json.dumps(["question_paper", "student_work"])})
        assert r.status_code == 200
        sid = r.json()["id"]
        inputs = manager.record(sid)["meta"]["inputs"]
        assert [x["role"] for x in inputs] == ["question_paper", "student_work"]
        assert inputs[0]["original"] != inputs[1]["original"]
        assert client.get(f"/api/submissions/{sid}/status").json()["ocr_mode"] == "deepseek_ocr"
    with TestClient(main.app) as client:
        retry = client.post(f"/api/submissions/{sid}/retry")
        assert retry.status_code == 200
        copied = manager.record(retry.json()["id"])["meta"]["inputs"]
        assert [x["filename"] for x in copied] == ["paper.png", "work.png"]
        assert all(x["original"] != y["original"] for x, y in zip(inputs, copied))


@pytest.mark.parametrize("roles", ['["student_work"]', '["wrong","auto"]', '{"a":"auto"}', 'broken'])
def test_invalid_roles_rejected_before_creating_a_job(monkeypatch, roles):
    manager = idle_manager(monkeypatch)
    with TestClient(main.app) as client:
        r = client.post("/api/submissions", files=[("files", ("a.png", image_bytes())), ("files", ("b.png", image_bytes()))],
                        data={"file_roles": roles})
        assert r.status_code == 400
        assert not manager.records


@pytest.mark.parametrize("filename,content", [("bad.txt", b"data"), ("empty.png", b"")])
def test_any_invalid_material_rejects_whole_bundle(monkeypatch, filename, content):
    manager = idle_manager(monkeypatch)
    with TestClient(main.app) as client:
        r = client.post("/api/submissions", files=[("files", ("a.png", image_bytes())), ("files", (filename, content))])
        assert r.status_code == 400 and not manager.records


@pytest.mark.asyncio
async def test_bundle_aligns_paper_and_key_and_only_grades_student_pages(monkeypatch):
    sid = "sub_materials"
    d = store.sub_dir(sid)
    d.mkdir()
    inputs = []
    for name in ("paper", "key", "work-1", "work-2"):
        original = d / f"{name}.png"
        original.write_bytes(image_bytes())
        inputs.append({"filename": f"{name}.png", "original": str(original), "role": "auto"})
    payloads = [
        {"material_role": "question_paper", "role_confidence": .99, "title": "Quiz", "questions": [
            {"id": "1", "question": "12+8=", "type": "calc"}, {"id": "2", "question": "15-6=", "type": "calc"}]},
        {"material_role": "answer_key", "role_confidence": .99,
         "reference_answers": [{"id": "1", "answer": "20"}, {"id": "2", "answer": "9"}]},
        {"material_role": "student_work", "role_confidence": .99, "student_name": "小明", "questions": [
            {"id": "1", "question": "", "student_answer": "20", "answer_state": "answered", "confidence": .99,
             "answer_block_ids": [], "question_bbox": [100, 100, 300, 150]}]},
        {"material_role": "student_work", "role_confidence": .99, "student_name": "小明", "questions": [
            {"id": "2", "question": "", "student_answer": "8", "answer_state": "answered", "confidence": .99,
             "answer_block_ids": [], "question_bbox": [100, 100, 300, 150]}]},
    ]
    parse_count = 0
    bundle_payload = {"title": "Quiz", "student_name": "小明",
                      "materials": [{"page": i, "role": obj["material_role"], "confidence": .99} for i, obj in enumerate(payloads, 1)],
                      "questions": [{**q, "page": i, "question": payloads[0]["questions"][int(q["id"]) - 1]["question"]}
                                    for i, obj in enumerate(payloads, 1) if obj["material_role"] == "student_work" for q in obj["questions"]],
                      "reference_answers": payloads[1]["reference_answers"]}
    solver_inputs = []
    async def model(cfg, *, system, blocks, **kw):
        nonlocal parse_count
        if system.startswith(prompts.PARSER):
            parse_count += 1
            assert "整组" in kw["label"] and len([b for b in blocks if b.get("type") == "image"]) == 4
            return bundle_payload
        if system == prompts.SOLVER:
            solver_inputs.append(str(blocks))
            return {"answers": [{"id": "1", "answer": "20", "confident": True}, {"id": "2", "answer": "9", "confident": True}]}
        return {"results": [{"id": "2", "verdict": "wrong", "confidence": .99}]}
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    # Disable actual OCR calls while preserving OCR positioning mode.
    monkeypatch.setattr(pipeline.ocr, "enabled", lambda cfg: False)
    meta = {"inputs": inputs, "original": inputs[0]["original"], "filename": "paper.png 等4份材料",
            "ocr_mode": "deepseek_ocr", "file_key": "bundle", "created_at": "now"}
    result = await pipeline.run_pipeline(sid, config.load_settings(), meta, lambda *a, **kw: None)
    assert parse_count == 1
    assert result["page_count"] == 4 and result["graded_pages"] == [3, 4]
    assert [(q["page"], q["question"], q["status"]) for q in result["questions"]] == [
        (3, "12+8=", "correct"), (4, "15-6=", "wrong")]
    assert result["answer_source"] == "teacher"
    assert solver_inputs and "20" in solver_inputs[0] and "9" in solver_inputs[0]
    assert [m["role"] for m in result["materials"]] == ["question_paper", "answer_key", "student_work", "student_work"]
    for n in (1, 2):
        with Image.open(d / f"annotated/page-{n}.png") as image:
            assert image.getcolors() == [(300 * 400, (255, 255, 255))]
    with TestClient(main.app) as client:
        import pymupdf
        pdf = client.get(f"/api/submissions/{sid}/pdf")
        with pymupdf.open(stream=pdf.content, filetype="pdf") as document:
            assert len(document) == 2


def test_bundle_fingerprint_includes_all_files_roles_and_order():
    source = [{"filename": "a.png", "role": "auto", "data": b"image A"},
              {"filename": "b.png", "role": "auto", "data": b"image B"}]
    key = materials.bundle_hash(source)
    assert key == materials.bundle_hash(source)
    assert key != materials.bundle_hash(list(reversed(source)))
    assert key != materials.bundle_hash([source[0], {**source[1], "data": b"new answer"}])
    assert key != materials.bundle_hash([{**source[0], "role": "answer_key"}, source[1]])


def manifest(roles=("auto", "auto")):
    return [{"page": i, "filename": f"input-{i}.png", "source_page": 1, "requested_role": role}
            for i, role in enumerate(roles, 1)]


def agent_output():
    return {"materials": [{"page": 1, "role": "question_paper", "confidence": .99},
                          {"page": 2, "role": "student_work", "confidence": .99}],
            "questions": [{"id": "1", "page": 2, "question": "12+8=", "student_answer": "20"}],
            "reference_answers": []}


@pytest.mark.parametrize("change,expected", [
    (lambda x: x["materials"][0].update(role="unknown"), "无法确认"),
    (lambda x: x["questions"][0].update(page=1), "不属于学生作答"),
    (lambda x: x["questions"].append(dict(x["questions"][0])), "重复题号"),
    (lambda x: x["questions"][0].update(question=""), "缺少题干"),
    (lambda x: x.update(multiple_students=True), "多位学生"),
    (lambda x: x.update(reference_answers=[{"id": "1", "answer": "20"}]), "非标准答案"),
])
def test_bad_agent_output_is_rejected_instead_of_grading_wrong_materials(change, expected):
    data = agent_output()
    change(data)
    with pytest.raises(ValueError, match=expected):
        materials.resolve_bundle(data, manifest())


def test_explicit_file_role_overrides_agent_classification():
    data = agent_output()
    data["materials"][0].update(role="student_work", confidence=0)
    roles, work_pages, _ = materials.resolve_bundle(data, manifest(("question_paper", "auto")))
    assert roles[0]["role"] == "question_paper" and work_pages == [2]


@pytest.mark.asyncio
async def test_teacher_edited_paper_answer_is_preserved_when_regrading_bundle(monkeypatch):
    questions = [{"id": "1", "type": "calc", "question": "请计算下面算式的结果：12+8="}]
    pid = store.save_paper("Quiz", questions, [{"id": "1", "answer": "20"}], "teacher")
    store.update_answers(pid, [{"id": "1", "answer": "新的评分要点"}])
    async def model(*a, **kw):
        return {"answers": [{"id": "1", "answer": "20"}]}
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    paper, hit, _ = await pipeline.paper_answers({"_prefer_saved_answers": True}, "Quiz", questions, [],
                                                "来自原始文件的标准答案：1.20", lambda *a, **kw: None)
    assert hit and paper["id"] == pid and paper["answers"][0]["answer"] == "新的评分要点"


@pytest.mark.parametrize("page_counts", [[41], [20, 21]])
def test_over_limit_pdf_bundle_is_rejected_before_any_page_is_rendered(tmp_path, page_counts):
    import pymupdf
    inputs = []
    for i, count in enumerate(page_counts):
        source = tmp_path / f"input-{i}.pdf"
        with pymupdf.open() as document:
            for _ in range(count):
                document.new_page(width=20, height=20)
            document.save(source)
        inputs.append({"original": str(source), "filename": source.name, "role": "auto"})
    with pytest.raises(ValueError, match="40"):
        materials.to_pages(inputs, tmp_path / "out")
    assert not list((tmp_path / "out").rglob("*.png"))


def test_retry_of_failed_regrade_preserves_teacher_answer_preference(monkeypatch):
    manager = idle_manager(monkeypatch)
    sid = "sub_failed_bundle_regrade"
    d = store.sub_dir(sid)
    d.mkdir()
    original = d / "original.png"
    original.write_bytes(image_bytes())
    store.save_artifact(sid, "job.json", {"id": sid, "status": "failed", "settings": {}, "teacher_key": "",
        "meta": {"filename": "a.png", "original": str(original), "ocr_mode": "deepseek_ocr",
                 "prefer_saved_answers": True, "input_version": 2,
                 "inputs": [{"original": str(original), "filename": "a.png", "role": "student_work"}]}})
    with TestClient(main.app) as client:
        retry = client.post(f"/api/submissions/{sid}/retry")
        assert retry.status_code == 200
        assert manager.record(retry.json()["id"])["meta"].get("prefer_saved_answers") is True


def test_pdf_and_image_share_global_page_numbers_and_keep_source_pages(tmp_path):
    import pymupdf
    pdf = tmp_path / "paper.pdf"
    with pymupdf.open() as document:
        document.new_page(width=100, height=100)
        document.new_page(width=100, height=100)
        document.save(pdf)
    image = tmp_path / "student.png"
    image.write_bytes(image_bytes())
    inputs = [{"filename": path.name, "original": str(path), "role": "auto"} for path in (pdf, image)]
    pages, sources = materials.to_pages(inputs, tmp_path / "out")
    assert [p.name for p in pages] == ["page-1.png", "page-2.png", "page-3.png"]
    assert [(m["filename"], m["source_page"], m["page"]) for m in sources] == [
        ("paper.pdf", 1, 1), ("paper.pdf", 2, 2), ("student.png", 1, 3)]


def test_modern_single_photo_without_reference_answers_can_be_graded(monkeypatch):
    import time
    stages = []
    async def model(cfg, *, system, **kwargs):
        stages.append(kwargs["stage"])
        if system.startswith(prompts.PARSER):
            assert prompts.MATERIAL_BUNDLE_HINT in system
            return {"materials": [{"page": 1, "role": "student_work", "confidence": .99}],
                    "questions": [{"id": "1", "page": 1, "question": "12+8=", "student_answer": "20",
                                   "answer_state": "answered", "confidence": .99,
                                   "question_bbox": [100, 100, 300, 150]}], "reference_answers": []}
        return {"answers": [{"id": "1", "answer": "20", "confident": True}]}
    cfg = {**config.load_settings(), "use_ocr": False}
    monkeypatch.setattr(config, "load_settings", lambda: cfg)
    monkeypatch.setattr(pipeline.ocr, "enabled", lambda cfg: False)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    monkeypatch.setattr(main, "job_manager", JobManager(pipeline.run_pipeline))
    with TestClient(main.app) as client:
        created = client.post("/api/submissions", files=[("files", ("student.png", image_bytes(), "image/png"))])
        assert created.status_code == 200
        sid = created.json()["id"]
        for _ in range(100):
            status = client.get(f"/api/submissions/{sid}/status").json()
            if status["done"]:
                break
            time.sleep(.005)
        assert status["status"] == "done" and status["ocr_mode"] == "deepseek_ocr"
        result = client.get(f"/api/submissions/{sid}").json()
        assert result["summary"]["correct"] == 1 and result["answer_source"] == "ai"
        assert result["graded_pages"] == [1] and result["questions"][0]["draw"]
        assert stages == ["parse", "solve"]


def test_local_mock_model_supports_new_material_bundle_contract(monkeypatch):
    import mock_llm
    monkeypatch.setitem(mock_llm.STATE, "n", 0)
    source = [{"page": 1, "filename": "student.png", "source_page": 1, "requested_role": "auto"}]
    body = {"system": prompts.PARSER + prompts.MATERIAL_BUNDLE_HINT,
            "messages": [{"content": [{"type": "text", "text": "这一组文件属于同一次批改。全局页码与文件来源：\n" + json.dumps(source)}]}]}
    output = json.loads(mock_llm.reply_for(body)["text"])
    metadata, work_pages, refs = materials.resolve_bundle(output, source)
    assert metadata[0]["role"] == "student_work" and work_pages == [1] and refs == []
