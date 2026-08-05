"""Selective approval of granular literature notes (HITL between extract and connect)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.index import VectorIndex
from zettel.llm import get_llm
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.vault import (
    approved_chunk_filename,
    build_literature_index_note,
    compose_note,
    literature_chunk_dirname,
    literature_index_filename,
    parse_frontmatter,
    safe_update_managed_blocks,
    safe_write_note,
)

logger = logging.getLogger(__name__)


def run_review(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    *,
    source_id: str | None = None,
    auto_approve: bool = False,
    interactive: bool = True,
    low_confidence_only: bool = False,
) -> dict[str, int]:
    """Approve/reject literature drafts awaiting review.

    Returns counts: approved, rejected, skipped.
    """
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("review")
    begin_run(run_id)

    chunks = db.get_chunks_by_status("awaiting_review", source_id=source_id)
    limiar = cfg.literature_review.auto_approve_min_confidence

    if low_confidence_only:
        chunks = [
            c for c in chunks
            if (c.get("review_confidence") or 0) < limiar
        ]

    stats = {"approved": 0, "rejected": 0, "skipped": 0}
    if not chunks:
        logger.info("Nenhum chunk aguardando review")
        finish_pipeline_run(db, run_id)
        return stats

    if auto_approve or not interactive:
        for chunk in chunks:
            conf = chunk.get("review_confidence") or 0
            if conf >= limiar:
                if approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                    stats["approved"] += 1
                else:
                    stats["skipped"] += 1
            elif auto_approve:
                # auto_approve mode: reject low confidence? Keep awaiting unless --yes force
                stats["skipped"] += 1
            else:
                stats["skipped"] += 1
        # Deduplicate newly approved concepts
        _dedupe_approved_concepts(cfg, db, idx, source_id)
        finish_pipeline_run(db, run_id)
        return stats

    from rich.console import Console
    from rich.prompt import Prompt
    from rich.table import Table

    console = Console(stderr=True)
    sample = chunks[: cfg.literature_review.batch_sample_size]
    table = Table(title=f"Review de LIT ({len(chunks)} aguardando)")
    table.add_column("#")
    table.add_column("Chunk")
    table.add_column("Pagina")
    table.add_column("Conf")
    table.add_column("Resumo")
    for i, c in enumerate(sample, 1):
        summary = ""
        if c.get("summary_json"):
            try:
                summary = (json.loads(c["summary_json"]).get("summary") or "")[:60]
            except json.JSONDecodeError:
                pass
        table.add_row(
            str(i),
            c["chunk_id"][-24:],
            str(c.get("page_in_book") or c.get("page_in_file") or "?"),
            f"{(c.get('review_confidence') or 0):.2f}",
            summary,
        )
    console.print(table)
    console.print(
        f"[cyan]Limiar auto-approve: {limiar}. "
        "Comandos: a=aprovar todos >= limiar, r=revisar um a um, q=sair[/cyan]"
    )
    mode = Prompt.ask("Modo", choices=["a", "r", "q"], default="a", console=console)
    if mode == "q":
        finish_pipeline_run(db, run_id)
        return stats
    if mode == "a":
        for chunk in chunks:
            conf = chunk.get("review_confidence") or 0
            if conf >= limiar:
                if approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                    stats["approved"] += 1
                else:
                    stats["skipped"] += 1
            else:
                stats["skipped"] += 1
        _dedupe_approved_concepts(cfg, db, idx, source_id)
        finish_pipeline_run(db, run_id)
        return stats

    for chunk in sample:
        conf = chunk.get("review_confidence") or 0
        summary = ""
        if chunk.get("summary_json"):
            try:
                summary = json.loads(chunk["summary_json"]).get("summary") or ""
            except json.JSONDecodeError:
                pass
        console.print(
            f"\n[bold]{chunk['chunk_id']}[/bold] conf={conf:.2f}\n{summary[:300]}"
        )
        choice = Prompt.ask(
            "Decisao",
            choices=["aprovar", "rejeitar", "pular", "sair"],
            default="aprovar" if conf >= limiar else "pular",
            console=console,
        )
        if choice == "sair":
            break
        if choice == "aprovar":
            if approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                stats["approved"] += 1
            else:
                stats["skipped"] += 1
        elif choice == "rejeitar":
            if reject_chunk(cfg, db, idx, chunk["chunk_id"]):
                stats["rejected"] += 1
            else:
                stats["skipped"] += 1
        else:
            stats["skipped"] += 1

    _dedupe_approved_concepts(cfg, db, idx, source_id)
    finish_pipeline_run(db, run_id)
    return stats


def approve_high_confidence(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, source_id: str | None = None
) -> int:
    limiar = cfg.literature_review.auto_approve_min_confidence
    n = 0
    for chunk in db.get_chunks_by_status("awaiting_review", source_id=source_id):
        if (chunk.get("review_confidence") or 0) >= limiar:
            if approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                n += 1
    if n:
        _dedupe_approved_concepts(cfg, db, idx, source_id)
    return n


def approve_chunk(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, chunk_id: str
) -> bool:
    """Move draft to 20_Literature, embed literature_notes, promote concepts."""
    chunk = db.get_chunk(chunk_id)
    if not chunk or chunk.get("status") != "awaiting_review":
        logger.warning("Chunk %s nao esta awaiting_review", chunk_id)
        return False

    source = db.get_source(chunk["source_id"])
    if not source:
        return False

    citekey = source["citekey"]
    chunk_index = int(chunk.get("chunk_index") or 0)
    draft_path_str = chunk.get("literature_note_path")
    draft_path = Path(draft_path_str) if draft_path_str else None

    dest_dir = (
        cfg.vault_path / "20_Literature" / literature_chunk_dirname(citekey)
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / approved_chunk_filename(chunk_index)

    if draft_path and draft_path.exists():
        content = draft_path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(content)
        meta["status"] = "approved"
        meta["updated_at"] = meta.get("updated_at")
        safe_write_note(dest_path, meta, body)
        try:
            draft_path.unlink()
        except OSError:
            pass
    else:
        # Rebuild from summary_json if draft missing
        summary_data: dict[str, Any] = {}
        if chunk.get("summary_json"):
            try:
                summary_data = json.loads(chunk["summary_json"])
            except json.JSONDecodeError:
                pass
        from zettel.vault import build_literature_chunk_note
        meta, body = build_literature_chunk_note(
            source_id=chunk["source_id"],
            citekey=citekey,
            title=source["title"],
            chunk_id=chunk_id,
            chunk_index=chunk_index,
            literature_id=chunk.get("literature_id") or chunk_id,
            summary=summary_data.get("summary", ""),
            key_concepts=summary_data.get("key_concepts") or [],
            candidates=summary_data.get("candidates") or [],
            page_in_file=chunk.get("page_in_file"),
            page_in_book=chunk.get("page_in_book"),
            page_confidence=chunk.get("page_confidence") or "unknown",
            status="approved",
            review_confidence=chunk.get("review_confidence"),
        )
        safe_write_note(dest_path, meta, body)

    # Embed literature note (summary + concepts)
    embed_text = _literature_embed_text(dest_path)
    lit_id = chunk.get("literature_id") or chunk_id
    idx.upsert_literature_note(
        lit_id,
        embed_text,
        {
            "source_id": chunk["source_id"],
            "chunk_id": chunk_id,
            "citekey": citekey,
            "path": str(dest_path.relative_to(cfg.vault_path)).replace("\\", "/"),
            "chunk_index": chunk_index,
            "page_in_book": chunk.get("page_in_book") or -1,
        },
    )

    db.update_chunk_review(
        chunk_id,
        status="persisted",
        literature_note_path=str(dest_path),
    )
    # Concepts become eligible for dedupe → approved
    for concept in db.get_concepts_for_chunk(chunk_id):
        if concept.get("status") == "awaiting_review":
            db.update_concept_status(concept["concept_id"], "extracted")

    _refresh_literature_index(cfg, db, chunk["source_id"])
    logger.info("[NOTE=%s] APPROVED → persistido no vetorial literature_notes", dest_path)
    return True


def reject_chunk(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, chunk_id: str
) -> bool:
    chunk = db.get_chunk(chunk_id)
    if not chunk:
        return False
    draft_path_str = chunk.get("literature_note_path")
    if draft_path_str:
        p = Path(draft_path_str)
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                logger.warning("Nao foi possivel apagar draft %s: %s", p, e)

    lit_id = chunk.get("literature_id")
    if lit_id:
        try:
            idx.delete_literature_notes([lit_id])
        except Exception:
            pass

    db.update_chunk_review(chunk_id, status="rejected", literature_note_path=None)
    db.update_concepts_status_for_chunk(chunk_id, "rejected")
    logger.info("[CHUNK=%s] REJECTED → descartado, nao indexado", chunk_id)
    return True


def _literature_embed_text(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(content)
    title = meta.get("chunk_id", path.stem)
    # Prefer resumo section
    return f"{title}\n\n{body[:3000]}"


def _refresh_literature_index(cfg: AppConfig, db: StateDB, source_id: str) -> None:
    source = db.get_source(source_id)
    if not source:
        return
    citekey = source["citekey"]
    title = source["title"]
    approved = [
        c for c in db.get_chunks_for_source(source_id)
        if c.get("status") in ("approved", "persisted")
    ]
    approved.sort(key=lambda c: c.get("chunk_index") or 0)
    links: list[str] = []
    for c in approved:
        idx_n = int(c.get("chunk_index") or 0)
        stem = f"{literature_chunk_dirname(citekey)}/{approved_chunk_filename(idx_n).removesuffix('.md')}"
        page = c.get("page_in_book") or c.get("page_in_file")
        label = f"Chunk {idx_n}" + (f" (p. {page})" if page is not None else "")
        links.append(f"[[{stem}|{label}]]")

    lit_dir = cfg.vault_path / "20_Literature"
    # Prefer existing index file
    matches = list(lit_dir.glob(f"LIT - @{citekey}*index.md"))
    if not matches:
        matches = list(lit_dir.glob(f"LIT - @{citekey}*"))
    if matches:
        lit_path = matches[0]
        block = "\n".join(f"- {link}" for link in links) if links else "_Nenhuma nota granular aprovada ainda._\n"
        safe_update_managed_blocks(lit_path, {"auto-lit-index": block})
        try:
            db.update_source_texts(source_id, lit_body=lit_path.read_text(encoding="utf-8"))
        except OSError:
            pass
    else:
        meta, body = build_literature_index_note(source_id, citekey, title, approved_links=links)
        lit_path = lit_dir / literature_index_filename(citekey, title)
        safe_write_note(lit_path, meta, body)
        db.update_source_texts(source_id, lit_body=compose_note(meta, body))


def _dedupe_approved_concepts(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, source_id: str | None
) -> None:
    """Run semantic dedupe on concepts with status=extracted (post-approve)."""
    rows = db.get_concepts_by_status("extracted")
    if source_id:
        rows = [r for r in rows if r["source_id"] == source_id]
    if not rows:
        return

    candidates: list[dict] = []
    for row in rows:
        raw = row.get("candidate_json")
        if not raw:
            continue
        try:
            cand = PermanentNoteCandidate(**json.loads(raw))
        except Exception:
            continue
        candidates.append({
            "concept_id": row["concept_id"],
            "source_id": row["source_id"],
            "chunk_id": row["chunk_id"],
            "candidate": cand,
        })

    if not candidates:
        return

    from zettel.extractor import deduplicate_candidates
    llm = get_llm(cfg)
    approved = deduplicate_candidates(cfg, db, idx, llm, candidates)
    logger.info(
        "Dedupe pos-review: %d / %d candidatos aprovados para connect",
        len(approved), len(candidates),
    )
