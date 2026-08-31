# Potential ADR: System+Human Prompt Split for Provider-Agnostic Prompt Caching

**Module**: LLM  
**Category**: API Protocol / LLM Prompt Architecture  
**Priority**: Must Document (Score: 135)  
**Date Identified**: 2026-08-30

---

## Existing ADR Context

No directly matching ADRs found (< 40% similarity to existing decisions).

Related context:
- **LLM/multi-provider-llm-strategy.md** — This decision builds on pluggable provider strategy; enables provider-specific caching hints
- **INFRA/hybrid-dense-bm25-retrieval.md** — Retrieval module also uses prompt caching for deterministic result consistency
- **INFRA/layered-hashing-strategy.md** — LLM-call hash enables deterministic caching; this architectural decision affects what gets hashed

---

## What Was Identified

The LLM module employs a **prompt template architecture** that splits every prompt file into two parts separated by a `<!-- zettel:user -->` HTML comment:

1. **System part** (stable instructions): Placed in `SystemMessage`
2. **User part** (per-call payload): Placed in `HumanMessage`

This layout enables provider-specific prefix reuse:
- **OpenAI/Gemini/Ollama**: Receive standard System+Human messages; prefix reuse is implicit in provider implementations
- **Anthropic**: Receives explicit `cache_control: {"type": "ephemeral"}` hints on the system message, triggering Anthropic's prompt-cache feature

**Key functions** (lines 224-275, 370-385, 278-351):
- `apply_prompt_cache_hints(provider, messages, enabled=True)` — Attaches Anthropic-specific cache hints to SystemMessage blocks (line 255: `"cache_control": {"type": "ephemeral"}`)
- `split_prompt_text(text)` — Splits on marker, returns `PromptParts(system, user_template, has_split=True)`
- `call_llm(..., system=None, user=None, ...)` — Accepts split system/user parameters; constructs SystemMessage + HumanMessage

**Temporal context**: Introduced 2026-08-13 (commit 6e32ef4, "feat(llm): add portable provider prompt caching via System+Human split"). This refactored all 17 prompt files (literature_note, permanent_note, moc_generation, moc_incremental, article_*, ask, etc.) to include the marker, plus updated all 8 call sites (extractor, connector, gardener, etc.) to use `load_prompt_parts()` instead of `load_prompt()`.

## Why This Might Deserve an ADR

- **Impact**: Affects prompt design workflow for every LLM-integrated module. 17 prompt files must follow the split convention; any new prompt requires the marker placement decision. Eight modules must call `load_prompt_parts()` + structure calls as `call_llm(..., system=parts.system, user=filled_user)`.
- **Trade-offs**: Enables provider-agnostic caching infrastructure (same prompt structure works with OpenAI, Anthropic, Gemini, Ollama), but introduces a new contract (HTML marker in markdown files) and provider-specific branching (`if provider == "anthropic"`). Anthropic gets caching; other providers don't.
- **Complexity**: Provider-specific hints (Anthropic's cache_control logic) are embedded in `apply_prompt_cache_hints()`, a moderate-complexity function. Adding a new provider with caching support requires extending this function.
- **Team Knowledge**: Critical for anyone writing or modifying prompts; must understand the marker convention. Important for LLM integration work (when to enable/disable caching, what prefix reuse means for cost savings). Affects understanding of why prompts are structured differently than a monolithic string.
- **Future Implications**: Prompt caching is provider-specific; if a provider (e.g., Claude 3.5) adds a caching feature with different syntax, the split architecture allows extending `apply_prompt_cache_hints()` without breaking existing prompts. Conversely, removing prompt caching would require refactoring to treat system+user as a unified template.
- **Temporal Context**: Stable for 17 days (since 2026-08-13); integrated into article pipeline (commit 64c5346, 2026-08-04) and refined with enhancements (e.g., handling multiple cache blocks). No regressions or rollbacks.

## Evidence Found in Codebase

### Key Files
- [`zettel/llm.py`](../../../zettel/llm.py) - Lines 354-385 (prompt splitting)
  - `PromptParts` dataclass (frozen)
  - `split_prompt_text()` function
  - `load_prompt_parts()` function
  - `USER_SPLIT_MARKER` constant (line 21)

- [`zettel/llm.py`](../../../zettel/llm.py) - Lines 224-275 (Anthropic-specific caching hints)
  - `apply_prompt_cache_hints()` function
  - Lines 248-258: SystemMessage reconstruction with cache_control
  - Lines 260-269: Content block iteration for multi-block systems

- [`zettel/llm.py`](../../../zettel/llm.py) - Lines 278-351 (unified call interface)
  - `call_llm()` function accepts `system` + `user` parameters
  - Lines 320-326: Message construction with prompt cache hints

- All 17 prompt files: `prompts/*.md`
  - Example: `prompts/literature_note.md` (split marker visible around line 1-10)
  - Example: `prompts/ask.md` (split marker + Anthropic-specific notes)

- All 8 call sites: `extractor.py`, `connector.py`, `gardener.py`, `gardener_hub.py`, `assets.py`, `bibliography.py`, `ask.py`, `article.py`
  - Pattern: `parts = load_prompt_parts(path); call_llm(..., system=parts.system, user=filled_user)`

- Config: [`config/config.yaml`](../../../config/config.yaml) - Line 30
  - `llm.prompt_cache: true` (enables/disables hints globally)

### Code Evidence

```python
# From zettel/llm.py:21
USER_SPLIT_MARKER = "<!-- zettel:user -->"

# From zettel/llm.py:370-385
def split_prompt_text(text: str) -> PromptParts:
    """Split raw prompt text on ``<!-- zettel:user -->``."""
    if USER_SPLIT_MARKER not in text:
        logger.debug(
            "Prompt sem marcador %s — enviando tudo como HumanMessage",
            USER_SPLIT_MARKER,
        )
        return PromptParts(system="", user_template=text.strip(), has_split=False)

    system, _, user = text.partition(USER_SPLIT_MARKER)
    return PromptParts(
        system=system.strip(),
        user_template=user.strip(),
        has_split=True,
    )

# From zettel/llm.py:224-275 (Anthropic-specific caching)
def apply_prompt_cache_hints(
    provider: str | None,
    messages: list[Any],
    *,
    enabled: bool = True,
) -> tuple[list[Any], dict[str, Any]]:
    """Attach provider-specific prompt-cache hints; no-op for most providers.
    
    Returns ``(messages, invoke_kwargs)``. Only Anthropic gets explicit
    ``cache_control`` on the system message content blocks.
    """
    invoke_kwargs: dict[str, Any] = {}
    if not enabled or not messages:
        return messages, invoke_kwargs

    if normalize_llm_provider(provider) != "anthropic":
        return messages, invoke_kwargs

    from langchain_core.messages import SystemMessage

    out: list[Any] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            content = msg.content
            if isinstance(content, str):
                out.append(
                    SystemMessage(
                        content=[
                            {
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ]
                    )
                )
            elif isinstance(content, list) and content:
                blocks = []
                for i, block in enumerate(content):
                    if isinstance(block, dict):
                        b = dict(block)
                        if i == len(content) - 1 and b.get("type") == "text":
                            b["cache_control"] = {"type": "ephemeral"}
                        blocks.append(b)
                    else:
                        blocks.append(block)
                out.append(SystemMessage(content=blocks))
            else:
                out.append(msg)
        else:
            out.append(msg)
    return out, invoke_kwargs

# From zettel/llm.py:278-302 (call_llm integration)
def call_llm(
    llm: Any,
    prompt: str = "",
    *,
    system: str | None = None,
    user: str | None = None,
    label: Optional[str] = None,
    step: Optional[int] = None,
    total: Optional[int] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    prompt_cache: bool = True,
) -> str:
    """Call the LLM and return the response text.
    
    Prefer ``system`` (stable instructions) + ``user``/``prompt`` (per-call
    payload) so providers can reuse the prefix (prompt caching).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    user_text = user if user is not None else prompt
    if not user_text and not system:
        raise ValueError("call_llm requires a user/prompt message")

    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=user_text))
    messages, invoke_kwargs = apply_prompt_cache_hints(
        provider, messages, enabled=prompt_cache,
    )

    response = llm.invoke(messages, **invoke_kwargs)
    # ... rest of function
```

### Example Prompt File Structure

```markdown
# prompts/literature_note.md

You are a literature-synthesis expert in Portuguese (PT-BR).
Your role is to extract key concepts from academic texts...

[System instructions continue...]

<!-- zettel:user -->

Context:
{context_chunk}

Source metadata:
{source_title}
{source_author}

[User prompt template with placeholders]
```

### Impact Analysis

- **Introduced**: 2026-08-13 (commit 6e32ef4)
- **Modified**: 5 commits since (refinements, config changes, test expansion)
- **Last change**: Recent commits focused on test additions (test_prompt_cache.py with 158 new lines) and config refactoring (8ac6f32), indicating continued validation
- **Themes**: Cost optimization, provider compatibility, test coverage
- **Affects**: Every LLM call path (extractor, connector, gardener, gardener_hub, assets, bibliography, ask, article + internal calls)

### Token Usage Tracking

`call_llm()` records prompt and completion cache tokens separately (lines 348-349):

```python
record_llm(
    ...
    cache_read_tokens=usage.cache_read_tokens,
    cache_write_tokens=usage.cache_write_tokens,
)
```

This allows visibility into how much cost savings derive from caching (tracked separately from SQLite's llm_cache hits).

### Scope & Extensibility

The architecture assumes future provider support:
- `apply_prompt_cache_hints()` has a branch-per-provider pattern
- Adding a new provider (e.g., Claude 4 with a different cache syntax) requires extending the function
- The marker convention is provider-agnostic; no marker changes needed

## Questions to Address in ADR (if created)

- **Why HTML comment as marker?** Other delimiters (e.g., `---`, `ZETTEL_USER_SPLIT`, YAML frontmatter) exist. What led to choosing an HTML comment?
- **Why system+human, not user+assistant?** LangChain's message types; could the abstraction be message-type-agnostic?
- **Anthropic ephemeral vs. cached?** Why ephemeral (5m TTL) instead of longer-lived cache? Cost/availability trade-off?
- **Prompt caching cost-benefit**: What is the empirical prompt-cache hit rate? How much does it reduce API costs?
- **Backward compatibility**: What happens when a prompt file lacks the marker? (Currently: logs debug, treats as user-only message.)
- **Multi-provider fallback**: If Anthropic request fails (or rate-limits), is there automatic fallback to another provider? (Currently: No.)

## Related Potential ADRs

- **LLM/multi-provider-llm-strategy.md** — Enables this decision by providing pluggable provider switching
- **INFRA/contextvars-cost-tracking.md** — Tracks caching impact on per-run cost via `cache_read/write_tokens`
- **INFRA/layered-hashing-strategy.md** — LLM-call checksum enables result caching; prefix reuse enables input caching

## Additional Notes

- **Opt-in control**: Prompt caching can be disabled per-call via `prompt_cache=False` parameter to `call_llm()`, and globally via `llm.prompt_cache: false` in config.yaml
- **Anthropic-specific optimization**: Only Anthropic benefits from explicit cache hints; OpenAI/Gemini/Ollama receive the split structure but no special hints. This is provider-asymmetry (fairness of optimization effort).
- **Prompt marker as contract**: The HTML comment must appear exactly (no typos, whitespace-sensitive) for the split to work. Typos silently degrade to user-only messages (no error thrown).
- **Content-block granularity**: Anthropic cache hints are applied to the *last* text block in the system message (lines 265-266), allowing multi-block systems (e.g., system instructions + embedded examples) while preserving token efficiency.
- **Test coverage**: `tests/test_prompt_cache.py` (158 lines, new in 6e32ef4) covers split logic and Anthropic hint application. No tests for other providers' prefix reuse behavior (would require provider-specific LLM mocking).

