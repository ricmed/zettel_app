"""Contract tests for the web package (ADR-022, ADR-039).

Everything here is offline and cheap: no LLM, no embedding, no vault I/O
beyond importing the package. What is locked:

* **the route surface** — paths and registration order, so a split or a later
  reshuffle cannot silently drop ``/notes/new`` behind ``/notes/{note_id}``;
* **the package's structural invariants** — the anti-cycle seam, the
  no-route-module-imports-a-route-module rule, lazy domain imports, and the
  ``.parent.parent`` resolution of templates and static files.
"""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from zettel.web import ROUTERS, app, create_app
from zettel.web.rendering import templates

WEB_PKG = Path(__file__).resolve().parents[1] / "zettel" / "web"
ZETTEL_ROOT = WEB_PKG.parent

# Paths in registration order. Detail routes are last on purpose: `/notes/new`
# would 404 if `/notes/{note_id}` were registered first.
EXPECTED_ROUTES = [
    ("GET", "/favicon.ico"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/logout"),
    ("GET", "/"),
    ("GET", "/documents"),
    ("POST", "/documents/upload"),
    ("POST", "/documents/harvest"),
    ("POST", "/documents/run-all"),
    ("GET", "/pipeline"),
    ("POST", "/pipeline/{operation}"),
    ("GET", "/review"),
    ("POST", "/review/action"),
    ("GET", "/notes"),
    ("GET", "/notes/new"),
    ("POST", "/notes/new/biblio-preview"),
    ("POST", "/notes/new"),
    ("GET", "/api/pickers/sources"),
    ("GET", "/api/pickers/literature"),
    ("GET", "/runs"),
    ("GET", "/jobs/{job_id}"),
    ("GET", "/api/jobs/{job_id}"),
    ("GET", "/api/jobs/{job_id}/events"),
    ("GET", "/settings"),
    ("GET", "/sources/{source_id}"),
    ("GET", "/notes/{note_id}"),
    ("GET", "/mocs/{moc_id}"),
]

_SKIP_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
_ALLOWLIST = {"zettel.web_app", "zettel.markdown", "zettel.hashing"}


def _top_level_imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def _is_route_module(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "router":
                return True
    return False


def _user_api_routes(application: FastAPI) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for route in application.routes:
        inners = []
        router = getattr(route, "original_router", None)
        if router is not None:
            inners = list(router.routes)
        elif isinstance(route, APIRoute):
            inners = [route]
        for inner in inners:
            if not isinstance(inner, APIRoute):
                continue
            if inner.path in _SKIP_PATHS or inner.path.startswith("/static"):
                continue
            for method in sorted(inner.methods - {"HEAD"}):
                found.append((method, inner.path))
    return found


def test_every_route_is_registered_in_order():
    assert _user_api_routes(app) == EXPECTED_ROUTES


def test_parametric_detail_routes_are_registered_last():
    """`/notes/new` must win over `/notes/{note_id}`. This is a correction, not style."""
    paths = [path for _method, path in _user_api_routes(app)]
    assert paths.index("/notes/new") < paths.index("/notes/{note_id}")
    last_three = [path for _method, path in _user_api_routes(app)[-3:]]
    assert last_three == ["/sources/{source_id}", "/notes/{note_id}", "/mocs/{moc_id}"]


def test_server_module_imports_nothing_from_the_package():
    offenders = [
        name for name in _top_level_imports(WEB_PKG / "server.py") if name.startswith("zettel.web.")
    ]
    assert not offenders, (
        f"zettel/web/server.py importa {offenders}; ele nao pode importar nada do pacote."
    )


def test_no_route_module_imports_another_route_module():
    problems: list[str] = []
    route_stems = {
        path.stem
        for path in WEB_PKG.glob("*.py")
        if path.name != "__init__.py" and _is_route_module(path)
    }
    for path in WEB_PKG.glob("*.py"):
        if path.name == "__init__.py" or not _is_route_module(path):
            continue
        for name in _top_level_imports(path):
            if not name.startswith("zettel.web."):
                continue
            sibling = name.split(".")[2]
            if sibling in route_stems:
                problems.append(f"{path.name} -> {sibling}")
    assert not problems, f"modulo de rota importando outro: {problems}"


def test_domain_modules_are_imported_lazily():
    offenders: list[str] = []
    for path in WEB_PKG.glob("*.py"):
        if path.name == "__init__.py":
            continue
        for name in _top_level_imports(path):
            if not name.startswith("zettel."):
                continue
            if name.startswith("zettel.web"):
                continue
            if name in _ALLOWLIST:
                continue
            offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        f"import de modulo de dominio no topo: {offenders}. Allowlist: {_ALLOWLIST}."
    )


def test_route_modules_tuple_covers_the_package():
    from zettel.web import ROUTE_MODULES

    bound = {module.__name__.split(".")[-1] for module in ROUTE_MODULES}
    found = {
        path.stem
        for path in WEB_PKG.glob("*.py")
        if path.name != "__init__.py" and _is_route_module(path)
    }
    assert bound == found
    assert len(ROUTERS) == len(ROUTE_MODULES)


def test_create_app_builds_independent_apps():
    one = create_app()
    two = create_app()
    assert one is not two
    assert id(one) != id(two)
    assert _user_api_routes(one) == _user_api_routes(two) == EXPECTED_ROUTES


def test_package_exposes_the_asgi_app():
    import zettel.web as package

    assert isinstance(package.app, FastAPI)


def test_templates_and_static_resolve_from_the_package():
    search = Path(templates.env.loader.searchpath[0]).resolve()
    assert search == (ZETTEL_ROOT / "templates").resolve()
    mounts = [route for route in app.routes if getattr(route, "name", None) == "static"]
    assert mounts
    directory = Path(mounts[0].app.directory).resolve()
    assert directory == (ZETTEL_ROOT / "static").resolve()
