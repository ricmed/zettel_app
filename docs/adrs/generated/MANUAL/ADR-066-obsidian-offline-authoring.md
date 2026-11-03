# ADR-066: Obsidian as an Offline Authoring Surface

**Status:** Accepted
**Date:** 2026-11-03
**Issue:** #340 (sub-issues #341–#349)
**Depends on:** [ADR-030: Manual Notes Are Adopted at Sync Time and Bypass the Review Gate](./ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)
**Related to:** [ADR-059: Fail Fast on LLM Unavailability](../LLM/ADR-059-fail-fast-on-llm-unavailability.md), [ADR-064: Managed Blocks Refuse Malformed Markers](../INFRA/ADR-064-managed-blocks-refuse-malformed-markers.md), [ADR-009: Graph-Based Note Discovery](../RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md), [ADR-057: Precedence and Provenance of Bibliographic Fields](../HARVEST/ADR-057-bibliographic-field-provenance.md)

## Context and Problem Statement

The user owns the notes even without internet or an LLM. ADR-030 made `sync-manual` the single adoption point for hand-written notes, and `zettel new-note` and `/notes/new` produce the structure `sync-manual` expects. Writing a note **in Obsidian itself** was the missing piece. A review of the code in November 2026 (epic #340) found that this path did not hold up:

- **The vault was erased.** `zettel init` deleted the whole vault (`shutil.rmtree`) on every run, which also removed the manual notes and `.obsidian/`.
- **One bad note broke the run.** A single malformed note aborted the whole sync. An unquoted date (`created: 2026-11-03`), which Obsidian's Date property writes, made `json.dumps` fail. Invalid YAML was read as `{}`, and the sync then rewrote the frontmatter on top of it.
- **The sync was online-only.** It refused to run without an embedding credential. When an embed failed after the SQLite write, the ZTL never got a vector at all: on the next run its checksum matched and it was skipped.
- **Edits did not reach the database.** An edited SRC was never read again. The order in which files were scanned dropped links between notes. A removed link kept its edge, and a deleted or renamed note stayed in SQLite with its old row.
- **There was nothing to start from.** The project had no Obsidian templates and no way to generate a ULID or a `chunk_id` inside Obsidian.

## Decision Drivers

* A note written in Obsidian must be the note `new-note` writes: the same frontmatter, sections and managed blocks, adopted the same way.
* Everything except vectors and suggestions must work offline: SQLite and FTS are the durable store.
* The vault is the user's. No command may delete or rewrite what the user wrote.
* The structure has one definition, so the templates cannot drift from the builders.

## Decision Outcome

1. **`init` never deletes the vault (#341).** It creates the folders that are missing. `--reset` removes only the notes whose frontmatter `origin` is `pipeline` or `hub_pipeline`. It keeps manual notes, notes with unreadable frontmatter, `.obsidian/`, `99_Templates/` and `90_Assets/`.
2. **Each note is synced on its own (#342).** A note that cannot be adopted, or any unexpected error, is reported with its path and reason, and the file is left untouched. Only a database error stops the run. Unreadable frontmatter is refused (`parse_frontmatter_strict`), never rewritten. YAML dates are serialized as ISO text. The placeholders the scaffolds write stay out of the embedding.
3. **Sync fails open on embedding (#343).** This is the exception to ADR-059, which every other phase keeps. The index is opened lazily, never without a credential. An outage stops further embeds in that run, not the run itself. A note without a vector counts as pending, and the next sync that reaches the index writes it without the note being edited. The web Sync button needs no credential.
4. **Templates are generated from the builders (#344).** `zettel obsidian-templates` runs the scaffolds `new-note` uses (`source_scaffold`, `literature_scaffold`, `permanent_scaffold`, `moc_scaffold`) with sentinel values and turns every sentinel into a JavaScript expression. A leftover sentinel is an error. The Templater user script prompts, generates the ULID or the next `chunk_index` from the vault, and names the file `PREFIX - ID - slug`, so links are born in the form the sync reads. A test runs the generated script under Node and compares each flow with the Python scaffold.
5. **The vault is prepared for Obsidian by `init` (#345).** The same `setup_obsidian` also runs from `zettel obsidian-setup`. It enables Templater in `.obsidian/community-plugins.json` and merges its `data.json`, keeping every other setting. It installs the plugin from a pinned release: version and sha256 come from `config.yaml`, the files come from the cache or `--templater-from`, and the GitHub download happens once. Without network or cache the plugin is only reported. The trigger on new file creation is a per-device switch that Templater 2.x deliberately keeps out of `data.json`. With it on, Templater executes `<% %>` in any new file, including source text the pipeline writes. So the vault sets the folder-template mode and the user turns the trigger on.
6. **An edited SRC reaches SQLite (#346).** Only the bibliographic columns change. Edited keys are recorded as `user` in `bibliography_json.provenance` (ADR-057). A change of authors or year recomputes the citation of the source's notes with the same `resolve_citation` that `connect` uses.
7. **The manual graph follows the bodies (#347).** Edges are derived in a second pass, once every note is in SQLite. A removed link deletes its `manual` edge; `llm` and `derived` edges are never touched. One link pattern (`vault.ZTL_LINK_RE`) serves edges and MOC backrefs.
8. **Moved and deleted notes are tracked (#348).** A row follows its file by id. A deleted note is reported by the sync, the web job and `doctor`. `sync-manual --prune`, CLI only, removes the manual ones. A deleted pipeline note is only reported, because `rebuild` restores it.

### Consequences

* The offline loop is complete:
  1. write in Obsidian with the templates;
  2. run `sync-manual` without a credential: notes, FTS, graph and backrefs are in SQLite, and vectors are pending;
  3. run `sync-manual` again later, online, to write the vectors and suggestions.
* Templater becomes an optional dependency of the vault, pinned with its hashes. Updating it means changing `obsidian.templater_version` and the hashes together.
* The templates are generated output. `obsidian-templates --force` rewrites them after an update and discards local edits.
* Opening the vault still asks the user to trust it, and the folder-template trigger stays off until the user enables it on each device. Neither can be automated, by design.
* `.obsidian/` settings that cannot be read are reported and never overwritten.

## Alternatives

* **Obsidian's core Templates plugin.** Rejected. It only fills `{{title}}`, `{{date}}` and `{{time}}`: no ULID, no rename, no source picker.
* **Obsidian CLI (1.12+) as the creation path.** Kept only as a shortcut. It needs the app running and still uses core templates.
* **Free file names, resolved by the sync.** Rejected in favour of creating `ZTL - ULID - slug` at once. This leaves nothing for the sync to guess and nothing to rename under an open editor.
* **Vendoring Templater into the repository.** Rejected. The license was not verified, and a download pinned by hash gives the same reproducibility.
* **Turning the trigger on from the vault.** Not possible in Templater 2.x, and not wanted: it would let any file the pipeline writes run code.

## References

* `zettel/vault.py` — `init_vault`, `partition_vault_notes`, `remove_pipeline_notes`, `parse_frontmatter_strict`, `frontmatter_json`, `ZTL_LINK_RE`
* `zettel/sync.py` — `_Vectors`, `_Run`, `_resync_source`, `_link_notes`, `_extract_body_edges`, `missing_note_files`
* `zettel/new_note.py` — `source_scaffold`, `literature_scaffold`, `permanent_scaffold`, `moc_scaffold`, `next_manual_chunk_index`
* `zettel/obsidian.py` — `build_script`, `write_templates`, `setup_obsidian`, `fetch_templater`, `obsidian_health`
* `zettel/obsidian_runtime.js` — the Templater user script runtime
* `zettel/citation.py` — `refresh_source_citations`
* `zettel/moc_backrefs.py` — backrefs matched by id or file stem
* `zettel/cli/obsidian.py`, `zettel/cli/manual.py`, `zettel/cli/maintenance.py`
* `tests/test_obsidian_templates.py`, `tests/test_sync.py`, `tests/test_vault.py`, `tests/test_cli_commands.py`
