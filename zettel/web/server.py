"""The FastAPI factory and lifespan. Imports nothing from the rest of this package.

``uvicorn zettel.web:app`` resolves the ``app`` attribute of the package, which
``zettel/web/__init__.py`` builds after the routers exist. This module is named
``server.py`` rather than ``app.py`` because a submodule ``zettel.web.app`` would
silently replace the FastAPI instance on the package (the same collision
ADR-029 and ADR-032 avoided by renaming).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from zettel.web_app import WebApplication


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = WebApplication(getattr(app.state, "config_path", None) or os.environ.get("ZETTEL_CONFIG"))
    app.state.service = service
    service.start()
    yield
    service.stop()


def create_app(config_path: str | Path | None = None, routers=None) -> FastAPI:
    application = FastAPI(title="Zettelkasten", lifespan=lifespan)
    if config_path:
        application.state.config_path = str(config_path)
    root = Path(__file__).resolve().parent.parent
    application.mount("/static", StaticFiles(directory=str(root / "static")), name="static")
    if routers is None:
        from zettel.web import ROUTERS
        routers = ROUTERS
    for router in routers:
        application.include_router(router)
    return application
