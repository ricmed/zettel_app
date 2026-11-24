"""Authenticated backup download and restore. The passphrase never enters the job row."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from starlette.datastructures import FormData, UploadFile

from zettel.web.enqueue import post_job
from zettel.web.forms import read_form
from zettel.web.rendering import error_page, render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login
from zettel.web_app import UserFacingError

router = APIRouter()

_CONFIRM = "restaurar"


def spool_upload(source, dest: Path, limit: int) -> int:
    """Copy ``source`` to ``dest``, stopping when the next chunk would pass ``limit``.

    An overflow deletes ``dest`` and raises. The byte already counted stays the
    archive ceiling, the same number the body limit uses for this route.
    """
    total = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                if total + len(chunk) > limit:
                    raise UserFacingError("O arquivo passa de 10 GB.")
                handle.write(chunk)
                total += len(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return total


def _outside_vault(path: Path, vault: Path) -> Path:
    resolved = path.resolve()
    root = vault.resolve()
    if resolved == root or root in resolved.parents:
        path.unlink(missing_ok=True)
        raise UserFacingError("Não foi possível guardar o arquivo fora do vault.")
    return path


def _write_passphrase(text: str, vault: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="zettel-pass-", suffix=".txt")
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    path = Path(name)
    with suppress(OSError):
        os.chmod(path, 0o600)
    return _outside_vault(path, vault)


def _receive_archive(upload: UploadFile, vault: Path) -> Path:
    from zettel.backup.archive import MAX_UNCOMPRESSED_BYTES

    descriptor, name = tempfile.mkstemp(prefix="zettel-restore-", suffix=".zettelbak")
    os.close(descriptor)
    path = _outside_vault(Path(name), vault)
    spool_upload(upload.file, path, MAX_UNCOMPRESSED_BYTES)
    return path


@router.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request):
    if not authenticated(request):
        return redirect_login()
    return render(request, "backup.html", page="backup")


@router.post("/backup")
def backup_create(
    request: Request,
    csrf: str = Form(""),
    passphrase: str = Form(""),
    include_chroma: str = Form(""),
    include_inbox: str = Form(""),
):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return error_page(request, 403, "CSRF inválido")
    if passphrase == "":
        return error_page(request, 400, "A senha está vazia.")
    phrase_path = _write_passphrase(passphrase, service(request).cfg.vault_path)
    return post_job(
        request,
        "backup",
        {
            "include_chroma": include_chroma == "1",
            "include_inbox": include_inbox == "1",
        },
        csrf,
        secret=(phrase_path, None),
    )


@router.post("/backup/restaurar")
def backup_restore(request: Request, form: FormData = Depends(read_form)):
    if not authenticated(request):
        return redirect_login()
    token = str(form.get("csrf") or "")
    if not csrf_ok(request, token):
        return error_page(request, 403, "CSRF inválido")
    if str(form.get("confirmar") or "").strip().casefold() != _CONFIRM:
        return error_page(request, 400, "Digite restaurar para confirmar.")
    passphrase = str(form.get("passphrase") or "")
    if passphrase == "":
        return error_page(request, 400, "A senha está vazia.")
    upload = form.get("arquivo")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return error_page(request, 400, "Escolha o arquivo .zettelbak.")
    vault = service(request).cfg.vault_path
    phrase_path = _write_passphrase(passphrase, vault)
    try:
        archive_path = _receive_archive(upload, vault)
    except UserFacingError as exc:
        phrase_path.unlink(missing_ok=True)
        return error_page(request, 400, str(exc))
    except Exception:
        phrase_path.unlink(missing_ok=True)
        raise
    return post_job(
        request,
        "restore",
        {
            "force_env": str(form.get("force_env") or "") == "1",
            "rotate_session": str(form.get("rotate_session") or "") == "1",
        },
        token,
        secret=(phrase_path, archive_path),
    )


@router.get("/backup/arquivo/{job_id}")
def backup_download(request: Request, job_id: str):
    """One download of the archive the job wrote. The file is removed afterwards."""
    if not authenticated(request):
        return redirect_login()
    path = service(request).claim_backup(job_id)
    if path is None or not path.is_file():
        return error_page(request, 404, "O arquivo de backup não está mais disponível.")
    return FileResponse(
        path,
        filename=f"acervo-{job_id[:8]}.zettelbak",
        media_type="application/octet-stream",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )
