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


@router.get("/projetos/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: str):
    if not authenticated(request):
        return redirect_login()
    from zettel.projects import ProjectNotFound, load_project

    cfg = service(request).cfg
    try:
        project = load_project(cfg, project_id)
    except ProjectNotFound:
        return error_page(request, 404, "Mesa não encontrada.")
    db = service(request).db()
    try:
        sections = _desk_view(cfg, db, project)
    finally:
        db.close()
    return render(
        request,
        "project.html",
        page="projects",
        project=project,
        sections=sections,
        targets=[s for s in sections if not s["intro"]],
    )
