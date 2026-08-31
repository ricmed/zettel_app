# Potential ADR: Separation of Retrieval from Deduplication Logic

**Module**: RETRIEVAL  
**Category**: Retrieval Architecture / System Boundaries  
**Priority**: Consider (Score: 88)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The retrieval module (`zettel/retrieval.py`, `zettel/graph.py`) provides hybrid search via RRF fusion and optional graph expansion. However, two other subsystems deliberately **do not use** this retrieval path:

1. **Harvester layer-3 (duplicate detection)**: Uses raw L2 vector distance (`harvest.duplicate_chunk_threshold`, default 0.88) to detect semantic duplicates across sources
2. **Extractor dedupe (extraction overlap detection)**: Uses raw L2 distance with extractor-specific thresholds

The module docstring explicitly states this intentional separation:
> "Every consumer that wants richer recall (`ask`, `connect`, `sync`) goes through :class:`Retriever`; the threshold-calibrated consumers (extractor dedupe, harvester layer-3) deliberately stay on the raw vector distance and do not use this."

This separation is a **deliberate architectural choice**, not an oversight. It reflects a design principle: threshold-calibrated operations (dedupe, quality gates) use raw signals; user-facing retrieval (search, discovery) uses rich composition.

**Introduced**: Implicit in the initial retrieval design (commit `2d6ff27`, 2026-07-18). The separation is documented but not formalized as a decision.

**Stable**: Since introduction, harvester and extractor have NOT been modified to use Retriever. This suggests the separation is intentional and stable.

---

## Why This Might Deserve an ADR

- **Impact**: Affects how deduplication quality is tuned independently from search quality.
- **Architectural Principle**: Encodes a boundary between:
  - **Internal quality gates** (dedupe, harvest layer-3): Use raw L2 distance with task-specific thresholds
  - **User-facing retrieval** (ask, article, connect, sync): Use rich RRF + floor + optional graph
- **Threshold Calibration**: Harvester uses `duplicate_chunk_threshold: 0.88`; this threshold is calibrated on raw L2, not RRF scores. If harvester switched to Retriever:
  - RRF fusion would change ranking order (BM25 + vector combined)
  - Relevance floor would gate results (some duplicates might fail the floor)
  - Graph expansion would add neighbours (potentially confusing dedupe logic)
  - Would need to recalibrate 0.88 to work with RRF + floor
- **Cost to Change**: Switching harvester/extractor to use Retriever would require:
  - Revalidating all dedupe thresholds on RRF + floor results
  - Potentially reprocessing historical duplicates
  - Risk of false negatives (duplicates that used to match now fail floor)
- **Team Knowledge**: Important for understanding why dedupe logic is separate; prevents accidental conflation of concerns.
- **Temporal Context**: Introduced 2026-07-18, stable for 44 days. The persistence of this separation suggests it's intentional.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/retrieval.py`](../../../zettel/retrieval.py) - Lines 1-16 (module docstring) document the separation
- [`zettel/harvester.py`](../../../zettel/harvester.py) - Uses raw L2 distance for layer-3 duplicate detection
- [`zettel/extractor.py`](../../../zettel/extractor.py) - Uses raw L2 distance for overlap detection
- [`zettel/config.py`](../../../zettel/config.py) - Lines 67-76 define HarvestConfig with `duplicate_chunk_threshold`

### Code Evidence

```python
# From zettel/retrieval.py (module docstring):
"""Hybrid retrieval: dense vectors (Chroma) + BM25 lexical (FTS5) + graph.

Historically every RAG lookup in the pipeline went straight to Chroma's dense
nearest-neighbour search. This module adds a single composition point that fuses
that dense ranking with the BM25 lexical ranking from the SQLite FTS5 index
(Reciprocal Rank Fusion), then optionally expands the top results 1..N hops over
the typed note-connection graph. Every consumer that wants richer recall
(``ask``, ``connect``, ``sync``) goes through :class:`Retriever`; the
threshold-calibrated consumers (extractor dedupe, harvester layer-3) deliberately
stay on the raw vector distance and do not use this.

RRF is used instead of score normalisation because it only needs the *rank* of an
id in each list, so the incompatible scales of L2 distance and bm25 rank never
have to be reconciled. Ids are shared across Chroma and SQLite (same note_id /
chunk_id), so fusion is a direct dictionary merge.
"""

# From zettel/harvester.py (_process_file) — layer-3 duplicate detection:
def _process_file(self, file_path: Path) -> Optional[Source]:
    # ... layers 1-2 (file hash, extraction hash) ...
    
    # Layer 3: Semantic similarity (raw L2 distance, not RRF)
    candidates = self._find_semantic_duplicate_candidates(
        new_chunks,
        threshold=self.cfg.harvest.duplicate_chunk_threshold  # 0.88 by default
    )
    # ... uses raw Chroma.query() results, not Retriever

# From zettel/extractor.py (extraction overlap detection):
def _find_overlapping_chunks(self, chunks: list[Chunk]) -> list[Chunk]:
    # Uses raw L2 distance from Chroma, not RRF + floor
    # Each chunk's embedding is compared to existing chunk embeddings
    # Overlaps rejected if similarity > some threshold
    # Does not go through Retriever.search_notes()

# From zettel/config.py (HarvestConfig):
class HarvestConfig(BaseModel):
    """Dedupe em 3 camadas (hash arquivo/texto + similaridade) e metadados ABNT."""

    duplicate_chunk_threshold: float = 0.88  # Raw L2 distance threshold
    duplicate_sample_size: int = 5           # Number of chunks to sample
    non_interactive_duplicate_action: Literal["skip", "continue", "abort"] = "skip"
    # ... other fields ...
```

### Comparison: Raw L2 vs. Retriever

| Aspect | Raw L2 (Dedupe) | Retriever (Search) |
|--------|-----------------|-------------------|
| **Thresholds** | Explicit: 0.88 for chunks, 0.90 for links | Implicit: relevance floor with 4 gates |
| **Scoring** | Raw L2 distance (dense only) | RRF fusion (dense + BM25) |
| **Ranking** | Proximity-only | Positional (rank-based) |
| **Filtering** | Single threshold per operation | Multi-step gating (absolute_min, bm25_bypass, min_vector_similarity) |
| **Graph expansion** | None | Optional (1 hop, weighted) |
| **Calibration** | Corpus-dependent; dataset-specific | Corpus + embedding model dependent |
| **Use cases** | Duplicate detection, overlap detection | Ask, article, connect, sync |

### Why Separation Matters

1. **Different precision/recall trade-off**:
   - Dedupe wants high precision (minimize false positives) to avoid discarding unique sources
   - Search wants higher recall (accept some noise) to surface relevant context
   - Same threshold doesn't work for both

2. **Threshold calibration is task-specific**:
   - Harvester's 0.88 is chosen to preserve unique sources (conservative)
   - Search's relevance floor (0.70) is chosen to answer user queries (moderate)
   - Mixing them confuses the intent

3. **Computational cost**:
   - Dedupe runs on raw chunks during harvest (many candidates, frequent)
   - Search runs on RRF-fused results (fewer candidates, user-triggered)
   - Different performance requirements

4. **Error modes**:
   - Dedupe false positive: loses a unique source (bad)
   - Dedupe false negative: keeps a duplicate (acceptable, can merge later)
   - Search false positive: returns irrelevant note (visible, user can dismiss)
   - Search false negative: misses relevant note (silent, but less critical)

---

## Questions to Address in ADR (if created)

1. Could harvester/extractor use Retriever with custom thresholds?
   - Answer likely: Yes, but would require careful recalibration. Threshold 0.88 is calibrated on raw L2; RRF changes the score scale.

2. What would break if harvester switched to using Retriever?
   - Answer likely: False negatives (duplicates not detected) if thresholds weren't recalibrated; performance hit from RRF overhead; potential duplicates that fail relevance floor.

3. Should this separation be formalized in code (e.g., separate DedupRetriever class)?
   - Answer likely: Current separation is implicit (different code paths); could be explicit via API.

4. Does the separation prevent accidental coupling?
   - Answer likely: Yes; if harvester called Retriever, changes to RRF/floor would affect dedupe.

5. Should dedupe thresholds be exposed in RetrievalConfig for visibility?
   - Answer likely: No; they're in HarvestConfig because they're harvest concerns, not retrieval concerns.

---

## Related Potential ADRs

- **hybrid-dense-bm25-retrieval.md**: Explains the RRF approach used only by Retriever; dedupe deliberately avoids this
- **three-layer-duplicate-detection** (if created): Documents all 3 harvest layers, explaining why layer-3 uses raw L2
- **extractor-deduplication-strategy** (if created): Extraction-phase overlap detection, complementary to harvest dedupe

---

## Additional Notes

- The separation is documented in the retrieval module docstring but not formalized as a design pattern
- Harvester layer-3 runs `_find_semantic_duplicate_candidates()` which directly queries Chroma with raw L2 distance (does not use Retriever)
- Extractor overlap detection similarly queries Chroma directly (not through Retriever)
- If harvester/extractor ever moved to use Retriever, the thresholds would need revalidation on a test corpus
- The implicit boundary (documented but not enforced) could be made explicit via code comments or a design document
- No tests explicitly verify that harvester/extractor avoid Retriever; the separation is architectural intent, not runtime enforcement
