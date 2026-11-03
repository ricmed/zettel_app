"""Tests for manual sync across all vault folders + provenance — Fase 4."""

import pytest
import yaml
from zettel.config import AppConfig
from zettel.state import StateDB
from zettel.sync import (
    _extract_body_edges,
    rebuild_manual_edges,
    repair_permanent_links,
    run_sync_manual,
)

from tests.fakes import FakeVectorIndex, calls_to


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig(vault_path=tmp_path / "vault")
    for d in ("10_Sources", "20_Literature", "30_Permanent", "40_MOCs"):
        (c.vault_path / d).mkdir(parents=True, exist_ok=True)
    return c


def _write(path, meta, body):
    fm = yaml.dump(meta, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{fm}---\n\n{body}", encoding="utf-8")


def _frontmatter(path):
    content = path.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    return yaml.safe_load(parts[1])


def test_manual_source_is_adopted(cfg, db):
    src = cfg.vault_path / "10_Sources" / "SRC - meu-artigo.md"
    _write(src, {"type": "source", "title": "Meu Artigo", "year": 2023}, "# Meu Artigo")
    idx = FakeVectorIndex()
    stats = run_sync_manual(cfg, db, idx)

    assert stats["sources"] == 1
    # source_id + origin:manual injected into the file.
    fm = _frontmatter(src)
    assert fm["source_id"].startswith("@")
    assert fm["origin"] == "manual"
    # Registered in the DB with manual origin.
    stored = db.get_source(fm["source_id"])
    assert stored is not None
    assert stored["origin"] == "manual"
    assert calls_to(idx, "upsert_source") == [fm["source_id"]]


def test_manual_literature_links_and_persists_body(cfg, db):
    lit = cfg.vault_path / "20_Literature" / "LIT - Autor2023 - artigo.md"
    _write(
        lit,
        {
            "type": "literature_index",
            "title": "Artigo",
            "source_id": "@Autor2023",
            "citekey": "Autor2023",
        },
        "Resumo manual do artigo.",
    )
    idx = FakeVectorIndex()
    stats = run_sync_manual(cfg, db, idx)

    assert stats["literature"] == 1
    # An orphan LIT creates a manual source it can attach to.
    src = db.get_source("@Autor2023")
    assert src is not None
    assert src["origin"] == "manual"
    assert "Resumo manual" in (src["lit_body"] or "")


def test_manual_permanent_gets_id_and_origin(cfg, db):
    ztl = cfg.vault_path / "30_Permanent" / "ZTL - nota-manual.md"
    _write(
        ztl,
        {"type": "permanent", "title": "Nota Manual"},
        "> **Tese**: uma tese manual\n\ntexto",
    )
    idx = FakeVectorIndex()
    stats = run_sync_manual(cfg, db, idx)

    assert stats["permanent"] == 1
    fm = _frontmatter(ztl)
    assert "note_id" in fm
    assert fm["origin"] == "manual"
    note = db.get_note(fm["note_id"])
    assert note["origin"] == "manual"
    assert note["body"]  # body persisted
    assert calls_to(idx, "upsert_permanent_note") == [fm["note_id"]]


def test_pipeline_permanent_is_skipped(cfg, db):
    ztl = cfg.vault_path / "30_Permanent" / "ZTL - 01ABC - pipe.md"
    _write(
        ztl,
        {
            "type": "permanent",
            "note_id": "01ABC",
            "title": "Pipe",
            "origin": "pipeline",
        },
        "> **Tese**: gerada pelo pipeline\n\ntexto",
    )
    idx = FakeVectorIndex()
    stats = run_sync_manual(cfg, db, idx)
    assert stats["permanent"] == 0
    assert db.get_note("01ABC") is None
    assert calls_to(idx, "upsert_permanent_note") == []
    assert _frontmatter(ztl)["origin"] == "pipeline"


def test_pipeline_permanent_does_not_adopt_images_or_reembed(cfg, db):
    source_id = "@Pipe2024"
    note_id = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    db.upsert_source(
        source_id,
        "Pipe2024",
        "Paper",
        ["Autor"],
        2024,
        "h",
        "/x.pdf",
        "pdf",
        origin="pipeline",
    )
    png = cfg.vault_path / "30_Permanent" / "figura.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
    )
    ztl = cfg.vault_path / "30_Permanent" / f"ZTL - {note_id} - pipe.md"
    body = "> **Tese**: gerada pelo pipeline\n\n![[figura.png]]\n"
    _write(
        ztl,
        {
            "type": "permanent",
            "note_id": note_id,
            "title": "Pipe",
            "origin": "pipeline",
            "source_id": source_id,
        },
        body,
    )
    db.upsert_note(
        note_id,
        source_id,
        str(ztl),
        "Pipe",
        body=body,
        origin="pipeline",
        note_semantic_checksum="stale",
    )
    idx = FakeVectorIndex()
    stats = run_sync_manual(cfg, db, idx)
    assert stats["permanent"] == 0
    assert calls_to(idx, "upsert_permanent_note") == []
    assert "![[figura.png]]" in ztl.read_text(encoding="utf-8")
    assert db.get_assets_for_source(source_id) == []
    assert db.get_note(note_id)["origin"] == "pipeline"


def test_resync_unchanged_is_skipped(cfg, db):
    ztl = cfg.vault_path / "30_Permanent" / "ZTL - nota.md"
    _write(
        ztl,
        {"type": "permanent", "title": "Nota"},
        "> **Tese**: tese estavel\n\ntexto",
    )
    idx = FakeVectorIndex()
    run_sync_manual(cfg, db, idx)  # first pass: new
    stats2 = run_sync_manual(cfg, db, idx)  # second pass: unchanged
    assert stats2["skipped"] >= 1
    assert stats2["permanent"] == 0


def _seed_pipeline_granular_lit(cfg, db, *, path=None):
    """Approved pipeline LIT: vault says status:approved, chunk row is persisted."""
    source_id = "@Pipe2024"
    chunk_id = f"{source_id}::ch000::abcd"
    lit_id = "lit-pipe-1"
    db.upsert_source(
        source_id,
        "Pipe2024",
        "Paper",
        ["Autor"],
        2024,
        "h",
        "/x.pdf",
        "pdf",
        origin="pipeline",
    )
    db.upsert_chapter(f"{source_id}::ch000", source_id, "Ch1", "chh")
    lit_dir = cfg.vault_path / "20_Literature" / "Pipe2024"
    lit_dir.mkdir(parents=True, exist_ok=True)
    lit = path or (lit_dir / "LIT - Pipe2024 - p001 - topic-0001.md")
    _write(
        lit,
        {
            "type": "literature",
            "origin": "pipeline",
            "source_id": source_id,
            "citekey": "Pipe2024",
            "chunk_id": chunk_id,
            "literature_id": lit_id,
            "status": "approved",
        },
        "Resumo gerado pelo pipeline.",
    )
    db.upsert_chunk(
        chunk_id,
        source_id,
        f"{source_id}::ch000",
        "excerpt",
        "ck",
        status="persisted",
        literature_note_path=str(lit),
        literature_id=lit_id,
    )
    return chunk_id, lit


def test_pipeline_granular_lit_unchanged_is_skipped(cfg, db):
    chunk_id, _ = _seed_pipeline_granular_lit(cfg, db)
    idx = FakeVectorIndex()
    stats = run_sync_manual(cfg, db, idx)
    assert stats["literature"] == 0
    assert stats["skipped"] >= 1
    assert db.get_chunk(chunk_id)["status"] == "persisted"


def test_pipeline_granular_lit_does_not_overwrite_persisted_status(cfg, db):
    chunk_id, lit = _seed_pipeline_granular_lit(cfg, db)
    # Simulate the old bug: path already matches, frontmatter says approved.
    idx = FakeVectorIndex()
    run_sync_manual(cfg, db, idx)
    assert db.get_chunk(chunk_id)["status"] == "persisted"
    assert db.get_chunk(chunk_id)["literature_note_path"] == str(lit)


def test_pipeline_granular_lit_moved_updates_path(cfg, db):
    chunk_id, old = _seed_pipeline_granular_lit(cfg, db)
    new = old.with_name("LIT - Pipe2024 - p001 - topic-renomeado.md")
    old.rename(new)
    db.update_chunk_review(chunk_id, literature_note_path=str(old))
    idx = FakeVectorIndex()
    stats = run_sync_manual(cfg, db, idx)
    assert stats["literature"] == 1
    row = db.get_chunk(chunk_id)
    assert row["literature_note_path"] == str(new)
    assert row["status"] == "persisted"


# ── Graph edges from manual wikilinks (Etapa 6) ────────────────────────

# Valid ULID-shaped ids (Crockford base32, 26 chars).
_A = "01HAAAAAAAAAAAAAAAAAAAAAAA"
_B = "01HBBBBBBBBBBBBBBBBBBBBBBB"
_C = "01HCCCCCCCCCCCCCCCCCCCCCCC"


def _seed(db, *note_ids):
    for nid in note_ids:
        db.upsert_note(nid, "@S", f"/p/{nid}.md", f"Nota {nid}", body="corpo")


def test_body_wikilink_creates_related_edge(db):
    _seed(db, _A, _B)
    body = f"> **Tese**: x\n\n## Conexoes\n\n- [[ZTL - {_B} - nota-b]]: relacionada"
    created = _extract_body_edges(db, _A, body)
    assert created == 1
    edges = db.get_note_connections(_A)
    assert len(edges) == 1
    assert edges[0]["relation_type"] == "related"
    assert edges[0]["origin"] == "manual"
    assert {edges[0]["source_note_id"], edges[0]["target_note_id"]} == {_A, _B}


def test_wikilink_in_managed_block_is_ignored(db):
    _seed(db, _A, _B, _C)
    body = (
        f"> **Tese**: x\n\n## Conexoes\n\n- [[ZTL - {_B} - nota-b]]\n\n"
        f"<!-- zettel:auto-connections:start -->\n"
        f"- [[ZTL - {_C} - nota-c]]\n"
        f"<!-- zettel:auto-connections:end -->\n"
    )
    _extract_body_edges(db, _A, body)
    edges = db.get_note_connections(_A)
    targets = {e["target_note_id"] for e in edges} | {e["source_note_id"] for e in edges}
    assert _B in targets  # body link accepted
    assert _C not in targets  # suggestion block link ignored


def test_distant_suggestion_is_not_an_edge_until_moved_out_of_block(db):
    """Issue #161: analogia no bloco gerenciado nao vira aresta."""
    _seed(db, _A, _C)
    body = (
        f"> **Tese**: ponte\n\n"
        f"<!-- zettel:auto-connections:start -->\n"
        f"- [[ZTL - {_C} - nota-c]] (exemplifies) -- analogia distante\n"
        f"<!-- zettel:auto-connections:end -->\n"
    )
    assert _extract_body_edges(db, _A, body) == 0
    assert db.get_note_connections(_A) == []
    body_endorsed = body + f"\n\n## Conexoes\n\n- [[ZTL - {_C} - nota-c]]\n"
    assert _extract_body_edges(db, _A, body_endorsed) == 1
    edges = db.get_note_connections(_A)
    assert edges[0]["origin"] == "manual"
    assert edges[0]["relation_type"] == "related"


def test_self_link_ignored(db):
    _seed(db, _A)
    body = f"- [[ZTL - {_A} - eu-mesmo]]"
    assert _extract_body_edges(db, _A, body) == 0
    assert db.get_note_connections(_A) == []


def test_link_to_unknown_note_ignored(db):
    _seed(db, _A)  # _B not seeded
    body = f"- [[ZTL - {_B} - fantasma]]"
    assert _extract_body_edges(db, _A, body) == 0


def test_existing_typed_edge_not_downgraded(db):
    _seed(db, _A, _B)
    db.upsert_note_connection(_A, _B, "contradicts", "tensiona")
    body = f"- [[ZTL - {_B} - nota-b]]"
    created = _extract_body_edges(db, _A, body)
    assert created == 0
    edges = db.get_note_connections(_A)
    assert len(edges) == 1
    assert edges[0]["relation_type"] == "contradicts"  # preserved


def test_existing_reverse_edge_not_duplicated(db):
    _seed(db, _A, _B)
    db.upsert_note_connection(_B, _A, "extends", "")  # reverse direction
    body = f"- [[ZTL - {_B} - nota-b]]"
    assert _extract_body_edges(db, _A, body) == 0


def test_rebuild_manual_edges_backfills(db):
    _seed(db, _A, _C)
    # Overwrite _A's body to contain a link to _C.
    db.upsert_note(
        _A,
        "@S",
        f"/p/{_A}.md",
        "Nota A",
        body=f"## Conexoes\n\n- [[ZTL - {_C} - nota-c]]",
    )
    stats = rebuild_manual_edges(db)
    assert stats["edges_created"] == 1
    assert db.count_note_connections() == 1


def test_edited_moc_returns_updated(cfg, db):
    moc = cfg.vault_path / "40_MOCs" / "MOC - topico.md"
    _write(
        moc,
        {"type": "moc", "topic": "Topico"},
        "# Topico\n\nResumo inicial.\n\n## Sub\n\n- a",
    )
    idx = FakeVectorIndex()
    run_sync_manual(cfg, db, idx)  # new (moc_id injected)

    moc_id = _frontmatter(moc)["moc_id"]
    # Edit the body → semantic checksum changes → must be 'updated', not 'new'.
    _write(
        moc,
        {"type": "moc", "topic": "Topico", "moc_id": moc_id},
        "# Topico\n\nResumo bem diferente agora.\n\n## Sub\n\n- a\n- b",
    )
    stats = run_sync_manual(cfg, db, idx)
    assert stats["updated"] >= 1


def test_repair_permanent_links_rewrites_double_prefix_and_rebuilds_backlinks(cfg, db):
    _A = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    _B = "01HBBBBBBBBBBBBBBBBBBBBBBB"
    path_a = cfg.vault_path / "30_Permanent" / f"ZTL - {_A} - analise-de-series-temporais.md"
    path_b = cfg.vault_path / "30_Permanent" / f"ZTL - {_B} - sazonalidade.md"
    _write(
        path_a,
        {"type": "permanent", "note_id": _A, "title": "Analise"},
        f"## Conexoes\n\n- [[ZTL - ZTL - {_B}]] (extends) -- contexto\n",
    )
    _write(
        path_b,
        {"type": "permanent", "note_id": _B, "title": "Sazonalidade"},
        (
            "## Conexoes\n\n"
            "<!-- zettel:auto-backlinks:start -->\n"
            "- [[ZTL - GHOST - fantasma]] (relacionado) -- morto\n"
            "<!-- zettel:auto-backlinks:end -->\n"
        ),
    )
    db.upsert_note(_A, "@S", str(path_a), "Analise", body="x")
    db.upsert_note(_B, "@S", str(path_b), "Sazonalidade", body="x")
    db.upsert_note_connection(_A, _B, "extends", "contexto")

    stats = repair_permanent_links(db)
    assert stats["wikilinks_rewritten"] >= 1

    body_a = path_a.read_text(encoding="utf-8")
    assert f"[[ZTL - {_B} - sazonalidade]]" in body_a
    assert f"ZTL - ZTL - {_B}" not in body_a

    from zettel.vault import read_managed_block

    block = read_managed_block(path_b.read_text(encoding="utf-8"), "auto-backlinks")
    assert block is not None
    assert "GHOST" not in block
    assert f"ZTL - {_A} - analise-de-series-temporais" in block
    assert "estendido por" in block


# -- one bad note never stops the run (#342) ---------------------------------


def _failures(stats):
    return {f["path"].rsplit("/", 1)[-1]: f["reason"] for f in stats["failures"]}


def test_invalid_yaml_is_reported_and_the_file_left_untouched(cfg, db):
    broken = cfg.vault_path / "30_Permanent" / "ZTL - quebrada.md"
    original = "---\ntitle: Ideia\ntags: [a, b\n---\n\n> **Tese**: algo.\n"
    broken.write_text(original, encoding="utf-8")
    good = cfg.vault_path / "30_Permanent" / "ZTL - boa.md"
    _write(good, {"title": "Boa"}, "> **Tese**: outra coisa.")

    stats = run_sync_manual(cfg, db, FakeVectorIndex())

    assert broken.read_text(encoding="utf-8") == original
    assert "YAML invalido" in _failures(stats)["ZTL - quebrada.md"]
    assert stats["failed"] == 1
    assert stats["permanent"] == 1
    assert db.get_last_run()["status"] == "completed"


def test_unclosed_or_non_mapping_frontmatter_is_reported(cfg, db):
    unclosed = cfg.vault_path / "30_Permanent" / "ZTL - aberta.md"
    unclosed.write_text("---\ntitle: x\n\nCorpo sem fechar.\n", encoding="utf-8")
    listing = cfg.vault_path / "40_MOCs" / "MOC - lista.md"
    listing.write_text("---\n- a\n- b\n---\n\nCorpo.\n", encoding="utf-8")

    stats = run_sync_manual(cfg, db, FakeVectorIndex())

    reasons = _failures(stats)
    assert "nunca fechado" in reasons["ZTL - aberta.md"]
    assert "chave: valor" in reasons["MOC - lista.md"]


def test_unquoted_obsidian_dates_do_not_break_the_sync(cfg, db):
    """Obsidian's Date property writes `created: 2026-11-03`, which YAML reads as a date."""
    ztl = cfg.vault_path / "30_Permanent" / "ZTL - datada.md"
    ztl.write_text(
        "---\ntitle: Datada\ncreated: 2026-11-03\n---\n\n> **Tese**: uma ideia.\n",
        encoding="utf-8",
    )
    moc = cfg.vault_path / "40_MOCs" / "MOC - datado.md"
    moc.write_text("---\ntopic: Tema\ncreated: 2026-11-03\n---\n\n# Tema\n", encoding="utf-8")
    src = cfg.vault_path / "10_Sources" / "SRC - site.md"
    src.write_text(
        "---\ntitle: Site\nauthor: [Ana Souza]\nyear: 2024\n"
        "accessed_at: 2026-11-03\n---\n\n# Site\n",
        encoding="utf-8",
    )

    stats = run_sync_manual(cfg, db, FakeVectorIndex())

    assert stats["failures"] == []
    (note,) = db.list_notes()
    assert yaml.safe_load(note["frontmatter_json"])["created"] == "2026-11-03"
    (source,) = db.list_sources()
    assert '"accessed_at": "2026-11-03"' in source["bibliography_json"]


def test_a_single_author_string_is_one_author_for_the_citekey(cfg, db):
    src = cfg.vault_path / "10_Sources" / "SRC - rapido.md"
    _write(src, {"title": "Rapido e devagar", "author": "Daniel Kahneman", "year": 2011}, "#")

    run_sync_manual(cfg, db, FakeVectorIndex())

    assert _frontmatter(src)["source_id"].startswith("@Kahneman2011")


def test_unexpected_error_in_one_note_is_reported_and_the_rest_synced(cfg, db, monkeypatch):
    from zettel import sync

    real = sync._sync_moc

    def explode(cfg, db, idx, file_path, meta, body):
        if file_path.name.startswith("MOC - ruim"):
            raise RuntimeError("boom")
        return real(cfg, db, idx, file_path, meta, body)

    monkeypatch.setattr(sync, "_sync_moc", explode)
    _write(cfg.vault_path / "40_MOCs" / "MOC - ruim.md", {"topic": "Ruim"}, "# Ruim")
    _write(cfg.vault_path / "40_MOCs" / "MOC - bom.md", {"topic": "Bom"}, "# Bom")

    stats = run_sync_manual(cfg, db, FakeVectorIndex())

    assert _failures(stats) == {"MOC - ruim.md": "RuntimeError: boom"}
    assert stats["mocs"] == 1


def test_an_embedding_error_that_is_not_an_outage_fails_only_that_note(cfg, db):
    _write(cfg.vault_path / "30_Permanent" / "ZTL - a.md", {"title": "A"}, "> **Tese**: a.")
    _write(cfg.vault_path / "30_Permanent" / "ZTL - b.md", {"title": "B"}, "> **Tese**: b.")
    idx = FakeVectorIndex(fail_on={"upsert_permanent_note": 1})

    stats = run_sync_manual(cfg, db, idx)

    assert "falha injetada" in _failures(stats)["ZTL - a.md"]
    assert stats["permanent"] == 1
    assert stats["embedding_pending"] == 0


# -- sync without embedding (#343) -------------------------------------------


def _offline_vault(cfg):
    _write(
        cfg.vault_path / "10_Sources" / "SRC - obra.md",
        {"title": "Obra", "author": ["Ana Souza"], "year": 2020, "source_id": "@Souza2020Obra"},
        "# Obra",
    )
    b = cfg.vault_path / "30_Permanent" / "ZTL - 01J0000000000000000000000B - b.md"
    _write(b, {"title": "B", "note_id": "01J0000000000000000000000B"}, "> **Tese**: b.")
    a = cfg.vault_path / "30_Permanent" / "ZTL - 01J0000000000000000000000A - a.md"
    _write(
        a,
        {"title": "A", "note_id": "01J0000000000000000000000A"},
        "> **Tese**: a.\n\nVer [[ZTL - 01J0000000000000000000000B - b]].",
    )


def test_without_an_index_notes_reach_sqlite_and_the_graph_with_vectors_pending(cfg, db):
    _offline_vault(cfg)

    stats = run_sync_manual(cfg, db, None)

    assert stats["failures"] == []
    assert stats["embedding_pending"] == 3
    assert stats["embedding_offline"]
    assert db.get_source("@Souza2020Obra") is not None
    assert {n["note_id"] for n in db.list_notes()} == {
        "01J0000000000000000000000A",
        "01J0000000000000000000000B",
    }
    assert db.get_note("01J0000000000000000000000A")["embedding_input_hash"] is None
    assert db.get_last_run()["status"] == "completed"


def test_the_next_sync_with_an_index_writes_the_pending_vectors_without_edits(cfg, db):
    _offline_vault(cfg)
    run_sync_manual(cfg, db, None)
    idx = FakeVectorIndex()

    stats = run_sync_manual(cfg, db, idx)

    assert stats["embedding_pending"] == 0
    assert stats["updated"] == 3
    assert calls_to(idx, "upsert_source") == ["@Souza2020Obra"]
    assert sorted(calls_to(idx, "upsert_permanent_note")) == [
        "01J0000000000000000000000A",
        "01J0000000000000000000000B",
    ]
    assert db.get_note("01J0000000000000000000000A")["embedding_input_hash"]

    again = run_sync_manual(cfg, db, idx)
    assert again["updated"] == 0 and again["embedding_pending"] == 0
    assert len(calls_to(idx, "upsert_permanent_note")) == 2


def test_an_outage_mid_run_stops_embedding_but_not_the_sync(cfg, db):
    """The note that failed used to stay without a vector for good (#343)."""
    from zettel.llm import LLMUnavailableError

    _offline_vault(cfg)
    down = FakeVectorIndex(fail_on={"upsert_source": (1, LLMUnavailableError("down"))})

    stats = run_sync_manual(cfg, db, down)

    assert stats["failures"] == []
    assert stats["embedding_pending"] == 3
    assert "LLMUnavailableError" in stats["embedding_offline"]
    assert calls_to(down, "upsert_permanent_note") == []  # no retry per note once offline

    up = FakeVectorIndex()
    assert run_sync_manual(cfg, db, up)["embedding_pending"] == 0
    assert len(calls_to(up, "upsert_permanent_note")) == 2


def test_without_a_credential_the_index_is_never_opened(cfg, db, monkeypatch):
    monkeypatch.setattr("zettel.credentials.embedding_ready", lambda _cfg: False)
    _offline_vault(cfg)

    def must_not_open():
        raise AssertionError("o indice nao devia ser aberto")

    stats = run_sync_manual(cfg, db, must_not_open)

    assert stats["embedding_offline"] == "sem credencial de embedding"
    assert stats["embedding_pending"] == 3


def test_the_index_opens_only_when_something_needs_a_vector(cfg, db):
    _write(cfg.vault_path / "40_MOCs" / "MOC - tema.md", {"topic": "Tema"}, "# Tema")
    opened = []

    stats = run_sync_manual(cfg, db, lambda: opened.append(1) or FakeVectorIndex())

    assert stats["mocs"] == 1
    assert opened == []


def test_notes_pending_embedding_counts_notes_without_a_current_vector(cfg, db):
    def pending():
        return db.count_notes_pending_embedding(cfg.embedding.provider, cfg.embedding.model)

    _offline_vault(cfg)
    run_sync_manual(cfg, db, None)
    assert pending() == 2

    run_sync_manual(cfg, db, FakeVectorIndex())
    assert pending() == 0


def _granular(path, chunk_id, origin="manual"):
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "literature", "source_id": "@Autor2020", "chunk_id": chunk_id}
    if origin is not None:
        meta["origin"] = origin
    _write(
        path,
        meta,
        "# Topico\n\n## Resumo\n\nUm resumo.\n\n"
        "<!-- zettel:auto-source-excerpt:start -->\nTrecho.\n"
        "<!-- zettel:auto-source-excerpt:end -->\n",
    )


def test_literature_in_a_source_folder_without_chunk_id_is_refused(cfg, db):
    note = cfg.vault_path / "20_Literature" / "Autor2020" / "LIT - Autor2020 - sem-id.md"
    note.parent.mkdir(parents=True)
    _write(note, {"type": "literature", "source_id": "@Autor2020"}, "# Sem id")

    stats = run_sync_manual(cfg, db, FakeVectorIndex())

    assert "chunk_id" in _failures(stats)["LIT - Autor2020 - sem-id.md"]
    assert db.get_source("@Autor2020") is None


def test_granular_literature_with_unknown_origin_is_refused(cfg, db):
    note = cfg.vault_path / "20_Literature" / "Autor2020" / "LIT - Autor2020 - c0001.md"
    _granular(note, "@Autor2020::manual::0001", origin="obsidian")

    stats = run_sync_manual(cfg, db, FakeVectorIndex())

    assert "'obsidian'" in _failures(stats)["LIT - Autor2020 - c0001.md"]
    assert db.get_chunk("@Autor2020::manual::0001") is None


def test_granular_literature_without_origin_is_adopted_as_manual(cfg, db):
    note = cfg.vault_path / "20_Literature" / "Autor2020" / "LIT - Autor2020 - c0001.md"
    _granular(note, "@Autor2020::manual::0001", origin=None)

    stats = run_sync_manual(cfg, db, FakeVectorIndex())

    assert stats["failures"] == []
    assert db.get_chunk("@Autor2020::manual::0001")["text"] == "Trecho."


def test_two_literature_notes_with_one_chunk_id_report_the_second(cfg, db):
    folder = cfg.vault_path / "20_Literature" / "Autor2020"
    _granular(folder / "LIT - a.md", "@Autor2020::manual::0001")
    _granular(folder / "LIT - b.md", "@Autor2020::manual::0001")

    stats = run_sync_manual(cfg, db, FakeVectorIndex())

    assert "LIT - a.md" in _failures(stats)["LIT - b.md"]
    assert db.get_chunk("@Autor2020::manual::0001")["literature_note_path"].endswith("LIT - a.md")


def test_scaffold_placeholders_are_not_embedded(cfg, db):
    from zettel.vault import scaffold_permanent_note_body

    note = cfg.vault_path / "30_Permanent" / "ZTL - rascunho.md"
    _write(note, {"title": "Rascunho"}, scaffold_permanent_note_body(thesis="Uma tese real."))
    idx = FakeVectorIndex()

    run_sync_manual(cfg, db, idx)

    ((text, _meta),) = idx.store["permanent_notes"].values()
    assert "Uma tese real." in text
    assert "preencha" not in text.lower()


# -- an SRC edited in Obsidian reaches SQLite (#346) ---------------------------


def _src(cfg, meta, name="SRC - Souza2020 - obra.md"):
    path = cfg.vault_path / "10_Sources" / name
    base = {"type": "source", "source_id": "@Souza2020Obra", "title": "Obra", "origin": "manual"}
    _write(path, {**base, **meta}, "# Obra")
    return path


def test_editing_year_and_doi_of_an_src_updates_the_source(cfg, db):
    import json

    idx = FakeVectorIndex()
    _src(cfg, {"author": ["Ana Souza"], "year": 2020})
    run_sync_manual(cfg, db, idx)

    _src(cfg, {"author": ["Ana Souza"], "year": 2021, "doi": "https://doi.org/10.1000/XYZ.1."})
    stats = run_sync_manual(cfg, db, idx)

    assert stats["sources"] == 1
    row = db.get_source("@Souza2020Obra")
    assert row["year"] == 2021
    assert row["doi"] == "10.1000/xyz.1"
    biblio = json.loads(row["bibliography_json"])
    assert biblio["year"] == 2021
    assert biblio["provenance"] == {"year": "user", "doi": "user"}
    assert calls_to(idx, "upsert_source") == ["@Souza2020Obra"]  # title and authors unchanged
    assert run_sync_manual(cfg, db, idx)["sources"] == 0


def test_a_minimal_source_made_by_a_lit_gets_the_src_bibliography(cfg, db):
    lit = cfg.vault_path / "20_Literature" / "Souza2020Obra" / "LIT - c0001.md"
    lit.parent.mkdir(parents=True)
    _write(
        lit,
        {
            "type": "literature",
            "source_id": "@Souza2020Obra",
            "chunk_id": "@Souza2020Obra::manual::0001",
        },
        "# Topico",
    )
    run_sync_manual(cfg, db, FakeVectorIndex())
    assert db.get_source("@Souza2020Obra")["authors"] == "[]"

    _src(cfg, {"author": ["Ana Souza", "Rui Lima"], "year": 2020, "publisher": "Editora X"})
    idx = FakeVectorIndex()
    run_sync_manual(cfg, db, idx)

    row = db.get_source("@Souza2020Obra")
    assert row["authors"] == '["Ana Souza", "Rui Lima"]'
    assert row["title"] == "Obra"
    assert calls_to(idx, "upsert_source") == ["@Souza2020Obra"]


def test_pipeline_columns_stay_when_a_pipeline_src_is_edited(cfg, db):
    import json

    db.upsert_source(
        source_id="@Souza2020Obra",
        citekey="Souza2020Obra",
        title="Obra",
        authors=["Ana Souza"],
        year=2020,
        file_checksum="arquivo",
        origin_path="/inbox/obra.pdf",
        origin_type="pdf",
        extraction_checksum="extracao",
        bibliography_json=json.dumps(
            {"title": "Obra", "year": 2020, "provenance": {"title": "llm", "year": "llm"}}
        ),
    )
    _src(cfg, {"author": ["Ana Souza"], "year": 2020, "origin": "pipeline", "place": "Recife"})

    run_sync_manual(cfg, db, FakeVectorIndex())

    row = db.get_source("@Souza2020Obra")
    assert (row["file_checksum"], row["extraction_checksum"], row["origin"]) == (
        "arquivo",
        "extracao",
        "pipeline",
    )
    biblio = json.loads(row["bibliography_json"])
    assert biblio["place"] == "Recife"
    assert biblio["provenance"]["place"] == "user"
    assert biblio["provenance"]["title"] == "llm"


def test_a_new_year_recomputes_the_citation_of_the_notes_of_the_source(cfg, db):
    import json

    from zettel.vault import compose_note, read_managed_block, upsert_managed_block

    _src(cfg, {"author": ["Ana Souza"], "year": 2020})
    run_sync_manual(cfg, db, FakeVectorIndex())
    db.upsert_chapter("@Souza2020Obra::ch001", "@Souza2020Obra", "Cap", "c")
    chunk_id = "@Souza2020Obra::ch001::abc"
    db.upsert_chunk(
        chunk_id, "@Souza2020Obra", "@Souza2020Obra::ch001", "texto", "k", page_in_book=5
    )
    meta = {"type": "permanent", "note_id": "NOTA1", "chunk_id": chunk_id, "origin": "pipeline"}
    body = upsert_managed_block(
        "> **Tese**: x.\n\n## Fonte\n", "auto-evidence", "- Citação: (SOUZA, 2020, p. 5)"
    )
    note = cfg.vault_path / "30_Permanent" / "ZTL - NOTA1 - x.md"
    note.write_text(compose_note(meta, body), encoding="utf-8")
    db.upsert_note(
        "NOTA1",
        "@Souza2020Obra",
        str(note),
        title="x",
        body=body,
        frontmatter_json=json.dumps(meta),
        provenance_json=json.dumps({"citation": "(SOUZA, 2020, p. 5)", "anchor_quote": ""}),
    )

    _src(cfg, {"author": ["Ana Souza"], "year": 2021})
    run_sync_manual(cfg, db, FakeVectorIndex())

    row = db.get_note("NOTA1")
    assert json.loads(row["provenance_json"])["citation"] == "(SOUZA, 2021, p. 5)"
    assert "(SOUZA, 2021, p. 5)" in read_managed_block(
        note.read_text(encoding="utf-8"), "auto-evidence"
    )
    assert "(SOUZA, 2021, p. 5)" in row["body"]


def test_an_src_edited_offline_is_updated_and_its_vector_left_pending(cfg, db):
    _src(cfg, {"author": ["Ana Souza"], "year": 2020})
    run_sync_manual(cfg, db, FakeVectorIndex())

    _src(cfg, {"title": "Obra revista", "author": ["Ana Souza"], "year": 2020})
    stats = run_sync_manual(cfg, db, None)

    assert db.get_source("@Souza2020Obra")["title"] == "Obra revista"
    assert stats["embedding_pending"] == 1
