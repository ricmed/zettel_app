# ADR-XXX: Typer and Rich as CLI Framework

**Status:** Accepted
**Date:** 2026-02-01
**Related to:**
- [ADR-XXX: Repository Pattern for Data Access (StateDB and VectorIndex)](../INFRA/ADR-008-repository-pattern-data-access.md)
- [ADR-XXX: FastAPI Server-Rendered Web Interface (No SPA)](../WEB/ADR-022-fastapi-server-rendered-jinja2-no-spa.md)

## Context and Problem Statement

The pipeline (`harvest` -> `extract` -> `review` -> `connect` -> `garden`, plus `ask`, `article`, `sync-manual`, `new-note`, `delete-source`, and others) needed a command-line entry point that could parse arguments, route them to orchestration functions, and present results to a human operator. From the initial commit, the project structured all 24 commands as decorated functions on a single Typer application, with Rich supplying colored status output, tables/panels for pipeline statistics, interactive confirmations, and progress spinners for long-running operations.

This pattern has remained unchanged for 6+ months: 20+ commits have extended `zettel/cli.py` (now 1,934 lines, with 156 Typer-specific and 127 Rich-specific call sites) by adding new commands or parameters, but none have questioned or replaced the underlying framework choice. No alternative CLI router (manual `sys.argv` parsing, a command dispatcher) exists anywhere in the codebase — Typer is the sole CLI abstraction, and Rich usage is confined to the CLI and the web UI's status tables, kept out of pipeline modules entirely.

[NEEDS INPUT: Confirm whether Typer was evaluated against Click or argparse before adoption, or chosen by default based on the team's familiarity with type-hint-driven frameworks]

## Decision Drivers

* Type-hint-driven parameter declarations reduce boilerplate for parsing and validating options/arguments across 24 commands.
* Rich provides terminal styling, tables, panels, and interactive prompts without requiring low-level ANSI escape-code handling.
* Each CLI command is a short-lived, one-shot pipeline invocation rather than a long-running service, matching Typer's "one invocation = one command execution" model.
* Presentation concerns (Rich formatting) needed to stay isolated from pipeline business logic, so harvester/extractor/connector/gardener modules remain framework-agnostic.
* Automatic help generation and built-in `typer.confirm()` prompts avoid hand-rolling argument validation and interactive confirmation flows for every command.

## Considered Options

* Typer (type-hint-driven CLI framework) with Rich for terminal output and interactivity
* Click (Typer's underlying dependency; more mature, decorator-based, less type-hint-native)
* argparse (Python stdlib; zero additional dependency, but verbose and without type-hint integration)

## Decision Outcome

Chosen option: "Typer with Rich", because type hints directly drive command signatures across all 24 commands (`typer.Option()` / `typer.Argument()`), minimizing boilerplate compared to argparse's manual parser configuration, while Rich supplies polished tables, panels, and spinners that argparse and bare Click provide no equivalent for. The codebase shows no commit messages, comments, or configuration evaluating Click or argparse as alternatives — the choice was made at project inception and has been extended, never revisited, across 20+ subsequent commits.

[NEEDS INPUT: Was this an explicit trade-off analysis, or a convenience/familiarity choice made without comparing alternatives?]

## Pros and Cons of the Options

### Typer with Rich

* Good, because type hints drive both parsing and automatic help generation, keeping command signatures self-documenting.
* Good, because Rich tables, panels, and spinners make CLI output scannable without manual ANSI formatting.
* Good, because it integrates natively with standard Python type annotations already used elsewhere in the codebase.
* Bad, because the decorator-driven architecture obscures control flow and makes `cli.py` difficult to unit test — there is no shared app instance across tests, and each invocation spawns a fresh argument parser.

### Click

* Good, because it is more mature and widely adopted, with Typer itself built on top of it.
* Good, because it uses a similar decorator-based command declaration model, so migration would not require an entirely different paradigm.
* Bad, because it is less type-hint-native than Typer, requiring more manual `@click.option()` boilerplate per parameter.
* Bad, because it has no bundled equivalent to Rich's tables/panels/spinners; the project would still need to add Rich separately for the same output quality.

### argparse

* Good, because it requires zero additional dependencies, being part of the Python standard library.
* Good, because it gives full manual control over parsing without any framework abstraction layer.
* Bad, because it has no type-hint integration, requiring verbose manual parser configuration across 24 commands.
* Bad, because it has no built-in rich-text formatting, leaving colored/tabular output to be hand-built on top of plain `print()`.

## Consequences

Typer and Rich are now non-negotiable dependencies: removing either would require rewriting all 24 commands and their output paths. The decorator model leaves `zettel/cli.py` with zero test coverage, in contrast to the underlying pipeline modules it orchestrates, because Typer's app-per-invocation design resists conventional unit testing. Because Typer models "one invocation = one command execution," every command independently calls `_load_deps()` -> `_get_db()` -> `_get_idx()` at startup rather than reusing a long-lived process, embedding stateful concerns such as embedding-model-mismatch handling into command startup instead of an always-on service.

Extensibility follows a fixed, predictable pattern: new commands are parameterized via `typer.Option()`/`typer.Argument()`, delegate to existing orchestration functions, and print results via Rich. This keeps the CLI surface consistent but rigid. Rich's output is human-optimized markup, not machine-parseable — any future scripting or programmatic-consumption use case (JSON export, CI integration) would need a parallel output path rather than reusing the existing formatting.

[NEEDS INPUT: Should the CLI add a `--json`/machine-readable output mode for programmatic consumers, or is human-readable Rich output the intended long-term interface?]

## References

* `zettel/cli.py:36-40` — Typer app initialization and Rich `Console` setup
* `zettel/cli.py:44-169` — dependency injection helpers (`_load_deps()`, `_get_db()`, `_get_idx()`) called at the start of every command
* `zettel/cli.py:174-246` — representative command declaration pattern (`init` command, with Rich `Panel` output)
* `zettel/__main__.py:3` — entry point invoking the Typer `app()`
* `pyproject.toml` — declares `typer>=0.21.1` and `typer-slim==0.21.1` as dependencies
