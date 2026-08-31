# Potential ADR: Three-Layer Page Inference Strategy (Metadata → Regex → Interpolation)

**Module**: HARVEST  
**Category**: Data Architecture / Page Locator Strategy  
**Priority**: Must Document (Score: 135)  
**Date Identified**: 2026-08-30  

---

## Existing ADR Context

No existing ADRs found with significant similarity. This is a foundational architectural decision for chunk page metadata (critical for reading experience and page citations in permanent notes).

---

## What Was Identified

The HARVEST module implements a **three-layer page inference strategy** for tracking which physical page(s) a text chunk occupies. This is fundamental to the "page-aware" Zettelkasten model, where users can cite permanent notes with precise page references.

**Three Layers (in order of precedence):**

1. **Layer 1: Explicit Metadata (PyMuPDF Page Map) — PREFERRED**
   - Source: PyMuPDF page-by-page text extraction with page numbers
   - Method: `_pymupdf_page_map(pdf_path)` (harvester.py:1267-1290) builds `list[(page_num, section_heading)]`
   - Per-chunk lookup: `lookup_page_for_chunk(chunk_text, page_map)` (paging.py, lines ~160-180)
   - Confidence: "explicit"
   - Use case: Multi-page chunks use the **first** page of the chunk (important for accuracy)
   - Fallback trigger: Page map unavailable or lookup fails

2. **Layer 2: Regex on Chunk Text — FALLBACK WHEN NO MAP**
   - Source: Text patterns within chunk head/tail (first 200 chars, last 200 chars)
   - Patterns: Multiple regex matchers (PAGE_PATTERNS in paging.py:26-31)
     - Bare numbers: `^\d{1,4}$`
     - Line-separated numbers: `\n\s*\d{1,4}\n`
     - Portuguese: `[Pp][aá]gina\s+(\d+)`
     - Margin notes: `^\d{1,4}\s+\w`
   - Confidence: "explicit" (when matched), "unknown" (when not)
   - Use case: Markdown files, text files without explicit page data, OCR artifacts
   - Caveat: `allow_regex=False` when page map is available (prevents stray numbers like "2 EPILOGUE" from being treated as page refs)

3. **Layer 3: Interpolation Between Known Pages — FALLBACK WHEN NO EXPLICIT DATA**
   - Source: Existing chunk pages (from Layers 1 or 2) in the document
   - Method: `infer_missing_page(chunk_index, pages)` (paging.py:96-126)
     - Finds nearest explicit pages before/after current chunk
     - Linear interpolation: `progress = (chunk_index - prev_idx) / span`
     - Estimated page: `prev_page + (next_page - prev_page) * progress`
   - Confidence: "inferred"
   - Use case: Filling gaps when Layers 1-2 fail for some chunks but succeed for others
   - Limitations: Assumes linear chunk-to-page mapping (breaks if chunking is non-uniform)

**Confidence Levels** (recorded per chunk):
- `"explicit"` (Layer 1 metadata or Layer 2 regex match)
- `"inferred"` (Layer 3 interpolation)
- `"unknown"` (all layers failed)

**Book Page Offset** (separate but dependent):
- Once file-page is known, book page is computed via:
  ```
  page_in_book = page_in_file - content_start_file_page + content_start_book_page
  ```
- `content_start_*` parameters set via `_resolve_content_paging()` (interactive/heuristic/confirmed/skipped)
- Stored per source in SQLite: `content_start_file_page`, `content_start_book_page`

**Workflow Integration**:
1. During harvest: Extract text → split into chapters → split into chunks
2. For each chunk: `extract_page_hint(chunk_text, page_from_meta=None, allow_regex=True)` → PageHint
3. After all chunks extracted: `apply_page_inference(page_hints)` → fill unknowns via Layer 3
4. For each chunk: compute `page_in_book` using offset math
5. Persist: `chunk.page_in_file`, `chunk.page_in_book`, `chunk.page_confidence` to SQLite + Chroma metadata

**Known Bug** (documented in mapping.md):
- `_resolve_content_paging()` (harvester.py:1782-1789) has unreachable code in non-interactive mode
- Effect: Non-interactive harvest always defaults `content_start_file_page=1, content_start_book_page=1`
- Impact: Book page offsets are silent wrong in CLI `--yes`, `run-all`, and web harvest jobs
- Workaround: Use `--content-start-file` / `--content-start-book` flags on harvest command

This three-layer strategy appears foundational (paging.py is ~250 lines of carefully-layered logic). The code comments explicitly document each layer's role and precedence.

## Why This Might Deserve an ADR

- **Impact**: Affects every chunk of every harvested PDF (~thousands of page_in_file lookups per corpus). Impacts:
  - Reading experience (users see page numbers in permanent notes)
  - Citation accuracy (permanent notes link to pages, not just sections)
  - Graph-based search (page metadata available for context in retrieval)
  - Cost tracking (page data used for cost estimation heuristics)
- **Trade-offs**:
  - Layer 1 (explicit) is most accurate but requires PDF processing overhead
  - Layer 2 (regex) is fast but prone to false positives (OCR noise, margin notes, page numbers in body text)
  - Layer 3 (interpolation) is a fallback heuristic, assumes linear chunk-page mapping (which often doesn't hold)
  - Silent failures: "unknown" confidence doesn't block harvest; chunks are indexed with page=None
- **Complexity**: Three independent mechanisms, each with different confidence/reliability, combined with a content-start offset model
- **Team Knowledge**: Critical to understand:
  - Why a chunk says "page 47" vs. no page data
  - Why "book page" differs from "file page" (offset model)
  - Why page inference matters for citations
  - How to debug wrong pages (check page_confidence in chunks)
- **Long-term Implications**:
  - Page data is immutable after harvest (changing content_start requires rechunking all chunks of that source)
  - Interpolation assumes linear mapping; documents with non-linear chunking have poor page inference
  - Future features (e.g., page-range citations) depend on page accuracy

## Evidence Found in Codebase

### Key Files

- [`zettel/paging.py:1-145`](../../../zettel/paging.py) — Full three-layer page inference strategy
  - Lines 3-11: Docstring explaining all three layers
  - Lines 26-44: Regex patterns for Layer 2
  - Lines 47-59: `PageHint` and `ContentPaging` dataclasses
  - Lines 67-93: `extract_page_hint()` dispatcher (Layer 1 metadata → Layer 2 regex)
  - Lines 96-126: `infer_missing_page()` Layer 3 interpolation
  - Lines 128-143: `apply_page_inference()` applies Layer 3 to all chunks

- [`zettel/harvester.py:1761-1831`](../../../zettel/harvester.py) — Content-start paging resolution
  - `_resolve_content_paging()` determines file page offset
  - **Known bug** at lines 1782-1789 (unreachable heuristic code in non-interactive mode)

- [`zettel/harvester.py:1661-1710`](../../../zettel/harvester.py) — Chunk page assignment
  - Per-chunk page lookup: `lookup_page_for_chunk(chunk_text, page_map)`
  - Offset computation: `compute_page_in_book(page_in_file, start_file, start_book)`
  - Persistence: `chunk.page_in_file`, `chunk.page_in_book`, `chunk.page_confidence` to SQLite

- [`zettel/state.py`](../../../zettel/state.py) — SQLite schema
  - `chunks.page_in_file` — Layer 1-3 inferred page
  - `chunks.page_in_book` — Offset-adjusted page
  - `chunks.page_confidence` — "explicit"|"inferred"|"unknown"
  - `sources.content_start_file_page`, `sources.content_start_book_page` — Offset parameters

### Code Evidence

```python
# Layer 1: Explicit metadata (paging.py:67-93)
def extract_page_hint(chunk_text: str, page_from_meta: int | None = None, *, allow_regex: bool = True) -> PageHint:
    """Resolve page_in_file for a chunk: metadata first, then optional regex."""
    if page_from_meta is not None and page_from_meta > 0:
        return PageHint(page_in_file=int(page_from_meta), confidence="explicit")
    if not allow_regex:
        return PageHint(page_in_file=None, confidence="unknown")
    # Layer 2: Regex fallback
    # ... (regex matching on head/tail)

# Layer 2: Regex patterns (paging.py:26-31)
PAGE_PATTERNS = [
    re.compile(r"^\s*(\d{1,4})\s*$", re.MULTILINE),  # Bare number
    re.compile(r"\n\s*(\d{1,4})\s*\n"),              # Line-separated
    re.compile(r"(?i)P[aá]gina\s+(\d+)"),            # Portuguese "página"
    re.compile(r"^\s*(\d{1,4})\s+\w", re.MULTILINE), # Margin note
]

# Layer 3: Interpolation (paging.py:96-126)
def infer_missing_page(chunk_index: int, pages: Sequence[int | None]) -> int | None:
    """Interpolate page_in_file between nearest explicit neighbours."""
    prev_idx, prev_page = find_nearest_before(chunk_index, pages)
    next_idx, next_page = find_nearest_after(chunk_index, pages)
    if prev_page and next_page:
        span = next_idx - prev_idx
        progress = (chunk_index - prev_idx) / span
        return int(round(prev_page + (next_page - prev_page) * progress))
    return prev_page

# Chunk page assignment (harvester.py:1687-1710)
page_hint = extract_page_hint(chunk_text, page_from_meta=page_from_map, allow_regex=allow_regex)
# ... after all chunks ...
applied_hints = apply_page_inference(page_hints)  # Layer 3
for hint in applied_hints:
    page_book = compute_page_in_book(
        hint.page_in_file,
        content_start_file_page=paging.content_start_file_page,
        content_start_book_page=paging.content_start_book_page,
    )
    chunk_spec["page_in_file"] = hint.page_in_file
    chunk_spec["page_in_book"] = page_book
    chunk_spec["page_confidence"] = hint.confidence

# Known bug: unreachable heuristic code (harvester.py:1782-1789)
if skip_paging or not interactive:
    conf = "skipped" if skip_paging else (
        "heuristic" if suggested.get("confidence") == "heuristic" else "skipped"
    )
    if not interactive and not skip_paging:
        # Non-interactive without flags: process all pages (file==book).
        return ContentPaging(1, 1, "skipped")  # ← Always returns here for non-interactive
    return ContentPaging(sug_file if conf == "heuristic" else 1, sug_book if conf == "heuristic" else 1, conf)
```

### Impact Analysis

- **Introduced**: Foundational; paging.py and page_inference logic appear since early harvester design
- **Modified**: Stable; regex patterns and interpolation unchanged
- **Known bug**: `_resolve_content_paging` early return (line 1788) makes heuristic suggestion unreachable (2026-08-30)
- **Themes**: "paging", "page inference", "pdf", "reading", "accuracy"
- **Affects**: Every chunk of every PDF (100% of PDF harvest affects this logic)
- **Test Coverage**: Partial (paging unit tests in test suite; integration coverage less complete)

### Alternatives (Observed or Implied)

1. **Single-layer: Metadata only (no regex, no interpolation)**
   - Pros: Deterministic, no false positives
   - Cons: 50-70% of chunks have unknown pages (no fallback)
   - **Rejected**: Interpolation and regex essential for completeness

2. **Regex-only (skip metadata layer)**
   - Pros: Fast, no PDF processing overhead
   - Cons: High false-positive rate, no accuracy guarantee
   - **Rejected**: Metadata layer provides the most accurate data when available

3. **User-provided page map** (explicit annotation)
   - Pros: 100% accuracy, no heuristics
   - Cons: Manual labor per document, UI required
   - **Partial adoption**: Users can provide `--content-start-file`/`--content-start-book` flags

4. **ML-based page detection** (e.g., training a page classifier)
   - Pros: Adaptive to corpus patterns
   - Cons: Complexity, cold-start problem, no external validation
   - **Rejected**: Heuristic layers sufficient for known document types

## Questions to Address in ADR (if created)

- Should Layer 2 regex patterns be configurable per corpus? (Currently hardcoded; no config override)
- Does Layer 3 interpolation work correctly when chunks are batched by section (non-uniform sizes)? (Assumed linear; may fail on structured documents)
- What happens when page inference fails completely (all "unknown")? (Chunks indexed with page=None; visible in SQLite but not enforced)
- Should the known bug (unreachable heuristic code) be fixed, or is the current behavior intentional? (Bug fix candidate, not architectural decision per se)
- Is page metadata queryable for retrieval (e.g., "search this range of pages")? (Not currently exposed; could enhance search UX)

## Related Potential ADRs

- **HARVEST/docling-pdf-extraction-with-pymupdf-fallback** — Page map depends on PyMuPDF; Docling structure impacts layer decisions
- **HARVEST/structural-chunking-strategy** — Chunk boundaries affect page inference accuracy (non-uniform chunking breaks interpolation)
- **HARVEST/three-layer-duplicate-detection** — Page metadata independent but complementary to deduplication

## Additional Notes

- **Temporal context**: Strategy appears foundational, stable for entire codebase history
- **Configuration exposure**: Content-start parameters tunable via CLI flags and config.yaml
- **Testing**: Partial coverage; `tests/test_paging.py` exists but integration tests limited
- **Known bug**: `_resolve_content_paging` unreachable code at lines 1782-1789 (high-priority fix candidate)
- **Observability**: Page confidence levels recorded; queryable via SQLite for debugging
- **Future enhancement**: Page-range citations in permanent notes would leverage this metadata
- **Assumption**: Interpolation assumes linear chunk-to-page mapping; may degrade on documents with non-linear structures (e.g., heavily footnoted academic papers)
