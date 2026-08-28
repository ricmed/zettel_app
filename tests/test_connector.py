"""Tests for connector: typed connections, inverse relations, note body rendering."""

from zettel.connector import (
    _build_rag_context,
    _fallback_image_ids,
    _inverse_relation,
    _relation_type_value,
    _resolve_connections,
    _resolve_images,
)
from zettel.retrieval import RetrievedNote
from zettel.schemas import RelationType, RelationshipResult
from zettel.state import StateDB
from zettel.vault import build_permanent_note_body


class _FakeDB:
    """Minimal stub for StateDB used in _resolve_connections tests."""

    def __init__(self, notes: dict[str, dict]):
        self._notes = notes

    def get_note(self, note_id: str):
        return self._notes.get(note_id)


def test_inverse_relation_mapping():
    """All defined relation types have a PT-BR inverse."""
    assert _inverse_relation("supports") == "suportado por"
    assert _inverse_relation("contradicts") == "contradiz"
    assert _inverse_relation("extends") == "estendido por"
    assert _inverse_relation("depends_on") == "base para"
    assert _inverse_relation("exemplifies") == "exemplificado por"
    assert _inverse_relation("related") == "relacionado"


def test_inverse_relation_unknown_falls_back():
    """Unknown relation type defaults to 'relacionado'."""
    assert _inverse_relation("unknown_type") == "relacionado"


def test_resolve_connections_with_known_note():
    """When the note has a path, wiki-link uses the file stem (not a title slug)."""
    db = _FakeDB({
        "ABC123": {
            "title": "Gradient Descent Adaptativo",
            "path": "/vault/30_Permanent/ZTL - ABC123 - gradient-descent-adaptativo.md",
        },
    })
    connections = [
        RelationshipResult(
            related_note_id="ABC123",
            relation_type="extends",
            description="Amplia o conceito base",
        ),
    ]
    resolved = _resolve_connections(db, connections)
    assert len(resolved) == 1
    assert "[[ZTL - ABC123 - gradient-descent-adaptativo]]" == resolved[0]["wiki_link"]
    assert resolved[0]["relation_type"] == "extends"
    assert resolved[0]["description"] == "Amplia o conceito base"


def test_resolve_connections_with_unknown_note():
    """When note is not in DB, wiki-link uses just the ID."""
    db = _FakeDB({})
    connections = [
        RelationshipResult(
            related_note_id="UNKNOWN",
            relation_type="related",
            description="",
        ),
    ]
    resolved = _resolve_connections(db, connections)
    assert len(resolved) == 1
    assert "[[ZTL - UNKNOWN]]" == resolved[0]["wiki_link"]


def test_relation_type_value_from_enum():
    """str Enum members must resolve to the value, not 'RelationType.X'."""
    assert _relation_type_value(RelationType.SUPPORTS) == "supports"
    assert _relation_type_value(RelationType.EXTENDS) == "extends"
    assert _relation_type_value("contradicts") == "contradicts"
    # Regression: f-string of the enum itself is NOT the vault label.
    assert f"{RelationType.SUPPORTS}" == "RelationType.SUPPORTS"


def test_resolve_connections_normalizes_enum_relation_type():
    """Pydantic may leave relation_type as RelationType; vault needs plain str."""
    db = _FakeDB({
        "ABC123": {"title": "Nota Alvo", "path": "/vault/note.md"},
    })
    connections = [
        RelationshipResult(
            related_note_id="ABC123",
            relation_type=RelationType.SUPPORTS,
            description="Reforca a tese",
        ),
    ]
    resolved = _resolve_connections(db, connections)
    assert resolved[0]["relation_type"] == "supports"
    assert "RelationType" not in resolved[0]["relation_type"]


def test_build_permanent_note_body_with_enum_relation_type():
    """Defensive: even if an Enum sneaks into the dict, render the value."""
    body = build_permanent_note_body(
        thesis="Tese",
        definition="Def",
        intuition="",
        example="",
        limits="",
        connections=[{
            "wiki_link": "[[ZTL - ABC - titulo]]",
            "relation_type": RelationType.SUPPORTS,
            "description": "Reforca",
        }],
        literature_ref="[[LIT - @x]]",
        source_locator="",
    )
    assert "(supports) -- Reforca" in body
    assert "RelationType.SUPPORTS" not in body


def test_build_permanent_note_body_with_connections():
    """Connections are rendered with type and description in the note body."""
    connections = [
        {
            "wiki_link": "[[ZTL - ABC - titulo-nota]]",
            "relation_type": "supports",
            "description": "Valida a tese",
        },
        {
            "wiki_link": "[[ZTL - DEF - outra-nota]]",
            "relation_type": "contradicts",
            "description": "",
        },
    ]
    body = build_permanent_note_body(
        thesis="Tese de teste",
        definition="Definicao de teste",
        intuition="",
        example="",
        limits="",
        connections=connections,
        literature_ref="[[LIT - @test]]",
        source_locator="p.10",
    )
    assert "## Conexões" in body
    assert "[[ZTL - ABC - titulo-nota]] (supports) -- Valida a tese" in body
    assert "[[ZTL - DEF - outra-nota]] (contradicts)" in body
    # Second connection has no description, so no " -- " suffix
    lines = body.split("\n")
    contradicts_line = [l for l in lines if "contradicts" in l][0]
    assert contradicts_line.endswith("(contradicts)")


def test_build_permanent_note_body_without_connections():
    """When connections list is empty, no Conexoes section is rendered."""
    body = build_permanent_note_body(
        thesis="Tese",
        definition="Def",
        intuition="",
        example="",
        limits="",
        connections=[],
        literature_ref="[[LIT - @x]]",
        source_locator="",
    )
    assert "## Conexões" not in body


def test_build_permanent_note_body_with_figures():
    body = build_permanent_note_body(
        thesis="Tese",
        definition="Def",
        intuition="",
        example="",
        limits="",
        connections=[],
        literature_ref="[[LIT - @x]]",
        source_locator="p.1",
        images=[{"path": "90_Assets/img-abc.png", "description": "Diagrama RAG"}],
    )
    assert "## Figuras" in body
    assert "![[90_Assets/img-abc.png]]" in body
    assert "Diagrama RAG" in body


def test_build_rag_context_two_groups():
    """RAG context separates embedding seeds (hop 0) from graph neighbours (hop 1)."""
    hits = [
        RetrievedNote(
            note_id="AAA", score=0.9, title="Nota Semente",
            document="corpo da semente", hop=0, metadata={"tags": "ml"},
        ),
        RetrievedNote(
            note_id="BBB", score=0.4, title="Nota Vizinha",
            document="corpo vizinho", hop=1,
            via=[{"from": "AAA", "relation_type": "contradicts", "description": ""}],
        ),
    ]
    ctx = _build_rag_context(_FakeDB({}), hits)
    assert "### Similares por embedding" in ctx
    assert "### Vizinhas por conexao no grafo" in ctx
    assert "[[ZTL - AAA - nota-semente]]" in ctx
    assert "[[ZTL - BBB - nota-vizinha]]" in ctx
    # Neighbour line carries the relation type and its anchor.
    assert "relacao: contradicts a partir de [[ZTL - AAA]]" in ctx


def test_build_rag_context_only_seeds_no_graph_heading():
    hits = [RetrievedNote(note_id="AAA", score=0.9, title="So Semente", hop=0)]
    ctx = _build_rag_context(_FakeDB({}), hits)
    assert "### Similares por embedding" in ctx
    assert "### Vizinhas por conexao no grafo" not in ctx


def test_build_rag_context_empty():
    assert _build_rag_context(_FakeDB({}), []) == "Nenhuma nota existente encontrada."


def test_fallback_image_ids_from_chunk_text(tmp_path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
        db.upsert_chapter("@S::ch000", "@S", "Cap", "ck", "Cap")
        chunk_text = "Texto com ![Imagem](90_Assets/img-fig.png) no meio."
        db.upsert_chunk("c1", "@S", "@S::ch000", chunk_text, "h1")
        db.upsert_asset("@S::img::fig", "@S", "90_Assets/img-fig.png", "ckfig")
        ids = _fallback_image_ids(db, {"chunk_id": "c1", "source_id": "@S"})
        assert ids == ["@S::img::fig"]
        resolved = _resolve_images(db, ids)
        assert resolved[0]["path"] == "90_Assets/img-fig.png"
    finally:
        db.close()


def test_fallback_image_ids_empty_when_no_paths(tmp_path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
        db.upsert_chapter("@S::ch000", "@S", "Cap", "ck", "Cap")
        db.upsert_chunk("c1", "@S", "@S::ch000", "sem imagens", "h1")
        db.upsert_asset("@S::img::fig", "@S", "90_Assets/img-fig.png", "ckfig")
        assert _fallback_image_ids(db, {"chunk_id": "c1", "source_id": "@S"}) == []
    finally:
        db.close()
