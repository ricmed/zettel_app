"""Pesquisa de livros por ISBN ou título: Google Books, ou Open Library a pedido.

Só leitura e sem chave de API. O resultado é uma lista de candidatos que a
página de cadastro mostra para o usuário escolher; nada é gravado aqui. A capa
de cada candidato é uma referência (``ol:<id>`` ou ``gb:<id>``) que só este
módulo transforma em URL, então nenhum endereço vindo de formulário chega à
rede.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

OPEN_LIBRARY_ISBN_URL = (
    "https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=data&format=json"
)
OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json?{query}"
OPEN_LIBRARY_COVER_URL = "https://covers.openlibrary.org/b/id/{id}-{size}.jpg?default=false"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes?{query}"
# Opcional. Sem ela, o Google Books divide uma cota diária com todos os clientes
# anônimos e costuma responder 429; com ela, a cota é da chave (1000 consultas/dia).
GOOGLE_BOOKS_KEY_ENV = "GOOGLE_BOOKS_API_KEY"
GOOGLE_COVER_URL = (
    "https://books.google.com/books/content?id={id}&printsec=frontcover&img=1&zoom={zoom}"
)
# O Wikidata pede um User-Agent que identifique o programa e onde achá-lo.
USER_AGENT = "zettel-estante/1.0 (https://github.com/ricmed/zettel_app)"
MAX_RESULTS = 10
MAX_JSON_BYTES = 2 * 1024 * 1024
_SEARCH_FIELDS = ",".join(
    [
        "key",
        "title",
        "subtitle",
        "author_name",
        "first_publish_year",
        "number_of_pages_median",
        "cover_i",
        "isbn",
        "publisher",
        "editions",
        "editions.title",
        "editions.subtitle",
        "editions.isbn",
        "editions.publisher",
        "editions.publish_date",
        "editions.number_of_pages",
        "editions.cover_i",
    ]
)
COVER_REF_RE = re.compile(r"^(?:ol:\d{1,12}|gb:[A-Za-z0-9_-]{4,40})$")
PROVIDER_LABELS = {"google_books": "Google Books", "open_library": "Open Library"}
# Com artigo, para as frases da página: "o Google Books", "a Open Library".
PROVIDER_ARTICLES = {"google_books": "o", "open_library": "a"}

# (url) -> (content-type, bytes); trocado nos testes, que não têm rede.
Fetcher = Callable[[str], tuple[str, bytes]]


class BookLookupError(ValueError):
    """Nenhum serviço respondeu; a mensagem diz qual e por quê."""


@dataclass
class BookCandidate:
    provider: str
    title: str
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    isbn: str = ""
    publisher: str = ""
    place: str = ""
    year: int | None = None  # ano desta edição (vai ao SRC)
    first_year: int | None = None  # ano da obra (vai à LIV, quando o serviço sabe)
    pages: int | None = None
    cover_ref: str = ""
    # Categorias do catálogo (Google Books: BISAC, "Fiction / Fantasy"): sugerem gêneros.
    categories: list[str] = field(default_factory=list)

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)

    def form_fields(self) -> dict[str, str]:
        """Os campos do formulário de cadastro que este candidato preenche."""
        title = self.title
        if self.subtitle:
            title = f"{title}: {self.subtitle}"
        values = {
            "title": title,
            "authors": "\n".join(self.authors),
            "isbn": self.isbn,
            "pages": str(self.pages or ""),
            "original_publication_year": str(self.first_year or ""),
            "publisher": self.publisher,
            "place": self.place,
            "year": str(self.year or ""),
            "cover_ref": self.cover_ref,
        }
        return {k: v for k, v in values.items() if v}


def _urlopen(url: str) -> tuple[str, bytes]:
    # URLs montadas aqui a partir das constantes acima (https fixo), nunca do usuário.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        return response.headers.get_content_type(), response.read(MAX_JSON_BYTES + 1)


def _get_json(url: str, fetch: Fetcher | None, service: str) -> Any:
    try:
        _, data = (fetch or _urlopen)(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 429:
            hint = ""
            if service == "o Google Books" and not os.environ.get(GOOGLE_BOOKS_KEY_ENV):
                hint = f" Sem chave a cota é compartilhada: defina {GOOGLE_BOOKS_KEY_ENV} no .env."
            raise BookLookupError(
                f"{service} recusou a consulta: cota esgotada (429).{hint}"
            ) from exc
        if exc.code in (401, 403) and service == "o Google Books":
            raise BookLookupError(
                f"o Google Books recusou a chave {GOOGLE_BOOKS_KEY_ENV} ({exc.code}). Use uma "
                "chave de API comum do Google Cloud (começa com AIza), de um projeto com a "
                "Books API ativada e sem restrição de site."
            ) from exc
        raise BookLookupError(f"{service} respondeu {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise BookLookupError(f"Não foi possível falar com {service}: {exc}") from exc
    if len(data) > MAX_JSON_BYTES:
        raise BookLookupError(f"{service} devolveu uma resposta grande demais.")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BookLookupError(f"{service} devolveu uma resposta ilegível.") from exc


def _year(raw: Any) -> int | None:
    match = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", str(raw or ""))
    return int(match.group(1)) if match else None


def _pages(raw: Any) -> int | None:
    return raw if isinstance(raw, int) and raw > 0 else None


def _text(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:300]


def _first(values: Any) -> str:
    return _text(values[0]) if isinstance(values, list) and values else ""


def _best_isbn(values: Any) -> str:
    """O ISBN-13 válido, se houver; senão o ISBN-10 válido."""
    from zettel.harvester.biblio_dedupe import normalize_isbn

    clean = [normalize_isbn(v) for v in values or [] if isinstance(v, str)]
    clean = [v for v in clean if v]
    return next((v for v in clean if len(v) == 13), clean[0] if clean else "")


def _names(values: Any, key: str = "name") -> list[str]:
    out = []
    for value in values or []:
        name = value.get(key) if isinstance(value, dict) else value
        if _text(name):
            out.append(_text(name))
    return list(dict.fromkeys(out))[:10]


# ── Open Library ────────────────────────────────────────────────────────


def _open_library_isbn(isbn: str, fetch: Fetcher | None) -> list[BookCandidate]:
    data = _get_json(OPEN_LIBRARY_ISBN_URL.format(isbn=isbn), fetch, "a Open Library")
    record = (data or {}).get(f"ISBN:{isbn}") if isinstance(data, dict) else None
    if not isinstance(record, dict) or not _text(record.get("title")):
        return []
    ids = record.get("identifiers") or {}
    cover = re.search(r"/b/id/(\d+)-", str((record.get("cover") or {}).get("large") or ""))
    return [
        BookCandidate(
            provider="open_library",
            title=_text(record.get("title")),
            subtitle=_text(record.get("subtitle")),
            authors=_names(record.get("authors")),
            isbn=_best_isbn([*(ids.get("isbn_13") or []), *(ids.get("isbn_10") or []), isbn]),
            publisher=_first(_names(record.get("publishers"))),
            place=_first(_names(record.get("publish_places"))),
            year=_year(record.get("publish_date")),
            pages=_pages(record.get("number_of_pages")),
            cover_ref=f"ol:{cover.group(1)}" if cover else "",
        )
    ]


def _open_library_search(query: str, fetch: Fetcher | None) -> list[BookCandidate]:
    params = urllib.parse.urlencode(
        {"q": query, "fields": _SEARCH_FIELDS, "limit": MAX_RESULTS, "lang": "pt"}
    )
    data = _get_json(OPEN_LIBRARY_SEARCH_URL.format(query=params), fetch, "a Open Library")
    docs = data.get("docs") if isinstance(data, dict) else None
    out = []
    for doc in docs or []:
        if not isinstance(doc, dict) or not _text(doc.get("title")):
            continue
        # A edição que a Open Library escolheu para a busca (preferindo PT), senão a obra.
        editions = (doc.get("editions") or {}).get("docs") or []
        edition = editions[0] if editions and isinstance(editions[0], dict) else {}
        cover = edition.get("cover_i") or doc.get("cover_i")
        out.append(
            BookCandidate(
                provider="open_library",
                title=_text(edition.get("title") or doc.get("title")),
                subtitle=_text(edition.get("subtitle") or doc.get("subtitle")),
                authors=_names(doc.get("author_name")),
                isbn=_best_isbn(edition.get("isbn") or doc.get("isbn")),
                publisher=_first(edition.get("publisher") or doc.get("publisher")),
                year=_year(_first(edition.get("publish_date"))),
                first_year=doc.get("first_publish_year")
                if isinstance(doc.get("first_publish_year"), int)
                else None,
                pages=_pages(edition.get("number_of_pages"))
                or _pages(doc.get("number_of_pages_median")),
                cover_ref=f"ol:{cover}" if isinstance(cover, int) and cover > 0 else "",
            )
        )
    return out


# ── Google Books ────────────────────────────────────────────────────────


def _google_books(query: str, fetch: Fetcher | None) -> list[BookCandidate]:
    query_params = {"q": query, "maxResults": MAX_RESULTS, "printType": "books"}
    key = os.environ.get(GOOGLE_BOOKS_KEY_ENV, "").strip()
    if key:
        query_params["key"] = key
    params = urllib.parse.urlencode(query_params)
    data = _get_json(GOOGLE_BOOKS_URL.format(query=params), fetch, "o Google Books")
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    for item in items or []:
        info = item.get("volumeInfo") if isinstance(item, dict) else None
        if not isinstance(info, dict) or not _text(info.get("title")):
            continue
        ids = [
            i.get("identifier")
            for i in info.get("industryIdentifiers") or []
            if isinstance(i, dict) and str(i.get("type", "")).startswith("ISBN")
        ]
        volume = str(item.get("id") or "")
        has_cover = bool(info.get("imageLinks")) and COVER_REF_RE.match(f"gb:{volume}")
        out.append(
            BookCandidate(
                provider="google_books",
                title=_text(info.get("title")),
                subtitle=_text(info.get("subtitle")),
                authors=_names(info.get("authors")),
                isbn=_best_isbn(ids),
                publisher=_text(info.get("publisher")),
                year=_year(info.get("publishedDate")),
                pages=_pages(info.get("pageCount")),
                cover_ref=f"gb:{volume}" if has_cover else "",
                categories=_names(info.get("categories")),
            )
        )
    return out


# ── Entrada pública ─────────────────────────────────────────────────────


GOOGLE_BOOKS = "google_books"
OPEN_LIBRARY = "open_library"
PROVIDERS = (GOOGLE_BOOKS, OPEN_LIBRARY)  # a ordem da página: o primeiro é o padrão


def _isbn_query(query: str) -> str:
    from zettel.harvester.biblio_dedupe import normalize_isbn

    return normalize_isbn(query) if re.fullmatch(r"[\dXx\s.-]{10,20}", query) else ""


def search_books(
    query: str, provider: str = GOOGLE_BOOKS, fetch: Fetcher | None = None
) -> list[BookCandidate]:
    """Candidatos para um ISBN ou um título (com ou sem autor) em **um** serviço.

    Não há reserva automática: quem pesquisa vê de qual serviço veio a lista e
    escolhe refazer a busca no outro. ``BookLookupError`` quando o serviço não responde.
    """
    if provider not in PROVIDERS:
        raise BookLookupError("Serviço de pesquisa desconhecido.")
    query = " ".join(str(query or "").split())[:200]
    if not query:
        return []
    isbn = _isbn_query(query)
    if provider == GOOGLE_BOOKS:
        return _google_books(f"isbn:{isbn}" if isbn else query, fetch)
    return _open_library_isbn(isbn, fetch) if isbn else _open_library_search(query, fetch)


def lookup_isbn(isbn: str, fetch: Fetcher | None = None) -> BookCandidate | None:
    """O primeiro candidato para um ISBN válido: Google Books, depois Open Library.

    É a parte de catálogo do "Completar dados", sem lista para escolher, então os dois serviços
    são tentados em ordem; ``BookLookupError`` só quando nenhum responde.
    """
    from zettel.harvester.biblio_dedupe import normalize_isbn

    clean = normalize_isbn(isbn)
    if not clean:
        raise BookLookupError("ISBN ausente ou inválido.")
    errors = []
    for provider in PROVIDERS:
        try:
            found = search_books(clean, provider, fetch)
        except BookLookupError as exc:
            errors.append(str(exc))
            continue
        if found:
            return found[0]
    if len(errors) == len(PROVIDERS):
        raise BookLookupError(" ".join(errors))
    return None


def cover_url(ref: str, *, large: bool) -> str:
    """URL da capa de uma referência ``ol:``/``gb:``; ``ValueError`` se ela não é válida."""
    if not COVER_REF_RE.match(ref or ""):
        raise ValueError("Referência de capa inválida.")
    provider, ident = ref.split(":", 1)
    if provider == "ol":
        return OPEN_LIBRARY_COVER_URL.format(id=ident, size="L" if large else "M")
    return GOOGLE_COVER_URL.format(id=ident, zoom=1)
