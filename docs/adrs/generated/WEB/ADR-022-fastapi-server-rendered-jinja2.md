# ADR-XXX: FastAPI Server-Rendered Web Interface (No SPA)

**Status:** Accepted
**Date:** 2026-08-29
**Related to:**
- [ADR-XXX: Repository Pattern for Data Access (StateDB and VectorIndex)](../INFRA/ADR-008-repository-pattern-data-access.md)
- [ADR-XXX: Typer and Rich as CLI Framework](../CLI/ADR-026-typer-rich-cli-framework.md)
- [ADR-XXX: SQLite-Backed Persistent Job Queue with Single Worker Thread](./ADR-023-sqlite-backed-job-queue-single-worker.md)

## Context and Problem Statement

The pipeline (harvest, extract, review, connect, garden) previously exposed only a CLI. A web interface was needed to give non-CLI users dashboards, upload forms, a review queue, and job monitoring over the same underlying operations. The choice was between rendering HTML on the server and returning it directly to the browser, or building a JSON API consumed by a separate client-side framework.

The web layer was implemented as a single-process FastAPI + Uvicorn application using Jinja2 templates: every route returns complete HTML (or a redirect), forms submit via standard POST/GET with server-side validation, and there is no Node, webpack, TypeScript, or other frontend build tooling anywhere in the project. All 23 web endpoints and 14 templates follow this pattern uniformly, and long-running pipeline operations are already modeled as background jobs whose progress streams to the browser via Server-Sent Events rather than requiring bidirectional client state.

[NEEDS INPUT: Confirm whether server rendering was an explicit evaluation against a SPA/API-first architecture, or the default choice for a v1 driven by the team's Python-only skill set]

## Decision Drivers

* All existing pipeline code is Python; a separate JS/TS frontend would split the toolchain and require a second skill set the team does not currently maintain.
* The web app is a control surface over operations that already exist as CLI commands, not a real-time collaborative application, so client-side state management offers limited benefit.
* Long-running jobs (harvest, extract, garden) are already modeled as a persistent queue; progress can be streamed with unidirectional SSE instead of a stateful client.
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

[NEEDS INPUT: Was a specific SPA framework or library ever evaluated and rejected, or is this a hypothetical alternative used only for comparison?]

## Consequences

Every future web feature must be implemented as a FastAPI route paired with a Jinja2 template and server-side form handling; there is no established path for adding an isolated client-heavy widget without revisiting this decision. This keeps the web layer's skill requirements aligned with the rest of the Python codebase, but it also means every user interaction pays the cost of a full server round trip, and real-time features beyond SSE-based progress streaming (for example collaborative editing or live search-as-you-type) are not supported by the current architecture.

If the web UI is expected to become the primary interface to the system, or if requirements introduce real-time collaborative features, this decision will need to be revisited. The evidence estimates a move toward an API-first or SPA architecture at 6+ months of work (extracting an API layer, introducing a frontend framework, and restructuring client/server data flow), so the cost of reversing this decision grows with every feature added on top of it.

[NEEDS INPUT: Is the current architecture intended to be permanent, or is an eventual SPA/API-first migration anticipated if the web UI becomes the primary interface or real-time features become a requirement?]

## References

* `zettel/web.py:1-30` — FastAPI app setup, Jinja2 template directory configuration, static file mounting
* `zettel/web.py` — 23 HTTP endpoints, all returning `HTMLResponse`/`RedirectResponse` (JSON reserved for the SSE progress stream)
* `zettel/web_app.py` — `WebApplication` service layer; web routes delegate business logic here, holding no HTTP concerns themselves
* `zettel/templates/` — 14 Jinja2 templates covering dashboard, documents, pipeline, review, notes, MOCs, runs, and settings pages
