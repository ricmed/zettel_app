"""Obsidian vault I/O — frontmatter, managed blocks, safe edits."""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ── Frontmatter ────────────────────────────────────────────────────────


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from Markdown. Returns (metadata, body)."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    body = parts[2].lstrip("\n")
    return meta, body


def render_frontmatter(metadata: dict[str, Any]) -> str:
    """Render a dict as YAML frontmatter block."""
    dumped = yaml.dump(metadata, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{dumped}---\n"


def compose_note(metadata: dict[str, Any], body: str) -> str:
    """Combine frontmatter and body into a full Markdown document."""
    return render_frontmatter(metadata) + "\n" + body


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
# Crockford base32 ULID (no I, L, O, U), 26 chars — same alphabet as sync/article.
_NOTE_ULID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_ZTL_PREFIX_RE = re.compile(r"^(?:ZTL\s*-\s*)+", re.IGNORECASE)
# ``[[ZTL - ULID]]`` or ``[[ZTL - ZTL - ULID]]`` with no slug after the id.
_BARE_PERMANENT_WIKILINK_RE = re.compile(
    r"\[\[(?:ZTL\s*-\s*)+([0-9A-HJKMNP-TV-Z]{26})\]\]"
)


def _wikilink_target_matches(target: str, link_targets: set[str]) -> bool:
    norm = target.replace("\\", "/").strip()
    if norm in link_targets:
        return True
    base = norm.rsplit("/", 1)[-1]
    return base in link_targets


def strip_matching_wikilinks(text: str, link_targets: set[str]) -> str:
    """Remove ``[[wikilinks]]`` whose target matches any entry in ``link_targets``.

    Targets may be bare stems or path-qualified (``Citekey/LIT - ...``).
    Cleans empty list bullets left behind. Safe to run on managed blocks.
    """
    if not link_targets or not text:
        return text

    def repl(match: re.Match[str]) -> str:
        if _wikilink_target_matches(match.group(1), link_targets):
            return ""
        return match.group(0)

    cleaned = _WIKILINK_RE.sub(repl, text)
    lines: list[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if stripped in ("-", "- ()", "←", "← "):
            continue
        if re.match(r"^-\s*Ref\. literatura:\s*$", stripped):
            line = re.sub(
                r"Ref\. literatura:\s*$",
                "Ref. literatura: _fonte removida_",
                line,
            )
        lines.append(line.rstrip())
    return "\n".join(lines)


# ── Managed Blocks ─────────────────────────────────────────────────────


def _block_pattern(name: str) -> tuple[str, str]:
    return (
        f"<!-- zettel:{name}:start -->",
        f"<!-- zettel:{name}:end -->",
    )


def read_managed_block(content: str, block_name: str) -> str | None:
    """Extract the content of a managed block, or None if not found."""
    start_tag, end_tag = _block_pattern(block_name)
    start_idx = content.find(start_tag)
    if start_idx == -1:
        return None
    end_idx = content.find(end_tag, start_idx)
    if end_idx == -1:
        return None
    inner_start = start_idx + len(start_tag)
    return content[inner_start:end_idx].strip()


def upsert_managed_block(content: str, block_name: str, new_inner: str) -> str:
    """Insert or replace a managed block. Preserves content outside the block."""
    start_tag, end_tag = _block_pattern(block_name)
    block_text = f"{start_tag}\n{new_inner}\n{end_tag}"

    start_idx = content.find(start_tag)
    if start_idx == -1:
        if not content.endswith("\n"):
            content += "\n"
        return content + "\n" + block_text + "\n"

    end_idx = content.find(end_tag, start_idx)
    if end_idx == -1:
        return content + "\n" + block_text + "\n"

    before = content[:start_idx]
    after = content[end_idx + len(end_tag):]
    return before + block_text + after


# ── Safe File I/O ──────────────────────────────────────────────────────


def safe_write_note(path: Path, metadata: dict[str, Any], body: str) -> None:
    """Write a note file, creating directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = compose_note(metadata, body)
    path.write_text(content, encoding="utf-8")
    logger.debug("Nota salva: %s", path)


def safe_update_managed_blocks(path: Path, blocks: dict[str, str]) -> None:
    """Update only the managed blocks in an existing note, preserving manual edits.

    When the file content actually changes, bumps ``updated_at`` in frontmatter
    (if present). Idempotent upserts that leave the body unchanged are a no-op.
    """
    if not path.exists():
        logger.warning("Arquivo não encontrado para atualização: %s", path)
        return

    original = path.read_text(encoding="utf-8")
    content = original
    for block_name, inner in blocks.items():
        content = upsert_managed_block(content, block_name, inner)
    if content == original:
        return

    meta, body = parse_frontmatter(content)
    if meta:
        meta["updated_at"] = datetime.now().isoformat()
        content = compose_note(meta, body)
    path.write_text(content, encoding="utf-8")
    logger.debug("Blocos gerenciados atualizados em: %s", path)


# ── Vault Structure ───────────────────────────────────────────────────


VAULT_DIRS = [
    "00_Inbox",
    "00_Inbox/Review",
    "10_Sources",
    "20_Literature",
    "30_Permanent",
    "40_MOCs",
    "90_Assets",
]


def init_vault(vault_path: Path) -> None:
    """Create the vault directory structure."""

    # First, delete the vault directory if it exists
    if vault_path.exists():
        shutil.rmtree(vault_path)
        logger.info("Vault apagado: %s", vault_path)

    # Then, create the vault directory structure
    for d in VAULT_DIRS:
        (vault_path / d).mkdir(parents=True, exist_ok=True)
    logger.info("Vault inicializado em: %s", vault_path)


_AUTHOR_YEAR = re.compile(r"^([A-Za-z]+\d{4})")
_GENERIC_SECTION_TOPICS = frozenset({"documento completo", ""})
_TOPIC_SLUG_MAX = 40


def _slug(text: str, max_len: int = 100) -> str:
    """Create a URL-safe slug from text."""
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s[:max_len].rstrip("-")


def author_year_label(citekey: str) -> str:
    """Short display key used in SRC/LIT filenames.

    Inverse of harvester ``_generate_citekey``: ``{surname}{year}{TitleSlug}``
    becomes ``{surname}{year}``. Citekeys without that prefix are returned
    unchanged (after stripping a leading ``@``).
    """
    key = citekey.lstrip("@")
    match = _AUTHOR_YEAR.match(key)
    return match.group(1) if match else key


def literature_source_dirname(citekey: str) -> str:
    """Per-source folder under ``20_Literature/`` or ``00_Inbox/Review/`` (no ``@``)."""
    return citekey.lstrip("@")


def source_note_filename(citekey: str, title: str) -> str:
    return note_filename("SRC", author_year_label(citekey), title)


def source_note_stem(citekey: str, title: str) -> str:
    return source_note_filename(citekey, title).removesuffix(".md")


def literature_index_filename(citekey: str, title: str = "") -> str:
    """Filename for the per-source literature index note."""
    slug = _slug(title) if title else "index"
    return f"LIT - {author_year_label(citekey)} - {slug}.md"


def literature_index_stem(citekey: str, title: str = "") -> str:
    """Wikilink stem (no .md) for the literature index."""
    return literature_index_filename(citekey, title).removesuffix(".md")


def _page_token(
    page_in_book: int | None,
    page_in_file: int | None,
    chunk_index: int,
) -> str:
    page = page_in_book if page_in_book is not None else page_in_file
    if page is not None:
        return f"p{int(page):03d}"
    return f"c{int(chunk_index):04d}"


def _section_topic(section_path: str | None) -> str:
    if not section_path:
        return ""
    last = section_path.split(">")[-1].strip()
    if last.lower() in _GENERIC_SECTION_TOPICS:
        return ""
    return last


def literature_chunk_topic(
    section_path: str | None = None,
    summary: str | None = None,
) -> str:
    """Human topic for H1 headings and index link aliases."""
    topic = _section_topic(section_path)
    if topic:
        return topic
    if summary and summary.strip():
        words = summary.strip().split()
        return " ".join(words[:8]) if words else "nota"
    return "nota"


def _topic_slug(section_path: str | None, summary: str | None) -> str:
    topic = _section_topic(section_path)
    if topic:
        slug = _slug(topic, max_len=_TOPIC_SLUG_MAX)
        if slug:
            return slug
    if summary and summary.strip():
        slug = _slug(summary, max_len=_TOPIC_SLUG_MAX)
        if slug:
            return slug
    return "nota"


def literature_chunk_filename(
    citekey: str,
    *,
    chunk_index: int,
    page_in_book: int | None = None,
    page_in_file: int | None = None,
    section_path: str | None = None,
    summary: str | None = None,
) -> str:
    """Basename for a granular LIT (same name in Review and 20_Literature)."""
    label = author_year_label(citekey)
    page = _page_token(page_in_book, page_in_file, chunk_index)
    topic = _topic_slug(section_path, summary)
    return f"LIT - {label} - {page} - {topic}-{int(chunk_index):04d}.md"


def _summary_from_chunk(chunk: dict[str, Any]) -> str:
    raw = chunk.get("summary_json")
    if not raw:
        return ""
    if isinstance(raw, dict):
        return str(raw.get("summary") or "")
    try:
        data = json.loads(raw)
        return str(data.get("summary") or "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ""


def literature_chunk_filename_for_row(citekey: str, chunk: dict[str, Any]) -> str:
    return literature_chunk_filename(
        citekey,
        chunk_index=int(chunk.get("chunk_index") or 0),
        page_in_book=chunk.get("page_in_book"),
        page_in_file=chunk.get("page_in_file"),
        section_path=chunk.get("section_path") or "",
        summary=_summary_from_chunk(chunk),
    )


def literature_chunk_wikilink(
    citekey: str,
    *,
    chunk_index: int,
    page_in_book: int | None = None,
    page_in_file: int | None = None,
    section_path: str | None = None,
    summary: str | None = None,
    alias: str | None = None,
) -> str:
    """Path-qualified wikilink to a granular LIT (unique even if stems collide)."""
    dirname = literature_source_dirname(citekey)
    stem = literature_chunk_filename(
        citekey,
        chunk_index=chunk_index,
        page_in_book=page_in_book,
        page_in_file=page_in_file,
        section_path=section_path,
        summary=summary,
    ).removesuffix(".md")
    target = f"{dirname}/{stem}"
    if alias:
        return f"[[{target}|{alias}]]"
    return f"[[{target}]]"


def literature_index_link_label(
    *,
    page_in_book: int | None = None,
    page_in_file: int | None = None,
    section_path: str | None = None,
    summary: str | None = None,
) -> str:
    page = page_in_book if page_in_book is not None else page_in_file
    topic = literature_chunk_topic(section_path, summary)
    if page is not None:
        return f"p. {page} — {topic}"
    return topic


def literature_chunk_wikilink_for_row(
    citekey: str, chunk: dict[str, Any], *, with_alias: bool = False
) -> str:
    summary = _summary_from_chunk(chunk)
    section_path = chunk.get("section_path") or ""
    alias = None
    if with_alias:
        alias = literature_index_link_label(
            page_in_book=chunk.get("page_in_book"),
            page_in_file=chunk.get("page_in_file"),
            section_path=section_path,
            summary=summary,
        )
    return literature_chunk_wikilink(
        citekey,
        chunk_index=int(chunk.get("chunk_index") or 0),
        page_in_book=chunk.get("page_in_book"),
        page_in_file=chunk.get("page_in_file"),
        section_path=section_path,
        summary=summary,
        alias=alias,
    )


# ── Note Builders ─────────────────────────────────────────────────────


def build_source_note(
    source_id: str,
    citekey: str,
    title: str,
    authors: list[str],
    year: int | None,
    origin_path: str,
    origin_type: str,
    checksum: str,
    origin: str = "pipeline",
    document_type: str | None = None,
    biblio_fields: dict[str, Any] | None = None,
    abnt_reference: str | None = None,
    total_pages_file: int | None = None,
    total_pages_book: int | None = None,
    page_offset: int | None = None,
    page_offset_confidence: str | None = None,
    content_start_file_page: int | None = None,
    content_start_book_page: int | None = None,
    processing_status: str | None = None,
    total_chunks: int | None = None,
    docling_config_hash: str | None = None,
    cost_usd_total: float | None = None,
    cost_usd_llm: float | None = None,
    cost_usd_embedding: float | None = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    tokens_embedding: int | None = None,
) -> tuple[dict[str, Any], str]:
    """Build frontmatter and body for a Source (SRC) note.

    Bibliographic fields appear both separately in the frontmatter and grouped
    as `abnt_reference` for easy citation copy-paste. Links to the literature
    *index* (not a monolithic LIT).
    """
    now = datetime.now().isoformat()
    meta: dict[str, Any] = {
        "type": "source",
        "source_id": source_id,
        "title": title,
        "author": authors,
        "year": year,
        "origin_path": origin_path,
        "origin_type": origin_type,
        "checksum": checksum,
        "origin": origin,
        "created_at": now,
        "updated_at": now,
    }
    if document_type:
        meta["document_type"] = document_type
    if biblio_fields:
        for key, value in biblio_fields.items():
            if value is None:
                continue
            if isinstance(value, list) and not value:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            meta[key] = value
    if abnt_reference:
        meta["abnt_reference"] = abnt_reference
    if total_pages_file is not None:
        meta["total_pages_file"] = total_pages_file
    if total_pages_book is not None:
        meta["total_pages_book"] = total_pages_book
    if page_offset is not None:
        meta["page_offset"] = page_offset
    if page_offset_confidence:
        meta["page_offset_confidence"] = page_offset_confidence
    if content_start_file_page is not None:
        meta["content_start_file_page"] = content_start_file_page
    if content_start_book_page is not None:
        meta["content_start_book_page"] = content_start_book_page
    if processing_status:
        meta["processing_status"] = processing_status
    if total_chunks is not None:
        meta["total_chunks"] = total_chunks
    if docling_config_hash:
        meta["docling_config_hash"] = docling_config_hash
    if cost_usd_total is not None:
        meta["cost_usd_total"] = round(float(cost_usd_total), 6)
    if cost_usd_llm is not None:
        meta["cost_usd_llm"] = round(float(cost_usd_llm), 6)
    if cost_usd_embedding is not None:
        meta["cost_usd_embedding"] = round(float(cost_usd_embedding), 6)
    if tokens_prompt is not None:
        meta["tokens_prompt"] = int(tokens_prompt)
    if tokens_completion is not None:
        meta["tokens_completion"] = int(tokens_completion)
    if tokens_embedding is not None:
        meta["tokens_embedding"] = int(tokens_embedding)

    index_stem = literature_index_stem(citekey, title)
    lit_link = f"[[{index_stem}]]"
    body = f"# {title}\n\n"
    body += f"**Autores**: {', '.join(authors) if authors else 'Desconhecido'}\n"
    body += f"**Ano**: {year or 'N/A'}\n"
    if document_type:
        body += f"**Tipo documental**: {document_type}\n"
    body += f"**Tipo de arquivo**: {origin_type}\n"
    if total_pages_file is not None:
        body += f"**Paginas (arquivo)**: {total_pages_file}\n"
    if content_start_file_page is not None:
        body += (
            f"**Inicio do conteudo (arquivo)**: p. {content_start_file_page}"
        )
        if content_start_book_page is not None:
            body += f" = p. impressa {content_start_book_page}"
        if page_offset_confidence:
            body += f" ({page_offset_confidence})"
        body += "\n"
    elif page_offset is not None:
        body += f"**Offset pagina**: {page_offset}"
        if page_offset_confidence:
            body += f" ({page_offset_confidence})"
        body += "\n"
    body += "\n"
    if abnt_reference:
        body += f"## Referencia ABNT\n\n{abnt_reference}\n\n"
    body += f"## Indice de Literatura\n\n{lit_link}\n"
    return meta, body


def sync_source_costs_to_vault(cfg: Any, db: Any, source_id: str) -> bool:
    """Copy accumulated cost fields from SQLite onto the SRC note frontmatter."""
    row = db.get_source(source_id)
    if not row:
        return False
    citekey = row.get("citekey") or source_id.lstrip("@")
    title = row.get("title") or citekey
    src_name = source_note_filename(citekey, title)
    path = cfg.vault_path / "10_Sources" / src_name
    if not path.exists():
        # Fallback: scan for source_id in frontmatter
        sources_dir = cfg.vault_path / "10_Sources"
        if not sources_dir.exists():
            return False
        path = None
        for candidate in sources_dir.glob("SRC - *.md"):
            meta, _ = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            if meta.get("source_id") == source_id:
                path = candidate
                break
        if path is None:
            return False

    content = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(content)
    meta["cost_usd_total"] = round(float(row.get("cost_usd_total") or 0), 6)
    meta["cost_usd_llm"] = round(float(row.get("cost_usd_llm") or 0), 6)
    meta["cost_usd_embedding"] = round(float(row.get("cost_usd_embedding") or 0), 6)
    meta["tokens_prompt"] = int(row.get("tokens_prompt") or 0)
    meta["tokens_completion"] = int(row.get("tokens_completion") or 0)
    meta["tokens_embedding"] = int(row.get("tokens_embedding") or 0)
    meta["updated_at"] = datetime.now().isoformat()
    path.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")
    return True


def build_literature_index_note(
    source_id: str,
    citekey: str,
    title: str,
    approved_links: list[str] | None = None,
    origin: str = "pipeline",
) -> tuple[dict[str, Any], str]:
    """Build the per-source literature index (replaces the old monolithic LIT)."""
    now = datetime.now().isoformat()
    meta = {
        "type": "literature_index",
        "source_id": source_id,
        "citekey": citekey,
        "literature_id": f"{source_id}::index",
        "language": "pt-BR",
        "origin": origin,
        "created_at": now,
        "updated_at": now,
    }
    body = f"# {title} — Indice de Literatura\n\n"
    body += f"← [[{source_note_stem(citekey, title)}]]\n\n"
    body += "## Notas de Literatura aprovadas\n\n"
    body += "<!-- zettel:auto-lit-index:start -->\n"
    if approved_links:
        body += "\n".join(f"- {link}" for link in approved_links) + "\n"
    else:
        body += "_Nenhuma nota granular aprovada ainda._\n"
    body += "<!-- zettel:auto-lit-index:end -->\n"
    return meta, body


def build_literature_chunk_note(
    *,
    source_id: str,
    citekey: str,
    title: str,
    chunk_id: str,
    chunk_index: int,
    literature_id: str,
    summary: str,
    key_concepts: list[str],
    candidates: list[dict[str, Any]],
    images: list[dict[str, Any]] | None = None,
    section_path: str = "",
    source_text: str = "",
    page_in_file: int | None = None,
    page_in_book: int | None = None,
    page_confidence: str = "unknown",
    status: str = "awaiting_review",
    review_confidence: float | None = None,
    llm_model: str = "",
    processing_time_ms: int | None = None,
    origin: str = "pipeline",
) -> tuple[dict[str, Any], str]:
    """Build a granular literature note for one chunk (draft or approved)."""
    now = datetime.now().isoformat()
    page_label = page_in_book if page_in_book is not None else page_in_file
    page_str = f"p. {page_label}" if page_label is not None else "p. ?"
    meta: dict[str, Any] = {
        "type": "literature",
        "source_id": source_id,
        "citekey": citekey,
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "literature_id": literature_id,
        "page_in_file": page_in_file,
        "page_in_book": page_in_book,
        "page_confidence": page_confidence,
        "status": status,
        "language": "pt-BR",
        "origin": origin,
        "created_at": now,
        "updated_at": now,
    }
    if review_confidence is not None:
        meta["review_confidence"] = review_confidence
    if llm_model:
        meta["llm_model"] = llm_model
    if processing_time_ms is not None:
        meta["processing_time_ms"] = processing_time_ms
    if section_path:
        meta["section_path"] = section_path

    index_stem = literature_index_stem(citekey, title)
    topic = literature_chunk_topic(section_path, summary)
    body = f"# {topic} ({page_str})\n\n"
    body += "## Resumo\n\n"
    body += f"{summary.strip() or '_Sem resumo._'}\n\n"
    body += "## Conceitos-chave\n\n"
    if key_concepts:
        body += " ".join(
            f"#{c.lstrip('#')}" if not c.startswith("#") else c for c in key_concepts
        ) + "\n\n"
    else:
        body += "_Nenhum._\n\n"
    body += "## Candidatos a Nota Permanente\n\n"
    if candidates:
        for cand in candidates:
            thesis = cand.get("thesis") or cand.get("definition") or "?"
            body += f"- [ ] {thesis}\n"
        body += "\n"
    else:
        body += "_Nenhum candidato._\n\n"
    excerpt = (source_text or "").strip() or "_Trecho nao disponivel._"
    body += "## Trecho da fonte\n\n"
    body += "<!-- zettel:auto-source-excerpt:start -->\n"
    body += f"{excerpt}\n"
    body += "<!-- zettel:auto-source-excerpt:end -->\n\n"
    body += "## Imagens Relacionadas\n\n"
    if images:
        for img in images:
            path = img.get("path", "")
            desc = img.get("description") or ""
            line = f"- ![[{path}]]"
            if desc:
                line += f" — {desc}"
            body += line + "\n"
        body += "\n"
    else:
        body += "_Nenhuma._\n\n"
    body += f"## Backlink\n\n← [[{index_stem}]]\n"
    return meta, body


def build_permanent_note_body(
    thesis: str,
    definition: str,
    intuition: str,
    example: str,
    limits: str,
    connections: list[dict] | None = None,
    literature_ref: str = "",
    source_ref: str = "",
    source_locator: str = "",
    images: list[dict] | None = None,
) -> str:
    """Build the Markdown body for a Permanent (ZTL) note.

    `images` is a list of {"path": ..., "description": ...} for figures deemed
    essential to the concept; rendered as a "## Figuras" section with embeds.
    """
    parts: list[str] = []
    parts.append(f"> **Tese**: {thesis}\n")
    parts.append(f"## Definição\n\n{definition}\n")
    if intuition:
        parts.append(f"## Intuição\n\n{intuition}\n")
    if example:
        parts.append(f"## Exemplo\n\n{example}\n")
    if limits:
        parts.append(f"## Limites\n\n{limits}\n")
    if images:
        fig_lines: list[str] = []
        for img in images:
            embed = f"![[{img['path']}]]"
            desc = img.get("description") or ""
            fig_lines.append(f"{embed}\n\n{desc}\n" if desc else f"{embed}\n")
        parts.append("## Figuras\n\n" + "\n".join(fig_lines))
    parts.append(f"## Fonte\n\n- Ref. literatura: {literature_ref}")
    if source_ref:
        parts.append(f"- Fonte (SRC): {source_ref}")
    if source_locator:
        parts.append(f"- Localizador: {source_locator}")
    parts.append("")
    if connections:
        conn_lines: list[str] = []
        for c in connections:
            link = c.get("wiki_link", c.get("related_note_id", "?"))
            rtype = c.get("relation_type", "related")
            # RelationType is a str Enum — f"{enum}" renders "RelationType.X", not "x".
            if hasattr(rtype, "value"):
                rtype = rtype.value
            rtype = str(rtype)
            desc = c.get("description", "")
            line = f"- {link} ({rtype})"
            if desc:
                line += f" -- {desc}"
            conn_lines.append(line)
        parts.append(f"## Conexões\n\n" + "\n".join(conn_lines) + "\n")
    return "\n".join(parts)


def note_filename(prefix: str, identifier: str, title: str) -> str:
    """Build a standardized filename: PREFIX - ID - slug.md"""
    slug = _slug(title)
    return f"{prefix} - {identifier} - {slug}.md"


def normalize_note_id(raw: str) -> str | None:
    """Extract a canonical note_id from LLM / wikilink noise.

    Accepts a bare ULID, ``ZTL - ULID``, ``ZTL - ULID - slug``, ``[[...]]``,
    and a repeated ``ZTL -`` prefix. If no ULID is present, returns the token
    after stripping those wrappers (so short test/legacy ids still resolve).
    Returns None when nothing usable remains.
    """
    if not raw:
        return None
    token = str(raw).strip()
    if token.startswith("[[") and "]]" in token:
        token = token[2:token.index("]]")]
    token = token.split("|", 1)[0].strip()
    if not token:
        return None

    match = _NOTE_ULID_RE.search(token)
    if match:
        return match.group(0)

    stripped = _ZTL_PREFIX_RE.sub("", token).strip()
    if not stripped:
        return None
    head = stripped.split(" - ", 1)[0].strip()
    if head and " " not in head and "/" not in head:
        return head
    return None


def rewrite_bare_permanent_wikilinks(
    text: str,
    lookup_path: Any,
) -> str:
    """Rewrite ``[[ZTL - ULID]]`` / ``[[ZTL - ZTL - ULID]]`` to the file stem.

    ``lookup_path(note_id)`` returns a filesystem path (or None). Links whose
    target is missing are left unchanged.
    """
    if not text:
        return text

    def repl(match: re.Match[str]) -> str:
        note_id = match.group(1)
        path = lookup_path(note_id)
        if not path:
            return match.group(0)
        return permanent_wikilink(note_id, path=path)

    return _BARE_PERMANENT_WIKILINK_RE.sub(repl, text)


def permanent_wikilink(
    note_id: str,
    title: str = "",
    *,
    path: str | Path | None = None,
) -> str:
    """Build a ZTL wikilink that matches the note file on disk when path is known."""
    if path:
        stem = Path(path).stem
        if stem:
            return f"[[{stem}]]"
    if title:
        return f"[[ZTL - {note_id} - {_slug(title)}]]"
    return f"[[ZTL - {note_id}]]"
