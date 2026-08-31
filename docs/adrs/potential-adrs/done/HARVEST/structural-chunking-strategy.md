# Potential ADR: Hybrid Structural Chunking (H3-H6 Sections + LangChain Splitter)

**Module**: HARVEST  
**Category**: Data Architecture / Document Segmentation Strategy  
**Priority**: Must Document (Score: 140)  
**Date Identified**: 2026-08-30  

---

## Existing ADR Context

No existing ADRs found with significant similarity. This is a foundational architectural decision for document chunking (critical to the extract/review/connect phases).

---

## What Was Identified

The HARVEST module implements a **hybrid two-stage chunking strategy** that combines structural document hierarchy with semantic text-splitter logic:

**Stage 1: Structural Sectioning by Heading Level**
- Split extracted document into **chapters** based on H1/H2 boundaries
- Function: `_split_into_chapters(text, origin_type)` (harvester.py:1400-1450)
- Method: Regex split on `^# |^## ` (Markdown heading levels 1-2)
- Result: Each chapter is a contiguous text block between H1/H2 boundaries
- Rationale: H1/H2 represent major document divisions (parts, chapters); preserve these boundaries in chunk metadata
- Fallback: Single chapter if no H1/H2 found (e.g., plain text documents, flat Markdown)

**Stage 2: Fine-Grained Chunking within Each Chapter**
- Split each chapter into **chunks** using hybrid approach
- Function: `_split_chapter_into_chunks(cfg, chapter)` (harvester.py:1570-1635)
- Two-tier strategy:
  1. **Structural sub-sections** (H3-H6 boundaries):
     - Regex split on `^### |^#### |^##### |^###### ` (heading levels 3-6)
     - Preserves mid-level structure (sections, subsections within a chapter)
     - Each structural sub-section becomes a potential chunk unit
  2. **LangChain text splitter** (when sub-section exceeds max chars):
     - Triggers when `len(sub_section) > cfg.chunking.max_chars_per_chunk`
     - Splitter: `RecursiveCharacterTextSplitter` from LangChain
     - Separators: `["\n\n", "\n", " ", ""]` (paragraphs → lines → words → chars)
     - Overlap: `chunk_overlap` (default 200 chars) for context preservation
     - Min size: `min_chunk_size` (default 50 chars) — discard tiny fragments

**Configuration Parameters** (from `config/config.yaml`):
```yaml
chunking:
  min_chars_per_chunk: 50
  max_chars_per_chunk: 2000
  chunk_overlap: 200
```

**Chunk Metadata** (persisted per chunk):
- `chunk_id` — ULID + short_hash(text)
- `chapter_id` — Source + chapter index (e.g., `@Citekey::ch001`)
- `section_path` — Hierarchical path of headings (e.g., `"## Part 1 > ### Section 1.1 > #### Subsection"`)
- `chunk_index` — Sequential index within chapter (0-based)
- `page_in_file`, `page_in_book` — Page inference (via three-layer strategy)
- `page_confidence` — "explicit"|"inferred"|"unknown"

**Workflow Integration**:
1. Extract text (Docling for PDF, native for Markdown)
2. Split into chapters (H1/H2 boundaries)
3. For each chapter:
   a. Split into structural sub-sections (H3-H6)
   b. For each sub-section:
      - If `len(sub_section) <= max_chars_per_chunk`: single chunk
      - Else: apply LangChain splitter (preserves context via overlap)
   c. Deduplicate chunks within chapter (remove duplicates via extraction hash)
4. Assign page numbers (three-layer page inference)
5. Persist to SQLite + Chroma

**Re-Chunking Workflow** (`zettel rechunk`):
- Input: Existing source with `extracted_text` persisted in SQLite
- Re-apply chunking logic without re-extracting the file
- Useful when:
  - Chunking config changes (min/max char limits, overlap)
  - Document structure is re-analyzed (new H3-H6 boundaries detected)
- Output: New chunks indexed in Chroma, old chunks deleted

This strategy was foundational at the codebase's inception. The `chunking` config schema is explicit in `config.py`; chunking parameters tunable via `config.yaml`.

## Why This Might Deserve an ADR

- **Impact**: Affects every chunk of every harvested document (~thousands of chunks per corpus). Impacts:
  - Extraction prompt context size (extract phase receives full chunk text; longer chunks = longer prompts)
  - Retrieval granularity (search returns chunks, not documents; fine grained = more results, lower recall)
  - Embedding efficiency (smaller chunks = more total embeddings, higher cost)
  - Reading experience (chapter/section navigation in permanent notes references chunk boundaries)
  - Graph connectivity (chunks become nodes; chunking strategy affects graph density)
- **Trade-offs**:
  - **Structural splitting** (H3-H6):
    - Pros: Preserves document intent (section boundaries), avoids splitting conceptual units
    - Cons: Variable chunk sizes (some sections much larger than others), irregular coverage
  - **LangChain overlap**:
    - Pros: Context preservation, smoother chunk transitions
    - Cons: Duplicate text across chunks (higher embedding cost, index bloat)
  - **Min/max char limits**:
    - Small chunks: higher retrieval precision, more embeddings (cost), smaller context per extraction
    - Large chunks: lower cost, broader context, worse retrieval precision
- **Complexity**: Two-stage hybrid approach requires:
  - Careful regex patterns (H1-H6 boundary detection)
  - LangChain splitter configuration (overlap, min size, separators)
  - Section path tracking (hierarchical heading names)
  - Metadata enrichment per chunk
- **Team Knowledge**: Critical to understand:
  - Why some chunks are 500 chars and others 2000 (structural vs. semantic splitting)
  - How to tune chunk size for different document types (research papers vs. books vs. plain text)
  - Impact of `chunk_overlap` on embedding cost (each overlapping word re-embedded)
  - How section_path helps with navigation/discovery
- **Long-term Implications**:
  - Chunk boundaries are immutable (changing config requires rechunk + re-embed all sources)
  - Overlapping text complicates deduplication (same text appears in multiple chunks)
  - Section paths are hierarchical; documents without H3-H6 lose structural metadata
  - Future search features (e.g., "find section X") depend on section_path accuracy

## Evidence Found in Codebase

### Key Files

- [`zettel/harvester.py:1400-1450`](../../../zettel/harvester.py) — Stage 1: Chapter splitting
  - `_split_into_chapters(text, origin_type)` — H1/H2 boundary detection

- [`zettel/harvester.py:1570-1635`](../../../zettel/harvester.py) — Stage 2: Fine-grained chunking
  - `_split_chapter_into_chunks(cfg, chapter)` — Hybrid structural + LangChain splitter
  - H3-H6 boundary detection
  - Conditional LangChain splitting

- [`zettel/config.py`](../../../zettel/config.py) — Configuration schema
  - `ChunkingConfig` dataclass with min_chars_per_chunk, max_chars_per_chunk, chunk_overlap

- [`config/config.yaml`](../../../config/config.yaml) — Operational defaults
  ```yaml
  chunking:
    min_chars_per_chunk: 50
    max_chars_per_chunk: 2000
    chunk_overlap: 200
  ```

- [`zettel/paging.py:128-143`](../../../zettel/paging.py) — Section path tracking
  - Hierarchical section names preserved per chunk

### Code Evidence

```python
# Stage 1: Chapter splitting (harvester.py:1400-1450)
def _split_into_chapters(text: str, origin_type: str) -> list[dict[str, str]]:
    """Split text into chapters by H1/H2 boundaries."""
    pattern = re.compile(r"^(# |## )", re.MULTILINE)
    sections = pattern.split(text)
    # Reconstruct: [header, body, header, body, ...]
    chapters = []
    for i in range(1, len(sections), 2):
        heading = sections[i].strip()
        body = sections[i + 1] if i + 1 < len(sections) else ""
        chapters.append({
            "heading": heading,
            "body": body,
        })
    return chapters or [{"heading": "Untitled", "body": text}]

# Stage 2: Fine-grained chunking (harvester.py:1570-1635)
def _split_chapter_into_chunks(cfg: AppConfig, chapter: dict[str, str]) -> list[tuple[str, str]]:
    """Hybrid chunking: H3-H6 sections + LangChain overlap."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    
    body = chapter["body"]
    h3_pattern = re.compile(r"^(### |#### |##### |###### )", re.MULTILINE)
    subsections = h3_pattern.split(body)
    
    chunks = []
    for i in range(0, len(subsections), 2):
        section_heading = subsections[i].strip() if i % 2 == 1 else ""
        section_body = subsections[i + 1] if i + 1 < len(subsections) else subsections[i]
        
        # Single chunk if within limit
        if len(section_body) <= cfg.chunking.max_chars_per_chunk:
            chunks.append((section_heading, section_body))
        else:
            # Apply LangChain splitter with overlap
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=cfg.chunking.max_chars_per_chunk,
                chunk_overlap=cfg.chunking.chunk_overlap,
                separators=["\n\n", "\n", " ", ""],
            )
            split_chunks = splitter.split_text(section_body)
            for split_text in split_chunks:
                if len(split_text) >= cfg.chunking.min_chars_per_chunk:
                    chunks.append((section_heading, split_text))
    
    return chunks

# Configuration schema (config.py)
@dataclass
class ChunkingConfig:
    min_chars_per_chunk: int = 50
    max_chars_per_chunk: int = 2000
    chunk_overlap: int = 200
```

### Impact Analysis

- **Introduced**: Foundational; chunking logic appears in early codebase versions
- **Modified**: Stable; core chunking strategy unchanged since inception
- **Themes**: "chunking", "structural", "langchain", "overlap", "granularity"
- **Affects**: Every chunk of every source (~thousands of chunks per corpus, 100% of harvest)
- **Test Coverage**: Partial; `tests/test_harvester_sections.py` covers chunking edge cases

### Alternatives (Observed or Implied)

1. **Single-stage semantic chunking (LangChain only, no structural stage)**
   - Pros: Simpler logic, uniform chunk sizes, predictable cost
   - Cons: Loses document structure, chunks may split sections mid-sentence
   - **Rejected**: Structure preservation important for document understanding

2. **Flat chunking (fixed-size windows, no overlap)**
   - Pros: Deterministic, no overlap complexity
   - Cons: No context preservation, chunks may span unrelated sections
   - **Partial adoption**: LangChain overlap optional (config tunable to 0)

3. **Full-document chunking (one chunk per document)**
   - Pros: No chunking overhead, maximum context per extraction
   - Cons: Poor retrieval precision, large prompts, expensive embeddings
   - **Rejected**: Granularity essential for search and extraction

4. **Sentence-level chunking**
   - Pros: High precision, minimal overlap
   - Cons: Very small context per extraction, too granular for coherent prompts
   - **Rejected**: Too fine-grained for extraction LLM

5. **Hybrid with LLM-based section detection**
   - Pros: Adaptive to document type
   - Cons: Complexity, cost, cold-start problem
   - **Rejected**: Regex sufficient for known document types

## Questions to Address in ADR (if created)

- Should chunking config be per-source (adaptive to document type) or global? (Currently global; no per-source override)
- What happens when a section is exactly at the boundary (==max_chars_per_chunk)? (Remains single chunk; no split)
- Does LangChain splitter preserve all content, or can overlaps result in loss? (Content preserved; overlapping text is duplicated, not lost)
- How does overlap interact with deduplication? (Overlapping text may be flagged as duplicates across chunks; dedupe logic handles this)
- Should very small chunks be merged with neighbors, or kept as-is? (Kept as-is; no post-merge logic)

## Related Potential ADRs

- **HARVEST/three-layer-page-inference-strategy** — Page assignment depends on chunk boundaries; chunking affects page inference accuracy
- **HARVEST/docling-pdf-extraction-with-pymupdf-fallback** — Docling structure (H1-H6) is foundational to structural chunking
- **INFRA/layered-hashing-strategy** — Chunk checksums depend on final chunk text (after chunking); hashing is deterministic per chunk_id

## Additional Notes

- **Temporal context**: Strategy foundational, stable for entire codebase history
- **Configuration exposure**: Min/max/overlap all tunable via `config.yaml`
- **Optimization opportunity**: Rechunk currently re-processes all chunks even if config only changed for unrelated parameter (no fine-grained invalidation)
- **Overlap cost**: Overlapping text is re-embedded; at 200-char overlap, significant embedding budget amplification (~10-15% additional embeddings per corpus)
- **Testing**: Partial coverage; `tests/test_harvester_sections.py` covers chunking, but integration with page inference needs more coverage
- **Section path tracking**: Hierarchical heading names enable future search features (e.g., "show all chunks from section X")
- **Assumption**: Structural chunking assumes documents follow H1-H6 hierarchy; flat or single-level documents default to single chapter
