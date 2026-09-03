"""Schema vs config.yaml: smoke load and operational-catalog coverage."""

from __future__ import annotations

from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import yaml
from pydantic import BaseModel

from zettel.config import AppConfig, load_config

_CONFIG_YAML = Path("config/config.yaml")
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
