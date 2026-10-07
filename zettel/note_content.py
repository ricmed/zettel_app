"""The content of a permanent note, as a prompt or a human labeller should read it.

A ZTL body (``vault.build_permanent_note_body``) holds the thesis line, the content
sections (``## Definição``, ``## Intuição``, ``## Exemplo``, ``## Limites``) and then
material that is *about* the note rather than *in* it: figures, ``## Fonte``, the
``auto-evidence`` block, ``## Conexões``. Only the content sections say what the note
claims. Connections would also leak a decision already taken (two notes already
linked), so they never travel with the content.

Stdlib-only leaf, like ``markdown_fences``: importable without chromadb. #209 is the
first consumer (dedupe); #211 extends it to every prompt that reads notes.
"""

from __future__ import annotations

import re
from typing import Any

from zettel.markdown_fences import h2_section

# (key, label) in reading order. Keys match `PermanentNoteCandidate` /
# `PermanentNoteLLMOutput`, so a candidate and a written note render alike.
NOTE_FIELDS = (
    ("thesis", "Tese"),
    ("definition", "Definição"),
    ("intuition", "Intuição"),
    ("example", "Exemplo"),
    ("limits", "Limites"),
)
_HEADINGS = {
    "definition": "Definição",
    "intuition": "Intuição",
    "example": "Exemplo",
    "limits": "Limites",
}
_TESE_LINE = re.compile(r"^>\s*\*\*Tese\*\*:\s*(.+)$", re.MULTILINE)


def thesis_from_permanent_note(meta: dict[str, Any], body: str) -> str:
    """Thesis line from a ZTL body (``> **Tese**: ...``), else frontmatter title."""
    match = _TESE_LINE.search(body or "")
    if match:
        return match.group(1).strip()
    return str(meta.get("title") or "").strip()


def note_sections(title: str, body: str) -> dict[str, str]:
    """Content fields of a ZTL body; empty string for a section the note lacks."""
    sections = {"thesis": thesis_from_permanent_note({"title": title}, body)}
    for key, heading in _HEADINGS.items():
        sections[key] = h2_section(body or "", heading)
    return sections


def render_note_content(fields: dict[str, Any], max_chars: int | None = None) -> str:
    """Every filled field, labelled, in reading order.

    ``max_chars`` is a ceiling for a prompt budget, not an excerpt: it cuts only a
    note longer than the ceiling, and says so with a trailing ``...``.
    """
    text = "\n\n".join(f"{label}: {fields[key]}" for key, label in NOTE_FIELDS if fields.get(key))
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text
