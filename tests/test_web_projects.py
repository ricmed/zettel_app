"""The Mesa on the web (#383): create, add from other pages, edit, conflicts."""

from __future__ import annotations

import re

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
            "@Thomas2017", "Thomas2017", "Credit Scoring", ["Thomas, L."], 2017, "f", "x", "pdf"
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
