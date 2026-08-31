# ADR Relationship Analysis Report

**Generated:** 2026-08-30 18:01
**Scanned:** `docs/adrs/generated/` (all modules: INFRA, RETRIEVAL, HARVEST, GARDEN, EXTRACT, REVIEW, WEB, LLM, CLI)
**Processed:** 26 ADRs (all still carrying `ADR-XXX` placeholder numbers — no renumbering performed, all linking done by filename/relative path)

## Summary

| Relationship type | Unique pairs | Link insertions (both directions) |
|---|---|---|
| Supersedes / Superseded by | 0 | 0 |
| Depends on / Used by | 14 | 28 |
| Related to | 23 | 46 |
| Amends / Amended by | 0 | 0 |
| **Total** | **37** | **74** |

All 26 ADRs were modified (every ADR in the corpus received at least one relationship link). No Supersedes/Superseded-by or Amends relationships were detected — this corpus has no version-evolution (v1→v2) or narrow-amendment pairs; every ADR is either `Accepted` or (one case) `Proposed`, describing a still-current decision.

## Method notes specific to this run

* **Foundational-exclusion module override**: per explicit guidance, all 8 INFRA-folder ADRs (SQLite/WAL/FTS5, Repository Pattern, ChromaDB, YAML-first config, Pydantic v2, Dual-Store Persistence, Layered Hashing) and the CLI ADR (Typer/Rich) were treated as foundational-exclusion candidates — allowed as a **Depends on** target only when an existing manual relationship already justified it, or confidence was explicit and very high (>0.85). The **Hybrid Dense+BM25 Retrieval** ADR physically lives in the `INFRA/` folder but was treated as a **RETRIEVAL-family** decision (not foundational) per explicit guidance, and does appear as a normal Depends-on target for `RETRIEVAL-TRANS` and `RETRIEVAL-GRAPH`.
* **Manual "Related ADRs" hints preserved and upgraded**: 17 ADRs carried a pre-existing, non-clickable `**Related ADRs:** ADR-XXX (Title)` line. All were converted to clickable Markdown links and reciprocated. Several were upgraded from generic "Related to" to a more precise **Depends on** where the ADR body explicitly described a structural/functional reliance (not just topical overlap) — e.g. `three-layer-duplicate-detection` → `layered-hashing-strategy` (Layer 1/2 dedup *is* the checksum mechanism), `sqlite-backed-job-queue-single-worker` → `sqlite-wal-fts5-primary-persistence` (explicit reuse of the same SQLite store), `pluggable-multi-provider-llm-strategy` → `yaml-first-configuration` (`get_llm()` reads `cfg.llm.provider` directly from `config.yaml`).
* **New (non-manual) relationships detected** from cross-referencing Decision Outcome / Consequences sections and shared code references: the HARVEST pipeline order (Docling extraction → chunking → page inference → duplicate detection), the REVIEW approval chain (granular per-chunk notes → confidence-band gate → post-approval dedup / web-CLI threshold asymmetry), the GARDEN MOC pipeline (taxonomy-first clustering ↔ single-LLM-call routing → hub-anchored pipeline, which explicitly reuses both the routing logic and the RETRIEVAL graph-BFS traversal), and the LLM prompt-caching mechanism's explicit reuse of the provider-dispatch `call_llm()` entry point.
* **Max-3-links rule**: enforced for all ADRs except `INFRA/ADR-XXX-layered-hashing-strategy.md`, which is preserved with **5** "Related to" links because every one of them is manual or the reciprocal of another ADR's manual hint (exempted from the cap per the manual-relationship exception). This is flagged as a warning below.
* No circular dependencies and no conflicting relationship types (e.g. Supersedes + Depends-on on the same pair) were found.

## Relationship Breakdown by Type

### Depends on / Used by (14 pairs)

| Dependent | Depends on | Basis |
|---|---|---|
| HARVEST three-layer-duplicate-detection | INFRA layered-hashing-strategy | Layers 1–2 of dedup *are* the file/extraction checksum mechanism (manual hint, upgraded) |
| HARVEST three-layer-duplicate-detection | HARVEST/needs-input docling-primary-pdf-extractor-pymupdf-fallback | Extraction-level checksum (Layer 2) is computed over Docling's output; Docling ADR names duplicate detection as a consumer |
| HARVEST hybrid-structural-chunking-strategy | HARVEST/needs-input docling-primary-pdf-extractor-pymupdf-fallback | Docling ADR: "Markdown output with heading hierarchy is a prerequisite for structural (H1-H6) chunking" (explicit) |
| HARVEST three-layer-page-inference-strategy | HARVEST/needs-input docling-primary-pdf-extractor-pymupdf-fallback | Same Docling ADR sentence names page inference as a second explicit prerequisite consumer |
| RETRIEVAL retrieval-result-transparency-hits-vs-candidates | INFRA hybrid-dense-bm25-retrieval | `NoteSearchResult` "designed from the same commit that introduced the relevance floor" (explicit, structural) |
| RETRIEVAL graph-based-note-discovery-weighted-bfs | INFRA hybrid-dense-bm25-retrieval | BFS expansion runs "after hybrid RRF retrieval ranks the initial seeds" (explicit, structural) |
| GARDEN hub-anchored-moc-pipeline | GARDEN single-llm-call-per-cluster-routing | Hub pipeline "routes each resulting cluster through the same single-LLM-call generation logic" (explicit reuse) |
| GARDEN hub-anchored-moc-pipeline | RETRIEVAL graph-based-note-discovery-weighted-bfs | Hub BFS "reuses the same weighted, decay-based graph expansion already built for retrieval" (explicit reuse) |
| REVIEW post-approval-concept-deduplication-timing | REVIEW/needs-input confidence-band-hitl-approval-gate | Dedup runs only after the confidence-band approval decision moves a concept out of `extracted` |
| REVIEW/needs-input web-cli-auto-approve-threshold-validation-asymmetry | REVIEW/needs-input confidence-band-hitl-approval-gate | The asymmetry is entirely about enforcement of `auto_approve_min_confidence`, the threshold the gate ADR establishes |
| REVIEW/needs-input confidence-band-hitl-approval-gate | EXTRACT granular-literature-notes-readable-filenames | Per-chunk confidence gating is only possible because EXTRACT produces one draft per chunk (explicit) |
| WEB sqlite-backed-job-queue-single-worker | INFRA sqlite-wal-fts5-primary-persistence | Job queue explicitly persists to the same SQLite store (`web_jobs`/`web_job_events` tables), manual hint upgraded |
| LLM pluggable-multi-provider-llm-strategy | INFRA yaml-first-configuration | `get_llm()` reads `cfg.llm.provider` directly from `config.yaml` (explicit), manual hint upgraded |
| LLM system-human-prompt-split-for-provider-agnostic-caching | LLM pluggable-multi-provider-llm-strategy | Caching mechanism explicitly built on the shared `call_llm()` provider-dispatch entry point |

### Related to (23 pairs)

| ADR A | ADR B | Basis |
|---|---|---|
| INFRA sqlite-wal-fts5-primary-persistence | INFRA repository-pattern-data-access | Same module, StateDB directly wraps this store |
| INFRA repository-pattern-data-access | WEB fastapi-server-rendered-jinja2-no-spa | Manual hint (web routes delegate to the repository-injected service layer) |
| INFRA repository-pattern-data-access | CLI typer-rich-cli-framework | Manual hint (`_get_db()`/`_get_idx()` composition root) |
| INFRA hybrid-dense-bm25-retrieval | INFRA layered-hashing-strategy | Manual hint (own file) |
| INFRA hybrid-dense-bm25-retrieval | HARVEST three-layer-duplicate-detection | Manual hint; content explicitly contrasts the two threshold systems rather than sharing one |
| INFRA hybrid-dense-bm25-retrieval | LLM system-human-prompt-split-for-provider-agnostic-caching | Manual hint (weak topical link, preserved) |
| INFRA chromadb-embedded-vector-store | EXTRACT granular-literature-notes-readable-filenames | Manual hint (Chroma indexing deferred to REVIEW approval) |
| INFRA yaml-first-configuration | INFRA pydantic-v2-config-dtos | Same module; Pydantic validates the YAML-loaded config (explicit mutual reference) |
| INFRA dual-store-persistence | EXTRACT granular-literature-notes-readable-filenames | Manual hint |
| INFRA dual-store-persistence | REVIEW/needs-input web-cli-auto-approve-threshold-validation-asymmetry | Manual hint |
| INFRA dual-store-persistence | REVIEW/needs-input confidence-band-hitl-approval-gate | Manual hint |
| INFRA layered-hashing-strategy | HARVEST hybrid-structural-chunking-strategy | Manual hint |
| INFRA layered-hashing-strategy | HARVEST/needs-input docling-primary-pdf-extractor-pymupdf-fallback | Manual hint |
| INFRA layered-hashing-strategy | GARDEN single-llm-call-per-cluster-routing | Manual hint |
| INFRA layered-hashing-strategy | LLM system-human-prompt-split-for-provider-agnostic-caching | Manual hint |
| HARVEST hybrid-structural-chunking-strategy | HARVEST three-layer-page-inference-strategy | Same module, shared `paging.py` heading-path metadata |
| RETRIEVAL retrieval-result-transparency-hits-vs-candidates | RETRIEVAL graph-based-note-discovery-weighted-bfs | Same module, introduced in the same retrieval-enrichment commit |
| RETRIEVAL graph-based-note-discovery-weighted-bfs | GARDEN taxonomy-first-moc-clustering | Manual hint (reciprocated) |
| RETRIEVAL graph-based-note-discovery-weighted-bfs | GARDEN single-llm-call-per-cluster-routing | Manual hint (reciprocated) |
| GARDEN taxonomy-first-moc-clustering | GARDEN single-llm-call-per-cluster-routing | Same module, same commit (216a725), cross-referenced decision outcomes |
| GARDEN taxonomy-first-moc-clustering | GARDEN hub-anchored-moc-pipeline | Same module, complementary MOC-generation strategies |
| WEB fastapi-server-rendered-jinja2-no-spa | CLI typer-rich-cli-framework | Manual hint (reciprocated) |
| WEB fastapi-server-rendered-jinja2-no-spa | WEB sqlite-backed-job-queue-single-worker | Same module, same date, mutually necessary (routes dispatch into the job queue) |

## Key Chains

1. **HARVEST extraction chain**: `docling-primary-pdf-extractor-pymupdf-fallback` → depended on by `hybrid-structural-chunking-strategy`, `three-layer-page-inference-strategy`, and `three-layer-duplicate-detection` — Docling's structured Markdown output is the explicit prerequisite for all three downstream HARVEST decisions.
2. **REVIEW approval chain**: `granular-literature-notes-readable-filenames` (EXTRACT) → `confidence-band-hitl-approval-gate` → {`post-approval-concept-deduplication-timing`, `web-cli-auto-approve-threshold-validation-asymmetry`} — per-chunk granularity enables per-chunk confidence gating, which both the dedup-timing decision and the web/CLI asymmetry issue build directly on.
3. **GARDEN MOC pipeline**: `taxonomy-first-moc-clustering` ↔ `single-llm-call-per-cluster-routing` (same pipeline, same commit) → `hub-anchored-moc-pipeline` (depends on both the routing logic and RETRIEVAL's `graph-based-note-discovery-weighted-bfs`).
4. **RETRIEVAL enrichment**: `hybrid-dense-bm25-retrieval` (INFRA-housed but RETRIEVAL-family) is depended on by both `retrieval-result-transparency-hits-vs-candidates` and `graph-based-note-discovery-weighted-bfs`.
5. **LLM call chain**: `pluggable-multi-provider-llm-strategy` (depends on `yaml-first-configuration`) → depended on by `system-human-prompt-split-for-provider-agnostic-caching`.

## Modules with Most Relationships

1. INFRA — 8 ADRs, involved in 21 of the 37 unique relationships (as source or target)
2. GARDEN — 3 ADRs, 6 relationships
3. REVIEW — 3 ADRs, 5 relationships
4. HARVEST — 4 ADRs, 8 relationships

## Warnings

* **`INFRA/ADR-XXX-layered-hashing-strategy.md`** carries **5** "Related to" links, exceeding the recommended maximum of 3. All five are manual or reciprocals of another ADR's manual "Related ADRs" hint, so per the manual-relationship exception they were all preserved rather than truncated. A note explaining this was added inline in the file's header.
* No other ADR exceeds the max-3 cap for Depends-on or Related-to.

## Errors

None. All 74 inserted links resolve to existing files (verified programmatically); all relationships are fully bidirectional (Depends-on ↔ Used-by, Related-to ↔ Related-to); no circular Depends-on chains; no conflicting relationship types on any pair.

## ADRs Modified (26 of 26)

All ADRs in `docs/adrs/generated/` received at least one relationship-link update:

- `CLI/ADR-XXX-typer-rich-cli-framework.md`
- `EXTRACT/ADR-XXX-granular-literature-notes-readable-filenames.md`
- `GARDEN/ADR-XXX-hub-anchored-moc-pipeline.md`
- `GARDEN/ADR-XXX-single-llm-call-per-cluster-routing.md`
- `GARDEN/ADR-XXX-taxonomy-first-moc-clustering.md`
- `HARVEST/ADR-XXX-hybrid-structural-chunking-strategy.md`
- `HARVEST/ADR-XXX-three-layer-duplicate-detection.md`
- `HARVEST/ADR-XXX-three-layer-page-inference-strategy.md`
- `HARVEST/needs-input/ADR-XXX-docling-primary-pdf-extractor-pymupdf-fallback.md`
- `INFRA/ADR-XXX-chromadb-embedded-vector-store.md`
- `INFRA/ADR-XXX-dual-store-persistence.md`
- `INFRA/ADR-XXX-hybrid-dense-bm25-retrieval.md`
- `INFRA/ADR-XXX-layered-hashing-strategy.md`
- `INFRA/ADR-XXX-pydantic-v2-config-dtos.md`
- `INFRA/ADR-XXX-repository-pattern-data-access.md`
- `INFRA/ADR-XXX-sqlite-wal-fts5-primary-persistence.md`
- `INFRA/ADR-XXX-yaml-first-configuration.md`
- `LLM/ADR-XXX-pluggable-multi-provider-llm-strategy.md`
- `LLM/ADR-XXX-system-human-prompt-split-for-provider-agnostic-caching.md`
- `RETRIEVAL/ADR-XXX-graph-based-note-discovery-weighted-bfs.md`
- `RETRIEVAL/ADR-XXX-retrieval-result-transparency-hits-vs-candidates.md`
- `REVIEW/ADR-XXX-post-approval-concept-deduplication-timing.md`
- `REVIEW/needs-input/ADR-XXX-confidence-band-hitl-approval-gate.md`
- `REVIEW/needs-input/ADR-XXX-web-cli-auto-approve-threshold-validation-asymmetry.md`
- `WEB/ADR-XXX-fastapi-server-rendered-jinja2-no-spa.md`
- `WEB/ADR-XXX-sqlite-backed-job-queue-single-worker.md`
