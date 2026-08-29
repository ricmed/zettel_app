"""Tests for the harvest extraction Markdown dump (Docling/MD headings)."""

from pathlib import Path

import pytest

from zettel.config import AppConfig, HarvestConfig
from zettel.extraction_dump import (
    dump_filename,
    dump_source_extraction,
    list_headings,
    render_extraction_dump,
    run_dump_extraction,
    write_extraction_dump,
)
from zettel.harvester import _process_file
from zettel.state import StateDB


def _cfg(**kwargs) -> AppConfig:
    return AppConfig(**kwargs)


def _source(**overrides) -> dict:
    base = {
        "source_id": "@Smith2020",
        "citekey": "Smith2020",
        "title": "Um ensaio",
        "origin_path": "/inbox/smith.pdf",
        "origin_type": "pdf",
    }
    base.update(overrides)
    return base


RAW_MD = (
    "# Titulo do paper\n\n"
    "preambulo\n\n"
    "## Resumo\n\n"
    "texto do resumo\n\n"
    "### Metodo\n\n"
    "detalhe do metodo\n"
)


class _FakeIdx:
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


def test_dump_filename_sanitizes_citekey():
    assert dump_filename("Smith:2020/foo") == "extraction-Smith_2020_foo.md"


def test_list_headings_collects_h1_to_h6():
    text = "# A\n## B\n### C\n#### D\n##### E\n###### F\nplain"
    assert list_headings(text) == [
        (1, "A"), (2, "B"), (3, "C"), (4, "D"), (5, "E"), (6, "F"),
    ]
    assert list_headings("sem heading") == []


def test_render_includes_frontmatter_outline_and_raw_text():
    md = render_extraction_dump(_source(), RAW_MD, _cfg(pdf_extractor="docling"))
    assert md.startswith("---\n")
    assert "source_id: '@Smith2020'" in md or "source_id: @Smith2020" in md
    assert "citekey: Smith2020" in md
    assert "title: Um ensaio" in md
    assert "origin_path:" in md
    assert "origin_type: pdf" in md
    assert "pdf_extractor: docling" in md
    assert f"chars: {len(RAW_MD)}" in md
    assert "## Headings detectados" in md
    assert "- H1 Titulo do paper" in md
    assert "- H2 Resumo" in md
    assert "- H3 Metodo" in md
    assert "## Texto extraido" in md
    extracted = md.split("## Texto extraido", 1)[1]
    assert extracted.lstrip("\n").startswith("# Titulo do paper")
    assert "### Metodo" in extracted
    assert extracted.rstrip("\n").endswith("detalhe do metodo")


def test_render_no_headings_notes_empty_outline():
    md = render_extraction_dump(_source(), "so paragrafo.", _cfg())
    assert "- (nenhum heading # a ######)" in md
    assert "so paragrafo." in md.split("## Texto extraido", 1)[1]


def test_write_extraction_dump_creates_sanitized_file(tmp_path: Path):
    path = write_extraction_dump(
        tmp_path,
        _source(citekey="Smith:2020/foo"),
        RAW_MD,
        _cfg(),
    )
    assert path == tmp_path / "extraction-Smith_2020_foo.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "# Titulo do paper" in text
    path2 = write_extraction_dump(
        tmp_path,
        _source(citekey="Smith:2020/foo"),
        "# Outro\n",
        _cfg(),
    )
    assert path2 == path
    body = path.read_text(encoding="utf-8")
    assert "# Outro" in body
    assert "Titulo do paper" not in body


def test_run_dump_extraction_writes_from_sqlite(tmp_path: Path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "Titulo", [], None, "h", "/p", "md")
        db.update_source_texts("@S", extracted_text=RAW_MD)
        dest = tmp_path / "dumps"
        stats = run_dump_extraction(_cfg(), db, "@S", dump_dir=dest)
        assert stats == {"sources": 1, "skipped": 0}
        out = dest / "extraction-S.md"
        assert out.exists()
        body = out.read_text(encoding="utf-8")
        assert "- H1 Titulo do paper" in body
        assert "texto do resumo" in body
    finally:
        db.close()


def test_run_dump_extraction_missing_source_raises(tmp_path: Path):
    db = StateDB(tmp_path / "s.db")
    try:
        with pytest.raises(ValueError, match="Fonte nao encontrada"):
            run_dump_extraction(_cfg(), db, "@Missing", dump_dir=tmp_path)
    finally:
        db.close()


def test_run_dump_extraction_skips_source_without_text(tmp_path: Path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "Titulo", [], None, "h", "/p", "md")
        stats = run_dump_extraction(_cfg(), db, "@S", dump_dir=tmp_path)
        assert stats == {"sources": 0, "skipped": 1}
        assert not (tmp_path / "extraction-S.md").exists()
    finally:
        db.close()


def test_dump_source_extraction_loads_db(tmp_path: Path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "Titulo", [], None, "h", "/p", "md")
        db.update_source_texts("@S", extracted_text="# Cap\n\ncorpo")
        path = dump_source_extraction(_cfg(), db, "@S", tmp_path)
        assert path is not None
        assert path.exists()
        assert dump_source_extraction(_cfg(), db, "@Missing", tmp_path) is None
        db.upsert_source("@Empty", "Empty", "T", [], None, "h", "/p", "md")
        assert dump_source_extraction(_cfg(), db, "@Empty", tmp_path) is None
    finally:
        db.close()


def test_process_file_writes_extraction_dump_when_dir_set(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "10_Sources").mkdir(parents=True)
    (vault / "20_Literature").mkdir(parents=True)
    cfg = AppConfig(
        vault_path=vault,
        harvest=HarvestConfig(biblio_llm_enabled=False, biblio_confidence_threshold=0.7),
    )
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
        "# Capitulo 1\n\n"
        "Texto do livro completo o bastante para chunking.\n\n"
        "### Secao interna\n\n"
        "Mais texto da secao.\n",
        encoding="utf-8",
    )
    db = StateDB(tmp_path / "s.db")
    dest = tmp_path / "extraction-dumps"
    try:
        sid, stats = _process_file(
            cfg, db, _FakeIdx(), path, run_id=db.start_run("sig"),
            interactive=False, skip_biblio=False,
            extraction_dump_dir=dest,
        )
        assert sid is not None
        assert stats.get("chunks", 0) >= 1
        dumps = list(dest.glob("extraction-*.md"))
        assert len(dumps) == 1
        body = dumps[0].read_text(encoding="utf-8")
        assert "- H1 Capitulo 1" in body
        assert "- H3 Secao interna" in body
        extracted = body.split("## Texto extraido", 1)[1]
        assert "# Capitulo 1" in extracted
        assert "### Secao interna" in extracted
    finally:
        db.close()
