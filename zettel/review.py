"""Selective approval of granular literature notes (HITL between extract and connect)."""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.index import VectorIndex
from zettel.llm import get_llm
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.time import now_vault_iso
from zettel.vault import (
    build_literature_index_note,
    literature_chunk_filename_for_row,
    literature_index_filename,
    literature_source_dirname,
    parse_frontmatter,
    safe_update_managed_blocks,
    safe_write_note,
)

logger = logging.getLogger(__name__)

# Faixa "baixissima": confianca inclusiva ate este valor.
LOW_CONFIDENCE_MAX = 0.4

# Shown by every path that approves by threshold. Measured against the human gold set
# (#175/#176), `review_confidence` does not rank what a human would keep above what a
# human would discard, so approving by threshold is approving whatever extract accepted.
# The gate stays (ADR-017, 2026-09-13 addendum); the operator is told what it does.
# Numbers live in the ADR, not here, so this line does not go stale when re-measured.
AUTO_APPROVE_UNVALIDATED_WARNING = (
    "Aviso: o limiar de review_confidence nao foi validado contra julgamento humano. "
    "Medido em 2026-09-13 (#176), ele nao separa o que seria guardado do que seria "
    "descartado: aprovar por limiar equivale a aprovar tudo o que o extract aceitou. "
    "Ver ADR-017."
)

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
    if conf <= LOW_CONFIDENCE_MAX:
        return BAND_VERY_LOW
    if conf < limiar:
        return BAND_MEDIUM
    return BAND_HIGH


def filter_chunks_by_band(chunks: list[dict], band: str, limiar: float) -> list[dict]:
    """Filtra chunks pela faixa; band=all devolve a lista inteira."""
    if band == BAND_ALL:
        return list(chunks)
    return [
        c
        for c in chunks
        if chunk_confidence_band(float(c.get("review_confidence") or 0), limiar) == band
    ]


def confidence_band_counts(chunks: list[dict], limiar: float) -> dict[str, int]:
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
    low_max = LOW_CONFIDENCE_MAX
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
    return f"{header}\n\nResumo\n{summary}\n\nTrecho\n{excerpt}"


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

    Returns counts: approved, rejected, skipped, requeued (extract rejections sent
    back to ``pending``), kept_duplicates / discarded_duplicates (dedupe decisions)
    and dedupe_pending (possible duplicates still waiting for a decision).
    """
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("review")
    begin_run(run_id)

    chunks = db.get_chunks_by_status("awaiting_review", source_id=source_id)
    limiar = cfg.literature_review.auto_approve_min_confidence

    if low_confidence_only:
        chunks = [c for c in chunks if (c.get("review_confidence") or 0) < limiar]

    stats = {
        "approved": 0,
        "rejected": 0,
        "skipped": 0,
        "requeued": 0,
        "kept_duplicates": 0,
        "discarded_duplicates": 0,
        "dedupe_pending": 0,
    }

    if auto_approve or not interactive:
        for chunk in chunks:
            conf = chunk.get("review_confidence") or 0
            if conf >= limiar and approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                stats["approved"] += 1
            else:
                stats["skipped"] += 1
        if chunks:
            _dedupe_approved_concepts(cfg, db, idx, source_id)
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db, source_id))
        finish_pipeline_run(db, run_id)
        return stats

    if not (
        chunks or pending_dedupe_concepts(db, source_id) or extract_rejected_chunks(db, source_id)
    ):
        logger.info("Nenhum chunk aguardando review")
        finish_pipeline_run(db, run_id)
        return stats

    from rich.console import Console

    console = Console(stderr=True)
    console.print(f"[yellow]{AUTO_APPROVE_UNVALIDATED_WARNING}[/yellow]")
    try:
        # Possible duplicates left by an earlier non-interactive run come first.
        _resolve_dedupe_pending(console, db, source_id, stats)
        _review_menu(cfg, db, idx, console, chunks, source_id, limiar, stats)
    finally:
        stats["dedupe_pending"] = len(pending_dedupe_concepts(db, source_id))
        finish_pipeline_run(db, run_id)
    return stats


def review_followups(stats: dict[str, int]) -> list[str]:
    """PT-BR next steps a review run leaves behind (empty when there are none)."""
    lines: list[str] = []
    if stats.get("kept_duplicates") or stats.get("discarded_duplicates"):
        lines.append(
            f"Duplicatas: {stats.get('kept_duplicates', 0)} mantida(s), "
            f"{stats.get('discarded_duplicates', 0)} descartada(s)."
        )
    if stats.get("dedupe_pending"):
        lines.append(
            f"{stats['dedupe_pending']} conceito(s) aguardando decisao de duplicata "
            "- rode `zettel review`."
        )
    if stats.get("requeued"):
        lines.append(f"{stats['requeued']} chunk(s) reenfileirado(s) - rode `zettel extract`.")
    return lines


def _review_menu(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    console,
    chunks: list[dict],
    source_id: str | None,
    limiar: float,
    stats: dict[str, int],
) -> None:
    """Menu loop: every action comes back here until ``q`` or nothing is left."""
    from rich.prompt import Prompt

    while True:
        rejected_by_extract = extract_rejected_chunks(db, source_id)
        if not chunks and not rejected_by_extract:
            console.print("[green]Nada mais aguardando review.[/green]")
            return
        _print_queue(cfg, console, chunks, limiar, len(rejected_by_extract))
        mode = Prompt.ask(
            "Modo",
            choices=["a", "d", "r", "x", "q"],
            default="a" if chunks else "q",
            console=console,
        )
        if mode == "q":
            return
        if mode == "x":
            _requeue_extract_rejected(console, db, rejected_by_extract, stats)
            continue
        if not chunks:
            console.print("[dim]Nenhum draft aguardando review.[/dim]")
            continue
        if mode == "d":
            chunks = _reject_by_band(cfg, db, idx, console, chunks, limiar, stats)
            continue

        handled: set[str] = set()
        if mode == "a":
            for chunk in chunks:
                if (chunk.get("review_confidence") or 0) >= limiar and approve_chunk(
                    cfg, db, idx, chunk["chunk_id"]
                ):
                    stats["approved"] += 1
                    handled.add(chunk["chunk_id"])
            console.print(
                f"[green]Aprovados {len(handled)} (>= limiar); "
                f"{len(chunks) - len(handled)} continuam aguardando review.[/green]"
            )
        else:  # mode == "r"
            for chunk in chunks:
                conf = chunk.get("review_confidence") or 0
                console.print()
                console.print(format_review_item(chunk), markup=False)
                choice = ask_review_decision(console, conf=conf, limiar=limiar)
                if choice == "sair":
                    break
                if choice == "aprovar" and approve_chunk(cfg, db, idx, chunk["chunk_id"]):
                    stats["approved"] += 1
                    handled.add(chunk["chunk_id"])
                elif choice == "rejeitar" and reject_chunk(cfg, db, idx, chunk["chunk_id"]):
                    stats["rejected"] += 1
                    handled.add(chunk["chunk_id"])
                else:
                    stats["skipped"] += 1

        chunks = [c for c in chunks if c["chunk_id"] not in handled]
        if handled:
            _dedupe_approved_concepts(cfg, db, idx, source_id)
            _resolve_dedupe_pending(console, db, source_id, stats)


def _print_queue(
    cfg: AppConfig, console, chunks: list[dict], limiar: float, n_extract_rejected: int
) -> None:
    from rich.table import Table

    if chunks:
        sample = chunks[: cfg.literature_review.batch_sample_size]
        table = Table(title=f"Review de LIT ({len(chunks)} aguardando)")
        table.add_column("#")
        table.add_column("Chunk")
        table.add_column("Pagina")
        table.add_column("Conf")
        table.add_column("Resumo")
        for i, c in enumerate(sample, 1):
            table.add_row(
                str(i),
                c["chunk_id"][-24:],
                str(c.get("page_in_book") or c.get("page_in_file") or "?"),
                f"{(c.get('review_confidence') or 0):.2f}",
                _summary_from_chunk(c)[:200],
            )
        console.print(table)
    report = format_confidence_report(confidence_band_counts(chunks, limiar), limiar)
    console.print(f"[cyan]{report}[/cyan]")
    # Not a confidence band: these chunks never produced a draft.
    console.print(f"[cyan]  Rejeitados pelo extract (sem draft): {n_extract_rejected}[/cyan]")
    console.print(
        "[cyan]Comandos: a=aprovar >= limiar, d=reprovar (todos ou por faixa), "
        "r=revisar um a um, x=rejeitados pelo extract, q=sair[/cyan]"
    )


def _reject_by_band(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    console,
    chunks: list[dict],
    limiar: float,
    stats: dict[str, int],
) -> list[dict]:
    """Batch reject (all or one band). Returns the drafts still waiting."""
    from rich.prompt import Prompt

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
        return chunks

    targets = filter_chunks_by_band(chunks, scope, limiar)
    label = _BAND_LABELS[scope]
    if not targets:
        console.print(f"[dim]Nenhum draft na faixa {label}.[/dim]")
        return chunks
    confirm = Prompt.ask(
        f"Confirmar rejeicao de {len(targets)} drafts ({label})?",
        choices=["s", "n"],
        default="n",
        console=console,
    )
    if confirm != "s":
        console.print("[dim]Rejeicao em lote cancelada.[/dim]")
        return chunks

    rejected_ids: set[str] = set()
    for chunk in targets:
        if reject_chunk(cfg, db, idx, chunk["chunk_id"]):
            stats["rejected"] += 1
            rejected_ids.add(chunk["chunk_id"])
        else:
            stats["skipped"] += 1
    console.print(f"[green]Rejeitados {len(rejected_ids)} ({label}).[/green]")
    return [c for c in chunks if c["chunk_id"] not in rejected_ids]


# ── Chunks the extract itself rejected ───────────────────────────────


def extract_rejected_chunks(db: StateDB, source_id: str | None = None) -> list[dict]:
    """Chunks the extract rejected: ``status=rejected`` with no candidate.

    A reviewer's rejection keeps the candidates that were on the draft, so an
    empty candidate list is what tells the two apart. These chunks never had a
    draft (title pages, affiliations, references...); they are listed so the
    reviewer can send a wrongly rejected one back to extract.
    """
    return [
        c
        for c in db.get_chunks_by_status("rejected", source_id=source_id)
        if not _load_json(c.get("summary_json")).get("candidates")
    ]


def requeue_extract_rejected(db: StateDB, chunk_ids: list[str]) -> int:
    """Send extract rejections back to ``pending``, dropping the cached verdict."""
    return sum(db.reset_chunk_to_pending(cid, drop_llm_cache=True) for cid in chunk_ids)


def _requeue_extract_rejected(
    console, db: StateDB, rejected: list[dict], stats: dict[str, int]
) -> None:
    from rich.prompt import Prompt
    from rich.table import Table

    if not rejected:
        console.print("[dim]Nenhum chunk rejeitado pelo extract.[/dim]")
        return
    table = Table(title=f"Rejeitados pelo extract ({len(rejected)})")
    table.add_column("#")
    table.add_column("Pagina")
    table.add_column("Secao")
    table.add_column("Categoria")
    table.add_column("Motivo")
    for i, c in enumerate(rejected, 1):
        summary = _load_json(c.get("summary_json"))
        table.add_row(
            str(i),
            str(c.get("page_in_book") or c.get("page_in_file") or "-"),
            (c.get("section_path") or "")[:50],
            str(summary.get("rejection_category") or "-"),
            str(summary.get("rejection_reason") or "")[:160],
        )
    console.print(table)

    raw = Prompt.ask(
        r"Re-extrair quais? \[numeros separados por virgula, t=todos, c=cancelar\]",
        default="c",
        console=console,
    )
    selected = parse_selection(raw, len(rejected))
    if not selected:
        console.print("[dim]Nada reenfileirado.[/dim]")
        return
    confirm = Prompt.ask(
        f"Reenfileirar {len(selected)} chunk(s) para o extract?",
        choices=["s", "n"],
        default="n",
        console=console,
    )
    if confirm != "s":
        console.print("[dim]Nada reenfileirado.[/dim]")
        return
    n = requeue_extract_rejected(db, [rejected[i]["chunk_id"] for i in selected])
    stats["requeued"] += n
    console.print(f"[green]{n} chunk(s) de volta a pending. Rode `zettel extract`.[/green]")


def parse_selection(raw: str, total: int) -> list[int]:
    """``"1, 3"`` -> ``[0, 2]``; ``t`` selects all; anything invalid is ignored."""
    key = (raw or "").strip().lower()
    if key in ("t", "todos"):
        return list(range(total))
    picked: list[int] = []
    for part in key.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= total and int(part) - 1 not in picked:
            picked.append(int(part) - 1)
    return picked


# ── Possible duplicates (same source) ────────────────────────────────


def pending_dedupe_concepts(db: StateDB, source_id: str | None = None) -> list[dict]:
    """Concepts dedupe flagged as possibly redundant, waiting for the reviewer."""
    rows = db.get_concepts_by_status("dedupe_pending")
    return [r for r in rows if not source_id or r["source_id"] == source_id]


def keep_duplicate(db: StateDB, concept_id: str) -> None:
    """Keep a flagged concept: it goes to ``connect`` and dedupe never flags it again."""
    concept = db.get_concept(concept_id) or {}
    dedupe = _load_json(concept.get("dedupe_json"))
    dedupe["override"] = True
    db.set_concept_dedupe(concept_id, "approved", dedupe)


def discard_duplicate(db: StateDB, concept_id: str) -> None:
    db.set_concept_dedupe(concept_id, "duplicate")


def format_dedupe_item(db: StateDB, concept: dict) -> str:
    """Card PT-BR: the new thesis next to what would make it redundant."""
    from zettel.vault import normalize_note_id

    cand = _load_json(concept.get("candidate_json"))
    dedupe = _load_json(concept.get("dedupe_json"))
    lines = [
        f"Tese nova: {cand.get('thesis') or '?'}",
        f"Definicao: {cand.get('definition') or '-'}",
    ]
    note_id = normalize_note_id(str(dedupe.get("target_note_id") or ""))
    other = db.get_concept(dedupe["duplicate_of"]) if dedupe.get("duplicate_of") else None
    if note_id:
        note = db.get_note(note_id) or {}
        lines.append(f"Nota existente: {note.get('title') or note_id} ({note_id})")
    elif other:
        other_thesis = _load_json(other.get("candidate_json")).get("thesis")
        lines.append(f"Candidato do mesmo lote: {other_thesis or other['concept_id']}")
    lines.append(f"Motivo: {dedupe.get('reason') or '-'}")
    return "\n".join(lines)


def _resolve_dedupe_pending(
    console, db: StateDB, source_id: str | None, stats: dict[str, int]
) -> None:
    """Ask keep / discard / skip for every possible duplicate of this source."""
    from rich.prompt import Prompt

    pending = pending_dedupe_concepts(db, source_id)
    if not pending:
        return
    console.print(
        f"[yellow]{len(pending)} conceito(s) parecem repetir outra nota da mesma fonte. "
        "Nada e descartado sem a sua decisao.[/yellow]"
    )
    for concept in pending:
        console.print()
        console.print(format_dedupe_item(db, concept), markup=False)
        choice = Prompt.ask(
            r"Decisao \[m=manter/d=descartar/p=pular\]",
            choices=["m", "d", "p"],
            default="p",
            show_choices=False,
            console=console,
        )
        if choice == "m":
            keep_duplicate(db, concept["concept_id"])
            stats["kept_duplicates"] += 1
        elif choice == "d":
            discard_duplicate(db, concept["concept_id"])
            stats["discarded_duplicates"] += 1


def _load_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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


def approve_chunk(cfg: AppConfig, db: StateDB, idx: VectorIndex, chunk_id: str) -> bool:
    """Move draft to 20_Literature and promote concepts.

    The note is persisted to the vault and to SQLite for audit, but is **not**
    embedded: nothing ever queried the old ``literature_notes`` collection, and
    the LIT body is derived from a chunk whose text is already indexed.
    """
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

    dest_dir = cfg.vault_path / "20_Literature" / literature_source_dirname(citekey)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / literature_chunk_filename_for_row(citekey, chunk)

    if draft_path and draft_path.exists():
        content = draft_path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(content)
        meta["status"] = "approved"
        meta["updated_at"] = now_vault_iso(cfg.vault_timezone)
        safe_write_note(dest_path, meta, body)
        with contextlib.suppress(OSError):
            draft_path.unlink()
    else:
        # Rebuild from summary_json if draft missing
        summary_data: dict[str, Any] = {}
        if chunk.get("summary_json"):
            with contextlib.suppress(json.JSONDecodeError):
                summary_data = json.loads(chunk["summary_json"])
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
            vault_timezone=cfg.vault_timezone,
        )
        safe_write_note(dest_path, meta, body)

    excerpt = (chunk.get("text") or "").strip() or "_Trecho nao disponivel._"
    safe_update_managed_blocks(
        dest_path, {"auto-source-excerpt": excerpt}, vault_timezone=cfg.vault_timezone
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
    sync_lit_permanent_links(cfg, db, [chunk_id])
    logger.info("[NOTE=%s] APPROVED → persistido no cofre e em SQLite", dest_path)
    return True


def reject_chunk(cfg: AppConfig, db: StateDB, idx: VectorIndex, chunk_id: str) -> bool:
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

    Literature notes are not embedded, so there is no vector cleanup for them.

    When ``compact`` is True and something was deleted, runs SQLite VACUUM on
    ``state.db`` and ``chroma.sqlite3`` to reclaim disk (no logical data change).

    Does not touch permanent notes, MOCs, or approved/persisted literature.
    """
    rows = db.get_chunks_by_status("rejected", source_id=source_id)
    if not rows:
        return {
            "chunks": 0,
            "compacted": False,
            "state_mb_before": 0.0,
            "state_mb_after": 0.0,
            "chroma_mb_before": 0.0,
            "chroma_mb_after": 0.0,
        }

    chunk_ids = [r["chunk_id"] for r in rows]

    removed_sqlite = db.delete_chunks(chunk_ids)
    idx.delete_chunks(chunk_ids)

    logger.info("Purge rejected: %d chunks SQLite + Chroma", removed_sqlite)

    result: dict[str, int | float | bool] = {
        "chunks": removed_sqlite,
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
            result["state_mb_before"],
            result["state_mb_after"],
            result["chroma_mb_before"],
            result["chroma_mb_after"],
        )
    return result


def _refresh_literature_index(cfg: AppConfig, db: StateDB, source_id: str) -> None:
    """Create the literature index if missing, then rewrite its chapter map.

    The chapter map is the single place approved LIT notes are listed, so it is
    refreshed on every approval instead of waiting for `summarize`/`connect`.
    """
    from zettel.summarize import refresh_chapter_map

    source = db.get_source(source_id)
    if not source:
        return

    lit_path = (
        cfg.vault_path
        / "20_Literature"
        / literature_index_filename(source["citekey"], source["title"])
    )
    if not lit_path.exists():
        meta, body = build_literature_index_note(
            source_id,
            source["citekey"],
            source["title"],
            vault_timezone=cfg.vault_timezone,
        )
        safe_write_note(lit_path, meta, body)
    refresh_chapter_map(cfg, db, source_id)


LIT_PERMANENT_BLOCK = "auto-lit-permanent"
_LIT_PERMANENT_HEADING = "## Notas permanentes geradas"


def sync_lit_permanent_links(cfg: AppConfig, db: StateDB, chunk_ids: list[str]) -> None:
    """Rewrite the ``auto-lit-permanent`` block on each chunk's approved LIT note.

    Lists the ZTL written from the chunk's concepts, so a LIT shows what it
    became — its ``literature_id`` and the ZTL's ``note_id`` are different
    ULIDs and the titles rarely match. Deterministic and free (SQLite only);
    called on approval (empty placeholder) and by ``connect`` after it writes.
    Drafts in review are skipped: only an approved LIT can have produced a note.
    """
    from zettel.vault import permanent_wikilink, upsert_managed_block

    for chunk_id in dict.fromkeys(chunk_ids):
        chunk = db.get_chunk(chunk_id)
        if not chunk or chunk.get("status") not in ("approved", "persisted"):
            continue
        path = Path(chunk.get("literature_note_path") or "")
        if not path.is_file():
            continue
        links = [
            f"- {permanent_wikilink(n['note_id'], n.get('title') or '', path=n.get('path'))}"
            for n in db.get_notes_for_chunk(chunk_id)
        ]
        inner = "\n".join(links) if links else "_Nenhuma nota permanente gerada ainda._"
        content = path.read_text(encoding="utf-8")
        if f"zettel:{LIT_PERMANENT_BLOCK}:start" not in content:
            # This function owns the section, so the LIT builders never scaffold it.
            content = content.rstrip() + f"\n\n{_LIT_PERMANENT_HEADING}\n"
            path.write_text(upsert_managed_block(content, LIT_PERMANENT_BLOCK, ""), "utf-8")
        safe_update_managed_blocks(
            path, {LIT_PERMANENT_BLOCK: inner}, vault_timezone=cfg.vault_timezone
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
        candidates.append(
            {
                "concept_id": row["concept_id"],
                "source_id": row["source_id"],
                "chunk_id": row["chunk_id"],
                "candidate": cand,
                "dedupe_override": bool(_load_json(row.get("dedupe_json")).get("override")),
            }
        )

    if not candidates:
        return

    from zettel.extractor import deduplicate_candidates

    llm = get_llm(cfg, "review")
    approved = deduplicate_candidates(cfg, db, idx, llm, candidates)
    logger.info(
        "Dedupe pos-review: %d / %d candidatos aprovados para connect",
        len(approved),
        len(candidates),
    )
