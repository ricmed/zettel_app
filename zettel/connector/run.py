"""Phase 3 entry points: which concepts are eligible, and the connect run itself."""

from __future__ import annotations

import json
import logging

from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from zettel.config import AppConfig
from zettel.connector import context
from zettel.connector.note import ConnectSession, process_candidate
from zettel.connector.prompt import ConnectRejected
from zettel.domain_examples import load_domain_examples, render_for_prompt
from zettel.index import VectorIndex
from zettel.llm import LLMUnavailableError, get_llm, load_prompt_parts
from zettel.logfmt import log_step
from zettel.progress import report
from zettel.retrieval import Retriever
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.usage import begin_run, finish_pipeline_run, get_tracker, set_source
from zettel.vault import sync_source_costs_to_vault

logger = logging.getLogger(__name__)


def load_approved_candidates(db: StateDB) -> list[dict]:
    """Load the concepts eligible for ``connect`` straight from SQLite.

    This is the entry gate of Phase 3, and the only definition of what "eligible"
    means: a concept whose ``status`` is ``approved`` (the reviewer let it through)
    **and** whose ``note_id`` is still NULL (no permanent note was written for it
    yet). Nothing else crosses the review/connect boundary — the phases talk to
    each other through StateDB, never through in-memory handoff. That includes
    the dedupe verdict ``refine_existing`` / ``merge``: its target note travels in
    ``concepts.dedupe_json`` and becomes an ``extends`` edge here.

    Rows without a ``candidate_json`` payload are skipped rather than raising: a
    concept row can exist before the extractor has serialized its candidate, and
    an unfinished row must not abort a batch of good ones.

    Returns:
        One dict per candidate with ``concept_id``, ``source_id``, ``chunk_id``, a
        parsed ``candidate`` (``PermanentNoteCandidate``) and, when dedupe judged it
        a refinement, ``refines_note_id`` / ``refine_reason`` — shaped exactly as
        ``run_connect`` expects its ``candidates`` argument.
    """
    candidates: list[dict] = []
    for concept in db.get_concepts_by_status("approved", without_notes=True):
        raw = concept.get("candidate_json")
        if not raw:
            continue
        cand_dict = {
            "concept_id": concept["concept_id"],
            "source_id": concept["source_id"],
            "chunk_id": concept["chunk_id"],
            "candidate": PermanentNoteCandidate.model_validate_json(raw),
        }
        dedupe = json.loads(concept.get("dedupe_json") or "{}")
        if dedupe.get("refines_note_id"):
            cand_dict["refines_note_id"] = dedupe["refines_note_id"]
            cand_dict["refine_reason"] = dedupe.get("reason") or ""
        candidates.append(cand_dict)
    return candidates


def run_connect(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    candidates: list[dict],
    *,
    observer=None,
    origin: str = "pipeline",
) -> list[str]:
    """Generate permanent notes from approved candidates. Returns created note_ids.

    ``origin`` is stamped on every note produced: the manual LIT-to-ZTL path reuses
    this same machinery but must stay distinguishable from pipeline output, and it
    stops at the first ``ConnectRejected`` (re-raised after the post-run refresh)
    where the pipeline just skips the concept.
    """
    run_id = db.start_run("connect")
    begin_run(run_id)
    run_status = "completed"
    created_ids: list[str] = []
    rejection: ConnectRejected | None = None

    try:
        session = ConnectSession(
            cfg=cfg,
            db=db,
            idx=idx,
            llm=get_llm(cfg, "connect"),
            prompt_parts=load_prompt_parts(cfg.prompts_path / "permanent_note.md"),
            retriever=Retriever(cfg, db, idx),
            example_fields=render_for_prompt(
                load_domain_examples(cfg.domain.examples_path), "permanent_note"
            ),
            taxonomy=context.load_connect_taxonomy(cfg, idx),
            origin=origin,
        )

        total = len(candidates)
        report(observer, "connect", f"{total} candidato(s) aprovado(s).", total_items=total)

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]Connect[/bold blue] {task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("notas", total=total)
            for i, cand_dict in enumerate(candidates, 1):
                thesis = cand_dict["candidate"].thesis
                set_source(cand_dict.get("source_id"))
                progress.update(task, description=f"nota {i}/{total}", advance=1)
                report(
                    observer,
                    "connect",
                    f"Gerando nota {i}/{total}.",
                    current_item=thesis[:80],
                    current_index=i,
                    total_items=total,
                )
                logger.debug("Gerando nota %d/%d: %s", i, total, thesis[:50])

                try:
                    note_id = process_candidate(session, cand_dict, step=i, total=total)
                except ConnectRejected as exc:
                    if origin == "manual":
                        rejection = exc
                        break
                    continue
                if note_id:
                    created_ids.append(note_id)
                    log_step(logger, "ok", f"ZTL {note_id}")

        logger.info("Notas permanentes criadas/atualizadas: %d", len(created_ids))
        refresh_chapter_maps(cfg, db, created_ids)
        refresh_lit_permanent_links(cfg, db, candidates, created_ids)
        if rejection:
            raise rejection
        return created_ids
    except LLMUnavailableError:
        run_status = "failed"
        raise
    finally:
        set_source(None)
        _record_source_costs(cfg, db)
        finish_pipeline_run(db, run_id, run_status)


def _record_source_costs(cfg: AppConfig, db: StateDB) -> None:
    """Accumulate this run's cost on every source it touched (SQLite + SRC frontmatter)."""
    tracker = get_tracker()
    if not tracker:
        return
    for sid in tracker.sources_touched():
        db.add_source_usage(sid, tracker.summary_for_source(sid).as_dict())
        sync_source_costs_to_vault(cfg, db, sid)


def refresh_chapter_maps(cfg: AppConfig, db: StateDB, note_ids: list[str]) -> None:
    """Rewrite the chapter map of every source that just gained a note.

    Deterministic and free (counts + wikilinks, no LLM, no embedding), so the
    per-chapter note counts stay live without re-running `zettel summarize`.
    Same pattern as `sync_moc_backrefs` after a MOC write.
    """
    if not note_ids:
        return
    from zettel.summarize import refresh_chapter_map

    source_ids = {
        row["source_id"]
        for row in (db.get_note(nid) for nid in note_ids)
        if row and row.get("source_id")
    }
    for sid in source_ids:
        try:
            refresh_chapter_map(cfg, db, sid)
        except OSError as e:
            logger.warning("Falha ao atualizar o mapa de capitulos de %s: %s", sid, e)


def refresh_lit_permanent_links(
    cfg: AppConfig, db: StateDB, candidates: list[dict], note_ids: list[str]
) -> None:
    """Link each source LIT to the ZTL just written from it (`auto-lit-permanent`)."""
    if not note_ids:
        return
    from zettel.review import sync_lit_permanent_links

    try:
        sync_lit_permanent_links(cfg, db, [c["chunk_id"] for c in candidates if c.get("chunk_id")])
    except OSError as e:
        logger.warning("Falha ao atualizar os links LIT -> ZTL: %s", e)
