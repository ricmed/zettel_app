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
    assert "Técnico/Não-ficção nacional" in detail
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
