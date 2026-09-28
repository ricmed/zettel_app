# zettel_app ADR Index (49 Decisions)

**Last Updated**: 2026-09-24  
**Status**: Complete — 49 formal ADRs across 13 modules

---

## Quick Navigation

| Module | Count | ADRs |
|--------|-------|------|
| **INFRA** | 11 | [001–008, 041–042, 052](#infra-core-infrastructure) |
| **RETRIEVAL** | 4 | [009–010, 036, 043](#retrieval-hybrid-search--graph) |
| **HARVEST** | 6 | [011–014, 027, 033](#harvest-ingestion--paging) |
| **EXTRACT** | 2 | [015, 034](#extract-literature-notes) |
| **REVIEW** | 1 | [016](#review-approval-gate) |
| **CONNECT** | 1 | [053](#connect-permanent-notes) |
| **GARDEN** | 3 | [019–021](#garden-moc-generation) |
| **WEB** | 4 | [022–023, 039–040](#web-ui--job-queue) |
| **LLM** | 4 | [024–025, 045, 048](#llm-provider--caching) |
| **QA-WRITING** | 4 | [028–029, 038, 051](#qa-writing--article-pipeline) |
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

### ADR-042: Domain as First-Class Config and Externalized Few-Shots

- **Status**: Accepted
- **Date**: 2026-09-06
- **Summary**: `domain.name` / `domain.examples_path` at the top level; few-shots leave the prompts. `gardener.domain` is gone. Category labels no longer share a domain prefix.
- **Link**: [`ADR-042-domain-as-first-class-config.md`](./generated/INFRA/ADR-042-domain-as-first-class-config.md)

### ADR-052: Embedding Providers via Registry, Asymmetric Query Side, Unit-Norm Contract

- **Status**: Accepted
- **Date**: 2026-09-21
- **Summary**: `_EF_BUILDERS` registry replaces the provider `if/elif`; adds `gemini` (`gemini-embedding-001`). A LangChain adapter routes Chroma's `embed_query` to the client's query side (`RETRIEVAL_QUERY`) and L2-normalizes when the model does not.
- **Link**: [`ADR-052-embedding-providers-registry.md`](./generated/INFRA/ADR-052-embedding-providers-registry.md)

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

### ADR-047: Chapter Summaries as a Library-Level Routing Index

- **Status**: Accepted
- **Date**: 2026-09-07
- **Summary**: The vault gains a library index it could not answer before: `zettel catalog` returns which sources treat a subject, which chapters, and how many permanent notes each chapter produced. Chapter summaries are embedded and searchable; the source summary is a reduce over them and is not embedded. A summary *routes* and is never evidence — a test pins it out of `ask`. Own relevance floor, measured at 0.70 (off-domain tops at 0.693, self-match bottoms at 0.745).
- **Link**: [`ADR-047-chapter-summaries-as-library-routing-index.md`](./generated/RETRIEVAL/ADR-047-chapter-summaries-as-library-routing-index.md)

---

### ADR-046: Bibliographic Duplicate Layers Before the Semantic One

- **Status**: Accepted
- **Date**: 2026-09-07
- **Summary**: Harvest dedupe goes from three layers to five. Exact DOI/ISBN (an identity) reuses the source silently; title + author (a heuristic) always asks and never merges on its own. Both are pure SQLite and run before any embedding.
- **Link**: [`ADR-046-bibliographic-duplicate-layers.md`](./generated/HARVEST/ADR-046-bibliographic-duplicate-layers.md)

---

### ADR-045: Cross-Source Overlap Is Corroboration, Not Duplication

- **Status**: Accepted
- **Date**: 2026-09-07
- **Summary**: Concept dedupe is scoped to one `source_id`. Two sources stating one idea yield two notes plus a `corroborates` edge derived by code at connect — never offered to the LLM, and weighted 0.45 because weight governs traversal, not importance.
- **Link**: [`ADR-045-cross-source-overlap-is-corroboration.md`](./generated/REVIEW/ADR-045-cross-source-overlap-is-corroboration.md)

---

### ADR-043: Distant Analogies as Suggestions, Not Graph Edges

- **Status**: Accepted
- **Date**: 2026-09-06
- **Summary**: Connect's third RAG group uses a local similarity floor and notes outside the candidate's taxonomy bucket. Those connections land in `auto-connections`, not `note_connections`.
- **Link**: [`ADR-043-distant-analogies-as-suggestions.md`](./generated/RETRIEVAL/ADR-043-distant-analogies-as-suggestions.md)

---

### ADR-036: A Topic Index for Routing, Fed Back Through the Relevance Floor

- **Status**: Accepted (2026-09-03)
- **Date**: 2026-09-03
- **Summary**: A cheap term -> note map on two surfaces (an `auto-topic-index` managed block per MOC, mirrored into a `topic_index_terms` table), sharing one term-extraction rule with the skill export. A query term that matches routes the note back through the **same** relevance floor carrying a real vector distance (id-restricted Chroma query) — never as a bypass, which is the shape of a bug already fixed once in the BM25 path. Amendment (2026-09-10): the literature-index / source-scope surface is gone — it never seeded retrieval.
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
- **Summary**: Two-stage: split at H1/H2 chapter boundaries, then at H3-H6 subsections, with recursive character-based splitter as fallback when a structural unit exceeds max size. Overlap preserves context across cuts. Amended 2026-09-02: CommonMark fenced blocks are atomic — headings inside a fence do not partition, and a fence larger than `chunk_size` is emitted as a single oversized chunk. Amended 2026-09-02: the original ATX heading is prefixed onto the first chunk of each section only (after fence split); continuations stay body-only. Amended 2026-09-05: a fenced section within `chunk_size * fence_section_slack` stays whole so a code block is not cut away from the prose that frames it, and prose orphaned above that budget is glued back onto its fence. Amended 2026-09-05: in native Markdown a single leading H1 is the document title, not a chapter — its body joins the preamble and its text prefixes every `section_path`.
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

### ADR-049: No Pre-LLM Gate on Extract - the Signal Is Real, the Saving Is Not

- **Status**: Accepted (2026-09-11)
- **Date**: 2026-09-11
- **Summary**: Measured won't-do. Leave-one-source-out over 611 labeled chunks from 5 sources: a logistic regression on chunk embeddings avoids only **5.4%** of Prompt 1 calls at zero note loss (USD 0.03 over the whole corpus), and at that operating point catches `structural` rejections exclusively (33/115) - none of `narrative`, `fragmented`, `promotional` or `trivial`. Per-fold AUC is 0.752-1.000, so the signal is real; what fails is the zero-loss constraint under one global threshold, which the single lowest-scoring accepted chunk fixes for the whole corpus - tolerating one lost note of 449 nearly doubles the saving, and a per-source oracle still only reaches 10.6%. A hand-written structural rule does worse (10/115 at the cost of 34 accepted notes).
- **Link**: [`ADR-049-no-pre-llm-gate-on-extract.md`](./generated/EXTRACT/ADR-049-no-pre-llm-gate-on-extract.md)

---

### ADR-050: The Extract Model Is Chosen Against the Human Gold Set — gemini-3.5-flash-lite

- **Status**: Accepted (2026-09-13)
- **Date**: 2026-09-13
- **Summary**: `llm.extract` returns to `gemini-3.5-flash-lite` @ 0.1 after a pre-registered comparison with `gpt-4o-mini` over the 120 human-labeled gold chunks (#181). Rule chosen by the user before the runs: net correct items, lost note and admitted junk weighing the same. Gemini gets 97 items right against 83 (18 vs 4 discordant, exact McNemar p = 0.0043), agrees with itself on 97.4% of verdicts, and the corpus-weighted net difference (+28.2 chunks) agrees in sign. Corpus precision 60.9% -> 65.8%, recall 97.5% -> 92.5%. The trade is explicit: ~15 more lost notes to keep out ~43 junk chunks; a criterion weighing a lost note ~3x more would flip it.
- **Link**: [`ADR-050-extract-model-chosen-against-gold-set.md`](./generated/EXTRACT/ADR-050-extract-model-chosen-against-gold-set.md)

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

## CONNECT — Permanent Notes

### ADR-053: Connect Phase as Python Package

- **Status**: Accepted (2026-09-24)
- **Date**: 2026-09-24
- **Summary**: `zettel/connector.py` (1135 lines, a ~340-line `_process_candidate`) becomes the package `zettel/connector/`: `run` (entry gate + run loop), `note` (one candidate -> one ZTL, via a frozen `ConnectSession`), `prompt` (Prompt 2, cache, PT-BR guard), `context` (RAG, distant analogies, images) and `links` (typed relations, corroboration, backlinks). It follows the ADR-032/039 rules: siblings import by absolute path, no symbol is imported from `__init__`, no submodule is named after an export, and monkeypatching targets the consuming module. The refactor also surfaced a lost `refine_existing` verdict (see the ADR-045 amendment) and replaced a double SQLite write per note with a single one.
- **Link**: [`ADR-053-connect-phase-as-python-package.md`](./generated/CONNECT/ADR-053-connect-phase-as-python-package.md)

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

### ADR-045: Fail-Fast on LLM Unavailability

- **Status**: Accepted
- **Date**: 2026-09-09
- **Summary**: Transport/auth/missing-package errors become `LLMUnavailableError` in the LLM gateway. Harvest, extract and connect abort on the first occurrence; the in-flight item stays pending (not `failed`). `llm.max_retries` is the per-call budget only. No provider fallback (ADR-024).
- **Link**: [`ADR-045-fail-fast-on-llm-unavailability.md`](./generated/LLM/ADR-045-fail-fast-on-llm-unavailability.md)

---

### ADR-048: Per-Phase LLM Thinking Mode

- **Status**: Accepted
- **Date**: 2026-09-10
- **Summary**: `llm.<phase>.thinking` (`null` / `false` / level / token budget) is forwarded by `get_llm` to each vendor client and included in the SQLite LLM cache key. Vendor default applies when unset. Thought text is not persisted.
- **Link**: [`ADR-048-per-phase-thinking-mode.md`](./generated/LLM/ADR-048-per-phase-thinking-mode.md)

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

### ADR-051: Citation Provenance on the Permanent Note

- **Status**: Accepted (2026-09-21)
- **Date**: 2026-09-21
- **Summary**: A ZTL becomes mechanically citable. `zettel/citation.py` derives by code, never through the LLM, the printed page on which the grounded `anchor_quote` actually sits (`paging.locate_quote_pages` over the Docling page map, `p. 42-43` across a break, fallback to the chunk's first page recorded as `citation_page_confidence: chunk`) and the ABNT `citation`. Both land in the frontmatter and in an `auto-evidence` managed block that stays out of the embedding. `zettel article` cites per note with page, allows a direct quote only as a verbatim `citacao_direta`, and `verify_article` flags any other quoted passage.
- **Link**: [`ADR-051-citation-provenance-on-permanent-note.md`](./generated/QA-WRITING/ADR-051-citation-provenance-on-permanent-note.md)

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
| **Total ADRs** | 49 |
| **Accepted** | 48 |
| **Needs Input** | 0 |
| **Total Relationships** | 42 |
| **Modules Covered** | 13 |

---

## Status Update (2026-09-24)

✅ **ADR-053 added**: the connector is now a package (`zettel/connector/`), and CONNECT is no longer deferred. References in ADR-007, 030, 032, 034, 037, 043, 045 (both) and 047 now point at module + symbol.

✅ **ADR-045 amended**: a `refine_existing` / `merge` dedupe verdict now reaches `connect` through `concepts.dedupe_json` and becomes an `extends` edge. Before this, the target was dropped at the review/connect boundary. ADR-016 points to the amendment.

---

## Status Update (2026-09-21)

✅ **ADR-052 added** — embedding providers via a registry (`_EF_BUILDERS`), with Gemini as the first addition. Queries take the model's query side through Chroma's `embed_query`; adapters guarantee unit-norm vectors, which the relevance floor assumes. ADR-002's consequences were corrected (drift detection exists).

✅ **ADR-051 added** — citation provenance on the permanent note (issue #187). Page, ABNT citation and the verbatim anchor are copied onto the ZTL by code, with the page resolved to where the anchor actually sits. `zettel article` cites per note and checks direct quotes against grounded anchors.

---

## Status Update (2026-09-13)

✅ **ADR-050 added** — the extract model is chosen against the human gold set: `gemini-3.5-flash-lite` @ 0.1 over `gpt-4o-mini`, by a rule the user fixed before the runs (`evals/preregistration/181-modelo-extract.md`). Net correct 97 vs 83, p = 0.0043; corpus precision 60.9% -> 65.8% at the cost of recall 97.5% -> 92.5%.

✅ **ADR-017 addendum** — measured against the human gold set of #175, `review_confidence` does not separate what a human would keep from what a human would discard among accepted chunks: AUC 0.491 [0.298, 0.684], with integrity and completeness saturated on 97% of them. Corpus-weighted precision of `extract` is 63.2%. The gate and its threshold stay; every path that approves by threshold now prints `review.AUTO_APPROVE_UNVALIDATED_WARNING`. Issue #176 continues with an LLM-as-reader candidate signal.

---

## Status Update (2026-09-11)

✅ **ADR-049 added** — measured won't-do for the pre-LLM gate on `extract` (issues #66/#173). Leave-one-source-out over 611 chunks / 5 sources: 5.4% of calls avoided at zero note loss, USD 0.03, and only `structural` rejections caught. The calibration instrument was fixed first — `calls_avoided_pct` counted `fp` (calls that were made) as savings, and validation split by chunk instead of by source, which together inflated the same gate to 23.5% / 11.6%.

---

## Status Update (2026-09-10)

✅ **ADR-048 added** — per-phase LLM thinking mode (`llm.<phase>.thinking`). `get_llm` maps the knob onto each vendor client; the SQLite cache key includes it.

✅ **ADR-036 amendment** — the literature-index `auto-topic-index` and `scope_kind='source'` rows are gone. They were a reading aid that never seeded the Retriever; leftover blocks are stripped on `review` / `zettel reindex`. The MOC surface and the `note_id IS NOT NULL` filter stay. Library routing remains ADR-047.

---

## Status Update (2026-09-09)

✅ **ADR-045 added** — fail-fast when the configured LLM is unreachable. Extract/connect/harvest stop on the first availability error; pending items are not marked `failed`.

---

## Status Update (2026-09-06)

✅ **ADR-003 addendum (2026-09-09)** — the BM25 bypass gained an absolute half. `bm25_bypass_max_rank` is a *relative* test, and since `_fts_match_expr` ORs the query's terms, a match pool smaller than the cutoff made "top 5" mean "everything": an off-domain question returned notes rescued by one shared common word, at similarity well below the floor. `bm25_bypass_min_coverage` (0.5) now also requires the note to contain half the query's terms. Off-domain hits 31 -> 1 with zero in-domain loss on any of the four consumers. Measure with `scripts/probe_bm25_bypass.py` (no LLM, no embedding).

✅ **ADR-047 added (2026-09-07)** — source and chapter summaries, persisted and searchable as a routing index (`zettel summarize` / `zettel catalog`). Reopens the "corpus-wide library index" ADR-036 deferred, under the same routing-not-representation rule that keeps it clear of ADR-034's double-counting and ADR-043's LLM-guess gate. New Chroma collection `chapter_summaries`, with `zettel/catalog.py` as its named reader.

✅ **ADR-045 and ADR-046 added (2026-09-07)** — cross-source overlap becomes corroboration instead of deduplication (ADR-045); harvest dedupe gains two bibliographic layers (ADR-046). Amendments: ADR-011 (five layers), ADR-015 (`literature_notes` collection removed — it was write-only), ADR-016 (scope superseded, timing intact), ADR-030 (adoption no longer embeds), ADR-009 (`corroborates` weight 0.45), ADR-043 (carve-out: code-derived edges are not LLM judgement).

✅ **ADR-042 and ADR-043 added** — domain is first-class config with externalized few-shots (issues #156–#160); distant analogies are suggestions, not edges (issues #161, #165). ADR-004, ADR-009, ADR-019 and ADR-030 gained amendments (pillar labels, `note_connections.origin`, `zettel suggest-links`).

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

- **CONNECT** (1): permanent note generation routing (RAG provenance groups: ADR-043)
- **QA-WRITING** (1): ABNT bibliography citation formatting
- **Consider-Priority** (4): Various lower-priority architectural observations across modules

These remain documented in `docs/adrs/potential-adrs/` for later formalization if circumstances change.

---

## Next Steps

- Review all 29 formal ADRs in the [generated/ directory](./generated/)
- Consult the [relationship report](./reports/) for dependency graphs and temporal evolution
- Use this index as a reference when making changes to architecture-sensitive modules
