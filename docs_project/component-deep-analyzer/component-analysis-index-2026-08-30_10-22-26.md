# Component Deep Analysis Report — `index` (VectorIndex)

## 1. Executive Summary

`zettel/index.py` is the sole ChromaDB access layer for the Zettelkasten pipeline. It exposes a single public class, `VectorIndex`, which wraps a `chromadb.PersistentClient` and manages five named collections (`sources`, `chunks`, `permanent_notes`, `mocs`, `literature_notes`). It is the only module in the codebase that imports `chromadb` directly — every other module (harvester, extractor, review, connector, gardener, gardener_hub, sync, retrieval, rebuild, ask, article, purge_source, cli, web_app) interacts with vectors exclusively through a `VectorIndex` instance passed down the call chain, making this component the project's data-access-abstraction boundary for the vector store, analogous to a Repository pattern.

The component's core responsibility is not just "store and query embeddings" — it is **guaranteeing embedding-space integrity**. Because ChromaDB has no built-in awareness that swapping embedding providers/models/dimensions invalidates existing vectors (mixing vector spaces silently corrupts nearest-neighbour search), `VectorIndex` layers an explicit identity-marker system (`embedding_provider`/`embedding_model`/`embedding_dimensions` stored in each collection's metadata) with fail-fast comparison logic (`EmbeddingSpaceMismatch`) on top of Chroma's own primitives. A second responsibility is **cost observability**: every embedding call (upsert or query) is metered against the process-wide `CostTracker` (from `zettel.usage`), estimating tokens/cost via `zettel.pricing`, so the CLI's `runs`/cost reporting stays accurate without requiring embedding-cost logic to be duplicated in each pipeline phase.

Key findings:
- The component supports three embedding providers (OpenAI, SentenceTransformers, Ollama via LangChain) through a factory method (`_build_embedding_fn`) with distinct fail-fast/fallback semantics per provider.
- A custom Chroma `EmbeddingFunction` adapter (`_LangChainOllamaChromaEF`) is required because Chroma has no native Ollama embedding-function class compatible with `langchain_ollama.OllamaEmbeddings`; it is registered globally at import time so Chroma can reconstruct persisted collections that used it.
- All collections share one embedding function and one metadata schema; there is no per-collection embedding configuration.
- The component has no network/API surface of its own (no REST/GraphQL/gRPC) — it is a library consumed in-process.
- Test coverage (`tests/test_index.py`) is concentrated on the embedding-safety machinery (mismatch detection, dimension checks, Ollama adapter) and metadata sanitization; CRUD-path methods (`upsert_chunk`, `query_similar_notes`, `find_similar_chunks`, etc.) have comparatively thin direct unit coverage and are instead exercised indirectly through consumer-module tests.

## 2. Data Flow Analysis

`VectorIndex` has no single "entry point" — it is instantiated once per CLI command / web job invocation and then called by whichever pipeline phase is running. Two representative flows:

**A. Construction / embedding-space validation (every command):**
```
1. CLI command calls _get_idx(cfg) / web_app._idx_kwargs(cfg)
2. VectorIndex.__init__ opens chromadb.PersistentClient(chroma_path)
3. _build_embedding_fn() constructs the provider-specific EmbeddingFunction
   (openai / sentence-transformers / ollama), or raises/falls back
4. get_stored_embedding_identity() reads embedding_provider/model/dimensions
   markers off any existing collection's metadata
5. embedding_space_matches() compares stored vs. configured identity
   - mismatch + reset_mismatched=False -> raise EmbeddingSpaceMismatch
     (caller in cli.py warns, prompts, and re-invokes with reset_mismatched=True)
   - mismatch + reset_mismatched=True  -> _delete_all_collections()
6. _ensure_collections() calls get_or_create_collection for all 5 names,
   stamping _collection_metadata() (provider/model/dimensions) on each
7. Handles cached on self.sources / self.chunks / self.permanent /
   self.mocs_col / self.literature for the lifetime of the instance
```

**B. Upsert (e.g. `connector.py` writing a new permanent note):**
```
1. Caller builds embeddable_text + metadata dict (may contain lists/None)
2. VectorIndex.upsert_permanent_note(note_id, embeddable_text, metadata)
3. _sanitize_metadata() coerces metadata to Chroma-legal types
   (drops None, joins lists with ", ", stringifies unknown types)
4. self.permanent.upsert(...) triggers the configured EmbeddingFunction,
   which computes the vector (calls out to OpenAI API / local model /
   Ollama server) and Chroma persists id+document+metadata+vector
5. _record_embed_usage() estimates tokens (chars // 4) and cost via
   zettel.pricing.estimate_embed_cost(), then records on the active
   CostTracker (zettel.usage) if one is bound to the current run
6. Caller (connector.py) proceeds to persist the note in StateDB/vault
```

**C. Query (e.g. `retrieval.py` dense search):**
```
1. Retriever.search_notes() calls idx.query_similar_notes(query_text, ...)
2. query_similar_notes() embeds the query text (implicit, via Chroma's
   query_texts=) and issues an ANN search against `permanent`
3. Raw Chroma results (ids/documents/metadatas/distances) are reshaped
   into a flat list of dicts, one per hit, excluding an optional self-id
4. _record_embed_usage() meters the query embedding cost
5. Retriever fuses this with BM25 (FTS5) results via RRF and applies the
   relevance floor (outside this component's scope)
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Invariant | All 5 collections share exactly one embedding function/space per `VectorIndex` instance | index.py:456-465 |
| Validation | Unknown embedding provider raises unless `allow_fallback=True` | index.py:296-322 |
| Validation | Missing OpenAI API key raises unless `allow_fallback=True` | index.py:329-335 |
| Validation | `embedding.dimensions` is silently ignored (with a warning) for `sentence-transformers` | index.py:349-355 |
| Business Logic | Embedding-space identity is stamped as collection metadata and compared on every open | index.py:376-403 |
| Business Logic | A stored-vs-configured identity mismatch raises `EmbeddingSpaceMismatch` unless `reset_mismatched=True` | index.py:234-251 |
| Business Logic | `reset_mismatched=True` deletes *all* 5 collections, not just the mismatched one | index.py:236-244, 405-410 |
| Business Logic | `_get_or_create` proactively checks metadata before calling Chroma, and also translates Chroma's own "Embedding function conflict" `ValueError` into a domain-specific `RuntimeError` | index.py:412-453 |
| Business Logic | Ollama base URL is normalized by stripping a legacy `/v1` OpenAI-compatible suffix | index.py:27-32 |
| Business Logic | The Ollama LangChain adapter forbids changing the embedding model on an existing collection (`validate_config_update`) | index.py:86-92 |
| Business Logic | `existing_ids()` deduplicates the input id list before querying, because Chroma's `get()` rejects duplicate ids | index.py:535-556 |
| Business Logic | `close()`/`vacuum()` release all Python references to the Chroma client (including collection handles) before a VACUUM, required for Windows file-locking semantics | index.py:255-288 |
| Business Logic | Every upsert records estimated token/cost usage against the active `CostTracker`, but is a silent no-op when no tracker is bound | index.py:699-728 |
| Data Sanitization | ChromaDB metadata only accepts str/int/float/bool; `None` is dropped, lists are joined with `", "`, everything else is stringified | index.py:754-766 |
| Business Logic | `reset_collection()` refreshes the cached instance attribute so subsequent calls hit the new collection object, not a stale reference | index.py:467-485 |

### Detailed breakdown of the business rules

---

### Business Rule: Embedding-Space Identity Enforcement

**Overview**:
Every ChromaDB collection managed by `VectorIndex` is stamped with `embedding_provider`, `embedding_model`, and (optionally) `embedding_dimensions` in its Chroma-native metadata dict at creation time. On every subsequent open, the currently configured identity is compared against whatever is stored, and any mismatch is treated as a hard error unless the caller explicitly opts into a full reset.

**Detailed description**:
ChromaDB's vector index (HNSW) has no concept of "embedding versioning" — it simply stores whatever vectors it is given and returns nearest neighbours by raw distance. If an operator changes `embedding.provider`, `embedding.model`, or `embedding.dimensions` in `config/config.yaml` (e.g. switching from OpenAI `text-embedding-3-small` to a local Ollama model, or changing MRL truncation dimensions), any vectors written under the old space become geometrically incomparable to vectors written under the new space — cosine/L2 distances computed across the two spaces are meaningless, and search results silently degrade into noise without raising any Chroma-level error. This is a subtle, high-severity data-integrity risk because the corruption is invisible until retrieval quality visibly regresses (e.g. `ask`/`connect` start returning irrelevant matches), by which point the cause is hard to trace back to a config change.

To close this gap, `VectorIndex.__init__` calls `get_stored_embedding_identity()` (which inspects `_identity_from_client`, scanning `permanent_notes` first, then `chunks`, `sources`, `mocs`, `literature_notes` for embedding markers) and compares it via `embedding_space_matches()`. If no marker exists anywhere (fresh store, or a legacy store predating this feature), the check passes vacuously — the component cannot invent history it never recorded. If a marker exists and disagrees on provider, model, *or* dimensions, `embedding_space_matches()` returns `False` and the constructor raises `EmbeddingSpaceMismatch` with a formatted message pointing the operator at `zettel reindex --force`. Callers (`cli._get_idx`, the `reindex` command, `doctor`) catch this exception, render a warning panel explaining the drift in human terms, and require either an interactive confirmation or the `--yes` flag before proceeding — the component itself never silently resolves the conflict.

The consequence of accepting the reset is total: `reset_mismatched=True` calls `_delete_all_collections()`, which iterates all five collection names and deletes each one (swallowing exceptions for already-absent collections), even though only one collection might carry the drifted marker in principle — in practice all five are always created/reset together (see the "single shared space" rule below), so this is consistent, not overreaching. The regeneration path afterward (`run_reindex` in `rebuild.py` with `force=True`) is a SQLite-driven rebuild with no LLM calls — the source-of-truth text lives in SQLite, and Chroma is treated as a disposable, rebuildable cache. This framing — "Chroma is a cache, not a source of truth" — is the architectural principle that makes an aggressive full-reset an acceptable trade-off for correctness over convenience.

**Rule workflow**:
```
VectorIndex(chroma_path, provider, model, dimensions=..., reset_mismatched=?)
  -> stored = get_stored_embedding_identity()   [scan 5 collections' metadata]
  -> if embedding_space_matches(stored):
        proceed to _ensure_collections() normally
     else:
        if reset_mismatched:
            log warning, _delete_all_collections(), proceed (fresh space)
        else:
            raise EmbeddingSpaceMismatch(stored..., current...)
            [caller: warn -> confirm/--yes -> retry with reset_mismatched=True
             -> run_reindex(force=True) to repopulate from SQLite]
```

---

### Business Rule: Fail-Fast Embedding Provider Construction (No Silent 384-d Fallback)

**Overview**:
Constructing the embedding function for any of the three supported providers (`openai`, `sentence-transformers`, `ollama`) either succeeds with a real, correctly configured embedding function, or raises — it never silently substitutes ChromaDB's built-in default embedding function (a 384-dimension MiniLM model) unless the operator has explicitly set `embedding.allow_fallback: true`.

**Detailed description**:
ChromaDB, if given no `embedding_function` at collection-creation time, transparently defaults to a bundled small SentenceTransformers model. This is a reasonable default for a toy project but a dangerous trap for this system: if the OpenAI API key is absent (e.g. `.env` misconfigured on a fresh clone) or a typo'd provider name slips into `config.yaml`, silently falling back to the 384-d default would (a) produce vectors in a completely different space than intended, and (b) do so without any error, meaning a corpus could be embedded for weeks with a "wrong" model before anyone notices via degraded search quality. `_build_embedding_fn` therefore treats an unknown provider name and a missing OpenAI key as fatal by default: `ValueError` for the former (naming the three valid provider strings in the message), `RuntimeError` for the latter (naming both env vars checked — `CHROMA_OPENAI_API_KEY` and `OPENAI_API_KEY` — and the config flag that would suppress the error).

The `allow_fallback` flag exists specifically for two legitimate use cases visible in the codebase: **tests**, which need to construct a `VectorIndex` offline without any real credentials (see `tests/test_index.py`'s repeated pattern of `allow_fallback=True` with an invalid provider name, deliberately exercising the fallback path), and **explicit operator opt-in** to use Chroma's bundled default when no OpenAI key is configured and local embedding infra isn't desired. Even when `allow_fallback=True` is set and a *known* provider (`sentence-transformers`, `ollama`) throws `ImportError` because its optional dependency isn't installed, the component still logs a warning and falls back rather than crashing — but a *missing OpenAI key* specifically is checked with an explicit `if not api_key and not self.allow_fallback: raise` inside `_build_openai_ef`, independent of the generic `ImportError` catch in `_build_embedding_fn`, because a missing key is a `RuntimeError`, not an `ImportError`.

A related but separate guard is the `dimensions` parameter: it is honored for `openai` (Matryoshka-style truncation supported by `text-embedding-3-*` models) and `ollama` (forwarded to `langchain_ollama.OllamaEmbeddings`), but explicitly *not* supported for `sentence-transformers` — if set, `_build_sentence_transformers_ef` logs a warning and ignores it rather than raising, since SentenceTransformers models have a fixed native dimensionality that cannot be truncated the same way.

**Detailed description (continued) — provider-specific construction paths**: OpenAI construction (`_build_openai_ef`) reads the API key from environment variables (never from `config.yaml`, consistent with the project-wide convention that secrets live in `.env`), optionally forwards a custom `api_base` for OpenAI-compatible gateways, and optionally forwards `dimensions`. SentenceTransformers construction (`_build_sentence_transformers_ef`) resolves the compute device via `zettel.config.detect_device()` (auto/cpu/cuda) and logs the selected device. Ollama construction (`_build_ollama_ef`) requires the optional `langchain-ollama` package (raising a `RuntimeError` with an install hint if absent), normalizes the base URL, and wraps the resulting `OllamaEmbeddings` object in the custom `_LangChainOllamaChromaEF` adapter described in the next rule.

**Rule workflow**:
```
_build_embedding_fn(provider, model):
  try:
    if provider == "openai":            -> _build_openai_ef (raises RuntimeError if no key)
    elif provider == "sentence-transformers" -> _build_sentence_transformers_ef
    elif provider == "ollama"           -> _build_ollama_ef (raises RuntimeError if no langchain-ollama)
    else:
      if not allow_fallback: raise ValueError(unknown provider)
      else: warn, return None (-> Chroma's built-in default)
  except ImportError:
    if not allow_fallback: re-raise
    else: warn, return None
```

---

### Business Rule: Ollama Embedding Function Adapter and Registration

**Overview**:
ChromaDB has no native, first-class embedding function for Ollama models accessed via the `langchain_ollama` package, so `VectorIndex` defines and globally registers a custom adapter class, `_LangChainOllamaChromaEF`, that satisfies Chroma's `EmbeddingFunction` protocol while delegating actual embedding calls to `langchain_ollama.OllamaEmbeddings`.

**Detailed description**:
Chroma persists which embedding function produced a collection's vectors by name (`name()` classmethod) and config (`get_config()`/`build_from_config()`), so that reopening a persisted collection can reconstruct the correct callable without the caller needing to remember which embedding function was used originally. `_LangChainOllamaChromaEF` implements this full protocol: `__call__` (embed a batch of documents, returning `numpy.float32` arrays — Chroma expects numpy arrays, not plain lists), `embed_query` (delegates to the same `__call__`, since LangChain's document/query embedding split isn't needed here), `name()` (returns the literal string `"ollama"`), `is_legacy()` (`False`), `get_config()`/`build_from_config()` (round-trip model/dimensions/base_url so Chroma can reconstruct the adapter from persisted metadata alone, without the `VectorIndex` instance that originally created it), `validate_config_update()` (rejects an attempt to change the model on an existing collection — a second, narrower defense layered underneath the broader `EmbeddingSpaceMismatch` check), and `default_space()`/`supported_spaces()` (cosine as default, with L2/inner-product also declared supported).

Because Chroma discovers embedding functions by name when reconstructing a persisted collection (e.g. on process restart, when `get_or_create_collection` is called without explicitly passing `embedding_function=`), the adapter class must be registered with Chroma's global registry *before* any collection reconstruction happens. This is done via `_register_ollama_chroma_ef()`, called unconditionally at module import time (line 116) — a deliberate side effect on import, guarded by a broad `try/except Exception` that only logs at debug level, so that an incompatible Chroma version (where `register_embedding_function` might not exist or might change signature) degrades to "ollama collections can't be silently reconstructed" rather than crashing the entire import of `zettel.index` for users who never use Ollama.

The `base_url` normalization rule (`_normalize_ollama_base_url`) exists because Ollama's native API and its OpenAI-compatibility shim live at different paths on the same host (native at `/`, OpenAI-compatible at `/v1`), and `langchain_ollama.OllamaEmbeddings` expects the native base. Since operators may have historically configured the `/v1` suffix (e.g. copy-pasted from an OpenAI-compatible client setup), the component defensively strips a trailing `/v1` (and any trailing slash) before use, defaulting to `http://localhost:11434` when no URL is given.

**Rule workflow**:
```
Module import:
  _register_ollama_chroma_ef() -> chromadb.utils.embedding_functions.register_embedding_function(
                                     _LangChainOllamaChromaEF)   [best-effort, logs on failure]

VectorIndex(provider="ollama", ...):
  _build_ollama_ef(model) -> normalize base_url -> OllamaEmbeddings(model, base_url, dimensions?)
                           -> wrap in _LangChainOllamaChromaEF

Collection reconstruction (implicit, on later process start):
  Chroma reads collection metadata -> finds EF name "ollama" -> looks up registered class
  -> _LangChainOllamaChromaEF.build_from_config(persisted_config) -> adapter instance
```

---

### Business Rule: Metadata Sanitization for ChromaDB Compatibility

**Overview**:
ChromaDB's metadata storage only accepts scalar `str`/`int`/`float`/`bool` values per key; `_sanitize_metadata()` is the single funnel through which every metadata dict passed to any upsert method is coerced to satisfy this constraint before reaching the Chroma client.

**Detailed description**:
Domain metadata throughout the pipeline is naturally richer than Chroma's flat scalar model — for example, a permanent note's `tags` field is a Python list of strings, and various optional fields (e.g. `locator`, `source_id`) may be `None` when not applicable to a given note type. `_sanitize_metadata` handles three cases distinctly: `None` values are dropped entirely from the resulting dict (rather than being coerced to an empty string or the literal string `"None"`, which would pollute filters/queries against that key); values already in an accepted scalar type (`str`, `int`, `float`, `bool` — checked via `isinstance` against a tuple, so `bool` is preserved as `bool` even though in Python `bool` is a subclass of `int`) pass through unchanged; `list` values are joined into a single string with `", "` as a separator (documented codebase-wide convention, referenced explicitly in `CLAUDE.md`); and any other type (e.g. a nested `dict`, a custom object) is coerced via `str()` as a last resort, guaranteeing the function never raises regardless of what shape of metadata a caller passes in.

This sanitization is applied inside `upsert_source`, `upsert_chunk`, `upsert_permanent_note`, `upsert_literature_note`, and `upsert_moc` — i.e., unconditionally on every write path into every collection — meaning callers throughout the codebase (`harvester.py`, `connector.py`, `gardener.py`, `gardener_hub.py`, `sync.py`, `rebuild.py`, `review.py`) are free to pass natural Python metadata (including lists like `tags` or `authors`) without needing to sanitize it themselves, centralizing this Chroma-specific constraint in exactly one place.

**Rule workflow**:
```
upsert_*(id, text, metadata):
  safe_meta = _sanitize_metadata(metadata)
    for k, v in metadata.items():
      if v is None: skip
      elif isinstance(v, (str, int, float, bool)): keep as-is
      elif isinstance(v, list): join(", ", str(x) for x in v)
      else: str(v)
  collection.upsert(ids=[id], documents=[text], metadatas=[safe_meta])
```

---

### Business Rule: Content-Addressed Deduplication via `existing_ids`

**Overview**:
`existing_ids()` lets pipeline phases (chiefly the harvester and `rebuild.py`'s reindex routines) cheaply skip re-embedding chunks or notes whose content-addressed identifier is already present in a target collection, avoiding both wasted embedding-API cost and duplicate vectors.

**Detailed description**:
Chunk IDs and note IDs throughout the system are content-addressed (per `CLAUDE.md`: chunk ids are `source_id::chapter_id::short_hash`) — identical content produces an identical id. This means that "is this content already indexed?" reduces to a single `get(ids=[...])` existence check rather than requiring a content-similarity search. `existing_ids(collection_name, ids)` resolves the collection by name via a lookup dict (`sources`/`chunks`/`permanent_notes`/`mocs`/`literature_notes` mapped to the corresponding cached attribute), raising `ValueError` for any unrecognized collection name so a typo'd caller fails immediately rather than silently returning an empty set.

A specific defensive measure is deduplicating the *input* id list before calling Chroma's `get()`: ChromaDB raises a `DuplicateIDError`-style failure if the same id appears twice in a single `get()` call, but callers (e.g. `rebuild.py`'s `_reindex_chunks`, or harvester's chunk-persistence loop) may legitimately need to check a batch where two logically distinct chunks hash to the same content id (e.g. a duplicated paragraph appearing twice in different chapters). `existing_ids` uses `dict.fromkeys(ids)` to deduplicate while preserving order before querying, then returns the resulting set of ids found — this is explicitly covered by `test_existing_ids_dedupes_duplicate_query_ids` in the test suite. Both `rebuild.py`'s reindex functions and `harvester.py` rely on this method as their sole "was this already embedded" gate.

**Rule workflow**:
```
existing_ids(collection_name, ids):
  if not ids: return set()
  collection = lookup(collection_name) or raise ValueError
  unique_ids = dict.fromkeys(ids).keys()      [dedupe, order-preserving]
  got = collection.get(ids=unique_ids)
  return set(got["ids"])
```

---

### Business Rule: Windows-Safe Resource Release for VACUUM

**Overview**:
`close()` and `vacuum()` exist specifically to release every Python reference the process holds into the underlying `chroma.sqlite3` file before attempting a `VACUUM`, because Windows enforces exclusive file locks that a lingering SQLite connection (held indirectly via Chroma's client/collection objects) would otherwise block.

**Detailed description**:
On POSIX systems, SQLite's file locking is advisory and a `VACUUM` can typically proceed even with other loosely-held references around; on Windows, an open file handle held by another connection (even one nominally "idle") can cause a `VACUUM` (which needs to rewrite the entire database file) to fail or hang. `close()` sets every cached collection handle (`sources`, `chunks`, `permanent`, `mocs_col`, `literature`), the `client` itself, and `embedding_fn` to `None`, dropping all Python references to Chroma's internal SQLite connection object. `vacuum()` then calls `close()`, explicitly forces a garbage-collection pass (`gc.collect()`) to ensure any C-extension-backed SQLite connection object is actually finalized (relying purely on refcounting can be insufficient when C extensions or circular references are involved), opens its own fresh `sqlite3.connect()` to `chroma.sqlite3`, runs `PRAGMA wal_checkpoint(TRUNCATE)` (to fold the write-ahead log back into the main file before compacting) followed by `VACUUM`, and closes that connection.

The docstring explicitly scopes what this operation does and does not achieve: it reclaims free pages in Chroma's *SQLite metadata store* (ids, documents, metadata), but does **not** rebuild the HNSW vector-index segment directories that Chroma stores separately on disk — those would require Chroma's own (external) compaction tooling, which this component does not attempt to invoke. This is called out as "safe for logical content" — it never deletes any remaining embeddings, only reclaims space already freed by prior `delete()` calls. This method is invoked by `review.purge_rejected` and `purge_source.py` after bulk deletions, mirroring the `state.db` VACUUM pattern described in `CLAUDE.md` for `zettel purge-rejected` / `zettel delete-source`, and by `--no-compact` CLI flags that skip it.

**Rule workflow**:
```
vacuum():
  close()                              [drop all Python refs: collections, client, embedding_fn]
  gc.collect()                         [force finalization of any lingering SQLite connection]
  if not chroma.sqlite3 exists: return None
  conn = sqlite3.connect(chroma.sqlite3)
  conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
  conn.execute("VACUUM")
  conn.commit(); conn.close()
  return db_path
```

---

### Business Rule: Embedding Usage/Cost Attribution

**Overview**:
Every embedding operation (upsert or query) that goes through `VectorIndex` estimates its token count and USD cost and attributes it to whichever `CostTracker` is currently active for the running process, but does nothing when no tracker is bound (e.g. outside a `runs`-tracked CLI command).

**Detailed description**:
The project's cost-accounting model (documented in `CLAUDE.md` under "pricing.py / usage.py") uses Python `contextvars` to bind a `CostTracker` for the duration of a pipeline run (`harvest`, `extract`, `connect`, `garden`, `review`, `ask`, `article`), so that costs incurred deep inside library code (like this component) can be attributed to the right run/source without every intermediate function needing to thread a tracker parameter through its signature. `_record_embed_usage()` is the private helper every public upsert/query method funnels through: it first checks `zettel.usage.get_tracker()`, and if `None` (no run in progress — e.g. ad hoc test or script usage), it returns immediately without doing any estimation work, avoiding both unnecessary computation and any chance of an unbound-tracker `AttributeError`.

When a tracker *is* bound, token count is estimated via `zettel.pricing.estimate_embed_tokens` (a rough `len(text) // 4` heuristic, not an actual tokenizer call — cheap and provider-agnostic at the cost of some precision) and cost via `estimate_embed_cost(model, tokens, provider=...)`, which returns `0.0` outright for `ollama`/`sentence-transformers` providers or any model heuristically detected as "local" (`_is_local_model`), and otherwise looks up per-token pricing via LiteLLM's public price map. The resulting `(model, tokens, cost_usd, label, step, total, kind)` tuple is passed to `zettel.usage.record_embed`, which attributes it to `get_source_id()` (another contextvar, set by the calling pipeline phase) if the caller didn't pass an explicit `source_id`. Every public method that touches the embedding function calls this exactly once per document embedded — including query-time embeddings (`query_similar_notes`, `find_similar_chunks`), which is a deliberate choice: query embeddings cost real API tokens too and are not free to omit from cost totals, even though they don't persist a new vector.

**Rule workflow**:
```
upsert_X(...) / query_X(...):
  <perform Chroma call using embedding_fn>
  _record_embed_usage(text, label=..., step=?, total=?, kind=?):
    if get_tracker() is None: return   [no-op outside a tracked run]
    tokens = estimate_embed_tokens(text)          [len(text)//4]
    cost = estimate_embed_cost(model, tokens, provider)  [0.0 for local/ollama/ST]
    record_embed(model, tokens, cost, label, step, total, kind)
      -> require_tracker().record_embed(...)      [attributes to active CostTracker]
```

---

## 4. Component Structure

```
zettel/index.py                          # entire component — single file, ~766 lines
├── Module-level constants
│   ├── COL_SOURCES / COL_CHUNKS / COL_PERMANENT / COL_MOCS / COL_LITERATURE  # collection names
│   ├── _ALL_COLLECTIONS                 # list of the 5 names above
│   ├── _DEFAULT_OLLAMA_URL              # "http://localhost:11434"
│   └── _SUPPORTED_PROVIDERS             # ("openai", "sentence-transformers", "ollama")
├── Free functions — Ollama support
│   ├── _normalize_ollama_base_url()     # strips legacy /v1 suffix
│   └── _register_ollama_chroma_ef()     # registers the adapter class with Chroma at import time
├── _LangChainOllamaChromaEF             # Chroma EmbeddingFunction adapter over langchain_ollama
│   ├── __call__ / embed_query           # actual embedding calls (numpy float32 output)
│   ├── name / is_legacy / get_config    # Chroma EF protocol (identity)
│   ├── build_from_config (staticmethod) # reconstruct adapter from persisted collection metadata
│   ├── validate_config_update           # rejects model change on existing collection
│   ├── validate_config (staticmethod)   # rejects missing 'model' in config
│   └── default_space / supported_spaces # cosine default; l2/ip also declared
├── EmbeddingSpaceMismatch(Exception)    # raised when stored vs. configured embedding identity differs
├── Free functions — embedding-identity helpers
│   ├── _format_space_id()               # "provider/model@Nd" human-readable id
│   ├── _parse_dimensions()              # tolerant int parse for metadata values
│   ├── peek_stored_embedding_identity() # read markers from a Chroma path without a full VectorIndex
│   └── _identity_from_client()          # scan the 5 collections' metadata for the first marker found
├── VectorIndex                          # the public component class
│   ├── __init__                         # client open, EF build, identity check, ensure collections
│   ├── close() / vacuum()               # Windows-safe resource release + SQLite VACUUM
│   ├── _build_embedding_fn / _build_openai_ef / _build_sentence_transformers_ef / _build_ollama_ef
│   ├── _collection_metadata()           # provider/model/dimensions marker dict
│   ├── get_stored_embedding_identity() / embedding_space_matches()
│   ├── _delete_all_collections() / _get_or_create() / _ensure_collections() / reset_collection()
│   ├── — Sources —      upsert_source()
│   ├── — Chunks —       upsert_chunk() / delete_chunks() / existing_ids()
│   ├── — Permanent —    upsert_permanent_note() / query_similar_notes() /
│   │                    get_all_permanent_embeddings() / count_permanent_notes()
│   ├── — Literature —   upsert_literature_note() / delete_literature_notes()
│   ├── — MOCs —         upsert_moc() / delete_mocs()
│   ├── — Cross-cutting — delete_sources() / delete_permanent_notes() / find_similar_chunks()
│   ├── _record_embed_usage()            # cost/usage attribution (private)
│   └── embed_texts()                    # raw embedding of arbitrary text (used by gardener_assign)
└── _sanitize_metadata()                 # module-level, ChromaDB metadata type coercion
```

There is no dedicated subdirectory or package for this component — it is intentionally a single, self-contained module, consistent with the project's convention of one file per pipeline concern (`harvester.py`, `extractor.py`, `connector.py`, etc., are siblings at the same `zettel/` package level).

## 5. Dependency Analysis

```
Internal Dependencies (imports FROM zettel.index, by other modules):
cli.py            -> VectorIndex, EmbeddingSpaceMismatch, peek_stored_embedding_identity
web_app.py        -> VectorIndex
ask.py            -> VectorIndex (type hint only, TYPE_CHECKING)
article.py / article_graph.py -> VectorIndex (type hint only)
extractor.py      -> VectorIndex; calls idx.query_similar_notes()
connector.py      -> VectorIndex; calls idx.upsert_permanent_note()
gardener.py       -> VectorIndex; calls idx.count_permanent_notes(), get_all_permanent_embeddings(),
                     delete_mocs(), upsert_moc()
gardener_hub.py   -> VectorIndex; calls idx.delete_mocs(), upsert_moc()
gardener_assign.py-> VectorIndex; calls idx.embed_texts()
review.py         -> VectorIndex; calls idx.upsert_literature_note(), delete_chunks(),
                     delete_literature_notes(), vacuum()
rebuild.py        -> VectorIndex, COL_SOURCES/COL_CHUNKS/COL_PERMANENT/COL_MOCS/COL_LITERATURE;
                     calls idx.reset_collection(), existing_ids(), upsert_source/chunk/permanent/moc/literature
retrieval.py      -> VectorIndex (type hint); calls idx.query_similar_notes()
sync.py           -> VectorIndex; calls idx.upsert_source(), upsert_permanent_note(), upsert_moc()
harvester.py      -> VectorIndex; calls idx.delete_chunks(), upsert_source(), find_similar_chunks(),
                     existing_ids(), upsert_chunk()
purge_source.py   -> VectorIndex; calls idx.delete_permanent_notes(), delete_chunks(),
                     delete_literature_notes(), delete_sources(), vacuum()

Internal Dependencies (imports BY zettel.index, lazy/deferred inside methods):
zettel.llm        -> clip_text()                      (log-preview truncation)
zettel.pricing    -> estimate_embed_cost(), estimate_embed_tokens()
zettel.usage      -> get_tracker(), record_embed()
zettel.config     -> detect_device()                  (sentence-transformers device selection)

External Dependencies:
- chromadb (PersistentClient, Settings, embedding_functions: OpenAIEmbeddingFunction,
  SentenceTransformerEmbeddingFunction, register_embedding_function) — vector store engine
- langchain_ollama.OllamaEmbeddings (optional, lazy-imported)  — Ollama embedding client
- numpy (lazy-imported inside _LangChainOllamaChromaEF.__call__) — float32 vector conversion
- litellm (lazy-imported inside zettel.pricing, not directly by index.py) — price lookups
- Filesystem: chroma.sqlite3 + HNSW segment directories under cfg.chroma_path
```

Notably, `zettel.index` imports nothing else from within the project at module scope — all intra-project imports (`zettel.llm`, `zettel.pricing`, `zettel.usage`, `zettel.config`) are deferred to inside method bodies. This keeps the module importable (and its Ollama-EF-registration side effect triggerable) even in contexts where those other modules' own dependencies aren't yet available, and avoids import cycles (e.g. `zettel.config` importing things that might eventually depend back on indexing).

## 6. Afferent and Efferent Coupling

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|-------------------|-------------------|
| `VectorIndex` | 14 (cli, web_app, ask, article, article_graph, extractor, connector, gardener, gardener_hub, gardener_assign, review, rebuild, retrieval, sync, harvester, purge_source — 16 distinct modules) | 5 (chromadb, langchain_ollama, numpy, zettel.llm, zettel.pricing/zettel.usage, zettel.config) | High |
| `_LangChainOllamaChromaEF` | 1 (`VectorIndex._build_ollama_ef`, plus Chroma's internal registry lookup) | 2 (`langchain_ollama.OllamaEmbeddings`, `numpy`) | Medium |
| `EmbeddingSpaceMismatch` | 3 (cli.py `_get_idx`/`reindex`/`doctor`, `VectorIndex.__init__`, `VectorIndex._get_or_create`) | 1 (`_format_space_id`) | Medium |
| `_sanitize_metadata` | 5 (every `upsert_*` method within `VectorIndex`) | 0 | Low |
| `peek_stored_embedding_identity` | 2 (cli.py `reindex`, cli.py `doctor`) | 1 (`_identity_from_client`) | Low |

`VectorIndex` itself is the highest-risk node in the whole project's coupling graph by afferent count: nearly every pipeline phase module depends on it directly, meaning any breaking change to its public method signatures has wide blast radius. This is expected and intentional for a Repository-pattern-style data-access class, but it does mean this file warrants disproportionate test rigor relative to its size.

## 7. Endpoints

Not applicable — `VectorIndex` is an in-process Python library component with no REST/GraphQL/gRPC surface of its own. It is invoked exclusively through direct Python calls from other modules within the same process (CLI commands via `zettel.cli`, or the FastAPI web worker via `zettel.web_app`).

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|----------------|
| ChromaDB (`chromadb.PersistentClient`) | Embedded library / local persistent store | Vector storage, ANN search, collection lifecycle | In-process API over local SQLite + HNSW segment files | Python objects (ids/documents/metadatas/embeddings as lists/numpy arrays) | Chroma `ValueError` ("Embedding function conflict") translated to domain `RuntimeError`; other exceptions largely propagate uncaught |
| OpenAI Embeddings API | External Service | Compute embeddings for `provider=openai` | HTTPS/REST (via `chromadb.utils.embedding_functions.OpenAIEmbeddingFunction`) | JSON (delegated to Chroma's OpenAI EF implementation) | Missing API key raises `RuntimeError` at construction time (fail fast, no per-call retry logic in this component) |
| Ollama server (native API) | External Service (local/remote) | Compute embeddings for `provider=ollama` via `langchain_ollama` | HTTP to `base_url` (native Ollama API, `/v1` suffix stripped) | JSON (delegated to `langchain_ollama.OllamaEmbeddings`) | No retry/circuit-breaker in this component; failures surface as whatever `OllamaEmbeddings.embed_documents` raises |
| SentenceTransformers (local model) | Local library / model weights | Compute embeddings for `provider=sentence-transformers`, offline | In-process (no network) | numpy/tensor vectors | `ImportError` on missing package handled per `allow_fallback` |
| `chroma.sqlite3` file | Local filesystem | Chroma's metadata/document store persisted alongside HNSW segments | SQLite (accessed directly for `VACUUM`, otherwise only through the Chroma client) | SQLite rows | `vacuum()` guards with an existence check (`db_path.exists()`) before connecting |
| `zettel.usage` CostTracker (contextvar) | Internal cross-cutting service | Attribute embedding token/cost usage to the active pipeline run | In-process function calls | Python dataclasses (`UsageEvent`) | No-op silently when no tracker is bound — never raises |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Repository / Data Access Object | `VectorIndex` | index.py:203-751 | Single abstraction point over ChromaDB; all 16 consumer modules interact with vectors only through this class, never `chromadb` directly |
| Adapter | `_LangChainOllamaChromaEF` | index.py:35-103 | Bridges `langchain_ollama.OllamaEmbeddings`'s interface to ChromaDB's `EmbeddingFunction` protocol |
| Factory Method | `_build_embedding_fn` + `_build_openai_ef` / `_build_sentence_transformers_ef` / `_build_ollama_ef` | index.py:290-374 | Selects and constructs the correct embedding function implementation based on `embedding_provider` string |
| Fail-Fast / Guard Clause | `_build_embedding_fn`, `_build_openai_ef`, `__init__`'s embedding-space check | index.py:290-322, 329-335, 234-251 | Prevents silent data corruption (wrong provider, wrong vector space) by raising early instead of degrading gracefully |
| Domain-Specific Exception | `EmbeddingSpaceMismatch` | index.py:119-142 | Wraps a low-level state mismatch in a rich, actionable exception carrying both stored and current identity, consumed by CLI-layer UX |
| Module-level Plugin Registration | `_register_ollama_chroma_ef()` called at import time | index.py:106-116 | Ensures Chroma can reconstruct persisted Ollama-backed collections without the registering `VectorIndex` instance being present |
| Sanitization / Boundary Coercion | `_sanitize_metadata` | index.py:754-766 | Centralizes type coercion at the exact boundary where domain metadata crosses into ChromaDB's constrained storage model |
| Lazy Import | `zettel.llm`, `zettel.pricing`, `zettel.usage`, `zettel.config`, `numpy`, `langchain_ollama` imported inside functions/methods, not at module top | throughout | Avoids import-time cost/cycles for providers/features not in use; keeps optional dependencies (`langchain-ollama`) truly optional |
| Cross-Cutting Concern via Context Variables | `_record_embed_usage` reading `zettel.usage.get_tracker()` | index.py:699-728 | Attributes cost/usage without threading a tracker parameter through every public method signature |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|----------------|-------|--------|
| High | `_delete_all_collections` / `reset_mismatched` path | An embedding-space mismatch on *any single collection* triggers deletion of **all five** collections, not just the affected one | If collections could ever drift independently (e.g. a partial write, or future code that stamps metadata differently per collection), unrelated collections lose their vectors unnecessarily; currently mitigated only by the invariant that all five are always created/reset together |
| Medium | `_build_embedding_fn` broad `except ImportError` | Only `ImportError` is caught for provider construction fallback; other exceptions from a provider constructor (e.g. a malformed `base_url`, an SDK-internal `TypeError`) propagate raw without the same fail-fast messaging quality applied to the known cases | Operators may see a less actionable low-level stack trace for edge-case misconfigurations instead of a domain-specific error message |
| Medium | `_register_ollama_chroma_ef` swallow-all `except Exception` | Any failure to register the adapter (e.g. Chroma API changes) is logged only at `debug` level, which is invisible by default | A future Chroma upgrade could silently break Ollama collection reconstruction (working at write time via direct construction, but failing when Chroma tries to reconstruct the EF from persisted metadata on process restart) with no visible warning to the operator |
| Medium | `estimate_embed_tokens` heuristic | Token estimate is `len(text) // 4`, not an actual tokenizer call, for every provider including OpenAI, where a real tokenizer (`tiktoken`) is available | Cost estimates in `runs`/SRC frontmatter can drift from actual OpenAI billing, particularly for non-English/PT-BR text where the chars-per-token ratio differs from the English-calibrated heuristic |
| Medium | `query_similar_notes` / `find_similar_chunks` | No pagination or cap on `n_results` beyond `collection.count()`; for very large corpora, an unusually high `n_results` request together with a large document field could return heavy payloads with no truncation | Potential memory/latency spike on large vaults; no explicit guard observed in this component (though callers currently pass small, config-bounded `topk` values) |
| Low | `_build_sentence_transformers_ef` | Silently ignores `embedding.dimensions` for this provider (logs a warning) rather than raising, unlike the strict fail-fast philosophy applied elsewhere in the same method | Inconsistent strictness: a misconfigured `dimensions` value under `sentence-transformers` is tolerated with only a log line, while an unknown provider string is a hard error |
| Low | `get_all_permanent_embeddings` | Loads **all** permanent-note embeddings into memory in one `get(include=["embeddings"])` call, with no batching | Acceptable at current vault scale (used by `gardener.py` for clustering) but does not scale indefinitely; no chunked/streaming retrieval path exists |
| Low | No retry/backoff for embedding provider calls | Neither OpenAI, Ollama, nor SentenceTransformers embedding calls are wrapped in retry logic within this component (contrast with `CLAUDE.md`'s mention of a circuit-breaker pattern as an example elsewhere in the ecosystem) | A transient network blip during a long harvest run surfaces as a hard failure requiring a manual re-run, rather than an automatic retry |

## 11. Test Coverage Analysis

Test files located: `tests/test_index.py` (the component's own dedicated test file, 237 lines / 20 test functions). No other test file under `tests/` imports `zettel.index` directly by name (confirmed via project-wide search restricted to the `zettel` package and `tests/` — matches for `VectorIndex(` and `embedding_provider`/`embedding_model` were confined to `zettel/index.py`, `zettel/cli.py`, `zettel/config.py`, `zettel/web_app.py`, `zettel/web.py`, `zettel/sync.py`, `zettel/state.py`, `zettel/connector.py`). Coverage of `VectorIndex`'s CRUD methods (`upsert_chunk`, `upsert_permanent_note`, `query_similar_notes`, `find_similar_chunks`, `delete_*`) instead comes indirectly through integration-style tests of consumer modules (e.g. harvester/connector/gardener/retrieval test files), which are out of scope for this component-level report but represent real, if indirect, exercise of this component's write/query paths.

| Component Area | Unit Tests (direct, in test_index.py) | Integration Tests (indirect, via consumers) | Coverage | Test Quality |
|-----------|------------|-------------------|----------|--------------|
| Embedding provider fail-fast (`_build_openai_ef`, unknown provider) | 2 (`test_fail_fast_without_api_key`, `test_unknown_provider_fails_fast`) | None observed directly | Good for the two explicit failure paths | Precise `pytest.raises` assertions with message-substring matching; does not test the `sentence-transformers` `ImportError` fallback path or OpenAI's `base_url`/`dimensions` kwarg forwarding |
| Embedding-space identity (`get_stored_embedding_identity`, `embedding_space_matches`, `EmbeddingSpaceMismatch`) | 5 (`test_collection_metadata_marks_provider`, `test_peek_empty_store`, `test_embedding_space_matches_empty_and_same`, `test_embedding_space_mismatch_raises`, `test_embedding_dimensions_mismatch_raises`) | Exercised in production via `cli._get_idx` / `reindex` / `doctor`, not covered by dedicated CLI tests found in this scan | Very good — covers empty store, match, provider/model mismatch, and dimensions-only mismatch as distinct cases | Assertions check both `stored_*` and `current_*` fields on the raised exception, not just that it raised |
| `reset_mismatched` full-collection reset | 1 (`test_embedding_space_reset_mismatched`) | None observed | Adequate for the happy path | Verifies old vectors are gone (`count() == 0`) and the new identity is stamped; does not verify that *other*, non-conflicting collections are also reset (an assumption implicit in the "Technical Debt" table above) |
| `existing_ids` | 2 (`test_existing_ids_empty_and_unknown_collection`, `test_existing_ids_dedupes_duplicate_query_ids`) | Indirectly via `rebuild.py`/`harvester.py` reindex logic | Good | Covers empty input, unknown-collection `ValueError`, and the duplicate-id dedup edge case explicitly |
| Ollama adapter (`_LangChainOllamaChromaEF`, `_normalize_ollama_base_url`) | 6 (`test_normalize_ollama_base_url_strips_v1`, `test_ollama_embedding_fn_builds_without_server`, `test_ollama_dimensions_forwarded_via_langchain`, `test_ollama_base_url_strips_v1_for_native_client`, `test_ollama_build_from_config_roundtrip`) | None observed | Very good for construction/config-forwarding; no test exercises `validate_config_update`'s rejection of a model change, nor `default_space`/`supported_spaces` | Uses `MagicMock`/`patch` on `langchain_ollama.OllamaEmbeddings` — tests never require a live Ollama server, keeping them fast and CI-safe |
| `_sanitize_metadata` | 1 (`test_sanitize_metadata_types`) | Implicitly via every upsert-path test elsewhere | Good | Single test covers all five branches (str/int/float/bool pass-through, None-drop, list-join, arbitrary-object stringify) in one assertion block |
| `upsert_source` / `upsert_chunk` / `upsert_permanent_note` / `upsert_literature_note` / `upsert_moc` (direct) | 0 dedicated; `upsert_chunk` exercised only as setup inside `test_existing_ids_dedupes_duplicate_query_ids` and `test_embedding_space_reset_mismatched` | Indirect, via harvester/connector/review/gardener/sync test suites (not enumerated here — outside this component's boundary) | Weak *direct* coverage — no test in `test_index.py` asserts on `upsert_source`, `upsert_permanent_note`, `upsert_literature_note`, or `upsert_moc` in isolation | Risk: a regression in metadata handling or cost-recording specific to one of these five near-identical methods could pass `test_index.py` entirely and only surface in a consumer module's test, complicating root-cause attribution |
| `query_similar_notes` / `find_similar_chunks` | 0 | Indirect, via `retrieval.py`/`extractor.py`/`harvester.py` consumer tests | Weak *direct* coverage | No test in this file constructs a populated `permanent`/`chunks` collection and asserts on `query_similar_notes`'s `exclude_id` filtering, result truncation (`output[:n_results]`), or `find_similar_chunks`'s multi-query flattening logic |
| `close()` / `vacuum()` | 0 | None observed in this scan | Not covered | No test verifies the Windows-safe resource-release sequence, the `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM` behavior, or the `db_path.exists()` early-return |
| `_record_embed_usage` / cost attribution | 0 | Indirectly plausible via `zettel.usage`/`zettel.pricing` test suites and pipeline-phase tests, not confirmed in this scan | Not directly covered in `test_index.py` | No test verifies that `get_tracker() is None` produces a true no-op, nor that the correct `(model, tokens, cost, label, step, total, kind)` tuple reaches `record_embed` |
| `embed_texts` | 0 | Indirectly via `gardener_assign.py`'s own tests (not confirmed in this scan) | Not directly covered | No direct assertion on `RuntimeError` when `embedding_fn is None`, nor on the `list(v) for v in vectors` conversion |

Overall assessment: the test suite is deliberately and effectively concentrated on the component's highest-risk surface — the embedding-space integrity machinery (identity marking, mismatch detection, provider construction fail-fast behavior, and the Ollama adapter) — which aligns with where this report identifies the component's most consequential business rules. The CRUD/query methods that are structurally simple thin wrappers over Chroma calls (`upsert_source`, `upsert_moc`, `delete_*`, `query_similar_notes`, `find_similar_chunks`) have little to no *direct* unit coverage in `test_index.py`, relying instead on incidental exercise through other modules' test suites; `close()`/`vacuum()` and the cost-attribution path (`_record_embed_usage`) appear to have no coverage located in this scan at all.
