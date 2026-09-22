"""Citation provenance of a permanent note (ADR-051).

The single definition of "how a ZTL is cited": the printed page on which its
anchor quote actually sits and the ABNT author-date string built from it. Both
are derived by code from SQLite (source row + chunk row + the grounded
``anchor_quote``) — never through the LLM, same rule as the structural ``page``
and ADR-034's judgement lists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from zettel.bibliography import format_abnt_in_text
from zettel.paging import compute_page_in_book, locate_quote_pages


@dataclass(frozen=True)
class NoteCitation:
    """How a permanent note is cited in a manuscript.

    ``page`` is the first printed page (frontmatter ``page``); ``pages`` the
    label that goes into the citation (``p. 42`` / ``p. 42-43``, empty for a
    source without pages). ``page_confidence`` records where the page came from:
    ``quote`` (the anchor was located on the page map), ``chunk`` (fallback to the
    chunk's first page) or ``none``.
    """

    page: int | None
    pages: str
    cite: str
    page_confidence: str


def stored_authors(raw: Any) -> list[str]:
    """Author list from a ``sources.authors`` value (JSON text or list)."""
    if isinstance(raw, list):
        return [str(a) for a in raw]
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(a) for a in value] if isinstance(value, list) else []


def page_label(first: int | None, last: int | None = None) -> str:
    """``p. 42`` / ``p. 42-43``; empty when there is no page."""
    if first is None:
        return ""
    if last is None or last == first:
        return f"p. {first}"
    return f"p. {first}-{last}"


def resolve_citation(
    source: dict[str, Any] | None,
    chunk: dict[str, Any] | None,
    anchor_quote: str,
) -> NoteCitation:
    """Citation for a note derived from ``chunk`` and grounded on ``anchor_quote``."""
    first: int | None = None
    last: int | None = None
    confidence = "none"
    if source and chunk:
        located = locate_quote_pages(
            source.get("extracted_text"), anchor_quote, chunk.get("page_in_file")
        )
        if located:
            start_file = source.get("content_start_file_page")
            start_book = source.get("content_start_book_page")
            first = compute_page_in_book(located[0], start_file, start_book)
            last = compute_page_in_book(located[1], start_file, start_book)
            if first is not None:
                confidence = "quote"
    if first is None and chunk and chunk.get("page_in_book") is not None:
        first, last = int(chunk["page_in_book"]), None
        confidence = "chunk"

    pages = page_label(first, last)
    cite = ""
    if source:
        cite = format_abnt_in_text(stored_authors(source.get("authors")), source.get("year"), pages)
    return NoteCitation(page=first, pages=pages, cite=cite, page_confidence=confidence)


def citation_frontmatter(citation: NoteCitation) -> dict[str, Any]:
    """Frontmatter keys for a ZTL; absent keys mean there is nothing to state.

    Only the page: the ABNT string and the verbatim anchor are already in the
    body's ``auto-evidence`` block, and repeating them in the frontmatter only
    clutters a note meant to be studied. They live in ``notes.provenance_json``.
    """
    meta: dict[str, Any] = {}
    # Omitted rather than null for a source without pages (native Markdown).
    if citation.page is not None:
        meta["page"] = citation.page
        meta["citation_page_confidence"] = citation.page_confidence
    return meta


def note_provenance(citation: NoteCitation, *, anchor_quote: str, **extra: Any) -> dict[str, Any]:
    """The ``notes.provenance_json`` payload: citation, anchor and anything else
    ``connect`` knows about a note but keeps out of its file (``extra``).

    Empty values are dropped, so a reader can treat an absent key as unknown.
    """
    record: dict[str, Any] = {
        "citation": citation.cite,
        "anchor_quote": (anchor_quote or "").strip(),
    }
    record.update(extra)
    return {k: v for k, v in record.items() if v not in ("", None)}


def load_provenance(row: dict[str, Any] | None) -> dict[str, Any]:
    """Parse ``notes.provenance_json`` from a note row; ``{}`` when absent."""
    try:
        value = json.loads((row or {}).get("provenance_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
