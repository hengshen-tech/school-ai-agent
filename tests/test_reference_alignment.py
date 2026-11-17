"""Teacher-reference alignment is conservative and never aborts unrelated grades."""
import pytest

from test_agent_workflow import state


def test_backend_accepts_agent_ids_but_never_infers_ids_from_teacher_labels():
    from app.reference_answers import validate_agent_references
    questions = [{'id': '1', 'question_label': '17(1)'}]
    valid = validate_agent_references(questions, [{'id': '1', 'teacher_question_label': '１７（１）', 'answer': '2'}])
    assert valid['aligned'][0]['id'] == '1' and not valid['unaligned']
    invalid = validate_agent_references(questions, [{'id': '17.1', 'answer': '2'}])
    assert not invalid['aligned'] and invalid['unaligned']


def test_agent_reported_uncertainty_and_duplicate_conflicts_do_not_abort():
    from app.reference_answers import validate_agent_references
    report = validate_agent_references([{'id': 'a'}, {'id': 'b'}],
        [{'id': 'b', 'answer': '3'}, {'id': 'b', 'answer': '4'}],
        [{'original_id': '17(1)', 'answer': '2', 'candidate_ids': ['a'], 'reason': '题干不清'}])
    assert not report['aligned'] and set(report['issues']) == {'a', 'b'}
    assert report['warnings'] and report['unaligned']


@pytest.mark.parametrize('references', [
    [{'id': '1', 'teacher_question_label': '17.1', 'answer': '2'}, {'id': '99', 'answer': '4'}],
    [{'id': '1', 'answer': '2'}, {'id': '2', 'answer': '4'}, {'id': '2', 'answer': '5'}],
])
def test_aligned_question_is_graded_while_problem_reference_stays_review(tmp_path, monkeypatch, references):
    from app import pipeline, store
    from app.workflow import WorkflowState
    base = state(tmp_path, monkeypatch)
    manifest = [*base.manifest, {'page': 3, 'requested_role': 'answer_key'}]
    s = WorkflowState('s', {}, {'mode': 'grade', 'execution_mode': 'vision_agent', 'filename': 'quiz'},
        [*base.pages, base.pages[0]], manifest, lambda *a, **k: None)
    payload = {'title': 'Quiz', 'materials': [{'page': 1, 'role': 'question_paper'},
        {'page': 2, 'role': 'student_work'}, {'page': 3, 'role': 'answer_key'}],
        'questions': [{'id': str(i), 'question_label': f'17({i})', 'type': 'calc',
            'question': f'{i}+{i}=', 'source_pages': [1]} for i in (1, 2)],
        'reference_answers': references,
        'answers': [{'id': str(i), 'answer': str(i * 2), 'teacher_answer': str(i * 2), 'confident': True} for i in (1, 2)],
        'results': [{'id': str(i), 'page': 2, 'student_answer': str(i * 2), 'answer_state': 'answered',
            'verdict': 'correct', 'confidence': .99, 'answer_bbox': [100, i * 200, 200, i * 200 + 50],
            'position_confidence': .99} for i in (1, 2)]}
    s.submit_single_result(payload)
    assert [q['status'] for q in s.grades] == ['correct', 'review']
    assert s.grades[1]['standard_answer'] == '4'
    assert s.grades[1]['reasons']
    monkeypatch.setattr(pipeline, 'render_annotations', lambda *a: None)
    result = s.finalize()
    assert result['status'] == 'done' and result['warnings']
    assert result['unaligned_reference_answers']
    assert store.get_result('s')['summary']['correct'] == 1
