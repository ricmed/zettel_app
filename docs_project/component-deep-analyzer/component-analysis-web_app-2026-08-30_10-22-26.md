# Component Deep Analysis Report — `web_app`

## 1. Executive Summary

`zettel/web_app.py` (405 lines) is the **application/service layer** that lets the server-rendered FastAPI UI (`zettel/web.py`) operate the entire Zettelkasten pipeline (`harvest → extract → review → connect → garden`) without a terminal, while never letting HTTP concerns leak into the pipeline core. Its own docstring states the intent directly (web_app.py:1-6): "This module deliberately contains no HTTP concerns. It owns the durable, single-worker queue and calls the existing pipeline entry points with their normal StateDB/VectorIndex dependencies."

The module exposes four collaborating pieces:

- **`WebApplication`** (web_app.py:356-405) — a thin facade instantiated once in `web.py`'s FastAPI `lifespan` (web.py:98-104) and stored on `app.state.service`; every route reads it via `_service(request)`. It owns a `WebWorker` and re-opens a fresh `StateDB` connection per call (`db()`, `dashboard()`, `jobs()`, `job()`, `events()`, `submit()`), always inside `try/finally: db.close()`.
- **`WebWorker`** (web_app.py:123-353) — a durable, SQLite-backed job queue with exactly one dedicated daemon polling thread (`threading.Thread(name="zettel-web-worker")`, web_app.py:143). It enforces **at most one mutating job (`queued`/`running`) at a time** at the database layer (`StateDB.create_web_job`, state.py:1529-1550, using `BEGIN IMMEDIATE`), recovers from an unclean shutdown at `start()` (`recover_web_jobs`, web_app.py:136-142), and dispatches eight named operations (`_dispatch`, web_app.py:214-353) to the same pipeline modules the CLI uses (`harvester`, `extractor`, `review`, `connector`, `gardener`, `gardener_hub`, `sync`).
- **`JobProgress`** (web_app.py:52-92) — the concrete, structurally-typed implementation of `zettel.progress.ProgressObserver` for the web path; every checkpoint it receives is persisted as **two** separate SQLite writes (a mutable "current state" row and an immutable append-only event) so the browser can both poll "where is this job now" and stream "everything that has happened."
- **`safe_error()` / `UserFacingError`** (web_app.py:25-40) — the single sanitization boundary between an internal Python exception and whatever text reaches the browser, guarding against leaking API keys, secrets, tokens, or host filesystem paths.

Key findings:

- `_idx_kwargs()` (web_app.py:95-104) is explicitly required by `CLAUDE.md` to mirror `cli._idx_kwargs()` (cli.py:83-93) "including `embedding.dimensions`" — confirmed field-for-field identical **except** it omits `reset_mismatched`. Because `VectorIndex.__init__` defaults `reset_mismatched=False` (index.py:206-215) and raises `EmbeddingSpaceMismatch` synchronously from inside `__init__` when the configured embedding differs from what is stored in Chroma (index.py:246-253), any web job that reaches `VectorIndex(**_idx_kwargs(cfg))` (web_app.py:229) after an embedding-config change fails outright with a raw exception — there is no web equivalent of the CLI's interactive "reprocess?" recovery flow (`_confirm_embedding_reprocess`, cli.py:137-146). This is a genuine, confirmed architectural gap (see §10).
- `CLAUDE.md`'s own architecture notes ("Not exposed in web: … `run-all`") is **stale relative to the current code**: `WebWorker._dispatch` has a full `operation == "run_all"` branch (web_app.py:230-269) that runs all five phases, and `zettel/web.py` exposes it at `POST /documents/run-all` (web.py:361-380), exercised end-to-end by `tests/test_web.py::test_documents_can_queue_full_pipeline`. This is a documentation/code drift worth flagging, not a defect in `web_app.py` itself.
- The `"review"` operation (web_app.py:304-335) does **not** call `review.run_review()` — it calls `approve_chunk`/`reject_chunk` directly per `chunk_id`, then `finalize_approved_concepts` once. This means the web UI's manual review action **bypasses** the `literature_review.auto_approve_min_confidence` gate entirely (any chunk id submitted is approved/rejected regardless of confidence), whereas `run_all`'s Phase 3 (web_app.py:249-252) calls `run_review(auto_approve=True, interactive=False)`, which **does** enforce that same threshold. Two different confidence-gating behaviors coexist under one component, driven by which code path is taken (see §3).
- `zettel/web.py` imports `safe_error` (web.py:24) but never calls it — the only real call site is inside `web_app.py` itself (web_app.py:205); this is a dead import in the consumer module (§10).
- `retry_chunks` and `retry_assets` (web_app.py:220-226) are the only two operations that never instantiate a `VectorIndex` — they are pure SQLite status-flip operations and therefore immune to the embedding-mismatch failure mode above.
- `_execute()`'s correlation of a job to the `runs` table row it produced (web_app.py:188-197) is a **heuristic**: it snapshots `db.get_last_run()` before dispatch and compares `run_id` after, assigning the job's `run_id` only if it changed. `sync`, `retry_chunks`, and `retry_assets` never call `StateDB.start_run`, so their jobs always persist `run_id=None`.
- No dedicated `tests/test_web_app.py` exists. Coverage of this component's logic lives in `tests/test_web_state.py` (queue/state-machine + `_idx_kwargs` + `safe_error` unit tests, plus one full dispatch-ordering test for `run_all` with every pipeline function monkeypatched) and transitively in `tests/test_web.py` (FastAPI `TestClient` end-to-end tests that mostly monkeypatch `service.submit` itself, sidestepping `WebWorker` entirely, except for one real `retry_assets` run).

## 2. Data Flow Analysis

Three distinct flows pass through this component: job submission, background execution, and status/progress read-back.

**Flow A — Submitting a mutating operation (HTTP → durable queue):**
```
1. Browser POSTs to a mutating route in web.py (e.g. /pipeline/extract, /review/action,
   /documents/harvest, /documents/run-all) — after auth + CSRF checks in web.py itself
2. web.py calls WebApplication.submit(operation, payload)                (web_app.py:376-377)
3. WebApplication.submit() delegates to WebWorker.submit()                (web_app.py:152-162)
4. WebWorker.submit() generates job_id = uuid4().hex, opens a fresh StateDB,
   calls db.create_web_job(job_id, operation, payload)                    (web_app.py:154-158)
5. StateDB.create_web_job() runs "BEGIN IMMEDIATE"; if any row already has
   state IN ('queued','running') it rolls back and returns False           (state.py:1529-1550)
6. IF False: WebWorker.submit() returns None → web.py renders the jobs
   page with a 409 "Outra operação mutante já está em andamento." error    (web.py:310-319)
   IF True: the new row is inserted with state='queued', WebWorker.submit()
   sets self._wake (a threading.Event) to nudge the polling loop, and
   returns job_id                                                         (web_app.py:159-162)
7. web.py redirects the browser to GET /jobs/{job_id}                     (web.py:319)
```

**Flow B — Background execution (the durable worker loop):**
```
1. WebWorker._run() (a daemon thread started once in WebApplication.start(),
   itself called from web.py's FastAPI lifespan at process startup)        (web_app.py:164-176, web.py:98-104)
2. Loop: open a fresh StateDB, list_web_jobs(limit=1) ordered by created_at
   DESC; if the newest row's state == 'queued', treat it as the job to run  (web_app.py:166-171)
3. IF no queued job: wait up to 0.5s on self._wake (Event), clear it, loop  (web_app.py:172-175)
4. IF a queued job exists: call self._execute(job_id)                      (web_app.py:176-178)
5. _execute(): load a fresh AppConfig + StateDB; claim_web_job() does an
   atomic UPDATE ... WHERE state='queued' (guards against a second worker
   claiming the same row); if the claim fails (rowcount != 1), return       (web_app.py:179-183, state.py:1552-1559)
6. Build JobProgress(db, job_id); read the job row + payload + operation;
   snapshot db.get_last_run() as previous_run (for later run_id correlation) (web_app.py:184-190)
7. Emit a "starting" ProgressEvent                                          (web_app.py:190)
8. result = self._dispatch(cfg, db, progress, operation, payload)           (web_app.py:192)
     → emits "Carregando dependências…" for every operation                 (web_app.py:219)
     → branches on `operation`; retry_chunks/retry_assets skip VectorIndex
       entirely; every other operation opens VectorIndex(**_idx_kwargs(cfg))
       (lazily imported) before doing anything else                        (web_app.py:220-229)
     → lazily imports and calls the matching pipeline entry point
       (run_harvest / run_extract / approve_chunk+reject_chunk+
       finalize_approved_concepts / run_connect / run_garden(_hubs) /
       run_sync_manual), threading `progress` through as `observer=`
       wherever the pipeline function accepts one                          (web_app.py:230-353)
9. ON SUCCESS: compare db.get_last_run() to previous_run to infer a run_id;
   update_web_job(state='succeeded', phase='completed', result=result,
   run_id=run_id, finished=True); add a terminal 'completed' event          (web_app.py:193-202)
10. ON EXCEPTION: log full traceback server-side; message = safe_error(exc);
    update_web_job(state='failed', phase='failed', message=message,
    error_message=message, finished=True); add a terminal 'failed' event    (web_app.py:203-210)
11. finally: db.close() — the StateDB connection opened for this job's
    execution is always released, success or failure                       (web_app.py:211-212)
```

**Flow C — Status / progress read-back (polling and SSE, consumed by web.py):**
```
1. Browser polls GET /api/jobs/{job_id}?after=N, or opens the SSE stream
   GET /api/jobs/{job_id}/events                                           (web.py:560-586)
2. web.py calls WebApplication.job(job_id) / .events(job_id, after)        (web_app.py:393-404)
3. Each call opens a fresh StateDB, queries web_jobs / web_job_events by
   primary key / event_id cursor, and closes the connection in `finally`   (web_app.py:393-404)
4. web.py assembles {"job": ..., "events": [...]}. The SSE generator loops
   up to 20 times at 0.5s intervals, stopping early once job.state is
   terminal (succeeded/failed/interrupted)                                 (web.py:574-586)
```

**Startup recovery flow (process boundary):**
```
FastAPI lifespan starts (web.py:98-104)
  → WebApplication(config_path).start()                                   (web_app.py:367-368)
    → WebWorker.start():
        1. Open a StateDB, call db.recover_web_jobs()                     (web_app.py:135-142)
             → UPDATE web_jobs SET state='interrupted', phase='interrupted',
               message='Interrompido pela reinicializacao da aplicacao'
               WHERE state='running'                                      (state.py:1515-1527)
             → rows that were merely 'queued' are left untouched, so a
               job submitted just before a restart resumes automatically
               once the new worker thread starts polling
        2. Start the daemon thread running WebWorker._run() (Flow B)      (web_app.py:143-144)
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Concurrency | At most one mutating job may be `queued` or `running` at any time, enforced atomically in SQLite (`BEGIN IMMEDIATE`) | state.py:1529-1550 |
| Concurrency | Claiming a job for execution is itself atomic (`UPDATE ... WHERE state='queued'`); a second claim attempt on the same job silently no-ops | web_app.py:181-183, state.py:1552-1559 |
| Recovery | On process start, any job left `running` by an unclean shutdown is marked `interrupted`; jobs left `queued` are resumed automatically by the new worker | web_app.py:136-142, state.py:1515-1527 |
| Validation/Parity | `_idx_kwargs()` must mirror `cli._idx_kwargs()` field-for-field (explicitly documented in `CLAUDE.md`), including `embedding.dimensions` — confirmed identical except the web version omits `reset_mismatched` | web_app.py:95-104 vs. cli.py:83-93 |
| Error handling | `safe_error()` returns the raw (truncated) message for an explicit `UserFacingError`, a generic PT-BR fallback for an empty message, and a generic redacted message whenever the text contains any of a fixed set of sensitive substrings (`api_key`, `api key`, `secret`, `password`, `token`, `/home/`, `\users\`) | web_app.py:29-40 |
| Error handling | Every dispatched operation is wrapped in one `try/except Exception` in `_execute`; the worker thread itself never dies from a failed job — it logs the full traceback server-side and persists only the sanitized message | web_app.py:191-212 |
| Business logic | `retry_chunks` resets every `failed` chunk (optionally filtered by `source_id`) back to `pending`, one `update_chunk_status` call per row; returns the count reset | web_app.py:220-224, state.py:886-892, 912-925 |
| Business logic | `retry_assets` is a single bulk `UPDATE assets SET status='pending' WHERE status='failed'`; returns the SQLite `rowcount` | web_app.py:225-226, state.py:1384-1390 |
| Business logic | `harvest` treats "no new sources created" as either a benign idempotent no-op (if the selected file is already linked to a `source_id` in `files`) or a `UserFacingError` (otherwise) — never a silent empty success | web_app.py:270-297 |
| Business logic | The web `"review"` operation approves/rejects specific `chunk_ids` directly (or all `awaiting_review` chunks below a `confidence_below` threshold), **bypassing** `literature_review.auto_approve_min_confidence` entirely — unlike `run_all`'s Phase 3, which calls `run_review(auto_approve=True, interactive=False)` and only auto-approves chunks **at or above** that same threshold | web_app.py:304-335 vs. web_app.py:249-252, review.py:160-206 |
| Business logic | `garden` never forwards a `recreate` flag — the web UI can only run incremental MOC generation (`gardener.run_garden`/`gardener_hub.run_garden_hubs` default `recreate=False`); `--recreate` is CLI-only, matching `CLAUDE.md`'s "Not exposed in web" list | web_app.py:341-348, gardener.py:60-62, gardener_hub.py:183-185 |
| Business logic | `run_all` hardcodes non-interactive settings for every phase (`interactive=False`, `auto_approve=False` for extract, `auto_approve=True, interactive=False` for review) and defaults `skip_paging=True` — differing from the standalone `harvest` operation's own default of `skip_paging=False` | web_app.py:237-262 vs. web_app.py:270-284 |
| Correlation | A completed job's `run_id` (linking it to the `runs`/cost-tracking table) is inferred by diffing `db.get_last_run()` before and after dispatch, not returned explicitly by the pipeline call | web_app.py:188-197 |
| Cost tracking | `"review"`'s dispatch branch manages its own `runs` row (`db.start_run("review")` + `begin_run`/`finish_pipeline_run`) instead of delegating to `review.run_review`, which independently does the exact same thing when called directly (by the CLI or by `run_all`) | web_app.py:315-334 vs. review.py:174-206 |
| Data shape | `_load_candidates()` only returns concepts whose `candidate_json` is non-empty, parsed via `PermanentNoteCandidate.model_validate_json`; concepts without a stored candidate are silently skipped | web_app.py:107-120 |
| Progress | Every operation unconditionally emits one "Carregando dependências…" checkpoint before branching, regardless of whether the operation actually needs a `VectorIndex` | web_app.py:219 |

### Detailed breakdown of the business rules

---

### Business Rule: Single-mutating-job mutual exclusion

**Overview**:
`WebWorker`/`StateDB` guarantee that only one pipeline-mutating operation can be `queued` or `running` at any given moment, system-wide, regardless of how many HTTP requests arrive concurrently.

**Detailed description**:
`create_web_job` (state.py:1529-1550) opens an explicit `BEGIN IMMEDIATE` transaction — SQLite's write-lock-acquiring transaction mode — before checking `SELECT job_id FROM web_jobs WHERE state IN ('queued','running') LIMIT 1`. If any row matches, the transaction is rolled back and the function returns `False` without inserting anything; only when no such row exists does it insert the new job and commit. `BEGIN IMMEDIATE` is the deliberate choice here: a plain `BEGIN DEFERRED` transaction would only acquire the write lock lazily at the first write statement, opening a window where two concurrent callers could both pass the `SELECT` check before either has written, defeating the guarantee under real concurrency. `WebWorker.submit()` (web_app.py:152-162) surfaces this as a simple `job_id | None` return value — `None` means "rejected, something is already active" — which `web.py`'s `_post_job()` helper (web.py:310-319) turns into an HTTP 409 with a fixed PT-BR message.

This rule exists because the underlying pipeline modules (`harvester`, `extractor`, `connector`, `gardener`) are not designed for concurrent invocation against the same `StateDB`/vault/Chroma collection — they assume exclusive access to in-progress state (e.g., chunk status transitions, vault file writes, Chroma upserts). Rather than adding locking inside each pipeline module, the constraint is centralized once, at the queue's insertion point, which is also the cheapest and least invasive place to enforce it. The trade-off is that the guarantee is strictly single-instance: it protects against concurrency *within one `state.db` file*, and `CLAUDE.md` explicitly documents the accompanying deployment assumption ("single Uvicorn worker") — if that assumption were violated (e.g., multiple Uvicorn worker processes each running their own `WebWorker` daemon thread against the same SQLite file), the `BEGIN IMMEDIATE` guard would still correctly prevent two jobs from being inserted concurrently, but each process's independent 0.5-second polling loop would race to claim whatever single queued job exists — `claim_web_job`'s own atomic `UPDATE ... WHERE state='queued'` (state.py:1552-1559) is the second, necessary half of this protection, ensuring only one of those racing claims actually succeeds.

Read-only operations (`dashboard`, `jobs`, `job`, `events`, and the config-reading `.cfg` property) are entirely exempt from this rule — they open independent `StateDB` connections with no `BEGIN IMMEDIATE` semantics, since concurrent reads are safe under SQLite's WAL mode (per `CLAUDE.md`'s architecture notes on `state.py`).

**Rule workflow**:
```
WebWorker.submit(operation, payload):
  job_id = uuid4().hex
  db = fresh StateDB connection
  created = db.create_web_job(job_id, operation, payload):
      BEGIN IMMEDIATE
      IF EXISTS (web_jobs WHERE state IN ('queued','running')):
          ROLLBACK; return False
      ELSE:
          INSERT web_jobs (..., state='queued', ...); COMMIT; return True
  db.close()
  IF NOT created: return None          # caller (web.py) → HTTP 409
  self._wake.set()                     # nudge the polling loop
  return job_id
```

---

### Business Rule: Startup recovery — running jobs are lost, queued jobs resume

**Overview**:
When the process restarts (deploy, crash, manual stop), any job that was actively `running` is marked `interrupted` and is never retried automatically; any job that was merely `queued` (never claimed) is left untouched and picked up normally by the new worker thread.

**Detailed description**:
`WebWorker.start()` (web_app.py:135-144) is called exactly once, from the FastAPI `lifespan` context manager (web.py:98-104), before the polling thread is started. It opens a `StateDB`, calls `db.recover_web_jobs()` (state.py:1515-1527) — a single `UPDATE web_jobs SET state='interrupted', phase='interrupted', message='Interrompido pela reinicializacao da aplicacao', finished_at=? WHERE state='running'` — and logs a warning if any rows were affected (web_app.py:138-140). Because the SQL predicate is `WHERE state='running'` specifically (not `IN ('queued','running')`), a job that was inserted but never claimed before the shutdown remains `state='queued'` and is architecturally indistinguishable from a job submitted after the restart: the new worker's `_run()` loop (web_app.py:164-176) will find it via the same `list_web_jobs(limit=1)` + `state == 'queued'` check it uses for any other queued job, and process it as normal — effectively an automatic resume, with no special-casing in the code for "was this job queued before or after I started."

The design deliberately does **not** attempt to resume a `running` job's in-flight progress — `interrupted` is a terminal state (the job's `finished_at` is stamped, per `recover_web_jobs`'s explicit `finished_at=?` in its `UPDATE`), and nothing in `WebWorker` re-queues it. This is a conservative choice: a pipeline stage killed mid-write (e.g., partway through writing chunks, mid-LLM-call, or mid-vault-file-write) cannot be safely assumed to be resumable from where it left off, since the underlying pipeline functions are not written as resumable/idempotent-per-step state machines at that granularity (they rely on `StateDB` status columns like `pending`/`awaiting_review`/`approved` for their own internal idempotency across *whole* command invocations, not sub-command checkpoints). The operator-facing recovery path is instead the existing per-operation retry primitives (`retry_chunks`, `retry_assets`) or simply re-submitting the same operation, which will naturally pick up wherever the underlying `StateDB` status columns left off.

**Rule workflow**:
```
On process start (WebWorker.start()):
  db = StateDB(...)
  recovered_count = db.recover_web_jobs()
    → UPDATE web_jobs SET state='interrupted', phase='interrupted',
        message='Interrompido pela reinicializacao da aplicacao',
        finished_at=NOW() WHERE state='running'
  IF recovered_count > 0: logger.warning(...)
  db.close()
  start daemon thread WebWorker._run()

WebWorker._run() loop (unchanged by recovery):
  list_web_jobs(limit=1) → newest row by created_at DESC
  IF newest.state == 'queued': self._execute(newest.job_id)
  ELSE: wait 0.5s on self._wake, loop
```

---

### Business Rule: `_idx_kwargs` parity with the CLI, minus mismatch recovery

**Overview**:
`web_app._idx_kwargs(cfg)` must construct the exact same `VectorIndex` embedding configuration as `cli._idx_kwargs(cfg)` so that a source harvested/embedded via the CLI and one harvested/embedded via the web UI land in the same Chroma vector space — but the web version cannot recover from a detected mismatch the way the CLI can.

**Detailed description**:
Both functions build an identical seven-key dict — `chroma_path`, `embedding_provider`, `embedding_model`, `device`, `allow_fallback`, `base_url`, `dimensions` (web_app.py:95-104 vs. cli.py:83-92) — read from the same `AppConfig.embedding`/`AppConfig.device`/`AppConfig.chroma_path` fields. `CLAUDE.md`'s own architecture notes call this out explicitly: "`_idx_kwargs` must mirror `cli._idx_kwargs`) (including `embedding.dimensions`) when opening `VectorIndex`," and a dedicated unit test (`tests/test_web_state.py::test_idx_kwargs_forwards_embedding_dimensions`) asserts the `dimensions` field specifically survives the round trip. This parity matters because `VectorIndex.__init__` (index.py:206-225) uses these exact values, at construction time, to detect whether the *currently configured* embedding provider/model/dimensions differ from whatever is already recorded in the Chroma collection's stored metadata (index.py:226-253) — if the web path silently used different defaults than the CLI (e.g., a different `device` or a missing `dimensions`), it could either falsely trigger a mismatch or, worse, mask a real one.

The one confirmed divergence is `reset_mismatched`: `cli._idx_kwargs()` accepts a `reset_mismatched: bool = False` keyword and the CLI's own `_get_idx()` helper (cli.py:137-152) catches `EmbeddingSpaceMismatch`, prompts the operator interactively (or via `--yes`), and on confirmation reopens `VectorIndex` with `reset_mismatched=True` to trigger a full reindex. `web_app._idx_kwargs()` has no `reset_mismatched` parameter at all, so `VectorIndex(**_idx_kwargs(cfg))` (web_app.py:229) always constructs the index with the class default `reset_mismatched=False`. Consequently, if a job's dispatch reaches this line while the configured embedding differs from what Chroma already stores, `EmbeddingSpaceMismatch` (a plain `Exception` subclass, index.py:119-142) propagates straight out of `_dispatch`, is caught by `_execute`'s generic `except Exception` (web_app.py:203-210), and the job simply fails with whatever `safe_error()` produces from the mismatch message — there is no web-native path to trigger the CLI's reindex recovery; an operator must use `zettel reindex --force` from a terminal to unblock the web UI.

This is a deliberate simplification (a web job cannot safely prompt an interactive confirmation mid-request), but it means an embedding configuration change made through `config.yaml` — with no code change — silently breaks every subsequent web-submitted job (except `retry_chunks`/`retry_assets`, which never instantiate `VectorIndex`) until someone runs the CLI reindex out-of-band. See §10 for the risk rating.

**Rule workflow**:
```
_dispatch(cfg, db, progress, operation, payload):
  IF operation in {retry_chunks, retry_assets}:
      # never touches VectorIndex — always succeeds independent of embedding config
      return ...
  ELSE:
      idx = VectorIndex(**_idx_kwargs(cfg))     # reset_mismatched always False here
        → VectorIndex.__init__ compares cfg.embedding.{provider,model,dimensions}
          against Chroma's stored metadata
        → IF mismatch AND NOT reset_mismatched: raise EmbeddingSpaceMismatch
             → uncaught inside _dispatch → propagates to _execute's except Exception
             → job persisted as state='failed', message=safe_error(exc)
        → IF match (or no prior stored metadata): idx is usable; dispatch proceeds
```

---

### Business Rule: `safe_error()` — the browser-facing sanitization boundary

**Overview**:
Every exception that reaches `_execute`'s `except` clause is converted through `safe_error()` before being persisted as the job's `message`/`error_message` — the one deliberate chokepoint preventing internal exception text (which may contain API keys, secrets, or absolute host paths) from ever reaching a browser.

**Detailed description**:
`safe_error(exc)` (web_app.py:29-40) applies, in order: (1) strip newlines and surrounding whitespace from `str(exc)`; (2) if `exc` is a `UserFacingError` (a `RuntimeError` subclass explicitly defined for this purpose, web_app.py:25-26), return the stripped text truncated to 300 characters, unconditionally trusting it as safe — this is the codebase's explicit "this message was authored to be shown to a user" escape hatch, used exactly once in the codebase, for the harvest "no sources created" case (web_app.py:292-296); (3) if the stripped text is empty, return a generic PT-BR fallback ("A operação falhou. Consulte os logs do servidor."); (4) otherwise, lower-case the text and check it for any of six fixed substrings — `api_key`, `api key`, `secret`, `password`, `token`, `/home/`, `\users\` — and if any match, return a different generic fallback ("A operação falhou. Verifique a configuração e os logs do servidor.") instead of the original text; (5) if none matched, return the original (unredacted) text, truncated to 300 characters.

This is a **denylist**, not an allowlist: any exception whose message happens not to contain one of those six substrings passes through verbatim (truncated only for length). This is a conscious trade-off — most internal Python/library exceptions (a `ValueError` from a malformed payload, a `FileNotFoundError`, a Pydantic validation error) are diagnostically useful to show an operator and unlikely to contain literal credentials, so blanket redaction would degrade usefulness; the denylist specifically targets the known-dangerous cases (LLM provider client libraries echoing back the request including an `Authorization` header or API key in a stack trace message, or a path-based error revealing the host's home directory / Windows user profile path). The check is deliberately substring-based and case-insensitive on the whole message, not structured (e.g., it does not attempt to detect a bearer-token-shaped string), so it is a heuristic net rather than a guarantee — a credential embedded in an exception message using different wording (e.g., "credential" instead of "secret") would not be caught. Full, unredacted exception detail is never lost: `_execute`'s except clause always logs the exception and full traceback via `logger.error(...)` (web_app.py:204) before calling `safe_error()`, so the sanitization only affects what is written to the `web_jobs`/`web_job_events` tables (and therefore what the browser can ever see), not the server's own logs.

**Rule workflow**:
```
safe_error(exc):
  text = str(exc).replace("\n", " ").strip()
  IF isinstance(exc, UserFacingError):
      return text[:300]                                  # trusted, authored message
  IF text == "":
      return "A operação falhou. Consulte os logs do servidor."
  IF any(word in text.lower() for word in
         ("api_key","api key","secret","password","token","/home/","\\users\\")):
      return "A operação falhou. Verifique a configuração e os logs do servidor."
  return text[:300]                                       # passthrough, length-capped
```

---

### Business Rule: Harvest's idempotency check for an already-processed file

**Overview**:
When a harvest run produces zero new sources for a specific selected file, `_dispatch` distinguishes "this file was already fully ingested — nothing to do" (a benign, successful no-op result) from "this file genuinely could not be harvested" (a `UserFacingError`), instead of treating both as the same silent empty result.

**Detailed description**:
`_dispatch`'s `"harvest"` branch (web_app.py:270-297) resolves the payload's `selected_file` to an absolute `Path`, calls `run_harvest(..., selected_file=file_path, ...)`, and inspects the returned `sources` list. If it is non-empty, the branch returns immediately with `{"sources": sources}` (web_app.py:297) — the common case. If it is empty, the branch does **not** immediately raise; it first looks up `db.get_file(str(file_path))` (web_app.py:286) to check whether that exact file path is already linked to a `source_id` in the `files` table. If so, it returns a **successful** result carrying the existing `source_id` and a `"skipped"` explanation string ("Documento já ingerido; nenhuma alteração necessária.", web_app.py:287-291) rather than an error — from the job-state machine's perspective this job still transitions to `succeeded`, not `failed`. Only when no existing linked source is found does it raise `UserFacingError("Nenhuma fonte foi criada. Verifique se o documento contém texto extraível, se não é uma duplicata e se as opções bibliográficas estão corretas.")` (web_app.py:292-296), which — per the `safe_error()` rule above — is trusted verbatim and shown to the operator.

This distinction exists because `run_harvest` can legitimately return an empty `new_sources` list for reasons that are not failures at all: the three-layer duplicate-detection system (file hash / extraction hash / semantic similarity, per `CLAUDE.md`'s harvester notes) can recognize the selected file as a byte-identical or content-identical duplicate of an already-harvested source and reuse the existing `source_id` without creating a new row — in that case, `run_harvest` itself returns no new source ids even though the file is, in a meaningful sense, "successfully processed" (it was mapped onto pre-existing data). Without the `db.get_file(...)` check, the web UI would surface a scary, generic failure for what is actually a correct and expected outcome of the dedup system, undermining the operator's trust in the "harvest a specific file" action. The check is deliberately scoped to only fire when a `selected_file` was actually specified (`file_path` is not `None`) — a harvest run over the whole inbox with genuinely zero eligible files present takes a different code path entirely (an empty inbox or a directory with no supported files returns `sources=[]` from `run_harvest`, but that branch of `_dispatch` is reached only from the `"harvest"` operation, which per `web.py`'s routes always specifies a single `selected_file`; the CLI's whole-inbox harvest is not exposed through this operation).

**Rule workflow**:
```
"harvest" branch of _dispatch:
  sources = run_harvest(cfg, db, idx, selected_file=file_path, ...)
  IF sources:
      return {"sources": sources}                          # success, new source(s)
  ELSE:
      existing = db.get_file(str(file_path)) if file_path else None
      IF existing AND existing.get("source_id"):
          return {"sources": [existing["source_id"]],
                   "skipped": "Documento já ingerido; nenhuma alteração necessária."}
                                                             # success, idempotent no-op
      ELSE:
          raise UserFacingError("Nenhuma fonte foi criada. ...")
                                                             # failure, shown verbatim
```

---

### Business Rule: Two different confidence-gating behaviors under one "review" umbrella

**Overview**:
The web UI's manual review action (approve/reject specific chunks) applies no confidence threshold at all — whatever the operator selects is approved or rejected — while `run_all`'s automated Phase 3 review step applies the configured `literature_review.auto_approve_min_confidence` threshold and silently skips anything below it.

**Detailed description**:
`_dispatch`'s `"review"` branch (web_app.py:304-335) is invoked from `web.py`'s `POST /review/action` route, driven by a human operator viewing the `/review` page and selecting specific chunk checkboxes (or, via `confidence_below`, requesting "everything currently below X confidence"). It resolves `chunk_ids` either directly from the payload or, if empty and `confidence_below` is present, by filtering `db.get_chunks_by_status("awaiting_review")` client-side in Python (web_app.py:309-312) — a linear scan, not a SQL predicate. It then calls `approve_chunk(cfg, db, idx, chunk_id)` or `reject_chunk(cfg, db, idx, chunk_id)` (review.py:387-481) **directly, per chunk**, with no confidence check anywhere in this loop — the operator's selection is authoritative. Once the loop finishes, if at least one approval succeeded, it calls `finalize_approved_concepts(cfg, db, idx)` once (web_app.py:329-330), which is documented in `review.py` (line 380-384) as running "post-approval deduplication after granular web review actions" — i.e., this function exists specifically to give the web's chunk-by-chunk approval flow the same concept-deduplication step that `review.run_review`'s own internal loop performs automatically after each batch.

By contrast, `run_all`'s Phase 3 (web_app.py:249-252) calls `review.run_review(cfg, db, idx, auto_approve=True, interactive=False)` directly — and `run_review`'s own auto-approve branch (review.py:194-206) **does** check `conf = chunk.get("review_confidence") or 0; if conf >= limiar: approve_chunk(...) else: stats["skipped"] += 1` for every chunk, where `limiar = cfg.literature_review.auto_approve_min_confidence`. So under `run_all`, a chunk with low LLM-reported confidence is left `awaiting_review` (never approved, never rejected) and must be handled later through the manual `/review` page — but once it reaches that manual page, the operator's explicit approve/reject action bypasses the same threshold that kept it out of `run_all`'s automatic pass. This is not a bug — a human explicitly reviewing and approving a low-confidence chunk is the intended escape hatch the threshold exists to gate — but it means "confidence threshold" has two different enforcement points with two different behaviors (automatic hard gate vs. informational filter on the UI) depending on which of this component's two review code paths executes.

**Rule workflow**:
```
Manual review (web_app.py "review" operation, human-selected chunk_ids):
  FOR each chunk_id in chunk_ids:
      approve_chunk(...) or reject_chunk(...)     # NO confidence check — operator decides
  IF any approved: finalize_approved_concepts(...)  # dedupe once, after the batch

Automated review (run_all Phase 3 → review.run_review(auto_approve=True, interactive=False)):
  FOR each chunk in get_chunks_by_status("awaiting_review"):
      conf = chunk.review_confidence or 0
      IF conf >= cfg.literature_review.auto_approve_min_confidence:
          approve_chunk(...)                       # confidence GATES approval
      ELSE:
          skip (stays awaiting_review — surfaces later on the /review page)
  IF any approved: _dedupe_approved_concepts(...)   # same dedupe step, called internally
```

---

### Business Rule: Run correlation is inferred, not returned

**Overview**:
A completed job's association with a row in the `runs` (cost-tracking) table is determined by comparing "what was the last run before dispatch" to "what is the last run after dispatch" — the pipeline functions never hand `_execute` a `run_id` directly.

**Detailed description**:
`_execute()` (web_app.py:178-212) captures `previous_run = db.get_last_run()` (a single "most recent row by `run_id`" query, state.py:1510-1511) before calling `self._dispatch(...)`. After dispatch returns successfully, it re-queries `last_run = db.get_last_run()` and computes `run_id = last_run["run_id"] if last_run and last_run["run_id"] != previous_run_id else None` (web_app.py:193-197). This works because every pipeline function that performs LLM/embedding work (`run_harvest`, `run_extract`, the review branch's own `db.start_run("review")`, `run_connect`, `run_garden`/`run_garden_hubs`) calls `StateDB.start_run(signature)` (state.py:1422-1428) near the top of its own execution, which does a plain auto-incrementing `INSERT INTO runs (...)` — so if dispatch created any new run at all, it is necessarily the new "last" one, since `runs.run_id` is monotonically increasing and jobs are strictly serialized (only one can be running at a time, per the mutual-exclusion rule above).

The consequence of this design is that `"sync"` (which never calls `start_run` — confirmed by inspection of `sync.run_sync_manual`, sync.py:39-onward, which has no `db.start_run` call anywhere in its body) always produces `run_id=None` on its job record, even though it did real, potentially lengthy work. Likewise `"retry_chunks"`/`"retry_assets"` never reach a run-creating pipeline function at all. This means the `runs` table (and therefore any cost/token reporting derived from it) has no row for sync or retry jobs — which is consistent with those operations not calling any LLM (sync only reads/writes vault files and SQLite/Chroma metadata; retries only flip status columns), so there is genuinely no cost to attribute. The heuristic's only latent fragility is that it assumes strict serialization holds — if that invariant were ever violated (e.g., a hypothetical second worker process, contrary to the single-worker deployment assumption), a job's inferred `run_id` could be misattributed to a run started by a different, concurrently-executing job.

**Rule workflow**:
```
_execute(job_id):
  previous_run_id = db.get_last_run()?.run_id
  result = _dispatch(...)                    # may or may not call db.start_run() internally
  last_run = db.get_last_run()
  run_id = last_run.run_id if (last_run exists AND last_run.run_id != previous_run_id) else None
  update_web_job(job_id, ..., run_id=run_id, ...)
```

---

### Business Rule: Progress checkpoints are unconditionally dual-persisted

**Overview**:
`JobProgress.emit()` — the only method that actually writes a checkpoint — always performs two separate SQLite statements for every single progress event, with no batching, coalescing, or option to skip either write.

**Detailed description**:
`JobProgress.emit(event)` (web_app.py:59-75) calls, in order, `self.db.update_web_job(self.job_id, phase=event.phase, current_item=event.current_item, current_index=event.current_index, total_items=event.total_items, message=event.message)` and then `self.db.add_web_job_event(self.job_id, event.phase, current_item=..., current_index=..., total_items=..., message=event.message)`. `update_web_job` (state.py:1581-1612) uses `COALESCE(?, column)` for every nullable field, meaning a checkpoint that omits (passes `None` for) `current_item` does **not** blank out whatever value was stored from a previous checkpoint — the `web_jobs` row therefore has "sticky per-column" semantics rather than "each update fully replaces the row's visible state." `add_web_job_event` (state.py:1614-1625), in contrast, is an unconditional `INSERT` that stores exactly what was passed, `None` values included, into an append-only, auto-incrementing (`event_id`) audit log with no `UPDATE`/`DELETE` path defined anywhere in the codebase for that table (only the `ON DELETE CASCADE` foreign key tying it to its parent `web_jobs` row, state.py:249-259, for when a job row itself is ever removed — no such deletion code was found for `web_jobs` either, so in practice these rows accumulate for the life of `state.db`).

This dual-write design directly serves the two different read patterns the web UI needs: `WebApplication.job(job_id)` (a cheap point lookup for "what is this job's current summary state," used by the job detail page's initial render and every polling refresh) and `WebApplication.events(job_id, after)` (an `event_id > after` cursor query, used both by the same polling endpoint to report only new events and by the Server-Sent-Events stream at `/api/jobs/{id}/events`). Every checkpoint costs two separate `commit()` calls with no shared transaction wrapping them together — a process crash between the two commits (e.g., inside `WebWorker._run()`'s thread, at the exact moment of a checkpoint) could in principle leave `web_jobs`'s snapshot one checkpoint behind `web_job_events`'s history; this window is narrow (two back-to-back synchronous SQLite statements with no I/O in between) and was not observed failing in the test suite, but it is a structural possibility with no defensive code guarding it.

**Rule workflow**:
```
JobProgress.emit(event: ProgressEvent):
  db.update_web_job(job_id, phase=event.phase, current_item=event.current_item,
                     current_index=event.current_index, total_items=event.total_items,
                     message=event.message)
    → SQL: UPDATE web_jobs SET col = COALESCE(?, col) for each field; COMMIT
  db.add_web_job_event(job_id, event.phase, current_item=..., current_index=...,
                        total_items=..., message=event.message)
    → SQL: INSERT INTO web_job_events (unconditional, including NULLs); COMMIT
```

## 4. Component Structure

`web_app.py` is a single, self-contained module. Its functional boundary for this analysis also includes the SQLite schema/CRUD it exclusively owns in `state.py` and the shared `ProgressObserver` contract it implements.

```
zettel/
├── web_app.py                      # THE COMPONENT (405 lines)
│   ├── UserFacingError             # (25-26)  RuntimeError subclass — "safe to show" marker
│   ├── safe_error()                # (29-40)  Exception → sanitized browser-facing string
│   ├── ProgressEvent                # (43-49)  frozen dataclass — one progress checkpoint
│   ├── JobProgress                  # (52-92)  ProgressObserver implementation → SQLite (dual-write)
│   ├── _idx_kwargs()                # (95-104) VectorIndex ctor kwargs (mirrors cli._idx_kwargs)
│   ├── _load_candidates()           # (107-120) approved concepts w/o notes → PermanentNoteCandidate list
│   ├── WebWorker                    # (123-353) durable SQLite job queue + single daemon worker thread
│   │   ├── start() / stop()         # (135-150) recovery + thread lifecycle
│   │   ├── submit()                 # (152-162) atomic enqueue (mutual exclusion)
│   │   ├── _run()                   # (164-176) polling loop (0.5s wake-or-timeout)
│   │   ├── _execute()               # (178-212) claim → dispatch → persist result/error
│   │   └── _dispatch() [staticmethod] # (214-353) operation router — 8 named operations
│   └── WebApplication               # (356-405) facade used by zettel/web.py's HTTP routes
│       ├── start()/stop()/db()/cfg  # lifecycle + fresh-connection accessors
│       ├── submit()                 # delegates to WebWorker
│       └── dashboard()/jobs()/job()/events()  # read-only StateDB proxies
├── progress.py                     # Defines ProgressObserver (Protocol) that JobProgress
│                                    #   satisfies structurally (no import relationship either way)
├── state.py                        # Owns the durable schema this component exclusively writes/reads:
│   ├── web_jobs / web_job_events tables          (230-259)
│   └── recover_web_jobs/create_web_job/claim_web_job/get_web_job/list_web_jobs/
│       update_web_job/add_web_job_event/list_web_job_events/get_web_dashboard  (1515-1665)
├── config.py                       # AppConfig / load_config — re-loaded fresh on every _db()/_execute() call
├── web.py                          # Sole consumer: FastAPI routes call WebApplication exclusively;
│                                    #   owns the FastAPI `lifespan` that starts/stops the worker (98-104)
├── harvester.py / extractor.py / review.py / connector.py / gardener.py /
│   gardener_hub.py / sync.py       # The eight dispatched pipeline operations' actual implementations
└── index.py                        # VectorIndex — constructed via _idx_kwargs() for 6 of 8 operations
```

## 5. Dependency Analysis

```
Internal Dependencies:

zettel.web.py (HTTP layer)
    └── zettel.web_app.WebApplication  (facade; instantiated once per process in FastAPI lifespan)
          ├── zettel.web_app.WebWorker
          │     ├── zettel.config.{AppConfig, load_config}      (fresh AppConfig per _db()/_execute() call)
          │     ├── zettel.state.StateDB                          (web_jobs/web_job_events CRUD, per-call connections)
          │     ├── zettel.web_app.JobProgress → zettel.state.StateDB (progress persistence)
          │     └── zettel.web_app._dispatch() [lazy imports, per operation]:
          │           ├── zettel.index.VectorIndex                (6 of 8 operations)
          │           ├── zettel.harvester.run_harvest             ("harvest", "run_all")
          │           ├── zettel.extractor.run_extract             ("extract", "run_all")
          │           ├── zettel.review.{run_review, approve_chunk,
          │           │      reject_chunk, finalize_approved_concepts}  ("review", "run_all")
          │           ├── zettel.connector.run_connect             ("connect", "run_all")
          │           ├── zettel.gardener.run_garden                ("garden", "run_all")
          │           ├── zettel.gardener_hub.run_garden_hubs        ("garden" + hubs=True)
          │           ├── zettel.sync.run_sync_manual               ("sync")
          │           └── zettel.usage.{begin_run, finish_pipeline_run}  ("review" branch's own run bookkeeping)
          └── zettel.state.StateDB   (direct, for dashboard()/jobs()/job()/events()/db())

External Dependencies:
- Python stdlib `threading`  — the single daemon worker thread (`threading.Thread`, `threading.Event`)
- Python stdlib `uuid`       — job_id generation (uuid4().hex)
- Python stdlib `dataclasses` — ProgressEvent (frozen)
- Python stdlib `json`        — payload/result JSON round-tripping (delegated to StateDB internally)
- SQLite (bundled, via zettel.state.StateDB) — the durable queue's sole storage engine (WAL mode)
- ChromaDB (via zettel.index.VectorIndex, transitively) — required by 6 of 8 operations
- Pydantic (via zettel.schemas.PermanentNoteCandidate) — used only inside _load_candidates()
```

## 6. Afferent and Efferent Coupling

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| `WebApplication` | 1 (only `zettel/web.py`, but from nearly every route handler — ~20 call sites) | 1 (`WebWorker`) + `StateDB` (direct, for read-only accessors) | High — sole entry point between the entire HTTP layer and the pipeline; any signature change here breaks every route |
| `WebWorker` | 1 (`WebApplication`, composition) | 9 (`StateDB`, `load_config`, `JobProgress`, and 7 lazily-imported pipeline modules: harvester, extractor, review, connector, gardener, gardener_hub, sync, plus `index.VectorIndex`) | High — the orchestrator; high fan-out is structurally inherent to being the dispatch table for every pipeline phase |
| `JobProgress` | Passed as `observer=` into 4 pipeline functions per `run_all` (harvest, extract, connect, garden) + constructed manually by `_dispatch`'s own `"review"`/`"sync"` branches (2 more effective call sites) | 1 (`StateDB.update_web_job` + `StateDB.add_web_job_event`) | Medium — structurally typed (no import-time link to `progress.ProgressObserver`); a signature drift would only surface as a runtime `TypeError` |
| `ProgressEvent` | 2 (`JobProgress.emit`, and `WebWorker._dispatch`'s direct construction for milestone/phase announcements) | 0 (plain frozen dataclass) | Low |
| `_idx_kwargs()` | 1 (`WebWorker._dispatch`, one call site, reused across 6 of 8 operations) | 0 (pure config → dict transform) | Medium — silent parity requirement with `cli._idx_kwargs`; drift here is not caught by any shared test |
| `_load_candidates()` | 2 (`_dispatch`'s `"run_all"` and `"connect"` branches) | 1 (`StateDB.get_concepts_by_status`) + `zettel.schemas.PermanentNoteCandidate` | Medium |
| `safe_error()` / `UserFacingError` | 1 real call site each within this module (`_execute`'s except clause; the harvest branch's raise) — plus 1 direct unit-test call site (`tests/test_web_state.py`) | 0 | Medium — the sole exception-sanitization boundary before any message reaches a browser; a regression here is a direct information-disclosure risk |

## 7. Endpoints

Not applicable. `zettel/web_app.py` defines no HTTP/GraphQL/gRPC routes of its own — it is a pure service/application layer. The HTTP surface that consumes it belongs to the separate `zettel/web.py` component (`/documents/harvest`, `/documents/run-all`, `/pipeline/{operation}`, `/review/action`, `/api/jobs/{job_id}`, `/api/jobs/{job_id}/events`, etc.). The internal "operation" contract this component exposes to that HTTP layer is documented as the Business Rules operations catalog in §3 and the dispatch table in §4.

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| `zettel.state.StateDB` | Internal persistence (SQLite, WAL) | Durable job queue (`web_jobs`), progress/audit log (`web_job_events`), and all pipeline read/write access needed by dispatched operations | Synchronous `sqlite3` calls, one connection opened and closed per method call (`db()`, `_db()`, `_execute()`) | SQL rows / JSON-encoded `payload_json`/`result_json` columns | No retries; each write auto-commits individually; a job-level failure is caught generically in `_execute` and persisted via `safe_error()` |
| `zettel.index.VectorIndex` (ChromaDB) | Internal service dependency | Embedding/vector storage needed by 6 of 8 operations (`harvest`, `extract`, `review`, `connect`, `garden`, `run_all`) | In-process Python calls to `chromadb` client | Vectors + metadata dicts (`_sanitize_metadata`, per project convention) | `EmbeddingSpaceMismatch` from `VectorIndex.__init__` is **not** specially handled here — it propagates to `_execute`'s generic exception handler and fails the job (see §3, §10) |
| `zettel.harvester` / `extractor` / `review` / `connector` / `gardener` / `gardener_hub` / `sync` | Internal pipeline modules | The actual business logic dispatched per operation | Direct in-process function calls, lazily imported per branch inside `_dispatch` | Python objects (lists of ids, dict stat summaries) | Each module raises its own exceptions on failure; none are caught inside `_dispatch` itself — all bubble to `_execute`'s single `except Exception` |
| `zettel.progress.ProgressObserver` (structural contract) | Internal interface | Lets the pipeline modules report checkpoints without depending on `web_app.py` | Direct method call (`observer.update(...)`), structurally typed, no shared import | Positional/keyword args (`phase`, `message`, `current_item`, `current_index`, `total_items`) | No error handling — a raising `JobProgress.update()` (e.g., a DB error mid-checkpoint) would abort the entire pipeline stage, since nothing in `progress.report()` or the producers wraps the call in a try/except |
| `zettel.web.py` (consumer) | Internal HTTP layer | Every mutating/read route calls `WebApplication`'s public methods | Direct in-process Python calls (`request.app.state.service`) | Python dict/list return values, rendered into Jinja2 templates or JSON | `web.py` itself handles auth/CSRF/404s; this component never returns HTTP status codes, only data or exceptions |
| Background daemon thread (`threading.Thread`, name `zettel-web-worker`) | Concurrency primitive | Decouples HTTP request/response latency from potentially long-running pipeline execution | In-process thread, `threading.Event` for wake signaling | N/A | Thread runs until `WebWorker.stop()` sets `self._stop` and joins with a 5-second timeout (`web_app.py:146-150`); an unhandled exception inside `_execute` itself (rather than inside the dispatched pipeline call) is not additionally guarded — `_execute`'s own `try/except Exception` (188-212) is the only safety net, and its own `finally: db.close()` guarantees the thread loop always continues to the next iteration |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Facade | `WebApplication` presents a small, HTTP-friendly surface (`submit`, `dashboard`, `jobs`, `job`, `events`) over `WebWorker` + direct `StateDB` access | web_app.py:356-405 | Keeps `web.py` free of any direct `StateDB`/`WebWorker` construction or lifecycle concerns |
| Durable Command Queue | `WebWorker` persists every submitted operation as a row before executing it, with atomic enqueue/claim semantics | web_app.py:123-353, state.py:1529-1559 | Survives process restarts; a submitted-but-not-yet-run job is never silently lost |
| Single-writer / mutual-exclusion lock via database transaction | `create_web_job`'s `BEGIN IMMEDIATE` + existence check | state.py:1529-1550 | Enforces the "one mutating pipeline operation at a time" invariant without an in-process lock (works across process restarts too) |
| Strategy / dispatch table | `_dispatch`'s `if operation == "...":` chain, each branch lazily importing and calling a different pipeline module | web_app.py:214-353 | Single, auditable place where every web-triggerable operation is enumerated; new operations are added by extending this one function |
| Observer | `JobProgress` implements `ProgressObserver` structurally; pipeline functions call `report(observer, ...)` without knowing the concrete type | web_app.py:52-92, progress.py:8-17 | Lets the same pipeline code serve both the CLI (no-op observer) and the web worker (persisting observer) |
| Adapter (event → dual persistence shape) | `JobProgress.emit()` fans one `ProgressEvent` out into a "current state" row update and an "append-only history" row insert | web_app.py:59-75 | Serves both "what's happening now" (point lookup) and "what happened" (cursor/stream) queries from one event stream |
| Sanitizing boundary / denylist filter | `safe_error()` | web_app.py:29-40 | Centralizes the only place internal exception text is translated into browser-safe text |
| Marker exception | `UserFacingError(RuntimeError)` | web_app.py:25-26 | Lets a specific raise site (harvest's "nothing created" case) opt out of `safe_error()`'s denylist scan, trusting its own authored message |
| Immutable event record | `ProgressEvent` is `@dataclass(frozen=True)` | web_app.py:43-49 | Matches the append-only semantics of the `web_job_events` table it is persisted into |
| Lazy/deferred import | `VectorIndex` and every pipeline module are imported inside `_dispatch`'s branches, not at module top | web_app.py:228, 231-235, 271, 299, 305, 337, 343/346, 350 | Codebase-wide convention (shared with the CLI); avoids importing heavy dependencies (Chroma, LangChain, Docling) for operations that don't need them (`retry_chunks`/`retry_assets`) |
| Resource-scoped connection (manual try/finally, not a context manager) | Every `WebApplication` accessor opens a `StateDB` and closes it in `finally:` | web_app.py:380-404 | Guarantees no leaked SQLite connections per HTTP request, without introducing a connection pool |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|----------------|-------|--------|
| High | `WebWorker._dispatch` / `_idx_kwargs()` | `_idx_kwargs()` omits `reset_mismatched` (present in `cli._idx_kwargs()`), so `VectorIndex(**_idx_kwargs(cfg))` (web_app.py:229) always constructs with `reset_mismatched=False`; an `EmbeddingSpaceMismatch` (index.py:246-253) is never recoverable from the web path — it just fails the job | Any embedding provider/model/dimensions change in `config.yaml` silently breaks all 6 non-retry web operations until an operator runs `zettel reindex --force` from a terminal; the web UI offers no way to trigger or even clearly diagnose this without reading server logs |
| Medium | Documentation / code drift | `CLAUDE.md`'s "Not exposed in web" list includes `run-all`, but `web_app.py`'s `"run_all"` branch (web_app.py:230-269) and `web.py`'s `POST /documents/run-all` route (web.py:361-380) both exist and are exercised by `tests/test_web.py::test_documents_can_queue_full_pipeline` | Architecture documentation for this exact component is stale; a reader relying on `CLAUDE.md` alone would incorrectly believe `run_all` is CLI-only |
| Low | `zettel/web.py` (consumer) | `safe_error` is imported (web.py:24) but never called anywhere in that file — its only real call site is inside `web_app.py` itself (web_app.py:205) | Dead import; harmless but slightly misleading about where the sanitization boundary actually lives |
| Medium | `_dispatch`'s `"review"` branch | Duplicates `review.run_review`'s own run-bookkeeping (`db.start_run("review")` + `begin_run`/`finish_pipeline_run`) instead of delegating to it, and applies no confidence gate at all for operator-selected chunk approvals — a materially different behavior from `run_all`'s auto-approve pass through the same underlying `approve_chunk`/`reject_chunk` primitives | Two independent code paths implement "review a chunk" with different guarantees; a future change to `run_review`'s bookkeeping (e.g., additional cost-tracking fields) would not automatically propagate to the web's manual review path unless both call sites are updated in lockstep |
| Low | `WebWorker._execute` | `run_id` correlation (web_app.py:188-197) is inferred by diffing `db.get_last_run()` before/after dispatch rather than the dispatched function returning its own `run_id` explicitly | Correct only under the strict single-worker/single-job-at-a-time assumption already documented elsewhere; any future relaxation of that assumption would silently misattribute run costs to the wrong job |
| Low | `WebWorker._run` polling loop | Fixed 0.5-second poll timeout (web_app.py:173) even though `submit()` also proactively sets `self._wake` (web_app.py:161) — the timeout path is a pure fallback for missed wake signals, not the primary trigger, but there is no jitter/backoff, and the loop runs indefinitely at that cadence for the life of the process whenever the queue is empty | Negligible CPU cost given the 0.5s interval, but a hard-coded magic number with no configuration knob |
| Low | `JobProgress.emit` / `StateDB` writes | The two SQLite writes per checkpoint (`update_web_job`, `add_web_job_event`) are not wrapped in one transaction; each calls its own `commit()` | A crash between the two commits could leave the "current state" snapshot one checkpoint behind the append-only history; narrow window, not observed failing in the test suite |
| Low | `_dispatch`'s `"review"` branch | Confidence filtering for `confidence_below` is a Python-side linear scan over `db.get_chunks_by_status("awaiting_review")` (web_app.py:310-312) rather than a SQL `WHERE review_confidence < ?` predicate | Minor inefficiency at scale (loads every awaiting-review chunk into memory before filtering); not a correctness issue given expected review-queue sizes |
| Low | Payload validation | Job payloads are untyped `dict[str, Any]`; `_dispatch` reads keys via `.get(...)` with inline defaults rather than validating against a schema (e.g., a Pydantic model) before dispatch | A malformed/misnamed payload key silently falls back to a default rather than failing fast with a clear validation error; failures instead surface later, deeper inside the dispatched pipeline call |

## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|--------------------|----------|---------------|
| `WebWorker` queue/state machine (`create_web_job`, `claim_web_job`, `recover_web_jobs`, `update_web_job`) | 2 (`tests/test_web_state.py::test_web_queue_enforces_mutual_exclusion_and_transitions` lines 10-26; `::test_recovery_interrupts_running_but_keeps_queued` lines 29-40) | Transitively covered by `tests/test_web.py::test_navigation_and_retry_job_flow` (drives one real `retry_assets` job end-to-end through the actual daemon thread) | Good for the two headline invariants (mutual exclusion; recovery marks `running`→`interrupted` but leaves `queued` alone) | Solid, direct assertions on state transitions; does not test the `claim_web_job` atomicity itself under real concurrent threads (only sequentially, via direct calls) |
| `JobProgress` / progress persistence | 1 (`tests/test_web_state.py::test_progress_events_and_dashboard_are_persisted` lines 43-59, exercises `add_web_job_event`/`list_web_job_events`/`get_web_dashboard` directly on `StateDB`, not through `JobProgress` itself) | Transitively via `test_navigation_and_retry_job_flow` (asserts `payload["events"]` is non-empty for a real `retry_assets` job) | Partial — the `update_web_job` COALESCE "sticky field" semantics and the dual-write ordering inside `JobProgress.emit()` itself are not directly asserted anywhere | The dedicated test bypasses `JobProgress` and calls `StateDB` methods directly; no test constructs a `JobProgress` instance and calls `.update()`/`.emit()` on it in isolation |
| `WebWorker._dispatch` — `"run_all"` | 1 thorough test (`tests/test_web_state.py::test_run_all_dispatches_every_phase_in_order` lines 72-136) — every pipeline function monkeypatched, asserts call order, kwargs forwarded (`interactive=False`, `duplicate_action`), and the exact aggregated result shape | None beyond this unit test (no end-to-end `run_all` test drives real pipeline modules) | Good for orchestration/sequencing and kwargs-forwarding; does not exercise real `run_harvest`/`run_extract`/etc. logic, nor error propagation if one phase raises mid-sequence | Strong, explicit assertions (`assert progress.phases == [...]`, `assert result == {...}`); a solid contract test for the dispatch order specifically |
| `WebWorker._dispatch` — `"harvest"`, `"extract"`, `"review"`, `"connect"`, `"garden"`, `"garden"` w/ hubs, `"sync"` | 0 dedicated unit tests found for these individual branches in isolation | Indirect only, through `tests/test_web.py` routes that mostly **monkeypatch `service.submit` itself** (`test_nested_inbox_file_can_be_selected_for_harvest`, `test_documents_hide_completed_file_but_show_changed_copy`, `test_documents_can_queue_full_pipeline`) — meaning the actual `_dispatch` code for these branches is never executed by those tests, only the route-level payload construction is verified | Weak for `_dispatch`'s per-branch logic specifically: the "harvest already-ingested idempotency" rule (web_app.py:285-296), the confidence-bypass behavior of the `"review"` branch (web_app.py:304-335), and the `"garden"`/`hubs` flag forwarding (web_app.py:341-348) have no test that actually runs `_dispatch` and inspects the result for these cases | The route-level tests are good for HTTP-layer concerns (auth, CSRF, path traversal, 409s) but leave `_dispatch`'s own business logic for 6 of 8 operations effectively untested at the unit level |
| `retry_chunks` / `retry_assets` | 0 dedicated unit tests for `retry_chunks`; `retry_assets` is exercised once end-to-end | `tests/test_web.py::test_navigation_and_retry_job_flow` (lines 117-133) runs a real `retry_assets` job through the full worker and asserts `result == {"assets_reset": 0}` | Partial — only the empty-queue case (`assets_reset: 0`) is exercised; no test asserts `retry_chunks`' per-chunk reset behavior or `retry_assets`' behavior when failed rows actually exist | The one existing test is solid for what it covers (full round trip through the real daemon thread, polling `/api/jobs/{id}` until terminal) but does not cover the non-trivial reset-count path for either operation |
| `_idx_kwargs()` | 1 (`tests/test_web_state.py::test_idx_kwargs_forwards_embedding_dimensions` line 67-69) | None | Narrow but targeted — asserts exactly the field `CLAUDE.md` calls out as easy to regress (`dimensions`) | Good, precise regression guard for the one historically fragile field; does not assert full parity with `cli._idx_kwargs()` (e.g., no shared test comparing both dicts key-for-key) |
| `safe_error()` / `UserFacingError` | 1 (`tests/test_web_state.py::test_expected_operational_error_is_safe_and_useful` line 62-64) | None | Narrow — only exercises the `UserFacingError` passthrough branch | The empty-message fallback and the sensitive-substring redaction branch (web_app.py:34-39) have **no test coverage at all** — a regression in the denylist (e.g., a typo in one of the six substrings, or a change in casing logic) would not be caught by any existing test |
| `WebApplication` facade (`dashboard`, `jobs`, `job`, `events`, `db`, `cfg`) | 0 dedicated unit tests | Indirect, via `tests/test_web.py`'s route tests (every page render implicitly calls `dashboard()`/`jobs()`/`job()`) | Adequate indirect coverage for the happy path; no test targets `WebApplication` in isolation (e.g., verifying `db.close()` is always called even when the wrapped `StateDB` call raises) | Acceptable given the class is a thin, low-risk pass-through, but the `finally: db.close()` guarantee itself is untested directly |

**Overall assessment**: The two structural invariants this component exists to guarantee — single-mutating-job mutual exclusion and crash recovery — are directly and well tested (`tests/test_web_state.py`). The orchestration-heavy `"run_all"` branch has a strong, explicit ordering/contract test. However, the individual `_dispatch` branches for `harvest`, `extract`, `review`, `connect`, and `garden` have **no unit test that actually executes `_dispatch` and inspects its behavior** — the route-level tests in `tests/test_web.py` largely monkeypatch `service.submit()` itself specifically to avoid running the real dispatch logic, meaning several of the confirmed business rules documented in §3 (the harvest idempotency fallback, the review confidence-bypass, the `reset_mismatched` gap) are exercised only by manual code reading, not by any automated test in the repository.

---

**Component analyzed**: `web_app` (`zettel/web_app.py`)

**Report saved to**: `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-web_app-2026-08-30_10-22-26.md`
