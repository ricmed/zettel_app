"""Tests for the three-layer duplicate detection in the harvest phase."""

from __future__ import annotations

import pytest

from zettel.config import AppConfig, HarvestConfig
from zettel.harvester import (
    HarvestAborted,
    find_semantic_duplicate_candidates as _find_semantic_duplicate_candidates,
    resolve_duplicate_decision as _resolve_duplicate_decision,
    sample_chunk_texts as _sample_chunk_texts,
)
from zettel.harvester.pipeline import _process_file
from zettel.hashing import normalize_text_for_hash, sha256_hex
from zettel.state import StateDB


class FakeVectorIndex:
    """Minimal stand-in for VectorIndex, avoiding real embeddings/network calls."""

    def __init__(self, chunk_matches: list[dict] | None = None):
        self._chunk_matches = chunk_matches or []
        self.upserted_sources: list[str] = []
        self.upserted_chunks: list[str] = []

    def find_similar_chunks(self, texts, n_results=3):
        return self._chunk_matches if texts else []

    def upsert_source(self, source_id, summary, metadata):
        self.upserted_sources.append(source_id)

    def upsert_chunk(self, chunk_id, text, metadata, **kwargs):
        self.upserted_chunks.append(chunk_id)

    def delete_chunks(self, chunk_ids):
        for cid in chunk_ids:
            if cid in self.upserted_chunks:
                self.upserted_chunks.remove(cid)

    def existing_ids(self, collection_name, ids):
        return {cid for cid in ids if cid in self.upserted_chunks}


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig(
        vault_path=tmp_path / "vault",
        harvest=HarvestConfig(
            duplicate_chunk_threshold=0.85,
            biblio_llm_enabled=False,
        ),
    )
    (tmp_path / "vault" / "10_Sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "vault" / "20_Literature").mkdir(parents=True, exist_ok=True)
    return c


# ── _sample_chunk_texts ──────────────────────────────────────────────────


def test_sample_chunk_texts_returns_all_when_fewer_than_sample_size(cfg):
    chapters = [{"title": "Intro", "text": "Um texto curto para dividir em poucos chunks."}]
    sample = _sample_chunk_texts(cfg, chapters, sample_size=10)
    assert len(sample) >= 1


def test_sample_chunk_texts_distributes_evenly(cfg):
    long_text = " ".join(f"palavra{i}" for i in range(2000))
    chapters = [{"title": "Cap", "text": long_text}]
    sample = _sample_chunk_texts(cfg, chapters, sample_size=3)
    assert len(sample) == 3


# ── _find_semantic_duplicate_candidates ─────────────────────────────────


def test_find_semantic_duplicate_candidates_aggregates_by_source(db, cfg):
    db.upsert_source(
        source_id="@Existing2024", citekey="Existing2024", title="Fonte Existente",
        authors=[], year=2024, file_checksum="h", origin_path="/p", origin_type="pdf",
    )
    idx = FakeVectorIndex(chunk_matches=[
        {"id": "c1", "distance": 0.10, "metadata": {"source_id": "@Existing2024"}},
        {"id": "c2", "distance": 0.05, "metadata": {"source_id": "@Existing2024"}},
        {"id": "c3", "distance": 1.90, "metadata": {"source_id": "@Other2024"}},
    ])
    chapters = [{"title": "Cap", "text": "Algum conteudo qualquer para amostragem de chunks."}]
    candidates = _find_semantic_duplicate_candidates(cfg, db, idx, chapters)

    assert len(candidates) == 1
    assert candidates[0]["source_id"] == "@Existing2024"
    assert candidates[0]["citekey"] == "Existing2024"
    assert candidates[0]["similarity"] > 0.85


def test_find_semantic_duplicate_candidates_below_threshold_ignored(db, cfg):
    idx = FakeVectorIndex(chunk_matches=[
        {"id": "c1", "distance": 1.5, "metadata": {"source_id": "@Distant2024"}},
    ])
    chapters = [{"title": "Cap", "text": "Texto sem relacao com nada existente."}]
    candidates = _find_semantic_duplicate_candidates(cfg, db, idx, chapters)
    assert candidates == []


# ── _resolve_duplicate_decision (non-interactive) ───────────────────────


def test_resolve_duplicate_decision_noninteractive_uses_override(cfg, tmp_path):
    action = _resolve_duplicate_decision(
        tmp_path / "file.pdf",
        [{"citekey": "X", "title": "T", "similarity": 0.9}],
        interactive=False,
        duplicate_action="continue",
        cfg=cfg,
    )
    assert action == "continue"


def test_resolve_duplicate_decision_noninteractive_uses_config_default(cfg, tmp_path):
    cfg.harvest.non_interactive_duplicate_action = "skip"
    action = _resolve_duplicate_decision(
        tmp_path / "file.pdf",
        [{"citekey": "X", "title": "T", "similarity": 0.9}],
        interactive=False,
        duplicate_action=None,
        cfg=cfg,
    )
    assert action == "skip"


# ── _process_file: layered duplicate detection ──────────────────────────


def _write_pdf_like_md(path, text):
    path.write_text(text, encoding="utf-8")


def test_process_file_detects_renamed_copy_by_file_hash(db, cfg, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    original = inbox / "original.md"
    copy = inbox / "copy_of_original.md"
    content = "# Titulo\n\nConteudo do artigo original."
    _write_pdf_like_md(original, content)
    _write_pdf_like_md(copy, content)

    idx = FakeVectorIndex()

    sid1, stats1 = _process_file(
        cfg, db, idx, original, run_id=db.start_run("sig"),
        interactive=False, skip_biblio=True,
    )
    assert sid1 is not None

    run_id2 = db.start_run("sig2")
    sid2, stats2 = _process_file(
        cfg, db, idx, copy, run_id=run_id2, interactive=False, skip_biblio=True,
    )
    assert sid2 is None
    assert stats2 == {}

    copy_record = db.get_file(str(copy))
    assert copy_record["source_id"] == sid1

    last_run = db.get_run(run_id2)
    assert last_run["duplicate_file_count"] == 1


def test_process_file_detects_cross_format_content_duplicate(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    md_file = inbox / "article.md"
    pdf_like_file = inbox / "article_copy.txt"  # different bytes, same normalized text

    body = "# Artigo Importante\n\nEste e o conteudo do artigo, identico em ambos formatos."
    md_file.write_text(body, encoding="utf-8")
    # Different raw bytes (extra trailing whitespace/newlines) but same normalized text.
    pdf_like_file.write_text(body + "   \n\n\n", encoding="utf-8")

    idx = FakeVectorIndex()

    run_id1 = db.start_run("sig1")
    sid1, _ = _process_file(
        cfg, db, idx, md_file, run_id=run_id1, interactive=False, skip_biblio=True,
    )
    assert sid1 is not None

    run_id2 = db.start_run("sig2")
    sid2, stats2 = _process_file(
        cfg, db, idx, pdf_like_file, run_id=run_id2, interactive=False, skip_biblio=True,
    )
    assert sid2 is None
    assert stats2 == {}

    second_file_record = db.get_file(str(pdf_like_file))
    assert second_file_record["source_id"] == sid1

    run2 = db.get_run(run_id2)
    assert run2["duplicate_content_count"] == 1


def test_process_file_semantic_duplicate_skip(db, cfg, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    new_file = inbox / "new_article.md"
    new_file.write_text("# Outro Titulo\n\nTexto totalmente diferente do indexado.", encoding="utf-8")

    db.upsert_source(
        source_id="@Similar2024", citekey="Similar2024", title="Fonte Similar",
        authors=[], year=2024, file_checksum="h", origin_path="/p", origin_type="pdf",
    )
    idx = FakeVectorIndex(chunk_matches=[
        {"id": "c1", "distance": 0.05, "metadata": {"source_id": "@Similar2024"}},
    ])

    run_id = db.start_run("sig")
    sid, stats = _process_file(
        cfg, db, idx, new_file, run_id=run_id, interactive=False, duplicate_action="skip",
        skip_biblio=True,
    )
    assert sid is None
    assert stats == {}
    run = db.get_run(run_id)
    assert run["duplicate_semantic_count"] == 1
    # No source should have been created for the skipped file.
    assert db.get_file(str(new_file)) is None


def test_process_file_semantic_duplicate_continue_creates_source(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    new_file = inbox / "new_article2.md"
    new_file.write_text("# Outro Titulo 2\n\nTexto diferente mas sinalizado como suspeito.", encoding="utf-8")

    db.upsert_source(
        source_id="@Similar2024b", citekey="Similar2024b", title="Fonte Similar 2",
        authors=[], year=2024, file_checksum="h", origin_path="/p", origin_type="pdf",
    )
    idx = FakeVectorIndex(chunk_matches=[
        {"id": "c1", "distance": 0.05, "metadata": {"source_id": "@Similar2024b"}},
    ])

    run_id = db.start_run("sig")
    sid, stats = _process_file(
        cfg, db, idx, new_file, run_id=run_id, interactive=False, duplicate_action="continue",
        skip_biblio=True,
    )
    assert sid is not None
    assert stats.get("chunks", 0) >= 1
    run = db.get_run(run_id)
    assert run["duplicate_semantic_count"] == 1


def test_process_file_semantic_duplicate_abort_raises(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    new_file = inbox / "new_article3.md"
    new_file.write_text("# Titulo 3\n\nConteudo suspeito de duplicidade semantica.", encoding="utf-8")

    db.upsert_source(
        source_id="@Similar2024c", citekey="Similar2024c", title="Fonte Similar 3",
        authors=[], year=2024, file_checksum="h", origin_path="/p", origin_type="pdf",
    )
    idx = FakeVectorIndex(chunk_matches=[
        {"id": "c1", "distance": 0.05, "metadata": {"source_id": "@Similar2024c"}},
    ])

    run_id = db.start_run("sig")
    with pytest.raises(HarvestAborted):
        _process_file(
            cfg, db, idx, new_file, run_id=run_id, interactive=False, duplicate_action="abort",
            skip_biblio=True,
        )
