"""Contract of the ``zettel.connector`` package layout (ADR-053)."""

import ast
from pathlib import Path

from zettel import connector

PACKAGE = Path(connector.__file__).resolve().parent
SUBMODULES = {p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__"}

PUBLIC_API = {
    "ConnectRejected",
    "literature_ref_for_chunk",
    "load_approved_candidates",
    "load_connect_taxonomy",
    "rebuild_auto_backlinks",
    "run_connect",
    "search_distant_analogies",
}


def test_public_api_is_reexported():
    assert set(connector.__all__) == PUBLIC_API
    for name in PUBLIC_API:
        assert getattr(connector, name) is not None


def test_no_submodule_shadows_an_exported_name():
    """A submodule named like an export would rebind the package attribute."""
    assert not SUBMODULES & PUBLIC_API


def test_submodules_import_siblings_never_the_package_namespace():
    """``from zettel.connector import X`` inside the package must name a submodule:
    importing a symbol from ``__init__`` would close an import cycle."""
    for path in PACKAGE.glob("*.py"):
        if path.stem == "__init__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "zettel.connector":
                names = {alias.name for alias in node.names}
                assert names <= SUBMODULES, f"{path.name} importa {names - SUBMODULES} do pacote"
            assert not (isinstance(node, ast.ImportFrom) and node.level), (
                f"{path.name}: use imports absolutos entre irmaos (ADR-053)"
            )
