"""Server-rendered FastAPI UI (ADR-022, ADR-039).

This package is the assembly point. ``create_app`` lives in ``server.py`` and
imports nothing from the rest of the package. Each route module below owns an
``APIRouter``; this file imports those modules and ``create_app`` includes their
routers. Importing a module *is* how a route reaches the app — a module not
listed here has no routes, however correct its code.

The import order below is the order routes are registered. Parametric detail
pages (``/notes/{note_id}`` and siblings) **must** come last: ``/notes/new``
would otherwise 404. ``tests/test_web_package.py`` pins both the paths and
that ordering.

Two more invariants a change here must not break:

1. ``server.py`` imports nothing else from this package. Adding
   ``from zettel.web.auth import router`` there closes a cycle.
2. Pipeline modules stay imported *inside* the handlers, never at module top
   level, except the allowlisted ``zettel.web_app``, ``zettel.markdown`` and
   ``zettel.hashing``. That is what keeps ``GET /`` from loading chromadb,
   docling and langchain.
"""

from __future__ import annotations

from zettel.web.server import create_app, lifespan

# isort: off
# Importing a route module exposes its APIRouter; the order of these statements
# is the order FastAPI matches paths. Deliberately not alphabetical.
from zettel.web import auth          # /favicon.ico, /login, /logout
from zettel.web import dashboard     # /
from zettel.web import documents     # /documents, /upload, /harvest, /run-all
from zettel.web import pipeline      # /pipeline
from zettel.web import review        # /review
from zettel.web import notes         # /notes (listing)
from zettel.web import manual        # /notes/new
from zettel.web import pickers       # /api/pickers/*
from zettel.web import jobs          # /runs, /jobs, /api/jobs
from zettel.web import settings      # /settings
from zettel.web import details       # /sources/{id}, /notes/{id}, /mocs/{id} — LAST
# isort: on

ROUTE_MODULES = (
    auth, dashboard, documents, pipeline, review, notes,
    manual, pickers, jobs, settings, details,
)

ROUTERS = tuple(module.router for module in ROUTE_MODULES)

app = create_app()

__all__ = ["ROUTE_MODULES", "ROUTERS", "app", "create_app", "lifespan"]
