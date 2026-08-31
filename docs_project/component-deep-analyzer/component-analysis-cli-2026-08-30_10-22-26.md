# Component Deep Analysis Report — `cli` (zettel/cli.py)

## 1. Executive Summary

`zettel/cli.py` is the sole user-facing entry point of the Zettelkasten pipeline and the orchestration root for every phase described in `CLAUDE.md` (harvest → extract → review → connect → garden), plus a set of maintenance/administrative commands (`sync-manual`, `new-note`, `delete-source`, `purge-rejected`, `reindex`, `rebuild`, `rechunk`, `set-paging`, `dump-chunks`, `dump-extraction`, `retry-failed`, `status`, `doctor`, `init`) and two retrieval-oriented commands (`ask`, `article`).

The file is a single Typer application (`app = typer.Typer(...)`) of 1934 lines containing 22 `@app.command()` functions and 11 module-level helper functions. It owns no business logic of its own beyond argument validation, flag-resolution, confirmation gating, and presentation (Rich tables/panels); every substantive operation is delegated to a dedicated module (`harvester`, `extractor`, `review`, `connector`, `gardener`/`gardener_hub`, `sync`, `rebuild`, `purge_source`, `new_note`, `ask`, `article`/`article_graph`, `chunk_dump`, `extraction_dump`, `taxonomy`). This makes `cli.py` a thin but extremely wide orchestration layer: it is the **highest efferent-coupling module in the codebase** (21 distinct internal `zettel.*` module dependencies), because every pipeline phase and every maintenance operation is wired through it.

Key findings:

- **Single composition root for `(AppConfig, StateDB, VectorIndex)`.** Every command follows the same three-step boilerplate (`_load_deps` → `_get_db` → `_get_idx`), and `_get_idx` is the single choke point that detects and resolves ChromaDB/config embedding-space drift (`EmbeddingSpaceMismatch`) across the entire CLI surface.
- **No dedicated automated test coverage.** No test file in `tests/` imports `zettel.cli`, uses Typer's `CliRunner`, or drives the module via subprocess. All of the flag-resolution and confirmation logic that lives directly in this file (mutual exclusivity checks, duplicate-flag precedence, destructive-action confirmations) is therefore exercised only manually.
- **Deliberate CLI/Web asymmetry.** `CLAUDE.md` and `web_app.py` both call out that `web_app.py::_idx_kwargs` must mirror `cli.py::_idx_kwargs`; several commands here (`new-note`, `delete-source`, `ask`, `article`, `run-all`, `purge-rejected`, `reindex`/`rebuild`, `garden --recreate`, `init --reset`, `set-paging`, `rechunk`, dumps, `doctor`, `status`) are intentionally **not** exposed in the web UI, making this file the only interface to a meaningful share of the system's functionality.
- **One confirmed dead/tautological expression** in the `harvest` command (`skip_paging=skip_paging or True`, line 336) that silently ignores the actual value of the `--skip-paging` flag in non-interactive mode (documented under Technical Debt).
- Destructive operations (`delete-source`, `purge-rejected`, `init --reset`, `garden --recreate`) consistently follow a "count-and-warn → confirm (unless `--yes`) → execute → report" pattern, and irreversible ones append an opt-out `VACUUM`/compaction step (`--no-compact`).

---

## 2. Data Flow Analysis

Because this component is a command dispatcher, "data flow" is best expressed per representative command rather than as one linear pipeline. All commands share the same bootstrap prefix.

**Shared bootstrap (every command):**
```
1. Typer parses CLI args/options into typed Python parameters
2. _load_deps(config) -> AppConfig (loads config/config.yaml or --config path, calls setup_logging)
3. _get_db(cfg) -> StateDB(cfg.state_db_path)   [most commands]
4. _get_idx(cfg, db, yes) -> VectorIndex          [commands touching Chroma]
     4a. VectorIndex(**_idx_kwargs(cfg)) attempted
     4b. on EmbeddingSpaceMismatch: _warn_embedding_mismatch -> _confirm_embedding_reprocess
     4c. if confirmed: VectorIndex(reset_mismatched=True) + rebuild.run_reindex(force=True)
     4d. if declined: console error + raise typer.Exit(1)
```

**`harvest` (representative full pipeline phase):**
```
1. CLI flags parsed (--yes/--skip-duplicates/--force/--skip-biblio/--content-start-*/--skip-paging/--dump-*)
2. cfg, db, idx bootstrapped (embedding-drift gate runs here)
3. _resolve_duplicate_flags(yes, skip_duplicates, force) -> (interactive, duplicate_action)
   - raises typer.Exit(1) if --skip-duplicates and --force both set
4. _resolve_chunk_dump_dir / _resolve_extraction_dump_dir resolve optional dump paths
5. zettel.harvester.run_harvest(cfg, db, idx, interactive=..., duplicate_action=..., skip_paging=..., dump_dir=..., extraction_dump_dir=...)
6. Result (list of new source_ids) rendered as Rich console lines
7. db.get_last_run() queried to report duplicate counts (file/content/semantic) in yellow
8. db.close()
```

**`ask` (retrieval command):**
```
1. cfg, db, idx bootstrapped
2. zettel.ask.run_ask(cfg, db, idx, question, topk, use_graph, mode) -> AskResult
3. result.answer rendered in a Rich Panel
4. if --show-context: result.retrieval_params rendered as a parameters table
5. result.candidates (always populated) rendered as a "Notas recuperadas" table with provenance columns
6. Save flow: --save-to path > --save flag > interactive Confirm.ask prompt (unless --no-save-prompt)
   -> zettel.ask.save_ask_note(result, cfg.vault_path, [path])
7. db.close()
```

**`connect` / `run-all` (candidate loading):**
```
1. cfg, db, idx bootstrapped
2. _load_approved_candidates(db): db.get_concepts_by_status("approved", without_notes=True)
   -> deserializes candidate_json via PermanentNoteCandidate.model_validate_json
3. if empty: error + typer.Exit(1) (connect only; run-all proceeds because it interleaves phases)
4. zettel.connector.run_connect(cfg, db, idx, candidates) -> note_ids
5. Rendered as console list
```

**`delete-source` / `purge-rejected` (destructive-action pattern):**
```
1. cfg, db bootstrapped
2. Pre-flight counts computed (chunks, linked permanent notes / rejected chunk count)
3. Console warning of scope (what will be deleted, whether VACUUM will run)
4. typer.confirm(...) unless --yes; abort (return/Exit) otherwise
5. idx bootstrapped only after confirmation
6. zettel.purge_source.purge_source(...) / zettel.review.purge_rejected(...) executed
7. Result dict rendered across multiple Rich console lines (vault, sqlite, chroma, compaction before/after MB)
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Validation | `--skip-duplicates` and `--force` are mutually exclusive on `harvest`/`run-all` | zettel/cli.py:379-381 |
| Validation | `rechunk`/`dump-chunks`/`dump-extraction` require exactly one of `--source-id` or `--all` | zettel/cli.py:812-814, 855-857, 894-896 |
| Validation | `rebuild --what` must be one of `vault`\|`chroma`\|`all` | zettel/cli.py:1218-1221 |
| Validation | `article --style` must be `blog`\|`academic` | zettel/cli.py:1512-1515 |
| Business Logic | Duplicate-flag precedence: `skip_duplicates` > `force` > `yes` > interactive default | zettel/cli.py:375-388 |
| Business Logic | Embedding-space drift gate wraps every `VectorIndex` open across the CLI | zettel/cli.py:126-169 |
| Business Logic | `reindex` proactively peeks Chroma's stored embedding identity before opening the index and auto-applies `--force` on drift | zettel/cli.py:1156-1184 |
| Business Logic | Save-destination precedence for `ask`/`article`: `--save-to` > `--save` > interactive prompt (skippable via `--no-save-prompt`) | zettel/cli.py:1441-1451, 1631-1642 |
| Business Logic | `new-note` derives `effective_source_id` from `--citekey` only for `permanent`/`source` note types | zettel/cli.py:1043-1047 |
| Business Logic | `run-all` forces `skip_paging = not interactive` and stops before Phase 3 when `--dry-run` | zettel/cli.py:1286, 1318-1321 |
| Confirmation Gate | Destructive operations (`delete-source`, `purge-rejected`, `garden --recreate`, `init --reset`) require an explicit `typer.confirm` unless `--yes` | zettel/cli.py:191-196, 523-526, 599-602, 713-719 |
| Confirmation Gate | Embedding reprocessing (drift-triggered full reindex) requires `typer.confirm` unless `--yes` | zettel/cli.py:120-123 |
| Side Effect | VACUUM/compaction runs by default after `purge-rejected` and `delete-source`, opt-out via `--no-compact` | zettel/cli.py:494-497, 518-522, 559-562, 595-598 |
| Side Effect | `delete-source` keeps linked permanent (ZTL) notes by default; only removed via `--delete-permanent` | zettel/cli.py:555-558, 587-594 |
| Diagnostic | `doctor` aggregates ~20 independent OK/FAIL checks (config, paths, prompt files, FTS5, required/optional deps, GPU, taxonomy, embedding drift, chunking coverage) without mutating state | zettel/cli.py:1741-1919 |

### Detailed breakdown of the business rules

---

### Business Rule: Duplicate-Flag Mutual Exclusivity and Precedence (`_resolve_duplicate_flags`)

**Overview**:
`harvest` and `run-all` expose three overlapping flags for handling suspected duplicate sources during ingestion: `--yes`, `--skip-duplicates`, and `--force`. `_resolve_duplicate_flags` (zettel/cli.py:375-388) is the single function that reconciles them into the two-value contract expected by `harvester.run_harvest`: `(interactive: bool, duplicate_action: str | None)`.

**Detailed description**:
The function first checks for mutual exclusivity: `--skip-duplicates` and `--force` represent opposite non-interactive resolutions for the same ambiguous situation (a file that resembles an existing source semantically). Passing both is treated as a user error and terminates the command immediately with `typer.Exit(1)` before any database or index connection is opened — this validation runs *before* `_load_deps`/`_get_db`/`_get_idx` are even called in `run_all`, but notably *after* them in `harvest` (harvest opens `cfg`/`db`/`idx` first, then calls `_resolve_duplicate_flags`, meaning a `db`/`idx` handle is opened and left dangling — not explicitly closed — if the flag combination is invalid; this is a minor resource-lifecycle asymmetry between the two call sites).

Precedence, once the exclusivity check passes, follows a strict priority order: `--skip-duplicates` wins outright (returns `(False, "skip")`), then `--force` (returns `(False, "continue")`), then `--yes` (returns `(False, None)`, deferring to `harvest.non_interactive_duplicate_action` from `config.yaml`), and only if none of the three flags are set does the function return `(True, None)` — the fully interactive Rich-prompt path handled deeper inside `harvester.py`. This means `--yes` alone does not pick a specific duplicate resolution; it only disables interactivity and lets the configured default decide, whereas `--skip-duplicates`/`--force` are explicit overrides that make sense even without `--yes`.

**Rule workflow**:
```
skip_duplicates and force?  -> console error + typer.Exit(1)
skip_duplicates?            -> (interactive=False, duplicate_action="skip")
force?                      -> (interactive=False, duplicate_action="continue")
yes?                        -> (interactive=False, duplicate_action=None)  # config default applies
else                        -> (interactive=True,  duplicate_action=None)  # Rich prompt per file
```

---

### Business Rule: Embedding-Space Drift Gate (`_get_idx`, `_warn_embedding_mismatch`, `_confirm_embedding_reprocess`)

**Overview**:
Nearly every command that touches ChromaDB opens the vector index through `_get_idx` rather than instantiating `VectorIndex` directly. This function is the CLI's single enforcement point for the invariant that ChromaDB's stored embedding identity (provider/model/dimensions) must match the currently configured one — protecting the corpus against silently mixing incompatible vector spaces.

**Detailed description**:
`_get_idx` first attempts a normal `VectorIndex(**_idx_kwargs(cfg))` open. If `VectorIndex` raises `EmbeddingSpaceMismatch` (defined in `zettel.index`), the CLI does not fail silently or degrade — it prints a detailed bilingual (PT-BR) `Panel` via `_warn_embedding_mismatch` showing the stored vs. configured `{provider}/{model}@{dimensions}d` identifiers and explicitly warns that existing vectors are incompatible with the new embedding space and must be regenerated with `zettel reindex --force`. It then calls `_confirm_embedding_reprocess(yes)`, which either short-circuits to `True` when `--yes` was passed, or prompts interactively via `typer.confirm(..., default=False)` — defaulting to **not** reprocessing, since a full re-embed can be an expensive, LLM-adjacent-cost operation (embedding calls are billed and tracked by `CostTracker`).

If the user declines, the command aborts with `typer.Exit(1)` and a message pointing at the manual recovery path. If confirmed, `_get_idx` opens a *second* `StateDB` connection only when the caller did not already pass one (`own_db = db is None`), reopens `VectorIndex` with `reset_mismatched=True` (which resets/clears the incompatible collection), and calls `zettel.rebuild.run_reindex(cfg, db, idx, force=True)` inside a Rich spinner, printing a summary table of vectors reindexed per collection before returning the now-consistent index. The `finally` block ensures a self-opened `db` handle is always closed, but a caller-supplied `db` is left open for the caller to manage — this ownership transfer is implicit and relies on every call site being aware of it (all 15 call sites of `_get_idx` do pass their own already-open `db`, so in practice `own_db` is only ever `True` from the `init` command, which does not pass `db=None` explicitly but rather always supplies its own `db`, making the `own_db=True` branch effectively dead in the current call graph — see Technical Debt).

**Rule workflow**:
```
try VectorIndex(**_idx_kwargs(cfg))
  -> success: return idx
except EmbeddingSpaceMismatch as exc:
  print drift panel (stored vs current identity)
  if not confirm_reprocess(yes):
     print abort message -> typer.Exit(1)
  own_db = (db is None)
  if own_db: db = _get_db(cfg)
  idx = VectorIndex(reset_mismatched=True, **rest)
  run_reindex(cfg, db, idx, force=True)   # inside console.status spinner
  print reindex stats table
  if own_db: db.close()
  return idx
```

---

### Business Rule: Proactive Drift Detection in `reindex` (bypasses the try/except gate)

**Overview**:
Unlike every other command, the `reindex` command (zettel/cli.py:1133-1195) does not rely on `_get_idx`'s try/except flow. It proactively calls `zettel.index.peek_stored_embedding_identity(cfg.chroma_path)` *before* attempting to open `VectorIndex` at all, and manually re-implements the same warn/confirm/force sequence.

**Detailed description**:
This duplication exists because `reindex` needs to force-apply `--force` automatically the moment drift is detected — regardless of whether the user also passed `--force` explicitly — since without it, "sources/chunks antigos nao seriam regenerados" (old sources/chunks would not be regenerated) per the command's own docstring. The drift check compares each of `stored[0..2]` (provider, model, dimensions) against `cfg.embedding.provider/model/dimensions`, but only considers it drift if at least one stored field is non-`None` (an empty/never-initialized Chroma store is not treated as drift). When drift is found, it constructs an `EmbeddingSpaceMismatch` manually (rather than catching one raised by `VectorIndex.__init__`), reuses the same `_warn_embedding_mismatch`/`_confirm_embedding_reprocess` helpers for UX consistency, and on confirmation sets `force = True` before opening `VectorIndex(reset_mismatched=True)`. If the user already passed `--force` explicitly, the confirmation is skipped entirely (`if not force and not _confirm_embedding_reprocess(yes)`) — meaning `--force` on `reindex` doubles as an implicit `--yes` for the drift-specific confirmation, which is a different semantic than `--yes` has anywhere else in the CLI (worth noting as an inconsistency, see Technical Debt).

**Rule workflow**:
```
stored = peek_stored_embedding_identity(chroma_path)
drift = any(stored[i] is not None) and (stored != current config identity)
if drift:
   build EmbeddingSpaceMismatch, print warning panel
   if not force and not confirm_reprocess(yes): abort (Exit 1)
   force = True   # auto-escalate regardless of how confirmation was satisfied
   idx = VectorIndex(reset_mismatched=True)
else:
   idx = VectorIndex()   # normal open
run_reindex(cfg, db, idx, collection, force)
print stats table
```

---

### Business Rule: Destructive-Action Confirmation Pattern (`delete-source`, `purge-rejected`, `garden --recreate`, `init --reset`)

**Overview**:
Four commands perform irreversible deletions (vault files, SQLite rows, Chroma vectors, or all three). Each follows an identical three-part pattern: pre-flight scope disclosure, gated confirmation, then execution with a post-hoc summary — but the *scope disclosure* and *bypass condition* differ meaningfully per command.

**Detailed description**:
`delete-source` (zettel/cli.py:547-642) resolves the `source_id` first, fails fast with `typer.Exit(1)` if the source does not exist, then reports exactly how many chunks and linked permanent notes will be affected *before* asking for confirmation — critically, it distinguishes between `--delete-permanent` set (permanent notes will be deleted, shown in red) vs. unset (permanent notes kept, only dead wikilinks cleaned, shown dim). This means the confirmation prompt text itself ("Excluir fonte permanentemente?") is generic, but the console output immediately above it is dynamically scoped to the actual blast radius — a good practice, since a blanket "are you sure?" without context is a well-known anti-pattern for irreversible operations. `purge-rejected` (zettel/cli.py:486-544) short-circuits entirely (no confirmation, no-op return) when there are zero rejected chunks for the given `source_id` filter, avoiding a pointless confirmation prompt for a no-op. Both commands mention the VACUUM/compaction step in the pre-confirmation text so the user understands the operation may take noticeably longer than the raw delete.

`garden --recreate` (zettel/cli.py:691-745) is the odd one out: its confirmation happens *before* `db`/`idx` are even opened (line 713, versus line 721 for `_get_db`), and the confirmation text is parameterized only by whether `--hubs` was passed (`"taxonomia"` vs `"hub"` scope), not by an actual count of MOCs that will be deleted — the user is told *which pipeline* will be purged but not *how many* MOCs, unlike `delete-source`'s precise chunk/note counts. `init --reset` (zettel/cli.py:174-246) is the only one of the four with **no `--yes`-equivalent bypass at all** — there is no flag on `init` to skip the `typer.confirm` for `--reset`, making it the most conservative of the four despite being (arguably) the most severe, since it deletes the entire State DB, ChromaDB, and cache directory unconditionally once confirmed.

**Rule workflow**:
```
delete-source:
  resolve source -> not found? Exit(1)
  compute chunk_count, permanent_note_count
  print scope (red if --delete-permanent else dim)
  print VACUUM note (unless --no-compact)
  if not --yes: typer.confirm(...) else abort
  open idx (drift gate runs here, AFTER confirmation)
  purge_source(...) -> print vault/sqlite/chroma/compaction results

purge-rejected:
  count rejected chunks (optionally by source_id)
  count == 0 -> print + return (no prompt)
  print scope + VACUUM note
  if not --yes: typer.confirm(...) else abort
  purge_rejected(...) -> print results

garden --recreate:
  if not --yes: typer.confirm(generic scope text) else abort
  (only then) open db/idx
  run_garden(recreate=True) or run_garden_hubs(recreate=True)

init --reset:
  typer.confirm(...) unconditionally (no bypass flag exists)
  delete state_db(+ -wal/-shm), chroma_path tree, cache_path tree
```

---

### Business Rule: Mandatory Source Selector for Bulk-Style Commands (`rechunk`, `dump-chunks`, `dump-extraction`)

**Overview**:
Three commands operate over a set of sources reconstructed from already-persisted data (no LLM calls) and require the caller to be explicit about scope: either one specific `--source-id` or the blanket `--all`.

**Detailed description**:
Each of `rechunk`, `dump-chunks`, and `dump-extraction` independently repeats the same guard: `if not source_id and not all_sources: print error; raise typer.Exit(1)`. Unlike the mutual-exclusivity check in `_resolve_duplicate_flags`, this is an "at least one required" check, not an "at most one" check — the code does not actually forbid passing both `--source-id` and `--all` simultaneously; in that case the downstream module functions (`run_rechunk`, `run_dump_chunks`, `run_dump_extraction`) receive a non-`None` `source_id` and it is unclear from `cli.py` alone whether `--all` is then silently ignored (this ambiguity is a candidate for the Technical Debt section, since the same guard is copy-pasted three times with no shared helper, unlike the analogous chunk-dump-dir resolution which *was* factored into `_resolve_chunk_dump_dir`).

`rechunk` additionally opens the full `db`/`idx` stack (including the embedding-drift gate) even though rechunking itself only touches SQLite chunk rows and does not call the LLM — the drift gate here exists purely because rechunked text will eventually need re-embedding, so `_get_idx` is invoked defensively. `dump-chunks` and `dump-extraction`, by contrast, only open `db` (no `idx` at all), since they are pure read/export operations with no vector-space dependency — this asymmetry is a correct, deliberate design choice reflecting that these two commands never write to Chroma.

**Rule workflow**:
```
if source_id is None and all_sources is False:
    print "[red]Informe --source-id <id> ou --all.[/red]"
    raise typer.Exit(1)
# no enforcement that both aren't set simultaneously
proceed with source_id if source_id else None  (None signals "all" to the downstream module)
```

---

### Business Rule: Save-Destination Precedence for `ask` / `article`

**Overview**:
Both retrieval-producing commands (`ask`, `article`) let the user persist the generated answer/article as a vault `.md` note, with three ways to control where/whether that happens, evaluated in a fixed priority order.

**Detailed description**:
The precedence is: an explicit `--save-to <path>` always wins and writes to that exact path via `save_ask_note`/`save_article_note`'s optional path argument; otherwise a bare `--save` flag writes to the module's default vault location without any further interaction; otherwise, unless `--no-save-prompt` was passed, the CLI falls back to an interactive `rich.prompt.Confirm.ask(...)` — notably with **different default answers** for the two commands: `ask` defaults the prompt to `False` ("no, don't save" — reflecting that a Q&A answer is often exploratory/disposable), while `article` defaults to `True` ("yes, save" — reflecting that article generation is typically the deliverable itself). Both wrap the `Confirm.ask` call in a `try/except (EOFError, KeyboardInterrupt): pass`, so that running in a genuinely non-interactive context (e.g. piped stdin, or a signal) degrades to "don't save" rather than crashing — this is a deliberate resilience measure for scripted/CI usage that forgot to pass `--no-save-prompt`.

After saving, both commands attempt to print the path *relative* to `cfg.vault_path` for readability, falling back to the absolute path if the saved file happens to live outside the vault tree (`ValueError` from `Path.relative_to`).

**Rule workflow**:
```
if save_to:            saved_path = save_note(result, vault_path, Path(save_to))
elif save:              saved_path = save_note(result, vault_path)          # default location
elif not no_save_prompt:
    try:
        if Confirm.ask("Salvar...?", default=<False for ask / True for article>):
            saved_path = save_note(result, vault_path)
    except (EOFError, KeyboardInterrupt):
        pass   # treated as "no"
if saved_path:
    print relative-to-vault path, or absolute path on ValueError
```

---

### Business Rule: `new-note` Note-Type Normalization and Source-ID Aliasing

**Overview**:
`new-note` (zettel/cli.py:978-1085) accepts a loosely-typed `note_type` positional argument (`ztl|lit|src|moc` or their long forms `permanent|literature|source`) and must resolve ambiguity between `--citekey` and `--source-id` before delegating to `zettel.new_note.scaffold_manual_note`.

**Detailed description**:
`normalize_note_type` (delegated to `zettel.new_note`, called from the CLI) is the actual normalizer; the CLI's own responsibility is narrower but still a business rule: it decides that `--citekey` should be treated as an alias for `--source-id` **only** when the normalized type is `"permanent"` (a ZTL note that cites a source) or `"source"` (a SRC note whose own identity *is* the citekey) — but explicitly *not* for `"literature"` (LIT notes), where `--citekey` and `--source-id` are semantically different enough that no fallback is applied (a LIT note's own identity is not simply its citekey, since granular LIT notes are `{Citekey}/LIT - ... - pNNN - topic-NNNN.md`). This is a subtle rule: omitting it silently would let `--citekey` do nothing for LIT notes without any error, so its absence must be read as intentional rather than an oversight, given the explicit `if` branches only cover the other two types.

Both `FileExistsError` (when `--force` was not passed and the target path already exists) and `ValueError` (invalid combinations, e.g. missing required identity fields) raised by `scaffold_manual_note` are caught at the CLI boundary and converted into a red console message plus `typer.Exit(1)` — the CLI itself performs no additional validation of the note fields beyond the type normalization and the source-id aliasing described above.

**Rule workflow**:
```
normalized = normalize_note_type(note_type)   # raises ValueError -> Exit(1)
effective_source_id = source_id
if not effective_source_id and citekey and normalized in ("permanent", "source"):
    effective_source_id = citekey
# normalized == "literature": no aliasing, source_id stays as explicitly passed (or None)
scaffold_manual_note(cfg, note_type, title, ..., source_id=effective_source_id, ...)
  -> FileExistsError / ValueError -> print + Exit(1)
```

---

### Business Rule: `run-all` Phase Sequencing, Dry-Run Cutoff, and Non-Interactive Paging

**Overview**:
`run-all` (zettel/cli.py:1252-1337) is the only command that chains four pipeline phases (harvest, extract, review, connect, garden) in a single invocation, and it must reconcile flags that make sense at the whole-pipeline level with flags that individual phase functions expect.

**Detailed description**:
Unlike `harvest` run standalone (which lets `--skip-paging` be explicitly toggled, subject to the `skip_paging or True` quirk described in Technical Debt), `run-all` does not expose `--skip-paging` at all — it derives it unconditionally as `skip_paging=not interactive` when calling `run_harvest`. This reflects a real constraint: HITL paging prompts (asking where book content starts) are incompatible with running four unattended phases back-to-back, so paging is only ever attempted when the whole pipeline run is interactive.

The review phase within `run-all` computes `auto_approve=yes or not interactive` and `interactive=interactive and not yes` — meaning that if `--yes` was passed, review is always auto-approved even if the harvest phase happened to run interactively (which cannot actually happen given the flags, but the expressions are written defensively as independent booleans rather than relying on the earlier derived `interactive` alone). `--dry-run` is honored only *after* Phase 2b (review): the command explicitly stops before Phase 3 (connect) and Phase 4 (garden), on the stated rationale of "parando antes da geracao de notas" (stopping before actual note generation) — i.e., dry-run in this codebase means "harvest, extract, and review still execute for real" (they mutate SQLite/Chroma state) but the two note-writing phases are skipped. This is a narrower interpretation of "dry run" than a literal read of the flag name would suggest, and is worth calling out since a user might reasonably expect `--dry-run` to avoid all state mutation.

**Rule workflow**:
```
interactive, duplicate_action = _resolve_duplicate_flags(yes, skip_duplicates, force)
Phase 1 Harvest:  run_harvest(interactive=interactive, skip_paging=not interactive, duplicate_action, skip_biblio)
Phase 2 Extract:  run_extract(auto_approve=False)                     # always manual-confidence gated
Phase 2b Review:  run_review(auto_approve = yes or not interactive,
                              interactive  = interactive and not yes)
if dry_run: print "stopping" message; db.close(); return             # SQLite/Chroma already mutated by 1/2/2b
Phase 3 Connect:  candidates = _load_approved_candidates(db); run_connect(...)
Phase 4 Garden:   run_garden(cfg, db, idx)                            # note: never --hubs, never --recreate
```

---

## 4. Component Structure

`cli.py` is a single flat module (no sub-package). Internal organization by section (as delimited by the file's own `# ── section ──` comment banners):

```
zettel/cli.py                              # 1934 lines, 22 @app.command() functions, 11 helpers
├── Typer app + Rich console setup          # lines 36-41
├── Dependency-injection helpers            # lines 44-169
│   ├── _load_deps                          # AppConfig + logging bootstrap
│   ├── _get_db                             # StateDB factory
│   ├── _load_approved_candidates           # SQLite -> PermanentNoteCandidate hydration (connect/run-all)
│   ├── _fmt_embedding_id                   # "{provider}/{model}@{dim}d" formatter
│   ├── _idx_kwargs                         # VectorIndex constructor kwargs (mirrored in web_app.py)
│   ├── _warn_embedding_mismatch            # drift warning Panel
│   ├── _confirm_embedding_reprocess        # yes-flag-or-prompt gate
│   └── _get_idx                            # VectorIndex factory + drift auto-repair
├── init                                    # lines 174-246   vault/DB/index bootstrap (+ --reset)
├── harvest                                 # lines 251-412   Phase 1 command + 3 flag-resolution helpers
│   ├── _resolve_duplicate_flags
│   ├── _resolve_chunk_dump_dir
│   └── _resolve_extraction_dump_dir
├── extract                                 # lines 418-444   Phase 2 command
├── review                                  # lines 447-483   Phase 2b command
├── purge-rejected (purge_rejected_cmd)     # lines 486-544   destructive maintenance
├── delete-source (delete_source_cmd)       # lines 547-642   destructive maintenance
├── connect                                 # lines 648-685   Phase 3 command
├── garden                                  # lines 691-745   Phase 4 command (+ --hubs, --recreate)
├── retry-failed (retry_failed)             # lines 750-787   chunk/asset status reset
├── rechunk                                 # lines 793-838   re-chunk from persisted extraction
├── dump-chunks (dump_chunks_cmd)           # lines 844-877   chunk export for inspection
├── dump-extraction (dump_extraction_cmd)   # lines 883-920   extraction export for inspection
├── set-paging (set_paging_cmd)             # lines 926-972   paging repair without LLM re-call
├── new-note                                # lines 978-1085  manual note scaffolding
├── sync-manual                             # lines 1090-1127 vault -> index sync (+ --rebuild-graph)
├── reindex                                 # lines 1133-1195 ChromaDB rebuild from SQLite
├── rebuild                                 # lines 1201-1246 vault and/or ChromaDB rebuild
├── run-all                                 # lines 1252-1337 full pipeline orchestration
├── ask                                     # lines 1343-1461 hybrid retrieval Q&A
├── article                                 # lines 1466-1652 LangGraph long-form article (+ HITL closures)
├── status                                  # lines 1657-1736 pipeline statistics dashboard
├── doctor                                  # lines 1741-1919 config/dependency/integrity checks
└── Entry point (main / __main__ guard)     # lines 1922-1934
```

---

## 5. Dependency Analysis

**Internal Dependencies** (all lazily imported inside function bodies — see Design Patterns section — 21 distinct `zettel.*` modules):

```
cli.<every command> → config.{AppConfig, load_config, setup_logging, detect_device, get_gpu_info}
cli.<every command touching Chroma> → index.{VectorIndex, EmbeddingSpaceMismatch, peek_stored_embedding_identity}
cli.<every command> → state.StateDB
cli.connect/run_all → schemas.PermanentNoteCandidate
cli.init → vault.init_vault
cli.harvest/rechunk/run_all/status/doctor → harvester.{run_harvest, run_rechunk, run_set_paging, list_incomplete_sources}
cli.harvest/rechunk/dump_chunks_cmd → chunk_dump.{default_dump_dir, run_dump_chunks}
cli.harvest/dump_extraction_cmd → extraction_dump.{default_dump_dir, run_dump_extraction}
cli.extract/run_all → extractor.run_extract
cli.review/purge_rejected_cmd/run_all → review.{run_review, purge_rejected}
cli.delete_source_cmd → purge_source.{normalize_source_id, purge_source}
cli.connect/run_all → connector.run_connect
cli.garden → gardener.run_garden | gardener_hub.run_garden_hubs
cli.reindex/rebuild/_get_idx → rebuild.{run_reindex, run_rebuild_vault}
cli.new_note → new_note.{normalize_note_type, scaffold_manual_note}
cli.sync_manual → sync.{run_sync_manual, rebuild_manual_edges}
cli.ask → ask.{run_ask, save_ask_note}
cli.article → article.{parse_extra_queries, save_article_note} + article_graph.run_article_graph
cli.doctor → taxonomy.{allowed_topic_names, load_moc_taxonomy}
```

**External Dependencies**:

```
- typer            — command/argument/option declaration, confirm() prompts, Exit()
- rich (console, panel, table, prompt) — all terminal output/formatting/interactive prompts
- Python stdlib: logging, sys, time, pathlib.Path, typing.Optional, shutil (lazy, in `init --reset`)
```

No direct database, HTTP, or filesystem-format libraries are imported in `cli.py` itself — those are encapsulated behind the `zettel.*` modules it calls (StateDB wraps SQLite, VectorIndex wraps ChromaDB, vault.py wraps Obsidian markdown I/O).

---

## 6. Afferent and Efferent Coupling

Because `cli.py` is a procedural module (Typer commands + helper functions, no classes), "components" here are the module-level functions. Afferent coupling (Ca) = number of distinct call sites within the file calling into that function; Efferent coupling (Ce) = number of distinct internal/external dependencies (helper functions + `zettel.*` module symbols + third-party APIs) that function calls into.

| Component (function) | Afferent Coupling | Efferent Coupling | Critical |
|---|---|---|---|
| `_load_deps` | 22 (every command) | 3 (config.AppConfig/load_config/setup_logging) | High |
| `_get_db` | ~20 (all but `new_note`, `article`'s prompt closures) | 1 (state.StateDB) | High |
| `_get_idx` | 15 (init, harvest, extract, review, purge-rejected, delete-source, connect, garden, rechunk, set-paging, sync-manual, rebuild, run-all, ask, article) | 6 (`_idx_kwargs`, `_warn_embedding_mismatch`, `_confirm_embedding_reprocess`, `_get_db`, index.VectorIndex, rebuild.run_reindex) | High |
| `_idx_kwargs` | 4 call sites / 2 callers (`_get_idx`, `reindex`) | 1 (cfg field reads only) | Medium |
| `_warn_embedding_mismatch` | 2 (`_get_idx`, `reindex`) | 2 (`_fmt_embedding_id`, rich.Panel) | Medium |
| `_confirm_embedding_reprocess` | 2 (`_get_idx`, `reindex`) | 1 (typer.confirm) | Medium |
| `_fmt_embedding_id` | 4 call sites / 2 callers (`_warn_embedding_mismatch`, `doctor`) | 0 (pure formatting) | Low |
| `_load_approved_candidates` | 2 (`connect`, `run-all`) | 2 (schemas.PermanentNoteCandidate, db.get_concepts_by_status) | Medium |
| `_resolve_duplicate_flags` | 2 (`harvest`, `run-all`) | 0 (pure logic) | Medium |
| `_resolve_chunk_dump_dir` | 2 (`harvest`, `rechunk`) | 1 (chunk_dump.default_dump_dir) | Low |
| `_resolve_extraction_dump_dir` | 1 (`harvest`) | 1 (extraction_dump.default_dump_dir) | Low |
| `run_all` (command) | 0 (Typer entry point) | ~10 (harvester, extractor, review, connector, gardener, `_load_approved_candidates`, `_resolve_duplicate_flags`, `_get_db`, `_get_idx`, `_load_deps`) | High |
| `harvest` (command) | 0 | ~9 (harvester, chunk_dump, extraction_dump, 3 local resolvers, `_get_db`, `_get_idx`, `_load_deps`) | High |
| `doctor` (command) | 0 | ~8 (config, index, taxonomy, harvester, `_get_db`, `_fmt_embedding_id`, `_load_deps`, importlib checks) | Medium |
| `ask` / `article` (commands) | 0 each | 5-7 each (ask/article/article_graph modules, `_get_db`, `_get_idx`, `_load_deps`, rich.prompt) | Medium |

`_load_deps`, `_get_db`, and `_get_idx` are the structural backbone of the file: nearly maximal afferent coupling (called by almost every command) combined with low-to-moderate efferent coupling each — the textbook shape of a well-factored composition root. The 22 command functions themselves are, by construction, never called from within the file (Ca = 0 for all of them — Typer invokes them from the process entry point), so all of their coupling is efferent; `run_all` and `harvest` are the highest-efferent commands because they are themselves mini-orchestrators over multiple phase modules.

---

## 7. Endpoints (CLI Command Interface)

Not a network-facing component — no REST/GraphQL/gRPC endpoints. The equivalent "interface surface" is the set of Typer subcommands under `python -m zettel <command>`:

| Command | Destructive? | Confirmation Gate | Key Flags | Description |
|---|---|---|---|---|
| `init` | Yes (`--reset`) | Always on `--reset` (no bypass) | `--config`, `--vault`, `--inbox`, `--reset` | Bootstrap vault/State DB/ChromaDB |
| `harvest` | No | Only via embedding-drift gate | `--yes`, `--skip-duplicates`, `--force`, `--skip-biblio`, `--content-start-file/-book`, `--skip-paging`, `--dump-chunks`, `--dump-extraction` | Phase 1 ingestion |
| `dump-chunks` | No | None | `--source-id`\|`--all`, `--dump-dir` | Export persisted chunks to markdown |
| `dump-extraction` | No | None | `--source-id`\|`--all`, `--dump-dir` | Export extracted Markdown |
| `extract` | No | Embedding-drift only | `--yes`, `--auto-approve` | Phase 2 LLM extraction |
| `review` | Rejects delete drafts | Embedding-drift only | `--source-id`, `--yes`, `--auto-approve`, `--low-confidence-only` | Phase 2b approval HITL |
| `purge-rejected` | Yes | `--yes` bypass | `--source-id`, `--yes`, `--no-compact` | Hard-delete rejected chunks |
| `delete-source` | Yes | `--yes` bypass | `source_id` (arg), `--yes`, `--delete-permanent`, `--no-compact` | Full source removal |
| `connect` | No | Embedding-drift only | `--topk`, `--dedupe-threshold`, `--yes` | Phase 3 permanent-note generation |
| `garden` | Yes (`--recreate`) | `--yes` bypass on `--recreate` | `--min-cluster-size`, `--hubs`, `--recreate`, `--yes` | Phase 4 MOC clustering |
| `retry-failed` | No | None | `--source-id`, `--assets` | Reset failed chunks/assets to pending |
| `rechunk` | No | Embedding-drift only | `--source-id`\|`--all`, `--yes`, `--dump-chunks`, `--dump-dir` | Re-chunk from persisted extraction |
| `set-paging` | No (mutates chunks) | Embedding-drift only | `--source-id`, `--content-start-file/-book`, `--drop-before-start`, `--yes` | Repair paging without LLM |
| `new-note` | Overwrite guarded by `--force` | `FileExistsError` unless `--force` | `note_type`, `title` (args), many metadata flags | Scaffold manual vault note |
| `sync-manual` | No | Embedding-drift only | `--rebuild-graph`, `--yes` | Sync manual notes to index |
| `reindex` | Resets a collection with `--force` | Auto-detected drift, or `--yes` | `--collection`, `--force`, `--yes` | Rebuild ChromaDB from SQLite |
| `rebuild` | Overwrite guarded by `--force` | Embedding-drift only (chroma path) | `--what`, `--force`, `--dry-run`, `--yes` | Rebuild vault and/or ChromaDB |
| `run-all` | No (dry-run truncates) | Inherits `harvest`'s duplicate gate | `--dry-run`, `--yes`, `--skip-duplicates`, `--force`, `--skip-biblio` | Full pipeline orchestration |
| `ask` | No | Embedding-drift only | `question` (arg), `--topk`, `--no-graph`, `--mode`, `--show-context`, `--save`, `--save-to`, `--no-save-prompt`, `--yes` | Hybrid-retrieval Q&A |
| `article` | No | Embedding-drift only | `topic` (arg), `--style`, `--personality`, `--style-notes`, `--outline-only`, `--skip-context-review`, `--skip-judge`, `--max-judge-iterations`, `--save`, `--save-to`, `--yes` | LangGraph long-form article |
| `status` | No | None | `--config` | Pipeline statistics dashboard |
| `doctor` | No | None | `--config` | Config/dependency/integrity checks |

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|---|---|---|---|---|---|
| `zettel.config` | Internal module | Load/validate `AppConfig` from `config/config.yaml`, logging setup, GPU/device detection | In-process function call | Pydantic model / YAML | Pydantic validation errors propagate uncaught (Typer prints traceback) |
| `zettel.state.StateDB` | Internal module (SQLite wrapper) | All persistent pipeline state (files, sources, chunks, concepts, notes, mocs, runs) | In-process (SQLite via sqlite3) | Rows / dicts | No CLI-level try/except around `StateDB()` construction; failures surface as raw exceptions |
| `zettel.index.VectorIndex` | Internal module (ChromaDB wrapper) | 5-collection vector store (sources, chunks, permanent_notes, mocs, literature_notes) | In-process (chromadb client) | Embeddings + metadata | `EmbeddingSpaceMismatch` explicitly caught and turned into a guided recovery flow (`_get_idx`); other exceptions propagate |
| `zettel.harvester` / `zettel.extractor` / `zettel.review` / `zettel.connector` / `zettel.gardener(_hub)` | Internal modules | The five pipeline phases | In-process function call, called with `(cfg, db, idx, ...)` | Domain objects / dicts | `ValueError` caught explicitly only in `set-paging`, `dump-chunks`, `dump-extraction`; other phase commands let exceptions propagate to Typer's default handler |
| `zettel.purge_source` / `zettel.review.purge_rejected` | Internal modules | Irreversible cascade deletes across vault/SQLite/Chroma | In-process function call | dict result payloads (counts, before/after MB) | No additional error handling at CLI level beyond the pre-confirmation gate |
| `zettel.ask` / `zettel.article` + `zettel.article_graph` | Internal modules | Retrieval-augmented Q&A and long-form generation (LLM calls) | In-process; `article_graph` uses LangGraph `interrupt()` for HITL | `AskResult` / `ArticleResult` dataclasses | `article`'s HITL closure (`_hitl`) is passed as a callback into the LangGraph run; no explicit LLM-error handling visible in `cli.py` (delegated to the called modules) |
| Rich `Console`/`Panel`/`Table`/`Prompt`/`Confirm` | External library | All terminal rendering and interactive prompts | In-process (stdout/stdin) | N/A | `EOFError`/`KeyboardInterrupt` explicitly caught around `Confirm.ask` in `ask`/`article` save flows only |
| Typer (`typer.Typer`, `typer.Option`, `typer.Argument`, `typer.confirm`, `typer.Exit`) | External library | CLI framework: parsing, help text, process exit codes | In-process | N/A | `typer.Exit(n)` is the CLI's uniform "abort with exit code" mechanism throughout |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---|---|---|---|
| Composition Root | `_load_deps` / `_get_db` / `_get_idx` | zettel/cli.py:44-169 | Single place constructing `(AppConfig, StateDB, VectorIndex)` for every command, keeping command bodies free of construction logic |
| Lazy / Deferred Import | Every `zettel.*` import happens inside function bodies, not at module top | throughout | Keeps `import zettel.cli` (and thus `--help`) fast by not eagerly importing heavy dependencies (ChromaDB, Docling, LangGraph, sklearn/umap/hdbscan) unless the specific command needs them |
| Guard Clause / Fail-Fast Validation | Mutual-exclusivity and required-flag checks return/`Exit(1)` before any I/O | e.g. zettel/cli.py:379-381, 812-814 | Avoids partially-opened DB/index handles for invalid invocations (mostly — see Technical Debt for the one exception in `harvest`) |
| Strategy-by-Flag (precedence chains) | `_resolve_duplicate_flags`; save-destination precedence in `ask`/`article` | zettel/cli.py:375-388, 1441-1451 | Resolves multiple overlapping CLI flags into one canonical downstream decision |
| Template Method (implicit) | Every command repeats `cfg → db → idx → delegate → render → close` | throughout | Not formally abstracted into a decorator/base, but consistently followed by convention (candidate for future refactor — noted, not recommended, per this report's scope) |
| Exception-to-Exit-Code Translation | `try/except ValueError/FileExistsError: console.print(...); raise typer.Exit(1)` | e.g. zettel/cli.py:1073-1078, 869-872 | Converts domain exceptions raised by delegated modules into user-facing error messages and process exit codes |
| Two-Phase Confirmation (scope disclosure + explicit confirm) | `delete-source`, `purge-rejected` | zettel/cli.py:547-642, 486-544 | Reduces risk of destructive-action mistakes by showing computed blast-radius before asking to proceed |
| Facade | `cli.py` as a whole, over the entire pipeline module graph | whole file | Presents 22 uniform commands over ~20 heterogeneous internal modules, hiding their individual construction/wiring requirements from the end user |

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|---|---|---|---|
| High | `cli.py` (entire file) | Zero automated test coverage: no file in `tests/` imports `zettel.cli`, uses `typer.testing.CliRunner`, or exercises the module via subprocess | Flag-resolution logic (`_resolve_duplicate_flags`, save precedence, mutual-exclusivity guards) and the embedding-drift recovery flow can regress silently; the file is also the *only* interface to CLI-only operations (`new-note`, `delete-source`, `purge-rejected`, `rechunk`, `set-paging`, dumps, `doctor`, `run-all`, interactive duplicate resolution) that have no web-UI fallback to catch regressions either |
| Medium | `harvest` command | `skip_paging=skip_paging or True` (zettel/cli.py:336) is a tautology — the boolean `or True` makes the left operand irrelevant, so the actual value of the `--skip-paging` flag has no effect in non-interactive `harvest` (paging is always skipped there regardless of what the user passed) | Confusing/misleading: a user who explicitly passes `--skip-paging=false`-equivalent (there is no such negated flag, but the expression reads as if the flag mattered) gets no behavioral change; also makes the flag's non-interactive semantics look accidental rather than intentional to a future reader |
| Medium | `reindex` vs. `_get_idx` | Two independent implementations of the embedding-drift warn/confirm/force sequence exist (`reindex`'s inline block at zettel/cli.py:1156-1184 vs. `_get_idx` at zettel/cli.py:126-169); `reindex` additionally treats `--force` as an implicit bypass of the confirmation prompt (`if not force and not _confirm_embedding_reprocess(yes)`), a semantic `--force` does not have anywhere else in the file | Divergent maintenance risk — a future fix to the drift-warning UX in one location is easy to forget in the other; the `--force`-bypasses-confirmation behavior is inconsistent with the rest of the CLI's confirmation model (elsewhere only `--yes` bypasses confirmations) |
| Medium | `rechunk` / `dump-chunks` / `dump-extraction` | The "`--source-id` or `--all` required" guard is copy-pasted three times (zettel/cli.py:812-814, 855-857, 894-896) with no shared helper (unlike the analogous `_resolve_chunk_dump_dir`), and none of the three actually forbid passing *both* flags simultaneously — the resulting precedence when both are set is not visible from `cli.py` | Copy-paste drift risk if the validation message or rule changes; ambiguous behavior when both flags are passed together is undocumented at the CLI layer |
| Low | `_get_idx` dead branch | `own_db = db is None` / the "caller passed no `db`" branch is only reachable if some call site invokes `_get_idx(cfg)` without `db=`; a scan of all 15 call sites shows every one passes an already-open `db` handle, so this branch, while not unreachable in principle (it is exercised by any future caller that omits `db`), is currently unexercised by any command in this file | Low risk, but the fallback path (opening a second `StateDB` connection) is untested code that would only run in a scenario the current codebase never creates |
| Low | `garden --recreate` confirmation | Unlike `delete-source`'s precise chunk/note counts, the `--recreate` confirmation text only names the pipeline (`"taxonomia"`/`"hub"`) with no count of MOCs about to be deleted | Less-informed confirmation for a destructive action, inconsistent with the more disclosure-rich pattern used elsewhere in the same file |
| Low | `init --reset` | The only destructive command in the file with no `--yes`/non-interactive bypass at all | Cannot be scripted/automated (e.g. in CI or a reset script) without piping a confirmation answer through stdin, unlike every other destructive command which supports `--yes` |
| Informational | Whole-file structure | 1934 lines, 22 commands, and 11 helpers in a single flat module with no sub-package boundaries | Not inherently a defect (Typer CLIs of this size are common), but the file's high efferent coupling (21 internal modules) means any signature change in a called module (e.g. `run_harvest`, `run_review`) requires searching this one large file for all call sites rather than a smaller, phase-scoped one |

---

## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|---|---|---|---|---|
| `zettel/cli.py` (all 22 commands + 11 helpers) | 0 | 0 | 0% (no test file references `zettel.cli`, `typer.testing.CliRunner`, or invokes the module via subprocess) | N/A — no tests exist to assess |

Search performed: `Grep` across `D:\projetos\zettel_app\tests` and the whole repository for `zettel.cli`, `from zettel import cli`, `CliRunner`, `subprocess`, and `python -m zettel` returned no matches outside `zettel/__main__.py` itself (which is the production entry point, not a test).

This is a real gap, not merely an artifact of test file naming: every other pipeline module referenced by `cli.py` — `harvester` (`tests/test_harvester_dedup.py`, `tests/test_harvester_sections.py`), `extractor` (`tests/test_extractor.py`), `review` (`tests/test_review.py`), `connector` (`tests/test_connector.py`), `gardener`/`gardener_hub` (`tests/test_gardener.py`, `tests/test_gardener_assign.py`, `tests/test_gardener_hub.py`), `sync` (`tests/test_sync.py`), `rebuild` (`tests/test_rebuild.py`), `purge_source` (`tests/test_purge_source.py`), `new_note` (`tests/test_new_note.py`), `ask` (`tests/test_ask.py`), `article`/`article_graph` (`tests/test_article.py`, `tests/test_article_graph.py`), `chunk_dump` (`tests/test_chunk_dump.py`), `extraction_dump` (`tests/test_extraction_dump.py`), `set_paging` (`tests/test_set_paging.py`, `tests/test_set_paging_filter.py`), `graph` (`tests/test_graph.py`), `retrieval` (`tests/test_retrieval.py`), `state`/`config`/`index`/`vault`/`hashing`/`pricing`/`usage` all have dedicated test modules — has coverage at the *module* level. The CLI layer that wires them together, resolves their flags, and gates destructive operations is the one piece of the pipeline with no dedicated test surface at all. The web UI has its own test suite (`tests/test_web.py`, `tests/test_web_state.py`) covering `web.py`/`web_app.py`, but those exercise the web routes and `WebApplication` job dispatch directly — not `cli.py` — so they provide no incidental coverage of this component's flag-resolution or confirmation logic.
