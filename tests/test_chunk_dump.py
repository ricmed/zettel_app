"""Tests for the harvest chunk markdown dump."""

from pathlib import Path

import pytest
from zettel.chunk_dump import (
    dump_filename,
    dump_source_chunks,
    overlap_prefix_len,
    render_chunk_dump,
    run_dump_chunks,
    sanitize_citekey,
    write_chunk_dump,
)
from zettel.config import AppConfig
from zettel.state import StateDB


def _cfg(**chunking) -> AppConfig:
    cfg = AppConfig()
    for k, v in chunking.items():
        setattr(cfg.chunking, k, v)
    return cfg


def _source(**overrides) -> dict:
    base = {
        "source_id": "@Smith2020",
        "citekey": "Smith2020",
        "title": "Um ensaio",
        "origin_path": "/inbox/smith.pdf",
        "origin_type": "pdf",
        "content_start_file_page": 15,
        "content_start_book_page": 1,
        "page_offset": 14,
        "page_offset_confidence": "mapped",
    }
    base.update(overrides)
    return base


def _chunk(index: int, text: str, **overrides) -> dict:
    base = {
        "chunk_id": f"@Smith2020::ch000::{index}",
        "chapter_id": "@Smith2020::ch000",
        "section_path": "Cap 1 > Intro",
        "chunk_index": index,
        "text": text,
        "page_in_file": 15 + index,
        "page_in_book": 1 + index,
        "page_confidence": "mapped",
    }
    base.update(overrides)
    return base


def test_sanitize_citekey_strips_unsafe_chars():
    assert sanitize_citekey("Smith:2020/foo") == "Smith_2020_foo"
    assert dump_filename("Smith:2020/foo") == "chunks-Smith_2020_foo.md"
    assert sanitize_citekey("???") == "unknown"


def test_overlap_prefix_len_finds_shared_boundary():
    prev = "abcXYZdef"
    curr = "XYZdefghi"
    assert overlap_prefix_len(prev, curr, cap=200) == 6
    assert overlap_prefix_len(prev, curr, cap=6) == 6
    assert overlap_prefix_len("", curr, cap=200) == 0
    assert overlap_prefix_len(prev, curr, cap=0) == 0


def test_render_includes_frontmatter_metadata_and_raw_text():
    cfg = _cfg(chunk_size=1500, chunk_overlap=400, min_section_chars=200)
    md = render_chunk_dump(
        _source(),
        [_chunk(0, "primeiro texto do chunk")],
        cfg,
    )
    assert md.startswith("---\n")
    assert "source_id: '@Smith2020'" in md or "source_id: @Smith2020" in md
    assert "citekey: Smith2020" in md
    assert "title: Um ensaio" in md
    assert "origin_path:" in md
    assert "origin_type: pdf" in md
    assert "chunk_size: 1500" in md
    assert "chunk_overlap: 400" in md
    assert "min_section_chars: 200" in md
    assert "content_start_file_page: 15" in md
    assert "content_start_book_page: 1" in md
    assert "n_chunks: 1" in md
    assert "primeiro texto do chunk" in md
    assert "# Chunk 000" in md
    assert "chunk_id: @Smith2020::ch000::0" in md
    assert "section_path: Cap 1 > Intro" in md
    assert "## Sumario" in md


def test_render_orders_by_chunk_index_and_reports_overlap():
    cfg = _cfg(chunk_overlap=200)
    prev = "abcXYZdef"
    curr = "XYZdefghi"
    md = render_chunk_dump(
        _source(),
        [
            _chunk(1, curr, section_path="Cap 1 > Depois"),
            _chunk(0, prev, section_path="Cap 1 > Antes"),
        ],
        cfg,
    )
    first = md.index("# Chunk 000")
    second = md.index("# Chunk 001")
    assert first < second
    assert "overlap_prev=0" in md
    assert "overlap_prev=6" in md
    assert "- overlap_prev: 0" in md
    assert "- overlap_prev: 6" in md
    # Sumario lists index 000 before 001 despite input order
    sumario = md[md.index("## Sumario") : md.index("# Chunk 000")]
    assert sumario.index("#000") < sumario.index("#001")


def test_render_empty_chunks_still_has_frontmatter():
    md = render_chunk_dump(_source(), [], _cfg())
    assert "n_chunks: 0" in md
    assert "(nenhum chunk persistido)" in md
    assert "# Chunk " not in md


def test_write_chunk_dump_creates_sanitized_file(tmp_path: Path):
    cfg = _cfg()
    path = write_chunk_dump(
        tmp_path,
        _source(citekey="Smith:2020/foo"),
        [_chunk(0, "corpo do chunk")],
        cfg,
    )
    assert path == tmp_path / "chunks-Smith_2020_foo.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "corpo do chunk" in text
    # overwrite
    path2 = write_chunk_dump(
        tmp_path,
        _source(citekey="Smith:2020/foo"),
        [_chunk(0, "segunda gravacao")],
        cfg,
    )
    assert path2 == path
    assert "segunda gravacao" in path.read_text(encoding="utf-8")
    assert "corpo do chunk" not in path.read_text(encoding="utf-8")


def test_run_dump_chunks_writes_from_sqlite(tmp_path: Path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "Titulo", [], None, "h", "/p", "md")
        db.upsert_chapter("@S::ch000", "@S", "Cap", "ch_hash")
        db.upsert_chunk(
            "@S::ch000::a",
            "@S",
            "@S::ch000",
            "texto alpha",
            "cka",
            section_path="Cap > Alpha",
            chunk_index=0,
            page_in_file=1,
            page_in_book=1,
            page_confidence="mapped",
        )
        db.upsert_chunk(
            "@S::ch000::b",
            "@S",
            "@S::ch000",
            "texto beta",
            "ckb",
            section_path="Cap > Beta",
            chunk_index=1,
            page_in_file=2,
            page_in_book=2,
            page_confidence="mapped",
        )
        dest = tmp_path / "dumps"
        stats = run_dump_chunks(_cfg(), db, "@S", dump_dir=dest)
        assert stats["sources"] == 1
        out = dest / "chunks-S.md"
        assert out.exists()
        body = out.read_text(encoding="utf-8")
        assert "texto alpha" in body
        assert "texto beta" in body
        assert "# Chunk 000" in body
        assert "# Chunk 001" in body
    finally:
        db.close()


def test_run_dump_chunks_missing_source_raises(tmp_path: Path):
    db = StateDB(tmp_path / "s.db")
    try:
        with pytest.raises(ValueError, match="Fonte nao encontrada"):
            run_dump_chunks(_cfg(), db, "@Missing", dump_dir=tmp_path)
    finally:
        db.close()


def test_dump_source_chunks_loads_db(tmp_path: Path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "Titulo", [], None, "h", "/p", "md")
        db.upsert_chapter("@S::ch000", "@S", "Cap", "ch_hash")
        db.upsert_chunk(
            "@S::ch000::a",
            "@S",
            "@S::ch000",
            "hello",
            "cka",
            chunk_index=0,
        )
        path = dump_source_chunks(_cfg(), db, "@S", tmp_path)
        assert path is not None
        assert path.exists()
        assert dump_source_chunks(_cfg(), db, "@Missing", tmp_path) is None
    finally:
        db.close()


class _FakeIdx:
    def __init__(self):
        self.chunks_store: set[str] = set()

    def upsert_chunk(self, chunk_id, text, metadata, **kwargs):
        self.chunks_store.add(chunk_id)

    def delete_chunks(self, chunk_ids):
        for cid in chunk_ids:
            self.chunks_store.discard(cid)

    def existing_ids(self, collection_name, ids):
        return {cid for cid in ids if cid in self.chunks_store}


def test_run_rechunk_writes_dump_when_dump_dir_set(tmp_path: Path):
    from zettel.harvester import run_rechunk

    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
        db.update_source_texts(
            "@S",
            extracted_text="# Cap\n\n### Alpha\n\n" + "conteudo suficientemente longo. " * 30,
        )
        dest = tmp_path / "dumps"
        stats = run_rechunk(AppConfig(), db, _FakeIdx(), "@S", dump_dir=dest)
        assert stats["sources"] == 1
        out = dest / "chunks-S.md"
        assert out.exists()
        body = out.read_text(encoding="utf-8")
        assert "conteudo suficientemente longo" in body
        assert "chunk_size:" in body
    finally:
        db.close()
