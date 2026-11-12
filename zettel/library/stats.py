"""Números do painel da Estante: leituras, páginas, notas, gêneros, formatos, países.

Tudo sai do espelho SQLite; nada aqui chama LLM ou embedding. Os gráficos são
barras de uma série só (uma cor, sem legenda), desenhados em SVG pelo template.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from zettel.library.book_file import FORMAT_LABELS, NATURE_LABELS, STATUS_LABELS
from zettel.library.schema import LibrarySchema

MONTHS = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    display: str
    href: str = ""


@dataclass
class Chart:
    title: str
    bars: list[Bar] = field(default_factory=list)
    caption: str = ""

    @property
    def peak(self) -> float:
        return max((b.value for b in self.bars), default=0) or 1


def _count_chart(title: str, counter: Counter, *, labels=None, limit: int = 12, href="") -> Chart:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))[:limit]
    return Chart(
        title,
        [
            Bar(
                (labels or {}).get(key, key) or "—",
                count,
                str(count),
                href.format(key=quote(str(key))) if href and key else "",
            )
            for key, count in items
        ],
    )


def _fmt(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def build_dashboard(db: Any, schema: LibrarySchema, year: int) -> dict[str, Any]:
    books = db.list_book_rows()
    readings = db.list_book_readings()
    finished = [r for r in readings if r.get("end_date")]
    in_year = [r for r in finished if r["end_date"][:4] == f"{year:04d}"]

    scored = [b["score"] for b in books if b.get("score") is not None]
    kpis = {
        "books": len(books),
        "read_in_year": len({r["book_id"] for r in in_year}),
        "pages_in_year": sum(r.get("pages") or 0 for r in in_year),
        "reading_now": sum(1 for b in books if b.get("status") in ("lendo", "relendo")),
        "average_score": _fmt(sum(scored) / len(scored)) if scored else "—",
        "rated": len(scored),
    }

    per_month = Counter(int(r["end_date"][5:7]) for r in in_year if r["end_date"][5:7].isdigit())
    months = Chart(
        f"Leituras terminadas em {year}",
        [Bar(MONTHS[m - 1], per_month.get(m, 0), str(per_month.get(m, 0))) for m in range(1, 13)],
    )
    per_year = Counter(r["end_date"][:4] for r in finished)
    years = Chart(
        "Leituras terminadas por ano",
        [Bar(y, per_year[y], str(per_year[y]), f"/books?year={y}") for y in sorted(per_year)],
    )

    step = schema.scale.step
    bins: Counter = Counter()
    for score in scored:
        bins[round(score / step) * step] += 1
    lo, hi = schema.scale.min, schema.scale.max
    edges = []
    value = lo
    while value <= hi + 1e-9:
        edges.append(round(value, 6))
        value += step
    scores = Chart(
        "Distribuição das notas",
        [Bar(f"{e:g}".replace(".", ","), bins.get(e, 0), str(bins.get(e, 0))) for e in edges],
        caption="Nota final arredondada ao passo da escala.",
    )

    genres: Counter = Counter()
    countries: Counter = Counter()
    for book in books:
        genres.update(json.loads(book.get("genres_json") or "[]"))
        if book.get("country"):
            countries[book["country"]] += 1

    top = sorted(
        (b for b in books if b.get("score") is not None),
        key=lambda b: (-b["score"], b["title"]),
    )[:10]
    top_rated = Chart(
        "Mais bem avaliados",
        [Bar(b["title"], b["score"], _fmt(b["score"]), f"/books/{b['book_id']}") for b in top],
    )

    sums: dict[str, list[float]] = defaultdict(list)
    for book in books:
        ratings = (json.loads(book.get("frontmatter_json") or "{}") or {}).get("ratings")
        if isinstance(ratings, dict):
            for key, value in ratings.items():
                if key in schema.criteria and isinstance(value, (int, float)):
                    sums[key].append(float(value))
    criteria = Chart(
        "Média por critério",
        [
            Bar(schema.criteria[k].label, sum(v) / len(v), f"{_fmt(sum(v) / len(v))} ({len(v)})")
            for k, v in sorted(sums.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))
        ],
        caption="Entre parênteses, quantos livros avaliaram o critério.",
    )

    return {
        "year": year,
        "years": sorted({int(y) for y in per_year if y.isdigit()} | {year}, reverse=True),
        "kpis": kpis,
        "months": months,
        "per_year": years,
        "scores": scores,
        "top_rated": top_rated,
        "criteria": criteria,
        "genres": _count_chart("Livros por gênero", genres, href="/books?genre={key}", limit=15),
        "formats": _count_chart(
            "Livros por formato",
            Counter(b.get("format") for b in books if b.get("format")),
            labels=FORMAT_LABELS,
            href="/books?format={key}",
        ),
        "statuses": _count_chart(
            "Livros por status",
            Counter(b.get("status") for b in books if b.get("status")),
            labels=STATUS_LABELS,
            href="/books?status={key}",
        ),
        "natures": _count_chart(
            "Livros por natureza",
            Counter(b.get("nature") or "" for b in books),
            labels={**NATURE_LABELS, "": "Indefinida"},
            href="/books?nature={key}",
        ),
        "countries": _count_chart(
            "Livros por país", countries, href="/books?country={key}", limit=10
        ),
    }
