# Potential ADR: Three-Layer Duplicate Detection Strategy for Source Ingestion

**Module**: HARVEST  
**Category**: Data Architecture / Deduplication Strategy  
**Priority**: Must Document (Score: 145)  
**Date Identified**: 2026-08-30  

---

## Existing ADR Context

No existing ADRs found with significant similarity. This is a domain-critical decision foundational to the harvest phase.

---

## What Was Identified

The HARVEST module implements a **three-layer duplicate detection strategy** that runs **before** a file is treated as a new source. Each layer is progressively more expensive but more semantically meaningful, designed to catch duplicates at different fidelity levels:

1. **Layer 1: File Hash (Byte-Level Checksum)**
   - Detects byte-identical files at different filesystem paths (renamed/moved copies)
   - Uses SHA256 checkssum via `file_sha256(file_path)` (hashing.py)
   - Cheapest check: O(file size)
   - If match found, reuses the existing `source_id`, no reprocessing (harvester.py:563-575)

2. **Layer 2: Extraction Hash (Normalized Content Checksum)**
   - Detects the same article ingested in different formats (PDF + Markdown + plain text)
   - Normalizes extracted text via `normalize_text_for_hash()` (NFKC, whitespace collapse, PDF dehyphenation)
   - Compares SHA256 of normalized extraction (harvester.py:585-599)
   - Moderate cost: requires text extraction, then hash computation
   - If match found, reuses existing source, no new chunks or citekey (cross-format deduplication)

3. **Layer 3: Semantic Similarity (ChromaDB Vector Search)**
   - Detects near-duplicate content via embedding similarity
   - Samples chunks from the new document (evenly distributed; `_sample_chunk_texts`)
   - Queries Chroma `chunks` collection for near-duplicates using `VectorIndex.find_similar_chunks()`
   - Converts L2 distance → similarity: `similarity = 1 - (distance / 2)`
   - Threshold: configurable `harvest.duplicate_chunk_threshold` (default 0.88)
   - Most expensive: requires embedding generation + vector search
   - If candidates found above threshold, prompts user interactively or applies non-interactive action

**Decision Flow** (all layers run in sequence, first match wins):
- Unchanged file → skip (no layer check)
- Layer 1 match → reuse source_id
- Layer 2 match → reuse source_id  
- Layer 3 match → prompt user (interactive) or apply config default (non-interactive)

**User Control**:
- Interactive mode: Rich prompt with table of candidates (citekey, title, similarity %), choose skip/continue/abort
- Non-interactive mode: `--skip-duplicates` (skip), `--force` (continue), or config default `harvest.non_interactive_duplicate_action`
- CLI commands: `harvest`, `run-all` (both support these flags)
- Web: harvest job enqueues with `interactive=False`, applies config default

**Decision Recording** (StateDB):
- Each decision recorded via `record_duplicate(run_id, layer)` (layer = "file"|"content"|"semantic")
- `status` command prints "Duplicatas — Última Execução do Harvest" table with counts

This strategy was introduced with commit **a542911** (2026-07-04), with full test coverage in `tests/test_harvester_dedup.py` (11 tests covering all three layers + decision resolution). Temporal context: stable for ~60 days, confirmed production-ready with zero regressions.

## Why This Might Deserve an ADR

- **Impact**: Affects every source ingestion; blocks non-duplicate sources from entering the vault. Prevents duplicate LLM processing (extract/connect phases) and redundant embeddings.
- **Trade-offs**: 
  - Layer 1 is O(file size) but only catches renamed files; false negatives on format-converted duplicates.
  - Layer 2 requires text extraction overhead but catches cross-format duplicates (same article as PDF + Markdown).
  - Layer 3 requires vector embedding but catches semantic near-duplicates (reformatted, lightly edited articles).
  - Cost: sampling + embedding generation, tunable via `duplicate_sample_size` (default 5).
- **Complexity**: Non-trivial orchestration of three independent checks with clear precedence and decision routing (skip/continue/abort).
- **Team Knowledge**: Essential for users to understand:
  - Why a file-at-different-path doesn't create a new source
  - Why a PDF re-exported as Markdown doesn't duplicate processing
  - How to override duplicate detection (--force, --skip-duplicates, config)
- **Long-term Implications**: 
  - Threshold tuning (`duplicate_chunk_threshold`) requires corpus-specific calibration; default 0.88 was chosen empirically.
  - False positives (rejecting legitimately distinct sources) + false negatives (accepting true near-duplicates) both have user friction.
  - Decision is irreversible: once a source_id is reused, re-extracting that document is non-trivial.

## Evidence Found in Codebase

### Key Files

- [`zettel/harvester.py:527-599`](../../../zettel/harvester.py) — `_process_file()` orchestrates all three layers in sequence
  - Layer 1: lines 563-575 (file hash check + reuse)
  - Layer 2: lines 587-599 (extraction hash check + reuse)
  - Layer 3: invoked after Layer 2 (not shown in excerpt but at line ~600+)
  
- [`zettel/harvester.py:841-887`](../../../zettel/harvester.py) — `_find_semantic_duplicate_candidates()` 
  - ChromaDB query via `VectorIndex.find_similar_chunks(sample_texts, n_results=3)`
  - Aggregates best similarity score per source
  - Returns candidates sorted by similarity (descending)

- [`zettel/harvester.py:890-930`](../../../zettel/harvester.py) — `_resolve_duplicate_decision()` 
  - Interactive vs. non-interactive routing
  - Rich table rendering for interactive prompt
  - Config default fallback: `cfg.harvest.non_interactive_duplicate_action`

- [`zettel/state.py`](../../../zettel/state.py) — StateDB methods
  - `get_file_by_checksum(checksum, exclude_path)` — Layer 1 lookup
  - `get_source_by_extraction_checksum(extraction_checksum)` — Layer 2 lookup
  - `record_duplicate(run_id, layer)` — Decision recording

- [`zettel/index.py`](../../../zettel/index.py) — VectorIndex method
  - `find_similar_chunks(texts, n_results)` — Layer 3 query

- [`config/config.yaml`](../../../config/config.yaml) — Configuration tuning
  ```yaml
  harvest:
    duplicate_chunk_threshold: 0.88
    duplicate_sample_size: 5
    non_interactive_duplicate_action: skip  # skip | continue | abort
  ```

### Code Evidence

```python
# Layer 1: File hash check (harvester.py:562-575)
checksum = file_sha256(file_path)
existing = db.get_file(str(file_path))
# ...
renamed_from = db.get_file_by_checksum(checksum, exclude_path=str(file_path))
if renamed_from and renamed_from.get("source_id"):
    logger.info("Arquivo '%s' e uma copia identica de '%s' (mesmo hash de arquivo)...", ...)
    sid = renamed_from["source_id"]
    db.upsert_file(str(file_path), checksum, file_path.suffix.lower().lstrip("."), sid)
    db.record_duplicate(run_id, "file")
    return None, empty_stats  # ← Reuse without reprocessing

# Layer 2: Extraction hash check (harvester.py:585-599)
extraction_checksum = sha256_hex(normalize_text_for_hash(text))
cross_format_source = db.get_source_by_extraction_checksum(extraction_checksum)
if cross_format_source:
    sid = cross_format_source["source_id"]
    logger.info("Conteudo de '%s' e identico (apos normalizacao) a fonte existente...", ...)
    db.record_duplicate(run_id, "content")
    return None, empty_stats  # ← Reuse without reprocessing

# Layer 3: Semantic similarity (harvester.py:841-887)
matches = idx.find_similar_chunks(sample_texts, n_results=3)
for m in matches:
    distance = m.get("distance")
    similarity = 1 - (distance / 2)
    if similarity >= threshold:
        candidates.append({...})
decision = _resolve_duplicate_decision(file_path, candidates, interactive, duplicate_action, cfg)
if decision == "skip":
    return None, empty_stats
elif decision == "abort":
    raise HarvestAborted(...)
```

### Impact Analysis

- **Introduced**: 2026-07-04 (commit a542911)
- **Modified**: Stable since introduction; no changes to core three-layer logic
- **Themes**: "duplicate detection", "data quality", "ingestion robustness"
- **Affects**: Every source ingestion (100% of harvest runs touch this logic)
- **Test Coverage**: 11 tests in `tests/test_harvester_dedup.py` (layer 1, 2, 3, decision resolution)

### Alternatives (Observed or Documented)

1. **Single-layer deduplication** (file hash only)
   - Simpler, no embedding cost
   - **Rejected**: Would not catch format-converted duplicates or semantic near-duplicates
   - Inferred from the decision to implement three layers

2. **Vector-only deduplication** (skip layers 1 & 2)
   - Single query, deterministic
   - **Rejected**: Misses byte-identical and format-converted duplicates (waste of embedding budget)
   - Evident from layers 1 & 2 implementation

3. **Configurable threshold only** (no binary threshold, soft ranking)
   - Return ranked candidates, let caller decide
   - **Partial adoption**: candidates are ranked, but binary threshold is applied for user prompt trigger
   - Threshold tuning observed as explicit concern in harvest config

## Questions to Address in ADR (if created)

- What happens when Layer 3 finds multiple candidates with similar scores? (Captured: best per source_id, then sorted by similarity descending)
- Should Layer 3 threshold be per-corpus calibrated or fixed? (Documented as configurable, default 0.88 is empirical)
- Can a user force accept a Layer 1/2 duplicate without interaction? (Yes, via `--force` / `continue` option)
- How is the decision logged for audit? (StateDB `duplicate` records; `status` command shows summary)
- What is the cost of Layer 3 in terms of embeddings? (Sampled chunks only, configurable sample_size; cost tracked in `runs` row)

## Related Potential ADRs

- **INFRA/hybrid-dense-bm25-retrieval** — Layer 3 uses raw L2 distance, not RRF (deliberate separation per CLAUDE.md)
- **HARVEST/docling-pdf-extraction** — Layer 2 deduplication depends on consistent text extraction (format-invariant)

## Additional Notes

- **Temporal context**: Stable for ~60 days (introduced 2026-07-04, no regressions)
- **Configuration exposure**: Fully configurable via `config.yaml` (threshold, sample size, non-interactive action)
- **Testing**: Comprehensive coverage of all three layers plus interactive/non-interactive paths
- **Known limitation**: Layer 3 sampling is deterministic but not uniform across document types (longer docs → more samples by evenly-spaced distribution)
- **Observability**: Deduplication decisions recorded and summarized in `zettel status` output
