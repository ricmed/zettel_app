"""Schema vs config.yaml: smoke load and operational-catalog coverage."""

from __future__ import annotations

from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import yaml
from pydantic import BaseModel
from zettel.config import _REPO_ROOT, AppConfig, load_config

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
    assert cfg.retrieval.mode == "hybrid"
    assert cfg.retrieval.relevance_floor.min_vector_similarity == 0.65
    assert cfg.hub_mocs.selection_mode in ("percentile", "absolute")
    assert "contradicts" in cfg.retrieval.graph_expansion.relation_weights


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
