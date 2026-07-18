"""Image extraction and multimodal description (Fase 3).

Two responsibilities:
  1. During harvest: pull images out of PDFs (Docling) and Markdown, save them
     under 90_Assets/ with content-addressed names, rewrite the extracted text to
     reference the saved files, and return their metadata for DB registration.
  2. During extract: describe pending images with a multimodal LLM, using the
     surrounding text as context, cached by a deterministic call checksum.

Image filenames are content hashes, so extraction is deterministic (the rewritten
text hashes identically across runs) and identical images dedup naturally.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import normalize_text_for_hash, sha256_hex, short_hash
from zettel.state import StateDB

logger = logging.getLogger(__name__)

ASSETS_DIR = "90_Assets"
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_DOCLING_PLACEHOLDER = "<!-- image -->"


# ── IDs and paths ──────────────────────────────────────────────────────


def asset_id_for(source_id: str, image_checksum: str) -> str:
    return f"{source_id}::img::{short_hash(image_checksum)}"


def _asset_relpath(image_checksum: str, ext: str) -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{ASSETS_DIR}/img-{short_hash(image_checksum, 16)}{ext}"


def _save_image(vault_path: Path, data: bytes, ext: str) -> str:
    """Save image bytes content-addressed under 90_Assets/. Returns vault-relative path."""
    checksum = sha256_hex_bytes(data)
    relpath = _asset_relpath(checksum, ext)
    dest = vault_path / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
    return relpath


def sha256_hex_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _context_snippet(text: str, pos: int, radius: int) -> str:
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    return normalize_text_for_hash(text[start:end])


# ── Markdown image extraction ──────────────────────────────────────────


def extract_markdown_images(
    cfg: AppConfig, body: str, source_file: Path
) -> tuple[str, list[dict[str, Any]]]:
    """Copy local images referenced by a Markdown file into 90_Assets and rewrite refs.

    Remote (http/https) images are left untouched. Returns (new_body, images) where
    each image dict has checksum, path (vault-relative), context_snippet.
    """
    if not cfg.images.enabled:
        return body, []

    images: list[dict[str, Any]] = []

    def _replace(match: re.Match) -> str:
        ref = match.group(1).strip()
        if ref.startswith(("http://", "https://")):
            return match.group(0)
        img_path = (source_file.parent / ref).resolve()
        if not img_path.exists() or not img_path.is_file():
            return match.group(0)
        try:
            data = img_path.read_bytes()
        except OSError:
            return match.group(0)
        checksum = sha256_hex_bytes(data)
        relpath = _save_image(cfg.vault_path, data, img_path.suffix or ".png")
        images.append({
            "checksum": checksum,
            "path": relpath,
            "context_snippet": _context_snippet(body, match.start(), cfg.images.context_chars),
        })
        return f"![Imagem]({relpath})"

    new_body = _MD_IMAGE_RE.sub(_replace, body)
    return new_body, images


# ── Docling (PDF) image extraction ─────────────────────────────────────


def extract_docling_images(
    cfg: AppConfig, docling_document: Any, markdown_text: str
) -> tuple[str, list[dict[str, Any]]]:
    """Save Docling-extracted pictures and replace `<!-- image -->` placeholders.

    Placeholders are replaced in document order. Pictures smaller than the configured
    minimum size are dropped (and their placeholder removed). Returns (new_text, images).
    """
    if not cfg.images.enabled:
        return markdown_text, []

    pictures = getattr(docling_document, "pictures", None) or []
    if not pictures:
        return markdown_text, []

    images: list[dict[str, Any]] = []
    text = markdown_text
    for pic in pictures:
        # Locate the next placeholder (document order).
        idx = text.find(_DOCLING_PLACEHOLDER)
        pil_image = _docling_pil(pic, docling_document)
        if pil_image is None:
            continue
        if pil_image.width < cfg.images.min_width or pil_image.height < cfg.images.min_height:
            if idx != -1:
                text = text[:idx] + text[idx + len(_DOCLING_PLACEHOLDER):]
            continue

        data = _png_bytes(pil_image)
        checksum = sha256_hex_bytes(data)
        relpath = _save_image(cfg.vault_path, data, ".png")
        ref = f"![Imagem]({relpath})"
        context_pos = idx if idx != -1 else len(text)
        images.append({
            "checksum": checksum,
            "path": relpath,
            "context_snippet": _context_snippet(text, context_pos, cfg.images.context_chars),
        })
        if idx != -1:
            text = text[:idx] + ref + text[idx + len(_DOCLING_PLACEHOLDER):]
        else:
            text = f"{text}\n\n{ref}"

    return text, images


def _docling_pil(picture: Any, document: Any) -> Any:
    """Return a PIL image for a Docling picture, or None."""
    try:
        img = picture.get_image(document)
        return img
    except Exception as e:
        logger.debug("Nao foi possivel obter imagem Docling: %s", e)
        return None


def _png_bytes(pil_image: Any) -> bytes:
    import io
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return buf.getvalue()


# ── DB registration (chapter resolution) ───────────────────────────────


def register_assets(
    db: StateDB, source_id: str, chapters: list[dict[str, str]], images: list[dict[str, Any]]
) -> None:
    """Register extracted images in the DB, resolving which chapter each fell into.

    The chapter is found by locating the (content-addressed) image path in each
    chapter's text — unambiguous because filenames are unique per image content.
    """
    for img in images:
        chapter_id = _resolve_chapter_id(source_id, chapters, img["path"])
        db.upsert_asset(
            asset_id=asset_id_for(source_id, img["checksum"]),
            source_id=source_id,
            path=img["path"],
            image_checksum=img["checksum"],
            chapter_id=chapter_id,
            context_snippet=img.get("context_snippet", ""),
        )


def reresolve_asset_chapters(
    db: StateDB, source_id: str, chapters: list[dict[str, str]]
) -> int:
    """Re-bind each asset of a source to the chapter that currently contains its path.

    Needed after rechunk/heading changes so orphan chapter_ids (e.g. ch026 after an
    interrupted harvest that only persisted ch000-ch012) do not keep images out of
    Prompt 1's images_context. Returns how many assets had their chapter_id changed.
    """
    updated = 0
    for asset in db.get_assets_for_source(source_id):
        new_ch = _resolve_chapter_id(source_id, chapters, asset["path"])
        if new_ch != asset.get("chapter_id"):
            db.update_asset_chapter(asset["asset_id"], new_ch)
            updated += 1
    if updated:
        logger.info(
            "Assets re-resolvidos para %s: %d chapter_id(s) atualizado(s)",
            source_id, updated,
        )
    return updated


def asset_ids_in_text(db: StateDB, source_id: str, text: str) -> list[str]:
    """Return asset_ids whose vault-relative path appears in text (deterministic fallback)."""
    if not text:
        return []
    ids: list[str] = []
    for asset in db.get_assets_for_source(source_id):
        path = asset.get("path") or ""
        if path and path in text:
            ids.append(asset["asset_id"])
    return ids


def _resolve_chapter_id(
    source_id: str, chapters: list[dict[str, str]], image_path: str
) -> str | None:
    for ch_idx, chapter in enumerate(chapters):
        if image_path in chapter.get("text", ""):
            return f"{source_id}::ch{ch_idx:03d}"
    return None


# ── Multimodal description ─────────────────────────────────────────────


def describe_pending_assets(cfg: AppConfig, db: StateDB) -> int:
    """Describe all pending assets with a multimodal LLM. Returns count described.

    Idempotent: each call is keyed by (prompt, image bytes, context, model) in the
    llm_cache, so re-running costs nothing for already-described images.
    """
    if not cfg.images.enabled:
        return 0

    pending = db.get_pending_assets()
    if not pending:
        return 0

    from zettel.llm import call_llm, load_prompt

    prompt_template = load_prompt(cfg.prompts_path / "image_description.md")
    prompt_hash = sha256_hex(prompt_template)
    model = cfg.images.model or cfg.llm.model
    llm = _get_multimodal_llm(cfg, model)

    described = 0
    for asset in pending:
        img_file = cfg.vault_path / asset["path"]
        if not img_file.exists():
            db.update_asset_description(asset["asset_id"], "", "", status="failed")
            continue

        context = asset.get("context_snippet", "")
        call_checksum = sha256_hex(
            f"{prompt_hash}|{asset['image_checksum']}|{sha256_hex(normalize_text_for_hash(context))}|{model}"
        )
        cached = db.get_cached_llm_response(call_checksum)
        if cached is not None:
            db.update_asset_description(asset["asset_id"], cached, call_checksum)
            described += 1
            continue

        try:
            description = _describe_one(llm, prompt_template, img_file, context)
            db.cache_llm_response(call_checksum, "(image)", description)
            db.update_asset_description(asset["asset_id"], description, call_checksum)
            described += 1
        except Exception as e:
            logger.error("Falha ao descrever imagem %s: %s", asset["asset_id"], e)
            db.update_asset_description(asset["asset_id"], "", call_checksum, status="failed")

    logger.info("Imagens descritas: %d", described)
    return described


def _get_multimodal_llm(cfg: AppConfig, model: str) -> Any:
    """Instantiate a chat model for image description (provider from cfg.llm)."""
    if cfg.llm.provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=cfg.llm.temperature, max_retries=cfg.llm.max_retries)
    if cfg.llm.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=cfg.llm.temperature, max_retries=cfg.llm.max_retries)
    if cfg.llm.provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model, temperature=cfg.llm.temperature)
    raise ValueError(f"Provider nao suportado para descricao de imagem: {cfg.llm.provider}")


def _describe_one(llm: Any, prompt_template: str, img_file: Path, context: str) -> str:
    from langchain_core.messages import HumanMessage

    b64 = base64.b64encode(img_file.read_bytes()).decode("ascii")
    prompt = prompt_template.replace("{context}", context or "(sem contexto textual)")
    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ])
    response = llm.invoke([message])
    return response.content.strip()
