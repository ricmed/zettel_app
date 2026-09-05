"""Job list, detail, JSON snapshot, SSE stream."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from zettel.web.rendering import render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


def continue_href(job: dict | None) -> str | None:
    if not job:
        return None
    raw = (job.get("payload") or {}).get("next") or ""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if "\\" in raw or "://" in raw:
        return None
    return raw


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
    return render(
        request,
        "job_detail.html",
        page="runs",
        job=job,
        continue_href=continue_href(job),
    )


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
