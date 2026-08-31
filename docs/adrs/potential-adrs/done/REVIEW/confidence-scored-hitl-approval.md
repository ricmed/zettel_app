# Potential ADR: Confidence-Scored HITL Approval with Band-Based UX

**Module**: REVIEW  
**Category**: Human-in-the-Loop (HITL) / Approval Gate / Pipeline Stage  
**Priority**: Consider (Score: 78)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The REVIEW module implements a structured, confidence-guided human-in-the-loop approval process for literature note drafts. Rather than automatic approval or fully manual review, the system:

1. **Partitions drafts into three confidence bands**:
   - Very low: `review_confidence <= 0.4`
   - Medium: `0.4 < confidence < auto_approve_min_confidence`
   - High: `confidence >= auto_approve_min_confidence` (configurable, default 0.7)

2. **Offers multiple review modes**:
   - **Batch approve** (mode "a"): Auto-approve all drafts >= threshold; below-threshold drafts remain `awaiting_review`
   - **Batch reject** (mode "d"): Reject all drafts in a chosen band with confirmation
   - **One-by-one** (mode "r"): Review sample drafts individually with shortcuts (a=approve, r=reject, p=skip, q=exit)
   - **Non-interactive / Auto** (CLI flags): Non-interactive mode respects threshold; auto-approve=False still checks threshold

3. **Persists all decisions** into SQLite immediately (not in-memory staging):
   - Approved drafts: `status=persisted`, embedded into Chroma `literature_notes` collection
   - Rejected drafts: `status=rejected`, marked for later purge (soft delete pattern)
   - Skipped drafts: remain `awaiting_review`

4. **Concept deduplication timing**: Post-approval dedup runs AFTER each batch operation (via `_dedupe_approved_concepts`), reusing extractor's `deduplicate_candidates` logic to collapse equivalent concept definitions before handoff to CONNECT.

This pattern emerged from 2026-08-29 web UI implementation (commit 5d9b504) and earlier HITL refinements (2026-08-29 11:11:15, "feat(review): improve HITL bands, purge rejected, and VACUUM").

## Why This Might Deserve an ADR

- **Impact**: The confidence-band approach shapes how the EXTRACTOR generates confidence scores (a coupling decision), how the CLI/web UX present options, and how the CONNECT phase expects downstream data. Moving to a different approval strategy (e.g., fully automatic, or one-by-one mandatory) would require reworking multiple layers.

- **Trade-offs Embedded**:
  - Confidence bands improve operator UX (focus on uncertain cases) but add UI complexity
  - Multiple modes (batch/one-by-one/auto) maximize flexibility but increase test surface area
  - Immediate SQLite persistence allows job resumption on crash but prevents transaction batching
  - Post-approval dedup avoids LLM cost during extraction but amortizes it after human decision

- **Complexity & Team Knowledge**: Operators need to understand bands. Developers need to understand why extractor generates confidence scores and what "awaiting_review" status means. The confidence threshold is tunable but its semantics must be consistent across CLI and web.

- **Stability**: The pattern has been stable since 2026-08-29; confidence band definitions (`_LOW_CONFIDENCE_MAX = 0.4`, limiar threshold) are constants with no recent changes. Interactive shortcuts (a/r/p/q) and mode aliases (a/d/r/q) are documented in the code.

- **Temporal Context**: Introduced/refined over the last 2 days (2026-08-29 to 2026-08-30) as part of web integration. Recent enough to still be in active design space; stable enough that immediate changes are not anticipated. Git history shows commits focused on robustness ("improve HITL bands, purge rejected, and VACUUM") rather than fundamentals.

## Evidence Found in Codebase

### Key Files
- [`zettel/review.py`](../../../zettel/review.py) - Lines 70-177: Confidence-band definition, filtering, and formatting; lines 160-363: Interactive modes with menus
- [`zettel/cli.py`](../../../zettel/cli.py) - Review command wiring (not directly examined, but referenced in mapping)
- [`zettel/web.py`](../../../zettel/web.py) - Lines 458-469: Web `/review` GET route with client-side band filtering
- [`zettel/web_app.py`](../../../zettel/web_app.py) - Review job dispatch (batch/reject routing)

### Code Evidence

**Confidence Band Definition (review.py:70-76)**:
```python
def chunk_confidence_band(conf: float, limiar: float) -> str:
    """Classifica uma confianca em very_low / medium / high."""
    if conf <= _LOW_CONFIDENCE_MAX:
        return BAND_VERY_LOW
    if conf < limiar:
        return BAND_MEDIUM
    return BAND_HIGH
```

**Band-Based Filtering (review.py:79-88)**:
```python
def filter_chunks_by_band(
    chunks: list[dict], band: str, limiar: float
) -> list[dict]:
    """Filtra chunks pela faixa; band=all devolve a lista inteira."""
    if band == BAND_ALL:
        return list(chunks)
    return [
        c for c in chunks
        if chunk_confidence_band(float(c.get("review_confidence") or 0), limiar) == band
    ]
```

**Interactive Mode Menu (review.py:244-363)**:
```python
while True:
    mode = Prompt.ask(
        "Modo",
        choices=["a", "d", "r", "q"],
        default="a",
        console=console,
    )
    if mode == "q":
        finish_pipeline_run(db, run_id)
        return stats
    if mode == "d":
        # Batch rejection submenu by band
        ...
    if mode == "a":
        # Batch approve >= threshold
        ...
    # mode == "r"
    # One-by-one review
```

**Confidence Threshold Enforcement (review.py:194-206, non-interactive path)**:
```python
if auto_approve or not interactive:
    for chunk in chunks:
        conf = chunk.get("review_confidence") or 0
        if conf >= limiar:
            if approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                stats["approved"] += 1
            else:
                stats["skipped"] += 1
        else:
            stats["skipped"] += 1
```

### Impact Analysis
- **Introduced**: 2026-08-29 17:15:35 (commit 5d9b504, web interface implementation)
- **Refined**: 2026-08-29 11:11:15 (commit eee..., "improve HITL bands, purge rejected, VACUUM")
- **Temporal Context**: Stable for ~1 day; recent enough to be in active discussion, stable enough for production
- **Themes from recent commits**: "HITL bands" (the core feature), "purge rejected" (data lifecycle), "VACUUM" (compaction)
- **Affects**: 
  - Extractor (must generate `review_confidence` for every draft)
  - REVIEW (core logic, 3 modes)
  - CLI + Web (different UX integration points)
  - CONNECT phase (expects certain chunk statuses)

### Alternatives (Observed vs. Not Taken)
- **Fully automatic approval**: Discussed in code comments, considered inferior (loses human oversight)
- **One-by-one mandatory**: Would slow down operators on high-confidence drafts
- **No bands**: Simpler UI but loses the "focus on uncertain" optimization
- **Database staging**: Current approach persists immediately; alternative would be to stage in-memory and commit atomically (would add transaction complexity)

## Questions to Address in ADR (if created)

1. **Confidence generation strategy**: Is the LLM confidence score from extractor calibrated for this approval gate? How were the band thresholds (0.4, limiar) chosen? Are they tunable without retraining?

2. **Approval semantics**: Why is post-approval dedup necessary? Could extraction-time dedup have been used instead? What's the cost impact?

3. **Multi-mode rationale**: Is the three-mode design (batch/reject/one-by-one) necessary, or could operators adapt to a single mode with fewer options?

4. **Client-side vs. server-side validation**: The web UI filters bands client-side (lines 458-462 in web.py), but the `/review/action` endpoint doesn't re-validate server-side. Is this intentional or a gap?

5. **Status persistence timing**: Why persist approved/rejected status immediately to SQLite instead of staging in-memory during the review run? What failure modes does immediate persistence protect against?

## Related Potential ADRs
- **Separation of Retrieval from Deduplication** (RETRIEVAL): Related — extractor uses raw L2 distance, not the Retriever, for dedup logic. REVIEW's post-approval dedup reuses extractor logic, not the retriever.
- **Post-Approval Concept Deduplication** (REVIEW): A follow-up focusing specifically on the timing/ordering of dedup in the pipeline.
- **Dual-Store Persistence** (INFRA): Related — REVIEW's approval operation touches both SQLite and Chroma, with no cross-store transaction guarantee.

## Additional Notes

- **Known inconsistency** (documented in mapping): Web `/review/action` endpoint enforces auto-approve threshold only client-side; CLI enforces server-side. This asymmetry is noted in mapping.md as "a candidate worth examining in Phase 2 for either a fix or an ADR on intended web/CLI parity."
- **UX detail**: Interactive mode uses Rich Prompt with escaped square brackets (`r"\[a=.../r=.../p=.../q=...\]"`) to prevent Rich markup parsing; worth noting for maintainability.
- **PT-BR specificity**: All confidence band labels and prompts are in Portuguese; the "limiar" terminology is domain-specific (means "threshold" in this context).
