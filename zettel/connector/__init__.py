"""The Connector (Phase 3) — RAG-based linking, permanent note generation, backlinking.

Takes approved candidates from review, generates full permanent notes with
Prompt 2, and creates/updates vault files with managed backlink blocks.
Connections are typed (supports, contradicts, extends, ...) and backlinks show
the inverse relation in PT-BR. Package layout: ADR-053.

- ``run``      entry gate (``load_approved_candidates``) and the run loop
- ``note``     one candidate -> one ZTL (vault + SQLite + Chroma)
- ``prompt``   Prompt 2 payload, cached call, parsing, PT-BR guard
- ``context``  RAG context, distant analogies, images
- ``links``    typed connections, corroboration, backlinks

Submodules never import from this ``__init__``; monkeypatch a dependency on the
submodule that uses it (e.g. ``zettel.connector.prompt.call_llm``).
"""

from zettel.connector.context import load_connect_taxonomy, search_distant_analogies
from zettel.connector.links import rebuild_auto_backlinks
from zettel.connector.note import literature_ref_for_chunk
from zettel.connector.prompt import ConnectRejected
from zettel.connector.run import load_approved_candidates, run_connect

__all__ = [
    "ConnectRejected",
    "literature_ref_for_chunk",
    "load_approved_candidates",
    "load_connect_taxonomy",
    "rebuild_auto_backlinks",
    "run_connect",
    "search_distant_analogies",
]
