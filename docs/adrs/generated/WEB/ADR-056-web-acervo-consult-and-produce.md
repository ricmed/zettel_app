# ADR-056: Web Acervo for Consult and Produce

**Status:** Accepted
**Date:** 2026-10-07
**Depends on:** [ADR-022: FastAPI Server-Rendered Web Interface](./ADR-022-fastapi-server-rendered-jinja2.md), [ADR-023: SQLite-Backed Persistent Job Queue](./ADR-023-sqlite-backed-job-queue-single-worker.md), [ADR-028: LangGraph StateGraph for Article Orchestration](../QA-WRITING/ADR-028-langgraph-stategraph-article-orchestration.md)
**Related to:** [ADR-035: Flat Agent Skill Export](../CLI/ADR-035-flat-agent-skill-export.md), [ADR-037: LLM Cost Preflight](../CLI/ADR-037-llm-cost-preflight-estimate.md), [ADR-047: Chapter Summaries as a Library Routing Index](../RETRIEVAL/ADR-047-chapter-summaries-as-library-routing-index.md)

## Context and Problem Statement

The web UI covered harvest, extract, review, connect and garden. Asking the vault a question, seeing which sources treat a subject, summarizing chapters, writing an article and exporting a skill existed only as CLI commands. Those five operations are how a person uses an already-built vault, and they take different shapes: one is a table with no LLM call, three spend a model, and the article pauses twice for a human decision.

Putting five new items in the top navigation would make the bar harder to scan. Giving the article the same fire-and-forget job as extract would drop the two reviews that the graph exists to support.

## Decision Drivers

* The operations belong together because they read or project an approved vault, rather than moving a source through the pipeline.
* Catalog answers on the page. Ask, summarize, skill and article take long enough, or write files, that they belong on the single worker.
* Catalog must not open Chroma while that worker holds the index.
* The article's two reviews are the feature. Skipping them by default would make the checkboxes a lie.
* The web must not accept an arbitrary filesystem path for a saved note or a skill pack.
* The article checkpoint already does not survive a process restart ([ADR-028](../QA-WRITING/ADR-028-langgraph-stategraph-article-orchestration.md)). The web pause uses that same limit.

## Considered Options

1. One Acervo page, three groups, catalog synchronous, the other four on the queue, article parked in memory (chosen).
2. Five new navigation entries, each a job, article always non-interactive.
3. Leave the five commands on the CLI.

## Decision Outcome

Chosen option 1. `GET /studio` is one navigation item, **Acervo**, with three groups:

* **Consultar** — ask and catalog.
* **Preparar** — summarize. The page says the catalog reads those chapter summaries.
* **Produzir** — article and skill.

Catalog is a GET on the same page. If a job is `queued` or `running`, the handler returns 409 and does not open the index. Ask, summarize, skill and article are jobs. Summarize and article show `estimate_summarize` / `estimate_article` on the form; submitting the form is the confirmation the CLI's `--yes` used to be.

The article runs through `ArticleDrive` in `zettel/article_graph/graph.py`. `run_article_graph` is that drive driven to completion, so the CLI and the web share one interrupt loop. When a review is requested, the worker stores the drive in memory, sets the job to `awaiting_input`, and returns to the queue. That state does not occupy the `queued`/`running` slot. `POST /jobs/{id}/resume` re-queues the same job with the decision. The worker claims the oldest `queued` row, so a parked article that is resumed is not hidden behind a newer job that already finished.

A restart marks `awaiting_input` as `interrupted`. The in-memory graph is gone. The operator starts the article again.

Saved ask notes and articles go to `00_Inbox/`. Skills go to `<vault>/.claude/skills/<slug>/`. `--save-to` and `--out` stay on the CLI.

### Positive Consequences

* The five operations are available without a terminal, grouped by what the operator is trying to do.
* The article's context and outline reviews happen in the browser.
* Another job can run while an article waits for a decision.
* A path outside the vault cannot be chosen from the form.

### Negative Consequences

* A restart drops an article that was waiting for a decision. The LLM spend up to the pause is kept on the open run only until the process dies; the next start does not resume the graph.
* Catalog returns 409 while any job is active, including one that does not touch Chroma.
* The paused drive holds a database connection and the compiled graph until the article ends or the process stops.

## References

* `zettel/web/studio.py` — the page and the four POST handlers
* `zettel/web_app.py` — `_dispatch_ask`, `_dispatch_summarize`, `_dispatch_skill`, `_dispatch_article`, `JobParked`
* `zettel/article_graph/graph.py` — `ArticleDrive`
* `zettel/state/web.py` — `awaiting_input` recovery, `next_queued_web_job`, `requeue_parked_job`
