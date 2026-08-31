# ADR Resolution Log — 2026-08-31

**Status**: Complete — All 3 needs-input ADRs resolved and promoted to Accepted

---

## ADR-012: Docling PDF Extraction (HARVEST)

**Previous Status**: Needs Input (3 unresolved questions)  
**New Status**: Accepted (2026-08-31)

### Decisions Made

| Question | Resolution | Rationale |
|----------|-----------|-----------|
| **PyMuPDF AGPL-3.0 licensing** | Remove PyMuPDF entirely; Docling only | AGPL-3.0 viral license blocks commercial distribution and exposing web UI beyond personal use. Docling-only is mandatory now; Harvest fails explicitly if Docling unavailable rather than degrading to plain text. |
| **Docling version pinning** | Pin to specific version | Protect extraction reproducibility. Version upgrades are deliberate, not silent. Old sources should be re-harvested if Docling output changes. |
| **GPU degradation (CPU fallback)** | Keep silent CPU fallback | Acceptable trade-off for single-VM personal deployments. Performance difference manageable. Startup GPU check not warranted. |

### Impact
- ✅ Removes licensing blocker for distribution
- ✅ Guarantees deterministic extraction via pinned version
- ⚠️ Harvest now hard-fails if Docling unavailable (no fallback)
- ⚠️ Requires `pyproject.toml` / `uv.lock` to pin Docling version

### Code Action Items
- [ ] Remove PyMuPDF from dependencies
- [ ] Pin Docling version in `pyproject.toml`
- [ ] Remove fallback extraction path in `harvester.py`
- [ ] Update error messaging for Docling failures (no longer a "degrade gracefully" scenario)

---

## ADR-017: Confidence-Band HITL Approval Gate (REVIEW)

**Previous Status**: Needs Input (1 unresolved question)  
**New Status**: Accepted (2026-08-31)

### Decisions Made

| Question | Resolution | Rationale |
|----------|-----------|-----------|
| **Confidence threshold calibration (0.4 and 0.7)** | Keep as initial heuristics, treat as tunable | Values were not empirically derived. As of 2026-08-31, they remain operational defaults. A formal calibration pass (analyzing extractor confidence distribution) warranted in future phase if becomes bottleneck or upstream model changes shift distribution significantly. |

### Impact
- ✅ Clarifies that thresholds are starting estimates, not immutable
- ✅ Licenses future tuning without needing an ADR update
- ✓ No code changes required (already operational)

### Documentation Notes
- Operators should monitor real-world workload and propose adjustments
- If confidence distribution shifts materially, schedule calibration analysis
- See ADR-018 for web/CLI validation resolution

---

## ADR-018: Web/CLI Validation Asymmetry (REVIEW)

**Previous Status**: Proposed, unresolved (1 unresolved question)  
**New Status**: Accepted (2026-08-31)

### Decisions Made

| Question | Resolution | Rationale |
|----------|-----------|-----------|
| **Enforce threshold server-side in web approval path?** | Yes, enforce uniformly | Closing the bypass vector. The configured threshold `literature_review.auto_approve_min_confidence` is now a uniform gate across CLI and web. No implicit override capability; if needed in future, add as explicit "Force Approve (admin)" action with audit trail. |

### Impact
- ✅ Eliminates security bypass at `/review/action` endpoint
- ✅ Makes threshold contract uniform across both interfaces
- ✅ Simplifies reasoning about review gate semantics
- ⚠️ Removes any undocumented flexibility to approve below-threshold chunks via web UI

### Code Action Items
- [ ] Add server-side confidence check in `web_app.py` review job handler
- [ ] Verify threshold re-check applies even for manually-selected chunk_ids
- [ ] Ensure CLI and web route through same validated `approve_chunk` path
- [ ] Add tests for `/review/action` validation (currently no test fixtures)
- [ ] Update documentation to clarify threshold is server-enforced on both paths
- [ ] If future feature requires override: design explicit "Force Approve" mechanism with audit log

---

## Summary: Promotion from Needs-Input → Accepted

| ADR | Module | Category | Decision | Complexity |
|-----|--------|----------|----------|------------|
| **ADR-012** | HARVEST | Infrastructure/Licensing | Remove PyMuPDF, Pin Docling | Medium (dependency removal + version constraint) |
| **ADR-017** | REVIEW | Validation/Thresholds | Keep heuristics, enable tuning | Low (documentation + future process) |
| **ADR-018** | REVIEW | Security/Validation | Enforce server-side uniformly | Medium (code changes + tests) |

---

## Updated ADR Statistics

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| **Total Accepted ADRs** | 23 | 26 | +3 |
| **Needs-Input ADRs** | 3 | 0 | -3 |
| **Coverage** | 88.5% (23/26) | 100% (26/26) | ✅ Complete |

---

## Next Steps

### Immediate (This week)
1. **Update ADR-INDEX.md** — Change 3 ADRs from Needs-Input → Accepted status
2. **Update ADR-OVERVIEW.md** — Remove notes about 3 pending decisions
3. **Implement code actions** from above (prioritized by complexity)

### Short-term (Next 2 weeks)
- [ ] Remove PyMuPDF dependency (ADR-012)
- [ ] Add server-side validation to web review path (ADR-018)
- [ ] Add test coverage for `/review/action` validation (ADR-018)

### Medium-term (Next month)
- [ ] Monitor confidence threshold real-world impact (ADR-017)
- [ ] Consider formal calibration pass if distribution shifts (ADR-017)

---

**Resolved by**: Ricardo Medeiros  
**Date**: 2026-08-31  
**Verification**: All 3 ADRs moved from `needs-input/` subdirectories to main module folders and updated with resolutions.
