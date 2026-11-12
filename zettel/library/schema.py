"""Vocabulário da Estante: gêneros, critérios de avaliação e perfis (``config/library.yaml``).

O arquivo é validado ao carregar: um perfil que pesa um critério inexistente, um
peso <= 0, dois perfis com o mesmo nome ou um gênero num grupo que não existe são
recusados com uma mensagem, em vez de virarem uma nota silenciosamente errada.
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
    # Grupo da tela (forma, gênero ficcional, não ficção...); vazio fica em "Outros".
    group: str = ""
    # Categorias do Google Books (BISAC) que sugerem este gênero no cadastro.
    aliases: list[str] = Field(default_factory=list)


class CriterionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    help: str = ""


class ProfileWhen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    genres_any: list[str] = Field(default_factory=list)
    # A natureza do livro (a escolhida, senão a dos gêneros).
    nature: Literal["ficcao", "nao_ficcao"] | None = None
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
    # Passo aceito no campo de nota; ``step`` fica para o histograma do painel.
    input_step: float | None = None

    @model_validator(mode="after")
    def ordered(self) -> ScaleSpec:
        if self.input_step is None:
            self.input_step = self.step
        if not (self.min < self.max) or self.step <= 0 or self.input_step <= 0:
            raise ValueError("escala invalida: exige min < max e passos > 0")
        return self


class LibrarySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scale: ScaleSpec = Field(default_factory=ScaleSpec)
    # Grupos de gêneros, na ordem da tela: chave -> rótulo.
    genre_groups: dict[str, str] = Field(default_factory=dict)
    genres: dict[str, GenreSpec] = Field(default_factory=dict)
    # Assuntos sugeridos (a área do livro); não decidem perfil nem modo de leitura.
    subjects: list[str] = Field(default_factory=list)
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
        stray = sorted(
            g for g, s in self.genres.items() if s.group and s.group not in self.genre_groups
        )
        if stray:
            raise ValueError(f"gêneros num grupo que não está em genre_groups: {stray}")
        return self

    def profile(self, name: str) -> ProfileSpec | None:
        return next((p for p in self.profiles if p.name == name), None)

    def genre(self, name: str) -> GenreSpec:
        return self.genres.get(name) or GenreSpec()

    def grouped_genres(self, extra: list[str] | None = None) -> list[tuple[str, list[str]]]:
        """``[(rótulo, gêneros)]`` na ordem do arquivo; sem grupo ou fora do vocabulário
        (``extra``, os gêneros que um livro já tem) vão para "Outros"."""
        groups = [
            (label, [g for g, s in self.genres.items() if s.group == key])
            for key, label in self.genre_groups.items()
        ]
        others = [g for g, s in self.genres.items() if not s.group]
        others += sorted(g for g in dict.fromkeys(extra or []) if g and g not in self.genres)
        if others:
            groups.append(("Outros", others))
        return [(label, names) for label, names in groups if names]

    def genres_for_categories(self, categories: list[str]) -> list[str]:
        """Gêneros sugeridos por categorias de catálogo ("Fiction / Fantasy / Epic").

        Cada trecho entre barras é comparado inteiro, sem acento nem caixa, com os
        ``aliases`` de cada gênero; a ordem é a do vocabulário.
        """
        from zettel.search_terms import fold

        segments = {
            fold(part).strip() for c in categories for part in str(c).split("/") if part.strip()
        }
        return [
            name
            for name, spec in self.genres.items()
            if any(fold(alias).strip() in segments for alias in spec.aliases)
        ]


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
