# Potential ADR: Docling Config Hash Strategy for Extracted Text Caching

**Module**: HARVEST  
**Category**: Performance / Caching Strategy  
**Priority**: Consider (Score: 88)  
**Date Identified**: 2026-08-30  

---

## Existing ADR Context

No existing ADRs found with significant similarity. This is an optimization decision affecting harvest re-run efficiency.

---

## What Was Identified

The HARVEST module implements a **Docling configuration hash strategy** to detect when a PDF's **extracted text** (and consequently its chunks) may need to be re-generated due to a change in Docling extraction parameters.

**How It Works**:

1. **Hash Computation** (`compute_docling_config_hash` in paging.py):
   - Collects all Docling-relevant config values:
     - Docling version (implicit in library version)
     - Pipeline options (accelerator device, image settings, etc.)
     - LangChain chunking parameters (min/max char limits, overlap)
     - Harvest config (content-start paging, duplicate thresholds)
   - Computes SHA256 hash of the serialized config dict
   - Result: A stable hash representing "extraction configuration state"

2. **Storage** (per source):
   - Persisted in `sources.docling_config_hash` (SQLite)
   - Also stored in source SRC note frontmatter for visibility
   - Compared on re-harvest: if current hash != stored hash, a warning is logged

3. **Usage during Harvest**:
   - When processing a file: compute current config hash
   - Compare with hash from existing source (if reusing source_id)
   - If mismatch: log warning suggesting `zettel rechunk --source-id @Citekey`
   - No automatic action taken; user must manually rechunk if desired

4. **Rechunk Workflow** (`zettel rechunk`):
   - Re-applies chunking to existing `extracted_text` without re-extracting the file
   - Avoids expensive PDF re-processing (Docling is slow)
   - Updates `docling_config_hash` to current value
   - Deletes old chunks from SQLite + Chroma, re-indexes new chunks

**Configuration Hash Inputs** (from harvester.py:90-96):
```python
signature = compute_pipeline_signature({
    "chunking": cfg.chunking.model_dump(),
    "harvest": cfg.harvest.model_dump(),
    "images": cfg.images.model_dump(),
    "pdf_extractor": cfg.pdf_extractor,
    "docling_config_hash": compute_docling_config_hash(cfg),
})
```

**Scenarios Triggering Hash Mismatch**:
- Docling library upgraded (new extraction algorithm)
- `images.enabled` toggled (changes Docling pipeline options)
- `images.scale` changed (affects image resolution in extraction)
- Device changed from CPU to CUDA (affects extraction via different accelerator)
- Chunking parameters modified (min/max char limits, overlap)

**Behavior on Mismatch**:
- Non-blocking: harvest continues with new chunks
- Warning logged: "docling_config_hash mudou (...). Use `zettel rechunk --source-id @Citekey`..."
- User can ignore warning or run rechunk manually
- No automatic deletion of old chunks (user retains optionality)

This strategy appears in the codebase as a safeguard against silent extraction divergence, particularly useful when:
- Docling is upgraded (library versions may extract differently)
- Configuration changes (e.g., enabling images mid-project)
- Users collaborate and have different Docling/config versions

## Why This Might Deserve an ADR

- **Impact**: Affects accuracy/consistency of extracted content across re-harvests. Impacts:
  - Re-harvest robustness (detects when re-extraction might differ)
  - Compliance/auditability (tracks extraction config changes)
  - Collaboration workflows (different machines with different Docling versions)
  - Cost planning (hash mismatch hints at potential embedding recalculation)
- **Trade-offs**:
  - **Automatic re-chunking on mismatch**:
    - Pros: Transparent, ensures consistency
    - Cons: Silent cost (embeddings recalculated), may break user workflows
  - **Current approach (warning + manual opt-in)**:
    - Pros: Explicit, user control, no hidden cost
    - Cons: Requires manual intervention, users may miss warnings
  - **No hash checking at all**:
    - Pros: Simpler code
    - Cons: Silent divergence if Docling upgraded; users unaware chunks may differ
- **Complexity**: Requires:
  - Comprehensive config collection (capturing all extraction-relevant parameters)
  - Stable hash computation (deterministic across runs)
  - State tracking (persisting hash in SQLite + vault)
  - User communication (clear warning message)
- **Team Knowledge**: Important to understand:
  - Why a hash mismatch warning appears
  - When rechunking is necessary vs. optional
  - Cost implications of config changes (e.g., toggling images)
  - How to recover from diverged extraction (rechunk workflow)
- **Long-term Implications**:
  - Hash brittleness: Any config change triggers warning (may lead to alert fatigue)
  - Backward compatibility: Old sources with no hash (NULL) are handled gracefully (no warning)
  - Future schema changes: Adding new Docling options requires updating hash inputs

## Evidence Found in Codebase

### Key Files

- [`zettel/paging.py`](../../../zettel/paging.py) — `compute_docling_config_hash(cfg)` function
  - Serializes Docling-relevant config to dict
  - Computes SHA256 hash

- [`zettel/harvester.py:90-96`](../../../zettel/harvester.py) — Hash computation during harvest
  - Creates pipeline signature including docling_config_hash
  - Stored in run metadata

- [`zettel/harvester.py:545-556`](../../../zettel/harvester.py) — Hash comparison on file reprocess
  ```python
  config_hash = compute_docling_config_hash(cfg)
  existing = db.get_source(sid)
  if src and src.get("docling_config_hash") and src["docling_config_hash"] != config_hash:
      logger.warning(
          "Fonte %s: docling_config_hash mudou (%s -> %s). "
          "Use `zettel rechunk --source-id %s` para reaplicar chunking.",
          sid, src["docling_config_hash"], config_hash, sid,
      )
  ```

- [`zettel/state.py`](../../../zettel/state.py) — SQLite schema
  - `sources.docling_config_hash` column stores hash

- [`zettel/vault.py`](../../../zettel/vault.py) — Source note builder
  - SRC frontmatter includes `docling_config_hash` for visibility

### Code Evidence

```python
# Hash computation (paging.py)
def compute_docling_config_hash(cfg: AppConfig) -> str:
    """Compute a stable hash of Docling-relevant configuration."""
    config_dict = {
        "docling_version": ...,  # Inferred from library
        "device": cfg.device,
        "images_enabled": cfg.images.enabled,
        "images_scale": cfg.images.scale,
        "chunking_min": cfg.chunking.min_chars_per_chunk,
        "chunking_max": cfg.chunking.max_chars_per_chunk,
        "chunking_overlap": cfg.chunking.chunk_overlap,
    }
    return sha256_hex(json.dumps(config_dict, sort_keys=True).encode())

# Hash comparison during harvest (harvester.py:545-556)
config_hash = compute_docling_config_hash(cfg)
if existing and existing["file_checksum"] == checksum:
    sid = existing.get("source_id")
    if sid:
        src = db.get_source(sid)
        if src and src.get("docling_config_hash") and src["docling_config_hash"] != config_hash:
            logger.warning(
                "Fonte %s: docling_config_hash mudou (%s -> %s). "
                "Use `zettel rechunk --source-id %s` para reaplicar chunking.",
                sid, src["docling_config_hash"], config_hash, sid,
            )

# Rechunk workflow (harvester.py:159-200)
def run_rechunk(cfg: AppConfig, db: StateDB, idx: VectorIndex, source_id: str | None = None) -> dict[str, int]:
    """Re-chunk sources from persisted extracted_text without re-extracting the file."""
    # Re-apply chunking to existing extracted_text
    chapters = _split_into_chapters(text, src["origin_type"])
    # ... chunking logic ...
    n = _chunk_and_persist(cfg, db, idx, source_id, chapters, ...)
    
    # Update config hash to current value
    db.update_source(source_id, docling_config_hash=compute_docling_config_hash(cfg))
    return {"sources": 1, "chunks": n, "skipped": 0}
```

### Impact Analysis

- **Introduced**: Strategy appears foundational; `docling_config_hash` field present in schema
- **Modified**: Stable; no changes to hash computation or comparison logic
- **Themes**: "caching", "extraction", "config", "consistency"
- **Affects**: Re-harvest workflows (users re-harvesting documents with config changes)
- **Cost impact**: Hash mismatch → manual rechunk → re-embedding (potentially significant)

### Alternatives (Observed or Implied)

1. **Automatic rechunk on hash mismatch**
   - Pros: Transparent consistency, no user action required
   - Cons: Silent cost (embeddings recalculated), unexpected side effects
   - **Rejected (implicitly)**: Current approach favors user opt-in

2. **No hash checking**
   - Pros: Simpler code, no warnings
   - Cons: Silent divergence if Docling upgraded, users unaware
   - **Rejected (implicitly)**: Consistency important for collaboration/auditing

3. **Hash-based content versioning** (track multiple extraction versions per source)
   - Pros: No data loss, can revert to previous chunks
   - Cons: Complex schema, storage overhead
   - **Rejected (implicitly)**: Simple overwrite sufficient

4. **Per-source config override**
   - Pros: Different sources can use different extraction settings
   - Cons: Complexity, configuration sprawl
   - **Rejected (implicitly)**: Global config sufficient

## Questions to Address in ADR (if created)

- Should hash mismatch automatically trigger rechunk, or remain opt-in? (Current: opt-in, warning-based)
- What happens if a user ignores the hash-mismatch warning? (Chunks remain unchanged; extraction divergence silent)
- Can users suppress hash-mismatch warnings for benign changes (e.g., image scaling)? (Currently: all changes treated equally)
- Should backward compatibility be maintained for sources with NULL docling_config_hash? (Yes; no warning if hash not previously stored)
- How frequently should docs recommend rechunking? (Currently: only on explicit config change)

## Related Potential ADRs

- **HARVEST/docling-pdf-extraction-with-pymupdf-fallback** — Hash inputs include device/images config related to Docling
- **HARVEST/structural-chunking-strategy** — Chunking parameters feed into hash computation
- **INFRA/layered-hashing-strategy** — Chunk-level hashing is separate from config-level hashing (different purposes)

## Additional Notes

- **Temporal context**: Strategy foundational, stable for entire codebase history
- **Configuration exposure**: Hash inputs not directly tunable (computed from config.yaml)
- **User visibility**: Hash visible in SRC note frontmatter for transparency
- **Testing**: No explicit tests for hash computation or mismatch scenarios (could add)
- **Backward compatibility**: Sources harvested before hash implementation have NULL field; handled gracefully (no warning)
- **Cost awareness**: Users should be aware that rechunk costs embeddings (all chunks re-embedded in Chroma)
- **Debugging**: Hash stored in SQLite + vault; easy to compare and debug divergence
- **Future enhancement**: Could track hash history (audit trail of config changes over time)
