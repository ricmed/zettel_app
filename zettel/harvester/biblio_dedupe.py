"""Bibliographic duplicate detection: exact DOI/ISBN, then title + author.

These are layers 3 and 4 of the harvest dedupe chain (see
:mod:`zettel.harvester.duplicates` for layers 1, 2 and 5). Both are pure SQLite
and run before any embedding is computed, so the common case gets cheaper rather
than more expensive.

The two layers differ in kind, not just in confidence, and the pipeline treats
them differently:

* **DOI / ISBN** is an *identity*. A match is the same work by definition, so the
  existing ``source_id`` is reused with no prompt.
* **Title + author** is a *heuristic*. It cannot separate a book from a chapter
  of that book, a second edition from the first, or a translation from its
  original — all of which share a title and an author. So it always asks, and
  its non-interactive default is to create a new source: a false positive here
  silently loses a unique source and is not reversible, while a false negative
  merely leaves two sources the user can purge later.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from zettel.hashing import fold_for_match
from zettel.state import StateDB

logger = logging.getLogger(__name__)

# A DOI can legitimately be typed with a URL prefix or a "doi:" scheme.
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def normalize_doi(raw: str | None) -> str:
    """Fold a DOI to its comparable form, or "" when there is nothing usable.

    DOIs are case-insensitive by specification, and the same one is written as a
    bare ``10.x/y``, as ``doi:10.x/y`` or as a doi.org URL.
    """
    if not raw:
        return ""
    value = _DOI_PREFIX.sub("", str(raw).strip()).strip().lower()
    # A DOI always starts with a "10." registrant prefix; anything else is junk
    # the extractor guessed, and matching on it would merge unrelated works.
    return value if value.startswith("10.") else ""


def normalize_isbn(raw: str | None) -> str:
    """Strip hyphens/spaces from an ISBN; keep only a plausible ISBN-10/13."""
    if not raw:
        return ""
    value = re.sub(r"[^0-9xX]", "", str(raw)).upper()
    return value if len(value) in (10, 13) else ""


def normalize_title(raw: str | None) -> str:
    """Fold a title for comparison, dropping any subtitle after a colon.

    The subtitle is where cataloguing disagrees most (present in one record,
    absent in another, or punctuated differently), so the main title alone is
    the more stable key. Reuses ``fold_for_match``: NFKC, accents stripped,
    lowercased, punctuation collapsed.
    """
    if not raw:
        return ""
    main = str(raw).split(":", 1)[0]
    return fold_for_match(main)


def author_surnames(authors: list[str] | None) -> set[str]:
    """Last token of each author name, folded.

    Enough to tell "Ricardo Medeiros" from "Daniel Kahneman" without needing a
    fuzzy matcher and its untuned threshold; deliberately tolerant of "R.
    Medeiros" vs "Ricardo Medeiros".
    """
    surnames: set[str] = set()
    for name in authors or []:
        folded = fold_for_match(str(name))
        if folded:
            surnames.add(folded.split()[-1])
    return surnames


def _stored_authors(row: dict[str, Any]) -> list[str]:
    try:
        parsed = json.loads(row.get("authors") or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(a) for a in parsed] if isinstance(parsed, list) else []


def find_exact_bibliographic_match(
    db: StateDB, *, doi: str, isbn: str
) -> tuple[dict[str, Any], str] | None:
    """A source with the same DOI or ISBN. Returns (row, which_key) or None.

    Both arguments must already be normalized. DOI is checked first: it
    identifies one work, whereas an ISBN identifies one *edition* of it and is
    occasionally reused across printings.
    """
    if doi:
        row = db.get_source_by_doi(doi)
        if row:
            return row, "doi"
    if isbn:
        row = db.get_source_by_isbn(isbn)
        if row:
            return row, "isbn"
    return None


def find_title_author_candidates(
    db: StateDB,
    *,
    title: str,
    authors: list[str] | None,
    year: int | None,
) -> list[dict[str, Any]]:
    """Sources sharing a normalized title *and* at least one author surname.

    Both conditions are required. Title alone matches a book against its own
    chapters; authors alone matches everything a prolific author wrote.

    The year is reported, never used to reject: two editions of one work are
    plausibly the same thing to a reader and plausibly distinct to a
    bibliographer, and that is exactly the call the user should make.
    """
    folded_title = normalize_title(title)
    surnames = author_surnames(authors)
    if not folded_title or not surnames:
        return []

    candidates: list[dict[str, Any]] = []
    for row in db.list_sources_with_authors():
        if normalize_title(row.get("title")) != folded_title:
            continue
        if not (surnames & author_surnames(_stored_authors(row))):
            continue
        row_year = row.get("year")
        candidates.append(
            {
                "source_id": row["source_id"],
                "citekey": row["citekey"],
                "title": row.get("title") or "(sem titulo)",
                "year": row_year,
                "same_year": year is not None and row_year == year,
            }
        )
    return candidates


def resolve_title_author_decision(
    file_path: Path,
    candidates: list[dict[str, Any]],
    interactive: bool,
) -> str:
    """Ask what to do about a title+author match. Returns "skip"/"continue"/"abort".

    Unlike the semantic layer, this one ignores ``non_interactive_duplicate_action``
    and defaults to ``continue`` without a TTY. Merging on a title match is the
    irreversible direction (ADR-011 records that there is no way to separate two
    documents again), and title collisions between genuinely distinct works —
    a book and its chapter, two editions — are common enough that silently
    dropping the file would lose real sources.
    """
    lines = ", ".join(f"{c['citekey']} ({c['year'] or 's.d.'})" for c in candidates)
    if not interactive:
        logger.warning(
            "Titulo e autor coincidem com fonte existente para '%s' (modo nao-interativo): "
            "seguindo como nova fonte. Candidatos: %s",
            file_path.name,
            lines,
        )
        return "continue"

    from rich.console import Console
    from rich.prompt import Prompt
    from rich.table import Table

    console = Console(stderr=True)
    table = Table(title=f"Titulo e autor ja cadastrados: {file_path.name}")
    table.add_column("Citekey", style="bold")
    table.add_column("Titulo")
    table.add_column("Ano", justify="right")
    for c in candidates:
        marker = "[bold]=[/bold]" if c["same_year"] else ""
        table.add_row(c["citekey"], c["title"], f"{c['year'] or '-'} {marker}".strip())
    console.print(table)
    console.print(
        "[dim]Anos diferentes costumam indicar outra edicao; o mesmo titulo tambem "
        "aparece entre um livro e um capitulo dele.[/dim]"
    )

    choice = Prompt.ask(
        "Ja existe uma fonte com este titulo e autor. O que deseja fazer?",
        choices=["pular", "continuar", "abortar"],
        default="continuar",
        console=console,
    )
    return {"pular": "skip", "continuar": "continue", "abortar": "abort"}[choice]
