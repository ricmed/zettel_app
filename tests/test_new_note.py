"""Tests for manual note scaffolding (new-note)."""

import pytest
from zettel.config import AppConfig
from zettel.new_note import (
    NewNoteResult,
    normalize_note_type,
    provisional_citekey,
    scaffold_manual_note,
)
from zettel.vault import parse_frontmatter, read_managed_block


@pytest.fixture
def cfg(tmp_path):
    vault = tmp_path / "vault"
    for d in ("10_Sources", "20_Literature", "30_Permanent", "40_MOCs"):
        (vault / d).mkdir(parents=True)
    return AppConfig(vault_path=vault)


def test_normalize_note_type_aliases():
    assert normalize_note_type("ztl") == "permanent"
    assert normalize_note_type("LIT") == "literature"
    assert normalize_note_type("source") == "source"
    assert normalize_note_type("moc") == "moc"


def test_normalize_note_type_invalid():
    with pytest.raises(ValueError, match="Tipo de nota invalido"):
        normalize_note_type("article")


@pytest.mark.parametrize("source_id", ["../../outside", r"..\\outside", "/tmp/outside"])
def test_scaffold_rejects_unsafe_source_ids(cfg, source_id):
    with pytest.raises(ValueError, match="source_id/citekey invalido"):
        scaffold_manual_note(
            cfg,
            "lit",
            "Unsafe",
            source_id=source_id,
            granular=True,
        )


def test_provisional_citekey_author_year():
    ck = provisional_citekey(["Maria Silva"], 2024, "Knowledge Graphs")
    assert ck == "Silva2024KnowledgeGraphs"


def test_scaffold_source_note(cfg):
    result = scaffold_manual_note(
        cfg,
        "src",
        "Meu Artigo",
        authors=["Joao Negro"],
        year=2026,
    )
    assert isinstance(result, NewNoteResult)
    assert result.path.parent.name == "10_Sources"
    assert result.path.name.startswith("SRC - Negro2026 - ")
    assert result.path.exists()

    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["type"] == "source"
    assert meta["origin"] == "manual"
    assert meta["source_id"].startswith("@")
    assert meta["citekey"] == meta["source_id"].lstrip("@")
    assert meta["origin_type"] == "md"
    assert "Meu Artigo" in body
    assert "Indice de Literatura" in body
    assert "## Referencia para notas permanentes" in body
    assert meta["source_id"] in body
    assert f"[[{result.path.stem}]]" in body


def test_scaffold_source_with_biblio_and_explicit_citekey(cfg):
    result = scaffold_manual_note(
        cfg,
        "src",
        "O Capital",
        citekey="Marx2013Capital",
        authors=["Karl Marx"],
        year=2013,
        document_type="livro",
        place="Sao Paulo",
        publisher="Boitempo",
        edition="2. ed.",
        abnt_reference="MARX, Karl. O Capital. 2. ed. Sao Paulo: Boitempo, 2013.",
    )
    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["source_id"] == "@Marx2013Capital"
    assert meta["citekey"] == "Marx2013Capital"
    assert meta["document_type"] == "livro"
    assert meta["place"] == "Sao Paulo"
    assert meta["publisher"] == "Boitempo"
    assert meta["edition"] == "2. ed."
    assert meta["abnt_reference"].startswith("MARX")
    assert "## Referencia ABNT" in body
    assert "Boitempo" in body


def test_scaffold_source_source_id_flag(cfg):
    result = scaffold_manual_note(
        cfg,
        "src",
        "Artigo Base",
        source_id="@CustomKey2024",
        authors=["Autor Teste"],
        year=2024,
    )
    meta, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["source_id"] == "@CustomKey2024"
    assert meta["citekey"] == "CustomKey2024"
    assert "CustomKey2024" in result.path.name or result.path.name.startswith("SRC - CustomKey2024")


def test_scaffold_source_sync_manual_adopts(cfg, tmp_path):
    from zettel.state import StateDB
    from zettel.sync import run_sync_manual

    class FakeIndex:
        def __init__(self):
            self.sources: list[str] = []

        def upsert_source(self, sid, summary, meta):
            self.sources.append(sid)

    scaffold_manual_note(
        cfg,
        "src",
        "Fonte Manual",
        citekey="Manual2025",
        authors=["Ana Autora"],
        year=2025,
        document_type="artigo_periodico",
        journal="Revista X",
    )
    db = StateDB(tmp_path / "state.db")
    idx = FakeIndex()
    try:
        stats = run_sync_manual(cfg, db, idx)
        assert stats["sources"] == 1
        stored = db.get_source("@Manual2025")
        assert stored is not None
        assert stored["origin"] == "manual"
        assert stored["document_type"] == "artigo_periodico"
        assert idx.sources == ["@Manual2025"]
    finally:
        db.close()


def test_scaffold_literature_index(cfg):
    result = scaffold_manual_note(
        cfg,
        "lit",
        "Artigo Manual",
        citekey="Autor2023",
    )
    assert result.path.parent.name == "20_Literature"
    assert result.path.name == "LIT - Autor2023 - artigo-manual.md"

    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["type"] == "literature_index"
    assert meta["origin"] == "manual"
    block = read_managed_block(body, "auto-lit-index")
    assert block is not None
    assert "Nenhuma nota granular" in block


def test_scaffold_literature_granular(cfg):
    result = scaffold_manual_note(
        cfg,
        "literature",
        "Fonte Granular",
        citekey="Autor2023",
        granular=True,
        chunk_index=2,
        page=42,
    )
    assert result.path.parent.name == "Autor2023"
    assert "p042" in result.path.name

    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["type"] == "literature"
    assert meta["status"] == "approved"
    assert read_managed_block(body, "auto-source-excerpt") is not None
    assert "_Preencha o resumo._" in body
    assert "_Cole o trecho da fonte aqui._" in read_managed_block(body, "auto-source-excerpt")


def test_scaffold_literature_granular_persists_body(cfg):
    scaffold_manual_note(cfg, "src", "Fonte Granular", citekey="Autor2023")
    result = scaffold_manual_note(
        cfg,
        "literature",
        "Sistema 1",
        source_id="@Autor2023",
        granular=True,
        chunk_index=1,
        page=20,
        summary="Pensar rapido e o modo padrao.",
        source_text="O sistema 1 opera automaticamente.",
        key_concepts=["sistema 1", "heuristica"],
        candidates=[{"thesis": "O sistema 1 e o modo padrao da mente."}],
    )
    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["section_path"] == "Sistema 1"
    excerpt = read_managed_block(body, "auto-source-excerpt")
    assert excerpt is not None
    assert "O sistema 1 opera automaticamente." in excerpt
    assert "Pensar rapido e o modo padrao." in body
    assert "#sistema 1" in body
    assert "O sistema 1 e o modo padrao da mente." in body
    assert "_Preencha o resumo._" not in body


def test_scaffold_permanent_note(cfg):
    result = scaffold_manual_note(cfg, "ztl", "Minha Tese")
    assert result.path.parent.name == "30_Permanent"
    assert result.path.name.startswith("ZTL - ")

    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["type"] == "permanent"
    assert meta["origin"] == "manual"
    assert "source_id" not in meta
    assert len(meta["note_id"]) == 26
    assert "> **Tese**:" in body
    assert read_managed_block(body, "auto-connections") is not None


def test_scaffold_permanent_note_persists_thesis(cfg):
    result = scaffold_manual_note(
        cfg,
        "ztl",
        "Heurísticas",
        thesis="Heurísticas reduzem esforço cognitivo.",
    )
    _meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert "Heurísticas reduzem esforço cognitivo." in body
    assert "_Preencha a tese._" not in body


def test_scaffold_permanent_with_existing_src(cfg):
    src = scaffold_manual_note(
        cfg,
        "src",
        "Knowledge Graphs",
        authors=["Maria Silva"],
        year=2024,
        citekey="Silva2024KG",
    )
    result = scaffold_manual_note(
        cfg,
        "ztl",
        "Grafos e recuperacao",
        source_id="@Silva2024KG",
    )
    assert result.warnings is None

    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["source_id"] == "@Silva2024KG"
    assert f"[[{src.path.stem}]]" in body
    assert "## Fonte" in body
    assert "- Fonte (SRC):" in body


def test_scaffold_permanent_provisional_src(cfg):
    result = scaffold_manual_note(
        cfg,
        "ztl",
        "Nota sem SRC ainda",
        source_id="MeuTemaCustom",
    )
    assert result.warnings
    assert any("SRC nao encontrada" in w for w in result.warnings)

    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["source_id"] == "@MeuTemaCustom"
    assert "[[SRC - MeuTemaCustom]]" in body


def test_scaffold_permanent_source_id_via_citekey(cfg):
    src = scaffold_manual_note(
        cfg,
        "src",
        "Artigo Base",
        citekey="Base2023",
    )
    result = scaffold_manual_note(
        cfg,
        "ztl",
        "Ideia derivada",
        citekey="Base2023",
    )
    assert result.warnings is None

    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["source_id"] == "@Base2023"
    assert f"[[{src.path.stem}]]" in body


def test_scaffold_moc(cfg):
    result = scaffold_manual_note(cfg, "moc", "Tema Principal")
    assert result.path.parent.name == "40_MOCs"
    assert result.path.name.startswith("MOC - ")

    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert meta["type"] == "moc"
    assert meta["topic"] == "Tema Principal"
    assert meta["origin"] == "manual"
    assert len(meta["moc_id"]) == 26
    assert body.startswith("# Tema Principal")


def test_scaffold_refuses_existing_file(cfg):
    scaffold_manual_note(cfg, "src", "Titulo Unico", citekey="Fix2024")
    with pytest.raises(FileExistsError):
        scaffold_manual_note(cfg, "src", "Titulo Unico", citekey="Fix2024")


def test_scaffold_force_overwrites(cfg):
    path = scaffold_manual_note(
        cfg,
        "src",
        "Titulo",
        citekey="Fix2024",
        authors=["Antigo"],
    ).path
    scaffold_manual_note(
        cfg,
        "src",
        "Titulo",
        citekey="Fix2024",
        authors=["Novo"],
        force=True,
    )
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert meta["author"] == ["Novo"]


def test_force_does_not_overwrite_an_existing_literature_index(cfg):
    """SRC --force rewrites the source note but must leave the literature index.

    The index carries the auto-lit-index block that review/sync maintain.
    Passing the same force flag through to _write_literature_index would
    destroy that block (risco #2 / WI-8).
    """
    scaffold_manual_note(cfg, "src", "Thinking Fast", citekey="Kahneman2011")
    index_dir = cfg.vault_path / "20_Literature"
    indexes = list(index_dir.glob("LIT - *.md"))
    assert len(indexes) == 1
    marker = "<!-- hand-edited index -->"
    original = indexes[0].read_text(encoding="utf-8")
    indexes[0].write_text(original + "\n" + marker + "\n", encoding="utf-8")
    scaffold_manual_note(
        cfg,
        "src",
        "Thinking Fast",
        citekey="Kahneman2011",
        force=True,
    )
    assert marker in indexes[0].read_text(encoding="utf-8")
