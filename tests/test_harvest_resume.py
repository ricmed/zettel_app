"""A crashed harvest must not leave a chapter marked complete without its chunks."""

from __future__ import annotations

import pytest
from zettel.config import AppConfig, HarvestConfig
from zettel.harvester.pipeline import run_harvest, run_rechunk, source_chunking_incomplete
from zettel.state import StateDB

from tests.fakes import FakeVectorIndex


class _Idx(FakeVectorIndex):
    """Canonical fake whose chunk deletes can be switched to fail mid-test."""

    fail_delete = False

    def delete_chunks(self, chunk_ids):
        if self.fail_delete:
            raise RuntimeError("queda no chroma")
        super().delete_chunks(chunk_ids)


def _cfg(tmp_path, *, embed: bool = False) -> AppConfig:
    vault = tmp_path / "vault"
    (vault / "10_Sources").mkdir(parents=True)
    (vault / "20_Literature").mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return AppConfig(
        vault_path=vault,
        inbox_path=inbox,
        harvest=HarvestConfig(
            biblio_llm_enabled=False,
            semantic_duplicate_enabled=embed,
        ),
    )


def _write_book(inbox) -> None:
    filler = "Conteudo longo deste capitulo para formar um chunk. "
    body = filler * 8
    parts = [f"# {name}\n\n{body}\n\n" for name in ("Cap A", "Cap B", "Cap C", "Cap D")]
    (inbox / "livro.md").write_text("".join(parts), encoding="utf-8")


def _assert_no_chapter_without_chunks(db: StateDB, source_id: str) -> None:
    chapters = db.get_chapters_for_source(source_id)
    assert chapters
    for chapter in chapters:
        assert db.get_chunks_for_chapter(chapter["chapter_id"]), chapter["chapter_id"]


def test_harvest_resumes_after_upsert_chunk_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_book(cfg.inbox_path)
    db = StateDB(tmp_path / "state.db")
    idx = _Idx()
    real = db.upsert_chunk
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("queda no sqlite")
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "upsert_chunk", flaky)
    with pytest.raises(RuntimeError, match="queda no sqlite"):
        run_harvest(cfg, db, idx, interactive=False, skip_biblio=True)

    src = db.list_sources()[0]
    sid = src["source_id"]
    assert src["processing_status"] == "in_progress"
    assert source_chunking_incomplete(db, sid)
    _assert_no_chapter_without_chunks(db, sid)
    after_crash = len(db.get_chunks_for_source(sid))

    monkeypatch.setattr(db, "upsert_chunk", real)
    outcome = run_harvest(cfg, db, idx, interactive=False, skip_biblio=True)
    assert outcome.source_ids == [sid]
    src = db.get_source(sid)
    assert src["processing_status"] == "completed"
    assert not source_chunking_incomplete(db, sid)
    assert len(db.get_chunks_for_source(sid)) > after_crash
    _assert_no_chapter_without_chunks(db, sid)
    assert len(db.get_chapters_for_source(sid)) == 4
    db.close()


def test_harvest_resumes_embeddings_after_chroma_failure(tmp_path):
    cfg = _cfg(tmp_path, embed=True)
    _write_book(cfg.inbox_path)
    db = StateDB(tmp_path / "state.db")
    idx = _Idx(fail_on={"upsert_chunk": (2, RuntimeError("queda no embedding"))})
    with pytest.raises(RuntimeError, match="queda no embedding"):
        run_harvest(cfg, db, idx, interactive=False, skip_biblio=True)

    src = db.list_sources()[0]
    sid = src["source_id"]
    assert src["processing_status"] == "in_progress"
    assert source_chunking_incomplete(db, sid)
    stored = {row["chunk_id"] for row in db.get_chunks_for_source(sid)}
    assert stored
    assert set(idx.store["chunks"]) < stored
    _assert_no_chapter_without_chunks(db, sid)

    idx.fail_on = {}
    outcome = run_harvest(cfg, db, idx, interactive=False, skip_biblio=True)
    assert outcome.source_ids == [sid]
    assert db.get_source(sid)["processing_status"] == "completed"
    assert not source_chunking_incomplete(db, sid)
    assert set(idx.store["chunks"]) == stored
    db.close()


def test_rechunk_resumes_after_upsert_chunk_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_book(cfg.inbox_path)
    db = StateDB(tmp_path / "state.db")
    idx = _Idx()
    outcome = run_harvest(cfg, db, idx, interactive=False, skip_biblio=True)
    sid = outcome.source_ids[0]
    assert db.get_source(sid)["processing_status"] == "completed"

    src = db.get_source(sid)
    db.update_source_texts(
        sid,
        extracted_text=src["extracted_text"].replace("longo", "revisado"),
    )
    real = db.upsert_chunk
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("queda no rechunk")
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "upsert_chunk", flaky)
    with pytest.raises(RuntimeError, match="queda no rechunk"):
        run_rechunk(cfg, db, idx, sid)

    assert db.get_source(sid)["processing_status"] == "in_progress"
    _assert_no_chapter_without_chunks(db, sid)

    monkeypatch.setattr(db, "upsert_chunk", real)
    run_rechunk(cfg, db, idx, sid)
    assert db.get_source(sid)["processing_status"] == "completed"
    assert not source_chunking_incomplete(db, sid)
    texts = " ".join(row["text"] for row in db.get_chunks_for_source(sid))
    assert "revisado" in texts
    _assert_no_chapter_without_chunks(db, sid)
    db.close()


def test_second_pass_drops_embedding_left_after_a_failed_delete(tmp_path):
    """A chapter that already matches still deletes Chroma ids SQLite dropped."""
    from zettel.harvester.chunking import chunk_and_persist

    cfg = _cfg(tmp_path, embed=True)
    db = StateDB(tmp_path / "state.db")
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    idx = _Idx()
    body = "Conteudo longo deste capitulo para formar um chunk. " * 8
    first = [{"title": "Cap", "text": body, "locator": "Cap"}]
    chunk_and_persist(cfg, db, idx, "@S", first, origin_type="md")
    old = set(idx.store["chunks"])
    assert old

    idx.fail_delete = True
    revised = body.replace("longo", "revisado")
    second = [{"title": "Cap", "text": revised, "locator": "Cap"}]
    with pytest.raises(RuntimeError, match="queda no chroma"):
        chunk_and_persist(cfg, db, idx, "@S", second, origin_type="md")
    assert old <= set(idx.store["chunks"])

    idx.fail_delete = False
    chunk_and_persist(cfg, db, idx, "@S", second, origin_type="md")
    live = {row["chunk_id"] for row in db.get_chunks_for_source("@S")}
    assert live
    assert old.isdisjoint(live)
    assert set(idx.store["chunks"]) == live
    db.close()
