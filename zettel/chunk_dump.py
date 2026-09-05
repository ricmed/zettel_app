"""Markdown dump of persisted harvest chunks, for inspecting chunking quality.

Opt-in diagnostic: writes one file per source under ``cache/chunk-dumps/`` (or
a caller-supplied directory). The body is the text SQLite stored — the same
payload ``extract`` will see — not a parallel chunker.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.state import StateDB
from zettel.vault import compose_note

logger = logging.getLogger(__name__)

DEFAULT_DUMP_SUBDIR = "chunk-dumps"

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def default_dump_dir(cfg: AppConfig) -> Path:
    return cfg.cache_path / DEFAULT_DUMP_SUBDIR


def sanitize_citekey(citekey: str) -> str:
    """Make a citekey safe as a Windows filename stem."""
    safe = _UNSAFE_FILENAME.sub("_", citekey).strip("._-")
    return safe or "unknown"


def dump_filename(citekey: str) -> str:
    return f"chunks-{sanitize_citekey(citekey)}.md"


def overlap_prefix_len(prev: str, curr: str, cap: int) -> int:
    """Longest prefix of ``curr`` that is a suffix of ``prev``, capped at ``cap``.

    Used as a diagnostic for ``chunk_overlap``: consecutive LangChain pieces
    should share up to ``cap`` characters at the boundary.
    """
    if not prev or not curr or cap <= 0:
        return 0
    max_n = min(len(prev), len(curr), cap)
    for n in range(max_n, 0, -1):
        if prev.endswith(curr[:n]):
            return n
    return 0


def sort_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(c: dict[str, Any]) -> tuple[int, str]:
        idx = c.get("chunk_index")
        order = int(idx) if idx is not None else 10**9
        return order, str(c.get("chunk_id") or "")

    return sorted(chunks, key=_key)


def _annotate_overlap(chunks: list[dict[str, Any]], overlap_cap: int) -> list[dict[str, Any]]:
    ordered = sort_chunks(chunks)
    out: list[dict[str, Any]] = []
    prev_text = ""
    for chunk in ordered:
        item = dict(chunk)
        text = item.get("text") or ""
        item["overlap_prev"] = overlap_prefix_len(prev_text, text, overlap_cap) if prev_text else 0
        out.append(item)
        prev_text = text
    return out


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def render_chunk_dump(
    source: dict[str, Any],
    chunks: list[dict[str, Any]],
    cfg: AppConfig,
) -> str:
    """Render one source's persisted chunks as a Markdown document."""
    overlap_cap = int(cfg.chunking.chunk_overlap)
    annotated = _annotate_overlap(chunks, overlap_cap)
    lengths = [len(c.get("text") or "") for c in annotated]
    n = len(annotated)
    stats: dict[str, Any] = {
        "n_chunks": n,
        "chars_total": sum(lengths),
    }
    if lengths:
        stats["chars_min"] = min(lengths)
        stats["chars_max"] = max(lengths)
        stats["chars_mean"] = round(sum(lengths) / n, 1)

    meta: dict[str, Any] = {
        "source_id": source.get("source_id") or "",
        "citekey": source.get("citekey") or "",
        "title": source.get("title") or "",
        "origin_path": source.get("origin_path") or "",
        "origin_type": source.get("origin_type") or "",
        "chunking": {
            "chunk_size": cfg.chunking.chunk_size,
            "chunk_overlap": cfg.chunking.chunk_overlap,
            "min_section_chars": cfg.chunking.min_section_chars,
        },
        "paging": {
            "content_start_file_page": source.get("content_start_file_page"),
            "content_start_book_page": source.get("content_start_book_page"),
            "page_offset": source.get("page_offset"),
            "page_offset_confidence": source.get("page_offset_confidence") or "",
        },
        "stats": stats,
    }

    lines: list[str] = ["## Sumario", ""]
    if not annotated:
        lines.append("- (nenhum chunk persistido)")
    for chunk in annotated:
        idx = chunk.get("chunk_index")
        idx_s = f"{int(idx):03d}" if idx is not None else "???"
        chars = len(chunk.get("text") or "")
        lines.append(
            f"- #{idx_s} chars={chars} "
            f"page_file={_fmt(chunk.get('page_in_file'))} "
            f"page_book={_fmt(chunk.get('page_in_book'))} "
            f"overlap_prev={chunk['overlap_prev']} "
            f"section_path={_fmt(chunk.get('section_path'))}"
        )

    for chunk in annotated:
        idx = chunk.get("chunk_index")
        heading_n = int(idx) if idx is not None else 0
        lines.extend(
            [
                "",
                "---",
                "",
                f"# Chunk {heading_n:03d}",
                "",
                f"- chunk_id: {_fmt(chunk.get('chunk_id'))}",
                f"- chapter_id: {_fmt(chunk.get('chapter_id'))}",
                f"- section_path: {_fmt(chunk.get('section_path'))}",
                f"- page_in_file: {_fmt(chunk.get('page_in_file'))}",
                f"- page_in_book: {_fmt(chunk.get('page_in_book'))}",
                f"- page_confidence: {_fmt(chunk.get('page_confidence'))}",
                f"- chars: {len(chunk.get('text') or '')}",
                f"- overlap_prev: {chunk['overlap_prev']}",
                "",
                chunk.get("text") or "",
            ]
        )

    body = "\n".join(lines).rstrip() + "\n"
    return compose_note(meta, body)


def write_chunk_dump(
    dump_dir: Path,
    source: dict[str, Any],
    chunks: list[dict[str, Any]],
    cfg: AppConfig,
) -> Path:
    """Write ``chunks-{citekey}.md`` under ``dump_dir`` (overwrites)."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    citekey = source.get("citekey") or source.get("source_id") or "unknown"
    path = dump_dir / dump_filename(str(citekey))
    path.write_text(render_chunk_dump(source, chunks, cfg), encoding="utf-8")
    return path


def dump_source_chunks(
    cfg: AppConfig,
    db: StateDB,
    source_id: str,
    dump_dir: Path,
) -> Path | None:
    """Load a source from SQLite and write its chunk dump. Returns the path."""
    src = db.get_source(source_id)
    if not src:
        logger.warning("Dump de chunks: fonte nao encontrada: %s", source_id)
        return None
    chunks = db.get_chunks_for_source(source_id)
    path = write_chunk_dump(dump_dir, src, chunks, cfg)
    logger.info("Dump de chunks gravado: %s (%d chunk(s))", path, len(chunks))
    return path


def run_dump_chunks(
    cfg: AppConfig,
    db: StateDB,
    source_id: str | None = None,
    dump_dir: Path | None = None,
) -> dict[str, int]:
    """Export persisted chunks for one source or every source. Read-only on the DB."""
    dest = dump_dir or default_dump_dir(cfg)
    if source_id:
        src = db.get_source(source_id)
        if not src:
            raise ValueError(f"Fonte nao encontrada: {source_id}")
        sources = [src]
    else:
        sources = db.list_sources()

    written = 0
    for src in sources:
        path = write_chunk_dump(dest, src, db.get_chunks_for_source(src["source_id"]), cfg)
        logger.info("Dump de chunks gravado: %s", path)
        written += 1
    return {"sources": written}
