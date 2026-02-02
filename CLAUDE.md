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

Four-phase pipeline that converts PDF/Markdown files into Obsidian-compatible Zettelkasten notes:

```
harvest → extract → connect → garden
```

**Phase 1 (harvester.py)**: Scans `data/inbox/`, extracts text (Docling for PDF, native for MD), generates citekeys, creates SRC + LIT vault notes, chunks text via LangChain `RecursiveCharacterTextSplitter`.

**Phase 2 (extractor.py)**: Processes each pending chunk through LLM Prompt 1 (`prompts/literature_note.md`), extracts atomic concept candidates, runs semantic deduplication against existing notes in ChromaDB. Approved candidates cached to `data/cache/candidates.json`.

**Phase 3 (connector.py)**: Takes approved candidates, uses RAG (top-k similar notes from ChromaDB) for context, calls LLM Prompt 2 (`prompts/permanent_note.md`) to generate full permanent notes. Writes ZTL files to vault and updates backlinks via managed blocks.

**Phase 4 (gardener.py)**: Clusters permanent note embeddings (UMAP+HDBSCAN or KMeans fallback), generates MOCs via LLM. Creates MOC files in vault.

### Key shared infrastructure

- **state.py (StateDB)**: SQLite with WAL mode. Tables: files, sources, chapters, chunks, concepts, notes, mocs, llm_cache, runs. All pipeline modules receive a `StateDB` instance for incremental processing.
- **index.py (VectorIndex)**: ChromaDB wrapper with 4 collections (sources, chunks, permanent_notes, mocs). Embedding provider configurable (OpenAI/SentenceTransformers). Falls back to ChromaDB default if API key missing.
- **vault.py**: Obsidian I/O — YAML frontmatter parse/render, managed blocks (`<!-- zettel:auto-backlinks:start/end -->`), safe file writes that never overwrite manual edits outside managed blocks.
- **hashing.py**: Canonical text normalization (NFKC, whitespace collapse, PDF dehyphenation) before hashing. Layered checksums: file → extraction → chapter → chunk → llm_call → note_semantic. `compute_llm_call_checksum()` enables deterministic LLM response caching.
- **schemas.py**: Pydantic v2 models for all data objects and LLM structured outputs (LiteratureChunkOutput, PermanentNoteLLMOutput, DedupeResult, MOCGenerationOutput).

### Data flow between phases

`extract` saves candidates to `data/cache/candidates.json` — `connect` reads from there. All other inter-phase communication goes through StateDB and ChromaDB. Each CLI command instantiates `(AppConfig, StateDB, VectorIndex)` via `_load_deps()`, `_get_db()`, `_get_idx()` in cli.py.

### LLM provider pattern

Each pipeline module that needs LLM has its own `_get_llm(cfg)` that returns a LangChain chat model based on `cfg.llm.provider` (openai/anthropic/ollama). LLM calls go through `_call_llm(llm, prompt) → str`, responses parsed via `_extract_json()` which handles markdown code blocks.

## Important Conventions

- All generated content is in **PT-BR** by default (configurable via `config.yaml`).
- Vault note filenames follow pattern: `PREFIX - IDENTIFIER - slug.md` (e.g., `ZTL - 01ARZ3N - titulo-da-nota.md`).
- Vault structure: `00_Inbox/`, `10_Sources/`, `20_Literature/`, `30_Permanent/`, `40_MOCs/`, `90_Assets/`.
- IDs: sources use `@citekey`, chunks use `source_id::chapter_id::short_hash`, notes/mocs use ULID.
- ChromaDB metadata only accepts str/int/float/bool — lists are joined with `", "` via `_sanitize_metadata()`.
- Windows cp1252 console: avoid Unicode arrows/special chars in CLI help strings (causes UnicodeEncodeError).
