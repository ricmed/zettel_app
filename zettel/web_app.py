"""Application layer for the server-rendered Zettelkasten web UI.

This module deliberately contains no HTTP concerns.  It owns the durable,
single-worker queue and calls the existing pipeline entry points with their
normal StateDB/VectorIndex dependencies.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from zettel.config import AppConfig, load_config
from zettel.llm import LLMUnavailableError
from zettel.state import StateDB
from zettel.state.runs import current_web_job

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


# A Windows drive path, or a POSIX path with two or more segments.
_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"()]*|(?<![\w:/.])/(?:[^\s/'\"()]+/)+[^\s'\"()]*")


# Exceptions whose message this codebase writes for a person to read, by
# qualified name: matching names, not importing the classes, keeps an import
# failure in the job from failing again here (and chromadb out of this path).
KNOWN_ERRORS = frozenset(
    {
        "zettel.web_app.UserFacingError",
        "zettel.llm.LLMUnavailableError",
        "zettel.oplock.LockBusy",
        "zettel.harvester.duplicates.HarvestAborted",
        "zettel.harvester.extract.PdfExtractionError",
        "zettel.gardener.MocAdoptError",
        "zettel.skill_export.SkillExportError",
        "zettel.connector.prompt.ConnectRejected",
        "zettel.index.EmbeddingSpaceMismatch",
    }
)


def _is_known(exc: BaseException) -> bool:
    return any(f"{cls.__module__}.{cls.__qualname__}" in KNOWN_ERRORS for cls in type(exc).__mro__)


def safe_error(exc: BaseException, *, ref: str | None = None) -> str:
    """The message a browser may see for ``exc`` (#315).

    An allowlist: only the exceptions in ``KNOWN_ERRORS`` (and their subclasses)
    show their text, and even then without absolute paths, since some
    interpolate a file or a library error. Anything else becomes a generic
    message naming ``ref``, the correlation id the caller also writes to the log.
    """
    if _is_known(exc):
        text = _ABSOLUTE_PATH.sub("<caminho>", str(exc).replace("\n", " ").strip())
        if text:
            return text[:300]
    suffix = f" (ref. {ref})" if ref else ""
    return f"A operação falhou{suffix}. Consulte os logs do servidor."


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


@dataclass
class JobContext:
    """What a worker operation receives (#318). ``idx`` opens on first use:
    an operation that never searches never opens Chroma (nor needs its key)."""

    cfg: AppConfig
    db: StateDB
    progress: JobProgress
    payload: dict[str, Any]
    sessions: dict[str, Any]
    job_id: str
    _idx: Any = None

    @property
    def idx(self) -> Any:
        if self._idx is None:
            from zettel.index import VectorIndex, index_kwargs

            self._idx = VectorIndex(**index_kwargs(self.cfg))
        return self._idx


Handler = Callable[[JobContext], dict[str, Any]]


def _op_review_force(ctx: JobContext) -> dict[str, Any]:
    """Mark extract rejections for a forced Prompt 1 call. No vector index."""
    db, progress, payload = ctx.db, ctx.progress, ctx.payload
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
        stats["forced"] = force_extract_rejected(
            db, eligible, mode="web_batch", surface="web", run_id=review_run_id, observer=progress
        )
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
    except Exception:
        finish_pipeline_run(db, review_run_id, status="failed")
        raise
    finish_pipeline_run(db, review_run_id)
    return stats


def _op_review_duplicates(ctx: JobContext) -> dict[str, Any]:
    """Keep or discard concepts the dedupe flagged. SQLite only: no vector index."""
    db, progress, payload = ctx.db, ctx.progress, ctx.payload
    from zettel.review import discard_duplicate, keep_duplicate, pending_dedupe_concepts
    from zettel.usage import begin_run, finish_pipeline_run

    action = payload.get("action")
    decide = keep_duplicate if action == "keep_duplicate" else discard_duplicate
    requested = list(payload.get("concept_ids") or [])
    stats = {"kept": 0, "discarded": 0, "skipped": 0}
    review_run_id = db.start_run("review")
    begin_run(review_run_id)
    try:
        total = len(requested)
        for number, concept_id in enumerate(requested, 1):
            concept = db.get_concept(concept_id)
            if not concept or concept.get("status") != "dedupe_pending":
                stats["skipped"] += 1
            else:
                decide(
                    db,
                    concept_id,
                    mode="web_batch",
                    surface="web",
                    run_id=review_run_id,
                )
                stats["kept" if action == "keep_duplicate" else "discarded"] += 1
            # Reported once decided, never before (#313).
            progress.emit(
                ProgressEvent(
                    "review",
                    f"Duplicata {number}/{total}.",
                    current_item=str(concept_id)[-18:],
                    current_index=number,
                    total_items=total,
                )
            )
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
    except Exception:
        finish_pipeline_run(db, review_run_id, status="failed")
        raise
    finish_pipeline_run(db, review_run_id)
    return stats


def _op_review_requeue(ctx: JobContext) -> dict[str, Any]:
    """Send extract rejections back to pending. Does not open the vector index."""
    db, progress, payload = ctx.db, ctx.progress, ctx.payload
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
        stats["requeued"] = requeue_extract_rejected(
            db, eligible, mode="web_batch", surface="web", run_id=review_run_id, observer=progress
        )
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
    except Exception:
        finish_pipeline_run(db, review_run_id, status="failed")
        raise
    finish_pipeline_run(db, review_run_id)
    return stats


def _op_review_discard(ctx: JobContext) -> dict[str, Any]:
    """Delete extract rejections from SQLite and Chroma. Drafts stay put."""
    db, progress, payload = ctx.db, ctx.progress, ctx.payload
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
            stats["discarded"] = discard_extract_rejected(
                db,
                ctx.idx,
                eligible,
                mode="web_batch",
                surface="web",
                run_id=review_run_id,
                observer=progress,
            )
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
    except Exception:
        finish_pipeline_run(db, review_run_id, status="failed")
        raise
    finish_pipeline_run(db, review_run_id)
    return stats


def _op_review_links(ctx: JobContext) -> dict[str, Any]:
    """Accept or reject a batch of proposed edges (#309). SQLite only."""
    cfg, db, progress, payload = ctx.cfg, ctx.db, ctx.progress, ctx.payload
    from zettel.connector.links import decide_link

    action = payload.get("action")
    edges = [tuple(edge) for edge in payload.get("edges") or []]
    stats = {"decided": 0, "skipped": 0}
    total = len(edges)
    for number, (source_id, target_id, relation) in enumerate(edges, 1):
        decided = decide_link(cfg, db, source_id, target_id, relation, action, surface="web")
        stats["decided" if decided else "skipped"] += 1
        progress.emit(
            ProgressEvent(
                "review",
                f"Aresta {number}/{total}.",
                current_item=f"{source_id[-8:]} {relation} {target_id[-8:]}",
                current_index=number,
                total_items=total,
            )
        )
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


def _op_ask(ctx: JobContext) -> dict:
    cfg, db, progress, payload, idx = ctx.cfg, ctx.db, ctx.progress, ctx.payload, ctx.idx
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


def _op_summarize(ctx: JobContext) -> dict:
    cfg, db, progress, payload, idx = ctx.cfg, ctx.db, ctx.progress, ctx.payload, ctx.idx
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


def _op_skill(ctx: JobContext) -> dict:
    cfg, db, progress, payload = ctx.cfg, ctx.db, ctx.progress, ctx.payload
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


def _op_article(ctx: JobContext) -> dict[str, Any]:
    cfg, progress, payload = ctx.cfg, ctx.progress, ctx.payload
    sessions, job_id = ctx.sessions, ctx.job_id
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
            on_phase=lambda message: progress.emit(ProgressEvent("article", message)),
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


class ConfigCache:
    """``load_config`` once, and again only when the YAML's mtime changes (#307).

    Reading and validating config.yaml costs ~54 ms; it used to happen on every
    access to ``WebApplication.cfg`` and twice a second in the idle worker.
    Editing the file still takes effect on the next request.
    """

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = config_path
        self._lock = threading.Lock()
        self._stamp: int | None = None
        self._cfg: AppConfig | None = None

    def get(self) -> AppConfig:
        from zettel.config import config_file_path

        try:
            stamp = config_file_path(self.config_path).stat().st_mtime_ns
        except OSError:
            stamp = None
        with self._lock:
            if self._cfg is None or stamp != self._stamp:
                self._cfg = load_config(self.config_path)
                self._stamp = stamp
            return self._cfg


# The worker sleeps until a submit wakes it. The timeout only covers a row
# queued without that signal (none today); it is not a polling interval.
_IDLE_WAKE_SECONDS = 30.0
# How often a queued job checks again whether the CLI released the lock.
_LOCK_RETRY_SECONDS = 2.0


class WebWorker:
    """A durable queue backed by SQLite and one process-local worker thread."""

    def __init__(self, config: ConfigCache, *, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._articles: dict[str, Any] = {}
        # When each article parked, by the worker's clock (#317).
        self._parked_since: dict[str, float] = {}
        self._clock = clock

    def _db(self) -> StateDB:
        return StateDB(self.config.get().state_db_path)

    def start(self) -> None:
        db = self._db()
        try:
            recovered = db.recover_web_jobs()
            if recovered:
                logger.warning("Web: %d trabalho(s) marcados como interrupted", recovered)
            purged = db.purge_web_jobs(older_than_days=self.config.get().web.job_retention_days)
            if purged:
                logger.info("Web: %d trabalho(s) antigo(s) expurgado(s)", purged)
            from zettel.harvester.prepared import (
                discard_orphan_preparations,
                remove_preparation,
            )

            for review_id in db.discard_unavailable_web_harvest_reviews():
                try:
                    remove_preparation(self.config.get(), review_id)
                except OSError:
                    logger.warning("Não foi possível limpar a preparação %s", review_id)
            discard_orphan_preparations(self.config.get(), db)
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

    def submit_review(self, review_id: str, payload: dict[str, Any]) -> str | None:
        job_id = uuid4().hex
        db = self._db()
        try:
            queued = db.queue_web_harvest_review(review_id, job_id, payload)
        finally:
            db.close()
        if not queued:
            return None
        self._wake.set()
        return job_id

    def _run(self) -> None:
        from zettel.oplock import LockBusy, OperationLock, lock_path

        while not self._stop.is_set():
            # Clear before looking: a submit after the lookup still wakes the wait.
            self._wake.clear()
            self.expire_parked_articles()
            db = self._db()
            try:
                job = db.next_queued_web_job()
                if not job:
                    lock = None
                else:
                    # The same lock the CLI takes (#308): while a terminal command
                    # writes the stores, the job waits queued instead of failing.
                    lock = OperationLock(lock_path(self.config.get()))
                    try:
                        lock.acquire(job["operation"], "web")
                    except LockBusy as busy:
                        db.update_web_job(job["job_id"], message=f"Aguardando: {busy}")
                        lock = None
            finally:
                db.close()
            if not job:
                self._wake.wait(timeout=_IDLE_WAKE_SECONDS)
                continue
            if lock is None:
                self._wake.wait(timeout=_LOCK_RETRY_SECONDS)
                continue
            try:
                self._execute(job["job_id"])
            finally:
                lock.release()

    def expire_parked_articles(self) -> int:
        """Release articles parked longer than ``web.article_pause_ttl_hours`` (#317).

        The job becomes ``interrupted`` only if it is still waiting; then its
        graph is dropped and its SQLite connection closed. Returns the count.
        """
        ttl = self.config.get().web.article_pause_ttl_hours
        if not ttl or not self._parked_since:
            return 0
        now = self._clock()
        expired = [j for j, since in self._parked_since.items() if now - since >= ttl * 3600]
        if not expired:
            return 0
        released = 0
        db = self._db()
        try:
            for job_id in expired:
                self._parked_since.pop(job_id, None)
                message = f"A pausa do artigo expirou após {ttl:g} h sem decisão."
                if not db.expire_parked_job(job_id, message):
                    continue
                db.add_web_job_event(job_id, "interrupted", message=message)
                drive = self._articles.pop(job_id, None)
                if drive is not None:
                    _release_article(drive, failed=True)
                released += 1
        finally:
            db.close()
        return released

    def _execute(self, job_id: str) -> None:
        # A private copy: a long job never sees a config edited halfway through.
        cfg = self.config.get().model_copy(deep=True)
        db = StateDB(cfg.state_db_path)
        if not db.claim_web_job(job_id):
            db.close()
            return
        self._parked_since.pop(job_id, None)  # resumed: the clock starts again if it parks
        progress = JobProgress(db, job_id)
        job = db.get_web_job(job_id) or {}
        payload = job.get("payload") or {}
        operation = job.get("operation", "")
        # Every run this job opens is linked to it in start_run (#311).
        run_link = current_web_job.set(job_id)
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
            db.update_web_job(
                job_id,
                state="succeeded",
                phase="completed",
                message="Operação concluída.",
                result=result or {},
                finished=True,
            )
            db.add_web_job_event(job_id, "completed", message="Operação concluída.")
        except JobParked as parked:
            self._parked_since[job_id] = self._clock()
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
            ref = secrets.token_hex(4)
            logger.error(
                "Trabalho web %s falhou [ref %s]: %s\n%s",
                job_id,
                ref,
                exc,
                traceback.format_exc(),
            )
            message = safe_error(exc, ref=ref)
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
            current_web_job.reset(run_link)
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
        handler = OPERATIONS.get(operation)
        if handler is None:
            raise ValueError("Operação web desconhecida")
        progress.emit(ProgressEvent(operation, f"Carregando dependências para {operation}."))
        ctx = JobContext(
            cfg, db, progress, payload, sessions if sessions is not None else {}, job_id or ""
        )
        return handler(ctx)


def _op_retry_chunks(ctx: JobContext) -> dict[str, Any]:
    """Failed chunks back to pending, optionally for one source."""
    db, payload = ctx.db, ctx.payload
    failed = db.get_chunks_by_status("failed", payload.get("source_id"))
    for chunk in failed:
        db.update_chunk_status(chunk["chunk_id"], "pending")
    return {"chunks_reset": len(failed)}


def _op_retry_assets(ctx: JobContext) -> dict[str, Any]:
    """Failed image descriptions back to pending."""
    db = ctx.db
    return {"assets_reset": db.reset_failed_assets()}


def _op_prepare_harvest(ctx: JobContext) -> dict[str, Any]:
    """Extract one inbox file and infer its bibliography for the review page."""
    cfg, db, progress, payload = ctx.cfg, ctx.db, ctx.progress, ctx.payload
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
        raise UserFacingError("O arquivo mudou ou não está mais no inbox. Selecione-o novamente.")
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
    save_snapshot(cfg, progress.job_id, checksum=payload["checksum"], text=text, metadata=metadata)
    db.create_web_harvest_review(progress.job_id, payload["checksum"])
    return {
        "selected_file": str(file_path),
        "checksum": payload["checksum"],
        "biblio": biblio.model_dump(),
        "abnt_reference": format_abnt(biblio),
        "render_hidden": metadata.get("_render_hidden") or [],
    }


def _op_adopt_moc(ctx: JobContext) -> dict[str, Any]:
    """Hand a generated MOC over to the author (ADR-061)."""
    cfg, db, payload = ctx.cfg, ctx.db, ctx.payload
    from zettel.gardener import MocAdoptError, adopt_moc

    try:
        return {"kind": "adopt_moc", **adopt_moc(cfg, db, payload["moc_id"])}
    except MocAdoptError as exc:
        raise UserFacingError(str(exc)) from exc


def _op_run_all(ctx: JobContext) -> dict[str, Any]:
    """Every phase over the whole inbox, without the bibliographic review (#310)."""
    cfg, db, progress, payload, idx = ctx.cfg, ctx.db, ctx.progress, ctx.payload, ctx.idx
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

    auto_approve = bool(payload.get("auto_approve"))
    if auto_approve:
        from zettel.review import AUTO_APPROVE_UNVALIDATED_WARNING

        progress.emit(
            ProgressEvent(
                "review",
                "Fase 3/5 — aprovando drafts elegíveis. " + AUTO_APPROVE_UNVALIDATED_WARNING,
            )
        )
    else:
        progress.emit(
            ProgressEvent(
                "review",
                "Fase 3/5 — drafts ficam aguardando revisão.",
            )
        )
    review_stats = run_review(
        cfg,
        db,
        idx,
        source_ids=list(sources),
        auto_approve=auto_approve,
        interactive=False,
        event_mode="run_all" if auto_approve else None,
        surface="web",
    )

    approved = load_approved_candidates(db, source_ids=list(sources))
    progress.emit(
        ProgressEvent(
            "connect",
            f"Fase 4/5 — gerando {len(approved)} nota(s).",
            total_items=len(approved),
        )
    )
    outcome = run_connect(cfg, db, idx, approved, observer=progress)
    note_ids = outcome.note_ids

    progress.emit(ProgressEvent("garden", "Fase 5/5 — atualizando mapas de conteúdo."))
    moc_ids = run_garden(cfg, db, idx, observer=progress)
    garden_report = db.latest_garden_report("garden")
    return {
        "garden_report": garden_report,
        "sources": sources,
        "drafts": len(drafts),
        "review": review_stats,
        "pending_review": int(review_stats.get("pending_review") or 0),
        "approved_threshold": int(review_stats.get("approved_threshold") or 0),
        "notes": note_ids,
        "mocs": moc_ids,
        # run_all harvests the whole inbox without a bibliographic review,
        # so a refused file must show up here, with its reason (#310).
        "harvest_skipped": [
            {"file": skip.path.name, "reason": skip.reason, "message": skip.message}
            for skip in [*harvest.skipped, *harvest.declined]
        ],
    }


def _op_harvest(ctx: JobContext) -> dict[str, Any]:
    """One inbox file, confirmed by its review when it has one."""
    cfg, db, progress, payload = ctx.cfg, ctx.db, ctx.progress, ctx.payload
    if not payload.get("selected_file"):
        raise UserFacingError("Selecione um documento pendente antes de processar.")
    from zettel.harvester import run_harvest
    from zettel.harvester.prepared import load_snapshot
    from zettel.hashing import file_sha256

    file_path = Path(payload["selected_file"]).resolve()
    prepared = None
    review_id = payload.get("review_id")
    if review_id:
        review = db.get_web_harvest_review(review_id)
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
            raise UserFacingError("O arquivo mudou ou a revisão expirou. Prepare-o novamente.")
        try:
            prepared = load_snapshot(cfg, review_id)
        except ValueError as exc:
            raise UserFacingError(str(exc)) from exc
        if prepared["checksum"] != review["file_checksum"]:
            raise UserFacingError("A preparação não corresponde ao arquivo. Prepare-o novamente.")
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
        ctx.idx,
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
        # The harvester says why, naming the duplicate layer (#316).
        from zettel.harvester.pipeline import REUSED_REASONS

        declined = harvest.declined[0] if harvest.declined else None
        if declined and declined.reason in REUSED_REASONS:
            return {"sources": [declined.source_id], "skipped": declined.message}
        if declined:
            raise UserFacingError(declined.message)
        raise UserFacingError(
            "Nenhuma fonte foi criada e o harvest não registrou o motivo. "
            "Consulte os logs do servidor."
        )
    return {
        "sources": sources,
        "chunk_dump_dir": payload.get("dump_dir"),
        "extraction_dump_dir": payload.get("extraction_dump_dir"),
    }


def _op_manual_ztl_from_lit(ctx: JobContext) -> dict[str, Any]:
    """A ZTL from a granular LIT; the index only with the LLM."""
    cfg, db, payload = ctx.cfg, ctx.db, ctx.payload
    from zettel.connector import ConnectRejected
    from zettel.manual_lit import create_permanent_from_literature

    ref = payload.get("ref") or payload["chunk_id"]
    kwargs: dict[str, Any] = {
        "thesis": payload.get("thesis") or None,
        "use_llm": bool(payload.get("use_llm")),
        "force": bool(payload.get("force")),
    }
    raw_image_paths = payload.get("image_paths") or []
    raw_paths = (
        [p for p in raw_image_paths if isinstance(p, str)]
        if isinstance(raw_image_paths, list)
        else []
    )
    if raw_paths:
        from zettel.assets import canonical_asset_paths

        safe_paths = canonical_asset_paths(cfg.vault_path, raw_paths)
        if safe_paths:
            kwargs["images"] = [{"path": path} for path in safe_paths]
    try:
        result = create_permanent_from_literature(
            cfg,
            db,
            ctx.idx if payload.get("use_llm") else None,
            ref,
            **kwargs,
        )
    except ConnectRejected as exc:
        raise UserFacingError(str(exc)) from exc
    path, used_llm = result
    return {"path": str(path), "used_llm": used_llm}


def _op_extract(ctx: JobContext) -> dict[str, Any]:
    """Prompt 1 over every pending chunk; drafts wait for review."""
    cfg, db, progress, idx = ctx.cfg, ctx.db, ctx.progress, ctx.idx
    from zettel.extractor import run_extract

    total = len(db.get_chunks_by_status("pending"))
    progress.emit(ProgressEvent("extract", f"Extraindo {total} chunk(s).", total_items=total))
    candidates = run_extract(cfg, db, idx, auto_approve=False, observer=progress)
    return {"drafts": len(candidates), "auto_approved": False}


def _op_review_drafts(ctx: JobContext) -> dict[str, Any]:
    """Approve or reject drafts, or concepts of a draft."""
    cfg, db, progress, payload, idx = ctx.cfg, ctx.db, ctx.progress, ctx.payload, ctx.idx
    from zettel.review import (
        approve_chunk,
        finalize_approved_concepts,
        pending_dedupe_concepts,
        refresh_approved_literature,
        reject_chunk,
    )
    from zettel.usage import begin_run, finish_pipeline_run

    action = payload.get("action")
    chunk_ids = list(payload.get("chunk_ids") or [])
    concept_ids = list(payload.get("concept_ids") or [])
    if action in {"approve", "reject"} and concept_ids:
        from zettel.review import decide_concepts

        review_run_id = db.start_run("review")
        begin_run(review_run_id)
        try:
            outcome = decide_concepts(
                cfg,
                db,
                idx,
                dict.fromkeys(concept_ids, action),
                mode="web_batch",
                surface="web",
                run_id=review_run_id,
            )
            if outcome["approved"]:
                finalize_approved_concepts(cfg, db, idx)
            stats = {
                "approved": len(outcome["persisted"]),
                "concepts_approved": len(outcome["approved"]),
                "rejected": len(outcome["rejected_chunks"]),
                "concepts_rejected": len(outcome["rejected"]),
                "refused": outcome["refused"],
                "skipped": 0,
                "draft_delete_failures": len(outcome.get("draft_delete_failures") or []),
            }
        except Exception:
            finish_pipeline_run(db, review_run_id, status="failed")
            raise
        finish_pipeline_run(db, review_run_id)
        return stats
    if not chunk_ids and payload.get("confidence_below") is not None:
        chunks = db.get_chunks_by_status("awaiting_review")
        threshold = float(payload["confidence_below"])
        chunk_ids = [c["chunk_id"] for c in chunks if (c.get("review_confidence") or 0) < threshold]
    stats = {"approved": 0, "rejected": 0, "skipped": 0, "draft_delete_failures": 0}
    total = len(chunk_ids)
    review_run_id = db.start_run("review")
    begin_run(review_run_id)
    persisted: list[str] = []
    try:
        for number, chunk_id in enumerate(chunk_ids, 1):
            if action == "approve":
                ok = approve_chunk(
                    cfg,
                    db,
                    idx,
                    chunk_id,
                    mode="web_batch",
                    surface="web",
                    run_id=review_run_id,
                    refresh_source=False,
                )
                if ok:
                    persisted.append(chunk_id)
            else:
                ok = reject_chunk(
                    cfg,
                    db,
                    idx,
                    chunk_id,
                    mode="web_batch",
                    surface="web",
                    run_id=review_run_id,
                    stats=stats,
                )
            stats["approved" if action == "approve" else "rejected"] += int(ok)
            stats["skipped"] += int(not ok)
            progress.emit(
                ProgressEvent(
                    "review",
                    f"Revisado {number}/{total}.",
                    current_item=chunk_id[-18:],
                    current_index=number,
                    total_items=total,
                )
            )
        if action == "approve":
            refresh_approved_literature(cfg, db, persisted)
            if stats["approved"]:
                progress.emit(ProgressEvent("review", "Deduplicando os conceitos aprovados."))
                finalize_approved_concepts(cfg, db, idx)
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db))
    except Exception:
        finish_pipeline_run(db, review_run_id, status="failed")
        raise
    finish_pipeline_run(db, review_run_id)
    return stats


def _op_connect(ctx: JobContext) -> dict[str, Any]:
    """Prompt 2 over the approved concepts."""
    cfg, db, progress, idx = ctx.cfg, ctx.db, ctx.progress, ctx.idx
    from zettel.connector import load_approved_candidates, run_connect

    candidates = load_approved_candidates(db)
    progress.emit(
        ProgressEvent(
            "connect",
            f"Gerando {len(candidates)} nota(s).",
            total_items=len(candidates),
        )
    )
    return {"notes": run_connect(cfg, db, idx, candidates, observer=progress).note_ids}


def _op_garden(ctx: JobContext) -> dict[str, Any]:
    """Taxonomy MOCs, or hub MOCs with ``hubs`` (no vector index: #314)."""
    cfg, db, progress, payload = ctx.cfg, ctx.db, ctx.progress, ctx.payload
    if payload.get("hubs"):
        from zettel.gardener_hub import run_garden_hubs

        mocs = run_garden_hubs(cfg, db, observer=progress)
    else:
        from zettel.gardener import run_garden

        mocs = run_garden(cfg, db, ctx.idx, observer=progress)
    pipeline = "garden_hubs" if payload.get("hubs") else "garden"
    return {
        "kind": "garden",
        "mocs": mocs,
        "garden_report": db.latest_garden_report(pipeline),
    }


def _op_sync(ctx: JobContext) -> dict[str, Any]:
    """Index the vault's manual edits."""
    cfg, db, progress, idx = ctx.cfg, ctx.db, ctx.progress, ctx.idx
    from zettel.sync import run_sync_manual

    progress.emit(ProgressEvent("sync", "Sincronizando notas manuais."))
    return run_sync_manual(cfg, db, idx)


def _op_review(ctx: JobContext) -> dict[str, Any]:
    """Every review action shares the ``review`` operation; ``action`` picks the work."""
    return REVIEW_ACTIONS[ctx.payload.get("action")](ctx)


# Action -> handler. The batch actions that never read the vector index
# (requeue, force, duplicates) are SQLite only, so a missing embedding
# credential cannot fail them.
REVIEW_ACTIONS: dict[str, Handler] = {
    "approve": _op_review_drafts,
    "reject": _op_review_drafts,
    "requeue": _op_review_requeue,
    "force": _op_review_force,
    "discard": _op_review_discard,
    "keep_duplicate": _op_review_duplicates,
    "discard_duplicate": _op_review_duplicates,
}

# Every operation the worker runs. Routes submit only these names, and
# tests/test_web_package.py fails if one a route submits is missing.
OPERATIONS: dict[str, Handler] = {
    "prepare_harvest": _op_prepare_harvest,
    "harvest": _op_harvest,
    "run_all": _op_run_all,
    "extract": _op_extract,
    "review": _op_review,
    "review_links": _op_review_links,
    "connect": _op_connect,
    "garden": _op_garden,
    "sync": _op_sync,
    "retry_chunks": _op_retry_chunks,
    "retry_assets": _op_retry_assets,
    "manual-ztl-from-lit": _op_manual_ztl_from_lit,
    "adopt_moc": _op_adopt_moc,
    "ask": _op_ask,
    "summarize": _op_summarize,
    "skill": _op_skill,
    "article": _op_article,
}


class WebApplication:
    """Facade used by HTTP handlers; keeps all DB access on the server."""

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = config_path
        self._config = ConfigCache(config_path)
        self.worker = WebWorker(self._config)
        self._session_key: bytes | None = None
        self._session_key_lock = threading.Lock()

    def session_key(self) -> bytes:
        """The cookie-signing key from SQLite, read once per process."""
        with self._session_key_lock:
            if self._session_key is None:
                db = self.db()
                try:
                    self._session_key = bytes.fromhex(db.web_session_key())
                finally:
                    db.close()
            return self._session_key

    def rotate_session_key(self) -> None:
        """End every session: the next request needs a new login."""
        with self._session_key_lock:
            db = self.db()
            try:
                self._session_key = bytes.fromhex(db.rotate_web_session_key())
            finally:
                db.close()

    @property
    def cfg(self) -> AppConfig:
        """Shared and read-only: handlers must not mutate it."""
        return self._config.get()

    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.worker.stop()

    def db(self) -> StateDB:
        return StateDB(self.cfg.state_db_path)

    def submit(self, operation: str, payload: dict[str, Any]) -> str | None:
        return self.worker.submit(operation, payload)

    def submit_review(self, review_id: str, payload: dict[str, Any]) -> str | None:
        return self.worker.submit_review(review_id, payload)

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
                relation_weights=self.cfg.retrieval.graph_expansion.relation_weights,
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
        """The job with the runs it opened and their summed cost (#311)."""
        db = self.db()
        try:
            job = db.get_web_job(job_id)
            if job is not None:
                job["runs"] = db.list_web_job_runs(job_id)
                job["cost_usd_total"] = sum(
                    float(run.get("cost_usd_total") or 0) for run in job["runs"]
                )
            return job
        finally:
            db.close()

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        db = self.db()
        try:
            return db.list_web_job_events(job_id, after)
        finally:
            db.close()
