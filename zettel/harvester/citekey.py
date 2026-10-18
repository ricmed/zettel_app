"""Citekey generation based on available metadata."""

from __future__ import annotations

import re
import unicodedata

from zettel.state import StateDB


def citekey_word(text: str) -> str:
    """Letters and digits only, NFC. Drops path separators, dots and spaces."""
    folded = unicodedata.normalize("NFC", text or "")
    return "".join(ch for ch in folded if ch.isalnum())


def _disambiguation_suffix(index: int) -> str:
    """``1 -> a``, ``26 -> z``, ``27 -> aa``. Never emits ``{``."""
    chars: list[str] = []
    n = index
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(ord("a") + rem))
    return "".join(reversed(chars))


def author_surname(authors: list[str] | None) -> str:
    """Last token of the first author, reduced to a path-safe word."""
    if not authors or not authors[0]:
        return ""
    parts = authors[0].strip().split()
    if not parts:
        return ""
    return citekey_word(parts[-1])


def generate_citekey(db: StateDB, authors: list[str], year: int | None, title: str) -> str:
    """Generate a tiered citekey based on available metadata."""
    surname = author_surname(authors)

    has_author = bool(surname)
    has_year = year is not None

    words = [citekey_word(w) for w in re.sub(r"[^\w\s]", "", title).split()]
    words = [w for w in words if w]

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
        citekey = f"{base}{_disambiguation_suffix(suffix_idx)}"

    return citekey
