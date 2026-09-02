# ADR-XXX: Pluggable Multi-Provider LLM Strategy

**Status:** Accepted
**Date:** 2026-07-02
**Depends on:** [ADR-XXX: YAML-First Configuration with Pydantic Fallback](../INFRA/ADR-004-yaml-first-configuration.md)
**Used by:** [ADR-XXX: System+Human Prompt Split for Provider-Agnostic Prompt Caching](./ADR-025-prompt-caching-system-human-split.md)

## Context and Problem Statement

Eight or more pipeline modules (extractor, connector, gardener, gardener_hub, assets, bibliography, review, ask, article) need to call an LLM to extract, summarize, cluster, and generate content, but none of them should depend directly on a specific vendor's client library. The project needed a single point where "which LLM answers this call" is decided, so that provider choice can change without touching every call site.

The chosen approach is a strategy-pattern gateway: `get_llm()` in `zettel/llm.py` reads `cfg.llm.provider` from `config.yaml` once per command invocation (via `_load_deps()` in `cli.py`) and instantiates the matching LangChain chat client (`ChatOpenAI`, `ChatAnthropic`, `ChatGoogleGenerativeAI`, `ChatOllama`). OpenAI-compatible gateways (OpenRouter, OpenCode, Azure) are grouped and routed through `ChatOpenAI` with an optional `base_url` override, and sampling parameters (temperature, top_p, max_retries) are forwarded uniformly regardless of which provider is selected.

This design has been stable since project inception (first commit visible 2026-07-02) and has absorbed two unrelated refactors — prompt-caching hints and the YAML-first config migration — without the provider-selection strategy itself being questioned, which is itself evidence of architectural stability rather than an oversight.

## Decision Drivers

* Eight-plus modules need to call an LLM without depending on a specific vendor SDK.
* Provider choice must be changeable via `config.yaml` alone, with no code change or redeploy.
* OpenAI-compatible gateways (OpenRouter, OpenCode, Azure) need a supported path without a fully separate integration per gateway.
* Sampling parameters (temperature, top_p, max_retries) must apply consistently regardless of the underlying provider.
* [NEEDS INPUT: Was multi-provider support driven by a specific cost or reliability concern, such as avoiding single-vendor outage risk or comparing per-provider pricing?]

## Considered Options

* Startup-time pluggable provider strategy via LangChain chat clients (chosen)
* Per-call dynamic provider selection with runtime fallback/load-balancing
* Direct single-provider SDK integration with no abstraction layer

## Decision Outcome

Chosen option: "Startup-time pluggable provider strategy via LangChain chat clients", because it decouples every dependent module from a specific vendor SDK behind one function call, lets provider choice change through configuration alone (the project currently binds `llm.provider: openai` while keeping `embedding.provider: ollama` independent), and avoids the runtime complexity that per-call switching would add across eight-plus call sites. [NEEDS INPUT: Was adopting LangChain's client interfaces specifically evaluated against a lighter, self-owned unified interface (e.g. normalizing providers to one wire protocol), or was LangChain the default choice from the outset?]

## Pros and Cons of the Options

### Startup-time pluggable provider strategy (chosen)

* Good, because a single function (`get_llm()`) is the only place that knows about vendor-specific client construction.
* Good, because switching providers is a one-line `config.yaml` edit, with no code change needed.
* Good, because sampling behavior (temperature, top_p, retries) stays consistent across providers.
* Bad, because provider choice is fixed for the whole process lifetime — no per-call fallback or load-balancing.
* Bad, because adding a new provider still requires a code change (a new if/elif branch plus a new LangChain sub-package dependency) — there is no provider registry.

### Per-call dynamic provider selection

* Good, because it would allow fallback on rate limits/errors and load-balancing across providers within a single run.
* Good, because it enables finer-grained cost or latency optimization per call type.
* Bad, because it adds client-pooling and fallback-orchestration complexity across eight-plus call sites.
* [NEEDS INPUT: Was this option evaluated and rejected during design, or never considered?]

### Direct single-provider SDK integration

* Good, because it removes the if/elif branching and one layer of abstraction.
* Good, because it removes dependence on LangChain's provider ecosystem tracking each vendor's API changes.
* Bad, because it hard-locks the project to one vendor — switching later would require refactoring all eight-plus call sites.
* Bad, because it would remove the ability to run against an alternate provider (e.g. Ollama) for offline or local development without a code change.

## Consequences

Every new provider still requires a code change in `get_llm()` and a new optional LangChain sub-package dependency; there is no factory or registration pattern that would let a provider be added without touching this module. A missing optional package (`langchain_anthropic`, `langchain_google_genai`, `langchain_ollama`) fails at call time with an `ImportError`, not at application startup, so a misconfigured environment surfaces only when that provider is actually invoked.

Cost estimation (`pricing.py`) depends on LiteLLM's public price map staying current for whichever provider/model combination `config.yaml` selects. [NEEDS INPUT: How is a newly added or changed provider/model's pricing validated against LiteLLM's price map before it is rolled out?] The embedding provider (`config.embedding.provider`) is deliberately kept independent of `config.llm.provider`, so the two are configured and can diverge separately, as the current `openai` LLM + `ollama` embedding split already demonstrates.

## References

* zettel/llm.py:50 — `get_llm()`, the provider-dispatch entry point
* zettel/llm.py:41 — `normalize_llm_provider()`, case-insensitive provider name normalization
* zettel/llm.py:46 — `is_openai_compatible()`, OpenAI-compatible gateway grouping
* zettel/cli.py — `_load_deps()`, calls `get_llm(cfg)` once per command invocation
* config/config.yaml:20 — `llm` configuration section (provider, model, base_url, sampling params)
