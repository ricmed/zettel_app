"""Obsidian vault I/O — frontmatter, managed blocks, safe edits."""

from __future__ import annotations

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
    """Update only the managed blocks in an existing note, preserving manual edits."""
    if not path.exists():
        logger.warning("Arquivo não encontrado para atualização: %s", path)
        return

    content = path.read_text(encoding="utf-8")
    for block_name, inner in blocks.items():
        content = upsert_managed_block(content, block_name, inner)
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


def literature_index_filename(citekey: str, title: str = "") -> str:
    """Filename for the per-source literature index note."""
    slug = _slug(title) if title else "index"
    return f"LIT - @{citekey} - {slug}-index.md" if title else f"LIT - @{citekey} - index.md"


def literature_index_stem(citekey: str, title: str = "") -> str:
    """Wikilink stem (no .md) for the literature index."""
    return literature_index_filename(citekey, title).removesuffix(".md")


def literature_chunk_dirname(citekey: str) -> str:
    return citekey if citekey.startswith("@") else f"@{citekey}"


def draft_chunk_filename(chunk_index: int) -> str:
    return f"chunk_{chunk_index:04d}_draft.md"


def approved_chunk_filename(chunk_index: int) -> str:
    return f"chunk_{chunk_index:04d}.md"


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
    body += f"\nPasta de notas granulares: `20_Literature/{literature_chunk_dirname(citekey)}/`\n"
    return meta, body


def sync_source_costs_to_vault(cfg: Any, db: Any, source_id: str) -> bool:
    """Copy accumulated cost fields from SQLite onto the SRC note frontmatter."""
    row = db.get_source(source_id)
    if not row:
        return False
    citekey = row.get("citekey") or source_id.lstrip("@")
    title = row.get("title") or citekey
    src_name = note_filename("SRC", f"@{citekey}", title)
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
    body += f"← [[SRC - {_slug(title)}]]\n\n"
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

    index_stem = literature_index_stem(citekey, title)
    body = f"# {title} — Chunk {chunk_index} ({page_str})\n\n"
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


def _slug(text: str, max_len: int = 100) -> str:
    """Create a URL-safe slug from text."""
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s[:max_len].rstrip("-")


def note_filename(prefix: str, identifier: str, title: str) -> str:
    """Build a standardized filename: PREFIX - ID - slug.md"""
    slug = _slug(title)
    if prefix == "SRC":
        return f"{prefix} - {slug}.md"
    return f"{prefix} - {identifier} - {slug}.md"
