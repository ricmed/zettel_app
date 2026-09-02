# ADR-XXX: Repository Pattern for Data Access (StateDB and VectorIndex)

**Status:** Accepted
**Date:** 2024-08-30 (approximate; see gap below)
**Related to:**
- [ADR-XXX: SQLite with WAL Mode and FTS5 as Primary Persistence Layer](./ADR-001-sqlite-wal-fts5-primary-persistence.md)
- [ADR-XXX: FastAPI Server-Rendered Web Interface (No SPA)](../WEB/ADR-022-fastapi-server-rendered-jinja2.md)
- [ADR-XXX: Typer and Rich as CLI Framework](../CLI/ADR-026-typer-rich-cli-framework.md)

## Context and Problem Statement

The pipeline is composed of many independent phases (harvest, extract, review, connect, garden, sync, ask, article) plus a web application, all of which need to read and write persistent state and vector embeddings. Two fundamentally different stores are involved: SQLite for relational/state data and ChromaDB for embeddings. Without a shared abstraction, every module would need direct knowledge of SQL syntax and the ChromaDB collection API, leading to duplicated connection setup, inconsistent error handling, and tight coupling between business logic and storage technology.

The project needed a boundary that lets 8+ independent modules and the web app perform data access consistently, without each one owning its own connection lifecycle or query logic, and without changes to the underlying store rippling across the whole codebase.

[NEEDS INPUT: Confirm the actual date this pattern was adopted — evidence only shows it as foundational/stable for 18+ months with no first-commit date available]

## Decision Drivers

* Every pipeline phase and the web app need consistent, safe access to SQLite state and ChromaDB vector data without duplicating low-level connection or query logic.
* SQLite and ChromaDB expose incompatible APIs, so any abstraction must accommodate two distinct persistence models rather than force a single interface.
* Swapping or upgrading an underlying store should require changes only inside the repository classes, not across every calling module.
* Uniform error handling, logging, and transaction semantics require a single chokepoint per store.
* Tests need to instantiate lightweight, real database instances rather than mock low-level driver calls.

## Considered Options

* Two dedicated repository classes — StateDB for SQLite, VectorIndex for ChromaDB
* Direct SQL / ChromaDB API calls made ad hoc from each pipeline module
* A single unified repository abstracting both stores behind one common interface

## Decision Outcome

Chosen option: "Two dedicated repository classes", because it isolates each store's API behind its own gateway without forcing an artificial common interface over two incompatible data models. Every module receives a `StateDB` and `VectorIndex` instance injected at composition time (in `cli.py`, mirrored in `web_app.py`) rather than constructing connections itself, so the rest of the codebase is agnostic to SQL syntax or the ChromaDB API. This keeps the abstraction boundary consistent across all 8+ consuming modules and centralizes future store-level changes to two files.

## Pros and Cons of the Options

### Two dedicated repository classes (StateDB and VectorIndex)

* Good, because it fully hides SQL and ChromaDB API details from calling modules.
* Good, because it enforces consistent error handling, logging, and access patterns across the codebase.
* Good, because each store's abstraction fits its own data model rather than being forced into a shared shape.
* Bad, because both classes are coarse-grained; adding a new query shape requires extending the class rather than composing a query at the call site.

### Direct SQL / ChromaDB API calls from each module

* Good, because it avoids an intermediate layer and any perceived overhead of indirection.
* Bad, because it duplicates connection and query logic across 8+ modules.
* Bad, because it couples every module directly to SQLite/ChromaDB, making a future store change touch the entire codebase.
* Bad, because it removes the single point where error handling and logging could be enforced consistently.

### Single unified repository for both stores

* Good, because callers would only depend on one repository type instead of two.
* Bad, because SQLite and ChromaDB have incompatible operation models (relational queries vs. vector search), so a shared interface would need to be either overly generic or leaky.
* Bad, because it would obscure which store a given operation actually hits, complicating reasoning about consistency and failure modes.

[NEEDS INPUT: Was a unified repository or a shared base interface/protocol for StateDB and VectorIndex ever formally evaluated and rejected, or is this option purely hypothetical?]

## Consequences

All 8+ pipeline modules and the web app depend on the method signatures exposed by `StateDB` and `VectorIndex`; adding a parameter or changing a return type to either class is a breaking change felt everywhere it is called. New data-access needs must be added as new repository methods rather than composed ad hoc, which is a deliberate extension cost traded for consistency.

Because the two repositories are independent classes with no shared base or protocol, there is no compiler- or interface-enforced guarantee that they expose a consistent shape; consistency currently relies on convention and code review rather than a structural contract. Schema evolution for SQLite is handled by executing the current schema at initialization, with no visible versioning layer separate from the repository classes themselves.

The pattern has proven stable: features added after the pipeline's initial design (the web job queue, MOC persistence, dashboard aggregation) were implemented as new methods on the existing classes rather than by introducing new access paths, confirming the boundary has held under real extension pressure.

## References

* `zettel/state.py` — StateDB class, sole gateway for SQLite operations
* `zettel/index.py` — VectorIndex class, sole gateway for ChromaDB operations
* `zettel/cli.py:50-100` — composition root (`_get_db()` / `_get_idx()` factories injecting repositories into every CLI command)
* `zettel/web_app.py` — mirrors the same injection pattern for the web application
