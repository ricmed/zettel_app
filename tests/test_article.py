"""Tests for the `article` command (structured writing from the vault)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import zettel.article as article_mod
from zettel.article import (
    ArticleCatalog,
    CatalogAsset,
    CatalogNote,
    CatalogSource,
    assemble_article,
    catalog_from_retrieved,
    format_outline_for_display,
    retrieved_note_to_dict,
    run_article,
    save_article_note,
    verify_article,
)
from zettel.bibliography import display_author_natural, format_abnt_in_text
from zettel.config import AppConfig
from zettel.retrieval import NoteSearchResult, RetrievedNote
from zettel.schemas import ArticleOutline, ArticleOutlineSection
from zettel.state import StateDB


NOTE_A = "01KZ6QKMSE8K4MWBDQ97N40F5Y"
NOTE_B = "01KZ6QKWGZAQVXBXPVC0T5MB40"


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "article.db")
    yield db
    db.close()


@pytest.fixture
def seeded(db, tmp_path):
    """Minimal source + two notes + one asset."""
    vault = tmp_path / "vault"
    (vault / "90_Assets").mkdir(parents=True)
    img = vault / "90_Assets" / "img-deadbeef12345678.png"
    img.write_bytes(b"fake")

    db.upsert_source(
        source_id="@Negro2026KnowledgeGraphs",
        citekey="Negro2026KnowledgeGraphs",
        title="Knowledge Graphs and LLMs in Action",
        authors=["Alessandro Negro", "Vlasta Kus", "Giuseppe Futia"],
        year=2026,
        file_checksum="abc",
        origin_path="x.pdf",
        origin_type="pdf",
        abnt_reference=(
            "NEGRO, Alessandro et al.. Knowledge Graphs and LLMs in Action. "
            "Shelter Island, NY: Manning Publications Co., 2026."
        ),
        document_type="livro",
    )
    db.upsert_asset(
        asset_id="@Negro2026KnowledgeGraphs::img::deadbeef",
        source_id="@Negro2026KnowledgeGraphs",
        path="90_Assets/img-deadbeef12345678.png",
        image_checksum="deadbeefdeadbeefdeadbeefdeadbeef",
        status="described",
    )
    db.update_asset_description(
        "@Negro2026KnowledgeGraphs::img::deadbeef",
        "Diagrama de grafo de conhecimento",
        "chk1",
    )

    body_a = (
        "# Prompt engineering\n\n"
        "Prompt engineering melhora a qualidade das respostas.\n\n"
        "## Figuras\n\n"
        "![[90_Assets/img-deadbeef12345678.png]]\n\n"
        "O diagrama ilustra um KG.\n"
    )
    body_b = "# Few-shot\n\nFew-shot prompting usa exemplos no prompt.\n"

    db.upsert_note(
        NOTE_A,
        source_id="@Negro2026KnowledgeGraphs",
        path=f"30_Permanent/ZTL - {NOTE_A} - prompt.md",
        title="Prompt engineering e qualidade",
        body=body_a,
    )
    db.upsert_note(
        NOTE_B,
        source_id="@Negro2026KnowledgeGraphs",
        path=f"30_Permanent/ZTL - {NOTE_B} - fewshot.md",
        title="Few-shot prompting",
        body=body_b,
    )
    return vault


class FakeIndex:
    def query_similar_notes(self, *a, **k):
        return []

    def find_similar_chunks(self, *a, **k):
        return []


def test_format_abnt_in_text_variants():
    assert format_abnt_in_text(["João Silva Santos"], 2020) == "(SANTOS, 2020)"
    assert (
        format_abnt_in_text(["Ana Silva", "Bruno Souza"], 2019)
        == "(SILVA; SOUZA, 2019)"
    )
    assert (
        format_abnt_in_text(
            ["Alessandro Negro", "Vlasta Kus", "Giuseppe Futia", "X Y"], 2026
        )
        == "(NEGRO et al., 2026)"
    )
    assert (
        format_abnt_in_text(["Ana Silva"], 2020, pages="p. 10")
        == "(SILVA, 2020, p. 10)"
    )
    assert display_author_natural(["Alessandro Negro", "Vlasta Kus"]) == (
        "Alessandro Negro e Vlasta Kus"
    )
    assert display_author_natural(
        ["A", "B", "C", "D"]
    ) == "A et al."


def test_catalog_from_retrieved_joins_source_and_assets(db, seeded):
    hits = [
        RetrievedNote(
            note_id=NOTE_A,
            score=0.9,
            title="Prompt engineering e qualidade",
            document=db.get_note(NOTE_A)["body"],
            metadata={"source_id": "@Negro2026KnowledgeGraphs"},
            passed_floor=True,
        ),
        RetrievedNote(
            note_id=NOTE_B,
            score=0.8,
            title="Few-shot prompting",
            document=db.get_note(NOTE_B)["body"],
            metadata={"source_id": "@Negro2026KnowledgeGraphs"},
            passed_floor=True,
        ),
    ]

    cfg = AppConfig(vault_path=seeded, prompts_path=Path("prompts"))
    catalog = catalog_from_retrieved(
        cfg,
        db,
        "prompt engineering",
        "blog",
        [retrieved_note_to_dict(h) for h in hits],
    )

    assert NOTE_A in catalog.notes
    assert "@Negro2026KnowledgeGraphs" in catalog.sources
    src = catalog.sources["@Negro2026KnowledgeGraphs"]
    assert "NEGRO" in src.in_text_cite
    assert catalog.notes[NOTE_A].assets
    assert "90_Assets/" in catalog.notes[NOTE_A].assets[0].path


def test_merge_retrieved_notes_keeps_best_score():
    from zettel.article import merge_retrieved_notes, retrieved_note_to_dict

    a = RetrievedNote(note_id=NOTE_A, score=0.5, title="A")
    b = RetrievedNote(note_id=NOTE_B, score=0.8, title="B")
    a2 = RetrievedNote(note_id=NOTE_A, score=0.9, title="A better")
    merged = merge_retrieved_notes(
        [retrieved_note_to_dict(a)], [b, a2], max_notes=10
    )
    by_id = {d["note_id"]: d for d in merged}
    assert by_id[NOTE_A]["score"] == 0.9
    assert by_id[NOTE_A]["title"] == "A better"
    assert NOTE_B in by_id


def test_personality_neutral_noop(db, seeded):
    from zettel.article import apply_personality_rewrite

    root = Path(__file__).resolve().parents[1]
    cfg = AppConfig(
        vault_path=seeded,
        prompts_path=root / "prompts",
    )
    cfg.retrieval.article.personalities_path = root / "config" / "personalities.yaml"
    body = "# Titulo\n\nTexto.\n"
    out, called = apply_personality_rewrite(cfg, db, body, "neutral")
    assert out == body
    assert called is False


def test_assemble_academic_with_cites_and_figure(seeded):
    catalog = ArticleCatalog(topic="KG", style="academic")
    catalog.sources["@Negro2026KnowledgeGraphs"] = CatalogSource(
        source_id="@Negro2026KnowledgeGraphs",
        citekey="Negro2026KnowledgeGraphs",
        title="Knowledge Graphs and LLMs in Action",
        authors=["Alessandro Negro", "Vlasta Kus", "Giuseppe Futia"],
        year=2026,
        abnt_reference=(
            "NEGRO, Alessandro et al.. Knowledge Graphs and LLMs in Action. "
            "Shelter Island, NY: Manning Publications Co., 2026."
        ),
    )
    catalog.assets["aid1"] = CatalogAsset(
        asset_id="aid1",
        path="90_Assets/img-deadbeef12345678.png",
        description="Diagrama de KG",
        source_id="@Negro2026KnowledgeGraphs",
    )
    catalog.notes[NOTE_A] = CatalogNote(
        note_id=NOTE_A,
        title="Prompt engineering",
        body="...",
        wiki_link=f"[[ZTL - {NOTE_A}]]",
        source_id="@Negro2026KnowledgeGraphs",
    )

    outline = ArticleOutline(
        title="Grafos e LLMs",
        thesis="Grafos melhoram LLMs.",
        sections=[
            ArticleOutlineSection(
                heading="Introducao",
                goal="Apresentar",
                note_ids=[NOTE_A],
            )
        ],
    )
    section = (
        "## Introducao\n\n"
        "Os grafos apoiam LLMs (NEGRO et al., 2026).\n\n"
        "![[90_Assets/img-deadbeef12345678.png]]\n\n"
        "<!-- cites: @Negro2026KnowledgeGraphs -->\n"
    )
    meta, body, cited, warnings = assemble_article(
        outline, [section], catalog, seeded
    )
    assert "Grafos e LLMs" in body
    assert "## Referencias" in body
    assert "NEGRO, Alessandro" in body
    assert "**Figura 1**" in body
    assert "@Negro2026KnowledgeGraphs" in cited
    assert "## Origem no vault" in body
    assert "cites:" not in body


def test_assemble_blog_light_reading_list():
    catalog = ArticleCatalog(topic="prompt", style="blog")
    catalog.sources["@Negro2026KnowledgeGraphs"] = CatalogSource(
        source_id="@Negro2026KnowledgeGraphs",
        citekey="Negro2026KnowledgeGraphs",
        title="Knowledge Graphs and LLMs in Action",
        authors=["Alessandro Negro"],
        year=2026,
    )
    catalog.notes[NOTE_A] = CatalogNote(
        note_id=NOTE_A,
        title="X",
        body="y",
        wiki_link=f"[[ZTL - {NOTE_A}]]",
        source_id="@Negro2026KnowledgeGraphs",
    )
    outline = ArticleOutline(
        title="Blog post",
        thesis="Tese curta.",
        sections=[
            ArticleOutlineSection(heading="Gancho", goal="g", note_ids=[NOTE_A])
        ],
    )
    section = (
        "## Gancho\n\n"
        "Como observa Alessandro Negro em *Knowledge Graphs*, o KG ajuda.\n"
        "<!-- cites: Negro2026KnowledgeGraphs -->\n"
    )
    _, body, cited, _ = assemble_article(outline, [section], catalog)
    assert "## Para saber mais" in body
    assert "Alessandro Negro" in body
    assert "Knowledge Graphs and LLMs in Action" in body
    assert cited == ["@Negro2026KnowledgeGraphs"]


def test_outline_schema_and_display():
    outline = ArticleOutline(
        title="T",
        thesis="Tes.",
        sections=[
            ArticleOutlineSection(
                heading="A", goal="g", note_ids=[NOTE_A], figure_asset_ids=[]
            )
        ],
    )
    text = format_outline_for_display(outline)
    assert "Tese:" in text
    assert "**A**" in text


def test_run_article_no_evidence(db, monkeypatch):
    def fake_search(self, *a, **k):
        return NoteSearchResult(hits=[], candidates=[])

    monkeypatch.setattr(
        "zettel.retrieval.Retriever.search_notes", fake_search
    )
    monkeypatch.setattr(
        article_mod,
        "call_llm",
        lambda llm, prompt, **kwargs: json.dumps({"queries": ["tema inexistente"]}),
    )
    monkeypatch.setattr(
        article_mod, "get_llm", lambda cfg, temperature=None: object()
    )

    prompts = Path(__file__).resolve().parents[1] / "prompts"
    result = run_article(
        AppConfig(prompts_path=prompts),
        db,
        FakeIndex(),
        "tema inexistente",
        skip_judge=True,
    )
    assert result.no_evidence


def test_run_article_full_mock(db, seeded, monkeypatch, tmp_path):
    hits = [
        RetrievedNote(
            note_id=NOTE_A,
            score=0.9,
            title="Prompt engineering e qualidade",
            document=db.get_note(NOTE_A)["body"],
            metadata={"source_id": "@Negro2026KnowledgeGraphs"},
            passed_floor=True,
        ),
    ]
    monkeypatch.setattr(
        "zettel.retrieval.Retriever.search_notes",
        lambda self, *a, **k: NoteSearchResult(hits=hits, candidates=hits),
    )

    enrich_json = json.dumps({"queries": ["prompt engineering", "few-shot"]})
    outline_json = json.dumps(
        {
            "title": "Tecnicas de Prompting",
            "thesis": "Prompting melhora respostas.",
            "style_notes": "claro",
            "sections": [
                {
                    "heading": "O que e prompting",
                    "goal": "Definir",
                    "note_ids": [NOTE_A],
                    "figure_asset_ids": [
                        "@Negro2026KnowledgeGraphs::img::deadbeef"
                    ],
                },
                {
                    "heading": "Conclusao",
                    "goal": "Fechar",
                    "note_ids": [NOTE_A],
                    "figure_asset_ids": [],
                },
            ],
        }
    )
    section1 = (
        "## O que e prompting\n\n"
        "Como observa Alessandro Negro em *Knowledge Graphs and LLMs in Action*, "
        "prompts bem feitos ajudam.\n\n"
        "![[90_Assets/img-deadbeef12345678.png]]\n"
        "<!-- cites: @Negro2026KnowledgeGraphs -->\n"
    )
    section2 = (
        "## Conclusao\n\n"
        "Vale praticar prompting com cuidado.\n"
        "<!-- cites: @Negro2026KnowledgeGraphs -->\n"
    )
    responses = [enrich_json, outline_json, section1, section2]

    def fake_llm(llm, prompt, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(article_mod, "call_llm", fake_llm)
    monkeypatch.setattr(
        article_mod, "get_llm", lambda cfg, temperature=None: object()
    )

    prompts = Path(__file__).resolve().parents[1] / "prompts"
    root = Path(__file__).resolve().parents[1]
    cfg = AppConfig(vault_path=seeded, prompts_path=prompts)
    cfg.retrieval.article.personalities_path = root / "config" / "personalities.yaml"

    result = run_article(
        cfg, db, FakeIndex(), "prompt engineering",
        style="blog",
        approve_outline=lambda o: ("approve", None),
        skip_judge=True,
        personality="neutral",
    )
    assert result.llm_called
    assert "Tecnicas de Prompting" in result.body
    assert "## Para saber mais" in result.body
    assert "![[90_Assets/" in result.body

    dest = save_article_note(result, seeded)
    assert dest.exists()
    assert dest.name.startswith("ART - ")
    text = dest.read_text(encoding="utf-8")
    assert "type: article" in text


def test_verify_missing_embed(tmp_path):
    catalog = ArticleCatalog(topic="t", style="blog")
    body = "# T\n\n![[90_Assets/missing.png]]\n"
    warnings = verify_article(body, catalog, tmp_path)
    assert any("missing.png" in w for w in warnings)
