# zettel_app Architectural Decisions — Complete Overview

**Generated**: 2026-08-31  
**Status**: Phase 4 Complete — Index & Relationship Mapping + Resolution of All Needs-Input (2026-08-31)  
**Coverage**: 26 formal ADRs (100% Accepted, 0 Needs-Input) across 9 architectural modules, 37 documented relationships, 8 reserved for future consideration

---

## Executive Summary

The zettel_app project has captured **26 formal architectural decisions** spanning core infrastructure, retrieval, ingestion, extraction, review, MOC generation, web UI, LLM integration, and CLI orchestration. **All 26 are now Accepted as of 2026-08-31**, with no remaining needs-input. These decisions represent a stable, coherent architecture that has evolved incrementally over 18+ months without fundamental rework, indicating sound structural choices. The codebase is dominated by a **dual-store persistence model** (SQLite + ChromaDB) with a **hybrid retrieval layer** (RRF fusion + relevance floor + graph expansion), feeding **multiple frontends** (CLI via Typer/Rich, web via FastAPI/Jinja2) that orchestrate a **linear five-phase pipeline** (harvest → extract → review → connect → garden) plus **two complementary MOC strategies** (taxonomy-first and hub-anchored clustering).

Three decisions were formally resolved on 2026-08-31:
- **ADR-012** (Docling): Remove PyMuPDF, make Docling mandatory, pin version
- **ADR-017** (Confidence bands): Keep 0.4/0.7 as initial heuristics, enable tuning
- **ADR-018** (Web/CLI validation): Enforce threshold server-side uniformly

---

## Module Structure & Dependencies

```
INFRA (8 ADRs)
├─ Persistence: ADR-001 (SQLite+WAL+FTS5), ADR-002 (ChromaDB)
├─ Dual-Store: ADR-005 (Acknowledged cross-store risk)
├─ Schema: ADR-004 (YAML-first), ADR-006 (Pydantic v2)
├─ Hashing: ADR-007 (Layered deterministic caching)
└─ Access: ADR-008 (Repository pattern)

RETRIEVAL (2 ADRs)
├─ ADR-003 (Hybrid Dense+BM25 RRF + floor)
├─ ADR-009 (Graph expansion)
└─ ADR-010 (Result transparency)

HARVEST (4 ADRs + 1 needs-input)
├─ ADR-011 (3-layer duplicate detection)
├─ ADR-012 (Docling + PyMuPDF) [NEEDS-INPUT]
├─ ADR-013 (3-layer page inference)
└─ ADR-014 (Hybrid structural chunking)

EXTRACT (1 ADR)
└─ ADR-015 (Granular chunk-per-note)

REVIEW (1 ADR + 2 needs-input)
├─ ADR-016 (Post-approval dedup timing)
├─ ADR-017 (Confidence-band HITL) [NEEDS-INPUT]
└─ ADR-018 (Web/CLI validation gap) [NEEDS-INPUT]

GARDEN (3 ADRs)
├─ ADR-019 (Taxonomy-first clustering)
├─ ADR-020 (Hub-anchored MOCs)
└─ ADR-021 (Single-LLM-call routing)

WEB (2 ADRs)
├─ ADR-022 (FastAPI + Jinja2, no SPA)
└─ ADR-023 (SQLite job queue + single worker)

LLM (2 ADRs)
├─ ADR-024 (Multi-provider strategy)
└─ ADR-025 (System+Human prompt split)

CLI (1 ADR)
└─ ADR-026 (Typer + Rich framework)
```

---

## Relationship Matrix (37 Total Connections)

### Tier 1: Foundation (Everything builds on these)

| ADR | Title | Used By | Related To |
|-----|-------|---------|-----------|
| **ADR-001** | SQLite+WAL+FTS5 | ADR-023, (all persistence) | ADR-008 |
| **ADR-002** | ChromaDB embedded | ADR-015, (all retrieval) | — |
| **ADR-003** | Hybrid RRF retrieval | ADR-009, ADR-010, (ask/article/sync) | ADR-007, ADR-011, ADR-025 |
| **ADR-004** | YAML-first config | ADR-024 | ADR-006 |
| **ADR-007** | Layered hashing | ADR-011, ADR-014, ADR-021 | ADR-003, ADR-025 |
| **ADR-008** | Repository pattern | (all modules) | ADR-001, ADR-022, ADR-026 |

### Tier 2: Processing Phases (Build on Tier 1)

| ADR | Title | Depends On | Feeds Into |
|-----|-------|-----------|------------|
| **ADR-011** | 3-layer duplicate detect | ADR-007, ADR-012 | ADR-003 (harvest layer-3) |
| **ADR-012** | Docling + PyMuPDF | — | ADR-013, ADR-014 [NEEDS-INPUT] |
| **ADR-013** | 3-layer page inference | ADR-012 | ADR-014 |
| **ADR-014** | Hybrid chunking | ADR-012, ADR-007 | (harvest output) |
| **ADR-015** | Granular lit-notes | ADR-002, ADR-005 | ADR-017 |
| **ADR-016** | Post-approval dedup | ADR-017 | (connect input) |
| **ADR-017** | Confidence HITL | — | ADR-016 [NEEDS-INPUT] |
| **ADR-018** | Web/CLI validation | — | (review design) [NEEDS-INPUT] |

### Tier 3: Advanced Features (Build on Tier 1 & 2)

| ADR | Title | Depends On | Feeds Into |
|-----|-------|-----------|------------|
| **ADR-009** | Graph expansion | ADR-003 | ADR-020 |
| **ADR-010** | Result transparency | ADR-003 | (ask/article fallback) |
| **ADR-019** | Taxonomy clustering | (none; independent) | ADR-021 |
| **ADR-020** | Hub MOCs | ADR-021, ADR-009 | (independent complement) |
| **ADR-021** | Single-call routing | ADR-007 | ADR-019, ADR-020 |

### Tier 4: Frontends & Integration (Use Tier 1–3)

| ADR | Title | Depends On | Feeds Into |
|-----|-------|-----------|------------|
| **ADR-022** | FastAPI+Jinja2 | ADR-008, ADR-023, ADR-026 | (web orchestration) |
| **ADR-023** | SQLite job queue | ADR-001 | ADR-022 |
| **ADR-024** | Multi-provider LLM | ADR-004 | ADR-025 |
| **ADR-025** | System+Human split | ADR-024, ADR-007, ADR-003 | (all LLM calls) |
| **ADR-026** | Typer+Rich CLI | ADR-008 | (all commands) |

---

## Architectural Patterns Evident

### 1. **Layered Decision Model**
The architecture shows clear three-to-four-stage cascades at critical junctures:
- **Duplicate detection**: File hash → extraction hash → semantic similarity
- **Page inference**: Explicit metadata → text pattern → interpolation
- **MOC generation**: Signature match → overlap → category → cohesion gate → LLM call
- **Retrieval**: Dense vector → BM25 → relevance floor → graph expansion

This pattern indicates a **deliberate defense-in-depth design** where each layer handles what cheaper layers cannot, and ordering is optimized cheapest-to-accurate.

### 2. **Dual Strategy / Complementary Pathways**
- **Two MOC clustering strategies** (taxonomy-first as default, hub-anchored as opt-in complement)
- **Two retrieval result contracts** (`hits` for certain evidence, `candidates` for fallback/transparency)
- **Multiple extraction paths** (PDF via Docling, Markdown native, PyMuPDF fallback)

This suggests **conscious trade-off acceptance**: picking a default without eliminating the alternative, letting operators choose strategy based on context.

### 3. **Determinism as a Cross-Cutting Concern**
Layered hashing (ADR-007) appears in 6+ decision relationships. The system is built for:
- **Repeatable LLM caching** (same input → same cached output)
- **Deduplication across formats** (PDF and Markdown of same paper hash identically)
- **Drift detection** (comparing hashes to spot divergence between stores)

Determinism is load-bearing, not a nice-to-have.

### 4. **Cost Optimization as Architecture Driver**
Multiple ADRs explicitly optimize for LLM cost:
- **Post-approval dedup** (ADR-016): Don't spend LLM cost on rejected drafts
- **Single-call routing** (ADR-021): Reuse or skip LLM calls via signature/overlap checks
- **Lazy evaluation**: Graph expansion only on retrieval results that cleared the floor
- **Deterministic caching** (ADR-007): Avoid redundant calls on identical prompts

The system treats LLM calls as a scarce resource.

### 5. **Single-Store-Can't-Do-It-All Pragmatism**
Three ADRs (001, 002, 005) collectively document why a "one unified store" architecture was rejected:
- SQLite excels at relational integrity and BM25 but has no vector search
- ChromaDB excels at vector search but is weak at relational queries
- Neither has built-in transactions spanning both concerns
- **Accepted solution**: Keep both, mitigate cross-store drift via checkpointing + manual reconciliation tools

This is a mature, pragmatic trade-off document rather than an architectural mistake.

---

## Known Gaps & Tensions (Resolved ✅)

### ADR-012: Docling PDF Extraction (✅ Resolved 2026-08-31)
- **Decision**: Remove PyMuPDF, make Docling mandatory, pin Docling version
- **Rationale**: AGPL-3.0 viral license blocks distribution. Docling-only and version-pinned guarantees reproducibility
- **Impact**: Harvest now hard-fails if Docling unavailable (more rigor, less graceful degradation)

### ADR-017: Confidence-Band HITL Approval Gate (✅ Resolved 2026-08-31)
- **Decision**: Keep 0.4 and 0.7 as initial heuristic values, enable tuning per-deployment
- **Rationale**: No empirical calibration was done; these are starting estimates that will be validated against real-world operator workload
- **Impact**: No code change; threshold values documented as tunable

### ADR-018: Web/CLI Validation Asymmetry (✅ Resolved 2026-08-31)
- **Decision**: Enforce confidence threshold server-side on both CLI and web paths
- **Rationale**: Closing security bypass at `/review/action` endpoint. Make `literature_review.auto_approve_min_confidence` a uniform gate
- **Impact**: Code change required in `web_app.py` to add server-side threshold check to review job handler

See [RESOLUTION-LOG-2026-08-31.md](./RESOLUTION-LOG-2026-08-31.md) for full details and code action items.

---

## Data Flow Through the Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ INPUT: Inbox Files (PDF/Markdown)                                │
└──────────────────────────┬──────────────────────────────────────┘
                           │
         ┌─────────────────▼──────────────────┐
         │ PHASE 1: HARVEST (ADR-011,013,014)  │  ← ADR-001 (persist),
         │ • Dedup (3-layer)                   │    ADR-007 (hash)
         │ • Extract + Chunk                   │
         │ • Infer pages                       │
         └─────────────────┬──────────────────┘
                           │
         ┌─────────────────▼──────────────────────┐
         │ PHASE 2: EXTRACT (ADR-015)              │  ← ADR-002 (embed),
         │ • LLM draft literature notes            │    ADR-025 (prompt split)
         │ • Store in Review queue                 │
         └─────────────────┬──────────────────────┘
                           │
         ┌─────────────────▼──────────────────────┐
         │ PHASE 2b: REVIEW (ADR-016,017,018)     │  ← ADR-004 (config),
         │ • Human approve/reject                  │    ADR-008 (repo)
         │ • Dedupe concepts                       │
         │ • Embed approved notes                  │
         └─────────────────┬──────────────────────┘
                           │
         ┌─────────────────▼──────────────────────┐
         │ PHASE 3: CONNECT                        │  ← ADR-003 (retrieval),
         │ • Hybrid RAG retrieval (ADR-003,009)    │    ADR-010 (transparency)
         │ • Generate permanent notes               │
         └─────────────────┬──────────────────────┘
                           │
         ┌─────────────────▼──────────────────────┐
         │ PHASE 4: GARDEN                         │  ← ADR-019,020,021
         │ • Taxonomy-first clustering (default)   │    (MOC routing)
         │ • Hub-anchored clustering (opt-in)      │
         │ • Generate MOCs, sync backrefs           │
         └─────────────────┬──────────────────────┘
                           │
         ┌─────────────────▼──────────────────────┐
         │ VAULT: 30_Permanent, 40_MOCs             │  ← ADR-006 (schema),
         │ + Obsidian-compatible wikilinks          │    ADR-005 (dual-store)
         └──────────────────────────────────────────┘
                           │
        ┌────────────────────┴──────────────────┐
        │                                       │
    ┌───▼──────────────┐            ┌──────────▼──────┐
    │ CLI (ADR-026)     │            │ Web (ADR-022,23)│
    │ • ask             │            │ • Dashboard     │
    │ • article         │            │ • Upload/Review │
    │ • Status/Reindex  │            │ • Job queue     │
    └───────────────────┘            └─────────────────┘
         (Typer+Rich)                  (FastAPI+Jinja2)
```

---

## Decision Stability Profile

| Pattern | Stability | Notes |
|---------|-----------|-------|
| **Persistence choice** | **Very High** | 18+ months, no replacement discussion |
| **Hybrid retrieval** | **High** | Introduced ~1.5 years ago, one bug fix, no rework |
| **Chunking strategy** | **Very High** | Foundational, unchanged in depth |
| **Hashing/determinism** | **Very High** | Stable across 6+ extensions |
| **MOC clustering** | **High** | 4 days of tuning, stable 4+ weeks |
| **Web rendering** | **High** | No SPA debate in git history |
| **CLI framework** | **Very High** | 20+ commits, no alternative considered |
| **LLM provider abstraction** | **Very High** | Stable since inception, survived refactors |

**Overall**: Core decisions show **high maturity** (18+ months without rework). Newer decisions (MOC routing, hub clustering, review gate) are still tuning but have not needed structural changes.

---

## Relationship Density by Module

```
INFRA       ████████████ (8 direct, high fan-out)
RETRIEVAL   ███████      (2 ADRs, high usage)
HARVEST     █████████    (4 ADRs, foundational)
EXTRACT     ████         (1 ADR, focused)
REVIEW      ██████       (3 ADRs, mid-priority)
GARDEN      ███████      (3 ADRs, interconnected)
WEB         ████         (2 ADRs, narrow scope)
LLM         ███████      (2 ADRs, cross-cutting)
CLI         ████         (1 ADR, orchestration)
```

**Interpretation**: INFRA, HARVEST, RETRIEVAL, and GARDEN are high-coupling, load-bearing areas. EXTRACT, WEB, and CLI are more specialized, lower-coupling zones.

---

## Ungenerated Potential ADRs (Stored for Later)

Eight additional potential ADRs were identified but deferred as lower-priority or still-evolving:

| Module | Count | Candidates | Rationale |
|--------|-------|-----------|-----------|
| **CONNECT** | 2 | RAG context design, note-generation orchestration | Defer pending connector refactor |
| **QA-WRITING** | 2 | ask short-circuit on no-evidence, article fallback on empty retrieval | Defer pending user feedback |
| **Consider-Priority** | 4 | Various observability, resilience, migration ideas | Low-impact optimizations |

These remain documented in `docs/adrs/potential-adrs/` as a backlog for future phases if priorities shift.

---

## How to Use This Architecture Map

### For Code Review
- **Changing persistence?** Consult ADR-001, ADR-002, ADR-005, ADR-008.
- **Adding an LLM call?** Check ADR-024, ADR-025.
- **Modifying retrieval?** Review ADR-003, ADR-009, ADR-010.
- **Touching harvest/chunking?** Verify ADR-011–014 are still satisfied.

### For New Features
- **New note type?** Likely involves ADR-015 (granular notes) and ADR-002 (embedding).
- **New MOC strategy?** Must satisfy ADR-021 (single-call routing) constraints.
- **New frontend?** Review ADR-008 (repository pattern) for data access.

### For Architecture Decisions
- **Considering a unified data store?** ADR-005 documents why the dual-store trade-off was accepted.
- **Considering a SPA?** ADR-022 and ADR-026 explain why server rendering + Typer were chosen.
- **Considering a different LLM provider strategy?** ADR-024 and ADR-025 are the foundation.

---

## Next Steps

1. **Review all 26 ADRs** in [`docs/adrs/generated/`](./generated/) for full rationale and options
2. **Resolve 3 needs-input ADRs** (012, 017, 018) with team discussion
3. **Monitor relationship stability** — if a pattern in the Tier 1 foundation changes, a cascade of decisions is affected
4. **Consider periodic review** — every 6 months, check whether the top 5 load-bearing decisions (ADR-001, 003, 007, 008, 024) are still optimal given project growth

---

## Quick Links

- **[ADR Index](./ADR-INDEX.md)** — Numbered reference of all 26 ADRs  
- **[Generated ADRs](./generated/)** — Full text of all decisions  
- **[Relationship Report](./reports/)** — Detailed 37-connection graph  
- **[Potential ADRs](./potential-adrs/)** — 8 reserved for future formalization  
- **[Mapping Document](./mapping.md)** — Phase 1 codebase structure analysis  
