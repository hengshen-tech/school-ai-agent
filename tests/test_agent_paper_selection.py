"""The single Agent can read and select a library key before grading, with no paid calls."""
import json
from pathlib import Path

import pytest

from app import store, submission_agent
from test_agent_workflow import source, state


def saved_paper():
    return store.save_reviewed_paper('加法练习', [{'id': '1', 'type': 'calc', 'question': '1+1='}],
        [{'id': '1', 'answer': '2', 'review_status': 'ai_solved'}], 'ai')


async def read_file(kwargs, path):
    data = {'tool_name': 'Read', 'tool_input': {'file_path': str(path)}}
    pre = kwargs['hooks']['PreToolUse'][0].hooks[0]
    permission = await pre(data, 'read', None)
    assert permission['hookSpecificOutput']['permissionDecision'] == 'allow'
    value = json.loads(Path(path).read_text())
    post = kwargs['hooks']['PostToolUse'][0].hooks[0]
    await post({**data, 'tool_response': {'content': 'read'}}, 'read', None)
    return value


@pytest.mark.asyncio
async def test_agent_reads_library_and_reuses_key_before_grading(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch)
    s.meta['execution_mode'] = 'vision_agent'
    pid = saved_paper()
    calls = []
    async def agent(cfg, **kwargs):
        calls.append(kwargs)
        context = json.loads(kwargs['blocks'][0]['text'])
        assert context['selected_paper'] is None
        index = await read_file(kwargs, context['paper_library']['index_path'])
        listing = await read_file(kwargs, index['catalog_paths'][0])
        entry = next(p for p in listing['papers'] if p['id'] == pid)
        paper = await read_file(kwargs, entry['path'])
        assert paper['answers'][0]['answer'] == '2'
        value = {'selected_paper_id': pid, 'selected_paper_rev': paper['rev'],
            'paper_match_reason': '题干、数字与小题对应一致',
            'materials': source()['materials'], 'results': [
                {'id': '1', 'page': 2, 'student_answer': '2', 'answer_state': 'answered',
                 'verdict': 'correct', 'confidence': .99,
                 'answer_bbox': [300, 300, 400, 350], 'position_confidence': .99}]}
        assert kwargs['result_filter'](value)
        return value
    monkeypatch.setattr(submission_agent, 'run_json_agent', agent)
    value = await submission_agent.run_single_agent({}, s)
    s.submit_single_result(value)
    assert len(calls) == 1
    assert s.paper['id'] == pid and s.paper_cache_hit
    assert s.answers[0]['answer'] == '2' and s.grades[0]['status'] == 'correct'
    assert s.grades[0]['draw']
    page_dir = store.sub_dir(s.sid) / 'pages'
    page_dir.mkdir()
    for number, path in enumerate(s.pages, 1):
        (page_dir / f'page-{number}.png').write_bytes(path.read_bytes())
    result = s.finalize()
    assert result['paper_id'] == pid and result['paper_cache_hit']
    assert store.get_paper(pid)['rev'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unread', 'revision', 'unknown'])
async def test_selection_rejects_unread_or_stale_library_keys(tmp_path, monkeypatch, failure):
    s = state(tmp_path, monkeypatch)
    pid = saved_paper()
    async def agent(cfg, **kwargs):
        context = json.loads(kwargs['blocks'][0]['text'])
        index = await read_file(kwargs, context['paper_library']['index_path'])
        listing = await read_file(kwargs, index['catalog_paths'][0])
        entry = listing['papers'][0]
        if failure != 'unread':
            await read_file(kwargs, entry['path'])
        if failure == 'revision':
            store.update_answers(pid, [{'id': '1', 'answer': '3'}])
        return {'selected_paper_id': 'paper_unknown' if failure == 'unknown' else pid,
                'selected_paper_rev': 0, 'paper_match_reason': 'same', 'results': []}
    monkeypatch.setattr(submission_agent, 'run_json_agent', agent)
    with pytest.raises(ValueError, match='试卷'):
        await submission_agent.run_single_agent({}, s)
    assert s.paper is None


@pytest.mark.asyncio
async def test_agent_no_match_keeps_new_paper_solution(tmp_path, monkeypatch):
    from test_single_agent import response
    s = state(tmp_path, monkeypatch)
    saved_paper()
    async def agent(cfg, **kwargs):
        value = response()
        value.update(selected_paper_id=None, paper_match_reason='题干不同')
        assert kwargs['result_filter'](value)
        return value
    monkeypatch.setattr(submission_agent, 'run_json_agent', agent)
    assert (await submission_agent.run_single_agent({}, s))['answers'][0]['answer'] == '2'
    assert s.paper is None


def test_unselected_vision_result_does_not_trigger_late_programmatic_matching(tmp_path, monkeypatch):
    s = state(tmp_path, monkeypatch)
    s.meta['execution_mode'] = 'vision_agent'
    saved_paper()
    monkeypatch.setattr(store, 'match_paper', lambda *args: pytest.fail('Agent owns same-paper selection'))
    s.submit_source(source())
    assert s.paper is None and s.paper_cache_hit is False


def test_catalog_is_paginated_and_does_not_inline_entire_answer_library(tmp_path, monkeypatch):
    from app.paper_library import PaperLibrary
    state(tmp_path, monkeypatch)
    for i in range(21):
        store.save_reviewed_paper(f'卷 {i}', source()['questions'], [{'id': '1', 'answer': '2'}], 'ai')
    library = PaperLibrary(tmp_path / 'agent')
    index = json.loads(Path(library.context['index_path']).read_text())
    assert index['paper_count'] == 21 and len(index['catalog_paths']) == 2
    listing = json.loads(Path(index['catalog_paths'][0]).read_text())
    assert len(listing['papers']) == 20
    assert all('answers' not in item for item in listing['papers'])
