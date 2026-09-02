# ADR-XXX: System+Human Prompt Split for Provider-Agnostic Prompt Caching
**Status:** Accepted
**Date:** 2026-08-13
**Depends on:** [ADR-XXX: Pluggable Multi-Provider LLM Strategy](./ADR-024-multi-provider-llm-strategy.md)
**Related to:**
- [ADR-XXX: Layered Hashing Strategy for Deterministic Caching and Drift Detection](../INFRA/ADR-007-layered-hashing-strategy.md)
- [ADR-XXX: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)

## Context and Problem Statement

Every LLM-integrated module (extractor, connector, gardener, gardener_hub, assets, bibliography, ask, article) previously sent prompts as a single monolithic string to the model. This made it impossible for any provider to distinguish stable instructions from per-call payload, so no provider-side prefix reuse could occur even when the same instructions were reused across dozens of calls.

The system was changed to split every prompt file into two parts using an `<!-- zettel:user -->` marker: a stable **system** part placed in `SystemMessage`, and a per-call **user** part placed in `HumanMessage`. This structural split is provider-agnostic by itself — OpenAI, Gemini, and Ollama gain implicit prefix reuse for free from the split alone. Anthropic additionally receives an explicit `cache_control: {"type": "ephemeral"}` hint attached to the system message, which activates Anthropic's dedicated prompt-cache feature and is not needed by the other providers.

This decision affects prompt-authoring workflow for the whole codebase: all 17 prompt files were refactored to include the marker, and all 8 call sites were updated to call `load_prompt_parts()` instead of `load_prompt()`. Any new prompt file added to the project must decide where the marker goes, and any new LLM call site must use the split-aware call path to benefit from caching.

## Decision Drivers

* Repeated LLM calls across the pipeline (extractor, connector, gardener, ask, article) reuse large stable instruction blocks per prompt, so prefix reuse has real cost-reduction potential once system content is isolated from the per-call payload.
* The pluggable multi-provider strategy already routes calls through a shared `call_llm()` entry point, so a caching mechanism must degrade cleanly for providers with no explicit caching API rather than branching at every call site.
* Provider caching APIs are not uniform: only Anthropic currently exposes an explicit `cache_control` hint in this codebase's supported providers, so the mechanism needs a way to special-case one provider without altering the prompt contract for the others.
* The marker is a plain-text contract embedded in Markdown prompt files; it must be discoverable and safe to omit without breaking existing calls.

## Considered Options

* System+Human split with explicit Anthropic `cache_control` hints (chosen)
* Monolithic single-string prompt template (prior architecture, no caching support)
* System+Human split without provider-specific hints (structural split only, relying on implicit prefix reuse for every provider uniformly)

## Decision Outcome

Chosen option: System+Human split with explicit Anthropic `cache_control` hints, because it gives every provider the structural benefit of separating stable instructions from per-call payload, while allowing the one provider with an explicit caching API (Anthropic) to actually activate it via `apply_prompt_cache_hints()`. The marker-based split keeps prompt files provider-agnostic — no marker changes are needed to add caching support for a future provider, only an extension to `apply_prompt_cache_hints()`. [NEEDS INPUT: What alternatives to the HTML-comment marker (e.g., a YAML frontmatter key, a plain delimiter line) were evaluated before choosing `<!-- zettel:user -->`, and why was this marker judged safer or more convenient?]

Caching is opt-in and reversible at two levels: per-call via the `prompt_cache` parameter to `call_llm()`, and globally via `llm.prompt_cache` in `config/config.yaml`, so the mechanism can be disabled without touching prompt files. [NEEDS INPUT: What is the rationale for Anthropic's ephemeral (short-TTL) cache tier specifically, versus a longer-lived cache option, given this pipeline's typical call frequency and cost profile?]

## Pros and Cons of the Options

### System+Human split with Anthropic cache hints (chosen)

* Good, because it enables real cost savings on Anthropic without requiring any change to prompt file structure for other providers
* Good, because disabling caching (per-call or global) requires no prompt-file changes, only a flag
* Good, because the marker convention isolates the caching mechanism from the prompt content itself, so adding a new provider's caching syntax only touches `apply_prompt_cache_hints()`
* Bad, because it introduces provider asymmetry — only Anthropic benefits from the explicit hint, while OpenAI/Gemini/Ollama receive the same split with no special treatment

### Monolithic single-string prompt template (prior architecture)

* Good, because it required no marker contract and no per-call `system`/`user` parameter handling
* Bad, because no provider — including Anthropic — could distinguish stable instructions from per-call payload, eliminating any prefix-reuse opportunity
* Bad, because it offered no path to provider-specific caching without a structural rewrite

### Structural split without provider-specific hints

* Good, because it would avoid the `if provider == "anthropic"` branching entirely, keeping `apply_prompt_cache_hints()` unnecessary
* Bad, because it would forgo Anthropic's explicit cache activation, leaving that provider's caching feature unused despite already receiving the necessary structural split
* Bad, because it relies entirely on undocumented, provider-controlled implicit prefix matching with no application-level control or visibility

## Consequences

Every prompt file added to the project must place the `<!-- zettel:user -->` marker correctly; a missing or mistyped marker silently degrades to a user-only message (no error is raised), which is a quiet failure mode rather than a hard one. Every LLM call site must go through `load_prompt_parts()` and `call_llm(system=..., user=...)` to get any benefit, so new call sites that fall back to a single-string prompt lose the caching opportunity without any warning.

The system records `cache_read_tokens`/`cache_write_tokens` separately from SQLite's `llm_cache` hits, giving visibility into caching activity, but the codebase does not document an empirically measured hit rate or cost reduction from this mechanism. [NEEDS INPUT: What is the measured prompt-cache hit rate and resulting API cost reduction in production, to validate the caching investment against its added complexity?]

Because only Anthropic currently receives explicit caching, a rate limit or outage on that provider is not currently mitigated by an automatic fallback to another provider — the multi-provider strategy exists at the configuration level, but this decision does not address runtime fallback. [NEEDS INPUT: Is an automatic fallback to another provider planned if the Anthropic request fails or is rate-limited, given only Anthropic currently benefits from this caching path?]

## References

* `zettel/llm.py:354-385` — `PromptParts`, `split_prompt_text()`, `load_prompt_parts()`, `USER_SPLIT_MARKER`
* `zettel/llm.py:224-275` — `apply_prompt_cache_hints()` (Anthropic-specific `cache_control` logic)
* `zettel/llm.py:278-351` — `call_llm()` unified system/user call interface
* `prompts/literature_note.md` — representative prompt file using the `<!-- zettel:user -->` split
* `config/config.yaml:30` — `llm.prompt_cache` global enable/disable flag
