# ADR-062: Web Handlers Run in the Threadpool

**Status:** Accepted
**Date:** 2026-10-20
**Issue:** #304 (epic #303)
**Depends on:** [ADR-022: FastAPI Server-Rendered Web Interface](./ADR-022-fastapi-server-rendered-jinja2.md), [ADR-039: Web as Python Package](./ADR-039-web-as-python-package.md)
**Related to:** [ADR-023: SQLite-Backed Job Queue](./ADR-023-sqlite-backed-job-queue-single-worker.md), [ADR-056: Web Acervo](./ADR-056-web-acervo-consult-and-produce.md)

## Context and Problem Statement

The web runs on one Uvicorn worker (ADR-023). Every route handler was `async def`, and every one of them did blocking work: `sqlite3`, reading and validating `config.yaml`, hashing inbox files, opening Chroma, and in two places a network call (the query embedding of the catalog, the LLM of the bibliography preview). FastAPI runs an `async def` handler on the event loop itself, so that work blocked the whole server while it ran.

Measured on a copy of the development data (`docs/artigo/06-web.md` §5.2): `GET /favicon.ico` answers in 27 ms alone and took 1,572 ms when requested while `GET /review?queue=links` rendered. This choice was never recorded; it came from writing the handlers `async` by habit.

## Decision Drivers

* No handler in the package awaits anything except the request body; the pipeline, `StateDB` and Chroma are synchronous libraries.
* A slow or hung provider must not freeze the pages that report progress.
* The fix must not change the job queue, the routes or the templates.

## Decision Outcome

Every route handler in `zettel/web/` is a plain `def`. FastAPI runs it in the AnyIO threadpool and the event loop only accepts connections and moves bytes.

* A handler that reads a free-form body declares `form: FormData = Depends(read_form)`. `zettel/web/forms.py::read_form` is the one `async` function of the package: FastAPI awaits it on the loop, then calls the handler in a thread with the parsed form.
* `UploadFile` is read with `file.file.read(...)`; the body is already spooled when the handler runs.
* A streaming response uses a sync generator; Starlette iterates it in the threadpool.
* Each handler opens and closes its own `StateDB` in the thread it runs on, which is what `sqlite3`'s `check_same_thread` requires. No connection is shared between requests.
* `tests/test_web_package.py::test_route_handlers_run_in_the_threadpool` fails on any `async def` decorated with `@router.*`.

Measured after the change on the same copy: `favicon.ico` answers in 31 ms while `/review?queue=links` renders, and in 48 ms while `/notes?q=kant` does.

### Consequences

* Concurrency between requests is now real: up to the threadpool size (40 by default) handlers run at once. Every mutation still goes through the job queue's `BEGIN IMMEDIATE`, and the routes that write outside it are tracked in #308.
* The cost of a slow page is no longer paid by every other request, but it is still paid by the person who asked for it; the slow pages themselves are #307, #309 and #312.
* An `async` handler is allowed again only if it awaits an async library end to end. A new handler that needs the body uses `read_form`.

## Alternatives

* Keep `async def` and wrap every blocking call in `run_in_threadpool`. Rejected: dozens of call sites, and a missed one silently blocks the loop again.
* Async drivers (`aiosqlite`, an async Chroma client). Rejected: the pipeline modules the handlers call are synchronous, so the loop would still block on them.
* More Uvicorn workers. Rejected: the job queue and the in-memory article pause (ADR-056) assume one process.

## References

* `zettel/web/forms.py` — `read_form`
* `zettel/web/*.py` — route handlers
* `tests/test_web_package.py` — `test_route_handlers_run_in_the_threadpool`
* FastAPI, *Concurrency and async / await*: <https://fastapi.tiangolo.com/async/>
