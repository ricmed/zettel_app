"""Weighted rating by profile (#390): the Notion branches, first match, completeness."""

from __future__ import annotations

import pytest
from zettel.library.rating import is_translated, match_profile, rate, weighted_score
from zettel.library.schema import LibrarySchema, LibrarySchemaError, load_library_schema

from tests.library_support import REPO

SCHEMA = load_library_schema(REPO / "config" / "library.yaml")

# (genres, translated) -> (profile, sum of weights) — the twelve branches of the
# Notion formula the bookshelf replaces.
NOTION_BRANCHES = [
    (["Quadrinho"], False, "quadrinho", 34),
    (["Mangá"], True, "quadrinho_traduzido", 37),
    (["Biografia"], False, "biografia", 36),
    (["Biografia"], True, "biografia_traduzida", 38),
    (["Ilustrado"], False, "ilustrado", 32),
    (["Ilustrado"], True, "ilustrado_traduzido", 34),
    (["Conto"], False, "conto", 34),
    (["Conto"], True, "conto_traduzido", 36),
    (["Técnico"], False, "tecnico", 21),
    (["Não-Ficção"], True, "tecnico_traduzido", 24),
    (["Clássico", "Drama"], False, "ficcao", 30),
    (["Clássico", "Drama"], True, "ficcao_traduzida", 32),
]


@pytest.mark.parametrize(("genres", "translated", "name", "total"), NOTION_BRANCHES)
def test_profiles_reproduce_the_notion_branches(genres, translated, name, total):
    profile = match_profile(SCHEMA, genres, translated)
    assert profile.name == name
    assert profile.total_weight == total


def test_first_matching_profile_decides():
    # Comics come before fiction, so a comic novel is rated as a comic.
    assert match_profile(SCHEMA, ["Quadrinho", "Drama"], False).name == "quadrinho"
    # Genre match ignores case and accents.
    assert match_profile(SCHEMA, ["quadrinho"], False).name == "quadrinho"


def test_rating_profile_forces_a_profile():
    meta = {"genres": ["Ilustrado", "Romance"], "rating_profile": "ficcao"}
    assert rate(SCHEMA, meta).profile.name == "ficcao"


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"original_language": "en"}, True),
        ({"original_language": "pt-BR", "country": "Inglaterra"}, False),
        ({"original_language": "Português"}, False),
        ({"country": "Brasil"}, False),
        ({"country": "Portugal"}, False),
        ({"country": "Inglaterra"}, True),
        ({}, False),
    ],
)
def test_translated_comes_from_the_language_then_the_country(meta, expected):
    assert is_translated(meta) is expected


def test_score_matches_the_notion_formula_when_every_criterion_is_rated():
    ratings = {
        "edition": 4,
        "translation_lettering": 5,
        "plot": 5,
        "characters": 4,
        "feeling": 5,
        "theme": 4,
        "setting": 5,
        "ending": 5,
    }
    result = rate(SCHEMA, {"genres": ["Clássico"], "country": "Inglaterra", "ratings": ratings})
    # (2*4 + 3*5 + 4*5 + 4*4 + 5*5 + 4*4 + 5*5 + 5*5) / 32 = 150/32 = 4.6875 -> ceil 4.69
    assert result.profile.name == "ficcao_traduzida"
    assert result.score == 4.69
    assert result.complete


def test_unrated_criteria_leave_the_denominator():
    result = rate(SCHEMA, {"genres": ["Técnico"], "ratings": {"usefulness": 5, "edition": 3}})
    assert result.score == pytest.approx(math_ceil((5 * 5 + 3 * 3) / 8))
    assert (result.rated, result.total) == (2, 5)
    assert not result.complete


def math_ceil(value: float) -> float:
    import math

    return math.ceil(round(value * 100, 6)) / 100


def test_out_of_scale_and_foreign_criteria_are_ignored_not_counted():
    result = rate(SCHEMA, {"genres": ["Técnico"], "ratings": {"usefulness": 9, "art": 5}})
    assert result.score is None
    assert result.ignored == {"usefulness": 9.0, "art": 5.0}


def test_float_noise_does_not_round_up():
    assert weighted_score({"a": 1.0}, {"a": 4.5}) == 4.5
    assert weighted_score({"a": 3.0, "b": 7.0}, {"a": 4.1, "b": 4.1}) == 4.1


def test_lines_list_heaviest_criteria_first():
    lines = rate(SCHEMA, {"genres": ["Técnico"]}).lines
    weights = [line.weight for line in lines]
    assert weights == sorted(weights, reverse=True)
    assert lines[0].label in {"Utilidade", "Custo-Benefício"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"profiles": [{"name": "x", "weights": {"nao_existe": 1}}]}, "inexistentes"),
        ({"profiles": [{"name": "x", "weights": {"plot": 0}}]}, "> 0"),
        (
            {
                "profiles": [
                    {"name": "x", "weights": {"plot": 1}},
                    {"name": "x", "weights": {"plot": 1}},
                ]
            },
            "repetido",
        ),
        ({"profiles": []}, "ao menos um perfil"),
    ],
)
def test_incoherent_vocabulary_is_refused(change, message):
    base = {"criteria": {"plot": {"label": "Enredo"}}, "profiles": []}
    with pytest.raises(ValueError, match=message):
        LibrarySchema(**{**base, **change})


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(LibrarySchemaError, match="nao encontrado"):
        load_library_schema(tmp_path / "nada.yaml")
