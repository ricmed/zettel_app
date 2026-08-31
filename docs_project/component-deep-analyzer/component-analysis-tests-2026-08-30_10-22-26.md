# Component Deep Analysis Report: `tests`

## 1. Executive Summary

The `tests/` directory is the project's entire automated-verification layer: a flat pytest suite of **37 files, 391 `test_*` functions** (several further multiplied by `@pytest.mark.parametrize`), covering essentially every module under `zettel/` (35 non-package `.py` files) plus the FastAPI web layer. It is invoked exclusively via `.venv/Scripts/python.exe -m pytest tests/ -v` per `CLAUDE.md`.

Key findings:

- **No `conftest.py` exists anywhere in the repository.** Every fixture (`db`, `cfg`, fake doubles) is redeclared locally, file by file — there is no shared fixture layer, no pytest plugin registration, and no test-wide `sys.path`/environment bootstrap beyond what `pyproject.toml`'s implicit defaults and each test module's own `monkeypatch` calls provide.
- **No pytest configuration exists.** `pyproject.toml` has no `[tool.pytest.ini_options]` section, and there is no standalone `pytest.ini`/`tox.ini`. Test discovery, markers, and warning filters all fall back to pytest defaults; the only markers used in the whole suite are the built-in `@pytest.mark.parametrize` (2 occurrences, both in `test_review.py`) — no custom markers (`slow`, `integration`, etc.) are registered or used anywhere.
- **`zettel/cli.py` (22 `@app.command()` Typer entry points — the single documented interface for the whole pipeline per `CLAUDE.md`'s "Build & Run Commands") has zero direct test coverage.** No file imports `zettel.cli`, and `typer.testing.CliRunner` is never referenced anywhere in `tests/`. Every CLI command is only exercised indirectly, by testing the underlying module functions the command wraps (e.g. `run_ask`, `run_sync_manual`, `purge_source`) — argument parsing, option wiring, and Typer-level error handling in `cli.py` itself are unverified.
- **The suite favors real collaborators over mocks wherever the collaborator is cheap and deterministic**: `StateDB` is instantiated against a real SQLite file under `tmp_path` in nearly every test file (28 of 37 files construct a `StateDB`), and `VectorIndex` in `test_index.py` / `test_set_paging_filter.py` runs against a **real on-disk Chroma store** (with `allow_fallback=True` to use Chroma's bundled default embedding function, avoiding network calls). Only genuinely expensive or non-deterministic collaborators — LLM calls, Docling/PyMuPDF extraction, embedding providers requiring API keys — are replaced with hand-written fakes or `unittest.mock`/`monkeypatch`.
- **No network I/O occurs in the suite.** Every LLM call site is neutralized via `monkeypatch.setattr(module, "call_llm", fake)` / `get_llm` stubs or minimal `_FakeLLM`/`FakeIndex` classes; `OPENAI_API_KEY` / `CHROMA_OPENAI_API_KEY` are explicitly deleted from the environment in `test_index.py` to force the offline fallback path and prove fail-fast behavior when no key is present.
- Heavy, slow, or platform-sensitive dependencies (Docling PDF conversion, PyMuPDF page mapping, `torch`/`umap`/`hdbscan` clustering internals) are **not exercised through their real implementations** in the sampled files — `test_extraction_dump.py` and `test_paging.py` test the paging/hash/dump logic around Docling output, not Docling itself, via pre-baked Markdown strings and fake indexes.
- Test file granularity mirrors module granularity almost 1:1 (`test_hashing.py` ↔ `hashing.py`, `test_gardener.py` ↔ `gardener.py`, etc.), with the sole exception of `test_web_state.py`, which targets `web_app.py`'s `WebWorker`/`_idx_kwargs` internals separately from `test_web.py`'s HTTP-level `TestClient` tests of `web.py`.
- No coverage tooling (`pytest-cov`, `.coveragerc`) is configured, so there is no enforced or measured line/branch coverage threshold — "coverage" in this report is assessed qualitatively (test count, assertion depth, edge cases) rather than from a coverage report artifact.

## 2. Data Flow Analysis

The suite has no runtime "data flow" of its own in the business sense; what flows is **test setup → collaborator wiring → invocation → assertion**, repeated per test function. The representative pattern, seen across the majority of files:

```
1. pytest discovers tests/test_<module>.py (no conftest.py, no plugin hooks — pure discovery)
2. A local @pytest.fixture (e.g. `db`, `cfg`, `web_client`) builds a fresh tmp_path-backed
   StateDB / AppConfig / VectorIndex / FastAPI TestClient for this test only
3. Expensive or non-deterministic externals (LLM, embeddings, Docling, filesystem edge cases)
   are replaced via:
     a. hand-written Fake*/`_Fake*` classes implementing just the methods under test
        (e.g. FakeVectorIndex, FakeIndex, _FakeDB, _FakeLLM), or
     b. monkeypatch.setattr(module, "symbol", stub) / unittest.mock.patch/MagicMock
4. The function under test (imported directly from `zettel.<module>`, often including
   private `_underscore` helpers) is invoked with the wired-up fixture/fakes
5. Assertions inspect return values, StateDB/VectorIndex/vault-file side effects, or
   (for test_web.py) HTTP status codes / rendered HTML fragments via regex
6. Fixture teardown closes the StateDB connection (`db.close()`); tmp_path is
   auto-cleaned by pytest; no explicit Chroma/vault cleanup beyond tmp_path removal
```

For the HTTP-level web tests (`test_web.py`), the flow additionally goes through a real login/CSRF cycle before any mutating test: `GET /login` → scrape `login_csrf` via regex → `POST /login` with `SESSION_SECRET` (set via `monkeypatch.setenv`) → scrape the page's `csrf` token → issue the mutating `POST` under test. This is the only place the suite drives a full request/response cycle rather than calling Python functions directly.

## 3. Business Rules & Logic

`tests/` has no domain business rules of its own — it is a verification harness for the rules that live in `zettel/*.py`. Reframed for this component, its "business rules" are the **testing conventions and invariants the suite enforces on itself and on contributors**. These are implicit (extracted from consistent patterns across files, not from any written test policy document) and are flagged with a confidence level.

## Overview of the testing conventions:

| Rule Type | Rule Description | Location / Evidence |
|-----------|------------------|----------|
| Isolation | Every stateful test gets a fresh SQLite file under `tmp_path`, never a shared/module-scoped DB | `db` fixture pattern in 28+ files, e.g. tests/test_state.py:12-16 |
| Isolation | No real network calls; LLM/embedding calls are always stubbed | monkeypatch of `call_llm`/`get_llm` across tests/test_ask.py, tests/test_llm_usage.py, tests/test_prompt_cache.py; env-var deletion in tests/test_index.py:17-19 |
| Determinism | Config-schema drift between `AppConfig` and `config/config.yaml` fails a dedicated test | tests/test_config.py:55-67 |
| Regression pinning | A pre-Fase-0 SQLite schema snapshot is hardcoded as a migration regression fixture | tests/test_state.py:20-36 (`_OLD_SCHEMA_SQL`) |
| Naming | Test module names mirror the module under test 1:1 (`test_X.py` ↔ `zettel/X.py`) | Directory listing (see Component Structure) |
| Scope discipline | Private (`_underscore`) functions are imported and tested directly rather than only through public entry points | e.g. tests/test_connector.py imports `_build_rag_context`, `_inverse_relation`, `_resolve_connections` |
| No shared fixtures | Fixtures are never centralized in a `conftest.py`; each file re-declares its own `db`/`cfg` fixture | Absence of conftest.py; repeated `@pytest.fixture def db(tmp_path)` boilerplate in ~15+ files |
| No CLI-level testing | The Typer `cli.py` surface (22 commands) is never invoked through `CliRunner` or any Typer test harness | Confirmed absence — no `CliRunner`/`typer.testing` import anywhere in tests/ |

## Detailed breakdown of the testing conventions:

---

### Rule: Per-test SQLite isolation via `tmp_path`

**Overview**:
Every test that needs a `StateDB` builds it fresh, backed by a file under pytest's `tmp_path` fixture, and closes it in fixture teardown. No test file shares a database instance across test functions, and no test relies on execution order.

**Detailed description**:
This is the single most consistent convention in the suite. Files as varied as `tests/test_state.py`, `tests/test_ask.py`, `tests/test_harvester_dedup.py`, `tests/test_assets.py`, `tests/test_graph.py`, `tests/test_review.py`, `tests/test_sync.py`, `tests/test_connector.py` (via `_FakeDB` for pure-function tests, but real `StateDB` for integration-style ones), `tests/test_purge_source.py`, `tests/test_rebuild.py`, `tests/test_moc_backrefs.py`, `tests/test_gardener_hub.py`, and `tests/test_web_state.py` all declare the near-identical fixture: `db = StateDB(tmp_path / "<name>.db"); yield db; db.close()`. Because `StateDB` opens SQLite in WAL mode (per `state.py`, documented in `CLAUDE.md`), using a real file (not `:memory:`) is a deliberate choice — WAL mode requires a real filesystem path and this exercises the actual persistence layer rather than an in-memory approximation that could hide WAL-specific bugs.

The consequence is that the suite trades a small amount of per-test I/O overhead (each test creates and tears down a real SQLite file) for high fidelity: schema migrations, FTS5 virtual table population, and cascade-delete behavior are tested against the real engine, not a mock. This is why `test_state.py` can include a hardcoded legacy schema (`_OLD_SCHEMA_SQL`) and test that `StateDB` correctly migrates a pre-existing database opened against that old schema — a test that would be meaningless against an in-memory mock.

The risk this convention manages is cross-test pollution: because `tmp_path` is unique per test invocation (pytest's default), there is no possibility of one test's leftover rows affecting another, and no cleanup ordering dependency needs to be maintained by contributors.

**Rule workflow**:
`tmp_path` (pytest builtin, function-scoped) → local `db` fixture instantiates `StateDB(tmp_path / "x.db")` → test body calls `db.upsert_*`/`db.get_*` → fixture teardown calls `db.close()` → pytest deletes `tmp_path` after the session (or leaves it for inspection depending on pytest's tmp dir retention policy).

---

### Rule: No real network or paid-API calls

**Overview**:
The suite never calls a real LLM provider or a real embedding API. Every code path that would make an outbound HTTP call to OpenAI/Anthropic/Ollama/etc. is intercepted before the test runs.

**Detailed description**:
This is enforced through three complementary techniques observed across the suite. First, direct monkeypatching of the call-site symbol: `tests/test_ask.py` does `monkeypatch.setattr(ask_mod, "call_llm", _boom)` in a test whose entire point is to assert the LLM is *never* invoked when retrieval returns no hits (`test_run_ask_empty_vault_no_llm`), and `monkeypatch.setattr(ask_mod, "call_llm", _fake_llm_call)` in the sibling test that captures the exact prompt/system text handed to the (fake) LLM. Second, minimal fake LLM objects: `tests/test_llm_usage.py` defines `_FakeLLM` with just an `.invoke()` method returning a `SimpleNamespace` shaped like a LangChain response (`content`, `usage_metadata`, `response_metadata`), letting `call_llm`'s usage/cost-recording logic be tested without any LangChain client construction. Third, environment scrubbing: `tests/test_index.py` explicitly does `monkeypatch.delenv("OPENAI_API_KEY", raising=False)` and the Chroma-specific `CHROMA_OPENAI_API_KEY` before constructing a `VectorIndex`, both to prove the fail-fast `RuntimeError` when a key is required and unavailable (`test_fail_fast_without_api_key`), and to force the `allow_fallback=True` path that uses Chroma's bundled local embedding function for all other index tests — meaning even the "real" `VectorIndex`/Chroma tests never touch a network embedding endpoint.

The practical effect is that the entire suite can run in a fully offline CI environment or sandbox with no API keys configured, which matches `CLAUDE.md`'s description of secrets living only in `.env` — the test suite deliberately does not depend on that file being populated.

**Rule workflow**:
Test setup deletes/never-sets provider env vars or monkeypatches the LLM call symbol → code under test attempts its normal call path → either it hits the fake/stub (asserted return value) or, for fail-fast tests, it raises the expected exception before any network layer would be reached.

---

### Rule: Config schema must stay in lockstep with `config/config.yaml`

**Overview**:
`tests/test_config.py` walks the `AppConfig` Pydantic model tree and asserts every leaf field has a corresponding key in the real, checked-in `config/config.yaml`, with one explicit Python-only exception.

**Detailed description**:
`schema_leaf_paths()` recursively walks `AppConfig.model_fields`, unwrapping `Optional`/`Union` annotations, and produces every dotted leaf path (e.g. `retrieval.relevance_floor.min_vector_similarity`). `test_config_yaml_covers_schema_keys` then loads the actual `config/config.yaml` from disk (not a fixture copy — the real operational file the whole application reads, per `CLAUDE.md`'s statement that this file is "the operational source") and asserts every schema leaf path resolves inside it, except for the single allowlisted exception `gardener.allowed_topics` (documented as Python-only, since real topic lists come from the taxonomy YAML instead). A companion smoke test (`test_load_config_yaml_smoke`) loads the real config and spot-checks a handful of values (`retrieval.mode == "hybrid"`, floor thresholds, hub MOC selection mode, presence of `"contradicts"` in relation weights) to catch config-value drift, not just key-presence drift.

This test doubles as living documentation enforcement: any new Pydantic field added to `config.py` without a matching YAML key fails CI immediately with a message pointing the contributor either to add the key or extend the allowlist — directly operationalizing the CLAUDE.md rule "toda chave do schema deve estar no YAML, exceto `gardener.allowed_topics`".

**Rule workflow**:
Pydantic model introspection (`model_fields`) → recursive dotted-path flattening → real YAML file loaded from repo root (relative path `config/config.yaml`, so this test is sensitive to the pytest working directory being the repo root) → set-difference against YAML keys minus allowlist → assert empty.

---

### Rule: Regression-pin legacy database schemas

**Overview**:
`tests/test_state.py` hardcodes a full CREATE-TABLE snapshot of a schema that predates retention columns and the `assets` table, to test that `StateDB` handles opening/migrating a database created under that older shape.

**Detailed description**:
The `_OLD_SCHEMA_SQL` constant is a literal, hand-maintained snapshot of table definitions for `sources`, `chunks`, `concepts`, `notes`, `mocs`, and `runs` as they existed "before Fase 0" (before retention columns / assets table existed, per the inline comment). This is a distinct testing strategy from the rest of the suite: rather than constructing state through the current API and asserting round-trip behavior, this test seeds a raw SQLite connection with old-shape DDL and then verifies `StateDB`'s migration/compatibility logic handles it. Because this snapshot is maintained by hand rather than derived from git history or a migrations directory, it is a manually-curated regression fixture — any future schema change that this constant doesn't account for would not automatically get a new legacy-shape test; a contributor would have to remember to add one.

**Rule workflow**:
Raw `sqlite3.connect()` (not through `StateDB`) executes `_OLD_SCHEMA_SQL` → `StateDB` is then pointed at that same file → migration path exercised → assertions confirm the newer expected shape/behavior is reachable without data loss.

---

### Rule: Fully flat suite with no shared `conftest.py`

**Overview**:
The suite has zero `conftest.py` files at any level (repo root or `tests/`), meaning there is no shared fixture, no shared marker registration, and no shared `sys.path`/import bootstrap beyond what pytest's default rootdir-based `sys.path` insertion and the `tests/__init__.py` (empty, 0 lines) provide.

**Detailed description**:
Every one of the ~15+ files that need a `StateDB` fixture re-declares essentially the same three lines (`db = StateDB(tmp_path / "x.db"); yield db; db.close()`), and every file that needs an `AppConfig` builds its own local `_cfg()` helper or `cfg` fixture rather than importing a shared factory. This is a deliberate-looking but costly convention: it keeps each test file fully self-contained and readable in isolation (no need to jump to a `conftest.py` to understand fixture behavior), at the cost of ~15-20 near-duplicate fixture declarations across the codebase and no single point of control if the `StateDB`/`AppConfig` construction signature changes in a way that affects fixture setup (a change would need to be hand-applied file by file, or each file's local fixture would silently continue using the old pattern until it broke). It also means there is nowhere to register custom pytest markers, shared CLI options, or a global autouse fixture (e.g. to force offline mode suite-wide) — each file that needs offline enforcement (env var deletion) does so itself rather than inheriting it.

**Rule workflow**:
N/A (absence of a mechanism) — every test file is fully self-sufficient for its own fixtures; pytest's default test collection (`tests/test_*.py`, `test_*` functions) governs the whole suite with no customization layer.

---

### Rule: Private/internal functions are tested directly, not only through public entry points

**Overview**:
Many test files import and directly unit-test module-private (`_`-prefixed) functions rather than exclusively driving the public pipeline entry point (`run_harvest`, `run_review`, `run_connect`, etc.).

**Detailed description**:
Examples: `tests/test_connector.py` imports `_build_rag_context`, `_fallback_image_ids`, `_inverse_relation`, `_relation_type_value`, `_resolve_connections`, `_resolve_images` — none of these are part of `connector.py`'s public surface. `tests/test_gardener.py` imports over a dozen private helpers (`_validate_moc_topic`, `_parse_moc_structure`, `_parse_incremental_output`, `_update_existing_moc`, `_apply_incremental_placements`, `_build_moc_body`, `_build_note_alias_map`, `_resolve_note_ref`, `_allowed_note_ids`, `_note_wikilink`). `tests/test_harvester_dedup.py` imports `_find_semantic_duplicate_candidates`, `_process_file`, `_resolve_duplicate_decision`, `_sample_chunk_texts`. This white-box strategy gives fine-grained coverage of business-rule branches (e.g. MOC topic validation logic, duplicate-decision resolution, RAG context assembly) without needing to construct an entire pipeline run through the public API, which would require far more setup (full config, full vault tree, full chunk/concept graph). The tradeoff is tighter coupling between the test suite and internal module structure: renaming or refactoring a private helper's signature breaks tests even if the module's public behavior is unchanged, which is a normal cost of white-box unit testing but worth flagging since it affects how "safe" internal refactors are without also touching `tests/`.

**Rule workflow**:
N/A (a testing strategy, not a runtime workflow) — test imports the private symbol directly from the module namespace and calls it with hand-built arguments (often plain dicts/dataclasses rather than full pipeline objects).

---

## 4. Component Structure

```
tests/
├── __init__.py                    # Empty (0 lines) — marks tests/ as a package for pytest import mode
├── test_article.py                # article.py (run_article) — LangGraph long-form writing pipeline
├── test_article_graph.py          # article_graph.py — LangGraph StateGraph node wiring for article.py
├── test_ask.py                    # ask.py (run_ask) — QA-over-vault, no-evidence short-circuit, prompt content
├── test_assets.py                 # assets.py — image extraction/dedup/description caching (Fase 3)
├── test_bibliography.py           # bibliography.py + harvester._resolve_bibliography — citation resolution
├── test_chunk_dump.py             # chunk_dump.py — --dump-chunks markdown export
├── test_config.py                 # config.py — AppConfig <-> config.yaml schema parity (see Business Rules)
├── test_connector.py              # connector.py — typed connections, inverse relations, RAG context, note body
├── test_dedupe_decision.py        # schemas.py (DedupeDecision/DedupeResult) — LLM structured-output shape
├── test_extraction_dump.py        # extraction_dump.py + harvester._process_file — extracted-text markdown dump
├── test_extractor.py              # extractor.py (_filter_candidates) — Prompt 1 candidate filtering
├── test_gardener.py               # gardener.py — MOC taxonomy validation, incremental updates (largest file, 827 lines)
├── test_gardener_assign.py        # gardener_assign.py — category/cluster assignment (UMAP+HDBSCAN inputs)
├── test_gardener_hub.py           # gardener_hub.py — hub-anchored MOC pipeline (Fase 4b)
├── test_graph.py                  # graph.py (expand_notes) — BFS graph expansion, relation weights/hop decay
├── test_harvester_dedup.py        # harvester.py — three-layer duplicate detection (file/extraction/semantic)
├── test_harvester_sections.py     # harvester.py — H3-H6 structural chunking
├── test_hashing.py                # hashing.py — text normalization, checksums, embeddable-text extraction
├── test_index.py                  # index.py (VectorIndex) — embedding safety, fail-fast, embedding-space mismatch
├── test_llm_usage.py              # llm.py (call_llm) + usage.py — cost/usage tracking on LLM calls
├── test_moc_backrefs.py           # moc_backrefs.py — auto-moc-backrefs managed block sync
├── test_new_note.py               # new_note.py — manual vault note scaffolding (ztl/src/lit/moc)
├── test_paging.py                 # paging.py — content-start paging, page_in_book computation
├── test_pricing.py                # pricing.py — LiteLLM cost_per_token wrapper
├── test_prompt_cache.py           # llm.py — Anthropic cache_control hints, prompt-cache toggling
├── test_purge_source.py           # purge_source.py — irreversible source deletion, wikilink stripping
├── test_rebuild.py                # rebuild.py — reindex/rebuild-vault commands
├── test_retrieval.py              # retrieval.py (Retriever) — RRF fusion, relevance floor gating
├── test_review.py                 # review.py — confidence bands, approve/reject decision normalization
├── test_set_paging.py             # harvester.run_set_paging — non-LLM paging repair on existing sources
├── test_set_paging_filter.py      # harvester._chunk_and_persist — paging filter interaction with chunking
├── test_state.py                  # state.py (StateDB) — CRUD, FTS5 match expression, legacy schema migration
├── test_sync.py                   # sync.py — manual vault sync, body-edge extraction, graph rebuild
├── test_usage.py                  # usage.py — CostTracker aggregation primitives
├── test_vault.py                  # vault.py — frontmatter parse/render, managed blocks, safe writes
├── test_web.py                    # web.py — FastAPI routes via TestClient (auth, CSRF, upload validation)
└── test_web_state.py              # web_app.py — WebWorker, _idx_kwargs parity with cli._idx_kwargs
```

No subdirectories, no `fixtures/` or `data/` folder for sample files — all fixture data (PDFs' worth of text, YAML taxonomies, config snippets) is inlined as Python string literals within each test file (e.g. `_MINI_TAXONOMY`, `RAW_MD`, `_OLD_SCHEMA_SQL`, `_PNG`).

## 5. Dependency Analysis

```
Internal Dependencies (tests/ -> zettel/):
tests/test_*.py -> zettel.<same-named-module>  (near 1:1 mapping, see Component Structure)
tests/test_web.py -> zettel.web (create_app) -> exercises zettel.web_app, zettel.state, zettel.index transitively
tests/test_web_state.py -> zettel.web_app (WebWorker, _idx_kwargs, UserFacingError, safe_error)
tests/test_extraction_dump.py -> zettel.harvester._process_file (cross-module: dump tests reuse harvest internals)
tests/test_bibliography.py -> zettel.harvester._process_file, zettel.harvester._resolve_bibliography
tests/test_set_paging_filter.py -> zettel.harvester._chunk_and_persist
tests/test_moc_backrefs.py -> zettel.gardener.purge_pipeline_mocs, zettel.sync.run_sync_manual (cross-module integration test)
tests/test_article_graph.py -> zettel.article (module-level monkeypatch target), zettel.article_graph.run_article_graph

External Dependencies (test-only or shared with runtime):
- pytest (>=9.0.2, declared in pyproject.toml)         - test runner, fixtures, parametrize, monkeypatch
- fastapi.testclient.TestClient (via fastapi>=0.115.0)  - HTTP-level testing for zettel.web (test_web.py)
- unittest.mock (stdlib: MagicMock, patch)              - mocking VectorIndex, gardener LLM calls, pricing
- sqlite3, tempfile (stdlib)                             - direct legacy-schema seeding in test_state.py
- numpy (test_gardener_assign.py)                        - constructing embedding-like arrays for clustering input
- yaml / PyYAML (test_config.py, test_sync.py, test_purge_source.py) - loading real config.yaml / asserting frontmatter
- langchain_core.messages (test_prompt_cache.py)         - constructing SystemMessage/HumanMessage for cache-hint assertions

Explicitly NOT exercised for real in tests/ (always faked/stubbed/skipped):
- OpenAI / Anthropic / Gemini / Ollama LLM providers    - always monkeypatched (call_llm/get_llm) or _FakeLLM
- Real embedding providers requiring API keys            - VectorIndex tests force allow_fallback=True, delete API-key env vars
- Docling PDF conversion                                 - only its Markdown *output* is simulated as string literals
- PyMuPDF page-map extraction                            - not directly exercised (paging tests operate on ContentPaging/hash logic)
- umap-learn / hdbscan clustering internals               - test_gardener_assign.py feeds numpy arrays to the assignment logic, not verified against real UMAP/HDBSCAN by file sampling done here
```

## 6. Afferent and Efferent Coupling

Coupling is measured at the file level (each `test_X.py` is the unit of analysis, since Python modules — not classes — are this project's primary unit of organization, per its procedural/module-based structure).

| Component (test file) | Afferent Coupling (depended on by) | Efferent Coupling (depends on) | Critical |
|-----------|-------------------|-------------------|-------------------|
| test_gardener.py | 0 | 6 (config, gardener, schemas, state, taxonomy, vault) | High (827 lines, largest file, validates MOC generation/taxonomy — a core Phase-4 business rule surface) |
| test_state.py | 0 | 1 (state) | High (StateDB underlies nearly every other test file's fixtures; a break here signals broad regression risk) |
| test_web.py | 0 | 1 (web, transitively web_app/state/index) | High (only end-to-end HTTP-level test; sole guard on auth/CSRF/upload security rules) |
| test_config.py | 0 | 1 (config) | High (guards schema/YAML parity for the entire application's configuration surface) |
| test_index.py | 0 | 1 (index) | High (guards embedding fail-fast and embedding-space-mismatch safety rules) |
| test_moc_backrefs.py | 0 | 4 (gardener, moc_backrefs, state, sync, vault) | Medium (cross-module integration between gardener purge and sync backref logic) |
| test_extraction_dump.py | 0 | 3 (extraction_dump, harvester, state) | Medium (reuses harvester internals rather than only its own module) |
| test_bibliography.py | 0 | 4 (bibliography, config, harvester, state, vault) | Medium |
| test_harvester_dedup.py | 0 | 3 (harvester, hashing, state) | Medium (guards the three-layer duplicate-detection business rule) |
| test_retrieval.py | 0 | 2 (config, retrieval, state) | High (guards the relevance-floor/RRF gating logic described in CLAUDE.md) |
| test_ask.py | 0 | 3 (ask, config, retrieval, state) | Medium |
| Remaining ~26 files | 0 | typically 1-3 modules each | Low-Medium (module-local unit tests, narrow blast radius) |

All afferent coupling values are 0 because test files are leaf nodes in the dependency graph — nothing in `zettel/` imports from `tests/`, and no test file imports another test file (no shared base-test-class pattern, no test-to-test imports observed in the file list). The "critical" column instead reflects each file's efferent breadth and the centrality of the module(s) it exercises (e.g. `state.py`/`config.py`/`index.py` are load-bearing for the whole app, so their test files are flagged High regardless of their own small efferent count).

## 7. Endpoints

Not applicable — `tests/` is a test suite, not a service; it consumes (via `TestClient`) the endpoints exposed by `zettel/web.py`, but exposes none of its own. (Per report instructions, this section is omitted for components without endpoints; it is left in skeletal form here only to record that the check was performed.)

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling in Tests |
|-------------|------|---------|----------|-------------|----------------|
| SQLite (`StateDB`) | Embedded DB | Real, on-disk (tmp_path) persistence for nearly all stateful tests | SQL / WAL-mode file | Rows / dict-like records | Real exceptions surface directly (e.g. `sqlite3` errors); no wrapping |
| ChromaDB (`VectorIndex`) | Embedded vector DB | Real, on-disk store for embedding-safety tests | Local Chroma client API | Vectors + metadata | Explicit `pytest.raises(RuntimeError)`, `pytest.raises(ValueError)`, `pytest.raises(EmbeddingSpaceMismatch)` assertions |
| FastAPI `TestClient` | In-process HTTP | Full request/response cycle for `web.py` routes | HTTP over ASGI (in-process, no sockets) | HTML (Jinja2 templates) + form data | Status-code assertions (303, 401, 403, 400, 409) drive security-rule verification |
| LLM providers (OpenAI/Anthropic/Gemini/Ollama) | External Service | Normally consumed by `llm.py`/`ask.py`/`connector.py`/`gardener.py` | N/A in tests | N/A in tests | Always intercepted before the network boundary via monkeypatch/fakes — never actually called |
| `config/config.yaml` (real file) | Local config file | Cross-checked against `AppConfig` schema | YAML | Nested dict | Test fails loudly (assertion with a Portuguese diagnostic message) listing exact missing dotted paths |
| Filesystem (vault tree) | Local FS | Building/asserting on real `.md` files under `tmp_path` | File I/O | Markdown + YAML frontmatter | Assertions on file existence/content; no simulated FS (no `pyfakefs` or similar) |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Test Double (hand-rolled Fake) | `FakeVectorIndex`, `FakeIndex`, `_FakeDB`, `_FakeLLM`, `_FakeIdx` | tests/test_harvester_dedup.py:19-42, tests/test_ask.py:19-24, tests/test_connector.py:17-24, tests/test_llm_usage.py:20-28, tests/test_extraction_dump.py:46-60 | Replace expensive/networked collaborators with minimal same-interface stand-ins, avoiding `unittest.mock`'s looser interface contracts where a precise fake is easy to write |
| Monkeypatching (pytest built-in) | `monkeypatch.setattr(module, "symbol", fn)` / `monkeypatch.setenv` / `monkeypatch.delenv` | Widely used, e.g. tests/test_ask.py:35-36, tests/test_index.py:17-19, tests/test_web.py:30 | Swap module-level symbols (LLM call functions) or environment state per-test without global side effects |
| `unittest.mock.patch`/`MagicMock` | Targeted patching of specific call sites (e.g. `zettel.pricing.estimate_llm_cost`) | tests/test_llm_usage.py:40, tests/test_gardener.py, tests/test_index.py, tests/test_review.py, tests/test_gardener_hub.py, tests/test_moc_backrefs.py | Used interchangeably with monkeypatch depending on file author's preference — no single convention chosen project-wide |
| Fixture-per-file (no shared conftest) | `@pytest.fixture def db(tmp_path): ...` repeated in ~15+ files | See Business Rules §"No shared conftest.py" | Keeps each test file self-contained; trades DRY-ness for locality |
| Table-driven / parametrized tests | `@pytest.mark.parametrize` | tests/test_review.py:195-233 (only occurrence in the suite) | Compact coverage of many input->output mappings for `normalize_reject_scope`/`normalize_review_decision` |
| Golden/inline fixture data | Multi-line string literals for YAML/Markdown/binary content | `_MINI_TAXONOMY` (test_gardener.py), `RAW_MD` (test_extraction_dump.py), `_PNG` (test_assets.py), `_OLD_SCHEMA_SQL` (test_state.py) | Keeps test data colocated and reviewable in the same diff as the test, at the cost of no reuse across files |
| Regression/legacy-shape fixture | `_OLD_SCHEMA_SQL` seeded via raw `sqlite3.connect()` | tests/test_state.py:20-36 | Pin behavior against a schema shape from before a specific migration ("Fase 0") |
| White-box unit testing of private helpers | Direct import of `_`-prefixed functions | tests/test_gardener.py, tests/test_connector.py, tests/test_harvester_dedup.py, tests/test_extractor.py, tests/test_review.py | Fine-grained branch coverage of business-rule internals without full pipeline setup |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|----------------|-------|--------|
| High | Whole suite / `zettel/cli.py` | No test imports `zettel.cli` or uses `typer.testing.CliRunner`; all 22 `@app.command()` entry points are untested at the argument-parsing/Typer-wiring level | A regression in option names, defaults, help text, or Typer-level error handling for any CLI command (the sole documented way to run the pipeline per `CLAUDE.md`) would not be caught by `pytest tests/` |
| Medium | Whole suite | No `conftest.py`; ~15+ files hand-duplicate the same `StateDB`/`tmp_path` fixture boilerplate | Any future change to `StateDB`'s constructor or lifecycle contract requires hunting down and editing every duplicated fixture individually; no single source of truth for "how to build a test DB" |
| Medium | Whole suite | No pytest configuration (`[tool.pytest.ini_options]`, `pytest.ini`) and no custom markers registered | No way to selectively run/skip slow vs. fast tests, no enforced warning-filter policy (e.g. deprecation warnings from `chromadb`/`langchain` could silently accumulate unnoticed), no `testpaths` pin (relies on being invoked as `pytest tests/` per CLAUDE.md rather than the config enforcing it) |
| Medium | Whole suite | No coverage tooling configured (`pytest-cov`/`.coveragerc` absent) | Coverage gaps (like the CLI gap above) are invisible without manual auditing; no CI gate on coverage regressions |
| Medium | test_config.py | Relies on a relative path `Path("config/config.yaml")`, which is only correct if pytest is invoked from the repository root | Running `pytest` from a different working directory (e.g. from within `tests/`) would make this test fail to find the file, producing a misleading error rather than a clear "wrong CWD" message |
| Low-Medium | test_state.py | `_OLD_SCHEMA_SQL` is a hand-maintained legacy-schema snapshot with no automated link back to actual migration history | If the schema changes again, nothing forces a contributor to add a new legacy-shape regression test; the existing one could become stale/irrelevant without anyone noticing |
| Low | Whole suite | Mocking style is inconsistent: some files use hand-rolled Fake classes, others `unittest.mock.patch`/`MagicMock`, others `monkeypatch.setattr` for the same kind of substitution (an LLM/DB/index stand-in) | Increases the learning curve when moving between test files; no single idiom to search for or extend |
| Low | test_gardener_assign.py | Feeds `numpy` arrays directly rather than exercising real `umap-learn`/`hdbscan` behavior (based on the sampled imports) | Real clustering-library version upgrades or parameter changes are not caught by this test path — clustering correctness relies on other, unsampled coverage or manual QA |
| Low | Whole suite | Docling PDF extraction and PyMuPDF page mapping are never exercised through their real implementations (only their string-literal *output* is simulated) | A behavior change or version bump in `docling`/`PyMuPDF` (both pinned with `>=` ranges in `pyproject.toml`, not exact pins) could silently change real extraction output without any test noticing, since tests bypass the real library entirely |

## 11. Test Coverage Analysis

This section inventories the suite's own internal coverage characteristics (test counts per file and qualitative depth), since `tests/` is the component under analysis rather than a consumer of it.

| Test File | Test Functions | Parametrized Cases | Test Quality / Notes |
|-----------|------------|-------------------|--------------|
| tests/test_gardener.py | 37 | 0 | Largest file (827 lines); dense coverage of MOC taxonomy validation, incremental vs. generation routing, alias/wikilink resolution; strong use of hand-built helper factories (`_make_moc_output`, `_make_config`) |
| tests/test_state.py | 27 | 0 | Broad CRUD coverage plus one dedicated legacy-schema migration regression test and FTS5 match-expression unit tests |
| tests/test_vault.py | 24 | 0 | Frontmatter parse/render and managed-block round-tripping; safe-write-never-clobbers-manual-edits behavior |
| tests/test_bibliography.py | 19 | 0 | Citation resolution across multiple source shapes; integrates with real `_process_file` harvester internals |
| tests/test_retrieval.py | 19 | 0 | Covers RRF fusion and the four-step relevance-floor gating order described in CLAUDE.md (absolute min similarity, bm25 bypass + rank cutoff, main vector floor, bm25-only rejection) |
| tests/test_index.py | 16 | 0 | Fail-fast/embedding-space-mismatch safety net; uses a real on-disk Chroma store with the bundled fallback embedding function |
| tests/test_review.py | 16 | 23 (parametrize) | Only file using `@pytest.mark.parametrize`; strong table-driven coverage of confidence-band and decision-normalization edge cases (case sensitivity, whitespace, abbreviations) |
| tests/test_new_note.py | 16 | 0 | Covers all four scaffold aliases (`ztl`/`src`/`lit`/`moc`) and citekey derivation paths |
| tests/test_connector.py | 15 | 0 | Good coverage of relation-type inversion (all 6 types + unknown fallback) and wikilink resolution for known/unknown notes |
| tests/test_sync.py | 13 | 0 | Covers manual sync, body-edge extraction, and full-vault edge rebuild |
| tests/test_harvester_sections.py | 12 | 0 | Structural H3-H6 chunking edge cases |
| tests/test_rebuild.py | 12 | 0 | reindex/rebuild-vault command internals |
| tests/test_harvester_dedup.py | 11 | 0 | Covers the three-layer duplicate-detection rule end to end with a purpose-built `FakeVectorIndex` |
| tests/test_paging.py | 11 | 0 | Docling-config-hash and content-paging arithmetic (page_in_book computation) |
| tests/test_chunk_dump.py | 10 | 0 | `--dump-chunks` export formatting |
| tests/test_extraction_dump.py | 10 | 0 | Extraction markdown dump; reuses harvester internals via a local `_FakeIdx` |
| tests/test_web.py | 10 | 0 | The suite's only true HTTP-integration file; covers auth redirect, CSRF mismatch (both directions), path-traversal upload rejection, filename-markup rejection, upload collision (409) |
| tests/test_hashing.py | 9 | 0 | Deterministic-hash and normalization edge cases (CRLF, dehyphenation, blank-line collapsing, frontmatter/managed-block stripping) |
| tests/test_prompt_cache.py | 9 | 0 | Anthropic `cache_control` hint construction and toggling via `llm.prompt_cache` |
| tests/test_gardener_hub.py | 9 | 0 | Hub-anchored MOC pipeline (Fase 4b), incremental vs. new-topic routing |
| tests/test_extractor.py | 6 | 0 | `_filter_candidates` review-confidence filtering logic |
| tests/test_web_state.py | 6 | 0 | `WebWorker`/`_idx_kwargs` — explicitly guards the CLAUDE.md-flagged parity requirement with `cli._idx_kwargs` |
| tests/test_purge_source.py | 6 | 0 | Irreversible deletion + wikilink-stripping side effects |
| tests/test_dedupe_decision.py | 5 | 0 | Pydantic structured-output shape for LLM dedupe results |
| tests/test_moc_backrefs.py | 5 | 0 | `auto-moc-backrefs` managed-block sync; cross-module with gardener purge and sync |
| tests/test_gardener_assign.py | 4 | 0 | Category/cluster assignment given pre-built numpy embedding arrays |
| tests/test_pricing.py | 4 | 0 | LiteLLM cost wrapper |
| tests/test_ask.py | 5 | 0 | No-evidence short-circuit (LLM never called) and exact-wikilink-in-prompt assertions — precise, behavior-critical tests |
| tests/test_article_graph.py | 3 | 0 | LangGraph node wiring for the article pipeline |
| tests/test_usage.py | 3 | 0 | `CostTracker` aggregation primitives |
| tests/test_config.py | 2 | 0 | Small file, high leverage — see Business Rules; guards the entire config schema surface with two tests |
| tests/test_article.py | 10 | 0 | Long-form article pipeline node behavior |
| tests/test_llm_usage.py | 1 | 0 | Single focused test, but exercises the full usage/cost-recording path end to end with a real `patch` on `estimate_llm_cost` |
| tests/test_set_paging.py | 1 | 0 | `run_set_paging` — a single test function name at the `def test_` grep level; file is 129 lines, likely containing substantial setup/assertions within that one function or additional non-`test_`-prefixed helpers |
| tests/test_set_paging_filter.py | 1 | 0 | Same pattern as above, 69 lines |

**Coverage gaps not visible in the per-file table above:**
- **`zettel/cli.py`**: zero direct coverage (see Technical Debt, High severity).
- **`zettel/progress.py`**: no `test_progress.py` file exists in `tests/` despite `progress.py` being listed as a distinct module in `zettel/` (shared `ProgressObserver` protocol per CLAUDE.md) — its behavior is presumably only exercised indirectly through other tests that trigger progress callbacks, if at all.
- **No coverage percentage is computable**: the project has no `pytest-cov` dependency and no `.coveragerc`/`coverage.xml` artifact, so this analysis is necessarily based on test-count and inspection rather than measured line/branch coverage.

## 12. Report Saved

Absolute path: `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-tests-2026-08-30_10-22-26.md`
