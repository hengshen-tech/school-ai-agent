"""Focused performance contracts, with no remote model calls."""
import asyncio

import pytest

from test_agent_workflow import state, reviewed
from app import agents, ocr, store, workflow




@pytest.mark.asyncio
async def test_low_effort_reaches_sdk_and_metrics(monkeypatch):
    captured, metrics = [], []
    class Client:
        def __init__(self, options): captured.append(options)
        async def __aenter__(self): raise agents.AgentError('captured')
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(agents, 'ClaudeSDKClient', Client)
    with pytest.raises(agents.AgentError, match='captured'):
        await agents.run_agent({'model': 'mock', 'base_url': 'http://localhost', 'api_key': 'fake'},
            system='classify', blocks=[], effort='low', emit=lambda *a, **kw: metrics.append(kw))
    assert captured[0].effort == 'low'
    assert captured[0].env['CLAUDE_CODE_EFFORT_LEVEL'] == 'low'
    assert [m['metric']['effort'] for m in metrics if 'metric' in m] == ['low']


@pytest.mark.parametrize('stale', [False, True])
def test_selected_paper_is_bound_by_id_and_revision_without_reemitting_questions(tmp_path, monkeypatch, stale):
    s = state(tmp_path, monkeypatch); reviewed(s)
    next_state = workflow.WorkflowState('second', {}, {'paper_id': s.paper['id']}, [s.pages[1]],
        [{'page': 1, 'requested_role': 'student_work'}], s.emit)
    payload = {'use_selected_paper': True, 'paper_id': s.paper['id'], 'paper_rev': s.paper['rev'],
               'materials': [{'page': 1}]}
    if stale:
        store.update_answers(s.paper['id'], [{'id': '1', 'answer': '3'}])
        with pytest.raises(ValueError, match='版本'):
            next_state.submit_source(payload)
    else:
        next_state.submit_source(payload)
        assert next_state.source['questions'][0]['question'] == '1+1='
        assert next_state.source['questions'][0]['reference_pages'] == [1]
        assert next_state.review and next_state.paper_cache_hit


@pytest.mark.parametrize('answer,block,complete', [('2', '2', True), ('2', '3', False), ('', '', True)])
def test_grading_auto_completes_only_valid_positions_or_confirmed_blanks(tmp_path, monkeypatch, answer, block, complete):
    s = state(tmp_path, monkeypatch); reviewed(s)
    s.ocr_pages[2] = {'page': 2, 'blocks': [{'id': 'p2:b1', 'text': block, 'box': [20, 20, 30, 30]}], 'complete': True}
    result = s.submit_grades({'source_version': 1, 'draft_version': 1, 'results': [
        {'id': '1', 'page': 2, 'student_answer': answer, 'answer_state': 'answered' if answer else 'blank',
         'verdict': 'correct', 'confidence': .99, 'answer_block_ids': ['p2:b1'] if answer else []}]})
    assert result['positioning_complete'] is complete
    assert s.positions_submitted is complete
    assert result['unpositioned'] == ([] if complete else ['1'])
    if complete:
        folder = store.sub_dir(s.sid) / 'pages'
        folder.mkdir(parents=True, exist_ok=True)
        for i, path in enumerate(s.pages, 1):
            (folder / f'page-{i}.png').write_bytes(path.read_bytes())
        assert s.finalize()['status'] == 'done'


