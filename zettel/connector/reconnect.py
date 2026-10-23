"""Relink permanent notes that already exist, without rewriting their prose.

``zettel reconnect`` searches the whole vault (an older note can point at a newer
one) and asks Prompt 2 only for edges. Title, thesis and the other body fields
in the answer are discarded. ``manual`` and ``derived`` edges stay. The relation
menu stays the one #231 already wrote: direction is from the note being
reconnected toward the candidate, and the inverse is the backlink.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zettel.config import AppConfig, llm_phase
from zettel.connector import context, links
from zettel.connector.note import ConnectSession
from zettel.connector.prompt import (
    ConnectRejected,
    Prompt2Payload,
    generate_note_links,
    generate_permanent_note,
)
from zettel.hashing import sha256_hex
from zettel.llm import LLMUnavailableError, load_prompt_parts
from zettel.note_content import note_sections
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.usage import begin_run, finish_pipeline_run, set_source

logger = logging.getLogger(__name__)

LinkFn = Callable[[dict], list[dict]]


@dataclass(frozen=True)
class EdgeRef:
    target_note_id: str
    relation_type: str
    description: str = ""


@dataclass(frozen=True)
class RelinkDiff:
    """LLM edges that would leave, arrive, or keep their endpoints."""

    note_id: str
    remove: tuple[EdgeRef, ...]
    add: tuple[EdgeRef, ...]
    keep: tuple[EdgeRef, ...]


def candidate_from_note(row: dict) -> PermanentNoteCandidate | None:
    """The claim already on the note, which is what the linker should relate."""
    sections = note_sections(row.get("title") or "", row.get("body") or "")
    thesis = (sections.get("thesis") or "").strip()
    definition = (sections.get("definition") or "").strip()
    if not thesis and not definition:
        return None
    return PermanentNoteCandidate(
        thesis=thesis or definition,
        definition=definition or thesis,
        limits=sections.get("limits") or "",
    )


def select_reconnect_notes(
    db: StateDB, *, source_id: str | None = None, note_id: str | None = None
) -> list[dict]:
    """Pipeline permanent notes, oldest id first so the order does not depend on insertion."""
    permanent = db.list_permanent_note_ids()
    chosen: list[dict] = []
    for row in db.list_notes():
        if row["note_id"] not in permanent:
            continue
        if (row.get("origin") or "") == "manual":
            continue
        if source_id and (row.get("source_id") or "") != source_id:
            continue
        if note_id and row["note_id"] != note_id:
            continue
        chosen.append(row)
    chosen.sort(key=lambda row: row["note_id"])
    return chosen


def diff_llm_edges(note_id: str, existing: list[dict], proposed: list[dict]) -> RelinkDiff:
    """Compare outgoing ``origin='llm'`` edges with the edges the linker just returned."""

    def ref_from_row(row: dict) -> EdgeRef:
        return EdgeRef(
            row["target_note_id"],
            row.get("relation_type") or "related",
            row.get("description") or "",
        )

    def ref_from_proposed(row: dict) -> EdgeRef:
        return EdgeRef(
            row["related_note_id"],
            row.get("relation_type") or "related",
            row.get("description") or "",
        )

    old = {(item.target_note_id, item.relation_type): item for item in map(ref_from_row, existing)}
    new = {
        (item.target_note_id, item.relation_type): item for item in map(ref_from_proposed, proposed)
    }
    remove = tuple(old[key] for key in sorted(old.keys() - new.keys()))
    add = tuple(new[key] for key in sorted(new.keys() - old.keys()))
    keep = tuple(new[key] for key in sorted(old.keys() & new.keys()))
    return RelinkDiff(note_id=note_id, remove=remove, add=add, keep=keep)


def render_diff(diff: RelinkDiff) -> str:
    """Portuguese lines the Windows console can encode."""
    lines = [f"nota {diff.note_id}"]
    if not diff.remove and not diff.add and not diff.keep:
        lines.append("  sem arestas de LLM")
        return "\n".join(lines)
    for label, edges in (("sair", diff.remove), ("entrar", diff.add), ("manter", diff.keep)):
        for edge in edges:
            lines.append(
                f"  {label}: {diff.note_id} -> {edge.target_note_id} ({edge.relation_type})"
            )
    return "\n".join(lines)


def apply_relink(
    cfg: AppConfig,
    db: StateDB,
    note: dict,
    proposed: list[dict],
    *,
    provenance: dict,
) -> None:
    """Replace outgoing LLM edges. ``manual`` and ``derived`` rows are not deleted."""
    note_id = note["note_id"]
    old_targets = {
        row["target_note_id"] for row in db.list_outgoing_connections(note_id, origin="llm")
    }
    db.delete_outgoing_connections(note_id, origin="llm")
    for edge in proposed:
        db.upsert_note_connection(
            source_note_id=note_id,
            target_note_id=edge["related_note_id"],
            relation_type=edge.get("relation_type") or "related",
            description=edge.get("description") or "",
            origin="llm",
            prompt_checksum=provenance.get("prompt_checksum") or "",
            model=provenance.get("model") or "",
            run_id=provenance.get("run_id"),
            status="proposed" if cfg.linking.llm_edges == "propose" else "accepted",
        )
    new_targets = {edge["related_note_id"] for edge in proposed}
    for target_id in sorted(old_targets | new_targets | {note_id}):
        links.rebuild_auto_backlinks(db, target_id, vault_timezone=cfg.vault_timezone)
    record = db.get_note(note_id)
    if links.note_on_disk(record):
        links.sync_connections_section(
            Path(record["path"]),
            links.outgoing_connection_bullets(db, note_id),
            vault_timezone=cfg.vault_timezone,
        )


def propose_links(session: ConnectSession, note: dict) -> list[dict]:
    """Prompt 2 connections for an existing note. The rewritten body is ignored."""
    cand = candidate_from_note(note)
    if cand is None:
        return []
    cfg, db = session.cfg, session.db
    query = f"{cand.thesis} {cand.definition}"
    similar = session.retriever.search_notes(
        query,
        topk=cfg.linking.topk,
        exclude_id=note["note_id"],
        source_id=note.get("source_id") or "",
        graph=cfg.linking.graph_expansion,
        record_neighbours=True,
    ).hits
    distant = context.search_distant_analogies(
        cfg, session.idx, session.retriever, query, note["note_id"], similar, session.taxonomy
    )
    rag_context = context.build_rag_context(
        db, similar, distant, note_chars=cfg.linking.rag_note_chars
    )
    if cfg.linking.prompt2_mode == "split":
        connections, _cache_hit = generate_note_links(
            cfg,
            db,
            session.llm,
            thesis=cand.thesis,
            definition=cand.definition,
            rag_context=rag_context,
            label=f"reconnect:{note['note_id']}",
            subject_id=note["note_id"],
        )
        output_connections = connections
    else:
        output, _cache_hit = generate_permanent_note(
            cfg,
            db,
            session.llm,
            session.prompt_parts,
            cand,
            Prompt2Payload(
                source_id=note.get("source_id") or "",
                literature_ref="",
                rag_context=rag_context,
                images_context="",
                examples=session.example_fields,
            ),
            label=f"reconnect:{note['note_id']}",
        )
        output_connections = list(output.connections)
    demoted = links.demote_llm_corroborates(output_connections)
    allowed = {hit.note_id for hit in similar} | {hit.note_id for hit in distant}
    resolved = links.resolve_connections(db, demoted, allowed_ids=allowed)
    distant_ids = {item.note_id for item in distant}
    return [
        edge
        for edge in resolved
        if edge["related_note_id"] != note["note_id"] and edge["related_note_id"] not in distant_ids
    ]


def run_reconnect(
    cfg: AppConfig,
    db: StateDB,
    session: ConnectSession,
    notes: list[dict],
    *,
    apply: bool,
    link: LinkFn | None = None,
) -> list[RelinkDiff]:
    """Relink ``notes``. ``apply`` false still calls the linker and writes nothing."""
    run_id = db.start_run("reconnect")
    begin_run(run_id)
    run_status = "completed"
    diffs: list[RelinkDiff] = []
    provenance = {
        "prompt_checksum": session.prompt_checksum,
        "model": session.model,
        "run_id": run_id,
    }
    try:
        for note in notes:
            set_source(note.get("source_id"))
            if candidate_from_note(note) is None:
                logger.warning("Reconnect pulou %s: nota sem tese e sem definicao", note["note_id"])
                continue
            try:
                proposed = (link or (lambda row: propose_links(session, row)))(note)
            except ConnectRejected as exc:
                logger.warning("Reconnect pulou %s: %s", note["note_id"], exc)
                continue
            diff = diff_llm_edges(
                note["note_id"],
                db.list_outgoing_connections(note["note_id"], origin="llm"),
                proposed,
            )
            diffs.append(diff)
            if apply:
                apply_relink(cfg, db, note, proposed, provenance=provenance)
        return diffs
    except LLMUnavailableError:
        run_status = "failed"
        raise
    except Exception:
        run_status = "failed"
        raise
    finally:
        set_source(None)
        finish_pipeline_run(db, run_id, run_status)


def edge_provenance(cfg: AppConfig, template: str, run_id: int | None) -> dict:
    """Prompt hash and model identity stored on each LLM edge."""
    spec = llm_phase(cfg, "connect")
    return {
        "prompt_checksum": sha256_hex(template),
        "model": f"{spec.provider}/{spec.model}",
        "run_id": run_id,
    }


def make_session(cfg: AppConfig, db: StateDB, idx) -> ConnectSession:
    """The same session connect uses, pointed at notes that already exist."""
    from zettel.domain_examples import load_domain_examples, render_for_prompt
    from zettel.llm import get_llm
    from zettel.retrieval import Retriever

    prompt_parts = load_prompt_parts(cfg.prompts_path / "permanent_note.md")
    spec = edge_provenance(cfg, prompt_parts.full_template, None)
    return ConnectSession(
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
        prompt_checksum=spec["prompt_checksum"],
        model=spec["model"],
    )
