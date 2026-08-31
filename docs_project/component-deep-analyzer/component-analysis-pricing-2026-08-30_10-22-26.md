# Component Deep Analysis Report — `pricing`

## 1. Executive Summary

`zettel/pricing.py` is a small, single-purpose, side-effect-free **calculator module**. Its entire responsibility is estimating the USD cost of LLM chat completions and embedding calls, using LiteLLM's public static price map (`litellm.cost_per_token`) as the sole pricing source. It is explicitly **not** an LLM client and does not perform any network I/O, routing, or retries — that is the job of `llm.py` (chat completions) and `index.py` (embeddings).

The module exposes five public functions (`estimate_embed_tokens`, `estimate_llm_cost`, `estimate_embed_cost`, `reset_price_warnings`, plus the module-level warning cache) and two private helpers (`_is_local_model`, `_normalize_model`, `_warn_once`). It has exactly three internal consumers in the codebase — `zettel/llm.py` (`call_llm`), `zettel/assets.py` (`_describe_one`, image-description LLM calls), and `zettel/index.py` (`VectorIndex._record_embed_usage`) — all of which feed the computed `cost_usd` into `zettel/usage.py`'s `CostTracker` via `record_llm`/`record_embed`. `pricing.py` itself never imports `usage.py`; the pricing and accounting concerns are cleanly separated, with the three consumer sites acting as the composition point.

Key findings:
- The module maintains **zero external state** except a process-lifetime `set[str]` of models it has already warned about (`_warned_models`), used purely to avoid log spam.
- It hard-codes **no prices**. All USD figures come from whatever version of `litellm` is installed; upgrading the dependency is the only way to refresh prices (stated explicitly in the module docstring and `requirements.txt:18`).
- It fails **safe, not loud**: any error talking to LiteLLM's price map (unknown model, network-independent internal lookup failure, etc.) is swallowed and the call is priced at `$0.00`, with a one-time warning per model name.
- Local/self-hosted models (Ollama, SentenceTransformers) are short-circuited to `$0.00` before ever touching LiteLLM, both by explicit `provider` argument and by heuristic pattern-matching on the model string itself.
- Test coverage (`tests/test_pricing.py`) exercises the four primary behaviors (token estimation, successful LiteLLM lookup, unknown-model fallback, local-provider short-circuit) but leaves several edge cases and the private helpers untested directly (see § 11).

## 2. Data Flow Analysis

Two independent cost-estimation flows exist, both converging on the same `CostTracker`. Neither flow originates inside `pricing.py` itself — it is a leaf/utility module invoked by two upstream call sites.

**Flow A — Chat completion cost (text or vision LLM call):**
```
1. Pipeline code (harvester/extractor/connector/gardener/ask/article) calls llm.call_llm(...)
   or assets._describe_one(...) for image description
2. call_llm() / _describe_one() invokes the LangChain chat model (llm.invoke(messages))
3. Response usage metadata extracted via llm._extract_usage() -> prompt/completion/cache tokens
4. pricing.estimate_llm_cost(model_name, prompt_tokens, completion_tokens, provider=provider)
   4a. Local-model / local-provider short circuit -> 0.0 (no LiteLLM call)
   4b. Zero-token short circuit -> 0.0 (no LiteLLM call)
   4c. Model name normalized (pricing._normalize_model)
   4d. litellm.cost_per_token(model=name, prompt_tokens=.., completion_tokens=..)
   4e. On exception -> pricing._warn_once() logs once, returns 0.0
5. usage.record_llm(model=.., tokens_in=.., tokens_out=.., cost_usd=<result of step 4>, ...)
6. CostTracker aggregates into per-run and per-source UsageSummary; logs a COST line
7. On pipeline completion, usage.finish_pipeline_run() persists totals onto the runs row (state.py)
   and SRC/ZTL frontmatter is synced with sync_source_costs_to_vault (vault.py)
```

**Flow B — Embedding cost (ChromaDB upsert):**
```
1. VectorIndex.upsert_source / upsert_chunk / upsert_permanent_note / upsert_literature_note /
   upsert_moc / query_notes / query_chunks calls self._record_embed_usage(text, ...)
2. _record_embed_usage() early-returns if usage.get_tracker() is None (no active run context)
3. pricing.estimate_embed_tokens(text) -> rough token count (chars // 4, min 1)
4. pricing.estimate_embed_cost(self.embedding_model, tokens, provider=self.embedding_provider)
   4a. Local-provider ("ollama"/"sentence-transformers") or local-model heuristic -> 0.0
   4b. tokens <= 0 -> 0.0
   4c. Model name normalized
   4d. litellm.cost_per_token(model=name, prompt_tokens=tokens, completion_tokens=0)
   4e. On exception -> _warn_once() logs once, returns 0.0
5. usage.record_embed(model=.., tokens=.., cost_usd=<result of step 4>, ...)
6. CostTracker aggregates into per-run and per-source UsageSummary; logs a COST line
```

Both flows are read-only with respect to `pricing.py`'s own state except for the `_warned_models` mutation on failure, and both are purely computational — no filesystem, network (beyond the in-process `litellm` price-table lookup, which is not a network call), or database access happens inside the module.

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Calculation | Embedding token estimate = `max(1, len(text) // 4)`, or `0` for empty text | `zettel/pricing.py:17-21` |
| Validation/Guard | Local models/providers are always priced at `$0.00`, never queried against LiteLLM | `zettel/pricing.py:24-33`, `54-55`, `83-84` |
| Business Logic | Model name normalization strips `openai/` prefix but preserves `anthropic/` prefix before pricing lookup | `zettel/pricing.py:36-43` |
| Business Logic | Chat completion cost = `prompt_cost + completion_cost` from `litellm.cost_per_token` | `zettel/pricing.py:60-70` |
| Business Logic | Embedding cost = `prompt_cost` only (`completion_tokens=0` always passed) | `zettel/pricing.py:91-96` |
| Validation | Negative/`None` token counts are coerced to `0` before any cost math | `zettel/pricing.py:56-57` |
| Validation | Zero prompt + zero completion tokens short-circuits to `$0.00` without calling LiteLLM | `zettel/pricing.py:58-59` |
| Validation | Non-positive embedding token count short-circuits to `$0.00` without calling LiteLLM | `zettel/pricing.py:85-86` |
| Resilience | Any exception from LiteLLM's price lookup (e.g. unknown model) degrades to `$0.00` cost, never raises | `zettel/pricing.py:71-73`, `97-99` |
| Resilience | Cost-lookup failures are warned exactly once per distinct (normalized) model name per process | `zettel/pricing.py:102-109` |
| Test Support | Warning de-duplication state can be reset (used only in test fixtures) | `zettel/pricing.py:112-114` |
| Architectural Constraint | No local price table is maintained; all prices derive from the installed `litellm` package version | `zettel/pricing.py:1-5`, `requirements.txt:18` |

### Detailed breakdown of the business rules

---

### Business Rule: Embedding token estimation heuristic

**Overview**:
`estimate_embed_tokens(text)` (pricing.py:17-21) provides a deliberately rough token count for embedding-cost purposes, computed as `max(1, len(text) // 4)`, with an explicit exception that empty text returns `0`.

**Detailed description**:
This is a coarse approximation, not a tokenizer call — no BPE/tiktoken/model-specific vocabulary is consulted. The 4-characters-per-token ratio is a widely used rule of thumb for English/Latin-script text and is "good enough" for cost *estimation* rather than billing reconciliation; the module's own docstring on the function calls it a "rough token estimate." The `max(1, ...)` floor ensures that any non-empty string, however short, is never priced as `0` tokens (which would make the eventual cost `0.0` even though a real embedding call would consume at least one token and incur non-zero cost against a paid provider) — except for the empty-string case, which is special-cased to `0` because upserting empty content should not spuriously attribute cost.

This function is the sole token-counting mechanism feeding Flow B (embedding cost, § 2). It is called once per text unit immediately before `estimate_embed_cost` in `VectorIndex._record_embed_usage` (index.py:714), for every source summary, chunk, permanent note, literature note, MOC summary, and every query text issued against `query_notes`/`query_chunks`. Because it undercounts or overcounts token usage relative to the embedding provider's actual tokenizer, the resulting cost figures recorded in `runs`/`sources` and shown in the CLI/web dashboards are approximations by design — this is consistent with the module's charter as a cost *estimator*, not a billing-grade accounting system.

**Rule workflow**:
```
text == "" ?
  yes -> return 0
  no  -> return max(1, len(text) // 4)
```

---

### Business Rule: Local model / local provider zero-cost short-circuit

**Overview**:
Any model or provider identified as "local" (self-hosted, no metered billing) is priced at exactly `$0.00` and never reaches the LiteLLM price lookup, via `_is_local_model()` (pricing.py:24-33) combined with explicit `provider` checks in `estimate_llm_cost` (pricing.py:54) and `estimate_embed_cost` (pricing.py:83).

**Detailed description**:
`_is_local_model(model)` treats a model string as local under any of these conditions: it is empty/`None` (defaults to local — a conservative choice that avoids ever attempting to price an unknown/blank model name); it starts with `ollama/` or `ollama:`; it contains a colon but no slash (the pattern used by Ollama tags such as `qwen3.5:4b`, distinguishing them from slash-delimited hosted-provider identifiers like `openai/gpt-4o`); or it is literally `"sentence-transformers"` or `"default"`. This heuristic-based detection exists as a second line of defense independent of the `provider` argument, because callers do not always pass an explicit provider (e.g. legacy call sites, or `model` strings that already self-identify as local).

In addition, `estimate_llm_cost` treats `provider == "ollama"` as an unconditional zero-cost signal, and `estimate_embed_cost` treats `provider in ("ollama", "sentence-transformers")` the same way — matching `EmbeddingConfig.provider`'s `Literal["openai", "sentence-transformers", "ollama"]` constraint in `config.py:37`. This dual mechanism (explicit provider check OR heuristic model-string check) means a caller can either pass a correct `provider` value or rely on a recognizably-local `model` string, and either is sufficient to avoid a LiteLLM lookup — and, more importantly, avoid attributing a nonzero dollar cost to infrastructure that is not actually billed per-token. This rule directly affects cost dashboards and per-source cost totals: sources/runs processed entirely with local models will show `$0.00` LLM/embedding cost, which is correct behavior for self-hosted inference but means the pipeline provides no cost signal at all for infrastructure/compute costs of running local models — only for metered API costs.

**Rule workflow**:
```
estimate_llm_cost(model, prompt_tokens, completion_tokens, provider=None):
  if provider == "ollama" OR _is_local_model(model):
      return 0.0
  ... continue to LiteLLM lookup ...

estimate_embed_cost(model, tokens, provider=None):
  if provider in ("ollama", "sentence-transformers") OR _is_local_model(model):
      return 0.0
  ... continue to LiteLLM lookup ...

_is_local_model(model):
  m = model.strip().lower() (or "" if None)
  return (m == "")
      or m.startswith("ollama/")
      or m.startswith("ollama:")
      or ("/" not in m and ":" in m)
      or m in {"sentence-transformers", "default"}
```

---

### Business Rule: Model name normalization before pricing lookup

**Overview**:
`_normalize_model(model)` (pricing.py:36-43) strips a leading `openai/` prefix but explicitly preserves a leading `anthropic/` prefix, passing the result to `litellm.cost_per_token` as the `model` argument.

**Detailed description**:
LiteLLM's internal price map keys models inconsistently across providers: some entries are stored under a bare name (`gpt-4o-mini`) while others require a provider-prefixed key (`anthropic/claude-...`). The comment in the code ("Strip provider prefixes LiteLLM sometimes needs inverted") documents that this asymmetry is a known quirk of the LiteLLM price table rather than an application-level design choice — the function exists purely to route around that inconsistency. Any other prefix (e.g. `gemini/`, `ollama/`, a raw bare name) passes through unchanged.

This rule is applied identically in both `estimate_llm_cost` (pricing.py:61) and `estimate_embed_cost` (pricing.py:87), so any model naming convention used elsewhere in the codebase (`llm.py`'s `get_llm`, which resolves provider strings like `openai`, `anthropic`, `ollama`, `gemini`, and OpenAI-compatible aliases like `openrouter`/`opencode`/`azure`/`compatible`) is fed through the same normalization before being priced. Because the normalization is a narrow, two-case special rule (only `openai/` is stripped) rather than a general-purpose provider-prefix stripper, any future LiteLLM price-map naming inconsistency for a *different* provider prefix would silently fall through to the "pass unchanged" branch and could produce a lookup miss (caught by the exception handler and priced at `$0.00` — see next rule) rather than a normalization fix.

**Rule workflow**:
```
_normalize_model(model):
  m = model.strip() (or "" if None)
  if m.startswith("openai/"):
      return m without the "openai/" prefix
  if m.startswith("anthropic/"):
      return m unchanged (prefix explicitly preserved)
  return m unchanged
```

---

### Business Rule: Chat completion cost = sum of prompt and completion cost

**Overview**:
`estimate_llm_cost` (pricing.py:46-73) computes the total USD cost of a chat completion as `prompt_cost + completion_cost`, both returned by a single `litellm.cost_per_token(model=name, prompt_tokens=.., completion_tokens=..)` call.

**Detailed description**:
This is the module's primary billable-cost calculation for text (and vision, via `assets.py`) LLM calls. Before reaching the LiteLLM call, token counts are defensively sanitized: `prompt_tokens = max(0, int(prompt_tokens or 0))` and the same for `completion_tokens` (pricing.py:56-57), guarding against `None`, negative, or non-integer-but-numeric inputs from upstream usage-metadata extraction (`llm._extract_usage`). If both sanitized counts are `0`, the function returns `0.0` immediately without calling LiteLLM at all (pricing.py:58-59) — this avoids an unnecessary lookup for calls where no billable usage metadata was available (e.g. a provider that doesn't report token counts).

When the LiteLLM call succeeds, both `prompt_cost` and `completion_cost` are individually coerced with `float(x or 0.0)` before summing (pricing.py:70), guarding against `None` being returned for one leg of the tuple (e.g. a model priced only for completion tokens, or vice versa) — without this guard, `None + float` would raise `TypeError` and be caught by the broad `except Exception` anyway, but the explicit coercion keeps a partial price (only prompt or only completion known) informative rather than zeroing the whole call. This rule is invoked from two call sites — `llm.call_llm` for standard text completions and `assets._describe_one` for image-description completions — both of which pass the `provider` argument through so the local-model short-circuit (previous rule) applies uniformly to both text and vision paths.

**Rule workflow**:
```
estimate_llm_cost(model, prompt_tokens, completion_tokens, provider=None):
  if provider == "ollama" or _is_local_model(model): return 0.0
  prompt_tokens = max(0, int(prompt_tokens or 0))
  completion_tokens = max(0, int(completion_tokens or 0))
  if prompt_tokens == 0 and completion_tokens == 0: return 0.0
  name = _normalize_model(model)
  try:
      prompt_cost, completion_cost = litellm.cost_per_token(model=name, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
      return float(prompt_cost or 0.0) + float(completion_cost or 0.0)
  except Exception as exc:
      _warn_once(name, exc)
      return 0.0
```

---

### Business Rule: Embedding cost uses prompt-side pricing only

**Overview**:
`estimate_embed_cost` (pricing.py:76-99) always calls `litellm.cost_per_token` with `completion_tokens=0` and uses only the returned prompt-side cost, discarding the completion-side value entirely (`prompt_cost, _ = ...`).

**Detailed description**:
Embedding models do not have a "completion" leg — they consume input tokens and return a vector, not generated text — so this is a semantically correct application of the same underlying LiteLLM pricing function used for chat completions. The choice to reuse `cost_per_token` (a chat-completion-shaped API) for embeddings rather than a dedicated embedding-pricing function is a deliberate simplification: LiteLLM's price map stores embedding model prices in the same table keyed by model name, and setting `completion_tokens=0` yields the correct prompt-only cost.

Before the LiteLLM call, the function short-circuits to `$0.0` whenever `tokens <= 0` (pricing.py:85-86) — note this is a plain `<=` check on a raw integer, not the `max(0, int(x or 0))` sanitization pattern used in `estimate_llm_cost`; a `None` value for `tokens` would raise `TypeError` at the comparison rather than being coerced, meaning `estimate_embed_cost` is slightly less defensive against malformed input than its LLM-cost counterpart. In practice this is safe because its only caller, `VectorIndex._record_embed_usage`, always supplies an `int` produced by `estimate_embed_tokens` (which itself always returns `int`), so the stricter contract has not surfaced as a bug, but it does mean the two "sibling" functions have asymmetric input-validation strictness.

**Rule workflow**:
```
estimate_embed_cost(model, tokens, provider=None):
  if provider in ("ollama", "sentence-transformers") or _is_local_model(model): return 0.0
  if tokens <= 0: return 0.0
  name = _normalize_model(model)
  try:
      prompt_cost, _ = litellm.cost_per_token(model=name, prompt_tokens=int(tokens), completion_tokens=0)
      return float(prompt_cost or 0.0)
  except Exception as exc:
      _warn_once(name, exc)
      return 0.0
```

---

### Business Rule: Fail-safe degradation to zero cost on pricing-lookup failure

**Overview**:
Both cost functions wrap the `litellm.cost_per_token` call in a bare `except Exception`, guaranteeing that an unrecognized model, a LiteLLM internal error, or any other lookup failure degrades to `$0.00` rather than propagating an exception into the calling pipeline stage.

**Detailed description**:
This is the module's central resilience rule and its most consequential design decision: `pricing.py` treats "I don't know the price" and "the price is zero" as behaviorally identical outcomes. This is a deliberate trade-off appropriate for a **cost estimator** embedded in a content pipeline — a pricing lookup failure must never abort a harvest/extract/connect/garden run, because the actual LLM call already succeeded and its output must still be persisted. The alternative (propagating the exception) would make the entire pipeline's availability depend on LiteLLM's price-map coverage of every model string ever configured, including provider-prefixed identifiers for OpenAI-compatible gateways (`openrouter`, `opencode`, `azure`, `compatible` — see `llm.py`'s `is_openai_compatible`) whose model names LiteLLM's price table may not recognize at all.

The direct consequence is that **cost figures recorded for unrecognized models are silently incomplete** — a `$0.00` LLM cost in the `runs` table or SRC/ZTL frontmatter can mean either "this call was genuinely free" (local model) or "the price for this model is unknown to the installed litellm version" (a gateway alias, a brand-new model not yet in the price map, or a typo in the configured model name). The module provides no way for a downstream caller to distinguish these two cases from the returned `float` alone — the only signal is the one-time WARNING log line (next rule), which is not surfaced in any user-facing cost report, dashboard, or CLI table.

**Rule workflow**:
```
try:
    <litellm.cost_per_token(...) and cost computation>
except Exception as exc:
    _warn_once(normalized_model_name, exc)
    return 0.0
```

---

### Business Rule: One-shot warning deduplication per model name

**Overview**:
`_warn_once(model, exc)` (pricing.py:102-109) logs a WARNING the first time a given (normalized) model name fails LiteLLM's price lookup, and silently suppresses all subsequent warnings for that same model name for the remainder of the process lifetime, tracked via the module-level `_warned_models: set[str]`.

**Detailed description**:
Without this deduplication, a pipeline run processing hundreds of chunks against an unrecognized/mispriced model would emit hundreds of identical WARNING lines (one per LLM call or embedding upsert), which would flood the logs and obscure genuinely actionable warnings. The set is keyed by the *normalized* model name (post `_normalize_model`), so `openai/gpt-x` and `gpt-x` correctly collapse to the same warning entry, but two distinct raw strings that normalize differently would warn independently even if they refer to the same underlying model from a user's perspective.

The state is process-global and unbounded in practice (no eviction), which is appropriate for a CLI process with a bounded lifetime (one pipeline command invocation) but means a long-lived process (e.g. the FastAPI web worker in `web_app.py`, which runs as a persistent daemon thread) would accumulate warned-model entries indefinitely across many jobs — in practice this is a non-issue because the cardinality of distinct model strings actually configured is small (bounded by `config.yaml`'s `llm.model`/`embedding.model` and any per-call overrides), not by an unbounded external input. `reset_price_warnings()` (pricing.py:112-114) exists solely to clear this state between test cases (`tests/test_pricing.py`'s `setup_function`), and has no production call site.

**Rule workflow**:
```
_warn_once(model, exc):
  if model in _warned_models: return  (already warned, suppress)
  _warned_models.add(model)
  logger.warning("Preco LiteLLM indisponivel para modelo %r (%s) — custo registrado como 0", model, exc)

reset_price_warnings():
  _warned_models.clear()   # test-only reset hook
```

---

### Business Rule: No local price table — prices track the installed LiteLLM version

**Overview**:
The module maintains zero hard-coded prices. Every dollar figure it produces is delegated entirely to `litellm.cost_per_token`, meaning the pricing data's accuracy and currency are a function of which `litellm` package version is installed (`requirements.txt:18` pins `litellm>=1.40.0`).

**Detailed description**:
This is an explicit architectural constraint stated in the module's own docstring ("Prices refresh when the `litellm` package is upgraded") and echoed in `CLAUDE.md`'s "LLM provider pattern" section ("No local price table to maintain"). The design trades control (the project cannot fix or override an individual model's price without patching around this module) for maintenance simplicity (no per-model price entries to keep in sync with providers' published pricing pages).

The practical implication is a **temporal coupling to the `litellm` release cadence**: a newly released model (e.g. a new Claude or GPT generation) will price at `$0.00` with a one-time warning until the installed `litellm` version's price map includes it, even though the actual API call to that model succeeds and incurs real provider cost. There is no fallback price, no manual override mechanism, and no per-project price table in this codebase to fill such a gap — the only remediation path is `pip install -U litellm` (or the project's equivalent dependency-update flow). This rule is what makes the earlier "fail-safe degradation" rule load-bearing in normal operation, not just in true error conditions: a lag between a model's release and its appearance in `litellm`'s price map is an *expected*, not exceptional, occurrence.

**Rule workflow**:
```
No stored price table exists in this codebase for LLM/embedding models.
All price data originates from litellm.cost_per_token(), sourced from the
installed litellm package's bundled price map.
Upgrading litellm is the only mechanism that changes computed costs.
```

---

## 4. Component Structure

`pricing.py` is a flat, single-file module with no submodules, no classes, and no package structure of its own.

```
zettel/
└── pricing.py                  # LLM/embedding cost estimation via LiteLLM's price map (calculator only)
    ├── estimate_embed_tokens() # Rough token estimate for embedding text (chars/4, min 1)
    ├── _is_local_model()       # Heuristic: is this model string a local/self-hosted model
    ├── _normalize_model()      # Strips "openai/" prefix; preserves "anthropic/" prefix
    ├── estimate_llm_cost()     # USD cost of a chat completion (prompt_cost + completion_cost)
    ├── estimate_embed_cost()   # USD cost of an embedding call (prompt-side cost only)
    ├── _warn_once()            # One-shot WARNING log per unrecognized model name
    └── reset_price_warnings()  # Test-only: clears the warned-model set

tests/
└── test_pricing.py             # Unit tests for the four public entry points
```

There is no configuration file, schema, or data model owned by this component — it is pure computation over its function arguments plus one external dependency (`litellm`).

## 5. Dependency Analysis

```
Internal Dependencies (who calls pricing.py):
zettel/llm.py            (call_llm)                -> pricing.estimate_llm_cost
zettel/assets.py         (_describe_one)            -> pricing.estimate_llm_cost
zettel/index.py          (VectorIndex._record_embed_usage) -> pricing.estimate_embed_cost, pricing.estimate_embed_tokens
tests/test_pricing.py                                -> all public functions + reset_price_warnings
tests/test_llm_usage.py, tests/test_prompt_cache.py  -> patch pricing.estimate_llm_cost (mocked, not exercised directly)

pricing.py has NO internal dependencies on other zettel/* modules
(no imports from config.py, state.py, usage.py, vault.py, etc.)

External Dependencies:
- litellm (>=1.40.0)  - imported lazily inside estimate_llm_cost/estimate_embed_cost
                         (local import, not a module-level import) - used ONLY for
                         litellm.cost_per_token(); no LLM routing/completion calls made.
- Python stdlib: logging, typing (Any)
```

Notably, `litellm` is imported **inside** each function body (pricing.py:63, 89) rather than at module load time. This defers the (nontrivial) cost of importing LiteLLM's price-map data until a non-local, non-zero-token cost estimate is actually needed, and means a process that only ever talks to local models never pays that import cost at all.

Downstream, the `cost_usd` values produced here flow into `zettel/usage.py` (`CostTracker`/`record_llm`/`record_embed`), which is consumed by `zettel/state.py` (`runs`/`sources` cost columns), `zettel/vault.py` (`sync_source_costs_to_vault`, mirroring costs onto SRC/ZTL frontmatter), and the web dashboard (`get_web_dashboard()` in `state.py`). `pricing.py` has no direct dependency on any of these — the coupling is one-directional (pricing -> consumed by usage.py's callers), which is consistent with its role as a leaf calculator.

## 6. Afferent and Efferent Coupling

Coupling is measured at function granularity since this is a procedural module with no classes.

| Component (function) | Afferent Coupling | Efferent Coupling | Critical |
|-----------------------|-------------------|--------------------|----------|
| `estimate_llm_cost` | 2 (llm.py:call_llm, assets.py:_describe_one) + tests | 3 (`_is_local_model`, `_normalize_model`, `_warn_once`) + `litellm` | High |
| `estimate_embed_cost` | 1 (index.py:_record_embed_usage) + tests | 3 (`_is_local_model`, `_normalize_model`, `_warn_once`) + `litellm` | High |
| `estimate_embed_tokens` | 1 (index.py:_record_embed_usage) + tests | 0 | Medium |
| `_is_local_model` | 2 (`estimate_llm_cost`, `estimate_embed_cost`) | 0 | Medium |
| `_normalize_model` | 2 (`estimate_llm_cost`, `estimate_embed_cost`) | 0 | Medium |
| `_warn_once` | 2 (`estimate_llm_cost`, `estimate_embed_cost`) | 0 (logging only) | Low |
| `reset_price_warnings` | 0 in production; test fixtures only | 0 | Low |

"Critical" reflects blast radius if the function's logic is wrong, not code complexity: `estimate_llm_cost` and `estimate_embed_cost` are High because every recorded cost figure across the entire application (CLI cost summaries, web dashboard, SRC/ZTL frontmatter cost fields, `runs` table totals) traces back to these two functions — an error here silently corrupts cost accounting project-wide without breaking any functional pipeline behavior (since costs are informational, not gating). The private helpers are Medium/Low because they are only reachable through the two High-criticality entry points and have no independent callers.

## 7. Endpoints

Not applicable — `pricing.py` exposes no REST, GraphQL, gRPC, or CLI-command surface. It is a pure internal calculator module with no `cli.py` `Typer` command mapped to it (confirmed: no references to `pricing`/`estimate_llm_cost`/`estimate_embed_cost` found in `zettel/cli.py`).

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|--------------|-----------------|
| LiteLLM price map (`litellm.cost_per_token`) | In-process library call (no network) | Look up per-token USD price for a given model string | Python function call | Python primitives (`str`, `int`) in; `tuple[float, float]` out | Broad `except Exception` -> log once (`_warn_once`) -> return `0.0`; no retry, no propagation |
| `zettel/llm.py` (`call_llm`) | Internal caller | Supplies `model`, `prompt_tokens`, `completion_tokens`, `provider` for text/chat completions | Direct Python function call | Plain args | N/A (pricing never raises to this caller) |
| `zettel/assets.py` (`_describe_one`) | Internal caller | Same as above, for vision/image-description completions | Direct Python function call | Plain args | N/A |
| `zettel/index.py` (`VectorIndex._record_embed_usage`) | Internal caller | Supplies embedding text (for token estimate) and `embedding_model`/`embedding_provider` | Direct Python function call | Plain args | N/A |

There is no database, message queue, or external HTTP integration inside this module.

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Facade / Adapter | Thin wrapper around `litellm.cost_per_token` presenting a domain-specific two-function API (`estimate_llm_cost`, `estimate_embed_cost`) | `zettel/pricing.py:46-99` | Isolates the rest of the codebase from LiteLLM's raw API shape (tuple return, prompt/completion split) |
| Null Object / Fail-Safe Default | Every failure path (local model, unknown model, zero tokens) converges on the same `0.0` return value | `zettel/pricing.py:54-55, 58-59, 71-73, 83-86, 97-99` | Guarantees callers never need to handle a pricing exception; cost accounting degrades gracefully instead of failing the pipeline |
| Lazy Import | `import litellm` performed inside each pricing function body, not at module top | `zettel/pricing.py:63, 89` | Defers the cost of loading LiteLLM's price-map data until actually needed (e.g. never paid for pure-local-model runs) |
| One-Shot Warning / Deduplicated Logging | Module-level `set[str]` gates repeated `logger.warning` calls per model | `zettel/pricing.py:14, 102-109` | Prevents log flooding across high-volume pipeline runs (hundreds of chunks/calls per model) |
| Strategy-like Branching by Provider String | `provider` parameter used as a discriminant to select the zero-cost path vs. the LiteLLM-lookup path | `zettel/pricing.py:54, 83` | Lets callers short-circuit pricing cheaply when they already know the backend is local, without needing `_is_local_model`'s string heuristics |

Architecturally, the module honors a single-responsibility boundary: it is a pure function library (no shared mutable state besides the warning cache, no classes, no I/O) that other modules compose into their own side-effecting recording pipeline (`usage.py`). This keeps "how much did this cost" fully decoupled from "what do we do with that cost" — a clean separation that the codebase's `CLAUDE.md` documentation explicitly calls out ("Shared helpers live in `llm.py`... Instrumented from `call_llm` and embedding upserts").

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|------------------|-------|--------|
| Medium | `estimate_llm_cost` / `estimate_embed_cost` exception handling | Bare `except Exception` cannot distinguish "model genuinely unpriced in LiteLLM" from "model name malformed/wrong" from "LiteLLM internal bug" — all collapse to a single WARNING + `$0.00` | Cost figures for any misconfigured or newly-released model are silently incomplete with no way to detect this from the recorded data alone (no `floor_reason`-style annotation as exists in `retrieval.py`'s relevance floor) |
| Medium | `_normalize_model` | Only handles the `openai/` <-> `anthropic/` asymmetry observed in LiteLLM's price map; any other provider-prefix inconsistency (e.g. for `gemini/`, or future OpenAI-compatible gateway aliases like `openrouter`/`opencode`) is not normalized and would silently fall through to a lookup miss | New provider integrations may silently price at `$0.00` until someone notices and patches this function |
| Low-Medium | `estimate_embed_cost` input validation | Uses a plain `tokens <= 0` check rather than the `max(0, int(x or 0))` sanitization pattern used in `estimate_llm_cost`; a `None` `tokens` argument would raise `TypeError` at the comparison (would still be caught by the broader pipeline's exception handling upstream, but not gracefully inside this function itself) | Inconsistent defensive-coding contract between two sibling functions; a future caller passing `None` directly (bypassing `estimate_embed_tokens`) would hit an uncaught `TypeError` inside `estimate_embed_cost` before the `try` block is even reached |
| Low | `_warned_models` (module-global set) | Unbounded, process-lifetime, never evicted (only test-reset via `reset_price_warnings`) | Negligible in CLI usage (bounded model cardinality, short process lifetime); theoretically accumulates indefinitely in the long-lived web worker process, though bounded in practice by the small number of distinct configured models |
| Low | Pricing accuracy | No local price table; entirely dependent on `litellm`'s bundled price map being current for whichever models/providers are configured | Cost dashboards can under-report actual spend for very recently released models until a `litellm` upgrade lands; there is no mechanism in this codebase to override or supplement a missing price |
| Informational | `estimate_embed_tokens` | Char/4 heuristic is explicitly approximate, not tied to any specific tokenizer | Embedding cost figures are estimates by design; acceptable given the module's stated purpose but worth flagging as a known accuracy ceiling, not a defect |

No dead code, unused imports, or structural code smells were found — the module is small, cohesive, and every line is reachable from at least one production call site (with the sole exception of `reset_price_warnings`, whose only caller is the test suite by design).

## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|---------------------|----------|----------------|
| `estimate_embed_tokens` | 1 (`test_estimate_embed_tokens_min_and_ratio`) | 0 direct (exercised transitively whenever `index.py` embeds during other tests, but not asserted there) | Good for the documented ratio + floor + empty-string cases | Covers `""`, a 4-char string (floor case), and a 40-char string (ratio case); does not test negative-length-impossible or non-ASCII/multibyte text length behavior explicitly |
| `estimate_llm_cost` | 3 (`test_estimate_llm_cost_uses_litellm`, `test_estimate_llm_cost_unknown_model_returns_zero`, `test_local_provider_is_zero`) | 1 (`tests/test_llm_usage.py::test_call_llm_records_usage_and_cost`, but it *mocks* `pricing.estimate_llm_cost` entirely rather than exercising this module's real logic through `call_llm`) | Good on the "happy path via LiteLLM" and "unknown model -> 0" and "explicit ollama provider -> 0" cases | No direct test exercises `_is_local_model`'s heuristic branches (colon-without-slash pattern, `sentence-transformers`/`default` literal match, empty/`None` model) or `_normalize_model`'s prefix-stripping behavior in isolation — these are only indirectly reachable through the public functions and never asserted against specifically |
| `estimate_embed_cost` | 1 (`test_local_provider_is_zero`, covers only the local-provider zero-cost branch for both `"ollama"` and `"sentence-transformers"`) | 0 | Gap: no test exercises the successful-LiteLLM-lookup path for `estimate_embed_cost` (analogous to `test_estimate_llm_cost_uses_litellm` for the LLM variant), nor its unknown-model exception path, nor its `tokens <= 0` short-circuit | Missing coverage for the module's second-most-critical function's primary (non-local) code path |
| `_is_local_model` | 0 direct | 0 | Only exercised incidentally through `estimate_llm_cost("qwen3.5:4b", ...)` in `test_local_provider_is_zero`, which happens to hit the colon-without-slash branch, but the test does not target this helper or its other branches (`ollama/` prefix, `ollama:` prefix, empty string, `"default"`) | Untested branches represent latent risk: a regression in the heuristic (e.g. breaking the `"default"` literal match) would not be caught by the current suite |
| `_normalize_model` | 0 direct | 0 (indirectly exercised via `test_estimate_llm_cost_uses_litellm`, which passes `"gpt-4o-mini"` — a string with no prefix to strip, so the stripping branch itself is never actually tested) | The `openai/` -> stripped and `anthropic/` -> preserved branches are entirely untested | This is the function most likely to need future maintenance (per § 10's Medium risk) and has zero direct test evidence of its current documented behavior |
| `_warn_once` / warning deduplication | 0 direct assertion on dedup behavior | 0 | `test_estimate_llm_cost_unknown_model_returns_zero` triggers `_warn_once` once but does not assert that a second call with the same model suppresses the warning (no `caplog`/mock assertion on `logger.warning` call count) | The "one-shot" deduplication behavior — a documented, deliberate design choice — has no regression test protecting it |
| `reset_price_warnings` | Used as a fixture (`setup_function`) in `tests/test_pricing.py:15-16` | N/A | Implicitly relied upon for test isolation but never itself asserted (e.g. no test confirms that omitting the reset would cause a second `test_*_unknown_model_returns_zero`-style test to silently skip its warning) | Low priority — this is test-support code, not production logic |

**Test file locations**:
- `D:\projetos\zettel_app\tests\test_pricing.py` — primary unit tests (4 test functions, all passing on the module's public functions)
- `D:\projetos\zettel_app\tests\test_llm_usage.py:31-47` — integration test for `call_llm`'s usage/cost recording; mocks `zettel.pricing.estimate_llm_cost` rather than exercising it
- `D:\projetos\zettel_app\tests\test_prompt_cache.py:93` — mocks `zettel.pricing.estimate_llm_cost` to isolate prompt-cache-hint behavior, not a pricing test
- No test file was found exercising `VectorIndex._record_embed_usage` together with real (non-mocked) `pricing.estimate_embed_cost`/`estimate_embed_tokens` logic (`tests/test_index.py` has no references to `pricing`, `estimate_embed_cost`, or `record_embed`)

**Overall assessment**: The four directly-tested behaviors are well-chosen and represent the module's most important guarantees (token floor/empty-string handling, successful LiteLLM pass-through, safe degradation on unknown models, and local-provider zero-cost). However, coverage has a real gap on `estimate_embed_cost`'s non-local success path, and the three private helpers (`_is_local_model`, `_normalize_model`, `_warn_once`) have no tests targeting their specific branches directly — they are only accidentally touched by the public-function tests using inputs that happen not to exercise most of their logic (e.g. no test uses an `openai/`-prefixed or `anthropic/`-prefixed model name, and no test uses an `ollama/`-prefixed or `ollama:`-prefixed model string, or the `"default"` literal).
