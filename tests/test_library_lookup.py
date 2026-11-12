"""Book search by ISBN or title (Google Books, or Open Library on request), with fake responses."""

from __future__ import annotations

import json
import urllib.error

import pytest
from zettel.library import covers, lookup
from zettel.library.book_file import Book
from zettel.library.editing import fill_missing

ISBN = "9788533302273"

OL_ISBN = {
    f"ISBN:{ISBN}": {
        "title": "O Hobbit",
        "authors": [{"name": "J. R. R. Tolkien"}],
        "publishers": [{"name": "Martins Fontes"}],
        "publish_places": [{"name": "São Paulo"}],
        "publish_date": "2001",
        "number_of_pages": 297,
        "identifiers": {"isbn_10": ["8533302272"], "isbn_13": [ISBN]},
        "cover": {"large": "https://covers.openlibrary.org/b/id/8406786-L.jpg"},
    }
}
OL_SEARCH = {
    "docs": [
        {
            "title": "The Hobbit",
            "author_name": ["J.R.R. Tolkien"],
            "first_publish_year": 1937,
            "cover_i": 1,
            "editions": {
                "docs": [
                    {
                        "title": "O Hobbit",
                        "isbn": ["8533302272", ISBN],
                        "publisher": ["Martins Fontes"],
                        "publish_date": ["2001"],
                        "number_of_pages": 297,
                        "cover_i": 8406786,
                    }
                ]
            },
        },
        {"title": "", "author_name": ["sem título é ignorado"]},
    ]
}
GOOGLE = {
    "items": [
        {
            "id": "abcDEF12",
            "volumeInfo": {
                "title": "O Hobbit",
                "subtitle": "lá e de volta outra vez",
                "authors": ["J. R. R. Tolkien"],
                "publisher": "HarperCollins",
                "publishedDate": "2019-11-25",
                "pageCount": 336,
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9788595084742"}],
                "imageLinks": {"thumbnail": "http://books.google.com/x"},
            },
        }
    ]
}


def _fetcher(routes: dict[str, object], seen: list[str]):
    def fetch(url: str):
        seen.append(url)
        for prefix, answer in routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return "application/json", json.dumps(answer).encode()
        raise AssertionError(url)

    return fetch


OL = lookup.OPEN_LIBRARY
GB = lookup.GOOGLE_BOOKS


def test_google_books_is_the_default_service_and_no_other_is_asked():
    seen: list[str] = []
    routes = {"https://www.googleapis.com/": GOOGLE}
    (book,) = lookup.search_books("o hobbit", fetch=_fetcher(routes, seen))
    assert len(seen) == 1 and seen[0].startswith(
        "https://www.googleapis.com/books/v1/volumes?q=o+hobbit"
    )
    assert book.provider == GB and book.provider_label == "Google Books"
    assert book.form_fields()["title"] == "O Hobbit: lá e de volta outra vez"
    assert (book.isbn, book.year, book.pages, book.cover_ref) == (
        "9788595084742",
        2019,
        336,
        "gb:abcDEF12",
    )
    lookup.search_books("978-85-333-0227-3", fetch=_fetcher(routes, seen))
    assert "q=isbn%3A9788533302273" in seen[-1]

    # Nada achado é uma lista vazia: a página oferece a Open Library, não a consulta sozinha.
    empty = _fetcher({"https://www.googleapis.com/": {}}, seen)
    assert lookup.search_books("o hobbit", fetch=empty) == []
    assert all(url.startswith("https://www.googleapis.com/") for url in seen)


def test_isbn_query_reads_the_open_library_edition():
    seen: list[str] = []
    found = lookup.search_books(
        "978-85-333-0227-3", OL, _fetcher({"https://openlibrary.org/api/books": OL_ISBN}, seen)
    )
    assert seen == [lookup.OPEN_LIBRARY_ISBN_URL.format(isbn=ISBN)]
    (book,) = found
    assert (book.title, book.authors, book.isbn) == ("O Hobbit", ["J. R. R. Tolkien"], ISBN)
    assert (book.publisher, book.place, book.year, book.pages) == (
        "Martins Fontes",
        "São Paulo",
        2001,
        297,
    )
    assert book.cover_ref == "ol:8406786"
    assert book.first_year is None  # o ano da obra não vem pela edição


def test_title_query_prefers_the_edition_and_keeps_the_work_year():
    seen: list[str] = []
    found = lookup.search_books(
        "o hobbit tolkien", OL, _fetcher({"https://openlibrary.org/search.json": OL_SEARCH}, seen)
    )
    assert len(found) == 1
    book = found[0]
    assert (book.title, book.isbn, book.year, book.first_year) == ("O Hobbit", ISBN, 2001, 1937)
    assert book.cover_ref == "ol:8406786"
    fields = book.form_fields()
    assert fields["original_publication_year"] == "1937" and fields["year"] == "2001"
    assert "q=o+hobbit+tolkien" in seen[0]


def test_a_service_that_does_not_answer_is_an_error_and_an_unknown_one_is_refused():
    down = _fetcher({"https://": urllib.error.URLError("sem rede")}, [])
    with pytest.raises(lookup.BookLookupError, match="Google Books"):
        lookup.search_books("o hobbit", fetch=down)
    with pytest.raises(lookup.BookLookupError, match="Open Library"):
        lookup.search_books("o hobbit", OL, down)
    with pytest.raises(lookup.BookLookupError, match="desconhecido"):
        lookup.search_books("o hobbit", "amazon", down)
    assert lookup.search_books("   ", fetch=down) == []


def test_lookup_isbn_tries_google_then_open_library():
    seen: list[str] = []
    routes = {"https://www.googleapis.com/": {}, "https://openlibrary.org/api/books": OL_ISBN}
    book = lookup.lookup_isbn(ISBN, _fetcher(routes, seen))
    assert book is not None and book.provider == OL
    assert [u.split("/")[2] for u in seen] == ["www.googleapis.com", "openlibrary.org"]
    down = _fetcher({"https://": urllib.error.URLError("sem rede")}, [])
    with pytest.raises(lookup.BookLookupError, match=r"Google Books.*Open Library"):
        lookup.lookup_isbn(ISBN, down)
    assert lookup.lookup_isbn(ISBN, _fetcher({"https://": {}}, [])) is None


def test_an_invalid_isbn_is_searched_as_text_and_refused_by_lookup_isbn():
    seen: list[str] = []
    lookup.search_books(
        "9788533302274", OL, _fetcher({"https://openlibrary.org/search.json": OL_SEARCH}, seen)
    )
    assert seen[0].startswith("https://openlibrary.org/search.json")
    with pytest.raises(lookup.BookLookupError, match="ISBN"):
        lookup.lookup_isbn("123")


@pytest.mark.parametrize("ref", ["", "http://evil", "ol:abc", "gb:../x", "xx:1"])
def test_cover_refs_never_become_an_arbitrary_url(ref):
    with pytest.raises(ValueError):
        lookup.cover_url(ref, large=True)
    with pytest.raises(covers.CoverError):
        covers.fetch_cover_ref(ref, fetch=lambda url: pytest.fail(url))


def test_cover_ref_urls_are_built_here():
    assert lookup.cover_url("ol:42", large=True) == lookup.OPEN_LIBRARY_COVER_URL.format(
        id=42, size="L"
    )
    assert lookup.cover_url("ol:42", large=False).endswith("42-M.jpg?default=false")
    assert "id=abcDEF12" in lookup.cover_url("gb:abcDEF12", large=False)
    data, suffix = covers.fetch_cover_ref("ol:42", fetch=lambda url: ("image/jpeg", b"\xff\xd8"))
    assert (data, suffix) == (b"\xff\xd8", ".jpg")


def test_fill_missing_never_replaces_what_the_user_wrote(tmp_path):
    meta = {"title": "O Hobbit", "authors": [], "pages": 300}
    book = Book(book_id="b", path=tmp_path / "x.md", meta=meta, body="", version="v")
    changed = fill_missing(
        book,
        {
            "authors": ["J. R. R. Tolkien"],
            "pages": 297,
            "original_publication_year": "1937",
            "isbn": ISBN,
        },
    )
    assert sorted(changed) == ["authors", "isbn", "original_publication_year"]
    assert book.meta["pages"] == 300
    assert book.meta["original_publication_year"] == 1937
    assert fill_missing(book, {"authors": ["Outro"], "isbn": "x"}) == []


def test_google_books_key_comes_from_the_environment(monkeypatch):
    seen: list[str] = []
    fetch = _fetcher({"https://www.googleapis.com/": GOOGLE}, seen)
    monkeypatch.delenv(lookup.GOOGLE_BOOKS_KEY_ENV, raising=False)
    lookup.search_books("o hobbit", fetch=fetch)
    assert "key=" not in seen[-1]
    monkeypatch.setenv(lookup.GOOGLE_BOOKS_KEY_ENV, "chave-de-teste")
    lookup.search_books("o hobbit", fetch=fetch)
    assert seen[-1].endswith("&key=chave-de-teste")


def test_an_exhausted_quota_says_so_and_points_at_the_key(monkeypatch):
    def quota(url):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)

    monkeypatch.delenv(lookup.GOOGLE_BOOKS_KEY_ENV, raising=False)
    with pytest.raises(lookup.BookLookupError, match=r"cota esgotada.*GOOGLE_BOOKS_API_KEY"):
        lookup.search_books("o hobbit", fetch=quota)
    monkeypatch.setenv(lookup.GOOGLE_BOOKS_KEY_ENV, "chave-de-teste")
    with pytest.raises(lookup.BookLookupError) as caught:
        lookup.search_books("o hobbit", fetch=quota)
    assert "GOOGLE_BOOKS_API_KEY" not in str(caught.value)


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_key_says_what_kind_of_key_works(code):
    def refused(url):
        raise urllib.error.HTTPError(url, code, "Unauthorized", None, None)

    with pytest.raises(lookup.BookLookupError, match=r"AIza.*Books API"):
        lookup.search_books("o hobbit", fetch=refused)
