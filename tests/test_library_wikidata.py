"""Work facts from Wikidata (author's country, first year, original language and title), offline."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse

import pytest
from zettel.library import wikidata
from zettel.library.lookup import BookLookupError


def _row(qid, author, *, date=None, country=None, lang=None, title=None, title_lang=None):
    row = {
        "work": {"value": f"http://www.wikidata.org/entity/{qid}"},
        "authorLabel": {"value": author},
    }
    if date:
        row["date"] = {"value": date}
    if country:
        row["countryLabel"] = {"value": country}
    if lang:
        row["langCode"] = {"value": lang}
    if title:
        row["origTitle"] = {"value": title, "xml:lang": title_lang or lang}
    return row


def _fetcher(answers: list[list[dict]], seen: list[str]):
    """Uma resposta SPARQL por chamada, na ordem."""
    queue = list(answers)

    def fetch(url):
        seen.append(url)
        rows = queue.pop(0)
        return "application/sparql-results+json", json.dumps(
            {"results": {"bindings": rows}}
        ).encode()

    return fetch


def _sparql(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]


HOBBIT = [
    _row(
        "Q74287",
        "J. R. R. Tolkien",
        date="1937-09-21T00:00:00Z",
        country="Reino Unido",
        lang="en",
        title="The Hobbit",
    ),
]


def test_the_work_gives_country_first_year_language_and_original_title():
    seen: list[str] = []
    facts = wikidata.work_facts(
        "O Hobbit: lá e de volta outra vez", ["J.R.R. Tolkien"], _fetcher([HOBBIT], seen)
    )
    assert facts is not None
    assert facts.book_fields() == {
        "country": "Reino Unido",
        "original_language": "en",
        "original_publication_year": 1937,
        "original_title": "The Hobbit",
    }
    assert facts.url == "https://www.wikidata.org/wiki/Q74287"
    query = _sparql(seen[0])
    assert 'mwapi:search "O Hobbit"' in query  # o subtítulo da edição sai da busca
    assert 'mwapi:language "pt"' in query
    assert seen[0].startswith("https://query.wikidata.org/sparql?")


def test_only_a_work_by_one_of_the_authors_is_accepted():
    rows = [
        _row("Q1", "René Descartes", date="1641-01-01T00:00:00Z", country="França", lang="fr"),
        _row("Q2", "Marco Aurélio", country="Roma Antiga", title="Τὰ εἰς ἑαυτὸν", title_lang="grc"),
    ]
    facts = wikidata.work_facts("Meditações", ["Marcus Aurelius"], _fetcher([rows], []))
    assert facts is not None and facts.qid == "Q2"
    assert facts.country == "Roma Antiga" and facts.first_year is None
    assert facts.original_language == "" and facts.original_title == ""


def test_transliterated_surnames_match_and_the_title_decides_the_language():
    rows = [
        _row(
            "Q161531",
            "Liev Tolstói",
            date="1869-01-01T00:00:00Z",
            country="Império Russo",
            lang="fr",
            title="Война и мир",
            title_lang="ru",
        ),
        _row(
            "Q161531",
            "Liev Tolstói",
            date="1865-01-01T00:00:00Z",
            country="Império Russo",
            lang="ru",
            title="Война и мир",
            title_lang="ru",
        ),
    ]
    facts = wikidata.work_facts("Guerra e Paz", ["Leo Tolstoy"], _fetcher([rows], []))
    assert facts is not None
    assert (facts.original_language, facts.original_title, facts.first_year) == (
        "ru",
        "Война и мир",
        1865,
    )


def test_portuguese_first_then_english_and_none_when_nobody_matches():
    seen: list[str] = []
    facts = wikidata.work_facts("The Hobbit", ["Tolkien"], _fetcher([[], HOBBIT], seen))
    assert facts is not None and facts.qid == "Q74287"
    assert [
        'mwapi:language "pt"' in _sparql(seen[0]),
        'mwapi:language "en"' in _sparql(seen[1]),
    ] == [True, True]
    other = [_row("Q9", "Fulano de Tal")]
    # obra (pt, en) e depois autor (pt, en): ninguém bate
    nobody = _fetcher([other, other, [], []], [])
    assert wikidata.work_facts("O Hobbit", ["Tolkien"], nobody) is None


def test_without_authors_nothing_is_guessed_and_no_query_is_made():
    assert wikidata.work_facts("O Hobbit", [], lambda url: pytest.fail(url)) is None
    assert wikidata.work_facts("O Hobbit", ["  "], lambda url: pytest.fail(url)) is None


def _author_row(qid, name, country=None):
    row = {
        "author": {"value": f"http://www.wikidata.org/entity/{qid}"},
        "authorLabel": {"value": name},
    }
    if country:
        row["countryLabel"] = {"value": country}
    return row


def test_a_translated_title_missing_in_portuguese_still_brings_the_author():
    """Enclausurado (Nutshell) não tem rótulo em PT no Wikidata: vem só o autor."""
    seen: list[str] = []
    author = [_author_row("Q190379", "Ian McEwan", "Reino Unido")]
    facts = wikidata.work_facts("Enclausurado", ["Ian McEwan"], _fetcher([[], [], author], seen))
    assert facts is not None and facts.match == "author"
    assert facts.book_fields() == {"country": "Reino Unido"}
    assert 'mwapi:search "Ian McEwan"' in _sparql(seen[2]) and "wd:Q5" in _sparql(seen[2])


def test_the_original_title_is_searched_first():
    seen: list[str] = []
    nutshell = [
        _row(
            "Q27940587",
            "Ian McEwan",
            date="2016-05-10T00:00:00Z",
            country="Reino Unido",
            lang="en",
            title="Nutshell",
        )
    ]
    facts = wikidata.work_facts(
        "Enclausurado", ["Ian McEwan"], _fetcher([nutshell], seen), original_title="Nutshell"
    )
    assert facts is not None and facts.match == "work" and facts.first_year == 2016
    assert len(seen) == 1 and 'mwapi:search "Nutshell"' in _sparql(seen[0])


def test_the_title_cannot_break_out_of_the_sparql_literal():
    seen: list[str] = []
    wikidata.work_facts('A "obra" \\ }\nSELECT', ["Autor"], _fetcher([[], [], [], []], seen))
    assert 'mwapi:search "A \\"obra\\" \\\\ } SELECT"' in _sparql(seen[0])


def test_dates_before_christ_and_several_citizenships():
    rows = [
        _row("Q5", "Edmund Husserl", date="-0400-01-01T00:00:00Z", country="Reich Alemão"),
        _row("Q5", "Edmund Husserl", country="Império Austríaco"),
    ]
    facts = wikidata.work_facts("Meditações Cartesianas", ["E. Husserl"], _fetcher([rows], []))
    assert facts is not None and facts.first_year is None
    assert (
        facts.countries == ["Reich Alemão", "Império Austríaco"] and facts.country == "Reich Alemão"
    )


def test_wikidata_down_is_a_lookup_error():
    def down(url):
        raise urllib.error.URLError("sem rede")

    with pytest.raises(BookLookupError, match="Wikidata"):
        wikidata.work_facts("O Hobbit", ["Tolkien"], down)
