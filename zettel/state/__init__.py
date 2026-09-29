"""SQLite state management for incremental processing (ADR-054).

Tracks files, sources, chapters, chunks, concepts, notes, MOCs, assets, runs
and the web job queue. ``StateDB`` is composed from one mixin per domain; see
``zettel/state/db.py`` for the list.
"""

from zettel.state.db import StateDB

__all__ = ["StateDB"]
