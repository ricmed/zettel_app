"""Chapter and source summaries (ADR-047).

The load-bearing properties, in order of how much they would cost to get wrong:

* an unchanged chapter costs **zero** LLM calls on a second pass;
* the per-chapter note count is a SQL aggregate, never a model's guess;
* the vault blocks are managed blocks, so hand edits outside them survive;
* a summary is a *routing* artifact and must never reach `ask`'s evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from zettel.config import AppConfig
from zettel.state import StateDB
from zettel.summarize import (
    CHAPTER_MAP_BLOCK,
    SOURCE_SUMMARY_BLOCK,
    chapter_summary_document,
    chapter_text_for_summary,
    generate_summaries,
    parse_topics,
    refresh_chapter_map,
    render_chapter_map,
    source_summary_checksum,
)
from zettel.vault import build_literature_index_note, read_managed_block, safe_write_note

SOURCE_ID = "@Autor2024Obra"
CITEKEY = "Autor2024Obra"
TITLE = "Uma Obra de Teste"


class FakeIndex:
    """Records chapter-summary upserts without touching Chroma."""

    def __init__(self) -> None:
        self.upserts: list[tuple[str, str, dict]] = []

    def upsert_chapter_summary(self, chapter_id, text, metadata):
        self.upserts.append((chapter_id, text, metadata))


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(vault_path=tmp_path / "vault", state_db_path=tmp_path / "state.db")


@pytest.fixture
def db(tmp_path: Path):
    database = StateDB(tmp_path / "state.db")
    yield database
    database.close()


def _seed(db: StateDB, *, chapters=2, chunks_per_chapter=2) -> None:
    db.upsert_source(SOURCE_ID, CITEKEY, TITLE, ["Autor"], 2024, "hash", "/tmp/o.pdf", "pdf")
    for ci in range(chapters):
        chapter_id = f"{SOURCE_ID}::ch{ci:03d}"
        db.upsert_chapter(chapter_id, SOURCE_ID, f"Capitulo {ci}", f"chk{ci}")
        for ki in range(chunks_per_chapter):
            db.upsert_chunk(
                f"{chapter_id}::c{ki}",
                SOURCE_ID,
                chapter_id,
                f"Texto do capitulo {ci}, parte {ki}.",
                f"cs{ci}{ki}",
                chunk_index=ci * 10 + ki,
                page_in_book=100 + ci * 10 + ki,
                status="persisted",
            )


def _fake_llm(monkeypatch, calls: list[str]):
    """Stub the one seam every summary call goes through."""

    def fake(cfg, db, prompt_name, mapping, label):
        calls.append(prompt_name)
        payload = {
            "summary": f"Resumo de {mapping.get('chapter_title', TITLE)}.",
            "key_topics": ["alfa", "beta"],
        }
        return json.dumps(payload, ensure_ascii=False), False

    monkeypatch.setattr("zettel.summarize._call_summary_llm", fake)
    return calls


# ── Pure helpers ───────────────────────────────────────────────────────


def test_document_carries_title_and_topics():
    """A reader searches a chapter by its vocabulary, not by its paraphrase."""
    doc = chapter_summary_document("Vies de ancoragem", "Prosa do resumo.", ["ancoragem"])
    assert "Vies de ancoragem" in doc
    assert "Prosa do resumo." in doc
    assert "ancoragem" in doc


def test_parse_topics_tolerates_garbage():
    assert parse_topics(None) == []
    assert parse_topics("nao e json") == []
    assert parse_topics('["a", "b"]') == ["a", "b"]


def test_source_checksum_changes_when_a_chapter_summary_changes():
    a = [{"chapter_id": "x::ch000", "summary_checksum": "one"}]
    b = [{"chapter_id": "x::ch000", "summary_checksum": "two"}]
    assert source_summary_checksum(a) != source_summary_checksum(b)


def test_source_checksum_is_order_independent():
    """Chapters arrive in whatever order SQLite returns; the hash must not care."""
    rows = [
        {"chapter_id": "x::ch001", "summary_checksum": "b"},
        {"chapter_id": "x::ch000", "summary_checksum": "a"},
    ]
    assert source_summary_checksum(rows) == source_summary_checksum(list(reversed(rows)))


def test_chapter_text_is_reassembled_in_chunk_order(db):
    _seed(db, chapters=1, chunks_per_chapter=3)
    text = chapter_text_for_summary(db, f"{SOURCE_ID}::ch000")
    assert text.index("parte 0") < text.index("parte 1") < text.index("parte 2")


# ── The checksum gate ──────────────────────────────────────────────────


def test_second_pass_costs_no_llm_call(cfg, db, monkeypatch):
    """The whole cost story: an unchanged chapter is never re-summarized."""
    _seed(db)
    calls: list[str] = []
    _fake_llm(monkeypatch, calls)
    idx = FakeIndex()

    first = generate_summaries(cfg, db, idx, SOURCE_ID)
    assert first.chapters_summarized == 2
    assert len(calls) == 3  # two chapters + one source reduce

    calls.clear()
    second = generate_summaries(cfg, db, idx, SOURCE_ID)
    assert second.chapters_summarized == 0
    assert second.chapters_skipped == 2
    assert calls == []


def test_changed_chapter_is_resummarized_alone(cfg, db, monkeypatch):
    _seed(db)
    calls: list[str] = []
    _fake_llm(monkeypatch, calls)
    idx = FakeIndex()
    generate_summaries(cfg, db, idx, SOURCE_ID)

    # A re-chunk changes the chapter's checksum; the summary is now stale.
    db.upsert_chapter(f"{SOURCE_ID}::ch001", SOURCE_ID, "Capitulo 1", "checksum-novo")
    assert db.count_stale_chapter_summaries() == 1

    calls.clear()
    outcome = generate_summaries(cfg, db, idx, SOURCE_ID)
    assert outcome.chapters_summarized == 1
    assert outcome.chapters_skipped == 1
    assert calls.count("chapter_summary.md") == 1


def test_chapter_with_no_persisted_text_is_skipped(cfg, db, monkeypatch):
    db.upsert_source(SOURCE_ID, CITEKEY, TITLE, ["Autor"], 2024, "h", "/tmp/o.pdf", "pdf")
    db.upsert_chapter(f"{SOURCE_ID}::ch000", SOURCE_ID, "Vazio", "chk")
    calls: list[str] = []
    _fake_llm(monkeypatch, calls)

    outcome = generate_summaries(cfg, db, FakeIndex(), SOURCE_ID)
    assert outcome.chapters_summarized == 0
    assert calls == []


def test_summary_is_embedded_once_per_chapter(cfg, db, monkeypatch):
    _seed(db)
    _fake_llm(monkeypatch, [])
    idx = FakeIndex()
    generate_summaries(cfg, db, idx, SOURCE_ID)
    assert [u[0] for u in idx.upserts] == [f"{SOURCE_ID}::ch000", f"{SOURCE_ID}::ch001"]
    assert idx.upserts[0][2]["citekey"] == CITEKEY


def test_source_summary_is_not_embedded(cfg, db, monkeypatch):
    """Only chapters are the search unit — a second dead collection is the
    defect that removed `literature_notes`."""
    _seed(db)
    _fake_llm(monkeypatch, [])
    idx = FakeIndex()
    generate_summaries(cfg, db, idx, SOURCE_ID)

    assert db.get_source(SOURCE_ID)["summary"]
    assert SOURCE_ID not in [u[0] for u in idx.upserts]


def test_missing_source_is_reported_not_raised(cfg, db):
    outcome = generate_summaries(cfg, db, FakeIndex(), "@NaoExiste")
    assert outcome.skipped and "nao encontrada" in outcome.skipped[0].lower()


# ── Map-reduce ─────────────────────────────────────────────────────────


def test_oversized_chapter_still_yields_one_summary(cfg, db, monkeypatch):
    cfg.summarize.max_input_chars = 200
    db.upsert_source(SOURCE_ID, CITEKEY, TITLE, ["Autor"], 2024, "h", "/tmp/o.pdf", "pdf")
    db.upsert_chapter(f"{SOURCE_ID}::ch000", SOURCE_ID, "Longo", "chk")
    for i in range(6):
        db.upsert_chunk(
            f"{SOURCE_ID}::ch000::c{i}",
            SOURCE_ID,
            f"{SOURCE_ID}::ch000",
            "paragrafo " * 30,
            f"cs{i}",
            chunk_index=i,
        )
    calls: list[str] = []
    _fake_llm(monkeypatch, calls)

    outcome = generate_summaries(cfg, db, FakeIndex(), SOURCE_ID)
    assert outcome.chapters_summarized == 1
    # Several map calls plus a reduce, but exactly one stored summary.
    assert calls.count("chapter_summary.md") > 2
    assert db.get_chapter(f"{SOURCE_ID}::ch000")["summary"]


# ── The note count and the chapter map ─────────────────────────────────


def test_note_count_is_a_sql_aggregate(db):
    _seed(db)
    chunk = f"{SOURCE_ID}::ch000::c0"
    db.upsert_note("note-1", SOURCE_ID, "/v/ZTL - note-1 - a.md", "Nota A")
    db.upsert_note("note-2", SOURCE_ID, "/v/ZTL - note-2 - b.md", "Nota B")
    db.upsert_concept("cp1", SOURCE_ID, chunk, note_id="note-1")
    db.upsert_concept("cp2", SOURCE_ID, chunk, note_id="note-2")
    # A concept with no note yet must not be counted.
    db.upsert_concept("cp3", SOURCE_ID, f"{SOURCE_ID}::ch001::c0")

    counts = db.get_chapter_note_counts(SOURCE_ID)
    assert counts[f"{SOURCE_ID}::ch000"] == 2
    assert f"{SOURCE_ID}::ch001" not in counts


def test_two_concepts_merged_into_one_note_count_once(db):
    """A merged note must not inflate the count of its chapter."""
    _seed(db)
    chunk = f"{SOURCE_ID}::ch000::c0"
    db.upsert_note("note-1", SOURCE_ID, "/v/ZTL - note-1 - a.md", "Nota A")
    db.upsert_concept("cp1", SOURCE_ID, chunk, note_id="note-1")
    db.upsert_concept("cp2", SOURCE_ID, f"{SOURCE_ID}::ch000::c1", note_id="note-1")
    assert db.get_chapter_note_counts(SOURCE_ID)[f"{SOURCE_ID}::ch000"] == 1


def test_chapters_for_note_is_the_reverse_lookup(db):
    _seed(db)
    db.upsert_note("note-1", SOURCE_ID, "/v/ZTL - note-1 - a.md", "Nota A")
    db.upsert_concept("cp1", SOURCE_ID, f"{SOURCE_ID}::ch000::c0", note_id="note-1")
    db.upsert_concept("cp2", SOURCE_ID, f"{SOURCE_ID}::ch001::c0", note_id="note-1")
    assert sorted(db.get_chapters_for_note("note-1")) == [
        f"{SOURCE_ID}::ch000",
        f"{SOURCE_ID}::ch001",
    ]


def test_chapter_map_shows_pages_counts_and_links(cfg, db):
    _seed(db)
    db.upsert_note("note-1", SOURCE_ID, "/v/ZTL - note-1 - tese.md", "Tese")
    db.upsert_concept("cp1", SOURCE_ID, f"{SOURCE_ID}::ch000::c0", note_id="note-1")
    db.update_chapter_summary(
        f"{SOURCE_ID}::ch000", "Resumo do primeiro.", ["alfa"], "chk0", "m", "2026-01-01T00:00:00Z"
    )

    block = render_chapter_map(cfg, db, SOURCE_ID)
    assert "### Capitulo 0" in block
    assert "p. 100-101" in block
    assert "1 nota permanente" in block
    assert "Resumo do primeiro." in block
    assert "[[ZTL - note-1 - tese]]" in block
    # A chapter with no summary says so instead of rendering blank.
    assert "Sem resumo" in block


def test_chapter_map_marks_a_stale_summary(cfg, db):
    _seed(db)
    db.update_chapter_summary(
        f"{SOURCE_ID}::ch000", "Resumo antigo.", [], "checksum-velho", "m", "2026-01-01T00:00:00Z"
    )
    assert "defasado" in render_chapter_map(cfg, db, SOURCE_ID)


def test_pageless_source_omits_the_page_label(cfg, db):
    """Native Markdown has no pages (ADR-013) — omit, never render `p. None`."""
    db.upsert_source(SOURCE_ID, CITEKEY, TITLE, ["Autor"], 2024, "h", "/tmp/o.md", "md")
    db.upsert_chapter(f"{SOURCE_ID}::ch000", SOURCE_ID, "Secao", "chk")
    db.upsert_chunk(f"{SOURCE_ID}::ch000::c0", SOURCE_ID, f"{SOURCE_ID}::ch000", "t", "cs")
    block = render_chapter_map(cfg, db, SOURCE_ID)
    assert "p." not in block


# ── Vault blocks ───────────────────────────────────────────────────────


def _write_lit_index(cfg: AppConfig, db: StateDB) -> Path:
    from zettel.vault import literature_index_filename

    lit_dir = cfg.vault_path / "20_Literature"
    lit_dir.mkdir(parents=True, exist_ok=True)
    path = lit_dir / literature_index_filename(CITEKEY, TITLE)
    meta, body = build_literature_index_note(SOURCE_ID, CITEKEY, TITLE)
    safe_write_note(path, meta, body)
    return path


def test_refresh_writes_both_blocks(cfg, db):
    _seed(db)
    path = _write_lit_index(cfg, db)
    db.update_source_summary(
        SOURCE_ID, "Resumo geral.", ["alfa"], "sc", "m", "2026-01-01T00:00:00Z"
    )

    assert refresh_chapter_map(cfg, db, SOURCE_ID) is True
    content = path.read_text(encoding="utf-8")
    assert "## Resumo geral" in content
    assert "## Mapa de capitulos" in content
    assert "Resumo geral." in read_managed_block(content, SOURCE_SUMMARY_BLOCK)
    assert "Capitulo 0" in read_managed_block(content, CHAPTER_MAP_BLOCK)


def test_refresh_preserves_manual_text_outside_the_blocks(cfg, db):
    _seed(db)
    path = _write_lit_index(cfg, db)
    refresh_chapter_map(cfg, db, SOURCE_ID)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## Minhas anotacoes\n\nNao me apague.\n",
        encoding="utf-8",
    )

    db.update_chapter_summary(
        f"{SOURCE_ID}::ch000", "Novo resumo.", [], "chk0", "m", "2026-01-01T00:00:00Z"
    )
    refresh_chapter_map(cfg, db, SOURCE_ID)

    content = path.read_text(encoding="utf-8")
    assert "Nao me apague." in content
    assert "Novo resumo." in content


def test_refresh_is_idempotent(cfg, db):
    """Running twice must not stack duplicate headings or blocks."""
    _seed(db)
    path = _write_lit_index(cfg, db)
    refresh_chapter_map(cfg, db, SOURCE_ID)
    refresh_chapter_map(cfg, db, SOURCE_ID)
    content = path.read_text(encoding="utf-8")
    assert content.count("## Mapa de capitulos") == 1
    assert content.count(f"zettel:{CHAPTER_MAP_BLOCK}:start") == 1


def test_refresh_without_a_lit_index_is_a_no_op(cfg, db):
    _seed(db)
    assert refresh_chapter_map(cfg, db, SOURCE_ID) is False
