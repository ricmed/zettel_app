# ADR-037: Pre-Flight Cost Estimate as a Pure Function, Confirmation Only in the CLI

**Status**: Accepted (2026-09-03)

**Depends on:**
- [ADR-026: Typer and Rich as CLI Framework](./ADR-026-typer-rich-cli-framework.md)
- [ADR-032: CLI as Python Package](./ADR-032-cli-as-python-package.md)

**Related to:**
- [ADR-004: YAML-First Configuration with Pydantic Fallback](../INFRA/ADR-004-yaml-first-configuration.md)
- [ADR-017: Confidence-Band HITL Approval Gate](../REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)
- [ADR-023: SQLite-Backed Job Queue with a Single Worker](../WEB/ADR-023-sqlite-backed-job-queue-single-worker.md)
- [ADR-024: Pluggable Multi-Provider LLM Strategy](../LLM/ADR-024-multi-provider-llm-strategy.md)

## Context and Problem Statement

`extract`, `connect` and `article` each fire dozens of LLM calls. The project already knows what a call costs — `CostTracker`, `pricing.estimate_llm_cost` and LiteLLM's price map — but only *afterwards*, in the `runs` row and in the note's frontmatter. The operator learns the price of a decision after making it.

The obvious fix is to print an estimate and ask. The non-obvious part is *where* the asking happens. `run_extract`, `run_connect` and `run_article_graph` are called by three very different callers: the CLI (a person at a terminal), the web worker (a daemon thread with no stdin), and the test suite (which must never block). Putting a prompt inside those functions would make the daemon and the tests hang on a question nobody can answer.

## Decision Drivers

* A test calling `run_extract` directly must not acquire a new way to block.
* The web worker is non-interactive by construction (ADR-023); it must not grow an interactive dependency.
* The estimate has to be honest about being an estimate: prices move, and the SQLite response cache can make the real number much smaller.
* No LLM call may happen to produce the estimate — a pre-flight that costs money defeats itself.
* Prices must keep coming from LiteLLM (ADR-024's convention: unknown or local model = $0), never from a table maintained here.
* This is a *courtesy*, not a budget cap. Aborting a run on a dollar threshold is a different feature with different failure modes.

## Considered Options

* Estimate and confirm inside `run_extract` / `run_connect` / `run_article`, guarded by an `interactive` flag.
* Estimate as pure functions in a new module; confirm only in the CLI wrappers.
* Skip the estimate and improve after-the-fact reporting in `zettel status`.

## Decision Outcome

**`zettel/preflight.py` holds pure estimators; `cli.deps.preflight_gate` does the rendering and the asking.** `estimate_extract`, `estimate_connect` and `estimate_article` read SQLite and config, return a frozen `PreflightEstimate`, and touch nothing else. The `run_*` functions are **unchanged** — the web worker, `run-all` and every existing test keep their exact behaviour, and no `interactive=` flag had to be threaded through three call chains.

The gate passes straight through when `--yes` is set *or* when stdin is not a TTY, so scripts and CI never block. A declined confirmation exits 1 before any LLM client is even constructed.

**How each number is derived** (all tokens are `chars // 4`, the estimator the cost layer already uses):

- **extract**: one call per `pending` chunk — chunk text plus the `literature_note.md` template, times `extraction.preflight_output_tokens_per_chunk` (default 800) for output.
- **connect**: one call per approved candidate — the candidate's own fields, plus a RAG context sized as `linking.topk + graph_expansion.max_neighbors` entries at `RAG_CHARS_PER_NOTE` (250) each. That constant comes from reading `_build_rag_context`, which renders a wikilink plus a 150-char snippet and tags — using the *average note length* would overstate the context by an order of magnitude.
- **article**: an explicit **floor** — enrich + outline + one draft per section + assemble + the judge ceiling, derived from the `retrieval.article` knobs. The panel says so; HITL revisions and the personality rewrite can only push it up.

The two new config keys live in `extraction` and `linking` rather than in `extract`/`connect` sections, because those sections do not exist — the schema names the *concern*, not the command. Both are targets for the estimate, explicitly **not** caps: nothing truncates a response to hit them, which would be the "padding to budget" anti-pattern in reverse.

The SQLite response cache is deliberately not discounted. A cache hit costs $0, so subtracting an unknown number of hits would turn an upper bound into a guess. The panel states this rather than silently rounding down.

### Positive Consequences

* The operator sees "42 chunks, ~110k in / ~34k out, ~$0.04, model X" before spending it.
* `run_*` signatures and semantics are untouched, so the web worker and the whole test suite are unaffected by construction.
* The estimate is testable with fixed numbers and no network: LiteLLM's price map is local, and an unknown model deterministically yields $0.
* The panel names the phase's *actual* model, which surfaces a misconfigured `llm.<phase>` before it costs anything.

### Negative Consequences

* The estimate can be wrong in both directions: the cache makes it too high; a model that ignores the output target makes it too low. It is a magnitude check, not an invoice.
* `RAG_CHARS_PER_NOTE` mirrors a rendering detail of `_build_rag_context`. If that renderer changes its snippet size, the constant silently drifts out of step.
* `garden`, `harvest` and `ask` have no pre-flight, so cost visibility is uneven across commands — a deliberate scoping choice (garden caps itself at one call per cluster, `ask` is one call and zero when the floor is empty).
* The web UI still shows no estimate before enqueuing a job.

## Pros and Cons of the Options

### Confirm inside `run_*` behind an `interactive` flag

* Good, because every caller gets the gate for free.
* Bad, because a default of `True` would hang the worker and the tests, and a default of `False` means the CLI has to opt in anyway — so the flag buys nothing.
* Bad, because it puts terminal concerns inside pipeline modules that currently have none.

### Pure estimators + CLI gate (chosen)

* Good, because the blast radius is the CLI only.
* Good, because the estimators are trivially unit-testable with no I/O beyond SQLite.
* Bad, because a future caller (say, the web UI) has to wire its own presentation — acceptable, since its presentation would be different anyway.

### Better after-the-fact reporting only

* Good, because `zettel status` already exists and needs no new module.
* Bad, because it answers the question after the money is gone, which is the problem being solved.

## Consequences

A future web pre-flight can reuse the same estimators and render them into the job's first `web_job_events` row without touching `preflight.py` — that is exactly why the estimate and the asking were split.

If a hard budget cap is ever wanted, it belongs on top of `PreflightEstimate`, not inside it: the estimator should keep answering "how much" without also deciding "whether".

## References

* `zettel/preflight.py` — `PreflightEstimate`, `estimate_extract`, `estimate_connect`, `estimate_article`, `RAG_CHARS_PER_NOTE`
* `zettel/cli/deps.py` — `preflight_gate`, next to the embedding-drift confirmation it mirrors
* `zettel/cli/curation.py`, `zettel/cli/synthesis.py`, `zettel/cli/writing.py` — the three call sites
* `zettel/config.py` — `extraction.preflight_output_tokens_per_chunk`, `linking.preflight_output_tokens_per_note`
* `zettel/pricing.py` — `estimate_llm_cost` (LiteLLM price map, $0 for unknown/local)
* `zettel/connector.py` — `_build_rag_context` (the renderer `RAG_CHARS_PER_NOTE` mirrors)
* `tests/test_preflight.py` — fixed-number arithmetic, `--yes` / non-TTY pass-through, abort path, "never calls an LLM"
