"""Visual answer positions are a fallback to verified OCR, not a new model call."""
import pytest
from PIL import Image

from test_agent_workflow import state, reviewed


def grade_payload(**kwargs):
    return {'source_version': 1, 'draft_version': 1, 'results': [
        {'id': '1', 'page': 2, 'student_answer': '2', 'answer_state': 'answered',
         'verdict': 'correct', 'confidence': .99, 'answer_bbox': [100, 200, 200, 260],
         'position_confidence': .96, **kwargs}]}


@pytest.mark.parametrize('confidence,accepted', [(.699, False), (.7, True), (.85, True)])
def test_vision_position_confidence_cutoff(tmp_path, monkeypatch, confidence, accepted):
    s = state(tmp_path, monkeypatch); reviewed(s)
    s.meta['execution_mode'] = 'vision_agent'
    Image.new('RGB', (600, 800), 'white').save(s.pages[1])
    s.submit_grades(grade_payload(position_confidence=confidence))
    assert s.grades[0]['bbox_ok'] is accepted
    assert s.grades[0]['draw'] is accepted


@pytest.mark.parametrize('ids,text,complete', [([], '', False), (['p2:b1'], '3', True), (['p2:b9'], '2', True)])
def test_visual_box_recovers_missing_or_rejected_ocr(tmp_path, monkeypatch, ids, text, complete):
    s = state(tmp_path, monkeypatch); reviewed(s)
    Image.new('RGB', (600, 800), 'white').save(s.pages[1])
    s.ocr_pages[2] = {'page': 2, 'complete': complete, 'blocks': [
        {'id': 'p2:b1', 'text': text, 'box': [300, 300, 400, 350]}]}
    result = s.submit_grades(grade_payload(answer_block_ids=ids))
    q = s.grades[0]
    assert q['position_source'] == 'vision_fallback'
    assert q['bbox_px'] == [60, 160, 120, 208]
    assert q['draw'] and result['positioning_complete']
    assert q['ocr_block_ids'] == []


def test_valid_ocr_keeps_priority_over_visual_box(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch); reviewed(s)
    s.ocr_pages[2] = {'page': 2, 'complete': True, 'blocks': [
        {'id': 'p2:b1', 'text': '2', 'box': [300, 300, 400, 350]}]}
    s.submit_grades(grade_payload(answer_block_ids=['p2:b1']))
    assert s.grades[0]['position_source'] == 'ocr_blocks'
    assert s.grades[0]['bbox_norm'] == [300, 300, 400, 350]


@pytest.mark.parametrize('patch', [
    {'position_confidence': .5}, {'position_confidence': float('nan')},
    {'answer_bbox': [0, 0, 1000, 1000]}, {'answer_bbox': [900, 100, 1200, 200]},
    {'answer_state': 'blank', 'student_answer': ''}, {'answer_state': 'unreadable'},
])
def test_unreliable_visual_boxes_remain_for_manual_position(tmp_path, monkeypatch, patch):
    s = state(tmp_path, monkeypatch); reviewed(s)
    s.ocr_pages[2] = {'page': 2, 'complete': False, 'blocks': []}
    s.submit_grades(grade_payload(**patch))
    assert not s.grades[0]['bbox_ok'] and not s.grades[0]['draw']


def test_pending_verdict_keeps_visual_position_for_manual_override(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch); reviewed(s)
    s.answers[0]['review_status'] = 'review'
    s.ocr_pages[2] = {'page': 2, 'complete': False, 'blocks': []}
    s.submit_grades(grade_payload())
    assert s.grades[0]['status'] == 'review' and s.grades[0]['bbox_ok']
    assert not s.grades[0]['draw']
