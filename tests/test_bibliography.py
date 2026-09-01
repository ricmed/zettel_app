"""Tests for bibliographic metadata (ABNT) inference, formatting, and harvest integration."""

from __future__ import annotations

import json

import pytest

from zettel.bibliography import (
    BibliographicMetadata,
    format_abnt,
    format_authors_abnt,
    frontmatter_biblio_fields,
    infer_from_file_metadata,
    invert_author_name,
    is_complete,
    missing_required,
    required_fields,
)
from zettel.config import AppConfig, HarvestConfig
from zettel.harvester import resolve_bibliography as _resolve_bibliography
from zettel.harvester.pipeline import _process_file
from zettel.state import StateDB
from zettel.vault import build_source_note, compose_note, parse_frontmatter


# ── Author / required fields ───────────────────────────────────────────


def test_invert_author_name():
    assert invert_author_name("João Silva Santos") == "SANTOS, João Silva"
    assert invert_author_name("Platão") == "PLATÃO"


def test_format_authors_abnt_et_al():
    authors = ["A Um", "B Dois", "C Tres", "D Quatro"]
    assert format_authors_abnt(authors) == "UM, A et al."


def test_required_fields_livro():
    assert "place" in required_fields("livro")
    assert "publisher" in required_fields("livro")
    assert missing_required(BibliographicMetadata(document_type="livro", title="T")) == [
        "authors", "place", "publisher", "year",
    ]


def test_is_complete_requires_confidence_and_fields():
    meta = BibliographicMetadata(
        document_type="livro",
        confidence=0.9,
        authors=["Ada Lovelace"],
        title="Notas",
        place="Londres",
        publisher="Murray",
        year=1843,
    )
    assert is_complete(meta, 0.7)
    meta.confidence = 0.2
    assert not is_complete(meta, 0.7)


# ── ABNT formatters ────────────────────────────────────────────────────


def test_format_abnt_livro():
    meta = BibliographicMetadata(
        document_type="livro",
        authors=["Karl Marx"],
        title="O Capital",
        subtitle="Critica da economia politica",
        edition="2",
        place="Sao Paulo",
        publisher="Boitempo",
        year=2013,
        translator="Rubens Enderle",
    )
    ref = format_abnt(meta)
    assert ref.startswith("MARX, Karl.")
    assert "O Capital: Critica da economia politica." in ref
    assert "Traducao: Rubens Enderle." in ref
    assert "2 ed." in ref
    assert "Sao Paulo: Boitempo, 2013." in ref


def test_format_abnt_artigo_periodico():
    meta = BibliographicMetadata(
        document_type="artigo_periodico",
        authors=["Ana Silva", "Bruno Costa"],
        title="Aprendizado profundo em saude",
        journal="Revista Brasileira de IA",
        place="Rio de Janeiro",
        volume="12",
        issue="3",
        pages="45-60",
        year=2021,
        doi="10.1234/rbia.2021",
    )
    ref = format_abnt(meta)
    assert "SILVA, Ana; COSTA, Bruno." in ref
    assert "Revista Brasileira de IA," in ref
    assert "v. 12" in ref
    assert "n. 3" in ref
    assert "p. 45-60" in ref
    assert "2021." in ref
    assert "DOI: 10.1234/rbia.2021." in ref


def test_format_abnt_artigo_internet():
    meta = BibliographicMetadata(
        document_type="artigo_internet",
        authors=["Carla Dias"],
        title="Guia de Zettelkasten",
        site_name="Blog Notas",
        year=2024,
        url="https://example.com/zk",
        accessed_at="2024-06-15",
    )
    ref = format_abnt(meta)
    assert "DIAS, Carla." in ref
    assert "Disponivel em: https://example.com/zk." in ref
    assert "Acesso em: 15 jun. 2024." in ref


def test_format_abnt_material_curso():
    meta = BibliographicMetadata(
        document_type="material_curso",
        authors=["Prof. Lima"],
        title="Slides aula 3",
        institution="USP",
        course="Ciencia da Computacao",
        discipline="IA",
        place="Sao Paulo",
        year=2023,
    )
    ref = format_abnt(meta)
    assert "LIMA, Prof." in ref or "LIMA, Prof" in ref
    assert "IA — Ciencia da Computacao." in ref
    assert "USP, Sao Paulo," in ref


def test_format_abnt_tese():
    meta = BibliographicMetadata(
        document_type="tese",
        authors=["Maria Souza"],
        title="Grafos de conhecimento",
        year=2020,
        institution="UNICAMP",
        place="Campinas",
        degree="Tese (Doutorado)",
        advisor="Joao Pedro",
    )
    ref = format_abnt(meta)
    assert "SOUZA, Maria." in ref
    assert "Tese (Doutorado)" in ref
    assert "UNICAMP" in ref
    assert "Orientacao: Joao Pedro." in ref


# ── Heuristics ─────────────────────────────────────────────────────────


def test_infer_tese_from_keywords():
    meta = infer_from_file_metadata(
        {"title": "Estudo X", "authors": ["A B"], "year": 2019},
        "Esta tese (doutorado) analisa o fenomeno Y.",
        "estudo.pdf",
    )
    assert meta.document_type == "tese"
    assert meta.title == "Estudo X"


def test_infer_artigo_internet_from_url():
    meta = infer_from_file_metadata(
        {"title": "Post", "authors": []},
        "Veja em https://site.org/artigo mais detalhes.",
        "post.html.md",
    )
    assert meta.document_type == "artigo_internet"
    assert meta.url and meta.url.startswith("https://")


def test_infer_respects_frontmatter_document_type():
    meta = infer_from_file_metadata(
        {
            "title": "Cap 1",
            "authors": ["X Y"],
            "year": 2010,
            "document_type": "capitulo_livro",
            "place": "SP",
            "publisher": "Ed",
        },
        "texto qualquer",
        "cap.md",
    )
    assert meta.document_type == "capitulo_livro"
    assert meta.confidence >= 0.85
    assert meta.place == "SP"


# ── Vault frontmatter ──────────────────────────────────────────────────


def test_build_source_note_includes_abnt_and_fields():
    meta, body = build_source_note(
        "@Marx2013Capital",
        "Marx2013Capital",
        "O Capital",
        ["Karl Marx"],
        2013,
        "/inbox/x.pdf",
        "pdf",
        "abc",
        document_type="livro",
        biblio_fields={"place": "Sao Paulo", "publisher": "Boitempo", "edition": "2. ed."},
        abnt_reference="MARX, Karl. O Capital. 2. ed. Sao Paulo: Boitempo, 2013.",
    )
    assert meta["document_type"] == "livro"
    assert meta["place"] == "Sao Paulo"
    assert meta["publisher"] == "Boitempo"
    assert "abnt_reference" in meta
    assert "## Referencia ABNT" in body
    assert "Boitempo" in body

    content = compose_note(meta, body)
    parsed, _ = parse_frontmatter(content)
    assert parsed["abnt_reference"].startswith("MARX")
    assert parsed["edition"] == "2. ed."


def test_frontmatter_biblio_fields_omits_core():
    meta = BibliographicMetadata(
        document_type="livro",
        title="T",
        authors=["A B"],
        year=2000,
        place="SP",
        publisher="Ed",
    )
    fields = frontmatter_biblio_fields(meta)
    assert "title" not in fields
    assert "authors" not in fields
    assert fields["place"] == "SP"


# ── Non-interactive harvest path ───────────────────────────────────────


class FakeVectorIndex:
    def __init__(self):
        self.upserted_sources: list[str] = []
        self.upserted_chunks: list[str] = []

    def find_similar_chunks(self, texts, n_results=3):
        return []

    def upsert_source(self, source_id, summary, metadata):
        self.upserted_sources.append(source_id)

    def upsert_chunk(self, chunk_id, text, metadata, **kwargs):
        self.upserted_chunks.append(chunk_id)

    def delete_chunks(self, chunk_ids):
        pass

    def existing_ids(self, collection_name, ids):
        return set()


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig(
        vault_path=tmp_path / "vault",
        harvest=HarvestConfig(biblio_llm_enabled=False, biblio_confidence_threshold=0.7),
    )
    (tmp_path / "vault" / "10_Sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "vault" / "20_Literature").mkdir(parents=True, exist_ok=True)
    return c


def test_resolve_bibliography_noninteractive_skips_without_flag(cfg, tmp_path):
    incomplete = BibliographicMetadata(
        document_type="livro", confidence=0.4, title="T", authors=["A B"], year=2020,
    )
    result = _resolve_bibliography(
        tmp_path / "x.pdf", incomplete, interactive=False, skip_biblio=False, cfg=cfg,
    )
    assert result is None


def test_resolve_bibliography_noninteractive_allows_with_skip_biblio(cfg, tmp_path):
    incomplete = BibliographicMetadata(
        document_type="livro", confidence=0.4, title="T", authors=["A B"], year=2020,
    )
    result = _resolve_bibliography(
        tmp_path / "x.pdf", incomplete, interactive=False, skip_biblio=True, cfg=cfg,
    )
    assert result is incomplete


def test_process_file_skips_incomplete_biblio_noninteractive(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    path = inbox / "book.md"
    path.write_text("# Meu Livro\n\nauthor: Fulano Silva\nyear: 2020\n\nCorpo.", encoding="utf-8")
    # Without YAML frontmatter place/publisher, livro is incomplete.
    path.write_text(
        "---\ntitle: Meu Livro\nauthor: Fulano Silva\nyear: 2020\n---\n\nCorpo do livro.",
        encoding="utf-8",
    )
    idx = FakeVectorIndex()
    sid, stats = _process_file(
        cfg, db, idx, path, run_id=db.start_run("sig"),
        interactive=False, skip_biblio=False,
    )
    assert sid is None
    assert stats == {}


def test_process_file_persists_biblio_with_complete_frontmatter(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    path = inbox / "book.md"
    path.write_text(
        "---\n"
        "title: O Capital\n"
        "author: Karl Marx\n"
        "year: 2013\n"
        "document_type: livro\n"
        "place: Sao Paulo\n"
        "publisher: Boitempo\n"
        "edition: 2. ed.\n"
        "---\n\n"
        "Texto do livro completo o bastante para chunking.\n",
        encoding="utf-8",
    )
    idx = FakeVectorIndex()
    sid, stats = _process_file(
        cfg, db, idx, path, run_id=db.start_run("sig"),
        interactive=False, skip_biblio=False,
    )
    assert sid is not None
    assert stats.get("chunks", 0) >= 1

    src = db.get_source(sid)
    assert src["document_type"] == "livro"
    assert src["abnt_reference"]
    assert "MARX" in src["abnt_reference"]
    biblio = json.loads(src["bibliography_json"])
    assert biblio["place"] == "Sao Paulo"
    assert biblio["publisher"] == "Boitempo"

    # SRC note on disk carries flat fields + abnt_reference
    src_files = list((tmp_path / "vault" / "10_Sources").glob("SRC - *.md"))
    assert len(src_files) == 1
    content = src_files[0].read_text(encoding="utf-8")
    fm, body = parse_frontmatter(content)
    assert fm["document_type"] == "livro"
    assert fm["place"] == "Sao Paulo"
    assert "abnt_reference" in fm
    assert "## Referencia ABNT" in body


def test_process_file_skip_biblio_persists_partial(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    path = inbox / "partial.md"
    path.write_text(
        "---\ntitle: Rascunho\nauthor: Alguem\nyear: 2022\n---\n\nTexto parcial sem editora.",
        encoding="utf-8",
    )
    idx = FakeVectorIndex()
    sid, _ = _process_file(
        cfg, db, idx, path, run_id=db.start_run("sig"),
        interactive=False, skip_biblio=True,
    )
    assert sid is not None
    src = db.get_source(sid)
    assert src["title"] == "Rascunho"
    # May lack place/publisher; still stored.
    assert src.get("bibliography_json")
