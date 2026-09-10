"""Read-only inventory of the two persistent stores: ``state.db`` and Chroma.

``zettel status`` answers what is in the pipeline queue. This module answers
how much each store occupies and what is inside it — no LLM, no embeddings,
no writes to either store (the Chroma client is opened only to call
``collection.count()``).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.state import StateDB

logger = logging.getLogger(__name__)

# Columns that actually carry document text or JSON. LENGTH on these is what
# answers "why is state.db large?" — distinct from dbstat page occupancy.
HEAVY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sources", "extracted_text"),
    ("sources", "lit_body"),
    ("sources", "bibliography_json"),
    ("sources", "summary"),
    ("chapters", "summary"),
    ("chunks", "text"),
    ("chunks", "summary_json"),
    ("concepts", "candidate_json"),
    ("notes", "body"),
    ("notes", "frontmatter_json"),
    ("mocs", "body"),
    ("mocs", "frontmatter_json"),
    ("assets", "description"),
    ("assets", "context_snippet"),
    ("llm_cache", "request_json"),
    ("llm_cache", "response_json"),
    ("web_jobs", "payload_json"),
    ("web_jobs", "result_json"),
)

_CHROMA_SQLITE_NAME = "chroma.sqlite3"


@dataclass(frozen=True)
class SqlitePragmas:
    page_size: int
    page_count: int
    freelist_count: int
    journal_mode: str
    unused_bytes: int


@dataclass(frozen=True)
class TableStat:
    name: str
    kind: str
    rows: int | None
    disk_bytes: int | None
    payload_bytes: int | None


@dataclass(frozen=True)
class ColumnStat:
    table: str
    column: str
    rows: int
    rows_nonnull: int
    bytes: int
    avg_bytes: float
    max_bytes: int


@dataclass(frozen=True)
class CollectionStat:
    name: str
    count: int | None
    error: str | None = None


@dataclass(frozen=True)
class SegmentDir:
    name: str
    bytes: int
    files: int


@dataclass(frozen=True)
class SqliteStoreReport:
    path: str
    exists: bool
    file_bytes: int
    wal_bytes: int
    shm_bytes: int
    total_bytes: int
    pragmas: SqlitePragmas | None
    tables: tuple[TableStat, ...]
    columns: tuple[ColumnStat, ...]
    dbstat_available: bool


@dataclass(frozen=True)
class ChromaReport:
    path: str
    exists: bool
    dir_total_bytes: int
    hnsw_bytes: int
    sqlite: SqliteStoreReport | None
    segments: tuple[SegmentDir, ...]
    collections: tuple[CollectionStat, ...]
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    client_error: str | None


@dataclass(frozen=True)
class DatabaseReport:
    state: SqliteStoreReport
    chroma: ChromaReport


def file_bytes(path: Path) -> int:
    """``st_size`` or 0 when the path is missing or unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def companion_bytes(db_path: Path) -> tuple[int, int, int]:
    """Main file, WAL, and SHM sizes for a SQLite database path."""
    return (
        file_bytes(db_path),
        file_bytes(Path(str(db_path) + "-wal")),
        file_bytes(Path(str(db_path) + "-shm")),
    )


def walk_dir_bytes(path: Path) -> int:
    """Sum of every regular file under ``path``, including ``path`` itself."""
    if not path.exists():
        return 0
    if path.is_file():
        return file_bytes(path)
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += file_bytes(child)
    return total


def walk_chroma_segments(chroma_path: Path) -> tuple[int, int, tuple[SegmentDir, ...]]:
    """Directory total, HNSW-segment total, and one row per segment folder.

    Files sitting next to the segment dirs (``chroma.sqlite3`` and WAL/SHM)
    count toward the directory total but not toward ``hnsw_bytes``.
    """
    if not chroma_path.exists():
        return 0, 0, ()
    dir_total = 0
    hnsw = 0
    segments: list[SegmentDir] = []
    try:
        children = sorted(chroma_path.iterdir(), key=lambda p: p.name)
    except OSError:
        return 0, 0, ()
    for child in children:
        if child.is_file():
            dir_total += file_bytes(child)
            continue
        if not child.is_dir():
            continue
        size = walk_dir_bytes(child)
        files = sum(1 for f in child.rglob("*") if f.is_file())
        dir_total += size
        hnsw += size
        segments.append(SegmentDir(name=child.name, bytes=size, files=files))
    return dir_total, hnsw, tuple(segments)


def try_dbstat(conn: sqlite3.Connection) -> dict[str, int] | None:
    """Per-object page occupancy via the ``dbstat`` virtual table.

    Returns ``None`` when this SQLite build has no ``dbstat`` VTAB, so callers
    can fall back to COUNT + LENGTH without failing the report.
    """
    try:
        conn.execute("DROP TABLE IF EXISTS temp.report_dbstat")
        conn.execute("CREATE VIRTUAL TABLE temp.report_dbstat USING dbstat")
        rows = conn.execute(
            "SELECT name, SUM(pgsize) AS bytes FROM temp.report_dbstat GROUP BY name"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    out: dict[str, int] = {}
    for row in rows:
        name = row[0]
        if not name or str(name).startswith("sqlite_") or name == "report_dbstat":
            continue
        out[str(name)] = int(row[1] or 0)
    return out


def inspect_sqlite(
    conn: sqlite3.Connection,
    path: Path,
    *,
    heavy_columns: Sequence[tuple[str, str]] = (),
) -> SqliteStoreReport:
    """Inspect one SQLite file through an already-open connection."""
    exists = path.exists()
    main, wal, shm = companion_bytes(path) if exists else (0, 0, 0)
    pragmas = _inspect_pragmas(conn) if exists else None
    sizes = try_dbstat(conn)
    tables = _collect_table_stats(conn, sizes)
    columns = _collect_column_stats(conn, heavy_columns)
    return SqliteStoreReport(
        path=str(path),
        exists=exists,
        file_bytes=main,
        wal_bytes=wal,
        shm_bytes=shm,
        total_bytes=main + wal + shm,
        pragmas=pragmas,
        tables=tables,
        columns=columns,
        dbstat_available=sizes is not None,
    )


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    """Open ``path`` URI-style read-only so a report cannot create a WAL."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect_db_report(cfg: AppConfig, db: StateDB) -> DatabaseReport:
    """Build the full report from the live ``StateDB`` and the Chroma directory."""
    state = inspect_sqlite(db.conn, Path(db.db_path), heavy_columns=HEAVY_COLUMNS)
    chroma = _collect_chroma(Path(cfg.chroma_path))
    return DatabaseReport(state=state, chroma=chroma)


def _collect_chroma(chroma_path: Path) -> ChromaReport:
    exists = chroma_path.exists()
    dir_total, hnsw, segments = walk_chroma_segments(chroma_path)
    sqlite_path = chroma_path / _CHROMA_SQLITE_NAME
    sqlite_report: SqliteStoreReport | None = None
    if sqlite_path.exists():
        conn: sqlite3.Connection | None = None
        try:
            conn = open_sqlite_readonly(sqlite_path)
            sqlite_report = inspect_sqlite(conn, sqlite_path)
        except sqlite3.Error as exc:
            logger.debug("Nao foi possivel inspecionar %s: %s", sqlite_path, exc)
        finally:
            if conn is not None:
                conn.close()

    collections, client_error, identity = _inspect_chroma_collections(chroma_path)
    return ChromaReport(
        path=str(chroma_path),
        exists=exists,
        dir_total_bytes=dir_total,
        hnsw_bytes=hnsw,
        sqlite=sqlite_report,
        segments=segments,
        collections=collections,
        embedding_provider=identity[0],
        embedding_model=identity[1],
        embedding_dimensions=identity[2],
        client_error=client_error,
    )


def _inspect_chroma_collections(
    chroma_path: Path,
) -> tuple[tuple[CollectionStat, ...], str | None, tuple[str | None, str | None, int | None]]:
    from zettel.index import _ALL_COLLECTIONS, peek_stored_embedding_identity

    identity = peek_stored_embedding_identity(chroma_path)
    if not chroma_path.exists():
        return (), None, identity

    try:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=str(chroma_path),
            settings=Settings(anonymized_telemetry=False),
        )
    except Exception as exc:
        logger.debug("Nao foi possivel abrir Chroma em %s: %s", chroma_path, exc)
        return (), str(exc), identity

    listed: set[str] = set()
    try:
        listed = {col.name for col in client.list_collections()}
    except Exception:
        listed = set()

    stats: list[CollectionStat] = []
    for name in _ALL_COLLECTIONS:
        stats.append(_collection_stat(client, name, listed))
    extras = sorted(listed.difference(_ALL_COLLECTIONS))
    for name in extras:
        stats.append(_collection_stat(client, name, listed))
    return tuple(stats), None, identity


def _collection_stat(client: Any, name: str, listed: set[str]) -> CollectionStat:
    try:
        return CollectionStat(name=name, count=int(client.get_collection(name).count()))
    except Exception as exc:
        if name in listed:
            return CollectionStat(name=name, count=None, error=str(exc))
        return CollectionStat(name=name, count=0)


def _inspect_pragmas(conn: sqlite3.Connection) -> SqlitePragmas:
    page_size = _pragma_int(conn, "page_size")
    page_count = _pragma_int(conn, "page_count")
    freelist = _pragma_int(conn, "freelist_count")
    journal = _pragma_str(conn, "journal_mode")
    return SqlitePragmas(
        page_size=page_size,
        page_count=page_count,
        freelist_count=freelist,
        journal_mode=journal,
        unused_bytes=freelist * page_size,
    )


def _pragma_int(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def _pragma_str(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    if row is None:
        return ""
    return str(row[0] or "")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _master_objects(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT name, type FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
          AND name NOT LIKE 'report_dbstat'
          AND type IN ('table', 'index')
        """
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[0]}


def _collect_table_stats(
    conn: sqlite3.Connection,
    sizes: dict[str, int] | None,
) -> tuple[TableStat, ...]:
    objects = _master_objects(conn)
    names = set(objects)
    if sizes is not None:
        names.update(sizes)
    stats: list[TableStat] = []
    for name in names:
        kind = objects.get(name, "object")
        rows = _safe_count(conn, name) if kind == "table" else None
        payload = _table_payload_bytes(conn, name) if kind == "table" else None
        disk = None if sizes is None else sizes.get(name)
        stats.append(
            TableStat(
                name=name,
                kind=kind,
                rows=rows,
                disk_bytes=disk,
                payload_bytes=payload,
            )
        )
    stats.sort(
        key=lambda s: (
            -(s.disk_bytes or 0),
            -(s.payload_bytes or 0),
            s.name,
        )
    )
    return tuple(stats)


def _safe_count(conn: sqlite3.Connection, name: str) -> int | None:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(name)}").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return int(row[0] or 0)


def _is_payload_type(declared: str) -> bool:
    typ = (declared or "").upper()
    if typ == "":
        return True
    return any(token in typ for token in ("TEXT", "BLOB", "CLOB", "CHAR"))


def _payload_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        cols = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    except sqlite3.Error:
        return []
    names: list[str] = []
    for col in cols:
        declared = col["type"] if isinstance(col, sqlite3.Row) else col[2]
        name = col["name"] if isinstance(col, sqlite3.Row) else col[1]
        if _is_payload_type(str(declared or "")):
            names.append(str(name))
    return names


def _table_payload_bytes(conn: sqlite3.Connection, table: str) -> int | None:
    columns = _payload_columns(conn, table)
    if not columns:
        return 0
    parts = [f"COALESCE(SUM(LENGTH({_quote_ident(c)})), 0)" for c in columns]
    sql = f"SELECT {' + '.join(parts)} FROM {_quote_ident(table)}"
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return 0
    return int(row[0] or 0)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _collect_column_stats(
    conn: sqlite3.Connection,
    heavy_columns: Sequence[tuple[str, str]],
) -> tuple[ColumnStat, ...]:
    stats: list[ColumnStat] = []
    for table, column in heavy_columns:
        if not _table_exists(conn, table):
            continue
        quoted_table = _quote_ident(table)
        quoted_col = _quote_ident(column)
        try:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS rows,
                    COUNT({quoted_col}) AS rows_nonnull,
                    COALESCE(SUM(LENGTH({quoted_col})), 0) AS bytes,
                    COALESCE(MAX(LENGTH({quoted_col})), 0) AS max_bytes
                FROM {quoted_table}
                """
            ).fetchone()
        except sqlite3.Error:
            continue
        if row is None:
            continue
        rows = int(row[0] or 0)
        rows_nonnull = int(row[1] or 0)
        nbytes = int(row[2] or 0)
        max_bytes = int(row[3] or 0)
        avg = (nbytes / rows_nonnull) if rows_nonnull else 0.0
        stats.append(
            ColumnStat(
                table=table,
                column=column,
                rows=rows,
                rows_nonnull=rows_nonnull,
                bytes=nbytes,
                avg_bytes=avg,
                max_bytes=max_bytes,
            )
        )
    stats.sort(key=lambda s: (-s.bytes, s.table, s.column))
    return tuple(stats)
