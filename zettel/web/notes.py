"""Permanent-note and MOC listing."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from zettel.web.rendering import render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


@router.get("/notes", response_class=HTMLResponse)
async def notes(request: Request):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        note_rows = db.list_notes()
        moc_rows = db.list_mocs()
    finally:
        db.close()
    return render(request, "notes.html", page="notes", notes=note_rows, mocs=moc_rows)
