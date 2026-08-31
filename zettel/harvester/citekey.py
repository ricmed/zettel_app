"""Citekey generation based on available metadata."""

from __future__ import annotations

import re

from zettel.state import StateDB


def generate_citekey(db: StateDB, authors: list[str], year: int | None, title: str) -> str:
    """Generate a tiered citekey based on available metadata."""
    surname = ""
    if authors and authors[0]:
        parts = authors[0].strip().split()
        if parts:
            surname = parts[-1]

    has_author = bool(surname)
    has_year = year is not None

    words = re.sub(r"[^\w\s]", "", title).split()

    if has_author and has_year:
        slug_words = [w.capitalize() for w in words[:2]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{surname}{year}{slug}"
    elif has_author:
        slug_words = [w.capitalize() for w in words[:3]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{surname}{slug}"
    elif has_year:
        slug_words = [w.capitalize() for w in words[:3]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{year}{slug}"
    else:
        slug_words = [w.capitalize() for w in words[:4]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = slug

    citekey = base
    suffix_idx = 0
    while db.get_source_by_citekey(citekey):
        suffix_idx += 1
        citekey = f"{base}{chr(96 + suffix_idx)}"

    return citekey
