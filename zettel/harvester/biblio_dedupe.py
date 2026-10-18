"""Bibliographic duplicate detection: exact DOI/ISBN, then title + author.

These are layers 3 and 4 of the harvest dedupe chain (see
:mod:`zettel.harvester.duplicates` for layers 1, 2 and 5). Both are pure SQLite
and run before any embedding is computed, so the common case gets cheaper rather
than more expensive.

The two layers differ in kind, not just in confidence, and the pipeline treats
them differently:

* **DOI** is an *identity*. A match is the same work, so the existing
  ``source_id`` is reused with no prompt. **ISBN** is an edition, and it is
  also printed on a chapter PDF: it merges without asking only when both
  records are the same normalized title and neither is a chapter or
  proceedings paper. Otherwise it asks, like layer 4.
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
# Crossref's recommended pattern (case-insensitive). Registrant is 4-9 digits.
_DOI_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:a-z0-9]+$", re.IGNORECASE)
_DOI_TRAILING = ".;,)"
# An ISBN on a chapter or proceedings PDF is the book's (or the volume's), not
# a private identity of that file.
_ISBN_NOT_IDENTITY = frozenset({"capitulo_livro", "anais_evento"})


def normalize_doi(raw: str | None) -> str:
    """Fold a DOI to its comparable form, or "" when it is not a DOI.

    DOIs are case-insensitive. A ``doi:`` scheme or a doi.org URL is stripped,
    then trailing sentence punctuation (``.``, ``)``, ``;``, ``,``). What
    remains must match the Crossref pattern; anything else is not used as an
    identity.
    """
    if not raw:
        return ""
    value = _DOI_PREFIX.sub("", str(raw).strip()).strip()
    value = value.rstrip(_DOI_TRAILING).lower()
    if not _DOI_RE.fullmatch(value):
        if value.startswith("10."):
            logger.info("DOI descartado (formato invalido): %s", value)
        return ""
    return value


def _isbn13_ok(digits: str) -> bool:
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(digits))
    return total % 10 == 0


def _isbn10_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(digits):
        value = 10 if ch == "X" else int(ch)
        total += value * (10 - i)
    return total % 11 == 0


def normalize_isbn(raw: str | None) -> str:
    """Strip hyphens/spaces and require a valid ISBN-10 or ISBN-13 check digit.

    An invalid check digit is "" so layer 3 does not merge on it. ``X`` is the
    ISBN-10 check character.
    """
    if not raw:
        return ""
    value = re.sub(r"[^0-9xX]", "", str(raw)).upper()
    if len(value) == 13 and value.isdigit() and _isbn13_ok(value):
        return value
    if (
        len(value) == 10
        and value[:-1].isdigit()
        and value[-1] in "0123456789X"
        and _isbn10_ok(value)
    ):
        return value
    if value:
        logger.info("ISBN descartado (digito verificador ou tamanho): %s", value)
    return ""


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


def isbn_requires_confirmation(
    incoming_type: str | None,
    existing: dict[str, Any],
    incoming_title: str | None,
) -> bool:
    """True when a shared ISBN is not enough to merge without asking.

    A chapter or proceedings paper often carries the book's ISBN. Differing
    normalized titles are the same situation: two works, one edition number.
    """
    existing_type = str(existing.get("document_type") or "")
    incoming = str(incoming_type or "")
    if incoming in _ISBN_NOT_IDENTITY or existing_type in _ISBN_NOT_IDENTITY:
        return True
    return normalize_title(incoming_title) != normalize_title(existing.get("title"))


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


def resolve_isbn_decision(
    file_path: Path,
    existing: dict[str, Any],
    interactive: bool,
) -> str:
    """Ask before merging on an ISBN that does not identify the work.

    Without a TTY the policy is layer 4's: continue as a new source.
    """
    if not interactive:
        logger.warning(
            "ISBN de '%s' coincide com %s, mas nao identifica a obra "
            "(capitulo, anais ou titulo diferente). Seguindo como nova fonte.",
            file_path.name,
            existing.get("citekey"),
        )
        return "continue"
    candidate = [
        {
            "citekey": existing.get("citekey") or "?",
            "title": existing.get("title") or "(sem titulo)",
            "year": existing.get("year"),
            "same_year": False,
        }
    ]
    return resolve_title_author_decision(file_path, candidate, interactive=True)
