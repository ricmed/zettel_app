"""Finding notes on the web (#382): the tag filter and the search by meaning."""

from __future__ import annotations

import json

from zettel.retrieval import NoteSearchResult, RetrievedNote

from tests.web_support import login


def _seed(client):
    db = client.app.state.service.db()
    try:
        db.upsert_source(
            "@Thomas2017", "Thomas2017", "Credit Scoring", ["Thomas, L."], 2017, "f", "x", "pdf"
        )
        db.upsert_note(
            "N1",
            "@Thomas2017",
            "30_Permanent/ZTL - N1 - a.md",
            title="Gradient boosting em credit scoring",
            body="> **Tese**: Boosting supera a regressão logística em dados tabulares.",
            frontmatter_json=json.dumps({"tags": ["credit_scoring", "boosting"]}),
        )
        db.upsert_note(
            "N2",
            None,
            "30_Permanent/ZTL - N2 - b.md",
            title="Regressão logística",
            body="> **Tese**: Coeficientes interpretáveis.",
            frontmatter_json=json.dumps({"tags": ["credit_scoring"]}),
        )
        db.upsert_note("N3", None, None, title="Sem tags", body="x", frontmatter_json="{oops")
    finally:
        db.close()


def test_tag_filter_keeps_notes_whose_frontmatter_lists_the_tag(web_client):
    client, _ = web_client
    _seed(client)
    login(client)
    db = client.app.state.service.db()
    try:
        assert db.catalog_facets()["tags"] == ["credit_scoring", "boosting"]
        rows, total = db.search_catalog(tag="boosting")
        assert total == 1 and rows[0]["item_id"] == "N1"
    finally:
        db.close()
    page = client.get("/notes", params={"tag": "credit_scoring"}).text
    assert "Gradient boosting em credit scoring" in page and "Regressão logística" in page
    assert "Sem tags" not in page
    assert '<option value="boosting">' in page


def test_search_by_meaning_needs_csrf_and_the_embedding(web_client, monkeypatch):
    client, _ = web_client
    csrf = login(client)
    assert client.post("/notes/semantica", data={"q": "x"}).status_code == 403
    monkeypatch.setattr("zettel.web.preconditions.embedding_ready", lambda cfg: False)
    page = client.get("/notes").text
    assert 'type="submit" disabled title="Sem credencial de embedding' in page
    refused = client.post("/notes/semantica", data={"csrf": csrf, "q": "credit"})
    assert refused.status_code == 409 and "embedding" in refused.text


def test_search_by_meaning_shows_theses_neighbours_and_what_the_floor_cut(web_client, monkeypatch):
    client, _ = web_client
    _seed(client)
    csrf = login(client)
    monkeypatch.setattr("zettel.web.preconditions.embedding_ready", lambda cfg: True)
    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: object())
    calls = []

    class FakeRetriever:
        def __init__(self, cfg, db, idx):
            pass

        def search_notes(self, query, expand_graph=None):
            calls.append((query, expand_graph))
            seed = RetrievedNote("N1", 0.03, vector_distance=0.2)
            neighbour = RetrievedNote(
                "N2", 0.01, hop=1, via=[{"from": "N1", "relation_type": "contradicts"}]
            )
            cut = RetrievedNote("N3", 0.005, passed_floor=False, floor_reason="abaixo de 0.40")
            return NoteSearchResult(hits=[seed, neighbour], candidates=[seed, cut])

    monkeypatch.setattr("zettel.retrieval.Retriever", FakeRetriever)
    response = client.post(
        "/notes/semantica", data={"csrf": csrf, "q": "ML em pontuação de crédito", "graph": "1"}
    )
    assert response.status_code == 200
    assert calls == [("ML em pontuação de crédito", True)]
    page = response.text
    assert "2 notas encontradas" in page
    assert "Boosting supera a regressão logística em dados tabulares." in page
    assert "Fonte: Credit Scoring" in page
    assert "similaridade 0.80" in page
    assert "vizinha (salto 1)" in page and "via contradicts" in page
    assert "1 candidata(s) abaixo do piso" in page and "abaixo de 0.40" in page
