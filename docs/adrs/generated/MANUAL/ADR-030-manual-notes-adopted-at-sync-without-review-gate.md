# ADR-XXX: Manual Notes Are Adopted at Sync Time and Bypass the Review Gate

**Status:** Accepted
**Date:** 2026-09-02
**Depends on:** [ADR-XXX: Granular Per-Chunk Literature Notes with Readable Filenames](../EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md)
**Used by:**
- [ADR-XXX: Vault-First Image Adoption for Manual Notes](../ASSETS/ADR-031-vault-first-image-adoption.md)

**Related to:**
- [ADR-XXX: Confidence-Band Human-in-the-Loop Approval Gate](../REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)
- [ADR-XXX: Post-Approval Concept Deduplication Timing](../REVIEW/ADR-016-post-approval-concept-deduplication-timing.md)
- [ADR-XXX: Dual-Store Persistence Without Cross-Store Transactions](../INFRA/ADR-005-dual-store-persistence.md)

## Context and Problem Statement

The pipeline (`harvest → extract → review → connect → garden`) assumes every note descends from a harvested file: a source produces chapters, chapters produce chunks, chunks produce literature notes, and literature notes produce concepts that become permanent notes. Notes the user writes by hand in Obsidian have no such lineage. `zettel new-note` scaffolds a file and `zettel sync-manual` adopts it afterwards — a deliberate vault-first split, since the user's editor is Obsidian and the vault is the source of truth.

That split worked for permanent notes and MOCs, whose adoption needs only a `notes`/`mocs` row plus an embedding. It did not work for literature notes. A granular LIT written by hand carries a synthetic `chunk_id` in its frontmatter but no `chunks` row, and `sync._sync_literature` only ever *updated* an existing row. The consequence was a silent dead end: the file existed in `20_Literature/{Citekey}/`, looked identical to an approved pipeline note, and was invisible to SQLite, to the `literature_notes` collection, to the source's literature index, and therefore to `ask`, `article` and `connect`. There was likewise no way to turn a literature note into a permanent note, because `concepts.chunk_id` is NOT NULL with a foreign key to `chunks`, so no concept could exist without that missing row.

A second question sits underneath: does hand-written content belong in the confidence-band approval gate ([ADR-017](../REVIEW/ADR-017-confidence-band-hitl-approval-gate.md))? That gate exists to keep un-inspected LLM output out of the graph. Its input, `review_confidence`, is produced by the extractor and has no meaning for text a human typed.

## Decision Drivers

* The vault must stay the source of truth: adoption is something `sync-manual` does to what it finds, not something a command must be run in the right order to enable.
* A hand-written literature note should be indistinguishable downstream from an approved pipeline one — same collection, same index entry, same `literature_ref` shape — or half the system silently ignores it.
* The approval gate protects against un-inspected LLM output. Asking a user to approve text they just wrote is ceremony, not oversight.
* Reusing the connector's Prompt 2 for the LLM-assisted permanent note avoids a second prompt, a second RAG path and a second relation-typing implementation drifting away from the pipeline's.
* `PRAGMA foreign_keys=ON` makes the `chunks` row a hard prerequisite for any `concepts` row, so the LIT-to-ZTL path cannot sidestep it.
* Adoption runs on every sync over the whole vault, so it must be idempotent and cheap for unchanged notes.

## Considered Options

* Synthesize the missing `chunks` row (plus a per-source synthetic chapter) during `sync-manual`, then reuse the existing post-approval steps verbatim.
* Introduce a parallel "manual literature note" concept with its own table, its own collection entry and its own index rendering, bypassing `chunks` entirely.
* Require the user to route manual notes through `extract` + `review` so the pipeline's own machinery produces the rows.

## Decision Outcome

Chosen option: "Synthesize the missing `chunks` row during `sync-manual`," because the chunk row is the only thing the downstream machinery was actually missing. Once it exists, embedding into `literature_notes`, the `auto-lit-index` refresh, `connector._literature_ref_for_chunk`, `delete-source` cascade and FTS all work with no code of their own. The alternative — a parallel manual concept — would have duplicated each of those five behaviours and guaranteed drift.

Concretely, `zettel/manual_lit.py` owns adoption. `ensure_manual_chapter` creates one `{source_id}::ch000` chapter titled `Manual` per source, satisfying the NOT NULL foreign key. `adopt_manual_literature` reads the excerpt out of the `auto-source-excerpt` managed block as `chunks.text`, rebuilds `summary_json` by parsing the note's own `## Resumo` / `## Conceitos-chave` / `## Candidatos a Nota Permanente` sections, writes the chunk as `status='persisted'`, embeds through the same `review._literature_embed_text` the approval path uses, and calls `review._refresh_literature_index`. Idempotency comes from a checksum over excerpt plus body, mirroring how `_sync_permanent` compares `note_semantic_checksum`.

Manual notes are adopted directly as `persisted`: they never enter `awaiting_review`, never appear in the review queue, and are never scored. [ADR-017](../REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)'s gate is hereby scoped to LLM-generated content only.

For permanent notes, `create_permanent_from_literature` offers two paths from a literature note. Without `--llm` it writes a pre-filled scaffold (thesis, definition, `literature_ref`, `source_ref`, `source_locator`, supporting excerpt) and does **not** insert a new `concepts` row. If extract/review already left `approved` concepts with `note_id` NULL for that idea, the scaffold **consumes** them (`status=noted`, `note_id` of the manual ZTL) so a later `zettel connect` cannot mint a duplicate. `sync-manual` does the same when adopting a hand-written ZTL. With `--llm` it derives a `PermanentNoteCandidate` from the note's own sections, persists it as an `approved` concept, and calls `connector.run_connect(..., origin="manual")` — inheriting hybrid RAG, Prompt 2, ULID minting, typed `note_connections`, `auto-backlinks` maintenance, the deterministic LLM cache and cost accounting. The only change required in the connector was threading an `origin` parameter so manual output stays distinguishable from pipeline output. When that call yields no note, the concept row survives as `approved`, so `zettel connect` retries it later. Connect itself also refuses to overwrite an `origin: manual` note: if a covering ZTL already exists, it marks the concept `noted` and skips the LLM.

The Chroma `chunks` collection is deliberately **not** written to. Its thresholds (`harvest.duplicate_chunk_threshold`) are calibrated on raw L2 distance for harvest-time duplicate detection, and a hand-typed excerpt — often a placeholder — is not a harvested chunk. SQLite FTS5 is still populated, for free, by `upsert_chunk`.

### Positive Consequences

* A hand-written literature note is now retrievable by `ask` and `article` and citable by `connect`, which was previously impossible.
* The LIT-to-ZTL path exercises the same code as the pipeline, so improvements to Prompt 2 or to relation typing benefit both automatically.
* `origin: manual` on both notes and sources keeps provenance auditable and survives `garden --recreate`, which only purges pipeline MOCs.

### Negative Consequences

* `chunks` now holds rows that never came from a harvested file. Any code that assumes `chunks.text` is verbatim extracted source text must tolerate a user-typed excerpt, or an empty one.
* Adoption parses the note body with regexes over `##` headings. A user who restructures those headings loses `summary_json` fidelity (the note itself and its embedding are unaffected).

## Pros and Cons of the Options

### Synthesize the chunks row at sync time (chosen)

* Good, because embedding, index refresh, `literature_ref` resolution, purge cascade and FTS all work with no new code.
* Good, because it unblocks `concepts` and therefore lets the LIT-to-ZTL path reuse the connector wholesale.
* Good, because adoption stays a property of `sync-manual`, so the user never has to run commands in a particular order.
* Bad, because it widens the meaning of a `chunks` row beyond "a slice of extracted text".

### Parallel manual-literature concept

* Good, because pipeline tables keep a single, narrow meaning.
* Bad, because `literature_notes` embedding, index rendering, `literature_ref`, purge and FTS would each need a second implementation.
* Bad, because two implementations of the same five behaviours will drift.

### Route manual notes through extract + review

* Good, because it reuses the pipeline with no new module at all.
* Bad, because it would run an LLM over text the user already wrote, and charge for it.
* Bad, because it would ask the user to approve their own writing, which the approval gate was never meant to cover.

## Consequences

`sync-manual` is now the single adoption point for every note type, and its literature branch dispatches on `origin`: `manual` goes through `adopt_manual_literature`, `pipeline` keeps the lightweight `update_chunk_review` path so a harvested chunk's real text and checksum are never overwritten by whatever is in the note file.

Because manual notes reach `permanent_notes` through `sync._sync_permanent` and the gardener clusters over `idx.get_all_permanent_embeddings()`, manual notes participate in MOC generation with no gardener change at all.

A related latent bug was fixed alongside this: `literature_chunk_wikilink_for_row` recomputed a note's filename from its database row, which only matches for pipeline-named files. It now prefers `chunks.literature_note_path` whenever that file still exists, so both hand-written and hand-renamed notes get links that resolve. The harvester's literature index was also moved to the flat, title-slugged path every generated wikilink already pointed at.

**Web exposure**: `/notes/new` scaffolds SRC, LIT and ZTL (including `--from-lit`) and is the web counterpart of `zettel new-note`. Layout and JSON pickers are [ADR-039](../WEB/ADR-039-web-as-python-package.md) and [ADR-040](../WEB/ADR-040-json-pickers-progressive-enhancement.md). Image upload is still not a dedicated web page: adoption of pasted Obsidian images remains `sync-manual` ([ADR-031](../ASSETS/ADR-031-vault-first-image-adoption.md)).

This decision supersedes the DISCARD verdict recorded for "Decision 4: Manual Note Adoption Pattern (Vault-First Design)" in `docs/adrs/SYNC-module-analysis.md`, which scored 42/150 when the pattern was only a folder convention.

## References

* `zettel/manual_lit.py` — adoption, body parsing, candidate derivation, LIT-to-ZTL
* `zettel/manual_lit.py:46-56` — `ensure_manual_chapter` (NOT NULL FK on `chunks.chapter_id`)
* `zettel/sync.py` — `_sync_literature` dispatch on `origin`
* `zettel/connector.py:102-110` — `run_connect(..., origin=...)`
* `zettel/vault.py` — `literature_chunk_wikilink_for_row` prefers the on-disk path
* `zettel/state.py:138-150` — `concepts.chunk_id` NOT NULL with FK to `chunks`
* `tests/test_manual_flow.py` — end-to-end coverage of SRC → LIT → ZTL
