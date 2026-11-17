"""Model output boundaries and ambiguity handling, without model calls."""
import json

import pytest

from app.agents import extract_json


def complete(answer='2'):
    return {'questions': [{'id': '1', 'question': '1+1='}], 'answers': [{'id': '1', 'answer': answer}],
        'results': [{'id': '1', 'student_answer': '2', 'page': 1, 'verdict': 'correct'}]}


def grading(obj):
    return all(isinstance(obj.get(key), list) for key in ('questions', 'answers', 'results'))


@pytest.mark.parametrize('wrap', [lambda s: s, lambda s: '\ufeff说明\n' + s + '\n完毕',
    lambda s: '```json\n' + s + '\n```'])
def test_single_json_with_nested_and_escaped_braces(wrap):
    value = {'question': '集合 {x} 与字符串 "a}b"', 'nested': {'items': [{'answer': '2'}]}}
    assert extract_json(wrap(json.dumps(value))) == value


@pytest.mark.parametrize('separator', ['', '\n', '\n```\n说明\n```json\n'])
def test_unique_grading_result_is_selected_among_tool_json(separator):
    text = json.dumps({'expression': '1+1', 'result': '2'}) + separator + json.dumps(complete())
    assert extract_json(text, result_filter=grading) == complete()


def test_repeated_identical_complete_json_is_not_ambiguous():
    value = complete()
    assert extract_json(json.dumps(value) + json.dumps(value, indent=2), result_filter=grading) == value


def test_two_conflicting_complete_results_are_rejected():
    with pytest.raises(ValueError, match='多份'):
        extract_json(json.dumps(complete('2')) + json.dumps(complete('3')), result_filter=grading)


def test_multiple_generic_objects_are_not_arbitrarily_selected():
    with pytest.raises(ValueError, match='多份'):
        extract_json('{"a":1}\n{"b":2}')


@pytest.mark.parametrize('text', ['[{"ok":true}]', '{"outer":{"ok":true}', '{"outer":{"ok":true},}'])
def test_array_or_malformed_parent_cannot_be_misread_as_inner_object(text):
    with pytest.raises(ValueError): extract_json(text)


def test_split_reports_are_not_unsafely_merged():
    with pytest.raises(ValueError, match='预期结构'):
        extract_json('{"answers":[]}\n{"results":[]}', result_filter=grading)


@pytest.mark.asyncio
async def test_json_filter_does_not_trigger_another_model_call(monkeypatch):
    from app import agents
    calls = []
    async def run(cfg, **kwargs):
        calls.append(kwargs)
        return '{"crop":"page.png"}\n' + json.dumps(complete())
    monkeypatch.setattr(agents, 'run_agent', run)
    result = await agents.run_json_agent({}, system='test', blocks=[], attempts=1, result_filter=grading)
    assert result == complete() and len(calls) == 1
    assert 'result_filter' not in calls[0]
