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
    (["Didático"], True, "tecnico_traduzido", 24),
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
    meta = {"genres": ["Clássico", "Romance"], "country": "Inglaterra", "ratings": ratings}
    result = rate(SCHEMA, meta)
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


@pytest.mark.parametrize(
    ("genres", "name"),
    [
        (["Filosofia"], "ideias"),
        (["Psicologia", "Ensaio"], "ideias"),
        (["História"], "divulgacao"),
        (["Livro-reportagem"], "divulgacao"),
        (["Memórias"], "biografia"),
        (["Poesia"], "poesia"),
        (["Crônica"], "conto"),
        (["Autoajuda"], "tecnico"),
        (["Fantasia"], "ficcao"),
        # Clássico sozinho não diz a natureza: o perfil geral, não o de ficção.
        (["Clássico"], "geral"),
        ([], "geral"),
    ],
)
def test_every_genre_family_has_its_own_profile(genres, name):
    assert match_profile(SCHEMA, genres, False).name == name


def test_the_chosen_nature_picks_the_profile_when_no_genre_does():
    meta = {"genres": ["Clássico"], "nature": "nao_ficcao", "original_language": "pt"}
    assert rate(SCHEMA, meta).profile.name == "nao_ficcao"
    meta = {"genres": [], "nature": "ficcao", "original_language": "en"}
    assert rate(SCHEMA, meta).profile.name == "ficcao_traduzida"


def test_genres_are_grouped_and_unknown_ones_go_to_others():
    groups = dict(SCHEMA.grouped_genres(["Cyberpunk"]))
    assert list(groups) == [
        "Forma",
        "Gênero ficcional",
        "Não ficção",
        "Público",
        "Marcador",
        "Outros",
    ]
    assert "Romance" in groups["Forma"] and "Fantasia" in groups["Gênero ficcional"]
    assert groups["Outros"] == ["Cyberpunk"]
    assert "Ficção" not in SCHEMA.genres and "Não-Ficção" not in SCHEMA.genres


def test_catalog_categories_suggest_genres_by_whole_segment():
    assert SCHEMA.genres_for_categories(["Fiction / Fantasy / Epic"]) == ["Romance", "Fantasia"]
    assert SCHEMA.genres_for_categories(["Young Adult Fiction"]) == ["Jovem Adulto"]
    # "Science Fiction" não sugere Ciência: o trecho é comparado inteiro.
    assert SCHEMA.genres_for_categories(["Science Fiction"]) == ["Ficção Científica"]
    assert SCHEMA.genres_for_categories(["Computers / Programming"]) == ["Técnico"]
    assert SCHEMA.genres_for_categories([]) == []


def test_a_genre_in_an_unknown_group_is_refused(tmp_path):
    raw = (REPO / "config" / "library.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "library.yaml"
    bad.write_text(
        raw.replace("{group: forma, nature: ficcao, aliases: [poetry]}", "{group: lirica}"),
        encoding="utf-8",
    )
    with pytest.raises(LibrarySchemaError, match="genre_groups"):
        load_library_schema(bad)


def test_a_psychological_novel_stays_fiction():
    """Enclausurado: as versões ficcionais de psicologia/filosofia mantêm o romance na ficção."""
    novel = ["Novela", "Ficção Psicológica", "Ficção Filosófica", "Suspense", "Sátira", "Releitura"]
    assert match_profile(SCHEMA, novel, True).name == "ficcao_traduzida"
    # Psicologia/Filosofia são não ficção: misturadas a um romance, a natureza some.
    assert match_profile(SCHEMA, ["Romance", "Psicologia"], True).name == "ideias_traduzido"
    assert SCHEMA.genres_for_categories(["Fiction / Psychological", "Fiction / Retellings"]) == [
        "Romance",
        "Ficção Psicológica",
        "Releitura",
    ]
