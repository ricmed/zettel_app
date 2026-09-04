# ADR-039: Web as Python Package

**Status**: Accepted (2026-09-04)  
**Depends on**: [ADR-022](./ADR-022-fastapi-server-rendered-jinja2.md)  
**Relates to**: [ADR-018](../REVIEW/ADR-018-web-cli-validation-asymmetry.md), [ADR-023](./ADR-023-sqlite-backed-job-queue-single-worker.md), [ADR-027](../HARVEST/ADR-027-harvest-phase-as-python-package.md), [ADR-029](../QA-WRITING/ADR-029-article-graph-as-python-package.md), [ADR-032](../CLI/ADR-032-cli-as-python-package.md)

## Context

`zettel/web.py` had grown to 745 lines holding session/CSRF, the ASGI factory, domain helpers and 23 HTTP routes. It was the same concentration that ADR-027 (harvester), ADR-029 (article_graph) and ADR-032 (cli) already solved by splitting a monolith into a package.

Two extra constraints, specific to the web layer:

1. **Registration order is a correction, not cosmetics.** `GET /notes/new` must be registered before `GET /notes/{note_id}`. Invert them and the composer 404s. Isolating the three parametric detail pages in `details.py` and importing that module last turns a fragile pairing into a single testable rule.
2. **The submodule cannot be named `app.py`.** `uvicorn zettel.web:app` resolves the `app` attribute of the package. A submodule `zettel.web.app` would rebind that attribute to the *module*, silently replacing the FastAPI instance. The failure would only show in production. Same naming collision ADR-029 avoided with `runtime.py` and ADR-032 avoided with `qa.py` / `writing.py`.

Repeated basenames (`web/manual.py` vs `cli/manual.py`, `web/pipeline.py` vs `harvester/pipeline.py` / `cli/pipeline.py`, `web/review.py` vs `zettel/review.py`) are acceptable: the CLI package already lives with that, and siblings import by absolute path (`from zettel.web.rendering import render`).

## Decision

Convert `zettel/web.py` into the package `zettel/web/`:

```
zettel/web/
├── __init__.py     assembly; import order = registration order; ROUTE_MODULES/ROUTERS
├── server.py       create_app / lifespan / static mount — imports nothing local
├── security.py     session cookie, CSRF, login redirect
├── rendering.py    templates, context, render, service
├── enqueue.py      post_job
├── health.py       llm_ready, llm_phase_rows
├── manual_form.py  parse / validate / preflight of the manual-note form
├── auth.py         /favicon.ico, /login, /logout
├── dashboard.py    /
├── documents.py    /documents, upload, harvest, run-all
├── pipeline.py     /pipeline
├── review.py       /review
├── notes.py        /notes listing
├── manual.py       /notes/new
├── pickers.py      /api/pickers/sources, /api/pickers/literature
├── jobs.py         /runs, /jobs, /api/jobs
├── settings.py     /settings
└── details.py      /sources/{id}, /notes/{id}, /mocs/{id} — always last
```

`create_app` includes routers explicitly (`include_router`) instead of cloning `globals()["app"]`. Template and static paths resolve via `Path(__file__).resolve().parent.parent` because the package sits one level deeper than the old module.

### Four seams (three from ADR-032, one new)

1. **`server.py` imports nothing from the package** (anti-cycle).
2. **`ROUTE_MODULES` / `ROUTERS` bind the imports to names**, so an "unused import" cleanup cannot drop routes.
3. **Domain modules are imported inside the handler.** Allowlist at module scope: `zettel.web_app`, `zettel.markdown`, `zettel.hashing`. `chromadb` / `docling` / `langchain` stay off the `GET /` path.
4. **Parametric detail routes register last.** This is a functional invariant, enforced by `tests/test_web_package.py`.

Siblings import by absolute path, following ADR-032's deviation from ADR-027.

## Consequences

`uvicorn zettel.web:app` is unchanged. Tests that monkeypatched `zettel.web._llm_ready` by string must target the consuming module (`zettel.web.documents._llm_ready`). ADR references that pointed at `zettel/web.py` move to module + symbol.

## Alternatives

* Keep the monolith and extract only `/notes/new`. Rejected: the order bug and the factory clone would stay.
* Name the factory module `app.py`. Rejected: attribute collision with the ASGI app.
* Relative imports inside the package. Rejected: `zettel.web.pipeline` already coexists with two other `pipeline.py` files.

## Acceptance Criteria

- [x] `uvicorn zettel.web:app` still resolves a FastAPI instance
- [x] `/notes/new` is registered before `/notes/{note_id}`
- [x] `tests/test_web_package.py` locks the four seams
- [x] `create_app(config_path)` builds an independent app (no clone of `globals()["app"]`)

## References

* `zettel/web/__init__.py` — `ROUTE_MODULES`, `ROUTERS`, `app`
* `zettel/web/server.py` — `create_app`, `lifespan`
* `zettel/web/details.py` — parametric routes, registered last
* `tests/test_web_package.py` — AST and route-order contract
