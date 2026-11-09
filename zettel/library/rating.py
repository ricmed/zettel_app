"""Nota ponderada de um livro: perfil por gênero e tradução, média dos critérios avaliados.

``score = ceil(Σ peso·nota / Σ peso · 100) / 100``, como no Notion, com uma
diferença deliberada: o denominador soma só os critérios que receberam nota. No
Notion um critério vazio contava zero e derrubava a nota de um livro avaliado
pela metade; aqui a completude aparece ao lado da nota.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from zettel.library.book_file import as_str_list, ratings_of
from zettel.library.schema import LibrarySchema, ProfileSpec
from zettel.search_terms import fold

# Países cuja língua é o português: livro de lá não é traduzido.
_PORTUGUESE_COUNTRIES = frozenset({"brasil", "brazil", "portugal"})


@dataclass(frozen=True)
class CriterionLine:
    key: str
    label: str
    weight: float
    value: float | None


@dataclass(frozen=True)
class RatingResult:
    profile: ProfileSpec | None
    score: float | None
    lines: list[CriterionLine] = field(default_factory=list)
    # Notas fora da escala ou de critérios que o perfil não pesa: ignoradas.
    ignored: dict[str, float] = field(default_factory=dict)

    @property
    def rated(self) -> int:
        return sum(1 for line in self.lines if line.value is not None)

    @property
    def total(self) -> int:
        return len(self.lines)

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.rated == self.total


def is_translated(meta: dict[str, Any]) -> bool:
    """Idioma original diferente do português; sem idioma, país fora da lusofonia."""
    language = fold(str(meta.get("original_language") or "")).strip()
    if language:
        return not language.startswith(("pt", "portug"))
    country = fold(str(meta.get("country") or "")).strip()
    if country:
        return country not in _PORTUGUESE_COUNTRIES
    return False


def match_profile(schema: LibrarySchema, genres: list[str], translated: bool) -> ProfileSpec | None:
    """O primeiro perfil cujo ``when`` casa com o livro."""
    folded = {fold(g) for g in genres}
    for profile in schema.profiles:
        wanted = {fold(g) for g in profile.when.genres_any}
        if wanted and not (wanted & folded):
            continue
        if profile.when.translated is not None and profile.when.translated != translated:
            continue
        return profile
    return None


def profile_for(schema: LibrarySchema, meta: dict[str, Any]) -> ProfileSpec | None:
    """``rating_profile`` da LIV quando nomeia um perfil; senão o primeiro que casa."""
    forced = schema.profile(str(meta.get("rating_profile") or "").strip())
    if forced:
        return forced
    return match_profile(schema, as_str_list(meta.get("genres")), is_translated(meta))


def weighted_score(weights: dict[str, float], ratings: dict[str, float]) -> float | None:
    numerator = sum(w * ratings[k] for k, w in weights.items() if k in ratings)
    denominator = sum(w for k, w in weights.items() if k in ratings)
    if not denominator:
        return None
    # round antes do ceil: 4.5 vira 449.99999999999994 em ponto flutuante.
    return math.ceil(round(numerator / denominator * 100, 6)) / 100


def rate(schema: LibrarySchema, meta: dict[str, Any]) -> RatingResult:
    profile = profile_for(schema, meta)
    raw = ratings_of(meta)
    if profile is None:
        return RatingResult(profile=None, score=None, ignored=raw)
    lo, hi = schema.scale.min, schema.scale.max
    valid = {k: v for k, v in raw.items() if k in profile.weights and lo <= v <= hi}
    lines = [
        CriterionLine(
            key=key,
            label=schema.criteria[key].label,
            weight=weight,
            value=valid.get(key),
        )
        for key, weight in sorted(profile.weights.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    ignored = {k: v for k, v in raw.items() if k not in valid}
    return RatingResult(
        profile=profile,
        score=weighted_score(profile.weights, valid),
        lines=lines,
        ignored=ignored,
    )
