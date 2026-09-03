"""End-to-end tests for the manual note flow: SRC -> LIT (+images) -> ZTL."""

import json

import pytest

from zettel.config import AppConfig
from zettel.manual_lit import (
    _candidate_theses,
    build_candidate_from_literature,
    create_permanent_from_literature,
)
from zettel.new_note import scaffold_manual_note
from zettel.state import StateDB
from zettel.sync import run_sync_manual
from zettel.vault import parse_frontmatter, read_managed_block, safe_write_note

# 1x1 transparent PNG.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


class FakeIndex:
    def __init__(self):
        self.sources: list[str] = []
        self.permanent: list[str] = []
        self.mocs: list[str] = []
        self.literature: list[tuple[str, str, dict]] = []

    def upsert_source(self, sid, summary, meta):
        self.sources.append(sid)

    def upsert_permanent_note(self, nid, text, meta):
        self.permanent.append(nid)

    def upsert_moc(self, mid, text, meta):
        self.mocs.append(mid)

    def upsert_literature_note(self, lid, text, meta):
        self.literature.append((lid, text, meta))

    def query_similar_notes(self, text, n_results=5, exclude_id=None):
        return []


@pytest.fixture
def cfg(tmp_path):
    vault = tmp_path / "vault"
    for d in ("10_Sources", "20_Literature", "30_Permanent", "40_MOCs", "90_Assets"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return AppConfig(vault_path=vault)


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


def _fill_literature(path, *, summary="Grafos conexos resistem a remocoes.",
                     excerpt="Um grafo e conexo quando ha caminho entre dois vertices.",
                     thesis="Conectividade determina robustez estrutural.",
                     extra_body=""):
    """Replace the scaffold placeholders with real content, as the user would."""
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    body = body.replace("_Preencha o resumo._", summary)
    body = body.replace("_Cole o trecho da fonte aqui._", excerpt)
    body = body.replace("_Nenhum._", "#grafos #conectividade")
    body = body.replace("_Nenhum candidato._", f"- [ ] {thesis}")
    safe_write_note(path, meta, body + extra_body)
    return path


def _scaffold_source_and_lit(cfg, db, idx, *, extra_body=""):
    scaffold_manual_note(
        cfg, "src", "Teoria dos Grafos",
        citekey="Diestel2017", authors=["Reinhard Diestel"], year=2017,
    )
    lit = scaffold_manual_note(
        cfg, "lit", "Conectividade",
        source_id="@Diestel2017", granular=True, page=42,
    ).path
    _fill_literature(lit, extra_body=extra_body)
    run_sync_manual(cfg, db, idx)
    return lit


# -- SRC ----------------------------------------------------------------


def test_source_scaffold_also_creates_literature_index(cfg):
    result = scaffold_manual_note(
        cfg, "src", "Teoria dos Grafos", citekey="Diestel2017", year=2017,
    )
    index = cfg.vault_path / "20_Literature" / "LIT - Diestel2017 - teoria-dos-grafos.md"
    assert index.is_file()
    # The SRC's index wikilink resolves to the file that was just written.
    assert index.stem in result.path.read_text(encoding="utf-8")


# -- LIT ----------------------------------------------------------------


def test_literature_scaffold_honours_source_id(cfg):
    scaffold_manual_note(cfg, "src", "Teoria dos Grafos", citekey="Diestel2017")
    result = scaffold_manual_note(
        cfg, "lit", "Conectividade", source_id="@Diestel2017", granular=True, page=42,
    )
    assert result.meta["source_id"] == "@Diestel2017"
    assert result.meta["citekey"] == "Diestel2017"
    assert result.path.parent.name == "Diestel2017"
    # The backlink targets the source's index, not the chunk topic.
    assert "[[LIT - Diestel2017 - teoria-dos-grafos]]" in result.path.read_text(
        encoding="utf-8"
    )


def test_manual_granular_literature_is_adopted(cfg, db):
    idx = FakeIndex()
    lit = _scaffold_source_and_lit(cfg, db, idx)

    meta, _ = parse_frontmatter(lit.read_text(encoding="utf-8"))
    chunk = db.get_chunk(meta["chunk_id"])
    assert chunk is not None, "a nota manual precisa virar uma linha em chunks"
    assert chunk["status"] == "persisted"
    assert chunk["page_in_book"] == 42
    assert chunk["literature_note_path"] == str(lit)
    assert "caminho entre dois vertices" in chunk["text"]

    # Chapter FK satisfied by the synthetic manual chapter.
    assert db.get_chapters_for_source("@Diestel2017")[0]["title"] == "Manual"

    # Embedded into literature_notes with the same metadata shape as approve_chunk.
    assert len(idx.literature) == 1
    lit_id, text, lmeta = idx.literature[0]
    assert lit_id == meta["literature_id"]
    assert lmeta["citekey"] == "Diestel2017"
    assert lmeta["page_in_book"] == 42
    # The excerpt lives in a managed block and must not be embedded.
    assert "Grafos conexos resistem" in text
    assert "caminho entre dois vertices" not in text

    # summary_json rebuilt from the body sections.
    payload = json.loads(chunk["summary_json"])
    assert payload["key_concepts"] == ["grafos", "conectividade"]
    assert payload["candidates"][0]["thesis"].startswith("Conectividade determina")


def test_adopted_literature_appears_in_source_index(cfg, db):
    idx = FakeIndex()
    _scaffold_source_and_lit(cfg, db, idx)

    index = cfg.vault_path / "20_Literature" / "LIT - Diestel2017 - teoria-dos-grafos.md"
    block = read_managed_block(index.read_text(encoding="utf-8"), "auto-lit-index")
    assert "p. 42" in block
    # The link must point at the file that is actually on disk.
    target = block.split("[[", 1)[1].split("|", 1)[0]
    assert (cfg.vault_path / "20_Literature" / f"{target}.md").is_file()


def test_adoption_is_idempotent(cfg, db):
    idx = FakeIndex()
    _scaffold_source_and_lit(cfg, db, idx)
    assert len(idx.literature) == 1

    stats = run_sync_manual(cfg, db, idx)
    assert stats["literature"] == 0
    assert len(idx.literature) == 1, "nota inalterada nao deve ser re-embedada"


def test_edited_literature_is_re_adopted(cfg, db):
    idx = FakeIndex()
    lit = _scaffold_source_and_lit(cfg, db, idx)

    meta, body = parse_frontmatter(lit.read_text(encoding="utf-8"))
    safe_write_note(lit, meta, body.replace("resistem a remocoes", "toleram falhas"))
    stats = run_sync_manual(cfg, db, idx)

    assert stats["literature"] == 1
    assert len(idx.literature) == 2


# -- Images -------------------------------------------------------------


def test_wiki_embed_image_is_adopted(cfg, db):
    (cfg.vault_path / "anexos").mkdir()
    (cfg.vault_path / "anexos" / "diagrama.png").write_bytes(PNG_BYTES)
    idx = FakeIndex()
    lit = _scaffold_source_and_lit(
        cfg, db, idx, extra_body="\n## Figura\n\n![[diagrama.png]]\n",
    )

    assets = db.get_assets_for_source("@Diestel2017")
    assert len(assets) == 1
    asset = assets[0]
    assert asset["status"] == "pending"
    assert asset["path"].startswith("90_Assets/img-")
    assert (cfg.vault_path / asset["path"]).is_file()
    # Reference rewritten to the canonical path, embed syntax preserved.
    content = lit.read_text(encoding="utf-8")
    assert f"![[{asset['path']}]]" in content
    # The original file is left where the user put it.
    assert (cfg.vault_path / "anexos" / "diagrama.png").is_file()


def test_markdown_image_is_adopted_keeping_syntax(cfg, db):
    (cfg.vault_path / "figura.png").write_bytes(PNG_BYTES)
    idx = FakeIndex()
    lit = _scaffold_source_and_lit(
        cfg, db, idx, extra_body="\n## Figura\n\n![Diagrama](figura.png)\n",
    )

    asset = db.get_assets_for_source("@Diestel2017")[0]
    assert f"![Diagrama]({asset['path']})" in lit.read_text(encoding="utf-8")


def test_remote_and_non_image_refs_are_untouched(cfg, db):
    idx = FakeIndex()
    extra = "\n![remoto](https://exemplo.com/a.png)\n\n![[Outra Nota]]\n"
    lit = _scaffold_source_and_lit(cfg, db, idx, extra_body=extra)

    assert db.get_assets_for_source("@Diestel2017") == []
    content = lit.read_text(encoding="utf-8")
    assert "https://exemplo.com/a.png" in content
    assert "![[Outra Nota]]" in content


def test_image_adoption_does_not_duplicate_on_resync(cfg, db):
    (cfg.vault_path / "figura.png").write_bytes(PNG_BYTES)
    idx = FakeIndex()
    lit = _scaffold_source_and_lit(
        cfg, db, idx, extra_body="\n![[figura.png]]\n",
    )
    before = lit.read_text(encoding="utf-8")

    run_sync_manual(cfg, db, idx)
    assert len(db.get_assets_for_source("@Diestel2017")) == 1
    assert lit.read_text(encoding="utf-8") == before


# -- ZTL from LIT -------------------------------------------------------


def test_candidate_derived_from_literature_body(cfg, db):
    idx = FakeIndex()
    lit = _scaffold_source_and_lit(cfg, db, idx)
    content = lit.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(content)

    cand = build_candidate_from_literature(meta, body, content)
    assert cand.thesis == "Conectividade determina robustez estrutural."
    assert cand.definition.startswith("Grafos conexos")
    assert cand.anchor_quote.startswith("Um grafo e conexo")
    assert cand.source_locator == "p.42 / Conectividade"
    assert cand.tags == ["grafos", "conectividade"]


def test_permanent_from_literature_without_llm(cfg, db):
    idx = FakeIndex()
    lit = _scaffold_source_and_lit(cfg, db, idx)

    path, via_llm = create_permanent_from_literature(cfg, db, idx, str(lit))
    assert via_llm is False
    assert path.parent.name == "30_Permanent"

    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert meta["origin"] == "manual"
    assert meta["source_id"] == "@Diestel2017"
    assert meta["source_locator"] == "p.42 / Conectividade"
    # literature_ref points at the granular LIT, not at the source index.
    assert meta["literature_ref"] == f"[[Diestel2017/{lit.stem}]]"
    assert "Conectividade determina robustez" in body
    # No approval gate and no concept row on the hand-written path.
    assert db.get_concepts_by_status("approved") == []


def test_permanent_from_literature_accepts_chunk_id(cfg, db):
    idx = FakeIndex()
    lit = _scaffold_source_and_lit(cfg, db, idx)
    chunk_id = parse_frontmatter(lit.read_text(encoding="utf-8"))[0]["chunk_id"]

    path, _ = create_permanent_from_literature(cfg, db, idx, chunk_id)
    assert path.is_file()


def test_permanent_from_literature_rejects_non_literature(cfg, db):
    idx = FakeIndex()
    _scaffold_source_and_lit(cfg, db, idx)
    src = next((cfg.vault_path / "10_Sources").glob("*.md"))

    with pytest.raises(ValueError, match="nota de literatura granular"):
        create_permanent_from_literature(cfg, db, idx, str(src))


def test_permanent_from_literature_with_llm(cfg, db, monkeypatch):
    """The LLM path reuses run_connect, so the note is fully indexed as manual."""
    import zettel.connector as connector
    from zettel.schemas import PermanentNoteLLMOutput

    idx = FakeIndex()
    lit = _scaffold_source_and_lit(cfg, db, idx)

    response = PermanentNoteLLMOutput(
        status="ok",
        reason="",
        category="",
        title="Conectividade e robustez",
        thesis="Conectividade determina robustez estrutural.",
        definition="Um grafo conexo mantem caminhos apos remocoes limitadas.",
        intuition="",
        example="",
        limits="",
        connections=[],
        tags=["grafos"],
    ).model_dump_json()
    monkeypatch.setattr(connector, "get_llm", lambda cfg, phase: object())
    monkeypatch.setattr(connector, "call_llm", lambda *a, **k: response)

    path, via_llm = create_permanent_from_literature(
        cfg, db, idx, str(lit), use_llm=True,
    )
    assert via_llm is True

    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert meta["origin"] == "manual"
    assert meta["literature_ref"] == f"[[Diestel2017/{lit.stem}]]"

    row = db.get_note(meta["note_id"])
    assert row is not None and row["origin"] == "manual"
    assert idx.permanent == [meta["note_id"]]
    # The concept is consumed, so a later `connect` will not duplicate the note.
    assert db.get_concepts_by_status("approved", without_notes=True) == []


def test_candidate_theses_strips_pipeline_rendering_metadata():
    """#57/#58: a checklist line rendered by build_literature_chunk_note still
    adopts a clean thesis, in case a pipeline draft is later hand-edited to
    origin: manual."""
    body = (
        "## Candidatos a Nota Permanente\n\n"
        "- [ ] **Backpropagation calcula gradientes via regra da cadeia** "
        "<sub>relevancia 5/5 · p.42</sub>\n"
        "- [ ] Tese simples sem formatacao\n"
    )
    assert _candidate_theses(body) == [
        "Backpropagation calcula gradientes via regra da cadeia",
        "Tese simples sem formatacao",
    ]
