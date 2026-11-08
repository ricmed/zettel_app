"""Files out of a Mesa (#385): bundle, BibTeX, DOCX through Pandoc, JSON Canvas."""

from __future__ import annotations

import json
import zipfile

import pytest
from zettel.manuscript import build_manuscript
from zettel.manuscript_export import (
    DEFAULT_REFERENCE_DOC,
    bibtex_entry,
    to_canvas,
    to_docx,
    write_bundle,
    zip_bundle,
)

from tests.manuscript_support import BODY, N1, N2, N3, N4, desk_project, seed_desk


@pytest.fixture
def setup(tmp_path):
    cfg, db = seed_desk(tmp_path / "desk")
    yield cfg, db, tmp_path / "desk"
    db.close()


def test_bundle_holds_markdown_figures_and_bibtex(setup, tmp_path):
    cfg, db, root = setup
    ms = build_manuscript(cfg, db, desk_project(root, BODY))
    bundle = write_bundle(ms, db, tmp_path / "out")
    assert (bundle / "manuscrito.md").read_text(encoding="utf-8") == ms.markdown
    assert (bundle / "figuras" / "fig-01.png").read_bytes().startswith(b"\x89PNG")
    bib = (bundle / "referencias.bib").read_text(encoding="utf-8")
    assert bib.count("@") == 2 and "@book{Alvares2020," in bib and "publisher = {Atlas}" in bib
    archive = zip_bundle(bundle, tmp_path / "m.zip")
    with zipfile.ZipFile(archive) as z:
        assert sorted(z.namelist()) == ["figuras/fig-01.png", "manuscrito.md", "referencias.bib"]


def test_bibtex_entry_maps_chapters_and_theses():
    chapter = bibtex_entry(
        {
            "citekey": "Silva2020Cap",
            "bibliography_json": json.dumps(
                {
                    "document_type": "capitulo_livro",
                    "chapter_title": "Risco",
                    "chapter_authors": ["Ana Silva"],
                    "book_title": "Finanças",
                    "book_editors": ["Rui Lima"],
                    "pages": "10-20",
                    "year": 2020,
                }
            ),
        }
    )
    assert chapter.startswith("@incollection{Silva2020Cap,")
    assert "title = {Risco}" in chapter and "booktitle = {Finanças}" in chapter
    assert "editor = {Rui Lima}" in chapter and "pages = {10-20}" in chapter
    thesis = bibtex_entry(
        {
            "citekey": "Dias2021",
            "bibliography_json": json.dumps(
                {"document_type": "tese", "degree": "Mestrado", "institution": "USP"}
            ),
            "title": "Crédito",
            "authors": '["Carla Dias"]',
            "year": 2021,
        }
    )
    assert thesis.startswith("@mastersthesis{Dias2021,") and "school = {USP}" in thesis


def test_canvas_lays_sections_out_and_links_cards(setup):
    cfg, db, root = setup
    db.upsert_note_connection(N1, N2, "extends", "amplia", origin="manual")
    db.upsert_note_connection(N4, N3, "contradicts", "x", status="rejected")
    for note_id in (N1, N2, N3, N4):
        (cfg.vault_path / "30_Permanent").mkdir(exist_ok=True)
        (cfg.vault_path / f"30_Permanent/ZTL - {note_id} - n.md").write_text("x", encoding="utf-8")
    canvas = to_canvas(cfg, db, desk_project(root, BODY))
    groups = [n for n in canvas["nodes"] if n["type"] == "group"]
    assert [g["label"] for g in groups] == ["Antes da primeira seção", "Dados", "Modelos"]
    files = [n["file"] for n in canvas["nodes"] if n["type"] == "file"]
    assert files[0] == f"30_Permanent/ZTL - {N1} - n.md" and len(files) == 5
    texts = [n["text"] for n in canvas["nodes"] if n["type"] == "text"]
    assert "Abertura escrita pelo autor." in texts
    # Only walkable edges between two cards; the rejected one is gone.
    assert [e["label"] for e in canvas["edges"]] == ["estende"]


@pytest.mark.slow
def test_docx_embeds_figures_equations_and_the_gap_style(setup, tmp_path):
    cfg, db, root = setup
    row = db.get_note(N1)
    formula = "\n\n$$JB = \\frac{n}{6} S^2$$\n\n## Limites"
    db.conn.execute(
        "UPDATE notes SET body = ? WHERE note_id = ?",
        (row["body"].replace("\n\n## Limites", formula), N1),
    )
    db.conn.commit()
    ms = build_manuscript(cfg, db, desk_project(root, BODY.replace("## Dados", "## Dados\n")))
    bundle = write_bundle(ms, db, tmp_path / "out")
    output = to_docx(bundle, tmp_path / "m.docx", DEFAULT_REFERENCE_DOC)
    with zipfile.ZipFile(output) as z:
        names = z.namelist()
        document = z.read("word/document.xml").decode("utf-8")
        styles = z.read("word/styles.xml").decode("utf-8")
    assert any(name.startswith("word/media/") for name in names)
    assert "<m:oMath" in document
    assert 'w:styleId="Complementar"' in styles and "Complementar" in document
    assert "Referências" in document
