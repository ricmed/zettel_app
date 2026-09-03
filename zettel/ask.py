"""The `ask` command — question answering over the vault.

Retrieves relevant permanent notes with the hybrid Retriever (dense + BM25 +
graph expansion), builds a cited context, and asks the LLM to answer in the
configured language using *only* that context. The answer can be saved as a
Markdown note with full provenance (which notes were consulted and why), so the
user can trace every claim back to its source.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .config import effective_temperature, llm_phase
from .hashing import compute_llm_call_checksum, normalize_text_for_hash, sha256_hex
from .llm import call_llm, fill_template, get_llm, load_prompt_parts
from .retrieval import RetrievedNote, Retriever
from .vault import _slug, permanent_wikilink, render_frontmatter

if TYPE_CHECKING:
    from .config import AppConfig
    from .index import VectorIndex
    from .state import StateDB

logger = logging.getLogger(__name__)

_NO_EVIDENCE = "Nao encontrei evidencia suficiente no vault para responder a essa pergunta."


@dataclass
class AskSource:
    """One note surfaced by retrieval, with provenance."""

    note_id: str
    title: str
    wiki_link: str
    rrf_score: float                  # fused Reciprocal Rank Fusion score (positional, not a relevance measure)
    hop: int
    origin: str                       # human-readable: "busca" or "conexao ..."
    source_id: Optional[str] = None
    path: Optional[str] = None
    passed_floor: bool = True         # False = shown for transparency, not used to answer
    vector_similarity: Optional[float] = None  # cosine similarity (1 - distance/2), when available
    bm25_rank: Optional[int] = None   # position in the lexical (BM25) ranking, when found
    floor_reason: str = ""            # human-readable explanation of the floor verdict


@dataclass
class AskResult:
    question: str
    answer: str
    sources: list[AskSource] = field(default_factory=list)
    # Raw ranked pool before the relevance floor, always populated (when the
    # corpus is non-empty) so callers can show "what was closest" even when
    # nothing cleared the floor and `sources` ends up empty.
    candidates: list[AskSource] = field(default_factory=list)
    mode: str = "hybrid"
    graph_expansion: bool = True
    llm_model: str = ""
    llm_called: bool = False
    # Snapshot of the retrieval parameters actually used for this call (topk,
    # RRF/floor/graph thresholds), for `--show-context` to display alongside
    # the per-note results — so the reader can see *why* the floor decided
    # what it decided, not just the verdict.
    retrieval_params: dict = field(default_factory=dict)


# ── Public API ─────────────────────────────────────────────────────────


def run_ask(
    cfg: "AppConfig",
    db: "StateDB",
    idx: "VectorIndex",
    question: str,
    topk: Optional[int] = None,
    use_graph: Optional[bool] = None,
    mode: Optional[str] = None,
) -> AskResult:
    """Answer ``question`` from the vault. Returns the answer plus its provenance."""
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("ask")
    begin_run(run_id)

    ask_cfg = cfg.retrieval.ask
    topk = topk if topk is not None else ask_cfg.topk
    mode = mode or cfg.retrieval.mode
    if use_graph is None:
        use_graph = cfg.retrieval.graph_expansion.enabled

    retriever = Retriever(cfg, db, idx)
    result_pool = retriever.search_notes(
        question, topk=topk, mode=mode, expand_graph=use_graph
    )
    hits = result_pool.hits[: ask_cfg.max_context_notes]

    floor_cfg = cfg.retrieval.relevance_floor
    graph_cfg = cfg.retrieval.graph_expansion
    retrieval_params = {
        "mode": mode,
        "topk": topk,
        "max_context_notes": ask_cfg.max_context_notes,
        "rrf_k": cfg.retrieval.rrf_k,
        "relevance_floor_enabled": floor_cfg.enabled,
        "min_vector_similarity": floor_cfg.min_vector_similarity,
        "absolute_min_similarity": floor_cfg.absolute_min_similarity,
        "bm25_hit_bypasses_floor": floor_cfg.bm25_hit_bypasses_floor,
        "bm25_bypass_max_rank": floor_cfg.bm25_bypass_max_rank,
        "graph_expansion_used": bool(use_graph),
        "graph_max_hops": graph_cfg.max_hops,
        "graph_decay": graph_cfg.decay,
        "graph_max_neighbors": graph_cfg.max_neighbors,
    }

    spec = llm_phase(cfg, "ask")
    result = AskResult(
        question=question,
        answer="",
        sources=[_to_ask_source(db, h) for h in hits],
        candidates=[_to_ask_source(db, h) for h in result_pool.candidates],
        mode=mode,
        graph_expansion=bool(use_graph),
        llm_model=spec.model,
        retrieval_params=retrieval_params,
    )

    if not hits:
        # Nothing cleared the relevance floor — answer deterministically without
        # spending an LLM call. `candidates` still carries the raw top-k pool so
        # the caller can show what was closest, for transparency/debugging.
        result.answer = _NO_EVIDENCE
        finish_pipeline_run(db, run_id)
        return result

    context = _build_context(db, hits, ask_cfg.max_chars_per_note)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "ask.md")
    mapping = {
        "language": cfg.language,
        "question": question,
        "context_notes": context,
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)
    filled_for_hash = f"{system}\n{user}" if system else user

    prompt_hash = sha256_hex(prompt_parts.full_template)
    filled_hash = sha256_hex(normalize_text_for_hash(filled_for_hash))
    call_checksum = compute_llm_call_checksum(
        prompt_hash, filled_hash, spec.model, effective_temperature(cfg, spec), cfg.language,
        provider=spec.provider, top_p=cfg.llm.top_p,
    )
    cached = db.get_cached_llm_response(call_checksum)
    if cached is not None:
        logger.debug("Cache hit (ask) para pergunta")
        from zettel.usage import record_cache_hit
        record_cache_hit(label="ask", model=spec.model)
        result.answer = cached
    else:
        llm = get_llm(cfg, "ask")
        answer = call_llm(
            llm,
            user,
            system=system or None,
            provider=spec.provider,
            prompt_cache=cfg.llm.prompt_cache,
        )
        db.cache_llm_response(
            call_checksum,
            json.dumps({"system": system, "user": user}, ensure_ascii=False),
            answer,
        )
        result.answer = answer
        result.llm_called = True

    finish_pipeline_run(db, run_id)
    return result


# ── Context building ───────────────────────────────────────────────────


def _origin_label(hit: RetrievedNote) -> str:
    """Human-readable retrieval origin for a hit ('busca' or 'conexao ...')."""
    if hit.hop == 0 or not hit.via:
        return "busca"
    step = hit.via[-1]
    rel = step.get("relation_type", "related")
    anchor = step.get("from", "")
    return f"conexao {rel} a partir de [[ZTL - {anchor}]]"


def _wiki_link(db: "StateDB", note_id: str, title: str) -> str:
    row = db.get_note(note_id)
    return permanent_wikilink(
        note_id, title, path=row.get("path") if row else None,
    )


def _build_context(db: "StateDB", hits: list[RetrievedNote], max_chars: int) -> str:
    """Render retrieved notes into the prompt's context block."""
    parts: list[str] = []
    for i, hit in enumerate(hits, 1):
        title = hit.title or "Sem titulo"
        wiki = _wiki_link(db, hit.note_id, title)
        body = (hit.document or "").strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "..."
        origin = _origin_label(hit)
        parts.append(
            f"### Nota {i}: {title}\n"
            f"- Wikilink para citar: {wiki}\n"
            f"- Origem: {origin}\n\n"
            f"{body}"
        )
    return "\n\n".join(parts)


def _to_ask_source(db: "StateDB", hit: RetrievedNote) -> AskSource:
    source_id = hit.metadata.get("source_id")
    path = hit.metadata.get("path")
    if source_id is None or path is None:
        row = db.get_note(hit.note_id)
        if row:
            source_id = source_id or row.get("source_id")
            path = path or row.get("path")
    similarity = (
        round(1.0 - hit.vector_distance / 2.0, 4)
        if hit.vector_distance is not None
        else None
    )
    return AskSource(
        note_id=hit.note_id,
        title=hit.title,
        wiki_link=_wiki_link(db, hit.note_id, hit.title),
        rrf_score=round(hit.score, 5),
        hop=hit.hop,
        origin=_origin_label(hit),
        source_id=source_id,
        path=path,
        passed_floor=hit.passed_floor,
        vector_similarity=similarity,
        bm25_rank=hit.bm25_rank,
        floor_reason=hit.floor_reason,
    )


# ── Saving the answer as a provenance-rich note ────────────────────────


def build_ask_note_body(result: AskResult) -> tuple[dict, str]:
    """Build (frontmatter, body) for a saved answer note with full provenance."""
    now = datetime.now().isoformat()
    meta = {
        "type": "ask_answer",
        "question": result.question,
        "created_at": now,
        "origin": "ask",
        "retrieval_mode": result.mode,
        "graph_expansion": result.graph_expansion,
        "llm_model": result.llm_model,
    }

    lines: list[str] = []
    lines.append("# Pergunta")
    lines.append("")
    lines.append(result.question)
    lines.append("")
    lines.append("## Resposta")
    lines.append("")
    lines.append(result.answer.strip())
    lines.append("")
    lines.append("## Fontes consultadas")
    lines.append("")
    if result.sources:
        for src in result.sources:
            detail = f"- {src.wiki_link} — {src.title or 'Sem titulo'}"
            lines.append(detail)
            sub = [f"origem: {src.origin}", f"score RRF: {src.rrf_score}"]
            if src.vector_similarity is not None:
                sub.append(f"similaridade: {src.vector_similarity}")
            if src.bm25_rank is not None:
                sub.append(f"rank BM25: {src.bm25_rank}")
            if src.source_id:
                sub.append(f"fonte: {src.source_id}")
            lines.append(f"    - {' | '.join(sub)}")
            if src.floor_reason:
                lines.append(f"    - motivo: {src.floor_reason}")
    else:
        lines.append("- (nenhuma nota recuperada)")
    lines.append("")

    return meta, "\n".join(lines)


def save_ask_note(result: AskResult, vault_path: Path, dest: Optional[Path] = None) -> Path:
    """Persist the answer as a Markdown note. Returns the written path.

    Default location is ``<vault>/00_Inbox/`` so the cited ``[[ZTL - ...]]``
    wikilinks resolve when opened in Obsidian.
    """
    meta, body = build_ask_note_body(result)
    if dest is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _slug(result.question) or "pergunta"
        filename = f"ASK - {ts} - {slug}.md"
        dest = Path(vault_path) / "00_Inbox" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = render_frontmatter(meta) + "\n" + body + "\n"
    dest.write_text(content, encoding="utf-8")
    logger.info("Resposta salva em: %s", dest)
    return dest
