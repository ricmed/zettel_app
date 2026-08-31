# Implementation Action Plan — ADR Resolutions 2026-08-31

**Status**: Planning phase  
**Priority**: Medium (1-2 weeks to implement)  
**Required Changes**: 2 ADRs need code/config changes; 1 is documentation-only

---

## Summary

| ADR | Module | Type | Effort | Blocker? |
|-----|--------|------|--------|----------|
| **ADR-012** | HARVEST | Config + Code | Medium | No |
| **ADR-017** | REVIEW | Documentation | Low | No |
| **ADR-018** | REVIEW | Code + Tests | Medium | Moderate (security) |

---

## ADR-012: Docling (Remove PyMuPDF)

### Changes Required

#### 1. **Dependency Management**
```
File: pyproject.toml (UV) or requirements.txt
Action: 
  - Remove: PyMuPDF / fitz
  - Pin Docling: e.g., docling==1.11.0 (specific version, not floating)
  
Example:
  # BEFORE
  PyMuPDF>=1.23.0
  docling>=1.0.0  # floating
  
  # AFTER
  docling==1.11.0  # pinned to specific version
```

#### 2. **Code Removal: harvester.py**
```
Files affected:
  - zettel/harvester.py:1170-1174  (PDF extraction dispatcher)
  - zettel/harvester.py:1234-1255  (PyMuPDF extraction path)
  - zettel/harvester.py:1267-1290  (PyMuPDF page-to-heading map)

Action:
  1. Keep Docling extraction path (lines 1177-1232)
  2. Remove PyMuPDF-specific code paths entirely
  3. Remove conditional logic that chooses between extractors
  4. Simplify dispatcher to: "use Docling or hard-fail"
  
Expected Impact:
  - Function _extract_pdf() becomes simpler (no fallback path)
  - Error handling changes: "Docling failed" becomes fatal, not degraded
  - Remove ~60 lines of fallback/conditional code
```

#### 3. **Error Handling Updates**
```
File: zettel/harvester.py (error messages)

Current behavior:
  - Docling fails → tries PyMuPDF
  - PyMuPDF fails → harvest continues with plain text, warns operator
  
New behavior:
  - Docling fails → harvest fails with clear error message
  
Action:
  - Update error messages to be explicit about Docling being mandatory
  - Example: "PDF extraction failed. Docling is required and unavailable. 
             Please check installation and GPU availability."
```

#### 4. **Config Changes**
```
File: zettel/config.py (if there's a pdf_extractor setting)

Action:
  - If there's a config key like `pdf_extractor: [docling|pymupdf]`
    → Remove pymupdf option, make docling-only, simplify config
  - If GPU acceleration config exists → Keep as-is (soft dependency is fine)
```

#### 5. **Documentation Updates**
```
Files to update:
  - CLAUDE.md (harvest section)
    → Remove mention of PyMuPDF fallback
    → Document that Docling is mandatory
  
  - Any README or installation guide
    → Emphasize Docling + torch/cuda dependencies
```

### Testing Strategy

```python
Test new behavior (after PyMuPDF removal):
  1. test_harvest_pdf_valid.py
     - Should pass (Docling works fine)
  
  2. test_harvest_pdf_docling_missing.py  
     - NEW: Test that harvest fails explicitly if Docling unavailable
     - Should raise clear error, not degrade
  
  3. test_harvest_pdf_extraction_determinism.py
     - NEW: Verify that harvesting same PDF twice produces identical chunks
     - (This validates version-pinning benefit)
```

### Rollout Steps

1. **Week 1**: Code removal + config update + error messages (1-2 days)
2. **Week 1**: Add/update tests (1 day)
3. **Week 2**: Manual testing on real PDFs + staging (1 day)
4. **Week 2**: Merge PR after review (1 day)

---

## ADR-017: Confidence Bands (Thresholds)

### Changes Required

#### Status
✅ **No code changes needed**. This is documentation + process update.

#### Documentation Updates
```
Files to update:
  1. CLAUDE.md (review section)
     - Document that thresholds are initial estimates, not empirically derived
     - Note: 0.4 (very-low) and 0.7 (auto-approve) are tunable
  
  2. config/config.yaml (if threshold is there)
     - Add comment: "These values are initial estimates. Monitor real-world 
       impact and adjust if operator workload becomes imbalanced."
  
  3. ADR-017 (already updated ✅)
     - Documents decision to keep as heuristics
```

#### Process Update
```
Operator guidance (add to runbook or FAQ):
  - Monitor how many drafts fall into each band after each harvest/extract run
  - If threshold seems misaligned (too many medium-confidence drafts being reviewed?):
    → Propose adjustment via GitHub issue
  - If upstream model changes significantly:
    → Schedule formal calibration analysis
```

### Timeline
- **Documentation only** → 30 min to update CLAUDE.md + config comments
- **No testing required** (already tested, just different threshold intent)

---

## ADR-018: Web/CLI Validation (Server-Side Enforcement)

### Changes Required

#### 1. **Add Server-Side Validation in web_app.py**
```
File: zettel/web_app.py (review job handler)

Current code (approximate):
  def handle_review_job(job_id, action, chunk_ids):
    for chunk_id in chunk_ids:
      if action == 'approve':
        approve_chunk(chunk_id)  # ← NO VALIDATION HERE
      else:
        reject_chunk(chunk_id)

New code:
  def handle_review_job(job_id, action, chunk_ids):
    min_confidence = config.literature_review.auto_approve_min_confidence
    for chunk_id in chunk_ids:
      if action == 'approve':
        chunk = db.get_chunk(chunk_id)
        if chunk.review_confidence < min_confidence:
          # Reject silently (or log) — do NOT approve
          logger.warning(f"Rejected low-confidence chunk: {chunk_id}")
          continue
        approve_chunk(chunk_id)  # ← NOW VALIDATED
      else:
        reject_chunk(chunk_id)
```

#### 2. **Add Threshold Check in web.py (POST endpoint)**
```
File: zettel/web.py (the /review/action endpoint)

Current code (approximate):
  @app.post("/review/action")
  def review_action(chunk_ids: List[str], action: str):
    # No validation here, just enqueue
    enqueue_review_job(chunk_ids, action)  # ← TRUST CLIENT

New code:
  @app.post("/review/action")
  def review_action(chunk_ids: List[str], action: str):
    min_confidence = config.literature_review.auto_approve_min_confidence
    if action == 'approve':
      # Filter client-provided list to only high-confidence chunks
      chunks = db.get_chunks(chunk_ids)
      approved_ids = [c.id for c in chunks if c.review_confidence >= min_confidence]
      if len(approved_ids) < len(chunk_ids):
        logger.info(f"Filtered {len(chunk_ids) - len(approved_ids)} low-confidence chunks from approval request")
      chunk_ids = approved_ids  # ← NOW FILTERED SERVER-SIDE
    
    enqueue_review_job(chunk_ids, action)
```

#### 3. **Add Tests for /review/action Validation**
```
File: tests/test_web.py (new test section)

Tests to add:
  1. test_review_action_respects_threshold()
     - Submit chunks with review_confidence below threshold
     - Verify they are NOT approved (either filtered or rejected)
  
  2. test_review_action_high_confidence_approved()
     - Submit chunks with review_confidence >= threshold
     - Verify they ARE approved
  
  3. test_review_action_mixed_confidence()
     - Submit mix of low/high confidence chunks
     - Verify only high-confidence are approved
  
  4. test_review_action_validation_matches_cli()
     - Verify web and CLI enforce same threshold
     - (This is a regression test for future changes)
```

Example test structure:
```python
def test_review_action_respects_threshold(web_app, test_db):
    # Setup
    min_conf = 0.7
    low_chunk = create_chunk(review_confidence=0.5)
    high_chunk = create_chunk(review_confidence=0.9)
    
    # Act
    response = web_app.post("/review/action", 
                             json={"chunk_ids": [low_chunk.id, high_chunk.id],
                                   "action": "approve"})
    
    # Assert
    assert response.status_code == 200  # Endpoint succeeds
    assert db.get_chunk(low_chunk.id).status == "awaiting_review"  # NOT approved
    assert db.get_chunk(high_chunk.id).status == "approved"  # IS approved
```

### Code Location Reference
```
Files to modify:
  - zettel/web.py          (lines ~458-477, /review/action endpoint)
  - zettel/web_app.py      (review job handler, exact line TBD)
  - tests/test_web.py      (add new test section, ~50 lines)
```

### Rollout Steps

1. **Week 1**: Add validation logic in web_app.py (1 day)
2. **Week 1**: Add corresponding check in web.py /review/action (1 day)
3. **Week 1**: Write tests + verify both paths enforce uniformly (1 day)
4. **Week 2**: Manual testing on staging (1 day)
5. **Week 2**: Merge PR after review (1 day)

### Verification Checklist

- [ ] Submitting high-confidence chunks via web → approved ✓
- [ ] Submitting low-confidence chunks via web → rejected/skipped ✓
- [ ] Submitting to CLI with same chunks → same result as web ✓
- [ ] Error logging clear when chunks are filtered (audit trail) ✓
- [ ] No regression in other approval paths (batch reject, etc.) ✓
- [ ] Web UI still allows operators to select chunks individually ✓
  (validation just prevents below-threshold approval)

---

## Consolidated Timeline

| Task | Week | Days | Dependencies |
|------|------|------|--------------|
| ADR-012: Remove PyMuPDF | 1-2 | 4 | None |
| ADR-017: Document thresholds | 1 | 0.5 | None |
| ADR-018: Server-side validation | 1-2 | 4 | ADR-012 completion? (No, independent) |
| Testing + Staging | 2 | 2 | All above |
| **Total** | **2 weeks** | **10.5 days** | — |

---

## Risk Assessment

### Low Risk
- ✅ ADR-017 (documentation only, no code change)

### Medium Risk
- ⚠️ ADR-012 (removing PyMuPDF)
  - Risk: Docling becomes hard dependency; any Docling bug/downtime blocks harvest
  - Mitigation: Version-pinning; clear error messages; staging test on real PDFs
  
- ⚠️ ADR-018 (server-side validation)
  - Risk: Changing approval flow could miss edge case (e.g., manually-selected chunks behave differently)
  - Mitigation: Comprehensive tests; manual staging; audit logs

### No Blocking Risks
- Both changes are reversible (revert PR if issues found)
- No database migration needed
- No production data cleanup needed

---

## Success Criteria

✅ **ADR-012**:
- [ ] PyMuPDF removed from dependencies
- [ ] `docling==X.Y.Z` pinned in pyproject.toml
- [ ] Harvest fails explicitly on Docling failure (error message is clear)
- [ ] All tests pass (including new extraction determinism test)

✅ **ADR-017**:
- [ ] CLAUDE.md documents thresholds as tunable estimates
- [ ] config.yaml has explanatory comment

✅ **ADR-018**:
- [ ] Server-side validation in web_app.py + web.py
- [ ] `/review/action` enforces threshold server-side
- [ ] CLI and web produce identical approval behavior
- [ ] New tests cover threshold validation scenarios
- [ ] All tests pass

---

## Notes for Review

1. **PyMuPDF removal is a hard dependency change** — once this lands, harvest requires Docling. This is intentional per ADR-012 decision (AGPL risk elimination).

2. **Threshold tuning is operator-driven** — ADR-017 doesn't require code change, but process change: ops should monitor real-world impact and propose adjustments if bands seem misaligned.

3. **Web validation fix is a security hardening** — ADR-018 eliminates a bypass path. There's no "looser is better" argument; uniform enforcement is the right call.

---

**Owner**: (assign to appropriate team member)  
**Next Step**: Prioritize which ADR to implement first (recommend ADR-018 first for security, then ADR-012)
