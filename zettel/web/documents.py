"""Inbox, upload, harvest, run-all."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from zettel.hashing import file_sha256
from zettel.web.enqueue import post_job
from zettel.web.health import llm_ready as _llm_ready
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

router = APIRouter()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}


def _file_needs_harvest(db: Any, file_path: Path) -> bool:
    """Return whether a file is new, changed, or incompletely ingested."""
    record = db.get_file(str(file_path.resolve()))
    if not record or not record.get("source_id"):
        return True
    source_id = record["source_id"]
    source = db.get_source(source_id)
    if not source or source.get("processing_status") != "completed":
        return True
    from zettel.harvester import source_chunking_incomplete

    if source_chunking_incomplete(db, source_id):
        return True
    try:
        return file_sha256(file_path) != record.get("file_checksum")
    except OSError:
        return True


def _list_pending_inbox(db: Any, cfg: Any) -> list[dict[str, Any]]:
    """List inbox files that still need a harvest or a retry."""
    if not cfg.inbox_path.exists():
        return []
    pending = []
    for file_path in sorted(cfg.inbox_path.rglob("*")):
        if file_path.is_file() and file_path.suffix.lower() in ALLOWED_EXTENSIONS:
            if _file_needs_harvest(db, file_path):
                pending.append(
                    {
                        "name": file_path.name,
                        "relative": file_path.relative_to(cfg.inbox_path).as_posix(),
                        "size": file_path.stat().st_size,
                    }
                )
    return pending


def _documents_page(request: Request, *, status_code: int = 200, error: str | None = None):
    svc = service(request)
    cfg = svc.cfg
    db = svc.db()
    try:
        sources = db.list_sources()
        inbox = _list_pending_inbox(db, cfg)
    finally:
        db.close()
    return render(
        request,
        "documents.html",
        page="documents",
        sources=sources,
        inbox=inbox,
        llm_ready=_llm_ready(cfg),
        error=error,
        status_code=status_code,
    )


@router.get("/documents", response_class=HTMLResponse)
async def documents(request: Request):
    if not authenticated(request):
        return redirect_login()
    return _documents_page(request)


@router.post("/documents/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...), csrf: str = Form("")):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    original_name = file.filename or ""
    name = Path(original_name).name
    suffix = Path(name).suffix.lower()
    if (
        not name
        or name in {".", ".."}
        or name != original_name
        or "/" in original_name
        or "\\" in original_name
        or suffix not in ALLOWED_EXTENSIONS
        or len(name) > 180
        or re.fullmatch(r"[\w .()\-]+", name, flags=re.UNICODE) is None
    ):
        return _documents_page(
            request, error="Use um arquivo PDF, Markdown ou TXT com nome válido.", status_code=400
        )
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        return _documents_page(request, error="O arquivo está vazio.", status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return _documents_page(
            request, error="O arquivo excede o limite de 25 MB.", status_code=413
        )
    cfg = service(request).cfg
    destination = (cfg.inbox_path / name).resolve()
    try:
        destination.relative_to(cfg.inbox_path.resolve())
    except ValueError:
        return HTMLResponse("Nome de arquivo inválido", status_code=400)
    if destination.exists():
        return _documents_page(
            request, error="Já existe um arquivo com esse nome no inbox.", status_code=409
        )
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return RedirectResponse("/documents", status_code=303)


@router.post("/documents/harvest")
async def harvest(
    request: Request,
    selected_file: str = Form(""),
    duplicate_action: str = Form("skip"),
    paging_mode: str = Form("auto"),
    skip_biblio: str = Form(""),
    skip_paging: str = Form(""),
    dump_chunks: str = Form(""),
    dump_extraction: str = Form(""),
    content_start_file: int | None = Form(None),
    content_start_book: int | None = Form(None),
    csrf: str = Form(""),
):
    if not authenticated(request):
        return RedirectResponse("/login", status_code=303)
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    if not selected_file.strip():
        return HTMLResponse("Selecione um documento pendente antes de processar.", status_code=400)
    cfg = service(request).cfg
    relative = Path(selected_file)
    if relative.is_absolute() or ".." in relative.parts or "\\" in selected_file:
        return HTMLResponse("Seleção inválida", status_code=400)
    selected = (cfg.inbox_path / relative).resolve()
    try:
        selected.relative_to(cfg.inbox_path.resolve())
    except ValueError:
        return HTMLResponse("Arquivo inválido", status_code=400)
    if not selected.is_file() or selected.suffix.lower() not in ALLOWED_EXTENSIONS:
        return HTMLResponse("Arquivo não encontrado", status_code=404)
    db = service(request).db()
    try:
        if not _file_needs_harvest(db, selected):
            return HTMLResponse(
                "Este documento já foi processado e não precisa de novo harvest.",
                status_code=409,
            )
    finally:
        db.close()
    if duplicate_action not in {"skip", "continue", "abort"}:
        return HTMLResponse("Escolha uma ação válida para duplicatas.", status_code=400)
    if paging_mode not in {"auto", "manual", "first"}:
        return HTMLResponse("Escolha um modo de paginação válido.", status_code=400)
    if any(
        value not in {"", "1", "on", "true"}
        for value in (skip_biblio, skip_paging, dump_chunks, dump_extraction)
    ):
        return HTMLResponse("Opção de processamento inválida.", status_code=400)
    if skip_paging:
        if paging_mode == "manual":
            return HTMLResponse("Escolha apenas um modo de paginação.", status_code=400)
        paging_mode = "first"
    if selected.suffix.lower() != ".pdf" and (
        paging_mode != "auto" or content_start_file is not None or content_start_book is not None
    ):
        return HTMLResponse("Paginação manual está disponível apenas para PDF.", status_code=400)
    if paging_mode == "manual":
        if (
            content_start_file is None
            or content_start_book is None
            or content_start_file < 1
            or content_start_book < 1
        ):
            return HTMLResponse(
                "Informe as duas páginas com valores a partir de 1.", status_code=400
            )
    elif content_start_file is not None or content_start_book is not None:
        return HTMLResponse("Remova as páginas manuais para usar este modo.", status_code=400)
    from zettel.chunk_dump import default_dump_dir as chunk_dump_dir
    from zettel.extraction_dump import default_dump_dir as extraction_dump_dir

    return post_job(
        request,
        "harvest",
        {
            "selected_file": str(selected),
            "duplicate_action": duplicate_action,
            "skip_biblio": bool(skip_biblio),
            "skip_paging": paging_mode == "first",
            "content_start_file": content_start_file if paging_mode == "manual" else None,
            "content_start_book": content_start_book if paging_mode == "manual" else None,
            "dump_dir": str(chunk_dump_dir(cfg)) if dump_chunks else None,
            "extraction_dump_dir": str(extraction_dump_dir(cfg)) if dump_extraction else None,
        },
        csrf,
    )


@router.post("/documents/run-all")
async def documents_run_all(request: Request, csrf: str = Form("")):
    """Queue a safe, non-interactive execution of every pipeline phase."""
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    if not _llm_ready(service(request).cfg):
        return HTMLResponse(
            "O provedor LLM não possui credencial configurada. Verifique Configuração / saúde.",
            status_code=409,
        )
    return post_job(
        request,
        "run_all",
        {"duplicate_action": "skip", "skip_biblio": False, "skip_paging": False},
        csrf,
    )
