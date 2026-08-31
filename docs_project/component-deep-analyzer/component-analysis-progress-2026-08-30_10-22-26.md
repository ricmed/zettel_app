# Component Deep Analysis Report — `progress`

## 1. Executive Summary

`zettel/progress.py` is a 37-line, dependency-free shared-infrastructure module that defines the **only presentation-neutral contract** through which the pipeline core (`harvester`, `extractor`, `connector`, `gardener`, `gardener_hub`, `assets`) may report checkpoint-level progress to a caller, without knowing or caring whether that caller is a terminal, a web request, or nothing at all.

It has exactly two members:

- `ProgressObserver` — a `typing.Protocol` with a single method, `update(phase, message, *, current_item, current_index, total_items)`.
- `report(observer, phase, message, *, current_item, current_index, total_items)` — a free function that forwards to `observer.update(...)` when `observer` is not `None`, and is a silent no-op otherwise.

The component itself contains **no business logic, no state, no I/O, and no error handling** — its value is entirely as an architectural seam (a dependency-inversion point). The actual behavior worth documenting lives at its two ends:

- **The producer side**: six pipeline functions (`run_harvest`, `run_extract`, `run_connect`, `run_garden`, `run_garden_hubs`, `describe_pending_assets`) that accept an untyped `observer=None` keyword-only parameter and call `report(...)` at well-defined checkpoints.
- **The consumer side**: `zettel/web_app.py`'s `JobProgress` class, the single concrete implementation of the protocol in the codebase, which persists every checkpoint into SQLite (`web_jobs` + `web_job_events`) so the FastAPI web UI can poll/stream job progress to a browser.

Confirmed architecturally: `ProgressObserver` has **afferent coupling = 6** (the six pipeline modules that `from zettel.progress import report`) and **efferent coupling = 0** (it imports nothing beyond `typing.Protocol`), matching the counts already recorded in the sibling architectural report (`docs_project/architectural-analyzer/architectural-report-2026-08-30_10-22-26.md:101`).

Key findings:

- The CLI (`cli.py`) **never** passes an `observer` — every `run_harvest`/`run_extract`/`run_connect`/`run_garden` call site in `cli.py` omits the keyword, so `report()` is always a no-op there; the CLI's user feedback comes entirely from independent `rich.console.status()` spinners and `rich.progress.Progress` bars that coexist with (but are architecturally unrelated to) this component.
- `JobProgress` (the web implementation) satisfies `ProgressObserver` purely by **structural typing** — it never imports `zettel.progress` or declares the `Protocol` as a base class.
- No call site in the six producer modules type-annotates the `observer` parameter as `ProgressObserver | None` — the only place that type appears in the entire codebase is inside `progress.py` itself.
- Two of the seven web-exposed operations (`review`, `sync`) do not route through `report()` at all: `review`'s per-chunk progress is emitted manually by `web_app.py` itself (bypassing the shared helper), and `sync` emits exactly one coarse, un-indexed checkpoint.
- Two operations (`retry_chunks`, `retry_assets`) emit **no** intra-operation progress whatsoever; their web job always renders as "Progresso indeterminado" until completion.
- There is no dedicated test file for `progress.py`; it is exercised only indirectly through `tests/test_web_state.py` (a hand-rolled fake observer) and end-to-end through `tests/test_web.py`.

## 2. Data Flow Analysis

Two independent data flows exist depending on the caller. `progress.py` sits at the fork.

**Flow A — CLI (observer is always `None`, `report()` is a no-op):**
```
1. User runs `zettel harvest` / `extract` / `connect` / `garden` (cli.py)
2. cli.py calls run_harvest()/run_extract()/... WITHOUT an `observer=` kwarg
3. Inside the pipeline function, report(None, phase, message, ...) is called
4. report() sees observer is None and returns immediately — no-op
5. User-visible feedback instead comes from rich.console.status()/rich.progress.Progress,
   called directly inside cli.py / harvester.py / extractor.py / connector.py,
   entirely independent of the ProgressObserver contract
```

**Flow B — Web UI (observer is a live `JobProgress`):**
```
1. Browser POSTs to a /pipeline/* route (web.py) → WebApplication.submit() enqueues
   a row in StateDB.web_jobs (state='queued')
2. WebWorker._execute() claims the job, builds progress = JobProgress(db, job_id)
3. WebWorker._dispatch() calls the matching pipeline entry point
   (run_harvest/run_extract/run_connect/run_garden/run_garden_hubs/describe_pending_assets),
   passing observer=progress
4. Inside the pipeline function, at each checkpoint:
     from zettel.progress import report
     report(observer, phase, message, current_item=..., current_index=..., total_items=...)
5. report() sees observer is not None → calls observer.update(phase, message, ...)
6. JobProgress.update() wraps the args into a ProgressEvent and calls self.emit(event)
7. JobProgress.emit() performs TWO SQLite writes:
     a. StateDB.update_web_job()   — overwrites the job's current phase/message/
        current_item/current_index/total_items (COALESCE — last-write-wins per field)
     b. StateDB.add_web_job_event() — INSERTs an immutable audit-log row into
        web_job_events (never overwritten)
8. Browser polls GET /api/jobs/{job_id}?after=N every 1s (job_detail.html's `refresh()`),
   or streams GET /api/jobs/{job_id}/events (Server-Sent Events, web.py:570-586)
9. web.py reads via WebApplication.job()/events() → StateDB.get_web_job()/list_web_job_events()
10. job_detail.html renders job.phase/job.message as text and computes
    (current_index / total_items * 100)% for the progress bar; falls back to an
    "indeterminate" animated bar when either field is falsy
```

Both flows can be active on the same pipeline call in principle (the `report()` call is unconditional in the producer code); in practice the CLI path never supplies an observer, so only one flow is ever live per invocation.

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Contract | `ProgressObserver.update()` must accept `phase: str`, `message: str`, and three keyword-only optional fields (`current_item`, `current_index`, `total_items`) | zettel/progress.py:8-17 |
| Null-safety | `report()` is a guaranteed no-op when `observer is None`; producers never need to null-check before calling it | zettel/progress.py:30-31 |
| Convention | `observer` is a keyword-only parameter, always defaulting to `None`, on every pipeline entry point that supports progress | harvester.py:67, extractor.py:62, connector.py:101, gardener.py:61, gardener_hub.py:184, assets.py:332 |
| Convention | Every producer imports `report` lazily, inside the function body, never at module scope | harvester.py:120, extractor.py:89, connector.py:118, gardener.py:86, gardener_hub.py:200, assets.py:367 |
| Convention | `phase` is a fixed, lowercase, snake_case token equal to (or derived from) the operation name; it doubles as the UI's "current stage" label | harvester.py:121 ("harvest"), extractor.py:90 ("extract"), connector.py:119 ("connect"), gardener.py:87 ("garden"), gardener_hub.py:201 ("garden_hubs"), assets.py:373 ("assets") |
| Checkpoint cadence | Each producer emits exactly one "totals announced" checkpoint before its loop, then one checkpoint per loop iteration (1-based `current_index`) | harvester.py:121+126, extractor.py:90+107, connector.py:119+133, gardener.py:87+154, gardener_hub.py:201+236, assets.py:373 |
| Persistence | Every `JobProgress.update()` call performs two durable writes: a mutable "latest state" row (`web_jobs`) and an immutable append-only audit row (`web_job_events`) | web_app.py:59-75 |
| Concurrency | At most one web job may be `queued`/`running` at a time (enforced upstream of progress, in `StateDB.create_web_job`); progress checkpoints are therefore always single-writer per `state.db` | state.py:1529-1550 |
| Presentation gap | The CLI never supplies an `observer`; all CLI progress feedback is via independent Rich widgets, not this contract | cli.py (no `observer=` kwarg at any `run_harvest`/`run_extract`/`run_connect`/`run_garden` call site) |
| Coverage gap | `review.py`'s pipeline functions accept no `observer` parameter at all; the web worker fabricates per-chunk `ProgressEvent`s manually instead of the pipeline module doing so | web_app.py:318-322 vs. review.py (no observer/report usage) |
| Coverage gap | `sync` emits one coarse, un-indexed checkpoint with no `total_items`/`current_index` | web_app.py:349-352 |
| Coverage gap | `retry_chunks` and `retry_assets` emit no per-item progress at all beyond the generic "loading dependencies" message | web_app.py:220-226 |
| UI derivation | The percentage bar is only rendered when **both** `total_items` and `current_index` are truthy; otherwise the UI shows an "indeterminate" animated state | templates/job_detail.html:2,5 |

### Detailed breakdown of the business rules

---

### Business Rule: Optional-observer / silent no-op contract

**Overview**:
`report(observer, phase, message, ...)` is the sole entry point producers use; it guarantees that calling it is always safe regardless of whether a progress sink exists.

**Detailed description**:
The function's entire body is a single `if observer is not None:` guard around a call to `observer.update(...)` (zettel/progress.py:30-37). This means every one of the six producer functions can call `report()` unconditionally, at every checkpoint, without an `if observer:` check of its own — the null-safety is centralized in one place instead of being duplicated six times. This is the textbook "Null Object"-adjacent pattern, except implemented as a guard clause rather than an actual null object instance.

The practical consequence is that the CLI path (where `observer` is always `None`) and the web path (where `observer` is a live `JobProgress`) execute the exact same production code paths in `harvester.py`/`extractor.py`/etc., with the only difference being whether `report()`'s internal branch is taken. This is what allows the same `run_harvest()` function to serve both a synchronous CLI invocation and an asynchronous, checkpointed web job without any `if is_web` branching inside the pipeline logic itself.

Because the guard is `is not None` rather than a truthiness check or a try/except, passing any object that does not implement `.update(...)` with the exact keyword-only signature would raise a `TypeError` at the first `report()` call inside the pipeline function, not inside `progress.py`. There is no defensive `hasattr`/`try` around the `observer.update(...)` call, so a malformed observer surfaces as an uncaught exception deep inside whichever pipeline stage first calls `report()`.

**Rule workflow**:
```
report(observer, phase, message, current_item=None, current_index=None, total_items=None)
  → if observer is None: return (no-op, no exception, no log)
  → else: observer.update(phase, message, current_item=..., current_index=..., total_items=...)
       → if observer does not implement update() with this signature → TypeError propagates
         out of report() into the calling pipeline function (uncaught here)
```

---

### Business Rule: Structural (duck-typed) protocol conformance

**Overview**:
`ProgressObserver` is a `typing.Protocol`, and its single real-world implementation (`JobProgress` in `web_app.py`) conforms to it purely by having a compatible `update()` method — it never imports `zettel.progress` or subclasses `ProgressObserver`.

**Detailed description**:
`class ProgressObserver(Protocol): def update(self, phase, message, *, current_item=None, current_index=None, total_items=None) -> None: ...` (progress.py:8-17) declares the shape of a valid observer. `JobProgress.update()` (web_app.py:77-92) has an identical method signature but is a fully independent class with no import of, or reference to, `progress.py`. This is legal and idiomatic under PEP 544 structural subtyping — a runtime `isinstance(x, ProgressObserver)` check would even succeed for `JobProgress` instances if `ProgressObserver` were declared `@runtime_checkable` (it is not, so no such check is actually performed anywhere in the codebase).

This design decision means the two modules (`progress.py` and `web_app.py`) have zero import-time coupling to each other. `web_app.py` is never required to import `progress.py`, and `progress.py` has no awareness that `JobProgress` exists. The cost of this decoupling is that nothing in the codebase — not a type checker run, not a runtime assertion — actually verifies that `JobProgress` continues to satisfy the protocol after a refactor; conformance is enforced only by convention and by the fact that `web_app.py`'s own tests (`tests/test_web_state.py`) exercise `JobProgress` end-to-end through the pipeline dispatch path.

At every one of the six producer call sites, the `observer` parameter itself is declared with no type hint at all (e.g. `observer=None` in harvester.py:67, connector.py:101, gardener.py:61) rather than `observer: ProgressObserver | None = None`. So even static analysis on the producer side cannot catch a caller passing an incompatible object — the only enforcement point in the entire codebase where the `ProgressObserver` type is spelled out is `report()`'s own signature (progress.py:21).

**Rule workflow**:
```
Producer function declares: observer=None   (no type annotation)
   ↓ (web path)
WebWorker._dispatch() passes: observer=JobProgress(db, job_id)
   ↓
Producer calls: report(observer, phase, message, ...)
   ↓
report() type-checks only at the signature level (progress.py:21); no runtime
isinstance/protocol check occurs anywhere
   ↓
observer.update(...) is called — succeeds because JobProgress.update() happens
to match the expected keyword signature exactly
```

---

### Business Rule: Phase-naming and checkpoint-cadence convention

**Overview**:
Every producer follows the same two-step cadence — one "totals" checkpoint announcing the item count, then one checkpoint per loop iteration with a stable, lowercase `phase` token equal to the operation's short name.

**Detailed description**:
This is not enforced by any code in `progress.py` — it is a convention repeated independently across six modules, each importing `report` and calling it with a literal phase string that never changes within that module: `"harvest"` (harvester.py:121,126), `"extract"` (extractor.py:90,108), `"connect"` (connector.py:119,134), `"garden"` (gardener.py:87,155), `"garden_hubs"` (gardener_hub.py:201,237), `"assets"` (assets.py:373). Because `web_app.py` reuses these exact same tokens as the `operation` value for `run_all`'s stage announcements ("Fase 1/5", "Fase 2/5", ... at web_app.py:237,246,249,255,261), the phase string doubles as the label the UI's "current stage" text (`#job-phase` in job_detail.html) shows the user — a naming mismatch between a producer's phase string and the operation name used elsewhere in `web_app.py`/`web.py` routing would surface as a cosmetic inconsistency in the UI, not a functional error, since nothing validates the phase value against an enum.

The cadence itself — one pre-loop "N item(s) found" checkpoint with only `total_items` set, followed by N in-loop checkpoints each setting `current_item`, `current_index` (1-based via `enumerate(..., 1)`), and `total_items` again — is consistent across all six modules (compare harvester.py:121+126-129, extractor.py:90+107-110, connector.py:119+133-136, gardener.py:87+154-157, gardener_hub.py:201+236-239, assets.py:373-375 which folds both steps into a single per-item call since `describe_pending_assets` has no separate "totals" line before its loop... actually it does log the total via `total_images = len(pending)` but only emits `report()` inside the loop, at assets.py:372-375). This is a "confidence: medium — inferred from repeated pattern, not enforced" business rule: nothing prevents a future producer from using 0-based indices, emitting more than one pre-loop announcement, or skipping the totals line — the convention exists only because six independent authors (or edits) followed the same template.

**Rule workflow**:
```
FOR each producer (harvest/extract/connect/garden/garden_hubs/assets):
  1. total = len(collection)
  2. report(observer, PHASE, "<total> item(s) found", total_items=total)   # once
  3. FOR i, item in enumerate(collection, start=1):
       report(observer, PHASE, "<progress message>",
              current_item=<item-derived label>, current_index=i, total_items=total)
       <do the actual work for item>
```

---

### Business Rule: Dual durable persistence per checkpoint (web path only)

**Overview**:
On the web side, every single `report()`-triggered `update()` call results in two separate, purpose-different SQLite writes inside `JobProgress.emit()`.

**Detailed description**:
`JobProgress.emit(event)` (web_app.py:59-75) calls `self.db.update_web_job(...)` and then `self.db.add_web_job_event(...)` for every event, with no batching or debouncing. `update_web_job` (state.py:1581-1612) uses `COALESCE(?, column)` for every field, meaning a checkpoint that omits `current_item` (passes `None`) does **not** clear the previously stored value — the job row always reflects the most recent non-null value for each field, effectively giving the row "sticky" semantics per column rather than "reset to blank on every update." `add_web_job_event` (state.py:1614-1625), by contrast, is a plain `INSERT` with no COALESCE — every event is stored exactly as emitted, `None` fields and all, forming an immutable, ordered (`event_id AUTOINCREMENT`) history the SSE endpoint and the polling endpoint's `?after=N` cursor both rely on.

This dual-write means a caller who wants "the current state of a job" reads `web_jobs` (one row, cheap point lookup, `StateDB.get_web_job`), while a caller who wants "everything that happened since I last checked" reads `web_job_events` with an `event_id` cursor (`StateDB.list_web_job_events`, used by both `/api/jobs/{id}` and the SSE stream at `/api/jobs/{id}/events`). Both reads are exercised together in `web.py:560-567`'s combined `{"job": ..., "events": ...}` payload. Every checkpoint therefore costs two `INSERT`/`UPDATE` statements plus two `commit()` calls (state.py:1612,1625) — there is no transaction wrapping both writes together, so a crash between the two `commit()` calls could in principle leave `web_jobs` ahead of `web_job_events` by one checkpoint (a narrow window, not observed in tests, but structurally possible).

**Rule workflow**:
```
observer.update(phase, message, current_item, current_index, total_items)
  → JobProgress.update() builds a ProgressEvent (frozen dataclass, web_app.py:44-49)
  → JobProgress.emit(event):
       1. db.update_web_job(job_id, phase=.., current_item=.., current_index=..,
                             total_items=.., message=..)
          → SQL: SET col = COALESCE(?, col) for every field  → commit()
       2. db.add_web_job_event(job_id, phase, current_item=.., current_index=..,
                                total_items=.., message=..)
          → SQL: INSERT (unconditional, including NULLs) → commit()
```

---

### Business Rule: CLI/web presentation split — the contract is opt-in per caller

**Overview**:
`ProgressObserver` exists specifically to let one caller (the web worker) observe pipeline execution without forcing the other caller (the CLI) to adopt the same mechanism; the CLI's own progress feedback (`rich.console.status`, `rich.progress.Progress`) is a completely separate, parallel system.

**Detailed description**:
Grepping every `run_harvest`/`run_extract`/`run_connect`/`run_garden` call site in `cli.py` shows none of them pass `observer=`; they are called as `run_harvest(cfg, db, idx, interactive=..., ...)` with no progress wiring (cli.py:319,331,1283) or `run_extract(cfg, db, idx, auto_approve=auto_approve)` (cli.py:437, 1302), etc. Since these functions default `observer` to `None`, every `report()` call made from inside them during a CLI run is a guaranteed no-op per the rule above — the CLI genuinely never touches this component at runtime. Instead, the CLI wraps whole operations in `rich.console.status(...)` spinners (cli.py:154,436,678,726,732,822,1110,1117,1186,1225,1237,1374) and, inside `run_extract`/`run_connect` themselves, opens an independent `rich.progress.Progress(...)` context (extractor.py:94-101, connector.py:121-128) that renders a live terminal bar using its own loop-local `i`/`total` counters — duplicating the same iteration the `report()` calls are also observing, but through an entirely different rendering mechanism with no shared state.

This means the two feedback mechanisms (Rich terminal UI, and the `ProgressObserver` checkpoint stream) run **side by side inside the exact same loop** in `run_extract`/`run_connect` (compare extractor.py:106 `progress.update(task, ...)` immediately followed by extractor.py:107-110 `report(observer, ...)`), each independently deriving the same `i/total` progress fraction. Neither is aware of the other. For `run_harvest`, `run_garden`, `run_garden_hubs`, and `describe_pending_assets`, there is no local Rich progress bar at all inside the function — only `logger.info(...)` calls and the `report()` checkpoints — so under the CLI, the primary user feedback for those four operations is plain log lines, not a progress bar.

**Rule workflow**:
```
IF caller is CLI (cli.py):
    run_X(cfg, db, idx, ...)                # observer omitted → defaults to None
      → report(None, ...) is a no-op everywhere inside run_X
      → user sees: rich.console.status() spinner around the whole call (cli.py),
                    OR (extract/connect only) a live rich.progress.Progress bar
                    driven by the same loop, OR (harvest/garden/garden_hubs/assets)
                    plain logger.info() lines
IF caller is the web worker (web_app.py):
    run_X(cfg, db, idx, ..., observer=JobProgress(db, job_id))
      → report(observer, ...) persists every checkpoint to SQLite
      → browser polls/streams job_detail.html, which renders phase/message/% bar
```

---

### Business Rule: Not every web operation reports progress the same way

**Overview**:
Of the seven operations `WebWorker._dispatch` can run, only five (`harvest`, `extract`, `connect`, `garden`/`garden_hubs`, and the asset-description sub-step of `extract`) get fine-grained, per-item progress via `report()`; `review` bypasses the shared helper, `sync` reports once with no counters, and `retry_chunks`/`retry_assets` report nothing beyond the generic startup line.

**Detailed description**:
`review.py` — unlike `harvester.py`, `extractor.py`, `connector.py`, `gardener.py`, `gardener_hub.py`, and `assets.py` — has no `observer` parameter on any of its functions and never imports `zettel.progress`. To still give the web UI *some* visibility into review progress, `WebWorker._dispatch`'s `"review"` branch (web_app.py:304-335) constructs `ProgressEvent` objects directly and calls `progress.emit(event)` itself, inline in its own per-chunk loop (web_app.py:318-322), rather than delegating that responsibility to `review.py`. This means review's progress reporting logic is duplicated in the web layer instead of living next to the business logic it describes, and the interactive CLI review flow (`zettel review`, driven by `review.py`'s own HITL menu) has zero progress instrumentation of any kind — not even Rich — since it is a menu-driven interactive command, not a batch loop.

`"sync"` (web_app.py:349-352) emits exactly one `ProgressEvent("sync", "Sincronizando notas manuais.")` with no `total_items`/`current_index`/`current_item`, even though `run_sync_manual` internally processes every manually-created note in the vault — a batch operation that, unlike harvest/extract/connect/garden, gives the UI no way to show "N/M notes synced," only an indeterminate spinner for the operation's entire duration.

`"retry_chunks"` and `"retry_assets"` (web_app.py:220-226) return their result dict immediately after a single DB call, with no `report()`/`progress.emit()` call inside their branch at all — the only progress event either job ever receives is the generic `progress.emit(ProgressEvent(operation, f"Carregando dependências para {operation}."))` emitted unconditionally at the top of `_dispatch` (web_app.py:219) before the operation-specific branching. Their job detail page therefore always shows "Progresso indeterminado" until the job transitions to `succeeded`/`failed`.

**Rule workflow**:
```
_dispatch(operation, ...):
  progress.emit(ProgressEvent(operation, "Carregando dependências..."))   # always, all ops
  IF operation in {"retry_chunks", "retry_assets"}:
      return result   # no further progress ever emitted
  IF operation == "review":
      FOR each chunk_id:
          progress.emit(ProgressEvent("review", ..., current_index=i, total_items=N))
          # emitted directly by web_app.py, NOT via report()/an observer param on review.py
  IF operation == "sync":
      progress.emit(ProgressEvent("sync", "Sincronizando notas manuais."))  # once, no counters
      run_sync_manual(...)   # internal note-by-note work is invisible to progress
  IF operation in {"harvest","extract","connect","garden","run_all"}:
      run_X(..., observer=progress)   # fine-grained report() checkpoints from inside run_X
```

---

### Business Rule: UI percentage derivation depends on both counters being truthy

**Overview**:
The browser only renders a determinate percentage bar when a job row has both `total_items` and `current_index` set to a truthy value; otherwise it falls back to an "indeterminate" animated bar, in both the initial server-rendered template and the client-side JS poller.

**Detailed description**:
`templates/job_detail.html:2` computes the initial bar width with a Jinja conditional: `{% if job.total_items and job.current_index %}width:{{ (job.current_index / job.total_items * 100)|round }}%{% endif %}`. The client-side `refresh()` function (job_detail.html:5) repeats the same logic in JavaScript: `if(j.total_items&&j.current_index){...}else if(terminal){...}else{bar.classList.add("indeterminate");...}`. Both checks are plain truthiness tests, not `is not None` checks — in Python, `0` is falsy, and in JavaScript, `0` is also falsy. Every producer in this codebase happens to start its loop index at `1` via `enumerate(collection, 1)` (harvester.py:125, extractor.py:102, connector.py:129, gardener.py:153, gardener_hub.py:235, assets.py:369-370's `step = idx + 1`), so `current_index` is never actually `0` in practice — but this is an unenforced invariant of the producer convention, not something `progress.py`, `JobProgress`, or the template guards against structurally. A hypothetical future producer that reports 0-based progress (`current_index=0` for the first item) would silently render as "indeterminate" instead of "0%" for its entire first checkpoint, with no error or warning anywhere in the stack.

**Rule workflow**:
```
Server-render (job_detail.html, Jinja):
  IF job.total_items AND job.current_index (both truthy):
      bar width = round(current_index / total_items * 100) + "%"
  ELSE: bar has no explicit width (CSS "indeterminate" class expected via JS)

Client poll (job_detail.html, JS, every 1000ms):
  IF j.total_items AND j.current_index (both truthy):
      bar width = (current_index / total_items * 100) + "%"; remove "indeterminate"
  ELSE IF job state in {succeeded, failed, interrupted} (terminal):
      bar width = 100% if succeeded else 0%; remove "indeterminate"
  ELSE:
      add "indeterminate" class (animated, no numeric width)
```

## 4. Component Structure

`progress.py` is a single, self-contained file. Its functional "component" for analysis purposes also includes the one concrete implementation of its contract and the six call sites that constitute its entire producer-side surface.

```
zettel/
├── progress.py                 # THE COMPONENT: ProgressObserver Protocol + report() dispatcher (37 lines, 0 internal deps)
├── web_app.py                  # Concrete consumer/implementer: ProgressEvent (dataclass) + JobProgress (structural
│                                #   implementation of ProgressObserver) + WebWorker._dispatch (wires observer= into
│                                #   every pipeline call; lines 43-92 define the classes, 214-353 wire them up)
├── harvester.py                # Producer call site — phase "harvest" (run_harvest, lines 54-156)
├── extractor.py                # Producer call site — phase "extract" (run_extract, lines 56-154)
├── connector.py                # Producer call site — phase "connect" (run_connect, lines 100-~150)
├── gardener.py                 # Producer call site — phase "garden" (run_garden, lines 60-~165)
├── gardener_hub.py             # Producer call site — phase "garden_hubs" (run_garden_hubs, lines 183-~250)
├── assets.py                   # Producer call site — phase "assets" (describe_pending_assets, lines 332-~420)
├── web.py                      # Downstream HTTP surface that reads what progress.py's data produced
│                                #   (/jobs/{id}, /api/jobs/{id}, /api/jobs/{id}/events — lines 550-586)
├── state.py                    # Durable storage the web-side implementation writes to
│                                #   (web_jobs / web_job_events tables, lines 230-259; CRUD lines 1515-1631)
└── templates/
    └── job_detail.html         # Renders the persisted progress fields (phase/message/current_index/total_items)
```

No `__init__.py` re-export, no package boundary, and no configuration file references `progress` — it is a plain internal module imported by absolute path (`from zettel.progress import report`) wherever needed.

## 5. Dependency Analysis

```
Internal Dependencies:

zettel.progress  (imports only `typing.Protocol` — stdlib, no internal deps)
       ↑ (imported by, all six lazily/function-local)
       ├── zettel.harvester.run_harvest
       ├── zettel.extractor.run_extract
       ├── zettel.connector.run_connect
       ├── zettel.gardener.run_garden
       ├── zettel.gardener_hub.run_garden_hubs
       └── zettel.assets.describe_pending_assets

zettel.web_app.JobProgress   (structurally satisfies ProgressObserver; NO import of zettel.progress)
       ├── depends on → zettel.state.StateDB (update_web_job, add_web_job_event)
       └── is depended on by → zettel.web_app.WebWorker._dispatch (constructs JobProgress,
                                 passes it as `observer=` into the six producer functions above)

zettel.web.py (HTTP routes)
       └── depends on → zettel.web_app.WebApplication.job()/events()
                          → zettel.state.StateDB.get_web_job()/list_web_job_events()
                             (the durable output of every JobProgress.emit() call)

External Dependencies:
- Python `typing` (stdlib) — Protocol base class. No third-party packages.
- SQLite (bundled, via zettel.state.StateDB) — durable store for web_jobs/web_job_events
  (consumed only by the JobProgress implementation, not by progress.py itself).
- Jinja2 (via FastAPI/Starlette templating, zettel/templates/job_detail.html) — renders
  the persisted progress fields; not a dependency of progress.py but of its data's
  eventual consumer.
```

`progress.py` itself has **zero external dependencies** and **zero internal dependencies** beyond the standard library — it is a pure leaf module in the dependency graph.

## 6. Afferent and Efferent Coupling

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|-------------------|----------|
| `ProgressObserver` (Protocol, progress.py) | 6 (harvester, extractor, connector, gardener, gardener_hub, assets — all via `from zettel.progress import report`, which references the type only internally) | 0 (depends only on stdlib `typing`) | Medium — small blast radius per call site, but touches every pipeline stage |
| `report()` (function, progress.py) | 6 (same six modules, each calling `report(observer, ...)` 2+ times) | 1 (calls `observer.update(...)`, an unresolved structural type at call time) | Medium |
| `JobProgress` (class, web_app.py) | 1 (`WebWorker._dispatch`, which instantiates it once per job and threads it through every pipeline call) | 1 (`StateDB`, for `update_web_job`/`add_web_job_event`) | High — the only implementation; any regression here silently breaks all web-visible progress with no compile-time signal, since nothing type-checks it against `ProgressObserver` |
| `ProgressEvent` (frozen dataclass, web_app.py) | 2 (`JobProgress.emit`, and `WebWorker`'s direct construction for `review`/`sync`/milestone events) | 0 | Low |
| `WebWorker._dispatch` (web_app.py) | 1 (`WebWorker._execute`) | 8 (harvester, extractor, review, connector, gardener, gardener_hub, sync, index — each conditionally imported per operation) | Medium — high fan-out is inherent to being the pipeline orchestrator, not a coupling smell specific to progress |

Note on methodology: "Critical" reflects blast radius if the component's contract silently breaks (e.g., a signature drift between `ProgressObserver.update` and `JobProgress.update`), not runtime call frequency. Because conformance is structural and untyped at every call site (see Business Rule 2), `JobProgress` is flagged **High** despite its small direct coupling numbers — a mismatch would not be caught by any test or type checker except by *running* a web job and observing the `TypeError`.

## 7. Endpoints

Not applicable. `progress.py` and `JobProgress` expose no REST/GraphQL/gRPC surface of their own — they are an internal library contract. The HTTP endpoints that *consume* the data this component produces (`/jobs/{job_id}`, `/api/jobs/{job_id}`, `/api/jobs/{job_id}/events`) belong to `zettel/web.py`, a separate component, and are documented above (§2, §5) only as downstream context.

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| Pipeline producers → `report()` | Internal function call | Emit a progress checkpoint from inside a long-running pipeline stage | Direct Python call (in-process) | Positional/keyword args (`phase: str`, `message: str`, `current_item/current_index/total_items: optional`) | None — an incompatible `observer` raises an uncaught `TypeError` at the call site inside the pipeline function |
| `report()` → `ProgressObserver.update()` | Internal function call (structural interface) | Dispatch to whatever concrete observer was supplied | Direct Python call (in-process) | Same as above | None — no try/except around `observer.update(...)`; a raising observer aborts the entire pipeline stage |
| `JobProgress` → `StateDB` (SQLite) | Internal persistence call | Durably record the job's latest state and an append-only event log | Synchronous SQLite via `sqlite3` (WAL mode, per CLAUDE.md) | SQL rows (`web_jobs`, `web_job_events` tables) | Each write is a single autocommitted statement (`self.conn.commit()`); no retry/backoff; no transaction spanning both writes, so a crash between the two commits can leave them one checkpoint apart |
| Browser polling → `web.py` → `WebApplication.job()/events()` → `StateDB` | Downstream HTTP consumer | Let the browser render live progress | HTTP GET, JSON (`/api/jobs/{id}`) | JSON (`{"job": {...}, "events": [...]}`) | `web.py` returns 404/401 JSON envelopes for missing job / unauthenticated request; no progress-specific error handling |
| Browser SSE → `web.py` `/api/jobs/{id}/events` | Downstream HTTP consumer | Push-style progress updates without polling | Server-Sent Events (`text/event-stream`) | `data: <json event>\n\n` | Stream self-terminates after 20 iterations or when job reaches a terminal state (web.py:576-586); no reconnect/backoff logic in the endpoint itself |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Protocol / Structural typing (PEP 544) | `ProgressObserver(Protocol)` | progress.py:8-17 | Decouple pipeline producers from any concrete progress sink; allows `JobProgress` to conform without inheriting or importing |
| Guard-clause "no-op on absent collaborator" | `report()`'s `if observer is not None:` | progress.py:30-31 | Centralize null-safety so producers never need their own `if observer:` checks |
| Dependency Inversion | Pipeline functions depend on the `ProgressObserver` abstraction (informally, since untyped) rather than on `web_app.JobProgress` or `rich.Progress` directly | harvester.py, extractor.py, connector.py, gardener.py, gardener_hub.py, assets.py (all via `observer=None` parameter) | Same pipeline code serves both CLI (no-op) and web (persisted) callers |
| Observer pattern | `ProgressObserver.update()` is the "notify" half of a classic Observer; `report()` is the "subject-side" trigger | progress.py (whole file) | Standard progress-reporting/telemetry decoupling |
| Adapter (event → durable row/log) | `JobProgress.emit()` translates a single `update()` call into two different persistence shapes (mutable snapshot + append-only log) | web_app.py:59-75 | Serve both "what's the current state" and "what's the history" queries from one event stream |
| Lazy/deferred import | `from zettel.progress import report` placed inside each producer function body, never at module top | harvester.py:120, extractor.py:89, connector.py:118, gardener.py:86, gardener_hub.py:200, assets.py:367 | Consistent codebase-wide convention (also used for `rich.progress`, `zettel.usage`, etc.) to minimize module-load-time import cost and avoid speculative circular imports |
| Immutable event record | `ProgressEvent` is a `@dataclass(frozen=True)` | web_app.py:43-49 | Guarantees a checkpoint's data cannot be mutated after construction, matching the append-only semantics of `web_job_events` |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|----------------|-------|--------|
| Medium | `progress.py` / all call sites | `observer` parameter is never type-annotated as `ProgressObserver \| None` at any of the six producer call sites (only `report()`'s own signature spells out the type) | A refactor that changes `ProgressObserver.update()`'s signature would not be caught by any producer's type checking — only by running the web path and hitting a runtime `TypeError` |
| Medium | `web_app.py` `JobProgress` | Conforms to `ProgressObserver` purely structurally, with no `@runtime_checkable` marker and no explicit inheritance or assertion anywhere in the codebase | Silent drift between the two classes' `update()` signatures is possible; nothing short of running a web job would surface it |
| Medium | `web_app.py` `review` branch | Review progress is emitted by hand inside `WebWorker._dispatch` (web_app.py:318-322) instead of `review.py` accepting an `observer` parameter like its five sibling pipeline modules | Progress-reporting logic for review is duplicated/misplaced outside the module that owns the business logic; a change to review's internal loop structure could silently desync from the progress events describing it |
| Low | `web_app.py` `sync` branch | `sync` emits a single checkpoint with no `total_items`/`current_index`, even though `run_sync_manual` processes a variable number of vault notes | Users see an indeterminate spinner for the entire sync duration regardless of vault size |
| Low | `web_app.py` `retry_chunks`/`retry_assets` branches | No progress events at all beyond the generic "loading dependencies" line | Same UX gap as above; likely acceptable given these are typically fast, bulk status-flip operations, but inconsistent with the rest of the operation catalog |
| Low | `templates/job_detail.html` | Both the server-rendered and client-polled percentage logic use truthiness (`current_index &&`) rather than `is not None`/`!= null`, so a hypothetical 0-based `current_index` would render as "indeterminate" instead of 0% | Currently latent — every producer happens to start indices at 1 — but the invariant is unenforced anywhere in code, comments, or tests |
| Low | `progress.py` / `JobProgress.emit` | The two SQLite writes per checkpoint (`update_web_job`, `add_web_job_event`) are not wrapped in a single transaction; each calls its own `commit()` | A crash between the two commits could leave the "current state" row and the "event history" one checkpoint out of sync; narrow window, no evidence found of it mattering in practice |
| Low | `progress.py` | No logging/telemetry inside `report()` or `ProgressObserver` itself when an observer's `update()` raises | An observer bug surfaces as an unqualified exception traceback inside whichever pipeline stage happens to call `report()` next, with no indication the failure originated in the progress layer |

## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|--------------------|----------|---------------|
| `zettel/progress.py` (`ProgressObserver`, `report()`) | 0 dedicated | 0 dedicated | None directly — no `tests/test_progress.py` exists in the repository | Not directly tested; behavior is exercised only transitively (see below). The no-op-when-`None` branch and the observer-dispatch branch of `report()` have no assertion anywhere that isolates `progress.py`'s own logic |
| `zettel/web_app.py` `JobProgress`/`ProgressEvent` | 1 indirect (`test_run_all_dispatches_every_phase_in_order`, `tests/test_web_state.py:72-136`, uses a hand-rolled fake `Progress` class with an `emit(event)` method, monkeypatching every pipeline module) | 1 (`tests/test_web.py::test_navigation_and_retry_job_flow`, `tests/test_web.py:101-134`, drives a real job end-to-end through `retry_assets` and asserts `/api/jobs/{id}` returns non-empty `events` and the `/jobs/{id}` HTML page renders state) | Partial — covers phase-ordering for `run_all` and the persistence/retrieval round-trip for one simple operation (`retry_assets`, which as noted above emits no intra-operation progress); does not cover `harvest`/`extract`/`connect`/`garden` producing real `current_item`/`current_index`/`total_items` values through the full stack | The `run_all` test is a solid ordering/contract check (asserts `progress.phases == ["run_all","harvest","extract","review","connect","garden"]`) but stubs out every producer, so it never exercises real `report()` calls from inside `harvester.py`/`extractor.py`/etc. The end-to-end web test only exhausts the "indeterminate" (no total_items) code path, not the percentage-bar path |
| `zettel/state.py` `web_jobs`/`web_job_events` persistence | 3 (`tests/test_web_state.py`: `test_web_queue_enforces_mutual_exclusion_and_transitions` lines 10-26, `test_recovery_interrupts_running_but_keeps_queued` lines 29-40, `test_progress_events_and_dashboard_are_persisted` lines 43-59) | Covered transitively by the `test_web.py` job-flow test above | Good for the storage layer itself (job lifecycle, event insertion/listing, mutual-exclusion, crash-recovery) | Solid, direct assertions on `current_index`/`total_items` round-tripping (`test_progress_events_and_dashboard_are_persisted`, `tests/test_web_state.py:43-59`) — but this validates `StateDB`, not `progress.py`'s contract itself |
| Producer call sites (`harvester.py`, `extractor.py`, `connector.py`, `gardener.py`, `gardener_hub.py`, `assets.py`) `report()` calls | 0 dedicated to progress | 0 dedicated to progress | None found — a search of `tests/` for `ProgressObserver`, `from zettel.progress`, or `from zettel import progress` returned no matches | These modules' existing test files (e.g. `tests/test_harvester*.py`, `tests/test_extractor*.py`, if present) were not found to assert anything about the `observer`/`report()` checkpoints emitted during their execution — a real gap, since these are the actual call sites where `phase`/`current_item`/`current_index`/`total_items` values are constructed |

**Overall assessment**: The component's *storage* and *orchestration* surroundings (`StateDB`'s web-job tables, `WebWorker._dispatch`'s phase sequencing) are reasonably well tested. The component itself — the `ProgressObserver` Protocol and the `report()` dispatcher in `progress.py` — has **no dedicated unit test** anywhere in `tests/`, and none of the six producer modules assert anything about the specific `phase`/`current_item`/`current_index`/`total_items` values they construct when calling `report()`. This is a real coverage gap: a change to a phase string, a checkpoint's cadence, or an argument name in any producer would not fail any existing test unless it also happened to break `test_run_all_dispatches_every_phase_in_order`'s phase-ordering assertion (which only checks the top-level `phases` list, not per-call arguments).

---

**Component analyzed**: `progress` (`zettel/progress.py`)

**Report saved to**: `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-progress-2026-08-30_10-22-26.md`
