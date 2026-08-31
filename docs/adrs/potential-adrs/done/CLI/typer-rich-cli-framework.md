# Potential ADR: Typer + Rich as CLI Framework

**Module**: CLI
**Category**: Primary Framework / CLI Orchestration
**Priority**: Must Document (Score: 150)
**Date Identified**: 2026-08-30

---

## Existing ADR Context

No directly related ADRs exist. Related decisions in progress:
- **Multi-Provider LLM Strategy** (LLM module) — different framework layer (LLM providers vs. CLI routing)
- **FastAPI + Server-Rendered Jinja2** (WEB module) — complementary presentation layer choice for web UI

Timeline: Typer chosen from project inception (2026-02-01), predating all other frameworks by the entire project duration.

---

## What Was Identified

The Zettelkasten CLI application is entirely structured on **Typer** (Python CLI framework, v0.21+) with **Rich** (Python rich-text terminal library) for output formatting and interactivity. Every command (`zettel init`, `zettel harvest`, `zettel extract`, `zettel review`, `zettel connect`, `zettel garden`, `zettel ask`, `zettel article`, `zettel sync-manual`, `zettel delete-source`, `zettel new-note`, etc. — 24 total commands) is defined as a `@app.command()` decorated function on a root Typer `app` instance, with command-line parameter routing via `typer.Option()` and `typer.Argument()` annotations.

Rich is deeply integrated for:
- **Colored output and styling** (green/red/yellow status messages)
- **Interactive prompts** (`typer.confirm()`, manual prompt chains)
- **Formatted tables** (`Table`, `Panel`, formatted columns) for pipeline statistics and diagnostic output
- **Progress spinners** (`console.status()`) for long-running operations

This pattern was established in the initial commit (2026-02-01:zettel/cli.py) and has been stable and extended consistently throughout the project (commit history shows 20+ commits modifying cli.py, all maintaining the same Typer+Rich foundation).

**Temporal Context**: Stable architectural choice for 6+ months (February 2026 → August 2026), with no alternative framework introduced or debated in the codebase.

---

## Why This Might Deserve an ADR

### Impact
- **Scope**: All 24 CLI commands, every user interaction with the CLI, every invocation of the pipeline
- **Code Footprint**: 156 instances of Typer-specific patterns (`@app.command()`, `typer.Option()`, `typer.confirm()`), 127 instances of Rich-specific patterns (`console.print()`, `Panel`, `Table`) across `zettel/cli.py` (1,934 lines)
- **Abstraction Level**: Typer and Rich are primary frameworks, not implementation details — they directly shape how commands are declared, how dependencies are passed, how output is rendered

### Trade-offs Embedded
- **Typer Pros**: 
  - Automatic CLI parsing, type hints drive parameter declarations, minimal boilerplate
  - Built-in help generation, automatic shell completions (though disabled in this project via `add_completion=False`)
  - Integrates with standard Python type annotations
- **Typer Cons**:
  - Decorators-driven architecture can obscure control flow
  - Not suitable for long-running services (one invocation per command)
  - Limited for distributed command routing (each command is a fresh process)
  
- **Rich Pros**:
  - Terminal formatting without low-level ANSI knowledge
  - Interactive components (prompts) that feel polished
  - Tables/panels make CLI output scannable
- **Rich Cons**:
  - Another dependency for a "just terminal output" concern
  - Output formatting rules live in code, not templates/themes
  - Not suitable for machine-readable output (JSON/XML export would need parallel code)

### Consequences
- **Dependency Contract**: Typer and Rich are non-negotiable dependencies. Removing either would require rewriting all 24 commands and output paths.
- **Testing Boundary**: CLI layer itself has zero test coverage (confirmed in mapping.md) — testing CLI code is difficult with Typer's decorator model (no shared app instance across tests, each command invocation spawns a new argument parser).
- **Command Lifecycle**: Typer model is "one invocation = one command execution." Stateful operations (like embedding model mismatch handling) must be embedded in command startup via functions like `_get_idx()`.
- **Extensibility**: Adding new commands follows a fixed pattern (parameterize via `typer.Option()`, call orchestration functions, print results via Rich). This is predictable but rigid.

### Team Knowledge Requirement
- **Essential for**: Anyone adding/modifying a CLI command (any contributor working on `zettel/cli.py`)
- **Helpful for**: Developers debugging CLI behavior, understanding how parameters flow from command-line → `@app.command()` function
- **Can ignore**: Non-CLI developers (web UI maintainers, pipeline module authors) as long as the CLI/pipeline boundaries are clean

---

## Evidence Found in Codebase

### Key Files
- [`zettel/cli.py`](../../../../cli.py) - Lines 1-50 (framework setup), lines 36-40 (Typer app initialization), lines 44-169 (dependency injection helpers), commands scattered throughout
- [`zettel/__main__.py`](../../../../__main__.py) - Line 3 (imports `main` from cli, which calls `app()`)
- `pyproject.toml` - Declares `typer>=0.21.1` and `typer-slim==0.21.1` as dependencies

### Code Evidence

**Framework Setup (cli.py:36-40)**:
```python
app = typer.Typer(
    name="zettel",
    help="Zettelkasten — Pipeline automatizado de geração de notas",
    add_completion=False,
)
console = Console()
```

**Command Declaration Pattern (cli.py:174-246, `init` command example)**:
```python
@app.command()
def init(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Caminho para config.yaml"),
    vault: Optional[str] = typer.Option(None, "--vault", help="Caminho do vault (override)"),
    inbox: Optional[str] = typer.Option(None, "--inbox", help="Caminho do inbox (override)"),
    reset: bool = typer.Option(False, "--reset", help="Apagar e recriar todas as bases de dados"),
):
    """Inicializar o vault, banco de dados e índice vetorial."""
    cfg = _load_deps(config)
    # ... command body
```

**Rich Formatting (cli.py:237-245)**:
```python
console.print(Panel(
    f"[green]Vault inicializado em:[/green] {cfg.vault_path}\n"
    f"[green]Inbox:[/green] {cfg.inbox_path}\n"
    f"[green]State DB:[/green] {cfg.state_db_path}\n"
    f"[green]ChromaDB:[/green] {cfg.chroma_path}\n"
    f"{device_line}{reset_line}",
    title="Zettelkasten -- Init",
    border_style="green",
))
```

**Interactive Prompt with Rich (cli.py:120-123, embedding reindex confirmation)**:
```python
def _confirm_embedding_reprocess(yes: bool) -> bool:
    if yes:
        return True
    return typer.confirm("Reprocessar todos os embeddings agora?", default=False)
```

### Impact Analysis
- **Introduced**: 2026-02-01 (initial commit)
- **Modified**: 20+ commits over 6+ months
- **Recent activity**: 2026-08-29 (feat(cli): add new-note and delete-source with MOC backrefs)
- **Themes**: Feature additions (`new-note`, `delete-source`), pipeline enhancements (`garden --hubs`, `--dump-chunks`), parameter refinement (all changes extend the framework rather than questioning it)
- **File count affected**: 1 file directly (`zettel/cli.py`), 1 entry point (`zettel/__main__.py`), 2 dependencies in manifest

### Alternatives (implicit in design)
The codebase makes no mention of alternative CLI frameworks. However, the field offers clear alternatives:
- **Click** (Flask's sibling CLI framework) — more mature, widely used, decorator-based like Typer but less type-hint friendly
- **argparse** (stdlib) — zero-dependency but verbose, no type-hint integration
- **Invoke** (Fabric's task runner) — task-oriented, less suitable for parameter-driven commands
- **Cement** — full-featured CLI framework with plugins, arguably overcomplicated for this use case

No explicit commit messages, comments, or configuration suggest evaluation of these alternatives. Typer+Rich choice appears to have been made early based on convenience/pythonic-ness rather than explicit trade-off analysis.

---

## Questions to Address in ADR (if created)

1. **Why Typer specifically?** What drove the choice over Click (more mature), argparse (zero-dep), or others? Was it type-hint-first philosophy, or just familiarity?

2. **Why no CLI layer testing?** Is the zero-coverage of `cli.py` a consequence of Typer's decorator model (hard to unit-test) or an explicit decision to test only the underlying pipeline modules and accept CLI as a thin integration layer?

3. **Output format lock-in**: Rich-formatted output is human-optimized but not machine-parseable (JSON export, scripting use cases). Should the project provide a `--json` flag or parallel JSON-output mode for programmatic use?

4. **Command-level composition pattern**: Every command independently calls `_load_deps()` → `_get_db()` → `_get_idx()` → close in sequence. Could shared state (application singleton) reduce resource churn, or is command isolation intentional?

5. **Completion disabled**: Typer can generate shell completions, but the project disables them (`add_completion=False`). Why? Is this a performance concern, or planned for future enablement?

---

## Related Potential ADRs

- **Composition Root Pattern with Lazy Dependency Loading** (considered but scores 45, below threshold) — how dependencies are wired within the CLI
- **No CLI Test Coverage** (documentation gap rather than architectural decision) — testing strategy consequence of framework choice

---

## Additional Notes

- **Project-Wide Consistency**: Rich usage is confined to the CLI (`zettel/cli.py`) and web UI (`zettel/web.py` for status tables). No Rich usage in pipeline modules, maintaining clear separation of concerns (presentation vs. logic).
- **Rich Dependency Lock-In**: Markdown rendering for the web UI (`zettel/markdown.py`) uses `markdown-it-py` + `bleach` independently, not Rich. Rich is CLI-only.
- **Typer as Sole CLI Abstraction**: No alternative command routers (no manual `sys.argv` parsing, no command dispatcher). Typer is the sole CLI abstraction.
- **Observability/Debugging**: The `[dim]` formatting in status messages (e.g., line 316, `"[dim]Coletando arquivos..."`) suggests a deliberate design to deemphasize verbose output. This is possible because Rich's markup language is expressive.

---

## Scoring Summary

| Dimension | Points | Justification |
|-----------|--------|---------------|
| **Base (Step 0: Primary Framework)** | 75 | Typer is the primary framework structuring the CLI application |
| **Scope + Impact** | 25 | Affects all 24 commands, entire CLI surface, every user interaction |
| **Cost to Change** | 25 | Would require rewriting entire CLI orchestration layer, weeks of effort |
| **Team Knowledge** | 25 | Required for anyone adding/modifying commands; essential to CLI contributors |
| **TOTAL** | **150** | **MUST-DOCUMENT** |

**Classification**: High-priority architectural decision. Framework choice that shapes the entire CLI application structure and will require careful migration planning if ever revisited.
