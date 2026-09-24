"""Tests for connector: typed connections, inverse relations, note body rendering."""

from pathlib import Path

from zettel.config import AppConfig
from zettel.connector.context import build_rag_context, fallback_image_ids, resolve_images
from zettel.connector.links import (
    assemble_connections,
    corroborating_note_ids,
    demote_llm_corroborates,
    inverse_relation,
    persist_and_backlink,
    rebuild_auto_backlinks,
    relation_type_value,
    resolve_connections,
)
from zettel.retrieval import RetrievedNote
from zettel.schemas import RelationshipResult, RelationType
from zettel.state import StateDB
from zettel.vault import build_permanent_note_body, read_managed_block


class _FakeDB:
    """Minimal stub for StateDB used in resolve_connections tests."""

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
    assert inverse_relation("supports") == "suportado por"
    assert inverse_relation("contradicts") == "contradiz"
    assert inverse_relation("extends") == "estendido por"
    assert inverse_relation("depends_on") == "base para"
    assert inverse_relation("exemplifies") == "exemplificado por"
    assert inverse_relation("related") == "relacionado"
    assert inverse_relation("corroborates") == "corroborado por"


def test_inverse_relation_unknown_falls_back():
    """Unknown relation type defaults to 'relacionado'."""
    assert inverse_relation("unknown_type") == "relacionado"


# ── Corroboration: an edge derived from source_id, never from the model ──


def _hit(note_id: str, distance: float, hop: int = 0):
    return RetrievedNote(note_id=note_id, score=1.0, vector_distance=distance, hop=hop)


def test_corroborating_note_ids_picks_other_sources_above_threshold():
    cfg = AppConfig()
    db = _FakeDB(
        {
            "SAME": {"source_id": "@A"},  # same source -> dedupe territory, not corroboration
            "OTHER": {"source_id": "@B"},
            "WEAK": {"source_id": "@C"},
            "NEIGHBOUR": {"source_id": "@D"},
        }
    )
    hits = [
        _hit("SAME", 0.10),
        _hit("OTHER", 0.10),  # similarity 0.95 >= 0.85
        _hit("WEAK", 0.60),  # similarity 0.70 < 0.85
        _hit("NEIGHBOUR", 0.10, hop=1),  # arrived by traversal: no similarity to judge
    ]
    assert corroborating_note_ids(cfg, db, hits, "@A", "SELF") == ["OTHER"]


def test_corroborating_note_ids_respects_max_edges():
    cfg = AppConfig()
    cfg.linking.corroborates_max_edges = 2
    db = _FakeDB({nid: {"source_id": f"@{nid}"} for nid in ("B1", "B2", "B3")})
    hits = [_hit("B1", 0.30), _hit("B2", 0.10), _hit("B3", 0.20)]
    # Ranked by similarity, so the closest two win.
    assert corroborating_note_ids(cfg, db, hits, "@A", "SELF") == ["B2", "B3"]


def test_corroborating_note_ids_ignores_notes_without_a_source():
    """A manual note with no source_id cannot be evidence of a second author."""
    cfg = AppConfig()
    db = _FakeDB({"ORPHAN": {"source_id": None}})
    assert corroborating_note_ids(cfg, db, [_hit("ORPHAN", 0.0)], "@A", "SELF") == []


def test_llm_emitted_corroborates_is_demoted_to_supports():
    """The prompt never offers it; a model that emits it anyway means `supports`."""
    conns = [
        RelationshipResult(
            related_note_id="X",
            relation_type=RelationType.CORROBORATES,
            description="tambem concorda",
        ),
        RelationshipResult(related_note_id="Y", relation_type="extends", description="amplia"),
    ]
    demoted = demote_llm_corroborates(conns)
    assert relation_type_value(demoted[0].relation_type) == "supports"
    assert relation_type_value(demoted[1].relation_type) == "extends"


def test_corroborates_edge_is_persisted_with_derived_origin(tmp_path):
    """An audit must be able to tell a code-derived edge from a model-proposed one."""
    db = StateDB(tmp_path / "state.db")
    for nid in ("SRC1", "TGT1", "TGT2"):
        path = _write_note(tmp_path / f"ZTL - {nid} - nota.md")
        db.upsert_note(nid, "@S", str(path), title=f"Nota {nid}", body="corpo")
    persist_and_backlink(
        AppConfig(vault_path=tmp_path),
        db,
        "SRC1",
        [
            {
                "related_note_id": "TGT1",
                "relation_type": "corroborates",
                "description": "outra fonte",
                "wiki_link": "[[ZTL - TGT1 - nota]]",
            },
            {
                "related_note_id": "TGT2",
                "relation_type": "extends",
                "description": "amplia",
                "wiki_link": "[[ZTL - TGT2 - nota]]",
            },
        ],
    )
    origins = {r["target_note_id"]: r["origin"] for r in db.get_note_connections("SRC1")}
    assert origins == {"TGT1": "derived", "TGT2": "llm"}
    db.close()


def test_resolve_connections_preserves_injected_corroborates(tmp_path):
    """The demotion guards the LLM boundary only — injected edges must survive."""
    note_path = _write_note(tmp_path / "ZTL - BBB222 - mesma-ideia-outro-autor.md")
    db = _FakeDB({"BBB222": {"title": "Mesma ideia", "path": str(note_path)}})
    resolved = resolve_connections(
        db,
        [
            RelationshipResult(
                related_note_id="BBB222",
                relation_type=RelationType.CORROBORATES,
                description="Outra fonte sustenta a mesma ideia",
            )
        ],
    )
    assert resolved[0]["relation_type"] == "corroborates"


def test_resolve_connections_with_known_note(tmp_path):
    """When the note has a path on disk, wiki-link uses the file stem."""
    note_path = _write_note(tmp_path / "ZTL - ABC123 - gradient-descent-adaptativo.md")
    db = _FakeDB(
        {
            "ABC123": {
                "title": "Gradient Descent Adaptativo",
                "path": str(note_path),
            },
        }
    )
    connections = [
        RelationshipResult(
            related_note_id="ABC123",
            relation_type="extends",
            description="Amplia o conceito base",
        ),
    ]
    resolved = resolve_connections(db, connections)
    assert len(resolved) == 1
    assert resolved[0]["wiki_link"] == "[[ZTL - ABC123 - gradient-descent-adaptativo]]"
    assert resolved[0]["relation_type"] == "extends"
    assert resolved[0]["description"] == "Amplia o conceito base"
    assert resolved[0]["related_note_id"] == "ABC123"


def test_resolve_connections_normalizes_prefixed_ulid(tmp_path):
    ulid = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    note_path = _write_note(tmp_path / f"ZTL - {ulid} - analise-de-series-temporais.md")
    db = _FakeDB(
        {
            ulid: {"title": "Analise de series temporais", "path": str(note_path)},
        }
    )
    connections = [
        RelationshipResult(
            related_note_id=f"ZTL - ZTL - {ulid}",
            relation_type="extends",
            description="Contexto mais amplo",
        ),
    ]
    resolved = resolve_connections(db, connections)
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
    resolved = resolve_connections(db, connections)
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
    resolved = resolve_connections(db, connections)
    assert resolved == []


def test_resolve_connections_drops_missing_file(tmp_path):
    db = _FakeDB(
        {
            "ABC123": {
                "title": "Fantasma",
                "path": str(tmp_path / "nao-existe.md"),
            },
        }
    )
    connections = [
        RelationshipResult(related_note_id="ABC123", relation_type="related"),
    ]
    assert resolve_connections(db, connections) == []


def test_relation_type_value_from_enum():
    """RelationType values must stay plain strings for vault labels."""
    assert relation_type_value(RelationType.SUPPORTS) == "supports"
    assert relation_type_value(RelationType.EXTENDS) == "extends"
    assert relation_type_value("contradicts") == "contradicts"
    assert f"{RelationType.SUPPORTS}" == "supports"


def test_resolve_connections_normalizes_enum_relation_type(tmp_path):
    """Pydantic may leave relation_type as RelationType; vault needs plain str."""
    note_path = _write_note(tmp_path / "note.md")
    db = _FakeDB(
        {
            "ABC123": {"title": "Nota Alvo", "path": str(note_path)},
        }
    )
    connections = [
        RelationshipResult(
            related_note_id="ABC123",
            relation_type=RelationType.SUPPORTS,
            description="Reforca a tese",
        ),
    ]
    resolved = resolve_connections(db, connections)
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
        connections=[
            {
                "wiki_link": "[[ZTL - ABC - titulo]]",
                "relation_type": RelationType.SUPPORTS,
                "description": "Reforca",
            }
        ],
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
    contradicts_line = next(line for line in lines if "contradicts" in line)
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


def test_permanent_note_source_section_renders_structural_page():
    """The page is its own field, distinct from the LLM-authored locator."""
    body = build_permanent_note_body(
        thesis="Tese",
        definition="Def",
        intuition="",
        example="",
        limits="",
        connections=[],
        literature_ref="[[Book2024/LIT - Book2024 - p042 - topico-0001|p. 42 — Topico]]",
        source_locator="p.42 / Capitulo 1",
        page=42,
    )
    assert "- Página: 42" in body
    assert "- Localizador: p.42 / Capitulo 1" in body
    assert "|p. 42 — Topico]]" in body


def test_permanent_note_source_section_omits_page_without_paging():
    """Native Markdown has no pages (ADR-013): omit the field, never render null."""
    body = build_permanent_note_body(
        thesis="Tese",
        definition="Def",
        intuition="",
        example="",
        limits="",
        connections=[],
        literature_ref="[[LIT - @x]]",
        source_locator="Documento > Secao",
        page=None,
    )
    assert "Página" not in body
    assert "- Localizador: Documento > Secao" in body


def test_permanent_note_source_section_omits_empty_literature_ref():
    """A blank ref used to render a dangling '- Ref. literatura: ' line."""
    body = build_permanent_note_body(
        thesis="Tese",
        definition="Def",
        intuition="",
        example="",
        limits="",
        connections=[],
        literature_ref="",
        source_locator="",
    )
    assert "Ref. literatura" not in body
    assert "## Fonte" in body


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
            note_id="AAA",
            score=0.9,
            title="Nota Semente",
            document="corpo da semente",
            hop=0,
            metadata={"tags": "ml"},
        ),
        RetrievedNote(
            note_id="BBB",
            score=0.4,
            title="Nota Vizinha",
            document="corpo vizinho",
            hop=1,
            via=[{"from": "AAA", "relation_type": "contradicts", "description": ""}],
        ),
    ]
    ctx = build_rag_context(_FakeDB({}), hits)
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
    ctx = build_rag_context(_FakeDB({}), hits)
    assert "### Similares por embedding" in ctx
    assert "### Vizinhas por conexao no grafo" not in ctx


def test_build_rag_context_empty():
    assert build_rag_context(_FakeDB({}), []) == "Nenhuma nota existente encontrada."


def test_build_rag_context_distant_group():
    similar = [RetrievedNote(note_id="AAA", score=0.9, title="Semente", hop=0)]
    distant = [
        RetrievedNote(
            note_id="CCC",
            score=0.4,
            title="Ponte",
            hop=0,
            origin="distant_analogy",
        )
    ]
    ctx = build_rag_context(_FakeDB({}), similar, distant)
    assert "### Analogias distantes (outro dominio)" in ctx
    assert "note_id: CCC" in ctx
    assert "analogia: outro bucket taxonomico" in ctx


def test_fallback_image_ids_from_chunk_text(tmp_path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
        db.upsert_chapter("@S::ch000", "@S", "Cap", "ck", "Cap")
        chunk_text = "Texto com ![Imagem](90_Assets/img-fig.png) no meio."
        db.upsert_chunk("c1", "@S", "@S::ch000", chunk_text, "h1")
        db.upsert_asset("@S::img::fig", "@S", "90_Assets/img-fig.png", "ckfig")
        ids = fallback_image_ids(db, "@S", db.get_chunk("c1"))
        assert ids == ["@S::img::fig"]
        resolved = resolve_images(db, ids)
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
        assert fallback_image_ids(db, "@S", db.get_chunk("c1")) == []
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

        safe_update_managed_blocks(
            target,
            {
                "auto-backlinks": "- [[ZTL - GONE - gone]] (relacionado) -- fantasma",
            },
        )
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

        safe_update_managed_blocks(
            target,
            {
                "auto-backlinks": "- [[ZTL - SRC - slug-antigo]] (estendido por) -- amplia",
            },
        )
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
        persist_and_backlink(
            AppConfig(vault_path=tmp_path),
            db,
            "NEW",
            [
                {
                    "related_note_id": "OLD",
                    "relation_type": "extends",
                    "description": "amplia",
                }
            ],
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
    from zettel.connector.prompt import parse_permanent_note_output

    out = parse_permanent_note_output(
        '{"status": "rejected", "reason": "propaganda", "category": "promotional"}'
    )
    assert out.status == "rejected"
    assert out.category == "promotional"
    assert out.title == "" and out.thesis == "" and out.definition == ""


def test_parse_permanent_note_rejects_accepted_without_body():
    """An accepted answer missing the body is a broken response, not an empty note."""
    import pytest
    from zettel.connector.prompt import parse_permanent_note_output

    with pytest.raises(ValueError, match="obrigatorios"):
        parse_permanent_note_output('{"status": "accepted", "reason": "ok"}')


def test_ptbr_guard_roundtrips_the_json_object(monkeypatch, tmp_path):
    """The guard sends 5 keys as JSON and must get the same object back.

    The prompt used to ask for "apenas o texto corrigido"; `json.loads` then raised
    and the `except` swallowed it, turning the guard into a silent no-op.
    """
    import json

    from zettel.config import AppConfig
    from zettel.connector.prompt import apply_ptbr_guard
    from zettel.schemas import PermanentNoteLLMOutput

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    output = PermanentNoteLLMOutput(
        status="accepted",
        reason="ok",
        category="",
        title="T",
        thesis="The model learns from data",
        definition="This definition is in English and should be translated",
        intuition="Like a student",
        example="An example",
        limits="Some limits",
    )

    sent: dict[str, str] = {}

    def fake_call_llm(llm, user, system=None, **kwargs):
        sent["user"] = user
        payload = json.loads(user[user.index("{") : user.rindex("}") + 1])
        assert set(payload) == {
            "thesis",
            "definition",
            "intuition",
            "example",
            "limits",
        }
        return json.dumps({k: f"[ptbr] {v}" for k, v in payload.items()})

    monkeypatch.setattr("zettel.connector.prompt.call_llm", fake_call_llm)
    fixed = apply_ptbr_guard(cfg, object(), output)

    assert fixed.thesis == "[ptbr] The model learns from data"
    assert fixed.definition.startswith("[ptbr] ")
    assert fixed.example == "[ptbr] An example"
    assert "{text}" not in sent["user"]


# ── Refinement edge, distant suggestions, PT-BR heuristic ─────────────


def test_assemble_connections_injects_extends_once(tmp_path):
    """A refinement becomes `extends`, unless the model already linked that note."""
    cfg = AppConfig(vault_path=tmp_path)
    db = _FakeDB({})
    injected = assemble_connections(
        cfg, db, [], similar=[], source_id="@A", note_id="NEW", refines_note_id="OLD"
    )
    assert [(c.related_note_id, relation_type_value(c.relation_type)) for c in injected] == [
        ("OLD", "extends")
    ]
    assert injected[0].description == "Refina nota existente"

    own = [RelationshipResult(related_note_id="OLD", relation_type="supports", description="x")]
    kept = assemble_connections(
        cfg, db, own, similar=[], source_id="@A", note_id="NEW", refines_note_id="OLD"
    )
    assert [relation_type_value(c.relation_type) for c in kept] == ["supports"]


def test_needs_ptbr_fix_counts_whole_words_only():
    from zettel.connector.prompt import needs_ptbr_fix

    assert needs_ptbr_fix("The model learns from data that it sees")
    # Substrings of Portuguese words ("grande", "atheneu", "fromage") are not English.
    assert not needs_ptbr_fix("Uma grande sandes no atheneu com fromage e mandioca")


def test_run_connect_links_refinement_and_writes_suggestions_once(tmp_path, monkeypatch):
    """The dedupe target crosses review->connect via SQLite and becomes `extends`;
    a distant analogy lands in `auto-connections`, and SQLite is written once."""
    from zettel.connector import context, prompt, run
    from zettel.retrieval import NoteSearchResult, Retriever
    from zettel.schemas import PermanentNoteCandidate, PermanentNoteLLMOutput

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    permanent = cfg.vault_path / "30_Permanent"
    db = StateDB(tmp_path / "state.db")
    try:
        db.upsert_source("@S", "S2024", "Livro", ["Autor"], 2024, "h", "/x.pdf", "pdf")
        db.upsert_chapter("@S::ch000", "@S", "Cap", "ck")
        db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "texto", "h1", status="persisted")
        for nid in ("OLD", "FAR"):
            path = _write_note(permanent / f"ZTL - {nid} - nota.md")
            db.upsert_note(nid, "@S", str(path), title=f"Nota {nid}", body="corpo")

        cand = PermanentNoteCandidate(
            thesis="Tese declarativa sobre um conceito atomico refinado",
            definition="Definicao autonoma com palavras suficientes para o schema.",
        )
        db.upsert_concept(
            "c1", "@S", "@S::ch000::a", candidate_json=cand.model_dump_json(), status="approved"
        )
        db.set_concept_dedupe("c1", "approved", {"refines_note_id": "OLD", "reason": "nuance"})

        response = PermanentNoteLLMOutput(
            status="ok",
            reason="",
            category="",
            title="Conceito refinado",
            thesis=cand.thesis,
            definition=cand.definition,
            connections=[
                RelationshipResult(
                    related_note_id="FAR", relation_type="related", description="analogia"
                )
            ],
        ).model_dump_json()
        monkeypatch.setattr(run, "get_llm", lambda *a, **k: object())
        monkeypatch.setattr(prompt, "call_llm", lambda *a, **k: response)
        monkeypatch.setattr(context, "load_connect_taxonomy", lambda *a, **k: ({}, {}))
        monkeypatch.setattr(
            Retriever, "search_notes", lambda *a, **k: NoteSearchResult(hits=[], candidates=[])
        )
        far = RetrievedNote(note_id="FAR", score=1.0, vector_distance=0.5)
        monkeypatch.setattr(context, "search_distant_analogies", lambda *a, **k: [far])

        writes: list[str] = []
        upsert_note = StateDB.upsert_note

        def _count(self, *args, **kwargs):
            writes.append(kwargs.get("note_id") or args[0])
            return upsert_note(self, *args, **kwargs)

        monkeypatch.setattr(StateDB, "upsert_note", _count)

        class _Index:
            def upsert_permanent_note(self, *_a, **_k):
                pass

        candidates = run.load_approved_candidates(db)
        assert candidates[0]["refines_note_id"] == "OLD"
        [note_id] = run.run_connect(cfg, db, _Index(), candidates)

        edges = {
            e["target_note_id"]: e["relation_type"]
            for e in db.get_note_connections(note_id)
            if e["source_note_id"] == note_id
        }
        assert edges == {"OLD": "extends"}
        assert writes.count(note_id) == 1
        row = db.get_note(note_id)
        assert "FAR" in read_managed_block(row["body"], "auto-connections")
        on_disk = Path(row["path"]).read_text(encoding="utf-8")
        assert read_managed_block(on_disk, "auto-connections") == read_managed_block(
            row["body"], "auto-connections"
        )
    finally:
        db.close()
