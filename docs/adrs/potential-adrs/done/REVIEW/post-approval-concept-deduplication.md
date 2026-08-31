# Potential ADR: Post-Approval Concept Deduplication Timing

**Module**: REVIEW  
**Category**: Pipeline Architecture / Deduplication Strategy / Data Quality  
**Priority**: Consider (Score: 76)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The system employs a deferred deduplication strategy for concepts within the pipeline:

1. **Extraction Phase (EXTRACT)**: 
   - Generates candidate permanent notes (ZTL concepts) for each approved chunk
   - Marks concepts with `status=awaiting_review` and stores the candidate JSON
   - Does **not** deduplicate; relies on post-approval dedup to collapse equivalent definitions

2. **Review Phase (REVIEW)**:
   - After approving chunks, runs semantic deduplication via `_dedupe_approved_concepts()` (line 636)
   - Calls into `extractor.deduplicate_candidates()` with only concepts marked `status=extracted` (promoted from awaiting_review during approval)
   - Deduplication uses LLM to merge equivalent concepts (same logic as extractor's dedup, but triggered post-approval)
   - Result: Concepts marked `status=approved` (eligible for CONNECT) or collapsed into survivors

3. **Connect Phase (CONNECT)**:
   - Processes only concepts with `status=approved` (the deduplicated survivors)
   - Generates permanent notes from the winners of dedup

This timing is a deliberate architectural choice: **dedup happens between REVIEW and CONNECT, not within EXTRACT.**

Introduced via commit 5d9b504 (2026-08-29 17:15:35, "Implement Python-first Zettelkasten web interface...") and refined in recent REVIEW enhancements.

## Why This Might Deserve an ADR

- **Pipeline Ordering Impact**: The decision to defer dedup until after human approval affects:
  - When/whether LLM is called for dedup (post-approval, incurring cost after human decision)
  - Which concepts CONNECT will see (only deduplicated survivors)
  - Recovery semantics (if review crashes, dedup must re-run on resume)
  
- **Cost Trade-off**: 
  - **Extraction-time dedup**: Would deduplicate all candidates, even those later rejected by humans. Wastes LLM cost on drafts humans discard.
  - **Post-approval dedup** (current): Only deduplicates concepts from approved chunks. Cost is paid only after humans filter out low-confidence drafts.
  - The current choice optimizes for human-driven filtering before dedup cost is incurred.

- **Complexity & Semantics**:
  - Concepts flow through three status values: `awaiting_review` → `extracted` (on chunk approval) → `approved` (after dedup) or `rejected` (if merge eliminated them)
  - CONNECT expects `status=approved`, creating a hard dependency on the dedup step
  - If dedup is skipped, CONNECT receives unmerged duplicates, potentially generating multiple overlapping notes

- **Team Knowledge**: Developers working on EXTRACT, REVIEW, or CONNECT need to understand:
  - Why EXTRACT doesn't deduplicate
  - Why REVIEW calls `extractor.deduplicate_candidates` after approval
  - That CONNECT implicitly depends on this dedup having run

- **Stability**: The pattern has been stable since 2026-08-29; no recent changes to the dedup timing logic. Code comments (line 639: "Run semantic dedupe on concepts with status=extracted (post-approve)") make the intent explicit.

## Evidence Found in Codebase

### Key Files
- [`zettel/review.py`](../../../zettel/review.py) - Lines 636-671: `_dedupe_approved_concepts()` function
- [`zettel/extractor.py`](../../../zettel/extractor.py) - `deduplicate_candidates()` function (called by review.py:665)
- [`zettel/state.py`](../../../zettel/state.py) - `get_concepts_by_status()`, `update_concept_status()` (status transitions)

### Code Evidence

**Post-Approval Dedup Trigger (review.py:329, 361, 384)**:
```python
if mode == "a":
    # ... batch approve >=limiar ...
    _dedupe_approved_concepts(cfg, db, idx, source_id)
    finish_pipeline_run(db, run_id)
    return stats

# mode == "r" (one-by-one)
for chunk in sample:
    # ... approve/reject individual chunks ...

_dedupe_approved_concepts(cfg, db, idx, source_id)
finish_pipeline_run(db, run_id)
return stats
```

**Dedup Implementation (review.py:636-671)**:
```python
def _dedupe_approved_concepts(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, source_id: str | None
) -> None:
    """Run semantic dedupe on concepts with status=extracted (post-approve)."""
    rows = db.get_concepts_by_status("extracted")
    if source_id:
        rows = [r for r in rows if r["source_id"] == source_id]
    if not rows:
        return

    candidates: list[dict] = []
    for row in rows:
        raw = row.get("candidate_json")
        if not raw:
            continue
        try:
            cand = PermanentNoteCandidate(**json.loads(raw))
        except Exception:
            continue
        candidates.append({
            "concept_id": row["concept_id"],
            "source_id": row["source_id"],
            "chunk_id": row["chunk_id"],
            "candidate": cand,
        })

    if not candidates:
        return

    from zettel.extractor import deduplicate_candidates
    llm = get_llm(cfg)
    approved = deduplicate_candidates(cfg, db, idx, llm, candidates)
    logger.info(
        "Dedupe pos-review: %d / %d candidatos aprovados para connect",
        len(approved), len(candidates),
    )
```

**Status Transition During Approval (review.py:475-477)**:
```python
# Concepts become eligible for dedupe → approved
for concept in db.get_concepts_for_chunk(chunk_id):
    if concept.get("status") == "awaiting_review":
        db.update_concept_status(concept["concept_id"], "extracted")
```

**CONNECT Expectation (implied by filtering)**:
```python
# From connector.py (not shown here, but per mapping: "Takes `approved` concepts")
# CONNECT processes: db.get_concepts_by_status("approved", without_notes=True)
```

### Impact Analysis
- **Introduced**: 2026-08-29 17:15:35 (commit 5d9b504, web interface with improved review pipeline)
- **Pattern**: Post-approval dedup reuses `extractor.deduplicate_candidates()` logic; no new dedup algorithm
- **Cost**: One LLM call per batch of extracted concepts (after human filters by approval)
- **Data flow**: 
  - `chunk status=persisted` → `concept status=extracted` → `concept status=approved` (dedup winner) or eliminated (merge loser)
  - Only survivors reach CONNECT

### Alternatives (Not Taken)
- **Extraction-time dedup**: Would deduplicate all candidates during EXTRACT, before humans see them. Wastes LLM cost on later-rejected drafts.
- **No dedup at all**: Would allow duplicate concept definitions to reach CONNECT, bloating the permanent note corpus.
- **Manual dedup**: Would add another HITL step; too slow for operator workflow.
- **Dedup within CONNECT**: Would be too late; CONNECT would generate multiple overlapping notes before dedup could fix them.

## Questions to Address in ADR (if created)

1. **Cost accounting**: What's the typical cost of dedup per batch of approved concepts? Is this visible in cost reports?

2. **Dedup quality**: Does the LLM-based merge in `deduplicate_candidates()` reliably collapse true duplicates? What's the false-positive rate (merging distinct concepts) vs. false-negative rate (leaving duplicates unmerged)?

3. **Failure recovery**: If dedup fails mid-batch, how does retry work? Are concepts left in `extracted` status until dedup succeeds?

4. **Batch size sensitivity**: How does performance degrade with large batches of concepts? Is there a batch-size ceiling?

5. **Tuning parameters**: Are there configuration knobs for dedup threshold, LLM model, or merge strategy? Currently hardcoded?

## Related Potential ADRs
- **Confidence-Scored HITL Approval** (REVIEW): Related — the dedup timing assumes human-filtered high-confidence chunks. If approval strategy changes, dedup economics change.
- **Separation of Retrieval from Deduplication** (RETRIEVAL): Related — dedup uses raw L2 distance (from extractor), not the hybrid Retriever. Both decisions are about keeping dedup orthogonal.
- **RAG-Based Permanent Note Generation** (CONNECT): Related — CONNECT's input contract assumes deduplicated concepts (status=approved).

## Additional Notes

- **Terminology**: Status `extracted` is an intermediate state (concept extracted from approved chunk, before dedup). Not to be confused with extraction as a pipeline phase. The naming could be clearer (e.g., `post_approved_pending_dedup` would be more explicit).
- **Logging signal**: The log message "Dedupe pos-review: X / Y candidatos aprovados para connect" provides visibility into dedup outcome; operators can see how many duplicates were merged. Useful for auditing.
- **PT-BR language**: Function names and comments are all in Portuguese (`_dedupe_approved_concepts`, `candidatos aprovados para connect`); maintainers should be aware of this language choice.
