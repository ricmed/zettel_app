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
import time
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
        logger.info("Docling: nenhuma imagem (picture) no documento")
        return markdown_text, []

    logger.info(
        "Docling: extraindo imagens — %d pictures detectadas "
        "(min %dx%d px; placeholders <!-- image -->)",
        len(pictures), cfg.images.min_width, cfg.images.min_height,
    )

    images: list[dict[str, Any]] = []
    text = markdown_text
    skipped_small = 0
    skipped_fail = 0
    for pi, pic in enumerate(pictures, 1):
        # Locate the next placeholder (document order).
        idx = text.find(_DOCLING_PLACEHOLDER)
        pil_image = _docling_pil(pic, docling_document)
        if pil_image is None:
            skipped_fail += 1
            continue
        if pil_image.width < cfg.images.min_width or pil_image.height < cfg.images.min_height:
            skipped_small += 1
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
        logger.info(
            "Docling: imagem %d/%d salva → %s (%dx%d)",
            pi, len(pictures), relpath, pil_image.width, pil_image.height,
        )

    logger.info(
        "Docling: imagens concluidas — %d salvas, %d pequenas ignoradas, %d falhas",
        len(images), skipped_small, skipped_fail,
    )
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
            page_in_file=img.get("page_in_file"),
        )
    logger.info(
        "[SOURCE=%s] %d imagens registradas no StateDB (90_Assets)",
        source_id, len(images),
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


_RETRY_AFTER_RE = re.compile(
    r"try again in\s+([\d.]+)\s*(ms|milliseconds?|s|seconds?|m|minutes?)",
    re.IGNORECASE,
)


class RateLimitExhausted(Exception):
    """Raised when an image description keeps hitting 429 after all retries."""


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect OpenAI/Anthropic/provider rate-limit errors across wrapped exceptions."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        name = type(cur).__name__.lower()
        if "ratelimit" in name:
            return True
        msg = str(cur).lower()
        if (
            "rate_limit" in msg
            or "rate limit" in msg
            or "429" in msg
            or "tokens per min" in msg
            or ("tpm" in msg and "limit" in msg)
        ):
            return True
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
        if cur.__context__ is not None and cur.__context__ is not cur.__cause__:
            stack.append(cur.__context__)
    return False


def _parse_retry_after_seconds(exc: BaseException) -> float | None:
    """Extract wait hint from provider messages like 'Please try again in 392ms'."""
    match = _RETRY_AFTER_RE.search(str(exc))
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("ms") or unit.startswith("millisecond"):
        return value / 1000.0
    if unit.startswith("m") and not unit.startswith("ms"):
        return value * 60.0
    return value


def _rate_limit_wait_seconds(
    exc: BaseException, attempt: int, backoff_max: float
) -> float:
    """Prefer provider hint; else exponential backoff capped at backoff_max."""
    hinted = _parse_retry_after_seconds(exc)
    if hinted is not None:
        # Provider hints are often sub-second while the TPM window is still full;
        # keep a small floor so we don't immediately re-hit the same limit.
        return min(backoff_max, max(hinted, 0.6))
    return min(backoff_max, (2 ** attempt) * 1.0)


def describe_pending_assets(cfg: AppConfig, db: StateDB, *, observer=None) -> int:
    """Describe all pending assets with a multimodal LLM. Returns count described.

    Idempotent: each call is keyed by (prompt, image bytes, context, model) in the
    llm_cache, so re-running costs nothing for already-described images.

    Rate limits (429/TPM): retries with backoff, leaves the asset ``pending`` (never
    ``failed``), and aborts the remaining batch after consecutive exhausted retries
    so a saturated TPM window does not mark hundreds of images as failed.
    """
    if not cfg.images.enabled:
        return 0

    pending = db.get_pending_assets()
    if not pending:
        return 0

    from zettel.llm import fill_template, load_prompt_parts

    prompt_parts = load_prompt_parts(cfg.prompts_path / "image_description.md")
    prompt_hash = sha256_hex(prompt_parts.full_template)
    model = cfg.images.model or cfg.llm.model
    llm = _get_multimodal_llm(cfg, model)

    min_interval = max(0.0, float(cfg.images.min_interval_seconds))
    max_retries = max(0, int(cfg.images.rate_limit_max_retries))
    backoff_max = max(1.0, float(cfg.images.rate_limit_backoff_max))
    abort_after = max(1, int(cfg.images.rate_limit_abort_after))

    described = 0
    consecutive_exhausted = 0
    last_llm_call_at = 0.0
    total_images = len(pending)

    from zettel.usage import clear_progress, set_progress
    from zettel.progress import report

    for idx, asset in enumerate(pending):
        step = idx + 1
        set_progress(step, total_images, "imagem")
        report(
            observer, "assets", f"Descrevendo asset {step}/{total_images}.",
            current_item=asset["asset_id"], current_index=step, total_items=total_images,
        )
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
            from zettel.usage import record_cache_hit
            record_cache_hit(label=f"image:{asset['asset_id']}", model=model)
            db.update_asset_description(asset["asset_id"], cached, call_checksum)
            described += 1
            consecutive_exhausted = 0
            continue

        if min_interval > 0 and last_llm_call_at > 0:
            elapsed = time.monotonic() - last_llm_call_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

        try:
            description = _describe_with_rate_limit_retry(
                llm,
                prompt_parts,
                img_file,
                context,
                asset_id=asset["asset_id"],
                max_retries=max_retries,
                backoff_max=backoff_max,
                step=step,
                total=total_images,
                provider=cfg.llm.provider,
                prompt_cache=cfg.llm.prompt_cache,
            )
            last_llm_call_at = time.monotonic()
            db.cache_llm_response(call_checksum, "(image)", description)
            db.update_asset_description(asset["asset_id"], description, call_checksum)
            described += 1
            consecutive_exhausted = 0
        except RateLimitExhausted as e:
            # Leave status=pending so a later extract / retry-failed is not required.
            consecutive_exhausted += 1
            logger.warning(
                "Rate limit ao descrever %s apos %d tentativas — mantendo pending (%s)",
                asset["asset_id"], max_retries + 1, e,
            )
            if consecutive_exhausted >= abort_after:
                remaining = len(pending) - idx
                logger.error(
                    "Abortando descricao de imagens: %d rate limits consecutivos "
                    "(TPM saturado). %d imagem(ns) permanecem pending — "
                    "rode extract de novo apos a janela TPM.",
                    consecutive_exhausted, remaining,
                )
                break
            # Cooldown before the next asset so the TPM window can recover.
            time.sleep(min(backoff_max, 5.0))
            last_llm_call_at = time.monotonic()
        except Exception as e:
            logger.error("Falha ao descrever imagem %s: %s", asset["asset_id"], e)
            db.update_asset_description(asset["asset_id"], "", call_checksum, status="failed")
            consecutive_exhausted = 0

    clear_progress()
    logger.info("Imagens descritas: %d", described)
    return described


def _describe_with_rate_limit_retry(
    llm: Any,
    prompt_parts: Any,
    img_file: Path,
    context: str,
    *,
    asset_id: str,
    max_retries: int,
    backoff_max: float,
    step: int | None = None,
    total: int | None = None,
    provider: str | None = None,
    prompt_cache: bool = True,
) -> str:
    """Call multimodal describe; retry on 429 using provider wait hints."""
    attempts = max_retries + 1
    for attempt in range(attempts):
        try:
            return _describe_one(
                llm, prompt_parts, img_file, context,
                step=step, total=total,
                provider=provider, prompt_cache=prompt_cache,
            )
        except Exception as e:
            if not _is_rate_limit_error(e) or attempt >= max_retries:
                if _is_rate_limit_error(e):
                    raise RateLimitExhausted(str(e)) from e
                raise
            wait = _rate_limit_wait_seconds(e, attempt, backoff_max)
            logger.warning(
                "Rate limit na imagem %s (tentativa %d/%d) — aguardando %.2fs",
                asset_id, attempt + 1, attempts, wait,
            )
            time.sleep(wait)
    raise RateLimitExhausted(f"esgotadas {attempts} tentativas para {asset_id}")


def _get_multimodal_llm(cfg: AppConfig, model: str) -> Any:
    """Instantiate a chat model for image description (provider from cfg.llm)."""
    from zettel.llm import is_openai_compatible, normalize_llm_provider

    provider = normalize_llm_provider(cfg.llm.provider)
    base_url = getattr(cfg.llm, "base_url", None)
    # Retries de 429 sao tratados em _describe_with_rate_limit_retry (com pacing
    # e wait hint da API). max_retries=0 evita double-retry curto do SDK.
    if is_openai_compatible(provider):
        from langchain_openai import ChatOpenAI
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": cfg.llm.temperature,
            "max_retries": 0,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=cfg.llm.temperature, max_retries=0)
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        kwargs = {"model": model, "temperature": cfg.llm.temperature}
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOllama(**kwargs)
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model, temperature=cfg.llm.temperature, max_retries=0,
        )
    raise ValueError(f"Provider nao suportado para descricao de imagem: {cfg.llm.provider}")


def _describe_one(
    llm: Any,
    prompt_parts: Any,
    img_file: Path,
    context: str,
    *,
    step: int | None = None,
    total: int | None = None,
    provider: str | None = None,
    prompt_cache: bool = True,
) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    from zettel.llm import (
        _extract_usage,
        _resolve_model_name,
        apply_prompt_cache_hints,
        fill_template,
    )
    from zettel.pricing import estimate_llm_cost
    from zettel.usage import record_llm

    b64 = base64.b64encode(img_file.read_bytes()).decode("ascii")
    mapping = {"context": context or "(sem contexto textual)"}
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user_text = fill_template(prompt_parts.user_template, mapping)

    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(
        HumanMessage(content=[
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ])
    )
    messages, invoke_kwargs = apply_prompt_cache_hints(
        provider, messages, enabled=prompt_cache,
    )
    response = llm.invoke(messages, **invoke_kwargs)
    content = response.content
    if not isinstance(content, str):
        content = str(content)

    model_name = _resolve_model_name(llm, None)
    usage = _extract_usage(response)
    cost = estimate_llm_cost(
        model_name, usage.prompt_tokens, usage.completion_tokens, provider=provider,
    )
    record_llm(
        model=model_name or "unknown",
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
        cost_usd=cost,
        label=f"image:{img_file.name}",
        step=step,
        total=total,
        kind="imagem" if step is not None else None,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    return content.strip()
