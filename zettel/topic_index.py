"""Cheap term -> note routing index.

A small map from a term a reader would type to the notes that answer it. It is a
*routing* aid, not a *representation*: the `Retriever` (RRF + relevance floor +
graph expansion) stays the way evidence is chosen. This module only decides which
terms are worth listing and in what order, so every surface that shows a topic
index shows the same one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from zettel.state import _PT_STOPWORDS

# A term must survive as something a person would actually search for.
MIN_TERM_CHARS = 3
MAX_TERMS_PER_NOTE = 6
MAX_NOTES_PER_TERM = 3
THESIS_HEAD_WORDS = 4

_WORD_RE = re.compile(r"[^\w\s-]", re.UNICODE)


@dataclass(frozen=True)
class TermSource:
    """One note as seen by the index builder."""
    note_id: str
    label: str
    frameworks: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    thesis: str = ""


@dataclass
class TermEntry:
    """A term and the notes that answer it, best first."""
    term: str
    note_ids: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


def _fold(term: str) -> str:
    """Accent- and case-insensitive key used to merge equivalent terms."""
    folded = unicodedata.normalize("NFKD", term.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[\s_-]+", " ", folded).strip()


def _is_usable(term: str) -> bool:
    """A term must be long enough and carry at least one non-stopword.

    "que" alone matches nearly every note and is exactly the false signal the
    FTS layer already drops; "Dropout funciona como ensemble" is fine — a
    stopword inside a phrase is glue, not the match.
    """
    folded = _fold(term)
    if len(folded) < MIN_TERM_CHARS:
        return False
    return any(word not in _PT_STOPWORDS for word in folded.split())


def _thesis_terms(thesis: str) -> list[str]:
    """The opening noun phrase of a thesis, as a fallback term.

    A thesis reads "Dropout funciona como ensemble implicito...", so the first
    few words name the concept. Stopwords are dropped first, which is also what
    keeps "que"-style tokens out of the index.
    """
    words = _WORD_RE.sub(" ", thesis).split()
    # Trim stopwords only at the edges: dropping them from the middle would
    # mangle the phrase ("Dropout funciona ensemble") instead of shortening it.
    while words and _fold(words[0]) in _PT_STOPWORDS:
        words.pop(0)
    head = words[:THESIS_HEAD_WORDS]
    while head and _fold(head[-1]) in _PT_STOPWORDS:
        head.pop()
    term = " ".join(head).strip()
    return [term] if term and _is_usable(term) else []


def _note_terms(source: TermSource) -> list[str]:
    """Terms for one note, most specific first, deduplicated by folded key.

    The thesis head is a *fallback*, used only when the note names no framework
    and carries no tag. A truncated sentence ("Dropout treina sub-redes faz") is
    a worse index key than a tag, so it should never compete with one.
    """
    ordered: list[str] = [*source.frameworks, *source.tags]
    if not ordered:
        ordered = _thesis_terms(source.thesis)

    seen: set[str] = set()
    terms: list[str] = []
    for raw in ordered:
        term = str(raw).strip().lstrip("#").replace("_", " ").strip()
        if not term or not _is_usable(term):
            continue
        key = _fold(term)
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= MAX_TERMS_PER_NOTE:
            break
    return terms


def build_term_map(sources: list[TermSource]) -> list[TermEntry]:
    """Map terms to the notes that answer them.

    Terms come from named frameworks (the author's own vocabulary), then tags,
    then the head of the thesis — in that order, because a name the author coined
    routes better than a tag the pipeline assigned. Entries are sorted by term so
    the same input always renders the same index.
    """
    by_key: dict[str, TermEntry] = {}
    for source in sources:
        for term in _note_terms(source):
            entry = by_key.setdefault(_fold(term), TermEntry(term=term))
            if source.note_id in entry.note_ids:
                continue
            if len(entry.note_ids) >= MAX_NOTES_PER_TERM:
                continue
            entry.note_ids.append(source.note_id)
            entry.labels.append(source.label)
    return sorted(by_key.values(), key=lambda e: _fold(e.term))
