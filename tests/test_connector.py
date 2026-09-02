"""Tests for connector: typed connections, inverse relations, note body rendering."""

from pathlib import Path

from zettel.connector import (
    _build_rag_context,
    _fallback_image_ids,
    _inverse_relation,
    _persist_and_backlink,
    _relation_type_value,
    _resolve_connections,
    _resolve_images,
    rebuild_auto_backlinks,
)
from zettel.retrieval import RetrievedNote
from zettel.schemas import RelationType, RelationshipResult
from zettel.state import StateDB
from zettel.vault import build_permanent_note_body, read_managed_block


class _FakeDB:
    """Minimal stub for StateDB used in _resolve_connections tests."""

    def __init__(self, notes: dict[str, dict]):
        self._notes = notes

    def get_note(self, note_id: str):
        return self._notes.get(note_id)


def _write_note(path: Path, body: str = "corpo") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


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


def test_resolve_connections_with_known_note(tmp_path):
    """When the note has a path on disk, wiki-link uses the file stem."""
    note_path = _write_note(
        tmp_path / "ZTL - ABC123 - gradient-descent-adaptativo.md"
    )
    db = _FakeDB({
        "ABC123": {
            "title": "Gradient Descent Adaptativo",
            "path": str(note_path),
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
    assert resolved[0]["related_note_id"] == "ABC123"


def test_resolve_connections_normalizes_prefixed_ulid(tmp_path):
    ulid = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    note_path = _write_note(tmp_path / f"ZTL - {ulid} - analise-de-series-temporais.md")
    db = _FakeDB({
        ulid: {"title": "Analise de series temporais", "path": str(note_path)},
    })
    connections = [
        RelationshipResult(
            related_note_id=f"ZTL - ZTL - {ulid}",
            relation_type="extends",
            description="Contexto mais amplo",
        ),
    ]
    resolved = _resolve_connections(db, connections)
    assert len(resolved) == 1
    assert resolved[0]["related_note_id"] == ulid
    assert resolved[0]["wiki_link"] == f"[[ZTL - {ulid} - analise-de-series-temporais]]"


def test_resolve_connections_normalizes_wikilink_with_slug(tmp_path):
    ulid = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    note_path = _write_note(tmp_path / f"ZTL - {ulid} - analise.md")
    db = _FakeDB({ulid: {"title": "Analise", "path": str(note_path)}})
    connections = [
        RelationshipResult(
            related_note_id=f"[[ZTL - {ulid} - analise]]",
            relation_type="related",
        ),
    ]
    resolved = _resolve_connections(db, connections)
    assert resolved[0]["related_note_id"] == ulid
    assert resolved[0]["wiki_link"] == f"[[ZTL - {ulid} - analise]]"


def test_resolve_connections_with_unknown_note():
    """When the note is not in DB, the connection is dropped (no phantom wikilink)."""
    db = _FakeDB({})
    connections = [
        RelationshipResult(
            related_note_id="UNKNOWN",
            relation_type="related",
            description="",
        ),
    ]
    resolved = _resolve_connections(db, connections)
    assert resolved == []


def test_resolve_connections_drops_missing_file(tmp_path):
    db = _FakeDB({
        "ABC123": {
            "title": "Fantasma",
            "path": str(tmp_path / "nao-existe.md"),
        },
    })
    connections = [
        RelationshipResult(related_note_id="ABC123", relation_type="related"),
    ]
    assert _resolve_connections(db, connections) == []


def test_relation_type_value_from_enum():
    """str Enum members must resolve to the value, not 'RelationType.X'."""
    assert _relation_type_value(RelationType.SUPPORTS) == "supports"
    assert _relation_type_value(RelationType.EXTENDS) == "extends"
    assert _relation_type_value("contradicts") == "contradicts"
    # Regression: f-string of the enum itself is NOT the vault label.
    assert f"{RelationType.SUPPORTS}" == "RelationType.SUPPORTS"


def test_resolve_connections_normalizes_enum_relation_type(tmp_path):
    """Pydantic may leave relation_type as RelationType; vault needs plain str."""
    note_path = _write_note(tmp_path / "note.md")
    db = _FakeDB({
        "ABC123": {"title": "Nota Alvo", "path": str(note_path)},
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
    assert "note_id: AAA" in ctx
    assert "note_id: BBB" in ctx
    # Neighbour line carries the relation type and its anchor as a raw id.
    assert "relacao: contradicts a partir de note_id: AAA" in ctx


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


def test_rebuild_auto_backlinks_drops_missing_source(tmp_path):
    db = StateDB(tmp_path / "s.db")
    try:
        target = tmp_path / "ZTL - TGT - alvo.md"
        source = tmp_path / "ZTL - SRC - origem.md"
        _write_note(target, "## Conexoes\n")
        _write_note(source, "## Conexoes\n")
        db.upsert_note("TGT", "@S", str(target), "Alvo", body="x")
        db.upsert_note("SRC", "@S", str(source), "Origem", body="x")
        db.upsert_note("GONE", "@S", str(tmp_path / "missing.md"), "Gone", body="x")
        db.upsert_note_connection("SRC", "TGT", "related", "ainda existe")
        db.upsert_note_connection("GONE", "TGT", "related", "fantasma")
        from zettel.vault import safe_update_managed_blocks
        safe_update_managed_blocks(target, {
            "auto-backlinks": "- [[ZTL - GONE - gone]] (relacionado) -- fantasma",
        })
        assert rebuild_auto_backlinks(db, "TGT") is True
        block = read_managed_block(target.read_text(encoding="utf-8"), "auto-backlinks")
        assert "SRC" in block
        assert "ainda existe" in block
        assert "GONE" not in block
    finally:
        db.close()


def test_rebuild_auto_backlinks_uses_current_stem(tmp_path):
    db = StateDB(tmp_path / "s.db")
    try:
        target = tmp_path / "ZTL - TGT - alvo.md"
        source = tmp_path / "ZTL - SRC - slug-novo.md"
        _write_note(target, "## Conexoes\n")
        _write_note(source, "## Conexoes\n")
        db.upsert_note("TGT", "@S", str(target), "Alvo", body="x")
        db.upsert_note("SRC", "@S", str(source), "Origem", body="x")
        db.upsert_note_connection("SRC", "TGT", "extends", "amplia")
        from zettel.vault import safe_update_managed_blocks
        safe_update_managed_blocks(target, {
            "auto-backlinks": "- [[ZTL - SRC - slug-antigo]] (estendido por) -- amplia",
        })
        assert rebuild_auto_backlinks(db, "TGT") is True
        block = read_managed_block(target.read_text(encoding="utf-8"), "auto-backlinks")
        assert "slug-novo" in block
        assert "slug-antigo" not in block
        assert "estendido por" in block
    finally:
        db.close()


def test_persist_and_backlink_writes_inverse_on_target(tmp_path):
    from zettel.config import AppConfig

    db = StateDB(tmp_path / "s.db")
    try:
        src = tmp_path / "ZTL - NEW - nova.md"
        tgt = tmp_path / "ZTL - OLD - velha.md"
        _write_note(src)
        _write_note(tgt)
        db.upsert_note("NEW", "@S", str(src), "Nova")
        db.upsert_note("OLD", "@S", str(tgt), "Velha")
        _persist_and_backlink(
            AppConfig(vault_path=tmp_path),
            db, "NEW", "Nova",
            [{
                "related_note_id": "OLD",
                "relation_type": "extends",
                "description": "amplia",
            }],
        )
        edges = db.get_note_connections("NEW")
        assert len(edges) == 1
        assert edges[0]["target_note_id"] == "OLD"
        block = read_managed_block(tgt.read_text(encoding="utf-8"), "auto-backlinks")
        assert "estendido por" in block
        assert "ZTL - NEW - nova" in block
        assert "amplia" in block
    finally:
        db.close()


def test_parse_permanent_note_accepts_minimal_rejection():
    """A rejected concept answers with status/reason/category only (no note body)."""
    from zettel.connector import _parse_permanent_note_output

    out = _parse_permanent_note_output(
        '{"status": "rejected", "reason": "propaganda", "category": "promotional"}'
    )
    assert out.status == "rejected"
    assert out.category == "promotional"
    assert out.title == "" and out.thesis == "" and out.definition == ""


def test_parse_permanent_note_rejects_accepted_without_body():
    """An accepted answer missing the body is a broken response, not an empty note."""
    import pytest

    from zettel.connector import _parse_permanent_note_output

    with pytest.raises(ValueError, match="obrigatorios"):
        _parse_permanent_note_output('{"status": "accepted", "reason": "ok"}')


def test_ptbr_guard_roundtrips_the_json_object(monkeypatch, tmp_path):
    """The guard sends 5 keys as JSON and must get the same object back.

    The prompt used to ask for "apenas o texto corrigido"; `json.loads` then raised
    and the `except` swallowed it, turning the guard into a silent no-op.
    """
    import json

    from zettel.config import AppConfig
    from zettel.connector import _apply_ptbr_guard
    from zettel.schemas import PermanentNoteLLMOutput

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    output = PermanentNoteLLMOutput(
        status="accepted", reason="ok", category="", title="T",
        thesis="The model learns from data",
        definition="This definition is in English and should be translated",
        intuition="Like a student", example="An example", limits="Some limits",
    )

    sent: dict[str, str] = {}

    def fake_call_llm(llm, user, system=None, **kwargs):
        sent["user"] = user
        payload = json.loads(user[user.index("{"):user.rindex("}") + 1])
        assert set(payload) == {"thesis", "definition", "intuition", "example", "limits"}
        return json.dumps({k: f"[ptbr] {v}" for k, v in payload.items()})

    monkeypatch.setattr("zettel.connector.call_llm", fake_call_llm)
    fixed = _apply_ptbr_guard(cfg, object(), output)

    assert fixed.thesis == "[ptbr] The model learns from data"
    assert fixed.definition.startswith("[ptbr] ")
    assert fixed.example == "[ptbr] An example"
    assert "{text}" not in sent["user"]
