from __future__ import annotations

from pathlib import Path

from zettel.state import StateDB
from zettel.web_app import UserFacingError, safe_error


def test_web_queue_enforces_mutual_exclusion_and_transitions(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    try:
        assert db.create_web_job("one", "extract", {"x": 1})
        assert not db.create_web_job("two", "garden", {})
        assert db.claim_web_job("one")
        db.update_web_job(
            "one", state="succeeded", phase="completed",
            result={"drafts": 2}, finished=True,
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
            "job", "extract", current_item="chunk-1",
            current_index=1, total_items=3, message="Processando",
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


def test_expected_operational_error_is_safe_and_useful():
    error = UserFacingError("Nenhuma fonte foi criada.")
    assert safe_error(error) == "Nenhuma fonte foi criada."