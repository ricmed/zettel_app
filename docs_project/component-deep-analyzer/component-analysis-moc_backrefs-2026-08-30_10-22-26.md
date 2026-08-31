# Component Deep Analysis Report — `moc_backrefs`

**Component**: `zettel/moc_backrefs.py`
**Analysis date**: 2026-08-30 10:22:26
**Analyzed by**: Component Deep Analysis (read-only)

---

## 1. Executive Summary

`moc_backrefs.py` is a small, single-file utility component (124 lines) whose sole purpose is to keep a **reverse-link managed block** (`auto-moc-backrefs`) synchronized on every permanent note (`ZTL - ...`) that a MOC (`MOC - ...` / `HUB - ...`) references. It is the mechanism that lets a reader open a permanent note in Obsidian and see, without any manual work, "which MOCs point at this note" — the inverse of a MOC's own note list.

The component is not itself a pipeline phase; it is **cross-cutting infrastructure** invoked at the end of every code path that writes or deletes a MOC body:

- Phase 4 taxonomy pipeline (`gardener.py`) — new MOC creation and incremental MOC updates.
- Phase 4b hub pipeline (`gardener_hub.py`) — new hub-MOC creation and incremental hub-MOC updates (via the same shared `gardener._apply_incremental_placements` helper).
- Manual MOC sync (`sync.py`, `zettel sync-manual`) — hand-edited or hand-created MOC files.
- MOC purge (`gardener.purge_pipeline_mocs`, `gardener_hub.purge_hub_pipeline_mocs`, triggered by `zettel garden --recreate` / `--hubs --recreate`) — full teardown of backrefs before a MOC row/file is deleted.

Key findings:

- The component performs a **diff between an old and new set of note IDs extracted from a MOC's Markdown body** (via regex in `gardener_assign.extract_note_ids_from_moc_body`) and applies only the delta (additions/removals) to each affected note's managed block — it never rewrites a note it doesn't need to touch.
- All file mutation is delegated to `vault.safe_update_managed_blocks`, which preserves everything outside the managed block (so it is safe to run against hand-edited permanent notes) and only bumps `updated_at` if content actually changed.
- The module has **no direct SQLite writes** — it only reads (`db.get_note`) to resolve a note's on-disk path. Its output is entirely file-system side effects (managed-block text) plus, indirectly, whatever the caller does afterward (e.g. re-indexing).
- Coverage is good for the two public entry points (`sync_moc_backrefs`, `clear_moc_backrefs`) via a dedicated test file, but several internal behaviors (dead branch in `moc_wikilink`, substring-based ID matching in removal) are not exercised and represent latent risk described in §10.

---

## 2. Data Flow Analysis

Two independent flows exist: an **update flow** (add/remove backrefs when a MOC's note list changes) and a **teardown flow** (strip a MOC's backrefs entirely before deleting the MOC).

### 2.1 `sync_moc_backrefs` — update flow

```
1. Caller (gardener.py / gardener_hub.py / sync.py) finishes writing the MOC's
   Markdown file/body (safe_write_note or in-place edit).
2. Caller invokes sync_moc_backrefs(db, moc_id, moc_topic, moc_path,
     previous_body=<body before the edit, or None for a brand-new MOC>,
     new_body=<body just written, or None to force a re-read from disk>)
3. If new_body is None: read moc_path from disk and parse frontmatter/body
   (vault.parse_frontmatter). If the file does not exist, abort silently (no-op).
4. Extract note IDs from new_body and from previous_body (regex over
   "[[ZTL - <id> - ...]]" wikilinks) -> new_ids, old_ids.
5. Build the single link line this MOC will contribute to any note's
   backref block: moc_link_line(moc_id, moc_topic, path=moc_path).
6. For each note_id in (old_ids - new_ids)  [removed from the MOC]:
     a. Resolve the note's on-disk path via db.get_note(note_id).
     b. If the file exists, strip any backref line mentioning moc_id
        from its auto-moc-backrefs block (_remove_moc_link_from_note).
7. For each note_id in (new_ids - old_ids)  [added to the MOC]:
     a. Resolve the note's on-disk path via db.get_note(note_id).
     b. If the file exists and the link line is not already present,
        append it to the note's auto-moc-backrefs block
        (_add_moc_link_to_note), via safe_update_managed_blocks.
8. Notes present in both old_ids and new_ids are left untouched (no file I/O).
```

### 2.2 `clear_moc_backrefs` — teardown flow (used by `--recreate` purge)

```
1. Caller (gardener.purge_pipeline_mocs / gardener_hub.purge_hub_pipeline_mocs)
   has already deleted the MOC's row from SQLite (db.delete_pipeline_mocs() /
   delete_hub_pipeline_mocs()) and holds the deleted row as a dict.
2. clear_moc_backrefs(db, moc_dict) is called once per deleted MOC.
3. Resolve the MOC's body: prefer moc["body"] (SQLite snapshot); if empty,
   fall back to re-parsing moc["path"] from disk if the file still exists.
4. If moc_id is missing/empty, abort silently.
5. Extract every note_id referenced by that body.
6. For each note_id: resolve its file path via db.get_note(note_id) and,
   if found, strip any line mentioning moc_id from its backref block
   (same _remove_moc_link_from_note as the update flow).
7. Caller then deletes the MOC's vector-index entry and vault file
   (outside this component's scope).
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Invariant | A backref block only ever lists MOCs that currently reference the note — additions and removals are diffed, not replaced wholesale | moc_backrefs.py:94-106 |
| Idempotency | Adding a link that is already present in the block is a no-op (no duplicate lines) | moc_backrefs.py:59-63 |
| Idempotency | `safe_update_managed_blocks` only writes to disk (and bumps `updated_at`) when the composed content actually differs from what's on disk | vault.py:156-157 |
| Guard | A note whose SQLite row is missing, or whose `path` no longer points at an existing file, is silently skipped (no exception, no dangling write) | moc_backrefs.py:45-50 |
| Guard | `sync_moc_backrefs` is a no-op if `new_body` is omitted and the MOC file no longer exists on disk | moc_backrefs.py:89-91 |
| Guard | `clear_moc_backrefs` is a no-op if the deleted MOC record carries no `moc_id` | moc_backrefs.py:117-119 |
| Filename resolution | The backref link text is derived from the MOC's actual file stem when a path is available, never re-slugified independently | moc_backrefs.py:26-30 |
| Filename resolution | Fallback link construction (no path given) always emits an `MOC -` prefixed filename, never `HUB -`, regardless of the MOC's real type | moc_backrefs.py:31-33 |
| Membership extraction | Note membership of a MOC is derived purely from `[[ZTL - <id> - ...]]` wikilinks appearing anywhere in the MOC body — headings, prose, or list items all count | gardener_assign.py:20-24 |
| Matching | Removal of a specific MOC's link line matches by plain substring containment of `moc_id` in the line text, not by parsing the wikilink target | moc_backrefs.py:53-54 |
| Cleanup | Removing a link also drops any blank lines left in the block, keeping the managed block compact | moc_backrefs.py:71-74 |
| Symmetry | Every pipeline/manual code path that writes a MOC body must supply the *previous* body (or `None` for first creation) so removals are computed correctly; supplying no `previous_body` means every existing member is treated as "old" only if it's also passed as such — never guessed | moc_backrefs.py:94-95 (see call-site audit below) |
| Isolation | The `auto-moc-backrefs` block itself is excluded when `sync.py` derives manual graph edges from a note's body, preventing MOC backref links from being miscounted as user-authored `related` edges | sync.py:36, 349-362 |

### Detailed breakdown of the business rules

---

### Business Rule: Diff-based backref synchronization

**Overview**:
`sync_moc_backrefs` never treats a MOC update as "regenerate every note's backref block from scratch." Instead, it computes the symmetric difference between the note-ID set extracted from the MOC's body *before* the edit and the set extracted *after* the edit, and only touches the notes that actually entered or left the MOC.

**Detailed description**:
This design exists because a MOC can reference dozens of permanent notes, and touching every one of them on every MOC update would mean rewriting (and bumping `updated_at` on) far more files than necessary — polluting git history / Obsidian's "recently modified" view and doing needless disk I/O. By keying the diff on the extracted ID sets (`new_ids - old_ids` for additions, `old_ids - new_ids` for removals) rather than "for every note currently in the MOC, ensure a backref exists," the component's cost is proportional to the actual change, not to MOC size.

The rule depends entirely on the caller supplying an accurate `previous_body`. For brand-new MOCs (`gardener._create_new_moc`, `gardener_hub._create_new_hub_moc`), no `previous_body` is passed, so `old_ids` is the empty set and every referenced note gets a backref added — correct, since there is nothing to remove yet. For incremental updates (`gardener._apply_incremental_placements`, shared by both the taxonomy and hub pipelines), the caller reads the MOC file from disk *before* rewriting it (`content = moc_path.read_text(...)`, `meta, previous_body = parse_frontmatter(content)`) and passes that exact snapshot as `previous_body`. For manual sync (`sync.py::_sync_moc`), `previous_body` comes from the SQLite `mocs.body` column captured before the new `db.upsert_moc(...)` call overwrites it — again, a genuine "before" snapshot.

One structural consequence worth flagging: the incremental placement path (`_apply_incremental_placements`) only ever *adds* notes to a MOC (`truly_new = [nid for nid in note_ids if nid not in existing_ids]`) — it never drops existing members. Since the rewritten body always keeps every previously-linked note, `old_ids - new_ids` is mathematically empty on that path. The removal branch of `sync_moc_backrefs` is therefore only exercised in practice by (a) a human manually deleting a `[[ZTL - ...]]` line from a MOC file before `sync-manual` runs, or (b) the full teardown flow (`clear_moc_backrefs`) during `--recreate`. This is an accurate description of current behavior, not a defect per se, but it means the "removal" half of this rule has narrower real-world exercise than the "addition" half.

**Rule workflow**:
```
old_ids = extract_note_ids(previous_body)   # empty set if no previous_body
new_ids = extract_note_ids(new_body)
removed = old_ids - new_ids  -> strip moc_id's link line from each note
added   = new_ids - old_ids  -> append moc_id's link line to each note
unchanged = old_ids & new_ids -> no file touched
```

---

### Business Rule: Silent skip on unresolved or missing note files

**Overview**:
Whenever the component needs to write to a permanent note referenced by a MOC, it first resolves that note's file path through `StateDB.get_note(note_id)`. If the note has no DB row, no `path` field, or the path no longer exists on disk, the update for that single note is skipped — no exception is raised and no other note's processing is aborted.

**Detailed description**:
This guard (`_note_path_from_db`) protects the whole vault-sync pipeline from being brought down by a single stale or dangling `[[ZTL - ...]]` reference — for example, a permanent note that was manually deleted from the vault, or a note whose file was moved without updating SQLite, or (most commonly) a note that belongs to a different vault snapshot than the one currently mounted (e.g., in tests, or when `--dry-run`-style tooling constructs partial fixtures). Because `sync_moc_backrefs` and `clear_moc_backrefs` are invoked as the very last step of MOC-writing operations that have already committed real work (an LLM call, a file write, an SQLite upsert), the design intentionally favors "best-effort, keep going" over "fail the whole `garden` run because one backref target is missing."

The cost of this permissiveness is that a genuinely broken reference (a MOC pointing at a note_id that was deleted from the vault without going through `delete-source`'s wikilink-stripping logic) will simply never receive a backref, indefinitely, with no warning surfaced to the operator — there is no `logger.warning` call on this path, only silent continuation. Combined with the fact that `extract_note_ids_from_moc_body` finds note references purely by regex over the MOC's *own* body (not cross-checked against `notes` table membership at extraction time), a MOC can carry forward a reference to a long-gone note across many `garden` runs without any of them detecting the inconsistency from the backref side.

**Rule workflow**:
```
note = db.get_note(note_id)
if note is None or note["path"] is missing or Path(note["path"]) is not a file:
    return None   # caller: skip this note_id entirely, no error surfaced
```

---

### Business Rule: Idempotent, additive managed-block writes

**Overview**:
Adding a backref link to a note is guarded twice: once at the semantic level (don't add a line that's already present) and once at the file-write level (don't touch the file at all if the fully composed content is byte-identical to what's already there).

**Detailed description**:
`_add_moc_link_to_note` reads the note's current `auto-moc-backrefs` block content and checks whether the exact link line (after `.strip()`) is already a substring of it; if so, it returns immediately without calling `safe_update_managed_blocks`. This matters because `sync_moc_backrefs` can be invoked multiple times against the same MOC/note pair across separate pipeline runs (e.g., `garden` run today adds NOTE001 to MOC001; a later `garden` run recomputes the same cluster and calls `sync_moc_backrefs` again with an unchanged body) — without this check, re-running the pipeline would be safe from a *correctness* standpoint (the block would still end up correct) but would unnecessarily rewrite files and bump `updated_at` timestamps on notes that didn't actually change.

The second layer of idempotency lives in `vault.safe_update_managed_blocks`: even when `_add_moc_link_to_note` does proceed to call it (because the line-level check passed, e.g., first time a link is added), that function composes the *entire* new file content in memory and compares it to the original before writing; only a real content diff triggers `path.write_text(...)` and the `updated_at` bump. This means the on-disk mtime and Obsidian's own "modified" indicator only change when the backref set genuinely changed — an important property for the "manual notes are the source of truth; the pipeline only touches its own managed blocks" invariant this project maintains project-wide (see `vault.py` module docstring).

**Rule workflow**:
```
existing = read_managed_block(note_content, "auto-moc-backrefs")
if existing and link_line.strip() in existing:
    return  # already present, no-op
inner = existing + "\n" + link_line   (or just link_line if block was empty/absent)
safe_update_managed_blocks(note_path, {"auto-moc-backrefs": inner})
  -> internally: compose full file, compare to original bytes,
     write + bump updated_at ONLY if different
```

---

### Business Rule: MOC filename resolution for the backref link text

**Overview**:
The exact wikilink text placed into a note's backref block is derived from the MOC's actual on-disk filename stem whenever that filename is known, rather than being independently reconstructed from `(moc_id, topic)`.

**Detailed description**:
`moc_wikilink(moc_id, topic, path=...)` prefers `Path(path).stem` when a path is supplied and non-empty — this is the case on every real call site, since `sync_moc_backrefs` always receives a concrete `moc_path` (either the file just written by the caller, or a path read from the MOC's SQLite row). Using the real filename stem guarantees the backref link always resolves in Obsidian even if the MOC's topic contains characters that the slugifier (`vault._slug`, used elsewhere for filename generation) would transform differently than however the file was actually named — e.g. a topic edited by a human after creation, or a topic whose slug drifted between the version used to name the file and the version now stored in `topic`.

Only when no `path` is supplied (not exercised by any current production call site — see §10) does the function fall back to building a filename from scratch via `note_filename(prefix, moc_id, slug_topic)`, where `prefix` is chosen as `"HUB"` only if the *path string itself* (not the constructed filename) starts with `"HUB -"`. Because this fallback branch is only reached when `path` is falsy, the `str(path or "")` expression is always `""` in that branch, so the condition can never be true in practice — the fallback always resolves to the `"MOC"` prefix, never `"HUB"`, regardless of whether the MOC being referenced is actually a hub MOC. This is a latent inconsistency (see §10) that has not manifested as a user-visible bug only because no current caller omits `path`.

**Rule workflow**:
```
if path given and Path(path).stem is non-empty:
    return "[[" + stem + "]]"          # authoritative: matches file on disk
else:
    prefix = "HUB" if str(path or "").startswith("HUB -") else "MOC"  # always "MOC" in practice
    return "[[" + note_filename(prefix, moc_id, topic or moc_id).removesuffix(".md") + "]]"
```

---

### Business Rule: Full backref teardown on MOC deletion

**Overview**:
When a pipeline-generated MOC is purged (`zettel garden --recreate` or `--hubs --recreate`), every permanent note that block references that MOC must have the corresponding line removed from its `auto-moc-backrefs` block before (or regardless of) the MOC's own file being deleted.

**Detailed description**:
`clear_moc_backrefs` is called once per deleted MOC row, immediately after `db.delete_pipeline_mocs()` / `db.delete_hub_pipeline_mocs()` has already removed the MOC's SQLite row (the caller passes the just-deleted row dict, which is why the function prefers `moc["body"]` — the SQLite snapshot — over re-reading the vault file, and only falls back to the file if the SQLite body was empty). This ordering matters: because the MOC's own vault file is deleted *after* `clear_moc_backrefs` runs (see `gardener.purge_pipeline_mocs`, lines ~183-189, which unlink the file only after calling `clear_moc_backrefs` for every removed row), there's a brief window where the MOC file could still be read from disk as a fallback if the SQLite body were ever empty — a defensive design against a partially-populated `mocs.body` column (e.g., data from an older schema version, or a row that predates `body` being stored).

Without this rule, a `--recreate` cycle would leave every previously-generated MOC's backref lines dangling in permanent notes, pointing at a wikilink target that no longer exists in the vault — a silent broken-link accumulation that would only be discovered by a human clicking through Obsidian's graph view. The rule is also why `--recreate` is presented to the user as fully regenerative: after purge, a fresh `garden` run (which happens as the next step of the same CLI invocation) will recreate all backrefs for the newly generated MOCs from scratch, so the net effect of `--recreate` observed by the end user is "MOCs and their backrefs look freshly built," even though internally it's teardown-then-rebuild rather than an in-place transform.

**Rule workflow**:
```
for moc in db.delete_pipeline_mocs():       # SQLite rows already gone at this point
    clear_moc_backrefs(db, moc):
        body = moc["body"] or (re-read moc["path"] from disk if it still exists)
        if not moc["moc_id"]: return
        for note_id in extract_note_ids(body):
            note_path = resolve(note_id)
            if note_path: strip moc_id's line from its backref block
idx.delete_mocs([...])                       # vector index cleanup (separate component)
for moc in removed: unlink moc["path"] if it still exists on disk
```

---

## 4. Component Structure

```
zettel/
├── moc_backrefs.py                 # THIS COMPONENT — backref block sync/teardown
│   ├── MOC_BACKREFS_BLOCK          # constant: "auto-moc-backrefs" managed-block name
│   ├── moc_wikilink()              # build a [[...]] link to a MOC file
│   ├── moc_link_line()             # wrap moc_wikilink() as a "- [[...]]" list line
│   ├── _note_path_from_db()        # resolve a note_id to an existing Path via StateDB
│   ├── _link_references_moc()      # substring test: does this line mention moc_id?
│   ├── _add_moc_link_to_note()     # append a MOC's link line to a note's block (idempotent)
│   ├── _remove_moc_link_from_note()# strip a MOC's link line from a note's block
│   ├── sync_moc_backrefs()         # PUBLIC: diff old/new MOC membership, apply deltas
│   └── clear_moc_backrefs()        # PUBLIC: strip a deleted MOC from every note it touched
│
├── gardener_assign.py               # supplies extract_note_ids_from_moc_body() (regex over
│                                     # "[[ZTL - <id> - ...]]") — the membership-extraction
│                                     # primitive this component depends on but does not own
├── vault.py                         # supplies managed-block I/O primitives this component
│                                     # depends on: parse_frontmatter, read_managed_block,
│                                     # safe_update_managed_blocks, note_filename
├── gardener.py                      # CONSUMER (taxonomy pipeline): calls sync_moc_backrefs
│                                     # on MOC create + incremental update; clear_moc_backrefs
│                                     # inside purge_pipeline_mocs (--recreate)
├── gardener_hub.py                  # CONSUMER (hub pipeline): calls sync_moc_backrefs on hub
│                                     # MOC create (incremental update reuses gardener's shared
│                                     # _apply_incremental_placements); clear_moc_backrefs inside
│                                     # purge_hub_pipeline_mocs (--hubs --recreate)
├── sync.py                          # CONSUMER (manual sync): calls sync_moc_backrefs from
│                                     # _sync_moc() for hand-created/hand-edited MOC files;
│                                     # also EXCLUDES the auto-moc-backrefs block from
│                                     # _extract_body_edges' manual-wikilink graph-edge scan
└── state.py                         # StateDB.get_note() is the only runtime DB dependency
                                      # (note lookup by ID -> file path)

tests/
└── test_moc_backrefs.py             # dedicated test file for this component (5 tests)
```

---

## 5. Dependency Analysis

```
Internal Dependencies:

moc_backrefs.sync_moc_backrefs
  -> gardener_assign.extract_note_ids_from_moc_body   (membership extraction)
  -> vault.parse_frontmatter                          (re-read MOC body from disk, fallback path)
  -> moc_backrefs.moc_link_line -> moc_backrefs.moc_wikilink -> vault.note_filename
  -> moc_backrefs._note_path_from_db -> state.StateDB.get_note
  -> moc_backrefs._add_moc_link_to_note    -> vault.read_managed_block, vault.safe_update_managed_blocks
  -> moc_backrefs._remove_moc_link_from_note -> vault.read_managed_block, vault.safe_update_managed_blocks
                                              -> moc_backrefs._link_references_moc

moc_backrefs.clear_moc_backrefs
  -> vault.parse_frontmatter                          (re-read MOC body from disk, fallback path)
  -> gardener_assign.extract_note_ids_from_moc_body
  -> moc_backrefs._note_path_from_db -> state.StateDB.get_note
  -> moc_backrefs._remove_moc_link_from_note

Inbound (who calls this component):
gardener.py       -> sync_moc_backrefs (new MOC), sync_moc_backrefs (incremental, via
                      _apply_incremental_placements), clear_moc_backrefs (purge_pipeline_mocs)
gardener_hub.py    -> sync_moc_backrefs (new hub MOC), clear_moc_backrefs (purge_hub_pipeline_mocs)
                      [incremental hub update reuses gardener._apply_incremental_placements,
                       which is the same code path that calls sync_moc_backrefs for gardener.py]
sync.py            -> sync_moc_backrefs (_sync_moc, manual MOC create/update)
cli.py             -> indirectly, via `zettel garden` / `garden --hubs` / `--recreate` /
                      `zettel sync-manual`, none of which call this module directly

External Dependencies:
- Python stdlib: pathlib.Path, logging, __future__ annotations, typing.TYPE_CHECKING
- No third-party packages, no network I/O, no database writes (read-only StateDB.get_note)
- Filesystem: reads/writes Markdown note files under the caller-supplied vault paths
```

There are no `requirements.txt` entries specific to this component — its only "external" surface is the local filesystem via `pathlib` and the shared `vault.py` I/O helpers.

---

## 6. Afferent and Efferent Coupling

Treating each top-level function as the unit of coupling (this is a procedural module, no classes):

| Function | Afferent Coupling (called by) | Efferent Coupling (calls out to) | Critical |
|----------|-------------------------------|-----------------------------------|----------|
| `sync_moc_backrefs` | 4 external call sites (gardener.py x2, gardener_hub.py x1, sync.py x1) + direct test coverage | 6 (`extract_note_ids_from_moc_body`, `parse_frontmatter`, `moc_link_line`, `_note_path_from_db`, `_add_moc_link_to_note`, `_remove_moc_link_from_note`) | High — sole update entry point, on the critical path of every MOC write |
| `clear_moc_backrefs` | 2 external call sites (`gardener.purge_pipeline_mocs`, `gardener_hub.purge_hub_pipeline_mocs`) + direct test coverage | 4 (`parse_frontmatter`, `extract_note_ids_from_moc_body`, `_note_path_from_db`, `_remove_moc_link_from_note`) | Medium — only exercised on the destructive `--recreate` path, but a regression here leaves orphaned backrefs across the whole vault |
| `moc_wikilink` | 2 internal (`moc_link_line`) + 1 direct test (`test_moc_link_line_uses_path_stem`) | 1 (`vault.note_filename`, fallback branch only) | Low — pure function, easy to reason about, but the fallback branch is effectively dead in production (see §10) |
| `moc_link_line` | 1 internal (`sync_moc_backrefs`) + 1 direct test | 1 (`moc_wikilink`) | Low |
| `_note_path_from_db` | 2 internal (`sync_moc_backrefs`, `clear_moc_backrefs`) | 1 (`state.StateDB.get_note`) | Medium — single point of failure for "can we even reach this note" across both flows |
| `_link_references_moc` | 1 internal (`_remove_moc_link_from_note`) | 0 | Low, but semantically risky (substring match — see §10) |
| `_add_moc_link_to_note` | 1 internal (`sync_moc_backrefs`) | 2 (`vault.read_managed_block`, `vault.safe_update_managed_blocks`) | Medium — the only write path for additions |
| `_remove_moc_link_from_note` | 2 internal (`sync_moc_backrefs`, `clear_moc_backrefs`) | 2 (`vault.read_managed_block`, `vault.safe_update_managed_blocks`) + `_link_references_moc` | Medium — the only write path for removals, shared by both update and teardown flows |

At the module level: `moc_backrefs.py` has **afferent coupling of 3 modules** (`gardener.py`, `gardener_hub.py`, `sync.py`, plus its dedicated test module) and **efferent coupling of 3 modules** (`gardener_assign.py`, `vault.py`, `state.py` as a type-only/runtime-method dependency). This is a favorable ratio for a piece of cross-cutting infrastructure: it is depended upon by all MOC-writing code paths but itself depends on very little, which keeps it easy to reason about in isolation — the risk surface is concentrated in the shared `vault.py` managed-block primitives rather than duplicated here.

---

## 7. Endpoints

Not applicable — `moc_backrefs.py` exposes no REST/GraphQL/gRPC/CLI surface of its own. It is an internal library module invoked exclusively by other Python modules within the same process (`gardener.py`, `gardener_hub.py`, `sync.py`).

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| Obsidian vault filesystem (`30_Permanent/*.md`) | Local filesystem | Read/write the `auto-moc-backrefs` managed block on permanent notes | Direct file I/O (`pathlib`) | Markdown with YAML frontmatter + HTML-comment-delimited managed blocks | Missing file: silent skip (`_note_path_from_db` returns `None`); no exception propagation |
| Obsidian vault filesystem (`40_MOCs/*.md`) | Local filesystem | Read a MOC's body to extract current note membership (fallback when `new_body` not passed) | Direct file I/O (`pathlib`) | Markdown with YAML frontmatter | Missing MOC file: `sync_moc_backrefs` returns early (no-op) |
| `StateDB` (`state.py`) | Internal service (SQLite) | Resolve `note_id -> file path` via `get_note()` | In-process method call | `dict` row (`sqlite3.Row`-like) | Missing/`None` row, missing `path` key, or non-existent path all degrade to "skip this note" rather than raising |
| `gardener_assign.extract_note_ids_from_moc_body` | Internal library | Regex-based extraction of `[[ZTL - <id> - ...]]` targets from a MOC body | In-process function call | `str` in, `set[str]` out | No error handling needed — regex `findall` never raises on non-matching text |
| `vault.safe_update_managed_blocks` | Internal library | Idempotent, diff-aware managed-block writer that preserves manual edits | In-process function call | Full file content in/out | Logs a warning and no-ops if the target path doesn't exist (defense already duplicated by this component's own `_note_path_from_db` check) |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Diff-and-patch synchronization | `sync_moc_backrefs` computes `old_ids`/`new_ids` and only mutates the symmetric-difference notes | moc_backrefs.py:94-106 | Minimizes file churn and `updated_at` noise; makes the operation naturally idempotent when called repeatedly with the same before/after state |
| Managed-block convention (delegated) | `MOC_BACKREFS_BLOCK = "auto-moc-backrefs"`, read/written exclusively through `vault.read_managed_block` / `safe_update_managed_blocks` | moc_backrefs.py:17, 59-75 | Lets the pipeline own a well-defined slice of a note's Markdown while leaving everything else (title, body prose, other managed blocks) under manual/human control — a project-wide convention this module conforms to rather than reimplements |
| Fail-soft / best-effort traversal | Every per-note operation inside a loop (`for note_id in ...`) is individually guarded; a single unresolved note never aborts the whole sync/clear call | moc_backrefs.py:98-106, 121-124 | Keeps a `garden` run's MOC-writing side effects resilient to partial vault/DB inconsistency |
| Snapshot-before-mutate (caller-side contract) | Every call site captures `previous_body` (from disk or from the SQLite row) *before* overwriting the MOC, then passes it explicitly | gardener.py:646-647, 715-722; sync.py:302-312 | Because this component has no way to independently know "what did this MOC look like a moment ago," correctness is entirely contingent on the caller upholding this snapshot discipline — an implicit interface contract rather than an enforced one |
| Facade over lower-level vault primitives | The two public functions hide the read/diff/write choreography from callers, who only ever pass IDs, bodies, and a path | moc_backrefs.py:78-125 | Keeps `gardener.py`/`gardener_hub.py`/`sync.py` free of managed-block manipulation details |

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| Medium | `moc_wikilink` (moc_backrefs.py:31-33) | The `prefix = "HUB" if str(path or "").startswith("HUB -") else "MOC"` fallback branch is unreachable with a correct `"HUB -"` result in practice: it only executes when `path` is falsy or its `Path(...).stem` is empty, in which case `str(path or "")` is always `""`, so the condition can never be true. Every current call site always supplies a real, stem-bearing `path`, so this dead branch has caused no observed bug — but if a future caller ever invokes `moc_wikilink`/`moc_link_line` without a path for a hub MOC, the generated link would incorrectly use the `MOC -` filename prefix instead of `HUB -`, producing a backref line that does not resolve to any file in Obsidian. | Silent broken wikilink if the no-path fallback is ever exercised for a hub MOC; currently dormant/latent. |
| Low-Medium | `_link_references_moc` (moc_backrefs.py:53-54) | Matching is `moc_id in line` — a raw substring test against the entire line text, not a parse of the wikilink target. Since production `moc_id` values are 26-character Crockford-base32 ULIDs, an accidental substring collision between two different MOC IDs is astronomically unlikely, but the check is not proven safe by construction — e.g. it would also match if `moc_id` happened to appear inside a topic name embedded elsewhere in the same line, or under future ID-format changes. No test exercises a near-collision case. | Could remove or fail to remove the wrong backref line under an ID-format change or an adversarial/unlikely ID collision; currently low real-world probability. |
| Low | Incremental update path (`gardener._apply_incremental_placements`, shared by both pipelines) | `truly_new` is computed as notes not already in the MOC; existing members are never dropped by the LLM-driven incremental classification. Consequently `old_ids - new_ids` is always empty on this path, meaning the *removal* half of `sync_moc_backrefs`'s diff logic is only exercised via `clear_moc_backrefs` (full purge) or manual hand-edits (`sync.py`), never via a normal incremental `garden` run that legitimately drops a note from a cluster. | Reduced real-world test/production exposure of the removal code path relative to the addition path; not a bug, but a coverage/verification gap worth being aware of. |
| Low | Silent skip with no logging (`_note_path_from_db`, moc_backrefs.py:45-50) | A note that cannot be resolved (missing DB row, missing path, deleted file) is skipped with zero logging, on both the add and remove flows. | Operators have no visibility into "this permanent note is referenced by a MOC but its backref could not be written/removed" without manually cross-referencing the vault; harder to detect vault/DB drift from log output alone. |
| Low | No transactional/atomic multi-file guarantee | `sync_moc_backrefs` writes N separate files sequentially (one per affected note) with no rollback if a later write fails (e.g., disk full, permissions) after earlier writes succeeded. | A partially-applied sync (some notes updated, others not) is possible on I/O failure mid-loop; the caller has no way to detect or retry only the failed subset — the next full `garden`/`sync-manual` run is the de facto recovery mechanism. |

---

## 11. Test Coverage Analysis

| Component Area | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------------|-----------|--------------------|----------|---------------|
| `sync_moc_backrefs` (add path) | 1 (`test_sync_moc_backrefs_adds_links_to_permanent_notes`) | 0 dedicated (exercised indirectly by `test_sync_manual_updates_moc_backrefs`) | Good | Asserts both block presence and exact wikilink text (`[[MOC - MOC001 - topico]]`); real files on `tmp_path`, real `StateDB` — genuine integration-style test despite being in a "unit" file |
| `sync_moc_backrefs` (remove path, add+remove in same call) | 1 (`test_sync_moc_backrefs_removes_stale_links`) | 0 | Good for the basic case | Verifies the removed note's block loses the reference while the retained note keeps it; does not test removing a note that itself has multiple MOC backrefs (block with 2+ lines, remove one, keep the other) |
| `clear_moc_backrefs` via `purge_pipeline_mocs` | 1 (`test_clear_moc_backrefs_on_purge`) | 1 (same test also validates `gardener.purge_pipeline_mocs` end-to-end) | Good | Confirms the backref block no longer mentions the purged MOC after `purge_pipeline_mocs`; uses a `MagicMock` for `VectorIndex`, appropriately isolating from ChromaDB |
| `sync_moc_backrefs` via manual sync (`sync.py::_sync_moc`) | 1 (`test_sync_manual_updates_moc_backrefs`) | 1 (drives the full `run_sync_manual` flow) | Good for the "new manual MOC" case | Does not cover the "edited manual MOC" case (i.e., `existing` MOC row present, `previous_body` non-`None`, some notes removed) |
| `moc_wikilink` / `moc_link_line` (path-stem branch) | 1 (`test_moc_link_line_uses_path_stem`) | 0 | Good for the branch actually used in production | Only exercises the `path` given / `HUB -` filename case; does **not** cover the no-`path` fallback branch, so the dead-code issue in §10 (Medium risk) has zero direct test coverage in either direction |
| `clear_moc_backrefs` via hub purge (`gardener_hub.purge_hub_pipeline_mocs`) | 0 dedicated in `tests/test_moc_backrefs.py` | Not found | Gap | No test in this file (or found elsewhere via search) exercises the hub-pipeline purge path specifically, even though `gardener_hub.py` imports `clear_moc_backrefs` the same way `gardener.py` does; risk is mitigated by the two call sites being structurally identical, but it is unverified for the hub variant specifically |
| `_link_references_moc` substring-match edge cases | 0 | 0 | Gap | No test constructs two MOC IDs where one is a substring of another, or a backref block with multiple MOC lines where only one should be removed |
| `sync_moc_backrefs` no-op / guard branches (`new_body=None` + missing file, `clear_moc_backrefs` with empty `moc_id`) | 0 | 0 | Gap | The early-return guards described in §3 have no direct test forcing them; they are currently correct by inspection only |

**Test file location**: `tests/test_moc_backrefs.py` (174 lines, 5 test functions). No other test file in the repository references `moc_backrefs`, `sync_moc_backrefs`, `clear_moc_backrefs`, or `MOC_BACKREFS_BLOCK` (verified via project-wide search excluding `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, `.pytest_cache`).

---

*End of report.*
