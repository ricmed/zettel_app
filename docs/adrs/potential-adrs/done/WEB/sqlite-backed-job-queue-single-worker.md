# Potential ADR: SQLite-Backed Persistent Job Queue with Single Worker Thread

**Module**: WEB
**Category**: Architecture / Concurrency / Job Processing
**Priority**: Consider (Score: 65)
**Date Identified**: 2026-08-30

---

## Existing ADR Context

No related ADRs identified. This is specific to the web layer's job orchestration model.

---

## What Was Identified

The Zettelkasten web interface uses a **durable, in-process job queue** backed by SQLite (with job tables `web_jobs` and `web_job_events`) and a single daemon worker thread. All long-running operations (harvest, extract, review, connect, garden, etc.) are submitted as jobs to this queue, serialized with their payload, and executed sequentially by the worker thread. The queue persists to SQLite, so jobs survive server restarts; the worker recovers interrupted jobs on startup.

This architectural decision trades off external queue complexity (Celery, RQ, Temporal) for in-process simplicity and SQLite durability, keeping the entire system as a single-process Python monolith.

**Key characteristics**:
- **Persistence**: Jobs stored in SQLite `web_jobs` table (job_id, operation, payload, state, phase, message, result, finished)
- **Events log**: `web_job_events` table tracks state transitions and progress events per job
- **Worker pattern**: `WebWorker` daemon thread polls for `queued` jobs, claims one, executes it, updates state
- **Concurrency control**: Single active job at a time; concurrent submit attempts receive HTTP 409 (Conflict) response
- **Recovery**: On startup, marked-running jobs → interrupted; queued jobs resume normally
- **Progress reporting**: Progress events emitted via `JobProgress` helper, saved to DB for SSE streaming

## Why This Might Deserve an ADR

- **Foundational concurrency model**: Defines how all web operations are sequenced and persisted. Breaking this pattern (e.g., adding async workers) would require significant refactoring.
- **Durability trade-off**: Deliberately avoids external queue dependencies (Celery, RQ, Temporal) to keep the system self-contained and deployable as a single OS process.
- **Single-job serialization**: The 409 on concurrent submit is a deliberate constraint — not a limitation to work around but a design choice to prevent simultaneous mutations.
- **Team understanding**: Developers maintaining the web UI need to understand job lifecycle (queued → running → succeeded/failed), progress event flow, and recovery behavior.
- **Cost to change**: Switching to Celery/RQ would require extracting job dispatch, managing separate worker processes/containers, coordinating with external queue broker.
- **Temporal stability**: Stable for ~1 day (2026-08-29 introduction); pattern unchanged despite bug fixes.

## Evidence Found in Codebase

### Key Files
- [`zettel/web_app.py`](../../../zettel/web_app.py) - Lines 123-180: `WebWorker` class, job queue orchestration
  - `submit()` method (lines 152-162): Job creation + worker wake-up
  - `_run()` method (lines 164-176): Worker loop, queued job polling
  - `_execute()` method (lines 178-212): Job execution, error handling, state persistence
- [`zettel/state.py`](../../../zettel/state.py) - Web job table schema and methods
  - `create_web_job()`, `claim_web_job()`, `update_web_job()`, `list_web_jobs()`, `get_web_job()`, `recover_web_jobs()`
- [`zettel/web.py`](../../../zettel/web.py) - Lines 200-350: Job submission endpoints (harvest, extract, review, connect, garden, sync)

### Code Evidence

**Job submission (web_app.py, lines 152-162)**:
```python
def submit(self, operation: str, payload: dict[str, Any]) -> str | None:
    job_id = uuid4().hex
    db = self._db()
    try:
        created = db.create_web_job(job_id, operation, payload)
    finally:
        db.close()
    if not created:
        return None  # 409 Conflict: job already running
    self._wake.set()
    return job_id
```

**Worker loop (web_app.py, lines 164-176)**:
```python
def _run(self) -> None:
    while not self._stop.is_set():
        db = self._db()
        try:
            queued = db.list_web_jobs(limit=1)
            job = queued[0] if queued and queued[0]["state"] == "queued" else None
        finally:
            db.close()
        if not job:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            continue
        self._execute(job["job_id"])
```

**Job execution with progress reporting (web_app.py, lines 178-212)**:
```python
def _execute(self, job_id: str) -> None:
    cfg = load_config(self.config_path)
    db = StateDB(cfg.state_db_path)
    if not db.claim_web_job(job_id):
        db.close()
        return
    progress = JobProgress(db, job_id)
    progress.emit(ProgressEvent("starting", f"Iniciando {operation}."))
    try:
        result = self._dispatch(cfg, db, progress, operation, payload)
        db.update_web_job(job_id, state="succeeded", ...)
    except Exception as exc:
        db.update_web_job(job_id, state="failed", message=safe_error(exc), ...)
    finally:
        db.close()
```

**409 Conflict response on concurrent submit (web.py, ~line 250)**:
```python
@app.post("/harvest/submit", response_class=RedirectResponse)
def submit_harvest(request: Request, ...):
    service = getattr(request.app.state, "service")
    job_id = service.worker.submit("harvest", {...})
    if not job_id:
        return JSONResponse({"error": "A job is already running"}, status_code=409)
    return RedirectResponse(url=f"/runs/{job_id}", status_code=303)
```

**SQLite job table schema (state.py)**:
```python
CREATE TABLE IF NOT EXISTS web_jobs (
    job_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    payload TEXT,
    state TEXT DEFAULT 'queued',  # queued | running | succeeded | failed | interrupted
    phase TEXT,
    message TEXT,
    current_item TEXT,
    current_index INTEGER,
    total_items INTEGER,
    error_message TEXT,
    result TEXT,
    run_id TEXT,
    finished INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS web_job_events (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    phase TEXT,
    message TEXT,
    current_item TEXT,
    current_index INTEGER,
    total_items INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES web_jobs(job_id)
);
```

### Impact Analysis

- Introduced: 2026-08-29 (commit 5d9b504)
- Modified: ~5 commits since (mostly progress tracking and recovery logic refinements)
- Scope: Job submission endpoints (7+ routes), worker orchestration, job status tracking, progress events
- Affects: All long-running operations, web dashboard (job history), job recovery on restart
- Cost to change: 3-4 weeks (extract job dispatch layer, add Celery/RQ, update endpoints, test recovery)
- Team knowledge requirement: Important for anyone debugging web jobs or implementing new operations

## Questions to Address in ADR (if created)

1. **Is this single-worker model sufficient long-term?** If the web interface becomes the primary entry point, will users need concurrent job execution?
2. **What are the implications of SQLite durability at scale?** If job volume grows, will SQLite WAL mode continue to be performant for high-frequency job submissions?
3. **Why not use Celery/RQ?** What is the cost/benefit trade-off of external queue complexity vs. in-process simplicity?
4. **How should distributed deployments (multi-instance) handle the job queue?** This design assumes single-process, single-machine; multi-instance setups would need rethinking.
5. **Should job execution be async (non-blocking) instead of blocking?** Currently, the worker thread blocks on pipeline operations; could this be problematic if operations take 30+ minutes?

## Related Potential ADRs

- **WEB: FastAPI + Server-Rendered Jinja2** — The presentation layer depends on job queue for progress streaming and job status display
- **INFRA: SQLite with WAL Mode and FTS5** — The job queue data lives in the same SQLite database as the pipeline state

## Additional Notes

- **Durability without external dependencies**: Unlike external queues (Celery requires Redis/RabbitMQ), this approach needs only SQLite (already a project dependency)
- **Simple recovery semantics**: Interrupted jobs are simply requeued; no complex distributed transaction semantics needed
- **Progress streaming**: Progress events enable SSE-based real-time feedback to the web UI without WebSocket complexity
- **Error isolation**: If a job fails, the worker catches the exception and marks the job failed; subsequent jobs in the queue continue normally
- **Single-threaded execution**: Serialized job execution prevents race conditions between pipeline operations (harvest + extract on the same source, etc.)
- **State collision risk**: Because job execution reads/writes to the same StateDB as CLI operations, concurrent CLI + web usage could cause conflicts (not explicitly handled)

---

## Temporal Context

**Git timeline**:
- 2026-08-29: Initial introduction (5d9b504, "Implement Python-first Zettelkasten web interface...")
- 2026-08-30: Bug fixes and recovery logic (9a16045, "Fix harvest completion feedback...")
- 2026-08-31: Progress tracking refinements (fe7bf5c, "Update dependencies and expand web test coverage")

**Stability**: Stable for ~4-5 days; core worker loop unchanged; only progress tracking and error messages refined.

---

## Scoring Justification

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| **Scope + Impact** | 20 | Affects all job submission routes and job status lifecycle; central to web job orchestration |
| **Cost to Change** | 20 | Refactoring to external queue (Celery/RQ) would require significant infrastructure changes |
| **Team Knowledge Requirement** | 15 | Important for developers maintaining web jobs, debugging job failures, implementing new operations |
| **3 E's Test** | ✓ | Structural (defines concurrency model), Evident (job queue is visible in UI), Stable (no regressions in 4+ days) |
| **Total (no base)** | 55 | Below must-document threshold (75) but above discard (25) |
| **Adjusted to 65** | — | Marginal: could justify 65-70 given durability importance, but conservative scoring keeps at 55-65 range |
