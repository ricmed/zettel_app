"""Tests for SQLite state management."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from zettel.state import StateDB, _fts_match_expr


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "test.db")
    yield db
    db.close()


# Schema of a pre-Fase-0 database (before retention columns / assets table existed).
_OLD_SCHEMA_SQL = """
CREATE TABLE sources (source_id TEXT PRIMARY KEY, citekey TEXT NOT NULL UNIQUE, title TEXT,
    authors TEXT, year INTEGER, file_checksum TEXT NOT NULL, extraction_checksum TEXT,
    origin_path TEXT NOT NULL, origin_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
    text TEXT NOT NULL, chunk_checksum TEXT NOT NULL, locator TEXT DEFAULT '', status TEXT DEFAULT 'pending',
    llm_prompt1_hash TEXT, llm_call_checksum_prompt1 TEXT);
CREATE TABLE concepts (concept_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    anchor_hash TEXT DEFAULT '', thesis_hash TEXT DEFAULT '', note_id TEXT);
CREATE TABLE notes (note_id TEXT PRIMARY KEY, source_id TEXT, path TEXT, title TEXT,
    note_semantic_checksum TEXT, auto_checksum TEXT, embedding_input_hash TEXT, embedding_model TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE mocs (moc_id TEXT PRIMARY KEY, topic TEXT, path TEXT, cluster_signature TEXT,
    embedding_input_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, pipeline_signature TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, status TEXT DEFAULT 'running');
"""


def test_upsert_and_get_file(db):
    db.upsert_file("/test/file.pdf", "abc123", "pdf", "@TestSource")
    record = db.get_file("/test/file.pdf")
    assert record is not None
    assert record["file_checksum"] == "abc123"
    assert record["source_id"] == "@TestSource"


def test_upsert_and_get_source(db):
    db.upsert_source(
        source_id="@Test2024",
        citekey="Test2024",
        title="Test Document",
        authors=["Author One"],
        year=2024,
        file_checksum="hash123",
        origin_path="/path/to/file.pdf",
        origin_type="pdf",
    )
    source = db.get_source("@Test2024")
    assert source is not None
    assert source["title"] == "Test Document"
    assert source["citekey"] == "Test2024"


def test_upsert_chunk_and_get_pending(db):
    db.upsert_source("@S", "S", "Source", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch1", "ch_hash")
    db.upsert_chunk("@S::ch000::abc", "@S", "@S::ch000", "text here", "ck_hash")

    pending = db.get_pending_chunks()
    assert len(pending) == 1
    assert pending[0]["chunk_id"] == "@S::ch000::abc"

    db.update_chunk_status("@S::ch000::abc", "awaiting_review")
    pending = db.get_pending_chunks()
    assert len(pending) == 0


def test_upsert_note(db):
    db.upsert_note("note1", "@S", "/path/note.md", "Test Note", "sem_hash")
    note = db.get_note("note1")
    assert note is not None
    assert note["title"] == "Test Note"


def test_llm_cache(db):
    db.cache_llm_response("call1", '{"prompt": "test"}', '{"result": "ok"}')
    cached = db.get_cached_llm_response("call1")
    assert cached == '{"result": "ok"}'

    assert db.get_cached_llm_response("nonexistent") is None


def test_stats(db):
    stats = db.get_stats()
    assert "files" in stats
    assert "chunks_pending" in stats
    assert all(v == 0 for v in stats.values())


def test_get_file_by_checksum_detects_renamed_copy(db):
    db.upsert_file("/inbox/original.pdf", "sharedhash", "pdf", "@Source2024")
    found = db.get_file_by_checksum("sharedhash", exclude_path="/inbox/copy.pdf")
    assert found is not None
    assert found["path"] == "/inbox/original.pdf"
    assert found["source_id"] == "@Source2024"

    assert db.get_file_by_checksum("nope") is None
    assert db.get_file_by_checksum("sharedhash", exclude_path="/inbox/original.pdf") is None


def test_get_source_by_extraction_checksum_cross_format(db):
    db.upsert_source(
        source_id="@Pdf2024",
        citekey="Pdf2024",
        title="Same Article",
        authors=["Author"],
        year=2024,
        file_checksum="pdfhash",
        origin_path="/inbox/article.pdf",
        origin_type="pdf",
        extraction_checksum="sharedtexthash",
    )
    found = db.get_source_by_extraction_checksum("sharedtexthash")
    assert found is not None
    assert found["source_id"] == "@Pdf2024"

    assert db.get_source_by_extraction_checksum("sharedtexthash", exclude_source_id="@Pdf2024") is None
    assert db.get_source_by_extraction_checksum("") is None


def test_run_duplicate_tracking(db):
    run_id = db.start_run("sig123")
    db.record_duplicate(run_id, "file")
    db.record_duplicate(run_id, "content")
    db.record_duplicate(run_id, "semantic")
    db.record_duplicate(run_id, "semantic")
    db.finish_run(run_id, "completed")

    last = db.get_last_run()
    assert last["run_id"] == run_id
    assert last["duplicate_file_count"] == 1
    assert last["duplicate_content_count"] == 1
    assert last["duplicate_semantic_count"] == 2
    assert last["status"] == "completed"

    with pytest.raises(ValueError):
        db.record_duplicate(run_id, "unknown-kind")


def test_get_recent_runs_newest_first(db):
    first = db.start_run("extract")
    db.finish_run(first, "completed", {"cost_usd_total": 0.1, "llm_calls": 2})
    second = db.start_run("garden")
    db.finish_run(second, "completed", {"cost_usd_total": 0.2, "llm_calls": 1})

    rows = db.get_recent_runs(10)
    assert [r["run_id"] for r in rows] == [second, first]
    assert rows[0]["pipeline_signature"] == "garden"
    assert float(rows[0]["cost_usd_total"]) == 0.2


def test_finish_run_persists_prompt_cache_tokens(db):
    """#64: provider-side prompt cache tokens survive past the run (they didn't before)."""
    run_id = db.start_run("extract")
    db.finish_run(run_id, "completed", {
        "cost_usd_total": 0.05,
        "llm_calls": 3,
        "prompt_cache_read_tokens": 12000,
        "prompt_cache_write_tokens": 500,
    })
    row = db.get_last_run()
    assert row["run_id"] == run_id
    assert int(row["prompt_cache_read_tokens"]) == 12000
    assert int(row["prompt_cache_write_tokens"]) == 500


def test_finish_run_prompt_cache_tokens_default_to_zero(db):
    run_id = db.start_run("extract")
    db.finish_run(run_id, "completed", {"cost_usd_total": 0.01})
    row = db.get_last_run()
    assert int(row["prompt_cache_read_tokens"]) == 0
    assert int(row["prompt_cache_write_tokens"]) == 0


# ── Fase 0 — retenção máxima no SQLite ─────────────────────────────────


def test_migration_adds_new_columns_to_old_db(tmp_path):
    """Opening a pre-Fase-0 DB must add all new columns without losing data."""
    old_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(old_path))
    conn.executescript(_OLD_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO sources VALUES ('@S','S','t','[]',2020,'fc','ec','/p','md','now','now')"
    )
    conn.commit()
    conn.close()

    db = StateDB(old_path)
    try:
        def cols(table):
            return {r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()}

        assert {"extracted_text", "lit_body", "origin"} <= cols("sources")
        assert "section_path" in cols("chunks")
        assert {"candidate_json", "status"} <= cols("concepts")
        assert {"body", "frontmatter_json", "origin"} <= cols("notes")
        assert {"body", "frontmatter_json", "origin"} <= cols("mocs")
        assert cols("assets")  # assets table created

        # Existing row preserved; new column defaulted.
        src = db.get_source("@S")
        assert src["title"] == "t"
        assert src["origin"] == "pipeline"
    finally:
        db.close()


def test_update_source_texts_selective(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.update_source_texts("@S", extracted_text="full text")
    assert db.get_source("@S")["extracted_text"] == "full text"
    # Updating lit_body must not clobber extracted_text.
    db.update_source_texts("@S", lit_body="lit content")
    src = db.get_source("@S")
    assert src["extracted_text"] == "full text"
    assert src["lit_body"] == "lit content"


def test_concept_candidate_and_status(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "txt", "ck")
    db.upsert_concept(
        "@S::concept::a", "@S", "@S::ch000::a", "ah", "th",
        candidate_json='{"thesis": "x"}', status="extracted",
    )
    concept = db.get_concept("@S::concept::a")
    assert concept["candidate_json"] == '{"thesis": "x"}'
    assert concept["status"] == "extracted"

    db.update_concept_status("@S::concept::a", "approved")
    approved = db.get_concepts_by_status("approved", without_notes=True)
    assert len(approved) == 1
    assert approved[0]["concept_id"] == "@S::concept::a"

    # Once noted, it drops out of the without_notes filter.
    db.upsert_concept("@S::concept::a", "@S", "@S::ch000::a", note_id="note1", status="noted")
    assert db.get_concepts_by_status("approved", without_notes=True) == []


def test_upsert_concept_preserves_hashes_when_not_passed(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "txt", "ck")
    db.upsert_concept(
        "@S::concept::a", "@S", "@S::ch000::a", "anchorhash123", "thesishash456",
        candidate_json='{"thesis": "x"}', status="extracted",
    )

    # connect-style call: no hashes passed, only note_id/status touched.
    db.upsert_concept("@S::concept::a", "@S", "@S::ch000::a", note_id="note1", status="noted")

    concept = db.get_concept("@S::concept::a")
    assert concept["anchor_hash"] == "anchorhash123"
    assert concept["thesis_hash"] == "thesishash456"
    assert concept["note_id"] == "note1"
    assert concept["status"] == "noted"

    # A later call that does pass new hashes still overwrites them.
    db.upsert_concept(
        "@S::concept::a", "@S", "@S::ch000::a", "newanchor", "newthesis",
    )
    concept = db.get_concept("@S::concept::a")
    assert concept["anchor_hash"] == "newanchor"
    assert concept["thesis_hash"] == "newthesis"


def test_upsert_concept_initial_insert_defaults_hashes_to_empty(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "txt", "ck")
    db.upsert_concept("@S::concept::a", "@S", "@S::ch000::a")

    concept = db.get_concept("@S::concept::a")
    assert concept["anchor_hash"] == ""
    assert concept["thesis_hash"] == ""


def test_note_body_and_embedding_hash(db):
    db.upsert_note(
        "n1", "@S", "/p/n1.md", "Title",
        body="corpo completo", frontmatter_json='{"type": "permanent"}', origin="pipeline",
    )
    note = db.get_note("n1")
    assert note["body"] == "corpo completo"
    assert note["frontmatter_json"] == '{"type": "permanent"}'
    assert note["origin"] == "pipeline"

    db.update_note_embedding("n1", "emb_hash_123", "text-embedding-3-small")
    note = db.get_note("n1")
    assert note["embedding_input_hash"] == "emb_hash_123"


def test_moc_body_and_get_moc(db):
    db.upsert_moc("m1", "Topic", "/p/m1.md", "sig1", body="moc body", frontmatter_json='{}')
    moc = db.get_moc("m1")
    assert moc is not None
    assert moc["body"] == "moc body"
    assert moc["origin"] == "pipeline"


def test_assets_crud(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_asset(
        "@S::img::x", "@S", "90_Assets/S/a.png", "imgck",
        chapter_id="@S::ch000", context_snippet="around the image",
    )
    assets = db.get_assets_for_source("@S")
    assert len(assets) == 1
    assert assets[0]["status"] == "pending"
    assert len(db.get_pending_assets()) == 1

    db.update_asset_description("@S::img::x", "Um grafico de barras.", "callck")
    assets = db.get_assets_for_source("@S")
    assert assets[0]["description"] == "Um grafico de barras."
    assert assets[0]["status"] == "described"
    assert db.get_pending_assets() == []


def test_delete_chunks_for_chapter(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "a", "cka")
    db.upsert_chunk("@S::ch000::b", "@S", "@S::ch000", "b", "ckb")
    db.upsert_chunk("@S::ch000::c", "@S", "@S::ch000", "c", "ckc")

    removed = db.delete_chunks_for_chapter("@S::ch000", keep_ids={"@S::ch000::a"})
    assert set(removed) == {"@S::ch000::b", "@S::ch000::c"}
    remaining = {c["chunk_id"] for c in db.get_chunks_for_source("@S")}
    assert remaining == {"@S::ch000::a"}


# ── FTS5 escaping ──────────────────────────────────────────────────────


def test_fts_match_expr_quotes_and_neutralizes_operators():
    # Each token becomes a double-quoted term joined by OR — FTS operators inert.
    assert _fts_match_expr("machine learning") == '"machine" OR "learning"'
    # Operator/punctuation tokens are split by \w+, so "-", NEAR(), ":" never leak.
    assert _fts_match_expr("deep-learning NEAR redes") == (
        '"deep" OR "learning" OR "NEAR" OR "redes"'
    )
    # C++ -> just "C" dropped (len<2) and "" — only tokens >= 2 chars survive.
    assert _fts_match_expr("C++") is None
    # Empty / whitespace / all-short -> None
    assert _fts_match_expr("") is None
    assert _fts_match_expr("   ") is None
    assert _fts_match_expr("a b c") is None


def test_fts_match_expr_caps_token_count():
    many = " ".join(f"tok{i}" for i in range(50))
    expr = _fts_match_expr(many, max_tokens=32)
    assert expr.count(" OR ") == 31  # 32 tokens => 31 separators


def test_fts_match_expr_drops_pt_stopwords():
    # "que" is an extremely common PT-BR conjunction — without filtering it,
    # the OR-joined MATCH would match nearly every note in a real corpus,
    # making a bm25 "hit" meaningless as a relevance signal.
    assert _fts_match_expr("Explique, o que e a chuva?") == '"Explique" OR "chuva"'
    # Meaningful content words are preserved even when short stopwords surround them.
    assert _fts_match_expr("o que e step-back prompting") == (
        '"step" OR "back" OR "prompting"'
    )
    # A query made entirely of stopwords has no usable token.
    assert _fts_match_expr("o que e isso") is None


# ── FTS5 index sync + search ───────────────────────────────────────────


def test_fts_notes_populated_on_upsert_and_search(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n1", "@S", "/p/n1.md", "Aprendizado de Maquina", body="redes neurais profundas")
    db.upsert_note("n2", "@S", "/p/n2.md", "Bancos de Dados", body="indices e transacoes")

    hits = db.search_notes_fts("redes neurais")
    ids = [h["note_id"] for h in hits]
    assert "n1" in ids
    assert "n2" not in ids


def test_fts_search_matches_across_accents(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n1", "@S", "/p/n1.md", "Conexao", body="grafo de conexões tipadas")
    # Query without accent must match indexed text with accent (remove_diacritics 2).
    hits = db.search_notes_fts("conexoes")
    assert any(h["note_id"] == "n1" for h in hits)


def test_fts_chunks_populated_and_deleted(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "transformers e atencao", "cka")
    assert any(h["chunk_id"] == "@S::ch000::a" for h in db.search_chunks_fts("transformers"))

    db.delete_chunks_for_chapter("@S::ch000", keep_ids=set())
    assert db.search_chunks_fts("transformers") == []


def test_fts_reindex_on_note_update(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n1", "@S", "/p/n1.md", "Titulo", body="conteudo antigo sobre arvores")
    assert any(h["note_id"] == "n1" for h in db.search_notes_fts("arvores"))
    db.upsert_note("n1", "@S", "/p/n1.md", "Titulo", body="conteudo novo sobre florestas")
    # Old term gone, new term found.
    assert db.search_notes_fts("arvores") == []
    assert any(h["note_id"] == "n1" for h in db.search_notes_fts("florestas"))


def test_rebuild_fts_counts(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "texto", "cka")
    db.upsert_note("n1", "@S", "/p/n1.md", "Titulo", body="corpo")
    counts = db.rebuild_fts()
    assert counts == {"fts_notes": 1, "fts_chunks": 1}


def test_fts_backfill_on_preexisting_db(tmp_path):
    """A DB that already has notes/chunks but no FTS gets backfilled on open."""
    path = tmp_path / "pre.db"
    db1 = StateDB(path)
    if not db1.fts_enabled:
        db1.close()
        pytest.skip("SQLite build sem FTS5")
    db1.upsert_note("n1", "@S", "/p/n1.md", "Titulo", body="grafos de conhecimento")
    # Simulate a DB created before FTS: drop the FTS tables, then reopen.
    db1.conn.execute("DROP TABLE fts_notes")
    db1.conn.execute("DROP TABLE fts_chunks")
    db1.conn.commit()
    db1.close()

    db2 = StateDB(path)
    try:
        hits = db2.search_notes_fts("grafos")
        assert any(h["note_id"] == "n1" for h in hits)
    finally:
        db2.close()


# ── note_connections (graph edges) ─────────────────────────────────────


def test_note_connections_roundtrip_and_batch(db):
    db.upsert_note_connection("a", "b", "supports", "reforca a tese")
    db.upsert_note_connection("a", "c", "contradicts", "tensiona")
    db.upsert_note_connection("b", "d", "related")

    assert db.count_note_connections() == 3

    # get_note_connections: edges where the note is source OR target.
    a_edges = db.get_note_connections("a")
    assert len(a_edges) == 2
    b_edges = db.get_note_connections("b")
    assert {e["source_note_id"] + "->" + e["target_note_id"] for e in b_edges} == {
        "a->b", "b->d",
    }

    # Batch fetch for BFS frontier.
    batch = db.get_connections_for_notes(["a", "d"])
    keys = {(e["source_note_id"], e["target_note_id"], e["relation_type"]) for e in batch}
    assert ("a", "b", "supports") in keys
    assert ("a", "c", "contradicts") in keys
    assert ("b", "d", "related") in keys
    assert db.get_connections_for_notes([]) == []


def test_note_connection_upsert_updates_description(db):
    db.upsert_note_connection("a", "b", "related", "primeira")
    db.upsert_note_connection("a", "b", "related", "atualizada")
    edges = db.get_note_connections("a")
    assert len(edges) == 1
    assert edges[0]["description"] == "atualizada"
