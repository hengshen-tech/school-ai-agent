"""Single-process background jobs with durable status and replayable progress.

The browser only observes jobs. No request or event-stream owns their lifetime.
Credentials live in the running task, never in the persisted job snapshot.
"""
import asyncio
import json
import time

from . import store
from .agents import AgentError
from .config import MODEL_EFFORT
from .agent_logging import log_event

ACTIVE = {"queued", "running"}


def valid_id(sid):
    return sid.startswith("sub_") and all(c.isalnum() or c == "_" for c in sid)


class JobManager:
    def __init__(self, pipeline, max_parallel=2):
        self.pipeline = pipeline
        self.max_parallel = max_parallel
        self.sem = asyncio.Semaphore(max_parallel)
        self.records = {}
        self.tasks = {}
        self.changed = {}

    def record(self, sid):
        if not valid_id(sid):
            return None
        if sid not in self.records:
            record = store._read(store.sub_dir(sid) / "job.json")
            if record:
                self.records[sid] = record
        return self.records.get(sid)

    def _save(self, record):
        store.save_artifact(record["id"], "job.json", record)

    def emit(self, sid, stage, status, message="", **extra):
        record = self.record(sid)
        now = time.time()
        event = {"id": record.get("last_event_id", 0) + 1, "stage": stage,
                 "status": status, "message": message, "t": now, "kind": "status", **extra}
        record["last_event_id"] = event["id"]
        if "page_count" in extra:
            record["page_count"] = extra["page_count"]
        for field in ("paper_id", "workflow_version", "mode"):
            if field in extra:
                record[field] = extra[field]
        if event["kind"] == "metric":
            record.setdefault("model_calls", []).append(extra["metric"])
        if event["kind"] == "status":
            record["message"] = message
            record["stage"] = stage
            steps = record.setdefault("stages", {})
            step = steps.setdefault(stage, {})
            running = step.get('started_at') is not None and step.get('status') in ('start', 'info')
            if status == "start" and not running:
                step["started_at"] = now
                step.pop('finished_at', None)
                step.setdefault('elapsed_s', 0)
            if status in {"done", "skip", "error"}:
                step['elapsed_s'] = round(step.get('elapsed_s', 0) +
                    (max(0, now - step['started_at']) if running else 0), 3)
                step['finished_at'] = now
                record.setdefault('timings', {})[stage] = step['elapsed_s']
            step.update(status=status, message=message, t=now)
            if "title" in extra:
                step["title"] = extra["title"]
            event.update({k: step[k] for k in ('started_at', 'finished_at', 'elapsed_s') if k in step})
        with (store.sub_dir(sid) / "events.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(event, ensure_ascii=False) + "\n")
        # Output is already durable in the append-only event log. Snapshot it at
        # stage boundaries instead of rewriting a growing JSON for every delta.
        if event["kind"] == "status":
            self._save(record)
        self.changed.setdefault(sid, asyncio.Event()).set()
        return event

    def start(self, sid, cfg, meta, teacher_key=""):
        now = time.time()
        meta = {**meta, "position_method": "ocr_blocks" if meta.get("ocr_mode") == "deepseek_ocr" else "visual_review"}
        if meta.get('workflow_version') == 2:
            meta.update(execution_mode='vision_agent', ocr_mode='vision', position_method='visual_agent')
        record = {"id": sid, "meta": meta, "teacher_key": teacher_key,
                  "settings": {k: cfg.get(k) for k in ("model", "base_url", "agentic_zoom", "use_ocr")},
                  "status": "queued", "created_at": now, "effort": MODEL_EFFORT, "stages": {}, "timings": {},
                  "last_event_id": 0, "page_count": 0, "model_calls": []}
        self.records[sid] = record
        self._save(record)
        self.emit(sid, "queue", "info", "已进入批改队列")
        task = asyncio.create_task(self._run(sid, cfg))
        self.tasks[sid] = task
        task.add_done_callback(lambda t: self.tasks.pop(sid, None))

    async def _run(self, sid, cfg):
        record = self.record(sid)
        emit = lambda stage, status, message="", **extra: self.emit(sid, stage, status, message, **extra)
        try:
            async with self.sem:
                record.update(status="running", started_at=time.time())
                record["queue_s"] = round(record["started_at"] - record["created_at"], 3)
                self._save(record)
                await self.pipeline(sid, cfg, record["meta"], emit, record["teacher_key"])
                record.update(status="done", finished_at=time.time())
                result = store.get_result(sid)
                if result:
                    record["page_count"] = result.get("page_count", record["page_count"])
                    result.update(timings=record["timings"], queue_s=record["queue_s"],
                                  model_calls=record["model_calls"], effort=record.get('effort', MODEL_EFFORT),
                                  elapsed_s=round(record["finished_at"] - record["started_at"], 1))
                    store.save_result(sid, result)
                if result:
                    record["paper_id"] = result.get("paper_id")
                emit("done", "done", "建库完成" if record["meta"].get("mode") == "prepare" else "批改完成")
        except asyncio.CancelledError:
            self._interrupt(record)
            raise
        except Exception as e:
            message = str(e) if isinstance(e, AgentError) else f"内部错误：{e}"
            record.update(status="failed", finished_at=time.time(), error=message)
            store.save_artifact(sid, "error.json", {"error": message})
            emit("error", "error", message)
        finally:
            self._save(record)
            self.changed.setdefault(sid, asyncio.Event()).set()

    def _interrupt(self, record):
        message = '任务已手动停止，可重试' if record.get('stop_requested') else '服务停止，批改已中断，可重试'
        record.update(status='interrupted', finished_at=time.time(), error=message)
        log_event('interrupted', submission_id=record['id'], message=message)
        for stage, step in list(record.get('stages', {}).items()):
            if step.get('status') in ('start', 'info') and step.get('started_at') is not None:
                self.emit(record['id'], stage, 'error', message)
        self.emit(record['id'], 'interrupted', 'error', message)

    async def stop(self, sid):
        record = self.record(sid)
        if not record or record['status'] not in ACTIVE:
            return self.status(sid)
        record['stop_requested'] = True
        log_event('stop_requested', submission_id=sid)
        self._save(record)
        task = self.tasks.get(sid)
        if task and not task.done():
            # Repeated stop requests must not interrupt SDK cleanup a second time.
            if not task.cancelling():
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        if record['status'] in ACTIVE:
            # Cancelling a task before its coroutine starts skips _run's finally.
            self._interrupt(record)
        return self.status(sid)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def recover(self):
        # Recreate loop-bound primitives when an application lifespan restarts.
        self.sem = asyncio.Semaphore(self.max_parallel)
        self.records.clear()
        self.changed.clear()
        for path in store.SUBS_DIR.glob("*/job.json"):
            record = self.record(path.parent.name)
            if not record:
                continue
            log = path.parent / "events.jsonl"
            if log.exists():
                data = log.read_bytes()
                if data and not data.endswith(b"\n"):
                    log.write_bytes(data[:data.rfind(b"\n") + 1])
                history = self.events_after(record["id"], 0)
                record["last_event_id"] = max([record.get("last_event_id", 0)] + [e["id"] for e in history])
                record["model_calls"] = [e["metric"] for e in history if e.get("kind") == "metric"]
            if record and record["status"] in ACTIVE:
                result = store.get_result(record["id"])
                record.update(status="done" if result else "interrupted", finished_at=time.time())
                if not result:
                    record["error"] = "服务重启，批改已中断，可重试"
                self.emit(record["id"], "done" if result else "interrupted", "done" if result else "error",
                          "批改完成" if result else record["error"])

    def status(self, sid):
        record = self.record(sid)
        if not record:
            result = store.get_result(sid) if valid_id(sid) else None
            if not result:
                return None
            # Results predating durable jobs remain accessible from the queue.
            return {"id": sid, "filename": result["filename"], "status": "done", "done": True,
                    "page_count": result.get("page_count", 0), "created_at": 0, "message": "批改完成",
                    "model": result.get("model", ""), "effort": result.get('effort', 'high'), "stages": {},
                    "timings": result.get("timings", {}), "elapsed_s": result.get("elapsed_s", 0),
                    "ocr_mode": result.get("ocr_mode", "deepseek_ocr" if result.get("ocr_used") else "vision"),
                    "position_method": result.get("position_method"),
                    "mode": result.get("mode", "grade"), "workflow_version": result.get("workflow_version", 1),
                    "execution_mode": result.get('execution_mode'),
                    "paper_id": result.get("paper_id"),
                    "last_event_id": 0, "task_id": result.get("task_id", "")}
        now = record.get("finished_at") or time.time()
        meta = record["meta"]
        started = record.get("started_at")
        return {"id": sid, "filename": meta["filename"], "status": record["status"],
                "done": record["status"] not in ACTIVE, "error": record.get("error"),
                "message": record.get("message", "等待中"), "stage": record.get("stage", "queue"),
                "created_at": record.get("created_at", 0), "started_at": started,
                "finished_at": record.get("finished_at"), "elapsed_s": round(now - started, 1) if started else 0,
                "queue_s": record.get("queue_s", round(now - record.get("created_at", now), 1)),
                "page_count": record.get("page_count", 0), "ocr_mode": meta.get("ocr_mode", "vision"),
                "position_method": meta.get("position_method"),
                "model": record.get("settings", {}).get("model", ""), "effort": record.get('effort', 'high'),
                "mode": meta.get("mode", "grade"), "workflow_version": meta.get("workflow_version", 1),
                "execution_mode": meta.get('execution_mode'),
                "paper_id": record.get("paper_id") or meta.get("paper_id"),
                "timings": record.get("timings", {}), "stages": record.get("stages", {}),
                "last_event_id": record.get("last_event_id", 0),
                **{k: meta.get(k, "") for k in ("class_id", "task_id", "student_id")}}

    def list(self, task_id="", status=""):
        ids = set(self.records) | {p.parent.name for p in store.SUBS_DIR.glob("*/job.json")}
        if status == "all" or status == "done":
            ids |= {p.parent.name for p in store.SUBS_DIR.glob("*/result.json")}
        results = []
        for sid in ids:
            job = self.status(sid)
            if not job or (task_id and job.get("task_id") != task_id):
                continue
            if status and status != "all" and job["status"] != status:
                continue
            if not status and job["done"] and not (job.get("error") and (job.get("finished_at") or 0) > time.time() - 3600):
                continue
            results.append(job)
        return sorted(results, key=lambda j: j["created_at"], reverse=True)

    def events_after(self, sid, cursor):
        path = store.sub_dir(sid) / "events.jsonl"
        events = []
        if path.exists():
            for line in path.read_text("utf-8").splitlines():
                try:
                    event = json.loads(line)
                    if event["id"] > cursor:
                        events.append(event)
                except (ValueError, KeyError):
                    continue  # A crash may leave one partial final line.
        return events

    async def stream(self, sid, cursor=0):
        signal = self.changed.setdefault(sid, asyncio.Event())
        path = store.sub_dir(sid) / "events.jsonl"
        if not path.exists():
            return
        # The file offset advances after every yield, including frames appended
        # while the consumer was reading. This also avoids quadratic log reads.
        with path.open(encoding="utf-8") as log:
            while True:
                signal.clear()
                while line := log.readline():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event["id"] <= cursor:
                        continue
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if self.status(sid)["done"]:
                    return
                try:
                    await asyncio.wait_for(signal.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
