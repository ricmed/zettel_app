"""Vault images shown inside rendered notes (#381).

Only content-addressed files under ``90_Assets`` are served, by name: the name
pattern admits no separator, so no path can leave the folder.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from zettel.markdown import ASSET_NAME_RE, ASSETS_FOLDER
from zettel.web.rendering import error_page, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


@router.get("/assets/{name}")
def asset(request: Request, name: str):
    if not authenticated(request):
        return redirect_login()
    if not ASSET_NAME_RE.fullmatch(name):
        return error_page(request, 404, "Imagem não encontrada")
    path = service(request).cfg.vault_path / ASSETS_FOLDER / name
    if not path.is_file():
        return error_page(request, 404, "Imagem não encontrada")
    # The name is the content hash, so the bytes behind a URL never change.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=604800, immutable"})
