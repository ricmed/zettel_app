"""FastAPI application for operating the Zettelkasten without the terminal."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from zettel.web_app import WebApplication, safe_error

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _secret() -> str:
    return os.environ.get("SESSION_SECRET", "")


def _sign(payload: str) -> str:
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def _session_value(csrf: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({"csrf": csrf, "created": int(time.time())}).encode()
    ).decode().rstrip("=")
    return f"{body}.{_sign(body)}"


def _session(request: Request) -> dict[str, str] | None:
    if not _secret():
        return None
    raw = request.cookies.get("zettel_session", "")
    try:
        body, signature = raw.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(body)):
            return None
        body += "=" * (-len(body) % 4)
        session = json.loads(base64.urlsafe_b64decode(body))
        if int(time.time()) - int(session.get("created", 0)) > 86400:
            return None
        return session
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _csrf_ok(request: Request, token: str | None) -> bool:
    session = _session(request)
    return bool(session and token and hmac.compare_digest(session.get("csrf", ""), token))


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = WebApplication(getattr(app.state, "config_path", None) or os.environ.get("ZETTEL_CONFIG"))
    app.state.service = service
    service.start()
    yield
    service.stop()


def create_app(config_path: str | Path | None = None) -> FastAPI:
    application = FastAPI(title="Zettelkasten", lifespan=lifespan)
    if config_path:
        application.state.config_path = str(config_path)
    application.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    # The module-level app is created before route decorators run.  Subsequent
    # factory calls (tests/alternate config) clone those registered routes.
    canonical = globals().get("app")
    if canonical is not None:
        built_in_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/static"}
        for route in canonical.router.routes:
            if getattr(route, "path", None) in built_in_paths:
                continue
            application.router.routes.append(route)
    return application


app = create_app()


def _context(request: Request, **extra: Any) -> dict[str, Any]:
    session = _session(request)
    return {
        "request": request,
        "authenticated": session is not None,
        "csrf": session.get("csrf") if session else "",
        **extra,
    }


def _render(
    request: Request, name: str, *, status_code: int = 200, **context: Any,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=_context(request, **context),
        status_code=status_code,
    )


def _service(request: Request) -> WebApplication:
    return request.app.state.service


def _auth(request: Request) -> bool:
    return _session(request) is not None


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not _secret():
        return _render(request, "login.html", error="SESSION_SECRET não está configurado.",
                       login_csrf="")
    if _auth(request):
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", login_csrf=_sign("login"))


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, instance_secret: str = Form(...), login_csrf: str = Form("")):
    valid_login_csrf = bool(
        _secret() and login_csrf and hmac.compare_digest(login_csrf, _sign("login"))
    )
    if not valid_login_csrf:
        return HTMLResponse("CSRF inválido", status_code=403)
    if not hmac.compare_digest(instance_secret, _secret()):
        return _render(request, "login.html", error="Segredo inválido.", status_code=401,
                       login_csrf=_sign("login"))
    response = RedirectResponse("/", status_code=303)
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie("zettel_session", _session_value(secrets.token_urlsafe(24)),
                        httponly=True, samesite="lax", secure=forwarded_proto == "https",
                        max_age=86400)
    return response


@app.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    if not _csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("zettel_session")
    return response


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    if not _auth(request):
        return _redirect_login()
    service = _service(request)
    return _render(request, "dashboard.html", page="overview",
                   dashboard=service.dashboard(), jobs=service.jobs()[:5])


@app.get("/documents", response_class=HTMLResponse)
async def documents(request: Request):
    if not _auth(request):
        return _redirect_login()
    db = _service(request).db()
    try:
        sources = db.list_sources()
        inbox = []
        cfg = _service(request).cfg
        if cfg.inbox_path.exists():
            inbox = [{"name": f.name, "relative": f.relative_to(cfg.inbox_path).as_posix(),
                      "size": f.stat().st_size} for f in cfg.inbox_path.rglob("*")
                     if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS]
    finally:
        db.close()
    return _render(request, "documents.html", page="documents", sources=sources, inbox=inbox)


@app.post("/documents/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...), csrf: str = Form("")):
    if not _auth(request):
        return _redirect_login()
    if not _csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    original_name = file.filename or ""
    name = Path(original_name).name
    suffix = Path(name).suffix.lower()
    if (
        not name or name in {".", ".."} or name != original_name
        or "/" in original_name or "\\" in original_name
        or suffix not in ALLOWED_EXTENSIONS or len(name) > 180
        or re.fullmatch(r"[\w .()\-]+", name, flags=re.UNICODE) is None
    ):
        return _render(request, "documents.html", page="documents", sources=[], inbox=[],
                       error="Use um arquivo PDF, Markdown ou TXT com nome válido.", status_code=400)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        return _render(request, "documents.html", page="documents", sources=[], inbox=[],
                       error="O arquivo está vazio.", status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return _render(request, "documents.html", page="documents", sources=[], inbox=[],
                       error="O arquivo excede o limite de 25 MB.", status_code=413)
    cfg = _service(request).cfg
    destination = (cfg.inbox_path / name).resolve()
    try:
        destination.relative_to(cfg.inbox_path.resolve())
    except ValueError:
        return HTMLResponse("Nome de arquivo inválido", status_code=400)
    if destination.exists():
        return _render(request, "documents.html", page="documents", sources=[], inbox=[],
                       error="Já existe um arquivo com esse nome no inbox.", status_code=409)
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return RedirectResponse("/documents", status_code=303)


def _post_job(request: Request, operation: str, payload: dict[str, Any], csrf: str):
    if not _auth(request):
        return _redirect_login()
    if not _csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    job_id = _service(request).submit(operation, payload)
    if not job_id:
        return _render(request, "jobs.html", page="jobs", jobs=_service(request).jobs(),
                       error="Outra operação mutante já está em andamento.", status_code=409)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/documents/harvest")
async def harvest(request: Request, selected_file: str = Form(""), duplicate_action: str = Form("skip"),
                  skip_biblio: str = Form(""), skip_paging: str = Form(""),
                  content_start_file: int | None = Form(None),
                  content_start_book: int | None = Form(None), csrf: str = Form("")):
    if not _auth(request):
        return RedirectResponse("/login", status_code=303)
    if not _csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    if selected_file:
        cfg = _service(request).cfg
        relative = Path(selected_file)
        if relative.is_absolute() or ".." in relative.parts:
            return HTMLResponse("Seleção inválida", status_code=400)
        selected = (cfg.inbox_path / relative).resolve()
        try:
            selected.relative_to(cfg.inbox_path.resolve())
        except ValueError:
            return HTMLResponse("Arquivo inválido", status_code=400)
        if not selected.is_file() or selected.suffix.lower() not in ALLOWED_EXTENSIONS:
            return HTMLResponse("Arquivo não encontrado", status_code=404)
        selected_file = str(selected)
    if duplicate_action not in {"skip", "continue", "abort"}:
        duplicate_action = "skip"
    return _post_job(request, "harvest", {"selected_file": selected_file or None,
        "duplicate_action": duplicate_action, "skip_biblio": bool(skip_biblio),
        "skip_paging": bool(skip_paging), "content_start_file": content_start_file,
        "content_start_book": content_start_book}, csrf)


@app.get("/pipeline", response_class=HTMLResponse)
async def pipeline(request: Request):
    if not _auth(request):
        return _redirect_login()
    db = _service(request).db()
    try:
        stats = db.get_stats()
    finally:
        db.close()
    return _render(request, "pipeline.html", page="pipeline", stats=stats)


@app.post("/pipeline/{operation}")
async def pipeline_action(request: Request, operation: str, csrf: str = Form("")):
    if operation not in {"extract", "connect", "garden", "garden_hubs", "sync", "retry_chunks", "retry_assets"}:
        return HTMLResponse("Operação indisponível", status_code=404)
    if operation == "connect":
        db = _service(request).db()
        try:
            if not db.get_concepts_by_status("approved", without_notes=True):
                return HTMLResponse("Nenhum candidato aprovado aguardando connect.", status_code=409)
        finally:
            db.close()
    return _post_job(request, "garden" if operation == "garden_hubs" else operation,
                     {"hubs": operation == "garden_hubs"}, csrf)


@app.get("/review", response_class=HTMLResponse)
async def review(request: Request, source_id: str = "", confidence: str = "", page: int = 1):
    if not _auth(request):
        return _redirect_login()
    db = _service(request).db()
    try:
        chunks = db.get_chunks_by_status("awaiting_review", source_id or None)
        sources = db.list_sources()
        enriched = []
        for chunk in chunks:
            summary = {}
            try:
                summary = json.loads(chunk.get("summary_json") or "{}")
            except json.JSONDecodeError:
                pass
            enriched.append({**chunk, "summary": summary.get("summary", ""),
                             "candidates": summary.get("candidates", [])})
    finally:
        db.close()
    if confidence in {"low", "medium", "high"}:
        threshold = _service(request).cfg.literature_review.auto_approve_min_confidence
        enriched = [c for c in enriched if
                    ("low" if (c.get("review_confidence") or 0) < .4 else
                     "medium" if (c.get("review_confidence") or 0) < threshold else "high") == confidence]
    page_size = 20
    total = len(enriched)
    page = max(1, page)
    enriched = enriched[(page - 1) * page_size:page * page_size]
    return _render(request, "review.html", page="review", chunks=enriched, sources=sources,
                   selected_source=source_id, selected_confidence=confidence,
                   review_page=page, has_next=page * page_size < total)


@app.post("/review/action")
async def review_action(request: Request, action: str = Form(...), csrf: str = Form(""),
                        chunk_ids: list[str] = Form(default=[])):
    if action not in {"approve", "reject"}:
        return HTMLResponse("Ação inválida", status_code=400)
    return _post_job(request, "review", {"action": action, "chunk_ids": chunk_ids}, csrf)


@app.get("/notes", response_class=HTMLResponse)
async def notes(request: Request):
    if not _auth(request):
        return _redirect_login()
    db = _service(request).db()
    try:
        note_rows = db.list_notes()
        moc_rows = db.list_mocs()
    finally:
        db.close()
    return _render(request, "notes.html", page="notes", notes=note_rows, mocs=moc_rows)


@app.get("/sources/{source_id}", response_class=HTMLResponse)
async def source_detail(request: Request, source_id: str):
    if not _auth(request):
        return _redirect_login()
    db = _service(request).db()
    try:
        source = db.get_source(source_id)
        chunks = db.get_chunks_for_source(source_id) if source else []
    finally:
        db.close()
    if not source:
        return HTMLResponse("Fonte não encontrada", status_code=404)
    return _render(request, "source_detail.html", page="documents", source=source, chunks=chunks)


@app.get("/notes/{note_id}", response_class=HTMLResponse)
async def note_detail(request: Request, note_id: str):
    if not _auth(request):
        return _redirect_login()
    db = _service(request).db()
    try:
        note = db.get_note(note_id)
        connections = db.get_note_connections(note_id) if note else []
    finally:
        db.close()
    if not note:
        return HTMLResponse("Nota não encontrada", status_code=404)
    return _render(request, "note_detail.html", page="notes", note=note, connections=connections)


@app.get("/mocs/{moc_id}", response_class=HTMLResponse)
async def moc_detail(request: Request, moc_id: str):
    if not _auth(request):
        return _redirect_login()
    db = _service(request).db()
    try:
        moc = db.get_moc(moc_id)
    finally:
        db.close()
    if not moc:
        return HTMLResponse("MOC não encontrado", status_code=404)
    return _render(request, "moc_detail.html", page="notes", moc=moc)


@app.get("/runs", response_class=HTMLResponse)
async def runs(request: Request):
    if not _auth(request):
        return _redirect_login()
    return _render(request, "jobs.html", page="runs", jobs=_service(request).jobs())


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: str):
    if not _auth(request):
        return _redirect_login()
    job = _service(request).job(job_id)
    if not job:
        return HTMLResponse("Trabalho não encontrado", status_code=404)
    return _render(request, "job_detail.html", page="runs", job=job)


@app.get("/api/jobs/{job_id}")
async def job_api(request: Request, job_id: str, after: int = 0):
    if not _auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    job = _service(request).job(job_id)
    if not job:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {"job": job, "events": _service(request).events(job_id, max(0, after))}


@app.get("/api/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str):
    if not _auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async def stream():
        last = 0
        for _ in range(20):
            events = _service(request).events(job_id, last)
            for event in events:
                last = max(last, event["event_id"])
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            job = _service(request).job(job_id)
            if job and job["state"] in {"succeeded", "failed", "interrupted"}:
                break
            import asyncio
            await asyncio.sleep(.5)
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    if not _auth(request):
        return _redirect_login()
    service = _service(request)
    cfg = service.cfg
    db = service.db()
    try:
        health = {"fts5": db.fts_enabled, "state_db": cfg.state_db_path.exists(),
                  "vault": cfg.vault_path.exists(), "inbox": cfg.inbox_path.exists()}
    finally:
        db.close()
    from zettel.index import peek_stored_embedding_identity
    stored_provider, stored_model = peek_stored_embedding_identity(cfg.chroma_path)
    embedding = {
        "stored": f"{stored_provider}/{stored_model}" if stored_provider or stored_model else "ainda não gravado",
        "configured": f"{cfg.embedding.provider}/{cfg.embedding.model}",
        "drift": bool(stored_provider and stored_model and (
            stored_provider != cfg.embedding.provider or stored_model != cfg.embedding.model
        )),
    }
    return _render(request, "settings.html", page="settings", cfg=cfg, health=health,
                   embedding=embedding)