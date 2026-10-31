# ADR-048: Per-Phase LLM Thinking Mode

**Status:** Accepted
**Date:** 2026-09-10
**Depends on:** [ADR-024: Pluggable Multi-Provider LLM Strategy](./ADR-024-multi-provider-llm-strategy.md), [ADR-004: YAML-First Configuration](../INFRA/ADR-004-yaml-first-configuration.md), [ADR-007: Layered Hashing](../INFRA/ADR-007-layered-hashing-strategy.md)
**Related to:** [ADR-059: Fail-Fast on LLM Unavailability](./ADR-059-fail-fast-on-llm-unavailability.md)

## Context and Problem Statement

Vendors ship models with thinking / reasoning enabled by default (Gemini 3 Flash Lite on `connect` is the operational case). Sampling knobs (`temperature`, `top_p`) were already in `config.yaml`, but `get_llm()` never forwarded `thinking_budget`, `reasoning_effort`, or `reasoning`. Turning thinking off, or pinning a level, required a code change. The SQLite `llm_cache` key also ignored the mode, so a later toggle would reuse the wrong response.

ADR-024 already forbids per-call provider fallback. The remaining question is how the gateway exposes a vendor-specific thinking API without leaking a second config schema per provider.

## Decision Outcome

**`llm.<phase>.thinking` is identity of the phase, like `temperature`.** `null` omits the parameter (vendor default). `false` / `0` disables. `true`, `minimal`/`low`/`medium`/`high`, or a non-negative token budget are mapped in `get_llm` via `_thinking_client_kwargs`:

- Gemini: `thinking_budget` or `reasoning_effort` (Gemini 3 thinking level)
- OpenAI-compatible (incl. DeepSeek): `reasoning_effort`
- Ollama: `reasoning` bool
- Anthropic (Haiku 4.5 / Sonnet 4.6 / Opus ≤ 4.6): `thinking: {type, budget_tokens}`
- Anthropic 5.5+ (Haiku 5.5, Sonnet 5.5, Opus 5.x, Opus 4.7/4.8, Fable, Mythos): adaptive thinking (see addendum)

`include_thoughts` is never set. Thought blocks that still arrive are dropped by `_message_text` (`type == text` only).

`compute_llm_call_checksum` includes the canonical token (`thinking_checksum_token`). There is no global thinking default — a missing YAML key falls to `None`, not to a project-wide on/off.

## Consequences

Disabling Gemini thinking is `thinking: false` on that phase. Changing the mode invalidates the SQLite cache for that call. Models that reject `reasoning_effort` (non-reasoning OpenAI chat) will fail at call time if thinking is set — same as an unsupported `temperature` on a given model; do not swallow that.

## Addendum (2026-10-28): Anthropic 5.5+ sampling and adaptive thinking

Claude Haiku 5.5 (and Sonnet 5.5, Opus 5.x, Opus 4.7/4.8, Fable, Mythos) reject `temperature`, `top_p` and `top_k`. They also reject `thinking: {type: enabled, budget_tokens: N}`. The vendor default is adaptive thinking, controlled by `output_config.effort`.

`get_llm` detects that family by model-id prefix (`_anthropic_omits_sampling`) and:

- omits `temperature` / `top_p` on the `ChatAnthropic` constructor (passing `None` still lands in `extra_body` under anthropic SDK 2.x and 400s on API `2026-10-21`)
- maps `thinking: false` / `0` / `none` to `output_config.effort: low` without `{type: disabled}` (Haiku 5.5 and Opus 5.5 cannot turn thinking off; low effort is the documented stand-in for Haiku 4.5 without thinking)
- maps `true` to `{type: adaptive}`; `minimal`/`low`/`medium`/`high` to adaptive plus `effort` (`minimal` → `low`); an integer budget to adaptive plus `effort: medium`
- `null` still omits the thinking parameter (vendor default)

Older Anthropic ids keep sampling knobs and the legacy `enabled`/`disabled` payload. The SQLite cache key is unchanged: configured temperature and thinking remain identity even when the wire omits sampling.

## References

* `zettel/config.py` (`LLMPhaseConfig.thinking`, `thinking_checksum_token`)
* `zettel/llm.py` (`_thinking_client_kwargs`, `_anthropic_omits_sampling`, `get_llm`)
* `zettel/hashing.py` (`compute_llm_call_checksum`)
