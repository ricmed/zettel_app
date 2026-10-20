"""Auth + CSRF + 409 wrapper around ``WebApplication.submit``, and the operation lock."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from zettel.web.rendering import render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login


def lock_busy_message(request: Request) -> str | None:
    """Who holds the operation lock (#308), or None when it is free."""
    from zettel.oplock import describe_holder, lock_holder, lock_path

    holder = lock_holder(lock_path(service(request).cfg))
    return None if holder is None else describe_holder(holder)


def operation_lock(request: Request, operation: str):
    """The cross-process lock for a write done in the request, not in a job.

    Raises ``zettel.oplock.LockBusy`` when the worker or the CLI holds it.
    """
    from zettel.oplock import OperationLock, lock_path

    lock = OperationLock(lock_path(service(request).cfg))
    lock.acquire(operation, "web")
    return lock


def post_job(request: Request, operation: str, payload: dict[str, Any], csrf: str):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    busy = lock_busy_message(request)
    job_id = None if busy else service(request).submit(operation, payload)
    if not job_id:
        return render(
            request,
            "jobs.html",
            page="jobs",
            jobs=service(request).jobs(),
            error=busy or "Outra operação mutante já está em andamento.",
            status_code=409,
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
