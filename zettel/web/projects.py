"""The Mesa (ADR-067): writing projects whose cards are permanent notes.

Every change is a form POST with CSRF, taken under the operation lock and
checked against the file version the page was rendered from, so an edit made
in Obsidian meanwhile is never overwritten (409 instead).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData

from zettel.markdown import render_markdown
from zettel.web.enqueue import operation_lock
from zettel.web.forms import read_form
from zettel.web.rendering import error_page, render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

router = APIRouter()

_MAX_NOTES_PER_ADD = 200
_MAX_TEXT = 20_000


def _int(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdecimal() and len(text) <= 6 else None


def _note_ids(form: FormData) -> list[str]:
    ids = [str(v).strip() for v in form.getlist("note_ids") if str(v).strip()]
    return list(dict.fromkeys(ids))[:_MAX_NOTES_PER_ADD]


def _redirect(project_id: str, anchor: str = "") -> RedirectResponse:
    target = f"/projetos/{project_id}" + (f"#{anchor}" if anchor else "")
    return RedirectResponse(target, status_code=303)


@router.get("/projetos", response_class=HTMLResponse)
def projects_list(request: Request):
    if not authenticated(request):
        return redirect_login()
    from zettel.projects import list_projects

    desks = []
    for project in list_projects(service(request).cfg):
        cards = project.body.note_ids()
        desks.append(
            {
                "project_id": project.project_id,
                "title": project.title,
                "style": project.style,
                "sections": sum(1 for s in project.body.sections if s.heading is not None),
                "cards": len(cards),
                "updated_at": project.meta.get("updated_at"),
            }
        )
    return render(request, "projects.html", page="projects", desks=desks)


@router.post("/projetos")
def project_create(
    request: Request,
    csrf: str = Form(""),
    title: str = Form(""),
    style: str = Form("academic"),
):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return error_page(request, 403, "CSRF inválido")
    if not title.strip():
        return error_page(request, 400, "Dê um título à mesa.")
    from zettel.oplock import LockBusy
    from zettel.projects import create_project

    try:
        lock = operation_lock(request, "mesa")
    except LockBusy as busy:
        return error_page(request, 409, f"O vault está em uso. {busy}")
    try:
        project = create_project(service(request).cfg, title, style)
    finally:
        lock.release()
    return _redirect(project.project_id)


@router.post("/projetos/adicionar")
def project_add_from_elsewhere(request: Request, form: FormData = Depends(read_form)):
    """Cards picked on another page land in the desk's "Para organizar" section."""
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    project_id = str(form.get("projeto") or "")
    note_ids = _note_ids(form)
    if not note_ids:
        return error_page(request, 400, "Selecione ao menos uma nota.")
    return _edit(request, project_id, None, lambda body, db: _add_to_inbox(body, db, note_ids))


def _add_to_inbox(body: Any, db: Any, note_ids: list[str]) -> str:
    from zettel.project_file import INBOX_SECTION, add_to_inbox, find_section
    from zettel.projects import note_links

    add_to_inbox(body, note_links(db, note_ids))
    return f"secao-{find_section(body, INBOX_SECTION)}"


def _edit(request: Request, project_id: str, version: str | None, change):
    """Load, change and save one project under the lock; ``change`` returns an anchor."""
    from zettel.oplock import LockBusy
    from zettel.project_file import ProjectEditError
    from zettel.projects import ProjectConflict, ProjectNotFound, load_project, save_project

    cfg = service(request).cfg
    try:
        lock = operation_lock(request, "mesa")
    except LockBusy as busy:
        return error_page(request, 409, f"O vault está em uso. {busy}")
    db = service(request).db()
    try:
        project = load_project(cfg, project_id)
        anchor = change(project.body, db)
        save_project(cfg, project, expected_version=version)
    except ProjectNotFound:
        return error_page(request, 404, "Mesa não encontrada.")
    except ProjectConflict:
        return error_page(
            request,
            409,
            "A mesa mudou desde que esta página foi aberta (talvez no Obsidian). "
            "Recarregue a mesa e refaça a alteração.",
        )
    except ProjectEditError as exc:
        return error_page(request, 400, str(exc))
    finally:
        db.close()
        lock.release()
    return _redirect(project.project_id, anchor or "")


def _change_for(form: FormData):
    """The edit an ``acao`` asks for, as a function of the body (and the database)."""
    from zettel import project_file as pf

    action = str(form.get("acao") or "")
    section = _int(form.get("secao"))
    block = _int(form.get("bloco"))
    text = str(form.get("texto") or "")[:_MAX_TEXT]
    title = str(form.get("titulo") or "")[:200]

    def need(*values: int | None) -> None:
        if any(v is None for v in values):
            raise pf.ProjectEditError("Item inválido.")

    def mover(body, db):
        need(section, block)
        target = _int(form.get("destino"))
        if target is not None:
            s, _ = pf.place_block(body, section, block, target, _int(form.get("antes")))
        else:
            s, _ = pf.move_block(body, section, block, str(form.get("direcao") or ""))
        return f"secao-{s}"

    def remover(body, db):
        need(section, block)
        pf.remove_block(body, section, block)
        return f"secao-{section}"

    def anotacao(body, db):
        need(section, block)
        pf.set_annotation(body, section, block, text)
        return f"secao-{section}"

    def texto(body, db):
        need(section, block)
        pf.set_text(body, section, block, text)
        return f"secao-{section}"

    def texto_novo(body, db):
        need(section)
        pf.add_text(body, section, text, after=block)
        return f"secao-{section}"

    def secao_criar(body, db):
        return f"secao-{pf.add_section(body, title)}"

    def secao_renomear(body, db):
        need(section)
        pf.rename_section(body, section, title)
        return f"secao-{section}"

    def secao_mover(direction: str):
        def apply(body, db):
            need(section)
            return f"secao-{pf.move_section(body, section, direction)}"

        return apply

    def secao_excluir(body, db):
        need(section)
        pf.delete_section(body, section)
        return f"secao-{section - 1}"

    def itens(body, db):
        from zettel.projects import note_links

        need(section)
        pf.add_cards(body, section, note_links(db, _note_ids(form)))
        return f"secao-{section}"

    actions = {
        "mover": mover,
        "remover": remover,
        "anotacao": anotacao,
        "texto": texto,
        "texto-novo": texto_novo,
        "secao-criar": secao_criar,
        "secao-renomear": secao_renomear,
        "secao-subir": secao_mover("up"),
        "secao-descer": secao_mover("down"),
        "secao-excluir": secao_excluir,
        "itens": itens,
    }
    change = actions.get(action)
    if change is None:
        raise pf.ProjectEditError("Ação desconhecida.")
    return action, change


@router.post("/projetos/{project_id}")
def project_edit(request: Request, project_id: str, form: FormData = Depends(read_form)):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    from zettel.project_file import ProjectEditError

    try:
        action, change = _change_for(form)
    except ProjectEditError as exc:
        return error_page(request, 400, str(exc))
    # Adding cards only appends, so it needs no version: a suggestion stays
    # usable after another tab edited the desk.
    version = None if action == "itens" else str(form.get("versao") or "")
    return _edit(request, project_id, version, change)


def _desk_view(cfg: Any, db: Any, project: Any) -> list[dict]:
    """Sections with their visible blocks (blank lines hidden) and card data."""
    from zettel.project_file import Card
    from zettel.projects import card_views

    views = card_views(cfg, db, project.body.note_ids())
    sections = []
    for s_index, section in enumerate(project.body.sections):
        blocks = []
        for b_index, block in enumerate(section.blocks):
            if isinstance(block, Card):
                blocks.append(
                    {
                        "kind": "card",
                        "index": b_index,
                        "note": views[block.note_id],
                        "annotation": block.annotation_text,
                    }
                )
            elif not block.is_blank:
                blocks.append(
                    {
                        "kind": "text",
                        "index": b_index,
                        "content": block.content,
                        "html": render_markdown(block.content),
                    }
                )
        if section.heading is None and not blocks:
            continue
        sections.append(
            {
                "index": s_index,
                "title": section.title,
                "intro": section.heading is None,
                "blocks": blocks,
            }
        )
    return sections


def _talk_view(talk: Any, section_titles: dict[int, str]) -> dict:
    """The conversation with titles resolved, ready for the side panel."""

    def link(item: Any) -> dict:
        return {
            "desk_note": item.desk_note,
            "desk_title": talk.titles.get(item.desk_note, item.desk_note),
            "other_note": item.other_note,
            "other_title": talk.titles.get(item.other_note, item.other_note),
            "other_thesis": talk.theses.get(item.other_note, ""),
            "label": item.label,
            "relation_type": item.relation_type,
            "description": item.description,
        }

    return {
        "tensions": [link(t) for t in talk.tensions],
        "frontier": [
            {**link(n.best), "links": len(n.links), "weight": n.weight} for n in talk.frontier
        ],
        "corroborations": [link(c) for c in talk.corroborations],
        "isolated": [
            {
                "section": section_titles.get(i, ""),
                "note_id": nid,
                "title": talk.titles.get(nid, nid),
            }
            for i, nid in talk.isolated
        ],
        "single_source": [
            {"section": section_titles.get(i, ""), "source_id": sid}
            for i, sid in talk.single_source
        ],
        "empty": talk.empty,
    }


def _desk_page(
    request: Request,
    project_id: str,
    *,
    status_code: int = 200,
    section_search: dict | None = None,
    error: str | None = None,
):
    from zettel.project_suggest import converse
    from zettel.projects import ProjectNotFound, load_project
    from zettel.web.preconditions import blocker

    cfg = service(request).cfg
    try:
        project = load_project(cfg, project_id)
    except ProjectNotFound:
        return error_page(request, 404, "Mesa não encontrada.")
    db = service(request).db()
    try:
        sections = _desk_view(cfg, db, project)
        talk = converse(cfg, db, project.body)
    finally:
        db.close()
    targets = [s for s in sections if not s["intro"]]
    return render(
        request,
        "project.html",
        status_code=status_code,
        page="projects",
        project=project,
        sections=sections,
        targets=targets,
        talk=_talk_view(talk, {s["index"]: s["title"] for s in targets}),
        section_search=section_search,
        search_blocker=blocker(cfg, "desk_search"),
        error=error,
    )


@router.get("/projetos/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: str):
    if not authenticated(request):
        return redirect_login()
    return _desk_page(request, project_id)


@router.post("/projetos/{project_id}/buscar", response_class=HTMLResponse)
def project_section_search(
    request: Request, project_id: str, csrf: str = Form(""), secao: str = Form("")
):
    """Notes that fit a section and are not on the desk yet (one query embedding)."""
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return error_page(request, 403, "CSRF inválido")
    from zettel.index import VectorIndex, index_kwargs
    from zettel.oplock import LockBusy
    from zettel.project_suggest import section_query
    from zettel.projects import ProjectNotFound, card_views, load_project
    from zettel.retrieval import Retriever
    from zettel.web.preconditions import blocker

    cfg = service(request).cfg
    reason = blocker(cfg, "desk_search")
    if reason:
        return _desk_page(request, project_id, status_code=409, error=reason)
    try:
        project = load_project(cfg, project_id)
    except ProjectNotFound:
        return error_page(request, 404, "Mesa não encontrada.")
    section = _int(secao)
    if section is None or not 0 < section < len(project.body.sections):
        return error_page(request, 400, "Escolha uma seção.")
    try:
        lock = operation_lock(request, "desk_search")
    except LockBusy as busy:
        message = f"O índice está em uso. {busy} Tente de novo quando ela terminar."
        return _desk_page(request, project_id, status_code=409, error=message)
    db = service(request).db()
    try:
        desk = project.body.note_ids()
        theses = {nid: view.thesis for nid, view in card_views(cfg, db, desk).items()}
        query = section_query(project.body, section, theses, project.title)
        result = Retriever(cfg, db, VectorIndex(**index_kwargs(cfg))).search_notes(query)
        found = [hit.note_id for hit in result.hits if hit.note_id not in set(desk)]
        views = card_views(cfg, db, found)
    finally:
        db.close()
        lock.release()
    search = {
        "section": section,
        "title": project.body.sections[section].title,
        "hits": [views[nid] for nid in found if views[nid].exists],
    }
    return _desk_page(request, project_id, section_search=search)
