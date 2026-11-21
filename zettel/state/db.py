"""``StateDB``: the connection lifecycle plus every domain mixin (ADR-054)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from zettel.search_terms import fold
from zettel.state.assets import AssetsMixin
from zettel.state.books import BooksMixin
from zettel.state.chapters import ChaptersMixin
from zettel.state.chunks import ChunksMixin
from zettel.state.concepts import ConceptsMixin
from zettel.state.connections import ConnectionsMixin
from zettel.state.decisions import DecisionsMixin
from zettel.state.fts import FtsMixin
from zettel.state.llm_cache import LlmCacheMixin
from zettel.state.mocs import MocsMixin
from zettel.state.notes import NotesMixin
from zettel.state.reviews import ReviewsMixin
from zettel.state.runs import RunsMixin
from zettel.state.schema import FTS_SQL, RETENTION_COLUMNS, SCHEMA_SQL, SOURCE_READER_COLUMNS
from zettel.state.sources import SourcesMixin
from zettel.state.web import WebMixin

logger = logging.getLogger(__name__)

# The mixins in pipeline order. `tests/test_state_package.py` checks that no two
# of them define the same attribute, since the MRO would silently shadow one.
MIXINS = (
    SourcesMixin,
    ChaptersMixin,
    ChunksMixin,
    ConceptsMixin,
    NotesMixin,
    ConnectionsMixin,
    MocsMixin,
    AssetsMixin,
    LlmCacheMixin,
    DecisionsMixin,
    ReviewsMixin,
    RunsMixin,
    FtsMixin,
    WebMixin,
    BooksMixin,
)


class StateDB(*MIXINS):
    """SQLite (WAL) state for incremental processing — see ADR-001 / ADR-008."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connect()

    def _connect(self) -> None:
        """Open ``db_path`` and apply the schema. ``reopen`` uses the same path."""
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.create_function("zfold", 1, fold, deterministic=True)
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        self._ensure_source_reader_columns()
        self._ensure_retention_columns()
        self.drop_legacy_shelf_tables()
        self.fts_enabled = self._init_fts()

    def _ensure_source_reader_columns(self) -> None:
        """Colunas de leitor num ``sources`` criado antes desta versão do esquema.

        ``CREATE TABLE IF NOT EXISTS`` não acrescenta coluna a uma tabela que já
        existe. Um banco novo já nasce com elas no ``SCHEMA_SQL``.
        """
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(sources)")}
        for name, decl in SOURCE_READER_COLUMNS:
            if name not in have:
                self.conn.execute(f"ALTER TABLE sources ADD COLUMN {name} {decl}")
        self.conn.commit()

    def _ensure_retention_columns(self) -> None:
        """Colunas de retenção num banco criado antes do markdown integral.

        ``CREATE TABLE IF NOT EXISTS`` não acrescenta coluna a uma tabela que já
        existe. Um banco novo já nasce com elas no ``SCHEMA_SQL``. A tabela
        ``projects`` é nova e entra pelo próprio ``SCHEMA_SQL``.
        """
        for table, columns in RETENTION_COLUMNS.items():
            have = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def _init_fts(self) -> bool:
        """Create the FTS5 tables; False when the SQLite build lacks the module.

        Without fts5 the pipeline keeps working with vector search only (the
        Retriever degrades to ``mode="vector"``).
        """
        try:
            self.conn.executescript(FTS_SQL)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "fts5" in msg or "no such module" in msg:
                logger.warning("SQLite sem suporte a FTS5 — busca hibrida (BM25) desabilitada")
                return False
            raise
        self.conn.commit()
        return True

    def close(self) -> None:
        """Close the connection. A second call is a no-op."""
        try:
            self.conn.close()
        except sqlite3.ProgrammingError:
            return

    def reopen(self) -> None:
        """Close and open the file again.

        Restore replaces ``state.db`` while this object is the worker's
        connection. The next statement has to see the file that was just written.
        """
        self.close()
        self._connect()

    def vacuum(self) -> None:
        """Reclaim free pages after bulk deletes (does not change logical data).

        Runs WAL checkpoint then VACUUM. Needs exclusive access; may use
        temporary disk space roughly the size of the DB file.
        """
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")
        # VACUUM recreates the DB file; ensure subsequent writes still commit.
        self.conn.commit()
