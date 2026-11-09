"""The bookshelf on the web (#393-#395): list, create, edit, conflicts, cover, notes, dashboard."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from zettel.library import book_file as bf
from zettel.library import covers
from zettel.vault import parse_frontmatter, partition_vault_notes

from tests.web_support import login, wait_for_job

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _cfg(client):
    return client.app.state.service.cfg


def _create(client, csrf, **fields) -> str:
    data = {"csrf": csrf, "title": "O Hobbit", "format": "fisico", "status": "quero_ler", **fields}
    response = client.post("/books", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[-1]


def _version(client, book_id: str) -> str:
    return bf.load_book(_cfg(client), book_id).version


def _edit(client, csrf, book_id, action, **fields):
    data = {"csrf": csrf, "action": action, "version": _version(client, book_id), **fields}
    return client.post(f"/books/{book_id}", data=data, follow_redirects=False)


def test_create_writes_a_manual_liv_kept_by_reset(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    assert client.post("/books", data={"title": "x"}).status_code == 403
    book_id = _create(
        client,
        csrf,
        authors="J. R. R. Tolkien",
        genres=["Fantasia", "Clássico"],
        country="Inglaterra",
        original_language="en",
    )
    book = bf.load_book(_cfg(client), book_id)
    assert book.code == "0001 - F"
    assert book.authors == ["J. R. R. Tolkien"]
    assert book.genres == ["Fantasia", "Clássico"]
    _pipeline, kept = partition_vault_notes(tmp_path / "vault")
    assert book.path in kept

    second = _create(client, csrf, title="Duna", format="digital")
    assert bf.load_book(_cfg(client), second).code == "0002 - D"

    listing = client.get("/books").text
    assert "O Hobbit" in listing and "0001 - F" in listing
    assert "Duna" in client.get("/books?format=digital").text
    assert "O Hobbit" not in client.get("/books?format=digital").text


@pytest.mark.parametrize("layout", ["table", "gallery", "board"])
def test_every_layout_renders(web_client, layout):
    client, _ = web_client
    csrf = login(client)
    _create(client, csrf)
    page = client.get(f"/books?layout={layout}")
    assert page.status_code == 200
    assert "O Hobbit" in page.text


def test_empty_shelf_explains_how_to_start(web_client):
    client, _ = web_client
    login(client)
    assert "Nenhum livro ainda" in client.get("/books").text
    assert client.get("/books/dashboard").status_code == 200


def test_reading_and_rating_flow(web_client):
    client, _ = web_client
    csrf = login(client)
    book_id = _create(client, csrf, genres=["Técnico"])

    assert _edit(client, csrf, book_id, "reading-start", date="2024-01-02").status_code == 303
    assert bf.load_book(_cfg(client), book_id).status == "lendo"
    assert _edit(client, csrf, book_id, "reading-finish", date="2024-02-03").status_code == 303
    book = bf.load_book(_cfg(client), book_id)
    assert book.status == "lido"
    assert [(r.start, r.end) for r in book.readings] == [("2024-01-02", "2024-02-03")]

    response = _edit(client, csrf, book_id, "rating", rating_usefulness="5", rating_edition="3")
    assert response.status_code == 303
    book = bf.load_book(_cfg(client), book_id)
    assert book.meta["ratings"] == {"usefulness": 5, "edition": 3}
    assert book.meta["score"] == pytest.approx(4.25)
    assert _edit(client, csrf, book_id, "rating", rating_usefulness="9").status_code == 400

    detail = client.get(f"/books/{book_id}").text
    assert "Técnico/Didático nacional" in detail
    assert "2 de 5 critérios" in detail

    dashboard = client.get("/books/dashboard?year=2024").text
    assert "<svg" in dashboard and "Leituras terminadas em 2024" in dashboard


def test_stale_version_is_a_conflict_not_an_overwrite(web_client):
    client, _ = web_client
    csrf = login(client)
    book_id = _create(client, csrf)
    stale = _version(client, book_id)
    path = bf.load_book(_cfg(client), book_id).path
    path.write_text(path.read_text(encoding="utf-8") + "\nEditado no Obsidian.\n", "utf-8")
    response = client.post(
        f"/books/{book_id}",
        data={"csrf": csrf, "action": "status", "status": "lido", "version": stale},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "Editado no Obsidian." in path.read_text(encoding="utf-8")


def test_unknown_action_and_missing_book(web_client):
    client, _ = web_client
    csrf = login(client)
    book_id = _create(client, csrf)
    assert _edit(client, csrf, book_id, "explodir").status_code == 400
    assert client.get("/books/01JZZZZZZZZZZZZZZZZZZZZZZZ").status_code == 404


def test_summary_is_rendered_and_kept_verbatim(web_client):
    client, _ = web_client
    csrf = login(client)
    book_id = _create(client, csrf)
    text = "Bilbo sai de casa.\n\n- e volta"
    assert _edit(client, csrf, book_id, "summary", summary=text, impressions="").status_code == 303
    book = bf.load_book(_cfg(client), book_id)
    assert bf.section_text(book.body, bf.SUMMARY_HEADING) == text
    assert "<li>e volta</li>" in client.get(f"/books/{book_id}").text


def test_cover_upload_and_open_library(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    book_id = _create(client, csrf, isbn="978-85-333-0227-3")
    response = client.post(
        f"/books/{book_id}",
        data={"csrf": csrf, "action": "cover", "version": _version(client, book_id)},
        files={"images": ("capa.png", PNG, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    cover = bf.load_book(_cfg(client), book_id).meta["cover"]
    assert re.fullmatch(r"90_Assets/img-[0-9a-f]{16}\.png", cover)
    assert (tmp_path / "vault" / cover).is_file()
    db = client.app.state.service.db()
    try:
        assert db.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    finally:
        db.close()

    seen = []

    def fake_fetch(url):
        seen.append(url)
        return "image/jpeg", b"\xff\xd8\xff" + b"\x00" * 32

    monkeypatch.setattr(covers, "_urlopen", fake_fetch)
    assert _edit(client, csrf, book_id, "cover-isbn", isbn="978-85-333-0227-3").status_code == 303
    assert seen == [covers.OPEN_LIBRARY_URL.format(isbn="9788533302273")]
    assert bf.load_book(_cfg(client), book_id).meta["cover"].endswith(".jpg")

    monkeypatch.setattr(covers, "_urlopen", lambda url: ("text/html", b"<html>"))
    failed = _edit(client, csrf, book_id, "cover-isbn", isbn="978-85-333-0227-3")
    assert failed.status_code == 400 and "não devolveu uma imagem" in failed.text


def test_reading_note_creates_a_source_and_an_adopted_lit(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    book_id = _create(
        client, csrf, authors="Tolkien", genres=["Fantasia"], isbn="978-85-333-0227-3"
    )
    response = client.post(
        f"/books/{book_id}/reading-notes",
        data={
            "csrf": csrf,
            "topic": "A coragem de Bilbo",
            "page": "12",
            "excerpt": "Não era uma toca desagradável.",
            "reflection_kind": "personagem",
            "characters": "Bilbo",
            "candidate": "A coragem nasce de quem menos parece apto.",
            "reflection": "O herói improvável.",
            "analogy": "Assim como Bilbo, o estagiário salvou o projeto.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    book = bf.load_book(_cfg(client), book_id)
    assert book.source_id
    db = client.app.state.service.db()
    try:
        source = db.get_source(book.source_id)
        assert source["isbn"] == "9788533302273"
        (chunk,) = db.get_chunks_for_source(book.source_id)
        assert chunk["status"] == "persisted"
    finally:
        db.close()
    lit = next((tmp_path / "vault" / "20_Literature").rglob("LIT - *p012*.md"))
    meta, body = parse_frontmatter(lit.read_text(encoding="utf-8"))
    assert meta["reading_mode"] == "reflexao" and meta["characters"] == ["Bilbo"]
    assert "Assim como Bilbo" in body
    detail = client.get(f"/books/{book_id}").text
    assert "A coragem de Bilbo" in detail or "p. 12" in detail
    assert "Gerar ZTL" in detail


def test_permanent_from_a_reading_note_is_the_existing_job(web_client):
    client, _ = web_client
    csrf = login(client)
    book_id = _create(client, csrf, authors="Tolkien")
    client.post(
        f"/books/{book_id}/reading-notes",
        data={"csrf": csrf, "topic": "Toca", "candidate": "Uma casa diz quem a habita."},
    )
    db = client.app.state.service.db()
    try:
        source_id = bf.load_book(_cfg(client), book_id).source_id
        (chunk,) = db.get_chunks_for_source(source_id)
    finally:
        db.close()
    response = client.post(
        f"/books/{book_id}/permanent",
        data={"csrf": csrf, "chunk_id": chunk["chunk_id"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    job = wait_for_job(client, job_id)["job"]
    assert job["state"] == "succeeded", job
    # Without the LLM the ZTL is a pre-filled scaffold in the vault, adopted by sync-manual.
    ztl = Path(job["result"]["path"])
    assert ztl.parent.name == "30_Permanent" and ztl.is_file()
    assert job["result"]["used_llm"] is False


def _fake_lookup(monkeypatch, answers):
    """Respostas falsas para a pesquisa e para as capas (a rede é bloqueada nos testes)."""
    from zettel.library import lookup

    seen = []
    # Sem resposta própria, o Wikidata não acha a obra.
    answers = {"https://query.wikidata.org/": _WIKIDATA_EMPTY, **answers}

    def fetch(url):
        seen.append(url)
        for prefix, answer in answers.items():
            if url.startswith(prefix):
                return answer
        raise AssertionError(url)

    monkeypatch.setattr(lookup, "_urlopen", fetch)
    monkeypatch.setattr(covers, "_urlopen", fetch)
    return seen


_OL_ISBN = (
    "application/json",
    (
        '{"ISBN:9788533302273": {"title": "O Hobbit", "authors": [{"name": "J. R. R. Tolkien"}],'
        ' "publishers": [{"name": "Martins Fontes"}], "publish_places": [{"name": "São Paulo"}],'
        ' "publish_date": "2001", "number_of_pages": 297,'
        ' "cover": {"large": "https://covers.openlibrary.org/b/id/77-L.jpg"}}}'
    ).encode(),
)
_WIKIDATA_EMPTY = ("application/json", b'{"results": {"bindings": []}}')
_WIKIDATA_HOBBIT = (
    "application/json",
    (
        b'{"results": {"bindings": [{"work": {"value": "http://www.wikidata.org/entity/Q74287"},'
        b' "authorLabel": {"value": "J. R. R. Tolkien"},'
        b' "date": {"value": "1937-09-21T00:00:00Z"}, "countryLabel": {"value": "Reino Unido"},'
        b' "langCode": {"value": "en"}, "origTitle": {"value": "The Hobbit", "xml:lang": "en"}}]}}'
    ),
)
_GOOGLE_EMPTY = ("application/json", b'{"totalItems": 0}')
_JPEG = ("image/jpeg", b"\xff\xd8\xff" + b"\x00" * 32)


def test_new_book_starts_with_a_search_and_the_pick_prefills_the_form(web_client, monkeypatch):
    client, _ = web_client
    csrf = login(client)
    page = client.get("/books/new").text
    assert 'name="q"' in page and 'action="/books"' not in page.split("Não achou?")[0]

    seen = _fake_lookup(
        monkeypatch,
        {
            "https://www.googleapis.com/": _GOOGLE_EMPTY,
            "https://openlibrary.org/api/books": _OL_ISBN,
            "https://covers.openlibrary.org/": _JPEG,
        },
    )
    # O Google Books vem primeiro; sem resultado, a página oferece refazer na Open Library.
    first = client.get("/books/new", params={"q": "978-85-333-0227-3"}).text
    assert "O Google Books não encontrou" in first
    assert "provider=open_library" in first and "Refazer a busca na Open Library" in first
    assert len(seen) == 1 and seen[0].startswith("https://www.googleapis.com/")
    results = client.get(
        "/books/new", params={"q": "978-85-333-0227-3", "provider": "open_library"}
    ).text
    assert "na Open Library para" in results and "Refazer a busca no Google Books" in results
    assert "O Hobbit" in results and "Martins Fontes" in results and "Usar este" in results
    assert "/books/lookup/cover?ref=ol%3A77" in results

    thumb = client.get("/books/lookup/cover", params={"ref": "ol:77"})
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert seen[-1].endswith("/b/id/77-M.jpg?default=false")
    assert client.get("/books/lookup/cover", params={"ref": "http://evil"}).status_code == 404

    form = client.get(
        "/books/new",
        params={
            "step": "form",
            "title": "O Hobbit",
            "authors_list": ["J. R. R. Tolkien"],
            "isbn": "9788533302273",
            "publisher": "Martins Fontes",
            "year": "2001",
            "cover_ref": "ol:77",
        },
    ).text
    assert 'value="Martins Fontes"' in form and "J. R. R. Tolkien</textarea>" in form

    book_id = _create(
        client,
        csrf,
        authors="J. R. R. Tolkien",
        isbn="9788533302273",
        publisher="Martins Fontes",
        place="São Paulo",
        year="2001",
        cover_ref="ol:77",
    )
    assert seen[-1].endswith("/b/id/77-L.jpg?default=false")
    book = bf.load_book(_cfg(client), book_id)
    assert book.meta["cover"].endswith(".jpg") and book.source_id
    db = client.app.state.service.db()
    try:
        source = db.get_source(book.source_id)
    finally:
        db.close()
    assert source["isbn"] == "9788533302273"
    assert '"publisher": "Martins Fontes"' in source["bibliography_json"]

    shelf = client.get("/books/new", params={"q": "9788533302273", "provider": "open_library"}).text
    assert "Já na estante (0001)" in shelf


def test_lookup_failure_is_explained_and_manual_form_stays_available(web_client, monkeypatch):
    import urllib.error

    client, _ = web_client
    login(client)

    def down(url):
        raise urllib.error.URLError("sem rede")

    from zettel.library import lookup

    monkeypatch.setattr(lookup, "_urlopen", down)
    page = client.get("/books/new", params={"q": "o hobbit"}).text
    assert (
        "Não foi possível falar com o Google Books" in page
        and "Refazer a busca na Open Library" in page
        and "Preencher à mão" in page
    )
    assert "step=form&amp;title=o%20hobbit" in page


def test_complete_by_isbn_fills_only_the_empty_fields(web_client, monkeypatch):
    client, _ = web_client
    csrf = login(client)
    book_id = _create(client, csrf, isbn="9788533302273", pages="300")
    _fake_lookup(
        monkeypatch,
        {
            "https://www.googleapis.com/": _GOOGLE_EMPTY,
            "https://openlibrary.org/api/books": _OL_ISBN,
            "https://covers.openlibrary.org/": _JPEG,
            "https://query.wikidata.org/": _WIKIDATA_HOBBIT,
        },
    )
    response = _edit(client, csrf, book_id, "complete")
    assert response.status_code == 303
    book = bf.load_book(_cfg(client), book_id)
    assert book.authors == ["J. R. R. Tolkien"]
    # A obra, pelo Wikidata (com os autores que o catálogo acabou de trazer).
    assert (book.meta["country"], book.meta["original_language"]) == ("Reino Unido", "en")
    assert book.meta["original_publication_year"] == 1937
    assert book.meta["original_title"] == "The Hobbit"
    assert book.meta["pages"] == 300  # o que o usuário escreveu fica
    assert book.meta["cover"].endswith(".jpg")
    db = client.app.state.service.db()
    try:
        assert '"place": "São Paulo"' in db.get_source(book.source_id)["bibliography_json"]
    finally:
        db.close()


def test_online_lookup_off_opens_the_form_directly(web_client):
    client, _ = web_client
    csrf = login(client)
    _cfg(client).library.online_lookup = False
    page = client.get("/books/new", params={"q": "o hobbit"}).text
    assert 'action="/books"' in page and "Pesquisar" not in page
    book_id = _create(client, csrf, isbn="9788533302273")
    assert _edit(client, csrf, book_id, "complete").status_code == 409


def test_automatic_nature_and_mode_show_what_the_genres_give(web_client):
    client, _ = web_client
    csrf = login(client)
    novel = _create(client, csrf, genres=["Romance"])
    page = client.get(f"/books/{novel}").text
    assert '<option value="">Automático: Ficção</option>' in page
    assert '<option value="">Automático: Reflexão</option>' in page
    manual = _create(client, csrf, title="Python Fluente", genres=["Técnico"])
    page = client.get(f"/books/{manual}").text
    assert '<option value="">Automático: Não ficção</option>' in page
    assert '<option value="">Automático: Conceito</option>' in page


def test_the_prefilled_form_brings_the_work_from_wikidata(web_client, monkeypatch):
    client, _ = web_client
    login(client)
    seen = _fake_lookup(monkeypatch, {"https://query.wikidata.org/": _WIKIDATA_HOBBIT})
    params = {
        "step": "form",
        "title": "O Hobbit",
        "authors_list": ["J.R.R. Tolkien"],
        # O "ano da obra" do catálogo de edições perde para o do Wikidata.
        "original_publication_year": "1990",
    }
    page = client.get("/books/new", params=params).text
    assert len(seen) == 1 and seen[0].startswith("https://query.wikidata.org/sparql?")
    assert 'value="Reino Unido"' in page and 'value="en"' in page and 'value="1937"' in page
    assert 'value="The Hobbit"' in page and "https://www.wikidata.org/wiki/Q74287" in page

    # Wikidata fora do ar: o formulário abre do mesmo jeito, com o aviso.
    import urllib.error

    from zettel.library import lookup

    def down(url):
        raise urllib.error.URLError("sem rede")

    monkeypatch.setattr(lookup, "_urlopen", down)
    page = client.get("/books/new", params=params).text
    assert "O Wikidata não respondeu" in page and 'value="1990"' in page


def test_complete_without_isbn_uses_only_wikidata(web_client, monkeypatch):
    client, _ = web_client
    csrf = login(client)
    book_id = _create(client, csrf, authors="J. R. R. Tolkien", country="Inglaterra")
    seen = _fake_lookup(monkeypatch, {"https://query.wikidata.org/": _WIKIDATA_HOBBIT})
    assert _edit(client, csrf, book_id, "complete").status_code == 303
    assert all(url.startswith("https://query.wikidata.org/") for url in seen)
    book = bf.load_book(_cfg(client), book_id)
    assert book.meta["country"] == "Inglaterra"  # o que o usuário escreveu fica
    assert book.meta["original_publication_year"] == 1937

    _fake_lookup(monkeypatch, {})
    missing = _edit(
        client, csrf, _create(client, csrf, title="Livro Raro", authors="Ninguém"), "complete"
    )
    assert missing.status_code == 400 and "Wikidata" in missing.text


def test_complete_reports_what_was_filled_and_asks_for_the_original_title(web_client, monkeypatch):
    client, _ = web_client
    csrf = login(client)
    book_id = _create(client, csrf, title="Enclausurado", authors="Ian McEwan")
    author_only = (
        "application/json",
        (
            b'{"results": {"bindings": [{"author": {"value": "http://www.wikidata.org/entity/Q190379"},'
            b' "authorLabel": {"value": "Ian McEwan"}, "countryLabel": {"value": "Reino Unido"}}]}}'
        ),
    )
    answers = iter([_WIKIDATA_EMPTY, _WIKIDATA_EMPTY, author_only])
    _fake_lookup(monkeypatch, {})
    from zettel.library import lookup

    monkeypatch.setattr(lookup, "_urlopen", lambda url: next(answers))
    response = _edit(client, csrf, book_id, "complete")
    location = response.headers["location"]
    assert "completed=country" in location and "work=author" in location
    page = client.get(location).text
    assert "preencheu: país do autor" in page
    assert "achou o autor, mas não a obra" in page and "título original" in page
    assert bf.load_book(_cfg(client), book_id).meta["country"] == "Reino Unido"

    # De novo: nada novo, e o aviso diz isso em vez de ficar calado.
    answers = iter([_WIKIDATA_EMPTY, _WIKIDATA_EMPTY, author_only])
    page = client.get(_edit(client, csrf, book_id, "complete").headers["location"]).text
    assert "não tinha nada novo para preencher" in page


def test_genres_by_group_subjects_and_catalog_suggestions(web_client, monkeypatch):
    client, _ = web_client
    csrf = login(client)
    page = client.get("/books/new?step=form").text
    assert 'class="genre-group-label">Gênero ficcional<' in page
    assert 'name="subjects" value="Programação"' in page

    book_id = _create(
        client,
        csrf,
        genres=["Técnico"],
        subjects=["Programação"],
        subjects_extra="Python, Programação",
    )
    assert bf.load_book(_cfg(client), book_id).meta["subjects"] == ["Programação", "Python"]
    detail = client.get(f"/books/{book_id}").text
    assert 'value="Python" checked' in detail  # assunto fora da lista vira opção marcada

    google = (
        "application/json",
        (
            b'{"items": [{"id": "abcDEF12", "volumeInfo": {"title": "O Hobbit",'
            b' "authors": ["J. R. R. Tolkien"], "categories": ["Juvenile Fiction"]}}]}'
        ),
    )
    _fake_lookup(monkeypatch, {"https://www.googleapis.com/": google})
    results = client.get("/books/new", params={"q": "o hobbit"}).text
    assert '<span class="tag genre-suggested">Infantojuvenil</span>' in results
    assert '<input type="hidden" name="genres" value="Infantojuvenil">' in results
    form = client.get("/books/new", params={"step": "form", "genres": ["Infantojuvenil"]}).text
    assert 'value="Infantojuvenil" checked' in form


def test_mixing_fiction_and_non_fiction_genres_is_flagged(web_client):
    client, _ = web_client
    csrf = login(client)
    mixed = _create(client, csrf, genres=["Romance", "Psicologia"])
    assert "misturam ficção e não ficção" in client.get(f"/books/{mixed}").text
    novel = _create(client, csrf, genres=["Romance", "Ficção Psicológica"])
    assert "misturam ficção e não ficção" not in client.get(f"/books/{novel}").text
