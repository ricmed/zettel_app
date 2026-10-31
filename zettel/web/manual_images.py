"""Image files posted with the manual-note form. No route, no vault write."""

from __future__ import annotations

from pathlib import Path
from typing import Any

IMAGE_MODES = frozenset({"LIT_GRANULAR", "ZTL_BLANK", "ZTL_FROM_LIT"})


def _reject_name(raw: str) -> str:
    name = Path(raw).name
    if (
        not name
        or name in {".", ".."}
        or name != raw
        or "/" in raw
        or "\\" in raw
        or len(name) > 180
    ):
        raise ValueError("Nome de imagem inválido.")
    return name


def read_image_uploads(form: Any) -> list[tuple[str, bytes]]:
    """Non-empty image parts from the ``images`` field.

    An empty file input is ignored. A bad suffix, an empty file or too many
    files raises ``ValueError`` before anything is written to the vault.
    """
    from zettel.assets import IMAGE_SUFFIXES, MAX_MANUAL_IMAGE_BYTES, MAX_MANUAL_IMAGES

    items = form.getlist("images") if hasattr(form, "getlist") else []
    uploads: list[tuple[str, bytes]] = []
    limit_mb = MAX_MANUAL_IMAGE_BYTES // (1024 * 1024)
    for item in items:
        if isinstance(item, str):
            if item.strip():
                raise ValueError("Envie a imagem como arquivo.")
            continue
        raw = str(getattr(item, "filename", "") or "").strip()
        if not raw:
            continue
        name = _reject_name(raw)
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise ValueError("Use uma imagem PNG, JPG, GIF, WEBP, BMP, SVG, TIF ou AVIF.")
        fileobj = getattr(item, "file", None)
        if fileobj is None:
            raise ValueError("Envie a imagem como arquivo.")
        data = fileobj.read(MAX_MANUAL_IMAGE_BYTES + 1)
        if not data:
            raise ValueError("A imagem está vazia.")
        if len(data) > MAX_MANUAL_IMAGE_BYTES:
            raise ValueError(f"Cada imagem pode ter no máximo {limit_mb} MB.")
        if len(uploads) >= MAX_MANUAL_IMAGES:
            raise ValueError(f"Anexe no máximo {MAX_MANUAL_IMAGES} imagens.")
        uploads.append((name, data))
    return uploads
