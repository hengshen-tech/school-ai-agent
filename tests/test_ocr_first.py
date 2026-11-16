"""OCR is shared input to the workflow, before any Agent starts."""
import pytest

from test_jobs import image_bytes, isolated_store  # noqa: F401
from app import config, ocr, store, workflow
from app.workflow_tools import read_page_image


@pytest.mark.asyncio
async def test_all_materials_are_ocrd_before_main_and_grader_reuses_positions(monkeypatch):
    sid = 'sub_ocr_first'
    folder = store.sub_dir(sid)
    folder.mkdir()
    inputs = []
    for index, role in enumerate(('question_paper', 'student_work'), 1):
        path = folder / f'input-{index}.png'
        path.write_bytes(image_bytes())
        inputs.append({'original': str(path), 'filename': path.name, 'role': role})
    calls, events = [], []
    async def recognize(cfg, data, filename, **kwargs):
        calls.append(filename)
        return {'pages': [{'finish_reason': 'stop', 'text': '<|ref|>text<|/ref|><|box|>[[300,300,400,350]]<|/box|>2'}]}
    async def main_agent(cfg, state):
        assert len(calls) == 2
        assert set(state.snapshot()['ocr_pages']) == {1, 2}
        view = await read_page_image(state, {'page': 2})
        assert any(b.get('type') == 'text' and 'p2:b1' in b.get('text', '') for b in view['content'])
        state.submit_source({'title': 'Quiz', 'materials': [{'page': 1}, {'page': 2}],
            'questions': [{'id': '1', 'type': 'calc', 'question': '1+1=', 'source_pages': [1]}]})
        assert set(state.ocr_pages) == {1, 2}
        state.submit_draft({'answers': [{'id': '1', 'answer': '2', 'confident': True}]})
        state.submit_review({'source_version': 1, 'draft_version': 1,
            'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
        state.submit_grades({'source_version': 1, 'draft_version': 1, 'results': [
            {'id': '1', 'page': 2, 'student_answer': '2', 'answer_state': 'answered', 'verdict': 'correct',
             'confidence': .99, 'answer_block_ids': ['p2:b1']}]})
        assert state.grades[0]['bbox_ok']
        await state.ocr_page({'page': 2})
        assert len(calls) == 2  # Reading shared OCR must not bill twice.
        state.submit_positions({'positions': []})
    monkeypatch.setattr(ocr, 'recognize', recognize)
    monkeypatch.setattr(workflow, 'run_workflow_agent', main_agent)
    result = await workflow.run_workflow(sid, config.load_settings(), {
        'mode': 'grade', 'inputs': inputs, 'filename': 'Quiz', 'workflow_version': 2},
        lambda stage, status, message='', **extra: events.append((stage, status)))
    assert result['questions'][0]['draw']
    assert events.index(('ocr', 'done')) < events.index(('parse', 'start'))
    assert (folder / 'ocr.json').is_file()


@pytest.mark.asyncio
async def test_ocr_is_available_before_material_classification(tmp_path, monkeypatch):
    from test_agent_workflow import state, source
    s = state(tmp_path, monkeypatch)
    calls = []
    async def recognize(*args, **kwargs):
        calls.append(1)
        return {'pages': [{'finish_reason': 'stop', 'text': '<|ref|>text<|/ref|><|box|>[[300,300,400,350]]<|/box|>2'}]}
    monkeypatch.setattr(ocr, 'recognize', recognize)
    await s.ocr_page({'page': 1})
    s.submit_source(source())
    s.submit_source(source())
    await s.ocr_page({'page': 1})
    assert len(calls) == 1 and s.ocr_pages[1]['blocks'][0]['id'] == 'p1:b1'
