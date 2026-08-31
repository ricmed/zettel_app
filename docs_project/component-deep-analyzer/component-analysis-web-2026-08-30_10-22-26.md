# Component Deep Analysis Report — `web`

## 1. Executive Summary

The `web` component is the HTTP presentation layer for the Zettelkasten pipeline, implemented as a single FastAPI application in `zettel/web.py` (622 lines), entry point `uvicorn zettel.web:app`. It is deliberately thin: it owns routing, HTML templating (Jinja2), authentication/CSRF, upload validation, and pre-flight business gating for pipeline operations, while delegating every state-mutating pipeline call (harvest, extract, review, connect, garden, sync, retry) to `zettel/web_app.py`'s `WebApplication`/`WebWorker` facade — a durable, SQLite-backed single-worker job queue that is a separate, already-analyzed component. `web.py` never runs pipeline logic inline and never blocks a request thread on a long operation; every mutation is queued as a background job and the browser polls/streams progress.

Key findings:
- **Security model is cookie+HMAC, not a framework.** There is no session store, no third-party auth library. A single shared instance secret (`SESSION_SECRET` env var) gates login; sessions are self-contained signed tokens (`hmac.compare_digest` throughout to avoid timing attacks). Without `SESSION_SECRET` set, the login page renders a hard "not configured" message and no session can ever be issued (`zettel/web.py:211-213`).
- **CSRF is defense-in-depth on top of the session**, not a separate secret store: the CSRF token is generated once at login (`secrets.token_urlsafe(24)`) and embedded in the signed session payload; every mutating form must echo it back in a hidden field and it is compared with `hmac.compare_digest` (`zettel/web.py:63-65`).
- **Every mutating route re-validates state server-side** before it will even enqueue a job — pipeline prerequisite checks (chunks pending, approved concepts, notes existing, LLM credential present) are duplicated in `web.py` (for a fast, specific HTTP error) in addition to whatever `web_app.py`/domain modules do internally.
- **The component intentionally exposes a curated subset of the CLI** — `harvest`, `extract`, `review` (batch only), `connect`, `garden` (+hubs), `sync`, `retry_chunks`, `retry_assets`, and a composite `run_all`. Interactive duplicate resolution, `purge-rejected`, `reindex`, `set-paging`, `rechunk`, dumps, `doctor`, `new-note`, `delete-source`, `ask`, and `article` are CLI-only by design (documented both in `CLAUDE.md` and reflected in the route table below).
- **XSS defense for user/LLM-generated Markdown** is centralized in `zettel/markdown.py` (used only by this component's `note_detail`/`moc_detail` routes): raw HTML is disabled in the Markdown parser and a second `bleach.clean()` pass with an explicit tag/attribute/protocol allowlist runs on the rendered HTML, plus a custom inline rule that rewrites internal `[[ZTL - id - label]]` wikilinks into safe `/notes/{id}` anchors.
- Test coverage is solid at the HTTP-integration level (`tests/test_web.py`, 10 tests via `TestClient`) covering auth/CSRF, upload validation, navigation, job polling, prerequisite gating, path-traversal-safe harvest selection, and Markdown sanitization. There is **no test that exercises the SSE endpoint** (`/api/jobs/{job_id}/events`) and no isolated unit tests of the auth/session primitives (`_sign`, `_session`, `_session_value`, `_llm_ready`) outside of what the integration tests happen to cover indirectly.

## 2. Data Flow Analysis

Two representative flows — a page render and a mutating job submission:

**A. Authenticated page render (e.g. `GET /documents`)**
```
1. Request enters via a FastAPI path function (e.g. documents())
2. _auth(request) checks _session(request): verifies HMAC signature on the
   zettel_session cookie and its 24h age window; unauthenticated -> 303 to /login
3. _service(request) fetches the shared WebApplication from app.state.service
4. Handler opens a StateDB via service.db(), queries (e.g. list_sources(),
   _list_pending_inbox()), and closes the DB in a finally block
5. _render() builds the Jinja2 context via _context() (adds `authenticated`,
   `csrf` token from the session) and returns a TemplateResponse
6. Jinja2 template (documents.html) extends base.html, renders KPIs/tables/forms
7. Browser receives fully-rendered HTML; no client-side framework
```

**B. Mutating job submission (e.g. `POST /pipeline/extract`)**
```
1. Request enters pipeline_action(operation="extract")
2. Route-level allowlist check: operation must be one of the 7 known pipeline ops
3. _auth(request) gate -> redirect to /login if no session
4. _csrf_ok(request, csrf) gate -> 403 "CSRF inválido" if token doesn't match session
5. A StateDB is opened read-only to fetch db.get_stats() and evaluate
   operation-specific prerequisites (chunks_pending, approved concepts, notes,
   chunks_failed, LLM credential) -> 409 with a specific PT-BR message if unmet
6. _post_job() re-checks auth/CSRF, then calls service.submit(operation, payload)
7. WebApplication.submit() delegates to WebWorker.submit(), which does an
   INSERT-guarded-by-uniqueness in SQLite (web_jobs) enforcing "only one
   queued/running mutating job at a time"; returns None on conflict
8. On success: 303 redirect to /jobs/{job_id}; on conflict: 409 "Outra operação
   mutante já está em andamento" rendered via jobs.html
9. The background WebWorker thread (in web_app.py, a separate component) later
   claims and executes the job, writing progress/events/result to SQLite
10. The browser at /jobs/{job_id} polls GET /api/jobs/{job_id}?after=N every 1s
    via inline JS (job_detail.html) until the job reaches a terminal state
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Security | No session can be issued without `SESSION_SECRET` set | `zettel/web.py:31-32,211-213` |
| Security | Session cookie is HMAC-SHA256 signed and expires after 86400s (24h) | `zettel/web.py:39-60` |
| Security | Login credential compared with `hmac.compare_digest`, never `==` | `zettel/web.py:226` |
| Security | CSRF token bound to session payload; every mutating POST validates it via `hmac.compare_digest` | `zettel/web.py:63-65` |
| Security | Login form itself is protected by a separate, secret-derived `login_csrf` token (valid before any session exists) | `zettel/web.py:216,220-225` |
| Security | Session cookie `Secure` flag derived from `X-Forwarded-Proto` (reverse-proxy aware) | `zettel/web.py:230-233` |
| Validation | Upload filename allowlist regex + extension allowlist + traversal/collision checks | `zettel/web.py:27,278-306` |
| Validation | Upload size bounded to 25 MB, empty file rejected | `zettel/web.py:26,289-295` |
| Validation | Harvest file selection rejects absolute paths and `..` traversal, must resolve inside inbox | `zettel/web.py:331-342` |
| Business Logic | A file is "needs harvest" if unseen, source incomplete, chunking incomplete, or checksum changed | `zettel/web.py:168-184` |
| Business Logic | Already-completed, unchanged documents are hidden from harvest selection and reject a forced re-harvest (409) | `zettel/web.py:187-201,343-351` |
| Business Logic | `duplicate_action` normalized to one of `skip`/`continue`/`abort`, defaulting to `skip` | `zettel/web.py:353-354` |
| Business Logic | `run_all` requires an LLM credential before it will be queued | `zettel/web.py:369-374` |
| Business Logic | Per-operation pipeline prerequisite gating (extract/connect/garden/garden_hubs/retry_chunks + LLM readiness) | `zettel/web.py:401-436` |
| Business Logic | Only one mutating job (`queued`/`running`) may exist at a time; second submit returns 409 | `zettel/web.py:310-319` (enforced in `web_app.py`) |
| Business Logic | Review chunks bucketed into confidence bands (`low` < 0.4, `medium` < configured threshold, `high` ≥ threshold) with pagination (20/page) | `zettel/web.py:439-469` |
| Business Logic | Review action restricted to `approve`/`reject`, applied as a batch job over selected `chunk_ids` | `zettel/web.py:472-477` |
| Business Logic | LLM readiness derived from provider-specific required env vars; unknown providers assumed ready | `zettel/web.py:156-165` |
| Rendering/Security | Note/MOC bodies rendered through sanitized Markdown (HTML disabled + `bleach` allowlist + protocol allowlist) before being sent to the browser | `zettel/markdown.py:59-73` (invoked at `zettel/web.py:522,539`) |
| Rendering | Internal `[[ZTL - id - label]]` wikilinks rewritten to safe in-app `/notes/{id}` links | `zettel/markdown.py:15-40` |
| Architecture | `create_app()` clones already-registered routes from the canonical module-level `app` into new `FastAPI` instances (except built-in OpenAPI/docs/static paths), enabling per-config app instances without re-declaring routes | `zettel/web.py:107-121` |
| Error handling | Unknown source/note/MOC detail IDs return 404 rather than leaking any existence signal or arbitrary file content | `zettel/web.py:503-504,518-519,535-536` |
| Streaming | SSE progress endpoint self-limits to 20 polling iterations (~10s) and stops early on terminal job state | `zettel/web.py:570-586` |

### Detailed breakdown of the business rules

---

### Business Rule: Session lifecycle and the `SESSION_SECRET` gate

**Overview**:
Authentication has no user database and no third-party identity provider — a single shared "instance secret" (`SESSION_SECRET`, read from the process environment, never from `config.yaml`) is the only credential. Anyone who knows it can log in as the single implicit operator of this Zettelkasten instance.

**Detailed description**:
`_secret()` reads `os.environ.get("SESSION_SECRET", "")` on every call — it is never cached, so rotating the environment variable and restarting the process (or, for a `.env`-loaded value, the process itself) immediately invalidates every previously issued cookie, because `_sign()` (HMAC-SHA256 keyed by the current secret) will no longer match. If `SESSION_SECRET` is unset or empty, `_secret()` returns `""`; every signing operation still executes but is trivially forgeable (`hmac.new(b"", ...)`), so the code takes the stronger step of never issuing a cookie at all: `_session()` returns `None` immediately when `_secret()` is falsy, and the `GET /login` handler renders an explicit "SESSION_SECRET não está configurado." message instead of a working login form. This means a misconfigured deployment fails safe (nobody can authenticate) rather than failing open (anyone can authenticate with a guessable empty secret).

A session cookie's value is `base64url(json({"csrf": ..., "created": ...})) + "." + HMAC-SHA256(that base64 body)`. `_session()` splits on the last `.`, verifies the signature with `hmac.compare_digest` (constant-time, avoiding a timing side-channel that could otherwise let an attacker binary-search a forged signature byte-by-byte), pads and decodes the base64 body, and re-checks `time.time() - created <= 86400` (24 hours). Any `ValueError`/`json.JSONDecodeError`/`UnicodeDecodeError` along that path (malformed cookie, non-UTF8 payload, non-JSON body) is caught and treated as "no session" rather than propagating a 500 — a cookie tampered with or corrupted by a browser extension degrades to a login redirect, not a crash.

The `Secure` cookie attribute is set dynamically from `request.headers.get("x-forwarded-proto", request.url.scheme) == "https"` at login time, which lets the same code run correctly behind a TLS-terminating reverse proxy (Replit, nginx, etc.) without hardcoding an assumption about the deployment topology — if the proxy identifies the original request as HTTPS via the standard forwarded-proto header, the cookie is marked `Secure` even though the app itself may be serving plain HTTP internally.

**Rule workflow**:
```
GET /login (no session) -> SESSION_SECRET unset? -> render login.html with
   error message, no usable form action
                        -> SESSION_SECRET set? -> render login form with a
   fresh login_csrf = _sign("login")
POST /login -> login_csrf must match _sign("login") (constant-time) else 403
            -> instance_secret must match SESSION_SECRET (constant-time) else
               401 + fresh login_csrf reissued
            -> success: set-cookie zettel_session = _session_value(new csrf),
               httponly, samesite=lax, secure=<proxy-aware>, max_age=86400
Any protected route -> _auth()/_session() re-validates signature + 24h TTL on
   every single request (stateless; no server-side session store to expire)
```

---

### Business Rule: CSRF protection bound to the session payload

**Overview**:
Every state-changing request (logout, upload, harvest, run-all, pipeline actions, review actions) requires a CSRF token that was minted specifically for the caller's current session and is compared in constant time.

**Detailed description**:
Rather than maintaining a separate CSRF token store, the token is generated once at successful login (`secrets.token_urlsafe(24)`) and folded directly into the signed session cookie payload (`_session_value(csrf)`). Because the cookie is HMAC-signed, the CSRF value embedded in it cannot be forged or altered without invalidating the whole cookie — this collapses "is this the right session" and "is this the right CSRF token" into a single verification. `_csrf_ok(request, token)` retrieves the current session (which re-validates the signature and TTL) and then does `hmac.compare_digest(session.get("csrf", ""), token)`; a missing session, a missing token, or a mismatched token all resolve to `False`.

Every mutating route follows the same two-gate pattern in order: `_auth(request)` first (unauthenticated → redirect to `/login`), then `_csrf_ok(request, csrf)` (bad/missing token → `403 "CSRF inválido"`). This ordering matters for information exposure: an unauthenticated caller is bounced before ever learning whether their CSRF token would have been valid. The templates place the CSRF token in a hidden form field (`<input type="hidden" name="csrf" value="{{ csrf }}">`) sourced from `_context()`, which pulls `session.get("csrf")` — so the token displayed in the page's own forms is always the one embedded in the browser's current cookie, and a cross-site form (which cannot read the victim's cookie or the page's rendered HTML) cannot supply a matching value.

The one route reachable before a session exists — `POST /login` — cannot use this mechanism (there is no session yet to bind a CSRF token to), so it uses a distinct, simpler token: `login_csrf = _sign("login")`, a fixed message signed with the instance secret. This is weaker (it's the same value for every visitor until the secret rotates) but sufficient for its purpose: it proves the POST originated from a page this server rendered moments earlier using the current secret, blocking a blind cross-site POST of the login form. It is not a substitute for the per-session CSRF token used everywhere else, and the two are never interchangeable — the login route explicitly checks `login_csrf`, not `csrf`.

**Rule workflow**:
```
Login success -> csrf = secrets.token_urlsafe(24) -> embedded in signed cookie
Every page render -> _context() exposes csrf = session["csrf"] to templates
Every mutating POST -> _auth() -> _csrf_ok(hmac.compare_digest(session.csrf,
   posted csrf)) -> 403 on mismatch, else proceed
POST /login specifically -> checked against _sign("login") instead, since no
   session/csrf exists pre-login
```

---

### Business Rule: Upload validation (`POST /documents/upload`)

**Overview**:
Files entering the pipeline's inbox are constrained to a safe filename shape, a small extension allowlist, a 25 MB size cap, and cannot escape the configured inbox directory or silently overwrite an existing file.

**Detailed description**:
The handler derives `name = Path(original_name).name` (strips any directory component the client may have sent) and then runs a conjunction of checks that must **all** pass: the name must be non-empty and not `.`/`..`; it must be byte-identical to what `Path(...).name` produced from the original filename (`name != original_name` catches a filename containing path separators that `Path.name` would have stripped, meaning the original was not a bare filename); it must not contain a literal `/` or `\`; its suffix (lower-cased) must be in `ALLOWED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}`; it must be at most 180 characters; and it must match `re.fullmatch(r"[\w .()\-]+", name, flags=re.UNICODE)` — word characters (letters/digits/underscore, Unicode-aware), spaces, dots, parentheses, and hyphens only, which is what specifically defeats HTML/script-injection attempts in a filename (the test suite explicitly checks `"<img onerror=x>.md"` is rejected). Any single failure produces a generic `400` with a PT-BR message rather than pinpointing which check failed, avoiding giving an attacker a validation oracle.

Content is read with `await file.read(MAX_UPLOAD_BYTES + 1)` — reading one byte past the limit lets the handler distinguish "exactly at the limit" from "over the limit" without needing to know the declared `Content-Length` up front (which can be absent or wrong for streamed/chunked uploads); an empty read is rejected as "arquivo vazio" and a read that produced more than `MAX_UPLOAD_BYTES` (25 MB) is rejected with `413`.

Path-traversal defense is applied twice: once implicitly (the filename-shape checks above already forbid `/`/`\` and `..`), and again explicitly via `destination.relative_to(cfg.inbox_path.resolve())` on the fully resolved destination path — if resolution ever produced a path outside the inbox, `relative_to` raises `ValueError` and the request is rejected with a generic 400. This second check is a structural belt-and-suspenders guard: even if a future change to the filename-shape regex introduced a gap, the final destination is still verified to be a descendant of the inbox directory before any bytes are written. Finally, `destination.exists()` is checked to prevent one uploaded file from silently overwriting another with the same name (`409`), and the inbox directory is created (`mkdir(parents=True, exist_ok=True)`) only after every validation has passed, so a rejected upload never has the side effect of creating the inbox directory.

**Rule workflow**:
```
Extract bare filename from client-supplied name
  -> reject if empty/"."/".."/mismatched-after-Path.name/contains slash
  -> reject if extension not in {.pdf,.md,.markdown,.txt}
  -> reject if length > 180 or fails [\w .()\-]+ regex        -> 400
Read up to MAX_UPLOAD_BYTES+1 bytes
  -> reject if empty                                          -> 400
  -> reject if > MAX_UPLOAD_BYTES                              -> 413
Resolve destination = inbox_path / name
  -> reject if destination not inside inbox_path (relative_to) -> 400
  -> reject if destination already exists                      -> 409
mkdir inbox_path if needed; write bytes; redirect to /documents (303)
```

---

### Business Rule: "Needs harvest" detection and re-harvest prevention

**Overview**:
The inbox listing hides files that have already been fully, successfully processed, while still surfacing files that are new, changed, or were left in an incomplete state by a prior interrupted run — and the harvest submission endpoint independently re-enforces the same rule to block a redundant harvest of an already-completed, byte-identical file.

**Detailed description**:
`_file_needs_harvest(db, file_path)` is the single predicate behind both the inbox listing (`_list_pending_inbox`) and the harvest submission guard. It looks up the file's tracked record by resolved absolute path; if there is no record, or the record has no `source_id` yet, the file is unconditionally "needs harvest" (never seen, or seen but not yet attached to a source). If a `source_id` exists, the linked source row is loaded and must have `processing_status == "completed"`; anything else (missing source, or a status like `pending`/`failed`/some in-progress marker) means the file still needs (re-)harvesting. Even a source marked `completed` is re-checked with `source_chunking_incomplete(db, source_id)` (imported lazily from `zettel.harvester` to avoid a module-level circular import) — a source can be "completed" at the source-record level while some of its chunks never finished being produced, and that combination must still surface as needing harvest. Only after all three structural checks pass does the function fall back to comparing the current on-disk SHA-256 (`file_sha256`) against the last recorded `file_checksum`; if the file changed since it was last processed, it needs harvest again. An `OSError` while hashing (file removed/permissions changed between listing and check) is treated conservatively as "needs harvest" rather than raising.

This predicate is applied twice with different consequences. In `_list_pending_inbox`, files failing the predicate (i.e., fully done and unchanged) are simply omitted from the dropdown of harvestable files — the operator never sees them as an option. In `POST /documents/harvest`, if an operator submits a `selected_file` value that resolves to a file the predicate says does *not* need harvest, the request is rejected outright with `409` and an explicit message ("Este documento já foi processado e não precisa de novo harvest.") rather than silently letting a redundant harvest job run. This second check exists because the dropdown is populated by a separate `GET /documents` request; state can change between page load and form submission (another operator finishes a harvest job in the meantime), so re-validating at submission time closes that race rather than trusting the client-supplied value from a possibly-stale page.

Critically, a file that *is* selectable specifically because it is incomplete (e.g., extraction succeeded but chunking did not finish) is still routed through the exact same `harvest` operation and payload shape as a brand-new file — there is no separate "resume" endpoint; `run_harvest` (in the harvester component) is expected to pick up wherever the prior attempt left off. `web.py`'s only job here is to decide whether a harvest attempt is warranted at all, not how it resumes.

**Rule workflow**:
```
For each inbox file with an allowed extension:
  no file record or no source_id                -> needs harvest
  else source missing or status != "completed"   -> needs harvest
  else source_chunking_incomplete(source_id)     -> needs harvest
  else current sha256 != recorded file_checksum  -> needs harvest
  else                                            -> hidden from pending list

POST /documents/harvest with selected_file:
  resolve + validate inside inbox, extension allowed, file exists  (else 400/404)
  re-run _file_needs_harvest -> if False, reject with 409 "já foi processado"
  else forward to WebApplication.submit("harvest", {selected_file: resolved
       absolute path, duplicate_action, skip_biblio, skip_paging,
       content_start_file, content_start_book})
```

---

### Business Rule: Pipeline prerequisite gating (`POST /pipeline/{operation}` and `POST /documents/run-all`)

**Overview**:
Before a pipeline phase job is even queued, `web.py` independently checks that the phase has something meaningful to do and, for LLM-backed phases, that a credential is configured — returning a specific `409` message instead of queuing a job that would immediately fail or do nothing.

**Detailed description**:
`pipeline_action` first restricts `operation` to a fixed allowlist (`extract`, `connect`, `garden`, `garden_hubs`, `sync`, `retry_chunks`, `retry_assets`); anything else is a `404` before auth is even checked, so unknown operation names don't leak whether a session is valid. After auth/CSRF, a read-only `StateDB` is opened purely to call `db.get_stats()` and `db.get_concepts_by_status(...)`, and the response depends entirely on domain counts rather than the caller's input: `extract` requires `stats["chunks_pending"]` to be truthy; `connect` requires at least one approved concept without a note yet; `garden`/`garden_hubs` require at least one permanent note to exist (there being nothing to cluster otherwise); `retry_chunks` requires at least one failed chunk. Additionally, `extract`, `connect`, `garden`, and `garden_hubs` all require `_llm_ready(cfg)` to be true, since all four make LLM calls. `sync` and `retry_assets` have no additional business gate beyond auth/CSRF — `sync` is always safe to run (it is idempotent over the vault) and `retry_assets` is a pure state reset. Every failed gate returns `HTMLResponse(..., status_code=409)` with a specific PT-BR sentence identifying exactly which prerequisite is missing, which the `pipeline.html` template also surfaces proactively (each phase's "Executar" button is rendered `disabled` client-side using the same `stats`/`llm_ready` values, so the 409 is a defense against a stale page or a directly-crafted request, not the primary UX).

`POST /documents/run-all` (the composite `run_all` operation covering harvest→extract→review→connect→garden in one background job) has a narrower, single gate: `_llm_ready(_service(request).cfg)`, because the harvest phase inside `run_all` might not need an LLM call at all (Docling/native extraction is not necessarily LLM-backed) but every later phase does, so the gate is conservative and blocks the whole composite job if the credential is missing. Unlike `pipeline_action`, `run_all`'s payload is **not** derived from the request body at all — it hardcodes `{"duplicate_action": "skip", "skip_biblio": False, "skip_paging": True}` — reflecting that this is explicitly the "safe, non-interactive" one-click path (per its own docstring), not a configurable one; an operator wanting fine-grained control over duplicate handling or paging must use the per-phase `/documents/harvest` form instead.

In both cases, the actual enqueue is funneled through the shared `_post_job()` helper, which re-checks auth/CSRF (defense in depth — by this point they were already checked once in the caller) and then calls `service.submit(operation, payload)`; a `None` return (meaning another mutating job is already `queued`/`running`) is surfaced as its own `409` with a distinct message ("Outra operação mutante já está em andamento"), independent of the domain-prerequisite `409`s above — the two `409` families share a status code but are mutually exclusive checks (concurrency vs. domain readiness).

**Rule workflow**:
```
POST /pipeline/{operation}
  operation not in allowlist                                -> 404 (pre-auth)
  not authenticated                                          -> redirect /login
  bad/missing csrf                                           -> 403
  extract   & !chunks_pending                                -> 409
  connect   & !approved-concepts-without-notes                -> 409
  garden(*) & !notes                                          -> 409
  retry_chunks & !chunks_failed                                -> 409
  operation in {extract,connect,garden,garden_hubs} & !llm_ready -> 409
  else -> _post_job(operation, {hubs: operation=="garden_hubs"})
             -> service.submit() returns None (job already running) -> 409
             -> else -> 303 redirect to /jobs/{job_id}

POST /documents/run-all
  not authenticated -> redirect /login; bad csrf -> 403
  !llm_ready -> 409
  else -> _post_job("run_all", fixed safe-defaults payload)
```

---

### Business Rule: Review confidence banding and batch approve/reject

**Overview**:
`GET /review` classifies each `awaiting_review` chunk into a low/medium/high confidence band relative to the configured auto-approve threshold, supports filtering by source and band, paginates at 20 items per page, and `POST /review/action` applies an approve/reject decision to an arbitrary batch of chunk IDs as a single background job — never auto-approving anything from the web UI itself.

**Detailed description**:
For every chunk currently `awaiting_review` (optionally pre-filtered by `source_id` at the SQL layer via `db.get_chunks_by_status`), the handler parses its stored `summary_json` (tolerating a malformed/missing value by falling back to `{}` rather than raising) to surface a human-readable `summary` and the LLM's candidate note theses/definitions for the reviewer to skim without opening the source. The confidence band boundaries are: `review_confidence < 0.4` → `low` (a literal constant, not configurable); `0.4 <= review_confidence < literature_review.auto_approve_min_confidence` (the same threshold the CLI's non-interactive `--auto-approve` and the `run_all` composite job's automatic review step use) → `medium`; `review_confidence >= threshold` → `high`. Because the band computation reuses the exact config value that governs auto-approval elsewhere in the pipeline, a "high" band in this UI specifically means "this is what would have been auto-approved if `--auto-approve`/`run_all` had processed it" — the web UI's manual review and the CLI/`run_all`'s automatic review share one threshold, so an operator reviewing a "medium" or "low" bucket is deliberately looking at exactly the drafts the automatic paths would have deferred to a human. A missing `review_confidence` is treated as `0` (lowest band) rather than excluded, so nothing silently disappears from every band filter. Filtering and pagination both happen in Python after the full `awaiting_review` set (for the source filter, if any) is loaded into memory and enriched — there is no SQL-level `LIMIT`/`OFFSET` for the confidence band or page slice, which is a reasonable trade-off at the expected review-queue scale but means very large `awaiting_review` backlogs are paginated in-process rather than in the database.

`POST /review/action` is intentionally minimal at the HTTP layer: it validates only that `action` is `approve` or `reject` (else `400`) and forwards the full list of submitted `chunk_ids` (via repeated `chunk_ids` form fields, one per checked checkbox in `review.html`) as-is to the `review` background job — it does not re-validate that each ID is actually `awaiting_review`, is well-formed, or belongs to the currently-filtered source; that validation is left to the domain layer (`web_app.py`'s dispatch calls `approve_chunk`/`reject_chunk` per ID and simply counts `skipped` for any that don't apply). This means the web layer's role here is strictly "collect an operator-confirmed batch and enqueue it," not "validate business state" — consistent with `review.html`'s own client-side `onclick="return confirm(...)"` gate, which is the actual point where an operator commits to the batch action.

**Rule workflow**:
```
GET /review?source_id&confidence&page
  load awaiting_review chunks (optionally filtered by source_id at SQL layer)
  parse summary_json per chunk (tolerate malformed JSON -> {})
  band(chunk) = low   if conf < 0.4
              = medium if 0.4 <= conf < auto_approve_min_confidence
              = high   if conf >= auto_approve_min_confidence
  filter by requested band (if any)
  paginate: page_size=20, slice [(page-1)*20 : page*20], has_next = page*20 < total

POST /review/action
  action must be "approve" or "reject"                        (else 400)
  auth + csrf gates
  enqueue job "review" {action, chunk_ids: [...]}  (no further validation here)
```

---

### Business Rule: LLM provider readiness check

**Overview**:
`_llm_ready(cfg)` decides, purely from environment variables and the configured provider name, whether the LLM-backed pipeline phases can be attempted at all — used to disable UI affordances and to gate job submission for `extract`, `connect`, `garden`, `garden_hubs`, and `run_all`.

**Detailed description**:
The function lower-cases `cfg.llm.provider` and looks it up in a small hardcoded map: `openai` requires `OPENAI_API_KEY`; `openrouter` requires either `OPENROUTER_API_KEY` or `OPENAI_API_KEY` (since OpenRouter's client can be configured to reuse an OpenAI-shaped key depending on setup); `anthropic` requires `ANTHROPIC_API_KEY`; `gemini` requires `GOOGLE_API_KEY`. For any provider name not in this map — notably including `ollama` and any OpenAI-compatible gateway reached via `llm.base_url` (per `CLAUDE.md`'s documented aliasing of `openrouter`/`opencode`/etc.) — the function returns `True` unconditionally, i.e. "assumed ready," since a locally-hosted or custom-endpoint model may not need any API key the web layer knows how to name. This is a deliberately conservative asymmetry: the check can produce false positives (says ready when the endpoint is actually unreachable or misconfigured) for unlisted providers, but it will never block a legitimately configured local/custom provider just because `web.py`'s map doesn't know its env var convention. The actual failure mode for a genuinely broken unlisted-provider configuration surfaces later, as a job failure with a `safe_error()`-sanitized message, not as a pre-flight `409` here.

This check is purely advisory/UI-facing for read paths (`documents.html`, `pipeline.html`, `settings.html` all display an LLM-readiness indicator) but becomes an enforced gate on the mutating routes listed above. It reads live process environment on every call (`os.getenv`), so flipping an API key in the environment and having the app pick it up does not require restarting the worker thread's config loading — only the readiness check itself needs to see the updated environment, which it does immediately.

**Rule workflow**:
```
provider = cfg.llm.provider.lower()
required_env_names = lookup(provider) in {openai, openrouter, anthropic, gemini}
if provider not in map: ready = True (assumed ready — local/custom endpoints)
else: ready = any(os.getenv(name) for name in required_env_names)
Used to: gray out pipeline/run-all buttons in templates; 409-gate extract/
  connect/garden/garden_hubs/run_all submission when False
```

---

### Business Rule: Markdown sanitization for note/MOC bodies

**Overview**:
`GET /notes/{note_id}` and `GET /mocs/{moc_id}` are the only two places this component renders LLM-authored or human-authored Markdown body text as HTML in the browser; both delegate to `zettel.markdown.render_markdown`, which is designed so that even maliciously crafted body content stored in SQLite cannot execute script or navigate to a dangerous URL scheme in the operator's browser.

**Detailed description**:
Body text (which may originate from an LLM's `PermanentNoteLLMOutput`/`MOCGenerationOutput`, from a hand-edited vault file ingested by `sync-manual`, or from any other writer of the `notes`/`mocs` tables) is never trusted as pre-sanitized. `render_markdown` first parses it with a `MarkdownIt("commonmark", {"html": False, "linkify": True})` instance — `html: False` means literal `<...>` HTML in the source Markdown is not passed through as HTML at all (it is escaped as text), which closes the most direct injection path (an LLM or manual note body containing `<script>...</script>` renders as visible escaped text, not an executable tag, as the test suite explicitly verifies). A custom inline rule (`_render_ztl_wikilink`) is registered ahead of the standard `link` rule to recognize Obsidian-style internal links of the shape `[[ZTL - <note_id> - <slug>]]` (optionally with a `|alias`) and rewrite them into a same-origin anchor `href="/notes/{note_id}"` — this is what lets the vault's own internal cross-referencing convention resolve to working in-app navigation instead of rendering as literal double-bracket text.

After Markdown-to-HTML conversion, a **second, independent** sanitization pass runs via `bleach.clean()` with an explicit allowlist: the tag set is `bleach`'s own defaults extended with headings, `p`/`pre`/`code`/`blockquote`, list/table elements, `hr`/`br`; allowed attributes are limited to `href`/`title` on `<a>`, `class` on `<code>` (for syntax-highlighting hints), and `align` on `<th>`/`<td>`; and allowed URL protocols are restricted to `http`, `https`, `mailto` — critically excluding `javascript:`, `data:`, and any other scheme, which is what defeats a `[link](javascript:alert(1))`-style payload even if it somehow survived Markdown parsing (again explicitly covered by the test suite). The module's own docstring states the rationale plainly: this second pass exists specifically so that a future change to the Markdown parser or its configuration (e.g. someone flips `html: True` later for a legitimate reason) does not silently reopen the injection surface — sanitization is not solely dependent on the first layer holding.

This function is infrastructure shared with other components (it lives in `zettel/markdown.py`, not `web.py`), but within the `web` component's boundary it is invoked at exactly two call sites (`zettel/web.py:522` for `note_detail`, `:539` for `moc_detail`) and is the only HTML-rendering path in the entire component that touches free-form stored text — every other template context value is either a plain scalar interpolated by Jinja2's own autoescaping or already-controlled structured data (dashboards, tables, forms).

**Rule workflow**:
```
note.body / moc.body (untrusted, from SQLite) 
  -> MarkdownIt(html=False, linkify=True).render()
       - literal HTML in source is escaped as text, not executed
       - [[ZTL - id - label|alias]] rewritten to <a href="/notes/{id}">
       - bare/linkified URLs become <a href="...">
  -> bleach.clean(tags=allowlist, attributes=allowlist, protocols={http,https,
     mailto}, strip=True)
       - any tag/attribute/protocol outside the allowlist is stripped, not
         escaped-and-shown
  -> rendered_body passed to note_detail.html / moc_detail.html, inserted via
     Jinja2 `| safe`-equivalent (already-sanitized HTML)
```

---

### Business Rule: `create_app()` route-cloning for multi-instance/testable configuration

**Overview**:
Because FastAPI route registration normally happens once via module-level `@app.get/post` decorators, and this app needs to support multiple independently configured instances (each test run, and potentially each deployment pointing at a different `ZETTEL_CONFIG`), `create_app()` builds a fresh `FastAPI` object and copies the canonical module-level app's already-registered routes onto it, rather than re-declaring routes per instance.

**Detailed description**:
The module defines every route with `@app.get`/`@app.post` decorators bound to a single module-level `app = create_app()` (line 124) — this is what actually populates `app.router.routes` the first time the module is imported. `create_app(config_path)` itself, when called again later (as `tests/test_web.py`'s `web_client` fixture does, once per test, each with a different temporary config file), constructs a brand-new `FastAPI(title=..., lifespan=lifespan)`, mounts its own `/static` StaticFiles instance, and then — only if a canonical `app` already exists in the module globals — iterates the canonical app's registered routes and appends each one (by reference, the same `APIRoute` objects) to the new app's router, skipping FastAPI's own auto-registered paths (`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/static`) so each instance gets its own static mount and its own (lazily generated) OpenAPI schema rather than inheriting the canonical instance's. Each cloned instance still gets a distinct `lifespan` invocation, so `app.state.service = WebApplication(config_path)` and its background worker thread are per-instance, not shared — which is the actual point of this pattern: many independently configured `WebApplication`/`WebWorker` pairs (each with its own `StateDB`/vault paths) can run against the same route table without that table being redefined per instance or the different app instances' background workers/state bleeding into each other.

The comment in the source (`zettel/web.py:112-113`) is explicit about why this exists: "The module-level app is created before route decorators run" — i.e., without this cloning step, a second `create_app()` call would produce an app with no routes at all, since the `@app.get(...)` decorators only ever execute against the one `app` object that existed in module scope when they ran at import time. This is a somewhat unusual pattern (most FastAPI apps use an `APIRouter` registered fresh per factory call specifically to avoid this problem) and is worth flagging as an architectural choice with a real constraint: any code that mutates a route object in place (rather than replacing it) would affect every cloned app simultaneously, since the `APIRoute` objects themselves are shared, not deep-copied.

**Rule workflow**:
```
Module import:
  @app.get/@app.post decorators register every route against the single
    module-level `app` (which does not exist yet on the very first call to
    create_app() that produces it — hence the `canonical = globals().get("app")`
    None-check)
  app = create_app()   # this becomes the canonical instance from here on

Any subsequent create_app(config_path):
  new FastAPI instance created, own /static mount
  canonical routes (excluding openapi/docs/redoc/static) appended by reference
  lifespan(app) later constructs a fresh WebApplication(config_path) and starts
    its own background worker thread, independent of any other instance
```

## 4. Component Structure

```
zettel/
├── web.py                      # FastAPI app: routes, auth/CSRF, validation, templating (622 lines) — THIS COMPONENT
├── web_app.py                  # WebApplication/WebWorker facade: SQLite job queue, pipeline dispatch (separate, already-analyzed component; treated as an external dependency here)
├── progress.py                 # ProgressObserver protocol + report() helper shared by CLI and web (36 lines)
├── markdown.py                 # render_markdown(): sanitized Markdown-it + bleach pipeline used only by note/MOC detail routes (73 lines)
├── templates/                  # Jinja2 templates (server-rendered, no client framework)
│   ├── base.html                 # Shared shell: nav, logout form, alert/error/message slots
│   ├── login.html                 # Login form (+ login_csrf) or "SESSION_SECRET missing" notice
│   ├── dashboard.html             # "/" overview: KPIs, funnel, quality, confidence, relations, hubs, costs, runs, recent activity
│   ├── documents.html             # "/documents": upload form, inbox picker + harvest form, run-all button, processed sources table
│   ├── pipeline.html              # "/pipeline": per-phase action cards with prerequisite-driven disabled state
│   ├── review.html                # "/review": filters, batch approve/reject form, paginated draft cards
│   ├── notes.html                  # "/notes": compact lists of permanent notes and MOCs
│   ├── source_detail.html          # "/sources/{id}": read-only source metadata + chunk table
│   ├── note_detail.html            # "/notes/{id}": rendered sanitized body + connections list
│   ├── moc_detail.html             # "/mocs/{id}": rendered sanitized body
│   ├── jobs.html                   # "/runs": full job history table (includes job_rows.html)
│   ├── job_rows.html               # Reusable job-table-rows partial (used by dashboard.html and jobs.html)
│   └── job_detail.html             # "/jobs/{id}": progress bar + inline JS polling loop against /api/jobs/{id}
└── static/
    ├── app.css                     # Base styling (0 lines reported by line-count tool — likely minified/no-trailing-newline; present and referenced)
    ├── mobile.css                  # Responsive overrides (19 lines)
    └── markdown.css                # Styling for sanitized rendered Markdown bodies (167 lines)
```

Note: `web_app.py`, `progress.py`'s consumers inside the pipeline modules, and `markdown.py` are shared/adjacent infrastructure. Per the task's component boundary, `web_app.py` is treated as an **external, already-analyzed dependency** rather than re-analyzed here; `markdown.py` is documented because it has exactly two call sites, both inside `web.py`, and no consumer outside this component.

## 5. Dependency Analysis

```
Internal Dependencies (within zettel/):
web.py -> web_app.py (WebApplication, safe_error)         [job queue facade — separate component]
web.py -> markdown.py (render_markdown)                    [note/MOC body sanitization]
web.py -> hashing.py (file_sha256)                          [needs-harvest checksum comparison]
web.py -> index.py (_format_space_id, peek_stored_embedding_identity)  [settings page, lazy-imported]
web.py -> harvester.py (source_chunking_incomplete)          [needs-harvest check, lazy-imported to avoid circular import]
web.py -> templates/*.html (Jinja2Templates)                 [all HTML rendering]
web.py -> static/*.css (StaticFiles mount)                    [served assets]
web_app.py (not this component) -> config.py, state.py, connector.py, extractor.py,
    gardener.py, gardener_hub.py, harvester.py, review.py, sync.py, index.py, usage.py
    [the actual pipeline dispatch — invoked indirectly by web.py via WebApplication.submit()]

External Dependencies:
- FastAPI                 - ASGI web framework: routing, request/response, dependency injection of Request
- Starlette (via FastAPI) - HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
- Jinja2 (via fastapi.templating.Jinja2Templates) - server-side HTML templating
- uvicorn                 - ASGI server that hosts `zettel.web:app` (per CLAUDE.md run command); not imported by web.py itself
- markdown-it-py          - CommonMark parsing (used transitively via markdown.py)
- bleach                  - HTML sanitization allowlist (used transitively via markdown.py)
- Python stdlib: base64, hashlib, hmac, json, os, re, secrets, time, asyncio (imported lazily inside job_events), contextlib.asynccontextmanager, pathlib.Path
- SQLite (via StateDB, reached through WebApplication/db() — not opened directly by web.py except through the service facade)
```

## 6. Afferent and Efferent Coupling

The component is functional (module-level route handlers and helpers), not class-based; "components" below are the top-level functions/handlers in `zettel/web.py`, grouped by role. Afferent = number of other functions in this module (or templates, for renders) that call into it; Efferent = number of distinct internal/external symbols it calls out to.

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|-------------------|----------|
| `_session()` | 5 (`_auth`, `_csrf_ok`, `_context`, `login_page`, indirectly all protected routes via `_auth`) | 3 (`_secret`, `_sign`, stdlib base64/json/time) | High |
| `_auth()` | 15 (every protected route handler) | 1 (`_session`) | High |
| `_csrf_ok()` | 8 (every mutating route: upload, harvest, run-all, pipeline_action, review_action, logout, `_post_job`) | 2 (`_session`, hmac.compare_digest) | High |
| `_secret()` / `_sign()` | 4 (`_session`, `_session_value`, `_csrf_ok` transitively, login/login_page) | 1 (os.environ) | High |
| `_service()` | 14 (every route that needs `WebApplication`) | 1 (`request.app.state`) | Medium |
| `_render()` / `_context()` | 15 (every HTML-returning route) | 1 (Jinja2Templates, `_session`) | Medium |
| `_post_job()` | 4 (upload-adjacent: harvest, documents_run_all, pipeline_action, review_action) | 3 (`_auth`, `_csrf_ok`, `WebApplication.submit`) | High |
| `_llm_ready()` | 5 (documents, pipeline, pipeline_action, documents_run_all, settings) | 1 (os.getenv, cfg.llm.provider) | Medium |
| `_file_needs_harvest()` | 2 (`_list_pending_inbox`, `harvest` route) | 3 (`db.get_file`, `db.get_source`, `harvester.source_chunking_incomplete`, `file_sha256`) | Medium |
| `_list_pending_inbox()` | 1 (`documents` route) | 2 (`_file_needs_harvest`, `cfg.inbox_path`) | Low |
| `_decorate_connections()` | 1 (`note_detail` route) | 1 (`db.get_note_connections`, `db.get_note`) | Low |
| `create_app()` / `lifespan()` | 1 (module init: `app = create_app()`) + N (test fixtures, alternate config callers) | 3 (`WebApplication`, `FastAPI`, `StaticFiles`) | High |
| Route handlers (individual, e.g. `documents`, `pipeline`, `review`) | 1 each (framework dispatch by path) | 2-6 each (`_auth`, `_service`, `db.*`, `_render`, business-rule helpers) | Medium (High for `harvest`, `pipeline_action`, `review`, `review_action` due to embedded business rules) |
| `job_api` / `job_events` | 1 each (framework dispatch) | 2 (`_auth`, `WebApplication.job`/`.events`) | Medium (`job_events` is the only unstreamed/untested SSE path) |

`_auth`, `_csrf_ok`, and `_session`/`_secret`/`_sign` form the highest-fan-in cluster — nearly every route depends on them, and a defect there compromises the entire component's security posture, which is why they carry "High" criticality despite low individual efferent complexity. `create_app()`/`lifespan()` is architecturally critical (it wires the whole app together and owns the per-instance route-cloning behavior) even though only a handful of call sites invoke it directly.

## 7. Endpoints

All routes are plain HTTP (no GraphQL/gRPC); one endpoint is Server-Sent Events (`text/event-stream`) rather than a standard JSON response.

| Endpoint | Method | Auth Required | Description |
|----------|--------|----------------|-------------|
| `/favicon.ico` | GET | No | Returns `204 No Content` (avoids noisy 404s / template rendering for browsers auto-requesting a favicon) |
| `/login` | GET | No | Renders login form (or a "SESSION_SECRET not configured" notice); redirects to `/` if already authenticated |
| `/login` | POST | No (this route establishes auth) | Validates `login_csrf` then `instance_secret`; on success sets the signed `zettel_session` cookie and redirects to `/` |
| `/logout` | POST | Yes (session) + CSRF | Clears the session cookie, redirects to `/login` |
| `/` | GET | Yes | Overview dashboard (KPIs, funnel, recent jobs) |
| `/documents` | GET | Yes | Upload form, harvest-eligible inbox listing, processed sources table |
| `/documents/upload` | POST | Yes + CSRF | Validates and writes an uploaded file into the inbox |
| `/documents/harvest` | POST | Yes + CSRF | Validates a selected inbox file / duplicate-handling options and enqueues a `harvest` job |
| `/documents/run-all` | POST | Yes + CSRF | Enqueues the composite `run_all` job (harvest→extract→review→connect→garden) with fixed safe defaults |
| `/pipeline` | GET | Yes | Shows per-phase prerequisite status and action buttons |
| `/pipeline/{operation}` | POST | Yes + CSRF | Enqueues one of `extract`/`connect`/`garden`/`garden_hubs`/`sync`/`retry_chunks`/`retry_assets` after prerequisite checks |
| `/review` | GET | Yes | Lists `awaiting_review` drafts with confidence-band/source filters and pagination |
| `/review/action` | POST | Yes + CSRF | Enqueues a batch `approve`/`reject` job over selected `chunk_ids` |
| `/notes` | GET | Yes | Compact list of permanent notes and MOCs |
| `/sources/{source_id}` | GET | Yes | Read-only source detail + its chunks |
| `/notes/{note_id}` | GET | Yes | Read-only note detail: sanitized rendered body + decorated connections |
| `/mocs/{moc_id}` | GET | Yes | Read-only MOC detail: sanitized rendered body |
| `/runs` | GET | Yes | Full job history table |
| `/jobs/{job_id}` | GET | Yes | Single job detail page with client-side polling (via `/api/jobs/{job_id}`) |
| `/api/jobs/{job_id}` | GET | Yes | JSON: `{job, events}` — `events` filtered to `event_id > after` |
| `/api/jobs/{job_id}/events` | GET | Yes | SSE stream of job events; self-terminates after ~10s (20 × 0.5s polls) or on terminal job state |
| `/settings` | GET | Yes | Read-only health/identity panel (DB/vault/inbox/FTS5/LLM availability, embedding drift check) |
| `/static/{path}` | GET | No | Static asset mount (`app.css`, `mobile.css`, `markdown.css`) |

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| `WebApplication` (web_app.py) | Internal service (in-process facade) | Enqueue/inspect background pipeline jobs; dashboard/db read access | Direct Python calls | Python dicts / `StateDB` rows | `submit()` returning `None` mapped to `409`; job failures surfaced later via job `state="failed"` + `safe_error()`-sanitized message, never raised into the HTTP response |
| `StateDB` (via `WebApplication.db()`) | Internal data store | Read sources/chunks/notes/MOCs/connections/stats for every GET page; existence checks for detail routes | Direct Python calls (SQLite underneath) | Python dicts | DB handles always opened/closed in `try/finally`; missing rows return `None`/empty lists, mapped to `404` at the route level, not exceptions |
| `zettel.markdown.render_markdown` | Internal library | Convert stored note/MOC Markdown bodies to sanitized HTML for display | Direct Python call | str -> sanitized HTML str | No exceptions expected; malformed/`None` body handled by `text or ""` |
| `zettel.hashing.file_sha256` | Internal library | Compare on-disk file checksum against last recorded checksum for re-harvest detection | Direct Python call (reads file bytes) | Path -> hex digest str | `OSError` caught in `_file_needs_harvest`, treated as "needs harvest" |
| `zettel.harvester.source_chunking_incomplete` | Internal library (lazy import) | Detect a source marked "completed" that never finished chunking | Direct Python call | Python bool | No explicit handling; assumed to not raise under normal DB state |
| `zettel.index` (`_format_space_id`, `peek_stored_embedding_identity`) | Internal library (lazy import) | Settings page: compare configured vs. persisted embedding provider/model/dimensions | Direct Python call | Tuple of `(provider, model, dimensions)` | No explicit handling; used only for a read-only display |
| Browser (session cookie) | Client | Carry the signed session across requests | HTTP cookie (`zettel_session`) | HMAC-signed base64 JSON | Any malformed/expired/tampered cookie degrades to "no session" (`_session()` catches `ValueError`/`JSONDecodeError`/`UnicodeDecodeError`) |
| Browser (polling JS in `job_detail.html`) | Client | Live job progress display | `fetch()` against `/api/jobs/{job_id}?after=N`, 1s interval, self-clearing on terminal state | JSON | `if (!r.ok) return;` — a failed poll is silently skipped and retried on the next tick |
| SSE consumer (declared, not used by shipped templates) | Client (potential) | Live event stream alternative to polling | `text/event-stream` over `/api/jobs/{job_id}/events` | `data: {json}\n\n` | Loop is bounded to 20 iterations (~10s); no reconnect/backoff logic; no client in the shipped templates actually consumes this endpoint (job_detail.html uses the polling `/api/jobs/{job_id}` endpoint instead) |
| Static file mount | Client | Serve CSS assets | HTTP GET | text/css | Delegated entirely to Starlette's `StaticFiles` (404 on missing file is framework-default) |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Facade | `WebApplication` wraps `WebWorker`/`StateDB` access | `zettel/web_app.py` (consumed at `zettel/web.py:148-149,250,...`) | Keeps all persistence/queueing concerns out of the HTTP layer; `web.py` never touches SQLite directly except through this facade |
| Post/Redirect/Get | Every mutating POST returns a `303 See Other` redirect (to `/documents`, `/jobs/{id}`, `/login`, `/pipeline`, etc.) rather than rendering a response body directly | Throughout, e.g. `zettel/web.py:229,234,241-243,307,319` | Prevents duplicate submission on browser refresh; keeps the URL bar meaningful after a mutation |
| Guard clause / early-return gating | `if not _auth(...): return ...` / `if not _csrf_ok(...): return ...` repeated at the top of every mutating handler rather than centralized middleware | Every POST handler | Explicit, locally-readable authorization per route at the cost of repetition; no FastAPI dependency-injection (`Depends`) or middleware used for auth/CSRF |
| Signed stateless token (HMAC) | Session and CSRF both derived from HMAC-SHA256 over a shared secret, no server-side session store | `zettel/web.py:31-65` | Avoids a session table/cache; trades revocability (can't invalidate one session without rotating the secret for everyone) for simplicity, matching the single-operator deployment model |
| Long-poll / SSE dual support for async job progress | `/api/jobs/{id}` (poll) is what the shipped UI uses; `/api/jobs/{id}/events` (SSE) exists as an alternative but is unused by templates | `zettel/web.py:560-586`; `job_detail.html` scripts | Decouples the background worker's progress reporting from any specific client transport; two transports share the same underlying `events()`/`job()` facade calls |
| Managed lifespan / dependency injection via `app.state` | `WebApplication` instance created in `lifespan()` and attached to `app.state.service`; retrieved per-request via `_service(request)` | `zettel/web.py:98-104,148-149` | Standard FastAPI pattern for a per-app singleton service without a global variable; ties the background worker's start/stop to the ASGI app's lifecycle |
| Template inheritance | All pages `{% extends "base.html" %}` with a `content` block; navigation/logout/alerts defined once | `zettel/templates/base.html` + all others | Consistent shell, single place to add a nav item or alert style |
| Two-layer output sanitization | Markdown parser configured with `html: False` plus an independent `bleach.clean()` allowlist pass | `zettel/markdown.py:59-73` | Defense-in-depth against XSS from LLM- or human-authored note bodies, explicitly designed to survive a future parser reconfiguration |
| Route table cloning for multi-instance apps | `create_app()` copies the canonical app's routes into new `FastAPI` instances | `zettel/web.py:107-121` | Enables per-config `FastAPI` app instances (tests, alternate `ZETTEL_CONFIG`) without redeclaring the route table via a factory-registered `APIRouter` |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|----------------|-------|--------|
| Medium | Session/auth model | Single shared instance secret with no per-user identity, no revocation short of rotating `SESSION_SECRET` for everyone, and no session store to force-expire one compromised cookie early | A leaked cookie is valid for up to 24h with no way to individually revoke it; acceptable for a single-operator local/self-hosted tool but would not scale to multi-user without redesign |
| Medium | `create_app()` route cloning | Routes are copied by reference (same `APIRoute` objects shared across cloned `FastAPI` instances) rather than via a reusable `APIRouter` factory | Any in-place mutation of a route object (unlikely today, but a plausible future refactor) would silently affect every cloned app instance simultaneously; the pattern is also non-idiomatic for FastAPI and could confuse future maintainers expecting `include_router` |
| Low-Medium | `/api/jobs/{job_id}/events` (SSE) | Endpoint exists, is fully implemented, but is not used by any shipped template (which polls `/api/jobs/{id}` instead) and has zero test coverage | Dead-code risk: if it silently breaks (e.g. the bounded 20-iteration loop or the `asyncio.sleep` import-inside-function pattern), nothing would detect it; unclear if it's intended for a future SSE-based client or should be removed |
| Low | `pipeline_action` / prerequisite checks | Prerequisite gating logic (chunk counts, approved concepts, notes existing) is duplicated between `web.py`'s pre-flight `409` checks and whatever validation the domain modules do internally when actually invoked | Two places must be kept in sync if a domain rule changes (e.g. what counts as "ready for connect"); a mismatch would surface as a job that queues fine but fails inside the worker, or vice versa |
| Low | Review pagination | Confidence-band filtering and page slicing happen in Python after loading the full `awaiting_review` set for the given source into memory, rather than at the SQL layer | Fine at expected review-queue sizes; would degrade linearly if a very large `awaiting_review` backlog accumulated (e.g. many large sources harvested without review keeping pace) |
| Low | `_llm_ready()` provider map | Hardcoded provider→env-var map only covers `openai`/`openrouter`/`anthropic`/`gemini`; any other named provider (including typos) is treated as "ready" by default | A misspelled provider name in config would show as "credential available" in the UI even though no such provider truly exists, deferring the real failure to job-execution time with a generic error rather than an actionable pre-flight message |
| Low | Error message sanitization boundary | `web.py`'s own 409/403/400 messages are hardcoded and safe by construction, but the component fully trusts `web_app.safe_error()` to have sanitized any exception message before it reaches a job's `error_message` field, which `job_detail.html` renders directly into the page | If `safe_error()`'s keyword-based sensitive-term filter (`api_key`, `secret`, `token`, `/home/`, `\users\`, etc.) misses a leak pattern, `web.py` has no second sanitization layer of its own before displaying it |

## 11. Test Coverage Analysis

| Component/Area | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------------|------------|--------------------|----------|---------------|
| Auth (`_session`, `_sign`, login/logout, 24h TTL) | 0 (no isolated tests of `_session`/`_sign`/`_session_value`) | 3 (`test_authentication_and_csrf_protect_mutations` + auth checks embedded in `test_navigation_and_retry_job_flow`, `test_unknown_details_do_not_expose_arbitrary_files`) | Good for the happy/rejection paths exercised via `TestClient`; the 86400s expiry branch and cookie-tamper/malformed-cookie branches are not directly exercised (no test manipulates a cookie's signature or age) | Assertions check exact status codes (303/401/403) tied to specific wrong-input scenarios |
| CSRF (`_csrf_ok`, login_csrf) | 0 | 2 (`test_authentication_and_csrf_protect_mutations` covers both wrong `login_csrf` and wrong `csrf`) | Good for the primary mismatch case; does not test a CSRF token from a *different, still-valid* session being replayed against another session (would require two sessions in one test) | Precise status-code assertions |
| Upload validation (`/documents/upload`) | 0 | 1 (`test_upload_rejects_traversal_and_collisions`, 4 sub-cases: traversal, script-like filename, success, collision) | Good coverage of the documented threat cases (traversal, injection-shaped filename, duplicate); does not separately test the 25 MB size cap, empty-file rejection, or the 180-char length limit | Assertions verify both status code and filesystem side effects (`not (tmp_path / "escape.txt").exists()`) — strong for a security-relevant test |
| Harvest selection (`/documents/harvest`, `_file_needs_harvest`, `_list_pending_inbox`) | 0 | 4 (`test_nested_inbox_file_can_be_selected_for_harvest`, `test_harvest_rejects_absolute_and_parent_paths`, `test_documents_hide_completed_file_but_show_changed_copy`, plus prerequisite check in `test_pipeline_blocks_extract_without_harvest_output`) | Very good — directly tests nested-path selection, absolute/`..` rejection, and the completed-vs-incomplete-vs-changed three-way needs-harvest logic including a checksum-change re-surfacing case | Uses real `StateDB` fixtures with realistic source/file rows rather than mocks, giving genuine confidence in the predicate's behavior |
| Pipeline prerequisite gating (`/pipeline/{operation}`) | 0 | 1 direct (`test_pipeline_blocks_extract_without_harvest_output`) + 1 indirect (`retry_assets` happy path in `test_navigation_and_retry_job_flow`) | Partial — only `extract`'s "no chunks pending" gate and `retry_assets`' happy path are directly tested; `connect`/`garden`/`garden_hubs`/`retry_chunks` prerequisite `409`s and the `_llm_ready` gate on these four operations have no dedicated test | Gap: a regression that broke the `connect`/`garden` gates specifically would not be caught by the current suite |
| `run_all` (`/documents/run-all`) | 0 | 1 (`test_documents_can_queue_full_pipeline`) | Good for the happy path with `_llm_ready` monkeypatched true; the `!llm_ready -> 409` branch for this specific route is not directly tested (only asserted generically for other routes via the shared helper's logic, not exercised here) | Uses `monkeypatch` on `service.submit` to assert exact payload shape — strong contract test for the fixed safe-defaults payload |
| Review (`/review`, `/review/action`) | 0 | 0 dedicated (only reachable indirectly via `test_navigation_and_retry_job_flow`'s generic GET-200 check on `/review`) | Weak — no test exercises confidence-band filtering, pagination, or the batch approve/reject POST at all | Gap: banding boundaries (0.4 / configured threshold) and the `action not in {approve,reject}` 400 path are entirely untested at this layer |
| Detail pages (`/sources/{id}`, `/notes/{id}`, `/mocs/{id}`) 404 handling | 0 | 1 (`test_unknown_details_do_not_expose_arbitrary_files`) | Good for the not-found path; happy-path source detail rendering has no dedicated assertion beyond what `test_navigation_and_retry_job_flow` implicitly would not catch (it doesn't visit `/sources/{id}` with a real source) | — |
| Markdown rendering/sanitization (`note_detail`, `moc_detail`) | 0 (no dedicated `zettel/markdown.py` unit test file found) | 1 (`test_note_and_moc_details_render_sanitized_markdown`) | Good breadth for a single test — covers headings/lists/blockquotes/links/wikilinks/image-embeds/script-tag stripping/`javascript:` stripping/multi-connection decoration in one pass | High-quality assertions (checks both what should render and what should be stripped), but concentrated in one test rather than isolated per case; no direct unit test of `render_markdown()` itself outside the HTTP layer |
| Job polling API (`/api/jobs/{job_id}`) | 0 | 1 (`test_navigation_and_retry_job_flow`, via a real `retry_assets` job run to completion) | Good — exercises a real end-to-end job lifecycle (queued→running→succeeded) through the actual `WebWorker` thread, not a mock | — |
| Job SSE stream (`/api/jobs/{job_id}/events`) | 0 | 0 | **None** | Untested code path; see Technical Debt §10 |
| Settings page (`/settings`) | 0 | 1 (generic GET-200 + text-presence check in `test_navigation_and_retry_job_flow`) | Weak — embedding-drift detection logic and per-key health flags are not individually asserted | — |
| `create_app()` route-cloning behavior | 0 | Implicit (every test relies on it working, via the `web_client` fixture calling `create_app(config)` per test) | Implicit/indirect only — no test asserts route count parity or that a second `create_app()` call actually produces a fully functional independent app beyond "the tests all pass" | — |

Overall: the security-critical and file-safety-critical paths (auth, CSRF, upload validation, path traversal, needs-harvest logic, Markdown sanitization) have strong, realistic integration coverage using `fastapi.testclient.TestClient` against a real temporary `StateDB`/config rather than mocks. The weakest areas are the review workflow's confidence-band/pagination/batch-action logic, the per-operation pipeline prerequisite gates beyond `extract`, and the SSE events endpoint, none of which have dedicated tests. `tests/test_web_state.py` (6 tests) covers `web_app.py`/`WebWorker` directly (queue mutual exclusion, job recovery, progress persistence, `run_all` dispatch order) — relevant as the dependency this component relies on for every mutation, but it is testing the adjacent component, not `web.py`'s HTTP surface.

---

**Analysis scope note**: Folders `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, and `.pytest_cache` were excluded per instructions. No files matching those paths were part of this analysis.
