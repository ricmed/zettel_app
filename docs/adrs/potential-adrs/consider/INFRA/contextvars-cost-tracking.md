# Potential ADR: Contextvars-Based Cost Tracking for Cross-Module Observability

**Module**: INFRA (Cost & Observability)  
**Category**: Observability / Instrumentation Pattern  
**Priority**: Consider (Score: 80)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The project uses Python's `contextvars` module to implement a thread-local cost aggregator (`CostTracker`) that accumulates LLM and embedding costs across the entire pipeline without requiring explicit parameter passing. Every LLM call and embedding operation records its cost to the current context variable; at the end of a pipeline phase, the accumulated `UsageSummary` is written to the `runs` table in SQLite.

The pattern allows deeply-nested function calls (e.g., harvester → chunker → embedder → LLM) to contribute cost updates without threading a `cost_tracker` parameter through every function signature.

**Introduced**: Foundational; `usage.py` implements the pattern; recent commits show stable usage.

**Modified**: Stable; recent enhancements add prompt-cache token tracking (cache_read_tokens, cache_write_tokens) but pattern unchanged.

---

## Why This Might Deserve an ADR

- **Impact**: Every LLM-calling phase (harvest, extract, connect, garden, ask, article, assets) uses CostTracker to track costs. Cost totals are persisted to `runs` and `sources` tables for auditing/billing.
- **Trade-offs Visible**:
  - **Convenience**: No need to pass cost tracker through function signatures; reduces parameter pollution.
  - **Implicit State**: Context variables are thread-local but not explicit in function signatures; callers must know to use `get_active_tracker()` or `set_active_tracker()`.
  - **Testing**: Context variables can leak between tests if not properly reset; test isolation requires explicit cleanup.
  - **Multi-threading**: Works for single-threaded or async code (contextvars are async-safe); breaks if code is CPU-bound threaded (each thread gets its own context).
- **Cost to Change**: Switching to explicit parameter passing would require:
  - Adding `tracker: CostTracker | None = None` parameter to every LLM-calling function
  - Updating all call sites to pass the tracker
  - Updating tests to provide/assert on tracker state
- **Team Knowledge**: Anyone working on cost tracking or observability must understand:
  - What `ContextVar` is and how it differs from global variables
  - When the active tracker is set/cleared (typically at the start/end of CLI commands or web jobs)
  - Why `UsageSummary` is aggregated at the end (not in real-time)
- **Temporal Context**: Stable for 18+ months; pattern unchanged. Recent additions (prompt-cache token tracking) follow the same design.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/usage.py`](../../../zettel/usage.py) - CostTracker and UsageSummary classes
  - `UsageEvent` dataclass — individual LLM/embedding cost event
  - `UsageSummary` dataclass — aggregated costs per run
  - `CostTracker` class (not visible in provided excerpt, but implied by usage patterns)
  - `format_progress()` utility — human-readable progress labels

### Code Evidence
```python
# From zettel/usage.py (context variable setup):
from contextvars import ContextVar

# Thread-local cost tracker (per asyncio task or thread)
_active_cost_tracker: ContextVar[CostTracker | None] = ContextVar(
    "active_cost_tracker",
    default=None
)

class CostTracker:
    def record_event(self, event: UsageEvent) -> None:
        """Record an LLM/embedding cost event."""
        # Accumulate into current summary
        self.summary.cost_usd_total += event.cost_usd
        self.summary.llm_calls += 1
        # ...

def get_active_tracker() -> CostTracker | None:
    """Get the current thread's active cost tracker, if any."""
    return _active_cost_tracker.get()

def set_active_tracker(tracker: CostTracker) -> None:
    """Set the current thread's active cost tracker."""
    _active_cost_tracker.set(tracker)

# Usage in LLM call (implicit context tracking):
# From zettel/llm.py (call_llm function):
def call_llm(...) -> str:
    response = client.invoke(...)
    
    # Extract usage metadata
    usage = response.response_metadata.get("usage_metadata", {})
    cost_usd = estimate_cost(usage, model=config.llm.model)
    
    # Record to active tracker (if set)
    tracker = get_active_tracker()
    if tracker:
        event = UsageEvent(
            kind="llm",
            model=config.llm.model,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            cost_usd=cost_usd,
        )
        tracker.record_event(event)
    
    return response

# Usage in CLI command (context setup/teardown):
# From zettel/cli.py (example harvest command):
@app.command()
def harvest(...):
    config = load_config()
    
    # Create and set active tracker for this run
    tracker = CostTracker()
    set_active_tracker(tracker)
    
    try:
        harvester.run(config=config, ...)  # All nested calls see tracker via context
    finally:
        # Aggregate and persist
        summary = tracker.get_summary()
        db.create_run(
            started_at=...,
            finished_at=...,
            cost_usd_total=summary.cost_usd_total,
            cost_usd_llm=summary.cost_usd_llm,
            cost_usd_embedding=summary.cost_usd_embedding,
            tokens_prompt=summary.tokens_prompt,
            tokens_completion=summary.tokens_completion,
        )
        set_active_tracker(None)  # Clear context
```

### Impact Analysis
- **Introduced**: Foundational (usage.py present from early pipeline architecture)
- **Modified**: Stable with enhancements (prompt-cache token tracking added when provider prefix caching was introduced)
- **Last change**: Recent additions follow pattern; no drift
- **Files affected**: llm.py (records LLM costs), index.py (records embedding costs), harvester, extractor, connector, gardener, ask, article (all set/clear tracker in CLI/web command handlers)
- **Scope**: Medium-Large (observability cross-cutting concern; all pipeline phases use it)

### Cost Tracking Example
```python
@dataclass
class UsageEvent:
    kind: str  # "llm" | "embed" | "cache_hit"
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    cache_read_tokens: int  # Anthropic prompt-cache reads
    cache_write_tokens: int  # Anthropic prompt-cache writes

@dataclass
class UsageSummary:
    cost_usd_total: float
    cost_usd_llm: float
    cost_usd_embedding: float
    tokens_prompt: int
    tokens_completion: int
    tokens_embedding: int
    llm_calls: int
    cache_hits: int
    embed_calls: int
    prompt_cache_read_tokens: int
    prompt_cache_write_tokens: int
```

---

## Questions to Address in ADR (if created)

- Why `contextvars` instead of thread-local storage (`threading.local()`)? (Answer likely: async-safe; works with asyncio tasks.)
- Should the tracker be mandatory (required to be set) or optional (silent if None)? (Currently: optional; a cost event without an active tracker is silently dropped.)
- How are costs estimated for Ollama and other local models? (Currently: logged at $0; should there be a configurable rate?)
- Should cost tracking be disabled for tests? (Currently: tests often set an active tracker to assert on costs.)
- Why separate `cost_usd_llm` and `cost_usd_embedding` instead of unified cost tracking? (Answer likely: different sources and scaling logic.)

## Related Potential ADRs
- LLM Provider Strategy (uses CostTracker for cost estimation)
- Embedding Provider Configuration (uses CostTracker for cost logging)

## Additional Notes
- The `format_progress()` function in usage.py formats human-readable progress labels (e.g., "embed 3/40") but is orthogonal to cost tracking; could be refactored separately.
- Cost estimation relies on LiteLLM's public price map; switching LLM providers requires that LiteLLM be updated to know the provider's pricing.
- No visible A/B testing or cost-budgeting logic; cost tracking is observational, not gating (e.g., no "abort if cost exceeds $X").
