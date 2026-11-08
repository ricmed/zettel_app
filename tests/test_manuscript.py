"""The manuscript of a Mesa (#385): the author's order, the notes' words, references once."""

from __future__ import annotations

import pytest
from zettel.bibliography import reference_entries, source_reference
from zettel.manuscript import build_manuscript, placeholder, plain_links

from tests.manuscript_support import ASSET, BODY, N1, N2, desk_project, seed_desk


@pytest.fixture
def setup(tmp_path):
    cfg, db = seed_desk(tmp_path)
    yield cfg, db, tmp_path
    db.close()


def test_cards_become_cited_prose_in_the_authors_order(setup):
    cfg, db, tmp_path = setup
    ms = build_manuscript(cfg, db, desk_project(tmp_path, BODY))
    text = ms.markdown
    assert text.startswith("# ML em crédito\n\nPor que olhar o crédito com dados.")
    assert "Dados como matéria-prima é a tese (ZUBOFF, 2019, p. 12)." in text
    # Native Markdown has no page: the citation comes as stored, without one.
    assert "Boosting supera logística é a tese (ÁLVARES, 2020)." in text
    assert text.index("Dados como matéria") < text.index("Previsão como produto")
    assert text.index("Abertura escrita pelo autor.") < text.index("Boosting supera")
    assert "Definição de Dados como matéria-prima, ver o boosting." in text
    assert "[[" not in text and "<!--" not in text
    assert "Limite de" not in text and "trecho de" not in text


def test_gaps_are_marked_and_can_be_turned_off(setup):
    cfg, db, tmp_path = setup
    text = build_manuscript(cfg, db, desk_project(tmp_path, BODY)).markdown
    assert placeholder("abertura da seção: o que ela mostra e por quê") in text
    assert 'transição de "Dados como matéria-prima" para "Previsão como produto"' in text
    assert "conclusão" in text
    # The author's opening of "Modelos" fills that gap: no marker before it.
    modelos = text.split("## Modelos")[1]
    assert modelos.lstrip().startswith("Abertura escrita pelo autor.")
    bare = build_manuscript(cfg, db, desk_project(tmp_path, BODY), placeholders=False).markdown
    assert "Complementar" not in bare


def test_figures_are_numbered_once_with_their_source(setup):
    cfg, db, tmp_path = setup
    ms = build_manuscript(cfg, db, desk_project(tmp_path, BODY))
    assert len(ms.figures) == 1
    figure = ms.figures[0]
    assert figure.name == "fig-01.png"
    assert figure.caption == (
        "Figura 1 — Curva ROC dos modelos. Fonte: adaptado de (ZUBOFF, 2019, p. 12)."
    )
    assert ms.markdown.count("(figuras/fig-01.png)") == 1


def test_references_close_the_text_once_per_source_in_alphabetical_order(setup):
    cfg, db, tmp_path = setup
    ms = build_manuscript(cfg, db, desk_project(tmp_path, BODY))
    references = ms.markdown.split("## Referências")[1]
    # Three notes from Zuboff, one entry; Álvares sorts before Zuboff despite the accent.
    assert references.count("ZUBOFF") == 1
    assert references.index("ÁLVARES") < references.index("ZUBOFF")
    # Álvares has no stored ABNT text: rebuilt from bibliography_json.
    assert "São Paulo: Atlas, 2020" in references
    assert set(ms.source_ids) == {"@Zuboff2019", "@Alvares2020"}
    assert any("sem fonte" in w for w in ms.warnings)


def test_options_add_quotes_limits_appendix_and_the_blog_style(setup):
    cfg, db, tmp_path = setup
    full = build_manuscript(
        cfg, db, desk_project(tmp_path, BODY), quotes=True, limits=True, appendix=True
    ).markdown
    assert "Ressalva: Limite de Previsão como produto." in full
    assert '> "trecho de Previsão como produto" (ZUBOFF, 2019, p. 40)' in full
    assert full.index("## Referências") < full.index("## Apêndice: notas de origem")
    blog = build_manuscript(cfg, db, desk_project(tmp_path, BODY, style="blog")).markdown
    assert "(segundo Shoshana Zuboff, *A era do capitalismo de vigilância* (2019))" in blog
    assert "## Referências" in blog


def test_a_missing_note_is_a_warning_not_a_failure(setup):
    cfg, db, tmp_path = setup
    body = "## Só\n- [[ZTL - 01JZZZZZZZZZZZZZZZZZZZZZZZ]]\n"
    ms = build_manuscript(cfg, db, desk_project(tmp_path, body))
    assert any("Nota ausente" in w for w in ms.warnings)
    assert "(Nenhuma fonte bibliográfica.)" in ms.markdown


def test_reference_entries_deduplicate_and_fall_back():
    entries, bare = reference_entries(
        [
            {"source_id": "@B", "abnt_reference": "BUENO, C. Obra. 2001."},
            {"source_id": "@A", "abnt_reference": "álvares, a. Obra. 2000."},
            {"source_id": "@B", "abnt_reference": "BUENO, C. Obra. 2001."},
            {"source_id": "@C", "title": "Apostila", "authors": '["Carla Dias"]', "year": None},
        ]
    )
    assert entries == [
        "álvares, a. Obra. 2000.",
        "BUENO, C. Obra. 2001.",
        "DIAS, Carla. Apostila. s.d.",
    ]
    assert bare == ["@C"]
    assert source_reference({"abnt_reference": " X. "}) == ("X.", True)


def test_plain_links_keep_readable_text():
    titles = {N1: "Título da nota"}
    text = f"Ver [[ZTL - {N1} - slug]], [[ZTL - {N2}|apelido]], [[LIT - x/y]] e ![[img.png]]."
    assert plain_links(text, titles) == "Ver Título da nota, apelido, y e ."


def test_a_multi_paragraph_description_makes_a_one_line_caption(setup):
    cfg, db, tmp_path = setup
    db.upsert_asset("@Zuboff2019::img::abc", "@Zuboff2019", "90_Assets/" + ASSET, "abc")
    description = "Tabela [A] de risco.\n\nConceitos: PD"
    db.update_asset_description("@Zuboff2019::img::abc", description, "checksum")
    ms = build_manuscript(cfg, db, desk_project(tmp_path, BODY))
    assert ms.figures[0].caption.startswith("Figura 1 — Tabela (A) de risco. Fonte:")
    assert "Conceitos" not in ms.markdown
