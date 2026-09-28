"""``StateDB``: the connection lifecycle plus every domain mixin (ADR-054)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from zettel.search_terms import fold
from zettel.state.assets import AssetsMixin
from zettel.state.chapters import ChaptersMixin
from zettel.state.chunks import ChunksMixin
from zettel.state.concepts import ConceptsMixin
from zettel.state.connections import ConnectionsMixin
from zettel.state.fts import FtsMixin
from zettel.state.llm_cache import LlmCacheMixin
from zettel.state.mocs import MocsMixin
from zettel.state.notes import NotesMixin
from zettel.state.runs import RunsMixin
from zettel.state.schema import FTS_SQL, SCHEMA_SQL
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
    RunsMixin,
    FtsMixin,
    WebMixin,
)


class StateDB(*MIXINS):
    """SQLite (WAL) state for incremental processing — see ADR-001 / ADR-008."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.create_function("zfold", 1, fold, deterministic=True)
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        self.fts_enabled = self._init_fts()

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
        self.conn.close()

    def vacuum(self) -> None:
        """Reclaim free pages after bulk deletes (does not change logical data).

        Runs WAL checkpoint then VACUUM. Needs exclusive access; may use
        temporary disk space roughly the size of the DB file.
        """
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")
        # VACUUM recreates the DB file; ensure subsequent writes still commit.
        self.conn.commit()
