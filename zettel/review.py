"""Selective approval of granular literature notes (HITL between extract and connect)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.index import VectorIndex
from zettel.llm import get_llm
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.vault import (
    build_literature_index_note,
    compose_note,
    literature_chunk_filename_for_row,
    literature_chunk_wikilink_for_row,
    literature_index_filename,
    literature_source_dirname,
    parse_frontmatter,
    safe_update_managed_blocks,
    safe_write_note,
)

logger = logging.getLogger(__name__)

# Faixa "baixissima": confianca inclusiva ate este valor.
_LOW_CONFIDENCE_MAX = 0.4

BAND_VERY_LOW = "very_low"
BAND_MEDIUM = "medium"
BAND_HIGH = "high"
BAND_ALL = "all"

_BAND_LABELS = {
    BAND_VERY_LOW: "Baixissima",
    BAND_MEDIUM: "Media",
    BAND_HIGH: "Alta",
    BAND_ALL: "todas as faixas",
}

_REJECT_SCOPE_ALIASES = {
    "t": BAND_ALL,
    "todos": BAND_ALL,
    "b": BAND_VERY_LOW,
    "baixissima": BAND_VERY_LOW,
    "m": BAND_MEDIUM,
    "media": BAND_MEDIUM,
    "h": BAND_HIGH,
    "alta": BAND_HIGH,
    "c": "cancel",
    "cancelar": "cancel",
}

_DECISION_ALIASES = {
    "a": "aprovar",
    "aprovar": "aprovar",
    "r": "rejeitar",
    "rejeitar": "rejeitar",
    "p": "pular",
    "pular": "pular",
    "q": "sair",
    "sair": "sair",
}


def chunk_confidence_band(conf: float, limiar: float) -> str:
    """Classifica uma confianca em very_low / medium / high."""
    if conf <= _LOW_CONFIDENCE_MAX:
        return BAND_VERY_LOW
    if conf < limiar:
        return BAND_MEDIUM
    return BAND_HIGH


def filter_chunks_by_band(
    chunks: list[dict], band: str, limiar: float
) -> list[dict]:
    """Filtra chunks pela faixa; band=all devolve a lista inteira."""
    if band == BAND_ALL:
        return list(chunks)
    return [
        c for c in chunks
        if chunk_confidence_band(float(c.get("review_confidence") or 0), limiar) == band
    ]


def confidence_band_counts(
    chunks: list[dict], limiar: float
) -> dict[str, int]:
    """Conta drafts por faixa de review_confidence.

    Faixas:
    - very_low: 0 <= conf <= 0.4
    - medium: 0.4 < conf < limiar
    - high: conf >= limiar
    """
    very_low = medium = high = 0
    for chunk in chunks:
        band = chunk_confidence_band(float(chunk.get("review_confidence") or 0), limiar)
        if band == BAND_VERY_LOW:
            very_low += 1
        elif band == BAND_MEDIUM:
            medium += 1
        else:
            high += 1
    return {
        BAND_VERY_LOW: very_low,
        BAND_MEDIUM: medium,
        BAND_HIGH: high,
        "total": len(chunks),
    }


def format_confidence_report(bands: dict[str, int], limiar: float) -> str:
    """Texto PT-BR do relatorio de faixas (sem markup Rich)."""
    low_max = _LOW_CONFIDENCE_MAX
    return (
        f"Total aguardando: {bands['total']} | Limiar: {limiar:.2f}\n"
        f"  Baixissima (0.00-{low_max:.2f}): {bands[BAND_VERY_LOW]}\n"
        f"  Media ({low_max:.2f} < conf < {limiar:.2f}): {bands[BAND_MEDIUM]}\n"
        f"  Alta (conf >= {limiar:.2f}): {bands[BAND_HIGH]}"
    )


def _summary_from_chunk(chunk: dict) -> str:
    raw = chunk.get("summary_json")
    if not raw:
        return ""
    try:
        return (json.loads(raw).get("summary") or "").strip()
    except json.JSONDecodeError:
        return ""


def format_review_item(chunk: dict) -> str:
    """Card PT-BR do review um-a-um: cabecalho, resumo do LLM e trecho da fonte."""
    conf = float(chunk.get("review_confidence") or 0)
    page = chunk.get("page_in_book") or chunk.get("page_in_file") or "?"
    section = (chunk.get("section_path") or "").strip()
    header = f"{chunk['chunk_id']} conf={conf:.2f}  p.{page}"
    if section:
        header += f"  {section}"
    summary = _summary_from_chunk(chunk) or "_Sem resumo._"
    excerpt = (chunk.get("text") or "").strip() or "_Trecho nao disponivel._"
    return (
        f"{header}\n\n"
        f"Resumo\n{summary}\n\n"
        f"Trecho\n{excerpt}"
    )


def normalize_reject_scope(raw: str) -> str | None:
    """Mapeia atalho/palavra para faixa de rejeicao ou cancel."""
    key = (raw or "").strip().lower()
    return _REJECT_SCOPE_ALIASES.get(key)


def normalize_review_decision(raw: str) -> str | None:
    """Mapeia atalho ou palavra completa para aprovar/rejeitar/pular/sair."""
    key = (raw or "").strip().lower()
    return _DECISION_ALIASES.get(key)


def ask_review_decision(console, *, conf: float, limiar: float) -> str:
    """Prompt HITL um-a-um com atalhos a/r/p/q e palavras completas."""
    from rich.prompt import Prompt

    default = "a" if conf >= limiar else "p"
    while True:
        # Colchetes escapados: Rich trata [...] como markup e engole o texto.
        raw = Prompt.ask(
            r"Decisao \[a=aprovar/r=rejeitar/p=pular/q=sair\]",
            choices=list(_DECISION_ALIASES.keys()),
            default=default,
            show_choices=False,
            console=console,
        )
        choice = normalize_review_decision(raw)
        if choice is not None:
            return choice


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
            else:
                stats["skipped"] += 1
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
        summary = _summary_from_chunk(c)[:200]
        table.add_row(
            str(i),
            c["chunk_id"][-24:],
            str(c.get("page_in_book") or c.get("page_in_file") or "?"),
            f"{(c.get('review_confidence') or 0):.2f}",
            summary,
        )
    console.print(table)

    bands = confidence_band_counts(chunks, limiar)
    report = format_confidence_report(bands, limiar)
    console.print(f"[cyan]{report}[/cyan]")
    console.print(
        "[cyan]Comandos: a=aprovar >= limiar, d=reprovar (todos ou por faixa), "
        "r=revisar um a um, q=sair[/cyan]"
    )

    while True:
        mode = Prompt.ask(
            "Modo",
            choices=["a", "d", "r", "q"],
            default="a",
            console=console,
        )
        if mode == "q":
            finish_pipeline_run(db, run_id)
            return stats

        if mode == "d":
            bands = confidence_band_counts(chunks, limiar)
            report = format_confidence_report(bands, limiar)
            console.print(f"[yellow]{report}[/yellow]")
            scope_raw = Prompt.ask(
                r"Reprovar \[t=todos/b=baixissima/m=media/h=alta/c=cancelar\]",
                choices=list(_REJECT_SCOPE_ALIASES.keys()),
                default="c",
                show_choices=False,
                console=console,
            )
            scope = normalize_reject_scope(scope_raw)
            if scope is None or scope == "cancel":
                console.print("[dim]Rejeicao em lote cancelada.[/dim]")
                continue

            targets = filter_chunks_by_band(chunks, scope, limiar)
            if not targets:
                console.print(
                    f"[dim]Nenhum draft na faixa "
                    f"{_BAND_LABELS[scope]}.[/dim]"
                )
                continue

            label = _BAND_LABELS[scope]
            confirm = Prompt.ask(
                f"Confirmar rejeicao de {len(targets)} drafts ({label})?",
                choices=["s", "n"],
                default="n",
                console=console,
            )
            if confirm != "s":
                console.print("[dim]Rejeicao em lote cancelada.[/dim]")
                continue

            rejected_ids: set[str] = set()
            for chunk in targets:
                if reject_chunk(cfg, db, idx, chunk["chunk_id"]):
                    stats["rejected"] += 1
                    rejected_ids.add(chunk["chunk_id"])
                else:
                    stats["skipped"] += 1

            chunks = [c for c in chunks if c["chunk_id"] not in rejected_ids]
            if not chunks:
                finish_pipeline_run(db, run_id)
                return stats

            sample = chunks[: cfg.literature_review.batch_sample_size]
            bands = confidence_band_counts(chunks, limiar)
            report = format_confidence_report(bands, limiar)
            console.print(
                f"[green]Rejeitados {len(rejected_ids)} ({label}). "
                f"Restam {len(chunks)} aguardando.[/green]"
            )
            console.print(f"[cyan]{report}[/cyan]")
            continue

        if mode == "a":
            below = 0
            for chunk in chunks:
                conf = chunk.get("review_confidence") or 0
                if conf >= limiar:
                    if approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                        stats["approved"] += 1
                    else:
                        stats["skipped"] += 1
                else:
                    below += 1
                    stats["skipped"] += 1
            console.print(
                f"[green]Aprovados {stats['approved']} (>= limiar); "
                f"abaixo do limiar {below} (permanecem awaiting_review)[/green]"
            )
            _dedupe_approved_concepts(cfg, db, idx, source_id)
            finish_pipeline_run(db, run_id)
            return stats

        # mode == "r"
        for chunk in sample:
            conf = chunk.get("review_confidence") or 0
            console.print()
            console.print(format_review_item(chunk), markup=False)
            choice = ask_review_decision(console, conf=conf, limiar=limiar)
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


def finalize_approved_concepts(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, source_id: str | None = None
) -> None:
    """Run post-approval deduplication after granular web review actions."""
    _dedupe_approved_concepts(cfg, db, idx, source_id)


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
        cfg.vault_path / "20_Literature" / literature_source_dirname(citekey)
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / literature_chunk_filename_for_row(citekey, chunk)

    if draft_path and draft_path.exists():
        content = draft_path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(content)
        meta["status"] = "approved"
        meta["updated_at"] = datetime.now().isoformat()
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
            section_path=chunk.get("section_path") or "",
            source_text=chunk.get("text") or "",
            page_in_file=chunk.get("page_in_file"),
            page_in_book=chunk.get("page_in_book"),
            page_confidence=chunk.get("page_confidence") or "unknown",
            status="approved",
            review_confidence=chunk.get("review_confidence"),
        )
        safe_write_note(dest_path, meta, body)

    excerpt = (chunk.get("text") or "").strip() or "_Trecho nao disponivel._"
    safe_update_managed_blocks(dest_path, {"auto-source-excerpt": excerpt})

    # Embed literature note (summary + concepts; source excerpt is a managed block)
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
    if not chunk or chunk.get("status") != "awaiting_review":
        logger.warning("Chunk %s nao esta awaiting_review", chunk_id)
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


def purge_rejected(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    *,
    source_id: str | None = None,
    compact: bool = True,
) -> dict[str, int | float | bool]:
    """Remove permanently chunks with status=rejected from SQLite and Chroma.

    Deletes:
    - SQLite ``chunks`` rows (+ FTS) and related ``concepts``
    - Chroma ``chunks`` embeddings (harvest index)
    - Chroma ``literature_notes`` ids, if any (normally absent — reject runs
      before approve)

    When ``compact`` is True and something was deleted, runs SQLite VACUUM on
    ``state.db`` and ``chroma.sqlite3`` to reclaim disk (no logical data change).

    Does not touch permanent notes, MOCs, or approved/persisted literature.
    """
    rows = db.get_chunks_by_status("rejected", source_id=source_id)
    if not rows:
        return {
            "chunks": 0,
            "literature_notes": 0,
            "compacted": False,
            "state_mb_before": 0.0,
            "state_mb_after": 0.0,
            "chroma_mb_before": 0.0,
            "chroma_mb_after": 0.0,
        }

    chunk_ids = [r["chunk_id"] for r in rows]
    lit_ids = [
        r["literature_id"] for r in rows
        if r.get("literature_id")
    ]

    removed_sqlite = db.delete_chunks(chunk_ids)
    idx.delete_chunks(chunk_ids)
    if lit_ids:
        try:
            idx.delete_literature_notes(lit_ids)
        except Exception as e:
            logger.warning("Falha ao limpar literature_notes no Chroma: %s", e)

    logger.info(
        "Purge rejected: %d chunks SQLite, %d literature_ids Chroma",
        removed_sqlite, len(lit_ids),
    )

    result: dict[str, int | float | bool] = {
        "chunks": removed_sqlite,
        "literature_notes": len(lit_ids),
        "compacted": False,
        "state_mb_before": 0.0,
        "state_mb_after": 0.0,
        "chroma_mb_before": 0.0,
        "chroma_mb_after": 0.0,
    }
    if compact and removed_sqlite:
        state_path = Path(db.db_path)
        chroma_db = Path(cfg.chroma_path) / "chroma.sqlite3"
        result["state_mb_before"] = round(state_path.stat().st_size / 1e6, 2)
        result["chroma_mb_before"] = (
            round(chroma_db.stat().st_size / 1e6, 2) if chroma_db.exists() else 0.0
        )
        db.vacuum()
        idx.vacuum()
        result["state_mb_after"] = round(state_path.stat().st_size / 1e6, 2)
        result["chroma_mb_after"] = (
            round(chroma_db.stat().st_size / 1e6, 2) if chroma_db.exists() else 0.0
        )
        result["compacted"] = True
        logger.info(
            "Compactacao: state %.2f→%.2f MB, chroma.sqlite3 %.2f→%.2f MB",
            result["state_mb_before"], result["state_mb_after"],
            result["chroma_mb_before"], result["chroma_mb_after"],
        )
    return result


def _literature_embed_text(path: Path) -> str:
    from zettel.hashing import extract_embeddable_text

    content = path.read_text(encoding="utf-8")
    meta, _ = parse_frontmatter(content)
    title = meta.get("chunk_id", path.stem)
    return f"{title}\n\n{extract_embeddable_text(content)}"


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
    links = [
        literature_chunk_wikilink_for_row(citekey, c, with_alias=True)
        for c in approved
    ]

    lit_dir = cfg.vault_path / "20_Literature"
    lit_path = lit_dir / literature_index_filename(citekey, title)
    if not lit_path.exists():
        meta, body = build_literature_index_note(source_id, citekey, title, approved_links=links)
        safe_write_note(lit_path, meta, body)
        db.update_source_texts(source_id, lit_body=compose_note(meta, body))
    else:
        block = "\n".join(f"- {link}" for link in links) if links else "_Nenhuma nota granular aprovada ainda._\n"
        safe_update_managed_blocks(lit_path, {"auto-lit-index": block})

    _refresh_source_topic_index(db, source_id, citekey, approved, lit_path)
    try:
        db.update_source_texts(source_id, lit_body=lit_path.read_text(encoding="utf-8"))
    except OSError:
        pass


def _refresh_source_topic_index(
    db: StateDB, source_id: str, citekey: str, approved: list[dict], lit_path: Path,
) -> None:
    """Rebuild the source's `auto-topic-index` from its approved literature notes.

    Targets are literature wikilinks, so the rows carry no ``note_id``: they route
    a reader to the right granular note, but a LIT note is not something the
    Retriever scores. The MOC scope is what feeds the `ask` boost.
    """
    from zettel.topic_index import SCOPE_SOURCE, TermSource, sync_topic_index

    sources: list[TermSource] = []
    for chunk in approved:
        try:
            summary = json.loads(chunk.get("summary_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = summary.get("candidates") or []
        if not candidates:
            continue
        best = max(candidates, key=lambda c: c.get("relevance_score") or 0)
        sources.append(TermSource(
            note_id=chunk["chunk_id"],
            label=literature_chunk_wikilink_for_row(citekey, chunk),
            frameworks=tuple(best.get("named_frameworks") or []),
            tags=tuple(str(t) for t in (best.get("tags") or [])),
            thesis=str(best.get("thesis") or ""),
        ))
    sync_topic_index(
        db, SCOPE_SOURCE, source_id, sources, note_path=lit_path,
        targets_are_permanent_notes=False,
    )


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
    llm = get_llm(cfg, "review")
    approved = deduplicate_candidates(cfg, db, idx, llm, candidates)
    logger.info(
        "Dedupe pos-review: %d / %d candidatos aprovados para connect",
        len(approved), len(candidates),
    )
