"""Application layer for the server-rendered Zettelkasten web UI.

This module deliberately contains no HTTP concerns.  It owns the durable,
single-worker queue and calls the existing pipeline entry points with their
normal StateDB/VectorIndex dependencies.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from zettel.config import AppConfig, load_config
from zettel.state import StateDB

logger = logging.getLogger(__name__)


class UserFacingError(RuntimeError):
    """Expected operational failure whose message is safe for the browser."""


def safe_error(exc: BaseException) -> str:
    """Return a useful, non-sensitive message for a browser response."""
    text = str(exc).replace("\n", " ").strip()
    if isinstance(exc, UserFacingError):
        return text[:300]
    if not text:
        return "A operação falhou. Consulte os logs do servidor."
    # Never echo host paths, API keys or provider response bodies to the UI.
    sensitive = ("api_key", "api key", "secret", "password", "token", "/home/", "\\users\\")
    if any(word in text.lower() for word in sensitive):
        return "A operação falhou. Verifique a configuração e os logs do servidor."
    return text[:300]


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    message: str
    current_item: str | None = None
    current_index: int | None = None
    total_items: int | None = None


class JobProgress:
    """Persist progress and events at every safe checkpoint."""

    def __init__(self, db: StateDB, job_id: str):
        self.db = db
        self.job_id = job_id

    def emit(self, event: ProgressEvent) -> None:
        self.db.update_web_job(
            self.job_id,
            phase=event.phase,
            current_item=event.current_item,
            current_index=event.current_index,
            total_items=event.total_items,
            message=event.message,
        )
        self.db.add_web_job_event(
            self.job_id,
            event.phase,
            current_item=event.current_item,
            current_index=event.current_index,
            total_items=event.total_items,
            message=event.message,
        )

    def update(
        self,
        phase: str,
        message: str,
        *,
        current_item: str | None = None,
        current_index: int | None = None,
        total_items: int | None = None,
    ) -> None:
        self.emit(ProgressEvent(
            phase=phase,
            message=message,
            current_item=current_item,
            current_index=current_index,
            total_items=total_items,
        ))


def _idx_kwargs(cfg: AppConfig) -> dict[str, Any]:
    return {
        "chroma_path": cfg.chroma_path,
        "embedding_provider": cfg.embedding.provider,
        "embedding_model": cfg.embedding.model,
        "device": cfg.device,
        "allow_fallback": cfg.embedding.allow_fallback,
        "base_url": cfg.embedding.base_url,
        "dimensions": cfg.embedding.dimensions,
    }


def _load_candidates(db: StateDB) -> list[dict]:
    from zettel.schemas import PermanentNoteCandidate

    output = []
    for concept in db.get_concepts_by_status("approved", without_notes=True):
        raw = concept.get("candidate_json")
        if raw:
            output.append({
                "concept_id": concept["concept_id"],
                "source_id": concept["source_id"],
                "chunk_id": concept["chunk_id"],
                "candidate": PermanentNoteCandidate.model_validate_json(raw),
            })
    return output


class WebWorker:
    """A durable queue backed by SQLite and one process-local worker thread."""

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = config_path
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def _db(self) -> StateDB:
        return StateDB(load_config(self.config_path).state_db_path)

    def start(self) -> None:
        db = self._db()
        try:
            recovered = db.recover_web_jobs()
            if recovered:
                logger.warning("Web: %d trabalho(s) marcados como interrupted", recovered)
        finally:
            db.close()
        self._thread = threading.Thread(target=self._run, name="zettel-web-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def submit(self, operation: str, payload: dict[str, Any]) -> str | None:
        job_id = uuid4().hex
        db = self._db()
        try:
            created = db.create_web_job(job_id, operation, payload)
        finally:
            db.close()
        if not created:
            return None
        self._wake.set()
        return job_id

    def _run(self) -> None:
        while not self._stop.is_set():
            db = self._db()
            try:
                queued = db.list_web_jobs(limit=1)
                job = queued[0] if queued and queued[0]["state"] == "queued" else None
            finally:
                db.close()
            if not job:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            self._execute(job["job_id"])

    def _execute(self, job_id: str) -> None:
        cfg = load_config(self.config_path)
        db = StateDB(cfg.state_db_path)
        if not db.claim_web_job(job_id):
            db.close()
            return
        progress = JobProgress(db, job_id)
        job = db.get_web_job(job_id) or {}
        payload = job.get("payload") or {}
        operation = job.get("operation", "")
        previous_run = db.get_last_run()
        previous_run_id = previous_run["run_id"] if previous_run else None
        progress.emit(ProgressEvent("starting", f"Iniciando {operation}."))
        try:
            result = self._dispatch(cfg, db, progress, operation, payload)
            last_run = db.get_last_run()
            run_id = (
                last_run["run_id"] if last_run and last_run["run_id"] != previous_run_id
                else None
            )
            db.update_web_job(
                job_id, state="succeeded", phase="completed",
                message="Operação concluída.", result=result or {}, run_id=run_id, finished=True,
            )
            db.add_web_job_event(job_id, "completed", message="Operação concluída.")
        except Exception as exc:  # worker must survive a failed job
            logger.error("Trabalho web %s falhou: %s\n%s", job_id, exc, traceback.format_exc())
            message = safe_error(exc)
            db.update_web_job(
                job_id, state="failed", phase="failed",
                message=message, error_message=message, finished=True,
            )
            db.add_web_job_event(job_id, "failed", message=message)
        finally:
            db.close()

    @staticmethod
    def _dispatch(
        cfg: AppConfig, db: StateDB, progress: JobProgress,
        operation: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        progress.emit(ProgressEvent(operation, f"Carregando dependências para {operation}."))
        if operation == "retry_chunks":
            failed = db.get_failed_chunks(payload.get("source_id"))
            for chunk in failed:
                db.update_chunk_status(chunk["chunk_id"], "pending")
            return {"chunks_reset": len(failed)}
        if operation == "retry_assets":
            return {"assets_reset": db.reset_failed_assets()}

        from zettel.index import VectorIndex
        idx = VectorIndex(**_idx_kwargs(cfg))
        if operation == "run_all":
            from zettel.connector import run_connect
            from zettel.extractor import run_extract
            from zettel.gardener import run_garden
            from zettel.harvester import run_harvest
            from zettel.review import run_review

            progress.emit(ProgressEvent("harvest", "Fase 1/5 — iniciando harvest."))
            sources = run_harvest(
                cfg, db, idx, interactive=False,
                duplicate_action=payload.get("duplicate_action", "skip"),
                skip_biblio=bool(payload.get("skip_biblio", False)),
                skip_paging=bool(payload.get("skip_paging", False)),
                observer=progress,
            )

            progress.emit(ProgressEvent("extract", "Fase 2/5 — iniciando extract."))
            drafts = run_extract(cfg, db, idx, auto_approve=False, observer=progress)

            progress.emit(ProgressEvent("review", "Fase 3/5 — aprovando drafts elegíveis."))
            review_stats = run_review(
                cfg, db, idx, auto_approve=True, interactive=False,
            )

            approved = _load_candidates(db)
            progress.emit(ProgressEvent(
                "connect", f"Fase 4/5 — gerando {len(approved)} nota(s).",
                total_items=len(approved),
            ))
            note_ids = run_connect(cfg, db, idx, approved, observer=progress)

            progress.emit(ProgressEvent("garden", "Fase 5/5 — atualizando mapas de conteúdo."))
            moc_ids = run_garden(cfg, db, idx, observer=progress)
            return {
                "sources": sources,
                "drafts": len(drafts),
                "review": review_stats,
                "notes": note_ids,
                "mocs": moc_ids,
            }
        if operation == "harvest":
            from zettel.harvester import run_harvest
            selected = payload.get("selected_file")
            file_path = Path(selected).resolve() if selected else None
            progress.emit(ProgressEvent("harvest", "Processando documento.", current_item=file_path.name if file_path else None))
            sources = run_harvest(
                cfg, db, idx, interactive=False,
                duplicate_action=payload.get("duplicate_action", "skip"),
                skip_biblio=bool(payload.get("skip_biblio", False)),
                content_start_file=payload.get("content_start_file"),
                content_start_book=payload.get("content_start_book"),
                skip_paging=bool(payload.get("skip_paging", False)),
                selected_file=file_path,
                observer=progress,
            )
            if not sources:
                existing = db.get_file(str(file_path)) if file_path else None
                if existing and existing.get("source_id"):
                    return {
                        "sources": [existing["source_id"]],
                        "skipped": "Documento já ingerido; nenhuma alteração necessária.",
                    }
                raise UserFacingError(
                    "Nenhuma fonte foi criada. Verifique se o documento contém texto "
                    "extraível, se não é uma duplicata e se as opções bibliográficas "
                    "estão corretas."
                )
            return {"sources": sources}
        if operation == "extract":
            from zettel.extractor import run_extract
            total = len(db.get_pending_chunks())
            progress.emit(ProgressEvent("extract", f"Extraindo {total} chunk(s).", total_items=total))
            candidates = run_extract(cfg, db, idx, auto_approve=False, observer=progress)
            return {"drafts": len(candidates), "auto_approved": False}
        if operation == "review":
            from zettel.review import approve_chunk, finalize_approved_concepts, reject_chunk
            from zettel.usage import begin_run, finish_pipeline_run
            action = payload.get("action")
            chunk_ids = list(payload.get("chunk_ids") or [])
            if not chunk_ids and payload.get("confidence_below") is not None:
                chunks = db.get_chunks_by_status("awaiting_review")
                threshold = float(payload["confidence_below"])
                chunk_ids = [c["chunk_id"] for c in chunks if (c.get("review_confidence") or 0) < threshold]
            stats = {"approved": 0, "rejected": 0, "skipped": 0}
            total = len(chunk_ids)
            review_run_id = db.start_run("review")
            begin_run(review_run_id)
            try:
                for number, chunk_id in enumerate(chunk_ids, 1):
                    progress.emit(ProgressEvent(
                        "review", f"Revisando item {number}/{total}.",
                        current_item=chunk_id[-18:], current_index=number, total_items=total,
                    ))
                    ok = (
                        approve_chunk(cfg, db, idx, chunk_id)
                        if action == "approve" else reject_chunk(cfg, db, idx, chunk_id)
                    )
                    stats["approved" if action == "approve" else "rejected"] += int(ok)
                    stats["skipped"] += int(not ok)
                if action == "approve" and stats["approved"]:
                    finalize_approved_concepts(cfg, db, idx)
            except Exception:
                finish_pipeline_run(db, review_run_id, status="failed")
                raise
            finish_pipeline_run(db, review_run_id)
            return stats
        if operation == "connect":
            from zettel.connector import run_connect
            candidates = _load_candidates(db)
            progress.emit(ProgressEvent("connect", f"Gerando {len(candidates)} nota(s).", total_items=len(candidates)))
            return {"notes": run_connect(cfg, db, idx, candidates, observer=progress)}
        if operation == "garden":
            if payload.get("hubs"):
                from zettel.gardener_hub import run_garden_hubs
                mocs = run_garden_hubs(cfg, db, idx, observer=progress)
            else:
                from zettel.gardener import run_garden
                mocs = run_garden(cfg, db, idx, observer=progress)
            return {"mocs": mocs}
        if operation == "sync":
            from zettel.sync import run_sync_manual
            progress.emit(ProgressEvent("sync", "Sincronizando notas manuais."))
            return run_sync_manual(cfg, db, idx)
        raise ValueError("Operação web desconhecida")


class WebApplication:
    """Facade used by HTTP handlers; keeps all DB access on the server."""

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = config_path
        self.worker = WebWorker(config_path)

    @property
    def cfg(self) -> AppConfig:
        return load_config(self.config_path)

    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.worker.stop()

    def db(self) -> StateDB:
        return StateDB(self.cfg.state_db_path)

    def submit(self, operation: str, payload: dict[str, Any]) -> str | None:
        return self.worker.submit(operation, payload)

    def dashboard(self) -> dict[str, Any]:
        db = self.db()
        try:
            return db.get_web_dashboard()
        finally:
            db.close()

    def jobs(self) -> list[dict]:
        db = self.db()
        try:
            return db.list_web_jobs()
        finally:
            db.close()

    def job(self, job_id: str) -> dict | None:
        db = self.db()
        try:
            return db.get_web_job(job_id)
        finally:
            db.close()

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        db = self.db()
        try:
            return db.list_web_job_events(job_id, after)
        finally:
            db.close()