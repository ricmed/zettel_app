"""Tests for gardener MOC topic validation and incremental updates."""

import json
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
    MOCGenerationOutput,
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


def _make_moc_output(topic: str, justification: str = "") -> MOCGenerationOutput:
    """Helper to build a MOCGenerationOutput."""
    return MOCGenerationOutput(
        topic=topic,
        summary="Resumo de teste",
        subsections=[],
        topic_justification=justification,
    )


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


def test_validate_topic_in_list():
    """Topic that exactly matches an allowed topic is approved."""
    cfg = _make_config(
        allowed_topics=["Machine Learning Classico", "Deep Learning e Modelos Neurais"],
        strict=True,
        topics_path=None,
    )
    moc = _make_moc_output("Machine Learning Classico")
    assert _validate_moc_topic(cfg, moc) is True


def test_validate_topic_substring():
    """Topic that contains a substring of an allowed topic is approved."""
    cfg = _make_config(
        allowed_topics=["Deep Learning e Modelos Neurais"],
        strict=True,
        topics_path=None,
    )
    moc = _make_moc_output("Deep Learning")
    assert _validate_moc_topic(cfg, moc) is True

    moc2 = _make_moc_output("Deep Learning e Modelos Neurais Avancados")
    assert _validate_moc_topic(cfg, moc2) is True


def test_validate_topic_strict_reject():
    """Topic not in the list is rejected when strict_topics=True."""
    cfg = _make_config(
        allowed_topics=["Machine Learning Classico"],
        strict=True,
        topics_path=None,
    )
    moc = _make_moc_output("Culinaria Molecular", justification="Nao se aplica")
    assert _validate_moc_topic(cfg, moc) is False


def test_validate_topic_permissive():
    """Topic not in the list is approved when strict_topics=False."""
    cfg = _make_config(
        allowed_topics=["Machine Learning Classico"],
        strict=False,
        topics_path=None,
    )
    moc = _make_moc_output("Culinaria Molecular", justification="Nao se aplica")
    assert _validate_moc_topic(cfg, moc) is True


def test_validate_empty_list():
    """When allowed_topics is empty and no topics_path, any topic is approved."""
    cfg = _make_config(allowed_topics=[], strict=True, topics_path=None)
    moc = _make_moc_output("Qualquer Topico")
    assert _validate_moc_topic(cfg, moc) is True


def test_validate_from_taxonomy_file(mini_taxonomy_path: Path):
    cfg = _make_config(allowed_topics=[], strict=True, topics_path=mini_taxonomy_path)
    assert _validate_moc_topic(cfg, _make_moc_output("Categoria Um")) is True
    assert _validate_moc_topic(cfg, _make_moc_output("Fora da Taxonomia")) is False


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
        created.append(args[4])
        return "NEW001"

    import zettel.gardener as gardener_mod

    original = gardener_mod._create_new_moc
    gardener_mod._create_new_moc = fake_create
    try:
        from zettel.gardener import _GardenStats, _process_cluster

        moc_id = _process_cluster(
            cfg, db, MagicMock(), MagicMock(), "Epistemologia", ["N1", "N2", "N3"], _GardenStats()
        )
    finally:
        gardener_mod._create_new_moc = original

    assert moc_id == "NEW001"
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

    result = _update_existing_moc(
        AppConfig(), db, MagicMock(), llm, db.get_moc("HUB001"), ["N1"], "sig-new"
    )

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
    """When all notes already exist in MOC, only signature is updated."""
    db, _moc_file = _setup_moc_file(tmp_path)
    cfg = _make_config()
    cfg.prompts_path = tmp_path / "prompts"

    idx = MagicMock()
    llm = MagicMock()

    existing_moc = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))
    result = _update_existing_moc(
        cfg,
        db,
        idx,
        llm,
        existing_moc,
        ["NOTE001", "NOTE002", "NOTE003"],  # all existing
        "new_sig",
    )

    assert result == "MOC001"
    # LLM should NOT have been called
    llm.invoke.assert_not_called()
    # Signature should be updated
    updated_moc = db.find_moc_by_topic("Machine Learning Classico", origins=("pipeline",))
    assert updated_moc["cluster_signature"] == "new_sig"

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

    idx = MagicMock()

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
        idx,
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

    idx = MagicMock()

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
        idx,
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

    idx = MagicMock()

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
        idx,
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


def test_generate_moc_routes_to_incremental(tmp_path):
    """_process_cluster routes to incremental when category matches existing MOC."""
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

    idx = MagicMock()

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

    from zettel.gardener import _GardenStats, _process_cluster

    stats = _GardenStats()
    result = _process_cluster(
        cfg,
        db,
        idx,
        llm,
        "Machine Learning Classico",
        ["NOTE001", "NOTE002", "NOTE003", "NOTE004"],
        stats,
    )

    assert result == "MOC001"
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

    idx = MagicMock()
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

    from zettel.gardener import _GardenStats, _process_cluster

    stats = _GardenStats()
    result = _process_cluster(
        cfg,
        db,
        idx,
        llm,
        "_unassigned",
        ["NOTE001", "NOTE002", "NOTE005"],
        stats,
    )

    assert result == "MOC001"
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
    assert truncated == 0
    db.close()


def test_render_caps_entries_per_subsection(tmp_path):
    db = StateDB(tmp_path / "test.db")
    ids = [f"NOTE00{i}" for i in range(1, 5)]
    for nid in ids:
        db.upsert_note(nid, "SRC001", None, nid)

    body, truncated = render_moc_body(
        db, "T", "R", [{"title": "G", "description": "", "entries": [(n, "") for n in ids]}], 2
    )

    assert truncated == 2
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
    assert (ignored, omitted) == (0, 1)
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
        cfg, db, MagicMock(), llm, existing, ["NOTE004", "NOTE005"], "sig2", stats=stats
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

    _update_existing_moc(
        cfg, db, MagicMock(), llm, existing, ["NOTE004", "NOTE005"], "sig2", stats=stats
    )

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

    _update_existing_moc(cfg, db, MagicMock(), llm, existing, ["NOTE004", "NOTE005"], "sig2")

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

    idx = MagicMock()
    cfg = AppConfig()
    cfg.vault_path = vault

    removed = purge_pipeline_mocs(cfg, db, idx)

    assert removed == 1
    assert not pipeline_file.exists()
    assert manual_file.exists()
    assert db.get_moc("PIPE001") is None
    assert db.get_moc("MAN001") is not None
    idx.delete_mocs.assert_called_once_with(["PIPE001"])
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
    idx = MagicMock()
    cfg = AppConfig()
    cfg.vault_path = vault

    removed = purge_pipeline_mocs(cfg, db, idx)

    assert removed == 0
    assert hijacked.exists()
    assert db.get_moc("MAN002") is not None
    idx.delete_mocs.assert_not_called()
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
        lambda cfg, idx, observer: (
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
        lambda cfg, idx, observer: (
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
        hub_mod.run_garden_hubs(cfg, db, MagicMock(), recreate=True)

    assert _last_run_status(db, "garden_hubs") == "failed"
    db.close()
