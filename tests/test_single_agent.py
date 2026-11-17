"""One prompt-driven submission request, with pictures and OCR supplied directly."""

import pytest

from test_jobs import image_bytes, isolated_store  # noqa: F401
from app import config, ocr, store, workflow


def response(answer='2', student='2'):
    return {'title': 'Quiz', 'student_name': '', 'student_no': '', 'multiple_students': False,
        'questions': [{'id': '1', 'type': 'calc', 'question': '1+1=', 'source_pages': [1]}],
        'answers': [{'id': '1', 'answer': answer, 'teacher_answer': '2', 'confident': True, 'knowledge': '加法'}],
        'results': [{'id': '1', 'page': 2, 'student_answer': student, 'answer_state': 'answered',
                     'verdict': 'correct', 'confidence': .99, 'answer_block_ids': ['p2:b1']}]}




@pytest.mark.asyncio
@pytest.mark.parametrize('subtype', ['success', 'error_max_turns'])
async def test_single_sdk_query_attaches_images_without_tools_or_mcp(monkeypatch, subtype):
    from app import agents
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    captured, messages = [], []
    class Client:
        def __init__(self, options): captured.append(options)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def query(self, prompt):
            async for message in prompt: messages.append(message)
        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text='{"ok":true}')], model='mock')
            yield ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=subtype != 'success',
                num_turns=1, session_id='s', result='{"ok":true}')
    monkeypatch.setattr(agents, 'ClaudeSDKClient', Client)
    call = agents.run_json_agent({'base_url': 'https://local.invalid', 'api_key': 'fake', 'model': 'mock'},
        system='test', blocks=[{'type': 'text', 'text': 'test'}, {'type': 'image', 'source': {}}],
        attempts=1, require_success=True)
    if subtype != 'success':
        with pytest.raises(agents.AgentError): await call
    else:
        assert await call == {'ok': True}
    assert len(captured) == len(messages) == 1
    options = captured[0]
    assert options.tools == [] and options.mcp_servers == {} and options.agents is None
    assert options.max_turns == 1
    assert messages[0]['message']['content'][1]['type'] == 'image'


@pytest.mark.parametrize('teacher,confident,expected', [('2', True, 'ai_solved'), ('3', True, 'review'), ('2', False, 'ai_solved')])
def test_single_solution_accepts_ai_keys_but_preserves_teacher_disputes(tmp_path, monkeypatch, teacher, confident, expected):
    from test_agent_workflow import state
    s = state(tmp_path, monkeypatch)
    for material in s.manifest:
        material.update(classified_role=material['requested_role'], classification_confidence=1.)
    s.teacher_key = '1: ' + teacher
    s.ocr_pages[2] = {'page': 2, 'complete': True,
        'blocks': [{'id': 'p2:b1', 'text': '2', 'box': [300, 300, 400, 350]}]}
    data = response()
    data['answers'][0].update(teacher_answer=teacher, confident=confident)
    s.submit_single_result(data)
    assert s.answers[0]['review_status'] == expected
    assert s.answers[0]['answer'] == '2'
    assert s.grades[0]['draw'] is (expected == 'ai_solved')


def test_legacy_flat_id_can_display_original_hierarchical_label(tmp_path, monkeypatch):
    from test_agent_workflow import state, reviewed
    s = state(tmp_path, monkeypatch); reviewed(s)
    s.source['questions'][0]['question_label'] = '1'
    s.ocr_pages[2] = {'page': 2, 'complete': False, 'blocks': []}
    s.submit_grades({'source_version': 1, 'draft_version': 1, 'results': [
        {'id': '1', 'question_label': '2.1', 'page': 2, 'student_answer': '2',
         'answer_state': 'answered', 'verdict': 'correct', 'confidence': .99}]})
    q = s.grades[0]
    assert q['id'] == s.answers[0]['id'] == '1'
    assert q['question_label'] == '2.1' and q['standard_answer'] == '2'
