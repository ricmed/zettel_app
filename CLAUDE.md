# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

```bash
# Run the CLI (all commands go through this)
.venv/Scripts/python.exe -m zettel <command>

# Run all tests
.venv/Scripts/python.exe -m pytest tests/ -v

# Run a single test file
.venv/Scripts/python.exe -m pytest tests/test_hashing.py -v

# Run a single test function
.venv/Scripts/python.exe -m pytest tests/test_hashing.py::test_normalize_collapses_whitespace -v

# Check dependencies and config integrity
.venv/Scripts/python.exe -m zettel doctor
```

Python 3.12, venv at `.venv/`, dependencies managed via `requirements.txt`. Environment variables loaded from `.env` (python-dotenv) — not system env vars.

## Architecture

Pipeline that converts PDF/Markdown files into Obsidian-compatible Zettelkasten notes:

```
harvest → extract → review → connect → garden
```

**Phase 1 (harvester.py + paging.py)**: Scans `data/inbox/`, extracts text (Docling for PDF, native for MD), generates citekeys, creates SRC + literature **index** notes. Resolves content-start paging (HITL or `--content-start-file` / `--content-start-book`): PDF page where content begins + printed number on that page; earlier pages produce no chunks. Chunks text via structural H3–H6 + LangChain splitter. Infers `page_in_file` (PyMuPDF map preferred; regex only if no map) and `page_in_book = file - start_file + start_book` (multi-page chunks use the **first** page). Runs three-layer duplicate detection. Indexes raw chunks into Chroma `chunks` (for dedupe/FTS); does **not** create per-chunk LIT files yet. `zettel set-paging` repairs paging on an existing source without re-calling the LLM.

**Phase 2 (extractor.py)**: Processes each `pending` chunk through LLM Prompt 1 (`prompts/literature_note.md`), writes a **draft** LIT note under `00_Inbox/Review/@Citekey/`, checkpoints chunk status to `awaiting_review` after every chunk. Concepts stay `awaiting_review`. Optional `--auto-approve` for high `review_confidence`.

**Phase 2b (review.py)**: Selective approval. Approved drafts move to `20_Literature/@Citekey/chunk_NNNN.md`, are embedded into Chroma collection **`literature_notes`**, and concepts are deduped then marked `approved` for connect. Rejected drafts are deleted and never indexed as literature notes.

**Phase 3 (connector.py)**: Takes `approved` concepts, uses RAG (hybrid `Retriever`) for context, calls Prompt 2, writes ZTL notes. `literature_ref` points at the **granular** approved LIT for that chunk (fallback: source index).

**Phase 4 (gardener.py + gardener_assign.py)**: Hybrid MOC pipeline — embeds category labels from `gardener.topics_path`, assigns notes to taxonomy buckets, clusters with UMAP+HDBSCAN (or KMeans) **within each category**, scores graph cohesion via `note_connections`, then routes each cluster through `_process_cluster` with at most **one LLM call** (incremental preferred when note overlap ≥ `overlap_threshold` or category already has a MOC; otherwise `moc_generation` with `suggested_category` hint). New MOC topics validated against **categorias** (substring match; rejected if `strict_topics: true`). `zettel garden --recreate` purges pipeline MOCs and regenerates. HDBSCAN noise stays out of MOCs; notes remain navigable via graph edges.

**sync.py**: Not part of the linear pipeline — scans `10_Sources/`, `20_Literature/` (including citekey subfolders), `30_Permanent/`, `40_MOCs/` for manually-created or hand-edited notes (via `zettel sync-manual`), assigns IDs/checksums if missing, indexes them into ChromaDB/StateDB, and writes suggested connections into an `auto-connections` managed block. It also closes the graph loop: `_extract_body_edges` persists `[[wikilinks]]` found in a note's body (outside the auto-generated managed blocks) as `related` edges in `note_connections`, never downgrading an already-typed edge. `zettel sync-manual --rebuild-graph` (`rebuild_manual_edges`) backfills these edges for the whole vault from bodies already in SQLite.

### Hybrid retrieval + GraphRAG (retrieval.py, graph.py, ask.py, article.py)

- **retrieval.py (`Retriever`)**: single composition point for note/chunk lookup. Fuses ChromaDB dense search with SQLite FTS5 BM25 via **Reciprocal Rank Fusion** (`retrieval.rrf_k`), then applies an absolute relevance floor, then optionally expands the surviving seeds over the note graph. `search_notes()` returns a `NoteSearchResult(hits, candidates)`: `hits` is what cleared the floor (+ graph neighbours, `hop >= 1`) — what callers should use as evidence; `candidates` is the raw RRF-ranked pool *before* the floor, always populated, so a caller can show "what was closest" even when `hits` is empty. Each `RetrievedNote` carries provenance (`vector_rank`/`bm25_rank`/`hop`/`via`/`passed_floor`). `retrieval.mode` = `hybrid` (default) or `vector` (legacy); degrades to vector-only when `StateDB.fts_enabled` is False. **Consumers that migrated**: connector RAG (`.hits`), sync suggestions (`.hits`), the `ask` command (`.hits` + `.candidates`), the `article` command (`.hits` + catalog). **Deliberately NOT migrated**: extractor dedupe and harvester layer-3 — their thresholds (`dedupe_threshold`/`duplicate_chunk_threshold`) are calibrated on raw L2 distance.
- **Absolute relevance floor (`RelevanceFloorConfig` in config.py)**: RRF's fused score is purely *positional* — dense kNN always returns the N closest notes in the corpus regardless of whether any are actually relevant, so a totally off-topic question gets a similarly "confident" score to a genuinely answerable one. `_apply_relevance_floor` in retrieval.py gates each hit in this order (sets both `passed_floor` and a human-readable `floor_reason`):
  1. `absolute_min_similarity` (default `0.15`) — a hard backstop: if similarity is below this, the hit fails even with bm25 support. Set well below the main floor so it doesn't undermine BM25's main use case (rescuing jargon/acronyms the embedding underrates).
  2. `bm25_hit_bypasses_floor` + `bm25_bypass_max_rank` (default True / `5`) — a bm25 hit ranked within the top `bm25_bypass_max_rank` bypasses the similarity check entirely (a strong lexical match is evidence a kNN "closest available" hit isn't). A *weak* bm25 match (found only deep in the pool) does **not** bypass — it falls through to the similarity check like any other hit. This is the fix for a real bug found in production: before this rank cutoff existed, any bm25 presence bypassed unconditionally, so a note only weakly/incidentally matching a query term (e.g. sharing one common domain word) could pass the floor despite low similarity.
  3. `min_vector_similarity` (default `0.70`, empirically calibrated on this project's corpus/embedding model — retune per corpus) — the default gate for hits without a strong lexical match.
  4. A weak bm25-only hit with no vector data at all fails (insufficient evidence either way).
- **graph.py (`expand_notes`)**: BFS (Python, not SQL CTE) over `note_connections`, undirected, weighted by relation type (`DEFAULT_RELATION_WEIGHTS` in config.py; `contradicts` highest — it's the signal embeddings miss) and hop decay. Seeds keyed by their RRF score via `seed_weights`. One batched query per frontier (`StateDB.get_connections_for_notes`). Only fed seeds that cleared the relevance floor.
- **ask.py (`run_ask`)**: `zettel ask "..."` — retrieves with the `Retriever`, builds a cited context from `.hits`, calls `prompts/ask.md` (PT-BR, answers only from the vault, says "no evidence" rather than hallucinating). When `.hits` is empty (nothing cleared the floor), the LLM is **not called at all** — a deterministic "no evidence" answer is returned, while `AskResult.candidates` (mirrors the Retriever's raw pool) still lets the CLI's `--show-context` table show what was closest, each row carrying `vector_similarity`, `bm25_rank` and `floor_reason` (the exact text explaining the floor verdict, e.g. "match lexical forte (bm25 rank 3 <= 5)"). `AskResult.retrieval_params` snapshots every threshold actually used for the call (mode, topk, rrf_k, the three relevance-floor knobs, graph expansion settings); the CLI renders it as a "Parametros de recuperacao" table when `--show-context` is passed, so the reader can see the exact rules a run was judged against without opening config.yaml. `build_ask_note_body`/`save_ask_note` persist the answer as a `.md` note in `00_Inbox/` with a full-provenance "Fontes consultadas" section (built from `.sources`, i.e. what actually fed the answer, including `floor_reason`). Uses the same deterministic LLM cache as connector (`compute_llm_call_checksum`).
- **article.py / article_graph.py (`run_article`)**: `zettel article "tema" --style blog|academic` — LangGraph StateGraph for long-form writing (not short QA). Nodes: query enricher → incremental hybrid search (merge by note_id) → context HITL → catalog (sources/ABNT/assets) → outline HITL → per-section draft (blog light mentions or academic ABNT; anti-AI prose block) → assemble → personality rewrite (`config/personalities.yaml`; `neutral` skips LLM) → judge loop (`max_judge_iterations`, `judge_min_score`) → verify/save to `00_Inbox/ART - ...md` without Chroma indexing. Reuses `Retriever`, `format_abnt_in_text`, LLM cache. CLI uses LangGraph `interrupt()` + Rich prompts; MemorySaver checkpointer per run.
- **FTS5 in state.py**: `fts_notes`/`fts_chunks` virtual tables (`unicode61 remove_diacritics`), kept in sync inside `upsert_note`/`upsert_chunk` (explicit populate, not triggers). `_fts_match_expr` quotes every token to neutralize FTS5 operators — never interpolate raw user text into a MATCH — and drops high-frequency PT-BR stopwords (`_PT_STOPWORDS`: articles, prepositions, conjunctions, pronouns) before building the OR-joined MATCH expression. Without this, a token like "que" matches nearly every note in the corpus, which both pollutes BM25 ranking generally and silently defeats the relevance floor's bm25-bypass (a "hit" on a stopword isn't real evidence). `rebuild_fts()` is wired into `zettel reindex`.

### Three-layer duplicate detection (harvester.py `_process_file`)

Runs in order, each layer cheaper/more certain than the next, before a file is treated as a new source:

1. **File hash** (`get_file_by_checksum`): identical bytes at a different path → treated as a renamed copy, reuses the existing `source_id`, no reprocessing.
2. **Extraction hash** (`get_source_by_extraction_checksum`): different bytes but identical normalized extracted text (e.g. same paper re-exported PDF vs. Markdown) → reuses the existing source.
3. **Semantic similarity** (`_find_semantic_duplicate_candidates`): samples chunks from the new file, queries ChromaDB for near-duplicate chunks (`harvest.duplicate_chunk_threshold`, default 0.88) belonging to other sources. If candidates are found, `_resolve_duplicate_decision` either prompts interactively (Rich `Prompt`) or applies `harvest.non_interactive_duplicate_action` (`skip`/`continue`/`abort`) — controlled by the `harvest` CLI flags `--yes`, `--skip-duplicates`, `--force`.

Every decision is recorded via `db.record_duplicate(run_id, layer)` and surfaced in `zettel status` / the `harvest` command's summary output.

### Key shared infrastructure

- **state.py (StateDB)**: SQLite with WAL mode. Tables: files, sources, chapters, chunks, concepts, notes, mocs, assets, llm_cache, note_connections, runs, plus FTS5 virtual tables fts_notes/fts_chunks. `runs` and `sources` store estimated LLM/embedding cost and token totals. All pipeline modules receive a `StateDB` instance for incremental processing.
- **index.py (VectorIndex)**: ChromaDB wrapper with 5 collections (sources, chunks, permanent_notes, mocs, **literature_notes**). Embedding provider configurable (OpenAI/SentenceTransformers/Ollama). Literature notes are embedded only after `review` approval. Upserts record estimated embedding usage on the active `CostTracker`.
- **vault.py**: Obsidian I/O — YAML frontmatter parse/render, managed blocks (`<!-- zettel:auto-backlinks:start/end -->`), safe file writes that never overwrite manual edits outside managed blocks. Builders for SRC, literature **index**, and granular literature chunk notes. `sync_source_costs_to_vault` mirrors SQLite cost fields onto SRC frontmatter.
- **pricing.py / usage.py**: LiteLLM `cost_per_token` as price calculator only (not an LLM client); `CostTracker` aggregates per run/source via contextvars. Instrumented from `call_llm` and embedding upserts.
- **hashing.py**: Canonical text normalization (NFKC, whitespace collapse, PDF dehyphenation) before hashing. Layered checksums: file → extraction → chapter → chunk → llm_call → note_semantic. `compute_llm_call_checksum()` enables deterministic LLM response caching.
- **schemas.py**: Pydantic v2 models for all data objects and LLM structured outputs (LiteratureChunkOutput, PermanentNoteLLMOutput, DedupeResult, MOCGenerationOutput, ArticleOutline).

### Data flow between phases

`extract` writes drafts + concepts `awaiting_review`. `review` promotes approved concepts (after dedupe) to `approved`. `connect` loads `get_concepts_by_status("approved", without_notes=True)` from SQLite only. All other inter-phase communication goes through StateDB and ChromaDB. Each CLI command instantiates `(AppConfig, StateDB, VectorIndex)` via `_load_deps()`, `_get_db()`, `_get_idx()` in cli.py.

**Breaking change**: the old monolithic LIT-per-source model is gone. Re-run `extract` + `review` for sources harvested before this change.

### LLM provider pattern

Shared helpers live in `llm.py` (`get_llm` / `call_llm` / `load_prompt_parts` / `fill_template`). Prompt files use `<!-- zettel:user -->` to split stable **system** instructions from per-call **user** payload; `call_llm` sends `SystemMessage` + `HumanMessage` so providers can reuse the prefix (OpenAI/Gemini implicit; Anthropic gets `cache_control` via `apply_prompt_cache_hints`; Ollama/OpenAI-compatible gateways are layout-only). `llm.prompt_cache` toggles hints; `llm.base_url` + aliases (`openrouter`/`opencode`/…) cover OpenAI-compatible APIs; `gemini` uses `ChatGoogleGenerativeAI`. `call_llm` reads `usage_metadata` (incl. provider `prompt_cache_read`/`write` tokens — distinct from SQLite `cache_hits`), estimates USD via LiteLLM's public price map, and records on the active `CostTracker`. SQLite `llm_cache` hits are `$0`. Pipeline commands (`harvest`, `extract`, `connect`, `garden`, `review`, `ask`, `article`) start/finish a `runs` row with cost totals; sources accumulate costs and SRC/ZTL frontmatter store them. Ollama / unknown models log tokens with `$0` cost. Upgrade `litellm` to refresh prices — no local price table to maintain.

## Important Conventions

- All generated content is in **PT-BR** by default (configurable via `config.yaml`).
- Vault note filenames follow pattern: `PREFIX - IDENTIFIER - slug.md` (e.g., `ZTL - 01ARZ3N - titulo-da-nota.md`).
- Vault structure: `00_Inbox/`, `10_Sources/`, `20_Literature/`, `30_Permanent/`, `40_MOCs/`, `90_Assets/`.
- IDs: sources use `@citekey`, chunks use `source_id::chapter_id::short_hash`, notes/mocs use ULID.
- ChromaDB metadata only accepts str/int/float/bool — lists are joined with `", "` via `_sanitize_metadata()`.
- Windows cp1252 console: avoid Unicode arrows/special chars in CLI help strings (causes UnicodeEncodeError).
