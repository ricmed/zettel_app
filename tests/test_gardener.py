"""Tests for gardener MOC topic validation and incremental updates."""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from zettel.config import AppConfig, GardenerConfig
from zettel.gardener import (
    _allowed_note_ids,
    _build_note_alias_map,
    _merge_incremental,
    _note_wikilink,
    _parse_incremental_output,
    _parse_moc_structure,
    _resolve_note_ref,
    _subsections_from_output,
    _update_existing_moc,
    _validate_moc_topic,
    purge_pipeline_mocs,
    render_moc_body,
)
from zettel.gardener_assign import ClusterReport
from zettel.schemas import (
    MOCEntry,
    MOCIncrementalOutput,
    MOCNotePlacement,
    MOCSubsection,
)
from zettel.state import StateDB
from zettel.taxonomy import (
    TaxonomyLoadError,
    allowed_topic_names,
    category_pillar_pairs,
    duplicate_category_names,
    format_taxonomy_for_prompt,
    load_moc_taxonomy,
    resolve_allowed_topics,
)
from zettel.vault import safe_write_note


def _make_config(
    allowed_topics: list[str] | None = None,
    strict: bool = True,
    topics_path: Path | None = None,
) -> AppConfig:
    """Helper to build an AppConfig with custom gardener settings.

    ``topics_path=None`` avoids loading the real YAML so unit tests can
    inject ``allowed_topics`` (or empty = allow-all) in isolation.
    """
    gardener_kwargs = {
        "strict_topics": strict,
        "allowed_topics": allowed_topics or [],
        "topics_path": topics_path,
    }
    return AppConfig(gardener=GardenerConfig(**gardener_kwargs))


# ── Taxonomy YAML ────────────────────────────────────────────────────


_MINI_TAXONOMY = """\
taxonomia_conhecimento:
  - pilar: "Pilar A"
    categorias:
      - nome: "Categoria Um"
        topicos:
          - "Folha A"
          - "Folha B"
      - nome: "Categoria Dois"
        topicos:
          - "Folha C"
  - pilar: "Pilar B"
    categorias:
      - nome: "Categoria Tres"
        topicos:
          - "Folha D"
"""


@pytest.fixture
def mini_taxonomy_path(tmp_path: Path) -> Path:
    p = tmp_path / "moc_topics.yaml"
    p.write_text(_MINI_TAXONOMY, encoding="utf-8")
    return p


def test_load_moc_taxonomy(mini_taxonomy_path: Path):
    tax = load_moc_taxonomy(mini_taxonomy_path)
    assert len(tax.taxonomia_conhecimento) == 2
    assert tax.taxonomia_conhecimento[0].pilar == "Pilar A"
    assert tax.taxonomia_conhecimento[0].categorias[0].nome == "Categoria Um"
    assert tax.taxonomia_conhecimento[0].categorias[0].topicos == ["Folha A", "Folha B"]


def test_allowed_topic_names_are_categories(mini_taxonomy_path: Path):
    tax = load_moc_taxonomy(mini_taxonomy_path)
    assert allowed_topic_names(tax) == [
        "Categoria Um",
        "Categoria Dois",
        "Categoria Tres",
    ]


def test_category_pillar_pairs(mini_taxonomy_path: Path):
    tax = load_moc_taxonomy(mini_taxonomy_path)
    assert category_pillar_pairs(tax) == [
        ("Pilar A", "Categoria Um"),
        ("Pilar A", "Categoria Dois"),
        ("Pilar B", "Categoria Tres"),
    ]
    assert duplicate_category_names(tax) == []


def test_format_taxonomy_for_prompt(mini_taxonomy_path: Path):
    tax = load_moc_taxonomy(mini_taxonomy_path)
    md = format_taxonomy_for_prompt(tax)
    assert "## Pilar: Pilar A" in md
    assert "### Categoria: Categoria Um" in md
    assert "- Folha A" in md
    assert "Categoria Tres" in md


def test_resolve_allowed_topics_from_file(mini_taxonomy_path: Path):
    allowed, detail = resolve_allowed_topics(mini_taxonomy_path, strict=True)
    assert "Categoria Um" in allowed
    assert "## Pilar: Pilar A" in detail


def test_resolve_allowed_topics_override(mini_taxonomy_path: Path):
    allowed, detail = resolve_allowed_topics(
        mini_taxonomy_path,
        override=["So Esta"],
        strict=True,
    )
    assert allowed == ["So Esta"]
    assert "## Pilar: Pilar A" in detail  # detail still from file


def test_resolve_missing_file_strict(tmp_path: Path):
    with pytest.raises(TaxonomyLoadError):
        resolve_allowed_topics(tmp_path / "missing.yaml", strict=True)


def test_resolve_missing_file_permissive(tmp_path: Path):
    allowed, detail = resolve_allowed_topics(tmp_path / "missing.yaml", strict=False)
    assert allowed == []
    assert "nao disponivel" in detail


def test_load_project_moc_topics_yaml():
    """Smoke: the shipped config/moc_topics.yaml parses and has categories."""
    path = Path("config/moc_topics.yaml")
    if not path.exists():
        pytest.skip("config/moc_topics.yaml ausente")
    tax = load_moc_taxonomy(path.resolve())
    names = allowed_topic_names(tax)
    assert len(names) >= 10
    assert (
        "Aplicações de LLMs" in names
        or "Aplicacoes de LLMs" in names
        or any("LLM" in n for n in names)
    )
    assert "Epistemologia" in names
    assert "Mente, Cognição e Representação" in names or any(
        "Cognição" in n or "Cognicao" in n for n in names
    )
    assert duplicate_category_names(tax) == []
    pairs = category_pillar_pairs(tax)
    assert ("Filosofia Teórica", "Epistemologia") in pairs or any(
        p == "Filosofia Teórica" and c == "Epistemologia" for p, c in pairs
    )


# ── Topic Validation Tests ───────────────────────────────────────────


def test_validate_topic_returns_the_canonical_category():
    cfg = _make_config(
        allowed_topics=["Machine Learning Clássico", "Deep Learning e Modelos Neurais"],
        strict=True,
        topics_path=None,
    )

    assert _validate_moc_topic(cfg, "machine learning classico") == (
        "Machine Learning Clássico",
        False,
    )


def test_validate_topic_no_longer_accepts_substrings():
    """ "IA" used to pass through every category containing the letters (#301)."""
    cfg = _make_config(
        allowed_topics=[
            "Confiabilidade e Avaliação",
            "Metafísica",
            "Deep Learning e Modelos Neurais",
        ],
        strict=True,
        topics_path=None,
    )

    assert _validate_moc_topic(cfg, "IA") is None
    assert _validate_moc_topic(cfg, "Deep Learning") is None


def test_validate_topic_permissive_keeps_an_emergent_theme():
    cfg = _make_config(allowed_topics=["Machine Learning Classico"], strict=False, topics_path=None)

    assert _validate_moc_topic(cfg, "Culinaria Molecular") == ("Culinaria Molecular", True)


def test_validate_empty_list():
    """When allowed_topics is empty and no topics_path, any topic is approved."""
    cfg = _make_config(allowed_topics=[], strict=True, topics_path=None)
    assert _validate_moc_topic(cfg, "Qualquer Topico") == ("Qualquer Topico", False)


def test_validate_from_taxonomy_file(mini_taxonomy_path: Path):
    cfg = _make_config(allowed_topics=[], strict=True, topics_path=mini_taxonomy_path)
    assert _validate_moc_topic(cfg, "Categoria Um") == ("Categoria Um", False)
    assert _validate_moc_topic(cfg, "Fora da Taxonomia") is None


def test_cluster_terms_are_distinctive_and_free_of_stopwords(tmp_path):
    from zettel.gardener import cluster_terms
    from zettel.search_terms import PT_STOPWORDS, fold

    db = StateDB(tmp_path / "test.db")
    texts = {
        "A1": "A autonomia da vontade em Kant",
        "A2": "Kant e a autonomia como lei moral",
        "B1": "Engenharia de prompt para modelos de linguagem",
        "B2": "O prompt como interface com o modelo",
    }
    for nid, title in texts.items():
        db.upsert_note(nid, "@S", None, title, body=f"> **Tese**: {title}.")

    terms_a, terms_b = cluster_terms(db, [["A1", "A2"], ["B1", "B2"]], n_terms=3)

    assert not {fold(w) for w in terms_a + terms_b} & PT_STOPWORDS
    assert {"kant", "autonomia"} <= set(terms_a)
    assert "prompt" in terms_b and "kant" not in terms_b
    db.close()


def test_orphan_moc_files_are_generated_files_without_a_row(tmp_path):
    from zettel.gardener import orphan_moc_files

    vault = tmp_path / "vault"
    moc_dir = vault / "40_MOCs"
    moc_dir.mkdir(parents=True)
    for moc_id, origin in (("P1", "pipeline"), ("P2", "pipeline"), ("M1", "manual")):
        meta = {"moc_id": moc_id, "origin": origin}
        safe_write_note(moc_dir / f"MOC - {moc_id} - x.md", meta, "# T\n")
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("P1", "A", str(moc_dir / "MOC - P1 - x.md"), "s")
    cfg = AppConfig()
    cfg.vault_path = vault

    assert [p.name for p in orphan_moc_files(cfg, db)] == ["MOC - P2 - x.md"]
    db.close()


# ── MOC Structure Parsing Tests ──────────────────────────────────────


SAMPLE_MOC_CONTENT = """\
---
type: moc
moc_id: 01ABCDEF
topic: Machine Learning Classico
cluster_signature: abc123
created_at: '2025-01-01T00:00:00'
updated_at: '2025-01-01T00:00:00'
---

# Machine Learning Classico

Resumo sobre Machine Learning Classico e seus algoritmos.

## Algoritmos Supervisionados

Algoritmos que aprendem a partir de dados rotulados.

- [[ZTL - NOTE001 - regressao-linear]]
- [[ZTL - NOTE002 - arvores-de-decisao]]

## Algoritmos Nao Supervisionados

Algoritmos que encontram padroes sem rotulos.

- [[ZTL - NOTE003 - k-means-clustering]]
"""


def test_parse_moc_structure(tmp_path):
    """Verify parsing correctly extracts subsections and note_ids."""
    moc_file = tmp_path / "40_MOCs" / "MOC - 01ABCDEF - machine-learning-classico.md"
    moc_file.parent.mkdir(parents=True, exist_ok=True)
    moc_file.write_text(SAMPLE_MOC_CONTENT, encoding="utf-8")

    structure = _parse_moc_structure(moc_file)

    assert structure is not None
    assert structure["topic"] == "Machine Learning Classico"
    assert "Resumo sobre Machine Learning" in structure["summary"]

    assert len(structure["subsections"]) == 2

    sub1 = structure["subsections"][0]
    assert sub1["title"] == "Algoritmos Supervisionados"
    assert [nid for nid, _ in sub1["entries"]] == ["NOTE001", "NOTE002"]
    assert "rotulados" in sub1["description"]

    sub2 = structure["subsections"][1]
    assert sub2["title"] == "Algoritmos Nao Supervisionados"
    assert [nid for nid, _ in sub2["entries"]] == ["NOTE003"]

    assert structure["all_note_ids"] == {"NOTE001", "NOTE002", "NOTE003"}


def test_parse_moc_structure_missing_file(tmp_path):
    """Returns None when file does not exist."""
    result = _parse_moc_structure(tmp_path / "nonexistent.md")
    assert result is None


# ── find_moc_by_topic Tests ──────────────────────────────────────────


def test_find_moc_by_topic_exact(tmp_path):
    """Exact topic match returns the MOC."""
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MOC001", "Machine Learning Classico", "/path/moc.md", "sig1")

    result = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))
    assert result is not None
    assert result["moc_id"] == "MOC001"
    db.close()


def test_find_moc_by_topic_rejects_substring(tmp_path):
    """Substring in either direction is not a match (#292)."""
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MOC001", "Etica Normativa e Metaetica", "/path/moc.md", "sig1")

    assert db.find_moc_by_topic("Etica", origins=None) is None
    assert db.find_moc_by_topic("Etica Normativa e Metaetica e Mais", origins=None) is None
    db.close()


def test_find_moc_by_topic_no_match(tmp_path):
    """Returns None when no match is found."""
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MOC001", "Machine Learning Classico", "/path/moc.md", "sig1")

    result = db.find_moc_by_topic("Culinaria Molecular", origins=None)
    assert result is None
    db.close()


def test_find_moc_by_topic_folds_case_and_accents(tmp_path):
    """Match ignores case and accents."""
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MOC001", "Ética Aplicada", "/path/moc.md", "sig1")

    result = db.find_moc_by_topic("etica aplicada", origins=None)
    assert result is not None
    assert result["moc_id"] == "MOC001"
    db.close()


def test_find_moc_by_topic_filters_origin(tmp_path):
    """The taxonomy pipeline never finds a manual or hub MOC (#292)."""
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MAN001", "Epistemologia", "/path/man.md", "sig1", origin="manual")
    db.upsert_moc("HUB001", "Epistemologia", "/path/hub.md", "sig2", origin="hub_pipeline")

    assert db.find_moc_by_topic("Epistemologia", origins=("pipeline",)) is None
    assert db.find_moc_by_topic("Epistemologia", origins=None)["moc_id"] in {"MAN001", "HUB001"}
    db.close()


def test_process_cluster_creates_new_moc_beside_manual_one(tmp_path):
    """A manual MOC named after the category stays untouched (#292)."""
    vault = tmp_path / "vault"
    moc_dir = vault / "40_MOCs"
    moc_dir.mkdir(parents=True)
    manual_file = moc_dir / "MOC - MAN001 - epistemologia.md"
    manual_body = "# Epistemologia\n\nMeu mapa.\n\n## Leituras\n\n- [[LIT - Kant1781 - p001 - x]]\n"
    safe_write_note(
        manual_file, {"type": "moc", "moc_id": "MAN001", "origin": "manual"}, manual_body
    )
    before = manual_file.read_bytes()

    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MAN001", "Epistemologia", str(manual_file), "sig-man", origin="manual")
    cfg = AppConfig()
    cfg.vault_path = vault
    cfg.gardener.topics_path = None
    cfg.gardener.strict_topics = False

    created: list[str] = []

    def fake_create(*args, **kwargs):
        created.append(args[3])
        return "NEW001"

    import zettel.gardener as gardener_mod

    original = gardener_mod._create_new_moc
    gardener_mod._create_new_moc = fake_create
    try:
        from zettel.gardener import _GardenStats, _process_subject

        moc_ids = _process_subject(
            cfg,
            db,
            MagicMock(),
            "Epistemologia",
            [["N1", "N2", "N3"]],
            _GardenStats(),
        )
    finally:
        gardener_mod._create_new_moc = original

    assert moc_ids == ["NEW001"]
    assert created == ["Epistemologia"]
    assert manual_file.read_bytes() == before
    assert db.get_moc("MAN001")["origin"] == "manual"
    db.close()


def test_update_existing_moc_refuses_foreign_origin(tmp_path):
    """The incremental rewrite never touches a MOC owned by another origin."""
    moc_file = tmp_path / "MOC - HUB001 - tema.md"
    safe_write_note(
        moc_file,
        {"type": "hub_moc", "moc_id": "HUB001", "origin": "hub_pipeline"},
        "# Tema\n\nResumo.\n\n## Porta de entrada\n\n",
    )
    before = moc_file.read_bytes()
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("HUB001", "Tema", str(moc_file), "sig", origin="hub_pipeline")
    llm = MagicMock()

    from zettel.gardener import _update_existing_moc

    result = _update_existing_moc(AppConfig(), db, llm, db.get_moc("HUB001"), ["N1"], "sig-new")

    assert result is None
    assert moc_file.read_bytes() == before
    assert db.get_moc("HUB001")["origin"] == "hub_pipeline"
    llm.invoke.assert_not_called()
    db.close()


# ── Incremental Output Parsing Tests ─────────────────────────────────


def test_parse_incremental_output_basic():
    """Parse a valid incremental JSON response."""
    llm_response = json.dumps(
        {
            "placements": [
                {
                    "note_id": "NOTE004",
                    "subsection": "Algoritmos Supervisionados",
                    "reason": "E supervisionado",
                },
                {
                    "note_id": "NOTE005",
                    "subsection": "ignorar",
                    "reason": "Nao se encaixa",
                },
            ],
            "new_subsections": [],
        }
    )
    result = _parse_incremental_output(llm_response)
    assert isinstance(result, MOCIncrementalOutput)
    assert len(result.placements) == 2
    assert result.placements[0].note_id == "NOTE004"
    assert result.placements[0].subsection == "Algoritmos Supervisionados"
    assert result.placements[1].subsection == "ignorar"
    assert len(result.new_subsections) == 0


def test_parse_incremental_output_with_new_subsection():
    """Parse response that includes new subsections."""
    llm_response = json.dumps(
        {
            "placements": [
                {
                    "note_id": "NOTE004",
                    "subsection": "Nova Subsecao",
                    "reason": "Pertence aqui",
                },
            ],
            "new_subsections": [
                {
                    "title": "Nova Subsecao",
                    "entries": [{"note_id": "NOTE004", "orientation": "Abre o subtema"}],
                    "description": "Algo novo",
                },
            ],
        }
    )
    result = _parse_incremental_output(llm_response)
    assert len(result.new_subsections) == 1
    assert result.new_subsections[0].title == "Nova Subsecao"
    assert result.new_subsections[0].entries == [
        MOCEntry(note_id="NOTE004", orientation="Abre o subtema")
    ]


def test_parse_incremental_output_markdown_wrapped():
    """Parse response wrapped in markdown code block."""
    inner = json.dumps(
        {
            "placements": [
                {"note_id": "NOTE004", "subsection": "Algo", "reason": "Test"},
            ],
            "new_subsections": [],
        }
    )
    llm_response = f"```json\n{inner}\n```"
    result = _parse_incremental_output(llm_response)
    assert len(result.placements) == 1


# ── Update Existing MOC Tests ────────────────────────────────────────


def _setup_moc_file(tmp_path):
    """Helper: create a MOC file and StateDB with notes."""
    moc_dir = tmp_path / "vault" / "40_MOCs"
    moc_dir.mkdir(parents=True, exist_ok=True)
    moc_file = moc_dir / "MOC - MOC001 - machine-learning-classico.md"

    meta = {
        "type": "moc",
        "moc_id": "MOC001",
        "topic": "Machine Learning Classico",
        "cluster_signature": "old_sig",
        "created_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-01T00:00:00",
    }
    body = (
        "# Machine Learning Classico\n\n"
        "Resumo sobre ML.\n\n"
        "## Algoritmos Supervisionados\n\n"
        "Descricao supervisionados.\n\n"
        "- [[ZTL - NOTE001 - regressao-linear]]\n"
        "- [[ZTL - NOTE002 - arvores-de-decisao]]\n\n"
        "## Algoritmos Nao Supervisionados\n\n"
        "Descricao nao supervisionados.\n\n"
        "- [[ZTL - NOTE003 - k-means-clustering]]\n\n"
    )
    safe_write_note(moc_file, meta, body)

    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MOC001", "Machine Learning Classico", str(moc_file), "old_sig")
    # Register notes
    for nid, title in [
        ("NOTE001", "Regressao Linear"),
        ("NOTE002", "Arvores de Decisao"),
        ("NOTE003", "K-Means Clustering"),
        ("NOTE004", "SVM"),
        ("NOTE005", "Random Forest"),
    ]:
        db.upsert_note(nid, "SRC001", None, title)

    return db, moc_file


def test_update_existing_moc_no_new_notes(tmp_path):
    """No new entry: no LLM call, and the signature stays (#296)."""
    db, _moc_file = _setup_moc_file(tmp_path)
    cfg = _make_config()
    cfg.prompts_path = tmp_path / "prompts"

    MagicMock()
    llm = MagicMock()

    existing_moc = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))
    result = _update_existing_moc(
        cfg,
        db,
        llm,
        existing_moc,
        ["NOTE001", "NOTE002", "NOTE003"],  # all existing
        "new_sig",
    )

    assert result == "MOC001"
    # LLM should NOT have been called
    llm.invoke.assert_not_called()
    updated_moc = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))
    assert updated_moc["cluster_signature"] == "old_sig"

    db.close()


def test_update_existing_moc_with_placements(tmp_path):
    """New notes are placed into correct subsections via LLM."""
    db, moc_file = _setup_moc_file(tmp_path)
    cfg = _make_config()

    # Create prompts dir with incremental prompt
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "moc_incremental.md").write_text(
        "Prompt: {moc_topic} {moc_summary} {existing_subsections} {new_notes_list}",
        encoding="utf-8",
    )
    cfg.prompts_path = prompts_dir

    MagicMock()

    # Mock LLM to return placement
    llm_response_data = {
        "placements": [
            {
                "note_id": "NOTE004",
                "subsection": "Algoritmos Supervisionados",
                "reason": "SVM e supervisionado",
            },
        ],
        "new_subsections": [],
    }
    mock_response = MagicMock()
    mock_response.content = json.dumps(llm_response_data)
    llm = MagicMock()
    llm.invoke.return_value = mock_response

    existing_moc = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))
    result = _update_existing_moc(
        cfg,
        db,
        llm,
        existing_moc,
        ["NOTE001", "NOTE002", "NOTE003", "NOTE004"],  # NOTE004 is new
        "new_sig",
    )

    assert result == "MOC001"

    # Verify the file was updated with NOTE004
    content = moc_file.read_text(encoding="utf-8")
    assert "NOTE004" in content
    assert "— SVM e supervisionado" in content
    assert "Algoritmos Supervisionados" in content
    # Existing notes should still be present
    assert "NOTE001" in content
    assert "NOTE002" in content
    assert "NOTE003" in content

    db.close()


def test_incremental_ignores_notes(tmp_path):
    """Notes marked as 'ignorar' by LLM do not appear in the updated MOC."""
    db, moc_file = _setup_moc_file(tmp_path)
    cfg = _make_config()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "moc_incremental.md").write_text(
        "Prompt: {moc_topic} {moc_summary} {existing_subsections} {new_notes_list}",
        encoding="utf-8",
    )
    cfg.prompts_path = prompts_dir

    MagicMock()

    # LLM says to ignore NOTE005
    llm_response_data = {
        "placements": [
            {
                "note_id": "NOTE005",
                "subsection": "ignorar",
                "reason": "Nao se encaixa no MOC",
            },
        ],
        "new_subsections": [],
    }
    mock_response = MagicMock()
    mock_response.content = json.dumps(llm_response_data)
    llm = MagicMock()
    llm.invoke.return_value = mock_response

    existing_moc = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))
    result = _update_existing_moc(
        cfg,
        db,
        llm,
        existing_moc,
        ["NOTE001", "NOTE003", "NOTE005"],  # NOTE005 is new
        "new_sig",
    )

    assert result == "MOC001"

    # NOTE005 should NOT appear in the MOC file
    content = moc_file.read_text(encoding="utf-8")
    assert "NOTE005" not in content
    # Existing notes should still be present
    assert "NOTE001" in content
    assert "NOTE003" in content

    db.close()


def test_update_existing_moc_with_new_subsection(tmp_path):
    """LLM can suggest new subsections for a group of new notes."""
    db, moc_file = _setup_moc_file(tmp_path)
    cfg = _make_config()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "moc_incremental.md").write_text(
        "Prompt: {moc_topic} {moc_summary} {existing_subsections} {new_notes_list}",
        encoding="utf-8",
    )
    cfg.prompts_path = prompts_dir

    MagicMock()

    llm_response_data = {
        "placements": [
            {
                "note_id": "NOTE004",
                "subsection": "Metodos Ensemble",
                "reason": "SVM combina modelos",
            },
            {
                "note_id": "NOTE005",
                "subsection": "Metodos Ensemble",
                "reason": "Random Forest e ensemble",
            },
        ],
        "new_subsections": [
            {
                "title": "Metodos Ensemble",
                "entries": [
                    {"note_id": "NOTE004", "orientation": "Combina modelos"},
                    {"note_id": "NOTE005", "orientation": "Ensemble classico"},
                ],
                "description": "Metodos que combinam multiplos modelos.",
            },
        ],
    }
    mock_response = MagicMock()
    mock_response.content = json.dumps(llm_response_data)
    llm = MagicMock()
    llm.invoke.return_value = mock_response

    existing_moc = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))
    result = _update_existing_moc(
        cfg,
        db,
        llm,
        existing_moc,
        ["NOTE001", "NOTE002", "NOTE003", "NOTE004", "NOTE005"],
        "new_sig",
    )

    assert result == "MOC001"

    content = moc_file.read_text(encoding="utf-8")
    assert "Metodos Ensemble" in content
    assert "NOTE004" in content
    assert "NOTE005" in content
    # Existing subsections still present
    assert "Algoritmos Supervisionados" in content
    assert "Algoritmos Nao Supervisionados" in content

    db.close()


# ── Routing Tests ────────────────────────────────────────────────────


def test_fresh_cluster_joins_the_category_moc_with_room(tmp_path):
    """A cluster with no MOC joins its category MOC while that MOC has room (#296)."""
    db, _moc_file = _setup_moc_file(tmp_path)
    cfg = _make_config()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "moc_incremental.md").write_text(
        "Prompt: {moc_topic} {moc_summary} {existing_subsections} {new_notes_list}",
        encoding="utf-8",
    )
    cfg.prompts_path = prompts_dir
    cfg.vault_path = tmp_path / "vault"

    MagicMock()

    incremental_response = MagicMock()
    incremental_response.content = json.dumps(
        {
            "placements": [
                {
                    "note_id": "N1",
                    "subsection": "Algoritmos Supervisionados",
                    "reason": "Teste",
                },
            ],
            "new_subsections": [],
        }
    )

    llm = MagicMock()
    llm.invoke.return_value = incremental_response

    from zettel.gardener import _GardenStats, _process_subject

    stats = _GardenStats()
    result = _process_subject(
        cfg,
        db,
        llm,
        "Machine Learning Classico",
        [["NOTE001", "NOTE002", "NOTE003", "NOTE004"]],
        stats,
    )

    assert result == ["MOC001"]
    assert [a.action for a in stats.actions] == ["incremental"]
    assert llm.invoke.call_count == 1
    assert stats.incremental == 1

    db.close()


def test_process_cluster_routes_by_overlap(tmp_path):
    """High note overlap with existing MOC skips generation and calls incremental only."""
    db, moc_file = _setup_moc_file(tmp_path)
    body = "# Outro Topico\n\n- [[ZTL - NOTE001 - a]]\n- [[ZTL - NOTE002 - b]]\n"
    db.upsert_moc(
        "MOC001",
        "Outro Topico",
        str(moc_file),
        "old_sig",
        body=body,
    )

    cfg = _make_config()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "moc_incremental.md").write_text(
        "Prompt: {moc_topic} {moc_summary} {existing_subsections} {new_notes_list}",
        encoding="utf-8",
    )
    cfg.prompts_path = prompts_dir
    cfg.vault_path = tmp_path / "vault"

    MagicMock()
    incremental_response = MagicMock()
    incremental_response.content = json.dumps(
        {
            "placements": [
                {
                    "note_id": "N3",
                    "subsection": "Algoritmos Supervisionados",
                    "reason": "Teste",
                },
            ],
            "new_subsections": [],
        }
    )
    llm = MagicMock()
    llm.invoke.return_value = incremental_response

    from zettel.gardener import _GardenStats, _process_subject

    stats = _GardenStats()
    result = _process_subject(
        cfg,
        db,
        llm,
        "_unassigned",
        [["NOTE001", "NOTE002", "NOTE005"]],
        stats,
    )

    assert result == ["MOC001"]
    assert llm.invoke.call_count == 1
    assert stats.incremental == 1

    db.close()


# ── Note ID resolution & reconciliation ──────────────────────────────


def test_resolve_note_ref_alias():
    alias_to_id = _build_note_alias_map(["NOTE001", "NOTE002"])
    allowed = {"NOTE001", "NOTE002"}
    assert _resolve_note_ref("N1", allowed, alias_to_id) == "NOTE001"
    assert _resolve_note_ref("N2", allowed, alias_to_id) == "NOTE002"


def test_resolve_note_ref_rejects_ghost():
    alias_to_id = _build_note_alias_map(["NOTE001"])
    allowed = {"NOTE001"}
    assert _resolve_note_ref("GHOST999", allowed, alias_to_id) is None


def test_resolve_note_ref_fuzzy_typo():
    alias_to_id = _build_note_alias_map(["NOTE001"])
    allowed = {"NOTE001"}
    assert _resolve_note_ref("NOTO001", allowed, alias_to_id) == "NOTE001"


def test_body_lists_only_placed_entries_with_orientation(tmp_path):
    """No reconciliation: an entry the LLM did not place is not listed (ADR-060)."""
    db = StateDB(tmp_path / "test.db")
    for nid, title in [("NOTE001", "A"), ("NOTE002", "B"), ("NOTE003", "C")]:
        db.upsert_note(nid, "SRC001", None, title)

    alias_to_id = _build_note_alias_map(["NOTE001", "NOTE002", "NOTE003"])
    allowed = _allowed_note_ids(db, ["NOTE001", "NOTE002", "NOTE003"])
    placed: set[str] = set()
    subsections = _subsections_from_output(
        [
            MOCSubsection(
                title="Grupo",
                description="Descricao",
                entries=[
                    MOCEntry(note_id="N1", orientation="Comece aqui"),
                    MOCEntry(note_id="GHOST"),
                    MOCEntry(note_id="N2", orientation="Depois esta"),
                ],
            )
        ],
        allowed,
        alias_to_id,
        placed,
    )
    body, truncated = render_moc_body(db, "Topico", "Resumo", subsections, 5)

    assert "GHOST" not in body
    assert "NOTE001" in body and "— Comece aqui" in body
    assert "NOTE002" in body
    assert "NOTE003" not in body
    assert "Outras notas do cluster" not in body
    assert allowed - placed == {"NOTE003"}
    assert truncated == []
    db.close()


def test_render_caps_entries_per_subsection(tmp_path):
    db = StateDB(tmp_path / "test.db")
    ids = [f"NOTE00{i}" for i in range(1, 5)]
    for nid in ids:
        db.upsert_note(nid, "SRC001", None, nid)

    body, truncated = render_moc_body(
        db, "T", "R", [{"title": "G", "description": "", "entries": [(n, "") for n in ids]}], 2
    )

    assert truncated == ["NOTE003", "NOTE004"]
    assert "NOTE001" in body and "NOTE002" in body
    assert "NOTE003" not in body
    db.close()


def test_merge_incremental_counts_unplaced_as_omitted(tmp_path):
    db, moc_file = _setup_moc_file(tmp_path)
    structure = _parse_moc_structure(moc_file)
    alias_to_id = _build_note_alias_map(["NOTE004", "NOTE005"])
    allowed = _allowed_note_ids(db, ["NOTE004", "NOTE005"])

    output = MOCIncrementalOutput(
        placements=[
            MOCNotePlacement(note_id="N1", subsection="algoritmos supervisionados", reason="ok"),
            MOCNotePlacement(note_id="GHOST", subsection="Algoritmos Supervisionados"),
        ],
        new_subsections=[],
    )
    merged, ignored, omitted = _merge_incremental(
        structure["subsections"], output, allowed, alias_to_id
    )

    supervised = merged[0]["entries"]
    assert ("NOTE004", "ok") in supervised
    assert all(nid != "NOTE005" for sub in merged for nid, _ in sub["entries"])
    assert (ignored, omitted) == ([], {"NOTE005"})
    db.close()


def test_orientation_round_trips_through_the_file(tmp_path):
    db = StateDB(tmp_path / "test.db")
    db.upsert_note("NOTE001", "SRC001", None, "A")
    moc_file = tmp_path / "MOC - M1 - t.md"
    body, _ = render_moc_body(
        db,
        "T",
        "Resumo",
        [{"title": "G", "description": "D", "entries": [("NOTE001", "Por aqui")]}],
        5,
    )
    safe_write_note(moc_file, {"moc_id": "M1", "topic": "T"}, body)

    structure = _parse_moc_structure(moc_file)

    assert structure["subsections"][0]["entries"] == [("NOTE001", "Por aqui")]
    db.close()


def _incremental_env(tmp_path, max_entries: int):
    db, moc_file = _setup_moc_file(tmp_path)
    cfg = _make_config()
    cfg.gardener.max_entries_per_moc = max_entries
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "moc_incremental.md").write_text(
        "Prompt: {moc_topic} {moc_summary} {existing_subsections} {new_notes_list}",
        encoding="utf-8",
    )
    (prompts_dir / "moc_summary_refresh.md").write_text(
        "Resumo: {moc_topic} {existing_subsections}", encoding="utf-8"
    )
    cfg.prompts_path = prompts_dir
    return db, moc_file, cfg


def test_full_moc_takes_no_entry_and_calls_no_llm(tmp_path):
    from zettel.gardener import _GardenStats

    db, moc_file, cfg = _incremental_env(tmp_path, max_entries=3)
    llm = MagicMock()
    stats = _GardenStats()
    existing = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))

    result = _update_existing_moc(
        cfg, db, llm, existing, ["NOTE004", "NOTE005"], "sig2", stats=stats
    )

    assert result == "MOC001"
    llm.invoke.assert_not_called()
    assert stats.cap_reached == 2
    assert "NOTE004" not in moc_file.read_text(encoding="utf-8")
    db.close()


def test_capacity_limits_the_candidates_sent(tmp_path):
    from zettel.gardener import _GardenStats

    db, _moc_file, cfg = _incremental_env(tmp_path, max_entries=4)
    response = MagicMock()
    response.content = json.dumps(
        {
            "placements": [
                {"note_id": "N1", "subsection": "Algoritmos Supervisionados", "reason": "ok"}
            ],
            "new_subsections": [],
        }
    )
    llm = MagicMock()
    llm.invoke.return_value = response
    stats = _GardenStats()
    existing = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))

    _update_existing_moc(cfg, db, llm, existing, ["NOTE004", "NOTE005"], "sig2", stats=stats)

    sent = str(llm.invoke.call_args[0][0][-1].content)
    assert "SVM" in sent and "Random Forest" not in sent
    assert stats.cap_reached == 1
    db.close()


def test_summary_is_rewritten_when_entries_grow(tmp_path):
    db, moc_file, cfg = _incremental_env(tmp_path, max_entries=10)
    meta, body = __import__("zettel.vault", fromlist=["parse_frontmatter"]).parse_frontmatter(
        moc_file.read_text(encoding="utf-8")
    )
    meta["summary_entries"] = 2
    safe_write_note(moc_file, meta, body)

    placement = MagicMock()
    placement.content = json.dumps(
        {
            "placements": [
                {"note_id": "N1", "subsection": "Algoritmos Supervisionados", "reason": "a"},
                {"note_id": "N2", "subsection": "Algoritmos Supervisionados", "reason": "b"},
            ],
            "new_subsections": [],
        }
    )
    summary = MagicMock()
    summary.content = json.dumps({"summary": "Resumo novo do MOC."})
    llm = MagicMock()
    llm.invoke.side_effect = [placement, summary]
    existing = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))

    _update_existing_moc(cfg, db, llm, existing, ["NOTE004", "NOTE005"], "sig2")

    content = moc_file.read_text(encoding="utf-8")
    assert "Resumo novo do MOC." in content
    assert "summary_entries: 5" in content
    db.close()


def test_note_wikilink_uses_file_stem_not_title_slug(tmp_path):
    db = StateDB(tmp_path / "test.db")
    db.upsert_note(
        "01KZ7YZNCQPT3693DP17PC3PVV",
        "SRC001",
        "/vault/30_Permanent/ZTL - 01KZ7YZNCQPT3693DP17PC3PVV - "
        "importancia-de-interfaces-interativas-em-sistemas.md",
        "Importancia de interfaces interativas em sistemas de questionamento",
    )
    link = _note_wikilink(db, "01KZ7YZNCQPT3693DP17PC3PVV")
    assert link is not None
    assert "importancia-de-interfaces-interativas-em-sistemas.md" not in link
    assert "importancia-de-interfaces-interativas-em-sistemas-de-questionamento" not in link
    assert "importancia-de-interfaces-interativas-em-sistemas]]" in link
    db.close()


def test_purge_pipeline_mocs_keeps_manual(tmp_path):
    vault = tmp_path / "vault"
    moc_dir = vault / "40_MOCs"
    moc_dir.mkdir(parents=True)

    pipeline_file = moc_dir / "MOC - PIPE001 - pipeline-topic.md"
    pipeline_file.write_text("# Pipeline\n", encoding="utf-8")
    manual_file = moc_dir / "MOC - MAN001 - manual-topic.md"
    manual_file.write_text("# Manual\n", encoding="utf-8")

    db = StateDB(tmp_path / "state.db")
    db.upsert_moc("PIPE001", "Pipeline Topic", str(pipeline_file), "sig1", origin="pipeline")
    db.upsert_moc("MAN001", "Manual Topic", str(manual_file), "sig2", origin="manual")

    MagicMock()
    cfg = AppConfig()
    cfg.vault_path = vault

    removed = purge_pipeline_mocs(cfg, db)

    assert removed == 1
    assert not pipeline_file.exists()
    assert manual_file.exists()
    assert db.get_moc("PIPE001") is None
    assert db.get_moc("MAN001") is not None
    db.close()


def test_purge_keeps_file_whose_frontmatter_names_another_origin(tmp_path):
    """A row mislabeled 'pipeline' is not deleted when the file says 'manual' (#292)."""
    vault = tmp_path / "vault"
    moc_dir = vault / "40_MOCs"
    moc_dir.mkdir(parents=True)
    hijacked = moc_dir / "MOC - MAN002 - epistemologia.md"
    safe_write_note(hijacked, {"type": "moc", "moc_id": "MAN002", "origin": "manual"}, "# E\n")

    db = StateDB(tmp_path / "state.db")
    db.upsert_moc("MAN002", "Epistemologia", str(hijacked), "sig", origin="pipeline")
    MagicMock()
    cfg = AppConfig()
    cfg.vault_path = vault

    removed = purge_pipeline_mocs(cfg, db)

    assert removed == 0
    assert hijacked.exists()
    assert db.get_moc("MAN002") is not None
    db.close()


# ── Fail-fast (#293) ─────────────────────────────────────────────────


def _garden_env(tmp_path):
    cfg = AppConfig()
    cfg.vault_path = tmp_path / "vault"
    cfg.gardener.topics_path = None
    cfg.gardener.strict_topics = False
    db = StateDB(tmp_path / "state.db")
    return cfg, db


def _last_run_status(db: StateDB, signature: str) -> str:
    row = db.conn.execute(
        "SELECT status FROM runs WHERE pipeline_signature=? ORDER BY run_id DESC LIMIT 1",
        (signature,),
    ).fetchone()
    return row[0]


def test_garden_stops_on_llm_unavailable(tmp_path, monkeypatch):
    import zettel.gardener as gardener_mod
    from zettel.llm import LLMUnavailableError

    cfg, db = _garden_env(tmp_path)
    calls: list[int] = []

    def down(*args, **kwargs):
        calls.append(1)
        raise LLMUnavailableError("provider down", phase="garden")

    monkeypatch.setattr(gardener_mod, "get_llm", lambda cfg, phase: MagicMock())
    monkeypatch.setattr(
        gardener_mod,
        "_cluster_pairs",
        lambda cfg, idx, observer, **_: (
            [("_unassigned", ["A", "B", "C"]), ("_unassigned", ["D", "E"])],
            ClusterReport(),
        ),
    )
    monkeypatch.setattr(gardener_mod, "call_llm", down)

    with pytest.raises(LLMUnavailableError):
        gardener_mod.run_garden(cfg, db, MagicMock())

    assert calls == [1]
    assert _last_run_status(db, "garden") == "failed"
    db.close()


def test_garden_records_failed_run_when_get_llm_raises(tmp_path, monkeypatch):
    import zettel.gardener as gardener_mod
    from zettel.llm import LLMUnavailableError

    cfg, db = _garden_env(tmp_path)

    def missing(cfg, phase):
        raise LLMUnavailableError("pacote ausente", phase=phase)

    monkeypatch.setattr(gardener_mod, "get_llm", missing)

    with pytest.raises(LLMUnavailableError):
        gardener_mod.run_garden(cfg, db, MagicMock())

    assert _last_run_status(db, "garden") == "failed"
    db.close()


def test_category_vectors_raise_on_embedding_outage(tmp_path):
    from zettel.gardener import _category_vectors
    from zettel.llm import LLMUnavailableError

    idx = MagicMock()
    idx.embed_texts.side_effect = ConnectionError("embedding down")

    with pytest.raises(LLMUnavailableError):
        _category_vectors(AppConfig(), idx, [("Pilar", "Categoria")])


def test_category_vectors_fall_back_on_content_error():
    from zettel.gardener import _category_vectors

    idx = MagicMock()
    idx.embed_texts.side_effect = ValueError("dimensao inesperada")

    assert _category_vectors(AppConfig(), idx, [("Pilar", "Categoria")]) is None


def test_invalid_taxonomy_with_recreate_deletes_nothing(tmp_path):
    from zettel.gardener import run_garden
    from zettel.taxonomy import TaxonomyLoadError

    cfg, db = _garden_env(tmp_path)
    cfg.gardener.topics_path = tmp_path / "missing.yaml"
    cfg.gardener.strict_topics = True
    moc_dir = cfg.vault_path / "40_MOCs"
    moc_dir.mkdir(parents=True)
    moc_file = moc_dir / "MOC - PIPE001 - tema.md"
    moc_file.write_text("# Tema\n", encoding="utf-8")
    db.upsert_moc("PIPE001", "Tema", str(moc_file), "sig", origin="pipeline")

    with pytest.raises(TaxonomyLoadError):
        run_garden(cfg, db, MagicMock(), recreate=True)

    assert moc_file.exists()
    assert db.get_moc("PIPE001") is not None
    assert _last_run_status(db, "garden") == "failed"
    db.close()


def test_invalid_response_goes_to_llm_failures_and_garden_continues(tmp_path, monkeypatch):
    import zettel.gardener as gardener_mod

    cfg, db = _garden_env(tmp_path)
    calls: list[int] = []

    def bad_answer(*args, **kwargs):
        calls.append(1)
        return "isto nao e json"

    monkeypatch.setattr(gardener_mod, "get_llm", lambda cfg, phase: MagicMock())
    monkeypatch.setattr(
        gardener_mod,
        "_cluster_pairs",
        lambda cfg, idx, observer, **_: (
            [("_unassigned", ["A", "B", "C"]), ("_unassigned", ["D", "E"])],
            ClusterReport(),
        ),
    )
    monkeypatch.setattr(gardener_mod, "call_llm", bad_answer)

    assert gardener_mod.run_garden(cfg, db, MagicMock()) == []

    assert calls == [1, 1]
    failures = db.conn.execute("SELECT phase, raw_response FROM llm_failures").fetchall()
    assert [tuple(f) for f in failures] == [("garden", "isto nao e json")] * 2
    assert _last_run_status(db, "garden") == "completed"
    db.close()


def test_garden_hubs_records_failed_run_when_get_llm_raises(tmp_path, monkeypatch):
    import zettel.gardener_hub as hub_mod
    from zettel.llm import LLMUnavailableError

    cfg, db = _garden_env(tmp_path)

    def missing(cfg, phase):
        raise LLMUnavailableError("pacote ausente", phase=phase)

    monkeypatch.setattr(hub_mod, "get_llm", missing)

    with pytest.raises(LLMUnavailableError):
        hub_mod.run_garden_hubs(cfg, db, recreate=True)

    assert _last_run_status(db, "garden_hubs") == "failed"
    db.close()


# ── Subjects: one door per subject, sub-MOCs, persisted discards (#296) ──


def _subject_env(tmp_path, monkeypatch, answers):
    """Garden env with notes ``{prefix}{i}`` and a fake ``call_llm`` fed by ``answers``.

    ``answers`` is a callable ``(system, user) -> dict``; every call is kept in
    the returned list as ``(system, user)``.
    """
    import zettel.gardener as gardener_mod

    cfg, db = _garden_env(tmp_path)
    (cfg.vault_path / "40_MOCs").mkdir(parents=True)
    for prefix in "ABCDE":
        for i in range(1, 5):
            db.upsert_note(f"{prefix}{i}", "@S", None, f"Nota {prefix}{i}")
    calls: list[tuple[str, str]] = []

    def fake_call(llm, user, *, system=None, **kwargs):
        calls.append((system or "", user))
        return json.dumps(answers(system or "", user))

    monkeypatch.setattr(gardener_mod, "call_llm", fake_call)
    return cfg, db, calls


def _cluster(prefix: str) -> list[str]:
    return [f"{prefix}{i}" for i in range(1, 5)]


def _generation(user: str, subtopic: str = "") -> dict:
    """One subsection per ``### Grupo`` block (or one for the whole list)."""
    blocks = user.split("### Grupo")[1:] or [user]
    subsections = []
    for i, block in enumerate(blocks, start=1):
        aliases = re.findall(r"\*\*(N\d+)\*\*", block)
        subsections.append(
            {
                "title": f"Sub {i}",
                "entries": [{"note_id": a, "orientation": f"Abre {a}"} for a in aliases],
            }
        )
    return {
        "topic": "Categoria Um",
        "summary": f"Resumo de {subtopic or 'tema'}. Segunda frase.",
        "subtopic": subtopic,
        "subsections": subsections,
    }


def test_subject_below_the_cap_gets_one_moc_with_a_subsection_per_cluster(tmp_path, monkeypatch):
    from zettel.gardener import _GardenStats, _process_subject

    cfg, db, calls = _subject_env(tmp_path, monkeypatch, lambda s, u: _generation(u))

    stats = _GardenStats()
    moc_ids = _process_subject(
        cfg, db, MagicMock(), "Categoria Um", [_cluster(p) for p in "ABC"], stats
    )

    assert len(calls) == 1
    assert "### Grupo 3" in calls[0][1]
    assert "uma subsecao por grupo" in calls[0][1]
    assert len(moc_ids) == 1 and len(db.list_mocs()) == 1
    structure = _parse_moc_structure(Path(db.get_moc(moc_ids[0])["path"]))
    assert [s["title"] for s in structure["subsections"]] == ["Sub 1", "Sub 2", "Sub 3"]
    # 10 // 3 entries per cluster, so every cluster has its door.
    assert [len(s["entries"]) for s in structure["subsections"]] == [3, 3, 3]
    assert [a.action for a in stats.actions] == ["created"]
    db.close()


def test_subject_above_the_cap_becomes_a_moc_of_mocs(tmp_path, monkeypatch):
    from zettel.gardener import _GardenStats, _process_subject

    names = iter(["Alfa", "Beta", "Gama", "Delta"])

    def answers(system, user):
        if "Reescrita do Resumo" in system:
            return {"summary": "Mapa dos subtemas."}
        return _generation(user, next(names))

    cfg, db, calls = _subject_env(tmp_path, monkeypatch, answers)

    stats = _GardenStats()
    moc_ids = _process_subject(
        cfg, db, MagicMock(), "Categoria Um", [_cluster(p) for p in "ABCD"], stats
    )

    assert len(calls) == 5  # four sub-MOCs + the category summary
    parent = db.find_moc_by_topic("Categoria Um", origins=("pipeline",))
    children = db.list_child_mocs(parent["moc_id"])
    assert sorted(c["topic"] for c in children) == [
        "Categoria Um — Alfa",
        "Categoria Um — Beta",
        "Categoria Um — Delta",
        "Categoria Um — Gama",
    ]
    assert set(moc_ids) == {parent["moc_id"], *(c["moc_id"] for c in children)}
    structure = _parse_moc_structure(Path(parent["path"]))
    assert structure["all_note_ids"] == set()
    assert structure["summary"] == "Mapa dos subtemas."
    assert [orientation for _, orientation in structure["children"]] == [
        "Resumo de Alfa.",
        "Resumo de Beta.",
        "Resumo de Gama.",
        "Resumo de Delta.",
    ]
    child_meta = _parse_moc_structure(Path(children[0]["path"]))["meta"]
    assert child_meta["parent_moc_id"] == parent["moc_id"]
    actions = [a.action for a in stats.actions]
    assert (actions.count("submoc"), actions.count("moc_of_mocs")) == (4, 1)
    db.close()


def _write_home(cfg, db, entries: list[str]) -> dict:
    """A pipeline MOC named after the category, listing ``entries``."""
    path = cfg.vault_path / "40_MOCs" / "MOC - HOME01 - categoria-um.md"
    subsections = [{"title": "Base", "description": "", "entries": [(n, "") for n in entries]}]
    body, _ = render_moc_body(db, "Categoria Um", "Resumo.", subsections, 5)
    meta = {"type": "moc", "moc_id": "HOME01", "topic": "Categoria Um", "origin": "pipeline"}
    safe_write_note(path, meta, body)
    db.upsert_moc("HOME01", "Categoria Um", str(path), "home-sig", body=body)
    return db.get_moc("HOME01")


def test_cluster_fully_discarded_by_the_incremental_gets_a_sub_moc(tmp_path, monkeypatch):
    """A subject never ends with a cluster without a door, not even then."""
    from zettel.gardener import _GardenStats, _process_subject

    def answers(system, user):
        if "Novas Portas de Entrada" in system:
            aliases = re.findall(r"\*\*(N\d+)\*\*", user)
            return {
                "placements": [
                    {"note_id": a, "subsection": "ignorar", "reason": "Fora do tema"}
                    for a in aliases
                ]
            }
        return _generation(user, "Prompts")

    cfg, db, calls = _subject_env(tmp_path, monkeypatch, answers)
    _write_home(cfg, db, _cluster("A")[:3])

    stats = _GardenStats()
    _process_subject(cfg, db, MagicMock(), "Categoria Um", [_cluster("E")], stats)

    assert len(calls) == 2  # incremental (all ignored) + the sub-MOC
    [child] = db.list_child_mocs("HOME01")
    assert child["topic"] == "Categoria Um — Prompts"
    home = _parse_moc_structure(Path(db.get_moc("HOME01")["path"]))
    assert home["all_note_ids"] == set(_cluster("A")[:3])
    assert [cid for cid, _ in home["children"]] == [child["moc_id"]]
    assert set(db.moc_discard_checksums("HOME01")) == set(_cluster("E"))
    assert db.get_moc("HOME01")["cluster_signature"] == "home-sig"
    assert [a.action for a in stats.actions] == ["submoc"]
    db.close()


def test_category_name_no_longer_swallows_a_cluster_into_a_full_moc(tmp_path, monkeypatch):
    from zettel.gardener import _GardenStats, _process_subject

    cfg, db, calls = _subject_env(
        tmp_path, monkeypatch, lambda s, u: _generation(u, "Novo subtema")
    )
    cfg.gardener.max_entries_per_moc = 3
    _write_home(cfg, db, _cluster("A")[:3])

    _process_subject(cfg, db, MagicMock(), "Categoria Um", [_cluster("E")], _GardenStats())

    assert len(calls) == 1  # no incremental offer to a full MOC
    assert len(db.list_child_mocs("HOME01")) == 1
    db.close()


def test_ignored_note_is_kept_and_not_offered_again(tmp_path):
    from zettel.gardener import _GardenStats

    db, moc_file = _setup_moc_file(tmp_path)
    db.upsert_note("NOTE005", "SRC001", None, "Random Forest", note_semantic_checksum="v1")
    cfg = _make_config()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "moc_incremental.md").write_text(
        "Prompt: {moc_topic} {moc_summary} {existing_subsections} {new_notes_list}",
        encoding="utf-8",
    )
    cfg.prompts_path = prompts_dir
    response = MagicMock()
    response.content = json.dumps(
        {"placements": [{"note_id": "N1", "subsection": "ignorar", "reason": "Outro tema"}]}
    )
    llm = MagicMock()
    llm.invoke.return_value = response
    before = moc_file.read_bytes()

    def offer(stats):
        moc = db.get_moc("MOC001")
        return _update_existing_moc(cfg, db, llm, moc, ["NOTE005"], "sig", stats=stats)

    assert offer(_GardenStats()) == "MOC001"
    assert llm.invoke.call_count == 1
    assert moc_file.read_bytes() == before
    assert db.get_moc("MOC001")["cluster_signature"] == "old_sig"
    row = db.conn.execute("SELECT reason, note_checksum FROM moc_discards").fetchone()
    assert tuple(row) == ("Outro tema", "v1")

    stats = _GardenStats()
    offer(stats)
    assert llm.invoke.call_count == 1
    assert stats.known_discards == 1

    db.upsert_note("NOTE005", "SRC001", None, "Random Forest", note_semantic_checksum="v2")
    offer(_GardenStats())
    assert llm.invoke.call_count == 2
    db.close()


def test_sub_mocs_round_trip_through_the_file(tmp_path):
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("CH1", "Cat — Sub", str(tmp_path / "MOC - CH1 - cat-sub.md"), "s")
    body, _ = render_moc_body(db, "Cat", "Resumo.", [], 5, children=[("CH1", "Comece aqui.")])
    path = tmp_path / "MOC - P1 - cat.md"
    safe_write_note(path, {"moc_id": "P1", "topic": "Cat"}, body)

    structure = _parse_moc_structure(path)

    assert "## Subtemas" in body and "[[MOC - CH1 - cat-sub]] — Comece aqui." in body
    assert structure["children"] == [("CH1", "Comece aqui.")]
    assert structure["subsections"] == [] and structure["summary"] == "Resumo."
    db.close()


def test_group_subjects_keeps_categories_together_and_unassigned_apart():
    from zettel.gardener import group_subjects

    pairs = [("X", ["a"]), ("_unassigned", ["b"]), ("X", ["c"]), ("_unassigned", ["d"])]

    assert group_subjects(pairs) == [
        ("X", [["a"], ["c"]]),
        ("_unassigned", [["b"]]),
        ("_unassigned", [["d"]]),
    ]


def test_delete_mocs_drops_their_discards(tmp_path):
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("M1", "T", None, "s")
    db.record_moc_discards("M1", [{"note_id": "N1", "reason": "r"}], prompt_checksum="p", model="m")

    db.delete_mocs(["M1"])

    assert db.moc_discard_checksums("M1") == {}
    db.close()


# ── Stable MOCs, rebalance and adoption (#300) ───────────────────────


def _vault_notes(cfg, db, prefixes: str = "ABCDE") -> None:
    """Give the notes of ``_subject_env`` real files, so backrefs can be checked."""
    perm = cfg.vault_path / "30_Permanent"
    perm.mkdir(parents=True, exist_ok=True)
    for prefix in prefixes:
        for i in range(1, 5):
            nid = f"{prefix}{i}"
            path = perm / f"ZTL - {nid} - nota.md"
            safe_write_note(path, {"note_id": nid}, f"# Nota {nid}\n\nTese.\n")
            db.upsert_note(nid, "@S", str(path), f"Nota {nid}")


def test_second_run_with_no_new_note_changes_no_moc(tmp_path, monkeypatch):
    from zettel.gardener import _GardenStats, _process_subject, _rebalance

    cfg, db, calls = _subject_env(tmp_path, monkeypatch, lambda s, u: _generation(u))
    subjects = {"Categoria Um": [_cluster(p) for p in "ABC"], "Categoria Dois": [_cluster("D")]}
    for category, clusters in subjects.items():
        _process_subject(cfg, db, MagicMock(), category, clusters, _GardenStats())
    snapshot = {
        m["moc_id"]: (Path(m["path"]).read_bytes(), m["cluster_signature"]) for m in db.list_mocs()
    }
    first_calls = len(calls)

    stats = _GardenStats()
    for category, clusters in subjects.items():
        _process_subject(cfg, db, MagicMock(), category, clusters, stats)
    _rebalance(cfg, db, stats)

    assert len(calls) == first_calls
    assert {
        m["moc_id"]: (Path(m["path"]).read_bytes(), m["cluster_signature"]) for m in db.list_mocs()
    } == snapshot
    for moc in db.list_mocs():
        assert "cluster_signature" not in _parse_moc_structure(Path(moc["path"]))["meta"]
    db.close()


def test_note_that_changed_cluster_leaves_its_old_moc_and_backref(tmp_path, monkeypatch):
    from zettel.gardener import _GardenStats, _process_subject, _rebalance

    cfg, db, _calls = _subject_env(tmp_path, monkeypatch, lambda s, u: _generation(u))
    _vault_notes(cfg, db)
    _process_subject(cfg, db, MagicMock(), "Categoria Um", [_cluster("A")], _GardenStats())
    old = db.find_moc_by_topic("Categoria Um", origins=("pipeline",))
    a1 = cfg.vault_path / "30_Permanent" / "ZTL - A1 - nota.md"
    assert old["moc_id"] in a1.read_text(encoding="utf-8")

    stats = _GardenStats()
    _process_subject(cfg, db, MagicMock(), "Categoria Um", [["A2", "A3", "A4"]], stats)
    _process_subject(cfg, db, MagicMock(), "Categoria Dois", [["A1", "B1", "B2", "B3"]], stats)
    _rebalance(cfg, db, stats)

    remaining = _parse_moc_structure(Path(old["path"]))["all_note_ids"]
    new = db.find_moc_by_topic("Categoria Dois", origins=("pipeline",))
    assert remaining == {"A2", "A3", "A4"}
    assert stats.departed == {old["moc_id"]: 1}
    backrefs = a1.read_text(encoding="utf-8")
    assert old["moc_id"] not in backrefs and new["moc_id"] in backrefs
    db.close()


def test_adopted_moc_is_left_alone_by_the_garden_and_synced_by_hand(tmp_path, monkeypatch):
    from zettel.gardener import (
        MocAdoptError,
        _GardenStats,
        _process_subject,
        adopt_moc,
        purge_pipeline_mocs,
    )
    from zettel.sync import _sync_moc

    cfg, db, _calls = _subject_env(tmp_path, monkeypatch, lambda s, u: _generation(u))
    _process_subject(cfg, db, MagicMock(), "Categoria Um", [_cluster("A")], _GardenStats())
    moc = db.find_moc_by_topic("Categoria Um", origins=("pipeline",))
    path = Path(moc["path"])

    result = adopt_moc(cfg, db, moc["moc_id"])
    adopted = path.read_bytes()

    assert result["previous_origin"] == "pipeline"
    assert db.get_moc(moc["moc_id"])["origin"] == "manual"
    assert db.get_moc(moc["moc_id"])["cluster_signature"] is None
    with pytest.raises(MocAdoptError):
        adopt_moc(cfg, db, moc["moc_id"])
    _process_subject(cfg, db, MagicMock(), "Categoria Um", [_cluster("A")], _GardenStats())
    purge_pipeline_mocs(cfg, db)
    assert path.read_bytes() == adopted
    assert db.get_moc(moc["moc_id"])["origin"] == "manual"

    meta = _parse_moc_structure(path)["meta"]
    body = path.read_text(encoding="utf-8").split("---", 2)[2]
    assert _sync_moc(cfg, db, MagicMock(), path, meta, body) == "updated"
    db.close()


def test_orphan_hub_mocs_are_listed_and_warned(tmp_path):
    from zettel.garden_report import GardenReport, graph_warnings

    db = StateDB(tmp_path / "test.db")
    db.upsert_note("HUB", "@S", None, "Hub")
    for moc_id, hub in (("M1", "HUB"), ("M2", "GONE")):
        db.upsert_moc(
            moc_id,
            f"Tema {hub}",
            None,
            "s",
            frontmatter_json=json.dumps({"hub_note_id": hub}),
            origin="hub_pipeline",
        )

    orphans = [m["topic"] for m in db.list_orphan_hub_mocs()]
    report = GardenReport("garden_hubs", total_edges=1, traversable_edges=1, orphan_hubs=orphans)

    assert orphans == ["Tema GONE"]
    assert any("Tema GONE" in w for w in graph_warnings(report, AppConfig().gardener))
    db.close()


def test_web_job_adopts_a_moc(tmp_path, monkeypatch):
    from zettel.web_app import UserFacingError, WebWorker

    cfg, db, _calls = _subject_env(tmp_path, monkeypatch, lambda s, u: _generation(u))
    from zettel.gardener import _GardenStats, _process_subject

    _process_subject(cfg, db, MagicMock(), "Categoria Um", [_cluster("A")], _GardenStats())
    moc_id = db.find_moc_by_topic("Categoria Um", origins=("pipeline",))["moc_id"]

    result = WebWorker._dispatch(cfg, db, MagicMock(), "adopt_moc", {"moc_id": moc_id})

    assert result["kind"] == "adopt_moc" and db.get_moc(moc_id)["origin"] == "manual"
    with pytest.raises(UserFacingError):
        WebWorker._dispatch(cfg, db, MagicMock(), "adopt_moc", {"moc_id": "NOPE"})
    db.close()
