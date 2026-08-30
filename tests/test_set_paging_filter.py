"""Tests for content-start filtering during chunk persist."""

from __future__ import annotations

from pathlib import Path

from zettel.config import AppConfig
from zettel.harvester import _chunk_and_persist
from zettel.paging import ContentPaging
from zettel.state import StateDB


class _FakeIdx:
    def existing_ids(self, collection, ids):
        return set()

    def delete_chunks(self, ids):
        pass

    def upsert_chunk(self, *args, **kwargs):
        pass


def test_chunk_and_persist_skips_pages_before_content_start(tmp_path: Path):
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        inbox_path=tmp_path / "inbox",
        chroma_path=tmp_path / "chroma",
        state_db_path=tmp_path / "state.db",
        cache_path=tmp_path / "cache",
        prompts_path=tmp_path / "prompts",
    )
    cfg.vault_path.mkdir(parents=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        source_id="@TestBook",
        citekey="TestBook",
        title="Test",
        authors=["A"],
        year=2024,
        file_checksum="abc",
        origin_path="x.pdf",
        origin_type="pdf",
    )

    # Distinct chapter texts so chunk ids differ; page_map maps start probes.
    chapters = [
        {"title": "Front", "locator": "Front", "text": "# Front\n\nFRONTMATTERUNIQUE alpha beta gamma delta epsilon.\n"},
        {"title": "Ch1", "locator": "Ch1", "text": "# Ch1\n\nCONTENTSTARTUNIQUE zeta eta theta iota kappa.\n"},
    ]
    page_map = [
        (5, "FRONTMATTERUNIQUE alpha beta gamma delta epsilon."),
        (35, "CONTENTSTARTUNIQUE zeta eta theta iota kappa."),
    ]
    paging = ContentPaging(35, 10, "confirmed")
    n = _chunk_and_persist(
        cfg, db, _FakeIdx(), "@TestBook", chapters, page_map=page_map, paging=paging,
    )
    chunks = db.get_chunks_for_source("@TestBook")
    assert n == len(chunks)
    assert all(
        c.get("page_in_file") is None or c["page_in_file"] >= 35 for c in chunks
    )
    # Content chunk should map to printed page 10
    content = [c for c in chunks if c.get("page_in_file") == 35]
    assert content
    assert content[0]["page_in_book"] == 10
    db.close()


def test_chunk_and_persist_markdown_has_no_pages(tmp_path: Path):
    """Native markdown must not invent page numbers from stray digits."""
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        inbox_path=tmp_path / "inbox",
        chroma_path=tmp_path / "chroma",
        state_db_path=tmp_path / "state.db",
        cache_path=tmp_path / "cache",
        prompts_path=tmp_path / "prompts",
    )
    cfg.vault_path.mkdir(parents=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        source_id="@CourseNotes",
        citekey="CourseNotes",
        title="Anotacoes",
        authors=["Eu"],
        year=2024,
        file_checksum="abc",
        origin_path="notes.md",
        origin_type="md",
    )
    chapters = [{
        "title": "Aula",
        "locator": "Aula",
        "text": "# Aula\n\nPasso 1 do tutorial.\n\n42\n\nContinuacao da anotacao pessoal.",
    }]
    n = _chunk_and_persist(
        cfg, db, _FakeIdx(), "@CourseNotes", chapters, page_map=[], paging=ContentPaging(),
        origin_type="md",
    )
    chunks = db.get_chunks_for_source("@CourseNotes")
    assert n == len(chunks) >= 1
    assert all(c.get("page_in_file") is None for c in chunks)
    assert all(c.get("page_in_book") is None for c in chunks)
    db.close()
