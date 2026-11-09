"""Vocabulário da Estante: gêneros, critérios de avaliação e perfis (``config/library.yaml``).

O arquivo é validado ao carregar: um perfil que pesa um critério inexistente, um
peso <= 0 ou dois perfis com o mesmo nome são recusados com uma mensagem, em vez
de virarem uma nota silenciosamente errada.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Nature = Literal["ficcao", "nao_ficcao", "neutro"]


class GenreSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nature: Nature = "neutro"
    # Pede leitura reflexiva mesmo sendo não ficção (filosofia, biografia...).
    reflective: bool = False


class CriterionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    help: str = ""


class ProfileWhen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    genres_any: list[str] = Field(default_factory=list)
    translated: bool | None = None


class ProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    label: str = ""
    when: ProfileWhen = Field(default_factory=ProfileWhen)
    weights: dict[str, float]

    @field_validator("weights")
    @classmethod
    def positive_weights(cls, weights: dict[str, float]) -> dict[str, float]:
        if not weights:
            raise ValueError("perfil sem pesos")
        bad = sorted(k for k, w in weights.items() if w <= 0)
        if bad:
            raise ValueError(f"pesos devem ser > 0: {bad}")
        return weights

    @property
    def total_weight(self) -> float:
        return sum(self.weights.values())


class ScaleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float = 1
    max: float = 5
    step: float = 0.5

    @model_validator(mode="after")
    def ordered(self) -> ScaleSpec:
        if not (self.min < self.max) or self.step <= 0:
            raise ValueError("escala invalida: exige min < max e step > 0")
        return self


class LibrarySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scale: ScaleSpec = Field(default_factory=ScaleSpec)
    genres: dict[str, GenreSpec] = Field(default_factory=dict)
    criteria: dict[str, CriterionSpec]
    profiles: list[ProfileSpec]

    @model_validator(mode="after")
    def coherent(self) -> LibrarySchema:
        if not self.profiles:
            raise ValueError("library.yaml precisa de ao menos um perfil")
        names = [p.name for p in self.profiles]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"perfis com nome repetido: {dupes}")
        for profile in self.profiles:
            unknown = sorted(set(profile.weights) - set(self.criteria))
            if unknown:
                raise ValueError(f"perfil {profile.name!r} pesa critérios inexistentes: {unknown}")
        return self

    def profile(self, name: str) -> ProfileSpec | None:
        return next((p for p in self.profiles if p.name == name), None)

    def genre(self, name: str) -> GenreSpec:
        return self.genres.get(name) or GenreSpec()


class LibrarySchemaError(ValueError):
    """``library.yaml`` ausente, ilegível ou incoerente."""


@lru_cache(maxsize=8)
def _load(path: str, mtime_ns: int) -> LibrarySchema:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return LibrarySchema(**raw)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise LibrarySchemaError(f"{path}: {exc}") from exc


def load_library_schema(path: Path | str) -> LibrarySchema:
    """O vocabulário de ``path``; relido só quando o arquivo muda (mtime)."""
    resolved = Path(path).resolve()
    try:
        mtime = resolved.stat().st_mtime_ns
    except OSError as exc:
        raise LibrarySchemaError(f"{resolved}: arquivo nao encontrado") from exc
    return _load(str(resolved), mtime)


def schema_for(cfg: object) -> LibrarySchema:
    """Atalho: o vocabulário configurado em ``cfg.library.rating_path``."""
    return load_library_schema(cfg.library.rating_path)  # type: ignore[attr-defined]
