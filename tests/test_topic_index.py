"""Term -> note routing index and its `ask` boost (ADR-036).

The index is a *routing* aid. The Retriever's relevance floor stays the arbiter
of what counts as evidence: a routed note is fed back through the same floor on
the same kind of evidence as any other candidate, never around it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from zettel.config import AppConfig
from zettel.state import StateDB
from zettel.topic_index import (
    SCOPE_MOC,
    SCOPE_SOURCE,
    TOPIC_INDEX_BLOCK,
    TermSource,
    fold,
    render_topic_index_block,
    sources_from_permanent_notes,
    sync_topic_index,
)
from zettel.vault import read_managed_block, safe_write_note

NOTE_A = "01HAAAAAAAAAAAAAAAAAAAAAAA"
NOTE_B = "01HBBBBBBBBBBBBBBBBBBBBBBB"


@pytest.fixture
def db(tmp_path: Path):
    database = StateDB(tmp_path / "state.db")
    yield database
    database.close()


def _permanent_note(db: StateDB, tmp_path: Path, note_id: str, title: str, meta: dict, thesis: str):
    path = tmp_path / f"ZTL - {note_id} - nota.md"
    path.write_text("corpo", encoding="utf-8")
    db.upsert_note(
        note_id=note_id,
        source_id=None,
        path=str(path),
        title=title,
        body=f"> **Tese**: {thesis}\n\n## Definição\n\nTexto.\n",
        frontmatter_json=json.dumps({"title": title, **meta}, ensure_ascii=False),
    )
    return path


# ── Block rendering ────────────────────────────────────────────────────


def test_block_is_empty_but_explicit_without_terms():
    assert render_topic_index_block([]) == "_Nenhum termo indexado ainda._"


def test_block_lists_term_then_targets(db, tmp_path):
    moc_path = tmp_path / "MOC - 01H - tema.md"
    safe_write_note(moc_path, {"type": "moc"}, "# Tema\n\n## Notas\n\n- [[ZTL - A]]\n")
    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A - dropout]]", frameworks=("The 5 Whys",))],
        note_path=moc_path,
    )
    block = read_managed_block(moc_path.read_text(encoding="utf-8"), TOPIC_INDEX_BLOCK)
    assert block == "- **The 5 Whys** -> [[ZTL - A - dropout]]"


def test_section_is_created_once_then_updated_in_place(db, tmp_path):
    moc_path = tmp_path / "MOC - 01H - tema.md"
    safe_write_note(moc_path, {"type": "moc"}, "# Tema\n\nCorpo original.\n")

    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", tags=("dropout",))],
        note_path=moc_path,
    )
    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", tags=("attention",))],
        note_path=moc_path,
    )
    content = moc_path.read_text(encoding="utf-8")
    assert content.count("## Topic Index") == 1
    assert "attention" in content
    assert "dropout" not in content
    # Manual content outside the block survives.
    assert "Corpo original." in content


def test_manual_edits_outside_the_block_survive(db, tmp_path):
    moc_path = tmp_path / "MOC - 01H - tema.md"
    safe_write_note(moc_path, {"type": "moc"}, "# Tema\n\nCorpo.\n")
    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", tags=("dropout",))],
        note_path=moc_path,
    )
    content = moc_path.read_text(encoding="utf-8")
    moc_path.write_text(content + "\n## Minhas anotacoes\n\nComentario a mao.\n", encoding="utf-8")

    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", tags=("attention",))],
        note_path=moc_path,
    )
    final = moc_path.read_text(encoding="utf-8")
    assert "Comentario a mao." in final
    assert "attention" in final


# ── SQLite lookup rows ─────────────────────────────────────────────────


def test_permanent_targets_are_routable(db):
    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", frameworks=("The 5 Whys",))],
    )
    matches = db.match_topic_index(fold("como aplico The 5 Whys aqui?"))
    assert [m["note_id"] for m in matches] == [NOTE_A]


def test_literature_targets_are_listed_but_never_routed(db):
    sync_topic_index(
        db,
        SCOPE_SOURCE,
        "@Fonte2020",
        [TermSource("chunk-1", "[[Fonte/LIT - p001]]", tags=("dropout",))],
        targets_are_permanent_notes=False,
    )
    assert db.match_topic_index(fold("dropout")) == []


def test_refresh_replaces_instead_of_accumulating(db):
    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", tags=("dropout",))],
    )
    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", tags=("attention",))],
    )
    assert db.match_topic_index(fold("dropout")) == []
    assert len(db.match_topic_index(fold("attention"))) == 1


def test_matching_is_accent_and_case_insensitive(db):
    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", tags=("regularização",))],
    )
    assert len(db.match_topic_index(fold("O que e REGULARIZACAO?"))) == 1


def test_deleting_a_scope_clears_its_rows(db):
    sync_topic_index(
        db,
        SCOPE_MOC,
        "01HMOC",
        [TermSource(NOTE_A, "[[ZTL - A]]", tags=("dropout",))],
    )
    db.delete_topic_index_scope(SCOPE_MOC, "01HMOC")
    assert db.match_topic_index(fold("dropout")) == []


def test_sources_from_permanent_notes_reads_frontmatter_and_thesis(db, tmp_path):
    _permanent_note(
        db,
        tmp_path,
        NOTE_A,
        "Dropout",
        {"tags": ["regularizacao"], "named_frameworks": ["The 5 Whys"]},
        "Dropout treina sub-redes",
    )
    sources = sources_from_permanent_notes(db, [NOTE_A, "inexistente"])
    assert len(sources) == 1
    assert sources[0].frameworks == ("The 5 Whys",)
    assert sources[0].tags == ("regularizacao",)
    assert sources[0].thesis == "Dropout treina sub-redes"


# ── MOC lifecycle hook ─────────────────────────────────────────────────


def test_moc_sync_builds_the_index_and_clear_removes_it(db, tmp_path):
    from zettel.moc_backrefs import clear_moc_backrefs, sync_moc_backrefs

    _permanent_note(db, tmp_path, NOTE_A, "Dropout", {"tags": ["dropout"]}, "Tese A")
    moc_path = tmp_path / "MOC - 01HMOC - tema.md"
    body = f"# Tema\n\n## Notas\n\n- [[ZTL - {NOTE_A} - nota]]\n"
    safe_write_note(moc_path, {"type": "moc", "moc_id": "01HMOC"}, body)

    sync_moc_backrefs(db, "01HMOC", "Tema", moc_path, new_body=body)
    assert [m["note_id"] for m in db.match_topic_index(fold("dropout"))] == [NOTE_A]
    assert TOPIC_INDEX_BLOCK in moc_path.read_text(encoding="utf-8")

    clear_moc_backrefs(db, {"moc_id": "01HMOC", "body": body})
    assert db.match_topic_index(fold("dropout")) == []


# ── ask boost ──────────────────────────────────────────────────────────


class _FakeIndex:
    """Chroma stand-in: `notes` maps note_id -> distance for any query."""

    def __init__(self, knn: list[dict], by_id: dict[str, float]):
        self.knn = knn
        self.by_id = by_id
        self.restricted_calls: list[list[str]] = []

    def query_similar_notes(self, query, n_results=5, exclude_id=None):
        return list(self.knn)

    def query_notes_by_ids(self, query_text, note_ids):
        self.restricted_calls.append(list(note_ids))
        return [
            {"id": nid, "distance": self.by_id[nid], "document": "", "metadata": {}}
            for nid in note_ids
            if nid in self.by_id
        ]


def _retriever(db, idx, **overrides):
    from zettel.retrieval import Retriever

    cfg = AppConfig()
    cfg.retrieval.mode = "vector"
    cfg.retrieval.graph_expansion.enabled = False
    for key, value in overrides.items():
        setattr(cfg.retrieval, key, value)
    return Retriever(cfg, db, idx)


def _index_note(db, tmp_path, note_id, tag, distance_map):
    _permanent_note(db, tmp_path, note_id, f"Nota {note_id[-1]}", {"tags": [tag]}, "Tese")
    sync_topic_index(
        db,
        SCOPE_MOC,
        f"moc-{note_id}",
        [TermSource(note_id, f"[[ZTL - {note_id}]]", tags=(tag,))],
    )
    return distance_map


def test_routed_note_becomes_a_seed_when_it_clears_the_floor(db, tmp_path):
    _index_note(db, tmp_path, NOTE_A, "dropout", {})
    # Distance 0.2 -> similarity 0.90, above the default 0.70 floor.
    idx = _FakeIndex(knn=[], by_id={NOTE_A: 0.2})
    result = _retriever(db, idx).search_notes("o que e dropout?")
    assert [h.note_id for h in result.hits] == [NOTE_A]
    assert result.hits[0].origin == "topic_index"
    assert idx.restricted_calls == [[NOTE_A]]


def test_routed_note_still_faces_the_floor(db, tmp_path):
    """The historical bug: a routing hint must not be a free pass."""
    _index_note(db, tmp_path, NOTE_A, "dropout", {})
    # Distance 1.2 -> similarity 0.40, below the default 0.70 floor.
    idx = _FakeIndex(knn=[], by_id={NOTE_A: 1.2})
    result = _retriever(db, idx).search_notes("o que e dropout?")
    assert result.hits == []
    assert [c.note_id for c in result.candidates] == [NOTE_A]
    assert result.candidates[0].passed_floor is False
    assert "abaixo do piso" in result.candidates[0].floor_reason


def test_boost_off_restores_the_previous_behaviour(db, tmp_path):
    _index_note(db, tmp_path, NOTE_A, "dropout", {})
    idx = _FakeIndex(knn=[], by_id={NOTE_A: 0.2})
    result = _retriever(db, idx, topic_index_boost=False).search_notes("o que e dropout?")
    assert result.hits == []
    assert idx.restricted_calls == []


def test_no_extra_query_when_the_note_is_already_in_the_pool(db, tmp_path):
    _index_note(db, tmp_path, NOTE_A, "dropout", {})
    idx = _FakeIndex(
        knn=[{"id": NOTE_A, "distance": 0.2, "document": "", "metadata": {}}],
        by_id={NOTE_A: 0.2},
    )
    result = _retriever(db, idx).search_notes("o que e dropout?")
    assert idx.restricted_calls == []
    assert result.hits[0].origin == "search"


def test_no_extra_query_when_nothing_matches(db, tmp_path):
    _index_note(db, tmp_path, NOTE_A, "dropout", {})
    idx = _FakeIndex(knn=[], by_id={NOTE_A: 0.2})
    _retriever(db, idx).search_notes("pergunta sobre astrofisica")
    assert idx.restricted_calls == []


def test_seed_count_is_capped(db, tmp_path):
    ids = [f"01H{i:023d}" for i in range(8)]
    for note_id in ids:
        _index_note(db, tmp_path, note_id, "dropout", {})
    idx = _FakeIndex(knn=[], by_id=dict.fromkeys(ids, 0.2))
    _retriever(db, idx, topic_index_max_seeds=3).search_notes("dropout")
    assert len(idx.restricted_calls[0]) == 3


# ── Backfill via reindex ───────────────────────────────────────────────


def test_reindex_backfills_every_scope(db, tmp_path):
    """A mature vault should not have to wait for the next review/garden."""
    from zettel.rebuild import rebuild_topic_index

    cfg = AppConfig(vault_path=tmp_path / "vault")
    (cfg.vault_path / "20_Literature").mkdir(parents=True)

    db.upsert_source(
        "@Autor2020",
        citekey="Autor2020",
        title="Livro",
        authors=["A"],
        year=2020,
        file_checksum="c",
        origin_path="/x.pdf",
        origin_type="pdf",
    )
    db.upsert_chapter("@Autor2020::ch000", "@Autor2020", "Cap", "chk")
    db.upsert_chunk("@Autor2020::ch000::a1", "@Autor2020", "@Autor2020::ch000", "t", "c1")
    db.update_chunk_review(
        "@Autor2020::ch000::a1",
        status="persisted",
        summary_json=json.dumps(
            {
                "candidates": [{"thesis": "Tese", "relevance_score": 4, "tags": ["dropout"]}],
            }
        ),
    )

    _permanent_note(db, tmp_path, NOTE_B, "Attention", {"tags": ["attention"]}, "Tese B")
    moc_path = tmp_path / "MOC - 01HMOC - tema.md"
    moc_body = f"# Tema\n\n- [[ZTL - {NOTE_B} - nota]]\n"
    safe_write_note(moc_path, {"type": "moc"}, moc_body)
    db.upsert_moc("01HMOC", topic="Tema", path=str(moc_path), body=moc_body)

    assert rebuild_topic_index(cfg, db) > 0
    # MOC scope routes; source scope is listed but not routable.
    assert [m["note_id"] for m in db.match_topic_index(fold("attention"))] == [NOTE_B]
    assert db.match_topic_index(fold("dropout")) == []
    assert db.match_topic_index_scope(SCOPE_SOURCE, "@Autor2020")
