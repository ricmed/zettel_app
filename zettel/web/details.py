"""Parametric detail pages. Registered last so ``/notes/new`` is not captured."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from zettel.markdown import render_markdown
from zettel.web.enqueue import post_job
from zettel.web.mesa import desk_choices
from zettel.web.rendering import error_page, render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


def _markdown_path(vault_path: Path, raw: str | None, folder: str) -> Path | None:
    """Only serve existing Markdown files from the indexed note's vault folder."""
    if not raw:
        return None
    vault = vault_path.resolve()
    candidate = Path(raw)
    possible = [candidate] if candidate.is_absolute() else [candidate, vault / candidate]
    for path in possible:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if (
            resolved.is_file()
            and resolved.suffix.lower() == ".md"
            and resolved.is_relative_to(vault / folder)
        ):
            return resolved
    return None


def _markdown_response(request: Request, row: dict | None, folder: str, download: bool):
    if not row:
        return error_page(request, 404, "Nota não encontrada")
    path = _markdown_path(service(request).cfg.vault_path, row.get("path"), folder)
    if path is None:
        return error_page(request, 404, "Arquivo Markdown não encontrado no vault")
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if download:
        return FileResponse(path, media_type="text/markdown", filename=path.name, headers=headers)
    try:
        content = path.read_bytes()
    except OSError:
        return error_page(request, 404, "Arquivo Markdown indisponível")
    return Response(content=content, media_type="text/markdown; charset=utf-8", headers=headers)


def _connection_groups(db: Any, note_id: str) -> dict[str, list[dict]]:
    """Outgoing and incoming edges of a note, grouped by relation; rejected ones hidden.

    Each group is ``{"label", "relation_type", "edges"}`` with the related
    note's title and whether it still exists, read in one query.
    """
    from zettel.connector.links import inverse_relation, relation_label

    edges = [e for e in db.get_note_connections(note_id) if e.get("status") != "rejected"]
    related_ids = [
        e["target_note_id"] if e["source_note_id"] == note_id else e["source_note_id"]
        for e in edges
    ]
    related = db.get_notes_by_ids(related_ids)
    groups: dict[str, dict[str, dict]] = {"outgoing": {}, "incoming": {}}
    for edge, related_id in zip(edges, related_ids, strict=True):
        relation = edge.get("relation_type") or "related"
        outgoing = edge["source_note_id"] == note_id
        direction = "outgoing" if outgoing else "incoming"
        label = relation_label(relation) if outgoing else inverse_relation(relation)
        group = groups[direction].setdefault(
            relation, {"label": label, "relation_type": relation, "edges": []}
        )
        row = related.get(related_id)
        group["edges"].append(
            {
                **edge,
                "related_note_id": related_id,
                "related_title": (row.get("title") or "Nota sem título")
                if row
                else "Nota não encontrada",
                "related_note_exists": row is not None,
            }
        )
    return {
        direction: sorted(by_relation.values(), key=lambda g: g["label"])
        for direction, by_relation in groups.items()
    }


def _note_source(db: Any, note: dict) -> dict | None:
    """The note's source, its ABNT reference and the note's own citation (ADR-051)."""
    from zettel.citation import load_provenance

    source = db.get_source(note["source_id"]) if note.get("source_id") else None
    provenance = load_provenance(note)
    if not source and not provenance.get("citation"):
        return None
    return {
        "source_id": note.get("source_id"),
        "title": (source or {}).get("title"),
        "reference": (source or {}).get("abnt_reference"),
        "citation": provenance.get("citation"),
        "anchor_quote": provenance.get("anchor_quote"),
    }


@router.get("/notes/{note_id}/markdown")
def note_markdown(request: Request, note_id: str, download: bool = False):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        row = db.get_note(note_id)
    finally:
        db.close()
    return _markdown_response(request, row, "30_Permanent", download)


@router.get("/mocs/{moc_id}/markdown")
def moc_markdown(request: Request, moc_id: str, download: bool = False):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        row = db.get_moc(moc_id)
    finally:
        db.close()
    return _markdown_response(request, row, "40_MOCs", download)


@router.post("/mocs/{moc_id}/adopt")
def moc_adopt(request: Request, moc_id: str, csrf: str = Form("")):
    """Hand a generated MOC to the user (#300); runs as a job so no garden overlaps it."""
    return post_job(request, "adopt_moc", {"moc_id": moc_id}, csrf)


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(request: Request, source_id: str):
    if not authenticated(request):
        return redirect_login()
    cfg = service(request).cfg
    db = service(request).db()
    try:
        source = db.get_source(source_id)
        chunks = db.get_chunks_for_source(source_id) if source else []
        book = db.get_book_by_source(source_id) if source else None
    finally:
        db.close()
    if not source:
        return error_page(request, 404, "Fonte não encontrada")
    return render(
        request,
        "source_detail.html",
        page="documents",
        source=source,
        chunks=chunks,
        book=book,
        # Uma fonte de livro sem LIV pode ganhar a sua aqui (o código nasce na LIV).
        can_shelve=source.get("document_type") in cfg.library.document_types,
        initial_status=cfg.library.initial_status,
    )


@router.get("/notes/{note_id}", response_class=HTMLResponse)
def note_detail(request: Request, note_id: str):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        note = db.get_note(note_id)
        if note:
            connections = _connection_groups(db, note_id)
            source = _note_source(db, note)
            mocs = db.list_mocs_listing_note(note_id)
    finally:
        db.close()
    if not note:
        return error_page(request, 404, "Nota não encontrada")
    return render(
        request,
        "note_detail.html",
        page="notes",
        note=note,
        rendered_body=render_markdown(note.get("body")),
        connections=connections,
        source=source,
        mocs=mocs,
        desks=desk_choices(request),
    )


@router.get("/mocs/{moc_id}", response_class=HTMLResponse)
def moc_detail(request: Request, moc_id: str):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        moc = db.get_moc(moc_id)
    finally:
        db.close()
    if not moc:
        return error_page(request, 404, "MOC não encontrado")
    from zettel.vault import ZTL_LINK_RE

    entries = list(dict.fromkeys(ZTL_LINK_RE.findall(moc.get("body") or "")))
    return render(
        request,
        "moc_detail.html",
        page="notes",
        moc=moc,
        rendered_body=render_markdown(moc.get("body")),
        entries=entries,
        desks=desk_choices(request),
    )
