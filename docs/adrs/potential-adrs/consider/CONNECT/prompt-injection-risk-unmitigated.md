# Potential ADR: Prompt Injection Risk in Permanent Note Generation (Unmitigated)

**Module**: CONNECT  
**Category**: Security / Design Trade-off  
**Priority**: Consider (Score: 55, but documented security concern)  
**Date Identified**: 2026-08-30  

---

## Existing ADR Context

This decision appears to be a **deliberate security trade-off** rather than an oversight. It was **explicitly acknowledged in code comments** as part of the permanent note generation logic, suggesting the team is aware of the risk and has chosen not to mitigate it for now (likely due to complexity/cost trade-offs).

---

## What Was Identified

The CONNECT module's Prompt 2 (permanent note generation) accepts **unvalidated user-supplied text** from LLM-extracted concepts and includes them directly in the prompt template without sanitization or prompt-injection defenses.

Specifically:
- `cand.thesis`, `cand.definition`, `cand.intuition`, `cand.limits`, and `cand.source_locator` originate from LLM output derived from user-uploaded PDF/Markdown files
- These values are interpolated into the permanent-note-generation prompt via `fill_template()` (lines 217-229)
- The source excerpt text itself (from Prompt 1) flows into RAG context without delimiter escaping
- No active sanitization of prompt delimiters (e.g., "---", "</s>", "###SYSTEM") occurs before interpolation

The security note at lines 212-215 explicitly acknowledges this:
```python
# SECURITY NOTE: cand.thesis, cand.definition, and other candidate fields originate
# from LLM output derived from user-supplied files. Sanitize prompt delimiters
# (e.g. strip "---", "</s>", "###SYSTEM") before interpolation if untrusted input
# is expected, to reduce prompt-injection risk.
```

This is classified as a **known risk** rather than a bug: the code comment frames it as a conditional mitigation ("if untrusted input is expected"), not a requirement.

---

## Why This Might Deserve an ADR

- **Visibility**: A documented security risk deserves formal ADR treatment so decision-makers can understand the trade-off and revisit it when threat model or usage context changes (e.g., if the tool is ever exposed to untrusted users or multi-tenant scenarios).
- **Awareness**: The fact that it's documented in code comments suggests the team knows about it; formalizing the decision captures *why* mitigation was deferred (cost, complexity, assumed trust model, etc.).
- **Trust Boundary**: The assumption that LLM-extracted text is "safe" because it came from LLM processing is worth questioning — LLM outputs can be influenced by adversarial source documents.
- **Prompt Injection Surface**: Unlike the **Prompt 1 (extractor) risk** which is primarily internal (LLM talks to LLM), the Prompt 2 risk affects the final permanent note quality — bad injection could cause the LLM to propose false connections or reject valid notes.
- **Complexity**: Any mitigation would need to:
  - Identify which fields carry untrusted input (all of them, from extracted concepts)
  - Define a sanitization strategy (strip delimiters? quote/escape? validate against a whitelist?)
  - Ensure sanitization doesn't corrupt legitimate content (e.g., code examples, quoted text)
  - Test against actual injection payloads

---

## Evidence Found in Codebase

### Key Files
- [`zettel/connector.py`](../../../zettel/connector.py) - Lines 212-215
  - Explicit security note acknowledging the risk
- Lines 217-229
  - Unvalidated interpolation of candidate fields into template
- Lines 464-514
  - RAG context (source excerpts) also interpolated without escaping

### Code Evidence

Security note (lines 212-215):
```python
# SECURITY NOTE: cand.thesis, cand.definition, and other candidate fields originate
# from LLM output derived from user-supplied files. Sanitize prompt delimiters
# (e.g. strip "---", "</s>", "###SYSTEM") before interpolation if untrusted input
# is expected, to reduce prompt-injection risk.
```

Unvalidated template interpolation (lines 217-227):
```python
mapping = {
    "thesis": cand.thesis,
    "definition": cand.definition,
    "intuition": cand.intuition or "",
    "limits": cand.limits or "",
    "source_id": source_id,
    "source_locator": cand.source_locator or "",
    "literature_ref": literature_ref,
    "rag_context": rag_context,  # <-- also from retrieval, no escaping
    "images_context": images_context,
}
system = fill_template(prompt_parts.system, mapping)
user = fill_template(prompt_parts.user_template, mapping)
```

RAG context building (lines 479-514):
```python
for n in embedding_hits:
    doc = (n.document or "")[:150]  # <-- truncated but not escaped
    parts.append(f"- **{wiki}**: {doc}... (tags: {tags})")
```

### Impact Analysis
- **Introduced**: Present from initial pipeline design (no explicit commit introducing the risk, it's structural)
- **Acknowledged**: 2026-08-29 (most recent commit) still carries the same unmitigated pattern
- **Scope**: Affects all permanent notes (635-line connector.py, called on every approved concept)
- **Entry points**: 
  - 1. User uploads source file → extractor generates concepts → connector receives them
  - 2. LLM-provided image descriptions (from assets module) also flow into templates
- **Attack surface**: A carefully crafted PDF/Markdown could embed prompt-injection payloads in quoted text, code examples, etc., that the LLM faithfully extracts and the connector then re-uses

---

## Questions to Address in ADR (if created)

- **What is the assumed trust boundary?** (Are users trusted? Is multi-tenancy planned?)
- **Why was mitigation deferred?** (Cost/complexity trade-off? Assumption of adequate upstream processing? Risk accepted?)
- **What would mitigation look like?** (String sanitization? Structured prompt format? Separate LLM call for input validation?)
- **How does this compare to Prompt 1 (extractor) risk handling?** (Same risk pattern; extractor also accepts user-supplied files)
- **Is there a threat model?** (e.g., "user can upload PDFs, but not scripts" vs. "user has full control of source content")

---

## Related Potential ADRs

- Extractor: Prompt Injection Risk (Prompt 1 has the same pattern; see extractor module analysis)
- Connector: RAG-Based Permanent Note Generation (shares same prompt-execution context)
- Connector: PT-BR Language Guard (secondary LLM call; may be subject to same injection risk)

---

## Additional Notes

- **No test coverage** exists for injection scenarios (expected: prompt injection is typically security-focused, not covered in functional tests)
- **`fill_template()` in `llm.py`** is the interpolation point; no escaping logic currently exists
- **Prompt 1 (extractor)** has an identical pattern and security note; both deserve unified consideration if mitigation is planned
- The PT-BR guard (lines 585-625) makes a **secondary LLM call** to fix English spillover — this call also receives unvalidated concept output and is subject to the same risk
- **Configuration note**: `llm.prompt_cache` setting may affect which LLM provider is used; some providers (Anthropic) offer prompt caching with token counting, which could help trace injection attempts in logs
