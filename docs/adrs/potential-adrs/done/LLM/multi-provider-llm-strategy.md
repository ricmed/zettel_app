# Potential ADR: Multi-Provider LLM Strategy with Pluggable Gateway

**Module**: LLM  
**Category**: Primary Framework / Provider Abstraction Strategy  
**Priority**: Must Document (Score: 140)  
**Date Identified**: 2026-08-30

---

## Existing ADR Context

No directly matching ADRs found in existing corpus (< 40% similarity to any documented decision).

Related decisions:
- **INFRA/yaml-first-configuration.md** — Configuration handles provider selection (`llm.provider`)
- **QA-WRITING/langgraph-statgraph-article-orchestration.md** — LangGraph uses this for article generation

---

## What Was Identified

The LLM module (`llm.py`) implements a **strategy pattern** for pluggable LLM provider selection, allowing the system to work with multiple external LLM providers through a unified interface. The `get_llm()` function instantiates the appropriate LangChain client (ChatOpenAI, ChatAnthropic, ChatGoogleGenerativeAI, ChatOllama) based on `cfg.llm.provider`, which is read from `config.yaml` at application startup.

**Design specifics**:
- Provider selection happens once per command execution via `_load_deps()` in cli.py
- Supported providers: openai, anthropic, ollama, gemini, openrouter, opencode, azure, compatible
- OpenAI-compatible gateways use ChatOpenAI with optional `base_url` override
- Temperature and top_p are forwarded to all providers for consistent sampling behavior
- Max retries are configurable per provider via `cfg.llm.max_retries`

**Temporal context**: Stable since project inception; provider switching capability has been present through recent refactorings (config centralization in 8ac6f32, prompt caching in 6e32ef4). No migrations or provider-strategy changes logged in recent history.

## Why This Might Deserve an ADR

- **Impact**: Universal coupling — every LLM-using module (extractor, connector, gardener, gardener_hub, assets, bibliography, review, ask, article) depends on this abstraction. Eight separate modules import from `llm.py`.
- **Trade-offs**: Enables flexible provider switching at startup, but locks provider choice at application boot (no per-call switching). Trades API simplicity (`get_llm(cfg)` returns a client) for coupling on LangChain's provider client interfaces.
- **Complexity**: Provider-specific branching (if/elif chain in `get_llm()`, provider name normalization) adds maintenance burden — each new provider requires code changes and new LangChain dependency.
- **Team Knowledge**: Critical for understanding how external API calls are routed, API key management, and cost estimation. Anyone working with LLM integration, testing providers, or changing embedding/generation models needs to understand this decision.
- **Future Implications**: Locking into LangChain's provider ecosystem means future provider additions depend on LangChain's adoption. Switching to a different LLM abstraction layer would require refactoring all call sites.
- **Temporal Context**: Stable for 58+ days; multiple refinements (config centralization, prompt caching hints) built on top without questioning the strategy itself, indicating architectural stability.

## Evidence Found in Codebase

### Key Files
- [`zettel/llm.py`](../../../zettel/llm.py) - Lines 50-107
  - `get_llm()` function: provider branching logic
  - `is_openai_compatible()` (line 46): helper for OpenAI-equivalent detection
  - `normalize_llm_provider()` (line 41): case-insensitive normalization

- [`zettel/cli.py`](../../../zettel/cli.py) - `_load_deps()` function
  - Calls `get_llm(cfg)` once per command invocation
  - Passes client to pipeline modules

- [`config/config.yaml`](../../../config/config.yaml) - Lines 20-30
  - LLM provider configuration section
  - Supports: openai (default), anthropic, ollama, gemini, openrouter, opencode

### Code Evidence

```python
# From zettel/llm.py:50-108
def get_llm(cfg: Any, temperature: float | None = None) -> Any:
    """Instantiate the configured LLM from AppConfig."""
    temp = cfg.llm.temperature if temperature is None else temperature
    provider = normalize_llm_provider(cfg.llm.provider)
    base_url = getattr(cfg.llm, "base_url", None)
    max_retries = cfg.llm.max_retries
    top_p = getattr(cfg.llm, "top_p", 1)

    if is_openai_compatible(provider):
        from langchain_openai import ChatOpenAI
        kwargs: dict[str, Any] = {
            "model": cfg.llm.model,
            "temperature": temp,
            "top_p": top_p,
            "max_retries": max_retries,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=cfg.llm.model,
            temperature=temp,
            top_p=top_p,
            max_retries=max_retries,
        )
    # ... ollama, gemini branches
```

### Impact Analysis

- **Introduced**: 2026-07-02 (59 days ago, earliest commit visible: bd2d67b)
- **Modified**: 6 commits over 59 days (major: 6e32ef4 for prompt caching hints, 8ac6f32 for config refactor)
- **Last change**: 2026-08-30 (8ac6f32, "feat(config): make config.yaml the operational source of truth" — no functional change to provider strategy itself)
- **Themes**: Config management, prompt caching integration, no breaking refactors
- **Affects**: 8+ modules (extractor, connector, gardener, gardener_hub, assets, bibliography, review, ask, article, index)

### Scope & Coupling

All LLM-using modules are discoverable via grep:

```bash
zettel/assets.py:        from zettel.llm import is_openai_compatible, normalize_llm_provider
zettel/bibliography.py:  from zettel.llm import call_llm, extract_json, fill_template, get_llm, load_prompt_parts
zettel/connector.py:     from zettel.llm import (...)
zettel/extractor.py:     from zettel.llm import (...)
zettel/gardener.py:      from zettel.llm import call_llm, extract_json, fill_template, get_llm, load_prompt_parts
zettel/gardener_hub.py:  from zettel.llm import call_llm, extract_json, fill_template, get_llm, load_prompt_parts
zettel/review.py:        from zettel.llm import get_llm
zettel/ask.py:           uses call_llm (via retrieval context assembly)
zettel/article.py:       uses call_llm, extract_json (via article_graph)
```

### Configuration Exposure

The strategy is fully externalized:

```yaml
# config/config.yaml
llm:
  provider: openai          # Switch here; no code change needed
  model: gpt-4o-mini
  temperature: 0.15
  top_p: 0.5
  max_retries: 2
  base_url: null            # Optional for OpenAI-compatible gateways
  prompt_cache: true
```

Supported provider values validated implicitly (raises ValueError if unknown). No explicit allowlist in code to update.

### Alternatives (If Observable in Comments/Config)

Not explicitly documented in comments. However, the presence of:
- `_OPENAI_COMPAT_PROVIDERS` frozenset (line 24-30): indicates deliberate grouping of OpenAI-compatible APIs
- `base_url` config option: designed with multi-gateway support in mind (OpenRouter, OpenCode)
- Multiple provider branches: suggests that each was considered a viable alternative at design time

No evidence of rejected alternatives (e.g., "we considered vendor lock-in but...").

## Questions to Address in ADR (if created)

- **Why LangChain clients?** What constraints led to choosing LangChain's provider abstractions vs. a unified interface (e.g., Anthropic's Messages API as a common protocol)?
- **Why per-startup provider selection?** What prevents per-call provider switching (e.g., round-robin load balancing, fallback on rate limit)?
- **Why OpenAI-compatible grouping?** Does the frozenset hint at a future "catch-all" provider for unknown compatible APIs?
- **Embedding provider coupling**: Why is the embedding provider (`config.embedding.provider`) separate from the LLM provider? Should they be unified or stay independent?
- **Cost tracking implications**: How does provider switching affect the cost calculation logic (pricing.py uses LiteLLM's price map)?

## Related Potential ADRs

- **LLM/prompt-caching-system-human-split.md** — Builds on this decision; assumes a pluggable provider abstraction to apply provider-specific hints
- **INFRA/contextvars-cost-tracking.md** — Records per-provider costs via this gateway
- **INFRA/yaml-first-configuration.md** — Configuration schema hosts the provider choice

## Additional Notes

- **Environment-specific binding**: The project currently hardcodes `provider: openai` and `embedding.provider: ollama` in `config/config.yaml`. This suggests the strategy is designed for flexibility but operationally bound to OpenAI LLM + Ollama embeddings. Switching providers requires a config change + environment variable (API key).
- **No provider registry**: The if/elif chain in `get_llm()` is the "registry"; adding a new provider requires code changes. An alternative pattern (e.g., provider classes, factory registration) could decouple provider addition from core logic.
- **Implicit vs. explicit**: Provider availability depends on optional LangChain sub-packages (`langchain_anthropic`, `langchain_google_genai`, `langchain_ollama`). Missing packages cause ImportError at call time, not at startup.
- **Test coverage**: No explicit provider-switching tests visible in the codebase (would require mocking multiple LLM clients).

