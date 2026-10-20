"""App-wide error handlers: an HTML error is a page with the layout (#316).

``/api/`` paths keep JSON. An unhandled exception shows a correlation id that
the log line also carries, never the exception text (#315's rule for jobs,
applied to requests).
"""

from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from zettel.web.rendering import error_page

logger = logging.getLogger(__name__)

_HTTP_DETAILS = {
    404: "Esta página não existe.",
    405: "Esta página não aceita este tipo de envio.",
}


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


async def _http_error(request: Request, exc: StarletteHTTPException):
    if _is_api(request):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    detail = _HTTP_DETAILS.get(exc.status_code) or str(exc.detail)
    return error_page(request, exc.status_code, detail)


async def _invalid_form(request: Request, exc: RequestValidationError):
    if _is_api(request):
        return JSONResponse({"error": "invalid_request"}, status_code=422)
    return error_page(request, 400, "O formulário chegou incompleto ou com um valor inválido.")


async def _unexpected(request: Request, exc: Exception):
    from zettel.web.hardening import SECURITY_HEADERS

    ref = secrets.token_hex(4)
    logger.error("Erro em %s %s [ref %s]", request.method, request.url.path, ref, exc_info=exc)
    detail = f"Erro inesperado (ref. {ref}). Consulte os logs do servidor."
    # This handler runs outside the middleware stack: add its headers here.
    try:
        response = (
            JSONResponse({"error": "internal", "ref": ref}, status_code=500)
            if _is_api(request)
            else error_page(request, 500, detail)
        )
    except Exception:  # the page itself failed to render
        response = PlainTextResponse(detail, status_code=500)
    for name, value in SECURITY_HEADERS:
        response.headers[name.decode()] = value.decode()
    return response


def install(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _invalid_form)
    app.add_exception_handler(Exception, _unexpected)
