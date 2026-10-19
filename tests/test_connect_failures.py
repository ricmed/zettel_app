"""Prompt 2 rejections, parse failures and per-connection validation (#284)."""

import json
from pathlib import Path

from zettel.config import AppConfig
from zettel.connector import prompt, run
from zettel.connector.links import resolve_connections
from zettel.connector.prompt import coerce_llm_connections, parse_permanent_note_output
from zettel.retrieval import NoteSearchResult, RetrievedNote, Retriever
from zettel.schemas import PermanentNoteCandidate, PermanentNoteLLMOutput, RelationshipResult
from zettel.state import StateDB


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        vault_path=tmp_path / "vault",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )


def _ready(db: StateDB, tmp_path: Path) -> None:
    db.upsert_source("@S", "S2024", "Livro", ["Autor"], 2024, "h", "/x.pdf", "pdf")
    db.upsert_chapter("@S::ch000", "@S", "Cap", "ck")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "texto", "h1", status="persisted")


def _note(db: StateDB, root: Path, note_id: str) -> None:
    path = root / "30_Permanent" / f"ZTL - {note_id} - nota.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("corpo\n", encoding="utf-8")
    db.upsert_note(note_id, "@S", str(path), title=f"Nota {note_id}", body="corpo")


def _candidate() -> PermanentNoteCandidate:
    return PermanentNoteCandidate(
        thesis="Tese declarativa sobre um conceito atomico aprovado",
        definition="Definicao autonoma com palavras suficientes para o schema.",
    )


def _approve(db: StateDB, cand: PermanentNoteCandidate) -> None:
    db.upsert_concept(
        "c1",
        "@S",
        "@S::ch000::a",
        candidate_json=cand.model_dump_json(),
        status="approved",
    )


def _wire(monkeypatch, response: str, hits: list[RetrievedNote] | None = None) -> dict:
    calls = {"n": 0}

    def _call(*_a, **_k):
        calls["n"] += 1
        return response

    monkeypatch.setattr(run, "get_llm", lambda *_a, **_k: object())
    monkeypatch.setattr(prompt, "call_llm", _call)
    monkeypatch.setattr(
        "zettel.connector.context.load_connect_taxonomy", lambda *_a, **_k: ({}, {})
    )
    monkeypatch.setattr(
        Retriever,
        "search_notes",
        lambda *_a, **_k: NoteSearchResult(hits=hits or [], candidates=[]),
    )
    return calls


class _Index:
    def upsert_permanent_note(self, *_a, **_k):
        return None


def test_rejected_concept_leaves_the_queue_and_is_not_asked_again(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    db = StateDB(tmp_path / "state.db")
    _ready(db, tmp_path)
    cand = _candidate()
    db.upsert_concept(
        "c1", "@S", "@S::ch000::a", candidate_json=cand.model_dump_json(), status="approved"
    )
    response = json.dumps(
        {
            "status": "rejected",
            "reason": "so define o termo",
            "category": "trivial",
            "title": "",
            "thesis": "",
            "definition": "",
            "connections": [],
        }
    )
    calls = _wire(monkeypatch, response)
    candidates = run.load_approved_candidates(db)
    try:
        outcome = run.run_connect(cfg, db, _Index(), candidates)
        assert outcome.note_ids == []
        row = db.get_concept("c1")
        assert row["status"] == "connect_rejected"
        stored = json.loads(row["dedupe_json"])["connect_rejection"]
        assert stored["reason"] == "so define o termo"
        assert stored["category"] == "trivial"
        assert outcome.rejected == 1
        assert db.get_stats()["concepts_connect_rejected"] == 1
        assert run.load_approved_candidates(db) == []
        run.run_connect(cfg, db, _Index(), candidates)
        assert calls["n"] == 1
    finally:
        db.close()


def test_invalid_json_is_recorded_and_not_cached(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    db = StateDB(tmp_path / "state.db")
    _ready(db, tmp_path)
    cand = _candidate()
    db.upsert_concept(
        "c1", "@S", "@S::ch000::a", candidate_json=cand.model_dump_json(), status="approved"
    )
    calls = _wire(monkeypatch, "isto nao e json")
    try:
        assert run.run_connect(cfg, db, _Index(), run.load_approved_candidates(db)).note_ids == []
        assert calls["n"] == 1
        assert db.get_concept("c1")["status"] == "approved"
        failures = db.llm_failure_counts()
        assert failures == [{"phase": "connect", "n": 1}]
        cached = db.conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        assert cached == 0
        row = db.conn.execute("SELECT subject_id, raw_response FROM llm_failures").fetchone()
        assert row[0] == "c1"
        assert "nao e json" in row[1]
    finally:
        db.close()


def test_one_bad_relation_does_not_drop_the_note(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _ready(db, tmp_path)
    _note(db, root, "B")
    _note(db, root, "C")
    _note(db, root, "OUT")
    cand = _candidate()
    db.upsert_concept(
        "c1", "@S", "@S::ch000::a", candidate_json=cand.model_dump_json(), status="approved"
    )
    response = PermanentNoteLLMOutput(
        status="ok",
        title="Conceito valido",
        thesis=cand.thesis,
        definition=cand.definition,
        connections=[
            RelationshipResult(related_note_id="B", relation_type="supports", description="vale"),
            RelationshipResult(related_note_id="C", relation_type="extends", description="vale"),
        ],
    ).model_dump()
    response["connections"].insert(
        0, {"related_note_id": "B", "relation_type": "refines", "description": "nao"}
    )
    response["connections"].append(
        {"related_note_id": "OUT", "relation_type": "related", "description": "fora"}
    )
    hits = [
        RetrievedNote(note_id="B", score=1.0, vector_distance=0.1),
        RetrievedNote(note_id="C", score=1.0, vector_distance=0.1),
    ]
    _wire(monkeypatch, json.dumps(response), hits)
    try:
        outcome = run.run_connect(cfg, db, _Index(), run.load_approved_candidates(db))
        [note_id] = outcome.note_ids
        edges = {
            (row["target_note_id"], row["relation_type"])
            for row in db.get_note_connections(note_id)
            if row["source_note_id"] == note_id
        }
        assert ("B", "supports") in edges
        assert ("C", "extends") in edges
        assert all(target != "OUT" for target, _rel in edges)
        assert all(rel != "refines" for _target, rel in edges)
        assert outcome.edges_by_type["supports"] == 1
        assert outcome.edges_by_type["extends"] == 1
        assert outcome.intra_source == 2
    finally:
        db.close()


def test_coerce_drops_invalid_duplicate_and_extra_connections():
    raw = [
        {"related_note_id": "B", "relation_type": "refines", "description": "nao"},
        {"related_note_id": "B", "relation_type": "supports", "description": "a"},
        {"related_note_id": "B", "relation_type": "extends", "description": "repetida"},
        {"related_note_id": "C", "relation_type": "related", "description": "b"},
        {"related_note_id": "D", "relation_type": "supports", "description": "c"},
        {"related_note_id": "E", "relation_type": "supports", "description": "d"},
        {"related_note_id": "F", "relation_type": "supports", "description": "teto"},
    ]
    kept = [
        (item.related_note_id, item.relation_type.value) for item in coerce_llm_connections(raw)
    ]
    assert kept == [("B", "supports"), ("C", "related"), ("D", "supports"), ("E", "supports")]


def test_resolve_drops_an_existing_note_outside_the_context(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "IN")
    _note(db, root, "OUT")
    resolved = resolve_connections(
        db,
        [
            RelationshipResult(related_note_id="IN", relation_type="supports", description=""),
            RelationshipResult(related_note_id="OUT", relation_type="related", description=""),
        ],
        allowed_ids={"IN"},
    )
    assert [item["related_note_id"] for item in resolved] == ["IN"]
    db.close()


def test_requeue_returns_the_concept_and_force_marks_the_next_call(tmp_path):
    db = StateDB(tmp_path / "state.db")
    _ready(db, tmp_path)
    cand = _candidate()
    db.upsert_concept(
        "c1", "@S", "@S::ch000::a", candidate_json=cand.model_dump_json(), status="approved"
    )
    db.set_concept_dedupe("c1", "approved", {"refines_note_id": "OLD", "reason": "nuance"})
    run.mark_connect_rejected(
        db,
        {"concept_id": "c1"},
        run.ConnectRejected("recusou", reason="recusou", category="trivial"),
    )
    assert db.get_concept("c1")["status"] == "connect_rejected"
    assert run.requeue_connect_rejected(db, force=True) == 1
    row = db.get_concept("c1")
    assert row["status"] == "approved"
    payload = json.loads(row["dedupe_json"])
    assert payload["force_connect"] is True
    assert payload["refines_note_id"] == "OLD"
    assert "connect_rejection" not in payload
    loaded = run.load_approved_candidates(db)
    assert loaded[0]["force_connect"] is True
    assert loaded[0]["refines_note_id"] == "OLD"
    db.close()


def test_parse_rejects_a_note_without_a_thesis():
    text = json.dumps(
        {"status": "ok", "title": "T", "thesis": "", "definition": "D", "connections": []}
    )
    try:
        parse_permanent_note_output(text)
    except ValueError as exc:
        assert "thesis" in str(exc)
    else:
        raise AssertionError("nota sem tese deveria falhar o parse")


def test_connect_queue_order_is_stable(tmp_path):
    db = StateDB(tmp_path / "state.db")
    cand = _candidate().model_dump_json()
    db.upsert_source("@B", "B", "B", ["B"], 2024, "h", "/b.pdf", "pdf")
    db.upsert_source("@A", "A", "A", ["A"], 2024, "h", "/a.pdf", "pdf")
    db.upsert_chapter("@B::ch000", "@B", "Cap", "ck")
    db.upsert_chapter("@A::ch000", "@A", "Cap", "ck")
    db.upsert_chunk("@B::ch000::z", "@B", "@B::ch000", "t", "h", status="persisted", chunk_index=2)
    db.upsert_chunk("@B::ch000::y", "@B", "@B::ch000", "t", "h", status="persisted", chunk_index=1)
    db.upsert_chunk("@A::ch000::z", "@A", "@A::ch000", "t", "h", status="persisted", chunk_index=9)
    db.upsert_chunk("@A::ch000::y", "@A", "@A::ch000", "t", "h", status="persisted", chunk_index=3)
    db.upsert_concept("late-b", "@B", "@B::ch000::z", candidate_json=cand, status="approved")
    db.upsert_concept("early-b", "@B", "@B::ch000::y", candidate_json=cand, status="approved")
    db.upsert_concept("late-a", "@A", "@A::ch000::z", candidate_json=cand, status="approved")
    db.upsert_concept("early-a", "@A", "@A::ch000::y", candidate_json=cand, status="approved")
    first = [row["concept_id"] for row in run.load_approved_candidates(db)]
    second = [row["concept_id"] for row in run.load_approved_candidates(db)]
    assert first == second == ["early-a", "late-a", "early-b", "late-b"]
    only_a = [row["concept_id"] for row in run.load_approved_candidates(db, source_ids=["@A"])]
    assert only_a == ["early-a", "late-a"]
    assert run.load_approved_candidates(db, source_ids=[]) == []
    db.close()


def test_connecting_concept_rewrites_the_same_note(tmp_path):
    from zettel.connector.note import _target_note_id

    db = StateDB(tmp_path / "state.db")
    _ready(db, tmp_path)
    path = tmp_path / "vault" / "30_Permanent" / "ZTL - N1 - nota.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("corpo\n", encoding="utf-8")
    db.upsert_note("N1", "@S", str(path), title="Nota", body="corpo", origin="pipeline")
    db.upsert_concept(
        "c1",
        "@S",
        "@S::ch000::a",
        candidate_json=_candidate().model_dump_json(),
        note_id="N1",
        status="connecting",
    )
    loaded = run.load_approved_candidates(db)
    assert loaded[0]["concept_id"] == "c1"
    assert _target_note_id(db, loaded[0]) == "N1"
    db.close()
