# ADR-040: JSON Pickers and Progressive Enhancement

**Status**: Accepted (2026-09-04)  
**Depends on**: [ADR-022](./ADR-022-fastapi-server-rendered-jinja2.md), [ADR-039](./ADR-039-web-as-python-package.md)  
**Relates to**: [ADR-023](./ADR-023-sqlite-backed-job-queue-single-worker.md), [ADR-030](../MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)

## Context

ADR-022 says every web feature is a FastAPI route + Jinja2 template + server-side form. That rule made `/notes/new` ship two `<select>`s built from `list_sources()` + `get_chunks_for_source()` — `SELECT *` over every source and every chunk, including `extracted_text` / `chunks.text`, on every GET and POST. The lists do not scale, and they cannot filter LIT by source.

The composer also needs type-ahead over citekeys and section paths. A SPA or a JSON mutation API would throw ADR-022 away. A server-rendered `<select>` of the 50 most recent rows, enhanced in the browser by a read-only JSON GET, keeps the form working with JavaScript off.

## Decision

**Allowed deviation from ADR-022:**

* GET JSON, read-only, session-authenticated, no CSRF (no side effect; cookie is `samesite=lax`; no CORS headers).
* The JSON feeds a form control that **degrades to a server-rendered `<select>`**.
* No new runtime dependency, no bundler, no client-rendered page.

**Not allowed:**

* JSON for mutations (POST/PATCH/DELETE stay as HTML forms).
* A page whose first paint depends on JavaScript.
* Any frontend build step.

Concrete surface:

* `GET /api/pickers/sources?q=&limit=`
* `GET /api/pickers/literature?q=&source_id=&limit=` (`source_id` required)
* `zettel/static/combobox.js` + `zettel/templates/_combobox.html`
* Queries: `StateDB.search_sources`, `search_literature_chunks`, `search_literature_chunks_fts` — never select `extracted_text`, `lit_body`, `chunks.text`, and the HTTP JSON never returns `literature_note_path`.

Unauthenticated picker calls return `401 {"error":"unauthorized"}` (JSON, not a login redirect), matching `/api/jobs/{id}`. Query strings are clamped (≤200 chars, limit 1..50). LIKE metacharacters are escaped; FTS user text is quoted by `_fts_match_expr`.

With JavaScript off, the `<select>` of the 50 most recent rows is the whole control. With JavaScript on, the script hides that select, types into a combobox, and on fetch failure restores the select.

## Consequences

ADR-022's "every feature is a template" sentence is amended to point here. The exception is narrow: search-as-you-type for an existing form field. Adding a second combobox should reuse `combobox.js` rather than invent a client store.

## Alternatives

* Keep the full-scan `<select>`. Rejected: it loads whole documents to fill a dropdown.
* A dedicated JSON API consumed by a SPA. Rejected: ADR-022.
* Server-side filtering via GET query on `/notes/new` and a full page reload per keystroke. Rejected: unusable, and still would need the narrow SELECT.

## Acceptance Criteria

- [x] `/notes/new` works with JavaScript disabled (fallback `<select>`)
- [x] Picker JSON never contains `extracted_text`, `lit_body`, `chunks.text`, or `literature_note_path`
- [x] Unauthenticated GET returns 401 JSON
- [x] `source_id` is required on the literature picker
- [x] `%` / `_` / FTS operators are treated as literals

## References

* `zettel/web/pickers.py` — `picker_sources`, `picker_literature`
* `zettel/state.py` — `search_sources`, `search_literature_chunks`, `search_literature_chunks_fts`
* `zettel/static/combobox.js` — progressive enhancement
* `zettel/templates/_combobox.html` — fallback `<select>`
* `tests/test_web.py` — picker contract and path-guard tests
