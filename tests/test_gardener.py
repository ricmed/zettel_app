"""Tests for gardener MOC topic validation and incremental updates."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from zettel.config import AppConfig, GardenerConfig
from zettel.gardener import (
    _MOC_FALLBACK_SUBSECTION,
    _allowed_note_ids,
    _apply_incremental_placements,
    _build_moc_body,
    _build_note_alias_map,
    _note_wikilink,
    _parse_incremental_output,
    _parse_moc_structure,
    _resolve_note_ref,
    _update_existing_moc,
    _validate_moc_topic,
    purge_pipeline_mocs,
)
from zettel.schemas import (
    MOCGenerationOutput,
    MOCIncrementalOutput,
    MOCNotePlacement,
    MOCSubsection,
)
from zettel.state import StateDB
from zettel.taxonomy import (
    TaxonomyLoadError,
    allowed_topic_names,
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
    assert sub1["note_ids"] == ["NOTE001", "NOTE002"]
    assert "rotulados" in sub1["description"]

    sub2 = structure["subsections"][1]
    assert sub2["title"] == "Algoritmos Nao Supervisionados"
    assert sub2["note_ids"] == ["NOTE003"]

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

    result = db.find_moc_by_topic("Machine Learning Classico")
    assert result is not None
    assert result["moc_id"] == "MOC001"
    db.close()


def test_find_moc_by_topic_substring(tmp_path):
    """Bidirectional substring match works."""
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MOC001", "Machine Learning Classico", "/path/moc.md", "sig1")

    # Existing topic contains the query
    result = db.find_moc_by_topic("Machine Learning")
    assert result is not None
    assert result["moc_id"] == "MOC001"

    # Query contains the existing topic
    result2 = db.find_moc_by_topic("Machine Learning Classico e Avancado")
    assert result2 is not None
    assert result2["moc_id"] == "MOC001"

    db.close()


def test_find_moc_by_topic_no_match(tmp_path):
    """Returns None when no match is found."""
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MOC001", "Machine Learning Classico", "/path/moc.md", "sig1")

    result = db.find_moc_by_topic("Culinaria Molecular")
    assert result is None
    db.close()


def test_find_moc_by_topic_case_insensitive(tmp_path):
    """Match is case-insensitive."""
    db = StateDB(tmp_path / "test.db")
    db.upsert_moc("MOC001", "Deep Learning", "/path/moc.md", "sig1")

    result = db.find_moc_by_topic("deep learning")
    assert result is not None
    assert result["moc_id"] == "MOC001"
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
                    "note_ids": ["NOTE004"],
                    "description": "Algo novo",
                },
            ],
        }
    )
    result = _parse_incremental_output(llm_response)
    assert len(result.new_subsections) == 1
    assert result.new_subsections[0].title == "Nova Subsecao"
    assert result.new_subsections[0].note_ids == ["NOTE004"]


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

    existing_moc = db.find_moc_by_topic("Machine Learning Classico")
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
    updated_moc = db.find_moc_by_topic("Machine Learning Classico")
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

    existing_moc = db.find_moc_by_topic("Machine Learning Classico")
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

    existing_moc = db.find_moc_by_topic("Machine Learning Classico")
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
                "note_ids": ["NOTE004", "NOTE005"],
                "description": "Metodos que combinam multiplos modelos.",
            },
        ],
    }
    mock_response = MagicMock()
    mock_response.content = json.dumps(llm_response_data)
    llm = MagicMock()
    llm.invoke.return_value = mock_response

    existing_moc = db.find_moc_by_topic("Machine Learning Classico")
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


def test_build_moc_body_filters_ghost_and_reconciles_missing(tmp_path):
    db = StateDB(tmp_path / "test.db")
    for nid, title in [("NOTE001", "A"), ("NOTE002", "B"), ("NOTE003", "C")]:
        db.upsert_note(nid, "SRC001", None, title)

    alias_to_id = _build_note_alias_map(["NOTE001", "NOTE002", "NOTE003"])
    allowed = _allowed_note_ids(db, ["NOTE001", "NOTE002", "NOTE003"])
    subsections = [
        MOCSubsection(
            title="Grupo",
            note_ids=["N1", "GHOST", "N2"],
            description="Descricao",
        ),
    ]
    body = _build_moc_body(db, "Topico", "Resumo", subsections, allowed, alias_to_id)

    assert "GHOST" not in body
    assert "NOTE001" in body
    assert "NOTE002" in body
    assert _MOC_FALLBACK_SUBSECTION in body
    assert "NOTE003" in body
    db.close()


def test_apply_incremental_reconciles_unplaced(tmp_path):
    db, moc_file = _setup_moc_file(tmp_path)
    structure = _parse_moc_structure(moc_file)
    alias_to_id = _build_note_alias_map(["NOTE004", "NOTE005"])
    allowed = _allowed_note_ids(db, ["NOTE004", "NOTE005"])

    output = MOCIncrementalOutput(
        placements=[
            MOCNotePlacement(note_id="N1", subsection="Algoritmos Supervisionados", reason="ok"),
        ],
        new_subsections=[],
    )
    _apply_incremental_placements(db, moc_file, structure, output, allowed, alias_to_id)

    content = moc_file.read_text(encoding="utf-8")
    assert "NOTE004" in content
    assert "NOTE005" in content
    assert _MOC_FALLBACK_SUBSECTION in content
    assert "GHOST" not in content
    db.close()


def test_apply_incremental_ghost_id_ignored(tmp_path):
    db, moc_file = _setup_moc_file(tmp_path)
    structure = _parse_moc_structure(moc_file)
    alias_to_id = _build_note_alias_map(["NOTE004"])
    allowed = _allowed_note_ids(db, ["NOTE004"])

    output = MOCIncrementalOutput(
        placements=[
            MOCNotePlacement(
                note_id="GHOST", subsection="Algoritmos Supervisionados", reason="bad"
            ),
        ],
        new_subsections=[],
    )
    _apply_incremental_placements(db, moc_file, structure, output, allowed, alias_to_id)

    content = moc_file.read_text(encoding="utf-8")
    assert "GHOST" not in content
    assert "NOTE004" in content
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
