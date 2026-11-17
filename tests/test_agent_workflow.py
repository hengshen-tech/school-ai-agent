"""Backend-enforced workflow contracts; no credentials or real student data."""
import os
import sys
import tempfile
from pathlib import Path
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="workflow-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import json
import pytest
from PIL import Image
from app import store
from app.agents import AgentError


def state(tmp_path, monkeypatch, mode='grade'):
    from app.workflow import WorkflowState
    monkeypatch.setattr(store, 'SUBS_DIR', tmp_path)
    monkeypatch.setattr(store, 'PAPERS_DIR', tmp_path / 'papers')
    monkeypatch.setattr(store, 'FILE_INDEX', tmp_path / 'index.json')
    pages = []
    for n in (1, 2):
        p = tmp_path / f'{n}.png'
        Image.new('RGB', (100, 100), 'white').save(p)
        pages.append(p)
    return WorkflowState('s', {}, {'mode': mode}, pages,
                         [{'page': 1, 'requested_role': 'question_paper'},
                          {'page': 2, 'requested_role': 'student_work' if mode == 'grade' else 'answer_key'}], lambda *a, **k: None)


def source():
    return {'title': 'Test', 'materials': [{'page': 1, 'role': 'question_paper'},
            {'page': 2, 'role': 'student_work'}], 'questions': [
            {'id': '1', 'type': 'calc', 'question': '1+1=', 'source_pages': [1]}]}


def draft():
    return {'answers': [{'id': '1', 'answer': '2', 'confident': True, 'edited': True}]}


@pytest.mark.parametrize('independent', ['', '3'])
def test_approval_without_agreeing_independent_evidence_stays_review(tmp_path, monkeypatch, independent):
    s = state(tmp_path, monkeypatch)
    s.submit_source(source()); s.submit_draft(draft())
    s.submit_review({'source_version': 1, 'draft_version': 1,
                     'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': independent}]})
    assert s.answers[0]['review_status'] == 'review'
    grade(s)
    assert s.grades[0]['status'] == 'review'


def test_equivalent_numeric_review_is_accepted(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch)
    s.submit_source(source())
    s.submit_draft({'answers': [{'id': '1', 'answer': '1/2', 'confident': True}]})
    s.submit_review({'source_version': 1, 'draft_version': 1,
                     'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '0.5'}]})
    assert s.answers[0]['review_status'] == 'approved'


@pytest.mark.parametrize('decimal,integer', [('.5', '5'), ('.125', '125')])
def test_leading_decimal_is_not_approved_as_an_integer(tmp_path, monkeypatch, decimal, integer):
    s = state(tmp_path, monkeypatch)
    s.submit_source(source())
    s.submit_draft({'answers': [{'id': '1', 'answer': decimal, 'confident': True}]})
    s.submit_review({'source_version': 1, 'draft_version': 1,
                     'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': integer}]})
    assert store.norm_answer(decimal) != store.norm_answer(integer)
    assert s.answers[0]['review_status'] == 'review'


def test_ocr_integer_cannot_locate_a_leading_decimal_answer(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch); reviewed(s)
    grade(s, student_answer='.5', verdict='wrong')
    s.ocr_pages[2] = {'blocks': [{'id': 'p2:b1', 'text': '5', 'box': [20, 20, 30, 30]}], 'complete': True}
    with pytest.raises(ValueError, match='学生答案'):
        s.submit_positions({'positions': [{'id': '1', 'page': 2, 'block_ids': ['p2:b1']}]})
    assert s.grades[0]['draw'] is False


def test_pending_grade_keeps_validated_location_for_manual_override(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch); reviewed(s)
    grade(s, confidence=.4)
    s.ocr_pages[2] = {'blocks': [{'id': 'p2:b1', 'text': '2', 'box': [20, 20, 30, 30]}], 'complete': True}
    s.submit_positions({'positions': [{'id': '1', 'page': 2, 'block_ids': ['p2:b1']}]})
    q = s.grades[0]
    assert q['status'] == 'review' and q['draw'] is False
    assert q['bbox_ok'] and q['bbox_px'] and q['ocr_block_ids'] == ['p2:b1']


@pytest.mark.asyncio
async def test_direct_page_input_limits_image_dimensions(tmp_path, monkeypatch):
    import base64
    import io
    from app import imaging
    s = state(tmp_path, monkeypatch)
    monkeypatch.setattr(imaging, 'MODEL_IMAGE_MAX_SIDE', 80)
    block = imaging.page_block(s.pages[0])['source']
    with Image.open(io.BytesIO(base64.b64decode(block['data']))) as image:
        assert max(image.size) <= 80


def attempted_ocr(s):
    for m in s.source['materials']:
        if m['role'] == 'student_work':
            s.ocr_pages.setdefault(m['page'], {'blocks': [], 'complete': False})


def reviewed(s):
    s.submit_source(source())
    s.submit_draft(draft())
    s.submit_review({'source_version': s.source_version, 'draft_version': s.draft_version,
                     'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2', 'reason': 'checked'}]})


def grade(s, **kw):
    return s.submit_grades({'source_version': s.source_version, 'draft_version': s.draft_version,
        'results': [{'id': '1', 'page': 2, 'student_answer': '2', 'answer_state': 'answered',
                     'verdict': 'correct', 'reason': 'equal', 'confidence': .99, **kw}]})


def test_workflow_exists():
    from app import workflow
    assert callable(getattr(workflow, 'run_workflow', None))


def test_source_version_review_required_and_model_edited_not_trusted(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch)
    s.submit_source(source()); s.submit_draft(draft())
    assert not s.answers[0].get('edited')
    with pytest.raises(ValueError): grade(s)
    with pytest.raises(ValueError):
        s.submit_review({'source_version': 0, 'draft_version': 1, 'reviews': []})
    s.submit_review({'source_version': 1, 'draft_version': 1,
                     'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
    grade(s)
    changed = source(); changed['questions'][0]['question'] = '2+2='
    s.submit_source(changed)
    assert s.grades is None and s.review is None


def test_prepare_roles_multiple_students_and_complete_coverage(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch, 'prepare')
    data = source()
    data['materials'][1]['role'] = 'answer_key'
    s.submit_source(data)
    data['multiple_students'] = True
    with pytest.raises(ValueError): s.submit_source(data)
    s = state(tmp_path, monkeypatch)
    with pytest.raises(ValueError): s.submit_source({**source(), 'materials': []})
    reviewed(s)
    with pytest.raises(ValueError): s.submit_grades({'source_version': 1, 'draft_version': 1, 'results': []})


@pytest.mark.parametrize('extra', [{'answer_state': 'unreadable'}, {'key_doubt': True},
                                  {'answer_state': 'unaligned'}, {'answer_state': 'answered', 'student_answer': ''}])
def test_uncertain_grades_never_become_wrong_or_draw(tmp_path, monkeypatch, extra):
    s = state(tmp_path, monkeypatch); reviewed(s); grade(s, verdict='wrong', **extra)
    assert s.grades[0]['status'] == 'review'
    assert not s.grades[0]['draw']


def test_position_validation_page_qualified_no_reuse_and_no_guessed_box(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch); reviewed(s); grade(s)
    s.ocr_pages[2] = {'blocks': [{'id': 'p2:b1', 'text': '2', 'box': [20, 20, 30, 30]}], 'complete': True}
    with pytest.raises(ValueError): s.submit_positions({'positions': [{'id': '1', 'page': 1, 'block_ids': ['p2:b1']}]})
    with pytest.raises(ValueError): s.submit_positions({'positions': [{'id': '1', 'page': 2, 'block_ids': ['p1:b1']}]})
    s.submit_positions({'positions': [{'id': '1', 'page': 2, 'block_ids': ['p2:b1'], 'answer_bbox': [0, 0, 999, 999]}]})
    assert s.grades[0]['bbox_norm'] == [20, 20, 30, 30]
    assert s.grades[0]['draw']
    s.submit_positions({'positions': []})
    assert s.grades[0]['draw']
    s.submit_positions({'positions': [{'id': '1', 'page': 2, 'block_ids': []}]})
    assert not s.grades[0]['draw'] and s.grades[0]['position_note']




@pytest.mark.parametrize('mode', ['grade', 'prepare'])
def test_finalize_requires_artifacts_and_returns_compatible_result(tmp_path, monkeypatch, mode):
    from app import pipeline
    s = state(tmp_path, monkeypatch, mode)
    with pytest.raises(AgentError): s.finalize()
    data = source()
    if mode == 'prepare': data['materials'][1]['role'] = 'answer_key'
    s.submit_source(data); s.submit_draft(draft())
    s.submit_review({'source_version': 1, 'draft_version': 1, 'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
    if mode == 'grade':
        with pytest.raises(AgentError): s.finalize()
        grade(s)
        attempted_ocr(s)
        s.submit_positions({'positions': []})
    monkeypatch.setattr(pipeline, 'render_annotations', lambda *a: None)
    result = s.finalize()
    assert result['mode'] == mode and result['workflow_version'] == 2
    assert result['annotation_version'] == 4 and result['position_method'] == 'ocr_blocks'
    assert len(result['questions']) == (1 if mode == 'grade' else 0)
    assert store.get_result('s')['paper_id'] == s.paper['id']


def test_teacher_conflict_and_low_confidence_force_review(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch)
    s.submit_source(source())
    s.submit_draft({'answers': [{'id': '1', 'answer': '2', 'teacher_answer': '3', 'ai_answer': '2', 'confident': False}]})
    s.submit_review({'source_version': 1, 'draft_version': 1, 'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
    assert s.answers[0]['review_status'] == 'review'
    grade(s)
    assert s.grades[0]['status'] == 'review'


def test_selected_paper_manual_answers_are_preserved(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    monkeypatch.setattr(store, 'get_paper', lambda pid: {'id': pid, 'questions': source()['questions'],
        'answers': [{'id': '1', 'answer': 'teacher', 'edited': True, 'review_status': 'manual_confirmed'}]})
    s0 = state(tmp_path, monkeypatch)
    s = WorkflowState('s', {}, {'mode': 'grade', 'paper_id': 'p'}, s0.pages, s0.manifest, s0.emit)
    s.submit_source(source()); s.submit_draft(draft())
    s.submit_review({'source_version': 1, 'draft_version': s.draft_version, 'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
    assert s.answers[0]['answer'] == 'teacher'
    assert s.answers[0]['review_status'] == 'manual_confirmed'


def test_review_publishes_before_grading_and_reuses_without_revision_bump(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    monkeypatch.setattr(store, 'PAPERS_DIR', tmp_path / 'papers')
    monkeypatch.setattr(store, 'FILE_INDEX', tmp_path / 'index.json')
    first = state(tmp_path, monkeypatch)
    reviewed(first)
    assert first.paper and store.get_paper(first.paper['id'])['answers'][0]['review_status'] == 'approved'
    assert first.grades is None
    pid, rev = first.paper['id'], first.paper['rev']
    second = WorkflowState('second', {}, {'mode': 'grade', 'paper_id': pid}, first.pages, first.manifest, first.emit)
    second.submit_source(source())
    assert second.review and second.answers[0]['answer'] == '2'
    grade(second); attempted_ocr(second); second.submit_positions({'positions': []})
    monkeypatch.setattr('app.pipeline.render_annotations', lambda *a: None)
    result = second.finalize()
    assert result['paper_cache_hit']
    assert store.get_paper(pid)['rev'] == rev


def test_new_teacher_reference_disables_reviewed_cache(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    monkeypatch.setattr(store, 'PAPERS_DIR', tmp_path / 'papers')
    first = state(tmp_path, monkeypatch); reviewed(first)
    second = WorkflowState('second', {}, {'mode': 'grade', 'paper_id': first.paper['id']}, first.pages, first.manifest, first.emit, '1:3')
    second.submit_source(source())
    assert second.review is None and not second.answers


def test_answer_key_image_without_transcription_cannot_skip_review(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    first = state(tmp_path, monkeypatch); reviewed(first)
    incoming = WorkflowState('new-key', {}, {'mode': 'grade', 'paper_id': first.paper['id']},
        [*first.pages, first.pages[0]], [*first.manifest, {'page': 3, 'requested_role': 'answer_key'}], first.emit)
    data = source(); data['materials'].append({'page': 3, 'role': 'answer_key'})
    incoming.submit_source(data)
    assert incoming.review is None and incoming.paper_cache_hit is False
    incoming.submit_draft(draft())
    incoming.submit_review({'source_version': 1, 'draft_version': 1,
        'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
    assert incoming.answers[0]['review_status'] == 'review'


def test_different_question_images_do_not_implicitly_share_a_paper(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    from PIL import ImageDraw
    first = state(tmp_path, monkeypatch)
    store.PAPERS_DIR.mkdir()
    with Image.open(first.pages[0]) as image:
        ImageDraw.Draw(image).rectangle((0, 0, 49, 99), fill='gray')
        image.save(first.pages[0])
    data = source()
    data['questions'][0]['question'] = '观察下面的图形，写出阴影部分占整个图形的几分之几？'
    first.submit_source(data)
    first.submit_draft({'answers': [{'id': '1', 'answer': '1/2', 'confident': True}]})
    first.submit_review({'source_version': 1, 'draft_version': 1,
        'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '1/2'}]})
    different = tmp_path / 'quarter.png'
    image = Image.new('RGB', (100, 100), 'white')
    ImageDraw.Draw(image).rectangle((0, 0, 24, 99), fill='gray'); image.save(different)
    second = WorkflowState('variant', {}, {'mode': 'grade'}, [different, first.pages[1]], first.manifest, first.emit)
    second.submit_source(data)
    assert second.paper is None and second.review is None and second.paper_cache_hit is False
    second.submit_draft({'answers': [{'id': '1', 'answer': '1/4', 'confident': True}]})
    second.submit_review({'source_version': 1, 'draft_version': 1,
        'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '1/4'}]})
    assert second.paper['id'] != first.paper['id']
    assert store.get_paper(first.paper['id'])['answers'][0]['answer'] == '1/2'


def test_identical_source_images_can_still_reuse_reviewed_answers(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    first = state(tmp_path, monkeypatch)
    store.PAPERS_DIR.mkdir()
    data = source(); data['questions'][0]['question'] = '请观察题目中的图片，独立完成计算：1+1='
    first.submit_source(data); first.submit_draft(draft())
    first.submit_review({'source_version': 1, 'draft_version': 1,
        'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
    second = WorkflowState('same-images', {}, {'mode': 'grade'}, first.pages, first.manifest, first.emit)
    second.submit_source(data)
    assert second.paper_cache_hit and second.review
    assert second.paper['id'] == first.paper['id']


def test_regrade_uses_confirmed_key_instead_of_original_answer_material(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    first = state(tmp_path, monkeypatch); reviewed(first)
    store.update_answers(first.paper['id'], [{'id': '1', 'answer': '3'}])
    meta = {'mode': 'grade', 'paper_id': first.paper['id'], 'prefer_saved_answers': True}
    manifest = [*first.manifest, {'page': 3, 'requested_role': 'answer_key'}]
    regrade_state = WorkflowState('regrade', {}, meta, [*first.pages, first.pages[0]], manifest, first.emit)
    data = source()
    data['materials'].append({'page': 3, 'role': 'answer_key'})
    data['reference_answers'] = [{'id': '1', 'answer': '2'}]
    regrade_state.submit_source(data)
    assert regrade_state.review is not None
    assert regrade_state.answers[0]['answer'] == '3'
    regrade_state.submit_draft(draft())
    assert regrade_state.answers[0]['answer'] == '3'
    assert regrade_state.answers[0]['review_status'] == 'manual_confirmed'


def test_answer_ids_are_normalized_across_all_stage_submissions(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch)
    s.submit_source(source())
    s.submit_draft({'answers': [{'id': ' 1 ', 'answer': '2', 'confident': True}]})
    assert s.answers[0]['id'] == '1'
    s.submit_review({'source_version': 1, 'draft_version': 1,
                     'reviews': [{'id': ' 1 ', 'status': 'approved', 'independent_answer': '2'}]})
    s.submit_grades({'source_version': 1, 'draft_version': 1, 'results': [
        {'id': ' 1 ', 'page': 2, 'student_answer': '2', 'answer_state': 'answered', 'verdict': 'correct', 'confidence': .99}]})
    assert s.grades[0]['id'] == '1' and s.grades[0]['status'] == 'correct'


def test_retry_restores_review_only_when_context_and_paper_revision_match(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    first = state(tmp_path, monkeypatch); reviewed(first)
    checkpoint = json.loads((store.sub_dir('s') / 'workflow-checkpoint.json').read_text())
    store.save_artifact('retry', 'workflow-checkpoint.json', checkpoint)
    retry = WorkflowState('retry', {}, {'mode': 'grade', 'retry_of': 's'}, first.pages, first.manifest, first.emit)
    assert retry.restore_checkpoint()
    assert retry.review and retry.grades is None
    revision = store.get_paper(first.paper['id'])['rev']
    assert revision == first.paper['rev']
    changed = WorkflowState('changed', {}, {'mode': 'grade', 'retry_of': 's'}, first.pages, first.manifest, first.emit, 'new teacher key')
    store.save_artifact('changed', 'workflow-checkpoint.json', checkpoint)
    assert not changed.restore_checkpoint()
    store.update_answers(first.paper['id'], [{'id': '1', 'answer': '3'}])
    store.save_artifact('stale', 'workflow-checkpoint.json', checkpoint)
    stale = WorkflowState('stale', {}, {'mode': 'grade', 'retry_of': 's'}, first.pages, first.manifest, first.emit)
    assert not stale.restore_checkpoint()


def test_selected_paper_snapshot_maps_sources_to_archived_references(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    first = state(tmp_path, monkeypatch); reviewed(first)
    second = WorkflowState('second', {}, {'paper_id': first.paper['id']}, [first.pages[1]],
        [{'page': 1, 'requested_role': 'student_work'}], first.emit)
    question = second.snapshot()['selected_paper']['questions'][0]
    assert question['source_pages'] == [] and question['reference_pages'] == [1]
    second.submit_source({'materials': [{'page': 1, 'role': 'student_work'}], 'questions': [question]})
    assert second.review


def test_unreadable_key_is_retained_without_inventing_answer(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch); s.submit_source(source())
    s.submit_draft({'answers': [{'id': '1', 'answer': '', 'confident': False}]})
    s.submit_review({'source_version': 1, 'draft_version': 1, 'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
    assert s.answers[0]['answer'] == '' and s.answers[0]['review_status'] == 'review'
    grade(s)
    assert s.grades[0]['status'] == 'review'


def test_positions_require_ocr_attempt_for_every_student_page(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch); reviewed(s); grade(s)
    with pytest.raises(ValueError, match='OCR'):
        s.submit_positions({'positions': []})


def test_new_teacher_key_keeps_original_question_archive(tmp_path, monkeypatch):
    from app.workflow import WorkflowState
    first = state(tmp_path, monkeypatch); reviewed(first)
    question = first.snapshot()['selected_paper']['questions'][0]
    next_state = WorkflowState('key-update', {}, {'mode': 'grade', 'paper_id': first.paper['id']}, first.pages,
                              [{'page': 1, 'requested_role': 'answer_key'}, {'page': 2, 'requested_role': 'student_work'}], first.emit)
    next_state.submit_source({'materials': [{'page': 1}, {'page': 2}], 'questions': [question],
                              'reference_answers': [{'id': '1', 'answer': '2'}]})
    next_state.submit_draft(draft())
    next_state.submit_review({'source_version': 1, 'draft_version': 1,
                              'reviews': [{'id': '1', 'status': 'approved', 'independent_answer': '2'}]})
    paper = store.get_paper(first.paper['id'])
    assert len(paper['reference_pages']) == 2
    assert paper['reference_pages'][0]['role'] == 'question_paper'
    assert paper['reference_pages'][1]['role'] == 'answer_key'
    assert paper['questions'][0]['reference_pages'] == [1]
