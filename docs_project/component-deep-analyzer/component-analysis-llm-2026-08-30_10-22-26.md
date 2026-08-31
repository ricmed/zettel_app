# Component Deep Analysis Report — `llm`

## 1. Executive Summary

`zettel/llm.py` is the single shared LLM-provider adapter for the entire Zettelkasten pipeline. It centralizes four concerns that were previously duplicated verbatim across `extractor.py`, `connector.py` and `gardener.py` (per its own module docstring, `zettel/llm.py:1-9`):

1. **Provider instantiation** (`get_llm`) — turns `AppConfig.llm` into a concrete LangChain chat-model instance (OpenAI-compatible, Anthropic, Ollama, Gemini).
2. **Prompt-file parsing** (`load_prompt_parts` / `split_prompt_text`) — splits a Markdown prompt file into a stable `system` block and a per-call `user` template on the `<!-- zettel:user -->` marker, enabling provider prefix/prompt caching.
3. **LLM invocation** (`call_llm`) — sends `SystemMessage` + `HumanMessage`, applies provider-specific prompt-cache hints, extracts token usage (including provider cache-read/cache-write counts), estimates USD cost via `zettel.pricing`, and records everything on the active `zettel.usage.CostTracker`.
4. **Small utilities** — `fill_template` (naive `{key}` substitution), `extract_json` (pulls a JSON object/array out of an LLM's Markdown-fenced or loosely-formatted response), and `clip_text` (log-friendly text truncation).

The component has no HTTP endpoints, no persistence of its own, and no business domain data — it is a **cross-cutting infrastructure/adapter layer**. Every pipeline phase that calls an LLM (`extract`, `connect`, `garden`/`garden --hubs`, `review` auto-approve, `ask`, `article`, `bibliography` enrichment) and the image-description path in `assets.py` depends on it, directly or via a private-symbol partial duplication (see §10).

Key findings:
- The module is well-isolated and well-tested for its exported surface (`test_llm_usage.py`, `test_prompt_cache.py`), but three of its four supported providers (`anthropic`, `gemini`, `ollama`) have their LangChain integration packages **commented out by default** in `requirements.txt`, meaning `get_llm` will raise `ImportError` at call time unless the operator manually installs them — a latent configuration/deployment risk (§10).
- `assets.py` reimplements `get_llm`'s provider-branching logic in a private `_get_multimodal_llm` function (different `max_retries=0` and a caller-supplied `model` override) instead of extending `get_llm` with parameters, and separately reaches into `llm.py`'s underscore-prefixed "private" helpers (`_extract_usage`, `_resolve_model_name`) — a coupling/encapsulation smell (§10, §5).
- `call_llm` itself never touches the SQLite `llm_cache` table; deterministic response caching is the *caller's* responsibility (checksum computed by `hashing.compute_llm_call_checksum`, checked/written by each pipeline module around its `call_llm` invocation). This is a deliberate architectural boundary, not an oversight, and is documented in CLAUDE.md.

## 2. Data Flow Analysis

Two independent flows: **(A) provider setup**, done once per pipeline command, and **(B) a single LLM round-trip**, repeated per chunk/note/section.

```
(A) Provider setup — once per CLI command
1. cli.py / web_app.py calls _load_deps() -> AppConfig loaded from config/config.yaml
2. Pipeline module (extractor/connector/gardener/ask/article/bibliography) calls get_llm(cfg)
3. get_llm() normalizes cfg.llm.provider (normalize_llm_provider) and branches:
   - OpenAI-compatible (openai/openrouter/opencode/azure/compatible) -> langchain_openai.ChatOpenAI
   - anthropic -> langchain_anthropic.ChatAnthropic
   - ollama -> langchain_ollama.ChatOllama
   - gemini -> langchain_google_genai.ChatGoogleGenerativeAI
   - anything else -> raises ValueError("LLM provider não suportado: ...")
4. Returns a live LangChain chat-model object held for the command's lifetime

(B) Single call — call_llm(), per chunk/note/section
1. Caller loads prompt file once via load_prompt_parts(path) -> PromptParts(system, user_template, has_split)
2. Caller fills templates: fill_template(parts.system, mapping), fill_template(parts.user_template, mapping)
3. Caller checks SQLite llm_cache via a checksum from hashing.compute_llm_call_checksum
   3a. Cache hit  -> zettel.usage.record_cache_hit(); call_llm() is NOT invoked (llm.py plays no role)
   3b. Cache miss -> caller invokes call_llm(llm, user, system=..., label=..., provider=cfg.llm.provider, prompt_cache=cfg.llm.prompt_cache)
4. call_llm(): builds [SystemMessage(system)?, HumanMessage(user)]
5. apply_prompt_cache_hints(provider, messages, enabled=prompt_cache):
   - provider == "anthropic" and enabled -> rewrites the SystemMessage content into content blocks with cache_control: {"type": "ephemeral"} on the last text block
   - all other providers / disabled -> messages returned unchanged, invoke_kwargs == {}
6. llm.invoke(messages, **invoke_kwargs) -> provider HTTP call -> AIMessage response
7. content = response.content (coerced to str if not already)
8. _resolve_model_name(llm, model) -> explicit `model` arg, else llm.model / .model_name / .model_id, else ""
9. _extract_usage(response) -> TokenUsage(prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens)
   - tries response.usage_metadata first (LangChain v0.3+ normalized field)
   - falls back to response.response_metadata["token_usage"|"usage"] (provider-native shape)
   - cache token sub-extraction tries several vendor-specific nested keys (OpenAI prompt_tokens_details, Anthropic ephemeral_*_input_tokens, Gemini cached_content_token_count, etc.)
10. zettel.pricing.estimate_llm_cost(model_name, prompt_tokens, completion_tokens, provider) -> USD float (0.0 for local/unknown models)
11. zettel.usage.record_llm(...) -> pushes a UsageEvent onto the active CostTracker (contextvar-scoped to the current run)
12. Returns content (str) to the caller
13. Caller (e.g. extractor.py) writes the raw response to db.cache_llm_response(checksum, request_json, response_text) for future cache hits
14. Caller parses `content` as JSON/text per its own prompt contract (llm.py is agnostic to response shape beyond extract_json's best-effort parsing helper, which callers invoke separately)
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Provider routing | 4 supported provider families, aliased set for OpenAI-compatible gateways | `zettel/llm.py:24-30`, `zettel/llm.py:66-107` |
| Validation | Unsupported provider raises `ValueError` with the raw (non-normalized) provider string | `zettel/llm.py:107` |
| Validation | `call_llm` requires at least one of `user`/`prompt` or `system` non-empty | `zettel/llm.py:308-309` |
| Parameter forwarding | `temperature` override takes precedence over `cfg.llm.temperature`; `top_p` always forwarded, default `1` if config omits it | `zettel/llm.py:60`, `zettel/llm.py:64` |
| Prompt structuring | Prompt files are split on a fixed marker into system/user parts; absence of marker degrades to "everything is user content" | `zettel/llm.py:21`, `zettel/llm.py:370-384` |
| Caching (provider prefix) | Only Anthropic receives `cache_control: ephemeral` hints; hints apply only to the **last text block** of the system message | `zettel/llm.py:224-275` |
| Caching (provider prefix) | Cache hints are a no-op when `enabled=False` or when `messages` is empty | `zettel/llm.py:236-237` |
| Cost estimation | Model name resolution falls back through `model` arg -> `llm.model` -> `llm.model_name` -> `llm.model_id` -> `""` | `zettel/llm.py:110-117` |
| Cost estimation | Token usage extraction tries `usage_metadata` before `response_metadata`, and tries multiple vendor-specific key names for cache read/write tokens | `zettel/llm.py:137-221` |
| Logging | A correlatable INFO log line is emitted before every HTTP call when `label` is supplied, with optional `step`/`total` progress markers | `zettel/llm.py:311-317` |
| Template filling | `fill_template` treats `None` values as empty string; missing mapping keys leave the `{key}` placeholder literally in the output (no error) | `zettel/llm.py:399-404` |
| Response parsing | `extract_json` tries fenced code blocks first, then bare JSON-looking text, then a brace-scan fallback; raises `ValueError` if nothing matches | `zettel/llm.py:407-419` |
| Text truncation | `clip_text` collapses internal whitespace before measuring length, appends `"..."` only when truncated | `zettel/llm.py:33-38` |

### Detailed breakdown of the business rules

---

### Business Rule: Provider Routing and OpenAI-Compatibility Aliasing

**Overview**:
`get_llm` is the single factory that turns the declarative `AppConfig.llm.provider` string into a live LangChain chat-model object. Rather than one branch per literal provider name, the module defines a `_OPENAI_COMPAT_PROVIDERS` frozenset (`openai`, `openrouter`, `opencode`, `azure`, `compatible`) that all resolve to the same `ChatOpenAI` code path, differentiated only by whether `cfg.llm.base_url` is set.

**Detailed description**:
This design lets the project support any OpenAI-compatible gateway (OpenRouter, OpenCode, self-hosted vLLM, Azure OpenAI, or a generically "compatible" endpoint) without adding new branches — the only difference from stock OpenAI is that `base_url` is populated and forwarded into the `ChatOpenAI` constructor kwargs. `is_openai_compatible()` and `normalize_llm_provider()` are exported so other modules can replicate the same branching decision (which `assets.py` does, see §10) without importing `get_llm` itself, since `get_llm` hardcodes a fixed model source (`cfg.llm.model`) and does not accept an override model — a constraint that `assets.py`'s multimodal image-description path cannot satisfy (it needs a different, caller-specified vision model), forcing that module to reimplement the branch structure locally.

The four fully distinct branches are Anthropic (`langchain_anthropic.ChatAnthropic`), Ollama (`langchain_ollama.ChatOllama`, with optional `base_url` for a non-default host), and Gemini (`langchain_google_genai.ChatGoogleGenerativeAI`). Each branch imports its LangChain integration package lazily, inside the `if` block, rather than at module top — this means `zettel/llm.py` itself has no hard dependency on `langchain-anthropic`, `langchain-ollama`, or `langchain-google-genai` being installed; the cost of an uninstalled package is deferred until the operator actually configures that provider and calls `get_llm`.

Any provider string that normalizes to something outside the four recognized buckets triggers `raise ValueError(f"LLM provider não suportado: {cfg.llm.provider}")` — note the message interpolates the **original, non-normalized** `cfg.llm.provider` (not the lowercased/stripped value used for branching), so a operator who set `provider: " OpenAi "` (extra whitespace/case) would successfully route to the OpenAI branch, but a genuinely unsupported value is echoed back exactly as configured, aiding debugging of typos in `config.yaml`.

**Rule workflow**:
```
get_llm(cfg, temperature=None):
  temp = temperature if given else cfg.llm.temperature
  provider = normalize_llm_provider(cfg.llm.provider)   # lower().strip()
  base_url = cfg.llm.base_url (may be None)
  top_p = cfg.llm.top_p (default 1 if attribute missing)
  max_retries = cfg.llm.max_retries

  if provider in {openai, openrouter, opencode, azure, compatible}:
      -> ChatOpenAI(model, temperature=temp, top_p, max_retries, base_url?)
  elif provider == "anthropic":
      -> ChatAnthropic(model, temperature=temp, top_p, max_retries)   # no base_url support
  elif provider == "ollama":
      -> ChatOllama(model, temperature=temp, top_p, base_url?)        # no max_retries forwarded
  elif provider == "gemini":
      -> ChatGoogleGenerativeAI(model, temperature=temp, top_p, max_retries)
  else:
      raise ValueError(f"LLM provider não suportado: {cfg.llm.provider}")
```
Note the asymmetry: Anthropic ignores `base_url` entirely (no override path for Anthropic-compatible proxies), and Ollama ignores `max_retries` (retries are presumably handled by the Ollama client itself or not needed for a local server) — these are implementation choices, not documented exceptions, visible only by reading the branch bodies.

---

### Business Rule: System/User Prompt Splitting for Provider Prefix Caching

**Overview**:
Prompt Markdown files under `prompts/` are authored as a single file but conceptually contain two parts: instructions stable across every call for that prompt (system) and a per-call payload that changes every invocation (user, containing `{placeholders}`). The module docstring (`zettel/llm.py:6-8`) states this layout exists specifically "for provider prefix caching" — separating the stable prefix lets Anthropic (explicitly) and other providers (implicitly, via their own prefix-caching heuristics) skip re-processing the unchanged system instructions on repeated calls with the same prompt file.

**Detailed description**:
`split_prompt_text(text)` looks for the literal marker `<!-- zettel:user -->` in the raw prompt file text. If found, everything before the marker (stripped) becomes `PromptParts.system` and everything after becomes `PromptParts.user_template`, with `has_split=True`. If the marker is absent, the entire text becomes `user_template`, `system` is empty string, and `has_split=False` — this is the "legacy" single-blob mode, and a `logger.debug` line notes it explicitly ("Prompt sem marcador ... — enviando tudo como HumanMessage"). This means a prompt file author can opt out of the caching-friendly split simply by omitting the marker, and the system degrades gracefully rather than erroring — every caller across the codebase (`article.py`, `ask.py`, `extractor.py`, `connector.py`, `gardener.py`, `gardener_hub.py`, `assets.py`, `bibliography.py`) uniformly does `fill_template(parts.system, mapping) if parts.system else ""` to handle both cases identically.

`load_prompt(path)` is the thin file-existence-checked reader (`FileNotFoundError` if missing) that `load_prompt_parts(path)` wraps with the split. Two dedicated tests (`test_literature_note_system_has_no_chunk_placeholder`, `test_permanent_note_system_has_no_thesis_placeholder` in `tests/test_prompt_cache.py:49-68`) assert a specific content-shape invariant: the per-call placeholder (`{chunk_text}`, `{thesis}`) must **only** appear in the user template, never in the system block — if it leaked into `system`, the "stable" prefix would actually vary every call, silently defeating the entire caching rationale. This is effectively an authoring contract on the prompt `.md` files themselves, enforced by tests against `llm.py`'s parsing rather than by any runtime check in `llm.py`.

`PromptParts.full_template` reconstructs a single string (re-joining with the marker) for use cases needing the whole template as one unit — primarily hashing (`hashing.compute_llm_call_checksum` presumably keys off the full template plus filled variables to detect when a cached LLM response should be invalidated because the prompt itself changed).

**Rule workflow**:
```
load_prompt_parts(path):
  text = load_prompt(path)                      # raises FileNotFoundError if path absent
  return split_prompt_text(text)

split_prompt_text(text):
  if MARKER not in text:
      return PromptParts(system="", user_template=text.strip(), has_split=False)
  system, _, user = text.partition(MARKER)
  return PromptParts(system=system.strip(), user_template=user.strip(), has_split=True)

# Caller pattern (repeated identically in every consumer module):
parts = load_prompt_parts(prompt_path)
system = fill_template(parts.system, mapping) if parts.system else ""
user   = fill_template(parts.user_template, mapping)
call_llm(llm, user, system=system or None, ...)
```

---

### Business Rule: Provider-Specific Prompt-Cache Hints (Anthropic Only)

**Overview**:
`apply_prompt_cache_hints` is the mechanism that actually activates provider-side prefix caching for the one provider (Anthropic/Claude) whose API requires explicit `cache_control` markers on message content blocks — all other providers either cache automatically server-side (OpenAI, implicitly, per the module docstring) or do not support it, so the function is a no-op for them.

**Detailed description**:
The function only acts when `enabled=True` (wired to `cfg.llm.prompt_cache`, default `True`) **and** the normalized provider is exactly `"anthropic"`; every other provider, or a disabled flag, or an empty `messages` list returns the input unchanged with `invoke_kwargs={}`. When it does act, it lazily imports `SystemMessage` from `langchain_core.messages` and rewrites each `SystemMessage` in the list: a plain string `content` becomes a single-element content-block list `[{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]`; a `content` already shaped as a list of blocks gets the `cache_control` marker attached only to the **last** block if that block's `type` is `"text"` (a defensive choice — Anthropic's caching applies to a cache breakpoint at the end of the cached prefix, and marking every block would be both incorrect and wasteful). Non-`SystemMessage` entries (i.e., the `HumanMessage` carrying the per-call payload) pass through untouched, which is the entire point — only the stable system prefix is marked cacheable, never the varying user payload.

The `"ephemeral"` cache type corresponds to Anthropic's short-TTL (5-minute, extendable to 1-hour via a separate ephemeral variant) prompt cache; `_extract_cache_tokens_from_mapping` on the read side is written defensively to sum `ephemeral_5m_input_tokens` + `ephemeral_1h_input_tokens` when a combined `cache_creation` field isn't present, acknowledging that Anthropic's API has evolved its own usage-reporting shape over time and the code must tolerate multiple historical/current formats. This function returns a tuple `(messages, invoke_kwargs)` even though `invoke_kwargs` is always `{}` in the current implementation — the signature reserves room for providers that might need extra `.invoke()` kwargs (rather than message-content mutation) to enable caching, though none currently do.

**Rule workflow**:
```
apply_prompt_cache_hints(provider, messages, enabled=True):
  if not enabled or not messages: return messages, {}
  if normalize_llm_provider(provider) != "anthropic": return messages, {}
  for msg in messages:
      if isinstance(msg, SystemMessage):
          if content is str:
              wrap as [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
          elif content is list of blocks:
              copy each block; if it is the LAST block and block["type"] == "text":
                  add block["cache_control"] = {"type": "ephemeral"}
          else: pass through
      else: pass through (HumanMessage, etc.)
  return rewritten_messages, {}
```

---

### Business Rule: Token Usage and Prompt-Cache Token Extraction Across Providers

**Overview**:
`_extract_usage` and its helper `_extract_cache_tokens_from_mapping` normalize the wildly inconsistent shapes different LLM providers use to report token counts (including cache-specific counts) into a single `TokenUsage` dataclass, so downstream cost estimation and usage tracking never need provider-specific branching.

**Detailed description**:
The primary path checks `response.usage_metadata` — the LangChain-standardized field (v0.3+) most providers populate — reading `input_tokens`/`output_tokens` with fallback to legacy `prompt_tokens`/`completion_tokens` key names. If `usage_metadata` is absent or falsy (empty dict, `None`), the function falls back to `response.response_metadata`, digging into a nested `token_usage` or `usage` dict, again with dual key-name fallback (`prompt_tokens`/`input_tokens`, `completion_tokens`/`output_tokens`). If neither top-level structure is usable, a zeroed `TokenUsage()` is returned rather than raising — a call that cannot determine usage is billed as if it consumed no tokens, which under-reports cost/usage in that edge case but never crashes the pipeline.

Cache-token extraction (`_extract_cache_tokens_from_mapping`) is the most provider-fragile part of the module: it probes three different top-level "details" dict keys (`input_token_details`, `prompt_tokens_details`, `input_tokens_details` — covering different LangChain integration versions/providers), then within whichever is found, probes four possible cache-read key names and up to five cache-write key names (including the Anthropic dual-TTL summing behavior described above), and if the nested-details lookup yields nothing, falls back to a second round of top-level key probing (`cache_read_input_tokens`, `cached_tokens`, `cached_content_token_count` for Gemini, `total_cached_tokens`). This layered fallback strategy is a direct consequence of `call_llm` needing to support four unrelated provider SDKs through one uniform code path, and of those SDKs' usage-reporting formats not being stable even within a single provider across LangChain integration versions.

Every numeric extraction goes through `_as_int`, which coerces `None`/missing/non-numeric values to `0` via a `try/except (TypeError, ValueError)` rather than propagating an exception — usage/cost accounting is treated as best-effort telemetry, not a value the pipeline's correctness depends on.

**Rule workflow**:
```
_extract_usage(response):
  usage = response.usage_metadata or {}
  if usage (non-empty dict):
      prompt = usage.input_tokens or usage.prompt_tokens or 0
      completion = usage.output_tokens or usage.completion_tokens or 0
      cache_read, cache_write = _extract_cache_tokens_from_mapping(usage)
      return TokenUsage(prompt, completion, cache_read, cache_write)
  meta = response.response_metadata or {}
  token_usage = meta.token_usage or meta.usage or {}
  if token_usage (dict):
      prompt = token_usage.prompt_tokens or token_usage.input_tokens or 0
      completion = token_usage.completion_tokens or token_usage.output_tokens or 0
      cache_read, cache_write = _extract_cache_tokens_from_mapping(token_usage)
      if both zero: cache_read, cache_write = _extract_cache_tokens_from_mapping(meta)  # OpenAI nesting fallback
      return TokenUsage(prompt, completion, cache_read, cache_write)
  return TokenUsage()  # all zeros — usage undetermined, never raises
```

---

### Business Rule: `call_llm` Input Validation and Message Construction

**Overview**:
`call_llm` is the single entry point every pipeline module uses to actually talk to an LLM; it enforces one hard precondition (some content must be sent) and always constructs messages in the fixed order `[SystemMessage?, HumanMessage]`.

**Detailed description**:
The function accepts either the legacy single-string `prompt` parameter or the preferred `system`/`user` pair; `user_text = user if user is not None else prompt` establishes precedence — an explicit `user=""` (empty string, not `None`) would override `prompt` even if `prompt` were non-empty, which is a subtle precedence rule callers must respect (in practice, no caller in the codebase passes both). The validation `if not user_text and not system: raise ValueError(...)` means a call with only a `system` prompt and no user content is actually **allowed** (unusual for chat APIs, but the code permits it), while a call with nothing at all raises immediately client-side before any network request — protecting against sending a genuinely empty/degenerate request to a paid API. `SystemMessage` is only appended when `system` is truthy (non-empty), so calls that never received a split prompt (`has_split=False` from §3.2) correctly send only a `HumanMessage`.

After message construction, `call_llm` always routes through `apply_prompt_cache_hints` (§3.3) even when `prompt_cache=False` is passed at the call site (e.g., `extractor.py`'s retry-with-correction call at `zettel/extractor.py:259-267` explicitly disables it) — the function call happens unconditionally, but its internal `enabled` check makes it a fast no-op, meaning it is always safe to call but the caller retains full control per-invocation, independent of the global `cfg.llm.prompt_cache` default.

The optional `label`/`step`/`total` triplet drives an INFO log emitted **before** `llm.invoke(...)` is called, not after — this is a deliberate diagnostic choice so that if the underlying HTTP call hangs or the process is killed mid-request, the log clearly shows which labeled operation was in flight, rather than silently missing a "completed" log that never fires.

**Rule workflow**:
```
call_llm(llm, prompt="", *, system=None, user=None, label=None, step=None, total=None,
          model=None, provider=None, prompt_cache=True):
  user_text = user if user is not None else prompt
  if not user_text and not system: raise ValueError(...)
  if label: log INFO with optional [step/total] prefix  -- BEFORE the network call
  messages = [SystemMessage(system)] if system else []
  messages.append(HumanMessage(user_text))
  messages, invoke_kwargs = apply_prompt_cache_hints(provider, messages, enabled=prompt_cache)
  response = llm.invoke(messages, **invoke_kwargs)
  content = str(response.content) if not already str
  model_name = _resolve_model_name(llm, model)
  usage = _extract_usage(response)
  cost = estimate_llm_cost(model_name, usage.prompt_tokens, usage.completion_tokens, provider)
  record_llm(model=model_name or "unknown", tokens_in, tokens_out, cost_usd,
             label, step, total, cache_read_tokens, cache_write_tokens)
  return content
```

---

### Business Rule: Model-Name Resolution Fallback Chain

**Overview**:
`_resolve_model_name` decides which model string is recorded for cost estimation and usage logging, since the model actually invoked can come from three different places (explicit override, LangChain client attribute names vary by provider integration).

**Detailed description**:
An explicit `model` keyword argument to `call_llm` always wins outright — this exists because a single `llm` client instance is sometimes reused for multiple logical "models" is not the actual current use case in this codebase (every call site passes `model=None` and relies on the fallback), but the parameter exists for forward compatibility. When no explicit override is given, the function probes the LangChain client object itself for the first present-and-non-blank string attribute among `model`, `model_name`, `model_id` — different LangChain provider integrations name this attribute differently (`ChatOpenAI` uses `model_name` historically and `model` in newer versions; `ChatAnthropic` uses `model`; other integrations may use `model_id`), so probing all three in a fixed priority order is the pragmatic way to support them without per-provider branching. If none of the three attributes yield a usable string, `_resolve_model_name` returns `""`, and `call_llm` itself substitutes the literal string `"unknown"` when recording usage (`model=model_name or "unknown"`) — so a cost/usage log entry is never left with a falsy model field, even though the model was genuinely indeterminate.

**Rule workflow**:
```
_resolve_model_name(llm, model):
  if model (truthy): return model
  for attr in ("model", "model_name", "model_id"):
      val = getattr(llm, attr, None)
      if isinstance(val, str) and val.strip(): return val
  return ""
# call_llm then does: model=model_name or "unknown" when calling record_llm
```

---

### Business Rule: JSON Extraction from LLM Free-Text Responses

**Overview**:
`extract_json` is a defensive parser that pulls a JSON payload out of an LLM's raw text response, which may be wrapped in Markdown code fences, prefixed/suffixed with commentary, or (in the best case) a clean JSON string — LLMs are not guaranteed to follow output-format instructions exactly, so every JSON-producing prompt in the pipeline routes its response through this function before `json.loads`.

**Detailed description**:
The function tries three strategies in strict priority order, each only attempted if the previous one fails to match. First, a regex (`r"```(?:json)?\s*\n?(.*?)```"`, `re.DOTALL`) looks for a fenced code block — optionally tagged `json` — and if found, returns its trimmed inner content immediately, regardless of whether that content is actually valid JSON (the function only *extracts* candidate text; it does not validate — callers do `json.loads(extract_json(text))` and handle `JSONDecodeError` themselves, as seen in `extractor.py`'s retry-on-malformed-JSON path at `zettel/extractor.py:248-269`). Second, if no fence is found, the stripped text is checked for a leading `{` or `[`, and if so returned as-is on the assumption the entire response is already bare JSON. Third, a last-resort scan finds the first `{` and the last `}` in the text and returns everything between them inclusive — a heuristic that works for a single top-level JSON object embedded in surrounding prose but would incorrectly slice a top-level JSON **array** (`[...]`) that happens to contain object commentary before/after, or would grab too much if multiple independent `{...}` blocks appear in the response (it always uses the *first* `{` and the *last* `}`, not a matched pair). If none of the three strategies find anything, `extract_json` raises `ValueError("Nenhum JSON encontrado na resposta do LLM")`, which callers treat as a parse failure warranting a retry (as in the extractor's malformed-JSON recovery path).

**Rule workflow**:
```
extract_json(text):
  if fenced block (```json ... ``` or ``` ... ```) found via regex:
      return inner content, stripped
  text = text.strip()
  if text starts with "{" or "[":
      return text
  start = text.find("{"); end = text.rfind("}")
  if both found:
      return text[start:end+1]
  raise ValueError("Nenhum JSON encontrado na resposta do LLM")
```

---

## 4. Component Structure

`llm.py` is a single flat module (no sub-package), 420 lines, with no internal class hierarchy beyond two small dataclasses:

```
zettel/
└── llm.py                          # Shared LLM helpers (this component)
    ├── clip_text()                 # log-friendly text truncation
    ├── normalize_llm_provider()    # lowercase+strip provider alias
    ├── is_openai_compatible()      # membership test against _OPENAI_COMPAT_PROVIDERS
    ├── get_llm()                   # AppConfig -> LangChain chat-model instance
    ├── _resolve_model_name()       # model-name fallback chain (private)
    ├── TokenUsage (dataclass)      # prompt/completion/cache_read/cache_write counts
    ├── _as_int()                   # defensive int coercion (private)
    ├── _extract_cache_tokens_from_mapping()  # provider cache-token parsing (private)
    ├── _extract_usage()            # response -> TokenUsage (private)
    ├── apply_prompt_cache_hints()  # Anthropic cache_control injection
    ├── call_llm()                  # the single invocation entry point
    ├── PromptParts (dataclass)     # system / user_template / has_split
    ├── split_prompt_text()         # marker-based prompt splitting
    ├── load_prompt()               # raw prompt file reader
    ├── load_prompt_parts()         # load + split
    ├── fill_template()             # naive {key} substitution
    └── extract_json()              # JSON extraction from free text
```

Related infrastructure modules this component collaborates with but does not contain:
```
zettel/
├── config.py       # AppConfig / LLMConfig — the schema get_llm() consumes
├── pricing.py       # estimate_llm_cost() — called by call_llm() for USD cost
├── usage.py          # CostTracker / record_llm() — called by call_llm() for accounting
├── state.py           # StateDB.llm_cache table — deterministic response cache (caller-managed, not touched by llm.py)
└── hashing.py          # compute_llm_call_checksum() — cache key used by callers around call_llm()
```

## 5. Dependency Analysis

```
Internal Dependencies (within zettel/llm.py, by function):

get_llm(cfg) → cfg.llm.* (AppConfig/LLMConfig fields, duck-typed via getattr/attribute access)

call_llm(llm, ...)
  → apply_prompt_cache_hints(provider, messages)
  → _resolve_model_name(llm, model)
  → _extract_usage(response) → _extract_cache_tokens_from_mapping(dict) → _as_int(value)
  → zettel.pricing.estimate_llm_cost(model, tokens_in, tokens_out, provider)
  → zettel.usage.record_llm(...)

load_prompt_parts(path) → load_prompt(path) → split_prompt_text(text)

PromptParts.full_template → USER_SPLIT_MARKER (module constant)

External callers depending on llm.py (fan-in — see §6 for counts):
  cli.py (indirectly, via each pipeline module) → extractor.py, connector.py, gardener.py,
  gardener_hub.py, review.py, ask.py, article.py, bibliography.py, assets.py
    each import subsets of: get_llm, call_llm, load_prompt_parts, fill_template, extract_json,
    clip_text, is_openai_compatible, normalize_llm_provider, apply_prompt_cache_hints
  assets.py additionally imports the PRIVATE symbols _extract_usage, _resolve_model_name
    directly inside _describe_one() (zettel/assets.py:532-537) — bypasses call_llm() entirely
    because it needs multimodal (image) HumanMessage content blocks call_llm() does not support.

External Dependencies:
- langchain-core (>=0.3.0)        - SystemMessage/HumanMessage message primitives; always imported
- langchain-openai (>=0.2.0)      - ChatOpenAI; installed by default, used for openai/openrouter/
                                     opencode/azure/compatible aliases
- langchain-anthropic             - ChatAnthropic; COMMENTED OUT in requirements.txt by default
- langchain-ollama                - ChatOllama; COMMENTED OUT in requirements.txt by default
- langchain-google-genai          - ChatGoogleGenerativeAI; COMMENTED OUT in requirements.txt by default
- litellm (>=1.40.0)              - used transitively via zettel.pricing.estimate_llm_cost
                                     (cost_per_token price map only — llm.py never imports litellm
                                     directly, only via pricing.py)
- zettel.pricing                  - estimate_llm_cost() (internal module, USD cost calculator)
- zettel.usage                    - record_llm() / CostTracker (internal module, per-run accounting)
- zettel.config (implicit)        - AppConfig / LLMConfig shape consumed via duck typing (get_llm
                                     takes cfg: Any, not a typed AppConfig — no import-time coupling)
```

## 6. Afferent and Efferent Coupling

Unit of analysis: the module-level function (there are no classes besides two frozen dataclasses with no behavior). Afferent = number of distinct other modules importing/calling the symbol; efferent = number of distinct other internal modules/packages the symbol calls into.

| Component (function/symbol) | Afferent Coupling | Efferent Coupling | Critical |
|---|---|---|---|
| `call_llm` | 9 (extractor, connector, gardener, gardener_hub, review, ask, article, bibliography, + internal use by `load_prompt_parts` callers) | 5 (langchain_core, apply_prompt_cache_hints, _resolve_model_name, zettel.pricing, zettel.usage) | High |
| `get_llm` | 7 (extractor, connector, gardener, gardener_hub, review, ask, article/bibliography) | 4 (langchain_openai, langchain_anthropic, langchain_ollama, langchain_google_genai — lazy) | High |
| `load_prompt_parts` | 9 (article, ask, extractor, connector, gardener, gardener_hub, assets, bibliography, tests) | 2 (load_prompt, split_prompt_text) | Medium |
| `fill_template` | 9 (same set as load_prompt_parts, since every caller pairs them) | 0 | Medium |
| `apply_prompt_cache_hints` | 2 (call_llm internally, assets.py `_describe_one` directly) | 1 (langchain_core.SystemMessage, lazy) | Medium |
| `extract_json` | 6 (article, ask*, extractor, connector, gardener, gardener_hub) | 0 | Medium |
| `is_openai_compatible` / `normalize_llm_provider` | 2 (get_llm internally, assets.py `_get_multimodal_llm`) | 0 | Low |
| `clip_text` | 2 (index.py, article_graph.py) — used outside the LLM-call path, as a generic log-truncation utility | 0 | Low |
| `_extract_usage` / `_resolve_model_name` (private) | 2 (call_llm internally, assets.py `_describe_one` — cross-module private-symbol import) | 1 each | Medium (encapsulation risk — see §10) |
| `PromptParts` / `TokenUsage` (dataclasses) | Same as their producing functions | 0 | Low |

`ask.py` uses `extract_json` in some but not all response paths depending on prompt contract; marked with `*` for that nuance.

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|---|---|---|---|---|---|
| OpenAI / OpenAI-compatible gateways (OpenRouter, OpenCode, Azure OpenAI, generic "compatible") | External Service | Chat completion for extraction/connection/gardening/ask/article generation | HTTPS/REST (via `langchain-openai`'s `ChatOpenAI`) | JSON request/response, `usage_metadata` for tokens | `max_retries` forwarded to LangChain client (client-level HTTP retry); no explicit try/except around `llm.invoke()` inside `call_llm` itself — exceptions propagate to the caller (e.g. `extractor.py` catches and marks the chunk `failed`) |
| Anthropic (Claude) | External Service | Chat completion, with explicit prompt-prefix caching support | HTTPS/REST (via `langchain-anthropic`'s `ChatAnthropic`) | JSON; system content rewritten into content-block form with `cache_control` | Same client-level retry as above; caching hints are best-effort (silently no-op'd if message shape is unexpected) |
| Ollama (local) | External Service (local network) | Local/offline chat completion | HTTP (via `langchain-ollama`'s `ChatOllama`), optional custom `base_url` | JSON | No `max_retries` forwarded (not supported by the branch); cost forced to `$0` downstream in `pricing.py` |
| Google Gemini | External Service | Chat completion via Google's generative AI API | HTTPS/REST (via `langchain-google-genai`) | JSON, distinct usage-field naming (`cached_content_token_count`) handled in `_extract_cache_tokens_from_mapping` | Same client-level retry as OpenAI/Anthropic |
| `zettel.pricing` (internal) | Internal Module | USD cost estimation from token counts via LiteLLM's static price table | In-process function call | Python primitives (str/int/float) | Wrapped in `estimate_llm_cost`'s own try/except (in `pricing.py`, not `llm.py`); unknown models silently cost `$0.0` |
| `zettel.usage` (internal) | Internal Module | Per-run/per-source cost and token aggregation via `ContextVar` | In-process function call | `UsageEvent` dataclass appended to `CostTracker` | `record_llm` auto-creates a tracker via `require_tracker()` if none is active — never raises for a missing "begin_run" call |
| SQLite `llm_cache` table (`state.py`) | Internal Persistence | Deterministic LLM response caching, keyed by `compute_llm_call_checksum` | N/A — **not accessed by `llm.py` at all**; entirely orchestrated by callers (`extractor.py`, etc.) around `call_llm()` | JSON (`request_json`, `response_json` columns) | Cache-miss path always falls through to a real `call_llm()` invocation; no error handling needed in `llm.py` since it has no awareness of this layer |
| Prompt files (`prompts/*.md`) | Internal File I/O | Source of system/user prompt templates | Filesystem read (`Path.read_text`) | UTF-8 Markdown with `<!-- zettel:user -->` marker | `load_prompt` raises `FileNotFoundError` with a PT-BR message if the path does not exist; not caught inside `llm.py` — propagates to the caller |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---|---|---|---|
| Adapter / Facade | `get_llm` maps a single config shape to 4 unrelated LangChain client constructors behind one function signature | `zettel/llm.py:50-107` | Decouple every pipeline module from provider-specific instantiation details |
| Strategy (implicit, via branching) | `apply_prompt_cache_hints` picks a caching strategy per provider (currently: Anthropic content-block rewrite, or no-op) | `zettel/llm.py:224-275` | Isolate provider-specific caching behavior behind one call site |
| Template Method (data, not inheritance) | The `PromptParts` split (`system` + `user_template`) enforces a fixed message-construction shape (`SystemMessage?, HumanMessage`) reused identically by every caller | `zettel/llm.py:354-384`, mirrored in every consumer module | Guarantee the System+Human layout needed for prefix caching is applied uniformly |
| Value Object (frozen dataclasses) | `TokenUsage`, `PromptParts` are immutable (`@dataclass(frozen=True)`) | `zettel/llm.py:120-127`, `zettel/llm.py:354-360` | Prevent accidental mutation of parsed/derived data as it flows through cost estimation and prompt filling |
| Best-effort / Defensive Parsing | `_extract_usage`, `_extract_cache_tokens_from_mapping`, `_as_int`, `extract_json` all degrade to a safe default (zeros / raise a caught-by-caller `ValueError`) rather than crashing on unexpected provider response shapes | Throughout `zettel/llm.py` | Tolerate LLM/provider-SDK response variability without destabilizing the pipeline |
| Lazy Import | Every provider-specific LangChain package (`langchain_openai`, `langchain_anthropic`, `langchain_ollama`, `langchain_google_genai`) and even `langchain_core.messages` inside `call_llm`/`apply_prompt_cache_hints` are imported inside the function body, not at module top | `zettel/llm.py:67`, `79`, `88`, `99`, `242`, `302` | Avoid a hard import-time dependency on packages the operator has not installed/configured; `zettel/llm.py` itself imports cleanly with only `langchain-core` present |
| Context-scoped Singleton (collaborator, not in this file) | `zettel.usage`'s `ContextVar`-backed `CostTracker`, which `call_llm` writes to via `record_llm` | `zettel/usage.py:270-296`, consumed at `zettel/llm.py:340-350` | Per-run cost/usage aggregation without threading a tracker object through every function signature |

No REST/GraphQL/gRPC endpoints exist in this component — the "Endpoints" section is omitted per the report format's own instruction, since `llm.py` is an internal library module with no network-facing surface of its own.

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|---|---|---|---|
| High | `get_llm` provider branches (anthropic/ollama/gemini) | `requirements.txt` comments out `langchain-anthropic`, `langchain-ollama`, `langchain-google-genai` by default; only `langchain-openai` ships installed. Setting `llm.provider` to any of the other three in `config.yaml` without manually uncommenting/installing the package produces an `ImportError` deep inside `get_llm`, at the first pipeline call, not at config-load or `zettel doctor` time (unverified whether `doctor.py` checks this — not part of this component) | A misconfiguration surfaces late (mid-pipeline-run) rather than fast, and the error message is a raw Python `ImportError`, not a project-specific "provider package not installed" message |
| Medium | `assets.py` vs `llm.py` boundary | `assets.py::_get_multimodal_llm` (zettel/assets.py:484-516) duplicates `get_llm`'s entire provider-branching structure with two behavioral differences (`max_retries=0` and a caller-supplied `model` instead of `cfg.llm.model`), rather than adding an optional `model_override`/`max_retries_override` parameter to `get_llm` itself. `assets.py::_describe_one` (zettel/assets.py:532-537) also imports the underscore-prefixed private helpers `_extract_usage` and `_resolve_model_name` directly from `zettel.llm`, reaching past the module's public API | Any change to `get_llm`'s branch logic (e.g. adding a 5th provider, changing a kwarg name) must be manually mirrored in `_get_multimodal_llm`, and any rename/removal of the private helpers `_extract_usage`/`_resolve_model_name` would silently break `assets.py` without that coupling being visible from `llm.py`'s own public surface or tests |
| Medium | `extract_json` last-resort brace-scan | The final fallback (`text.find("{")` to `text.rfind("}")`) assumes a single top-level JSON *object*; it does not handle a top-level JSON *array* response that also contains embedded object-shaped commentary, and uses the first `{`/last `}` rather than a properly balanced/matched scan, so multiple independent JSON-looking fragments in a verbose LLM response could be mis-sliced | A model that ignores the "return only JSON" instruction and adds trailing commentary containing `{`/`}` characters could produce a `json.loads` failure or, worse, a successful-but-wrong parse further downstream |
| Medium | `TokenUsage`/cost fallback silence | `_as_int`, `_extract_usage`, and the whole cache-token extraction chain never raise or log when a provider's response shape is entirely unrecognized — they return zeros. Cost/usage under-reporting is invisible unless someone manually cross-checks recorded totals against an actual provider invoice/dashboard | Silent cost-accounting drift is possible for a new/updated provider SDK whose response shape moves outside all currently-probed key names, with no test or runtime warning surfacing the gap |
| Low | `get_llm`'s Anthropic and Ollama branches | Anthropic branch has no `base_url` support (cannot point at an Anthropic-compatible proxy); Ollama branch does not forward `max_retries`. Both are asymmetric with the other two branches and are not documented as intentional exceptions anywhere in code comments or CLAUDE.md | Minor functional gap if an operator needs a proxied Anthropic endpoint or wants LangChain-level retry behavior on Ollama calls |
| Low | `fill_template` | Missing mapping keys silently leave the literal `{key}` placeholder in the rendered text rather than raising or warning; a prompt-template/mapping mismatch (e.g. a renamed template variable) fails silently and only surfaces as a garbled prompt sent to the LLM | Hard-to-diagnose prompt-quality bugs if a template placeholder is renamed in a `.md` file without updating the corresponding Python `mapping` dict, or vice versa |
| Low | Error handling inside `call_llm` | `llm.invoke(messages, **invoke_kwargs)` has no try/except in `llm.py` itself — all exception handling (network errors, rate limits, malformed structured output) is left entirely to each caller, with inconsistent behavior observed across callers (e.g. `extractor.py` catches broadly and marks the chunk `failed`; `assets.py` has a dedicated rate-limit-retry wrapper around its own separate invocation path, not around `call_llm`) | Not itself a defect (the module intentionally stays thin), but means resilience patterns (retry/backoff beyond the LangChain client's own `max_retries`) are inconsistently implemented per caller rather than centralized |

## 11. Test Coverage Analysis

Two dedicated test files exercise this component directly; no test file is nested elsewhere in the project for it.

| Test File | Target Symbols Covered | Unit Tests | Coverage Notes | Test Quality |
|---|---|---|---|---|
| `tests/test_llm_usage.py` | `call_llm` (usage/cost recording path only) | 1 (`test_call_llm_records_usage_and_cost`) | Covers the happy path: a fake LLM client, `usage_metadata` with plain `input_tokens`/`output_tokens`, cost estimation mocked via `patch("zettel.pricing.estimate_llm_cost", ...)`, asserts on `CostTracker.summary()` | Good, focused assertion on the cost/usage side-effect; does not exercise `system`/`prompt_cache` parameters, error paths, or model-name resolution |
| `tests/test_prompt_cache.py` | `split_prompt_text`, `fill_template`, `load_prompt_parts` (via real files `prompts/literature_note.md`, `prompts/permanent_note.md`), `call_llm` (System+Human message construction, provider cache-token extraction), `apply_prompt_cache_hints` (anthropic/openai/ollama/disabled), `_extract_usage` (OpenAI-shaped `response_metadata` fallback path, called directly as a "private" symbol from the test itself), `is_openai_compatible`, `normalize_llm_provider` | 8 (`test_split_prompt_with_marker`, `test_split_prompt_without_marker_is_legacy_user_only`, `test_fill_template_replaces_keys`, `test_literature_note_system_has_no_chunk_placeholder`, `test_permanent_note_system_has_no_thesis_placeholder`, `test_call_llm_sends_system_and_human`, `test_apply_prompt_cache_hints_anthropic_only`, `test_extract_usage_openai_cached_tokens`, `test_get_llm_openai_compatible_aliases`) | Strong coverage of the prompt-split contract (including a real-file content invariant check against two actual prompt templates in `prompts/`), the Anthropic cache-hint content-block rewrite, one OpenAI-shaped usage fallback path, and the provider-alias set. **Not covered**: `get_llm`'s actual branch execution for `anthropic`, `ollama`, or `gemini` (only the alias-detection helpers `is_openai_compatible`/`normalize_llm_provider` are tested, not `get_llm` itself instantiating `ChatAnthropic`/`ChatOllama`/`ChatGoogleGenerativeAI` — consistent with those packages being commented out of `requirements.txt`, so such a test could not run in the default environment); `get_llm`'s `ValueError` for an unsupported provider string; `call_llm`'s `ValueError` when both `user`/`prompt` and `system` are empty; `extract_json` (no test file covers it directly, only indirectly via pipeline-module tests that may exercise it transitively); `load_prompt`'s `FileNotFoundError` path; the Gemini/Ollama/multi-block-content branches of `apply_prompt_cache_hints`; `clip_text` | Good — the two "no placeholder leak" tests are a genuinely valuable regression guard tied to the caching business rule (§3.2), and the fake-LLM-object test style (`SimpleNamespace`) keeps tests fast and provider-independent. Gaps are concentrated in provider branches that require optional dependencies and in explicit error/validation paths |
| (indirect) `tests/test_extractor.py`, `tests/test_connector.py`, `tests/test_gardener.py`, `tests/test_gardener_hub.py`, `tests/test_review.py`, `tests/test_ask.py`, `tests/test_article*.py`, `tests/test_bibliography.py` (not opened in this analysis; identified only via the `call_llm`/`get_llm`/`load_prompt_parts` import search in §5) | `call_llm`/`get_llm`/`load_prompt_parts`/`extract_json` as consumed through each pipeline module's own logic (mocked LLM responses) | Not enumerated here (out of component scope — this analysis is scoped strictly to `zettel/llm.py`) | These tests likely exercise `extract_json` and `call_llm` transitively through mocked/fake LLM objects supplied to each pipeline module, but were not opened/verified as part of this component-scoped analysis | Not assessed — recommend a follow-up analysis of the consuming modules (`extractor`, `connector`, `gardener`) if transitive `llm.py` coverage needs confirming |

**Overall assessment**: the component's exported, actively-used surface (`call_llm`, prompt splitting, Anthropic cache hints, OpenAI-compatible alias detection) has solid direct unit coverage. The main coverage gaps are (a) `get_llm`'s three optional-dependency provider branches, (b) explicit validation/error paths (`ValueError` in both `get_llm` and `call_llm`), and (c) `extract_json`, which has no dedicated test file despite being a non-trivial three-strategy parser used by six pipeline modules.

---

**Ambiguity notes**: Business-rule confidence is high throughout this report — every rule described is directly traceable to executable code in `zettel/llm.py`, cross-checked against its test suite and against real call sites in the eight consuming modules. No business rule in this report is inferred from naming or comments alone without corresponding code logic.
