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

## Addendum (2026-11-07): Gemini thinking_level only, no sampling, sampling out of the cache key

Google notified the project that requests carried `thinking_budget`, `temperature`, `top_p` and `top_k`. Since Gemini 3.6 Flash the three sampling knobs have no effect. On the upcoming models they will error, and `thinking_budget` will stop being remapped to `thinking_level` (400 `INVALID_ARGUMENT`). The extract phase (`gemini-3.5-flash-lite`, `thinking: null`) only escaped by accident: `langchain-google-genai` drops sampling for a hard-coded list of two models.

- **Gemini thinking is a level.** `_thinking_client_kwargs("gemini", ...)` sends only `thinking_level`: `false`/`0` → `minimal`, a level → that level, `true`/`null` → omitted. `LLMPhaseConfig` refuses a positive integer budget when `provider: gemini`. Supported levels vary by model; a model that refuses `minimal` fails at call time and the phase must name `low`.
- **No sampling for Gemini 3+.** `config.gemini_omits_sampling` (major version >= 3) joins `config.anthropic_omits_sampling` (the former `_anthropic_omits_sampling`, moved to `config.py` so cache-key callers can use it). `get_llm` sends no `temperature`/`top_p` for either family.
- **The cache key follows the wire.** This supersedes the last sentence of the 2026-10-28 addendum. `config.checksum_sampling(cfg, spec, temperature)` returns `(None, None)` when the model is sent no sampling, and `compute_llm_call_checksum` writes an empty fragment for it. Callers: `cached_call_llm`, extract, bibliography, vision. A temperature that never reaches the model can no longer split the cache. The one-time cost is that existing extract and connect entries stop matching, which matters only if the same chunks are reprocessed. `config.yaml` sets `temperature: null` on those phases.

**Anthropic `false` now turns thinking off where the model allows it.** This corrects the 2026-10-28 addendum, which said Haiku 5.5 "cannot turn thinking off". Haiku 5.5, Sonnet 5, Opus 5 and Opus 4.7/4.8 accept `thinking: {type: disabled}` at effort `high` or below. `false` sends them `{type: disabled}` plus `effort: low`. Opus 5.5, Sonnet 5.5, Fable and Mythos reject `disabled` at every effort (`_ANTHROPIC_NO_DISABLE_PREFIXES`) and keep `effort: low` alone. The old mapping left Haiku 5.5 thinking: the two-book connect spent ~43% of its output tokens on hidden thinking. `llm.connect` now says `thinking: low`, which is the wire it had before (adaptive + effort low). Switching connect to `false` waits for `evals/preregistration/connect-thinking.md`.

The Interactions API that Google recommends for new projects is not adopted: `generateContent` stays fully supported and the project reaches Gemini through `langchain-google-genai`.

## References

* `zettel/config.py` (`LLMPhaseConfig.thinking`, `thinking_checksum_token`, `checksum_sampling`, `anthropic_omits_sampling`, `gemini_omits_sampling`)
* `zettel/llm.py` (`_thinking_client_kwargs`, `get_llm`)
* `zettel/hashing.py` (`compute_llm_call_checksum`)
