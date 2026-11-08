"""Permanent-note and MOC listing: lexical catalog (GET) and search by meaning (POST)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from zettel.web.enqueue import operation_lock
from zettel.web.rendering import error_page, render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

router = APIRouter()

_PER_PAGE = 12
_EMPTY_FILTERS = {
    "q": "",
    "kind": "",
    "source_id": "",
    "author": "",
    "origin": "",
    "tag": "",
    "sort": "",
}


def _author_names(raw: str | None) -> str:
    try:
        names = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ""
    if not isinstance(names, list):
        return ""
    return ", ".join(name for name in names if isinstance(name, str))


def _search(db: Any, filters: dict[str, str], page: int) -> tuple[list[dict], int]:
    return db.search_catalog(
        query=filters["q"],
        kind=filters["kind"],
        source_id=filters["source_id"],
        author=filters["author"],
        origin=filters["origin"],
        tag=filters["tag"],
        sort=filters["sort"] or "relevance",
        page=page,
        per_page=_PER_PAGE,
    )


@router.get("/notes", response_class=HTMLResponse)
def notes(
    request: Request,
    q: str = "",
    kind: str = "",
    source_id: str = "",
    author: str = "",
    origin: str = "",
    tag: str = "",
    sort: str = "",
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
        "tag": tag.strip()[:200],
        # Empty is the default order: relevance for a search, recent otherwise (#312).
        "sort": sort if sort in {"recent", "oldest", "title"} else "",
    }
    current = int(page) if page.isdecimal() and len(page) <= 6 and int(page) > 0 else 1
    db = service(request).db()
    try:
        facets = db.catalog_facets()
        rows, total = _search(db, filters, current)
        pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
        if current > pages:
            current = pages
            rows, _ = _search(db, filters, current)
    finally:
        db.close()
    for row in rows:
        row["author_names"] = _author_names(row["authors"])
    url_filters = {key: value for key, value in filters.items() if value}

    def page_url(number: int) -> str:
        return "/notes?" + urlencode({**url_filters, "page": number})

    return _page(
        request,
        filters=filters,
        facets=facets,
        rows=rows,
        total=total,
        current=current,
        pages=pages,
        previous_url=page_url(current - 1) if current > 1 else None,
        next_url=page_url(current + 1) if current < pages else None,
    )


def _page(request: Request, *, status_code: int = 200, **values: Any):
    """``notes.html`` with the defaults the template expects for both modes."""
    context = {
        "filters": dict(_EMPTY_FILTERS),
        "rows": [],
        "total": 0,
        "current": 1,
        "pages": 1,
        "previous_url": None,
        "next_url": None,
        "semantic": None,
        "semantic_query": "",
        "semantic_graph": False,
        "error": None,
        **values,
    }
    if "facets" not in context:
        db = service(request).db()
        try:
            context["facets"] = db.catalog_facets()
        finally:
            db.close()
    from zettel.web.preconditions import blocker

    # Same rule as the handler: the button is enabled iff the POST is accepted (#314).
    context["semantic_blocker"] = blocker(service(request).cfg, "semantic_search")
    return render(request, "notes.html", page="notes", status_code=status_code, **context)


def _semantic_rows(db: Any, notes: list[Any]) -> list[dict]:
    """Display rows for retrieved notes: thesis, source and why each one came back."""
    from zettel.note_content import note_sections

    by_id = db.get_notes_by_ids([n.note_id for n in notes])
    sources: dict[str, dict | None] = {}
    rows = []
    for hit in notes:
        row = by_id.get(hit.note_id) or {}
        source_id = row.get("source_id")
        if source_id and source_id not in sources:
            sources[source_id] = db.get_source(source_id)
        source = sources.get(source_id) if source_id else None
        thesis = note_sections(row.get("title") or "", row.get("body") or "")["thesis"]
        rows.append(
            {
                "note_id": hit.note_id,
                "title": row.get("title") or hit.title or hit.note_id,
                "thesis": thesis,
                "source_id": source_id,
                "source_title": (source or {}).get("title"),
                "similarity": hit.similarity,
                "bm25_rank": hit.bm25_rank,
                "hop": hit.hop,
                "via": hit.via,
                "origin": hit.origin,
                "floor_reason": hit.floor_reason,
            }
        )
    return rows


@router.post("/notes/semantica", response_class=HTMLResponse)
def notes_semantic(
    request: Request,
    csrf: str = Form(""),
    q: str = Form(""),
    graph: str = Form(""),
):
    """Hybrid search (dense + BM25, relevance floor) over the permanent notes.

    A POST with CSRF, like the catalog: the query embedding is paid, and a GET
    can be fired by a prefetch or an outside link (#315).
    """
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return error_page(request, 403, "CSRF inválido")
    query = q.strip()[:500]
    expand = graph in {"1", "on", "true"}
    keep = {"semantic_query": query, "semantic_graph": expand}
    if not query:
        return _page(request, status_code=400, error="Informe o que procurar.", **keep)
    from zettel.web.preconditions import blocker

    cfg = service(request).cfg
    reason = blocker(cfg, "semantic_search")
    if reason:
        return _page(request, status_code=409, error=reason, **keep)
    from zettel.index import VectorIndex, index_kwargs
    from zettel.oplock import LockBusy
    from zettel.retrieval import Retriever

    # Chroma is not process-safe: hold the operation lock for the read (#308).
    try:
        lock = operation_lock(request, "semantic_search")
    except LockBusy as busy:
        message = f"O índice está em uso. {busy} Tente de novo quando ela terminar."
        return _page(request, status_code=409, error=message, **keep)
    db = service(request).db()
    try:
        idx = VectorIndex(**index_kwargs(cfg))
        result = Retriever(cfg, db, idx).search_notes(query, expand_graph=expand)
        below = [c for c in result.candidates if not c.passed_floor]
        semantic = {
            "hits": _semantic_rows(db, result.hits),
            "below": _semantic_rows(db, below),
        }
        facets = db.catalog_facets()
    finally:
        db.close()
        lock.release()
    return _page(request, semantic=semantic, facets=facets, **keep)
