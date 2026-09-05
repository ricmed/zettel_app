"""Contract tests for the CLI package (ADR-026, ADR-032).

Everything here is offline and cheap: no LLM, no embedding, no database, no
vault. What is locked:

* **the command surface** — the 22 commands and the order they appear in
  ``zettel --help``, so the split of ``cli.py`` into ``zettel/cli/`` (and any
  later reshuffle) cannot silently drop or reorder one;
* **every command's parser builds** — an ``Annotated`` signature that Typer
  cannot turn into a parser fails at invocation time, not at import time, so
  ``import zettel.cli`` passing proves nothing on its own;
* **the package's structural invariants** — the anti-cycle seam, the no-command-
  imports-a-command rule, and the lazy-import rule that keeps ``--help`` fast.
  These are stated in ``zettel/cli/__init__.py``; here they are enforced;
* **the pure helpers** — the flag resolvers and formatters, which is where the
  CLI's only real logic lives.

``fmt_prompt_cache_ratio`` is deliberately not retested here: it is covered in
``tests/test_usage.py`` next to the ``UsageSummary`` it formats.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner
from zettel.cli import app
from zettel.cli.formatting import fmt_embedding_id, fmt_usd
from zettel.cli.options import (
    resolve_chunk_dump_dir,
    resolve_duplicate_flags,
    resolve_extraction_dump_dir,
)

CLI_PKG = Path(__file__).resolve().parents[1] / "zettel" / "cli"

# The command surface, in the order `zettel --help` lists it: the life of a vault
# — set it up, feed it, curate it, synthesise, prune, write by hand, run it all,
# query it, inspect it. This order comes from the import order in
# `zettel/cli/__init__.py`; changing either without the other is the bug this
# list catches.
EXPECTED_COMMANDS = [
    # maintenance.py — store lifecycle
    "init",
    "reindex",
    "rebuild",
    # ingest.py — phase 1 and its repair tools
    "harvest",
    "rechunk",
    "dump-chunks",
    "dump-extraction",
    "set-paging",
    # curation.py — phase 2 and the review gate
    "extract",
    "review",
    "retry-failed",
    # synthesis.py — phases 3 and 4
    "connect",
    "garden",
    # purge.py — irreversible deletions
    "purge-rejected",
    "delete-source",
    # manual.py — hand-written notes
    "new-note",
    "sync-manual",
    # pipeline.py / qa.py / writing.py / export.py
    "run-all",
    "ask",
    "article",
    "skill",
    # diagnostics.py — read-only inspection
    "status",
    "doctor",
]

# Infrastructure modules: they may be imported by command modules. Anything else
# in the package is a command module and must not be imported by a sibling.
INFRA_MODULES = {"app", "deps", "formatting", "options"}


def registered_command_names() -> list[str]:
    """Command names as Typer will expose them, in registration order."""
    return [cmd.name or cmd.callback.__name__.replace("_", "-") for cmd in app.registered_commands]


def cli_modules() -> list[Path]:
    return sorted(p for p in CLI_PKG.glob("*.py") if p.name != "__init__.py")


def _top_level_imports(path: Path) -> list[str]:
    """Modules imported at module scope (not inside a function body)."""
    names: list[str] = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


# ── Command surface ───────────────────────────────────────────────────


def test_every_command_is_registered_in_order():
    """The 22 commands, in the documented order.

    A command lost during a refactor disappears from the CLI without any import
    error — the module simply stops being imported, or its decorator is dropped.
    """
    assert registered_command_names() == EXPECTED_COMMANDS


def test_no_duplicate_command_names():
    """Two modules registering the same name: the second silently shadows nothing
    and both appear, which is worse than an error."""
    names = registered_command_names()
    assert len(names) == len(set(names))


@pytest.mark.parametrize("command", EXPECTED_COMMANDS)
def test_command_help_builds_and_exits_zero(command: str):
    """Typer builds each command's parser only when it is invoked.

    A malformed ``Annotated`` signature (a bad option type, a default that does
    not match, an argument declared after an option with a default) imports fine
    and blows up here.
    """
    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output


def test_root_help_lists_every_command():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in EXPECTED_COMMANDS:
        assert command in result.output


def test_harvest_no_longer_offers_move_processed():
    """`--move-processed` was removed in ADR-032's cleanup: it never moved a file,
    it only created `data/processed/` and told the user to move them by hand."""
    result = CliRunner().invoke(app, ["harvest", "--help"])
    assert "--move-processed" not in result.output


# ── Package structure ─────────────────────────────────────────────────


def test_package_imports_without_cycle():
    """`python -m zettel` does nothing but this import, so a cycle is fatal."""
    import importlib

    module = importlib.import_module("zettel.cli")
    assert callable(module.main)


def test_app_module_imports_nothing_from_the_package():
    """The anti-cycle seam.

    ``app.py`` holds the ``app``/``console`` objects every command module imports.
    If it imports a command module back, the two initialise each other and
    ``python -m zettel`` dies on a partially-built module.
    """
    offenders = [
        name for name in _top_level_imports(CLI_PKG / "app.py") if name.startswith("zettel.")
    ]
    assert not offenders, (
        f"zettel/cli/app.py importa {offenders}; ele nao pode importar nada do "
        f"pacote (ver o seam anticircular em zettel/cli/__init__.py)."
    )


def test_no_command_module_imports_another_command_module():
    """Command modules talk to infrastructure, never to each other.

    A command that needs another command's behaviour needs the *domain* function
    both call — that is what ``run-all`` does.
    """
    problems: list[str] = []
    for path in cli_modules():
        if path.stem in INFRA_MODULES:
            continue
        for name in _top_level_imports(path):
            if not name.startswith("zettel.cli."):
                continue
            sibling = name.split(".")[2]
            if sibling not in INFRA_MODULES:
                problems.append(f"{path.name} -> {sibling}")
    assert not problems, f"modulo de comando importando outro: {problems}"


def test_pipeline_modules_are_imported_lazily():
    """``zettel --help`` must not pay for chromadb, docling and langchain.

    Every pipeline import lives inside the command function that needs it. Hoisting
    one to module scope adds seconds to *every* invocation, including ``--help``
    and tab completion, and the cost is invisible until someone measures it.
    """
    offenders: list[str] = []
    for path in cli_modules():
        for name in _top_level_imports(path):
            if name.startswith("zettel.") and not name.startswith("zettel.cli"):
                offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        f"import de modulo de dominio no topo: {offenders}. Mova para dentro da funcao de comando."
    )


# ── Flag resolvers ────────────────────────────────────────────────────


def test_resolve_duplicate_flags_defaults_to_interactive():
    assert resolve_duplicate_flags(False, False, False) == (True, None)


def test_resolve_yes_defers_to_the_config_default():
    """``--yes`` means non-interactive but does *not* choose the action: ``None``
    tells harvest to use ``harvest.non_interactive_duplicate_action``."""
    assert resolve_duplicate_flags(True, False, False) == (False, None)


def test_resolve_skip_and_force_pick_opposite_actions():
    assert resolve_duplicate_flags(False, True, False) == (False, "skip")
    assert resolve_duplicate_flags(False, False, True) == (False, "continue")


def test_resolve_skip_and_force_together_is_an_error():
    """They ask for opposite outcomes; guessing either way silently loses files
    or silently duplicates them."""
    with pytest.raises(typer.Exit) as exc:
        resolve_duplicate_flags(False, True, True)
    assert exc.value.exit_code == 1


class _Cfg:
    """Only what the dump-dir resolvers read."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path


def test_chunk_dump_dir_is_none_when_the_dump_is_off(tmp_path: Path):
    assert resolve_chunk_dump_dir(_Cfg(tmp_path), False, None) is None


def test_chunk_dump_dir_defaults_under_the_cache(tmp_path: Path):
    from zettel.chunk_dump import default_dump_dir

    cfg = _Cfg(tmp_path)
    assert resolve_chunk_dump_dir(cfg, True, None) == default_dump_dir(cfg)


def test_explicit_dump_dir_implies_the_flag(tmp_path: Path):
    """Asking where to write the dump and then not getting one is a surprise."""
    target = tmp_path / "saida"
    resolved = resolve_chunk_dump_dir(_Cfg(tmp_path), False, str(target))
    assert resolved == target.resolve()


def test_extraction_dump_dir_follows_the_same_contract(tmp_path: Path):
    from zettel.extraction_dump import default_dump_dir

    cfg = _Cfg(tmp_path)
    assert resolve_extraction_dump_dir(cfg, False, None) is None
    assert resolve_extraction_dump_dir(cfg, True, None) == default_dump_dir(cfg)
    target = tmp_path / "extracao"
    assert resolve_extraction_dump_dir(cfg, False, str(target)) == target.resolve()


# ── Formatters ────────────────────────────────────────────────────────


def test_fmt_usd_keeps_six_decimals():
    """A single cheap call costs ~$0.000002: fewer decimals round it to zero."""
    assert fmt_usd(0.002066) == "0.002066"
    assert fmt_usd(None) == "0.000000"
    assert fmt_usd(0) == "0.000000"


def test_fmt_embedding_id_includes_dimensions_when_set():
    """Dimensions are part of the identity: the same model at a reduced width
    produces vectors that cannot be compared with the others in the store."""
    assert fmt_embedding_id("ollama", "qwen3-embedding", 1024) == "ollama/qwen3-embedding@1024d"
    assert (
        fmt_embedding_id("openai", "text-embedding-3-small", None)
        == "openai/text-embedding-3-small"
    )
