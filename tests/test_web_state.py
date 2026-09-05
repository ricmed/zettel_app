from __future__ import annotations

from pathlib import Path

import pytest
from zettel.config import AppConfig, EmbeddingConfig
from zettel.index import index_kwargs
from zettel.state import StateDB
from zettel.web_app import UserFacingError, WebWorker, safe_error


def test_web_queue_enforces_mutual_exclusion_and_transitions(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("one", "extract", {"x": 1})
        assert not db.create_web_job("two", "garden", {})
        assert db.claim_web_job("one")
        db.update_web_job(
            "one",
            state="succeeded",
            phase="completed",
            result={"drafts": 2},
            finished=True,
        )
        job = db.get_web_job("one")
        assert job is not None
        assert job["state"] == "succeeded"
        assert job["result"] == {"drafts": 2}
        assert db.create_web_job("two", "garden", {})
    finally:
        db.close()


def test_recovery_interrupts_running_but_keeps_queued(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("running", "extract", {})
        assert db.claim_web_job("running")
        assert db.recover_web_jobs() == 1
        assert db.get_web_job("running")["state"] == "interrupted"
        assert db.create_web_job("queued", "garden", {})
        assert db.recover_web_jobs() == 0
        assert db.get_web_job("queued")["state"] == "queued"
    finally:
        db.close()


def test_progress_events_and_dashboard_are_persisted(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("job", "extract", {})
        db.add_web_job_event(
            "job",
            "extract",
            current_item="chunk-1",
            current_index=1,
            total_items=3,
            message="Processando",
        )
        events = db.list_web_job_events("job")
        assert events[0]["current_index"] == 1
        assert events[0]["total_items"] == 3
        dashboard = db.get_web_dashboard()
        assert dashboard["counts"]["sources"] == 0
        assert dashboard["counts"]["isolated_notes"] == 0
        assert dashboard["relations"] == []
    finally:
        db.close()


def test_dashboard_permanent_notes_count_windows_paths(tmp_path: Path):
    """Dashboard must count ZTL notes when path uses backslashes (Windows)."""
    db = StateDB(tmp_path / "state.db")
    try:
        win_path = str(tmp_path / "vault" / "30_Permanent" / "ZTL - 01ABC - teste.md")
        posix_path = "vault/30_Permanent/ZTL - 01DEF - teste.md"
        db.upsert_note("NOTE01", None, win_path, title="Windows")
        db.upsert_note("NOTE02", None, posix_path, title="Posix")
        db.upsert_note("NOTE03", None, "vault/20_Literature/LIT - x.md", title="LIT")
        dashboard = db.get_web_dashboard()
        assert dashboard["counts"]["permanent_notes"] == 2
        assert db.count_permanent_notes() == 2
    finally:
        db.close()


def test_expected_operational_error_is_safe_and_useful():
    error = UserFacingError("Nenhuma fonte foi criada.")
    assert safe_error(error) == "Nenhuma fonte foi criada."


def test_idx_kwargs_forwards_embedding_dimensions():
    """The web and the CLI must open the same embedding space.

    ``index_kwargs`` is the single builder both entry points call. It replaced a
    copy in each of them: the web copy had dropped ``dimensions``, so a reducing
    model wrote full-width vectors through the web and reduced ones through the
    CLI, into the same Chroma store.
    """
    cfg = AppConfig(
        embedding=EmbeddingConfig(provider="ollama", model="qwen3-embedding", dimensions=1024)
    )
    assert index_kwargs(cfg)["dimensions"] == 1024


def test_run_all_dispatches_every_phase_in_order(tmp_path: Path, monkeypatch):
    from zettel import connector, extractor, gardener, harvester, index, review

    calls = []

    monkeypatch.setattr(index, "VectorIndex", lambda **kwargs: object())
    monkeypatch.setattr(
        harvester,
        "run_harvest",
        lambda cfg, db, idx, **kwargs: (
            calls.append(("harvest", kwargs)) or harvester.HarvestOutcome(source_ids=["@source"])
        ),
    )
    monkeypatch.setattr(
        extractor,
        "run_extract",
        lambda cfg, db, idx, **kwargs: calls.append(("extract", kwargs)) or ["draft"],
    )
    monkeypatch.setattr(
        review,
        "run_review",
        lambda cfg, db, idx, **kwargs: (
            calls.append(("review", kwargs)) or {"approved": 1, "rejected": 0, "skipped": 0}
        ),
    )
    monkeypatch.setattr(
        connector,
        "load_approved_candidates",
        lambda db: [{"candidate": "approved"}],
    )
    monkeypatch.setattr(
        connector,
        "run_connect",
        lambda cfg, db, idx, candidates, **kwargs: (
            calls.append(("connect", {"candidates": candidates, **kwargs})) or ["note"]
        ),
    )
    monkeypatch.setattr(
        gardener,
        "run_garden",
        lambda cfg, db, idx, **kwargs: calls.append(("garden", kwargs)) or ["moc"],
    )

    class Progress:
        def __init__(self):
            self.phases = []

        def emit(self, event):
            self.phases.append(event.phase)

    progress = Progress()
    result = WebWorker._dispatch(
        AppConfig(chroma_path=tmp_path / "chroma"),
        object(),
        progress,
        "run_all",
        {"duplicate_action": "skip", "skip_biblio": False, "skip_paging": True},
    )

    assert [name for name, _ in calls] == [
        "harvest",
        "extract",
        "review",
        "connect",
        "garden",
    ]
    assert calls[0][1]["interactive"] is False
    assert calls[0][1]["duplicate_action"] == "skip"
    assert calls[2][1] == {"auto_approve": True, "interactive": False}
    assert progress.phases == [
        "run_all",
        "harvest",
        "extract",
        "review",
        "connect",
        "garden",
    ]
    assert result == {
        "sources": ["@source"],
        "drafts": 1,
        "review": {"approved": 1, "rejected": 0, "skipped": 0},
        "notes": ["note"],
        "mocs": ["moc"],
    }


def test_manual_lit_to_ztl_dispatches_specialized_flow(tmp_path: Path, monkeypatch):
    from zettel import index, manual_lit

    captured = {}
    monkeypatch.setattr(
        index,
        "VectorIndex",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("index must stay lazy")),
    )

    def fake_create(cfg, db, idx, ref, **kwargs):
        captured.update(ref=ref, kwargs=kwargs)
        assert idx is None
        return tmp_path / "vault" / "note.md", False

    monkeypatch.setattr(manual_lit, "create_permanent_from_literature", fake_create)

    class Progress:
        def emit(self, event):
            pass

    result = WebWorker._dispatch(
        AppConfig(chroma_path=tmp_path / "chroma"),
        object(),
        Progress(),
        "manual-ztl-from-lit",
        {"chunk_id": "chunk-1", "thesis": "Uma tese", "use_llm": False},
    )

    assert captured == {
        "ref": "chunk-1",
        "kwargs": {"thesis": "Uma tese", "use_llm": False, "force": False},
    }
    assert result == {
        "path": str(tmp_path / "vault" / "note.md"),
        "used_llm": False,
    }


def test_manual_lit_to_ztl_accepts_ref_and_force(tmp_path: Path, monkeypatch):
    from zettel import index, manual_lit

    captured = {}
    monkeypatch.setattr(
        index,
        "VectorIndex",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("index must stay lazy")),
    )

    def fake_create(cfg, db, idx, ref, **kwargs):
        captured.update(ref=ref, kwargs=kwargs)
        return tmp_path / "vault" / "note.md", False

    monkeypatch.setattr(manual_lit, "create_permanent_from_literature", fake_create)

    class Progress:
        def emit(self, event):
            pass

    result = WebWorker._dispatch(
        AppConfig(chroma_path=tmp_path / "chroma"),
        object(),
        Progress(),
        "manual-ztl-from-lit",
        {
            "ref": "20_Literature/Kahneman2011/note.md",
            "chunk_id": "legacy",
            "thesis": "Tese",
            "use_llm": False,
            "force": True,
        },
    )
    assert captured["ref"] == "20_Literature/Kahneman2011/note.md"
    assert captured["kwargs"]["force"] is True
    assert result == {
        "path": str(tmp_path / "vault" / "note.md"),
        "used_llm": False,
    }


def test_manual_lit_llm_rejection_is_user_facing(tmp_path: Path, monkeypatch):
    from zettel import index, manual_lit
    from zettel.connector import ConnectRejected
    from zettel.web_app import UserFacingError

    monkeypatch.setattr(index, "VectorIndex", lambda **kwargs: object())

    def fake_create(cfg, db, idx, ref, **kwargs):
        raise ConnectRejected(
            "O modelo recusou gerar a nota permanente: definicao generica. "
            "Enriqueça o resumo ou a tese da LIT e tente de novo, ou crie a ZTL sem o LLM.",
            reason="definicao generica",
        )

    monkeypatch.setattr(manual_lit, "create_permanent_from_literature", fake_create)

    class Progress:
        def emit(self, event):
            pass

    with pytest.raises(UserFacingError, match="definicao generica") as caught:
        WebWorker._dispatch(
            AppConfig(chroma_path=tmp_path / "chroma"),
            object(),
            Progress(),
            "manual-ztl-from-lit",
            {"chunk_id": "chunk-1", "thesis": "Uma tese", "use_llm": True},
        )
    assert safe_error(caught.value).startswith("O modelo recusou")
