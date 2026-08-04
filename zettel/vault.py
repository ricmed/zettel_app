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
) -> tuple[dict[str, Any], str]:
    """Build frontmatter and body for a Source (SRC) note.

    Bibliographic fields appear both separately in the frontmatter and grouped
    as `abnt_reference` for easy citation copy-paste.
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

    lit_link = f"[[LIT - {citekey} - {_slug(title)}]]"
    body = f"# {title}\n\n"
    body += f"**Autores**: {', '.join(authors) if authors else 'Desconhecido'}\n"
    body += f"**Ano**: {year or 'N/A'}\n"
    if document_type:
        body += f"**Tipo documental**: {document_type}\n"
    body += f"**Tipo de arquivo**: {origin_type}\n\n"
    if abnt_reference:
        body += f"## Referencia ABNT\n\n{abnt_reference}\n\n"
    body += f"## Nota de Literatura\n\n{lit_link}\n"
    return meta, body


def build_literature_note(
    source_id: str,
    citekey: str,
    title: str,
    origin: str = "pipeline",
) -> tuple[dict[str, Any], str]:
    """Build frontmatter and body for a Literature (LIT) master note."""
    now = datetime.now().isoformat()
    meta = {
        "type": "literature",
        "source_id": source_id,
        "literature_id": source_id,
        "language": "pt-BR",
        "origin": origin,
        "created_at": now,
        "updated_at": now,
    }
    body = f"# {title}\n\n"
    body += "## Resumo\n\n"
    body += "<!-- zettel:auto-resumo:start -->\n"
    body += "_Preenchido automaticamente durante a extracao._\n"
    body += "<!-- zettel:auto-resumo:end -->\n\n"
    body += "## Conceitos-chave\n\n"
    body += "<!-- zettel:auto-conceitos:start -->\n"
    body += "<!-- zettel:auto-conceitos:end -->\n\n"
    body += "## Potenciais Notas Permanentes\n\n"
    body += "<!-- zettel:auto-candidatos:start -->\n"
    body += "<!-- zettel:auto-candidatos:end -->\n\n"
    body += "## Imagens\n\n"
    body += "<!-- zettel:auto-imagens:start -->\n"
    body += "<!-- zettel:auto-imagens:end -->\n\n"
    body += "## Log de chunks processados\n\n\n"
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


def _slug(text: str, max_len: int = 50) -> str:
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
