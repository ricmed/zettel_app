"""The content of a permanent note, as a prompt or a human labeller should read it.

A ZTL body (``vault.build_permanent_note_body``) holds the thesis line, the content
sections (``## Definição``, ``## Intuição``, ``## Exemplo``, ``## Limites``) and then
material that is *about* the note rather than *in* it: figures, ``## Fonte``, the
``auto-evidence`` block, ``## Conexões``. Only the content sections say what the note
claims. Connections would also leak a decision already taken (two notes already
linked), so they never travel with the content.

Stdlib-only leaf, like ``markdown_fences``: importable without chromadb. #209 was the
first consumer (dedupe); since #211 ``note_content`` is what every retrieval path
hands a prompt, so a note found by vector, BM25, graph or MOC reads the same.
"""

from __future__ import annotations

import re
from typing import Any

from zettel.markdown_fences import h2_section, headings_outside_fences

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
# H2 sections *about* a note rather than in it; dropped from a free-form body.
_NOT_CONTENT = {"Conexões", "Fonte", "Figuras"}
_H2 = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_MANAGED_BLOCK = re.compile(
    r"<!--\s*zettel:(auto-[\w-]+):start\s*-->.*?<!--\s*zettel:\1:end\s*-->", re.DOTALL
)


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


def note_content(title: str, body: str) -> str:
    """The text a prompt reads for a permanent note, whatever path found it.

    A note in the pipeline layout renders its labelled content fields. A
    hand-written note without those sections keeps its own prose, minus managed
    blocks and the sections about the note (``## Conexões``, ``## Fonte``,
    ``## Figuras``), so its content is not lost to a layout it never had.
    """
    sections = note_sections(title, body)
    if any(sections[key] for key in _HEADINGS):
        return render_note_content(sections)
    return _free_form_content(body)


def _free_form_content(body: str) -> str:
    text = _MANAGED_BLOCK.sub("", body or "")
    headings = headings_outside_fences(_H2, text)
    kept, cursor = [], 0
    for i, match in enumerate(headings):
        if match.group(1) not in _NOT_CONTENT:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        kept.append(text[cursor : match.start()])
        cursor = end
    kept.append(text[cursor:])
    return re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()
