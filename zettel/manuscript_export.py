"""Files out of a Mesa (ADR-067): Markdown bundle, DOCX, BibTeX and JSON Canvas.

The bundle is the source of every other format: ``manuscrito.md`` with
relative figure links, ``figuras/`` with the copies, and ``referencias.bib``
for Zotero or LaTeX. The DOCX is that Markdown through Pandoc (``pypandoc``
ships the binary), so TeX becomes Word equations and figures are embedded.
The canvas is a one-way picture of the desk for Obsidian's Canvas.
"""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from zettel.manuscript import FIGURES_DIR, Manuscript

MANUSCRIPT_FILE = "manuscrito.md"
BIBTEX_FILE = "referencias.bib"
DEFAULT_REFERENCE_DOC = Path(__file__).resolve().parent / "resources" / "manuscript-reference.docx"

_BIBTEX_TYPES = {
    "livro": "book",
    "capitulo_livro": "incollection",
    "artigo_periodico": "article",
    "artigo_internet": "online",
    "material_curso": "misc",
    "tese": "phdthesis",
    "anais_evento": "inproceedings",
    "relatorio": "techreport",
}
_BIBTEX_FIELDS = (
    ("journal", "journal"),
    ("booktitle", "book_title"),
    ("volume", "volume"),
    ("number", "issue"),
    ("pages", "pages"),
    ("edition", "edition"),
    ("publisher", "publisher"),
    ("address", "place"),
    ("institution", "institution"),
    ("isbn", "isbn"),
    ("doi", "doi"),
    ("url", "url"),
    ("urldate", "accessed_at"),
)


def _bib_value(value: Any) -> str:
    text = " ".join(str(value).split())
    return text.replace("{", "(").replace("}", ")")


def bibtex_entry(source: dict) -> str:
    """One BibTeX entry from a ``sources`` row, keyed by its citekey."""
    try:
        record = json.loads(source.get("bibliography_json") or "{}")
    except ValueError:
        record = {}
    if not isinstance(record, dict):
        record = {}
    kind = _BIBTEX_TYPES.get(str(record.get("document_type") or ""), "misc")
    if kind == "phdthesis" and "mestr" in str(record.get("degree") or "").lower():
        kind = "mastersthesis"
    key = re.sub(r"[^\w-]", "", str(source.get("citekey") or source.get("source_id") or "fonte"))
    title = record.get("chapter_title") if kind == "incollection" else None
    title = title or record.get("title") or source.get("title") or ""
    if record.get("subtitle") and kind != "incollection":
        title = f"{title}: {record['subtitle']}"
    authors = record.get("chapter_authors") or record.get("authors") or []
    if not authors:
        try:
            authors = json.loads(source.get("authors") or "[]")
        except ValueError:
            authors = []
    fields = [("title", title), ("author", " and ".join(str(a) for a in authors))]
    fields.append(("year", record.get("year") or source.get("year") or ""))
    if kind == "incollection":
        fields.append(("booktitle", record.get("book_title") or record.get("title") or ""))
        fields.append(("editor", " and ".join(record.get("book_editors") or [])))
    if kind in {"phdthesis", "mastersthesis"}:
        fields.append(("school", record.get("institution") or ""))
    fields.extend(
        (name, record.get(key_name) or "")
        for name, key_name in _BIBTEX_FIELDS
        if not (kind == "incollection" and name == "booktitle")
    )
    body = ",\n".join(f"  {name} = {{{_bib_value(value)}}}" for name, value in fields if value)
    return f"@{kind}{{{key},\n{body}\n}}\n"


def write_bundle(manuscript: Manuscript, db: Any, destination: Path) -> Path:
    """Write ``manuscrito.md``, ``figuras/`` and ``referencias.bib`` into ``destination``."""
    destination.mkdir(parents=True, exist_ok=True)
    (destination / MANUSCRIPT_FILE).write_text(manuscript.markdown, encoding="utf-8", newline="\n")
    if manuscript.figures:
        figures = destination / FIGURES_DIR
        figures.mkdir(exist_ok=True)
        for figure in manuscript.figures:
            shutil.copyfile(figure.vault_path, figures / figure.name)
    sources = [db.get_source(sid) for sid in manuscript.source_ids]
    entries = [bibtex_entry(row) for row in sources if row]
    (destination / BIBTEX_FILE).write_text("\n".join(entries), encoding="utf-8", newline="\n")
    return destination


def reference_doc(cfg: Any) -> Path:
    configured = cfg.manuscript.docx_reference_path
    return Path(configured) if configured else DEFAULT_REFERENCE_DOC


def to_docx(bundle: Path, output: Path, reference: Path) -> Path:
    """Convert the bundle's Markdown with Pandoc; figures resolve from the bundle."""
    import pypandoc

    pypandoc.convert_file(
        str(bundle / MANUSCRIPT_FILE),
        "docx",
        format="markdown",
        outputfile=str(output),
        extra_args=[f"--resource-path={bundle}", f"--reference-doc={reference}"],
        cworkdir=str(bundle),
    )
    return output


def zip_bundle(bundle: Path, output: Path) -> Path:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    return output


# ── JSON Canvas (https://jsoncanvas.org, 1.0) ─────────────────────────

_CARD_W, _CARD_H, _TEXT_H, _GAP, _COLUMN_GAP, _HEADER = 400, 240, 160, 40, 120, 80


def _vault_relative(cfg: Any, path: str | None) -> str | None:
    if not path:
        return None
    vault = Path(cfg.vault_path).resolve()
    candidate = Path(path)
    resolved = candidate if candidate.is_absolute() else vault / candidate
    try:
        return resolved.resolve().relative_to(vault).as_posix()
    except (ValueError, OSError):
        return None


def to_canvas(cfg: Any, db: Any, project: Any) -> dict:
    """Sections as side-by-side groups, cards as file nodes, bridges as text nodes.

    Edges are the walkable connections between two cards on the desk.
    """
    from zettel.connector.links import relation_label
    from zettel.project_file import Card, Text

    rows = db.get_notes_by_ids(project.body.note_ids())
    nodes: list[dict] = []
    node_of: dict[str, str] = {}
    column = 0
    for s_index, section in enumerate(project.body.sections):
        visible = [b for b in section.blocks if not (isinstance(b, Text) and b.is_blank)]
        if section.heading is None and not visible:
            continue
        x = column * (_CARD_W + _COLUMN_GAP)
        y = _HEADER
        children: list[dict] = []
        for b_index, block in enumerate(visible):
            node_id = f"s{s_index}b{b_index}"
            if isinstance(block, Card):
                file = _vault_relative(cfg, (rows.get(block.note_id) or {}).get("path"))
                if file is None:
                    node = {"type": "text", "text": f"Nota ausente: {block.note_id}"}
                else:
                    node = {"type": "file", "file": file}
                    node_of[block.note_id] = node_id
                height = _CARD_H
            else:
                node = {"type": "text", "text": block.content}
                height = _TEXT_H
            children.append(
                {"id": node_id, **node, "x": x, "y": y, "width": _CARD_W, "height": height}
            )
            y += height + _GAP
        label = section.title or "Antes da primeira seção"
        group_height = max(y - _GAP, _HEADER + _CARD_H) + _GAP
        nodes.append(
            {
                "id": f"s{s_index}",
                "type": "group",
                "label": label,
                "x": x - _GAP // 2,
                "y": 0,
                "width": _CARD_W + _GAP,
                "height": group_height,
            }
        )
        nodes.extend(children)
        column += 1
    edges = []
    seen: set[tuple[str, str, str]] = set()
    for edge in db.get_connections_for_notes(sorted(node_of)):
        source, target = edge["source_note_id"], edge["target_note_id"]
        relation = edge.get("relation_type") or "related"
        key = (source, target, relation)
        if source in node_of and target in node_of and key not in seen:
            seen.add(key)
            edges.append(
                {
                    "id": f"e{len(edges)}",
                    "fromNode": node_of[source],
                    "toNode": node_of[target],
                    "label": relation_label(relation),
                }
            )
    return {"nodes": nodes, "edges": edges}


def canvas_path(project: Any) -> Path:
    return project.path.with_suffix(".canvas")


def write_canvas(cfg: Any, db: Any, project: Any, destination: Path | None = None) -> Path:
    path = destination or canvas_path(project)
    data = json.dumps(to_canvas(cfg, db, project), ensure_ascii=False, indent=2)
    path.write_text(data + "\n", encoding="utf-8", newline="\n")
    return path
