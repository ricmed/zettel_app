# ADR-XXX: Vault-First Image Adoption for Manual Notes

**Status:** Accepted
**Date:** 2026-09-02
**Depends on:** [ADR-XXX: Manual Notes Are Adopted at Sync Time and Bypass the Review Gate](../MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)

**Related to:**
- [ADR-XXX: Granular Per-Chunk Literature Notes with Readable Filenames](../EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md)
- [ADR-XXX: Layered Checksums for Incremental Processing](../INFRA/ADR-007-layered-hashing-strategy.md)

## Context and Problem Statement

The harvest pipeline pulls images out of PDFs (Docling) and Markdown, stores them content-addressed under `90_Assets/`, registers an `assets` row per image, and later describes each one with a multimodal LLM. Literature notes then render `## Imagens Relacionadas`, and permanent notes render `## Figuras`, from those rows.

None of that reached notes written by hand. `scaffold_manual_note` never passed `images=`, and `sync-manual` did not look at image references at all, so a manually authored literature note rendered `_Nenhuma._` no matter what the author had pasted into it. The user's requirement was explicitly about ease: attaching a figure to a hand-written literature note had to be as frictionless as the pipeline makes it, without a separate ceremony.

Obsidian already provides the natural gesture — paste or drag an image into the note and it writes an `![[file.png]]` embed, with the file landing wherever the vault's attachment settings put it. The open question was whether the system should meet the user at that gesture, or ask for an explicit command.

## Decision Drivers

* The vault is the source of truth and Obsidian is the editor; a workflow that requires leaving Obsidian to attach a figure is the friction the requirement was about removing.
* Whatever mechanism is chosen must produce the same `assets` rows the pipeline produces, so `## Figuras`, `asset_ids_in_text` and the purge cascade work identically for manual and harvested images.
* Obsidian's link forms vary — `![[name.png]]` shortest-path embeds and `![alt](relative/path.png)` Markdown refs both occur — and the attachment folder is user-configurable, so resolution cannot assume one layout.
* Describing an image costs a multimodal LLM call and is rate-limit sensitive; that cost should not be silently attached to a sync.
* `cfg.images.enabled` defaults to `false`, so anything gated on it would silently do nothing for most users.
* Adoption runs over the whole vault on every sync, so re-adopting an unchanged image must be free and must not duplicate rows or mutate the note.

## Considered Options

* Adopt during `sync-manual`: scan note bodies for image references, copy into `90_Assets/`, rewrite the reference, register the asset, leave description to the existing pipeline step.
* A dedicated `zettel attach-image <note> <file>` command.
* A web upload page for attaching images to a note.

## Decision Outcome

Chosen option: "Adopt during `sync-manual`," because it costs the user no new gesture at all: paste the image in Obsidian, run the sync they already run, and the figure is registered. The explicit command and the web page were both considered and rejected for this round — the command adds a step to the workflow whose friction was the whole point, and the web page is out of scope while manual note creation itself is CLI-only ([ADR-030](../MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)).

`assets.adopt_vault_images` handles both `![[...]]` embeds and `![alt](...)` Markdown refs, preserving whichever syntax the author used so the note keeps rendering the way they wrote it. Only extensions in a known image set are touched, so `![[Another Note]]` transclusions and `http(s)` URLs pass through untouched. A referenced file is resolved vault-relative, then note-relative, then by a recursive search for its basename — the last covering Obsidian's shortest-path links into a user-configured attachment folder. Files are copied through the pipeline's own `_save_image`, so names are content hashes: identical images deduplicate, and a second sync is a no-op. The original file is never deleted; a copy under `90_Assets/` is cheap, and deleting something the user placed is not the sync's call to make.

Two consequences of the drivers deserve to be explicit:

**Adoption is not gated on `cfg.images.enabled`.** That flag governs the cost of extraction and multimodal description. Adoption is vault bookkeeping — a file copy and a database row — with no LLM involved. Gating it would mean the chosen "easy path" silently did nothing under the default configuration, which is the opposite of the requirement.

**Adoption never calls the LLM.** The asset is registered `status='pending'`, exactly as harvest leaves it. Description happens where it already happens: `extract` calls `describe_pending_assets`, and the web UI's "retry assets" button does the same, both respecting `images.enabled` and the existing rate-limit backoff. Until then the figure renders in Obsidian and, because the canonical `90_Assets/` path now appears in the note body, `assets.asset_ids_in_text` already resolves it for `connector`'s `## Figuras`.

### Positive Consequences

* Attaching a figure to a manual note requires no command the user was not already running.
* Manual and harvested images are indistinguishable downstream: same content-addressed naming, same `assets` schema, same description path, same purge cascade.
* Because names are content hashes, the same figure pasted into several notes occupies one file and one row.

### Negative Consequences

* The recursive basename fallback scans the vault when a reference resolves neither vault-relative nor note-relative; on a large vault with many unresolvable references this is repeated work.
* The user's original copy remains outside `90_Assets/`, so the vault holds two copies until the user removes one.
* An image referenced only from a note with no resolvable `source_id` is left alone, since `assets.source_id` is NOT NULL with a foreign key to `sources`.

## Pros and Cons of the Options

### Adoption during sync-manual (chosen)

* Good, because it matches the gesture Obsidian already gives the user.
* Good, because it reuses `_save_image`, `asset_id_for` and `upsert_asset`, so manual images are pipeline images.
* Good, because content-addressed naming makes re-adoption idempotent for free.
* Bad, because it adds a filesystem scan and a body rewrite to a command that previously only read notes.

### Dedicated `attach-image` command

* Good, because resolution is unambiguous — the user names the file.
* Good, because it can attach images that live outside the vault.
* Bad, because it adds a step to exactly the workflow whose friction motivated the requirement.
* Bad, because it does not help the user who already pasted the image into Obsidian.

### Web upload page

* Good, because it would suit users working primarily in the web UI.
* Bad, because manual note creation is not exposed in the web UI at all, so an upload page would have nothing to attach to.
* Bad, because `web.py` currently rejects image uploads by extension, so it is a larger change than it appears.

## Consequences

`sync-manual` now rewrites note bodies, not just frontmatter. Adoption runs for granular literature notes (through `manual_lit.adopt_manual_literature`) and for permanent notes whose frontmatter names a known source (through `sync._adopt_note_images`); a rewrite bumps the note's checksum, so the note is legitimately re-embedded on that pass and skipped on the next.

Turning on `images.enabled` later retroactively describes every asset adopted while it was off, because they are all sitting in `status='pending'` — no re-sync or re-adoption is needed.

If a future round adds `attach-image` or a web upload, both should funnel into `adopt_vault_images` rather than reimplementing copy-and-register, so there stays exactly one definition of what adopting an image means.

## References

* `zettel/assets.py` — `adopt_vault_images`, `_resolve_vault_image`, `_is_canonical_asset`
* `zettel/assets.py:38-55` — `asset_id_for` / `_asset_relpath` / `_save_image` (content-addressed naming, reused)
* `zettel/assets.py` — `describe_pending_assets` (unchanged; still the only LLM path)
* `zettel/manual_lit.py` — adoption call inside `adopt_manual_literature`
* `zettel/sync.py` — `_adopt_note_images` for permanent notes
* `zettel/state.py:180-193` — `assets` schema (`source_id` NOT NULL with FK)
* `tests/test_manual_flow.py` — wiki embed, Markdown ref, remote URL, idempotency
