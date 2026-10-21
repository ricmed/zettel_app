# ADR-023: SQLite-Backed Persistent Job Queue with Single Worker Thread
**Status:** Accepted
**Date:** 2026-08-29
**Depends on:** [ADR-001: SQLite with WAL Mode and FTS5 as Primary Persistence Layer](../INFRA/ADR-001-sqlite-wal-fts5-primary-persistence.md)
**Related to:** [ADR-022: FastAPI Server-Rendered Web Interface (No SPA)](./ADR-022-fastapi-server-rendered-jinja2.md)

## Context and Problem Statement

The web interface needs to run long operations — harvest, extract, review, connect, garden, and sync — without blocking HTTP request handling, and without losing job state if the server process restarts mid-run. These operations already read and write a shared SQLite `StateDB` and the Obsidian vault, so any concurrency model also has to prevent two operations from mutating that shared state at the same time.

The system persists job records to two SQLite tables, `web_jobs` and `web_job_events`, and processes them with a single in-process daemon worker thread that waits on an event set by every submit (with a 30 s fallback, #307), executes them one at a time, and writes progress checkpoints back to the database. On startup, jobs left in a `running` state from an unclean shutdown are marked `interrupted`, while `queued` jobs resume normally. This keeps the entire system deployable as a single OS process, with no broker or worker-process infrastructure beyond the SQLite file the pipeline already depends on.

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

**Recorded answer (2026-10-20).** No formal evaluation of Celery or RQ is recorded: the single worker was the simplest option for a single-machine deployment. The 3-4 week migration estimate below comes from the analysis that generated this ADR, not from a measurement.

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

Because job execution reads and writes the same StateDB used by CLI commands, concurrent CLI and web usage was a risk, not just a fact: a CLI run and a web job could collide on the same source, chunk or Chroma collection. Since #308 an exclusive OS lock arbitrates the two (addendum below). The design also implicitly assumes a single process on a single machine: SQLite's single-writer model does not extend across processes or hosts, so any future multi-instance or horizontally scaled deployment of the web UI would require replacing this queue with a broker-backed one.

**Recorded answer (2026-10-20).** No multi-instance deployment is planned, and more of the code now assumes one process: the OS lock (#308), the in-memory login throttle (ADR-063) and the parked article graph (ADR-056). `docs/interface-web.md` tells the operator not to run several Uvicorn workers.

**Recorded answer (2026-10-20).** No growth target is recorded. Serialization costs only when two long jobs are wanted at once, so the practical limit is a second person working at the same time, not job volume: one person runs the phases in order anyway, and a busy submit now names and links the job holding the queue (#316).

## References

- `zettel/web_app.py` — `WebWorker.submit`, the polling loop in `_run`, and job execution/state persistence in `_execute`
- `zettel/state/web.py` — `web_jobs` / `web_job_events` job lifecycle methods (`create_web_job`, `claim_web_job`, `update_web_job`, `recover_web_jobs`)
- `zettel/web/enqueue.py` — `post_job` (HTTP 409 on a concurrent submit attempt)

## Addendum (2026-10-07): a parked article does not hold the slot

An article review sets the job to `awaiting_input`. That state is neither `queued` nor `running`, so another job may be submitted. The compiled graph stays in the worker process. `POST /jobs/{id}/resume` moves the same row back to `queued`.

The worker claims `WHERE state='queued' ORDER BY created_at ASC, rowid ASC`. Taking the newest row overall would hide a resumed article behind a job that had already finished. On restart, `awaiting_input` becomes `interrupted` together with `running`. See [ADR-056](./ADR-056-web-acervo-consult-and-produce.md).

## Addendum (2026-10-20): the CLI is arbitrated too (#308)

**Status:** Accepted. The consequence above ("concurrent CLI and web usage is not arbitrated") no longer holds. `zettel/oplock.py` holds an exclusive OS lock on `data/zettel.lock` (`msvcrt.locking` on Windows, `flock` elsewhere); the OS drops it when its holder dies, so a crash never leaves it behind. Who holds it is written to `zettel.lock.info`.

* Every CLI command in `cli.deps.MUTATING_COMMANDS` takes it in `load_deps` and exits with code 1, naming the holder, when it is taken. Read-only commands (status, doctor, ask, catalog, article, skill, dumps) do not.
* The worker takes it around each job. When a CLI command holds it, the job stays `queued` with "Aguardando: ..." as its message and retries every 2 s.
* `post_job` answers 409 with the operation and the surface that hold the lock.
* The writes the web does in the request take it as well: a single edge decision on `/review?queue=links`, the scaffolds of `/notes/new`, and the catalog's read of Chroma. An upload only adds a new file to the inbox and does not.

Two handles exclude each other inside one process too, which is what keeps a request thread out while the worker runs a job. This is not a broker: it serialises one machine's writers, the deployment ADR-023 assumes.

## Addendum (2026-10-20): retention of finished jobs (#313)

`web_jobs` and `web_job_events` grew forever. On start, after `recover_web_jobs`, the worker calls `StateDB.purge_web_jobs(older_than_days=web.job_retention_days)` (90 by default, `0` keeps everything): finished jobs (`succeeded`, `failed`, `interrupted`) older than that go, with their events and their `web_job_runs` links by cascade. The `runs` rows stay, since they are pipeline history that `zettel status` reads. A preparation's `web_harvest_reviews` row goes with its job, and its snapshot is then removed by `discard_orphan_preparations`. Queued, running and parked jobs are never purged.
