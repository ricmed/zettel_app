"""Reading a note on the web (#381): vault images, MOC links, edge direction, source."""

from __future__ import annotations

import json

from tests.web_support import login

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00"
    b"\x00\x00\x00IEND\xaeB`\x82"
)
ASSET = "img-0123456789abcdef.png"


def _seed(client, tmp_path):
    (tmp_path / "vault" / "90_Assets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "vault" / "90_Assets" / ASSET).write_bytes(PNG)
    db = client.app.state.service.db()
    try:
        db.upsert_source(
            "@Lai2025",
            "Lai2025",
            "Data Science",
            ["Lai, T.", "Xing, H."],
            2025,
            "f",
            "inbox/lai.pdf",
            "pdf",
            abnt_reference="LAI, T.; XING, H. Data Science. 2025.",
        )
        db.upsert_note(
            "N1",
            "@Lai2025",
            "30_Permanent/ZTL - N1 - a.md",
            title="Nota principal",
            body=f"> **Tese**: tese\n\n## Figuras\n\n![[90_Assets/{ASSET}]]\n\n"
            "Ver [[MOC - M1 - tema|o mapa]].",
            provenance_json=json.dumps({"citation": "(LAI; XING, 2025, p. 3)"}),
        )
        db.upsert_note("N2", None, None, title="Nota que estende", body="b")
        db.upsert_note("N3", None, None, title="Nota proposta", body="c")
        db.upsert_note("N4", None, None, title="Nota rejeitada", body="d")
        db.upsert_note_connection("N2", "N1", "extends", "amplia")
        db.upsert_note_connection("N1", "N3", "contradicts", "discorda", status="proposed")
        db.upsert_note_connection("N1", "N4", "supports", "x", status="rejected")
        db.upsert_moc("M1", "Risco de crédito", body="- [[ZTL - N1 - a]] — porta")
    finally:
        db.close()


def test_asset_route_serves_only_vault_images(web_client):
    client, tmp_path = web_client
    _seed(client, tmp_path)
    assert client.get(f"/assets/{ASSET}", follow_redirects=False).status_code == 303
    login(client)
    response = client.get(f"/assets/{ASSET}")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    for name in ("img-0123456789abcdef.svg", "..%2F..%2Fstate.db", "img-missing00000000.png"):
        assert client.get(f"/assets/{name}").status_code == 404


def test_asset_folder_matches_the_asset_module():
    from zettel.assets import ASSETS_DIR
    from zettel.markdown import ASSETS_FOLDER

    assert ASSETS_FOLDER == ASSETS_DIR


def test_note_detail_shows_figure_source_mocs_and_directed_edges(web_client):
    client, tmp_path = web_client
    _seed(client, tmp_path)
    login(client)
    page = client.get("/notes/N1").text
    assert f'<img src="/assets/{ASSET}"' in page
    assert '<a href="/mocs/M1">o mapa</a>' in page
    assert "(LAI; XING, 2025, p. 3)" in page
    assert "LAI, T.; XING, H. Data Science. 2025." in page
    assert '<a href="/mocs/M1">Risco de crédito</a>' in page
    outgoing, incoming = page.split("Entradas: esta nota é")
    assert "Nota proposta" in outgoing and "contradiz" in outgoing and "proposta</span>" in outgoing
    assert "Nota que estende" in incoming and "estendido por" in incoming
    assert "Nota rejeitada" not in page
