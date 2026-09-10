# ADR-048: Per-Phase LLM Thinking Mode

**Status:** Accepted
**Date:** 2026-09-10
**Depends on:** [ADR-024: Pluggable Multi-Provider LLM Strategy](./ADR-024-multi-provider-llm-strategy.md), [ADR-004: YAML-First Configuration](../INFRA/ADR-004-yaml-first-configuration.md), [ADR-007: Layered Hashing](../INFRA/ADR-007-layered-hashing-strategy.md)
**Related to:** [ADR-045: Fail-Fast on LLM Unavailability](./ADR-045-fail-fast-on-llm-unavailability.md)

## Context and Problem Statement

Vendors ship models with thinking / reasoning enabled by default (Gemini 3 Flash Lite on `connect` is the operational case). Sampling knobs (`temperature`, `top_p`) were already in `config.yaml`, but `get_llm()` never forwarded `thinking_budget`, `reasoning_effort`, or `reasoning`. Turning thinking off, or pinning a level, required a code change. The SQLite `llm_cache` key also ignored the mode, so a later toggle would reuse the wrong response.

ADR-024 already forbids per-call provider fallback. The remaining question is how the gateway exposes a vendor-specific thinking API without leaking a second config schema per provider.

## Decision Outcome

**`llm.<phase>.thinking` is identity of the phase, like `temperature`.** `null` omits the parameter (vendor default). `false` / `0` disables. `true`, `minimal`/`low`/`medium`/`high`, or a non-negative token budget are mapped in `get_llm` via `_thinking_client_kwargs`:

- Gemini: `thinking_budget` or `reasoning_effort` (Gemini 3 thinking level)
- OpenAI-compatible (incl. DeepSeek): `reasoning_effort`
- Ollama: `reasoning` bool
- Anthropic: `thinking: {type, budget_tokens}`

`include_thoughts` is never set. Thought blocks that still arrive are dropped by `_message_text` (`type == text` only).

`compute_llm_call_checksum` includes the canonical token (`thinking_checksum_token`). There is no global thinking default — a missing YAML key falls to `None`, not to a project-wide on/off.

## Consequences

Disabling Gemini thinking is `thinking: false` on that phase. Changing the mode invalidates the SQLite cache for that call. Models that reject `reasoning_effort` (non-reasoning OpenAI chat) will fail at call time if thinking is set — same as an unsupported `temperature` on a given model; do not swallow that.

## References

* `zettel/config.py` (`LLMPhaseConfig.thinking`, `thinking_checksum_token`)
* `zettel/llm.py` (`_thinking_client_kwargs`, `get_llm`)
* `zettel/hashing.py` (`compute_llm_call_checksum`)
