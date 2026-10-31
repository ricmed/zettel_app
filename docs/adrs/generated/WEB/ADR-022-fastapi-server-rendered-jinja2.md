# ADR-022: FastAPI Server-Rendered Web Interface (No SPA)

**Status:** Accepted
**Date:** 2026-08-29
**Related to:**
- [ADR-008: Repository Pattern for Data Access (StateDB and VectorIndex)](../INFRA/ADR-008-repository-pattern-data-access.md)
- [ADR-026: Typer and Rich as CLI Framework](../CLI/ADR-026-typer-rich-cli-framework.md)
- [ADR-023: SQLite-Backed Persistent Job Queue with Single Worker Thread](./ADR-023-sqlite-backed-job-queue-single-worker.md)
- [ADR-039: Web as Python Package](./ADR-039-web-as-python-package.md)
- [ADR-040: JSON Pickers and Progressive Enhancement](./ADR-040-json-pickers-progressive-enhancement.md)

## Context and Problem Statement

The pipeline (harvest, extract, review, connect, garden) previously exposed only a CLI. A web interface was needed to give non-CLI users dashboards, upload forms, a review queue, and job monitoring over the same underlying operations. The choice was between rendering HTML on the server and returning it directly to the browser, or building a JSON API consumed by a separate client-side framework.

The web layer was implemented as a single-process FastAPI + Uvicorn application using Jinja2 templates: every route returns complete HTML (or a redirect), forms submit via standard POST/GET with server-side validation, and there is no Node, webpack, TypeScript, or other frontend build tooling anywhere in the project. All the web routes (41 on 2026-10-20: 38 HTML pages and form posts, 3 read-only JSON GETs) and the 19 templates follow this pattern, and long-running pipeline operations are background jobs whose progress the job page reads by polling a JSON snapshot once a second, with no bidirectional client state. Every handler is a plain `def` run in the threadpool ([ADR-062](./ADR-062-web-handlers-run-in-threadpool.md)).

**Recorded answer (2026-10-20).** Neither the repository nor the issue history records a formal evaluation against a SPA or an API-first design: server rendering was the default for a single-user control surface in a Python-only project. It has held. The web audit of 2026-10 (`docs/artigo/06-web.md`) traced the interface's problems to the execution model, the security of the login and the user flow, none to the rendering choice.

## Decision Drivers

* All existing pipeline code is Python; a separate JS/TS frontend would split the toolchain and require a second skill set the team does not currently maintain.
* The web app is a control surface over operations that already exist as CLI commands, not a real-time collaborative application, so client-side state management offers limited benefit.
* Long-running jobs (harvest, extract, garden) are already modeled as a persistent queue; the page reads their progress from a JSON snapshot instead of keeping client state.
* Avoiding a JS build step removes an entire class of deployment and CI complexity (no Node toolchain, no npm dependency surface to audit).
* Server-side rendering keeps session and CSRF state centralized on the server rather than split across an API and a separate client.

## Considered Options

* Server-rendered FastAPI + Jinja2 templates, full page responses
* Single Page Application with a dedicated JS/TS framework consuming a JSON API

## Decision Outcome

Chosen option: "Server-rendered FastAPI + Jinja2 templates", because it keeps the entire web layer in the same language as the rest of the pipeline, avoids introducing frontend build infrastructure the project has never needed, and fits a UI that is fundamentally a set of forms, dashboards, and job monitors rather than a low-latency interactive application. Every route already delegates business logic to the existing service layer, so the server-rendering choice only affects how the response is produced, not how the pipeline operations themselves work.

## Pros and Cons of the Options

### Server-rendered FastAPI + Jinja2 templates

* Good, because the web layer uses the same language as the pipeline, with no context-switching for contributors.
* Good, because it requires no frontend build tooling (Node, webpack, npm) to install, configure, or keep patched.
* Good, because session state and CSRF protection are centralized server-side rather than split across an API boundary and a client.
* Bad, because every interaction requires a full page reload and server round trip, with no fine-grained client-side interactivity.

### SPA with a dedicated JS/TS framework and JSON API

* Good, because it would enable low-latency, partial-page client-side interactions.
* Good, because frontend and backend release cycles could evolve independently.
* Bad, because it requires standing up a JSON API layer, a JS/TS build pipeline, and client-side state management from nothing.
* Bad, because it splits the team's required skill set across Python and JavaScript/TypeScript.

**Recorded answer (2026-10-20).** No SPA framework was evaluated; the option above is a point of comparison, not a rejected candidate.

## Consequences

Every future **mutating** web feature must be implemented as a FastAPI route paired with a Jinja2 template and server-side form handling. Read-only JSON GETs that feed a form control with a server-rendered `<select>` fallback are a documented exception — see [ADR-040](./ADR-040-json-pickers-progressive-enhancement.md). There is still no path for a client-rendered page, a JSON mutation API, or a JS bundler. This keeps the web layer's skill requirements aligned with the rest of the Python codebase, but it also means every mutating interaction pays the cost of a full server round trip.

If the web UI is expected to become the primary interface to the system, or if requirements introduce real-time collaborative features, this decision will need to be revisited. The evidence estimates a move toward an API-first or SPA architecture at 6+ months of work (extracting an API layer, introducing a frontend framework, and restructuring client/server data flow), so the cost of reversing this decision grows with every feature added on top of it.

**Recorded answer (2026-10-20).** It is meant to stay. The web UI is a control surface for one person on one instance ([ADR-023](./ADR-023-sqlite-backed-job-queue-single-worker.md)); the decision is to be reopened only if that changes, that is, several users at once or live collaboration, not because the UI becomes the main entry point.

## References

* `zettel/web/server.py` — `create_app`, lifespan, static mount
* `zettel/web/rendering.py` — Jinja2 template directory (``templates``)
* `zettel/web/` — HTTP endpoints (HTML or redirect; JSON reserved for job progress and the picker GETs of ADR-040)
* `zettel/web_app.py` — `WebApplication` service layer; web routes delegate business logic here, holding no HTTP concerns themselves
* `zettel/templates/` — Jinja2 templates covering dashboard, documents, pipeline, review, notes, MOCs, runs, and settings pages

## Addendum (2026-10-20): progress is polled, there is no SSE (#313)

**Status:** Accepted. The context above says job progress streams to the browser by Server-Sent Events. It did not: `GET /api/jobs/{id}/events` existed, but the job page never opened it and polled `GET /api/jobs/{id}?after=<last event>` once a second. The endpoint was removed instead of adopted.

* Polling is enough for one user watching one job, and it keeps the page a plain `fetch`.
* Since [ADR-062](./ADR-062-web-handlers-run-in-threadpool.md) every handler runs in the threadpool, so a stream would hold one worker thread per open job page for the whole job.
* The poll reads only the events after the last one it saw, through the index `web_job_events(job_id, event_id)`.

A batch job reports item k only once item k is done; a batch that is one call (deleting extract rejections from SQLite and Chroma) reports that it started, with no count. Before, the bar reached 100% before the work began.

## Addendum (2026-10-20): security headers and a CSP without inline code (#315)

**Status:** Accepted. Every response carries `Content-Security-Policy` (`default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`), `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`, added by plain ASGI middleware in `zettel/web/hardening.py`. `frame-ancestors 'none'` replaces `X-Frame-Options`.

* No `'unsafe-inline'`: the six inline `<script>` blocks moved to `zettel/static/` (`theme-init.js`, `theme-toggle.js`, `job-detail.js`, `manual-notes.js`, `review.js`, `studio.js`). A value a script needs travels in a `data-*` attribute, a confirmation in `data-confirm` / `data-review-confirm`, and the progress bar width is set by script. `tests/test_web_hardening.py` fails on any template with an inline script, an `on*=` handler, a `style=` attribute or a `<style>` block.
* The same middleware module refuses a body over 26 MB by its `Content-Length` (413) before the multipart parser reads a byte, and a chunked body (411), which no browser sends for a form. The 25 MB check in the upload handler stays, for the file itself. An upload is written to `.<name>.part` and hard-linked into the inbox, so half a file never sits under its final name and a name taken meanwhile is refused, never overwritten.
* The second sanitizing pass of `zettel/markdown.py` is `nh3` (ammonia), with the same tags, attributes and URL schemes; `bleach` is deprecated since 2023 and was removed from the dependencies.
* An error reaches the browser only if its class is in `web_app.KNOWN_ERRORS` (domain errors this codebase words for a reader), with absolute paths removed. Anything else shows a generic message with a correlation id that the log line also carries. The previous blocklist let a `D:\...` path through.

## Addendum (2026-10-26): TeX on LIT/ZTL pages is MathML, not MathJax

**Status:** Accepted. Notes store `$...$` / `$$...$$` (and `\\(...\\)` / `\\[...\\]`). The Markdown parser used to treat `_` inside those delimiters as emphasis, so `$Z_t$` reached the browser as `$Z<em>t</em>$`. MathJax from a CDN ([getting started](https://www.mathjax.org/#gettingstarted)) would fix the typesetting, but it needs a remote `script-src` and injects `<style>` / `style=` attributes, which the CSP above forbids.

`zettel/markdown.py` now parses those delimiters before emphasis and converts the TeX to MathML with `latex2mathml`. The sanitizer allows presentation MathML. Review cards that are not Markdown (tese, definição) go through the same conversion via the Jinja filter `with_math`. No client script, no CSP change.
