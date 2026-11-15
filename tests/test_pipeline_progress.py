import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pipeline-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pytest
from PIL import Image
from app import agents, imaging, pipeline, store
from app.agents import AgentError
from claude_agent_sdk.types import StreamEvent, AssistantMessage, TextBlock, ResultMessage


@pytest.mark.asyncio
async def test_agent_streams_output_metrics_and_explicit_high(monkeypatch):
    seen, events = [], []
    class Client:
        def __init__(self, opts):
            seen.append(opts)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def query(self, prompt):
            async for _ in prompt:
                pass
        async def receive_response(self):
            yield StreamEvent(uuid="a", session_id="s", event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "private"}})
            for part in ('{"ok":', 'true}'):
                yield StreamEvent(uuid="b", session_id="s", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": part}})
            yield AssistantMessage(content=[TextBlock(text='{"ok":true}')], model="test")
            yield ResultMessage(subtype="success", duration_ms=20, duration_api_ms=10, is_error=False,
                                num_turns=1, session_id="s", result='{"ok":true}', usage={"output_tokens": 4})
    monkeypatch.setattr(agents, "ClaudeSDKClient", Client)
    cfg = {"model": "deepseek-flash", "api_key": "secret", "base_url": "https://api.deepseek.com/anthropic"}
    emit = lambda stage, status, message="", **extra: events.append({"stage": stage, "status": status, **extra})
    result = await agents.run_json_agent(cfg, system="test", blocks=[], emit=emit, stage="parse", label="第 1 页")
    assert result == {"ok": True}
    assert seen[0].effort == "high" and seen[0].thinking["type"] in ("enabled", "adaptive")
    assert seen[0].include_partial_messages is True
    assert ''.join(e.get("text", "") for e in events if e.get("kind") == "output") == '{"ok":true}'
    assert "private" not in str(events)
    metrics = [e["metric"] for e in events if e.get("kind") == "metric"]
    assert len(metrics) == 1 and metrics[0]["usage"]["output_tokens"] == 4


@pytest.mark.asyncio
async def test_ocr_page_can_parse_while_other_page_waits(tmp_path, monkeypatch):
    pages = [tmp_path / f"{i}.png" for i in (1, 2)]
    for page in pages:
        Image.new("RGB", (100, 100), "white").save(page)
    release, parsed = asyncio.Event(), asyncio.Event()
    async def recognize(cfg, data, filename, *args):
        if "2" in filename:
            await release.wait()
        return {"pages": [{"text": ""}]}
    async def model(*args, **kwargs):
        if kwargs.get("label") == "第 1 页":
            parsed.set()
        return {"questions": [{"id": "1", "question": "12 + 8 = ?", "student_answer": "20", "answer_bbox": [10, 10, 30, 30]}]}
    monkeypatch.setattr(pipeline.ocr, "recognize", recognize)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    cfg = {"use_ocr": True, "api_key": "x", "base_url": "https://api.deepseek.com/anthropic"}
    task = asyncio.create_task(pipeline.parse_pages(cfg, pages, lambda *a, **k: None, {}))
    try:
        await asyncio.wait_for(parsed.wait(), 1)
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_review_batches_overlap_but_normal_errors_do_not_trigger_tool_fallback(tmp_path, monkeypatch):
    page = tmp_path / "page.png"
    Image.new("RGB", (500, 500), "white").save(page)
    questions = [{"id": str(i), "page": 1, "bbox_px": [10, 10, 30, 30], "bbox_norm": [20, 20, 60, 60],
                  "answer_state": "answered", "student_answer": "20", "reasons": []} for i in range(25)]
    active, peak, calls = 0, 0, 0
    async def model(*a, **kw):
        nonlocal active, peak, calls
        active += 1; peak = max(active, peak); calls += 1
        await asyncio.sleep(.03)
        active -= 1
        raise AgentError("模型调用超时")
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    await pipeline.review({"agentic_zoom": True}, questions, [page], lambda *a, **k: None, {})
    assert peak > 1
    assert calls == 3  # one attempt per chunk, no false capability downgrade
    assert all("复核未完成" in q["reasons"] for q in questions)


def test_marks_are_close_and_avoid_neighbor_answers(tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (1000, 1400), "white").save(page)
    items = [{"status": "correct", "bbox_px": [100, 100, 180, 135], "draw": True},
             {"status": "wrong", "bbox_px": [182, 95, 240, 145], "draw": False}]
    imaging.annotate(page, tmp_path / "out.png", items)
    img = Image.open(tmp_path / "out.png")
    points = [(x, y) for y in range(80, 155) for x in range(60, 260) if img.getpixel((x, y))[0] > 150 and img.getpixel((x, y))[1] < 100]
    assert points
    assert not any(182 <= x <= 240 and 95 <= y <= 145 for x, y in points)
    # Right candidate collides, so the symbol belongs immediately to the left.
    assert max(x for x, _ in points) < 100


@pytest.mark.asyncio
async def test_same_paper_solves_once_without_blocking_different_paper(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PAPERS_DIR", tmp_path)
    release = asyncio.Event()
    both_started = asyncio.Event()
    calls = []
    async def solve(cfg, title, questions, pages, teacher_key, emit=None):
        calls.append(title)
        if len(calls) == 2:
            both_started.set()
        await release.wait()
        return [{"id": "1", "answer": "20", "confident": True}]
    monkeypatch.setattr(pipeline, "solve", solve)
    a = [{"id": "1", "question": "12 + 8 = ? Please calculate."}]
    b = [{"id": "1", "question": "81 / 9 = ? Please calculate."}]
    emit = lambda *a, **kw: None
    tasks = [asyncio.create_task(pipeline.paper_answers({}, name, qs, [], "", emit))
             for name, qs in (("A", a), ("A duplicate", a), ("B", b))]
    try:
        await asyncio.wait_for(both_started.wait(), 1)
        assert calls == ["A", "B"]
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert results[0][0]["id"] == results[1][0]["id"]
    assert results[0][0]["id"] != results[2][0]["id"]


def test_malformed_tool_or_image_input_is_not_capability_failure():
    assert not pipeline.tool_unsupported(AgentError("400 invalid_request_error: image dimensions exceed maximum allowed size"))
    assert not pipeline.tool_unsupported(AgentError("400 tools.0.input_schema: required property missing"))
    assert pipeline.tool_unsupported(AgentError("tool_result image content is not supported by this endpoint"))


@pytest.mark.asyncio
async def test_review_crops_are_encoded_once_across_capability_fallback(tmp_path, monkeypatch):
    page = tmp_path / "page.png"
    Image.new("RGB", (200, 200), "white").save(page)
    questions = [{"id": "1", "page": 1, "bbox_px": [10, 10, 30, 30], "bbox_norm": [50, 50, 150, 150],
                  "answer_state": "answered", "student_answer": "20", "reasons": []}]
    encoded = []
    original = imaging.crop
    def crop(*args, **kwargs):
        encoded.append(1)
        return original(*args, **kwargs)
    calls = []
    async def model(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("tool_result image content is not supported by this endpoint")
        return {"reviews": [{"id": "1", "bbox_ok": True, "matches": True}]}
    monkeypatch.setattr(imaging, "crop", crop)
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    await pipeline.review({"agentic_zoom": True}, questions, [page], lambda *a, **k: None, {})
    assert len(calls) == 2 and len(encoded) == 1


@pytest.mark.asyncio
async def test_parallel_grading_covers_all_questions_and_maps_by_id(monkeypatch):
    import json
    questions = [{"id": str(i), "type": "calc", "question": f"Question {i}", "student_answer": "x",
                  "answer_state": "answered"} for i in range(85)]
    key = {q["id"]: {"answer": "y"} for q in questions}
    active, peak = 0, 0
    processed = []
    async def model(*args, **kwargs):
        nonlocal active, peak
        payload = json.loads(kwargs["blocks"][0]["text"])
        active += 1; peak = max(peak, active)
        await asyncio.sleep(.01)
        active -= 1
        processed.extend(q["id"] for q in payload)
        return {"results": [{"id": q["id"], "verdict": "wrong" if int(q["id"]) % 2 else "correct",
                             "reason": q["id"], "confidence": .99} for q in reversed(payload)]}
    monkeypatch.setattr(pipeline, "run_json_agent", model)
    await pipeline.grade({}, questions, key, lambda *a, **k: None)
    assert peak == 3 and len(set(processed)) == 85
    assert all(q["reason"] == q["id"] and q["verdict"] == ("wrong" if int(q["id"]) % 2 else "correct") for q in questions)


@pytest.mark.asyncio
async def test_model_concurrency_limit_is_shared_across_all_callers(monkeypatch):
    active, peak = 0, 0
    class Client:
        def __init__(self, opts):
            pass
        async def __aenter__(self):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            return self
        async def __aexit__(self, *args):
            nonlocal active
            active -= 1
        async def query(self, prompt):
            async for _ in prompt:
                pass
        async def receive_response(self):
            await asyncio.sleep(.02)
            yield AssistantMessage(content=[TextBlock(text="OK")], model="test")
    monkeypatch.setattr(agents, "ClaudeSDKClient", Client)
    monkeypatch.setenv("MAX_PARALLEL_MODEL_CALLS", "2")
    cfg = {"model": "deepseek-flash", "api_key": "secret", "base_url": "https://api.deepseek.com/anthropic"}
    assert await asyncio.gather(*(agents.run_agent(cfg, system="test", blocks=[]) for _ in range(6))) == ["OK"] * 6
    assert peak == 2 and active == 0
