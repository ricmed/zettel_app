"""Phase 3 entry points: which concepts are eligible, and the connect run itself."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from zettel.config import AppConfig, llm_phase
from zettel.connector import context
from zettel.connector.note import ConnectSession, process_candidate
from zettel.connector.prompt import ConnectCandidateFailed, ConnectRejected
from zettel.domain_examples import load_domain_examples, render_for_prompt
from zettel.hashing import sha256_hex
from zettel.index import VectorIndex
from zettel.llm import get_llm, load_prompt_parts
from zettel.logfmt import log_step
from zettel.progress import report
from zettel.retrieval import Retriever
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.usage import begin_run, finish_pipeline_run, get_tracker, set_source
from zettel.vault import sync_source_costs_to_vault

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectOutcome:
    """What one ``run_connect`` wrote, for the CLI summary and the web job."""

    note_ids: list[str]
    rejected: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    edges_by_type: dict[str, int] = field(default_factory=dict)
    intra_source: int = 0
    inter_source: int = 0
    corroborates: int = 0
    suggestions: int = 0
    cost_usd: float = 0.0


def load_approved_candidates(db: StateDB, source_ids: list[str] | None = None) -> list[dict]:
    """Load the concepts eligible for ``connect`` straight from SQLite.

    Eligible means ``approved`` with no note yet, or ``connecting``: a note was
    started (file, row, ``note_id``) and the run stopped before ``noted``.
    The second set is how a failed embedding is resumed on the same note.
    ``source_ids`` limits the queue to those sources (``run-all``); ``None``
    is the whole vault, which is what ``zettel connect`` wants.
    """
    rows = list(db.get_concepts_by_status("approved", without_notes=True))
    rows.extend(db.get_concepts_by_status("connecting"))
    if source_ids is not None:
        allowed = set(source_ids)
        rows = [row for row in rows if row["source_id"] in allowed]
    candidates: list[dict] = []
    for concept in rows:
        cand_dict = _candidate_from_row(concept)
        if cand_dict is not None:
            candidates.append(cand_dict)
    return candidates


def _candidate_from_row(concept: dict) -> dict | None:
    raw = concept.get("candidate_json")
    if not raw:
        return None
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
    if dedupe.get("force_connect"):
        cand_dict["force_connect"] = True
    return cand_dict


def run_connect(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    candidates: list[dict],
    *,
    observer=None,
    origin: str = "pipeline",
) -> ConnectOutcome:
    """Generate permanent notes from approved candidates.

    ``origin`` is stamped on every note produced: the manual LIT-to-ZTL path reuses
    this same machinery but must stay distinguishable from pipeline output, and it
    stops at the first ``ConnectRejected`` (re-raised after the post-run refresh)
    where the pipeline just skips the concept.
    """
    run_id = db.start_run("connect")
    begin_run(run_id)
    run_status = "completed"
    created_ids: list[str] = []
    rejected = 0
    failed = 0
    skipped = 0
    errors: list[str] = []
    rejection: ConnectRejected | None = None

    try:
        prompt_parts = load_prompt_parts(cfg.prompts_path / "permanent_note.md")
        spec = llm_phase(cfg, "connect")
        session = ConnectSession(
            cfg=cfg,
            db=db,
            idx=idx,
            llm=get_llm(cfg, "connect"),
            prompt_parts=prompt_parts,
            retriever=Retriever(cfg, db, idx, strict_embedding=True),
            example_fields=render_for_prompt(
                load_domain_examples(cfg.domain.examples_path), "permanent_note"
            ),
            taxonomy=context.load_connect_taxonomy(cfg, idx),
            origin=origin,
            run_id=run_id,
            prompt_checksum=sha256_hex(prompt_parts.full_template),
            model=f"{spec.provider}/{spec.model}",
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
                    mark_connect_rejected(db, cand_dict, exc)
                    rejected += 1
                    continue
                except ConnectCandidateFailed as exc:
                    failed += 1
                    msg = str(exc).strip() or "falha ao gerar a nota"
                    if exc.concept_id and exc.concept_id not in msg:
                        msg = f"{exc.concept_id}: {msg}"
                    errors.append(msg)
                    report(
                        observer,
                        "connect",
                        f"Falha: {msg}",
                        current_item=thesis[:80],
                        current_index=i,
                        total_items=total,
                    )
                    continue
                if note_id:
                    created_ids.append(note_id)
                    log_step(logger, "ok", f"ZTL {note_id}")
                else:
                    skipped += 1

        logger.info("Notas permanentes criadas/atualizadas: %d", len(created_ids))
        refresh_chapter_maps(cfg, db, created_ids)
        refresh_lit_permanent_links(cfg, db, candidates, created_ids)
        if rejection:
            raise rejection
        tally = tally_connect_notes(db, created_ids)
        tracker = get_tracker()
        cost = float((tracker.summary().as_dict() if tracker else {}).get("cost_usd_llm") or 0)
        return ConnectOutcome(
            note_ids=created_ids,
            rejected=rejected,
            failed=failed,
            skipped=skipped,
            errors=errors,
            cost_usd=cost,
            **tally,
        )
    except Exception:
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


def tally_connect_notes(db: StateDB, note_ids: list[str]) -> dict:
    """Outgoing edges and vault suggestions for the notes this run wrote."""
    from zettel.vault import read_managed_block

    types: Counter[str] = Counter()
    intra = inter = corroborates = suggestions = 0
    for nid in note_ids:
        row = db.get_note(nid) or {}
        src = row.get("source_id") or ""
        for edge in db.list_outgoing_connections(nid):
            rel = str(edge.get("relation_type") or "")
            types[rel] += 1
            if rel == "corroborates":
                corroborates += 1
            target = db.get_note(edge["target_note_id"]) or {}
            tgt = target.get("source_id") or ""
            if src and tgt and src == tgt:
                intra += 1
            elif src and tgt:
                inter += 1
        path = row.get("path")
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        block = read_managed_block(text, "auto-connections") or ""
        suggestions += sum(1 for line in block.splitlines() if line.lstrip().startswith("-"))
    return {
        "edges_by_type": dict(types),
        "intra_source": intra,
        "inter_source": inter,
        "corroborates": corroborates,
        "suggestions": suggestions,
    }


def mark_connect_rejected(db: StateDB, cand_dict: dict, exc: ConnectRejected) -> None:
    """Take an approved concept out of the connect queue and keep the model's reason.

    ``dedupe_json`` keeps any refine payload already stored. The rejection sits
    beside it, so a later requeue can drop only that key.
    """
    concept_id = cand_dict["concept_id"]
    row = db.get_concept(concept_id) or {}
    payload = json.loads(row.get("dedupe_json") or "{}")
    payload["connect_rejection"] = {"reason": exc.reason, "category": exc.category}
    payload.pop("force_connect", None)
    db.set_concept_dedupe(concept_id, "connect_rejected", payload)


def requeue_connect_rejected(db: StateDB, *, force: bool = False) -> int:
    """Return ``connect_rejected`` concepts to ``approved``.

    ``force`` asks the next Prompt 2 call not to decline the concept. The stored
    rejection reason is cleared either way. A refine payload in the same JSON stays.
    """
    restored = 0
    for row in db.get_concepts_by_status("connect_rejected"):
        payload = json.loads(row.get("dedupe_json") or "{}")
        payload.pop("connect_rejection", None)
        if force:
            payload["force_connect"] = True
        else:
            payload.pop("force_connect", None)
        db.set_concept_dedupe(row["concept_id"], "approved", payload)
        restored += 1
    return restored
