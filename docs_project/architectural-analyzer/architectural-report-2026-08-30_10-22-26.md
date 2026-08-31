# Architectural Analysis Report

**Project:** zettel_app
**Analysis date:** 2026-08-30
**Scope:** entire project root (`D:/projetos/zettel_app`)
**Excluded from analysis:** `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, `.pytest_cache`

---

## 1. Executive Summary

`zettel_app` is a single-codebase Python 3.12 system that implements a linear content pipeline (`harvest -> extract -> review -> connect -> garden`) converting PDF/Markdown source documents into an Obsidian-compatible Zettelkasten vault, augmented by a hybrid retrieval/GraphRAG subsystem (`ask`, `article`) and a server-rendered FastAPI web UI that exposes a subset of the same pipeline as background jobs.

The system is organized as one Python package (`zettel/`) of 41 modules (~20,000 lines) fronted by a Typer CLI (`cli.py`, 1934 lines, 24 subcommands) and a FastAPI app (`web.py` + `web_app.py`). There is no network-service decomposition: persistence, vector search, LLM orchestration, and presentation all run in a single OS process against local files (SQLite `state.db`, an embedded ChromaDB store, and an Obsidian vault directory tree). This is a **layered, modular-monolith architecture** with a clear feature-pipeline core, a thin CLI/web presentation layer, and a set of shared infrastructure modules (`config`, `state`, `index`, `vault`, `hashing`, `schemas`, `llm`, `usage`, `pricing`, `progress`) that nearly every pipeline module depends on.

Key findings:
- **`config.py` and `state.py` are structural hubs** — 25 and 22 of the other 38 internal modules import them respectively — making them the highest-risk single points of failure in the codebase from a blast-radius standpoint (see Section 6).
- **`cli.py` is the highest-efferent-coupling module** (imports 21 other internal modules), consistent with its role as the sole command orchestrator, but this concentrates a large amount of wiring logic in one file.
- Persistence is split across two independently-managed stores (SQLite for relational/state/FTS5/graph data, ChromaDB for vectors) with no cross-store transaction; consistency is maintained by application discipline (`web_app.py`'s comment: `_idx_kwargs` must mirror `cli._idx_kwargs`), which is itself a documented architectural debt/coupling risk.
- LLM/embedding providers are pluggable (OpenAI, Anthropic, Gemini, Ollama, OpenAI-compatible gateways) behind `llm.py` and `index.py`, giving good external-integration flexibility, but the active `config/config.yaml` pins the system to a local Ollama embedding model plus CUDA, which is an environment-specific hard dependency.
- The web layer has deliberate, currently-sound security controls (HMAC-signed, HttpOnly/SameSite/Secure session cookie; per-session CSRF tokens compared with `hmac.compare_digest`; strict upload filename/extension/path-traversal validation; `bleach`-based Markdown sanitization) — see Section 8.
- Concurrency is intentionally serialized: a single Uvicorn worker processes at most one mutating job at a time via a SQLite-backed job queue, which is a scalability boundary by design, not an oversight.

## 2. System Overview

```
zettel_app/
├── zettel/                      # Main Python package (pipeline + web + shared infra)
│   ├── cli.py                   # Typer CLI — orchestrates every pipeline command
│   ├── __main__.py              # `python -m zettel` entry point -> cli.main()
│   ├── harvester.py             # Phase 1: ingest, extract, chunk, dedupe
│   ├── paging.py                # Phase 1 helper: content-start page resolution
│   ├── extractor.py             # Phase 2: LLM literature-note drafting
│   ├── review.py                # Phase 2b: HITL approval of drafts
│   ├── connector.py             # Phase 3: RAG-based permanent-note generation
│   ├── gardener.py              # Phase 4: taxonomy-driven MOC clustering
│   ├── gardener_assign.py       # Phase 4 helper: category assignment
│   ├── gardener_hub.py          # Phase 4b: hub-anchored MOC generation
│   ├── graph.py                 # BFS graph expansion over note_connections
│   ├── retrieval.py             # Hybrid retriever (dense + BM25 + RRF + floor)
│   ├── ask.py                   # `zettel ask` — RAG Q&A over the vault
│   ├── article.py               # `zettel article` — long-form writing pipeline
│   ├── article_graph.py         # LangGraph StateGraph backing `article.py`
│   ├── sync.py                  # Manual-note ingestion + graph backfill
│   ├── new_note.py              # Manual note scaffolding (no DB/index writes)
│   ├── purge_source.py          # Irreversible full-source deletion
│   ├── rebuild.py               # Reindex/rebuild utilities
│   ├── moc_backrefs.py          # Maintains auto-moc-backrefs managed blocks
│   ├── taxonomy.py              # Category/topic taxonomy loading
│   ├── assets.py                # Image extraction + multimodal description
│   ├── bibliography.py          # ABNT bibliographic metadata heuristics + LLM
│   ├── chunk_dump.py            # Debug export of persisted chunks
│   ├── extraction_dump.py       # Debug export of extracted source text
│   ├── config.py                # AppConfig (Pydantic) + load_config()
│   ├── state.py                 # StateDB — SQLite schema/access (WAL, FTS5)
│   ├── index.py                 # VectorIndex — ChromaDB wrapper (5 collections)
│   ├── vault.py                 # Obsidian file I/O, frontmatter, managed blocks
│   ├── hashing.py                # Canonical text normalization + checksums
│   ├── schemas.py               # Pydantic models (LLM structured I/O, domain)
│   ├── llm.py                   # Provider-agnostic LLM client helpers
│   ├── pricing.py               # LiteLLM-based cost-per-token calculator
│   ├── usage.py                 # CostTracker (contextvars-based aggregation)
│   ├── progress.py              # ProgressObserver protocol
│   ├── markdown.py              # Markdown -> sanitized HTML (bleach) for web UI
│   ├── web.py                   # FastAPI routes, auth, templates, uploads
│   ├── web_app.py               # WebApplication — job queue + worker thread
│   ├── templates/                # 14 Jinja2 templates (dashboard, review, jobs, ...)
│   └── static/                   # app.css, markdown.css, mobile.css
├── prompts/                      # 18 LLM prompt templates (system/user split)
├── config/                       # config.yaml, moc_topics.yaml, personalities.yaml
├── tests/                        # 37 pytest modules (one per architectural concern)
├── scripts/post-merge.sh         # `upm install` post-merge hook (Replit)
├── .replit                       # Replit workflow + VM deployment descriptor
├── pyproject.toml / requirements.txt / uv.lock   # Dependency management (uv + pip)
├── main.py                       # Vestigial placeholder entry point (unused by CLI)
└── cuda-test.py                  # Standalone CUDA availability diagnostic script
```

**Architectural patterns identified:**
1. **Pipeline / staged-processing architecture** — the dominant pattern: `harvest -> extract -> review -> connect -> garden`, each phase reading/writing well-defined status fields in `StateDB` (`pending -> awaiting_review -> approved`), so phases are decoupled through persistent state rather than direct calls.
2. **Layered architecture** within each phase: presentation (CLI commands / web routes) -> domain/pipeline module -> shared infrastructure (`state.py`, `index.py`, `vault.py`).
3. **Repository-ish data-access pattern**: `StateDB` and `VectorIndex` act as the sole gateways to SQLite and ChromaDB respectively; no other module touches these stores directly.
4. **Job-queue / worker pattern** in the web layer: `web_app.py` runs a daemon worker thread consuming a SQLite-backed queue (`web_jobs`/`web_job_events`), decoupling HTTP request/response from long-running pipeline execution.
5. **Strategy pattern** for LLM/embedding providers: `llm.get_llm()` and `index.py`'s embedding-function selection switch on `config.llm.provider` / `config.embedding.provider` to instantiate different LangChain clients behind a common interface.
6. **Graph-augmented retrieval (GraphRAG-lite)**: `retrieval.py` + `graph.py` combine RRF-fused hybrid search with a typed-edge BFS expansion, a hybrid of classic IR and graph-based retrieval.

## 3. Critical Components Analysis

Afferent coupling (Ca) below is the count of other internal `zettel/*` modules that import a given module; efferent coupling (Ce) is the count of distinct internal `zettel/*` modules that module itself imports. Both were derived by statically extracting `from zettel.X import ...` / `from .X import ...` statements from every file in `zettel/` and building a directed module-dependency graph, then counting in-degree (Ca) and out-degree (Ce) per node. High Ca marks a module many others depend on (a change ripples outward, and it is a stability/SPOF concern); high Ce marks a module that depends on much of the rest of the system (an orchestrator, likely to be affected by changes anywhere).

| Component | Type | Location | Afferent Coupling | Efferent Coupling | Architectural Role |
|-----------|------|----------|-------------------|--------------------|---------------------|
| config | Shared Infrastructure / Configuration | zettel/config.py | 25 | 0 | Pydantic `AppConfig` schema + `load_config()`; single source of runtime settings for every other module |
| state | Data Access (SQLite) | zettel/state.py | 22 | 1 | `StateDB` — SQLite schema, WAL, FTS5, graph edges, job queue, cost totals; sole relational persistence gateway |
| index | Data Access (Vector Store) | zettel/index.py | 17 | 4 | `VectorIndex` — ChromaDB wrapper, 5 collections, pluggable embedding providers |
| vault | Data Access (Filesystem / Obsidian) | zettel/vault.py | 16 | 0 | Obsidian I/O: frontmatter, managed blocks, safe writes, note builders |
| usage | Shared Infrastructure | zettel/usage.py | 14 | 0 | `CostTracker` — contextvars-based LLM/embedding cost aggregation |
| hashing | Shared Infrastructure | zettel/hashing.py | 13 | 0 | Canonical text normalization + layered checksums (file/extraction/chunk/llm_call) |
| llm | Integration Gateway | zettel/llm.py | 10 | 2 | Provider-agnostic LLM client (`get_llm`/`call_llm`/prompt loading) |
| schemas | Shared Infrastructure | zettel/schemas.py | 9 | 0 | Pydantic v2 models for domain objects and LLM structured outputs |
| progress | Shared Infrastructure | zettel/progress.py | 6 | 0 | `ProgressObserver` protocol shared by CLI (Rich) and web (`JobProgress`) |
| cli | Presentation / Orchestration | zettel/cli.py | 1 | 21 | Typer CLI; sole orchestrator wiring `(AppConfig, StateDB, VectorIndex)` into every pipeline command |
| gardener | Pipeline (Phase 4) | zettel/gardener.py | 5 | 12 | Taxonomy-driven MOC clustering (UMAP+HDBSCAN/KMeans), one-LLM-call-per-cluster routing |
| harvester | Pipeline (Phase 1) | zettel/harvester.py | 4 | 13 | Ingestion, text extraction (Docling/native), chunking, three-layer duplicate detection |
| review | Pipeline (Phase 2b) | zettel/review.py | 4 | 9 | Selective HITL approval of literature-note drafts; promotes concepts to `approved` |
| retrieval | Cross-cutting / GraphRAG | zettel/retrieval.py | 5 | 3 | `Retriever` — hybrid dense+BM25 search fused via RRF, relevance-floor gating |
| taxonomy | Shared Infrastructure | zettel/taxonomy.py | 4 | 0 | Loads/validates the pillar > category > topic taxonomy (`moc_topics.yaml`) |
| extractor | Pipeline (Phase 2) | zettel/extractor.py | 3 | 12 | LLM Prompt-1 processing of chunks into literature-note drafts |
| assets | Pipeline support | zettel/assets.py | 3 | 7 | Image extraction (Docling/Markdown) and multimodal LLM description |
| chunk_dump | Debug / Export utility | zettel/chunk_dump.py | 3 | 3 | Exports persisted chunk text + paging metadata to Markdown |
| moc_backrefs | Pipeline support | zettel/moc_backrefs.py | 3 | 3 | Maintains `auto-moc-backrefs` managed blocks on linked permanent notes |
| pricing | Integration Gateway | zettel/pricing.py | 3 | 0 | LiteLLM `cost_per_token` wrapper (price calculator only, not an LLM client) |
| article_graph | Pipeline (article) | zettel/article_graph.py | 2 | 6 | LangGraph `StateGraph` implementing the `article` long-form writing flow |
| bibliography | Pipeline support | zettel/bibliography.py | 2 | 5 | ABNT bibliographic metadata heuristics + LLM enrichment |
| connector | Pipeline (Phase 3) | zettel/connector.py | 2 | 11 | RAG-based permanent (ZTL) note generation from approved concepts |
| extraction_dump | Debug / Export utility | zettel/extraction_dump.py | 2 | 4 | Exports raw extracted source text (Docling/native Markdown) |
| gardener_assign | Pipeline (Phase 4) | zettel/gardener_assign.py | 2 | 4 | Embeds category labels and assigns notes to taxonomy buckets |
| gardener_hub | Pipeline (Phase 4b) | zettel/gardener_hub.py | 2 | 13 | Hub-anchored complementary MOC generation via graph-degree ranking + BFS |
| paging | Pipeline (Phase 1) | zettel/paging.py | 2 | 1 | Content-start page resolution (HITL or CLI flags) |
| rebuild | Maintenance / CLI support | zettel/rebuild.py | 2 | 6 | Reindex/rebuild utilities (`zettel reindex`, FTS rebuild) |
| sync | Pipeline (manual ingestion) | zettel/sync.py | 2 | 10 | Scans vault for manual notes, indexes them, extracts body-link graph edges |
| article | Pipeline (long-form writing) | zettel/article.py | 1 | 11 | `zettel article` orchestration entry point (wraps `article_graph`) |
| ask | Pipeline (Q&A) | zettel/ask.py | 1 | 8 | `zettel ask` — retrieval-grounded Q&A with deterministic "no evidence" fallback |
| graph | Cross-cutting / GraphRAG | zettel/graph.py | 1 | 2 | `expand_notes` — weighted BFS over `note_connections` |
| markdown | Presentation support | zettel/markdown.py | 1 | 0 | Markdown -> sanitized HTML rendering (bleach allowlist) for the web UI |
| new_note | Manual authoring | zettel/new_note.py | 1 | 2 | Scaffolds manual vault notes (`origin: manual`), no DB/index writes |
| purge_source | Maintenance / CLI support | zettel/purge_source.py | 1 | 4 | Irreversible full-source removal (vault + SQLite + Chroma cascade) |
| web_app | Presentation (Web) | zettel/web_app.py | 1 | 12 | `WebApplication` — job queue, worker thread, dispatches pipeline ops |
| __main__ | Entry Point | zettel/__main__.py | 0 | 1 | `python -m zettel` entry point |
| web | Presentation (Web) | zettel/web.py | 0 | 5 | FastAPI routes, Jinja2 rendering, auth/CSRF, upload handling |
| templates/static | Presentation (Web assets) | zettel/templates/, zettel/static/ | N/A (rendered by web.py) | N/A | 14 Jinja2 templates + 3 stylesheets; server-rendered UI, no JS build step |
| prompts | LLM Integration Content | prompts/*.md (18 files) | N/A (loaded by llm.py) | N/A | System/user-split prompt templates driving every LLM-backed phase |
| config files | Configuration | config/config.yaml, config/moc_topics.yaml, config/personalities.yaml | N/A | N/A | Operational configuration source of truth (per CLAUDE.md) |
| tests | Test Suite | tests/*.py (37 files) | 0 | ~33 (imports most modules) | Pytest coverage, one file per architectural concern, mirrors module boundaries |
| main.py | Vestigial entry point | main.py | 0 | 0 | Placeholder `print("Hello from zettel-app!")`; not used by CLI (`zettel/__main__.py` is) |
| cuda-test.py | Diagnostic script | cuda-test.py | 0 | 0 | Standalone CUDA/torch availability check, outside the pipeline |

## 4. Dependency Mapping

```
High-Level Dependencies (staged pipeline):

  harvester -> [assets, bibliography, chunk_dump, extraction_dump, paging, review*]
     |            (*harvester imports review for status constants, not the reverse)
     v
  state.db (chunks: pending) + Chroma "chunks" collection
     |
     v
  extractor -> [review, assets] -> LIT drafts (00_Inbox/Review) -> state.db (chunks: awaiting_review)
     |
     v
  review -> extractor (types) -> state.db (chunks: approved) + Chroma "literature_notes"
     |
     v
  connector -> [assets, retrieval, index] (RAG) -> ZTL notes -> state.db (notes) + Chroma
     |
     v
  gardener / gardener_assign / gardener_hub -> [taxonomy, moc_backrefs, graph] -> MOC notes

Cross-cutting (not staged, invoked independently):
  sync        -> [harvester, gardener, rebuild, retrieval, moc_backrefs]  (manual vault ingestion + graph backfill)
  ask         -> [retrieval, graph(via retrieval)]                        (Q&A)
  article     -> article_graph -> [retrieval, bibliography, index]        (long-form writing)
  new_note    -> [vault]                                                  (scaffolding only, no DB/index)
  purge_source-> [state, index, vault]                                    (irreversible deletion)
  rebuild     -> [state, index, gardener]                                 (reindex/FTS rebuild)

Everything above -> shared infrastructure:
  {harvester, extractor, review, connector, gardener*, sync, ask, article, ...}
        -> config, state, index, vault, hashing, schemas, llm, usage, progress

  llm -> pricing, usage            (cost accounting wraps every LLM call)
  index -> config, llm, pricing, usage   (embedding upserts also cost-tracked)

Presentation layer:
  cli.py     -> almost every pipeline module (21 internal imports) + config/state/index/vault/schemas
  web.py     -> web_app, harvester, hashing, index, markdown
  web_app.py -> connector, extractor, gardener, gardener_hub, harvester, review, sync + config/state/index/schemas/usage

External:
  {harvester, assets, bibliography}      -> Docling / PyMuPDF (PDF extraction)
  llm.py                                  -> LangChain provider clients (OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible)
  index.py                                -> ChromaDB (embedded) + embedding provider clients
  pricing.py                              -> LiteLLM (price map only)
  state.py                                -> SQLite (file-based, WAL)
  vault.py                                -> local filesystem (Obsidian vault directory tree)
```

Notable dependency-direction facts:
- `state`, `config`, `vault`, `index`, `hashing`, `usage`, `schemas` never import any other pipeline-phase module (Ce = 0 or near-0) — they are the stable base of the dependency graph, which is the correct direction for shared infrastructure.
- `cli.py` and `web_app.py` are the only modules that import across phase boundaries broadly (harvester + extractor + review + connector + gardener*, etc.) — consistent with their role as composition roots. No pipeline-phase module imports another out of sequence (e.g., `harvester` does not import `connector`), indicating the staged pipeline's phase boundaries are respected in the import graph, not just in documentation.
- `sync.py` and `rebuild.py` are the two modules that reach across the most phase boundaries outside the two composition roots (`sync` imports `gardener`, `harvester`, `moc_backrefs`, `rebuild`, `retrieval`; `rebuild` imports `gardener`), reflecting their role as cross-cutting maintenance/backfill utilities rather than pipeline stages.

## 5. Integration Points

| Integration | Type | Location | Purpose | Risk Level |
|--------------|------|----------|---------|------------|
| OpenAI API | External LLM/Embedding API | zettel/llm.py, zettel/index.py, config/config.yaml (`llm.provider`) | Default LLM provider (`gpt-4o-mini`); optional embedding provider | High |
| Anthropic API | External LLM API | zettel/llm.py (`provider: anthropic`), prompt-cache hints via `apply_prompt_cache_hints` | Alternate LLM provider, optional | Medium |
| Google Gemini API | External LLM API | zettel/llm.py (`ChatGoogleGenerativeAI`, `provider: gemini`) | Alternate LLM provider, optional | Medium |
| Ollama (local) | Local LLM/Embedding server | zettel/llm.py, zettel/index.py; config/config.yaml currently active (`embedding.provider: ollama`, model `qwen3-embedding`) | Local, no-cost LLM/embedding inference; currently the configured embedding provider | Medium |
| OpenRouter / OpenCode / OpenAI-compatible gateways | External LLM API | zettel/llm.py (`base_url` + provider aliases) | Alternate gateway routing for LLM calls | Medium |
| ChromaDB (embedded) | Vector database | zettel/index.py, path `data/chroma` (config `chroma_path`) | 5 collections (sources, chunks, permanent_notes, mocs, literature_notes); dense retrieval backend | High |
| SQLite (`state.db`) | Relational database + FTS5 + job queue | zettel/state.py, path `data/state.db` | Sole relational persistence: files/sources/chapters/chunks/concepts/notes/mocs/assets/llm_cache/note_connections/runs/web_jobs/web_job_events + FTS5 virtual tables | High |
| Docling | External PDF-extraction library | zettel/harvester.py, zettel/assets.py, config `pdf_extractor: docling` | Primary PDF text/structure/image extraction engine | Medium |
| PyMuPDF | PDF library (fallback + page mapping) | zettel/harvester.py, zettel/paging.py, config `pdf_extractor: pymupdf` (fallback) | Fallback PDF extractor; preferred source for page-number mapping | Low |
| LiteLLM | Pricing/cost library (not an LLM client) | zettel/pricing.py | `cost_per_token` price-map lookups for cost estimation only | Low |
| Obsidian vault (filesystem) | File-based integration | zettel/vault.py, config `vault_path` | All generated/manual notes are plain Markdown files read directly by the Obsidian desktop app; no API, pure filesystem contract (frontmatter + managed HTML-comment blocks) | Medium |
| FastAPI / Uvicorn | Web application server | zettel/web.py, `.replit` deployment run command | Serves the web UI; single-worker process per `.replit` VM deployment | Medium |
| Replit platform | Deployment/hosting environment | .replit, scripts/post-merge.sh | Nix-based dev/runtime environment, VM deployment target, `upm install` post-merge hook | Low |
| torch / torchvision (CUDA) | ML runtime dependency | pyproject.toml (`sys_platform == 'win32' or 'linux'`), config `device: cuda` | Backing runtime for Docling models and/or local embedding inference | Medium |
| python-dotenv | Local secrets loading | project-wide (`.env`) | Loads `OPENAI_API_KEY`, `SESSION_SECRET`, etc. from `.env`, not committed | Low |

## 6. Architectural Risks & Single Points of Failure

| Risk Level | Component | Issue | Impact | Details |
|------------|-----------|-------|--------|---------|
| Critical | state.py (`StateDB`) | Single relational store, single point of coordination | System-wide | 22 of 38 internal modules depend on it directly; it also hosts the web job queue, FTS5 index, and the graph (`note_connections`) — a schema regression or file corruption stalls every pipeline phase and the entire web UI simultaneously. |
| Critical | config.py (`AppConfig`) | Highest afferent coupling in the codebase (25 dependents) | System-wide | Any schema-loading regression or misconfiguration in `config/config.yaml` prevents every CLI command and the web app from starting, since `_load_deps()` is the universal entry point per CLAUDE.md. |
| High | ChromaDB + SQLite dual persistence | No cross-store transaction; consistency relies on application-level discipline | Data integrity | CLAUDE.md itself documents this as a known coupling concern (`web_app.py`'s `_idx_kwargs` "must mirror" `cli._idx_kwargs`) — a missed parameter (e.g., `embedding.dimensions`) between the two composition roots silently produces vector/keyspace mismatches rather than a hard failure. |
| High | Embedding provider/model coupling | Changing `embedding.provider`/`model`/`dimensions` invalidates all existing vectors | Data integrity / availability | Documented in config.yaml and CLAUDE.md: requires a full `zettel reindex --force` and recalibration of `retrieval.relevance_floor` / dedupe thresholds; there is no automatic migration path. |
| High | Single Uvicorn worker + serialized job queue | At most one mutating job (`queued`/`running`) processed at a time; second submit returns 409 | Throughput / scalability | Documented as an intentional design (CLAUDE.md), but it is a hard ceiling on concurrent multi-user throughput for the web UI; a long-running `garden` or `harvest` job blocks all other mutating operations for every user. |
| High | Environment/hardware coupling (`device: cuda`, torch/torchvision) | Docling extraction and/or local embedding path assumes a CUDA-capable host | Portability / availability | `pyproject.toml` restricts torch/torchvision to `win32`/`linux` (no macOS wheel path declared); `config.yaml` hardcodes `device: cuda`, so deployment to a non-CUDA host requires manual config edits to avoid failure or silent CPU fallback behavior differences. |
| Medium | cli.py | Very large (1934 lines), highest efferent coupling (21 internal modules) | Maintainability | Acts as the single composition root for the entire pipeline; any refactor of a pipeline module's public function signature has a high chance of requiring a matching change in this one file. |
| Medium | harvester.py, state.py | Largest files in the codebase (1894 and 1725 lines respectively) | Maintainability | Size concentrates multiple responsibilities (extraction, chunking, paging inference, three-layer dedupe in `harvester.py`; full schema + FTS5 + job queue + graph queries in `state.py`) in single modules, which raises the cost of localized changes and code review. |
| Medium | vault.py | High afferent coupling (16) combined with direct filesystem writes | Data integrity | It is the only writer of Obsidian notes; a bug in managed-block handling or frontmatter rendering can corrupt manually-edited vault content across every note type (SRC/LIT/ZTL/MOC), and the "never overwrite manual edits outside managed blocks" guarantee is enforced only by code discipline within this one file, not by an independent safety layer. |
| Medium | SQLite as the web job queue backend | `web_jobs`/`web_job_events` share the same SQLite file as all pipeline data | Availability | Under concurrent read/write load (WAL mode mitigates but does not eliminate contention), long-running pipeline transactions and job-queue polling compete for the same database file; there is no separate queue technology (e.g., Redis) to isolate this concern. |
| Low | main.py | Vestigial/dead entry point (`print("Hello from zettel-app!")`) not wired to the actual CLI (`zettel/__main__.py`) | Developer confusion | Present at the project root alongside the real entry point; could mislead a new contributor about how to run the application. |

## 7. Technology Stack Assessment

- **Language/runtime:** Python 3.12 (`requires-python = ">=3.12"`), managed with `uv` (per CLAUDE.md conventions) with a parallel `requirements.txt` kept for pip-based installs; `uv.lock` present.
- **CLI framework:** Typer (`typer>=0.21.1`, `typer-slim`) — 24 `@app.command()` entries in `cli.py`.
- **Web framework:** FastAPI (`>=0.115.0`) + Uvicorn (`>=0.30.0`), server-rendered with Jinja2 templates (no SPA/JS build pipeline); `python-multipart` for uploads.
- **Data validation / schemas:** Pydantic v2 (`>=2.12.5`) throughout (`config.py` `AppConfig`, `schemas.py` domain and LLM structured-output models).
- **LLM orchestration:** LangChain core/text-splitters/openai/ollama (`langchain-core`, `langchain-openai`, `langchain-ollama`, `langchain-chroma`), LangGraph (`>=0.2.0`) specifically for the `article` command's `StateGraph`.
- **Cost estimation:** LiteLLM (`>=1.95.0`) used exclusively as a static price-map lookup (`cost_per_token`), not as an LLM client — an explicit, documented architectural choice.
- **Vector store:** ChromaDB (`==1.5.9`, pinned), embedded/local mode, 5 named collections.
- **Relational store / search:** Python's built-in `sqlite3` via `state.py` (not visible as a pyproject dependency since it's stdlib), WAL journal mode, FTS5 virtual tables (`unicode61 remove_diacritics`) for BM25-style lexical search.
- **PDF/document processing:** Docling (`>=2.71.0`) as primary extractor, PyMuPDF (`>=1.24.0`) as fallback/page-mapping source.
- **Clustering (MOC generation):** scikit-learn, UMAP (`umap-learn==0.5.12`), HDBSCAN (`==0.8.44`), pinned exact versions (higher pinning risk than the `>=` pattern used elsewhere).
- **ML runtime:** torch/torchvision (`>=2.10.0`/`>=0.25.0`), platform-gated to win32/linux, sourced from a custom PyTorch CUDA 12.6 wheel index (`pytorch-cu126`) via `[tool.uv.sources]`.
- **Markdown handling:** `markdown-it-py` + `linkify-it-py` for parsing, `bleach` for HTML sanitization (web note rendering).
- **IDs/hashing:** `python-ulid` for ULID-based note/MOC identifiers; stdlib `hashlib` via `hashing.py` for content checksums.
- **Testing:** pytest (`>=9.0.2`), 37 test modules under `tests/`, one per architectural concern (mirrors the module boundaries closely).
- **Linting:** `ruff` declared as a project dependency (not confirmed here whether wired into CI, since no CI config directory was found in scope).
- **Deployment target observed:** Replit VM deployment (`.replit`), Nix-based system packages (freetype, mupdf, openjpeg, etc. — consistent with Docling/PyMuPDF's native dependencies), single-port (5000) web workflow.

## 8. Security Architecture and Risks

**Authentication and session management** (`zettel/web.py`):
- The web UI uses a single shared **instance secret** (`SESSION_SECRET`, read only from the process environment, never from `config.yaml`) compared with `hmac.compare_digest` — a timing-safe comparison, and a fail-closed design: if `SESSION_SECRET` is unset, `_session()` always returns `None` and the login page reports it explicitly rather than silently accepting any credential.
- Session state is a **self-contained signed cookie** (`zettel_session`): a base64url JSON payload (`{csrf, created}`) plus an HMAC-SHA256 signature over that payload, verified with `hmac.compare_digest` before trusting it. Sessions expire after 86400 seconds (24h), checked server-side on every request. The cookie itself is set with `httponly=True`, `samesite="lax"`, and `secure=` conditional on the `X-Forwarded-Proto` header being `https` — appropriate for a reverse-proxied deployment (e.g., Replit's VM target), though this makes the `secure` flag dependent on the proxy setting that header correctly.
- **CSRF protection** is applied uniformly: every mutating route (`upload`, `_post_job`, `harvest`, `documents_run_all`, `pipeline_action`, `review_action`, `logout`) calls `_csrf_ok()`, which requires a valid session **and** a token matching the session's embedded CSRF value via `hmac.compare_digest`. The login form itself carries a separate CSRF-like token (`login_csrf`, signed with a fixed `"login"` payload) checked before the instance secret is even compared, mitigating CSRF against the login endpoint itself.
- No login rate-limiting or lockout mechanism was found; because the secret comparison is timing-safe and the secret is presumably high-entropy (operator-supplied), this is a lower-severity gap than a typical password endpoint, but repeated guesses are not throttled at the application layer.

**File upload handling** (`zettel/web.py`, `upload()`):
- Filenames are validated against an extension allowlist (`.pdf`, `.md`, `.markdown`, `.txt`), a strict regex (`[\w .()\-]+`), a length cap (180 chars), and explicit rejection of any path separator or a name that differs from `Path(name).name` (this blocks `../` traversal attempts and null-byte/segment tricks at the filename level).
- The destination path is resolved and checked with `destination.relative_to(cfg.inbox_path.resolve())`, providing a second, path-based traversal guard independent of the filename regex — defense in depth.
- Upload size is capped at `MAX_UPLOAD_BYTES = 25 MB`, read with an explicit `+1` byte over-read to reliably detect oversized uploads without buffering unbounded data.

**Output encoding:**
- Rendered Markdown (note bodies shown in the web UI) is sanitized through `zettel/markdown.py` using `bleach.clean()` with an explicit allowed-tags set, mitigating stored-XSS risk from LLM-generated or manually-authored note content that reaches the browser.

**Secrets management:**
- API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) and `SESSION_SECRET` are loaded from `.env` (git-ignored, confirmed absent from tracked files) via `python-dotenv`, not from the checked-in `config/config.yaml` — a sound separation of secrets from versioned configuration, consistent with CLAUDE.md's stated convention.
- `.env.example` provides placeholders only; no real secret material is present in the repository based on the files reviewed.

**Data-flow/trust-boundary observations (architectural, not code-level findings):**
- The pipeline routinely feeds **untrusted external content** (arbitrary PDFs/Markdown dropped into `data/inbox/`) into LLM prompts (`extractor.py`, `bibliography.py`, `assets.py` image descriptions) whose structured outputs are then written directly to the Obsidian vault and to SQLite/Chroma. Pydantic schema validation (`schemas.py`) constrains the *shape* of LLM output, but does not constrain its *content* — a maliciously crafted source document could in principle attempt prompt injection to influence generated note content (not code execution, since no `eval`/`exec`/`subprocess`/`pickle`/`shell=True` usage was found anywhere in `zettel/`, which is a positive finding).
- The web UI intentionally does not expose the full CLI surface (`new-note`, `delete-source`, `purge-rejected`, `reindex`, `garden --recreate`, `init --reset`, etc. are CLI-only per CLAUDE.md) — this reduces the web attack surface for irreversible/destructive operations, though it also means an authenticated web session cannot trigger those operations, which is a deliberate trade-off rather than a gap.
- No transport-layer configuration (TLS termination) was found in-repo; this is expected to be handled by the hosting platform (Replit) in front of the single Uvicorn process, per the `secure=` cookie logic keying off `X-Forwarded-Proto`.

## 9. Infrastructure Analysis

- **Deployment target:** Replit VM (`.replit`, `deploymentTarget = "vm"`), running `uvicorn zettel.web:app --host 0.0.0.0 --port 5000`, with port 5000 mapped to external port 80.
- **Runtime provisioning:** Nix channel `stable-25_05` with native packages required by the PDF/image stack (`freetype`, `glibcLocales`, `gumbo`, `harfbuzz`, `jbig2dec`, `libjpeg_turbo`, `libxcrypt`, `libyaml`, `mupdf`, `openjpeg`, `swig`, `xcbuild`) — these correspond to Docling/PyMuPDF's native dependencies for font rendering, JPEG2000/JBIG2 decoding, and PDF parsing.
- **Process model:** a single `workflows.workflow` named `Project` (mode `parallel`) launches one task, `Start application`, which runs the Uvicorn command directly via `shell.exec` — no process manager, health check, or restart policy is declared beyond what Replit's platform provides implicitly.
- **Post-merge automation:** `scripts/post-merge.sh` runs `upm install` (Replit's universal package manager) on merge, keeping the Nix/language-specific dependency set in sync with the repository state.
- **No containerization** (no `Dockerfile`/`docker-compose.yml`) and **no Kubernetes manifests** were found in scope; the Replit VM target is the only deployment descriptor present.
- **No CI/CD pipeline configuration** (e.g., `.github/workflows/`) was found within the analyzed scope; test execution and linting (`pytest`, `ruff`) appear to be developer/agent-invoked per CLAUDE.md's documented commands rather than automated on push, based on the files available for analysis.
- **Persistence is entirely local-disk-based** — SQLite file, ChromaDB embedded store, and the Obsidian vault directory all live under the same filesystem as the application process, with no external managed database or object storage integration observed. This centralizes all state with the single VM instance, which reinforces the single-point-of-failure characteristics noted in Section 6.

---

**Component inventory produced for this report:** config, state, index, vault, usage, hashing, llm, schemas, progress, cli, gardener, harvester, review, retrieval, taxonomy, extractor, assets, chunk_dump, moc_backrefs, pricing, article_graph, bibliography, connector, extraction_dump, gardener_assign, gardener_hub, paging, rebuild, sync, article, ask, graph, markdown, new_note, purge_source, web_app, __main__, web, templates/static, prompts, config files, tests, main.py, cuda-test.py.
