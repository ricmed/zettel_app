# GitHub Issues Templates — All 26 ADRs

**Purpose**: Template file for creating GitHub issues for all 26 ADRs  
**Workflow**: 
1. 3 issues are OPEN (need action)
2. 23 issues are REFERENCE (will be closed immediately as "documentation")

---

## OPEN ISSUES (Action Required)

These 3 issues require code/documentation changes:

### Issue #1: ADR-018 (Priority: HIGH - Security)

**Title:**
```
ADR-018: Add server-side validation to review threshold (security fix)
```

**Labels:** `architecture`, `security`, `adr-resolution`

**Body:**
```markdown
## Summary
**ADR**: [ADR-018: Web/CLI Validation Asymmetry](docs/adrs/generated/REVIEW/ADR-018-web-cli-validation-asymmetry.md)  
**Priority**: HIGH (closes security bypass)  
**Effort**: ~4 days (1 PR)

## Problem
The web UI's `/review/action` endpoint enforces `literature_review.auto_approve_min_confidence` only client-side, while CLI enforces server-side. This allows a crafted POST request to bypass the threshold and approve low-confidence chunks.

## Decision (ADR-018 Resolved 2026-08-31)
**Enforce threshold server-side on both CLI and web paths** to close the bypass and make configuration uniform.

## Implementation
See [ACTION-PLAN-2026-08-31.md](docs/adrs/ACTION-PLAN-2026-08-31.md#adr-018-web-cli-validation-server-side-enforcement) for:
- Code snippets (zettel/web_app.py, zettel/web.py)
- Test cases for `/review/action` validation
- Risk assessment + success criteria

## Acceptance Criteria
- [ ] `/review/action` enforces threshold server-side
- [ ] CLI and web produce identical approval behavior
- [ ] Tests cover low/high/mixed confidence scenarios
- [ ] Audit logging for filtered chunks
- [ ] All web tests pass
```

---

### Issue #2: ADR-012 (Priority: MEDIUM)

**Title:**
```
ADR-012: Remove PyMuPDF, pin Docling version
```

**Labels:** `architecture`, `dependencies`, `adr-resolution`

**Body:**
```markdown
## Summary
**ADR**: [ADR-012: Docling as Primary PDF Extractor](docs/adrs/generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md)  
**Priority**: MEDIUM (eliminates AGPL licensing blocker)  
**Effort**: ~4 days (1 PR)

## Problem
PyMuPDF carries AGPL-3.0 license, which is viral and blocks commercial distribution or exposing the web UI beyond personal/local use. Docling should be the only extractor.

## Decision (ADR-012 Resolved 2026-08-31)
1. **Remove PyMuPDF** entirely from dependencies
2. **Pin Docling version** to specific version (e.g., `docling==1.11.0`) for reproducibility
3. **Make Docling mandatory** — harvest fails explicitly if Docling unavailable (no graceful degradation)

## Implementation
See [ACTION-PLAN-2026-08-31.md](docs/adrs/ACTION-PLAN-2026-08-31.md#adr-012-docling-remove-pymupdf) for:
- Dependency changes (pyproject.toml)
- Code removal (harvester.py fallback paths)
- Error handling updates
- Test strategy

## Acceptance Criteria
- [ ] PyMuPDF removed from dependencies
- [ ] Docling pinned to specific version
- [ ] Fallback extraction code paths removed
- [ ] Error messages explicit about Docling being mandatory
- [ ] Tests verify harvest fails clearly on Docling unavailability
- [ ] Manual testing on real PDFs passes
```

---

### Issue #3: ADR-017 (Priority: LOW)

**Title:**
```
ADR-017: Document confidence thresholds as tunable heuristics
```

**Labels:** `documentation`, `adr-resolution`

**Body:**
```markdown
## Summary
**ADR**: [ADR-017: Confidence-Band HITL Approval Gate](docs/adrs/generated/REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)  
**Priority**: LOW (documentation only)  
**Effort**: ~0.5 day (1 PR)

## Problem
The confidence thresholds (0.4 very-low, 0.7 auto-approve) were initial estimates, not empirically calibrated. This should be documented so operators understand they're tunable.

## Decision (ADR-017 Resolved 2026-08-31)
Keep thresholds as heuristic values. Operators should monitor real-world impact and propose adjustments if imbalanced.

## Implementation
1. Update [CLAUDE.md](CLAUDE.md):
   - Document that thresholds are initial estimates
   - Note tuning process for operators
   
2. Update [config/config.yaml](config/config.yaml):
   - Add comment explaining thresholds are tunable
   
3. Verify [RUNBOOK.md](docs/adrs/RUNBOOK.md):
   - Includes threshold adjustment guidance ✅

## Acceptance Criteria
- [ ] CLAUDE.md review section documents threshold tuning
- [ ] config.yaml has explanatory comments
- [ ] RUNBOOK.md includes threshold adjustment guidance
```

---

## REFERENCE ISSUES (Documentation — Will Close as Completed)

These 23 issues are for **tracking/reference only**. Create them, then close with:
```
Close as: Completed
Comment: "Documented as ADR. Reference in code reviews via docs/adrs/ADR-INDEX.md"
```

---

### INFRA Module (8 ADRs)

#### Issue #4: ADR-001
**Title:** `ADR-001: SQLite with WAL Mode and FTS5 as Primary Persistence Layer`
**Labels:** `architecture`, `persistence`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-001: SQLite with WAL+FTS5](docs/adrs/generated/INFRA/ADR-001-sqlite-wal-fts5-primary-persistence.md)

## Status
✅ Accepted (2025-02, stable 18+ months)

## Purpose
Documents the choice of SQLite with WAL mode and FTS5 as primary persistence.
Used for relational state, metadata, cost tracking, and BM25 lexical search.

## Reference
Consult this ADR when:
- Changing persistence layer
- Modifying database schema
- Implementing new storage requirements

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #5: ADR-002
**Title:** `ADR-002: ChromaDB Embedded Client as Vector Store`
**Labels:** `architecture`, `embeddings`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-002: ChromaDB Embedded](docs/adrs/generated/INFRA/ADR-002-chromadb-embedded-vector-store.md)

## Status
✅ Accepted (2025-03-01)

## Purpose
Documents the choice of ChromaDB in embedded PersistentClient mode for vector storage.
Maintains 5 collections: sources, chunks, permanent_notes, mocs, literature_notes.

## Reference
Consult this ADR when:
- Adding new embedding collections
- Changing vector store provider
- Modifying embedding model

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #6: ADR-003
**Title:** `ADR-003: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor`
**Labels:** `architecture`, `retrieval`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-003: Hybrid Retrieval](docs/adrs/generated/INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)

## Status
✅ Accepted (2024-08-30, stable 1.5+ years, one bug fix)

## Purpose
Documents Reciprocal Rank Fusion combining ChromaDB embeddings + SQLite FTS5 BM25.
Includes absolute relevance floor to prevent confidently-ranked but off-topic results.

## Reference
Consult this ADR when:
- Modifying retrieval pipeline
- Tuning relevance thresholds
- Optimizing search quality

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #7: ADR-004
**Title:** `ADR-004: YAML-First Configuration with Pydantic Fallback`
**Labels:** `architecture`, `configuration`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-004: YAML-First Config](docs/adrs/generated/INFRA/ADR-004-yaml-first-configuration.md)

## Status
✅ Accepted (2025-02-01)

## Purpose
Documents that config.yaml is the operational source of truth.
Pydantic Field defaults exist only as test scaffolding.

## Reference
Consult this ADR when:
- Adding new configuration options
- Changing config loading logic
- Understanding config/code relationship

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #8: ADR-005
**Title:** `ADR-005: Dual-Store Persistence Without Cross-Store Transactions`
**Labels:** `architecture`, `persistence`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-005: Dual-Store Persistence](docs/adrs/generated/INFRA/ADR-005-dual-store-persistence.md)

## Status
✅ Accepted (2025-03-01)

## Purpose
Documents the architectural choice to use SQLite + ChromaDB independently.
No cross-store transactions; mitigated via checkpointing + manual reconciliation.

## Reference
Consult this ADR when:
- Working on sync/reindex operations
- Investigating data consistency issues
- Planning disaster recovery

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #9: ADR-006
**Title:** `ADR-006: Pydantic v2 for Configuration Schema and LLM-Backed DTOs`
**Labels:** `architecture`, `schema`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-006: Pydantic v2](docs/adrs/generated/INFRA/ADR-006-pydantic-v2-config-dtos.md)

## Status
✅ Accepted (2024-08-30)

## Purpose
Documents use of Pydantic v2 for both config schema and LLM structured outputs.

## Reference
Consult this ADR when:
- Adding LLM DTOs (schemas.py)
- Modifying config schema
- Changing validation rules

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #10: ADR-007
**Title:** `ADR-007: Layered Hashing Strategy for Deterministic Caching and Drift Detection`
**Labels:** `architecture`, `hashing`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-007: Layered Hashing](docs/adrs/generated/INFRA/ADR-007-layered-hashing-strategy.md)

## Status
✅ Accepted (2025-02-28)

## Purpose
Documents 6-layer SHA-256 checksums over normalized text for deterministic LLM caching.

## Reference
Consult this ADR when:
- Implementing deduplication
- Changing hashing strategy
- Modifying LLM caching logic

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #11: ADR-008
**Title:** `ADR-008: Repository Pattern for Data Access (StateDB and VectorIndex)`
**Labels:** `architecture`, `data-access`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-008: Repository Pattern](docs/adrs/generated/INFRA/ADR-008-repository-pattern-data-access.md)

## Status
✅ Accepted (2024-08-30)

## Purpose
Documents two dedicated repository classes: StateDB for SQLite, VectorIndex for ChromaDB.

## Reference
Consult this ADR when:
- Adding new data access methods
- Changing repository interfaces
- Understanding data layer abstraction

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

---

### RETRIEVAL Module (2 ADRs)

#### Issue #12: ADR-009
**Title:** `ADR-009: Graph-Based Note Discovery with Weighted BFS Expansion`
**Labels:** `architecture`, `retrieval`, `graph`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-009: Graph Expansion](docs/adrs/generated/RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md)

## Status
✅ Accepted (2026-07-18)

## Purpose
Documents weighted BFS over note_connections to find conceptually-opposite notes.

## Reference
Consult this ADR when:
- Modifying graph expansion logic
- Tuning edge weights
- Optimizing traversal algorithm

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #13: ADR-010
**Title:** `ADR-010: Retrieval Result Transparency (Hits vs Candidates)`
**Labels:** `architecture`, `retrieval`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-010: Result Transparency](docs/adrs/generated/RETRIEVAL/ADR-010-retrieval-result-transparency-hits-vs-candidates.md)

## Status
✅ Accepted (2026-07-18)

## Purpose
Documents NoteSearchResult with both hits (cleared floor) and candidates (raw pool).

## Reference
Consult this ADR when:
- Modifying retrieval return types
- Changing result filtering
- Adding new retrieval consumers

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

---

### HARVEST Module (4 ADRs)

#### Issue #14: ADR-011
**Title:** `ADR-011: Three-Layer Duplicate Detection Strategy for Source Ingestion`
**Labels:** `architecture`, `harvest`, `deduplication`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-011: 3-Layer Dedup](docs/adrs/generated/HARVEST/ADR-011-three-layer-duplicate-detection.md)

## Status
✅ Accepted (2026-07-04)

## Purpose
Documents file hash → extraction hash → semantic similarity detection pipeline.

## Reference
Consult this ADR when:
- Modifying duplicate detection
- Tuning similarity thresholds
- Understanding dedup behavior

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #15: ADR-012 ⭐ (See OPEN ISSUES above)

#### Issue #16: ADR-013
**Title:** `ADR-013: Three-Layer Page Inference Strategy for Chunk Page Metadata`
**Labels:** `architecture`, `harvest`, `paging`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-013: 3-Layer Paging](docs/adrs/generated/HARVEST/ADR-013-three-layer-page-inference-strategy.md)

## Status
✅ Accepted (2024-08-30)

## Purpose
Documents explicit PDF metadata → text pattern → interpolation for page inference.

## Reference
Consult this ADR when:
- Modifying page detection
- Handling paging edge cases
- Supporting new document types

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #17: ADR-014
**Title:** `ADR-014: Hybrid Structural Chunking (H1-H6 Boundaries + Recursive Splitter)`
**Labels:** `architecture`, `harvest`, `chunking`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-014: Hybrid Chunking](docs/adrs/generated/HARVEST/ADR-014-hybrid-structural-chunking-strategy.md)

## Status
✅ Accepted (foundational, stable)

## Purpose
Documents two-stage: split at H1-H2 boundaries, then H3-H6, then recursive fallback.

## Reference
Consult this ADR when:
- Modifying chunk boundaries
- Changing chunking configuration
- Understanding chunk structure

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

---

### EXTRACT Module (1 ADR)

#### Issue #18: ADR-015
**Title:** `ADR-015: Granular Per-Chunk Literature Notes with Readable Filenames`
**Labels:** `architecture`, `extract`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-015: Granular Lit Notes](docs/adrs/generated/EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md)

## Status
✅ Accepted (2026-08-28)

## Purpose
Documents one literature note per chunk (not monolithic per-source).
Filenames are human-readable: `LIT - AuthorYear - pNNN - topic-NNNN.md`

## Reference
Consult this ADR when:
- Modifying literature note structure
- Changing approval workflow
- Understanding note organization

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

---

### REVIEW Module (3 ADRs)

#### Issue #19: ADR-016
**Title:** `ADR-016: Post-Approval Concept Deduplication Timing`
**Labels:** `architecture`, `review`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-016: Post-Approval Dedup](docs/adrs/generated/REVIEW/ADR-016-post-approval-concept-deduplication-timing.md)

## Status
✅ Accepted (2026-08-29)

## Purpose
Documents deduplication runs after chunk approval, not during extraction.

## Reference
Consult this ADR when:
- Modifying dedup timing
- Understanding concept workflow
- Optimizing LLM cost

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #20: ADR-017 ⭐ (See OPEN ISSUES above)

#### Issue #21: ADR-018 ⭐ (See OPEN ISSUES above)

---

### GARDEN Module (3 ADRs)

#### Issue #22: ADR-019
**Title:** `ADR-019: Taxonomy-First MOC Clustering with UMAP+HDBSCAN`
**Labels:** `architecture`, `garden`, `clustering`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-019: Taxonomy Clustering](docs/adrs/generated/GARDEN/ADR-019-taxonomy-first-moc-clustering.md)

## Status
✅ Accepted (2026-08-26, stable 4+ weeks)

## Purpose
Documents primary MOC strategy: embed category labels, assign notes, cluster within buckets.

## Reference
Consult this ADR when:
- Modifying MOC generation
- Tuning clustering parameters
- Understanding taxonomy integration

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #23: ADR-020
**Title:** `ADR-020: Hub-Anchored MOC Generation as a Complementary Clustering Strategy`
**Labels:** `architecture`, `garden`, `clustering`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-020: Hub MOCs](docs/adrs/generated/GARDEN/ADR-020-hub-anchored-moc-pipeline.md)

## Status
✅ Accepted (2026-08-27, stable 4+ weeks)

## Purpose
Documents complementary MOC strategy: rank by graph degree, expand via BFS.

## Reference
Consult this ADR when:
- Modifying hub MOC generation
- Tuning hub ranking strategy
- Understanding graph-based organization

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #24: ADR-021
**Title:** `ADR-021: Single LLM Call Per Cluster with Intelligent Routing`
**Labels:** `architecture`, `garden`, `routing`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-021: Single-Call Routing](docs/adrs/generated/GARDEN/ADR-021-single-llm-call-per-cluster-routing.md)

## Status
✅ Accepted (2026-08-26, stable 4+ weeks)

## Purpose
Documents five-step decision tree: signature match → overlap → category → cohesion → generation.

## Reference
Consult this ADR when:
- Modifying MOC generation routing
- Tuning overlap threshold
- Optimizing LLM cost

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

---

### WEB Module (2 ADRs)

#### Issue #25: ADR-022
**Title:** `ADR-022: FastAPI Server-Rendered Web Interface (No SPA)`
**Labels:** `architecture`, `web`, `frontend`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-022: FastAPI+Jinja2](docs/adrs/generated/WEB/ADR-022-fastapi-server-rendered-jinja2.md)

## Status
✅ Accepted (2026-08-29)

## Purpose
Documents server-rendered Jinja2 templates (no SPA, no Node.js).

## Reference
Consult this ADR when:
- Modifying web UI architecture
- Adding new web routes
- Understanding frontend strategy

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #26: ADR-023
**Title:** `ADR-023: SQLite-Backed Persistent Job Queue with Single Worker Thread`
**Labels:** `architecture`, `web`, `queue`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-023: Job Queue](docs/adrs/generated/WEB/ADR-023-sqlite-backed-job-queue-single-worker.md)

## Status
✅ Accepted (2026-08-29)

## Purpose
Documents SQLite-backed job queue with single in-process worker thread.

## Reference
Consult this ADR when:
- Modifying job queue logic
- Adding new background operations
- Understanding concurrency model

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

---

### LLM Module (2 ADRs)

#### Issue #27: ADR-024
**Title:** `ADR-024: Pluggable Multi-Provider LLM Strategy`
**Labels:** `architecture`, `llm`, `provider`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-024: Multi-Provider LLM](docs/adrs/generated/LLM/ADR-024-multi-provider-llm-strategy.md)

## Status
✅ Accepted (2026-07-02, stable since inception)

## Purpose
Documents get_llm() gateway for pluggable LLM providers.

## Reference
Consult this ADR when:
- Switching LLM providers
- Adding new provider support
- Understanding LLM abstraction

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

#### Issue #28: ADR-025
**Title:** `ADR-025: System+Human Prompt Split for Provider-Agnostic Prompt Caching`
**Labels:** `architecture`, `llm`, `caching`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-025: Prompt Caching](docs/adrs/generated/LLM/ADR-025-prompt-caching-system-human-split.md)

## Status
✅ Accepted (2026-08-13)

## Purpose
Documents <!-- zettel:user --> marker split for provider-agnostic caching.

## Reference
Consult this ADR when:
- Writing new prompts
- Optimizing LLM cost
- Adding provider-specific caching

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

---

### CLI Module (1 ADR)

#### Issue #29: ADR-026
**Title:** `ADR-026: Typer and Rich as CLI Framework`
**Labels:** `architecture`, `cli`, `framework`, `adr-reference`
**Body:**
```markdown
## Reference ADR
[ADR-026: Typer+Rich](docs/adrs/generated/CLI/ADR-026-typer-rich-cli-framework.md)

## Status
✅ Accepted (2026-02-01, stable 20+ commits)

## Purpose
Documents Typer (type-hint CLI) + Rich (terminal styling).

## Reference
Consult this ADR when:
- Adding new CLI commands
- Modifying CLI framework
- Understanding CLI design

See [ADR-INDEX.md](docs/adrs/ADR-INDEX.md) for all 26 ADRs.
```

---

## Workflow for Creating Issues

### Step 1: Create OPEN Issues (Top Priority)
```
Create and leave OPEN:
  - Issue #1: ADR-018 (HIGH priority)
  - Issue #2: ADR-012 (MEDIUM priority)
  - Issue #3: ADR-017 (LOW priority)
```

### Step 2: Create REFERENCE Issues
```
Create all 23 reference issues, then immediately:
  1. Close each with comment: 
     "Documented as ADR. Reference in code reviews via docs/adrs/ADR-INDEX.md"
  2. Label as `adr-reference` + `wontfix`
  3. No milestone or assignee needed
```

### Step 3: Add to Project
```
Add all 26 issues to your project #3:
  - Status column: 
    - OPEN: "Todo" or "In Progress"
    - REFERENCE: "Done"
  - Link related issues for dependencies
```

---

**Total**: 26 Issues
- 3 OPEN (action required)
- 23 CLOSED (documentation/reference)

Use this file as your checklist! ✅
