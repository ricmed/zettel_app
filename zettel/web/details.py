"""Parametric detail pages. Registered last so ``/notes/new`` is not captured."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from zettel.markdown import render_markdown
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


def _decorate_connections(db: Any, note_id: str) -> list[dict]:
    """Add the related note title and existence flag for the detail template."""
    decorated = []
    for edge in db.get_note_connections(note_id):
        related_id = (
            edge["target_note_id"] if edge["source_note_id"] == note_id else edge["source_note_id"]
        )
        related_note = db.get_note(related_id)
        decorated.append(
            {
                **edge,
                "related_note_id": related_id,
                "related_title": (
                    related_note.get("title") or "Nota sem título"
                    if related_note
                    else "Nota não encontrada"
                ),
                "related_note_exists": related_note is not None,
            }
        )
    return decorated


@router.get("/sources/{source_id}", response_class=HTMLResponse)
async def source_detail(request: Request, source_id: str):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        source = db.get_source(source_id)
        chunks = db.get_chunks_for_source(source_id) if source else []
    finally:
        db.close()
    if not source:
        return HTMLResponse("Fonte não encontrada", status_code=404)
    return render(request, "source_detail.html", page="documents", source=source, chunks=chunks)


@router.get("/notes/{note_id}", response_class=HTMLResponse)
async def note_detail(request: Request, note_id: str):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        note = db.get_note(note_id)
        connections = _decorate_connections(db, note_id) if note else []
    finally:
        db.close()
    if not note:
        return HTMLResponse("Nota não encontrada", status_code=404)
    return render(
        request,
        "note_detail.html",
        page="notes",
        note=note,
        rendered_body=render_markdown(note.get("body")),
        connections=connections,
    )


@router.get("/mocs/{moc_id}", response_class=HTMLResponse)
async def moc_detail(request: Request, moc_id: str):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        moc = db.get_moc(moc_id)
    finally:
        db.close()
    if not moc:
        return HTMLResponse("MOC não encontrado", status_code=404)
    return render(
        request,
        "moc_detail.html",
        page="notes",
        moc=moc,
        rendered_body=render_markdown(moc.get("body")),
    )
