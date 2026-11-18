"""Pure image exam agent: no OCR, scoped native tools, original-page coordinates."""
import io
import json
import shlex
import subprocess

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from test_jobs import image_bytes, isolated_store  # noqa: F401
from app import config, main, ocr, store, workflow
from app.jobs import JobManager


def final_result(status='correct'):
    return {'title': 'Quiz', 'materials': [{'page': 1, 'role': 'question_paper', 'confidence': .99},
        {'page': 2, 'role': 'student_work', 'confidence': .99}],
        'questions': [{'id': '1.1', 'question_label': '1.1', 'type': 'calc', 'question': '1+1=', 'source_pages': [1]}],
        'answers': [{'id': '1.1', 'answer': '2', 'confident': True}],
        'results': [{'id': '1.1', 'page': 2, 'student_answer': '2', 'answer_state': 'answered',
            'verdict': status, 'confidence': .99 if status != 'review' else .4,
            'answer_bbox': [300, 300, 400, 350], 'position_confidence': .99}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('status,color', [('correct', 'red'), ('wrong', 'red'), ('review', 'yellow')])
async def test_one_vision_agent_owns_roles_grading_and_dynamic_inspection(monkeypatch, status, color):
    from app import submission_agent
    sid = 'sub_vision_exam'
    folder = store.sub_dir(sid); folder.mkdir()
    inputs = []
    for i in (1, 2):
        path = folder / f'input-{i}.png'; path.write_bytes(image_bytes())
        inputs.append({'filename': path.name, 'original': str(path), 'role': 'auto'})
    async def forbid_ocr(*a, **kw): pytest.fail('OCR must never be called')
    calls = []
    async def agent(cfg, **kwargs):
        calls.append(kwargs)
        assert 'Read' in kwargs['builtin_tools'] and 'Bash' in kwargs['builtin_tools']
        assert 'Agent' not in kwargs['builtin_tools'] and 'agent_definitions' not in kwargs
        assert kwargs['submission_id'] == sid
        assert kwargs['max_turns'] > 1 and kwargs['attempts'] == 1
        assert sum(b['type'] == 'image' for b in kwargs['blocks']) == 2
        context = json.loads(kwargs['blocks'][0]['text'])
        assert 'ocr_pages' not in context and context['materials'][0]['requested_role'] == 'auto'
        assert 'Multi-scale Image Analysis' in kwargs['system']
        value = final_result(status)
        if status == 'review': value['results'][0]['verdict'] = None
        assert kwargs['result_filter'](value)
        assert not kwargs['result_filter']({'path': 'crop.png', 'result': '2'})
        return value
    monkeypatch.setattr(workflow, 'run_json_agent', forbid_ocr, raising=False)
    monkeypatch.setattr(ocr, 'recognize', forbid_ocr)
    monkeypatch.setattr(submission_agent, 'run_json_agent', agent)
    result = await workflow.run_workflow(sid, config.load_settings(), {'mode': 'grade', 'inputs': inputs,
        'filename': 'Quiz', 'workflow_version': 2}, lambda *a, **kw: None)
    assert len(calls) == 1 and result['ocr_used'] is False and result['ocr_mode'] == 'vision'
    assert result['execution_mode'] == 'vision_agent'
    assert result['questions'][0]['draw']
    with Image.open(folder / 'annotated/page-2.png') as image:
        colors = list(image.convert('RGB').getdata())
    tests = {'red': lambda c: c == (230, 131, 125),
             'yellow': lambda c: c[0] > 150 and c[1] > 100 and c[2] < 80}
    assert any(tests[color](c) for c in colors)


def test_upload_accepts_non_deepseek_models_without_ocr(monkeypatch):
    import asyncio
    async def idle(*args): await asyncio.Event().wait()
    manager = JobManager(idle); monkeypatch.setattr(main, 'job_manager', manager)
    cfg = {**config.load_settings(), 'base_url': 'https://example.com/anthropic'}
    monkeypatch.setattr(config, 'load_settings', lambda: cfg)
    with TestClient(main.app) as client:
        response = client.post('/api/submissions', files={'file': ('work.png', image_bytes(), 'image/png')})
        assert response.status_code == 200
        record = manager.record(response.json()['id'])
        assert record['meta']['ocr_mode'] == 'vision'
        assert record['settings']['use_ocr'] is False


def test_opening_an_existing_vision_result_refreshes_marks():
    sid = 'sub_saved_vision'
    folder = store.sub_dir(sid)
    (folder / 'pages').mkdir(parents=True)
    (folder / 'annotated').mkdir()
    Image.new('RGB', (200, 300), 'white').save(folder / 'pages/page-1.png')
    Image.new('RGB', (200, 300), 'white').save(folder / 'annotated/page-1.png')
    question = {'id': '1', 'page': 1, 'status': 'wrong', 'overridden': True,
                'bbox_px': [50, 50, 150, 100], 'bbox_ok': True, 'draw': True}
    store.save_result(sid, {'id': sid, 'workflow_version': 2, 'annotation_version': 4,
        'execution_mode': 'vision_agent', 'page_count': 1, 'questions': [question]})

    result = main.get_submission(sid)
    assert result['questions'] == [question]
    with Image.open(folder / 'annotated/page-1.png') as annotated:
        assert (230, 131, 125) in annotated.crop((93, 68, 107, 82)).getdata()


@pytest.mark.asyncio
async def test_zoom_tool_preserves_mapping_and_only_allows_scoped_commands(tmp_path):
    from app.visual_tools import VisualTools
    page = tmp_path / 'page.png'; Image.new('RGB', (1000, 2000), 'white').save(page)
    tools = VisualTools([page], [], tmp_path / 'agent')
    command = tools.command_prefix + ' crop --page 1 --bbox 100 200 300 400 --scale 2'
    permission = await tools.authorize({'tool_name': 'Bash', 'tool_input': {'command': command}}, None, None)
    assert permission['hookSpecificOutput']['permissionDecision'] == 'allow'
    run = subprocess.run(shlex.split(permission['hookSpecificOutput']['updatedInput']['command']), capture_output=True, text=True, check=True)
    crop = json.loads(run.stdout)
    assert crop['original_bbox'] == [100, 200, 300, 400]
    assert crop['original_pixels'] == [100, 400, 300, 800]
    with Image.open(crop['path']) as image: assert image.size == (400, 800)
    assert (await tools.authorize({'tool_name': 'Read', 'tool_input': {'file_path': crop['path']}}, None, None))['hookSpecificOutput']['permissionDecision'] == 'allow'
    for name, args in [('Bash', {'command': 'cat /etc/passwd'}), ('Read', {'file_path': '/etc/passwd'})]:
        assert (await tools.authorize({'tool_name': name, 'tool_input': args}, None, None))['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_reportlab_export_preserves_page_order_size_and_pixels(tmp_path):
    import pymupdf
    from app.imaging import pages_to_pdf
    paths = []
    for i, (size, color) in enumerate([((600, 800), 'green'), ((800, 500), 'red')], 1):
        path = tmp_path / f'page-{i}.png'
        Image.new('RGB', size, color).save(path, dpi=(200, 200))
        paths.append(path)
    pdf = pymupdf.open(stream=pages_to_pdf(paths), filetype='pdf')
    assert pdf.page_count == 2
    for page, size, color in zip(pdf, ((600,800), (800,500)), ((0,128,0),(255,0,0))):
        assert abs(page.rect.width - size[0]*72/200) < .1
        assert abs(page.rect.height - size[1]*72/200) < .1
        pix = page.get_pixmap()
        assert pix.pixel(pix.width//2, pix.height//2)[:3] == color
    pdf.close()
