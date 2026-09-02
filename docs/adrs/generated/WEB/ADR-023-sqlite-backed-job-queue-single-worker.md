# ADR-XXX: SQLite-Backed Persistent Job Queue with Single Worker Thread
**Status:** Accepted
**Date:** 2026-08-29
**Depends on:** [ADR-XXX: SQLite with WAL Mode and FTS5 as Primary Persistence Layer](../INFRA/ADR-001-sqlite-wal-fts5-primary-persistence.md)
**Related to:** [ADR-XXX: FastAPI Server-Rendered Web Interface (No SPA)](./ADR-022-fastapi-server-rendered-jinja2.md)

## Context and Problem Statement

The web interface needs to run long operations — harvest, extract, review, connect, garden, and sync — without blocking HTTP request handling, and without losing job state if the server process restarts mid-run. These operations already read and write a shared SQLite `StateDB` and the Obsidian vault, so any concurrency model also has to prevent two operations from mutating that shared state at the same time.

The system persists job records to two SQLite tables, `web_jobs` and `web_job_events`, and processes them with a single in-process daemon worker thread that polls for queued jobs, executes them one at a time, and writes progress checkpoints back to the database. On startup, jobs left in a `running` state from an unclean shutdown are marked `interrupted`, while `queued` jobs resume normally. This keeps the entire system deployable as a single OS process, with no broker or worker-process infrastructure beyond the SQLite file the pipeline already depends on.

The alternative most directly considered was an external task queue (Celery, RQ, or similar), which would allow concurrent job execution across separate worker processes but would require a message broker (typically Redis or RabbitMQ) and a different deployment topology.

## Decision Drivers

- Long-running pipeline operations must survive a server restart rather than silently disappearing mid-execution.
- Job execution must not race with other pipeline operations against the same StateDB and vault files.
- The deployment target is a single OS process with no managed broker service, so any new dependency has real infrastructure cost.
- Job progress must be observable in the web UI in near real time without adding a separate transport (e.g., WebSockets).
- SQLite is already the project's persistence layer, so reusing it for job state avoids introducing a second storage system.

## Considered Options

1. SQLite-backed job queue with a single in-process worker thread (chosen)
2. External task queue (Celery/RQ) with a message broker and dedicated worker process(es)

## Decision Outcome

Chosen option: SQLite-backed job queue with a single worker thread, because it requires no new infrastructure beyond the SQLite database the pipeline already uses, persists job state durably enough to recover from a restart, and its single-worker serialization directly prevents concurrent pipeline operations from mutating the shared StateDB and vault at the same time — the 409 response on a second concurrent submission is a deliberate consequence of that serialization, not an incidental limitation.

[NEEDS INPUT: Was an external queue (Celery/RQ) formally evaluated and rejected at the time, or was the single-worker model adopted as the simplest option to reach for given the single-VM deployment, with a switch to be revisited later?]

## Pros and Cons of the Options

### SQLite-Backed Job Queue with Single Worker Thread (chosen)

- Good, because it introduces no new infrastructure — only the SQLite database already used by the rest of the pipeline.
- Good, because job state persists across restarts, and interrupted jobs are recovered automatically on startup.
- Good, because single-worker serialization prevents concurrent jobs from racing on the shared StateDB and vault.
- Bad, because only one job can run at a time system-wide; a concurrent submission is rejected with HTTP 409 rather than queued for later execution.
- Bad, because the worker thread executes each job synchronously with no built-in timeout or cancellation path for a long-running operation.

### External Task Queue (Celery/RQ)

- Good, because it allows multiple jobs to execute concurrently across separate worker processes.
- Good, because it brings a mature ecosystem for retries, scheduling, and monitoring.
- Bad, because it requires a message broker (Redis/RabbitMQ) and separate worker process(es), breaking the single-process deployment model.
- Bad, because migrating to it was estimated at 3-4 weeks of work: extracting the job-dispatch layer, wiring the broker, updating every submission endpoint, and re-validating recovery behavior.

## Consequences

The web UI can run only one long-running operation at a time by design; any other submission is turned away with a 409 rather than silently queued behind it. This is a deliberate constraint that keeps job execution predictable, but it also means the web interface cannot presently support concurrent users triggering independent long-running operations at the same time.

Because job execution reads and writes the same StateDB used by CLI commands, concurrent CLI and web usage is not arbitrated by this design — a CLI run and a web job could still collide on the same source or chunk state. The design also implicitly assumes a single process on a single machine: SQLite's single-writer model does not extend across processes or hosts, so any future multi-instance or horizontally scaled deployment of the web UI would require replacing this queue with a broker-backed one.

[NEEDS INPUT: Is a multi-instance or horizontally scaled deployment of the web UI planned, given the current design assumes a single process on a single machine?]

[NEEDS INPUT: What is the expected growth in job volume or concurrent-user demand if the web interface becomes the primary entry point, and at what point would single-job serialization become a practical bottleneck?]

## References

- `zettel/web_app.py:123-212` — `WebWorker` class: `submit()`, the polling loop in `_run()`, and job execution/state persistence in `_execute()`
- `zettel/state.py` — `web_jobs` / `web_job_events` schema and job lifecycle methods (`create_web_job`, `claim_web_job`, `update_web_job`, `recover_web_jobs`)
- `zettel/web.py:200-350` — job submission endpoints that return HTTP 409 on a concurrent submit attempt
