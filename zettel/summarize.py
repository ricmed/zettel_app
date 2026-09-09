"""Chapter and source summaries — generation, persistence, vault rendering.

ADR-047. Two artifacts with two different jobs:

* A **chapter summary** is generated from the chapter's real text and is the
  unit of search: it goes into the ``chapter_summaries`` Chroma collection and
  the ``fts_chapter_summaries`` FTS5 table, and is read back by
  :mod:`zettel.catalog`.
* A **source summary** is a *reduce* over the chapter summaries — one cheap
  call, without re-reading the book. It is **not** embedded: the search unit is
  the chapter, and an unread collection is the defect that killed
  ``literature_notes`` (ADR-015 amendment).

Both are *routing* artifacts, not evidence. They say where to look; they are
never quoted as support for a claim, which is what keeps an LLM-authored
artifact out of the territory ADR-043 gates.

The module is split so cost and rendering are independent:

* :func:`generate_summaries` costs LLM calls and is gated by
  ``chapter_checksum`` — an unchanged chapter is never re-summarized.
* :func:`refresh_chapter_map` is deterministic and free, so ``connect`` can call
  it after writing notes to keep the per-chapter note counts live.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import compute_llm_call_checksum, normalize_text_for_hash, sha256_hex
from zettel.index import VectorIndex
from zettel.progress import ProgressObserver, report
from zettel.schemas import ChapterSummaryOutput, SourceSummaryOutput
from zettel.state import StateDB
from zettel.time import now_utc_iso

logger = logging.getLogger(__name__)

SOURCE_SUMMARY_BLOCK = "auto-source-summary"
CHAPTER_MAP_BLOCK = "auto-chapter-map"

_SOURCE_SUMMARY_HEADING = "## Resumo geral"
_CHAPTER_MAP_HEADING = "## Mapa de capitulos"


@dataclass
class SummarizeOutcome:
    """What one `zettel summarize` pass did."""

    source_ids: list[str] = field(default_factory=list)
    chapters_summarized: int = 0
    chapters_skipped: int = 0
    sources_summarized: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    skipped: list[str] = field(default_factory=list)


# ── Pure helpers (shared with rebuild.py, so the two cannot drift) ──────


def parse_topics(raw: str | None) -> list[str]:
    """Decode a ``summary_topics`` JSON column into a list."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(t) for t in data] if isinstance(data, list) else []


def chapter_summary_document(title: str, summary: str, topics: list[str]) -> str:
    """The text that gets embedded for one chapter.

    Title and topics ride along with the prose because a reader searching for a
    chapter types its vocabulary, not its paraphrase.
    """
    parts = [title.strip(), summary.strip()]
    if topics:
        parts.append(", ".join(topics))
    return "\n".join(p for p in parts if p)


def chapter_summary_metadata(chapter: dict, source: dict | None = None) -> dict[str, Any]:
    """Chroma metadata for a chapter summary (str/int/float/bool only)."""
    meta: dict[str, Any] = {
        "chapter_id": chapter["chapter_id"],
        "source_id": chapter["source_id"],
        "chapter_title": chapter.get("title") or "",
    }
    if source:
        meta["citekey"] = source.get("citekey") or ""
        meta["source_title"] = source.get("title") or ""
    return meta


def source_summary_checksum(chapters: list[dict]) -> str:
    """Hash over the chapters' summary checksums, in document order.

    Any chapter whose summary is regenerated changes this, which is what makes
    the source summary regenerate too.
    """
    joined = "|".join(
        f"{c['chapter_id']}:{c.get('summary_checksum') or ''}"
        for c in sorted(chapters, key=lambda c: c["chapter_id"])
    )
    return sha256_hex(joined)


def chapter_text_for_summary(db: StateDB, chapter_id: str) -> str:
    """Reassemble a chapter's real text from its persisted chunks."""
    chunks = [c for c in db.get_chunks_for_chapter(chapter_id) if (c.get("text") or "").strip()]
    chunks.sort(key=lambda c: c.get("chunk_index") if c.get("chunk_index") is not None else 0)
    return "\n\n".join(c["text"].strip() for c in chunks)


# ── Generation ─────────────────────────────────────────────────────────


def _call_summary_llm(
    cfg: AppConfig,
    db: StateDB,
    prompt_name: str,
    mapping: dict[str, str],
    label: str,
) -> tuple[str, bool]:
    """Run one summary prompt through the deterministic LLM cache.

    Returns ``(response_text, was_cache_hit)``.
    """
    from zettel.config import effective_temperature, llm_phase
    from zettel.llm import call_llm, fill_template, get_llm, load_prompt_parts

    spec = llm_phase(cfg, "summarize")
    parts = load_prompt_parts(cfg.prompts_path / prompt_name)
    system = fill_template(parts.system, mapping) if parts.system else ""
    user = fill_template(parts.user_template, mapping)
    filled_for_hash = f"{system}\n{user}" if system else user

    checksum = compute_llm_call_checksum(
        sha256_hex(parts.full_template),
        sha256_hex(normalize_text_for_hash(filled_for_hash)),
        spec.model,
        effective_temperature(cfg, spec),
        cfg.language,
        provider=spec.provider,
        top_p=cfg.llm.top_p,
    )
    cached = db.get_cached_llm_response(checksum)
    if cached is not None:
        from zettel.usage import record_cache_hit

        record_cache_hit(label="summarize", model=spec.model)
        return cached, True

    llm = get_llm(cfg, "summarize")
    response = call_llm(
        llm,
        user,
        system=system or None,
        label=label,
        provider=spec.provider,
        prompt_cache=cfg.llm.prompt_cache,
    )
    db.cache_llm_response(
        checksum,
        json.dumps({"system": system, "user": user}, ensure_ascii=False),
        response,
    )
    return response, False


def _parse_chapter_summary(text: str) -> ChapterSummaryOutput:
    from zettel.llm import extract_json

    return ChapterSummaryOutput(**json.loads(extract_json(text)))


def _parse_source_summary(text: str) -> SourceSummaryOutput:
    from zettel.llm import extract_json

    return SourceSummaryOutput(**json.loads(extract_json(text)))


def _split_for_map_reduce(text: str, budget: int) -> list[str]:
    """Split an oversized chapter at paragraph boundaries into <=budget groups."""
    groups: list[str] = []
    current: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        para_len = len(para) + 2
        if current and size + para_len > budget:
            groups.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += para_len
    if current:
        groups.append("\n\n".join(current))
    return groups


def _summarize_chapter_text(
    cfg: AppConfig,
    db: StateDB,
    source_title: str,
    chapter_title: str,
    text: str,
    outcome: SummarizeOutcome,
) -> ChapterSummaryOutput:
    """One chapter summary, map-reducing when the text exceeds the budget.

    The reduce step feeds the partial summaries back through the same
    ``chapter_summary`` prompt: the partials are a condensed form of the same
    chapter, so the prompt's contract still holds and no third prompt file is
    needed.
    """
    budget = cfg.summarize.max_input_chars

    def run(chapter_text: str, title: str, label: str) -> ChapterSummaryOutput:
        mapping = {
            "language": cfg.language,
            "domain": cfg.domain.name,
            "max_topics": str(cfg.summarize.max_topics),
            "source_title": source_title,
            "chapter_title": title,
            "chapter_text": chapter_text,
        }
        response, hit = _call_summary_llm(cfg, db, "chapter_summary.md", mapping, label)
        outcome.cache_hits += int(hit)
        outcome.llm_calls += int(not hit)
        return _parse_chapter_summary(response)

    if len(text) <= budget:
        return run(text, chapter_title, f"resumo capitulo: {chapter_title}")

    groups = _split_for_map_reduce(text, budget)
    logger.info(
        "Capitulo '%s' com %d chars acima do orcamento (%d): map-reduce em %d partes",
        chapter_title,
        len(text),
        budget,
        len(groups),
    )
    partials: list[ChapterSummaryOutput] = []
    for i, group in enumerate(groups, start=1):
        partials.append(
            run(
                group,
                f"{chapter_title} (parte {i}/{len(groups)})",
                f"resumo parcial {i}/{len(groups)}: {chapter_title}",
            )
        )
    merged = "\n\n".join(p.summary for p in partials)
    return run(merged, chapter_title, f"reduce capitulo: {chapter_title}")


def generate_summaries(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    source_id: str | None = None,
    *,
    progress: ProgressObserver | None = None,
) -> SummarizeOutcome:
    """Summarize every stale chapter, then reduce each source's chapters.

    A chapter whose ``summary_checksum`` still matches its ``chapter_checksum``
    is skipped without an LLM call, so re-running on a settled vault is free.
    """
    from zettel.usage import begin_run, finish_pipeline_run, get_tracker, set_source
    from zettel.vault import sync_source_costs_to_vault

    outcome = SummarizeOutcome()
    sources = [db.get_source(source_id)] if source_id else db.list_sources()
    sources = [s for s in sources if s]
    if not sources:
        if source_id:
            outcome.skipped.append(f"Fonte nao encontrada: {source_id}")
        return outcome

    run_id = db.start_run("summarize")
    begin_run(run_id)

    for src in sources:
        sid = src["source_id"]
        chapters = db.get_chapters_for_source(sid)
        if not chapters:
            outcome.skipped.append(f"{src['citekey']}: sem capitulos (rode harvest antes)")
            continue

        outcome.source_ids.append(sid)
        set_source(sid)
        for chapter in chapters:
            fresh = (
                chapter.get("summary")
                and chapter.get("summary_checksum") == chapter["chapter_checksum"]
            )
            if fresh:
                outcome.chapters_skipped += 1
                continue

            text = chapter_text_for_summary(db, chapter["chapter_id"])
            if not text.strip():
                outcome.chapters_skipped += 1
                logger.debug("Capitulo sem texto persistido: %s", chapter["chapter_id"])
                continue

            report(
                progress,
                "summarize",
                f"Resumindo capitulo: {chapter.get('title') or chapter['chapter_id']}",
                current_item=chapter["chapter_id"],
            )
            output = _summarize_chapter_text(
                cfg, db, src["title"], chapter.get("title") or "", text, outcome
            )
            topics = [t.strip() for t in output.key_topics if t.strip()][: cfg.summarize.max_topics]
            db.update_chapter_summary(
                chapter["chapter_id"],
                output.summary.strip(),
                topics,
                chapter["chapter_checksum"],
                cfg.llm.summarize.model,
                now_utc_iso(),
            )
            idx.upsert_chapter_summary(
                chapter["chapter_id"],
                chapter_summary_document(chapter.get("title") or "", output.summary, topics),
                chapter_summary_metadata(chapter, src),
            )
            outcome.chapters_summarized += 1

        _summarize_source(cfg, db, src, outcome)
        refresh_chapter_map(cfg, db, sid)

    set_source(None)
    tracker = get_tracker()
    if tracker:
        for sid in tracker.sources_touched():
            db.add_source_usage(sid, tracker.summary_for_source(sid).as_dict())
            sync_source_costs_to_vault(cfg, db, sid)

    finish_pipeline_run(db, run_id)
    return outcome


def _summarize_source(cfg: AppConfig, db: StateDB, src: dict, outcome: SummarizeOutcome) -> None:
    """Reduce the source's chapter summaries into one general summary."""
    chapters = db.get_chapters_with_summaries(src["source_id"])
    if not chapters:
        return
    checksum = source_summary_checksum(chapters)
    if src.get("summary") and src.get("summary_checksum") == checksum:
        return

    rendered = "\n\n".join(
        f"### {c.get('title') or c['chapter_id']}\n{c['summary']}" for c in chapters
    )
    authors = ", ".join(json.loads(src.get("authors") or "[]"))
    mapping = {
        "language": cfg.language,
        "domain": cfg.domain.name,
        "max_topics": str(cfg.summarize.max_topics),
        "source_title": src["title"],
        "source_authors": authors,
        "chapter_summaries": rendered,
    }
    response, hit = _call_summary_llm(
        cfg, db, "source_summary.md", mapping, f"resumo geral: {src['citekey']}"
    )
    outcome.cache_hits += int(hit)
    outcome.llm_calls += int(not hit)
    output = _parse_source_summary(response)
    topics = [t.strip() for t in output.key_topics if t.strip()][: cfg.summarize.max_topics]
    db.update_source_summary(
        src["source_id"],
        output.summary.strip(),
        topics,
        checksum,
        cfg.llm.summarize.model,
        now_utc_iso(),
    )
    outcome.sources_summarized += 1


# ── Vault rendering (deterministic, no LLM) ────────────────────────────


def _page_label(page_range: tuple[int, int] | None) -> str:
    """`p. 41-58` / `p. 41`, or empty for a source with no pages (ADR-013)."""
    if not page_range:
        return ""
    lo, hi = page_range
    return f"p. {lo}" if lo == hi else f"p. {lo}-{hi}"


def render_chapter_map(cfg: AppConfig, db: StateDB, source_id: str) -> str:
    """Render the `auto-chapter-map` block for one source."""
    from zettel.vault import literature_chunk_wikilink_for_row, permanent_wikilink

    src = db.get_source(source_id)
    if not src:
        return "_Fonte nao encontrada._"
    chapters = db.get_chapters_for_source(source_id)
    if not chapters:
        return "_Nenhum capitulo registrado ainda._"

    counts = db.get_chapter_note_counts(source_id)
    ranges = db.get_chapter_page_ranges(source_id)
    chunks_by_chapter: dict[str, list[dict]] = {}
    for chunk in db.get_chunks_for_source(source_id):
        chunks_by_chapter.setdefault(chunk["chapter_id"], []).append(chunk)

    max_links = cfg.summarize.max_links_per_chapter
    lines: list[str] = []
    for chapter in chapters:
        cid = chapter["chapter_id"]
        title = chapter.get("title") or cid
        n_notes = counts.get(cid, 0)

        facts = [f for f in (_page_label(ranges.get(cid)),) if f]
        plural = "nota permanente" if n_notes == 1 else "notas permanentes"
        facts.append(f"{n_notes} {plural}")
        stale = bool(
            chapter.get("summary")
            and chapter.get("summary_checksum") != chapter["chapter_checksum"]
        )
        if stale:
            facts.append("resumo defasado")

        lines.append(f"### {title}")
        lines.append(" - ".join(facts))
        lines.append("")
        lines.append(chapter.get("summary") or "_Sem resumo. Rode `zettel summarize`._")
        lines.append("")

        lit_links = [
            literature_chunk_wikilink_for_row(src["citekey"], c, with_alias=True)
            for c in sorted(
                (
                    c
                    for c in chunks_by_chapter.get(cid, [])
                    if c.get("status") in ("approved", "persisted")
                ),
                key=lambda c: c.get("chunk_index") or 0,
            )
        ]
        if lit_links:
            lines.append(f"Notas de literatura: {' '.join(lit_links[:max_links])}")
        ztl_links = [
            permanent_wikilink(n["note_id"], n.get("title") or "", path=n.get("path"))
            for n in db.get_notes_for_chapter(cid)
        ]
        if ztl_links:
            lines.append(f"Notas permanentes: {' '.join(ztl_links[:max_links])}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _literature_index_path(cfg: AppConfig, src: dict) -> Path:
    from zettel.vault import literature_index_filename

    return (
        cfg.vault_path / "20_Literature" / literature_index_filename(src["citekey"], src["title"])
    )


def _write_blocks(cfg: AppConfig, path: Path, blocks: dict[str, str]) -> None:
    """Own the two summary sections on the literature index note.

    Mirrors ``topic_index._write_block``: this module scaffolds its own headings
    so the note builders never have to know about them.
    """
    from zettel.vault import safe_update_managed_blocks, upsert_managed_block

    if not path.is_file():
        return
    content = path.read_text(encoding="utf-8")
    changed = False
    for name, heading in (
        (SOURCE_SUMMARY_BLOCK, _SOURCE_SUMMARY_HEADING),
        (CHAPTER_MAP_BLOCK, _CHAPTER_MAP_HEADING),
    ):
        if name in blocks and f"zettel:{name}:start" not in content:
            # upsert_managed_block adds its own blank line before an appended
            # block, so the heading gets one trailing newline, not two.
            content = content.rstrip() + f"\n\n{heading}\n"
            content = upsert_managed_block(content, name, "")
            changed = True
    if changed:
        path.write_text(content, encoding="utf-8")
    safe_update_managed_blocks(path, blocks, vault_timezone=cfg.vault_timezone)


def refresh_chapter_map(cfg: AppConfig, db: StateDB, source_id: str) -> bool:
    """Rewrite both summary blocks on the source's literature index note.

    Deterministic and cheap — no LLM, no embedding. ``connect`` calls this so
    the per-chapter note counts stay live as notes are written.
    """
    src = db.get_source(source_id)
    if not src:
        return False
    path = _literature_index_path(cfg, src)
    if not path.is_file():
        logger.debug("Nota indice de literatura ausente para %s", src["citekey"])
        return False

    summary = src.get("summary")
    if summary:
        topics = parse_topics(src.get("summary_topics"))
        block = summary if not topics else f"{summary}\n\n**Termos-chave:** {', '.join(topics)}"
    else:
        block = "_Sem resumo geral. Rode `zettel summarize`._"

    _write_blocks(
        cfg,
        path,
        {
            SOURCE_SUMMARY_BLOCK: block + "\n",
            CHAPTER_MAP_BLOCK: render_chapter_map(cfg, db, source_id),
        },
    )
    with contextlib.suppress(OSError):
        db.update_source_texts(source_id, lit_body=path.read_text(encoding="utf-8"))
    return True
