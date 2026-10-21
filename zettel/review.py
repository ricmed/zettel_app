"""Selective approval of granular literature notes (HITL between extract and connect)."""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.decision.shadow import label_dedupe
from zettel.hashing import quote_is_grounded
from zettel.index import VectorIndex
from zettel.progress import ProgressObserver, report
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.time import now_vault_iso
from zettel.vault import (
    build_literature_index_note,
    literature_chunk_filename_for_row,
    literature_index_filename,
    literature_source_dirname,
    parse_frontmatter,
    replace_literature_candidates,
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

_EXTRACT_DECISION_ALIASES = {
    "e": "reenfileirar",
    "reenfileirar": "reenfileirar",
    "f": "processar",
    "processar": "processar",
    "d": "apagar",
    "apagar": "apagar",
    "descartar": "apagar",
    "m": "manter",
    "manter": "manter",
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


def highlight_spans(text: str, quotes: list[str]) -> list[tuple[str, bool]]:
    """Split ``text`` into plain and highlighted slices around each quote."""
    spans: list[tuple[int, int]] = []
    for quote in quotes:
        needle = (quote or "").strip()
        # A one-letter anchor would split an unrelated word. Real anchors are sentences.
        if len(needle) < 8:
            continue
        start = text.find(needle)
        if start < 0:
            continue
        spans.append((start, start + len(needle)))
    if not spans:
        return [(text, False)] if text else []
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    parts: list[tuple[str, bool]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            parts.append((text[cursor:start], False))
        parts.append((text[start:end], True))
        cursor = end
    if cursor < len(text):
        parts.append((text[cursor:], False))
    return parts


def _marked_excerpt(text: str, quotes: list[str]) -> str:
    parts = highlight_spans(text, quotes)
    return "".join(f">>>{part}<<<" if marked else part for part, marked in parts)


def ordered_review_concepts(db: StateDB, chunk: dict) -> list[dict]:
    """Concepts still awaiting a decision, in the order the draft listed them."""
    concepts = [
        row
        for row in db.get_concepts_for_chunk(chunk["chunk_id"])
        if row.get("status") == "awaiting_review"
    ]
    wanted = [
        str(item.get("thesis") or "")
        for item in (_load_json(chunk.get("summary_json")).get("candidates") or [])
        if isinstance(item, dict)
    ]
    used: set[str] = set()
    ordered: list[dict] = []
    for thesis in wanted:
        for concept in concepts:
            if concept["concept_id"] in used:
                continue
            if _load_json(concept.get("candidate_json")).get("thesis") == thesis:
                ordered.append(concept)
                used.add(concept["concept_id"])
                break
    ordered.extend(concept for concept in concepts if concept["concept_id"] not in used)
    return ordered


def format_candidate_block(concept: dict, *, index: int, total: int) -> str:
    """One candidate: the fields that become the permanent note."""
    cand = _load_json(concept.get("candidate_json"))
    relevance = cand.get("relevance_score")
    score = f"  relevancia {relevance}/5" if relevance is not None else ""
    lines = [
        f"Candidato {index}/{total}  {concept['concept_id']}{score}",
        "",
        "Tese",
        str(cand.get("thesis") or "").strip() or "_Sem tese._",
        "",
        "Definicao",
        str(cand.get("definition") or "").strip() or "_Sem definicao._",
        "",
        "Intuicao",
        str(cand.get("intuition") or "").strip() or "_Sem intuicao._",
        "",
        "Limites",
        str(cand.get("limits") or "").strip() or "_Sem limites._",
        "",
        "Ancora",
        str(cand.get("anchor_quote") or "").strip() or "_Sem ancora._",
    ]
    return "\n".join(lines)


def format_review_item(chunk: dict, concepts: list[dict] | None = None) -> str:
    """Card PT-BR do review um-a-um: candidatos, ancora destacada e trecho."""
    conf = float(chunk.get("review_confidence") or 0)
    page = chunk.get("page_in_book") or chunk.get("page_in_file") or "?"
    section = (chunk.get("section_path") or "").strip()
    header = f"{chunk['chunk_id']} conf={conf:.2f}  p.{page}"
    if section:
        header += f"  {section}"
    summary = _summary_from_chunk(chunk) or "_Sem resumo._"
    excerpt = (chunk.get("text") or "").strip() or "_Trecho nao disponivel._"
    rows = concepts if concepts is not None else []
    quotes = [
        str(_load_json(concept.get("candidate_json")).get("anchor_quote") or "") for concept in rows
    ]
    blocks = [header, "", "Resumo", summary, ""]
    dropped = _load_json(chunk.get("summary_json")).get("rejected_candidates") or []
    if dropped:
        blocks.extend(["Descartados pelo filtro", ""])
        for item in dropped:
            if not isinstance(item, dict):
                continue
            blocks.extend(
                [
                    str(item.get("thesis") or "").strip() or "_Sem tese._",
                    str(item.get("reason") or "").strip() or "_Sem motivo._",
                    "",
                ]
            )
    if rows:
        for index, concept in enumerate(rows, 1):
            blocks.extend([format_candidate_block(concept, index=index, total=len(rows)), ""])
    blocks.extend(["Trecho", _marked_excerpt(excerpt, quotes)])
    return "\n".join(blocks)


def normalize_reject_scope(raw: str) -> str | None:
    """Mapeia atalho/palavra para faixa de rejeicao ou cancel."""
    key = (raw or "").strip().lower()
    return _REJECT_SCOPE_ALIASES.get(key)


def normalize_review_decision(raw: str) -> str | None:
    """Mapeia atalho ou palavra completa para aprovar/rejeitar/pular/sair."""
    key = (raw or "").strip().lower()
    return _DECISION_ALIASES.get(key)


def normalize_extract_decision(raw: str) -> str | None:
    """Mapeia atalho para reenfileirar/processar/apagar/manter/pular/sair."""
    key = (raw or "").strip().lower()
    return _EXTRACT_DECISION_ALIASES.get(key)


def chunk_has_review_defect(chunk: dict) -> bool:
    """True when the filter dropped a candidate or a survivor is missing a field.

    Those drafts are the ones a threshold would hide: the score already noticed
    something, and they belong at the front of the queue (#268).
    """
    summary = _load_json(chunk.get("summary_json"))
    if summary.get("rejected_candidates"):
        return True
    for cand in summary.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        if (
            not str(cand.get("intuition") or "").strip()
            or not str(cand.get("limits") or "").strip()
        ):
            return True
    return False


def review_queue_key(chunk: dict) -> tuple:
    """Defects first, then ascending confidence, then chunk id."""
    conf = chunk.get("review_confidence")
    conf_key = float(conf) if isinstance(conf, (int, float)) else 0.0
    return (
        0 if chunk_has_review_defect(chunk) else 1,
        conf_key,
        str(chunk.get("chunk_id") or ""),
    )


def ask_review_decision(console, *, conf: float, limiar: float) -> str:
    """Prompt HITL um-a-um. Enter pula, qualquer que seja a confianca (#268)."""
    from rich.prompt import Prompt

    del conf, limiar  # a confianca aparece no cartao; nao escolhe o default
    default = "p"
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
    source_ids: list[str] | None = None,
    auto_approve: bool = False,
    interactive: bool = True,
    low_confidence_only: bool = False,
    event_mode: str | None = None,
    surface: str = "cli",
) -> dict[str, int]:
    """Approve/reject literature drafts awaiting review.

    Returns counts: approved, rejected, skipped, requeued (extract rejections sent
    back to ``pending``), forced (the same queue marked ``force_extract``),
    discarded, kept_duplicates / discarded_duplicates (dedupe decisions)
    and dedupe_pending (possible duplicates still waiting for a decision).

    ``source_ids`` limits the queue to that set, including an empty set (``run-all``
    passes the sources this harvest actually wrote). ``None`` keeps the old
    behaviour: every source, or just ``source_id`` when that is set.
    """
    from zettel.llm import LLMUnavailableError
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("review")
    begin_run(run_id)

    if source_ids is not None:
        allowed = set(source_ids)
        chunks = [
            chunk
            for chunk in db.get_chunks_by_status("awaiting_review")
            if chunk.get("source_id") in allowed
        ]
    else:
        chunks = db.get_chunks_by_status("awaiting_review", source_id=source_id)
    limiar = cfg.literature_review.auto_approve_min_confidence

    def dedupe_waiting() -> int:
        return len(pending_dedupe_concepts(db, source_id, source_ids=source_ids))

    if low_confidence_only:
        chunks = [c for c in chunks if (c.get("review_confidence") or 0) < limiar]

    stats = {
        "approved": 0,
        "rejected": 0,
        "skipped": 0,
        "requeued": 0,
        "forced": 0,
        "discarded": 0,
        "kept_duplicates": 0,
        "discarded_duplicates": 0,
        "dedupe_pending": 0,
        "approved_manual": 0,
        "approved_threshold": 0,
    }

    if low_confidence_only and auto_approve:
        raise ValueError(
            "--low-confidence-only com --yes ou --auto-approve nao aprova nada: "
            "o filtro fica abaixo do limiar e a aprovacao so pega o que esta acima."
        )

    if auto_approve:
        persisted: list[str] = []
        for chunk in chunks:
            conf = chunk.get("review_confidence") or 0
            if conf >= limiar and approve_chunk(
                cfg,
                db,
                idx,
                chunk["chunk_id"],
                mode=event_mode or "threshold",
                surface=surface,
                run_id=run_id,
                refresh_source=False,
            ):
                stats["approved"] += 1
                stats["approved_threshold"] += 1
                persisted.append(chunk["chunk_id"])
            else:
                stats["skipped"] += 1
        refresh_approved_literature(cfg, db, persisted)
        try:
            if chunks:
                scoped = source_ids if source_ids is not None else [source_id]
                for sid in dict.fromkeys(scoped):
                    _dedupe_approved_concepts(cfg, db, idx, sid)
        except LLMUnavailableError:
            finish_pipeline_run(db, run_id, status="failed")
            raise
        stats["dedupe_pending"] = dedupe_waiting()
        stats["pending_review"] = stats["skipped"]
        finish_pipeline_run(db, run_id)
        return stats

    if not interactive:
        # A full pipeline used to treat "no TTY" as approval. The score does not
        # separate keep from discard, so drafts stay in the queue unless the
        # caller passed auto_approve (issue #254).
        stats["skipped"] = len(chunks)
        stats["pending_review"] = len(chunks)
        stats["dedupe_pending"] = dedupe_waiting()
        finish_pipeline_run(db, run_id)
        return stats

    if not (chunks or dedupe_waiting() or extract_rejected_chunks(db, source_id)):
        logger.info("Nenhum chunk aguardando review")
        finish_pipeline_run(db, run_id)
        return stats

    from rich.console import Console

    console = Console(stderr=True)
    console.print(f"[yellow]{AUTO_APPROVE_UNVALIDATED_WARNING}[/yellow]")
    try:
        # Possible duplicates left by an earlier non-interactive run come first.
        _resolve_dedupe_pending(console, db, source_id, stats, source_ids=source_ids)
        _review_menu(
            cfg, db, idx, console, chunks, source_id, limiar, stats, run_id=run_id, surface=surface
        )
    except LLMUnavailableError:
        stats["dedupe_pending"] = dedupe_waiting()
        finish_pipeline_run(db, run_id, status="failed")
        raise
    stats["dedupe_pending"] = dedupe_waiting()
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
    if stats.get("forced"):
        lines.append(
            f"{stats['forced']} chunk(s) marcado(s) para extracao obrigatoria "
            "- rode `zettel extract`."
        )
    if stats.get("discarded"):
        lines.append(f"{stats['discarded']} chunk(s) apagado(s) do banco.")
    if stats.get("draft_delete_failures"):
        lines.append(f"{stats['draft_delete_failures']} draft(s) rejeitado(s) continuam no disco.")
    if stats.get("pending_review"):
        lines.append(f"{stats['pending_review']} draft(s) aguardando revisao.")
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
    *,
    run_id: int | None = None,
    surface: str = "cli",
) -> None:
    """Menu loop: every action comes back here until ``q`` or nothing is left."""
    from rich.prompt import Prompt

    dismissed_extract: set[str] = set()
    while True:
        rejected_by_extract = extract_rejected_chunks(db, source_id)
        if not chunks and not rejected_by_extract:
            console.print("[green]Nada mais aguardando review.[/green]")
            return
        chunks.sort(key=review_queue_key)
        _print_queue(cfg, console, chunks, limiar, rejected_by_extract)
        mode = Prompt.ask(
            "Modo",
            choices=["a", "d", "r", "x", "q"],
            default="r" if chunks else "q",
            console=console,
        )
        if mode == "q":
            return
        if mode == "x":
            _requeue_extract_rejected(
                console, db, idx, rejected_by_extract, stats, dismissed_extract
            )
            continue
        if not chunks:
            console.print("[dim]Nenhum draft aguardando review.[/dim]")
            continue
        if mode == "d":
            chunks = _reject_by_band(
                cfg, db, idx, console, chunks, limiar, stats, run_id=run_id, surface=surface
            )
            continue

        handled: set[str] = set()
        if mode == "a":
            above = [c for c in chunks if (c.get("review_confidence") or 0) >= limiar]
            confirm = Prompt.ask(
                f"Aprovar {len(above)} drafts sem ler? {AUTO_APPROVE_UNVALIDATED_WARNING}",
                choices=["s", "n"],
                default="n",
                console=console,
            )
            if confirm != "s":
                console.print("[dim]Aprovacao em lote cancelada.[/dim]")
                continue
            for chunk in above:
                if approve_chunk(
                    cfg,
                    db,
                    idx,
                    chunk["chunk_id"],
                    mode="threshold",
                    surface=surface,
                    run_id=run_id,
                    refresh_source=False,
                ):
                    stats["approved"] += 1
                    stats["approved_threshold"] += 1
                    handled.add(chunk["chunk_id"])
            refresh_approved_literature(cfg, db, list(handled))
            console.print(
                f"[green]Aprovados {len(handled)} (>= limiar); "
                f"{len(chunks) - len(handled)} continuam aguardando review.[/green]"
            )
        else:  # mode == "r"
            for chunk in chunks:
                conf = chunk.get("review_confidence") or 0
                concepts = ordered_review_concepts(db, chunk)
                console.print()
                console.print(format_review_item(chunk, concepts), markup=False)
                if not concepts:
                    choice = ask_review_decision(console, conf=conf, limiar=limiar)
                    if choice == "sair":
                        break
                    if choice == "aprovar" and approve_chunk(
                        cfg,
                        db,
                        idx,
                        chunk["chunk_id"],
                        mode="manual",
                        surface=surface,
                        run_id=run_id,
                    ):
                        stats["approved"] += 1
                        stats["approved_manual"] += 1
                        handled.add(chunk["chunk_id"])
                    elif choice == "rejeitar" and reject_chunk(
                        cfg,
                        db,
                        idx,
                        chunk["chunk_id"],
                        mode="manual",
                        surface=surface,
                        run_id=run_id,
                        stats=stats,
                    ):
                        stats["rejected"] += 1
                        handled.add(chunk["chunk_id"])
                    else:
                        stats["skipped"] += 1
                    continue
                decisions: dict[str, str] = {}
                stopped = False
                for index, concept in enumerate(concepts, 1):
                    thesis = str(_load_json(concept.get("candidate_json")).get("thesis") or "")
                    console.print(f"Decidindo {index}/{len(concepts)}: {thesis[:80]}")
                    choice = ask_review_decision(console, conf=conf, limiar=limiar)
                    if choice == "sair":
                        stopped = True
                        break
                    if choice == "aprovar":
                        decisions[concept["concept_id"]] = "approve"
                    elif choice == "rejeitar":
                        decisions[concept["concept_id"]] = "reject"
                if decisions:
                    outcome = decide_concepts(
                        cfg, db, idx, decisions, mode="manual", surface=surface, run_id=run_id
                    )
                    if outcome["persisted"]:
                        stats["approved"] += 1
                        stats["approved_manual"] += 1
                        handled.add(chunk["chunk_id"])
                    elif outcome["rejected_chunks"]:
                        stats["rejected"] += 1
                        handled.add(chunk["chunk_id"])
                    stats["draft_delete_failures"] = stats.get("draft_delete_failures", 0) + len(
                        outcome.get("draft_delete_failures") or []
                    )
                    for concept_id in outcome["refused"]:
                        console.print(
                            f"[yellow]Ancora editada de {concept_id} nao esta no trecho; "
                            "o candidato nao foi aprovado.[/yellow]"
                        )
                if stopped:
                    break

        chunks = [c for c in chunks if c["chunk_id"] not in handled]
        if handled:
            _dedupe_approved_concepts(cfg, db, idx, source_id)
            _resolve_dedupe_pending(console, db, source_id, stats)


def _print_queue(
    cfg: AppConfig, console, chunks: list[dict], limiar: float, extract_rejected: list[dict]
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
    origins = extract_rejection_origin_counts(extract_rejected)
    console.print(
        "[cyan]  Rejeitados pelo extract (sem draft): "
        f"{origins['total']} (modelo: {origins['modelo']}, filtro: {origins['filtro']})[/cyan]"
    )
    console.print(
        "[cyan]Comandos: a=aprovar >= limiar, d=reprovar (todos ou por faixa), "
        "r=revisar um a um, x=rejeitados pelo extract (um a um), q=sair[/cyan]"
    )


def _reject_by_band(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    console,
    chunks: list[dict],
    limiar: float,
    stats: dict[str, int],
    *,
    run_id: int | None = None,
    surface: str = "cli",
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
        if reject_chunk(
            cfg,
            db,
            idx,
            chunk["chunk_id"],
            mode="band",
            surface=surface,
            run_id=run_id,
            stats=stats,
        ):
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
        if is_extract_rejected(c)
    ]


def is_extract_rejected(chunk: dict) -> bool:
    """A rejected chunk with no candidate: the extract's verdict, not a reviewer's."""
    return not _load_json(chunk.get("summary_json")).get("candidates")


def requeue_extract_rejected(
    db: StateDB,
    chunk_ids: list[str],
    *,
    mode: str = "manual",
    surface: str = "cli",
    run_id: int | None = None,
    observer: ProgressObserver | None = None,
) -> int:
    """Send extract rejections back to ``pending``, dropping the cached verdict.

    This is a second opinion: the next extract may reject the chunk again.
    It does not set ``force_extract``. ``observer`` hears item k once it moved.
    """
    moved = 0
    total = len(chunk_ids)
    for number, cid in enumerate(chunk_ids, 1):
        chunk = db.get_chunk(cid)
        if db.reset_chunk_to_pending(cid, drop_llm_cache=True):
            log_review_event(
                db, chunk, decision="requeue", mode=mode, surface=surface, run_id=run_id
            )
            moved += 1
        _report_item(observer, "Reenfileirado", cid, number, total)
    return moved


def _report_item(
    observer: ProgressObserver | None, verb: str, chunk_id: str, number: int, total: int
) -> None:
    """Progress for a batch item that is already done, never one about to start (#313)."""
    report(
        observer,
        "review",
        f"{verb} {number}/{total}.",
        current_item=chunk_id[-18:],
        current_index=number,
        total_items=total,
    )


def force_extract_rejected(
    db: StateDB,
    chunk_ids: list[str],
    *,
    mode: str = "manual",
    surface: str = "cli",
    run_id: int | None = None,
    observer: ProgressObserver | None = None,
) -> int:
    """Send extract rejections back to ``pending`` with ``force_extract`` set.

    The next extract must produce a draft: Prompt 1 is told to ignore the
    selectivity locks. A draft or a reviewer rejection is not eligible.
    ``observer`` hears item k once it moved.
    """
    forced = 0
    eligible = requeueable_extract_ids(db, chunk_ids)
    for number, cid in enumerate(eligible, 1):
        chunk = db.get_chunk(cid)
        if not chunk:
            continue
        summary = _load_json(chunk.get("summary_json"))
        summary["force_extract"] = True
        db.update_chunk_review(
            cid,
            summary_json=json.dumps(summary, ensure_ascii=False),
        )
        if db.reset_chunk_to_pending(cid, drop_llm_cache=True):
            log_review_event(db, chunk, decision="force", mode=mode, surface=surface, run_id=run_id)
            forced += 1
        _report_item(observer, "Extração obrigatória marcada", cid, number, len(eligible))
    return forced


def discard_extract_rejected(
    db: StateDB,
    idx: VectorIndex,
    chunk_ids: list[str],
    *,
    mode: str = "manual",
    surface: str = "cli",
    run_id: int | None = None,
    observer: ProgressObserver | None = None,
) -> int:
    """Hard-delete extract rejections from SQLite and the chunks index.

    A draft or a reviewer rejection is not in :func:`requeueable_extract_ids`,
    so this cannot remove it. No VACUUM: reclaiming disk stays on ``purge-rejected``.
    The delete is one batch, so ``observer`` hears that it started, with no count.
    """
    eligible = requeueable_extract_ids(db, chunk_ids)
    if not eligible:
        return 0
    report(
        observer,
        "review",
        f"Apagando {len(eligible)} item(ns) do SQLite e do índice vetorial.",
    )
    for cid in eligible:
        log_review_event(
            db,
            db.get_chunk(cid),
            decision="discard",
            mode=mode,
            surface=surface,
            run_id=run_id,
        )
    db.delete_chunks(eligible)
    idx.delete_chunks(eligible)
    return sum(db.get_chunk(cid) is None for cid in eligible)


def requeueable_extract_ids(db: StateDB, chunk_ids: list[str]) -> list[str]:
    """Ids in ``chunk_ids`` that the extract rejected (no candidates).

    A reviewer rejection and an awaiting draft are not in this set, so a web
    ``requeue`` cannot send them back to extract.
    """
    allowed = {c["chunk_id"] for c in extract_rejected_chunks(db)}
    return [cid for cid in chunk_ids if cid in allowed]


def extract_rejection_origin(chunk: dict) -> str:
    """``filtro`` when the model accepted and the deterministic filter dropped everyone.

    ``modelo`` when Prompt 1 itself returned ``chunk_status=rejected``.
    """
    summary = _load_json(chunk.get("summary_json"))
    if summary.get("rejection_category") == "filtered":
        return "filtro"
    if summary.get("chunk_status") == "accepted" and summary.get("rejected_candidates"):
        return "filtro"
    return "modelo"


def extract_rejection_origin_counts(chunks: list[dict]) -> dict[str, int]:
    modelo = filtro = 0
    for chunk in chunks:
        if extract_rejection_origin(chunk) == "filtro":
            filtro += 1
        else:
            modelo += 1
    return {"modelo": modelo, "filtro": filtro, "total": len(chunks)}


def format_extract_rejected_item(chunk: dict, *, index: int, total: int) -> str:
    """Card PT-BR de um chunk que o extract rejeitou: motivo e trecho inteiros."""
    page = chunk.get("page_in_book") or chunk.get("page_in_file") or "?"
    section = (chunk.get("section_path") or "").strip()
    header = f"[{index}/{total}] {chunk['chunk_id']}  p.{page}"
    if section:
        header += f"  {section}"
    summary = _load_json(chunk.get("summary_json"))
    origin = extract_rejection_origin(chunk)
    excerpt = (chunk.get("text") or "").strip() or "_Trecho nao disponivel._"
    if origin == "filtro":
        blocks = [header, "", "Origem", "filtro", ""]
        dropped = summary.get("rejected_candidates") or []
        if not dropped:
            blocks.extend(["Motivo", "_Sem motivo._", ""])
        for item in dropped:
            if not isinstance(item, dict):
                continue
            blocks.extend(
                [
                    "Tese",
                    str(item.get("thesis") or "").strip() or "_Sem tese._",
                    "",
                    "Ancora",
                    str(item.get("anchor_quote") or "").strip() or "_Sem ancora._",
                    "",
                    "Motivo",
                    str(item.get("reason") or "").strip() or "_Sem motivo._",
                    "",
                ]
            )
        blocks.extend(["Trecho", excerpt])
        return "\n".join(blocks)
    category = str(summary.get("rejection_category") or "-")
    reason = str(summary.get("rejection_reason") or "").strip() or "_Sem motivo._"
    return (
        f"{header}\n\nOrigem\nmodelo\n\nCategoria\n{category}\n\n"
        f"Motivo\n{reason}\n\nTrecho\n{excerpt}"
    )


def ask_extract_decision(console) -> str:
    """Prompt HITL um-a-um para um chunk rejeitado pelo extract."""
    from rich.prompt import Prompt

    while True:
        raw = Prompt.ask(
            r"Decisao \[e=reenfileirar/f=processar mesmo assim/"
            r"d=apagar do banco/m=manter/p=pular/q=voltar\]",
            choices=list(_EXTRACT_DECISION_ALIASES.keys()),
            default="p",
            show_choices=False,
            console=console,
        )
        choice = normalize_extract_decision(raw)
        if choice is not None:
            return choice


def _requeue_extract_rejected(
    console,
    db: StateDB,
    idx: VectorIndex,
    rejected: list[dict],
    stats: dict[str, int],
    dismissed: set[str],
) -> None:
    """Walk extract rejections one by one. ``m`` hides a chunk for this session."""
    if not rejected:
        console.print("[dim]Nenhum chunk rejeitado pelo extract.[/dim]")
        return
    pending = [c for c in rejected if c["chunk_id"] not in dismissed]
    if not pending:
        console.print("[dim]Nenhum chunk rejeitado pelo extract nesta sessao.[/dim]")
        return
    total = len(pending)
    for index, chunk in enumerate(pending, 1):
        console.print()
        console.print(
            format_extract_rejected_item(chunk, index=index, total=total),
            markup=False,
        )
        choice = ask_extract_decision(console)
        if choice == "sair":
            return
        if choice == "reenfileirar":
            n = requeue_extract_rejected(db, [chunk["chunk_id"]])
            stats["requeued"] += n
            console.print("[green]Reenfileirado. Rode `zettel extract`.[/green]")
        elif choice == "processar":
            n = force_extract_rejected(db, [chunk["chunk_id"]])
            stats["forced"] += n
            console.print(
                "[green]Marcado para extracao obrigatoria. Rode `zettel extract`.[/green]"
            )
        elif choice == "apagar":
            from rich.prompt import Prompt

            confirm = Prompt.ask(
                "Apagar este chunk do banco? Nao da para desfazer.",
                choices=["s", "n"],
                default="n",
                console=console,
            )
            if confirm != "s":
                console.print("[dim]Nada apagado.[/dim]")
                continue
            n = discard_extract_rejected(db, idx, [chunk["chunk_id"]])
            stats["discarded"] += n
            console.print("[green]Apagado do banco.[/green]")
        elif choice == "manter":
            dismissed.add(chunk["chunk_id"])
            console.print("[dim]Mantido como rejeitado.[/dim]")
        else:
            console.print("[dim]Pulado.[/dim]")


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


def pending_dedupe_concepts(
    db: StateDB,
    source_id: str | None = None,
    *,
    source_ids: list[str] | None = None,
) -> list[dict]:
    """Concepts dedupe flagged as possibly redundant, waiting for the reviewer.

    ``source_ids`` is an allow-list, and an empty list matches nothing. It wins
    over ``source_id``.
    """
    rows = db.get_concepts_by_status("dedupe_pending")
    if source_ids is not None:
        allowed = set(source_ids)
        return [row for row in rows if row.get("source_id") in allowed]
    return [row for row in rows if not source_id or row["source_id"] == source_id]


def keep_duplicate(
    db: StateDB,
    concept_id: str,
    *,
    mode: str = "manual",
    surface: str = "cli",
    run_id: int | None = None,
) -> None:
    """Keep a flagged concept: it goes to ``connect`` and dedupe never flags it again."""
    concept = db.get_concept(concept_id) or {}
    dedupe = _load_json(concept.get("dedupe_json"))
    dedupe["override"] = True
    db.set_concept_dedupe(concept_id, "approved", dedupe)
    label_dedupe(db, concept_id, "keep")
    chunk = db.get_chunk(concept.get("chunk_id") or "") if concept else None
    log_review_event(
        db,
        chunk or {"chunk_id": concept.get("chunk_id"), "source_id": concept.get("source_id")},
        decision="keep_duplicate",
        mode=mode,
        surface=surface,
        concept_id=concept_id,
        run_id=run_id,
        concepts=[concept] if concept else None,
    )


def discard_duplicate(
    db: StateDB,
    concept_id: str,
    *,
    mode: str = "manual",
    surface: str = "cli",
    run_id: int | None = None,
) -> None:
    concept = db.get_concept(concept_id) or {}
    db.set_concept_dedupe(concept_id, "duplicate")
    label_dedupe(db, concept_id, "discard")
    chunk = db.get_chunk(concept.get("chunk_id") or "") if concept else None
    log_review_event(
        db,
        chunk or {"chunk_id": concept.get("chunk_id"), "source_id": concept.get("source_id")},
        decision="discard_duplicate",
        mode=mode,
        surface=surface,
        concept_id=concept_id,
        run_id=run_id,
        concepts=[concept] if concept else None,
    )


def format_dedupe_item(db: StateDB, concept: dict) -> str:
    """Card PT-BR: the new thesis next to what would make it redundant.

    Always names the source. When the LLM named no target note, the card lists
    every same-source note it was compared against, so the reviewer never has to
    decide "repeats what?" blind.
    """
    from zettel.vault import normalize_note_id

    cand = _load_json(concept.get("candidate_json"))
    dedupe = _load_json(concept.get("dedupe_json"))
    source_id = concept.get("source_id") or ""
    source = db.get_source(source_id) or {}
    lines = [
        f"Fonte: {source_id}" + (f" - {source['title']}" if source.get("title") else ""),
        f"Tese nova: {cand.get('thesis') or '?'}",
        f"Definicao: {cand.get('definition') or '-'}",
    ]
    note_id = normalize_note_id(str(dedupe.get("target_note_id") or ""))
    other = db.get_concept(dedupe["duplicate_of"]) if dedupe.get("duplicate_of") else None
    if note_id:
        lines.append(f"Nota existente: {_note_label(db, note_id)}")
    elif other:
        other_thesis = _load_json(other.get("candidate_json")).get("thesis")
        label = (
            "Conceito aprovado ainda sem nota"
            if other.get("status") == "approved"
            else "Candidato do mesmo lote"
        )
        lines.append(f"{label}: {other_thesis or other['concept_id']}")
    elif dedupe.get("compared_note_ids"):
        lines.append("Nota existente: nao indicada pelo LLM. Notas comparadas:")
        lines.extend(f"  - {_note_label(db, nid)}" for nid in dedupe["compared_note_ids"])
    lines.append(f"Motivo: {dedupe.get('reason') or '-'}")
    return "\n".join(lines)


def _note_label(db: StateDB, note_id: str) -> str:
    note = db.get_note(note_id) or {}
    return f"{note.get('title') or note_id} ({note_id})"


def _resolve_dedupe_pending(
    console,
    db: StateDB,
    source_id: str | None,
    stats: dict[str, int],
    *,
    source_ids: list[str] | None = None,
) -> None:
    """Ask keep / discard / skip for every possible duplicate of this source."""
    from rich.prompt import Prompt

    pending = pending_dedupe_concepts(db, source_id, source_ids=source_ids)
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


def _review_snapshot(chunk: dict | None, concepts: list[dict] | None = None) -> dict:
    """Chunk text and candidates at decision time. Survives purge of the row."""
    chunk = chunk or {}
    if concepts is None:
        concepts = []
    return {
        "source_id": chunk.get("source_id") or "",
        "text": chunk.get("text") or "",
        "candidates": [_load_json(row.get("candidate_json")) for row in concepts],
        "summary_candidates": _load_json(chunk.get("summary_json")).get("candidates") or [],
    }


def log_review_event(
    db: StateDB,
    chunk: dict | None,
    *,
    decision: str,
    mode: str,
    surface: str,
    concept_id: str | None = None,
    run_id: int | None = None,
    concepts: list[dict] | None = None,
) -> None:
    """One row per human decision. Nothing in the pipeline reads it back to decide."""
    chunk = chunk or {}
    confidence = chunk.get("review_confidence")
    db.record_review_event(
        source_id=str(chunk.get("source_id") or ""),
        chunk_id=str(chunk.get("chunk_id") or ""),
        concept_id=concept_id,
        decision=decision,
        mode=mode,
        surface=surface,
        review_confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        run_id=run_id,
        snapshot=_review_snapshot(chunk, concepts),
    )


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
    persisted: list[str] = []
    for chunk in db.get_chunks_by_status("awaiting_review", source_id=source_id):
        if (chunk.get("review_confidence") or 0) >= limiar:
            if approve_chunk(
                cfg,
                db,
                idx,
                chunk["chunk_id"],
                mode="threshold",
                surface="cli",
                run_id=None,
                refresh_source=False,
            ):
                n += 1
                persisted.append(chunk["chunk_id"])
    refresh_approved_literature(cfg, db, persisted)
    if n:
        _dedupe_approved_concepts(cfg, db, idx, source_id)
    return n


def finalize_approved_concepts(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, source_id: str | None = None
) -> None:
    """Run post-approval deduplication after granular web review actions."""
    _dedupe_approved_concepts(cfg, db, idx, source_id)


def _draft_candidate_lines(body: str) -> tuple[list[str], list[str]]:
    """Theses from the checklist and anchor quotes from the managed block, in order."""
    from zettel.manual_lit import _candidate_theses
    from zettel.vault import read_managed_block

    theses = _candidate_theses(body)
    raw = read_managed_block(body, "auto-candidate-quotes") or ""
    quotes = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or not stripped[0].isdigit():
            continue
        if ". >" not in stripped and ".>" not in stripped:
            continue
        _, _, quote = stripped.partition(">")
        quotes.append(quote.strip())
    return theses, quotes


def _draft_line_indexes(db: StateDB, chunk: dict, theses: list[str]) -> dict[str, int]:
    """Checklist line of each concept. The draft lists every candidate.

    Order is ``summary_json.candidates``. A concept keeps that line when the
    caller approves only some of them. The thesis still stored on the concept
    is the line's original text. A thesis already copied from the draft matches
    the edited line instead.
    """
    catalog = [
        str(item.get("thesis") or "")
        for item in (_load_json(chunk.get("summary_json")).get("candidates") or [])
        if isinstance(item, dict)
    ]
    used: set[str] = set()
    indexes: dict[str, int] = {}
    rows = db.get_concepts_for_chunk(chunk["chunk_id"])
    for index, thesis in enumerate(catalog):
        for concept in rows:
            concept_id = concept["concept_id"]
            if concept_id in used:
                continue
            stored = str(_load_json(concept.get("candidate_json")).get("thesis") or "")
            if stored == thesis:
                indexes[concept_id] = index
                used.add(concept_id)
                break
    taken = set(indexes.values())
    for concept in rows:
        concept_id = concept["concept_id"]
        if concept_id in indexes:
            continue
        stored = str(_load_json(concept.get("candidate_json")).get("thesis") or "")
        matches = [
            index for index, line in enumerate(theses) if line == stored and index not in taken
        ]
        if len(matches) == 1:
            indexes[concept_id] = matches[0]
            taken.add(matches[0])
    return indexes


def _apply_draft_edits(cfg: AppConfig, db: StateDB, chunk: dict, concepts: list[dict]) -> set[str]:
    """Copy thesis and anchor edits from the draft into ``candidate_json``.

    An anchor that is no longer in the chunk text is refused: that concept
    stays ``awaiting_review`` and is not approved (#269, option a).
    Each concept reads its own checklist line. Approving one of several
    candidates still sees the line the draft wrote for it.
    """
    draft_path_str = chunk.get("literature_note_path")
    if not draft_path_str or not concepts:
        return set()
    draft_path = Path(draft_path_str)
    if not draft_path.exists():
        return set()
    _meta, body = parse_frontmatter(draft_path.read_text(encoding="utf-8"))
    theses, quotes = _draft_candidate_lines(body)
    indexes = _draft_line_indexes(db, chunk, theses)
    catalog_len = len(_load_json(chunk.get("summary_json")).get("candidates") or [])
    if catalog_len and len(theses) != catalog_len:
        logger.warning(
            "Chunk %s: o draft tem %d teses e o resumo tem %d candidatos",
            chunk["chunk_id"],
            len(theses),
            catalog_len,
        )
    refused: set[str] = set()
    chunk_text = chunk.get("text") or ""
    ratio = cfg.extraction.anchor_quote_min_ratio
    same_length = len(theses) == len(concepts)
    for position, concept in enumerate(concepts):
        index = indexes.get(concept["concept_id"])
        if index is None and same_length:
            index = position
        if index is None or index >= len(theses):
            logger.warning(
                "Chunk %s: conceito %s sem linha correspondente no draft",
                chunk["chunk_id"],
                concept["concept_id"],
            )
            continue
        thesis = theses[index]
        quote = quotes[index] if index < len(quotes) else ""
        try:
            cand = PermanentNoteCandidate(**_load_json(concept.get("candidate_json")))
        except Exception:
            logger.warning("Conceito %s com candidate_json ilegivel", concept["concept_id"])
            continue
        updates: dict[str, str] = {}
        if thesis and thesis != cand.thesis:
            updates["thesis"] = thesis
        if quote and quote != cand.anchor_quote:
            if quote_is_grounded(quote, chunk_text, ratio):
                updates["anchor_quote"] = quote
            else:
                refused.add(concept["concept_id"])
                logger.warning(
                    "Ancora editada do conceito %s nao esta no trecho",
                    concept["concept_id"],
                )
        if not updates:
            continue
        cand = cand.model_copy(update=updates)
        db.upsert_concept(
            concept["concept_id"],
            concept["source_id"],
            concept["chunk_id"],
            candidate_json=cand.model_dump_json(),
            status="awaiting_review",
        )
    return refused


def decide_concepts(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    decisions: dict[str, str],
    *,
    mode: str = "manual",
    surface: str = "cli",
    run_id: int | None = None,
    refresh_source: bool = True,
) -> dict[str, Any]:
    """Approve or reject individual concepts. The chunk settles when none remain.

    A chunk with at least one approved concept is persisted and its LIT lists
    only those candidates. A chunk whose every concept was rejected is rejected.
    """
    outcome: dict[str, list[str]] = {
        "approved": [],
        "rejected": [],
        "refused": [],
        "persisted": [],
        "rejected_chunks": [],
        "draft_delete_failures": [],
    }
    by_chunk: dict[str, list[tuple[str, str]]] = {}
    for concept_id, decision in decisions.items():
        if decision not in {"approve", "reject"}:
            continue
        concept = db.get_concept(concept_id)
        if not concept or concept.get("status") != "awaiting_review":
            continue
        by_chunk.setdefault(concept["chunk_id"], []).append((concept_id, decision))

    for chunk_id, items in by_chunk.items():
        chunk = db.get_chunk(chunk_id)
        if not chunk or chunk.get("status") != "awaiting_review":
            continue
        concepts = ordered_review_concepts(db, chunk)
        chosen = {concept_id for concept_id, _decision in items}
        targets = [concept for concept in concepts if concept["concept_id"] in chosen]
        approve_ids = {concept_id for concept_id, decision in items if decision == "approve"}
        refused = _apply_draft_edits(
            cfg, db, chunk, [concept for concept in targets if concept["concept_id"] in approve_ids]
        )
        outcome["refused"].extend(sorted(refused))
        for concept in targets:
            concept_id = concept["concept_id"]
            if concept_id in refused:
                continue
            decision = dict(items)[concept_id]
            if decision == "approve":
                db.update_concept_status(concept_id, "extracted")
                outcome["approved"].append(concept_id)
            else:
                db.update_concept_status(concept_id, "rejected")
                outcome["rejected"].append(concept_id)
            log_review_event(
                db,
                chunk,
                decision=decision,
                mode=mode,
                surface=surface,
                concept_id=concept_id,
                run_id=run_id,
                concepts=[concept],
            )
        remaining = ordered_review_concepts(db, chunk)
        if remaining:
            continue
        promoted = [
            row
            for row in db.get_concepts_for_chunk(chunk_id)
            if row.get("status") in {"extracted", "approved", "dedupe_pending"}
        ]
        if promoted:
            payloads = [_load_json(row.get("candidate_json")) for row in promoted]
            if approve_chunk(
                cfg,
                db,
                idx,
                chunk_id,
                approved_candidates=payloads,
                refresh_source=False,
            ):
                outcome["persisted"].append(chunk_id)
        elif reject_chunk(cfg, db, idx, chunk_id, record=False):
            outcome["rejected_chunks"].append(chunk_id)
            kept = db.get_chunk(chunk_id) or {}
            path = kept.get("literature_note_path") or ""
            if path and Path(path).is_file():
                outcome["draft_delete_failures"].append(chunk_id)
    if refresh_source:
        refresh_approved_literature(cfg, db, outcome["persisted"])
    return outcome


def approve_chunk(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    chunk_id: str,
    *,
    approved_candidates: list[dict] | None = None,
    mode: str = "manual",
    surface: str = "cli",
    run_id: int | None = None,
    refresh_source: bool = True,
) -> bool:
    """Move draft to 20_Literature and promote concepts.

    The note is persisted to the vault and to SQLite for audit, but is **not**
    embedded: nothing ever queried the old ``literature_notes`` collection, and
    the LIT body is derived from a chunk whose text is already indexed.
    """
    chunk = db.get_chunk(chunk_id)
    if not chunk or chunk.get("status") != "awaiting_review":
        logger.warning("Chunk %s nao esta awaiting_review", chunk_id)
        return False
    if approved_candidates is None:
        concepts = ordered_review_concepts(db, chunk)
        if concepts:
            outcome = decide_concepts(
                cfg,
                db,
                idx,
                {concept["concept_id"]: "approve" for concept in concepts},
                mode=mode,
                surface=surface,
                run_id=run_id,
                refresh_source=refresh_source,
            )
            return chunk_id in outcome["persisted"]

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

    wrote_from_draft = False
    if draft_path and draft_path.exists():
        content = draft_path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(content)
        meta["status"] = "approved"
        meta["updated_at"] = now_vault_iso(cfg.vault_timezone)
        wrote_from_draft = True
    else:
        # Rebuild from summary_json when the draft is already gone.
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
    if approved_candidates is not None:
        body = replace_literature_candidates(body, approved_candidates)
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
    if wrote_from_draft and draft_path is not None:
        _unlink_draft(draft_path)
    if refresh_source:
        refresh_approved_literature(cfg, db, [chunk_id])
    if approved_candidates is None:
        log_review_event(
            db,
            chunk,
            decision="approve",
            mode=mode,
            surface=surface,
            run_id=run_id,
        )
    logger.info("[NOTE=%s] APPROVED → persistido no cofre e em SQLite", dest_path)
    return True


def reject_chunk(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    chunk_id: str,
    *,
    mode: str = "manual",
    surface: str = "cli",
    run_id: int | None = None,
    record: bool = True,
    stats: dict[str, int] | None = None,
) -> bool:
    chunk = db.get_chunk(chunk_id)
    if not chunk or chunk.get("status") != "awaiting_review":
        logger.warning("Chunk %s nao esta awaiting_review", chunk_id)
        return False
    if record:
        log_review_event(
            db,
            chunk,
            decision="reject",
            mode=mode,
            surface=surface,
            run_id=run_id,
            concepts=db.get_concepts_for_chunk(chunk_id),
        )
    draft_path_str = chunk.get("literature_note_path")
    if draft_path_str and not _unlink_draft(Path(draft_path_str)):
        if stats is not None:
            stats["draft_delete_failures"] = stats.get("draft_delete_failures", 0) + 1
        log_review_event(
            db,
            chunk,
            decision="reject_draft_kept",
            mode=mode,
            surface=surface,
            run_id=run_id,
        )

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


def _unlink_draft(path: Path) -> bool:
    """Remove a review draft. False means the file is still on disk."""
    if not path.is_file():
        return True
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("Nao foi possivel apagar draft %s: %s", path, exc)
        return False
    return True


def rejected_drafts_on_disk(db: StateDB) -> list[dict]:
    """Rejected chunks whose draft file is still in the review inbox."""
    kept = []
    for chunk in db.get_chunks_by_status("rejected"):
        path = chunk.get("literature_note_path") or ""
        if path and Path(path).is_file():
            kept.append(chunk)
    return kept


def refresh_approved_literature(cfg: AppConfig, db: StateDB, chunk_ids: list[str]) -> None:
    """Rewrite each source index once, then the permanent-link block on each LIT."""
    sources: list[str] = []
    for chunk_id in dict.fromkeys(chunk_ids):
        chunk = db.get_chunk(chunk_id)
        if not chunk:
            continue
        source_id = str(chunk.get("source_id") or "")
        if source_id and source_id not in sources:
            sources.append(source_id)
    for source_id in sources:
        _refresh_literature_index(cfg, db, source_id)
    if chunk_ids:
        sync_lit_permanent_links(cfg, db, chunk_ids)


def _refresh_literature_index(cfg: AppConfig, db: StateDB, source_id: str) -> None:
    """Create the literature index if missing, then rewrite its chapter map.

    The chapter map is the single place approved LIT notes are listed. A batch
    approval calls this once per source, after every draft of that source is
    persisted (#276).
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
    from zettel.vault import MalformedManagedBlock, permanent_wikilink, upsert_managed_block

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
            try:
                content = upsert_managed_block(content, LIT_PERMANENT_BLOCK, "")
            except MalformedManagedBlock as e:
                logger.warning("%s em %s; nota nao atualizada", e, path)
                continue
            path.write_text(content, "utf-8")
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
    invalid = 0
    for row in rows:
        raw = row.get("candidate_json")
        if not raw:
            logger.warning(
                "Conceito %s sem candidate_json; permanece extracted",
                row["concept_id"],
            )
            invalid += 1
            continue
        try:
            cand = PermanentNoteCandidate(**json.loads(raw))
        except Exception:
            logger.warning(
                "Conceito %s com candidate_json invalido; permanece extracted",
                row["concept_id"],
            )
            invalid += 1
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

    if invalid:
        logger.warning(
            "%d conceito(s) presos em extracted por candidate_json invalido ou ausente",
            invalid,
        )
    if not candidates:
        return

    from zettel.extractor import deduplicate_candidates

    approved = deduplicate_candidates(cfg, db, idx, None, candidates)
    logger.info(
        "Dedupe pos-review: %d / %d candidatos aprovados para connect",
        len(approved),
        len(candidates),
    )
