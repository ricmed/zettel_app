# Codebase Architecture Mapping

## Project Overview

**Name**: zettel_app
**Purpose**: Converts PDF/Markdown source documents into an Obsidian-compatible Zettelkasten vault through a staged pipeline (`harvest -> extract -> review -> connect -> garden`), backed by a hybrid dense+lexical retrieval layer (GraphRAG-lite) that also powers a grounded Q&A command (`zettel ask`) and a LangGraph-based long-form writing pipeline (`zettel article`). A server-rendered FastAPI web UI exposes a curated subset of the same pipeline as background jobs.
**Type**: Single-process Python application — layered/modular monolith. CLI-first (Typer), with an optional web front end. No network-service decomposition; persistence, vector search, LLM orchestration, and presentation all run in one OS process against local files.
**Primary language**: Python 3.12 (`requires-python = ">=3.12"`), managed with `uv` (`pyproject.toml` + `uv.lock` authoritative; a parallel, looser `requirements.txt` exists for pip installs and is known to drift — see Cross-Cutting Concerns).
**Entry points**: `zettel/__main__.py` -> `cli.py` (`python -m zettel <command>`, 24 Typer subcommands); `uvicorn zettel.web:app` (separate FastAPI app, not a CLI subcommand). Two vestigial, unreferenced root scripts (`main.py`, `cuda-test.py`) are not real entry points.

## Technology Stack

- **Language/runtime**: Python 3.12, `uv`-managed.
- **CLI framework**: Typer (`typer[all]`) — 24 commands in `cli.py`.
- **Web framework**: FastAPI + Uvicorn, server-rendered Jinja2 templates (no SPA/JS build), `python-multipart` for uploads.
- **Data validation**: Pydantic v2 throughout (`config.py`, `schemas.py`).
- **LLM orchestration**: LangChain (`langchain-core`, `-text-splitters`, `-openai`, optional `-anthropic`/`-google-genai`/`-ollama`), LangGraph (article pipeline `StateGraph`).
- **LLM providers (pluggable via `llm.py`)**: OpenAI (default), Anthropic, Google Gemini, Ollama (local), OpenAI-compatible gateways (OpenRouter/OpenCode/etc. via `base_url`).
- **Cost estimation**: LiteLLM `cost_per_token` — price-map lookup only, not an LLM client.
- **Vector store**: ChromaDB (`==1.5.9`, pinned), embedded `PersistentClient` mode, 5 collections (`sources`, `chunks`, `permanent_notes`, `mocs`, `literature_notes`).
- **Relational store / search**: stdlib `sqlite3` (`state.py`), WAL journal mode, FTS5 virtual tables (`unicode61 remove_diacritics`) for BM25-style lexical search.
- **PDF/document processing**: Docling (primary extractor), PyMuPDF (fallback extractor + page-number mapping source; AGPL-3.0 licensed).
- **Clustering (MOC generation)**: scikit-learn always; UMAP + HDBSCAN when installed (pinned exact versions), silently falls back to KMeans otherwise.
- **ML runtime**: torch/torchvision, CUDA 12.6 wheel index, platform-gated to win32/linux — backs Docling models and/or local embedding inference; `config.yaml` hardcodes `device: cuda`.
- **Markdown handling**: `markdown-it-py` + `linkify-it-py` (parsing), `bleach` (HTML sanitization for web rendering — end-of-life library, no further patches).
- **IDs/hashing**: `python-ulid` (note/MOC IDs), stdlib `hashlib` via `hashing.py` (layered content checksums).
- **Testing**: pytest, 37 modules under `tests/`, no shared `conftest.py`, no `pytest.ini`.
- **Linting**: `ruff` (declared dependency; no CI config found to confirm automated enforcement).
- **Deployment target**: Replit VM (`.replit`), Nix-provisioned native deps (freetype, mupdf, openjpeg, etc.), single Uvicorn worker on port 5000, no containerization, no CI/CD pipeline found.

## Context Notes

**Source Files**: `docs_project/MANIFEST.md`, `docs_project/PROJECT-OVERVIEW-2026-08-30_11-12-00.md`, `docs_project/README-2026-08-30_11-12-00.md`, `docs_project/architectural-analyzer/architectural-report-2026-08-30_10-22-26.md`, `docs_project/dependency-auditor/dependencies-report-2026-08-30_10-22-26.md`, and all 43 files under `docs_project/component-deep-analyzer/` (one deep-dive per module/concern).

**Key Insights**:
- **Architectural patterns documented**: staged/pipeline processing (phases decoupled through persistent SQLite status fields, not direct calls), layered architecture within each phase (presentation -> pipeline module -> shared infrastructure), repository-pattern data access (`StateDB`/`VectorIndex` as sole gateways to their stores), job-queue/worker pattern in the web layer, strategy pattern for pluggable LLM/embedding providers, and a GraphRAG-lite hybrid retrieval pattern (RRF fusion + relevance floor + weighted BFS graph expansion).
- **Business domains identified**: none in the commercial-SaaS sense — this is a personal knowledge-management / research tool. The "domains" are pipeline phases (ingestion, drafting, review, synthesis, organization) plus two higher-level features (Q&A, long-form writing) built on the same retrieval core.
- **Module boundaries documented vs. code**: the architectural report's module inventory (43 components) matches the code 1:1 — no modules mentioned in docs but missing from code, and no undocumented modules found in code. This mapping consolidates those 43 fine-grained components into coarser, ADR-analysis-sized module groups (below) along pipeline-phase and infrastructure-role lines, which is a mapping-level grouping choice, not a discrepancy.
- **Technologies documented vs. discovered**: fully consistent. One confirmed drift already documented in the context: `requirements.txt` (loosely pinned, pip-oriented) has diverged from the authoritative `pyproject.toml`/`uv.lock` (UV-managed) — installing from `requirements.txt` would not reproduce the audited/locked environment and has `umap-learn`/`hdbscan` commented out, silently degrading MOC clustering to KMeans.
- **Discrepancies (code vs. its own docs, per the architectural analysis)**: CLAUDE.md says `run-all` is "Not exposed in web," but `web_app.py`'s dispatch table and a tested `web.py` route both support it; `zettel doctor`'s prompt-integrity checklist omits the `moc_hub_*` prompt files that `gardener_hub.py` depends on, and doesn't check `moc_topics.yaml`/`personalities.yaml` despite both being referenced by path from `config.yaml`. These are noted here for awareness during Phase 2 (potential "document what doctor actually checks" or "fix run-all exposure claim" candidates, not necessarily ADR-worthy on their own).

## System Modules

### Module Index
1. **CLI** - Command-Line Interface: Typer orchestration root wiring every pipeline command; zero test coverage.
2. **WEB** - Web UI: FastAPI app, job queue/worker, Jinja2 templates, auth/CSRF, Markdown rendering.
3. **INFRA** - Shared Infrastructure: config schema, SQLite persistence, ChromaDB wrapper, vault (Obsidian) I/O, hashing, DTOs, cost tracking, progress protocol.
4. **LLM** - LLM Integration Gateway: provider-agnostic LLM client, pricing.
5. **HARVEST** - Phase 1 Ingestion: PDF/Markdown extraction, paging, chunking, three-layer dedupe.
6. **EXTRACT** - Phase 2 Drafting: LLM-driven literature-note draft generation.
7. **REVIEW** - Phase 2b HITL Approval: selective approval gate promoting drafts into the vault/index.
8. **CONNECT** - Phase 3 Synthesis: RAG-based permanent (Zettelkasten) note generation.
9. **GARDEN** - Phase 4/4b Organization: taxonomy-driven and hub-anchored MOC generation, backlink maintenance.
10. **RETRIEVAL** - Hybrid Retrieval / GraphRAG: dense+BM25 fusion, relevance floor, graph BFS expansion.
11. **QA-WRITING** - Higher-level Features: grounded Q&A (`ask`) and long-form writing (`article`) built on Retrieval.
12. **MANUAL-SYNC** - Manual Vault Integration: manual note scaffolding, vault scan/sync, graph-loop closure, irreversible deletion, reindex/rebuild.
13. **ASSETS** - Media & Diagnostics Support: image/attachment handling, chunk/extraction debug dumps.
14. **PROMPTS-CFG** - LLM Prompts & Operational Configuration: prompt contract files, YAML config sources.
15. **TESTS** - Test Suite: pytest coverage across pipeline and infrastructure modules.

---

### CLI: Command-Line Interface
**Purpose**: Sole orchestration entry point; every pipeline/maintenance command is wired here (`(AppConfig, StateDB, VectorIndex)` composition via `_load_deps()`/`_get_db()`/`_get_idx()`).
**Location**: `zettel/cli.py`, `zettel/__main__.py`
**Key Components**: 24 `@app.command()` entries covering harvest, extract, review, connect, garden (+hubs, +recreate), ask, article, new-note, delete-source, sync-manual, purge-rejected, reindex, doctor, status, set-paging, rechunk, dump-chunks, dump-extraction, run-all, init.
**Technologies**: Typer, Rich (interactive prompts/tables).
**Dependencies**: Internal — nearly every pipeline module (21 internal imports, the highest efferent coupling in the codebase). External — none directly (delegates everything).
**Patterns**: Composition root / orchestrator; presentation layer holding no business logic itself.
**Key Files**: `zettel/cli.py` (1934 lines), `zettel/__main__.py`.
**Scope**: Large — 2 files, ~1935 lines. Confirmed **zero test coverage** (no `CliRunner`, no subprocess test) across the 391-test suite — notable for Phase 2 scoring.

### WEB: Web UI
**Purpose**: Server-rendered FastAPI front end exposing a curated subset of pipeline operations as background jobs, with authentication and a single-mutating-job-at-a-time queue.
**Location**: `zettel/web.py`, `zettel/web_app.py`, `zettel/markdown.py`, `zettel/templates/*` (14 files), `zettel/static/*` (3 stylesheets)
**Key Components**: HTTP routes (dashboard, documents, pipeline, review, notes/MOCs, runs/jobs, settings/health), `WebApplication` (daemon worker thread + SQLite-backed job queue `web_jobs`/`web_job_events`), HMAC-signed session cookie + CSRF, upload validation (extension allowlist, path-traversal guards, 25MB cap), Markdown -> sanitized-HTML rendering (bleach allowlist).
**Technologies**: FastAPI, Uvicorn, Jinja2, python-multipart, bleach, markdown-it-py.
**Dependencies**: Internal — `web_app.py` dispatches into `connector`, `extractor`, `gardener`, `gardener_hub`, `harvester`, `review`, `sync` + `config`/`state`/`index`/`schemas`/`usage`; `web.py` -> `web_app`, `harvester`, `hashing`, `index`, `markdown`. External — none direct (browser is the client).
**Patterns**: Job-queue/worker pattern; single-worker serialized concurrency by design (409 on concurrent mutating submit); auth via self-contained signed cookie, not a session store.
**Key Files**: `zettel/web.py` (622 lines, 23 endpoints incl. 1 SSE stream), `zettel/web_app.py` (404 lines).
**Scope**: Medium-Large — ~19 files including templates/static, ~1,000+ lines of Python. Known gap: the web `review` action enforces the auto-approve confidence threshold only client-side, unlike the CLI path.

### INFRA: Shared Infrastructure
**Purpose**: The stable base of the dependency graph — configuration, relational persistence, vector-store wrapper, Obsidian filesystem I/O, canonical hashing, DTOs, cost aggregation, and the progress-reporting protocol shared by CLI and web.
**Location**: `zettel/config.py`, `zettel/state.py`, `zettel/index.py`, `zettel/vault.py`, `zettel/hashing.py`, `zettel/schemas.py`, `zettel/usage.py`, `zettel/progress.py`
**Key Components**: `AppConfig` (Pydantic schema + `load_config()`), `StateDB` (SQLite/WAL, FTS5, graph edges `note_connections`, job queue, cost totals), `VectorIndex` (ChromaDB wrapper, 5 collections, pluggable embedding providers), vault builders/managed-block I/O (SRC/LIT/ZTL/MOC), layered checksums (file/extraction/chapter/chunk/llm_call/note_semantic) enabling deterministic LLM-response caching, Pydantic DTOs for every LLM structured output, `CostTracker` (contextvars-based), `ProgressObserver` protocol.
**Technologies**: Pydantic v2, stdlib `sqlite3`, ChromaDB, PyYAML, stdlib `hashlib`.
**Dependencies**: Internal — `state`/`config`/`vault`/`index`/`hashing`/`usage`/`schemas` have zero or near-zero outward dependency on other pipeline modules (Ce = 0-4); everything else depends on them. External — SQLite (file), ChromaDB (embedded), local filesystem (Obsidian vault tree).
**Patterns**: Repository-pattern data access (StateDB/VectorIndex as sole gateways); single source of runtime settings (`config.py`, 25 dependents — the highest afferent coupling in the codebase, tied with `state.py`'s 22).
**Key Files**: `zettel/state.py` (1725 lines — largest infra file), `zettel/config.py` (395 lines), `zettel/index.py` (766 lines), `zettel/vault.py` (758 lines), `zettel/hashing.py`, `zettel/schemas.py` (174 lines), `zettel/usage.py` (428 lines), `zettel/progress.py`.
**Scope**: Large — 8 files, ~4,000+ lines. Two structural single points of failure per the architectural report (`config.py`, `state.py`); no cross-store transaction guarantee between SQLite and ChromaDB, documented as a known coupling risk in CLAUDE.md itself.

### LLM: LLM Integration Gateway
**Purpose**: Provider-agnostic LLM client construction/invocation and cost-per-token price lookups, isolated from the pipeline modules that call them.
**Location**: `zettel/llm.py`, `zettel/pricing.py`
**Key Components**: `get_llm`/`call_llm`/`load_prompt_parts`/`fill_template`, system/user prompt splitting (`<!-- zettel:user -->`), Anthropic prompt-cache hints (`apply_prompt_cache_hints`), `usage_metadata` cost extraction onto the active `CostTracker`, LiteLLM `cost_per_token` wrapper.
**Technologies**: LangChain provider clients (OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible via `base_url`), LiteLLM (pricing map only, not a client).
**Dependencies**: Internal — `pricing`, `usage`. External — OpenAI API, Anthropic API, Google Gemini API, Ollama (local), OpenRouter/OpenCode-style gateways.
**Patterns**: Strategy pattern for provider selection (switches on `config.llm.provider`).
**Key Files**: `zettel/llm.py` (419 lines), `zettel/pricing.py` (no local price table — relies on LiteLLM's public map, refreshed by upgrading the dependency).
**Scope**: Medium — 2 files. Config currently pins the *embedding* path to local Ollama + CUDA (`config/config.yaml`), an environment-specific hard dependency worth flagging for Phase 2.

### HARVEST: Phase 1 Ingestion
**Purpose**: Scans `data/inbox/`, extracts text (Docling for PDF, native for Markdown), resolves content-start paging, chunks text, runs three-layer duplicate detection, indexes raw chunks into Chroma.
**Location**: `zettel/harvester.py`, `zettel/paging.py`
**Key Components**: SRC + literature-index file creation, citekey generation, structural (H3-H6) + LangChain-splitter chunking, `page_in_file`/`page_in_book` inference, three-layer dedupe (file hash -> extraction hash -> semantic similarity via Chroma), `set-paging`/`rechunk`/`dump-chunks`/`dump-extraction` CLI-adjacent flows.
**Technologies**: Docling, PyMuPDF, LangChain text splitters.
**Dependencies**: Internal — `assets`, `bibliography`, `chunk_dump`, `extraction_dump`, `paging`, `review` (status constants only, not a reverse dependency) + shared infra (`config`/`state`/`index`/`vault`/`hashing`/`schemas`/`usage`/`progress`). External — Docling, PyMuPDF.
**Patterns**: First stage of the staged pipeline; layered duplicate detection (cheapest/most-certain check first).
**Key Files**: `zettel/harvester.py` (1894 lines — the largest file in the codebase), `zettel/paging.py` (251 lines).
**Scope**: Large — 2 files, ~2,145 lines. **Confirmed high-risk bug already documented**: an unconditional early return in `_resolve_content_paging` (harvester.py:1782-1789) makes the content-start heuristic unreachable on every non-interactive path (CLI `--yes`, `run-all`, web harvest job) — silently defaults file page 1 = book page 1. Worth a look during Phase 2, though this is a bug-fix candidate more than an ADR candidate per se.

### EXTRACT: Phase 2 Drafting
**Purpose**: Processes each `pending` chunk through an LLM call (Prompt 1) to draft a literature note, with deterministic response caching.
**Location**: `zettel/extractor.py`
**Key Components**: Draft LIT note generation under `00_Inbox/Review/{Citekey}/`, source-excerpt managed block, per-chunk checkpointing to `awaiting_review`, `--auto-approve` fast path, dedupe logic deliberately kept separate from the shared `Retriever` (calibrated on raw L2 distance, not RRF).
**Technologies**: LangChain (via `llm.py`), Pydantic (`LiteratureChunkOutput`).
**Dependencies**: Internal — `review`, `assets` + shared infra. External — the configured LLM provider.
**Patterns**: Second stage of the staged pipeline; deliberate architectural isolation of its dedupe thresholds from the unified `Retriever`.
**Key Files**: `zettel/extractor.py` (639 lines).
**Scope**: Medium — 1 file. Core LLM-drafting logic has direct test coverage, but some interactive/heuristic branches are untested per the architectural report.

### REVIEW: Phase 2b HITL Approval
**Purpose**: Human-in-the-loop selective approval of literature-note drafts; promotes approved drafts into the vault and Chroma, dedupes and marks concepts `approved` for Phase 3.
**Location**: `zettel/review.py`
**Key Components**: Confidence-band interactive report (`<=0.4` / mid / `>= limiar`), batch approve/reject with sub-menus, `--yes`/`--auto-approve` non-interactive gate, rejected-draft deletion + `status=rejected` bookkeeping (hard-deleted later by `purge-rejected`).
**Technologies**: Rich (interactive prompts), Chroma `literature_notes` collection.
**Dependencies**: Internal — `extractor` (type reuse) + shared infra. External — none direct.
**Patterns**: HITL approval gate as an explicit pipeline stage (not just a UI convenience) — status transitions live in `StateDB`, not in-memory.
**Key Files**: `zettel/review.py` (671 lines).
**Scope**: Medium — 1 file. Documented web/CLI gating asymmetry: the web `/review/action` path enforces the auto-approve confidence threshold only client-side, and the web's manual review operation bypasses `literature_review.auto_approve_min_confidence` entirely — a candidate worth examining in Phase 2 for either a fix or an ADR on intended web/CLI parity.

### CONNECT: Phase 3 Synthesis
**Purpose**: Takes `approved` concepts and generates permanent (Zettelkasten) notes using RAG context from the hybrid `Retriever`.
**Location**: `zettel/connector.py`
**Key Components**: `run_connect`/`_process_candidate` orchestration, RAG context assembly via `Retriever.hits`, typed bidirectional backlink writing, `literature_ref` resolution (granular LIT preferred, source index fallback).
**Technologies**: LangChain (via `llm.py`), the shared `Retriever`.
**Dependencies**: Internal — `assets`, `retrieval`, `index` + shared infra. External — the configured LLM provider.
**Patterns**: Third stage of the staged pipeline; RAG generation pattern.
**Key Files**: `zettel/connector.py` (635 lines).
**Scope**: Medium — 1 file. **Explicitly acknowledged, unmitigated prompt-injection risk documented in-code** (connector.py:212-215) — source excerpts flow into the note-generation prompt without injection defenses; a strong Phase 2 candidate. Core orchestration function has no direct/indirect test coverage per the architectural report.

### GARDEN: Phase 4/4b Organization
**Purpose**: Builds Maps of Content (MOCs) via two complementary strategies — taxonomy-driven clustering and hub-anchored graph-degree neighborhoods — and maintains bidirectional MOC backlinks on linked permanent notes.
**Location**: `zettel/gardener.py`, `zettel/gardener_assign.py`, `zettel/gardener_hub.py`, `zettel/moc_backrefs.py`, `zettel/taxonomy.py`
**Key Components**: Category-label embedding + taxonomy-bucket assignment, UMAP+HDBSCAN clustering per category (KMeans fallback if optional packages absent), graph-cohesion scoring (`note_connections`), one-LLM-call-per-cluster routing (incremental vs. full `moc_generation`), hub ranking + BFS neighborhood expansion (`expand_notes`) for the complementary hub pipeline, `sync_moc_backrefs`/`clear_moc_backrefs` shared lifecycle, `--recreate` (taxonomy-only) vs. `--hubs --recreate` (hub-only) purge scoping.
**Technologies**: scikit-learn, UMAP, HDBSCAN (optional, pinned exact versions).
**Dependencies**: Internal — `taxonomy`, `moc_backrefs`, `graph` + shared infra; `gardener_hub` reaches into ~11 private symbols of `gardener.py` (documented duplication). External — none direct (clustering libraries only).
**Patterns**: Fourth stage of the staged pipeline; strategy duality (taxonomy-driven vs. hub-anchored) sharing a common backlink-maintenance substrate.
**Key Files**: `zettel/gardener.py` (892 lines), `zettel/gardener_hub.py` (625 lines), `zettel/gardener_assign.py` (283 lines).
**Scope**: Large — 5 files, ~1,900 lines. Clustering-library fallback (UMAP+HDBSCAN -> KMeans) is silent and undocumented at the point of failure — a candidate worth flagging for Phase 2 (dependency-driven behavior change with no runtime signal).

### RETRIEVAL: Hybrid Retrieval / GraphRAG
**Purpose**: Single composition point for note/chunk lookup — fuses ChromaDB dense search with SQLite FTS5 BM25 via Reciprocal Rank Fusion, applies an absolute relevance floor, then optionally expands surviving seeds over the note graph via weighted BFS.
**Location**: `zettel/retrieval.py`, `zettel/graph.py`
**Key Components**: `Retriever.search_notes()` -> `NoteSearchResult(hits, candidates)`, per-hit provenance (`vector_rank`/`bm25_rank`/`hop`/`via`/`passed_floor`), the 4-step relevance-floor gate (`absolute_min_similarity` -> bm25-rank bypass -> `min_vector_similarity` -> bm25-only-fails), `retrieval.mode` (`hybrid`/`vector`, auto-degrades when FTS disabled), `expand_notes` (Python BFS, weighted by `DEFAULT_RELATION_WEIGHTS`, hop decay).
**Technologies**: ChromaDB, SQLite FTS5.
**Dependencies**: Internal — used by `connector` (`.hits`), `sync` suggestions (`.hits`), `ask` (`.hits` + `.candidates`), `article` (`.hits` + catalog). Deliberately **not** used by extractor dedupe or harvester layer-3 (different, raw-L2-calibrated thresholds). External — none direct.
**Patterns**: GraphRAG-lite — classic IR (RRF fusion) combined with graph-based retrieval (BFS expansion), gated by an explicit, previously-buggy-in-production relevance floor.
**Key Files**: `zettel/retrieval.py` (331 lines), `zettel/graph.py`.
**Scope**: Medium — 2 files, but the architectural report calls this "the single most architecturally significant component reviewed." Already has one documented production bug fixed (unconditional BM25-bypass rank cutoff) — strong Phase 2 candidate given it's shared by four downstream consumers.

### QA-WRITING: Higher-level Features (Ask + Article)
**Purpose**: Grounded question-answering (`zettel ask`) and long-form writing (`zettel article`) built entirely on top of the `Retriever`, with a deterministic "no evidence" short-circuit and a LangGraph-based multi-stage writing flow respectively.
**Location**: `zettel/ask.py`, `zettel/article.py`, `zettel/article_graph.py`, `zettel/bibliography.py`
**Key Components**: `run_ask` (skips the LLM call entirely when `.hits` is empty, still surfaces `.candidates` for `--show-context`), `AskResult.retrieval_params` snapshot; `article_graph`'s 13-node LangGraph `StateGraph` (query enricher -> incremental hybrid search -> context HITL -> catalog -> outline HITL -> per-section draft -> assemble -> personality rewrite -> judge loop -> verify/save), ABNT citation formatting (`bibliography.py`, includes an LLM-merge path every test fixture disables).
**Technologies**: LangGraph (`StateGraph`, `MemorySaver` checkpointer, `interrupt()` for CLI HITL).
**Dependencies**: Internal — `retrieval`, `index`, `bibliography` + shared infra. External — the configured LLM provider.
**Patterns**: RAG Q&A with deterministic fallback; graph-of-nodes orchestration (LangGraph) with a bounded judge/redraft loop — distinct from the staged SQLite-status pipeline pattern used by phases 1-4.
**Key Files**: `zettel/article.py` (1161 lines), `zettel/article_graph.py` (715 lines), `zettel/ask.py` (314 lines), `zettel/bibliography.py` (836 lines).
**Scope**: Large — 4 files, ~3,000 lines. `bibliography.py`'s LLM-merge logic is untested in every fixture — worth noting for Phase 2 as a test-coverage gap rather than an architectural decision per se.

### MANUAL-SYNC: Manual Vault Integration
**Purpose**: Cross-cutting utilities that let hand-created/hand-edited vault content join the pipeline's SQLite/Chroma state, close the graph loop from body wikilinks, scaffold new manual notes, and irreversibly delete sources.
**Location**: `zettel/sync.py`, `zettel/new_note.py`, `zettel/purge_source.py`, `zettel/rebuild.py`
**Key Components**: `sync-manual` (scans `10_Sources/`, `20_Literature/`, `30_Permanent/`, `40_MOCs/`; assigns IDs/checksums; `auto-connections` managed block), `_extract_body_edges` (persists `[[wikilinks]]` outside managed blocks as `related` edges, never downgrading an already-typed edge), `rebuild_manual_edges` (`--rebuild-graph` full-vault backfill), `new-note` scaffolding (`origin: manual`, no DB/index writes), `delete-source` (irreversible cascade: vault + SQLite + Chroma, dead-wikilink stripping, optional `--delete-permanent`), `rebuild.py` (reindex/FTS-rebuild utilities).
**Technologies**: shared infra only.
**Dependencies**: Internal — `sync` -> `harvester`, `gardener`, `moc_backrefs`, `rebuild`, `retrieval` (reaches across the most phase boundaries outside the two composition roots); `rebuild` -> `gardener`; `new_note`/`purge_source` -> `vault`/`state`/`index`. External — none direct.
**Patterns**: Cross-cutting maintenance/backfill utilities, not staged-pipeline members; `purge_source`/`purge-rejected` are the only irreversible-deletion code paths in the system, both CLI-only.
**Key Files**: `zettel/sync.py` (421 lines), `zettel/purge_source.py` (342 lines), `zettel/new_note.py` (379 lines), `zettel/rebuild.py` (400 lines).
**Scope**: Medium-Large — 4 files, ~1,540 lines. Shares the cross-store-consistency caveat with the rest of the persistence layer (no cross-store transaction guarantee).

### ASSETS: Media & Diagnostics Support
**Purpose**: Embedded image/attachment extraction and multimodal description during harvesting, plus opt-in diagnostic export utilities for persisted chunks and raw extracted text.
**Location**: `zettel/assets.py`, `zettel/chunk_dump.py`, `zettel/extraction_dump.py`
**Key Components**: Docling/Markdown image extraction, multimodal LLM image-description calls, `dump-chunks`/`dump-extraction` markdown exports (with paging/`section_path`/overlap metadata) for both live and re-export (`--source-id @Citekey`) flows.
**Technologies**: Docling, PyMuPDF, the LLM gateway (for image description).
**Dependencies**: Internal — `assets` is used by `harvester` and `connector`; `chunk_dump`/`extraction_dump` are invoked by `harvester`/CLI. External — Docling/PyMuPDF (image extraction).
**Patterns**: Pipeline-support and debug/export utility patterns.
**Key Files**: `zettel/assets.py` (580 lines), `zettel/chunk_dump.py` (218 lines), `zettel/extraction_dump.py`.
**Scope**: Medium — 3 files. Documented duplication: `assets.py` reaches into private internals of `llm.py`'s provider-branching logic (candidate for a "should this be a shared internal API" note in Phase 2, likely below the ADR bar).

### PROMPTS-CFG: LLM Prompts & Operational Configuration
**Purpose**: The externalized, version-controlled contract for every LLM-backed phase, plus the YAML sources of truth for runtime behavior (per CLAUDE.md: "toda chave do schema deve estar no YAML").
**Location**: `prompts/*.md` (17 files), `config/config.yaml`, `config/moc_topics.yaml`, `config/personalities.yaml`
**Key Components**: System/user-split prompt templates (`<!-- zettel:user -->` marker) for literature notes, permanent notes, MOC generation (taxonomy + hub, full + incremental), dedupe decisions, bibliographic metadata, image description, ask, article (query enrichment, outline, section drafting, judge, personality rewrite, anti-AI guard), PT-BR language guard; `config.yaml` (all schema keys except `gardener.allowed_topics`), `moc_topics.yaml` (pillar > category > topic taxonomy), `personalities.yaml` (article rewrite styles, `neutral` skips the LLM).
**Technologies**: Plain Markdown (prompts), YAML (config).
**Dependencies**: Internal — loaded by `llm.py` (prompts) and `config.py`/`taxonomy.py` (YAML). External — none.
**Patterns**: Configuration-as-code / prompt-as-contract; secrets deliberately excluded (`.env` only, never `config.yaml`).
**Key Files**: `prompts/literature_note.md`, `prompts/permanent_note.md`, `prompts/moc_generation.md`, `prompts/moc_hub_generation.md`, `config/config.yaml`.
**Scope**: Small-Medium — 20 files, mostly declarative content. Documented drift: `zettel doctor`'s prompt-integrity checklist omits the two `moc_hub_*` files `gardener_hub.py` actually depends on, and doesn't check `moc_topics.yaml`/`personalities.yaml`.

### TESTS: Test Suite
**Purpose**: Pytest coverage mirroring the module boundaries above, one file per architectural concern.
**Location**: `tests/*.py` (37 files)
**Key Components**: Per-module unit/integration tests (harvester, extractor, review, connector, gardener x3, retrieval, ask, article x2, sync, state, index, vault, web x2, etc.).
**Technologies**: pytest.
**Dependencies**: Internal — imports most `zettel/*` modules (~33). External — none.
**Patterns**: One-test-file-per-module convention; no shared fixtures.
**Key Files**: `tests/test_state.py`, `tests/test_harvester_sections.py`, `tests/test_web.py`, `tests/test_web_state.py`.
**Scope**: Large — 37 files, 391 tests. **Confirmed gaps**: zero coverage of `cli.py`/`__main__.py`; no `conftest.py` (15+ near-duplicated `StateDB`/`tmp_path` fixtures redeclared per file); no `pytest.ini`/`[tool.pytest.ini_options]` anywhere in the project.

## Cross-Cutting Concerns

**Infrastructure & Deployment**: Single-process modular monolith; Replit VM deployment (`.replit`), Nix-provisioned native PDF/image dependencies, single Uvicorn worker on port 5000, no containerization, no CI/CD pipeline found in scope. All persistence (SQLite, embedded ChromaDB, Obsidian vault directory) is local-disk-based on the same VM instance — a full-system single point of failure by design, not oversight.

**Authentication & Security** (web layer only — CLI has no auth): HMAC-signed, HttpOnly/SameSite/Secure session cookie (`SESSION_SECRET` from process env, never `config.yaml`, timing-safe comparison via `hmac.compare_digest`); per-session CSRF tokens on every mutating route; separate login-CSRF token; strict upload validation (extension allowlist, filename regex, path-traversal guard via `relative_to()`, 25MB cap); two-layer Markdown sanitization (`markdown.py` allowlist + `bleach.clean()`). No login rate-limiting/lockout. No `eval`/`exec`/`subprocess`/`pickle`/`shell=True` found anywhere in `zettel/`.

**Data Layer**: Dual-store persistence with **no cross-store transaction guarantee** between SQLite (`state.py`) and ChromaDB (`index.py`) — flagged independently across the `review`, `index`, `state`, `purge_source`, and `sync` component analyses, and explicitly acknowledged in CLAUDE.md (`web_app.py`'s `_idx_kwargs` "must mirror" `cli._idx_kwargs`). Changing `embedding.provider`/`model`/`dimensions` invalidates all existing vectors with no automatic migration path (requires full `zettel reindex --force` + relevance-floor/dedupe threshold recalibration).

**API/Integration Layer**: No public API surface — this is a local tool, not a service. External integrations are all outbound: LLM/embedding providers (OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible gateways), Docling/PyMuPDF (PDF extraction), LiteLLM (pricing map only), Obsidian (pure filesystem contract, no API).

**Dependency Health** (from the dependency-auditor report): three risks rated critical — an unauthenticated RCE CVE (CVSS 10.0) in `chromadb`'s unused FastAPI server component (mitigated by embedded-client-only usage but the vulnerable code still ships); `bleach` (sole HTML sanitizer) at permanent end-of-life; `PyMuPDF` AGPL-3.0 licensing exposure if the web UI is ever exposed beyond personal/local use or the project distributed closed-source. Plus the `requirements.txt` vs. `pyproject.toml`/`uv.lock` divergence noted above.

**Cost/Observability**: LiteLLM-backed cost estimation with no local price table (upgrade LiteLLM to refresh prices); `CostTracker` aggregates per run/source via contextvars; every pipeline command starts/finishes a `runs` row with cost totals; SQLite `llm_cache` hits are free; Ollama/unknown models log tokens at $0.

## Guidance for Phase 2

Suggested analysis order given coupling/risk concentration: start with **INFRA** (config.py/state.py are the two highest-blast-radius modules), then **RETRIEVAL** (already has one fixed production bug and four downstream consumers), then **CONNECT** (documented open prompt-injection risk), then the remaining pipeline phases (**HARVEST**, **EXTRACT**, **REVIEW**, **GARDEN**), then **WEB**, **QA-WRITING**, **MANUAL-SYNC**, **CLI**, **LLM**, **ASSETS**, **PROMPTS-CFG**. **TESTS** is unlikely to yield ADRs directly but its gaps (no `cli.py` coverage, no `conftest.py`) are useful context when scoring decisions in modules it doesn't cover.
