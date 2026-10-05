"""Inbox, upload, harvest, run-all."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from zettel.hashing import file_sha256
from zettel.web.enqueue import post_job
from zettel.web.health import llm_ready as _llm_ready
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login, session

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
        "prepare_harvest",
        {
            "selected_file": str(selected),
            "checksum": file_sha256(selected),
            "session_hash": _session_hash(request),
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


def _session_hash(request: Request) -> str:
    """Bind a review to the signed browser session without persisting its CSRF token."""
    current = session(request)
    return hashlib.sha256(current["csrf"].encode()).hexdigest()


def _review_data(request: Request, review_id: str):
    svc = service(request)
    db = svc.db()
    try:
        job = db.get_web_job(review_id)
        review = db.get_web_harvest_review(review_id, _session_hash(request))
        if review and review["state"] == "submitted":
            review["retry_job"] = db.get_web_job(review["harvest_job_id"])
    finally:
        db.close()
    if not job or job["operation"] != "prepare_harvest" or not review:
        return None
    return job, review


def _review_error(request: Request, job: dict, review: dict) -> str | None:
    if job["state"] != "succeeded":
        return "A preparação não terminou. Consulte a execução e tente novamente."
    retry_job = review.get("retry_job")
    if review["state"] != "ready" and not (
        retry_job and retry_job["state"] in {"failed", "interrupted"}
    ):
        return "Esta revisão já foi cancelada ou enviada para processamento."
    path = Path(job["result"]["selected_file"]).resolve()
    cfg = service(request).cfg
    if (
        not path.is_file()
        or not path.is_relative_to(cfg.inbox_path.resolve())
        or path.suffix.lower() not in ALLOWED_EXTENSIONS
        or file_sha256(path) != review["file_checksum"]
    ):
        return "O arquivo mudou após a preparação. Volte a Documentos e prepare-o novamente."
    from zettel.harvester.prepared import snapshot_path

    if not snapshot_path(cfg, job["job_id"]).exists():
        return "A preparação não está mais disponível. Volte a Documentos e prepare novamente."
    return None


def _review_page(
    request: Request,
    review_id: str,
    job: dict,
    *,
    error: str | None = None,
    values: dict | None = None,
    status_code: int = 200,
):
    from zettel.bibliography import (
        DOCUMENT_TYPE_LABELS,
        FIELD_LABELS,
        REQUIRED_FIELDS,
        BibliographicMetadata,
        missing_required,
    )

    inferred = BibliographicMetadata.model_validate(job["result"]["biblio"])
    retry = _review_data(request, review_id)
    retry_job = retry[1].get("retry_job") if retry else None
    saved = (
        retry_job["payload"]
        if retry_job and retry_job["state"] in {"failed", "interrupted"}
        else {}
    )
    values = values or {**inferred.model_dump(), **saved.get("biblio", {})}
    labels = {**FIELD_LABELS, "document_type": "Tipo de documento"}
    displayed = {}
    for name in BibliographicMetadata.model_fields:
        if name in {"document_type", "confidence"}:
            continue
        value = values.get(name)
        displayed[name] = (
            "\n".join(value) if isinstance(value, list) else value if value is not None else ""
        )
    return render(
        request,
        "document_review.html",
        page="documents",
        status_code=status_code,
        review_id=review_id,
        filename=Path(job["result"]["selected_file"]).name,
        confidence=inferred.confidence,
        fields=displayed,
        field_labels=labels,
        document_types=DOCUMENT_TYPE_LABELS,
        required_fields=REQUIRED_FIELDS,
        required_by_field={
            name: [kind for kind, names in REQUIRED_FIELDS.items() if name in names]
            for name in displayed
        },
        selected_type=values.get("document_type") or "",
        initial_missing=[
            labels.get(n, n)
            for n in missing_required(BibliographicMetadata.model_validate(
                saved.get("biblio", inferred.model_dump())
            ))
        ],
        abnt_reference=values.get(
            "abnt_reference", saved.get("abnt_reference", job["result"]["abnt_reference"])
        ),
        allow_incomplete=bool(job["payload"]["skip_biblio"]),
        retry=bool(saved),
        error=error,
    )


def _parsed_biblio(form: Any, original: dict):
    from zettel.bibliography import DOCUMENT_TYPES, BibliographicMetadata

    raw_type = str(form.get("document_type") or "").strip()
    if raw_type and raw_type not in DOCUMENT_TYPES:
        raise ValueError("Selecione um tipo documental válido.")
    data = {"document_type": raw_type or None, "confidence": original["confidence"]}
    for name in BibliographicMetadata.model_fields:
        if name in {"document_type", "confidence"}:
            continue
        raw = str(form.get(name) or "").strip()
        if len(raw) > 2000:
            raise ValueError(f"O campo {name} é muito longo.")
        if name in {"authors", "chapter_authors", "book_editors"}:
            data[name] = [part.strip() for part in raw.splitlines() if part.strip()]
        elif name == "year":
            if raw and (not raw.isdigit() or not 1000 <= int(raw) <= 2100):
                raise ValueError("Informe um ano válido entre 1000 e 2100.")
            data[name] = int(raw) if raw else None
        else:
            data[name] = raw or None
    return BibliographicMetadata.model_validate(data)


@router.get("/documents/review/{review_id}", response_class=HTMLResponse)
async def document_review(request: Request, review_id: str):
    if not authenticated(request):
        return redirect_login()
    found = _review_data(request, review_id)
    if not found:
        return HTMLResponse("Revisão não encontrada para esta sessão.", status_code=404)
    job, review = found
    error = _review_error(request, job, review)
    if error:
        return HTMLResponse(error, status_code=409)
    return _review_page(request, review_id, job)


@router.post("/documents/review/{review_id}/preview")
async def document_review_preview(request: Request, review_id: str):
    if not authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    form = await request.form()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return JSONResponse({"error": "csrf"}, status_code=403)
    found = _review_data(request, review_id)
    if not found:
        return JSONResponse({"error": "Revisão não encontrada."}, status_code=404)
    job, review = found
    error = _review_error(request, job, review)
    if error:
        return JSONResponse({"error": error}, status_code=409)
    try:
        from zettel.bibliography import FIELD_LABELS, format_abnt, missing_required

        biblio = _parsed_biblio(form, job["result"]["biblio"])
        return {
            "abnt_reference": format_abnt(biblio),
            "missing": [FIELD_LABELS.get(n, n) for n in missing_required(biblio)],
        }
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.post("/documents/review/{review_id}")
async def confirm_document_review(request: Request, review_id: str):
    if not authenticated(request):
        return redirect_login()
    form = await request.form()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return HTMLResponse("CSRF inválido", status_code=403)
    found = _review_data(request, review_id)
    if not found:
        return HTMLResponse("Revisão não encontrada para esta sessão.", status_code=404)
    job, review = found
    if form.get("decision") == "cancel":
        db = service(request).db()
        try:
            if not db.cancel_web_harvest_review(review_id, _session_hash(request)):
                return HTMLResponse("A revisão já foi concluída.", status_code=409)
        finally:
            db.close()
        from zettel.harvester.prepared import remove_preparation

        remove_preparation(service(request).cfg, review_id)
        return RedirectResponse("/documents", status_code=303)
    error = _review_error(request, job, review)
    if error:
        return HTMLResponse(error, status_code=409)
    if form.get("decision") != "confirm":
        return HTMLResponse("Escolha uma ação válida.", status_code=400)
    from zettel.bibliography import FIELD_LABELS, missing_required

    values = {name: str(form.get(name) or "") for name in
              (*job["result"]["biblio"].keys(), "abnt_reference")}
    try:
        biblio = _parsed_biblio(form, job["result"]["biblio"])
        missing = missing_required(biblio)
        if missing:
            if not job["payload"]["skip_biblio"]:
                raise ValueError(
                    "Complete os campos obrigatórios: "
                    + ", ".join(FIELD_LABELS.get(n, n) for n in missing)
                    + ". Ou marque 'Permitir bibliografia incompleta' ao iniciar."
                )
            if str(form.get("incomplete_ack") or "") != "1":
                raise ValueError(
                    "Confirme explicitamente que deseja processar com bibliografia incompleta."
                )
        from zettel.bibliography import format_abnt

        reference = str(form.get("abnt_reference") or "").strip()
        retry_job = review.get("retry_job")
        previous = (
            retry_job["payload"]
            if retry_job and retry_job["state"] in {"failed", "interrupted"}
            else {
                "biblio": job["result"]["biblio"],
                "abnt_reference": job["result"]["abnt_reference"],
            }
        )
        if (
            reference == previous["abnt_reference"]
            and biblio.model_dump(exclude={"confidence"})
            != {k: v for k, v in previous["biblio"].items() if k != "confidence"}
        ):
            reference = format_abnt(biblio)
        if len(reference) > 5000:
            raise ValueError("A referência ABNT é muito longa.")
        biblio.confidence = max(
            biblio.confidence, service(request).cfg.harvest.biblio_confidence_threshold
        )
    except ValueError as exc:
        return _review_page(
            request, review_id, job, error=str(exc), values=values, status_code=400
        )
    payload = {
        **{k: v for k, v in job["payload"].items() if k not in {"checksum", "session_hash"}},
        "review_id": review_id,
        "session_hash": _session_hash(request),
        "biblio": biblio.model_dump(),
        "abnt_reference": reference,
    }
    harvest_job_id = service(request).submit_review(review_id, _session_hash(request), payload)
    if not harvest_job_id:
        return _review_page(
            request,
            review_id,
            job,
            error="Outra operação está em andamento ou a revisão já foi enviada.",
            values=values,
            status_code=409,
        )
    return RedirectResponse(f"/jobs/{harvest_job_id}", status_code=303)


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
