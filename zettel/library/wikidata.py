"""Dados da obra que os catálogos de edição não têm, pelo Wikidata.

Google Books e Open Library descrevem uma **edição**. O país do autor (P27,
cidadania), o ano da primeira publicação (P577), o idioma original (P407 com o
código ISO P218) e o título original (P1476) são da **obra**, e o Wikidata os
guarda. A consulta SPARQL procura a obra pelo título original (quando a LIV o
tem) e pelo título do livro (busca de entidades do próprio Wikidata, em PT e
depois em EN) e só aceita um resultado cujo autor (P50) tenha o sobrenome de um
dos autores do livro. Uma tradução cujo título não está no Wikidata em PT
("Enclausurado" é "Nutshell", sem rótulo em português) não acha a obra: aí vale
ao menos o autor, que traz o país. Sem chave; só leitura.
"""

from __future__ import annotations

import difflib
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from zettel.library.lookup import Fetcher, _get_json

SPARQL_URL = "https://query.wikidata.org/sparql?{query}"
ENTITY_URL = "https://www.wikidata.org/wiki/{qid}"
SERVICE = "o Wikidata"
SEARCH_LANGUAGES = ("pt", "en")
# Sobrenomes transliterados variam ("Tolstói", "Tolstoy"): parecidos o bastante valem.
SURNAME_SIMILARITY = 0.8
_QUERY = """SELECT ?work ?ordinal ?authorLabel ?date ?countryLabel ?langCode ?origTitle WHERE {
  SERVICE wikibase:mwapi {
    bd:serviceParam wikibase:endpoint "www.wikidata.org";
                    wikibase:api "EntitySearch";
                    mwapi:search "%(title)s";
                    mwapi:language "%(language)s";
                    mwapi:limit "15".
    ?work wikibase:apiOutputItem mwapi:item.
    ?ordinal wikibase:apiOrdinal true.
  }
  ?work wdt:P50 ?author.
  FILTER NOT EXISTS { ?work wdt:P629 ?editionOf }
  OPTIONAL { ?work wdt:P577 ?date. }
  OPTIONAL { ?author wdt:P27 ?country. }
  OPTIONAL { ?work wdt:P407 ?lang. ?lang wdt:P218 ?langCode. }
  OPTIONAL { ?work wdt:P1476 ?origTitle. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "pt-br,pt,en". }
}
ORDER BY ?ordinal
LIMIT 300"""
# Reserva quando a obra não é achada: a pessoa (Q5) que assina alguma obra (P50).
_AUTHOR_QUERY = """SELECT ?author ?ordinal ?authorLabel ?countryLabel WHERE {
  SERVICE wikibase:mwapi {
    bd:serviceParam wikibase:endpoint "www.wikidata.org";
                    wikibase:api "EntitySearch";
                    mwapi:search "%(title)s";
                    mwapi:language "%(language)s";
                    mwapi:limit "10".
    ?author wikibase:apiOutputItem mwapi:item.
    ?ordinal wikibase:apiOrdinal true.
  }
  ?author wdt:P31 wd:Q5.
  FILTER EXISTS { ?anyWork wdt:P50 ?author }
  OPTIONAL { ?author wdt:P27 ?country. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "pt-br,pt,en". }
}
ORDER BY ?ordinal
LIMIT 50"""


@dataclass
class WorkFacts:
    qid: str
    author: str
    country: str = ""
    countries: list[str] = field(default_factory=list)
    first_year: int | None = None
    original_language: str = ""
    original_title: str = ""
    # "work": a obra foi achada; "author": só o autor (o país vale, o resto não veio).
    match: str = "work"

    @property
    def url(self) -> str:
        return ENTITY_URL.format(qid=self.qid)

    def book_fields(self) -> dict[str, Any]:
        """O que vai à LIV (``editing.fill_missing`` só preenche o que está vazio)."""
        values = {
            "country": self.country,
            "original_language": self.original_language,
            "original_publication_year": self.first_year,
            "original_title": self.original_title,
        }
        return {k: v for k, v in values.items() if v}


def _literal(text: str) -> str:
    """O título como literal SPARQL: sem quebra de linha, aspas e barra escapadas."""
    clean = " ".join(text.split())[:150]
    return clean.replace("\\", "\\\\").replace('"', '\\"')


def _search_title(title: str) -> str:
    # O subtítulo é da edição e atrapalha a busca ("O Hobbit: lá e de volta outra vez").
    return re.split(r"\s*[:;]\s+", title.strip(), maxsplit=1)[0]


def _surname(name: str) -> str:
    from zettel.hashing import fold_for_match

    parts = fold_for_match(name).replace(".", " ").split()
    return parts[-1] if parts else ""


def same_author(wanted: list[str], found: str) -> bool:
    """Um dos autores do livro tem sobrenome igual (ou quase) ao do autor no Wikidata."""
    other = _surname(found)
    return bool(other) and any(
        difflib.SequenceMatcher(None, _surname(a), other).ratio() >= SURNAME_SIMILARITY
        for a in wanted
        if _surname(a)
    )


def _value(row: dict[str, Any], key: str) -> str:
    return str((row.get(key) or {}).get("value") or "")


def _year(raw: str) -> int | None:
    match = re.match(r"^(\d{1,4})-", raw)  # datas antes de Cristo ("-0400") ficam de fora
    return int(match.group(1)) if match else None


def _facts(qid: str, rows: list[dict[str, Any]]) -> WorkFacts:
    countries = list(dict.fromkeys(c for r in rows if (c := _value(r, "countryLabel"))))
    years = [y for r in rows if (y := _year(_value(r, "date"))) is not None]
    codes = list(dict.fromkeys(c for r in rows if (c := _value(r, "langCode"))))
    titles = {
        (r["origTitle"].get("xml:lang") or ""): r["origTitle"]["value"]
        for r in rows
        if r.get("origTitle")
    }
    # Idioma original: o que tem também o título na mesma língua ("Война и мир" decide
    # entre russo e francês em Guerra e Paz); com um só idioma, ele.
    language = next((c for c in codes if c in titles), codes[0] if len(codes) == 1 else "")
    return WorkFacts(
        qid=qid,
        author=_value(rows[0], "authorLabel"),
        country=countries[0] if countries else "",
        countries=countries,
        first_year=min(years) if years else None,
        original_language=language,
        original_title=" ".join(titles.get(language, "").split())[:300],
    )


def _query(
    title: str, language: str, fetch: Fetcher | None, template: str = _QUERY
) -> list[dict[str, Any]]:
    sparql = template % {"title": _literal(title), "language": language}
    params = urllib.parse.urlencode({"query": sparql, "format": "json"})
    data = _get_json(SPARQL_URL.format(query=params), fetch, SERVICE)
    bindings = (
        ((data or {}).get("results") or {}).get("bindings") if isinstance(data, dict) else None
    )
    return [b for b in bindings or [] if isinstance(b, dict)]


def _find_work(search: str, authors: list[str], fetch: Fetcher | None) -> WorkFacts | None:
    for language in SEARCH_LANGUAGES:
        by_work: dict[str, list[dict[str, Any]]] = {}
        for row in _query(search, language, fetch):  # já em ordem de relevância da busca
            if same_author(authors, _value(row, "authorLabel")):
                qid = _value(row, "work").rsplit("/", 1)[-1]
                by_work.setdefault(qid, []).append(row)
        if by_work:
            qid, matched = next(iter(by_work.items()))
            return _facts(qid, matched)
    return None


def _find_author(name: str, fetch: Fetcher | None) -> WorkFacts | None:
    for language in SEARCH_LANGUAGES:
        rows = [
            r
            for r in _query(name, language, fetch, _AUTHOR_QUERY)
            if same_author([name], _value(r, "authorLabel"))
        ]
        if rows:
            qid = _value(rows[0], "author").rsplit("/", 1)[-1]
            mine = [r for r in rows if _value(r, "author").endswith(f"/{qid}")]
            countries = list(dict.fromkeys(c for r in mine if (c := _value(r, "countryLabel"))))
            return WorkFacts(
                qid=qid,
                author=_value(rows[0], "authorLabel"),
                country=countries[0] if countries else "",
                countries=countries,
                match="author",
            )
    return None


def work_facts(
    title: str,
    authors: list[str],
    fetch: Fetcher | None = None,
    *,
    original_title: str = "",
) -> WorkFacts | None:
    """A obra cujo autor bate com ``authors``; senão o autor; ``None`` se nada.

    Procura pelo título original e depois pelo título; sem obra, pelo primeiro
    autor (só o país vem). ``BookLookupError`` quando o Wikidata não responde.
    Sem autores não há como saber se o resultado é o livro certo, e nada é devolvido.
    """
    named = [a for a in authors if _surname(a)]
    if not named:
        return None
    searches = [_search_title(t or "") for t in (original_title, title)]
    for search in dict.fromkeys(t for t in searches if t):
        found = _find_work(search, named, fetch)
        if found:
            return found
    return _find_author(named[0], fetch)
