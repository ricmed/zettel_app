"""The Mesa on the web (#383): create, add from other pages, edit, conflicts."""

from __future__ import annotations

import re

import pytest
from zettel.projects import list_projects, load_project
from zettel.vault import partition_vault_notes

from tests.web_support import login

N1 = "01JAAAAAAAAAAAAAAAAAAAAAAA"
N2 = "01JBBBBBBBBBBBBBBBBBBBBBBB"
N3 = "01JCCCCCCCCCCCCCCCCCCCCCCC"


def _seed_notes(client):
    db = client.app.state.service.db()
    try:
        db.upsert_source(
            "@Thomas2017", "Thomas2017", "Credit Scoring", ["Lyn Thomas"], 2017, "f", "x", "pdf"
        )
        for note_id, title in (
            (N1, "Boosting em crédito"),
            (N2, "Regressão logística"),
            (N3, "Viés"),
        ):
            db.upsert_note(
                note_id,
                "@Thomas2017",
                f"30_Permanent/ZTL - {note_id} - n.md",
                title=title,
                body=f"> **Tese**: tese de {title}.",
            )
    finally:
        db.close()


def _create(client, csrf, title="ML em credit score"):
    response = client.post(
        "/projetos",
        data={"csrf": csrf, "title": title, "style": "academic"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def _cfg(client):
    return client.app.state.service.cfg


def test_a_desk_is_a_manual_vault_note_kept_by_reset(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    assert client.post("/projetos", data={"title": "x"}).status_code == 403
    project_id = _create(client, csrf)
    project = load_project(_cfg(client), project_id)
    assert project.path.parent.name == "50_Projetos"
    assert project.meta["origin"] == "manual" and project.meta["type"] == "project"
    _pipeline, kept = partition_vault_notes(tmp_path / "vault")
    assert project.path in kept
    listing = client.get("/projetos").text
    assert "ML em credit score" in listing


def test_notes_added_from_another_page_land_in_para_organizar(web_client):
    client, _ = web_client
    _seed_notes(client)
    csrf = login(client)
    project_id = _create(client, csrf)
    detail = client.get(f"/notes/{N1}").text
    assert f'<option value="{project_id}">ML em credit score</option>' in detail
    response = client.post(
        "/projetos/adicionar",
        data={"csrf": csrf, "projeto": project_id, "note_ids": [N1, N2, "fantasma"]},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"].endswith("#secao-2")
    again = client.post(
        "/projetos/adicionar",
        data={"csrf": csrf, "projeto": project_id, "note_ids": [N1]},
        follow_redirects=False,
    )
    assert again.status_code == 303
    project = load_project(_cfg(client), project_id)
    assert project.body.note_ids() == [N1, N2]
    page = client.get(f"/projetos/{project_id}").text
    assert "Para organizar" in page and "Boosting em crédito" in page
    assert "tese de Regressão logística." in page


def _version(page: str) -> str:
    return re.search(r'name="versao" value="([0-9a-f]+)"', page).group(1)


def test_edits_follow_the_version_and_refuse_a_stale_page(web_client):
    client, _ = web_client
    _seed_notes(client)
    csrf = login(client)
    project_id = _create(client, csrf)
    client.post(
        "/projetos/adicionar", data={"csrf": csrf, "projeto": project_id, "note_ids": [N1, N2]}
    )
    page = client.get(f"/projetos/{project_id}").text
    version = _version(page)
    url = f"/projetos/{project_id}"
    edit = {"csrf": csrf, "versao": version}
    created = client.post(url, data={**edit, "acao": "secao-criar", "titulo": "Modelos"})
    assert created.status_code == 200
    stale = client.post(url, data={**edit, "acao": "secao-criar", "titulo": "Outra"})
    assert stale.status_code == 409 and "mudou" in stale.text

    project = load_project(_cfg(client), project_id)
    inbox = next(i for i, s in enumerate(project.body.sections) if s.title == "Para organizar")
    first = project.body.sections[inbox].blocks.index(project.body.sections[inbox].cards[0])
    edit["versao"] = project.version
    moved = client.post(
        url, data={**edit, "acao": "mover", "secao": inbox, "bloco": first, "destino": inbox + 1}
    )
    assert moved.status_code == 200
    project = load_project(_cfg(client), project_id)
    assert [c.note_id for c in project.body.sections[inbox + 1].cards] == [N1]

    edit["versao"] = project.version
    modelos = inbox + 1
    card = project.body.sections[modelos].blocks.index(project.body.sections[modelos].cards[0])
    note = client.post(
        url,
        data={
            **edit,
            "acao": "anotacao",
            "secao": modelos,
            "bloco": card,
            "texto": "abre o argumento",
        },
    )
    assert note.status_code == 200
    project = load_project(_cfg(client), project_id)
    assert project.body.sections[modelos].cards[0].annotation_text == "abre o argumento"

    edit["versao"] = project.version
    bridge = client.post(
        url,
        data={**edit, "acao": "texto-novo", "secao": modelos, "texto": "Por isso, **boosting**."},
    )
    assert bridge.status_code == 200
    page = client.get(url).text
    assert "<strong>boosting</strong>" in page and "abre o argumento" in page

    unknown = client.post(url, data={**edit, "acao": "apagar-tudo"})
    assert unknown.status_code == 400


def test_a_card_whose_note_is_gone_is_shown_as_missing(web_client):
    client, _ = web_client
    _seed_notes(client)
    csrf = login(client)
    project_id = _create(client, csrf)
    client.post("/projetos/adicionar", data={"csrf": csrf, "projeto": project_id, "note_ids": [N3]})
    db = client.app.state.service.db()
    try:
        db.conn.execute("DELETE FROM notes WHERE note_id=?", (N3,))
        db.conn.commit()
    finally:
        db.close()
    page = client.get(f"/projetos/{project_id}").text
    assert "Nota ausente" in page and N3 in page
    assert len(list_projects(_cfg(client))) == 1
    assert client.get("/projetos/01JZZZZZZZZZZZZZZZZZZZZZZZ").status_code == 404
    assert client.get("/projetos/..%2F..%2Fstate").status_code == 404


def test_the_desk_converses_and_searches_a_section(web_client, monkeypatch):
    from zettel.retrieval import NoteSearchResult, RetrievedNote

    client, _ = web_client
    _seed_notes(client)
    csrf = login(client)
    project_id = _create(client, csrf)
    client.post("/projetos/adicionar", data={"csrf": csrf, "projeto": project_id, "note_ids": [N1]})
    db = client.app.state.service.db()
    try:
        db.upsert_note_connection(N3, N1, "contradicts", "o viés anula o ganho", origin="manual")
    finally:
        db.close()
    page = client.get(f"/projetos/{project_id}").text
    assert "Tensões" in page and "o viés anula o ganho" in page and "contradiz" in page

    monkeypatch.setattr("zettel.web.preconditions.embedding_ready", lambda cfg: True)
    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: object())
    queries = []

    class FakeRetriever:
        def __init__(self, cfg, db, idx):
            pass

        def search_notes(self, query):
            queries.append(query)
            return NoteSearchResult(
                hits=[RetrievedNote(N1, 0.03), RetrievedNote(N2, 0.02)], candidates=[]
            )

    monkeypatch.setattr("zettel.retrieval.Retriever", FakeRetriever)
    project = load_project(_cfg(client), project_id)
    inbox = next(i for i, s in enumerate(project.body.sections) if s.title == "Para organizar")
    found = client.post(f"/projetos/{project_id}/buscar", data={"csrf": csrf, "secao": inbox})
    assert found.status_code == 200
    assert queries == ["Para organizar tese de Boosting em crédito."]
    results = found.text.split("Para «Para organizar»")[1]
    # The note already on the desk is not suggested again.
    assert "Regressão logística" in results and f'value="{N1}"' not in results
    added = client.post(
        f"/projetos/{project_id}",
        data={"csrf": csrf, "acao": "itens", "secao": inbox, "note_ids": N2},
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert load_project(_cfg(client), project_id).body.note_ids() == [N1, N2]


def test_the_desk_exports_a_preview_a_zip_a_canvas(web_client):
    import io
    import zipfile

    client, _ = web_client
    _seed_notes(client)
    csrf = login(client)
    project_id = _create(client, csrf)
    client.post(
        "/projetos/adicionar", data={"csrf": csrf, "projeto": project_id, "note_ids": [N1, N2]}
    )
    page = client.get(f"/projetos/{project_id}").text
    assert "Baixar .docx" in page and "Gerar canvas no vault" in page

    preview = client.get(f"/projetos/{project_id}/manuscrito", params={"enviado": "1"})
    assert preview.status_code == 200
    assert "tese de Boosting em crédito (THOMAS, 2017)." in preview.text
    assert "Complementar" not in preview.text  # unticked on a submitted form
    assert "Referências" in preview.text and "THOMAS" in preview.text

    archive = client.get(
        f"/projetos/{project_id}/exportar", params={"formato": "zip", "marcadores": "1"}
    )
    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        text = z.read("manuscrito.md").decode("utf-8")
        assert "[Complementar:" in text and "\r" not in text
        assert text.split("## Referências")[1].count("THOMAS") == 1
        assert "@misc{Thomas2017," in z.read("referencias.bib").decode("utf-8")
    assert (
        client.get(f"/projetos/{project_id}/exportar", params={"formato": "pdf"}).status_code == 400
    )

    canvas = client.post(
        f"/projetos/{project_id}/canvas", data={"csrf": csrf}, follow_redirects=False
    )
    assert canvas.status_code == 303
    written = load_project(_cfg(client), project_id).path.with_suffix(".canvas")
    assert written.is_file() and '"type": "group"' in written.read_text(encoding="utf-8")
    assert written.name in client.get(f"/projetos/{project_id}").text


@pytest.mark.slow
def test_the_desk_downloads_a_docx(web_client):
    client, _ = web_client
    _seed_notes(client)
    csrf = login(client)
    project_id = _create(client, csrf)
    client.post("/projetos/adicionar", data={"csrf": csrf, "projeto": project_id, "note_ids": [N1]})
    response = client.get(f"/projetos/{project_id}/exportar", params={"formato": "docx"})
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert response.headers["content-disposition"].endswith('.docx"')
