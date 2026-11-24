"""Auth + CSRF + 409 wrapper around ``WebApplication.submit``, and the operation lock."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse

from zettel.web.rendering import error_page, service
from zettel.web.security import authenticated, csrf_ok, redirect_login


def discard_local_files(*paths: Path | None) -> None:
    """Delete temp files the request created and the worker will not own."""
    for path in paths:
        if path is not None:
            path.unlink(missing_ok=True)


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


def post_job(
    request: Request,
    operation: str,
    payload: dict[str, Any],
    csrf: str,
    *,
    secret: tuple[Path, Path | None] | None = None,
):
    """Enqueue ``operation``. ``secret`` is a passphrase file and an optional upload.

    The payload stays free of the passphrase. When the job is not queued, the
    files are deleted here; otherwise the worker deletes them.
    """
    if not authenticated(request):
        discard_local_files(*(secret or ()))
        return redirect_login()
    if not csrf_ok(request, csrf):
        discard_local_files(*(secret or ()))
        return error_page(request, 403, "CSRF inválido")
    busy = lock_busy_message(request)
    if secret is None:
        job_id = None if busy else service(request).submit(operation, payload)
    else:
        phrase, archive = secret
        job_id = (
            None if busy else service(request).submit_secret(operation, payload, phrase, archive)
        )
        if not job_id:
            discard_local_files(phrase, archive)
    if job_id:
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    if busy:  # the CLI, or a job between two of its steps, holds the lock
        return error_page(request, 409, busy)
    db = service(request).db()
    try:
        holder = db.active_web_job()
    finally:
        db.close()
    detail = (
        f"O {holder['operation']} ({holder['state']}) ocupa a fila: uma operação mutante "
        "por vez. Tente de novo quando ele terminar."
        if holder
        else "Outra operação mutante já está em andamento."
    )
    return error_page(request, 409, detail, busy_job=holder)
