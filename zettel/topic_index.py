"""Cheap term -> note routing index.

A small map from a term a reader would type to the notes that answer it. It is a
*routing* aid, not a *representation*: the `Retriever` (RRF + relevance floor +
graph expansion) stays the way evidence is chosen. This module only decides which
terms are worth listing and in what order, so every surface that shows a topic
index shows the same one.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from zettel.state import _PT_STOPWORDS

if TYPE_CHECKING:
    from zettel.state import StateDB

logger = logging.getLogger(__name__)

# A term must survive as something a person would actually search for.
MIN_TERM_CHARS = 3
MAX_TERMS_PER_NOTE = 6
MAX_NOTES_PER_TERM = 3
THESIS_HEAD_WORDS = 4

TOPIC_INDEX_BLOCK = "auto-topic-index"
SCOPE_SOURCE = "source"
SCOPE_MOC = "moc"

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


def fold(term: str) -> str:
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
    folded = fold(term)
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
    while words and fold(words[0]) in _PT_STOPWORDS:
        words.pop(0)
    head = words[:THESIS_HEAD_WORDS]
    while head and fold(head[-1]) in _PT_STOPWORDS:
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
        key = fold(term)
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
            entry = by_key.setdefault(fold(term), TermEntry(term=term))
            if source.note_id in entry.note_ids:
                continue
            if len(entry.note_ids) >= MAX_NOTES_PER_TERM:
                continue
            entry.note_ids.append(source.note_id)
            entry.labels.append(source.label)
    return sorted(by_key.values(), key=lambda e: fold(e.term))


# ── Vault surfaces ─────────────────────────────────────────────────────


def render_topic_index_block(entries: list[TermEntry]) -> str:
    """Markdown for the ``auto-topic-index`` managed block."""
    if not entries:
        return "_Nenhum termo indexado ainda._"
    return "\n".join(f"- **{entry.term}** -> " + " ".join(entry.labels) for entry in entries)


def sync_topic_index(
    db: StateDB,
    scope_kind: str,
    scope_id: str,
    sources: list[TermSource],
    note_path: Path | str | None = None,
    *,
    vault_timezone: str = "America/Sao_Paulo",
    targets_are_permanent_notes: bool = True,
) -> list[TermEntry]:
    """Regenerate one scope's topic index: the managed block and the lookup rows.

    ``targets_are_permanent_notes`` decides whether a row carries a ``note_id``.
    Literature targets route a reader but are not something the Retriever can
    score, so they are stored without one and never seed a search.
    """

    entries = build_term_map(sources)
    rows = [
        {
            "term": entry.term,
            "term_folded": fold(entry.term),
            "target": label,
            "note_id": note_id if targets_are_permanent_notes else None,
        }
        for entry in entries
        for note_id, label in zip(entry.note_ids, entry.labels, strict=True)
    ]
    db.replace_topic_index_terms(scope_kind, scope_id, rows)

    path = Path(note_path) if note_path else None
    if path and path.is_file():
        _write_block(path, render_topic_index_block(entries), vault_timezone=vault_timezone)
    return entries


def _write_block(path: Path, inner: str, *, vault_timezone: str) -> None:
    """Update the managed block, creating its section the first time.

    This function owns the `## Topic Index` section on every surface that has one
    (literature index, taxonomy MOC, hub MOC, manual MOC), so the note builders
    do not each have to remember to scaffold it.
    """
    from zettel.vault import compose_note, parse_frontmatter, safe_update_managed_blocks

    content = path.read_text(encoding="utf-8")
    if f"zettel:{TOPIC_INDEX_BLOCK}:start" not in content:
        meta, body = parse_frontmatter(content)
        body = body.rstrip("\n") + (
            f"\n\n## Topic Index\n\n"
            f"<!-- zettel:{TOPIC_INDEX_BLOCK}:start -->\n"
            f"{inner}\n"
            f"<!-- zettel:{TOPIC_INDEX_BLOCK}:end -->\n"
        )
        path.write_text(compose_note(meta, body) if meta else body, encoding="utf-8")
        return
    safe_update_managed_blocks(path, {TOPIC_INDEX_BLOCK: inner}, vault_timezone=vault_timezone)


def sources_from_permanent_notes(db: StateDB, note_ids: list[str]) -> list[TermSource]:
    """Term sources for permanent notes, read from what `connect` already stored."""
    from zettel.vault import permanent_wikilink

    sources: list[TermSource] = []
    for note_id in sorted(note_ids):
        row = db.get_note(note_id)
        if not row:
            continue
        meta = _load_json(row.get("frontmatter_json"))
        sources.append(
            TermSource(
                note_id=note_id,
                label=permanent_wikilink(note_id, row.get("title") or "", path=row.get("path")),
                frameworks=tuple(meta.get("named_frameworks") or []),
                tags=tuple(str(t) for t in (meta.get("tags") or [])),
                thesis=_thesis_from_body(row.get("body") or ""),
            )
        )
    return sources


def _load_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_THESIS_RE = re.compile(r"^>\s*\*\*Tese\*\*:\s*(.+?)\s*$", re.MULTILINE)


def _thesis_from_body(body: str) -> str:
    match = _THESIS_RE.search(body or "")
    return match.group(1).strip() if match else ""
