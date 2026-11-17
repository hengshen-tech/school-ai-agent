"""Single-Agent console diagnostics and one-time thinking activity."""
import json
import logging

import pytest
from PIL import Image


@pytest.mark.asyncio
async def test_single_agent_logs_content_without_keys_images_or_thinking(monkeypatch, caplog):
    from app import agents
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    from claude_agent_sdk.types import StreamEvent
    options, events = [], []
    caplog.set_level(logging.INFO, logger='uvicorn.error')
    class Client:
        def __init__(self, value): options.append(value)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def query(self, prompt):
            async for _ in prompt: pass
        async def receive_response(self):
            for index in range(3):
                yield StreamEvent(uuid=str(index), session_id='s', event={
                    'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': 'private reasoning'}})
            yield AssistantMessage(content=[TextBlock(text='{"ok":true}')], model='mock')
            yield ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id='s', result='{"ok":true}')
    monkeypatch.setattr(agents, 'ClaudeSDKClient', Client)
    result = await agents.run_json_agent({'base_url': 'https://local.invalid', 'api_key': 'do-not-log-key', 'model': 'mock'},
        system='grading instructions', blocks=[{'type': 'text', 'text': 'task instructions'},
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'do-not-log-image'}}],
        attempts=1, require_success=True, submission_id='sub_log', emit=lambda *a, **k: events.append((a, k)))
    assert result == {'ok': True} and options[0].agents is None
    assert options[0].effort == 'low' and options[0].env['CLAUDE_CODE_EFFORT_LEVEL'] == 'low'
    assert [k['metric']['effort'] for _, k in events if k.get('kind') == 'metric'] == ['low']
    assert 'grading instructions' in caplog.text and 'task instructions' in caplog.text
    assert 'sub_log' in caplog.text and '{\\"ok\\":true}' in caplog.text
    assert 'do-not-log-key' not in caplog.text and 'do-not-log-image' not in caplog.text
    assert 'private reasoning' not in caplog.text
    assert len([k for _, k in events if k.get('activity_type') == 'thinking']) == 1


@pytest.mark.asyncio
async def test_single_agent_tools_keep_detailed_console_logs(tmp_path, caplog):
    from app.visual_tools import VisualTools
    caplog.set_level(logging.INFO, logger='uvicorn.error')
    page = tmp_path / 'paper.png'; Image.new('RGB', (100, 200), 'white').save(page)
    tools = VisualTools([page], [], tmp_path / 'work', submission_id='sub_tools')
    requests = [('Read', {'file_path': str(page)}),
        ('Bash', {'command': tools.command_prefix + ' crop --page 1 --bbox 100 200 400 500 --scale 2'}),
        ('Bash', {'command': tools.command_prefix + " calc --expression '1/2+1/4'"})]
    for name, args in requests:
        result = await tools.authorize({'tool_name': name, 'tool_input': args}, 'tool-id', None)
        assert result['hookSpecificOutput']['permissionDecision'] == 'allow'
    await tools.tool_finished({'tool_name': 'Bash', 'tool_response': {'stdout': json.dumps({'result': '3/4'})}}, 'tool-id', None)
    await tools.tool_finished({'tool_name': 'Read', 'tool_response': {'data': 'do-not-log-image'}}, 'tool-id', None)
    for expected in ['sub_tools', 'paper.png', '100', '500', '1/2+1/4', '3/4']:
        assert expected in caplog.text
    assert 'do-not-log-image' not in caplog.text
