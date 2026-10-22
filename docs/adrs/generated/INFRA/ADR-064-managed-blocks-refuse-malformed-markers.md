# ADR-064: Managed Blocks Refuse Malformed Markers

**Status:** Accepted
**Date:** 2026-10-21
**Issue:** #321 (epic #320)
**Related to:** [ADR-030: Manual Notes Adopted at Sync Without the Review Gate](../MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md), [ADR-043: Distant Analogies as Suggestions](../RETRIEVAL/ADR-043-distant-analogies-as-suggestions.md)

## Context and Problem Statement

The pipeline writes inside an existing note only between `<!-- zettel:NAME:start -->` and `<!-- zettel:NAME:end -->`. Everything outside those markers belongs to the author; the README states it as a principle ("atualizações automáticas não destroem edições manuais").

`upsert_managed_block` located the block as "the first start, then the first end after it", and when the end was missing it appended a fresh block and left the old start in place. The next upsert found the orphan start and the end of the appended block, and replaced everything between them — including whatever the author had written there. The result looked like a valid note and nothing was logged. `docs/artigo/07-testes.md` §6.1 reproduces it with the real function.

A missing end marker is a realistic state of a note edited by hand in Obsidian: a deleted line, a sync conflict, a block copied half from another note. No test asserted the invariant: the one test of a replacement checked only the content of the block.

## Decision Drivers

* The author's text outside the blocks is never changed by the pipeline, whatever state the markers are in.
* A broken note must not stop a batch (`connect`, `review`, `sync-manual`, `summarize` touch many notes per run).
* The author must learn which note is broken without reading logs.

## Decision Outcome

1. **Valid structure is exactly one start followed by exactly one end, or no marker at all, per block name.** Anything else — start without end, end without start, end before start, two of either — raises `MalformedManagedBlock` from `upsert_managed_block`. The function never repairs: rewriting a note means deciding where the block ends, and every guess can erase text.
2. **Readers treat a malformed block as absent.** `read_managed_block` returns `None`; a caller that then tries to write is refused by (1).
3. **Writers skip the note, not the batch.** `safe_update_managed_blocks` catches the exception, logs the block, the reason and the path, and leaves the file byte for byte as it was (no `updated_at` bump). The two callers that scaffold a block directly (`review.sync_lit_permanent_links`, `summarize._write_blocks`) do the same.
4. **`zettel doctor` lists the broken notes** ("Blocos gerenciados malformados", path and block names) through `vault.vault_notes_with_malformed_blocks`, so the author can fix the markers by hand; the next run then updates the note normally.
5. **Fences are not consulted.** A marker inside a fenced code block counts like any other. `markdown_fences.iter_fenced_spans` extends an unclosed fence to the end of the file, so a source excerpt with an unbalanced fence would hide every later marker; the writer would then see "no block" and append a new one on each run. Counting every marker makes a note that documents the marker syntax inside a code block malformed, which is the safe failure: it is refused and reported, never rewritten.

### Consequences

* The invariant is pinned by property tests (`tests/test_managed_blocks_properties.py`, Hypothesis, 1,000 generated notes per property): text outside the blocks survives an upsert, the upsert is idempotent, a stray marker is refused and reported. Hypothesis is a development dependency (`uv` dev group).
* A note with broken markers stops receiving updates to that block until the author fixes it. Backlinks, connection suggestions and MOC back-references on that note go stale in the meantime; `doctor` says which note.
* `hashing.extract_embeddable_text` and `note_content` still strip blocks with their own line/regex rules; an orphan start there hides the rest of the note from the embedding (not from the vault). Left as is: no text is lost, and the note is already flagged by `doctor`.

## Alternatives

* **Repair: drop the orphan start, or close it at the next heading.** Rejected: both decide on the author's behalf where the managed text ends, which is exactly the guess that caused the loss.
* **Fence-aware markers.** Rejected for the reason in (5); it trades a refusal for unbounded block duplication.
* **Raise and let the batch fail.** Rejected: one hand-edited note would block `connect` for the whole vault.

## References

* `zettel/vault.py` — `MalformedManagedBlock`, `_managed_block_span`, `upsert_managed_block`, `read_managed_block`, `safe_update_managed_blocks`, `malformed_managed_blocks`, `vault_notes_with_malformed_blocks`
* `zettel/review.py` — `sync_lit_permanent_links`
* `zettel/summarize.py` — `_write_blocks`
* `zettel/cli/diagnostics.py` — `doctor`
* `tests/test_managed_blocks.py`, `tests/test_managed_blocks_properties.py`
