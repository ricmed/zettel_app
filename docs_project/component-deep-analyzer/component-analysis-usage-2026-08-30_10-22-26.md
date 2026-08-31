# Component Deep Analysis Report — `usage` (CostTracker)

## 1. Executive Summary

`zettel/usage.py` is a single-file, dependency-free component that provides **per-run and per-source LLM/embedding cost and token aggregation** for the entire Zettelkasten pipeline. It has no knowledge of *how* a cost is computed (that is `pricing.py`'s job, a pure calculator over LiteLLM's public price map) and no knowledge of *where* usage is persisted (that is `state.py`'s `StateDB.finish_run` / `StateDB.add_source_usage`, and `vault.py`'s `sync_source_costs_to_vault`). Its only responsibility is to be the **in-memory, thread/async-safe accumulator** that every pipeline stage writes into and every pipeline stage reads back from before persisting.

The design center is a trio of `contextvars.ContextVar` globals (`_tracker`, `_source_id`, `_progress`) that give each pipeline command (`harvest`, `extract`, `review`, `connect`, `garden`, `ask`, `article`) an isolated `CostTracker` instance for the lifetime of one `begin_run()` → `finish_pipeline_run()` bracket, without needing to thread a tracker object through every function signature in the call graph. Call sites reach the tracker either directly (`get_tracker()`) or via convenience module-level functions (`record_llm`, `record_embed`, `record_cache_hit`) that lazily create a tracker (`require_tracker()`) if none exists — this makes the component usable even from code paths that never call `begin_run()` explicitly (defensive default, e.g. ad-hoc scripts or tests).

Key findings:
- **Zero internal dependencies**: `usage.py` imports nothing from the rest of `zettel/` — every one of its 18 call sites imports it locally (function-scoped `from zettel.usage import ...`), never at module top-level. This is a deliberate, consistent convention across the codebase (confirmed in `assets.py`, `ask.py`, `connector.py`, `extractor.py`, `gardener.py`, `gardener_hub.py`, `harvester.py`, `index.py`, `llm.py`, `review.py`, `web_app.py`, `article.py`, `article_graph.py`, `bibliography.py`).
- **Persistence gap**: `UsageSummary` tracks `embed_calls`, `prompt_cache_read_tokens` and `prompt_cache_write_tokens`, and exposes them via `as_dict()`, but `StateDB.finish_run()` (state.py:1430) and `StateDB.add_source_usage()` (state.py:1465) only read `cost_usd_*`, `tokens_*`, `llm_calls` and `cache_hits` from that dict — the three other fields are silently dropped when the run/source row is written (no corresponding SQLite columns exist). Provider prompt-cache economics (a first-class concept elsewhere in the codebase, e.g. `apply_prompt_cache_hints` in `llm.py`) are visible only in the in-process `CostTracker`/`UsageEvent` and in log lines, never in `runs`/`sources` tables.
- **Dead code**: `UsageSummary.add()` (usage.py:57-68) is not called anywhere in the codebase (`_by_source` buckets are populated field-by-field inline in `record_llm`/`record_embed`/`record_cache_hit`, not via `.add()`).
- Cost visibility flows all the way to the user: CLI `zettel status` (cli.py:1722-1733) and the web dashboard (`get_web_dashboard()` in state.py, rendered by `dashboard.html`/`source_detail.html`) both read the persisted totals this component computed.

## 2. Data Flow Analysis

There are two independent producer paths (LLM calls, embedding upserts) that converge on the same `CostTracker`, and one consumer path (persistence) that every pipeline command triggers at its own boundary.

```
Producer path A — LLM call (any of: extractor, connector, gardener, ask, article, bibliography, assets/image-description)
  1. Pipeline command entry point calls usage.begin_run(run_id)          → fresh CostTracker bound to this context
  2. Loop over work items calls usage.set_source(source_id)              → tags subsequent events
  3. (optional) usage.set_progress(step, total, kind)                    → tags subsequent COST log lines
  4. llm.call_llm(...) invokes the LangChain client, extracts token usage via llm._extract_usage()
  5. llm.call_llm() calls pricing.estimate_llm_cost(model, in, out)      → USD via litellm.cost_per_token (0.0 for local/unknown models)
  6. llm.call_llm() calls usage.record_llm(...)                         → CostTracker.record_llm() appends a UsageEvent,
                                                                            updates _total and _by_source[sid], logs "COST llm ..."
  7. Cache-hit shortcut: if db.get_cached_llm_response() has a hit, callers call
     usage.record_cache_hit(...) INSTEAD of call_llm — no cost, cache_hits += 1
  8. At loop end, pipeline command reads usage.get_tracker().sources_touched() and
     summary_for_source(sid), and calls db.add_source_usage(sid, ...) + vault.sync_source_costs_to_vault(...)
  9. usage.finish_pipeline_run(db, run_id) → db.finish_run(run_id, status, tracker.summary().as_dict())
                                            → usage.log_run_summary() (INFO log)
                                            → usage.reset() (clears all 3 ContextVars)

Producer path B — Embedding upsert (index.py VectorIndex)
  1. VectorIndex.upsert_*() calls self._record_embed_usage(text, ...)
  2. pricing.estimate_embed_tokens(text) → chars//4 heuristic
  3. pricing.estimate_embed_cost(model, tokens, provider) → USD via litellm.cost_per_token (0.0 for local/unknown)
  4. usage.record_embed(...) → CostTracker.record_embed() → same _total/_by_source/log pattern as record_llm,
     but guarded: _record_embed_usage() first checks usage.get_tracker() is not None and no-ops if there is no
     active run (embeddings performed outside a begin_run bracket, e.g. ad-hoc reindex, are not costed)

Consumer path — Persistence & display
  10. db.finish_run() persists cost_usd_total/llm/embedding + tokens_prompt/completion/embedding + llm_calls/cache_hits
      onto the `runs` row (prompt_cache_* and embed_calls are computed but NOT columns on `runs` — dropped)
  11. db.add_source_usage() accumulates (COALESCE(...) + delta) the same subset of fields onto the `sources` row
  12. vault.sync_source_costs_to_vault() copies the now-updated `sources` row cost fields into the SRC note's
      YAML frontmatter (round(6) for USD, int for tokens)
  13. CLI `zettel status` reads db.get_last_run() and renders a Rich "Custo — Ultimo Run" table
  14. Web dashboard reads StateDB.get_web_dashboard()["runs"] / ["sources_cost"] and renders dashboard.html /
      source_detail.html
```

A second, finer-grained flow exists **inside** `connector.py::_process_candidate` (connector.py:234-304): it snapshots `tracker.summary().as_dict()` *before* and *after* generating one permanent note, diffs `cost_usd_llm`/`tokens_prompt`/`tokens_completion`, and logs a per-note cost line. This is a read-only consumption of the tracker's running total — it does not create a separate event or bucket, it is purely a delta computed by the caller from two point-in-time snapshots of `CostTracker.summary()`.

## 3. Business Rules & Logic

### Overview of the business rules:

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Isolation | Each `begin_run()` creates a brand-new `CostTracker` and resets source/progress context | usage.py:278-284 |
| Default/Fallback | `require_tracker()` lazily creates a tracker via `begin_run()` if none exists, so recording never raises | usage.py:291-295 |
| Attribution | An explicit `source_id` argument always overrides the ambient `_source_id` context value | usage.py:129, 189, 234 |
| Attribution | Per-source buckets (`_by_source`) are only created/updated when a truthy `source_id` is resolved | usage.py:151, 206, 246 |
| Progress tagging | Explicit `step`/`total`/`kind` arguments win over ambient `_progress` context; missing pieces fall back to context | usage.py:93-104 |
| Cost classification | LLM cost accrues to both `cost_usd_llm` and `cost_usd_total`; embedding cost accrues to both `cost_usd_embedding` and `cost_usd_total` — the two sub-totals are mutually exclusive by construction | usage.py:149-150, 204-205 |
| Cache accounting | A cache hit (`record_cache_hit`) records `cache_hits += 1` at `cost_usd=0.0` and does **not** increment `llm_calls` — cache hits and paid LLM calls are counted separately | usage.py:224-258 |
| Cost estimation | Local/self-hosted models (Ollama, `sentence-transformers`, `default`, or any `provider:tag`-shaped name) always cost `$0.0`, regardless of token counts | pricing.py:24-33, 54-55, 83 |
| Cost estimation | An unknown/unpriced model in LiteLLM's map costs `$0.0` (never raises) and logs a one-time warning per model name | pricing.py:62-73, 88-99, 102-109 |
| Cost estimation | Zero-token calls (`prompt_tokens == 0 and completion_tokens == 0`) short-circuit to `$0.0` without calling LiteLLM | pricing.py:58-59 |
| Provider-cache tracking | `cache_read_tokens`/`cache_write_tokens` (provider prompt-prefix cache, e.g. Anthropic `cache_control`) are tracked separately from SQLite's `llm_cache` deterministic-response cache (`cache_hits`) — two unrelated notions of "cache" coexist | usage.py:23-25, 39-40 |
| Lifecycle | `finish_pipeline_run()` always calls `reset()` after persisting, regardless of `status` passed in — no tracker/source/progress state survives past a pipeline command's boundary | usage.py:421-428 |
| Aggregation | `sources_touched()` + `summary_for_source(sid)` + `db.add_source_usage(sid, ...)` must be invoked **at most once per source per run** — `summary_for_source` returns the tracker's *cumulative* total for that source since `begin_run`, and `add_source_usage` performs an additive `COALESCE(...) + ?` on the SQLite row, so a second call for the same source within the same run would double-count | usage.py:263-264; state.py:1465-1487; connector.py:150-151; extractor.py:140-141; harvester.py:776-779 |

### Detailed breakdown of the business rules:
---

### Business Rule: Context-Isolated Run Lifecycle

**Overview**:
Every cost-tracked pipeline command follows a strict `begin_run(run_id)` → work → `finish_pipeline_run(db, run_id)` bracket. `begin_run` does three things atomically from the caller's point of view: it creates a fresh `CostTracker(run_id=run_id)`, and it resets both `_source_id` and `_progress` context vars to `None`, guaranteeing that no source attribution or progress tag leaks in from a previous run that happened to run in the same thread/task.

**Detailed description**:
The mechanism relies on Python's `contextvars.ContextVar`, which is safe across `asyncio` tasks and threads without explicit locking, but is *not* automatically isolated across sequential calls in the same context — a `ContextVar.set()` persists until explicitly changed. This is why `begin_run()` must proactively reset `_source_id` and `_progress`, and why `finish_pipeline_run()` must proactively call `reset()` (which clears `_tracker`, `_source_id`, and `_progress` together) — if either bracket were skipped, cost from a subsequent unrelated call could be misattributed to the previous run's tracker (if `reset()` is skipped) or to a stale source_id (if `begin_run()` didn't reset `_source_id`).

Every one of the 9 pipeline entry points that own a `CostTracker` lifecycle (`harvester.run_harvest`, `extractor.run_extract`, `review.run_review`, `connector.run_connect`, `gardener.run_garden`, `gardener_hub.run_garden_hubs`, `ask.run_ask`, `article_graph` article compilation, and the web worker's inline `review` operation in `web_app.py`) follows this exact bracket, each starting its own `db.start_run(...)` row first and passing that `run_id` into `begin_run`. This means `CostTracker.run_id` is always populated from a real `runs.run_id` foreign-key-shaped value, even though nothing in `usage.py` enforces the relationship (it's a convention followed by every caller, not a constraint in the dataclass).

`require_tracker()` provides a safety net for the (in practice, none-in-production) case where `record_llm`/`record_embed`/`record_cache_hit` module-level functions are called without an active `begin_run()` — it silently starts one. This means cost accounting is never destructive (it can't raise `AttributeError: NoneType`), but it also means a caller who forgets to call `begin_run()` still "succeeds" and silently gets a tracker whose totals are never persisted anywhere (no `run_id` is known to `finish_pipeline_run`, since nothing calls it), a debuggability trade-off favoring resilience over strictness.

**Rule workflow**:
1. Pipeline command calls `db.start_run(signature)` → obtains a SQLite `run_id`.
2. Pipeline command calls `usage.begin_run(run_id)` → new `CostTracker`, `_source_id=None`, `_progress=None`.
3. Work loop calls `set_source`/`set_progress` per item, and `record_llm`/`record_embed`/`record_cache_hit` per LLM/embedding event — all implicitly targeting the tracker created in step 2 via `get_tracker()`/`require_tracker()`.
4. Pipeline command calls `usage.finish_pipeline_run(db, run_id, status)` → persists `tracker.summary().as_dict()` onto the `runs` row, logs the aggregate summary, and calls `usage.reset()`.
5. Any code running after step 4 that calls `get_tracker()` gets `None`; any code calling `record_llm`/etc. after step 4 silently starts a brand-new, unpersisted tracker via `require_tracker()`.

---

### Business Rule: Source Attribution Priority (explicit argument over ambient context)

**Overview**:
Every recording method (`record_llm`, `record_embed`, `record_cache_hit`) resolves the event's `source_id` with `source_id if source_id is not None else get_source_id()` — an explicit keyword argument always wins over whatever `set_source(...)` last established in the ambient `_source_id` ContextVar.

**Detailed description**:
This dual-channel design exists because the codebase has two different attribution patterns depending on the pipeline stage. In loop-oriented stages (`extractor.run_extract`, `connector.run_connect`, `harvester._process_file`), the current item's `source_id` is known at the top of each loop iteration, so the code calls `set_source(source_id)` once per iteration and lets every downstream `call_llm`/`record_embed` call inherit it implicitly — this avoids threading `source_id` through every helper function signature between the loop and the eventual `usage.record_llm` call several stack frames down (e.g., through `call_llm` in `llm.py`, which has no `source_id` parameter at all and relies entirely on ambient context).

In contrast, callers that already have an explicit `source_id` value in hand at the exact call site (none currently pass one explicitly to `record_llm`/`record_embed` in the audited call sites — all 18 call sites rely on ambient `set_source`/`get_source_id()`) could bypass the context mechanism entirely. The explicit-argument path exists in the API primarily as a deliberate design affordance/escape hatch (documented via the `Optional[str] = None` default and the ternary resolution) for future or out-of-loop callers, not because any current caller exercises it.

A second nuance: `_by_source` is a `dict[str, UsageSummary]` keyed by `source_id`, and the bucket is only created/updated `if sid:` — a falsy `source_id` (i.e., `None` or `""`) means the event still counts toward `_total` (the run-wide summary) but is invisible in any `summary_for_source()` lookup and never reaches `sources_touched()`. This is intentional: not every LLM call in the system is source-scoped (e.g., `ask.run_ask` and `article_graph`'s article-writing calls have no natural `source_id` — a question or an article topic isn't a single source — so their cost only shows up in the `runs` table, never attributed to any row in `sources`).

**Rule workflow**:
1. Caller (loop body) calls `set_source(current_source_id)` before doing per-item work.
2. Deep call stack eventually reaches `usage.record_llm(...)` (via `llm.call_llm`) or `usage.record_embed(...)` (via `VectorIndex._record_embed_usage`) without passing `source_id`.
3. `record_llm`/`record_embed`/`record_cache_hit` resolve `sid = source_id if source_id is not None else get_source_id()` → picks up the ambient value from step 1.
4. If `sid` is truthy, the event also updates `_by_source[sid]` in addition to `_total`; if falsy, only `_total` is updated.
5. At loop end, caller calls `set_source(None)` to clear ambient attribution before the next unrelated phase of the same run (e.g., after the extract loop, before auto-approve).

---

### Business Rule: Cache Hits Are Zero-Cost and Counted Separately From LLM Calls

**Overview**:
`record_cache_hit()` always records `cost_usd=0.0` and increments only `cache_hits`, never `llm_calls`, `tokens_prompt`, or `tokens_completion`. This reflects a deterministic SQLite-level LLM response cache (`db.get_cached_llm_response(call_checksum)` / `db.cache_llm_response(...)`) that every prompt-driven stage (extractor, connector, ask, article, bibliography, assets image description) checks before calling `call_llm`.

**Detailed description**:
The checksum (`compute_llm_call_checksum` in `hashing.py`) is computed from the prompt template hash, the filled/chunk content hash, model, temperature, and language — so a cache hit means "this exact call, with this exact model/config, has already produced this exact response," and the pipeline can reuse the stored text with zero token cost and zero LLM calls counted. This is architecturally distinct from the LLM provider's own prompt-prefix caching (Anthropic `cache_control`, tracked as `cache_read_tokens`/`cache_write_tokens` on `UsageEvent`/`UsageSummary`) — the module's docstring for those fields explicitly calls this out: "Provider prompt-prefix cache (not SQLite llm_cache hits)."

Because `cache_hits` and `llm_calls` are separate counters, a caller (or a human reading `zettel status` / the web dashboard) can distinguish "how many times did we skip an LLM call entirely" from "how many times did we actually pay for one," which is the basis for re-run economics: re-running `extract` or `connect` after a partial failure re-processes the same chunks/candidates, but because the checksums match, the bulk of the re-run becomes `cache_hits` at `$0`, and only the previously-unprocessed tail incurs real `llm_calls` cost. This rule is explicitly exercised by `test_cache_hit_is_zero_cost` (tests/test_usage.py:64-70).

**Rule workflow**:
1. Caller computes `call_checksum` from prompt+content+model+temperature+language.
2. Caller calls `db.get_cached_llm_response(call_checksum)`.
3. If a cached response exists: caller calls `usage.record_cache_hit(label=..., source_id=..., model=...)` — `cache_hits += 1`, `cost_usd_total` unchanged, `llm_calls` unchanged — and reuses the cached text, skipping the LLM invocation entirely.
4. If no cached response exists: caller calls `llm.call_llm(...)`, which internally calls `usage.record_llm(...)` — `llm_calls += 1`, tokens and `cost_usd_llm`/`cost_usd_total` increase by the real amounts — and then caller persists the new response via `db.cache_llm_response(...)` for future cache hits.

---

### Business Rule: Local and Unpriced Models Always Cost $0.0 (pricing.py, consumed via usage.py's `cost_usd` inputs)

**Overview**:
`pricing.estimate_llm_cost` / `estimate_embed_cost` — the sole producers of the `cost_usd` values that flow into every `usage.record_llm`/`record_embed` call — return `0.0` whenever the model is judged "local" (`_is_local_model`) or when LiteLLM's `cost_per_token` raises (unknown/unpriced model), rather than raising an exception or returning `None`.

**Detailed description**:
`_is_local_model` treats a model name as local if it is empty, starts with `ollama/` or `ollama:`, matches the `provider:tag` shape without a `/` (e.g. `qwen3.5:4b`, a common Ollama naming convention), or is literally `sentence-transformers`/`default` (the two non-API embedding provider identifiers used elsewhere in `config.py`/`index.py`). This heuristic runs *before* any LiteLLM lookup, so self-hosted inference never even attempts a network-free-but-potentially-slow price lookup, and always short-circuits to `$0.0` deterministically. `estimate_llm_cost` additionally accepts an explicit `provider="ollama"` override that forces `$0.0` even if the model name itself doesn't match the local-name heuristic (defense in depth against a locally-hosted model that happens to be named like a hosted one).

For genuinely hosted-but-unpriced models (a brand-new or obscure model id that LiteLLM's price map doesn't recognize), `_warn_once` logs a warning the *first* time each distinct model name is seen (deduplicated via a module-level `_warned_models: set[str]`, resettable only for tests via `reset_price_warnings()`), then every subsequent call for that same model silently returns `$0.0` without re-logging. This means a misconfigured or newly-released model shows up in the logs exactly once per process lifetime, and thereafter silently under-reports cost as zero — the pipeline keeps running (no `UserFacingError`), but `zettel status`'s cost table becomes an under-count for that model without any further signal.

Downstream, `usage.py` treats every `cost_usd` value it receives as ground truth — it has no way to distinguish "this call genuinely cost $0.0002" from "this call's true cost is unknown and was floored to $0.0 by pricing.py." This coupling means any change to `pricing.py`'s local-model heuristic or its exception handling directly and silently changes what `CostTracker` reports as the "real" spend, with no validation or cross-check inside `usage.py` itself.

**Rule workflow**:
1. `call_llm` (or `_record_embed_usage`, or `assets._describe_one`) obtains real token counts from the provider response.
2. It calls `pricing.estimate_llm_cost(model, prompt_tokens, completion_tokens, provider=...)` (or `estimate_embed_cost`).
3. `pricing.py` checks `provider in ("ollama",)` or `_is_local_model(model)` → returns `0.0` immediately if true.
4. Otherwise it calls `litellm.cost_per_token(model=normalized_name, ...)`; on success, returns `prompt_cost + completion_cost`; on any exception, logs once via `_warn_once` and returns `0.0`.
5. The (possibly zero) cost value is passed straight into `usage.record_llm`/`record_embed` as `cost_usd`, and from there into every downstream sum (`_total`, `_by_source`, and eventually the `runs`/`sources` SQLite rows and SRC frontmatter).

---

### Business Rule: Per-Source Cost Persistence Must Happen Exactly Once Per Source Per Run

**Overview**:
`CostTracker.summary_for_source(source_id)` returns the *cumulative* total recorded for that source since the tracker was created by `begin_run()` — not a delta since the last time it was read. Every caller that persists per-source usage (`extractor.run_extract`, `connector.run_connect`, `harvester._process_file`) must therefore call `db.add_source_usage(sid, tracker.summary_for_source(sid).as_dict())` at most once per source within a single run, because `StateDB.add_source_usage` performs an *additive* SQL update (`cost_usd_total = COALESCE(cost_usd_total, 0) + ?`).

**Detailed description**:
`extractor.run_extract` and `connector.run_connect` both follow the same pattern: they run their entire work loop first (potentially touching many sources, each contributing 0+ events), and only *after* the loop ends do they call `tracker.sources_touched()` once and loop over the resulting source id list, calling `add_source_usage` exactly once per distinct source for that run. This is safe by construction because `sources_touched()` returns `list(self._by_source.keys())` — each key naturally appears once regardless of how many events were recorded for it during the loop.

`harvester._process_file`, however, calls `add_source_usage` *inline*, immediately after finishing one file's chunking (harvester.py:776-779), rather than batching at the end of the whole harvest run. This is safe as long as `_process_file` is invoked at most once per `source_id` within a single `run_harvest` execution — which holds under the observed three-layer duplicate detection (a file that resolves to an already-known `source_id` via file-hash or extraction-hash match takes an early-return path before reaching the LLM-cost-incurring paging/chunking logic, per the harvester's documented duplicate-detection layers) — but nothing in `usage.py` itself enforces or asserts this invariant; the guarantee lives entirely in the calling convention of `harvester.py` and would silently double-count source-level cost (though not run-level cost, since `_total` is unaffected) if that invariant were ever violated by a future code change.

**Rule workflow**:
1. Loop-batched pattern (extractor/connector): work loop runs to completion, mutating `_by_source[sid]` for every touched source as it goes; after the loop, `sources_touched()` is read once and `add_source_usage` is called once per source.
2. Inline pattern (harvester): immediately after one file/source finishes processing, `tracker.summary_for_source(source_id)` is read and `add_source_usage` is called for that source before moving to the next file — relies on each source being processed at most once per run.
3. In both patterns, `db.add_source_usage` performs `COALESCE(existing, 0) + delta` — meaning the *first* call for a given source in a run adds its full tracked total to whatever the source already had accumulated from *previous* runs (e.g., a prior `extract` run's LLM cost plus this run's embedding cost), which is the intended cross-run accumulation behavior on `sources.cost_usd_*`.

---

## 4. Component Structure

`usage.py` is a flat, single-file component with no subpackage structure of its own. Its logical sections (in file order) are:

```
zettel/
├── usage.py                    # THE COMPONENT — contextvar-based cost/token aggregation
│   ├── UsageEvent               # dataclass: one recorded llm|embed|cache_hit event (line 13)
│   ├── UsageSummary              # dataclass: aggregated totals + as_dict()/add() (line 28)
│   ├── format_progress /         # pure string helpers for "kind step/total" progress tags
│   │   format_progress_from_context / _progress_tag  (lines 71-104)
│   ├── CostTracker                # dataclass: events list + _total + _by_source dict;
│   │                               #   record_llm / record_embed / record_cache_hit / summary*
│   │                               #   (lines 107-267)
│   ├── module ContextVars: _tracker, _source_id, _progress (lines 270-275)
│   ├── lifecycle functions: begin_run / get_tracker / require_tracker / reset (lines 278-295, 319-322)
│   ├── context setters: set_source / get_source_id / set_progress / clear_progress (lines 298-316)
│   ├── module-level convenience wrappers: record_llm / record_embed / record_cache_hit
│   │   (delegate to require_tracker(), lines 325-393)
│   └── log_run_summary / finish_pipeline_run  (persistence + logging entry point, lines 396-428)
│
├── pricing.py                   # SIBLING/COLLABORATOR — pure cost calculator, no state, no zettel imports
│   ├── estimate_embed_tokens     # chars//4 heuristic
│   ├── _is_local_model / _normalize_model
│   ├── estimate_llm_cost / estimate_embed_cost   # LiteLLM cost_per_token wrapper, $0 on local/unknown
│   └── _warn_once / reset_price_warnings
│
└── (18 consumer modules — see Dependency Analysis)
```

There is no `__init__.py` re-export or package boundary beyond the two flat modules; "the component" for this analysis is `usage.py`, with `pricing.py` documented alongside it as its closest, functionally-inseparable collaborator (every `cost_usd` value `usage.py` ever stores originates from a `pricing.py` call performed by the caller just before recording).

## 5. Dependency Analysis

```
Internal Dependencies (usage.py's own imports):
  usage.py → (stdlib only: logging, contextvars.ContextVar, dataclasses, typing) — ZERO imports from
             any other zettel/* module. This is a leaf component in the internal dependency graph.

Internal Dependencies (consumers → usage.py), all via function-scoped imports:
  llm.call_llm()                          → usage.record_llm
  index.VectorIndex._record_embed_usage() → usage.get_tracker, usage.record_embed
  harvester.run_harvest()                 → usage.begin_run, usage.finish_pipeline_run
  harvester._process_file()               → usage.get_tracker, usage.set_source
  extractor.run_extract()                 → usage.begin_run, usage.finish_pipeline_run, usage.get_tracker, usage.set_source
  extractor._process_chunk()              → usage.clear_progress, usage.set_progress, usage.record_cache_hit
  review.run_review()                     → usage.begin_run, usage.finish_pipeline_run
  connector.run_connect()                 → usage.begin_run, usage.finish_pipeline_run, usage.get_tracker, usage.set_source
  connector._process_candidate()          → usage.clear_progress, usage.set_progress, usage.get_tracker,
                                             usage.record_cache_hit, usage.format_progress_from_context
  gardener.run_garden()                   → usage.begin_run, usage.finish_pipeline_run
  gardener_hub.run_garden_hubs()          → usage.begin_run, usage.finish_pipeline_run
  ask.run_ask()                           → usage.begin_run, usage.finish_pipeline_run, usage.record_cache_hit
  article_graph (compile/run)             → usage.begin_run, usage.finish_pipeline_run
  article.py (per-section LLM cache hit)  → usage.record_cache_hit
  bibliography.py (ABNT LLM cache hit)    → usage.record_cache_hit
  assets.py (image description)           → usage.clear_progress, usage.set_progress, usage.record_cache_hit, usage.record_llm
  web_app.py (inline "review" operation)  → usage.begin_run, usage.finish_pipeline_run

  Downstream consumers of usage.py's OUTPUT (not usage.py itself, but the data it produces):
  vault.sync_source_costs_to_vault()      → reads db.get_source(sid)'s cost_usd_*/tokens_* (populated via
                                             usage.CostTracker → StateDB.add_source_usage)
  state.StateDB.finish_run() / add_source_usage() → persist usage.CostTracker.summary().as_dict() /
                                             summary_for_source(sid).as_dict()
  cli.py `zettel status`                  → renders db.get_last_run() cost fields
  web dashboard (get_web_dashboard)       → renders "sources_cost" / "runs" cost fields

External Dependencies:
  - Python stdlib `contextvars`  — core isolation mechanism, no version dependency beyond Python 3.7+
  - Python stdlib `dataclasses`, `logging`, `typing` — no version dependency
  - (transitively, via pricing.py, NOT via usage.py directly) `litellm` — public price map (`cost_per_token`),
    used only as a calculator; usage.py has no direct import of litellm
```

## 6. Afferent and Efferent Coupling

Component granularity here is per-class/module-function-group within `usage.py`, since this is a Python module rather than an OOP-heavy component.

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|-------------------|-------------------|
| `usage.py` (module, aggregate) | 18 (distinct importing modules) | 0 (no internal `zettel.*` imports; stdlib only) | High |
| `CostTracker` (class) | ~14 call sites across 11 modules (direct `.record_*`/`.summary*`/`.sources_touched` use) | 0 (self-contained; only touches its own dataclass fields) | High |
| module-level `record_llm`/`record_embed`/`record_cache_hit` wrappers | 7 direct call sites (assets, ask, extractor, article, bibliography — via `require_tracker()`) | 1 (`require_tracker` → may call `begin_run`) | Medium |
| `begin_run` / `finish_pipeline_run` | 9 pipeline entry points (harvest, extract, review, connect, garden, garden-hubs, ask, article, web review op) | 1 (`finish_pipeline_run` → `db.finish_run`, an external `StateDB` call — the only place usage.py touches another component's API) | High |
| `set_source` / `get_source_id` | 3 modules set (harvester, extractor, connector); read internally by `record_*` methods only | 0 | Medium |
| `set_progress` / `clear_progress` / `format_progress_from_context` | 3 modules (assets, extractor, connector) | 0 | Low |
| `UsageSummary` (dataclass) | Constructed/read in every `CostTracker` method plus by all 9 pipeline entry points via `.summary()`/`.as_dict()` | 0 | High |
| `pricing.py` (collaborator, not part of `usage.py` itself) | Called from `llm.py`, `index.py`, `assets.py` immediately before every `usage.record_llm`/`record_embed` call | 1 (`litellm.cost_per_token`, imported lazily inside function bodies) | High |

`usage.py` itself has **zero efferent coupling** to the rest of the codebase (a leaf/sink node), which is the strongest structural property of this component: it can be reasoned about, tested, and modified without needing to understand any other `zettel/*` module's internals. Its **afferent coupling is broad but shallow** — 18 files import it, but each import is scoped to a handful of narrow, side-effect-documented function calls (record/set/get), not to any internal state or class hierarchy.

## 7. Integration Points

`usage.py` does not expose network, CLI, or web endpoints itself. Its integration surface is purely intra-process (Python function calls) plus the two persistence hand-offs it triggers.

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|----------------|
| `zettel.pricing` | Internal collaborator (sibling module) | Supplies the `cost_usd` value passed into every `record_llm`/`record_embed` call | Direct Python call | `float` (USD) | `pricing.py` swallows all `litellm` exceptions internally and returns `0.0`; `usage.py` never sees or handles a pricing exception |
| `zettel.state.StateDB` | Internal persistence sink | `finish_pipeline_run()` calls `db.finish_run(run_id, status, usage_dict)`; callers separately call `db.add_source_usage(sid, delta_dict)` | Direct Python call | `dict[str, Any]` (subset of `UsageSummary.as_dict()` keys) | No try/except in `usage.py` around the `db.finish_run` call — a `StateDB` exception propagates uncaught through `finish_pipeline_run` |
| `zettel.vault.sync_source_costs_to_vault` | Downstream consumer (not called by usage.py) | Mirrors the now-persisted `sources` row cost fields onto SRC note YAML frontmatter | Direct Python call, invoked by the *pipeline caller* (extractor/connector), not by usage.py | Frontmatter dict keys (`cost_usd_total`, etc.) | Returns `False` on missing file rather than raising |
| `logging` (`logger = logging.getLogger(__name__)`, i.e. `zettel.usage`) | Observability | Every `record_llm`/`record_embed`/`record_cache_hit` call and `log_run_summary()` emit one INFO-level `COST ...` line | Python `logging` | Structured-ish single-line text (`"COST llm [prog] model=%s in=%d out=%d usd=%.6f label=%s source=%s%s"`) | None needed (logging never raises in normal operation) |

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Context Object / Ambient Context | `_tracker`, `_source_id`, `_progress` `ContextVar`s | usage.py:270-275 | Makes the active `CostTracker` and current source/progress implicitly available to deeply-nested call stacks (e.g. `llm.call_llm`) without threading parameters through every intermediate function |
| Facade | Module-level `record_llm`/`record_embed`/`record_cache_hit`/`begin_run`/`get_tracker`/`reset` functions | usage.py:278-393 | Gives callers a flat function-call API instead of requiring them to obtain and hold a `CostTracker` instance themselves |
| Null Object / Lazy Default | `require_tracker()` auto-creates a tracker via `begin_run()` if none is active | usage.py:291-295 | Prevents `NoneType` errors for callers that record usage without an explicit `begin_run()` bracket |
| Snapshot Diffing | Caller captures `tracker.summary().as_dict()` before/after a unit of work and subtracts to get a per-item delta | connector.py:241-242, 286-291 | Attributes cost to one permanent note without `CostTracker` needing a native "sub-scope" concept |
| Aggregator / Accumulator | `CostTracker._total` and `_by_source` dict, both incrementally updated in `record_*` | usage.py:107-267 | Two-level rollup (run-wide + per-source) from a flat event stream |
| Value Object / DTO | `UsageEvent`, `UsageSummary` dataclasses | usage.py:13-68 | Immutable-by-convention (though not frozen) records passed to loggers and persistence layers |
| Strategy-by-convention (pricing) | `pricing.estimate_llm_cost`/`estimate_embed_cost` return `0.0` for local/unknown models instead of raising | pricing.py:46-99 | Lets the pipeline keep running with any LLM backend (hosted or local) without cost-tracking becoming a hard dependency on price-map coverage |
| Deferred/Lazy Import | Every one of the 18 consumer call sites imports `zettel.usage` inside a function body, never at module top level | all consumer files, e.g. connector.py:106 | Project-wide convention (also applied to `pricing`, `vault`, and other cross-cutting modules) to avoid import cycles and to keep module-load-time side effects minimal |

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|----------------|-------|--------|
| Medium | Persistence gap | `UsageSummary.embed_calls`, `prompt_cache_read_tokens`, `prompt_cache_write_tokens` are computed and exposed via `as_dict()` but have no corresponding columns in the `runs`/`sources` SQLite schema (state.py:1430-1487) — `StateDB.finish_run`/`add_source_usage` silently ignore these three keys | Provider prompt-cache savings/spend (a first-class feature elsewhere, e.g. `apply_prompt_cache_hints` in llm.py) is invisible in `zettel status`, the web dashboard, and SRC frontmatter — only visible in transient logs |
| Low | Dead code | `UsageSummary.add(other)` (usage.py:57-68) is never called anywhere in the codebase; all `_by_source` aggregation is done field-by-field inline in `CostTracker.record_llm/record_embed/record_cache_hit` | Unused surface area to maintain/test; a future refactor toward "merge two summaries" could silently rely on unverified behavior |
| Medium | Implicit invariant, no runtime guard | `db.add_source_usage(sid, tracker.summary_for_source(sid).as_dict())` must be called at most once per source per run because `summary_for_source` returns a cumulative (not delta) total and `add_source_usage` performs an additive SQL update; this invariant is enforced only by each caller's control flow (batched-once in extractor/connector, inline-once-per-file in harvester), not by `CostTracker` itself | A future code change that calls this pattern twice for the same source within one run (e.g. a retry loop, a resumed/incremplete-source completion path) would silently double-count that source's cost/tokens in SQLite and, downstream, in the SRC note frontmatter |
| Low | Silent cost under-reporting | `pricing.py`'s `_warn_once` logs an unpriced-model warning exactly once per process lifetime per model name, then permanently returns `$0.0` for that model for the rest of the process | A newly-released/misconfigured hosted model's true cost is silently reported as `$0.0` in every subsequent `usage.record_llm` call after the first, with no recurring signal to notice the gap |
| Low | Ambient-state footgun | Because `set_source`/`set_progress` mutate ContextVars rather than being scoped (no context-manager `with` form), a caller that raises an exception mid-loop before calling `set_source(None)` leaves stale source attribution active for any code that runs afterward in the same context, until the next `begin_run()` (which does reset `_source_id`) — the risk window is any code between the exception and the next `begin_run()` | Misattributed cost/tokens to the wrong source if an unrelated `record_llm`/`record_embed` call happens (e.g. in an `except`/`finally` cleanup path) before the next run begins |
| Info | Coupling by convention, not by type | `finish_pipeline_run(db: Any, run_id: int, ...)` types `db` as `Any` and calls `db.finish_run(...)` duck-typed — no `StateDB` import/type hint in `usage.py` | Keeps `usage.py` dependency-free (a deliberate positive), but means a typo or interface drift in `StateDB.finish_run`'s signature would only surface at runtime, not via static typing, from this call site |

## 10. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|--------------------|----------|---------------|
| `CostTracker` (`record_llm`/`record_embed`/`record_cache_hit`, `summary`/`summary_for_source`) | 3 (`tests/test_usage.py`: `test_tracker_aggregates_by_source`, `test_cost_log_includes_progress`, `test_cache_hit_is_zero_cost`) | 0 dedicated | Good for the core aggregation and per-source bucketing; per-source cost math and cross-source isolation are directly asserted | Good assertions on totals, per-source sums, and cache-hit zero-cost invariant; uses `caplog` to assert the `COST llm [chunk 3/10]` progress-tag format. Missing: no test exercises `cache_read_tokens`/`cache_write_tokens` accumulation on `CostTracker` directly (only indirectly via `test_call_llm_sends_system_and_human` in test_prompt_cache.py), no test for `sources_touched()`, no test for `require_tracker()`'s auto-create-on-missing behavior, no test for `UsageSummary.add()` (dead code, untested), no test for `reset()` clearing all three ContextVars, no test for explicit `source_id` argument overriding ambient context |
| `llm.call_llm` → `usage.record_llm` integration | 1 (`tests/test_llm_usage.py::test_call_llm_records_usage_and_cost`) | Effectively integration-level (mocks `pricing.estimate_llm_cost`, exercises real `call_llm` → `record_llm` path) | Covers the primary production path (LLM call → usage recording) with a fake LangChain-shaped LLM | Good: verifies model/prompt/completion token pass-through and cost association end-to-end through a real (non-mocked) `usage.py`. Missing: no equivalent integration test for `index.VectorIndex._record_embed_usage` → `usage.record_embed`, and no test for the `record_cache_hit` path from any real pipeline module (extractor/connector/ask) |
| `usage.begin_run`/`finish_pipeline_run` lifecycle | 0 direct | 5 indirect (`tests/test_review.py`: `test_review.py:297,317,336,352,367` all `patch("zettel.usage.begin_run")` and `patch("zettel.usage.finish_pipeline_run")` around `review.py` operations) | Weak — the lifecycle functions are mocked out (not exercised) in every test that touches a pipeline command's cost bracket | These patches prove the *pipeline* code calls `begin_run`/`finish_pipeline_run` at the right points, but tell us nothing about `usage.py`'s own `begin_run`/`finish_pipeline_run` correctness (e.g., whether `reset()` is actually called, whether the persisted dict is well-formed) — that correctness is only covered indirectly by `tests/test_state.py:137` (`db.finish_run(run_id, "completed")` called directly, bypassing `usage.py` entirely) |
| `usage.format_progress` / `format_progress_from_context` / `_progress_tag` | 1 indirect (`test_cost_log_includes_progress` asserts the rendered log string, exercising `_progress_tag`/`format_progress` transitively) | 0 | Adequate for the common case | No test for `format_progress` with `total=None` (the `"{prefix}{step}"` branch), no test for `format_progress_from_context()` returning `""` when no progress is set, no direct unit test isolated from the logging side-effect |
| `pricing.py` (tightly-coupled collaborator) | 5 (`tests/test_pricing.py`: token-estimate ratio/minimum, LiteLLM-backed cost happy path, unknown-model zero-cost, local-provider zero-cost for both LLM and embed) | 0 | Good coverage of the branching logic (local vs. hosted vs. unknown model) | Good use of `patch("litellm.cost_per_token", ...)` to avoid a real network/price-map dependency in tests; `reset_price_warnings()` called in `setup_function` to prevent cross-test warning-suppression leakage. Missing: no test asserts the *content* of the one-time warning log, no test for `_normalize_model`'s `openai/`/`anthropic/` prefix handling in isolation, no test for the `provider="ollama"` override path on `estimate_llm_cost` combined with a non-local-looking model name |
| Prompt-cache token fields (`cache_read_tokens`/`cache_write_tokens`) on `call_llm`→`usage.record_llm` | 1 (`tests/test_prompt_cache.py::test_call_llm_sends_system_and_human`, asserts `s.prompt_cache_read_tokens == 80` / `== 20` on the tracker after a call) | 0 | Adequate for the one happy-path shape (`input_token_details` with `cache_read`/`cache_creation` keys) | No test for the alternate `response_metadata`/`token_usage`/`prompt_tokens_details.cached_tokens` extraction shape (covered for plain `prompt_tokens`/`completion_tokens` in `test_extract_usage_openai_cached_tokens`, but not combined with an end-to-end `usage.record_llm` assertion the way the Anthropic-shaped test does) |

No test file directly exercises `StateDB.add_source_usage`'s additive-COALESCE behavior in combination with `usage.CostTracker.summary_for_source` (the double-counting risk noted in Section 9 is untested in either direction — neither a regression test proving single-call safety nor a test demonstrating the double-count failure mode exists).

---

**Report metadata**: Component analyzed: `usage` (`zettel/usage.py`, `CostTracker`), with `zettel/pricing.py` documented as its inseparable cost-calculation collaborator. Scope: `D:/projetos/zettel_app`, excluding `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, `.pytest_cache`.
