# ADR-032: CLI as Python Package

**Status**: Accepted (2026-09-03)  
**Depends on**: [ADR-026](./ADR-026-typer-rich-cli-framework.md)  
**Relates to**: [ADR-008](../INFRA/ADR-008-repository-pattern-data-access.md), [ADR-027](../HARVEST/ADR-027-harvest-phase-as-python-package.md), [ADR-029](../QA-WRITING/ADR-029-article-graph-as-python-package.md)

## Context

`zettel/cli.py` had grown to 2085 lines holding 22 Typer commands plus everything
they share. It was the largest file in the project and the sole user-facing entry
point — the widest blast radius in the codebase — while also being the least
covered: exactly one test touched it, and that test covered a formatter.

Four concerns lived in the one file:

| Concern | Size |
|---|---|
| Composition root (`_load_deps`, `_get_db`, `_get_idx`, `_idx_kwargs`) plus the embedding-drift UX | ~200 lines |
| 22 command bodies, `init` through `doctor` | ~1750 lines |
| Rich renderers (cost-by-phase table, four hand-built metric tables) | ~120 lines |
| Repeated option declarations (`--config/-c` in 21 signatures, `--yes/-y` in 12) | ~200 lines |

Reading the whole file to make the split surfaced three concrete defects that the
size had been hiding, and which this decision fixes rather than relocates:

1. **Duplication with the web worker.** `_idx_kwargs` and `_load_approved_candidates`
   existed verbatim in `zettel/web_app.py`. CLAUDE.md *documented* the obligation to
   mirror them by hand — and they had already drifted: the web copy omitted
   `embedding.dimensions`, so a dimension-reducing model wrote full-width vectors
   through the web and reduced ones through the CLI, into the same Chroma store.
   Resolved separately in ADR-008's stores (`index.index_kwargs`,
   `connector.load_approved_candidates`, `llm.REQUIRED_PROMPTS`).
2. **Dead and misleading code.** `import logging` and `import sys` were never used;
   an `if __name__ == "__main__"` block with a stopwatch never executed, because the
   entry point is `zettel/__main__.py`; and `harvest --move-processed`, documented as
   *"move o arquivo para data/processed/"*, only created the directory and told the
   user to move the files themselves.
3. **Help-text drift.** Each re-declaration of `--config` and `--yes` re-typed its
   own help string, and they had already diverged between commands.

This is the third instance of the same problem in the repository, and the previous
two are already decided: ADR-027 turned `zettel/harvester.py` (1776 lines) into a
package of 8 modules, and ADR-029 did the same for `zettel/article_graph.py`
(716 lines). Applying the identical pattern here closes the set.

## Decision

Convert `zettel/cli.py` into the package `zettel/cli/` — four infrastructure
modules and ten command modules grouped by pipeline phase:

```
zettel/cli/
├── __init__.py       # ~77  Assembles the app; import order = --help order
├── app.py            # ~33  The `app` / `console` singletons — imports nothing local
├── deps.py           # ~125 Composition root + embedding-drift UX
├── formatting.py     # ~144 Rich renderers (cost table, metrics_table, formatters)
├── options.py        # ~161 Shared Annotated options + flag resolvers
├── maintenance.py    # ~226 init, reindex, rebuild
├── ingest.py         # ~309 harvest, rechunk, dump-chunks, dump-extraction, set-paging
├── curation.py       # ~133 extract, review, retry-failed
├── synthesis.py      # ~127 connect, garden
├── purge.py          # ~191 purge-rejected, delete-source
├── manual.py         # ~240 new-note, sync-manual
├── pipeline.py       # ~118 run-all
├── qa.py             # ~151 ask
├── writing.py        # ~235 article
└── diagnostics.py    # ~308 status, doctor
```

`qa.py` and `writing.py` are deliberately not named `ask.py` and `article.py`:
their commands import `zettel/ask.py` and `zettel/article.py`, and two identically
named things in one import block is a readability regression — the same reasoning
ADR-029 used to name its state module `runtime.py`.

### Two structural seams

**1. `app.py` imports nothing from the package.** It creates `app` and `console`;
every command module imports them from it. Because `__init__.py` must import the
command modules to trigger their `@app.command()` registration, any import from
`app.py` back into the package closes a cycle and kills `python -m zettel` during
module initialisation. This is why the singletons do not live in `__init__.py`.

**2. The import order in `__init__.py` is the order of `zettel --help`.** Command
registration is a side effect of importing a module, so the sequence of import
statements is the sequence of the help listing. The modules are bound to a
`COMMAND_MODULES` tuple so those imports are real references rather than
side-effect-only statements that an "unused import" cleanup would delete —
silently removing commands from the CLI.

Both seams, and the lazy-import rule below, are enforced by AST checks in
`tests/test_cli.py` rather than left as convention in a docstring.

### Import rules

Inherited from ADR-027, with one deviation:

* **Siblings by absolute import** (`from zettel.cli.deps import get_idx`), not the
  relative imports ADR-027 chose. The package's module names sit close to domain
  module names (`zettel.cli.qa` beside `zettel.ask`), and the absolute form makes
  which one is meant unambiguous at the import site.
* **Domain modules stay imported inside the command function**, as they already
  were. This is what keeps `zettel --help` from loading chromadb, docling and
  langchain — seconds of startup for a help screen.
* **No command module imports another command module.** A command needing another
  command's behaviour needs the domain function both call; that is what `run-all`
  does.

### Shared options

`Annotated` aliases in `options.py` replace the repeated declarations. An alias is
created **only when two or more commands declare the same flags with the same help
text**. Where a flag carries extra meaning in one command — `harvest --yes` also
selects the duplicate action, `garden --yes` also confirms `--recreate`,
`review --yes` also sets the approval threshold behaviour — that command keeps its
own inline declaration. Collapsing those would delete information the user needs.

### Removals

| Item | Rationale |
|---|---|
| `import logging`, `import sys` | Never used. |
| `if __name__ == "__main__"` stopwatch block (and `import time`) | Never executed: the entry point is `zettel/__main__.py` → `main()`. Printing elapsed time only under `python zettel/cli.py` is undocumented and inconsistent. |
| `harvest --move-processed` | The flag never moved anything. It created `data/processed/` and printed that the user could move files manually. Harvest is idempotent by file hash, so leaving files in the inbox does not reprocess them. Removed from `docs/cli.md` and `docs/arquitetura.md` too. |
| Four duplicated `Table` constructions | Replaced by `formatting.metrics_table`. |

## Consequences

### Advantages

✓ **Progressive disclosure**: no module over ~310 lines; an agent changing one
command loads ~150 lines instead of 2085  
✓ **Nameable responsibilities**: each module's docstring says what its commands
have in common and why  
✓ **Enforced structure**: the seams are tests, not comments  
✓ **Testability**: the CLI went from 1 test to 40, covering the command surface,
every parser, the invariants and the pure helpers  
✓ **One source per contract**: help strings for shared flags exist once  
✓ **Consistency**: same package pattern as `zettel/harvester/` and
`zettel/article_graph/`

### Trade-offs

⚠ **The `--help` listing order changed.** The original order interleaved groups
(`init` first, `reindex`/`rebuild` sixteenth and seventeenth), which no module
grouping can reproduce from import order. Preserving it would have meant either
fragmenting modules to serve a cosmetic sequence, or mutating
`app.registered_commands` after import. The new order follows the life of a vault —
set up, ingest, curate, synthesise, prune, hand-write, run, query, inspect — and is
pinned by `tests/test_cli.py`.  
⚠ **Registration by import side effect** is less explicit than a manual registry.
Mitigated by `COMMAND_MODULES`, the golden-list test, and the module map in
`__init__.py`.  
⚠ **More files to navigate** for a reader who wanted one file.  
⚠ **One test target moved**: `tests/test_usage.py` imported
`zettel.cli._fmt_prompt_cache_ratio`; it now imports
`zettel.cli.formatting.fmt_prompt_cache_ratio`. Same "test migration" trade-off
ADR-027 accepted.

### No architectural changes

* `from zettel.cli import main` still works; `zettel/__main__.py` is untouched
* All 22 commands keep their names, flags and behaviour
* Every per-command `--help` block is byte-identical to before, verified by diffing
  a captured baseline; the only differences in the whole surface are the root
  listing order and the removed `--move-processed`
* Typer and Rich are unchanged (ADR-026 stands); no compatibility shim or alias for
  `zettel.cli` as a module is introduced

### File deletion order (critical on Windows)

A package directory takes precedence over a same-named module, so both can exist
during the transition:

```bash
# Step 1: the shared-helper extraction, independent of the split
git commit -m "Unify index_kwargs, load_approved_candidates, REQUIRED_PROMPTS"

# Step 2: the complete package
git add zettel/cli/
git commit -m "Extract cli package modules"

# Step 3: only then delete the monolith
git rm zettel/cli.py
git commit -m "Remove monolithic cli.py (migrated to package)"
```

Reason: Windows file locks; git tracking clarity.

## Alternatives Considered

1. **Keep the monolith, add section comments** (~2085 lines)  
   - Rejected: does not reduce what a reader or an agent must load, and leaves the
     duplication and dead code in place.

2. **A `zettel/cli/commands/` subpackage**  
   - Rejected: over-engineering for ten command modules. ADR-027 reached the same
     conclusion for eight — a flat package is sufficient.

3. **Flat sibling modules at the top level** (`cli_ingest.py`, `cli_purge.py`, …)  
   - Rejected: ADR-027 already evaluated and rejected this shape — a flat namespace
     does not scale and invites circular imports. It would also scatter fourteen
     `cli_*` files across the top of `zettel/`.

4. **Explicit registration in `__init__.py`** (`app.command(name="purge-rejected")(purge_rejected_cmd)`)  
   - Rejected: it would preserve the original `--help` order and remove the import
     side effect, but it splits each command's identity — its name — from its
     definition, and the golden-list test already covers what it would protect.

5. **Reordering `app.registered_commands` after import** to preserve the old order  
   - Rejected: reaching into Typer's internals to serve a cosmetic ordering is
     exactly the kind of workaround the project does not accept.

## Acceptance Criteria

- [x] `zettel/cli.py` deleted; `zettel/cli/` exists with 15 modules
- [x] No module in the package exceeds ~310 lines
- [x] Public API maintained: `from zettel.cli import main` works; `zettel/__main__.py` untouched
- [x] All 22 commands registered; every per-command `--help` byte-identical except `harvest`
- [x] `app.py` imports nothing from the package; no command module imports another
- [x] No domain module imported at package module scope
- [x] `--move-processed`, the dead imports and the stopwatch block removed
- [x] `tests/test_cli.py` covers the surface, the parsers, the invariants and the helpers
- [x] Full suite passes (811 tests); `ruff check zettel/cli/` introduces no new findings
- [x] CLAUDE.md, `docs/cli.md` and `docs/arquitetura.md` describe the package
- [x] ADR-008, ADR-024, ADR-026 and ADR-028 no longer reference `zettel/cli.py`

## Related ADRs

- **ADR-026** (Typer + Rich): the framework decision this package structures. Its
  stated consequence of zero test coverage no longer holds.
- **ADR-027** (harvest package): the precedent — same problem, same solution, same
  import rules.
- **ADR-029** (article_graph package): the second instance, and the source of the
  "name a module for what it is, not for what it wraps" reasoning.
- **ADR-008** (repository pattern): `deps.py` is the composition root that ADR
  describes.

## References

* `zettel/cli/__init__.py` — module map, the two seams, `COMMAND_MODULES`
* `zettel/cli/app.py` — the `app` / `console` singletons
* `zettel/cli/deps.py` — `load_deps`, `get_db`, `get_idx`, embedding-drift resolution
* `zettel/cli/options.py` — shared `Annotated` options and the flag resolvers
* `zettel/cli/formatting.py` — `metrics_table`, `print_cost_by_phase`
* `zettel/__main__.py` — the unchanged entry point
* `tests/test_cli.py` — golden command list, parser smoke, AST invariant checks
* `zettel/index.py` — `index_kwargs`, the single config-to-VectorIndex translation
* `zettel/connector.py` — `load_approved_candidates`, the Phase 3 entry gate
* `zettel/llm.py` — `REQUIRED_PROMPTS`, the canonical prompt list the doctor checks
