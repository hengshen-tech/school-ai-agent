"""Agent diagnostics on Uvicorn's console handler, visible in docker logs."""
import json
import logging

logger = logging.getLogger('uvicorn.error')


def log_event(event, *, submission_id=None, agent='main', **fields):
    record = {'event': event, 'submission_id': submission_id, 'agent': agent, **fields}
    logger.info('[grading-agent] %s', json.dumps(record, ensure_ascii=False, default=str))


def log_text(event, text, **fields):
    """Keep full textual content in bounded lines; callers never pass image data."""
    if not text:
        return
    size = 3000
    chunks = (len(text) + size - 1) // size
    for index in range(chunks):
        log_event(event, part=index + 1, parts=chunks, text=text[index * size:(index + 1) * size], **fields)
