from __future__ import annotations

from pathlib import Path

import pytest
from zettel.config import DEFAULT_RELATION_WEIGHTS, AppConfig, EmbeddingConfig
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


def test_next_queued_job_is_the_oldest_even_after_a_newer_one_finished(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("old", "article", {"topic": "a"})
        db.update_web_job("old", state="succeeded", finished=True)
        assert db.create_web_job("new", "extract", {})
        db.update_web_job("new", state="succeeded", finished=True)
        db.conn.execute("UPDATE web_jobs SET state='queued', phase='queued'")
        db.conn.commit()
        job = db.next_queued_web_job()
        assert job is not None
        assert job["job_id"] == "old"
    finally:
        db.close()


def test_parked_article_does_not_block_and_requeues(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("parked", "article", {"topic": "t"})
        db.update_web_job("parked", state="awaiting_input", phase="context_review")
        assert db.active_web_job() is None
        assert db.requeue_parked_job("parked", {"resume": {"context_decision": "approve"}}) == "ok"
        job = db.get_web_job("parked")
        assert job is not None
        assert job["state"] == "queued"
        assert job["payload"]["resume"]["context_decision"] == "approve"
        assert db.requeue_parked_job("parked", {"resume": {}}) == "busy"
    finally:
        db.close()


def test_recovery_interrupts_a_parked_article(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("parked", "article", {})
        db.update_web_job("parked", state="awaiting_input", phase="outline_review")
        assert db.recover_web_jobs() == 1
        assert db.get_web_job("parked")["state"] == "interrupted"
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
        dashboard = db.get_web_dashboard(
            low_max=0.4, limiar=0.75, relation_weights=DEFAULT_RELATION_WEIGHTS
        )
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
        dashboard = db.get_web_dashboard(
            low_max=0.4, limiar=0.75, relation_weights=DEFAULT_RELATION_WEIGHTS
        )
        assert dashboard["counts"]["permanent_notes"] == 2
        assert db.count_permanent_notes() == 2
    finally:
        db.close()


def test_expected_operational_error_is_safe_and_useful():
    error = UserFacingError("Nenhuma fonte foi criada.")
    assert safe_error(error) == "Nenhuma fonte foi criada."


def test_safe_error_keeps_llm_unavailable_message():
    from zettel.llm import LLMUnavailableError

    err = LLMUnavailableError("connection refused", phase="extract")
    text = safe_error(err)
    assert text.startswith("LLM indisponível")
    assert "connection refused" in text


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


class _GardenDB:
    """The run_all dispatch only reads the garden report back from the database."""

    def latest_garden_report(self, pipeline):
        return None


def _outcome(*, note_ids=None, rejected=0, failed=0, skipped=0, errors=None):
    """Stand-in for ``ConnectOutcome``; ``failed=0`` is success even with no notes."""
    return type(
        "O",
        (),
        {
            "note_ids": list(note_ids or []),
            "rejected": rejected,
            "failed": failed,
            "skipped": skipped,
            "errors": list(errors or []),
        },
    )()


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
        lambda db, source_ids=None: [{"candidate": "approved"}],
    )
    monkeypatch.setattr(
        connector,
        "run_connect",
        lambda cfg, db, idx, candidates, **kwargs: (
            calls.append(("connect", {"candidates": candidates, **kwargs}))
            or _outcome(note_ids=["note"])
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
        _GardenDB(),
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
    assert calls[2][1]["auto_approve"] is False
    assert calls[2][1]["interactive"] is False
    assert calls[2][1]["source_ids"] == ["@source"]
    assert calls[2][1]["surface"] == "web"
    assert calls[2][1]["event_mode"] is None
    assert progress.phases == [
        "run_all",
        "harvest",
        "extract",
        "review",
        "connect",
        "garden",
    ]
    assert result == {
        "garden_report": None,
        "sources": ["@source"],
        "drafts": 1,
        "review": {"approved": 1, "rejected": 0, "skipped": 0},
        "pending_review": 0,
        "approved_threshold": 0,
        "notes": ["note"],
        "mocs": ["moc"],
        "connect_failed": 0,
        "connect_errors": [],
        "harvest_skipped": [],
    }


def test_run_all_reports_each_file_the_harvest_refused(tmp_path: Path, monkeypatch):
    """A refused file used to vanish from the run_all result (#310)."""
    from zettel import connector, extractor, gardener, harvester, index, review
    from zettel.harvester.pipeline import HarvestSkip

    monkeypatch.setattr(index, "VectorIndex", lambda **kwargs: object())
    monkeypatch.setattr(
        harvester,
        "run_harvest",
        lambda cfg, db, idx, **kwargs: harvester.HarvestOutcome(
            source_ids=[],
            skipped=[
                HarvestSkip(
                    path=tmp_path / "inbox" / "scan.pdf",
                    reason="empty_text_layer",
                    message="scan.pdf não tem camada de texto; aplique OCR.",
                )
            ],
        ),
    )
    monkeypatch.setattr(extractor, "run_extract", lambda cfg, db, idx, **kwargs: [])
    monkeypatch.setattr(review, "run_review", lambda cfg, db, idx, **kwargs: {})
    monkeypatch.setattr(connector, "load_approved_candidates", lambda db, source_ids=None: [])
    monkeypatch.setattr(
        connector,
        "run_connect",
        lambda cfg, db, idx, candidates, **kwargs: _outcome(),
    )
    monkeypatch.setattr(gardener, "run_garden", lambda cfg, db, idx, **kwargs: [])

    class Progress:
        def emit(self, event):
            pass

    result = WebWorker._dispatch(
        AppConfig(chroma_path=tmp_path / "chroma"),
        _GardenDB(),
        Progress(),
        "run_all",
        {"duplicate_action": "skip"},
    )
    assert result["harvest_skipped"] == [
        {
            "file": "scan.pdf",
            "reason": "empty_text_layer",
            "message": "scan.pdf não tem camada de texto; aplique OCR.",
        }
    ]


def test_run_all_auto_approve_records_the_unvalidated_warning(tmp_path: Path, monkeypatch):
    from zettel import connector, extractor, gardener, harvester, index, review
    from zettel.review import AUTO_APPROVE_UNVALIDATED_WARNING

    monkeypatch.setattr(index, "VectorIndex", lambda **kwargs: object())
    monkeypatch.setattr(
        harvester,
        "run_harvest",
        lambda cfg, db, idx, **kwargs: harvester.HarvestOutcome(source_ids=[]),
    )
    monkeypatch.setattr(extractor, "run_extract", lambda cfg, db, idx, **kwargs: [])
    monkeypatch.setattr(
        review,
        "run_review",
        lambda cfg, db, idx, **kwargs: {"approved": 2, "pending_review": 0, **kwargs},
    )
    monkeypatch.setattr(connector, "load_approved_candidates", lambda db, source_ids=None: [])
    monkeypatch.setattr(connector, "run_connect", lambda *a, **k: _outcome())
    monkeypatch.setattr(gardener, "run_garden", lambda *a, **k: [])

    class Progress:
        def __init__(self):
            self.messages = []

        def emit(self, event):
            self.messages.append(event.message)

    progress = Progress()
    WebWorker._dispatch(
        AppConfig(chroma_path=tmp_path / "chroma"),
        _GardenDB(),
        progress,
        "run_all",
        {"auto_approve": True},
    )
    review_messages = [message for message in progress.messages if "Fase 3/5" in message]
    assert review_messages
    assert AUTO_APPROVE_UNVALIDATED_WARNING in review_messages[0]


def test_connect_dispatch_succeeds_when_nothing_failed(tmp_path: Path, monkeypatch):
    """An empty notes list used to be the only payload, and looked like success."""
    from zettel import connector, index

    monkeypatch.setattr(index, "VectorIndex", lambda **kwargs: object())
    monkeypatch.setattr(connector, "load_approved_candidates", lambda db: [])
    monkeypatch.setattr(connector, "run_connect", lambda *a, **k: _outcome())

    class Progress:
        def emit(self, event):
            pass

    result = WebWorker._dispatch(
        AppConfig(chroma_path=tmp_path / "chroma"),
        object(),
        Progress(),
        "connect",
        {},
    )
    assert result == {
        "kind": "connect",
        "notes": [],
        "rejected": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }


def test_connect_job_with_failures_is_failed_and_keeps_the_notes(tmp_path: Path, monkeypatch):
    """Prompt 2 errors must not land as succeeded with `{notes: []}`."""
    from zettel import connector, index
    from zettel.web_app import ConfigCache

    config = tmp_path / "config.yaml"
    config.write_text(
        f"state_db_path: {tmp_path / 'state.db'}\n"
        f"chroma_path: {tmp_path / 'chroma'}\n"
        f"cache_path: {tmp_path / 'cache'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(index, "VectorIndex", lambda **kwargs: object())
    monkeypatch.setattr(connector, "load_approved_candidates", lambda db: [{"candidate": "x"}])
    monkeypatch.setattr(
        connector,
        "run_connect",
        lambda *a, **k: _outcome(
            note_ids=["note-1"],
            failed=1,
            errors=["temperature: Extra inputs are not permitted"],
        ),
    )
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("job", "connect", {})
    finally:
        db.close()
    WebWorker(ConfigCache(config))._execute("job")
    db = StateDB(tmp_path / "state.db")
    try:
        job = db.get_web_job("job")
        assert job["state"] == "failed"
        assert "falharam" in job["error_message"]
        assert "temperature" in job["error_message"]
        assert job["result"]["kind"] == "connect"
        assert job["result"]["notes"] == ["note-1"]
        assert job["result"]["failed"] == 1
        assert any(event["phase"] == "failed" for event in db.list_web_job_events("job"))
    finally:
        db.close()


def test_run_all_fails_when_connect_fails_but_keeps_the_garden(tmp_path: Path, monkeypatch):
    from zettel import connector, extractor, gardener, harvester, index, review

    calls = []
    monkeypatch.setattr(index, "VectorIndex", lambda **kwargs: object())
    monkeypatch.setattr(
        harvester,
        "run_harvest",
        lambda cfg, db, idx, **kwargs: harvester.HarvestOutcome(source_ids=["@source"]),
    )
    monkeypatch.setattr(extractor, "run_extract", lambda cfg, db, idx, **kwargs: [])
    monkeypatch.setattr(review, "run_review", lambda cfg, db, idx, **kwargs: {})
    monkeypatch.setattr(connector, "load_approved_candidates", lambda db, source_ids=None: [])
    monkeypatch.setattr(
        connector,
        "run_connect",
        lambda *a, **k: _outcome(note_ids=["n"], failed=1, errors=["boom"]),
    )
    monkeypatch.setattr(gardener, "run_garden", lambda *a, **k: calls.append("garden") or ["moc"])

    class Progress:
        def emit(self, event):
            pass

    with pytest.raises(UserFacingError, match="boom") as caught:
        WebWorker._dispatch(
            AppConfig(chroma_path=tmp_path / "chroma"),
            _GardenDB(),
            Progress(),
            "run_all",
            {"duplicate_action": "skip"},
        )
    assert calls == ["garden"]
    assert caught.value.result["notes"] == ["n"]
    assert caught.value.result["mocs"] == ["moc"]
    assert caught.value.result["connect_failed"] == 1
    assert caught.value.result["connect_errors"] == ["boom"]


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


def test_runs_started_by_a_job_are_linked_to_it_and_no_other(tmp_path: Path):
    """The job used to guess its run as "the last one that changed" (#311)."""
    from zettel.state.runs import current_web_job

    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("job", "run_all", {})
        token = current_web_job.set("job")
        try:
            first = db.start_run("harvest")
            second = db.start_run("extract")
        finally:
            current_web_job.reset(token)
        outside = db.start_run("connect")  # a CLI run in the same window
        assert [run["run_id"] for run in db.list_web_job_runs("job")] == [first, second]
        assert outside not in {run["run_id"] for run in db.list_web_job_runs("job")}
    finally:
        db.close()


def test_recovery_closes_the_runs_of_an_interrupted_job(tmp_path: Path):
    from zettel.state.runs import current_web_job

    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("job", "extract", {})
        assert db.claim_web_job("job")
        token = current_web_job.set("job")
        try:
            run_id = db.start_run("extract")
        finally:
            current_web_job.reset(token)
        cli_run = db.start_run("connect")
        db.recover_web_jobs()
        runs = {run["run_id"]: run for run in db.list_web_job_runs("job")}
        assert runs[run_id]["status"] == "interrupted"
        assert runs[run_id]["finished_at"]
        assert (
            db.conn.execute("SELECT status FROM runs WHERE run_id=?", (cli_run,)).fetchone()[0]
            == "running"
        )
    finally:
        db.close()


def test_sync_records_a_run(tmp_path: Path):
    from zettel.sync import run_sync_manual

    cfg = AppConfig(vault_path=tmp_path / "vault", state_db_path=tmp_path / "state.db")
    db = StateDB(cfg.state_db_path)
    try:
        run_sync_manual(cfg, db, None)
        last = db.get_last_run()
        assert last["pipeline_signature"] == "sync"
        assert last["status"] == "completed"
    finally:
        db.close()


_LONG_AGO = "2020-01-01T00:00:00+00:00"


def _finished_job(db, job_id, operation="extract", state="succeeded", finished_at=_LONG_AGO):
    assert db.create_web_job(job_id, operation, {})
    assert db.claim_web_job(job_id)
    db.update_web_job(job_id, state=state, phase="completed", finished=True)
    db.add_web_job_event(job_id, operation, message="feito")
    db.conn.execute("UPDATE web_jobs SET finished_at=? WHERE job_id=?", (finished_at, job_id))
    db.conn.commit()


def test_purge_web_jobs_drops_finished_jobs_past_the_retention(tmp_path: Path):
    """Jobs, events and run links no longer grow forever (#313); runs stay as history."""
    from zettel.state.runs import current_web_job
    from zettel.time import now_utc_iso

    db = StateDB(tmp_path / "state.db")
    try:
        _finished_job(db, "old-ok")
        token = current_web_job.set("old-ok")
        try:
            run_id = db.start_run("extract")
        finally:
            current_web_job.reset(token)
        _finished_job(db, "old-failed", state="failed")
        _finished_job(db, "old-prep", operation="prepare_harvest")
        db.create_web_harvest_review("old-prep", "checksum")
        _finished_job(db, "recent", finished_at=now_utc_iso())
        assert db.create_web_job("queued-old", "extract", {})
        db.conn.execute("UPDATE web_jobs SET created_at=? WHERE job_id='queued-old'", (_LONG_AGO,))
        db.conn.commit()

        assert db.purge_web_jobs(older_than_days=0) == 0  # 0 keeps everything
        assert db.purge_web_jobs(older_than_days=30) == 3

        assert {job["job_id"] for job in db.list_web_jobs()} == {"recent", "queued-old"}
        assert db.list_web_job_events("old-ok") == []
        assert db.list_web_job_runs("old-ok") == []
        assert db.get_web_harvest_review("old-prep") is None
        assert db.get_run(run_id) is not None
    finally:
        db.close()


def test_job_events_are_read_through_an_index(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        plan = db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM web_job_events "
            "WHERE job_id=? AND event_id>? ORDER BY event_id",
            ("job", 0),
        ).fetchall()
    finally:
        db.close()
    details = " ".join(str(row[-1]) for row in plan)
    assert "idx_web_job_events_job" in details
    assert "TEMP B-TREE" not in details


def test_worker_start_applies_the_job_retention(tmp_path: Path):
    from zettel.web_app import ConfigCache

    config = tmp_path / "config.yaml"
    config.write_text(
        f"state_db_path: {tmp_path / 'state.db'}\n"
        f"cache_path: {tmp_path / 'cache'}\n"
        "web:\n  job_retention_days: 7\n",
        encoding="utf-8",
    )
    db = StateDB(tmp_path / "state.db")
    try:
        _finished_job(db, "old-ok")
    finally:
        db.close()
    worker = WebWorker(ConfigCache(config))
    worker.start()
    worker.stop()
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.get_web_job("old-ok") is None
    finally:
        db.close()


def test_safe_error_hides_any_message_it_does_not_know(tmp_path: Path):
    """An allowlist, not a blocklist: a Windows path has no "secret" in it (#315)."""
    leak = RuntimeError(r"no such table em D:\projetos\zettel_app\data\state.db")
    message = safe_error(leak, ref="ab12cd34")
    assert "D:\\" not in message and "state.db" not in message
    assert "ab12cd34" in message
    assert "projetos" not in safe_error(ValueError("D:/projetos/x"), ref="r1")


def test_safe_error_keeps_known_messages_without_their_paths():
    from zettel.harvester.extract import PdfExtractionError
    from zettel.oplock import LockBusy

    assert safe_error(UserFacingError("Nenhuma fonte foi criada.")) == "Nenhuma fonte foi criada."
    busy = safe_error(LockBusy({"operation": "connect", "surface": "cli", "pid": 7}))
    assert busy.startswith("Outra operação mutante está em andamento: connect (CLI, pid 7")
    pdf = safe_error(
        PdfExtractionError(r"Extracao falhou: erro em C:\Users\ana\AppData\docling\x.bin")
    )
    assert pdf.startswith("Extracao falhou")
    assert "Users" not in pdf and "C:\\" not in pdf
    posix = safe_error(UserFacingError("Arquivo /home/ana/vault/nota.md sumiu"))
    assert "/home/" not in posix and "sumiu" in posix


def test_every_known_error_name_is_a_real_exception():
    """A rename must not silently drop a class from the allowlist."""
    import importlib

    from zettel.web_app import KNOWN_ERRORS

    for name in KNOWN_ERRORS:
        module, _, attr = name.rpartition(".")
        cls = getattr(importlib.import_module(module), attr)
        assert issubclass(cls, BaseException), name


@pytest.mark.parametrize(
    ("declined", "outcome"),
    [
        (("duplicate_content", "mesmo texto da fonte @A (camada 2, conteúdo)", "@A"), "reused"),
        (("biblio_incomplete", "metadados bibliográficos incompletos", None), "refused"),
        ((None, None, None), "unexplained"),
    ],
)
def test_a_harvest_without_a_source_says_why(tmp_path: Path, monkeypatch, declined, outcome):
    """One message per cause, not three guesses (#316)."""
    from zettel import harvester, index
    from zettel.harvester.pipeline import HarvestSkip

    reason, message, source_id = declined
    path = tmp_path / "inbox" / "obra.md"
    result = harvester.HarvestOutcome(
        declined=[HarvestSkip(path, reason, message, source_id)] if reason else []
    )
    monkeypatch.setattr(index, "VectorIndex", lambda **kwargs: object())
    monkeypatch.setattr(harvester, "run_harvest", lambda cfg, db, idx, **kwargs: result)

    class Progress:
        job_id = "job"

        def emit(self, event):
            pass

    db = StateDB(tmp_path / "state.db")
    try:
        run = lambda: WebWorker._dispatch(  # noqa: E731
            AppConfig(chroma_path=tmp_path / "chroma"),
            db,
            Progress(),
            "harvest",
            {"selected_file": str(path)},
        )
        if outcome == "reused":
            assert run() == {"sources": ["@A"], "skipped": message}
        elif outcome == "refused":
            with pytest.raises(UserFacingError, match="incompletos"):
                run()
        else:
            with pytest.raises(UserFacingError, match="não registrou o motivo"):
                run()
    finally:
        db.close()


class _ParkedDrive:
    """Stands in for an ArticleDrive parked at a review: it owns a connection."""

    def __init__(self, path):
        self.db = StateDB(path)
        self.abandoned = False

    def abandon(self):
        self.abandoned = True


def _parked_worker(tmp_path: Path, ttl_hours: float, now: list[float]):
    from zettel.web_app import ConfigCache

    config = tmp_path / "config.yaml"
    config.write_text(
        f"state_db_path: {tmp_path / 'state.db'}\nweb:\n  article_pause_ttl_hours: {ttl_hours}\n",
        encoding="utf-8",
    )
    return WebWorker(ConfigCache(config), clock=lambda: now[0])


def _park(db, worker, job_id, tmp_path):
    assert db.create_web_job(job_id, "article", {"topic": "t"})
    assert db.claim_web_job(job_id)
    db.update_web_job(job_id, state="awaiting_input", phase="article")
    worker._articles[job_id] = _ParkedDrive(tmp_path / "state.db")
    worker._parked_since[job_id] = 0.0


def test_a_parked_article_expires_after_the_ttl(tmp_path: Path):
    """An abandoned pause held a connection and the graph until the process stopped (#317)."""
    import sqlite3

    now = [0.0]
    worker = _parked_worker(tmp_path, 24, now)
    db = StateDB(tmp_path / "state.db")
    try:
        _park(db, worker, "art", tmp_path)
        drive = worker._articles["art"]

        now[0] = 23 * 3600
        assert worker.expire_parked_articles() == 0
        assert db.get_web_job("art")["state"] == "awaiting_input"

        now[0] = 24 * 3600 + 1
        assert worker.expire_parked_articles() == 1
        job = db.get_web_job("art")
        assert job["state"] == "interrupted"
        assert "expirou" in job["message"]
        assert drive.abandoned and "art" not in worker._articles
        with pytest.raises(sqlite3.ProgrammingError):
            drive.db.conn.execute("SELECT 1")  # its connection is closed
    finally:
        db.close()


def test_a_resumed_article_is_never_expired(tmp_path: Path):
    now = [0.0]
    worker = _parked_worker(tmp_path, 1, now)
    db = StateDB(tmp_path / "state.db")
    try:
        _park(db, worker, "art", tmp_path)
        assert db.requeue_parked_job("art", {"resume": {"context_decision": "approve"}}) == "ok"
        now[0] = 10 * 3600
        assert worker.expire_parked_articles() == 0
        assert db.get_web_job("art")["state"] == "queued"
        assert not worker._articles["art"].abandoned
    finally:
        worker._articles.pop("art").db.close()
        db.close()
