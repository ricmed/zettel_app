"""Job list, detail, JSON snapshot, SSE stream."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from zettel.web.rendering import render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login
from zettel.web_app import UserFacingError

router = APIRouter()


def continue_href(job: dict | None) -> str | None:
    if not job:
        return None
    if job.get("operation") == "prepare_harvest" and job.get("state") == "succeeded":
        return f"/documents/review/{job['job_id']}"
    raw = (job.get("payload") or {}).get("next") or ""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if "\\" in raw or "://" in raw:
        return None
    return raw


def _resume_decision(
    interrupt_type: str, decision: str, extra_queries: str, outline_feedback: str
) -> dict:
    choice = (decision or "").strip()
    if interrupt_type == "outline_review":
        if choice == "approve":
            return {"outline_decision": "approve", "outline_feedback": ""}
        if choice == "abort":
            return {"outline_decision": "abort", "outline_feedback": ""}
        if choice == "regenerate":
            return {
                "outline_decision": "regenerate",
                "outline_feedback": (outline_feedback or "").strip()[:2000],
            }
    else:
        if choice == "approve":
            return {"context_decision": "approve", "extra_queries": []}
        if choice == "abort":
            return {"context_decision": "abort", "extra_queries": []}
        if choice == "enrich":
            from zettel.article import parse_extra_queries

            extras = parse_extra_queries(extra_queries)[:20]
            if not extras:
                return {"context_decision": "approve", "extra_queries": []}
            return {"context_decision": "enrich", "extra_queries": extras}
    raise UserFacingError("Decisão inválida.")


def _cell(value: object) -> str:
    return str(value or "").replace("|", "/").replace("\n", " ").strip()


def result_copy_text(result: dict) -> str:
    """Plain text a person can paste elsewhere. Empty when the job has none."""
    kind = str((result or {}).get("kind") or "")
    if kind == "ask":
        return str(result.get("answer") or "").strip()
    if kind == "article":
        parts: list[str] = []
        if result.get("aborted"):
            parts.append("Geração abortada.")
        if result.get("no_evidence"):
            parts.append("Sem evidência no acervo.")
        title = str(result.get("title") or "").strip()
        body = str(result.get("body") or "").strip()
        if title and body:
            parts.append(f"# {title}\n\n{body}")
        elif body or title:
            parts.append(body or title)
        warnings = [
            str(item).strip() for item in (result.get("warnings") or []) if str(item).strip()
        ]
        if warnings:
            parts.append("Avisos:\n" + "\n".join(f"- {item}" for item in warnings))
        return "\n\n".join(parts).strip()
    if kind == "article_pause":
        if result.get("interrupt_type") == "outline_review":
            return str(result.get("preview") or "").strip()
        notes = [note for note in (result.get("notes") or []) if isinstance(note, dict)]
        if not notes:
            return ""
        lines = ["| Título | Score | Salto | Fonte |", "| --- | ---: | ---: | --- |"]
        for note in notes:
            score = float(note.get("score") or 0)
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(note.get("title") or note.get("note_id")),
                        f"{score:.4f}",
                        str(int(note.get("hop") or 0)),
                        _cell(note.get("source_id") or "-"),
                    ]
                )
                + " |"
            )
        queries = [
            str(item) for item in (result.get("executed_queries") or []) if str(item).strip()
        ]
        header = f"Queries usadas: {', '.join(queries)}\n\n" if queries else ""
        return header + "\n".join(lines)
    if kind == "summarize":
        text = (
            f"Capítulos resumidos: {result.get('chapters_summarized')} "
            f"(inalterados: {result.get('chapters_skipped')}). "
            f"Resumos gerais: {result.get('sources_summarized')}. "
            f"Chamadas LLM: {result.get('llm_calls')}, cache: {result.get('cache_hits')}."
        )
        skipped = [str(item).strip() for item in (result.get("skipped") or []) if str(item).strip()]
        if skipped:
            text += "\n" + "\n".join(f"- {item}" for item in skipped)
        return text
    if kind == "skill":
        excerpts = "sim" if result.get("include_excerpts") else "não"
        return (
            f"Caminho: {result.get('path')}\n"
            f"Slug: {result.get('slug')}\n"
            f"Notas: {result.get('notes')}\n"
            f"Tensões: {result.get('contradictions')}\n"
            f"SKILL.md (tokens est.): {result.get('tokens')}\n"
            f"Trechos: {excerpts}"
        )
    return ""


def _presentation(job: dict) -> dict:
    result = job.get("result") or {}
    kind = result.get("kind")
    prose = ""
    if kind == "ask":
        prose = result.get("answer") or ""
    elif kind == "article":
        prose = result.get("body") or ""
    preview = result.get("preview") or "" if kind == "article_pause" else ""
    if not prose and not preview:
        return {"prose_html": "", "preview_html": ""}
    from zettel.markdown import render_markdown

    return {
        "prose_html": render_markdown(prose) if prose else "",
        "preview_html": render_markdown(preview) if preview else "",
    }


@router.get("/runs", response_class=HTMLResponse)
async def runs(request: Request):
    if not authenticated(request):
        return redirect_login()
    return render(request, "jobs.html", page="runs", jobs=service(request).jobs())


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: str):
    if not authenticated(request):
        return redirect_login()
    job = service(request).job(job_id)
    if not job:
        return HTMLResponse("Trabalho não encontrado", status_code=404)
    view = _presentation(job)
    return render(
        request,
        "job_detail.html",
        page="runs",
        job=job,
        continue_href=continue_href(job),
        prose_html=view["prose_html"],
        preview_html=view["preview_html"],
        copy_text=result_copy_text(job.get("result") or {}),
    )


@router.post("/jobs/{job_id}/resume")
async def job_resume(
    request: Request,
    job_id: str,
    csrf: str = Form(""),
    decision: str = Form(""),
    extra_queries: str = Form(""),
    outline_feedback: str = Form(""),
):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    job = service(request).job(job_id)
    if not job:
        return HTMLResponse("Trabalho não encontrado", status_code=404)
    interrupt = ((job.get("result") or {}).get("interrupt_type")) or "context_review"
    try:
        choice = _resume_decision(interrupt, decision, extra_queries, outline_feedback)
        resumed = service(request).resume_article(job_id, choice)
    except UserFacingError as exc:
        return HTMLResponse(str(exc), status_code=409)
    if resumed is None:
        return HTMLResponse(
            "Outra operação mutante já está em andamento.",
            status_code=409,
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/api/jobs/{job_id}")
async def job_api(request: Request, job_id: str, after: int = 0):
    if not authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    job = service(request).job(job_id)
    if not job:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {
        "job": job,
        "events": service(request).events(job_id, max(0, after)),
        "continue_href": continue_href(job),
    }


@router.get("/api/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str):
    if not authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async def stream():
        last = 0
        for _ in range(20):
            events = service(request).events(job_id, last)
            for event in events:
                last = max(last, event["event_id"])
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            job = service(request).job(job_id)
            if job and job["state"] in {"succeeded", "failed", "interrupted"}:
                break
            import asyncio

            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")
