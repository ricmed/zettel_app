# zettel_app ADR Index (41 Decisions)

**Last Updated**: 2026-09-05  
**Status**: Complete — 41 formal ADRs across 12 modules

---

## Quick Navigation

| Module | Count | ADRs |
|--------|-------|------|
| **INFRA** | 9 | [001–008, 041](#infra-core-infrastructure) |
| **RETRIEVAL** | 3 | [009–010, 036](#retrieval-hybrid-search--graph) |
| **HARVEST** | 6 | [011–014, 027, 033](#harvest-ingestion--paging) |
| **EXTRACT** | 2 | [015, 034](#extract-literature-notes) |
| **REVIEW** | 1 | [016](#review-approval-gate) |
| **GARDEN** | 3 | [019–021](#garden-moc-generation) |
| **WEB** | 4 | [022–023, 039–040](#web-ui--job-queue) |
| **LLM** | 2 | [024–025](#llm-provider--caching) |
| **QA-WRITING** | 3 | [028–029, 038](#qa-writing--article-pipeline) |
| **MANUAL** | 1 | [030](#manual-hand-written-notes) |
| **ASSETS** | 1 | [031](#assets-images) |

---

## INFRA — Core Infrastructure

### ADR-001: SQLite with WAL Mode and FTS5 as Primary Persistence Layer

- **Status**: Accepted
- **Date**: 2025-02 (foundational, stable ~18 months)
- **Summary**: SQLite with Write-Ahead Logging and FTS5 virtual tables as the sole relational store, chosen for single-VM deployment, concurrent read access during LLM calls, and co-located BM25 search without external infrastructure.
- **Link**: [`ADR-001-sqlite-wal-fts5-primary-persistence.md`](./generated/INFRA/ADR-001-sqlite-wal-fts5-primary-persistence.md)

---

### ADR-002: ChromaDB Embedded Client as Vector Store

- **Status**: Accepted
- **Date**: 2025-03-01
- **Summary**: ChromaDB in embedded `PersistentClient` mode (no server component) for local-first vector storage, chosen to keep all vectors on-disk, avoid third-party data exposure, and sidestep the known FastAPI server RCE surface.
- **Link**: [`ADR-002-chromadb-embedded-vector-store.md`](./generated/INFRA/ADR-002-chromadb-embedded-vector-store.md)

---

### ADR-003: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor

- **Status**: Accepted
- **Date**: 2024-08-30
- **Summary**: Reciprocal Rank Fusion combining ChromaDB embeddings and SQLite FTS5, gated by an absolute relevance floor, chosen to rescue jargon that dense-only search underrates while preventing confidently-ranked but off-topic results from passing as "relevant."
- **Link**: [`ADR-003-hybrid-dense-bm25-retrieval.md`](./generated/INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)

---

### ADR-004: YAML-First Configuration with Pydantic Fallback

- **Status**: Accepted
- **Date**: 2025-02-01
- **Summary**: `config/config.yaml` as the operational source of truth; Pydantic Field defaults exist only as test scaffolding. Secrets stay in `.env`, separate from this contract.
- **Link**: [`ADR-004-yaml-first-configuration.md`](./generated/INFRA/ADR-004-yaml-first-configuration.md)

---

### ADR-005: Dual-Store Persistence Without Cross-Store Transactions

- **Status**: Accepted
- **Date**: 2025-03-01
- **Summary**: SQLite and ChromaDB operate independently with no cross-store transaction guarantee; mitigated via phase-based checkpointing and manual reconciliation commands (`zettel reindex`, `zettel sync-manual`). This is a known risk accepted for architectural simplicity.
- **Link**: [`ADR-005-dual-store-persistence.md`](./generated/INFRA/ADR-005-dual-store-persistence.md)

---

### ADR-006: Pydantic v2 for Configuration Schema and LLM-Backed DTOs

- **Status**: Accepted
- **Date**: 2024-08-30
- **Summary**: Pydantic v2 chosen as the single validation mechanism for both operational configuration (15+ nested classes) and LLM structured outputs (5+ DTO classes across all pipeline phases).
- **Link**: [`ADR-006-pydantic-v2-config-dtos.md`](./generated/INFRA/ADR-006-pydantic-v2-config-dtos.md)

---

### ADR-007: Layered Hashing Strategy for Deterministic Caching and Drift Detection

- **Status**: Accepted
- **Date**: 2025-02-28
- **Summary**: Six-layer SHA-256 checksums over canonically normalized text (file → extraction → chapter → chunk → LLM call → note semantic), enabling deterministic LLM caching, dedup at multiple granularities, and cross-format equivalence detection.
- **Link**: [`ADR-007-layered-hashing-strategy.md`](./generated/INFRA/ADR-007-layered-hashing-strategy.md)

---

### ADR-008: Repository Pattern for Data Access (StateDB and VectorIndex)

- **Status**: Accepted
- **Date**: 2024-08-30
- **Summary**: Two dedicated repository classes — `StateDB` for SQLite, `VectorIndex` for ChromaDB — abstract the different APIs behind consistent gateways, keeping business logic decoupled from storage technology.
- **Link**: [`ADR-008-repository-pattern-data-access.md`](./generated/INFRA/ADR-008-repository-pattern-data-access.md)

---

### ADR-041: Dual Timezone — UTC in SQLite, Vault Timezone in Frontmatter

- **Status**: Accepted
- **Date**: 2026-09-05
- **Summary**: SQLite rows use UTC ISO timestamps; vault frontmatter and web display use `vault_timezone` (default `America/Sao_Paulo`) via `zettel/time.py`. No legacy naive parsing.
- **Link**: [`ADR-041-dual-timezone-utc-sqlite-vault-local.md`](./generated/INFRA/ADR-041-dual-timezone-utc-sqlite-vault-local.md)

---

## RETRIEVAL — Hybrid Search & Graph

### ADR-009: Graph-Based Note Discovery with Weighted BFS Expansion

- **Status**: Accepted
- **Date**: 2026-07-18
- **Summary**: Breadth-first search over `note_connections` (undirected, weighted by relation type, exponential hop decay) enriches RRF retrieval results with conceptually-opposite notes that embeddings structurally cannot find (e.g., `contradicts` edges).
- **Link**: [`ADR-009-graph-based-note-discovery-weighted-bfs.md`](./generated/RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md)

---

### ADR-010: Retrieval Result Transparency (Hits vs Candidates)

- **Status**: Accepted
- **Date**: 2026-07-18
- **Summary**: `NoteSearchResult` carries both `hits` (results cleared the relevance floor) and `candidates` (raw RRF-ranked pool before the floor), each with provenance fields (`floor_reason`, `vector_rank`, `bm25_rank`), making filtering transparent rather than opaque.
- **Link**: [`ADR-010-retrieval-result-transparency-hits-vs-candidates.md`](./generated/RETRIEVAL/ADR-010-retrieval-result-transparency-hits-vs-candidates.md)

---

### ADR-036: A Topic Index for Routing, Fed Back Through the Relevance Floor

- **Status**: Accepted (2026-09-03)
- **Date**: 2026-09-03
- **Summary**: A cheap term -> note map on two surfaces (an `auto-topic-index` managed block per source and per MOC, mirrored into a `topic_index_terms` table), sharing one term-extraction rule with the skill export. A query term that matches routes the note back through the **same** relevance floor carrying a real vector distance (id-restricted Chroma query) — never as a bypass, which is the shape of a bug already fixed once in the BM25 path.
- **Link**: [`ADR-036-topic-index-routing-not-representation.md`](./generated/RETRIEVAL/ADR-036-topic-index-routing-not-representation.md)

---

## HARVEST — Ingestion & Paging

### ADR-011: Three-Layer Duplicate Detection Strategy for Source Ingestion

- **Status**: Accepted
- **Date**: 2026-07-04
- **Summary**: Sequential file hash → extraction hash → semantic similarity checks before accepting a source as new, chosen to catch byte-identical copies, cross-format re-exports, and reformatted near-duplicates with cost-effective cheap-to-expensive ordering.
- **Link**: [`ADR-011-three-layer-duplicate-detection.md`](./generated/HARVEST/ADR-011-three-layer-duplicate-detection.md)

---

### ADR-012: Docling as Primary PDF Extractor (PyMuPDF Removed)

- **Status**: Accepted (2026-08-31)
- **Date**: 2024-08-30, Resolved 2026-08-31
- **Summary**: Docling is now the sole PDF extractor; PyMuPDF fallback removed to eliminate AGPL-3.0 licensing risk. Docling version is pinned for reproducibility. Harvest fails explicitly if Docling unavailable.
- **Link**: [`ADR-012-docling-pdf-extraction-pymupdf-fallback.md`](./generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md)

---

### ADR-013: Three-Layer Page Inference Strategy for Chunk Page Metadata

- **Status**: Accepted
- **Date**: 2024-08-30
- **Summary**: Explicit PDF metadata → text-pattern matching → interpolation (cascaded layers) to assign `page_in_file` to chunks, with each layer recorded as a confidence level (`explicit`, `inferred`, `unknown`), chosen to maximize page coverage across PDFs, Markdown, and OCR-derived sources.
- **Link**: [`ADR-013-three-layer-page-inference-strategy.md`](./generated/HARVEST/ADR-013-three-layer-page-inference-strategy.md)

---

### ADR-014: Hybrid Structural Chunking (H1-H6 Boundaries + Recursive Splitter)

- **Status**: Accepted
- **Date**: Unknown (foundational, predates tracked history)
- **Summary**: Two-stage: split at H1/H2 chapter boundaries, then at H3-H6 subsections, with recursive character-based splitter as fallback when a structural unit exceeds max size. Overlap preserves context across cuts. Amended 2026-09-02: CommonMark fenced blocks are atomic — headings inside a fence do not partition, and a fence larger than `chunk_size` is emitted as a single oversized chunk. Amended 2026-09-02: the original ATX heading is prefixed onto the first chunk of each section only (after fence split); continuations stay body-only.
- **Link**: [`ADR-014-hybrid-structural-chunking-strategy.md`](./generated/HARVEST/ADR-014-hybrid-structural-chunking-strategy.md)

---

### ADR-027: Harvest Phase as Python Package (Module Extraction from Monolith)

- **Status**: Accepted (2026-08-31)
- **Date**: 2026-08-31
- **Summary**: Extract monolithic `harvester.py` (1776 lines) into 8-module package (`extract.py`, `chunking.py`, `duplicates.py`, `biblio_hitl.py`, `citekey.py`, `pipeline.py`, `set_paging.py`, `__init__.py`) to improve testability, agent context window efficiency, and code readability. Public API maintained; no behavior changes.
- **Link**: [`ADR-027-harvest-phase-as-python-package.md`](./generated/HARVEST/ADR-027-harvest-phase-as-python-package.md)

---

### ADR-033: Invisible-Unicode Sanitization and a Text-Layer Probe Before Docling

- **Status**: Accepted (2026-09-03)
- **Date**: 2026-09-03
- **Summary**: Strip zero-width, bidi and Unicode-tag-block characters once at the extraction boundary (before `extraction_checksum`), so nothing invisible to the human reviewer reaches the prompt, the vault or the embedding; probe the first three pages with `pypdfium2` (already a Docling dependency, no PyMuPDF revival) and abort a scanned PDF before paying for conversion. One unusable file no longer stops the batch: `run_harvest` returns a `HarvestOutcome` carrying the skipped files and their reasons.
- **Link**: [`ADR-033-invisible-unicode-sanitization-and-text-layer-probe.md`](./generated/HARVEST/ADR-033-invisible-unicode-sanitization-and-text-layer-probe.md)

---

## EXTRACT — Literature Notes

### ADR-015: Granular Per-Chunk Literature Notes with Readable Filenames

- **Status**: Accepted
- **Date**: 2026-08-28
- **Summary**: Each chunk generates its own draft note (not a monolithic per-source index) with human-readable filename (`LIT - AuthorYear - pNNN - topic-NNNN.md`), chosen to enable per-chunk confidence tracking, individual approval, and human comparison of LLM interpretation against source excerpts.
- **Link**: [`ADR-015-granular-literature-notes-readable-filenames.md`](./generated/EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md)

---

### ADR-034: Author-Judgement Fields on the Candidate, Optional by Construction

- **Status**: Accepted (2026-09-03)
- **Date**: 2026-09-03
- **Summary**: Three optional list fields (`decision_rules`, `anti_patterns`, `named_frameworks`) on `PermanentNoteCandidate` — not on the chunk output, so a rule stays attached to the thesis it qualifies. Optionality is structural: `[]` defaults, no participation in `_check_candidate`, and a validator that truncates instead of raising. They render as an `auto-decision` managed block on the literature note and travel verbatim into the permanent note's frontmatter, so the skill export never re-parses a draft.
- **Link**: [`ADR-034-optional-author-judgement-fields.md`](./generated/EXTRACT/ADR-034-optional-author-judgement-fields.md)

---

## REVIEW — Approval Gate

### ADR-016: Post-Approval Concept Deduplication Timing

- **Status**: Accepted
- **Date**: 2026-08-29
- **Summary**: Semantic concept deduplication runs once after chunk approval (not during extraction or later during connection), avoiding LLM cost on rejected drafts while guaranteeing CONNECT never reads unmerged duplicates.
- **Link**: [`ADR-016-post-approval-concept-deduplication-timing.md`](./generated/REVIEW/ADR-016-post-approval-concept-deduplication-timing.md)

---

### ADR-017: Confidence-Band Human-in-the-Loop Approval Gate

- **Status**: Accepted (2026-08-31)
- **Date**: 2026-08-29, Resolved 2026-08-31
- **Summary**: Interactive REVIEW mode groups drafts by confidence band (≤0.4 / 0.4–limiar / ≥limiar), allowing batch approve-all-above-threshold or selective rejection per band. Thresholds (0.4, 0.7) are initial heuristics, tunable based on real-world impact.
- **Link**: [`ADR-017-confidence-band-hitl-approval-gate.md`](./generated/REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)

---

### ADR-018: Web/CLI Validation Asymmetry (Server-Side Enforcement)

- **Status**: Accepted (2026-08-31)
- **Date**: 2026-08-29, Resolved 2026-08-31
- **Summary**: Threshold validation is now enforced server-side on both CLI and web paths, eliminating bypass vector. The configuration `literature_review.auto_approve_min_confidence` is now a uniform gate. Future override capability (if needed) must be explicit and audited.
- **Link**: [`ADR-018-web-cli-validation-asymmetry.md`](./generated/REVIEW/ADR-018-web-cli-validation-asymmetry.md)

---

## GARDEN — MOC Generation

### ADR-019: Taxonomy-First MOC Clustering with UMAP+HDBSCAN

- **Status**: Accepted
- **Date**: 2026-08-26
- **Summary**: Embed category labels from `moc_topics.yaml`, assign notes to highest-similarity category first, then cluster within each bucket using UMAP+HDBSCAN (with KMeans fallback when optional dependencies are missing). Anchors MOCs to user-defined domains rather than emergent clustering alone.
- **Link**: [`ADR-019-taxonomy-first-moc-clustering.md`](./generated/GARDEN/ADR-019-taxonomy-first-moc-clustering.md)

---

### ADR-020: Hub-Anchored MOC Generation as a Complementary Clustering Strategy

- **Status**: Accepted
- **Date**: 2026-08-27
- **Summary**: Complementary pipeline (opt-in via `--hubs`): rank notes by weighted graph degree, expand neighborhoods via BFS, deduplicate overlaps, generate MOCs. Surfaces connectivity-based organization taxonomy-first clustering cannot detect.
- **Link**: [`ADR-020-hub-anchored-moc-pipeline.md`](./generated/GARDEN/ADR-020-hub-anchored-moc-pipeline.md)

---

### ADR-021: Single LLM Call Per Cluster with Intelligent Routing

- **Status**: Accepted
- **Date**: 2026-08-26
- **Summary**: Five-step decision tree (signature match → overlap → category → cohesion gate → generation) ensures at most one LLM call per cluster. Reuses existing MOCs when overlap ≥ threshold, preserves user edits on incremental updates.
- **Link**: [`ADR-021-single-llm-call-per-cluster-routing.md`](./generated/GARDEN/ADR-021-single-llm-call-per-cluster-routing.md)

---

## WEB — UI & Job Queue

### ADR-022: FastAPI Server-Rendered Web Interface (No SPA)

- **Status**: Accepted
- **Date**: 2026-08-29
- **Summary**: 23 endpoints serving complete HTML via Jinja2 templates, no separate frontend build; forms submit via standard POST/GET with server-side validation. SSE streams job progress without stateful client.
- **Link**: [`ADR-022-fastapi-server-rendered-jinja2.md`](./generated/WEB/ADR-022-fastapi-server-rendered-jinja2.md)

---

### ADR-023: SQLite-Backed Persistent Job Queue with Single Worker Thread

- **Status**: Accepted
- **Date**: 2026-08-29
- **Summary**: `web_jobs` and `web_job_events` tables + one in-process daemon thread, chosen to avoid external broker infrastructure, persist state across restarts, and serialize mutations to prevent concurrent races on StateDB/vault.
- **Link**: [`ADR-023-sqlite-backed-job-queue-single-worker.md`](./generated/WEB/ADR-023-sqlite-backed-job-queue-single-worker.md)

---

### ADR-039: Web as Python Package

- **Status**: Accepted
- **Date**: 2026-09-04
- **Summary**: `zettel/web.py` becomes `zettel/web/`. `server.py` (not `app.py`) holds the FastAPI factory; parametric detail routes register last so `/notes/new` is not captured by `/notes/{note_id}`. Same seams as ADR-032, plus that ordering rule.
- **Link**: [`ADR-039-web-as-python-package.md`](./generated/WEB/ADR-039-web-as-python-package.md)

---

### ADR-040: JSON Pickers and Progressive Enhancement

- **Status**: Accepted
- **Date**: 2026-09-04
- **Summary**: Deliberate, narrow exception to ADR-022: read-only JSON GETs may feed a form control that degrades to a server-rendered `<select>`. No JSON mutations, no SPA, no bundler.
- **Link**: [`ADR-040-json-pickers-progressive-enhancement.md`](./generated/WEB/ADR-040-json-pickers-progressive-enhancement.md)

---

## LLM — Provider & Caching

### ADR-024: Pluggable Multi-Provider LLM Strategy

- **Status**: Accepted
- **Date**: 2026-07-02
- **Summary**: `get_llm()` gateway instantiates LangChain chat clients (OpenAI, Anthropic, Gemini, Ollama) from `cfg.llm.provider`, making provider choice configurable at runtime without touching call sites.
- **Link**: [`ADR-024-multi-provider-llm-strategy.md`](./generated/LLM/ADR-024-multi-provider-llm-strategy.md)

---

### ADR-025: System+Human Prompt Split for Provider-Agnostic Prompt Caching

- **Status**: Accepted
- **Date**: 2026-08-13
- **Summary**: Every prompt file split via `<!-- zettel:user -->` marker into stable system instructions and per-call user payload, enabling implicit prefix reuse on OpenAI/Gemini/Ollama and explicit `cache_control` hints on Anthropic.
- **Link**: [`ADR-025-prompt-caching-system-human-split.md`](./generated/LLM/ADR-025-prompt-caching-system-human-split.md)

---

## CLI — Orchestration

### ADR-026: Typer and Rich as CLI Framework

- **Status**: Accepted
- **Date**: 2026-02-01
- **Summary**: 24 commands as decorated Typer functions with Rich for tables, panels, progress spinners, and interactive confirmations. Chosen for type-hint-driven argument parsing and polished terminal UI without adding frontend build infrastructure.
- **Link**: [`ADR-026-typer-rich-cli-framework.md`](./generated/CLI/ADR-026-typer-rich-cli-framework.md)

---

### ADR-032: CLI as Python Package

- **Status**: Accepted
- **Date**: 2026-09-03
- **Summary**: `zettel/cli.py` (2085 lines, 22 commands) split into `zettel/cli/` — four infrastructure modules plus ten command modules grouped by pipeline phase. Third application of the ADR-027/ADR-029 package pattern, closing the set of monoliths. Two structural seams (`app.py` imports nothing local; import order is `--help` order) are enforced by AST checks instead of convention.
- **Link**: [`ADR-032-cli-as-python-package.md`](./generated/CLI/ADR-032-cli-as-python-package.md)

---

### ADR-035: `zettel skill` Projects a Vault Slice as a Flat Agent Skill

- **Status**: Accepted (2026-09-03)
- **Date**: 2026-09-03
- **Summary**: A deterministic projection (no LLM, no new state) of an already-approved slice — source, MOC or taxonomy topic — into a flat Agent Skill pack: `SKILL.md` + `notes/` + `cheatsheet.md` + `glossary.md`. Only the Core section is budgeted at ~4000 tokens; the two indexes are the routing table and are never truncated. Source excerpts are excluded by default so a pack is publishable, while citekey and locator survive.
- **Link**: [`ADR-035-flat-agent-skill-export.md`](./generated/CLI/ADR-035-flat-agent-skill-export.md)

---

### ADR-037: Pre-Flight Cost Estimate as a Pure Function, Confirmation Only in the CLI

- **Status**: Accepted (2026-09-03)
- **Date**: 2026-09-03
- **Summary**: `zettel/preflight.py` estimates tokens and USD for `extract`, `connect` and `article` as **pure functions** (SQLite + config, no LLM call); `cli.deps.preflight_gate` renders the panel and asks. `run_*` is untouched, so the web worker and the test suite cannot acquire a new way to block. `--yes` and a non-TTY stdin pass straight through; a declined confirmation exits before any client is constructed. The estimate is a magnitude check, never a budget cap.
- **Link**: [`ADR-037-llm-cost-preflight-estimate.md`](./generated/CLI/ADR-037-llm-cost-preflight-estimate.md)

---

## QA-WRITING — Article Pipeline

### ADR-028: LangGraph StateGraph for Article Orchestration

- **Status**: Accepted
- **Date**: 2026-09-01
- **Summary**: `zettel article` is orchestrated by a LangGraph `StateGraph` (13 nodes, 3 conditional routers) instead of the SQLite-status staged pipeline used by harvest through garden, because the article flow needs loops (judge redraft), conditional re-entry (context enrichment) and two human-in-the-loop pauses via `interrupt()` / `Command(resume=...)`. Per-run `MemorySaver`; CLI-only, not exposed in web.
- **Link**: [`ADR-028-langgraph-stategraph-article-orchestration.md`](./generated/QA-WRITING/ADR-028-langgraph-stategraph-article-orchestration.md)

---

### ADR-029: Article Graph as Python Package

- **Status**: Accepted (2026-09-01)
- **Date**: 2026-09-01
- **Summary**: Extract monolithic `article_graph.py` (716 lines) into a 5-file package (`runtime.py`, `search.py`, `nodes.py`, `graph.py`, `__init__.py`), applying the ADR-027 precedent. Retrieval logic leaves `node_vector_search_merge` as four pure functions. Public API, graph topology and behavior unchanged; all LLM calls stay routed through `zettel/article.py`.
- **Link**: [`ADR-029-article-graph-as-python-package.md`](./generated/QA-WRITING/ADR-029-article-graph-as-python-package.md)

---

### ADR-038: Ask Evaluation as Offline Replay, Isolated from the Production Path

- **Status**: Accepted (2026-09-03)
- **Date**: 2026-09-03
- **Summary**: Research infrastructure that separates *routing* (`routing_miss`) from *representation* (`floor_reject`, `answer_fail`) instead of treating retrieval failure as one undifferentiated outcome. Replay first, live later: recorded trajectories are scored deterministically with no LLM and no network, so the scorer is verified before it is trusted to judge anything. Run identity hashes the manifest (including `commit_sha`), so a cross-commit comparison must be deliberate. Isolation from the pipeline is asserted by a test, not assumed.
- **Link**: [`ADR-038-ask-trajectory-evals-offline-replay.md`](./generated/QA-WRITING/ADR-038-ask-trajectory-evals-offline-replay.md)

---

## MANUAL — Hand-Written Notes

### ADR-030: Manual Notes Are Adopted at Sync Time and Bypass the Review Gate

- **Status**: Accepted
- **Date**: 2026-09-02
- **Summary**: A hand-written granular LIT note had no `chunks` row, so it was invisible to SQLite, `literature_notes`, the source index and `connect`. `sync-manual` now synthesizes that row (plus a per-source `::ch000` "Manual" chapter, required by the NOT NULL FK) and reuses the post-approval steps verbatim. Manual notes land as `persisted` and never enter the confidence-band gate, which is hereby scoped to LLM-generated content. `new-note ztl --from-lit [--llm]` derives a candidate from the note's own sections and reuses `connector.run_connect(..., origin="manual")` — same Prompt 2, RAG, relation typing and backlinks. CLI only; web deferred to phase 2.
- **Link**: [`ADR-030-manual-notes-adopted-at-sync-without-review-gate.md`](./generated/MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)

---

## ASSETS — Images

### ADR-031: Vault-First Image Adoption for Manual Notes

- **Status**: Accepted
- **Date**: 2026-09-02
- **Summary**: Attaching a figure to a manual note is the Obsidian gesture the user already makes: paste the image, run `sync-manual`. Adoption handles `![[...]]` embeds and `![alt](...)` refs, resolves vault-relative / note-relative / by basename, copies content-addressed into `90_Assets/` and registers an `assets` row identical to harvest's. Deliberately **not** gated on `images.enabled` (that flag governs LLM cost, and defaults to false) and deliberately **never** calls the LLM: the asset stays `pending` for `describe_pending_assets`. A dedicated `attach-image` command and a web upload page were considered and rejected for this round.
- **Link**: [`ADR-031-vault-first-image-adoption.md`](./generated/ASSETS/ADR-031-vault-first-image-adoption.md)

---

## Statistics

| Category | Count |
|----------|-------|
| **Total ADRs** | 41 |
| **Accepted** | 41 |
| **Needs Input** | 0 |
| **Total Relationships** | 42 |
| **Modules Covered** | 12 |

---

## Status Update (2026-09-05)

✅ **ADR-041 added** — dual timezone: UTC in SQLite, `vault_timezone` in vault frontmatter and web UI (issue #148). Central helpers in `zettel/time.py`; no legacy naive parsing.

---

## Status Update (2026-09-04)

✅ **ADR-039 and ADR-040 added** — the web layer is now the package `zettel/web/` (sister of ADR-027/029/032), and read-only JSON pickers with a `<select>` fallback are a documented exception to ADR-022's "every feature is a template".

---

## Status Update (2026-09-03)

✅ **ADR-033 through ADR-038 added** (epic #10). ADR-033 covers document hygiene at the ingestion boundary (issue #8): invisible-Unicode sanitization before the extraction checksum, plus a pdfium text-layer probe that aborts scanned PDFs before Docling runs. ADR-034 covers the optional author-judgement fields on the extraction candidate (issue #5). ADR-035 covers `zettel skill`, the deterministic flat Agent Skill export (issue #4). ADR-036 covers the topic index — routing, fed back through the relevance floor (issue #6). ADR-037 covers the pre-flight cost estimate for extract/connect/article (issue #7). ADR-038 covers the offline `ask` evaluation harness (issue #9). Epic #10 is complete.

---

## Status Update (2026-09-02)

✅ **ADR-030 and ADR-031 added** — the manual note flow gets formal coverage. ADR-030 (new MANUAL module) records that hand-written notes are adopted at sync time and skip the approval gate; ADR-031 (new ASSETS module, previously uncovered) records vault-first image adoption. ADR-030 supersedes the DISCARD verdict for "Decision 4: Manual Note Adoption Pattern" in [SYNC-module-analysis.md](./SYNC-module-analysis.md).

---

## Status Update (2026-09-01)

✅ **ADR-028 and ADR-029 added** — the article pipeline (QA-WRITING) now has formal coverage: ADR-028 records the LangGraph orchestration choice (promoted from `potential-adrs/`), ADR-029 records the package extraction of `article_graph.py` following the ADR-027 precedent.

---

## Status Update (2026-08-31)

✅ **All 26 ADRs now Accepted** — No needs-input remain. Three ADRs (012, 017, 018) were resolved on 2026-08-31 via team decision. See [RESOLUTION-LOG-2026-08-31.md](./RESOLUTION-LOG-2026-08-31.md) for details.

---

## Ungenerated Potential ADRs (7 of 34 identified, reserved for future decisions)

- **CONNECT** (2): RAG context handling, permanent note generation routing
- **QA-WRITING** (1): ABNT bibliography citation formatting
- **Consider-Priority** (4): Various lower-priority architectural observations across modules

These remain documented in `docs/adrs/potential-adrs/` for later formalization if circumstances change.

---

## Next Steps

- Review all 29 formal ADRs in the [generated/ directory](./generated/)
- Consult the [relationship report](./reports/) for dependency graphs and temporal evolution
- Use this index as a reference when making changes to architecture-sensitive modules
