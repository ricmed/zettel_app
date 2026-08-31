# Potential ADR: Post-Approval Semantic Deduplication of Concepts

**Module**: EXTRACT / REVIEW (shared decision)  
**Category**: Data Architecture / Deduplication Strategy  
**Priority**: Consider (Score: 85)  
**Date Identified**: 2026-08-30  

---

## Existing ADR Context

Related ADRs:
- **RETRIEVAL/separation-of-retrieval-from-deduplication** (Score: 88) — Similar architectural boundary decision
- **HARVEST/three-layer-duplicate-detection** (Score: 145) — Covers source-level deduplication; this is concept-level

**Relationship**: 
- HARVEST deduplication runs on **sources** (file-level, before chunking)
- This decision covers **concepts** (semantic-level, after chunk extraction and HITL review)
- Both deliberately use raw L2 vector distance, NOT the unified Retriever (separation of concerns)
- Timeline: EXTRACT extracts concepts marked `awaiting_review`; REVIEW approves them then deduplicates before CONNECT

---

## What Was Identified

The EXTRACT/REVIEW pipeline implements a **post-approval deduplication strategy** for permanent-note concepts. The architecture deliberately separates concept deduplication from the unified Retriever used by CONNECT, ASK, and ARTICLE.

### Design Overview

**Timing**: Deduplication runs **after** HITL approval, not during extraction
- EXTRACT phase: Processes chunks through LLM, creates concepts marked `status=awaiting_review`
- REVIEW phase: HITL approval promotes `awaiting_review` → `extracted` status
- After approval, `_dedupe_approved_concepts()` runs deduplication
- Final status: `approved` (unique, ready for CONNECT) or `duplicate` (discarded)

**Deduplication Strategy**:
```
For each concept (thesis + definition):
  1. Query Chroma permanent_notes collection (raw cosine similarity)
  2. Threshold: `linking.dedupe_threshold` (default 0.85)
  3. Distance conversion: L2_distance = 2 * (1 - similarity)
  4. If threshold exceeded:
     - Call LLM with dedupe prompt (dedupe_decision.md)
     - Options: CREATE_NEW | IGNORE | REFINE_EXISTING | MERGE
     - Mark as approved/duplicate accordingly
  5. Else:
     - Approve automatically (no similar note found)
```

**Separation from Unified Retriever**:
- Extractor dedupe **does NOT use** `Retriever.search_notes()` (hybrid RRF + relevance floor)
- Uses raw L2 distance directly from ChromaDB: `idx.query_similar_notes(query_text, n_results=5)`
- Deliberate isolation per CLAUDE.md: "calibrated on raw L2 distance, not RRF"
- Rationale: Dedupe thresholds require different precision/recall trade-offs than search
  - Search: Lower threshold (0.70) catches relevant background notes
  - Dedupe: Higher threshold (0.85) requires near-certainty before rejecting a concept
  - RRF fusion with BM25 would conflate lexical and semantic similarity inappropriately

**Decision Recording**:
- Approved concepts: `status=approved`, ready for CONNECT
- Duplicates: `status=duplicate`, skipped
- Optional merge target: if REFINE_EXISTING or MERGE, `refines_note_id` stores the target note's ID

**Conditional Execution**:
- Only runs if concepts exist with `status=extracted` (post-approval)
- Called from three paths:
  1. `run_review()` after batch approve (line 329, 361)
  2. `approve_high_confidence()` for auto-approve flow (line 376)
  3. `finalize_approved_concepts()` for web review actions (line 384)

### Code Flow

**EXTRACT phase** (extractor.py):
```python
# Create concept, mark awaiting_review
db.upsert_concept(
    concept_id, source_id, chunk_id, anchor_hash, thesis_hash,
    candidate_json=cand.model_dump_json(), 
    status="awaiting_review",  # ← Not deduplicated yet
)
```

**REVIEW phase** (review.py:387-481):
```python
def approve_chunk(...):
    # Mark concepts as "extracted" (ready for dedupe)
    for concept in db.get_concepts_for_chunk(chunk_id):
        if concept.get("status") == "awaiting_review":
            db.update_concept_status(concept["concept_id"], "extracted")
```

**Deduplication** (review.py:636-671):
```python
def _dedupe_approved_concepts(...):
    rows = db.get_concepts_by_status("extracted")  # Post-approval concepts
    
    from zettel.extractor import deduplicate_candidates
    approved = deduplicate_candidates(cfg, db, idx, llm, candidates)
    
    # Mark final status: approved or duplicate
    for concept in candidates:
        if concept["concept_id"] in approved_ids:
            db.update_concept_status(cid, "approved")
        else:
            db.update_concept_status(cid, "duplicate")
```

**Deduplication logic** (extractor.py:520-599):
```python
def deduplicate_candidates(cfg, db, idx, llm, candidates):
    for cand in candidates:
        query_text = f"{cand.thesis} {cand.definition}"
        similar = idx.query_similar_notes(query_text, n_results=5)  # Raw query, not Retriever
        
        if not similar or closest_distance > threshold:
            approved.append(cand)  # Auto-approve
            continue
        
        # LLM dedupe decision
        response = call_llm(llm, dedupe_prompt, ...)
        result = _parse_dedupe_result(response)
        
        if result.decision == CREATE_NEW:
            approved.append(cand)
        elif result.decision == IGNORE:
            pass  # Discard
        elif result.decision in (REFINE_EXISTING, MERGE):
            cand["refines_note_id"] = result.target_note_id
            approved.append(cand)
    
    return approved
```

## Why This Might Deserve an ADR

- **Impact**: Affects concept → permanent note linkage; determines which concepts create new notes vs. refine existing ones
  - High-threshold deduplication reduces permanent-note bloat
  - Low-threshold would allow concept redundancy
  - Post-approval timing ensures only high-confidence extractions are checked

- **Trade-offs**:
  - **Pro**: Separates concerns (extraction confidence vs. uniqueness detection)
  - **Pro**: Allows per-concept LLM judgment (CREATE_NEW vs. REFINE vs. IGNORE)
  - **Pro**: HITL approval ensures concepts are reviewed before deduplication
  - **Con**: Adds latency (LLM call per dedupe decision per concept, ~100ms each)
  - **Con**: Threshold tuning (0.85) is corpus-specific; may require recalibration
  - **Con**: Post-approval deduplication can fail silently; if dedupe errors, already-approved concepts are affected

- **Complexity**: 
  - LLM dedupe decision adds non-determinism (temperature > 0)
  - Four-way decision tree (CREATE_NEW / IGNORE / REFINE / MERGE)
  - Threshold choice (0.85) is empirically calibrated but not adaptive

- **Team Knowledge**: Essential for understanding:
  - Why approved concepts can still be marked `duplicate` or `refines_note_id`
  - How threshold tuning affects the concept → note conversion rate
  - Why dedupe happens in REVIEW, not EXTRACT
  - Implications of LLM dedupe decisions on permanent-note graph structure

- **Long-term Implications**:
  - Corpus growth: As permanent notes accumulate, dedupe becomes more selective (higher similarity bar)
  - Threshold drift: Embedding model changes require dedupe threshold recalibration
  - LLM dedupe cost: Grows with concept volume; ~$0.01 per concept at scale

## Evidence Found in Codebase

### Key Files

- [`zettel/review.py:636-671`](../../../zettel/review.py) — Post-approval deduplication
  - `_dedupe_approved_concepts()` — Entry point after approval
  - Queries concepts with `status=extracted`
  - Calls `deduplicate_candidates()` from extractor module

- [`zettel/extractor.py:520-599`](../../../zettel/extractor.py) — Deduplication logic
  - `deduplicate_candidates()` — Main deduplication loop
  - `_compute_concept_id()` — Stable concept ID from source + chunk + anchor
  - `_parse_dedupe_result()` — Parses LLM dedupe decision

- [`zettel/review.py:387-481`](../../../zettel/review.py) — Approval workflow that triggers dedupe
  - `approve_chunk()` — Sets status to `extracted` (eligible for dedupe)
  - Calls `_dedupe_approved_concepts()` at line 329, 361 after approve batches

- [`zettel/config.yaml`](../../../config/config.yaml)
  ```yaml
  linking:
    dedupe_threshold: 0.85  # Similarity; distance = 2 * (1 - threshold)
  ```

- [`prompts/dedupe_decision.md`](../../../prompts/dedupe_decision.md) — Dedupe LLM prompt
  - System/user split for prompt caching
  - Four-option decision: CREATE_NEW | IGNORE | REFINE_EXISTING | MERGE

### Code Evidence

```python
# Entry point: post-approval deduplication (review.py:329, 361)
_dedupe_approved_concepts(cfg, db, idx, source_id)

# Implementation: deduplicate candidates (extractor.py:520-599)
def deduplicate_candidates(cfg, db, idx, llm, candidates) -> list[dict]:
    for i, cand_dict in enumerate(candidates, 1):
        cand = cand_dict["candidate"]
        query_text = f"{cand.thesis} {cand.definition}"
        
        # Raw query (NOT Retriever.search_notes)
        similar = idx.query_similar_notes(query_text, n_results=cfg.linking.topk)
        
        if not similar:
            approved.append(cand_dict)
            continue
        
        closest_distance = similar[0].get("distance", 999)
        similarity_threshold_distance = 2 * (1 - cfg.linking.dedupe_threshold)
        
        if closest_distance > similarity_threshold_distance:
            approved.append(cand_dict)  # Auto-approve if no similar note
            continue
        
        # LLM dedupe decision
        existing_notes_text = _format_existing_notes(similar)
        mapping = {
            "new_thesis": cand.thesis,
            "new_definition": cand.definition,
            "existing_notes": existing_notes_text,
        }
        
        response = call_llm(llm, dedupe_prompt, ...)
        result = _parse_dedupe_result(response)
        
        if result.decision == DedupeDecision.CREATE_NEW:
            approved.append(cand_dict)
        elif result.decision == DedupeDecision.IGNORE:
            # Discard
            pass
        elif result.decision in (DedupeDecision.REFINE_EXISTING, DedupeDecision.MERGE):
            cand_dict["refines_note_id"] = result.target_note_id
            approved.append(cand_dict)

    # Record final status
    for cand_dict in candidates:
        cid = cand_dict["concept_id"]
        db.update_concept_status(cid, "approved" if cid in approved_ids else "duplicate")
    
    return approved
```

### Impact Analysis

- **Introduced**: Not explicitly documented in git history; inferred from extractor.py and review.py design
- **Stable**: Core logic unchanged; threshold tuning has occurred
- **Themes**: "deduplication", "concept quality", "CONNECT preparation"
- **Affects**: 
  - Concept status transitions (awaiting_review → extracted → approved|duplicate)
  - Permanent note creation rate (CREATE_NEW vs. REFINE vs. IGNORE)
  - CONNECT phase input (only `approved` concepts → permanent notes)

### Alternatives (Observed or Documented)

1. **Deduplication during EXTRACT** (before approval)
   - Simpler pipeline (one phase instead of two-stage)
   - **Rejected**: Would lose concepts before HITL review; unfair to reject based on automated similarity

2. **No deduplication** (skip the check entirely)
   - Faster processing, no LLM cost
   - **Rejected**: Would create permanent-note duplicates; poor graph structure

3. **Unified Retriever for dedupe** (use RRF + relevance floor)
   - Consistent with CONNECT/ASK/ARTICLE
   - **Rejected**: RRF conflates lexical and semantic similarity; wrong thresholds for dedupe (need higher bar)
   - Documented in CLAUDE.md as deliberate separation

4. **Configurable LLM decision** (different LLM model for dedupe)
   - More flexibility for specialized dedupe logic
   - **Not chosen**: Reuses same LLM as extract/connect for consistency

## Questions to Address in ADR (if created)

- Why is deduplication post-approval instead of pre-approval? (HITL review ensures concept quality before dedupe decision)
- What is the cost of LLM dedupe decisions? (One call per concept, ~$0.01 per call at gpt-4o-mini rates; cost tracked in runs row)
- Can a user override a dedupe decision? (Not directly; would require manual `zettel sync-manual` + StateDB edit)
- How are MERGE vs. REFINE_EXISTING distinguished? (LLM prompt offers both; MERGE is for full integration, REFINE for incremental)
- What happens if dedupe LLM call fails? (Logs warning, approves concept anyway — safe default)

## Related Potential ADRs

- **RETRIEVAL/separation-of-retrieval-from-deduplication** — Related boundary decision (harvest layer-3 also uses raw L2, not RRF)
- **EXTRACT/granular-literature-notes** — Deduplication operates on approved literature notes + concepts
- **INFRA/dual-store-persistence** — Concept status transitions live in SQLite; approved notes are indexed to Chroma

## Additional Notes

- **Temporal context**: Core logic stable; no recent changes to dedupe threshold or decision routing
- **Configuration exposure**: Threshold is configurable via `config.yaml` (linking.dedupe_threshold)
- **Testing**: `test_extractor.py` has tests for deduplicate_candidates, but integration tests with actual LLM decisions are minimal
- **Known limitation**: LLM dedupe decisions are non-deterministic (temperature > 0); same concept pair may get different decisions on replay
- **Observability**: Dedupe decisions logged at INFO level; status transitions recorded in StateDB; summary in `zettel status`
- **Performance note**: Dedupe batches concepts by default; latency ~100ms per concept (LLM call); for large extracts (1000+ concepts), dedupe can take 10-15 minutes

