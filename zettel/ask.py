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

from .hashing import compute_llm_call_checksum, normalize_text_for_hash, sha256_hex
from .llm import call_llm, get_llm, load_prompt
from .retrieval import RetrievedNote, Retriever
from .vault import _slug, render_frontmatter

if TYPE_CHECKING:
    from .config import AppConfig
    from .index import VectorIndex
    from .state import StateDB

logger = logging.getLogger(__name__)

_NO_EVIDENCE = "Nao encontrei evidencia suficiente no vault para responder a essa pergunta."


@dataclass
class AskSource:
    """One note that fed the answer, with retrieval provenance."""

    note_id: str
    title: str
    wiki_link: str
    score: float
    hop: int
    origin: str                       # human-readable: "busca" or "conexao ..."
    source_id: Optional[str] = None
    path: Optional[str] = None


@dataclass
class AskResult:
    question: str
    answer: str
    sources: list[AskSource] = field(default_factory=list)
    mode: str = "hybrid"
    graph_expansion: bool = True
    llm_model: str = ""
    llm_called: bool = False


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
    ask_cfg = cfg.retrieval.ask
    topk = topk if topk is not None else ask_cfg.topk
    mode = mode or cfg.retrieval.mode
    if use_graph is None:
        use_graph = cfg.retrieval.graph_expansion.enabled

    retriever = Retriever(cfg, db, idx)
    hits = retriever.search_notes(
        question, topk=topk, mode=mode, expand_graph=use_graph
    )
    hits = hits[: ask_cfg.max_context_notes]

    sources = [_to_ask_source(db, h) for h in hits]
    result = AskResult(
        question=question,
        answer="",
        sources=sources,
        mode=mode,
        graph_expansion=bool(use_graph),
        llm_model=cfg.llm.model,
    )

    if not hits:
        # Nothing retrieved — answer deterministically without spending an LLM call.
        result.answer = _NO_EVIDENCE
        return result

    context = _build_context(hits, ask_cfg.max_chars_per_note)
    prompt_template = load_prompt(cfg.prompts_path / "ask.md")
    filled = (
        prompt_template
        .replace("{language}", cfg.language)
        .replace("{question}", question)
        .replace("{context_notes}", context)
    )

    prompt_hash = sha256_hex(prompt_template)
    filled_hash = sha256_hex(normalize_text_for_hash(filled))
    call_checksum = compute_llm_call_checksum(
        prompt_hash, filled_hash, cfg.llm.model, cfg.llm.temperature, cfg.language,
    )
    cached = db.get_cached_llm_response(call_checksum)
    if cached is not None:
        logger.debug("Cache hit (ask) para pergunta")
        result.answer = cached
    else:
        llm = get_llm(cfg)
        answer = call_llm(llm, filled)
        db.cache_llm_response(
            call_checksum, json.dumps({"prompt": filled}, ensure_ascii=False), answer
        )
        result.answer = answer
        result.llm_called = True

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


def _wiki_link(note_id: str, title: str) -> str:
    if title:
        return f"[[ZTL - {note_id} - {_slug(title)}]]"
    return f"[[ZTL - {note_id}]]"


def _build_context(hits: list[RetrievedNote], max_chars: int) -> str:
    """Render retrieved notes into the prompt's context block."""
    parts: list[str] = []
    for i, hit in enumerate(hits, 1):
        title = hit.title or "Sem titulo"
        wiki = _wiki_link(hit.note_id, hit.title)
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
    return AskSource(
        note_id=hit.note_id,
        title=hit.title,
        wiki_link=_wiki_link(hit.note_id, hit.title),
        score=round(hit.score, 5),
        hop=hit.hop,
        origin=_origin_label(hit),
        source_id=source_id,
        path=path,
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
            sub = [f"origem: {src.origin}", f"score: {src.score}"]
            if src.source_id:
                sub.append(f"fonte: {src.source_id}")
            lines.append(f"    - {' | '.join(sub)}")
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
