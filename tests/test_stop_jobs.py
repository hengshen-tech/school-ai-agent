"""Stopping queued/running work cancels execution, not merely SSE observation."""
import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from test_jobs import isolated_store, image_bytes  # noqa: F401
from app import agents, config, main, store
from app.jobs import JobManager


@pytest.mark.asyncio
async def test_stop_queued_task_before_first_execution_is_durable():
    calls = []
    async def pipeline(*args): calls.append(args[0])
    manager = JobManager(pipeline)
    manager.start('sub_stop_queued', config.load_settings(), {'filename': 'queued.png'})
    result = await manager.stop('sub_stop_queued')
    assert calls == []
    assert result['status'] == 'interrupted' and result['done']
    assert result['effort'] == 'low'
    assert '手动停止' in result['error']
    assert store._read(store.sub_dir('sub_stop_queued') / 'job.json')['status'] == 'interrupted'
    assert (await manager.stop('sub_stop_queued'))['last_event_id'] == result['last_event_id']


@pytest.mark.asyncio
async def test_stop_running_task_closes_sdk_and_releases_queue_slot(monkeypatch):
    entered, closed, next_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Client:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args):
            await asyncio.sleep(0)
            closed.set()
        async def query(self, prompt):
            async for _ in prompt: pass
        async def receive_response(self):
            entered.set()
            await asyncio.Event().wait()
            if False: yield
    monkeypatch.setattr(agents, 'ClaudeSDKClient', Client)
    async def pipeline(sid, cfg, meta, emit, key):
        if sid == 'sub_stop_next': next_entered.set(); return
        emit('process', 'start', '批改中')
        await agents.run_agent(cfg, system='test', blocks=[], emit=emit)
    manager = JobManager(pipeline, max_parallel=1)
    manager.start('sub_stop_running', config.load_settings(), {'filename': 'running.png'})
    await entered.wait()
    manager.start('sub_stop_next', config.load_settings(), {'filename': 'next.png'})
    result, duplicate = await asyncio.gather(manager.stop('sub_stop_running'), manager.stop('sub_stop_running'))
    await asyncio.wait_for(next_entered.wait(), 1)
    assert closed.is_set() and result['status'] == 'interrupted'
    assert duplicate['status'] == result['status']
    assert result['stages']['process']['status'] == 'error'
    assert result['stages']['process']['finished_at'] is not None
    assert store.get_result('sub_stop_running') is None
    await manager.close()


def test_stop_endpoint_and_retry_stopped_upload(monkeypatch):
    async def idle(*args): await asyncio.Event().wait()
    monkeypatch.setattr(main, 'job_manager', JobManager(idle))
    with TestClient(main.app) as client:
        sid = client.post('/api/submissions', files={'file': ('work.png', image_bytes(), 'image/png')}).json()['id']
        response = client.post(f'/api/submissions/{sid}/stop')
        assert response.status_code == 200 and response.json()['status'] == 'interrupted'
        assert client.get(f'/api/submissions/{sid}/status').json()['done']
        assert 'interrupted' in client.get(f'/api/submissions/{sid}/events').text
        retry = client.post(f'/api/submissions/{sid}/retry')
        assert retry.status_code == 200 and retry.json()['id'] != sid
        assert client.post('/api/submissions/sub_missing/stop').status_code == 404


@pytest.mark.asyncio
async def test_stop_completed_task_does_not_relabel_completion():
    async def finished(*args): pass
    manager = JobManager(finished)
    manager.start('sub_stop_finished', config.load_settings(), {'filename': 'done.png'})
    await asyncio.gather(*list(manager.tasks.values()))
    assert (await manager.stop('sub_stop_finished'))['status'] == 'done'


@pytest.mark.asyncio
async def test_stopped_background_renderer_cannot_publish_a_result(tmp_path, monkeypatch):
    from test_agent_workflow import state, reviewed
    from app import pipeline
    s = state(tmp_path, monkeypatch); reviewed(s)
    s.grades, s.positions_submitted = [], True
    entered, release = threading.Event(), threading.Event()
    def rendering(*args):
        entered.set()
        release.wait(2)
    monkeypatch.setattr(pipeline, 'render_annotations', rendering)
    worker = asyncio.create_task(asyncio.to_thread(s.finalize))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert s.stop_finalization() is None
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError): await worker
    assert store.get_result(s.sid) is None
