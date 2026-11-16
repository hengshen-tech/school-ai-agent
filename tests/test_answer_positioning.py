"""Final answer fragments must retain real OCR coordinates, including saved jobs."""
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from test_jobs import image_bytes, isolated_store  # noqa: F401
from app import main, store


@pytest.mark.parametrize('student,block,expected', [
    ('465-278=187（竖式465−278=187）', '$187$', True),
    ('826-(378+139)=309（竖式378+139=517，826−517=309）', '$517 \\quad 309$', True),
    ('96+498+132=594+132=726', '$=726$', True),
    ('360+226=586(米)；250+276=526(米)；586>526，586-526=60(米)',
     '$586>526 \\quad 586-526=60(\\text{米})$', True),
    ('答：童童家距离学校396米。', '答：童童家距离学396米', True),
    ('.5', '5', False), ('20', '2', False), ('x=-5', '5', False),
    ('212-123+49=89+49=138', '$=158$', False), ('A', '班级A', False),
])
def test_answer_fragment_matching(student, block, expected):
    from app.positioning import answer_text_matches
    assert answer_text_matches(student, block) is expected


def sample(tmp_path):
    path = tmp_path / 'page.png'
    Image.new('RGB', (1000, 1000), 'white').save(path)
    questions = [
        {'id': '1', 'page': 1, 'student_answer': '465-278=187（竖式465−278=187）', 'answer_state': 'answered', 'status': 'correct', 'overridden': True},
        {'id': '2', 'page': 1, 'student_answer': '212-123+49=89+49=138', 'answer_state': 'answered', 'status': 'review'},
    ]
    blocks = [{'id': 'p1:b1', 'text': '$187$', 'box': [100, 200, 180, 240]},
              {'id': 'p1:b2', 'text': '$=158$', 'box': [400, 200, 480, 240]}]
    return path, questions, {1: {'complete': True, 'blocks': blocks, 'error': ''}}


def test_one_bad_match_does_not_discard_other_positions(tmp_path):
    from app.positioning import apply_positions
    page, questions, ocr_pages = sample(tmp_path)
    result = apply_positions(questions, ocr_pages, [page], [
        {'id': '1', 'page': 1, 'block_ids': ['p1:b1']},
        {'id': '2', 'page': 1, 'block_ids': ['p1:b2']},
    ])
    assert questions[0]['draw'] and questions[0]['bbox_norm'] == [100, 200, 180, 240]
    assert not questions[1].get('draw') and questions[1]['position_note']
    assert result['positioned'] == 1 and result['rejected'][0]['id'] == '2'
    apply_positions(questions, ocr_pages, [page], [])
    assert questions[0]['draw']  # A later empty submission must not erase prior successes.


def test_existing_job_replays_saved_selections_and_keeps_manual_verdict(tmp_path):
    from app.positioning import ensure_workflow_annotations, ANNOTATION_VERSION
    sid = 'sub_fragment_repair'
    d = store.sub_dir(sid)
    (d / 'pages').mkdir(parents=True)
    (d / 'pages/page-1.png').write_bytes(image_bytes())
    _, questions, ocr_pages = sample(tmp_path)
    for q in questions:
        q.update(draw=False, bbox_px=None, bbox_norm=None, bbox_ok=False, ocr_block_ids=[])
    result = {'id': sid, 'workflow_version': 2, 'annotation_version': 3,
              'position_method': 'ocr_blocks', 'page_count': 1, 'questions': questions}
    store.save_result(sid, result)
    store.save_artifact(sid, 'ocr.json', ocr_pages)
    transcript = d / 'sdk-config/projects/project/session.jsonl'
    transcript.parent.mkdir(parents=True)
    call = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
        'name': 'mcp__workflow__submit_positions', 'input': {'data': {'positions': [
            {'id': '1', 'page': 1, 'block_ids': ['p1:b1']}, {'id': '2', 'page': 1, 'block_ids': ['p1:b2']}]}}}]}}
    transcript.write_text(json.dumps(call) + '\n')
    repaired = ensure_workflow_annotations(sid, result)
    first = repaired['questions'][0]
    assert repaired['annotation_version'] == ANNOTATION_VERSION
    assert first['status'] == 'correct' and first['overridden'] and first['draw']
    assert repaired['questions'][1]['status'] == 'review' and not repaired['questions'][1]['draw']
    assert (d / 'annotated/page-1.png').is_file()
    with Image.open(d / 'annotated/page-1.png') as image:
        assert any(r > 150 and g < 100 and b < 100 for r, g, b in image.getdata())
    assert ensure_workflow_annotations(sid, repaired) == repaired


def test_unpositioned_note_explains_ocr_failure(tmp_path):
    from app.positioning import apply_positions
    page, questions, _ = sample(tmp_path)
    ocr_pages = {1: {'complete': False, 'blocks': [], 'error': 'OCR HTTP 503'}}
    apply_positions(questions, ocr_pages, [page], [])
    assert '503' in questions[0]['position_note']


def test_manual_position_can_draw_after_correct_override(tmp_path):
    sid = 'sub_manual_position'
    d = store.sub_dir(sid)
    (d / 'pages').mkdir(parents=True)
    (d / 'pages/page-1.png').write_bytes(image_bytes())
    store.save_result(sid, {'id': sid, 'workflow_version': 2, 'annotation_version': 4, 'page_count': 1,
        'questions': [{'id': '1', 'page': 1, 'status': 'correct', 'overridden': True, 'draw': False,
                       'bbox_ok': False, 'bbox_px': None, 'position_note': '未定位'}]})
    with TestClient(main.app) as client:
        response = client.post(f'/api/submissions/{sid}/position', json={'qid': '1', 'bbox': [100, 200, 200, 250]})
        assert response.status_code == 200
        q = response.json()['questions'][0]
        assert q['draw'] and q['position_source'] == 'manual' and q['overridden']
        assert q['bbox_norm'] == [100, 200, 200, 250]
        bad = client.post(f'/api/submissions/{sid}/position', json={'qid': '1', 'bbox': [100, 200, 1200, 250]})
        assert bad.status_code == 400
