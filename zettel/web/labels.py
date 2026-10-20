"""Display labels shared by several route modules (#318).

One definition each: the manual-note form and the JSON pickers label a source
or a LIT the same way, and every Markdown table the copy buttons build escapes
its cells the same way.
"""

from __future__ import annotations


def source_label(row: dict) -> str:
    """``Citekey · Title (year)``."""
    citekey = row.get("citekey") or ""
    title = (row.get("title") or "").strip()
    year = row.get("year")
    parts = [citekey]
    if title:
        parts.append(title)
    label = " · ".join(parts)
    if year:
        label = f"{label} ({year})"
    return label


def lit_label(row: dict) -> str:
    """``0007 · p. 42 · Section`` for a granular LIT chunk."""
    index = row.get("chunk_index")
    token = f"{int(index):04d}" if index is not None else "?"
    page = row.get("page_in_book")
    page_bit = f"p. {int(page)}" if page is not None else ""
    section = (row.get("section_path") or row.get("locator") or "").strip()
    bits = [token]
    if page_bit:
        bits.append(page_bit)
    if section:
        bits.append(section)
    return " · ".join(bits)


def md_cell(value: object) -> str:
    """One Markdown table cell: no pipe, no line break."""
    return str(value or "").replace("|", "/").replace("\n", " ").strip()
