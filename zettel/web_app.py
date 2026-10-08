"""Application layer for the server-rendered Zettelkasten web UI.

This module deliberately contains no HTTP concerns.  It owns the durable,
single-worker queue and calls the existing pipeline entry points with their
normal StateDB/VectorIndex dependencies.
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from zettel.config import DEFAULT_RELATION_WEIGHTS, AppConfig, load_config
from zettel.llm import LLMUnavailableError
from zettel.state import StateDB

logger = logging.getLogger(__name__)


class UserFacingError(RuntimeError):
    """Expected operational failure whose message is safe for the browser."""


class JobParked(Exception):
    """The worker should leave this job in ``awaiting_input`` and move on."""

    def __init__(self, phase: str, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.phase = phase
        self.message = message
        self.result = result


def safe_error(exc: BaseException) -> str:
    """Return a useful, non-sensitive message for a browser response."""
    text = str(exc).replace("\n", " ").strip()
    if isinstance(exc, (UserFacingError, LLMUnavailableError)):
        return text[:300]
    if not text:
        return "A operação falhou. Consulte os logs do servidor."
    # Never echo host paths, API keys or provider response bodies to the UI.
    sensitive = (
        "api_key",
        "api key",
        "secret",
        "password",
        "token",
        "/home/",
        "\\users\\",
    )
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
        self.emit(
            ProgressEvent(
                phase=phase,
                message=message,
                current_item=current_item,
                current_index=current_index,
                total_items=total_items,
            )
        )


def _force_extract_review(
    db: StateDB, progress: JobProgress, payload: dict[str, Any]
) -> dict[str, Any]:
    """Mark extract rejections for a forced Prompt 1 call. No vector index."""
    from zettel.review import (
        force_extract_rejected,
        pending_dedupe_concepts,
        requeueable_extract_ids,
    )
    from zettel.usage import begin_run, finish_pipeline_run

    requested = list(payload.get("chunk_ids") or [])
    eligible = requeueable_extract_ids(db, requested)
    stats = {
        "approved": 0,
        "rejected": 0,
        "skipped": len(requested) - len(eligible),
        "requeued": 0,
        "forced": 0,
    }
    review_run_id = db.start_run("review")
    begin_run(review_run_id)
    try:
        total = len(eligible)
        for number, chunk_id in enumerate(eligible, 1):
            progress.emit(
                ProgressEvent(
                    "review",
                    f"Marcando extração obrigatória {number}/{total}.",
                    current_item=chunk_id[-18:],
                    current_index=number,
                    total_items=total,
                )
            )
        stats["forced"] = force_extract_rejected(db, eligible)
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
    except Exception:
        finish_pipeline_run(db, review_run_id, status="failed")
        raise
    finish_pipeline_run(db, review_run_id)
    return stats


def _requeue_extract_review(
    db: StateDB, progress: JobProgress, payload: dict[str, Any]
) -> dict[str, Any]:
    """Send extract rejections back to pending. Does not open the vector index."""
    from zettel.review import (
        pending_dedupe_concepts,
        requeue_extract_rejected,
        requeueable_extract_ids,
    )
    from zettel.usage import begin_run, finish_pipeline_run

    requested = list(payload.get("chunk_ids") or [])
    eligible = requeueable_extract_ids(db, requested)
    stats = {
        "approved": 0,
        "rejected": 0,
        "skipped": len(requested) - len(eligible),
        "requeued": 0,
    }
    review_run_id = db.start_run("review")
    begin_run(review_run_id)
    try:
        total = len(eligible)
        for number, chunk_id in enumerate(eligible, 1):
            progress.emit(
                ProgressEvent(
                    "review",
                    f"Reenfileirando item {number}/{total}.",
                    current_item=chunk_id[-18:],
                    current_index=number,
                    total_items=total,
                )
            )
        stats["requeued"] = requeue_extract_rejected(db, eligible)
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
    except Exception:
        finish_pipeline_run(db, review_run_id, status="failed")
        raise
    finish_pipeline_run(db, review_run_id)
    return stats


def _discard_extract_review(
    cfg: AppConfig, db: StateDB, progress: JobProgress, payload: dict[str, Any]
) -> dict[str, Any]:
    """Delete extract rejections from SQLite and Chroma. Drafts stay put."""
    from zettel.index import VectorIndex, index_kwargs
    from zettel.review import (
        discard_extract_rejected,
        pending_dedupe_concepts,
        requeueable_extract_ids,
    )
    from zettel.usage import begin_run, finish_pipeline_run

    requested = list(payload.get("chunk_ids") or [])
    eligible = requeueable_extract_ids(db, requested)
    stats = {
        "approved": 0,
        "rejected": 0,
        "skipped": len(requested) - len(eligible),
        "requeued": 0,
        "discarded": 0,
    }
    review_run_id = db.start_run("review")
    begin_run(review_run_id)
    try:
        if eligible:
            idx = VectorIndex(**index_kwargs(cfg))
            total = len(eligible)
            for number, chunk_id in enumerate(eligible, 1):
                progress.emit(
                    ProgressEvent(
                        "review",
                        f"Apagando item {number}/{total}.",
                        current_item=chunk_id[-18:],
                        current_index=number,
                        total_items=total,
                    )
                )
            stats["discarded"] = discard_extract_rejected(db, idx, eligible)
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
    except Exception:
        finish_pipeline_run(db, review_run_id, status="failed")
        raise
    finish_pipeline_run(db, review_run_id)
    return stats


def _vault_rel(path: Path, vault: Path) -> str:
    try:
        return str(path.relative_to(vault))
    except ValueError:
        return str(path)


def _ask_candidate(src: Any) -> dict[str, Any]:
    return {
        "title": src.title or src.note_id,
        "note_id": src.note_id,
        "rrf_score": src.rrf_score,
        "vector_similarity": src.vector_similarity,
        "bm25_rank": src.bm25_rank,
        "hop": src.hop,
        "passed_floor": src.passed_floor,
        "floor_reason": src.floor_reason,
        "origin": src.origin,
    }


def _dispatch_ask(
    cfg: AppConfig, db: StateDB, idx: Any, progress: JobProgress, payload: dict
) -> dict:
    from zettel.ask import run_ask, save_ask_note

    progress.emit(ProgressEvent("ask", "Consultando o acervo."))
    result = run_ask(
        cfg,
        db,
        idx,
        payload["question"],
        topk=payload.get("topk"),
        use_graph=False if payload.get("no_graph") else None,
        mode=payload.get("mode") or None,
    )
    saved = None
    if payload.get("save"):
        saved = _vault_rel(
            save_ask_note(result, cfg.vault_path, vault_timezone=cfg.vault_timezone),
            cfg.vault_path,
        )
    body: dict[str, Any] = {
        "kind": "ask",
        "question": result.question,
        "answer": result.answer,
        "saved_path": saved,
        "llm_called": result.llm_called,
        "candidates": [_ask_candidate(src) for src in result.candidates],
    }
    if payload.get("show_context"):
        body["retrieval_params"] = result.retrieval_params
    return body


def _dispatch_summarize(
    cfg: AppConfig, db: StateDB, idx: Any, progress: JobProgress, payload: dict
) -> dict:
    from zettel.summarize import generate_summaries

    progress.emit(ProgressEvent("summarize", "Resumindo capítulos."))
    outcome = generate_summaries(cfg, db, idx, payload.get("source_id") or None)
    return {
        "kind": "summarize",
        "chapters_summarized": outcome.chapters_summarized,
        "chapters_skipped": outcome.chapters_skipped,
        "sources_summarized": outcome.sources_summarized,
        "llm_calls": outcome.llm_calls,
        "cache_hits": outcome.cache_hits,
        "skipped": list(outcome.skipped),
        "source_ids": list(outcome.source_ids),
    }


def _dispatch_skill(cfg: AppConfig, db: StateDB, progress: JobProgress, payload: dict) -> dict:
    from zettel.skill_export import SkillExportError, estimate_tokens, run_skill_export

    progress.emit(ProgressEvent("skill", "Exportando skill."))
    try:
        pack_dir, pack = run_skill_export(
            cfg,
            db,
            source_id=payload.get("source_id") or None,
            moc_id=payload.get("moc_id") or None,
            topic=payload.get("topic") or None,
            slug=payload.get("slug") or None,
            overwrite=bool(payload.get("overwrite")),
            include_excerpts=bool(payload.get("include_excerpts")),
        )
    except SkillExportError as exc:
        raise UserFacingError(str(exc)) from exc
    skill_md = pack_dir / "SKILL.md"
    tokens = estimate_tokens(skill_md.read_text(encoding="utf-8")) if skill_md.is_file() else 0
    return {
        "kind": "skill",
        "path": _vault_rel(pack_dir, cfg.vault_path),
        "slug": pack.slug,
        "notes": len(pack.notes),
        "contradictions": len(pack.contradictions),
        "tokens": tokens,
        "include_excerpts": pack.include_excerpts,
    }


def _pause_result(payload: dict) -> tuple[str, str, dict[str, Any]]:
    kind = str(payload.get("type") or "context_review")
    if kind == "outline_review":
        message = "Aguardando revisão do outline."
        result = {
            "kind": "article_pause",
            "interrupt_type": "outline_review",
            "preview": str(payload.get("preview") or ""),
        }
        return kind, message, result
    notes = []
    for note in payload.get("notes") or []:
        if not isinstance(note, dict):
            continue
        meta = note.get("metadata") or {}
        notes.append(
            {
                "title": str(note.get("title") or note.get("note_id") or ""),
                "note_id": str(note.get("note_id") or ""),
                "score": float(note.get("score") or 0),
                "hop": int(note.get("hop") or 0),
                "source_id": str(meta.get("source_id") or ""),
            }
        )
    message = "Aguardando revisão do contexto."
    result = {
        "kind": "article_pause",
        "interrupt_type": "context_review",
        "notes": notes,
        "executed_queries": [str(q) for q in (payload.get("executed_queries") or [])],
    }
    return "context_review", message, result


def _article_result(result: Any, cfg: AppConfig, payload: dict) -> dict[str, Any]:
    from zettel.article import save_article_note

    saved = None
    outline_only = bool(payload.get("outline_only"))
    if payload.get("save") and not result.aborted and not result.no_evidence and not outline_only:
        saved = _vault_rel(
            save_article_note(result, cfg.vault_path, vault_timezone=cfg.vault_timezone),
            cfg.vault_path,
        )
    return {
        "kind": "article",
        "title": result.title,
        "body": result.body,
        "warnings": list(result.warnings),
        "aborted": bool(result.aborted),
        "no_evidence": bool(result.no_evidence),
        "outline_only": outline_only,
        "saved_path": saved,
    }


def _release_article(drive: Any, *, failed: bool) -> None:
    try:
        if failed:
            drive.abandon()
    finally:
        db = getattr(drive, "db", None)
        if db is not None:
            db.close()


def _dispatch_article(
    cfg: AppConfig,
    progress: JobProgress,
    payload: dict,
    sessions: dict[str, Any],
    job_id: str,
) -> dict[str, Any]:
    from zettel.article_graph.graph import ArticleDrive
    from zettel.index import VectorIndex, index_kwargs

    resume = payload.get("resume")
    drive = sessions.get(job_id)
    if resume:
        if drive is None:
            raise UserFacingError("A pausa do artigo expirou. Gere o artigo novamente.")
        progress.emit(ProgressEvent("article", "Retomando o artigo."))
        try:
            step = drive.resume(resume)
        except Exception:
            sessions.pop(job_id, None)
            _release_article(drive, failed=True)
            raise
    else:
        if drive is not None:
            sessions.pop(job_id, None)
            _release_article(drive, failed=True)
        owned = StateDB(cfg.state_db_path)
        try:
            idx = VectorIndex(**index_kwargs(cfg))
        except Exception:
            owned.close()
            raise
        review_context = bool(payload.get("review_context")) and not payload.get("outline_only")
        review_outline = bool(payload.get("review_outline")) and not payload.get("outline_only")

        def _approve(_outline: Any) -> tuple[str, None]:
            return ("approve", None)

        drive = ArticleDrive(
            cfg,
            owned,
            idx,
            payload["topic"],
            style=payload.get("style") or "blog",
            topk=payload.get("topk"),
            use_graph=False if payload.get("no_graph") else None,
            mode=payload.get("mode") or None,
            outline_only=bool(payload.get("outline_only")),
            approve_outline=None if review_outline else _approve,
            personality=payload.get("personality") or None,
            custom_style_notes=payload.get("style_notes") or None,
            skip_context_review=not review_context,
            skip_judge=bool(payload.get("skip_judge")),
            max_judge_iterations=payload.get("max_judge_iterations"),
            pause_for_review=review_context or review_outline,
        )
        sessions[job_id] = drive
        progress.emit(ProgressEvent("article", "Gerando o artigo."))
        try:
            step = drive.start()
        except Exception:
            sessions.pop(job_id, None)
            _release_article(drive, failed=True)
            raise
    if step.interrupt is not None:
        phase, message, result = _pause_result(step.interrupt)
        raise JobParked(phase, message, result)
    sessions.pop(job_id, None)
    try:
        return _article_result(step.result, cfg, payload)
    finally:
        _release_article(drive, failed=False)


class WebWorker:
    """A durable queue backed by SQLite and one process-local worker thread."""

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = config_path
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._articles: dict[str, Any] = {}

    def _db(self) -> StateDB:
        return StateDB(load_config(self.config_path).state_db_path)

    def start(self) -> None:
        db = self._db()
        try:
            recovered = db.recover_web_jobs()
            if recovered:
                logger.warning("Web: %d trabalho(s) marcados como interrupted", recovered)
            from zettel.harvester.prepared import (
                discard_orphan_preparations,
                remove_preparation,
            )

            for review_id in db.discard_unavailable_web_harvest_reviews():
                try:
                    remove_preparation(load_config(self.config_path), review_id)
                except OSError:
                    logger.warning("Não foi possível limpar a preparação %s", review_id)
            discard_orphan_preparations(load_config(self.config_path), db)
        finally:
            db.close()
        self._thread = threading.Thread(target=self._run, name="zettel-web-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        for job_id, drive in list(self._articles.items()):
            self._articles.pop(job_id, None)
            try:
                _release_article(drive, failed=True)
            except Exception:
                logger.warning("Não foi possível encerrar o artigo pausado %s", job_id)

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

    def submit_review(
        self, review_id: str, session_hash: str, payload: dict[str, Any]
    ) -> str | None:
        job_id = uuid4().hex
        db = self._db()
        try:
            queued = db.queue_web_harvest_review(review_id, session_hash, job_id, payload)
        finally:
            db.close()
        if not queued:
            return None
        self._wake.set()
        return job_id

    def _run(self) -> None:
        while not self._stop.is_set():
            db = self._db()
            try:
                job = db.next_queued_web_job()
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
            result = self._dispatch(
                cfg,
                db,
                progress,
                operation,
                payload,
                sessions=self._articles,
                job_id=job_id,
            )
            last_run = db.get_last_run()
            run_id = (
                last_run["run_id"] if last_run and last_run["run_id"] != previous_run_id else None
            )
            db.update_web_job(
                job_id,
                state="succeeded",
                phase="completed",
                message="Operação concluída.",
                result=result or {},
                run_id=run_id,
                finished=True,
            )
            db.add_web_job_event(job_id, "completed", message="Operação concluída.")
        except JobParked as parked:
            db.update_web_job(
                job_id,
                state="awaiting_input",
                phase=parked.phase,
                message=parked.message,
                result=parked.result,
            )
            db.add_web_job_event(job_id, parked.phase, message=parked.message)
        except (UserFacingError, LLMUnavailableError) as exc:
            logger.warning("Trabalho web %s falhou: %s", job_id, exc)
            message = safe_error(exc)
            db.update_web_job(
                job_id,
                state="failed",
                phase="failed",
                message=message,
                error_message=message,
                finished=True,
            )
            db.add_web_job_event(job_id, "failed", message=message)
        except Exception as exc:  # worker must survive a failed job
            logger.error("Trabalho web %s falhou: %s\n%s", job_id, exc, traceback.format_exc())
            message = safe_error(exc)
            db.update_web_job(
                job_id,
                state="failed",
                phase="failed",
                message=message,
                error_message=message,
                finished=True,
            )
            db.add_web_job_event(job_id, "failed", message=message)
        finally:
            succeeded = (db.get_web_job(job_id) or {}).get("state") == "succeeded"
            from zettel.harvester.prepared import remove_preparation

            try:
                for unusable_id in db.discard_unavailable_web_harvest_reviews():
                    try:
                        remove_preparation(cfg, unusable_id)
                    except OSError:
                        logger.warning("Não foi possível limpar a preparação %s", unusable_id)
            finally:
                db.close()
            if succeeded and operation == "harvest" and payload.get("review_id"):
                try:
                    remove_preparation(cfg, payload["review_id"])
                except OSError:
                    logger.warning("Não foi possível limpar a preparação %s", payload["review_id"])

    @staticmethod
    def _dispatch(
        cfg: AppConfig,
        db: StateDB,
        progress: JobProgress,
        operation: str,
        payload: dict[str, Any],
        *,
        sessions: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        progress.emit(ProgressEvent(operation, f"Carregando dependências para {operation}."))
        if operation == "retry_chunks":
            failed = db.get_chunks_by_status("failed", payload.get("source_id"))
            for chunk in failed:
                db.update_chunk_status(chunk["chunk_id"], "pending")
            return {"chunks_reset": len(failed)}
        if operation == "retry_assets":
            return {"assets_reset": db.reset_failed_assets()}
        if operation == "manual-ztl-from-lit" and not payload.get("use_llm"):
            from zettel.manual_lit import create_permanent_from_literature

            ref = payload.get("ref") or payload["chunk_id"]
            path, used_llm = create_permanent_from_literature(
                cfg,
                db,
                None,
                ref,
                thesis=payload.get("thesis") or None,
                use_llm=False,
                force=bool(payload.get("force")),
            )
            return {"path": str(path), "used_llm": used_llm}

        if operation == "harvest" and not payload.get("selected_file"):
            raise UserFacingError("Selecione um documento pendente antes de processar.")

        if operation == "prepare_harvest":
            from zettel.bibliography import build_bibliographic_metadata, format_abnt
            from zettel.harvester import extract
            from zettel.harvester.prepared import asset_stage_path, save_snapshot
            from zettel.hashing import file_sha256

            file_path = Path(payload["selected_file"]).resolve()
            if (
                not file_path.is_file()
                or file_path.suffix.lower() not in {".pdf", ".md", ".markdown", ".txt"}
                or not file_path.is_relative_to(cfg.inbox_path.resolve())
                or file_sha256(file_path) != payload["checksum"]
            ):
                raise UserFacingError(
                    "O arquivo mudou ou não está mais no inbox. Selecione-o novamente."
                )
            progress.emit(
                ProgressEvent(
                    "preparing",
                    "Extraindo texto e inferindo bibliografia.",
                    current_item=file_path.name,
                )
            )
            text, metadata = extract.extract_text(
                cfg.model_copy(update={"vault_path": asset_stage_path(cfg, progress.job_id)}),
                file_path,
                "pdf" if file_path.suffix.lower() == ".pdf" else "md",
            )
            biblio = build_bibliographic_metadata(cfg, db, metadata, text, file_path.name)
            if file_sha256(file_path) != payload["checksum"]:
                raise UserFacingError("O arquivo mudou durante a preparação. Tente novamente.")
            save_snapshot(
                cfg, progress.job_id, checksum=payload["checksum"], text=text, metadata=metadata
            )
            db.create_web_harvest_review(
                progress.job_id, payload["session_hash"], payload["checksum"]
            )
            return {
                "selected_file": str(file_path),
                "checksum": payload["checksum"],
                "biblio": biblio.model_dump(),
                "abnt_reference": format_abnt(biblio),
            }

        if operation == "review" and payload.get("action") == "requeue":
            # Status flip only: opening the vector index would fail the job when
            # the embedding credential is absent, and this action never reads it.
            return _requeue_extract_review(db, progress, payload)
        if operation == "review" and payload.get("action") == "force":
            return _force_extract_review(db, progress, payload)
        if operation == "review" and payload.get("action") == "discard":
            return _discard_extract_review(cfg, db, progress, payload)

        if operation == "skill":
            return _dispatch_skill(cfg, db, progress, payload)
        if operation == "article":
            return _dispatch_article(
                cfg, progress, payload, sessions if sessions is not None else {}, job_id or ""
            )

        from zettel.index import VectorIndex, index_kwargs

        idx = VectorIndex(**index_kwargs(cfg))
        if operation == "ask":
            return _dispatch_ask(cfg, db, idx, progress, payload)
        if operation == "summarize":
            return _dispatch_summarize(cfg, db, idx, progress, payload)
        if operation == "run_all":
            from zettel.connector import load_approved_candidates, run_connect
            from zettel.extractor import run_extract
            from zettel.gardener import run_garden
            from zettel.harvester import run_harvest
            from zettel.review import run_review

            progress.emit(ProgressEvent("harvest", "Fase 1/5 — iniciando harvest."))
            harvest = run_harvest(
                cfg,
                db,
                idx,
                interactive=False,
                duplicate_action=payload.get("duplicate_action", "skip"),
                skip_biblio=bool(payload.get("skip_biblio", False)),
                skip_paging=bool(payload.get("skip_paging", False)),
                observer=progress,
            )
            sources = harvest.source_ids

            progress.emit(ProgressEvent("extract", "Fase 2/5 — iniciando extract."))
            drafts = run_extract(cfg, db, idx, auto_approve=False, observer=progress)

            progress.emit(ProgressEvent("review", "Fase 3/5 — aprovando drafts elegíveis."))
            review_stats = run_review(
                cfg,
                db,
                idx,
                auto_approve=True,
                interactive=False,
            )

            approved = load_approved_candidates(db)
            progress.emit(
                ProgressEvent(
                    "connect",
                    f"Fase 4/5 — gerando {len(approved)} nota(s).",
                    total_items=len(approved),
                )
            )
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
            from zettel.harvester.prepared import load_snapshot
            from zettel.hashing import file_sha256

            file_path = Path(payload["selected_file"]).resolve()
            prepared = None
            review_id = payload.get("review_id")
            if review_id:
                review = db.get_web_harvest_review(review_id, payload["session_hash"])
                preparation = db.get_web_job(review_id)
                if (
                    not review
                    or review["state"] != "submitted"
                    or review["harvest_job_id"] != progress.job_id
                    or not preparation
                    or preparation["state"] != "succeeded"
                    or preparation["result"]["selected_file"] != str(file_path)
                    or not file_path.is_file()
                    or not file_path.is_relative_to(cfg.inbox_path.resolve())
                    or file_sha256(file_path) != review["file_checksum"]
                ):
                    raise UserFacingError(
                        "O arquivo mudou ou a revisão expirou. Prepare-o novamente."
                    )
                try:
                    prepared = load_snapshot(cfg, review_id)
                except ValueError as exc:
                    raise UserFacingError(str(exc)) from exc
                if prepared["checksum"] != review["file_checksum"]:
                    raise UserFacingError(
                        "A preparação não corresponde ao arquivo. Prepare-o novamente."
                    )
                prepared["biblio"] = payload["biblio"]
                prepared["abnt_reference"] = payload["abnt_reference"]
                prepared["review_id"] = review_id
            progress.emit(
                ProgressEvent(
                    "harvest",
                    "Processando documento.",
                    current_item=file_path.name,
                )
            )
            harvest = run_harvest(
                cfg,
                db,
                idx,
                interactive=False,
                duplicate_action=payload.get("duplicate_action", "skip"),
                skip_biblio=bool(payload.get("skip_biblio", False)),
                content_start_file=payload.get("content_start_file"),
                content_start_book=payload.get("content_start_book"),
                skip_paging=bool(payload.get("skip_paging", False)),
                selected_file=file_path,
                dump_dir=Path(payload["dump_dir"]) if payload.get("dump_dir") else None,
                extraction_dump_dir=Path(payload["extraction_dump_dir"])
                if payload.get("extraction_dump_dir")
                else None,
                observer=progress,
                prepared=prepared,
            )
            sources = harvest.source_ids
            if harvest.skipped:
                raise UserFacingError(harvest.skipped[0].message)
            if not sources:
                existing = db.get_file(str(file_path))
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
            return {
                "sources": sources,
                "chunk_dump_dir": payload.get("dump_dir"),
                "extraction_dump_dir": payload.get("extraction_dump_dir"),
            }
        if operation == "manual-ztl-from-lit":
            from zettel.connector import ConnectRejected
            from zettel.manual_lit import create_permanent_from_literature

            ref = payload.get("ref") or payload["chunk_id"]
            try:
                result = create_permanent_from_literature(
                    cfg,
                    db,
                    idx,
                    ref,
                    thesis=payload.get("thesis") or None,
                    use_llm=bool(payload.get("use_llm")),
                    force=bool(payload.get("force")),
                )
            except ConnectRejected as exc:
                raise UserFacingError(str(exc)) from exc
            path, used_llm = result
            return {"path": str(path), "used_llm": used_llm}
        if operation == "extract":
            from zettel.extractor import run_extract

            total = len(db.get_chunks_by_status("pending"))
            progress.emit(
                ProgressEvent("extract", f"Extraindo {total} chunk(s).", total_items=total)
            )
            candidates = run_extract(cfg, db, idx, auto_approve=False, observer=progress)
            return {"drafts": len(candidates), "auto_approved": False}
        if operation == "review":
            from zettel.review import (
                approve_chunk,
                finalize_approved_concepts,
                pending_dedupe_concepts,
                reject_chunk,
            )
            from zettel.usage import begin_run, finish_pipeline_run

            action = payload.get("action")
            chunk_ids = list(payload.get("chunk_ids") or [])
            if not chunk_ids and payload.get("confidence_below") is not None:
                chunks = db.get_chunks_by_status("awaiting_review")
                threshold = float(payload["confidence_below"])
                chunk_ids = [
                    c["chunk_id"] for c in chunks if (c.get("review_confidence") or 0) < threshold
                ]
            stats = {"approved": 0, "rejected": 0, "skipped": 0}
            total = len(chunk_ids)
            review_run_id = db.start_run("review")
            begin_run(review_run_id)
            try:
                for number, chunk_id in enumerate(chunk_ids, 1):
                    progress.emit(
                        ProgressEvent(
                            "review",
                            f"Revisando item {number}/{total}.",
                            current_item=chunk_id[-18:],
                            current_index=number,
                            total_items=total,
                        )
                    )
                    ok = (
                        approve_chunk(cfg, db, idx, chunk_id)
                        if action == "approve"
                        else reject_chunk(cfg, db, idx, chunk_id)
                    )
                    stats["approved" if action == "approve" else "rejected"] += int(ok)
                    stats["skipped"] += int(not ok)
                if action == "approve" and stats["approved"]:
                    finalize_approved_concepts(cfg, db, idx)
                # Possible duplicates are never dropped here: they wait for `zettel review`.
                stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
            except Exception:
                finish_pipeline_run(db, review_run_id, status="failed")
                raise
            finish_pipeline_run(db, review_run_id)
            return stats
        if operation == "connect":
            from zettel.connector import load_approved_candidates, run_connect

            candidates = load_approved_candidates(db)
            progress.emit(
                ProgressEvent(
                    "connect",
                    f"Gerando {len(candidates)} nota(s).",
                    total_items=len(candidates),
                )
            )
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

    def submit_review(
        self, review_id: str, session_hash: str, payload: dict[str, Any]
    ) -> str | None:
        return self.worker.submit_review(review_id, session_hash, payload)

    def resume_article(self, job_id: str, decision: dict[str, Any]) -> str | None:
        """Re-queue a parked article. ``None`` means another job holds the slot.

        Raises ``UserFacingError`` when the in-memory graph is gone or the job
        is not waiting.
        """
        if job_id not in self.worker._articles:
            db = self.db()
            try:
                job = db.get_web_job(job_id)
                if job and job["state"] == "awaiting_input":
                    db.update_web_job(
                        job_id,
                        state="interrupted",
                        phase="interrupted",
                        message="A pausa do artigo expirou.",
                        finished=True,
                    )
            finally:
                db.close()
            raise UserFacingError("A pausa do artigo expirou. Gere o artigo novamente.")
        db = self.db()
        try:
            status = db.requeue_parked_job(job_id, {"resume": decision})
        finally:
            db.close()
        if status == "busy":
            return None
        if status != "ok":
            raise UserFacingError("Esta execução não está aguardando uma decisão.")
        self.worker._wake.set()
        return job_id

    def dashboard(self) -> dict[str, Any]:
        db = self.db()
        try:
            from zettel.review import LOW_CONFIDENCE_MAX

            return db.get_web_dashboard(
                low_max=LOW_CONFIDENCE_MAX,
                limiar=self.cfg.literature_review.auto_approve_min_confidence,
                relation_weights=DEFAULT_RELATION_WEIGHTS,
            )
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
