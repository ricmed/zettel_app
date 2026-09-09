"""Schema vs config.yaml: smoke load and operational-catalog coverage."""

from __future__ import annotations

from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest
import yaml
from pydantic import BaseModel, ValidationError
from zettel.config import (
    _REPO_ROOT,
    DEFAULT_RELATION_WEIGHTS,
    AppConfig,
    RelevanceFloorConfig,
    load_config,
)

_CONFIG_YAML = _REPO_ROOT / "config" / "config.yaml"
_PYTHON_ONLY_PATHS = frozenset({"gardener.allowed_topics"})


def _unwrap_annotation(ann: Any) -> Any:
    origin = get_origin(ann)
    if origin is Union or origin is UnionType:
        args = [a for a in get_args(ann) if a is not type(None)]
        return args[0] if args else ann
    return ann


def schema_leaf_paths(model: type[BaseModel], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        inner = _unwrap_annotation(field.annotation)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            paths.extend(schema_leaf_paths(inner, path))
        else:
            paths.append(path)
    return paths


def yaml_has_path(data: dict[str, Any], dotted: str) -> bool:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def test_load_config_yaml_smoke():
    cfg = load_config(_CONFIG_YAML)
    assert cfg.vault_timezone == "America/Sao_Paulo"
    assert cfg.domain.name
    assert cfg.domain.examples_path.name == "domain_examples.yaml"
    assert cfg.gardener.category_label_template == "{pilar}: {categoria}"
    assert "manual" in cfg.retrieval.graph_expansion.relation_weights
    assert cfg.retrieval.mode == "hybrid"
    # O YAML tem que concordar com o default do schema: em 0.65 consultas fora
    # do dominio passavam no piso (medido em scripts/probe_relevance_floor.py).
    assert cfg.retrieval.relevance_floor.min_vector_similarity == 0.70
    assert (
        cfg.retrieval.relevance_floor.min_vector_similarity
        == RelevanceFloorConfig().min_vector_similarity
    )
    assert cfg.hub_mocs.selection_mode in ("percentile", "absolute")
    assert "contradicts" in cfg.retrieval.graph_expansion.relation_weights


def test_chapter_floor_is_pinned_and_separate_from_the_note_floor():
    """The two floors measure different text distributions (ADR-047).

    A chapter summary is longer and more diffuse than a note, so it scores lower
    against the same question; inheriting the note floor would be a category
    error even when the two numbers agree.

    0.70 was measured on 2026-09-07 over 71 summarized chapters
    (ollama/qwen3-embedding@1024d): off-domain queries topped out at 0.693,
    self-match bottomed at 0.745. Re-measure with
    `scripts/probe_relevance_floor.py --collection chapter_summaries` after any
    embedding change; YAML and schema must not drift apart meanwhile.

    Deliberately NOT asserted: that the two values differ. They coincide at 0.70
    today, and that is two independent measurements agreeing, not inheritance —
    pinning them apart would fail the moment a probe legitimately landed on the
    same number. What must hold is that they are separate objects.
    """
    cfg = load_config(_CONFIG_YAML)
    assert cfg.retrieval.chapter_floor.min_vector_similarity == 0.70
    assert (
        cfg.retrieval.chapter_floor.min_vector_similarity
        == AppConfig().retrieval.chapter_floor.min_vector_similarity
    )
    # Separate objects, so tuning one can never silently move the other.
    assert cfg.retrieval.chapter_floor is not cfg.retrieval.relevance_floor


def test_bypass_coverage_gate_is_pinned_on_both_floors():
    """The BM25 bypass needs an absolute half, not just a rank (ADR-003 addendum).

    `bm25_bypass_max_rank` asks whether a hit ranked well *among whoever
    matched*; BM25 ORs the query terms, so on a small corpus the match pool is
    routinely smaller than the cutoff and "top 5" means "everything". Measured
    2026-09-09 over 62 notes: off-domain bypasses reached coverage 0.50 at most,
    in-domain bypasses 0.50 at least, and a one-word jargon query scores 1.00 —
    so 0.5 blocks 97% of the noise at zero recall cost.
    """
    cfg = load_config(_CONFIG_YAML)
    for floor in (cfg.retrieval.relevance_floor, cfg.retrieval.chapter_floor):
        assert floor.bm25_bypass_min_coverage == 0.5
    assert (
        cfg.retrieval.relevance_floor.bm25_bypass_min_coverage
        == RelevanceFloorConfig().bm25_bypass_min_coverage
    )


def test_corroborates_weight_is_low_on_purpose():
    """Weight governs traversal, not importance.

    A corroboration clique (N sources, one idea) is what the researcher wants to
    read and the worst thing to spend `max_neighbors` slots on — every extra hop
    returns a paraphrase instead of new information. Keeping it at or below
    `related` is the primary mitigation; the relation earns its prominence in the
    rendered Conexoes/backlinks instead.
    """
    weights = load_config(_CONFIG_YAML).retrieval.graph_expansion.relation_weights
    assert weights["corroborates"] == DEFAULT_RELATION_WEIGHTS["corroborates"]
    assert weights["corroborates"] <= weights["related"]
    assert weights["corroborates"] < weights["supports"]


def test_config_yaml_covers_schema_keys():
    raw = yaml.safe_load(_CONFIG_YAML.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)

    missing = [
        path
        for path in schema_leaf_paths(AppConfig)
        if path not in _PYTHON_ONLY_PATHS and not yaml_has_path(raw, path)
    ]
    assert missing == [], (
        "Chaves do schema ausentes em config/config.yaml (fonte operacional). "
        f"Declare-as ou, se forem so de codigo, acrescente na allowlist: {missing}"
    )


def test_pydantic_defaults_match_operational_yaml_for_chunking_and_linking():
    """AppConfig() (sem YAML) nao deve exercitar uma config diferente da producao.

    Chave ausente no YAML cai no default do Field (load_config faz
    AppConfig(**yaml)), entao um default historico diferente do YAML e uma
    armadilha silenciosa para qualquer teste que instancie AppConfig() puro.
    """
    defaults = AppConfig()
    operational = load_config(_CONFIG_YAML)

    assert defaults.chunking.chunk_size == operational.chunking.chunk_size
    assert defaults.chunking.chunk_overlap == operational.chunking.chunk_overlap
    assert defaults.chunking.min_section_chars == operational.chunking.min_section_chars
    assert defaults.chunking.min_chunk_chars == operational.chunking.min_chunk_chars
    assert defaults.linking.dedupe_threshold == operational.linking.dedupe_threshold


def test_load_config_paths_ignore_process_cwd(monkeypatch, tmp_path: Path):
    """Web/CLI must use repo-root data/, not a stray cwd-relative state.db."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.state_db_path == _REPO_ROOT / "data" / "state.db"
    assert cfg.vault_path == _REPO_ROOT / "vault"


def test_load_config_from_package_subdir_still_hits_repo_data(monkeypatch):
    monkeypatch.chdir(_REPO_ROOT / "zettel")
    cfg = load_config()
    assert cfg.state_db_path == _REPO_ROOT / "data" / "state.db"


def test_vault_timezone_invalid_rejected():
    with pytest.raises(ValidationError):
        AppConfig(vault_timezone="Not/A_Real_Zone")
