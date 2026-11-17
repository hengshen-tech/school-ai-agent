"""Library keys are usable by default and remain editable, without model calls."""
from fastapi.testclient import TestClient

from test_jobs import isolated_store  # noqa: F401
from app import main, store


def test_legacy_ai_key_is_usable_without_confirmation(tmp_path, monkeypatch):
    from test_agent_workflow import source, state
    from app.workflow import WorkflowState

    first = state(tmp_path, monkeypatch)
    original = [{'id': '1', 'answer': '2', 'confident': False, 'review_status': 'review'}]
    pid = store.save_paper('Quiz', source()['questions'], original, 'ai')
    # Simulate a library entry written before the default-use policy.
    store._write(store.PAPERS_DIR / pid / 'answers.json', original)
    with TestClient(main.app) as client:
        paper = client.get(f'/api/papers/{pid}').json()
    assert paper['answers'][0]['review_status'] == 'ai_solved'
    assert paper['answers'][0]['confident'] is False  # Keep model evidence, not fake teacher approval.
    assert paper['rev'] == 0
    incoming = WorkflowState('next', {}, {'mode': 'grade', 'paper_id': pid},
                             first.pages, first.manifest, first.emit)
    incoming.submit_source(source())
    assert incoming.paper_cache_hit and incoming.answers[0]['answer'] == '2'


def test_library_edit_changes_the_key_and_preserves_unchanged_answers():
    pid = store.save_reviewed_paper('Quiz', [{'id': '1', 'question': '1+1='}, {'id': '2', 'question': '2+2='}],
        [{'id': '1', 'answer': '3', 'accepted': ['3.0'], 'review_status': 'review'},
         {'id': '2', 'answer': '4', 'review_status': 'review'}], 'ai')
    assert store.get_paper(pid)['review_summary']['review'] == 0
    with TestClient(main.app) as client:
        response = client.put(f'/api/papers/{pid}/answers', json={'answers': [
            {'id': '1', 'answer': '2'}, {'id': '2', 'answer': '4'}]})
    assert response.status_code == 200
    paper = response.json()
    assert paper['rev'] == 1
    assert paper['answers'][0]['answer'] == '2'
    assert paper['answers'][0]['accepted'] == []
    assert paper['answers'][0]['review_status'] == 'manual_confirmed'
    assert paper['answers'][1]['review_status'] == 'ai_solved'


def test_empty_library_answer_is_still_missing():
    pid = store.save_paper('Quiz', [{'id': '1', 'question': '1+1='}],
                           [{'id': '1', 'answer': '  ', 'review_status': 'review'}], 'ai')
    assert store.get_paper(pid)['answers'][0]['review_status'] == 'review'
