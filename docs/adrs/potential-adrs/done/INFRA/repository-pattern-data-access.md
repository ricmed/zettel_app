# Potential ADR: Repository Pattern for Data Access (StateDB and VectorIndex)

**Module**: INFRA (Data Access)  
**Category**: Architectural Pattern  
**Priority**: Must Document (Score: 115)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The project isolates all data access through two repository classes:
1. **StateDB** (`state.py`) — Single gateway for all SQLite operations (sources, chunks, concepts, notes, MOCs, job queue, cost tracking)
2. **VectorIndex** (`index.py`) — Single gateway for all ChromaDB operations (5 collections: sources, chunks, permanent_notes, mocs, literature_notes)

No module directly constructs database connections or calls Chroma collection methods. Instead, modules receive a `StateDB` and `VectorIndex` instance injected at composition time (in `cli.py` and `web_app.py`), and call methods like `db.get_sources()`, `db.upsert_chunk()`, `index.upsert_chunk()`, etc.

This creates a clean abstraction boundary: the rest of the codebase is agnostic to SQLite's SQL syntax or ChromaDB's API — it just calls repository methods. If the underlying store changes, only StateDB/VectorIndex needs rewriting.

**Introduced**: Foundational; StateDB and VectorIndex classes have been stable, suggesting this pattern was chosen at architecture inception.

**Modified**: Stable; methods evolve to support new features (e.g., `get_web_dashboard()` added when web UI was implemented), but the pattern itself unchanged.

---

## Why This Might Deserve an ADR

- **Impact**: Every module (harvester, extractor, review, connector, gardener, sync, ask, article, web_app) depends on StateDB and/or VectorIndex. 22+ afferent dependencies on `state.py` and 21+ on `index.py` per mapping.
- **Trade-offs Visible**:
  - **Abstraction**: Repository hides SQL/API details from callers, enabling easy swaps (e.g., PostgreSQL for SQLite).
  - **Consistency**: All database access goes through the same methods, enforcing consistent error handling, logging, and transaction semantics.
  - **Testing**: Repositories can be mocked/stubbed for unit tests; callers don't need real databases.
  - **Performance**: Repository methods are coarse-grained (e.g., `get_sources()` returns all sources); fine-grained queries require adding new methods (extension cost).
  - **Coupling**: All callers depend on the StateDB/VectorIndex class signatures; adding a parameter to a method affects all callers.
- **Cost to Change**: Switching to a different pattern (e.g., direct SQL via ORM, no abstraction) would require rewriting:
  - StateDB and VectorIndex class definitions
  - All 8+ modules that call `db.*` and `index.*` methods (not the methods themselves, but how they construct/pass repositories)
- **Team Knowledge**: Anyone working on data access must understand:
  - The StateDB class and its 30+ methods (`get_sources`, `upsert_chunk`, `get_notes_by_origin`, etc.)
  - The VectorIndex class and its 10+ methods (`upsert_chunk`, `search`, etc.)
  - How repositories are injected (via `_get_db()` and `_get_idx()` in cli.py)
  - Why direct SQL/Chroma API calls are forbidden (violates the abstraction)
- **Temporal Context**: Stable for 18+ months; no drift. Recent additions (web_jobs methods, get_web_dashboard) follow the pattern.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/state.py`](../../../zettel/state.py) - StateDB class (1725 lines)
  - 30+ methods: `get_sources()`, `upsert_chunk()`, `get_chunks_by_source()`, `get_notes_by_origin()`, `upsert_note()`, `get_web_dashboard()`, etc.
  
- [`zettel/index.py`](../../../zettel/index.py) - VectorIndex class (766 lines)
  - 10+ methods: `upsert_chunk()`, `upsert_note()`, `search()`, `get_document_by_id()`, etc.

- [`zettel/cli.py`](../../../zettel/cli.py) - Composition root
  - Lines ~50-100: `_get_db()` and `_get_idx()` factory functions
  - Every CLI command receives `(db, idx) = _get_db(), _get_idx()`

### Code Evidence
```python
# From zettel/state.py (repository abstraction):
class StateDB:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(...)
        # ... initialize schema
    
    def get_sources(self, limit: int = 100) -> list[SourceRecord]:
        """Fetch all sources. Repository method — no raw SQL in callers."""
        rows = self.conn.execute("SELECT * FROM sources LIMIT ?", (limit,))
        return [SourceRecord(**row) for row in rows]
    
    def upsert_chunk(self, source_id: str, text: str, ...) -> str:
        """Insert or update a chunk. Caller doesn't know it's SQL."""
        chunk_id = generate_ulid()
        self.conn.execute(
            "INSERT OR REPLACE INTO chunks (...) VALUES (...)",
            (...)
        )
        return chunk_id

# From zettel/index.py (repository abstraction):
class VectorIndex:
    def __init__(self, chroma_path: Path, embedding_fn):
        self.client = chromadb.PersistentClient(path=str(chroma_path))
        self.collections = {
            'chunks': self.client.get_or_create_collection(...),
            'permanent_notes': ...,
            ...
        }
    
    def upsert_chunk(self, chunk_id: str, text: str, embedding: list[float], ...):
        """Upsert chunk to ChromaDB. Caller doesn't know it's Chroma."""
        self.collections['chunks'].upsert(
            ids=[chunk_id],
            documents=[text],
            embeddings=[embedding],
            metadatas=[{...}]
        )
    
    def search(self, query: str, topk: int = 10) -> list[SearchResult]:
        """Search across all collections. Caller sees results, not API."""
        ...

# From zettel/cli.py (composition and dependency injection):
def _get_db() -> StateDB:
    """Factory: create and return StateDB instance."""
    config = load_config()
    return StateDB(config.state_db_path)

def _get_idx() -> VectorIndex:
    """Factory: create and return VectorIndex instance."""
    config = load_config()
    return VectorIndex(
        chroma_path=config.chroma_path,
        embedding_fn=get_embedding_function(config.embedding),
    )

@app.command()
def harvest(...):
    """CLI command receives injected repositories."""
    db = _get_db()
    idx = _get_idx()
    harvester.run(db=db, idx=idx, ...)  # Pass repositories to pipeline module
```

### Impact Analysis
- **Introduced**: Foundational (StateDB and VectorIndex present from early architecture)
- **Modified**: Stable; methods added to support new features (web_jobs, MOCs) but pattern unchanged
- **Last change**: Recent methods (`get_web_dashboard()`, web_jobs methods) follow the pattern
- **Files affected**: cli.py (composes), every phase module (harvester, extractor, review, connector, gardener, sync, ask, article, web_app)
- **Scope**: Large (universal across all modules; foundational to architecture)

### Method Coverage
StateDB has ~30 public methods covering:
- Source/chapter/chunk CRUD
- Concept and note persistence
- MOC persistence
- Asset management
- LLM cache (SQLite `llm_cache` table)
- Note connections (graph edges)
- Pipeline run tracking
- Web job queue operations
- FTS5 search

VectorIndex has ~10 methods covering:
- Chunk/note/MOC embedding upsert
- Collection search
- Document retrieval by ID
- Collection reset

---

## Questions to Address in ADR (if created)

- Why two separate repositories (StateDB and VectorIndex) instead of a unified repository?
  - Answer likely: Different stores (SQLite vs. ChromaDB) have incompatible APIs; separation respects that.
- Should the repositories share a common interface or base class?
  - Currently: No; they're independent classes. A shared `Repository` interface could enforce consistency.
- How are schema migrations handled? (Currently: `_SCHEMA_SQL` is executed at init; no versioning system visible.)
- Why are StateDB methods sometimes named `get_*` (plural) and sometimes singular (e.g., `upsert_note` singular)? (Inconsistency?)

## Related Potential ADRs
- SQLite with WAL + FTS5 (underlying store for StateDB)
- ChromaDB Embedded Vector Store (underlying store for VectorIndex)
- Dual-Store Persistence (StateDB + VectorIndex have no transactional guarantee)

## Additional Notes
- The `_get_db()` and `_get_idx()` factory functions are in `cli.py`; the web app has its own initialization in `web_app.py`. Both follow the same pattern.
- Tests often create temporary StateDB instances (in test fixtures) rather than mocking; real database tests, not unit tests with mocks.
- No visible use of abstract base classes or protocols to enforce the repository contract; reliance on duck typing and convention.
