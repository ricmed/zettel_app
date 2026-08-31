"""Three-layer duplicate detection: file hash, extraction hash, semantic similarity."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.index import VectorIndex
from zettel.state import StateDB

logger = logging.getLogger(__name__)


class HarvestAborted(Exception):
    """Raised to stop `run_harvest` early when the user chooses to abort."""


def sample_chunk_texts(cfg: AppConfig, chapters: list[dict[str, str]], sample_size: int) -> list[str]:
    """Split chapters into chunks (without persisting) and return an evenly distributed sample.

    Reuses the same structural chunker as the chunking pipeline, so the semantic
    duplicate check samples exactly the chunks that would be persisted.
    """
    from . import chunking

    all_chunks: list[str] = []
    for chapter in chapters:
        all_chunks.extend(text for _, text in chunking.split_chapter_into_chunks(cfg, chapter))

    if not all_chunks:
        return []
    if len(all_chunks) <= sample_size:
        return all_chunks

    step = len(all_chunks) / sample_size
    return [all_chunks[int(i * step)] for i in range(sample_size)]


def find_semantic_duplicate_candidates(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, chapters: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Query the chunk index for near-duplicates of a sample of this file's chunks.

    Returns candidate sources (aggregated, best score per source_id) whose
    similarity is at or above `cfg.harvest.duplicate_chunk_threshold`.
    """
    threshold = cfg.harvest.duplicate_chunk_threshold
    sample_size = max(1, cfg.harvest.duplicate_sample_size)
    sample_texts = sample_chunk_texts(cfg, chapters, sample_size)
    if not sample_texts:
        return []

    logger.info(
        "Deduplicacao semantica: consultando Chroma com %d amostras de chunks "
        "(threshold=%.2f) — gera embeddings das amostras",
        len(sample_texts), threshold,
    )
    matches = idx.find_similar_chunks(sample_texts, n_results=3)
    logger.info(
        "Deduplicacao semantica: %d hits retornados pelo indice de chunks",
        len(matches),
    )
    best_by_source: dict[str, float] = {}
    for m in matches:
        distance = m.get("distance")
        if distance is None:
            continue
        similarity = 1 - (distance / 2)
        meta = m.get("metadata") or {}
        source_id = meta.get("source_id")
        if not source_id or similarity < threshold:
            continue
        if source_id not in best_by_source or similarity > best_by_source[source_id]:
            best_by_source[source_id] = similarity

    candidates: list[dict[str, Any]] = []
    for source_id, similarity in sorted(best_by_source.items(), key=lambda kv: -kv[1]):
        src = db.get_source(source_id)
        candidates.append({
            "source_id": source_id,
            "citekey": src["citekey"] if src else source_id,
            "title": src["title"] if src else "(desconhecido)",
            "similarity": similarity,
        })
    return candidates


def resolve_duplicate_decision(
    file_path: Path,
    candidates: list[dict[str, Any]],
    interactive: bool,
    duplicate_action: str | None,
    cfg: AppConfig,
) -> str:
    """Decide what to do about a suspected semantic duplicate.

    Returns one of "skip", "continue", "abort".
    """
    if not interactive:
        action = duplicate_action or cfg.harvest.non_interactive_duplicate_action
        logger.warning(
            "Suspeita de duplicidade semantica para '%s' (modo nao-interativo, acao='%s'). "
            "Candidatos: %s",
            file_path.name, action,
            ", ".join(f"{c['citekey']} ({c['similarity']:.2f})" for c in candidates),
        )
        return action

    from rich.console import Console
    from rich.prompt import Prompt
    from rich.table import Table

    console = Console(stderr=True)
    table = Table(title=f"Possivel duplicata: {file_path.name}")
    table.add_column("Citekey", style="bold")
    table.add_column("Titulo")
    table.add_column("Similaridade", justify="right")
    for c in candidates:
        table.add_row(c["citekey"], c["title"], f"{c['similarity']:.0%}")
    console.print(table)

    choice = Prompt.ask(
        "O conteudo parece semelhante a fonte(s) ja existente(s). O que deseja fazer?",
        choices=["pular", "continuar", "abortar"],
        default="pular",
        console=console,
    )
    return {"pular": "skip", "continuar": "continue", "abortar": "abort"}[choice]
