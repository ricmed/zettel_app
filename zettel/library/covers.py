"""Capa do livro: upload ou Open Library (``covers.openlibrary.org``).

A imagem vai para ``90_Assets/`` com o mesmo endereçamento por conteúdo das
figuras, mas **não** entra na tabela ``assets``: lá a visão a descreveria no
``extract`` como figura da fonte.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

OPEN_LIBRARY_URL = "https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"
OPEN_LIBRARY_HOME = "https://openlibrary.org"
MAX_COVER_BYTES = 8 * 1024 * 1024
RASTER_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"})
_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# (url) -> (content-type, bytes); trocado nos testes, que não têm rede.
Fetcher = Callable[[str], tuple[str, bytes]]


class CoverError(ValueError):
    pass


def _urlopen(url: str) -> tuple[str, bytes]:
    # URL montada aqui a partir de OPEN_LIBRARY_URL (https fixo), nunca do usuário.
    request = urllib.request.Request(url, headers={"User-Agent": "zettel-estante"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        content_type = response.headers.get_content_type()
        return content_type, response.read(MAX_COVER_BYTES + 1)


def fetch_open_library_cover(isbn: str, fetch: Fetcher | None = None) -> tuple[bytes, str]:
    """``(bytes, sufixo)`` da capa grande do ISBN; ``CoverError`` se não houver."""
    from zettel.harvester.biblio_dedupe import normalize_isbn

    clean = normalize_isbn(isbn)
    if not clean:
        raise CoverError("ISBN ausente ou inválido.")
    try:
        content_type, data = (fetch or _urlopen)(OPEN_LIBRARY_URL.format(isbn=clean))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CoverError("A Open Library não tem capa para este ISBN.") from exc
        raise CoverError(f"A Open Library respondeu {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CoverError(f"Não foi possível falar com a Open Library: {exc}") from exc
    suffix = _CONTENT_TYPES.get(content_type)
    if suffix is None:
        raise CoverError("A Open Library não devolveu uma imagem.")
    if not data:
        raise CoverError("A Open Library devolveu uma imagem vazia.")
    if len(data) > MAX_COVER_BYTES:
        raise CoverError("A capa passa de 8 MB.")
    return data, suffix


def store_cover(vault_path: Path, data: bytes, suffix: str) -> str:
    """Grava a capa em ``90_Assets/`` e devolve o caminho relativo ao vault."""
    from zettel.assets import _save_image

    suffix = suffix.lower()
    if suffix not in RASTER_SUFFIXES:
        raise CoverError("Use uma imagem PNG, JPG, GIF, WEBP ou AVIF.")
    return _save_image(vault_path, data, suffix)
