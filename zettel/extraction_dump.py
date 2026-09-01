"""Markdown dump of persisted extraction text (Docling / MD nativo).

Opt-in diagnostic: writes one file per source under ``cache/extraction-dumps/``
(or a caller-supplied directory). The body is ``sources.extracted_text`` — the
same Markdown harvest used for H1-H2 chapters and H3-H6 ``section_path`` —
not a re-run of Docling.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from zettel.chunk_dump import sanitize_citekey
from zettel.config import AppConfig
from zettel.state import StateDB
from zettel.vault import compose_note

logger = logging.getLogger(__name__)

DEFAULT_DUMP_SUBDIR = "extraction-dumps"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def default_dump_dir(cfg: AppConfig) -> Path:
    return cfg.cache_path / DEFAULT_DUMP_SUBDIR


def dump_filename(citekey: str) -> str:
    return f"extraction-{sanitize_citekey(citekey)}.md"


def list_headings(text: str) -> list[tuple[int, str]]:
    """ATX headings H1-H6 in document order (same regex harvest uses to split)."""
    return [(len(m.group(1)), m.group(2).strip()) for m in _HEADING_RE.finditer(text or "")]


def render_extraction_dump(
    source: dict[str, Any],
    extracted_text: str,
    cfg: AppConfig,
) -> str:
    """Render one source's persisted extraction Markdown."""
    text = extracted_text or ""
    headings = list_headings(text)
    meta: dict[str, Any] = {
        "source_id": source.get("source_id") or "",
        "citekey": source.get("citekey") or "",
        "title": source.get("title") or "",
        "origin_path": source.get("origin_path") or "",
        "origin_type": source.get("origin_type") or "",
        "extractor": "docling" if source.get("origin_type") == "pdf" else "md nativo",
        "chars": len(text),
    }

    lines: list[str] = ["## Headings detectados", ""]
    if headings:
        for level, title in headings:
            lines.append(f"- H{level} {title}")
    else:
        lines.append("- (nenhum heading # a ######)")

    lines.extend(["", "## Texto extraido", ""])
    outline = "\n".join(lines)
    body = outline + "\n" + text
    if not body.endswith("\n"):
        body += "\n"
    return compose_note(meta, body)


def write_extraction_dump(
    dump_dir: Path,
    source: dict[str, Any],
    extracted_text: str,
    cfg: AppConfig,
) -> Path:
    """Write ``extraction-{citekey}.md`` under ``dump_dir`` (overwrites)."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    citekey = source.get("citekey") or source.get("source_id") or "unknown"
    path = dump_dir / dump_filename(str(citekey))
    path.write_text(
        render_extraction_dump(source, extracted_text, cfg), encoding="utf-8",
    )
    return path


def dump_source_extraction(
    cfg: AppConfig,
    db: StateDB,
    source_id: str,
    dump_dir: Path,
) -> Path | None:
    """Load a source from SQLite and write its extraction dump. Returns the path."""
    src = db.get_source(source_id)
    if not src:
        logger.warning("Dump de extracao: fonte nao encontrada: %s", source_id)
        return None
    text = src.get("extracted_text") or ""
    if not text:
        logger.warning(
            "Dump de extracao: fonte %s nao tem texto extraido persistido "
            "(anterior a Fase 0). Reprocesse o arquivo original via harvest.",
            source_id,
        )
        return None
    path = write_extraction_dump(dump_dir, src, text, cfg)
    logger.info("Dump de extracao gravado: %s (%d caracteres)", path, len(text))
    return path


def run_dump_extraction(
    cfg: AppConfig,
    db: StateDB,
    source_id: str | None = None,
    dump_dir: Path | None = None,
) -> dict[str, int]:
    """Export persisted extracted_text for one source or every source.

    Sources without extracted_text are skipped (warning). Missing ``source_id``
    raises ValueError. Read-only on the DB.
    """
    dest = dump_dir or default_dump_dir(cfg)
    if source_id:
        src = db.get_source(source_id)
        if not src:
            raise ValueError(f"Fonte nao encontrada: {source_id}")
        sources = [src]
    else:
        sources = db.list_sources()

    written = 0
    skipped = 0
    for src in sources:
        text = src.get("extracted_text") or ""
        if not text:
            logger.warning(
                "Dump de extracao: fonte %s nao tem texto extraido persistido "
                "(anterior a Fase 0). Pulando.",
                src.get("source_id"),
            )
            skipped += 1
            continue
        path = write_extraction_dump(dest, src, text, cfg)
        logger.info("Dump de extracao gravado: %s", path)
        written += 1
    return {"sources": written, "skipped": skipped}
