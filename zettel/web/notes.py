"""Permanent-note and MOC listing."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from zettel.web.rendering import render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


@router.get("/notes", response_class=HTMLResponse)
async def notes(
    request: Request,
    q: str = "",
    kind: str = "",
    source_id: str = "",
    author: str = "",
    origin: str = "",
    sort: str = "recent",
    page: str = "1",
):
    if not authenticated(request):
        return redirect_login()
    filters = {
        "q": q.strip()[:200],
        "kind": kind if kind in {"ZTL", "MOC"} else "",
        "source_id": source_id[:200],
        "author": author[:200],
        "origin": origin[:100],
        "sort": sort if sort in {"recent", "oldest", "title"} else "recent",
    }
    current = int(page) if page.isdecimal() and len(page) <= 6 and int(page) > 0 else 1
    db = service(request).db()
    try:
        facets = db.catalog_facets()
        rows, total = db.search_catalog(
            query=filters["q"],
            kind=filters["kind"],
            source_id=filters["source_id"],
            author=filters["author"],
            origin=filters["origin"],
            sort=filters["sort"],
            page=current,
        )
        pages = max(1, (total + 11) // 12)
        if current > pages:
            current = pages
            rows, _ = db.search_catalog(
                query=filters["q"],
                kind=filters["kind"],
                source_id=filters["source_id"],
                author=filters["author"],
                origin=filters["origin"],
                sort=filters["sort"],
                page=current,
            )
    finally:
        db.close()
    for row in rows:
        try:
            names = json.loads(row["authors"] or "[]")
        except (TypeError, ValueError):
            names = []
        row["author_names"] = (
            ", ".join(name for name in names if isinstance(name, str))
            if isinstance(names, list)
            else ""
        )
    url_filters = {key: value for key, value in filters.items() if value and value != "recent"}

    def page_url(number: int) -> str:
        return "/notes?" + urlencode({**url_filters, "page": number})

    return render(
        request,
        "notes.html",
        page="notes",
        rows=rows,
        total=total,
        current=current,
        pages=pages,
        previous_url=page_url(current - 1) if current > 1 else None,
        next_url=page_url(current + 1) if current < pages else None,
        filters=filters,
        facets=facets,
    )
