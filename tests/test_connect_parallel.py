"""Connect with several sources in flight: order inside a source, usage, outages."""

import json
import threading
import time
from pathlib import Path

import pytest
from zettel.config import AppConfig
from zettel.connector import prompt, run
from zettel.connector.parallel import source_groups
from zettel.llm import LLMUnavailableError
from zettel.retrieval import NoteSearchResult, RetrievedNote, Retriever
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.usage import record_llm

from tests.fakes import FakeVectorIndex

SOURCES = ("@A", "@B")
PER_SOURCE = 3


def _cfg(tmp_path: Path, workers: int) -> AppConfig:
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    cfg.linking.connect_parallel_sources = workers
    return cfg


def _seed(db: StateDB) -> None:
    for sid in SOURCES:
        db.upsert_source(sid, sid[1:] + "2024", "Livro", ["Autor"], 2024, sid, f"/{sid}.pdf", "pdf")
        db.upsert_chapter(f"{sid}::ch000", sid, "Cap", "ck")
        for k in range(PER_SOURCE):
            chunk_id = f"{sid}::ch000::{k}"
            db.upsert_chunk(
                chunk_id, sid, f"{sid}::ch000", "texto", f"h{sid}{k}", status="persisted"
            )
            cand = PermanentNoteCandidate(
                thesis=f"Tese declarativa numero {k} da fonte {sid} sobre um conceito",
                definition="Definicao autonoma com palavras suficientes para o schema aqui.",
            )
            db.upsert_concept(
                f"{sid}-c{k}",
                sid,
                chunk_id,
                candidate_json=cand.model_dump_json(),
                status="approved",
            )


class _Model:
    """Fake Prompt 2: records overlap, usage on the caller's tracker, optional outage."""

    def __init__(self, *, fail_on: str = "") -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.threads: set[str] = set()
        self.fail_on = fail_on
        self.calls = 0

    def __call__(self, _llm, user, **_kwargs):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.threads.add(threading.current_thread().name)
            self.calls += 1
        try:
            time.sleep(0.05)
            if self.fail_on and self.fail_on in user:
                raise LLMUnavailableError("provedor fora do ar")
            record_llm(
                model="claude-haiku-5-5",
                provider="anthropic",
                tokens_in=1000,
                tokens_out=200,
                cost_usd=0.001,
            )
            return json.dumps(
                {
                    "status": "ok",
                    "title": "Nota escrita pelo modelo falso",
                    "thesis": "Tese escrita pelo modelo falso sobre o conceito",
                    "definition": "Definicao escrita pelo modelo falso com palavras.",
                    "connections": [],
                }
            )
        finally:
            with self.lock:
                self.active -= 1


def _wire(monkeypatch, db: StateDB, model: _Model) -> dict[str, list[int]]:
    """Fake search returns the notes already written; records how many per source."""
    seen: dict[str, list[int]] = {sid: [] for sid in SOURCES}

    def search(self, query, *_a, source_id=None, **_k):
        own = [n for n in db.list_notes() if n["source_id"] == source_id]
        seen[source_id].append(len(own))
        hits = [RetrievedNote(note_id=n["note_id"], score=1.0, vector_distance=0.1) for n in own]
        return NoteSearchResult(hits=hits, candidates=[])

    monkeypatch.setattr(run, "get_llm", lambda *_a, **_k: object())
    monkeypatch.setattr(prompt, "call_llm", model)
    monkeypatch.setattr(
        "zettel.connector.context.load_connect_taxonomy", lambda *_a, **_k: ({}, {})
    )
    monkeypatch.setattr(Retriever, "search_notes", search)
    return seen


@pytest.mark.parametrize("workers", [1, 2])
def test_each_source_sees_every_earlier_note_of_its_own(tmp_path, monkeypatch, workers):
    cfg = _cfg(tmp_path, workers)
    db = StateDB(tmp_path / "state.db")
    _seed(db)
    model = _Model()
    seen = _wire(monkeypatch, db, model)
    try:
        outcome = run.run_connect(cfg, db, FakeVectorIndex(), run.load_approved_candidates(db))
        assert len(outcome.note_ids) == len(SOURCES) * PER_SOURCE
        # Inside a source the order is sequential: note k saw the k notes before it.
        assert seen == {sid: list(range(PER_SOURCE)) for sid in SOURCES}
        assert model.max_active == workers
        if workers > 1:
            assert all(name.startswith("connect") for name in model.threads)
        statuses = {db.get_concept(f"{s}-c{k}")["status"] for s in SOURCES for k in range(3)}
        assert statuses == {"noted"}
    finally:
        db.close()


def test_worker_usage_reaches_the_run_and_each_note(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, 2)
    db = StateDB(tmp_path / "state.db")
    _seed(db)
    _wire(monkeypatch, db, _Model())
    try:
        outcome = run.run_connect(cfg, db, FakeVectorIndex(), run.load_approved_candidates(db))
        last = db.get_last_run()
        assert last["llm_calls"] == 6
        assert last["tokens_prompt"] == 6000
        [row] = db.usage_by_provider([last["run_id"]])
        assert (row["provider"], row["model"], row["calls"]) == ("anthropic", "claude-haiku-5-5", 6)
        for note_id in outcome.note_ids:
            provenance = json.loads(db.get_note(note_id)["provenance_json"])
            assert provenance["llm_tokens_prompt"] == 1000
            assert provenance["llm_tokens_completion"] == 200
    finally:
        db.close()


def test_a_provider_outage_stops_every_source(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, 2)
    db = StateDB(tmp_path / "state.db")
    _seed(db)
    model = _Model(fail_on="numero 1 da fonte @B")
    _wire(monkeypatch, db, model)
    try:
        with pytest.raises(LLMUnavailableError):
            run.run_connect(cfg, db, FakeVectorIndex(), run.load_approved_candidates(db))
        assert db.get_last_run()["status"] == "failed"
        assert db.get_concept("@B-c1")["status"] == "approved"
        assert db.get_concept("@B-c2")["status"] == "approved"
    finally:
        db.close()


def test_source_groups_keep_queue_order():
    queue = [{"source_id": s, "n": n} for n, s in enumerate(["@A", "@B", "@A", "@C", "@B"])]
    groups = source_groups(queue)
    assert list(groups) == ["@A", "@B", "@C"]
    assert [c["n"] for c in groups["@A"]] == [0, 2]
    assert [c["n"] for c in groups["@B"]] == [1, 4]
