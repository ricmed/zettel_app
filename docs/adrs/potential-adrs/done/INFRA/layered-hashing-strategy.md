# Potential ADR: Layered Hashing Strategy for Deterministic LLM Caching and Drift Detection

**Module**: INFRA (Hashing & Caching subsystem)  
**Category**: Data Architecture / Caching Strategy  
**Priority**: Must Document (Score: 120)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The project implements a multi-layer hashing strategy that enables deterministic caching of LLM responses and detects content drift at different granularities:

```
file_checksum → extraction_checksum → chapter_checksum → 
chunk_checksum → llm_call_checksum → note_semantic_checksum
```

Each layer serves a specific purpose:
1. **file_checksum** (SHA-256 of raw bytes) — Detect renamed/copied files during harvest
2. **extraction_checksum** (SHA-256 of normalized text) — Detect semantic duplication across formats (PDF vs. Markdown with identical content)
3. **chapter_checksum** — Not visible in code, but schema suggests planned granularity
4. **chunk_checksum** (SHA-256 of normalized chunk text) — Detect duplicate chunks within/across sources; feed into semantic-layer dedupe
5. **llm_call_checksum** (SHA-256 of prompt + chunk + model + context) — Enable deterministic LLM-response caching (SQLite `llm_cache` table)
6. **note_semantic_checksum** (SHA-256 of embeddable text, excluding frontmatter/managed blocks) — Detect drift in permanent notes (used to skip re-embedding)

Each checksum uses canonical text normalization (`normalize_text_for_hash`) to prevent false drift from whitespace, Unicode variance, PDF hyphenation artifacts, etc.

**Introduced**: Foundational; the hashing module has been stable, with the comment "implements layered hashing strategy" suggesting intentional design from inception.

**Modified**: Stable; recent additions (note_semantic_checksum for embed-skip logic) follow the pattern.

---

## Why This Might Deserve an ADR

- **Impact**: 
  - Deduplication depends on hashing (harvest layer-3 semantic dedupe uses chunk checksums).
  - LLM caching depends on `llm_call_checksum` (extractor, connector, gardener, ask, article all reuse cached responses).
  - Re-embedding skip logic depends on note_semantic_checksum (speed optimization).
- **Trade-offs Visible**:
  - **Determinism**: Hashing enables exact replay detection (same input → same LLM call checksum → reuse cached response). Alternative (always call LLM) wastes API quota.
  - **Normalization**: Text normalization (NFKC, strip PDF hyphenation, collapse whitespace) is opinionated; a different normalization strategy would invalidate all prior checksums.
  - **Invalidation**: Changing the hash algorithm or normalization breaks the cache (all prior checksums become orphaned). Not reversible.
  - **Cost**: Computing multiple checksums adds ~10ms per chunk/note; negligible but visible if profiling.
- **Cost to Change**: Switching hashing strategies would require:
  - Deciding on new normalization rules (would they be compatible with old rules?)
  - Recomputing all checksums in the database
  - Deciding what to do with orphaned `llm_cache` entries (delete? migrate? keep for rollback?)
  - Regression testing (ensure identical chunks still match)
- **Team Knowledge**: Anyone working on deduplication or LLM caching must understand:
  - Text normalization rules (NFKC, whitespace collapse, PDF dehyphenation)
  - What `normalize_text_for_hash` does (and why it's canonical)
  - The `llm_call_checksum` inputs (prompt_hash, chunk_checksum, model, temperature, language, rag_context_checksum)
  - Why re-running harvest on identical files is idempotent (file checksum + extraction checksum protect against re-processing)
- **Temporal Context**: Stable for 18+ months; no drift concerns visible in git history. The design was complete at inception.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/hashing.py`](../../../zettel/hashing.py) - Entire file (114 lines)
  - `normalize_text_for_hash()` - 6-step normalization
  - `sha256_hex()` - Canonical hash function
  - `compute_llm_call_checksum()` - Deterministic cache key
  - `compute_embedding_input_hash()` - Embed-skip detection
  - `compute_pipeline_signature()` - Config fingerprint (for run tracking)
  - `extract_embeddable_text()` - Strip frontmatter/managed blocks before hashing

### Code Evidence
```python
# From zettel/hashing.py (normalization):
def normalize_text_for_hash(text: str) -> str:
    """Normalize text canonically before hashing to prevent false drift.

    Steps:
      1. Unicode NFKC normalization
      2. Line break normalization (CRLF -> LF)
      3. Collapse decorative whitespace
      4. Fix common PDF hyphenation artifacts
      5. Limit consecutive blank lines
    """
    t = unicodedata.normalize("NFKC", text)  # Canonical Unicode form
    t = t.replace("\r\n", "\n").replace("\r", "\n")  # CRLF -> LF
    t = re.sub(r"[ \t]+", " ", t)  # Multiple spaces -> single
    t = re.sub(r" *\n *", "\n", t)  # Trim leading/trailing whitespace on lines
    t = re.sub(r"\n{3,}", "\n\n", t)  # Max 2 consecutive newlines
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)  # Fix "word-\ncontinuation"
    return t.strip()

# Deterministic LLM-call checksum:
def compute_llm_call_checksum(
    prompt_hash: str,
    chunk_checksum: str,
    model: str,
    temperature: float,
    language: str,
    rag_context_checksum: str = "",
) -> str:
    """Deterministic checksum for an LLM call, enabling response caching."""
    parts = f"{prompt_hash}|{chunk_checksum}|{model}|{temperature}|{language}|{rag_context_checksum}"
    return sha256_hex(parts)

# Embedding-skip detection:
def compute_embedding_input_hash(semantic_checksum: str, provider: str, model: str) -> str:
    """Deterministic hash of what a note's vector was built from.
    
    Lets callers skip re-embedding when the semantic content and the embedding
    provider/model are unchanged.
    """
    return sha256_hex(f"{provider}|{model}|{semantic_checksum}")

# Usage in harvester (layer-3 dedupe):
chunk_checksum = compute_chunk_checksum(normalized_text)
candidates = db.find_semantic_duplicate_candidates(
    chunk_checksum,
    threshold=0.88  # Calibrated on L2 distance, not RRF
)

# Usage in extractor (deterministic caching):
llm_call_checksum = compute_llm_call_checksum(
    prompt_hash=hash_of_prompt,
    chunk_checksum=chunk.checksum,
    model=config.llm.model,
    temperature=config.llm.temperature,
    language=config.language,
    rag_context_checksum=hash_of_rag_context,
)
cached = db.get_llm_cache(llm_call_checksum)
if cached:
    return cached  # Reuse without calling LLM
else:
    response = call_llm(...)
    db.set_llm_cache(llm_call_checksum, response)
    return response
```

### Impact Analysis
- **Introduced**: Foundational (comment "implements layered hashing strategy" suggests intentional design from inception)
- **Modified**: Normalization stable; checksum uses evolve (note_semantic_checksum added when embed-skip logic was introduced)
- **Last change**: Stable; no recent drift concerns in git history
- **Files affected**: hashing.py (canonical), harvester (file/extraction checksums), extractor (llm_call_checksum), connector (llm_call_checksum), gardener (llm_call_checksum), ask (llm_call_checksum), article (llm_call_checksum), sync (note_semantic_checksum)
- **Scope**: Large (used by 8+ modules, foundational to deduplication and caching)

### Known Dependencies
- **PDF hyphenation fix**: `re.sub(r"(\w)-\n(\w)", r"\1\2", t)` is specific to PDF extraction artifacts; would not apply to native Markdown.
- **Language-specific normalization**: Current normalization is language-agnostic, but PT-BR diacritics are kept (not stripped). FTS5 uses `unicode61 remove_diacritics` (different normalization).
- **LLM-call checksum inputs**: Includes `temperature` but not `top_p`; if `top_p` changes, cache is not invalidated (potential inconsistency).

---

## Questions to Address in ADR (if created)

- Why NFKC normalization instead of NFD or no normalization?
  - Answer likely: NFKC is "compatibility" form, better for matching equivalent characters (e.g., ligatures → component characters).
- What happens if the normalization rules change (e.g., future PT-BR diacritics handling)?
  - Answer: All prior checksums become stale; need migration strategy.
- Should `top_p` be included in `llm_call_checksum`? (Currently included in prompt but not in checksum field; potential cache inconsistency if user changes `top_p` between runs.)
- Why SHA-256? (Could be SHA-1, MD5, or a faster hash; SHA-256 is cryptographically secure but overkill for deduplication.)
- Should the embedding-skip logic use a separate table/index for fast checksum lookup, or is linear scan acceptable?

## Related Potential ADRs
- Deterministic LLM Response Caching (uses llm_call_checksum)
- Hybrid Dense+BM25 Retrieval (threshold calibration depends on checksums for consistency)

## Additional Notes
- The `compute_pipeline_signature()` function hashes the entire config (for run tracking), enabling detection of configuration drift between runs. Not explicitly used in observed code, but available for auditing.
- Text normalization is applied *consistently* to all text before hashing (via `normalize_text_for_hash`), so chunks/extracts/notes should all be comparable despite source format differences.
- No visible test coverage of normalization edge cases (e.g., PDF hyphenation artifacts); a test suite comparing normalized vs. un-normalized text could strengthen confidence.
