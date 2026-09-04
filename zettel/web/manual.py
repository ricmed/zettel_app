"""GET/POST /notes/new — manual SRC / LIT / ZTL scaffolds and LIT→ZTL jobs."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi import APIRouter

from zettel.web.enqueue import post_job
from zettel.web.health import llm_ready as _llm_ready
from zettel.web.manual_form import (
    PreflightConflict,
    parse,
    preflight_from_lit,
    resolve_from_lit,
)
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

router = APIRouter()

_VALID_TYPES = {"SRC", "LIT", "ZTL"}


def _source_label(row: dict) -> str:
    citekey = row.get("citekey") or ""
    title = (row.get("title") or "").strip()
    year = row.get("year")
    parts = [citekey]
    if title:
        parts.append(title)
    label = " · ".join(parts)
    if year:
        label = f"{label} ({year})"
    return label


def _lit_label(row: dict) -> str:
    index = row.get("chunk_index")
    token = f"{int(index):04d}" if index is not None else "?"
    page = row.get("page_in_book")
    page_bit = f"p. {int(page)}" if page is not None else ""
    section = (row.get("section_path") or row.get("locator") or "").strip()
    bits = [token]
    if page_bit:
        bits.append(page_bit)
    if section:
        bits.append(section)
    return " · ".join(bits)


def _picker_sources(db: Any) -> list[dict]:
    items = []
    for row in db.search_sources("", 50):
        items.append({
            "id": row["source_id"],
            "label": _source_label(row),
            "next_chunk_index": db.next_manual_chunk_index(row["source_id"]),
        })
    return items


def _picker_literature(db: Any, source_id: str | None) -> list[dict]:
    if not source_id:
        return []
    return [
        {"id": row["chunk_id"], "label": _lit_label(row)}
        for row in db.search_literature_chunks("", source_id=source_id, limit=50)
    ]


def _clean_type(raw: str) -> str:
    value = (raw or "").upper()
    return value if value in _VALID_TYPES else "SRC"


def _accepted_source(db: Any, source_id: str | None) -> str | None:
    if not source_id:
        return None
    row = db.get_source(source_id)
    return row["source_id"] if row else None


def _document_type_options() -> list[dict]:
    from zettel.bibliography import DOCUMENT_TYPE_LABELS

    return [{"value": value, "label": label} for value, label in DOCUMENT_TYPE_LABELS.items()]


def _form_page(
    request: Request, db: Any, *, status_code: int = 200, error: str | None = None,
    result: Any = None, next_step: dict | None = None, selected: dict | None = None,
):
    selected = selected or {}
    source_id = selected.get("source_id")
    cfg = service(request).cfg
    return render(
        request, "manual_notes.html", status_code=status_code, page="manual-notes",
        recent_sources=_picker_sources(db),
        recent_literature=_picker_literature(db, source_id),
        document_types=_document_type_options(),
        selected_document_type=selected.get("document_type") or "",
        llm_ready=_llm_ready(cfg),
        result=result, error=error, next_step=next_step,
        selected_type=selected.get("type") or "SRC",
        selected_source_id=source_id or "",
        selected_from_lit=selected.get("from_lit") or "",
        selected_from_lit_path=selected.get("from_lit_path") or "",
        selected_chunk_index=selected.get("chunk_index"),
        selected_ztl_origin=selected.get("ztl_origin") or "blank",
        selected_granular=selected.get("granular") or "",
        default_chunk_index=selected.get("chunk_index") or (
            db.next_manual_chunk_index(source_id) if source_id else 1
        ),
    )


def _next_step(cfg: Any, db: Any, result: Any) -> dict | None:
    meta = result.meta or {}
    chunk_id = meta.get("chunk_id")
    if not chunk_id:
        return None
    try:
        rel = result.path.relative_to(cfg.vault_path).as_posix()
    except ValueError:
        rel = result.path.name
    source_id = meta.get("source_id") or ""
    return {
        "chunk_id": chunk_id,
        "rel_path": rel,
        "source_id": source_id,
        "source_in_db": db.get_source(source_id) is not None if source_id else False,
        "sync_next": f"/notes/new?type=ZTL&from_lit={chunk_id}",
        "direct_href": f"/notes/new?type=ZTL&from_lit_path={rel}",
    }


@router.get("/notes/new", response_class=HTMLResponse)
async def new_note(
    request: Request,
    type: str = "SRC",
    source_id: str = "",
    from_lit: str = "",
    from_lit_path: str = "",
    chunk_index: str = "",
):
    if not authenticated(request):
        return redirect_login()
    svc = service(request)
    db = svc.db()
    try:
        note_type = _clean_type(type)
        accepted = _accepted_source(db, source_id or None)
        chunk = db.get_chunk(from_lit) if from_lit else None
        if chunk and not chunk.get("literature_note_path"):
            chunk = None
        if chunk and not accepted:
            accepted = chunk.get("source_id")
        ztl_origin = "from_lit" if (chunk or from_lit_path) and note_type == "ZTL" else "blank"
        if from_lit_path and (".." in from_lit_path or from_lit_path.startswith("/") or ":\\" in from_lit_path):
            from_lit_path = ""
        if (chunk or from_lit_path) and note_type != "LIT":
            note_type = "ZTL"
        index: int | None = None
        if chunk_index.isdigit():
            index = int(chunk_index)
        elif accepted and note_type == "LIT":
            index = db.next_manual_chunk_index(accepted)
        granular = "1" if note_type == "LIT" and chunk_index else ""
        return _form_page(request, db, selected={
            "type": note_type,
            "source_id": accepted,
            "from_lit": chunk["chunk_id"] if chunk else "",
            "from_lit_path": from_lit_path,
            "chunk_index": index,
            "ztl_origin": ztl_origin,
            "granular": granular,
        })
    finally:
        db.close()


@router.post("/notes/new", response_class=HTMLResponse)
async def create_note(request: Request):
    if not authenticated(request):
        return redirect_login()
    form = await request.form()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return HTMLResponse("CSRF inválido", status_code=403)
    svc = service(request)
    db = svc.db()
    try:
        parsed = parse(form)
        selected = {
            "type": str(form.get("note_type") or "SRC").upper(),
            "source_id": _accepted_source(db, parsed.get("source_id")),
            "from_lit": parsed.get("from_lit") or "",
            "from_lit_path": parsed.get("from_lit_path") or "",
            "chunk_index": parsed.get("chunk_index"),
            "ztl_origin": "from_lit" if parsed["mode"] == "ZTL_FROM_LIT" else "blank",
            "granular": "1" if parsed["mode"] == "LIT_GRANULAR" else "",
        }
        if parsed["mode"] in {"LIT_INDEX", "LIT_GRANULAR"}:
            if not selected["source_id"]:
                raise ValueError("Selecione uma fonte existente para a nota LIT.")
        if parsed["mode"] == "ZTL_BLANK" and parsed.get("source_id") and not selected["source_id"]:
            raise ValueError("Selecione uma fonte existente para a nota ZTL.")
        if parsed["mode"] == "ZTL_FROM_LIT":
            ref = resolve_from_lit(svc.cfg, db, parsed)
            thesis = preflight_from_lit(
                svc.cfg, db, parsed, ref, llm_ok=_llm_ready(svc.cfg),
            )
            return post_job(request, "manual-ztl-from-lit", {
                "ref": ref,
                "chunk_id": ref if not str(ref).endswith(".md") else parsed.get("from_lit") or "",
                "thesis": thesis,
                "use_llm": bool(parsed["use_llm"]),
                "force": bool(parsed["force"]) and not parsed["use_llm"],
            }, str(form.get("csrf")))
        from zettel.new_note import scaffold_manual_note
        note_type = {
            "SRC": "SRC", "LIT_INDEX": "LIT", "LIT_GRANULAR": "LIT", "ZTL_BLANK": "ZTL",
        }[parsed["mode"]]
        result = scaffold_manual_note(
            svc.cfg, note_type=note_type, title=parsed["title"],
            citekey=parsed["citekey"] if parsed["mode"] == "SRC" else None,
            authors=parsed["authors"] or None if parsed["mode"] == "SRC" else None,
            year=parsed["year"] if parsed["mode"] == "SRC" else None,
            document_type=parsed["document_type"] if parsed["mode"] == "SRC" else None,
            abnt_reference=parsed["abnt_reference"] if parsed["mode"] == "SRC" else None,
            publisher=parsed["publisher"] if parsed["mode"] == "SRC" else None,
            place=parsed["place"] if parsed["mode"] == "SRC" else None,
            doi=parsed["doi"] if parsed["mode"] == "SRC" else None,
            url=parsed["url"] if parsed["mode"] == "SRC" else None,
            journal=parsed["journal"] if parsed["mode"] == "SRC" else None,
            edition=parsed["edition"] if parsed["mode"] == "SRC" else None,
            institution=parsed["institution"] if parsed["mode"] == "SRC" else None,
            pages=parsed["pages"] if parsed["mode"] == "SRC" else None,
            thesis=parsed["title"] if parsed["mode"] == "ZTL_BLANK" else None,
            source_id=None if parsed["mode"] == "SRC" else selected["source_id"],
            granular=parsed["granular"],
            chunk_index=parsed["chunk_index"],
            page=parsed["page"],
            force=parsed["force"],
        )
        return _form_page(
            request, db, status_code=201, result=result,
            next_step=_next_step(svc.cfg, db, result), selected=selected,
        )
    except PreflightConflict as exc:
        body = str(exc)
        if exc.href:
            body = f'{body} <a href="{exc.href}">Abrir o Pipeline</a>.'
        return HTMLResponse(body, status_code=409)
    except (ValueError, FileExistsError) as exc:
        return _form_page(request, db, status_code=400, error=str(exc), selected={
            "type": str(form.get("note_type") or "SRC").upper(),
            "source_id": str(form.get("source_id") or "") or None,
            "from_lit": str(form.get("from_lit") or ""),
            "from_lit_path": str(form.get("from_lit_path") or ""),
            "ztl_origin": str(form.get("ztl_origin") or "blank"),
            "granular": str(form.get("granular") or ""),
            "document_type": str(form.get("document_type") or ""),
        })
    finally:
        db.close()
