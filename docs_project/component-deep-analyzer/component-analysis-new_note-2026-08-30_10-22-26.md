# Component Deep Analysis Report: new_note

## 1. Executive Summary

`new_note` (`zettel/new_note.py`) is a single-file, CLI-only component that scaffolds
manually-authored Obsidian vault notes for the four Zettelkasten note families
recognized by the project: Source (`src`), Literature (`lit`, index or granular
chunk), Permanent (`ztl`), and Map of Content (`moc`). It is the write-path counterpart
to `zettel sync-manual` (`zettel/sync.py`): where `sync-manual` *adopts* hand-edited
files already sitting in the vault into SQLite/ChromaDB, `new_note` *creates* those
files in the first place, with correctly-shaped frontmatter, filenames, and body
skeletons, but deliberately does not touch SQLite or ChromaDB itself.

The component is exposed exclusively through the `zettel new-note` Typer subcommand
in `zettel/cli.py` (lines 978-1084) and is not reachable from the web UI (confirmed
in `README.md:474` and `CLAUDE.md`'s "Not exposed in web" list). Its only executable
entry point is the `scaffold_manual_note()` function; everything else in the module is
a private helper feeding that function.

Key findings:
- The component is intentionally "dumb" about persistence: it never imports
  `zettel.state` or `zettel.index`, so a scaffolded note is invisible to search,
  retrieval, or the graph until `zettel sync-manual` is run — this is a deliberate
  architectural boundary, not an oversight (see Design Patterns §9 and Business Rule
  "Manual-only, unindexed scaffolding").
- All four note types funnel through one 185-line dispatch function
  (`scaffold_manual_note`, lines 195-379) with a `note_type`-keyed if/elif chain,
  each branch calling distinct `zettel.vault` builder functions to produce
  (frontmatter, body) tuples.
- Citekey/source_id resolution logic (`provisional_citekey`, `_resolve_citekey`,
  `normalize_source_id`) is shared across the `source`, `literature`, and `permanent`
  branches and is the component's densest piece of business logic.
- Test coverage for the pure-Python scaffolding logic is good (16 tests in
  `tests/test_new_note.py`), but the Typer CLI wrapper itself
  (`cli.py:978-1084`, including its own citekey→source_id aliasing logic) has zero
  dedicated tests — no `tests/test_cli.py` exists in the repository.

## 2. Data Flow Analysis

```
1. User invokes `zettel new-note <type> "<title>" [options]` (Typer CLI)
2. cli.py:new_note() loads AppConfig via _load_deps(config)
3. cli.py calls new_note.normalize_note_type(note_type)
     -> raises ValueError -> CLI prints red error, exit(1) on invalid type
4. cli.py derives `effective_source_id`:
     - if --source-id given, use it
     - elif --citekey given AND type is "permanent" or "source", reuse citekey as source_id
     - else leave unset (literature always derives its own citekey internally)
5. cli.py calls new_note.scaffold_manual_note(cfg, note_type, title, ...many kwargs...)
6. scaffold_manual_note() re-normalizes note_type, branches on it:

   a) "source":
      - resolve citekey (explicit source_id/citekey, or provisional_citekey())
      - compute path = vault_path/10_Sources/<SRC filename>
      - collect non-empty bibliographic fields (_collect_biblio_fields)
      - vault.build_source_note() -> (meta, body)
      - _append_src_ztl_hints() appends a "Referencia para notas permanentes" section
      - _write_scaffold() -> vault.safe_write_note() -> file written to disk

   b) "literature":
      - resolve citekey (always via _resolve_citekey; --source-id from CLI is ignored here)
      - if --granular: compute chunk path under 20_Literature/<Citekey>/,
        vault.build_literature_chunk_note() -> (meta, body) with status="approved"
      - else: compute index path under 20_Literature/, vault.build_literature_index_note()
      - _write_scaffold() -> file written to disk

   c) "permanent" (ztl):
      - generate note_id via ULID()
      - compute path = vault_path/30_Permanent/<ZTL filename>
      - if source_id or citekey given: normalize_source_id(), then
        resolve_src_in_vault() scans 10_Sources/*.md frontmatter for a match
          - found -> exact wikilink to that file's stem
          - not found -> provisional wikilink + warning appended to result.warnings
      - vault.build_permanent_note_body() -> body skeleton
      - body += static "Sugestoes de conexao" placeholder block (auto-connections)
      - _write_scaffold() -> file written to disk

   d) "moc":
      - generate moc_id via ULID()
      - compute path = vault_path/40_MOCs/<MOC filename>
      - static minimal body skeleton (no vault.py builder call)
      - _write_scaffold() -> file written to disk

7. scaffold_manual_note() returns NewNoteResult(path, note_type, meta, warnings)
8. cli.py prints "Nota criada: <path>", any warnings, and a reminder to run
   `zettel sync-manual`
9. (Out of this component's scope) User later runs `zettel sync-manual`, which
   scans the same four vault folders, reads the frontmatter this component wrote
   (`origin: manual`), and only then registers the note in SQLite/ChromaDB.
```

No network calls, no database calls, and no ChromaDB calls occur anywhere in this
component — the only I/O is local filesystem read (glob + frontmatter parse inside
`resolve_src_in_vault`) and write (`safe_write_note`).

## 3. Business Rules & Logic

### Overview of the business rules:

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Validation | Note type must be one of `ztl/permanent/lit/literature/src/source/moc` (case-insensitive) | `new_note.py:49-55` |
| Validation | `source_id`/citekey cannot be empty after stripping `@` | `new_note.py:103-108` |
| Business Logic | Explicit `--citekey`/`--source-id` always wins over derived citekey | `new_note.py:88-96` |
| Business Logic | Provisional citekey derivation has 4 fallback tiers based on available metadata | `new_note.py:58-85` |
| Business Logic | Manual scaffolding never writes to SQLite/ChromaDB — `origin: manual` only | `new_note.py:195-219` (docstring + absence of state/index imports) |
| Business Logic | Existing file at target path blocks the write unless `--force` | `new_note.py:143-147` |
| Business Logic | Source note body always gets an appended "Referencia para notas permanentes" cross-reference block | `new_note.py:177-192, 258-260` |
| Business Logic | Literature notes always derive their own citekey; CLI-level `--source-id` aliasing does not apply to this type | `new_note.py:264-266` vs `cli.py:1044-1047` |
| Business Logic | Granular literature notes are scaffolded with `status="approved"`, bypassing the pipeline's normal `awaiting_review` gate | `new_note.py:282-296` |
| Business Logic | Permanent (ZTL) notes optionally link to an existing SRC note; if unresolved, a provisional wikilink is generated and a warning is surfaced (never a hard failure) | `new_note.py:322-336` |
| Business Logic | Permanent and MOC notes get fresh ULID identifiers, never derived from content | `new_note.py:310, 360` |
| Business Logic | Bibliographic fields are included in frontmatter only when non-empty/non-None | `new_note.py:150-174` |

### Detailed breakdown of the business rules:
---

### Business Rule: Note Type Alias Normalization

**Overview**:
`normalize_note_type()` (`new_note.py:49-55`) maps the six CLI-facing aliases
(`ztl`, `permanent`, `lit`, `literature`, `src`, `source`, `moc`) to four internal
canonical type strings (`permanent`, `literature`, `source`, `moc`) via the
module-level `_NOTE_TYPE_ALIASES` dict (`new_note.py:30-38`). Matching is
case-insensitive and whitespace-tolerant (`raw.strip().lower()`).

**Detailed description**:
This rule exists because the CLI wants short, memorable aliases (`ztl`, `lit`,
`src`, `moc` mirror the vault folder prefixes `30_Permanent`/ZTL, `20_Literature`/LIT,
`10_Sources`/SRC, `40_MOCs`/MOC) while the internal pipeline vocabulary (used
throughout `vault.py`, `state.py`, and `schemas.py`) uses the longer, more explicit
type names `permanent`, `literature`, `source`, `moc`. The alias map is intentionally
a superset — both the short and long forms resolve to the same canonical value — so a
user or another script calling `scaffold_manual_note()` directly can pass either
`"lit"` or `"literature"` interchangeably.

Any string not present as a key raises `ValueError` with a message that lists all
valid aliases sorted alphabetically (`", ".join(sorted(_NOTE_TYPE_ALIASES))`), which
in practice enumerates all seven keys (both short and long forms), not just four —
so a user typo is met with a fairly verbose but complete hint list. This normalization
is called twice per invocation: once in `cli.py:1038` (to fail fast with a friendly
Rich-formatted red error and `typer.Exit(1)` before any other argument processing
happens) and again inside `scaffold_manual_note()` itself (`new_note.py:220`), making
the function callable safely even if a caller bypasses the CLI layer entirely.

**Rule workflow**:
```
raw type string -> strip() -> lower() -> dict lookup in _NOTE_TYPE_ALIASES
  found -> canonical type ("permanent" | "literature" | "source" | "moc")
  not found -> ValueError("Tipo de nota invalido: {raw!r}. Use um de: <sorted aliases>")
```

---

### Business Rule: Citekey Resolution Precedence

**Overview**:
`_resolve_citekey()` (`new_note.py:88-96`) enforces that an explicitly supplied
citekey always takes precedence over one derived from bibliographic metadata; only
in the absence of an explicit citekey is `provisional_citekey()` invoked.

**Detailed description**:
This rule underlies both the `source` and `literature` branches of
`scaffold_manual_note()`. When a citekey is supplied via `--citekey`, it is stripped
of any leading `@` (so both `Silva2024` and `@Silva2024` are accepted identically)
and used verbatim — no validation is performed on its shape (it need not match the
`{Surname}{Year}{Slug}` convention used elsewhere in the harvester pipeline). This is
a deliberate looseness: manual notes are allowed citekeys that don't fit the
auto-generated pattern, since the user is expected to know and control what they're
naming.

When no citekey is supplied, the function falls through to `provisional_citekey()`,
which derives a *best-effort* key from whatever author/year/title information is
available. The word "provisional" in the function's docstring is significant — the
docstring explicitly notes this citekey is derived "without touching SQLite (sync may
refine)", meaning the eventual `zettel sync-manual` step, via
`harvester._generate_citekey()`, may recompute a different/canonical citekey for the
same note if uniqueness collides with existing sources, and will rewrite the
frontmatter and possibly the file path accordingly (see `sync.py:106-121`). The
`new_note` component itself performs no uniqueness check against existing sources in
the vault or SQLite before writing — that check is entirely deferred to
`sync-manual`.

**Rule workflow**:
```
citekey param given? 
  yes -> return citekey.lstrip("@")
  no  -> return provisional_citekey(authors, year, title)
```

---

### Business Rule: Provisional Citekey Derivation (4-Tier Fallback)

**Overview**:
`provisional_citekey()` (`new_note.py:58-85`) synthesizes a citekey from up to three
optional inputs — first author's surname, publication year, and note title — using
four mutually exclusive fallback tiers ordered by information richness.

**Detailed description**:
The function first extracts a surname by taking the last whitespace-separated token
of the first author's name (`parts[-1]` after `.split()`), so `"Maria Silva"` yields
`"Silva"` and a name with no author list yields an empty string. It also strips all
punctuation from the title (`re.sub(r"[^\w\s]", "", title)`) and splits it into
capitalized words for concatenation into a "TitleCase" slug — this mirrors the
`{Surname}{Year}{TitleSlug}` convention used by the harvester's automatic citekey
generator, so manually-created notes look consistent with pipeline-generated ones
when both pieces of information are present.

The four tiers, evaluated in order, are: (1) surname **and** year present — uses only
the first 2 title words (the richest case needs the fewest title words for
uniqueness, e.g. `Silva2024KnowledgeGraphs`); (2) surname only — uses the first 3
title words to compensate for the missing year; (3) year only — uses the first 3
title words prefixed by the year instead of a surname; (4) neither surname nor year —
falls back to the first 4 title words alone, with the final safety net `"Untitled"`
if the title itself produces zero usable words (e.g. an all-punctuation title). Every
tier that would otherwise return an empty concatenation (`"".join(slug_words)`)
guards with `if slug_words else "Untitled"`.

Only tier 1 has direct unit test coverage
(`test_provisional_citekey_author_year`, `tests/test_new_note.py:35-37`); tiers 2-4
are exercised only indirectly through the `source`/`literature` scaffold tests when
authors/year both happen to be supplied, meaning the "surname only", "year only", and
"neither" code paths have no direct assertions in the test suite (see §11).

**Rule workflow**:
```
surname = last token of authors[0], or "" if no authors
words   = title stripped of punctuation, split on whitespace

if surname and year is not None:
    slug = TitleCase(words[:2]) or "Untitled"
    return f"{surname}{year}{slug}"
elif surname:
    slug = TitleCase(words[:3]) or "Untitled"
    return f"{surname}{slug}"
elif year is not None:
    slug = TitleCase(words[:3]) or "Untitled"
    return f"{year}{slug}"
else:
    return TitleCase(words[:4]) or "Untitled"
```

---

### Business Rule: source_id Normalization

**Overview**:
`normalize_source_id()` (`new_note.py:103-108`) canonicalizes any raw citekey or
`@Citekey` string into the strict `@Citekey` form used throughout the vault/SQLite
schema, rejecting empty input.

**Detailed description**:
This function is applied whenever a user-supplied `source_id` (for a `source` note)
or a `source_id`/`citekey` used to link a `permanent` note to a source needs to be
compared against or stored as canonical `source_id` values elsewhere in the system
(SQLite `sources.source_id`, vault frontmatter `source_id` fields). It strips
leading/trailing whitespace and any leading `@` before re-prefixing exactly one `@`,
so `"  @Foo  "`, `"Foo"`, and `"@Foo"` all normalize to `"@Foo"`.

An empty result after stripping (e.g. the raw input was `"@"`, `""`, or only
whitespace) raises `ValueError("source_id/citekey invalido (vazio)")`. This guard
protects both the `source` branch (`source_id` CLI flag) and the `permanent` branch
(`source_id or citekey` combined flag) from silently producing a malformed
`"@"`-only source_id that would corrupt filenames and frontmatter downstream. Notably,
this specific error path has no dedicated unit test in `tests/test_new_note.py` (see
§11 gap).

**Rule workflow**:
```
key = raw.strip().lstrip("@")
if not key: raise ValueError("source_id/citekey invalido (vazio)")
return f"@{key}"
```

---

### Business Rule: Manual-Only, Unindexed Scaffolding (No SQLite/ChromaDB Writes)

**Overview**:
The entire component performs pure filesystem writes and never imports or calls
`zettel.state.StateDB` or `zettel.index.VectorIndex`. Every scaffolded note carries
`origin: "manual"` in its frontmatter, and the CLI explicitly reminds the user to run
`zettel sync-manual` afterward.

**Detailed description**:
This is the component's central architectural business rule and is what allows it to
remain a lightweight, side-effect-free scaffolding tool rather than a full pipeline
stage. Every one of the four note-type branches in `scaffold_manual_note()` sets
`origin` to `"manual"` (explicitly for `source`/`literature` via the `origin="manual"`
keyword forwarded into the `vault.build_*` functions, and via a literal
`"origin": "manual"` dict entry for `permanent` and `moc`). This flag is what
`sync.py:_manual_origin()` and `sync.py:_sync_source()` later use to distinguish
hand-authored notes from pipeline-generated ones (which carry `origin: "pipeline"`)
— see `CLAUDE.md`'s sync.py section: "Each note gets a provenance flag `origin: manual
| pipeline`... allowing distinction of what was hand-written."

The practical consequence is that a note created by `new-note` is completely invisible
to `zettel ask`, `zettel article`, graph expansion, RRF retrieval, and MOC clustering
until `sync-manual` is separately run — there is no chunk indexed in ChromaDB, no row
in `notes`/`sources`/`chunks` tables, and no FTS entry. This is by design (per
`CLAUDE.md`: "does NOT touch SQLite/Chroma") and lets the user free-form edit the
scaffold (fix the provisional citekey, fill in the thesis/definition placeholders,
add wikilinks) in Obsidian before the note becomes searchable/connectable, avoiding
premature/incomplete embeddings.

**Rule workflow**:
```
scaffold_manual_note() writes {meta, body} to disk via safe_write_note()
    meta["origin"] = "manual"  (always, all 4 note types)
    -- no StateDB call, no VectorIndex call --
CLI prints: "Indexe com: zettel sync-manual"
(separate, later command) sync-manual scans vault dirs, reads origin flag,
    registers into SQLite + ChromaDB
```

---

### Business Rule: File Overwrite Protection (`--force` Gate)

**Overview**:
`_write_scaffold()` (`new_note.py:143-147`) refuses to write to a path that already
exists unless the caller explicitly passes `force=True`, protecting hand-edited notes
from silent clobbering.

**Detailed description**:
Every one of the four note-type branches funnels its final write through this single
helper. Before any file content is composed, `path.exists()` is checked; if true and
`force` is not set, a `FileExistsError` is raised carrying the offending path in its
message (`f"Arquivo ja existe: {path}"`), which `cli.py:1073-1075` catches and
re-presents as a red console error plus `typer.Exit(1)`. This prevents the common
accident of re-running `new-note` with the same title/citekey (which deterministically
recomputes the same filename) and silently destroying manually-added content (theses,
definitions, connections) the user had already written into the file.

When `force=True` is passed, the existence check is skipped entirely and
`safe_write_note()` overwrites unconditionally — there is no merge, backup, or
diff-preservation logic; this is a full overwrite, not a managed-block-only update
(contrast with `vault.safe_update_managed_blocks()`, used elsewhere in the pipeline
for non-destructive updates, which this component does not use at all). Both branches
of this rule are covered by tests: `test_scaffold_refuses_existing_file` and
`test_scaffold_force_overwrites` (`tests/test_new_note.py:244-258`).

**Rule workflow**:
```
if path.exists() and not force:
    raise FileExistsError(f"Arquivo ja existe: {path}")
ensure parent directories exist
safe_write_note(path, meta, body)   # full overwrite if force=True
```

---

### Business Rule: Source Note Cross-Reference Appendix

**Overview**:
Every scaffolded Source (`src`) note automatically gets a "Referencia para notas
permanentes" section appended to its body via `_append_src_ztl_hints()`
(`new_note.py:177-192`), regardless of any other flag the user passed.

**Detailed description**:
After `vault.build_source_note()` produces the standard SRC frontmatter and body
(title, authors, year, ABNT reference, literature index link), the `source` branch
unconditionally appends three lines of guidance: the exact `source_id` value to copy
into a future ZTL's frontmatter, a ready-made wikilink to the just-created SRC file
(computed via `source_wikilink(citekey, path=path, title=title)`, which — because
`path` is always non-`None` here — resolves to `f"[[{path.stem}]]"`, the exact file
stem rather than a reconstructed one), and a one-line instruction reminding the user
of the `source_id` + **Fonte** convention used by permanent notes.

This is a usability/consistency rule rather than a data-integrity one: nothing
downstream parses or depends on this appended section (it's outside any managed
block, so `sync-manual` treats it as ordinary manual body content and, per
`sync.py`'s body-wikilink extraction, would even register the embedded wikilink as a
`related` graph edge to itself if left in place — though in practice it links to the
SRC file itself, not to a distinct note). Its sole purpose is to reduce the friction
of manually linking a later `zettel new-note ztl ... -s @Citekey` invocation back to
this source. Test coverage: `test_scaffold_source_note` asserts the section header,
the `source_id` string, and the exact wikilink all appear in the written body
(`tests/test_new_note.py:56-60`).

**Rule workflow**:
```
body = build_source_note(...)  # base SRC body from vault.py
body += "\n## Referencia para notas permanentes\n\n"
body += f"- **source_id** (frontmatter ZTL): `{source_id}`\n"
body += f"- Wikilink desta fonte: [[{path.stem}]]\n"
body += "- Em ZTL, use `source_id` no frontmatter e cite esta nota ou uma LIT em **Fonte**.\n"
```

---

### Business Rule: Literature Type Ignores CLI-Level source_id Aliasing

**Overview**:
Unlike `source` and `permanent`, the `literature` branch of `scaffold_manual_note()`
always derives its own citekey via `_resolve_citekey(citekey, author_list, year,
title)` and never consults the `source_id` parameter for citekey purposes — even
though `cli.py` accepts a `--source-id` flag globally for the command.

**Detailed description**:
At the CLI layer (`cli.py:1043-1047`), the `effective_source_id` computed from
`--citekey` is only forwarded as `source_id` when the normalized type is `"permanent"`
or `"source"`; for `"literature"`, the raw `citekey` parameter is passed through
unchanged and `source_id` remains whatever `--source-id` was explicitly given (or
`None`). Inside `scaffold_manual_note()`'s `literature` branch itself
(`new_note.py:264-266`), the incoming `source_id` parameter is not read at all for
citekey derivation — it's immediately overwritten: `source_id = f"@{ck}"` where `ck`
comes purely from `_resolve_citekey(citekey, ...)`. This means a user cannot use
`--source-id` to force a literature note's citekey the way they can for `permanent`;
only `--citekey` (or the provisional-derivation fallback) controls it for this type.

This is a subtle, implicit special-case that lives partly in `cli.py` and partly in
`new_note.py` — reading either file in isolation does not fully explain the behavior,
which is a minor cross-module coupling/documentation gap (see §10, Technical Debt).
Functionally, it reflects a deliberate rule: literature notes are always scoped to
"the source they belong to," identified by citekey, whereas a permanent note may or
may not be scoped to a source at all, so `source_id` for `permanent` plays a genuinely
different, optional-linkage role that justifies the extra CLI aliasing for that type
only.

**Rule workflow**:
```
literature branch:
    ck = _resolve_citekey(citekey, author_list, year, title)   # source_id param unused here
    source_id = f"@{ck}"                                        # always derived, not passed through
```

---

### Business Rule: Granular Literature Chunks Default to `status="approved"`

**Overview**:
When `--granular` is passed for a `literature` note, `scaffold_manual_note()` calls
`vault.build_literature_chunk_note()` with the literal keyword `status="approved"`
(`new_note.py:294`), bypassing the pipeline's normal `awaiting_review` intermediate
state.

**Detailed description**:
In the automated pipeline (Phase 2 `extractor.py` → Phase 2b `review.py`), every
literature chunk note is created with `status="awaiting_review"` and only promoted to
`approved` through the explicit HITL review workflow (interactive approval, confidence
band gating, or `--auto-approve`/`--yes`). The `new_note` component, in contrast,
marks manually-created granular chunks as already `approved` at creation time — there
is no equivalent "draft" stage for hand-written literature notes, since the human
author is, by definition, the one who has already vetted the content they're typing
directly into the file.

This has a direct downstream consequence for `zettel connect` (Phase 3): concepts
tied to `approved` literature chunks become eligible for connection-building
immediately once `sync-manual` registers them, with no separate review gate. The
`chunk_id`/`literature_id` values are also synthesized with a distinctive
`"manual-{index:04d}"` / `"manual::{index:04d}"` pattern (`new_note.py:268-269`)
rather than the pipeline's content-hash-based IDs, making manually-created chunks
visually distinguishable in the data model. Test coverage:
`test_scaffold_literature_granular` asserts `meta["status"] == "approved"`
(`tests/test_new_note.py:167-169`).

**Rule workflow**:
```
if granular:
    lit_id    = f"{source_id}::manual-{chunk_index:04d}"
    chunk_id  = f"{source_id}::manual::{chunk_index:04d}"
    build_literature_chunk_note(..., status="approved", origin="manual", ...)
```

---

### Business Rule: Permanent Note Source Linkage with Graceful Degradation

**Overview**:
A `permanent` (ZTL) note may optionally be linked to an existing Source via
`--source-id` or `--citekey`; if the referenced SRC file cannot be found in
`10_Sources/`, the scaffold still succeeds, using a provisional (possibly-broken)
wikilink and returning a non-fatal warning instead of raising an error.

**Detailed description**:
`scaffold_manual_note()`'s `permanent` branch (`new_note.py:322-336`) first checks
whether either `source_id` or `citekey` was supplied (`raw_source = source_id or
citekey`); if neither was given, the note is created with no `source_id` frontmatter
key at all (confirmed by `test_scaffold_permanent_note`'s assertion `"source_id" not
in meta`). If one was given, it's normalized via `normalize_source_id()` (raising
`ValueError` only if it normalizes to empty) and stored as `meta["source_id"]`
unconditionally — even if no matching SRC file exists. `resolve_src_in_vault()`
(`new_note.py:111-126`) then performs a best-effort filesystem scan of `10_Sources/*.md`,
parsing each file's frontmatter and comparing `source_id`/`citekey` fields against the
requested identifier (matching on exact `source_id`, `source_id` without `@`, or
`citekey`).

If a match is found, `source_wikilink()` builds an exact wikilink using the found
file's actual stem — guaranteed to resolve correctly in Obsidian. If no match is
found, the function does not fail; instead it builds a *provisional* wikilink purely
from the citekey pattern (`f"[[SRC - {author_year_label(citekey)}]]"`) that will only
resolve once a matching SRC file is later created with that exact name, and appends a
human-readable warning to `result.warnings` instructing the user to run `new-note src`
or `harvest` to create the missing source. This "soft failure with warning" pattern
(rather than raising) reflects the intentional editorial flexibility of manual
note-taking: a user should be able to jot down a permanent-note idea and reference a
source they intend to add later, without the tool blocking them. Both branches are
tested: `test_scaffold_permanent_with_existing_src` (found case, `warnings is None`)
and `test_scaffold_permanent_provisional_src` (not-found case, warning text asserted).

**Rule workflow**:
```
raw_source = source_id or citekey
if raw_source:
    sid = normalize_source_id(raw_source)          # ValueError only if empty
    meta["source_id"] = sid
    src_path, _ = resolve_src_in_vault(cfg, sid)    # scans 10_Sources/*.md frontmatter
    if src_path found:
        source_ref = "[[<exact src file stem>]]"
    else:
        source_ref = "[[SRC - <author_year_label(citekey)>]]"   # provisional
        warnings.append("SRC nao encontrada em 10_Sources/ para {sid}; ...")
else:
    # no source_id key at all in frontmatter
```

---

### Business Rule: Fresh ULID Identity for Permanent Notes and MOCs

**Overview**:
`permanent` and `moc` notes each receive a brand-new `ULID()` as their `note_id`/
`moc_id` at scaffold time (`new_note.py:310, 360`) — never derived from title,
content, or any deterministic hash.

**Detailed description**:
This mirrors the identifier strategy used by the pipeline's own Phase 3/4 note
creation (`connector.py`, `gardener.py`), which also mint fresh ULIDs for permanent
notes and MOCs — ULIDs are lexicographically sortable by creation time and
collision-resistant without any central coordination, which matters because manual
notes and pipeline notes can be created concurrently and must never collide on ID.
Because the ID is random (time-based, but otherwise unrelated to content), re-running
`new-note ztl` with the same title twice (without `--force`) does not raise a
duplicate-ID error — it raises a duplicate-*path* error instead, since the filename
is derived from `note_filename("ZTL", note_id, title)`, which slugifies the title,
not the ID; two different ULIDs with the identical title slug produce two different
filenames (because the ID segment differs) and therefore do NOT collide via
`_write_scaffold`'s existence check — meaning two `new-note ztl "Same Title"` calls
without `--force` will each succeed and produce two separate files with two different
ULIDs, silently creating apparent duplicates. This is a natural consequence of the ID
strategy rather than a guarded rule, and is not covered by any test.

**Rule workflow**:
```
note_id = str(ULID())              # 26-char Crockford base32, time-sortable
path = vault_path / "30_Permanent" / note_filename("ZTL", note_id, title)
                                    # filename embeds full ULID + slug(title)
```

---

## 4. Component Structure

`new_note` is a single Python module (no package/subfolder). Its internal
organization by function, with the vault destination each branch targets:

```
zettel/new_note.py                          # 380 lines — this component
├── _NOTE_TYPE_ALIASES (dict, L30-38)       # ztl/permanent/lit/literature/src/source/moc -> canonical
├── NewNoteResult (dataclass, L41-46)       # path, note_type, meta, warnings — the public return contract
├── normalize_note_type() (L49-55)          # alias -> canonical type, or ValueError
├── provisional_citekey() (L58-85)          # 4-tier citekey derivation from authors/year/title
├── _resolve_citekey() (L88-96)             # explicit citekey wins, else provisional_citekey()
├── _ensure_parent() (L99-100)              # mkdir -p on path.parent
├── normalize_source_id() (L103-108)        # raw -> "@Citekey", or ValueError on empty
├── resolve_src_in_vault() (L111-126)       # scans 10_Sources/*.md frontmatter for a source_id/citekey match
├── source_wikilink() (L129-140)            # builds [[...]] to a SRC note (exact stem, or provisional)
├── _write_scaffold() (L143-147)            # FileExistsError guard + safe_write_note()
├── _collect_biblio_fields() (L150-174)     # filters 8 optional bibliographic kwargs to non-empty dict
├── _append_src_ztl_hints() (L177-192)      # appends cross-reference section to SRC note body
└── scaffold_manual_note() (L195-379)       # main dispatcher — the component's single public entry point
    ├── "source"     branch (L224-262) -> writes to  10_Sources/
    ├── "literature" branch (L264-307) -> writes to  20_Literature/  or  20_Literature/{Citekey}/
    ├── "permanent"  branch (L309-357) -> writes to  30_Permanent/
    └── "moc"        branch (L359-377) -> writes to  40_MOCs/

Related files (outside the component but directly coupled):
zettel/cli.py           # L978-1084: the only caller — Typer command "new-note", arg parsing,
                         #            error presentation, CLI-level source_id/citekey aliasing
zettel/vault.py          # supplies every note-builder function this component calls (frontmatter/body
                         #            construction, filename conventions, safe_write_note, parse_frontmatter)
zettel/config.py         # AppConfig.vault_path — the only config value this component reads
tests/test_new_note.py  # the component's dedicated test suite (16 tests)
```

## 5. Dependency Analysis

```
Internal Dependencies:

cli.py:new_note()
    -> new_note.normalize_note_type()
    -> new_note.scaffold_manual_note()
        -> new_note.normalize_note_type()          (re-validated internally)
        -> new_note._resolve_citekey()
            -> new_note.provisional_citekey()
        -> new_note.normalize_source_id()
        -> new_note.resolve_src_in_vault()
            -> vault.parse_frontmatter()
        -> new_note.source_wikilink()
            -> vault.source_note_stem()
            -> vault.author_year_label()
        -> new_note._collect_biblio_fields()
        -> new_note._append_src_ztl_hints()
            -> new_note.source_wikilink()
        -> new_note._write_scaffold()
            -> new_note._ensure_parent()
            -> vault.safe_write_note()
        -> vault.build_source_note()               ("source" branch)
        -> vault.source_note_filename()             ("source" branch)
        -> vault.build_literature_index_note()      ("literature" branch, non-granular)
        -> vault.literature_index_filename()        ("literature" branch, non-granular)
        -> vault.build_literature_chunk_note()      ("literature" branch, granular)
        -> vault.literature_chunk_filename()        ("literature" branch, granular)
        -> vault.literature_source_dirname()        ("literature" branch, granular)
        -> vault.build_permanent_note_body()        ("permanent" branch)
        -> vault.note_filename()                    ("permanent" + "moc" branches)
        -> ulid.ULID()                              ("permanent" + "moc" branches)
    -> new_note.NewNoteResult                        (return value read by cli.py for console output)

Downstream consumer (not a code dependency, but a data/contract dependency):
    zettel/sync.py:run_sync_manual() reads files this component writes
        (matches on frontmatter `origin: manual`, `source_id`, `citekey`, etc.)
    zettel/moc_backrefs.py is invoked by sync.py after adopting a manual MOC note

External Dependencies:
- ulid-py (`from ulid import ULID`)  - Generates time-sortable unique IDs for permanent/MOC notes
- pathlib (stdlib)                    - All path construction and filesystem existence checks
- re (stdlib)                         - Title-word extraction/punctuation stripping in provisional_citekey
- dataclasses (stdlib)                - NewNoteResult definition
- datetime (stdlib)                   - `created_at`/`updated_at` ISO timestamps
- typing (stdlib)                     - Type hints only (Any)
```

No third-party network SDKs, database drivers, or LLM clients are imported anywhere
in this component — it is pure, synchronous, local-filesystem code.

## 6. Afferent and Efferent Coupling

Unit of analysis: functions/dataclass within `zettel/new_note.py` (this is a
function-oriented module, not a class hierarchy).

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| `scaffold_manual_note` | 1 (cli.py only) | 15 (13 `vault.py` calls + `ULID` + 6 internal helpers across its 4 branches) | High |
| `normalize_note_type` | 3 (cli.py, scaffold_manual_note, + reused by any external caller per docstring) | 0 | Medium |
| `_resolve_citekey` | 2 (scaffold_manual_note "source" + "literature" branches) | 1 (`provisional_citekey`) | Medium |
| `provisional_citekey` | 1 (`_resolve_citekey`) | 0 | Medium |
| `normalize_source_id` | 2 (scaffold_manual_note "source" + "permanent" branches) | 0 | Medium |
| `resolve_src_in_vault` | 1 (scaffold_manual_note "permanent" branch) | 1 (`vault.parse_frontmatter`) | Medium |
| `source_wikilink` | 2 (scaffold_manual_note "permanent" branch, `_append_src_ztl_hints`) | 2 (`vault.source_note_stem`, `vault.author_year_label`) | Low |
| `_write_scaffold` | 4 (all 4 note-type branches) | 2 (`_ensure_parent`, `vault.safe_write_note`) | High |
| `_collect_biblio_fields` | 1 ("source" branch) | 0 | Low |
| `_append_src_ztl_hints` | 1 ("source" branch) | 1 (`source_wikilink`) | Low |
| `_ensure_parent` | 1 (`_write_scaffold`) | 0 | Low |
| `NewNoteResult` | 5 (returned by all 4 branches + consumed by cli.py) | 0 | Medium |

`scaffold_manual_note` and `_write_scaffold` are the highest-risk nodes: the former
because a change to any of its four branches, or to any `vault.py` builder signature
it calls, ripples through the entire component; the latter because every single
write path in the module passes through it, making it the sole gatekeeper of the
overwrite-protection business rule (§3).

## 7. Integration Points

`new_note` has no network, database, or message-queue integrations. Its only
"integration" is with the local Obsidian vault filesystem and, indirectly, with the
CLI framework.

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|--------------|-----------------|
| Vault filesystem (`10_Sources/`, `20_Literature/`, `30_Permanent/`, `40_MOCs/`) | Local filesystem | Write scaffolded note files; read existing SRC frontmatter for source linkage | Direct file I/O (`pathlib`) | Markdown with YAML frontmatter | `FileExistsError` on collision (no `force`); silent skip-and-warn if a SRC lookup finds nothing |
| Typer CLI (`cli.py:new_note`) | In-process function call | Sole entry point exposing this component to users | Direct Python call | Python kwargs | `ValueError`/`FileExistsError` from this component are caught in `cli.py` and converted to Rich console errors + `typer.Exit(1)` |
| `zettel.vault` builder functions | In-process module | Delegates all frontmatter/body construction and filename conventions | Direct Python call | `(dict, str)` tuples | No error handling at this boundary — assumes `vault.py` functions are pure and non-throwing under valid input |
| `zettel sync-manual` (downstream, no code coupling) | Deferred/manual next step | Consumes the `origin: manual` files this component writes and indexes them into SQLite/ChromaDB | N/A (file-based hand-off) | Same Markdown/YAML contract | Not this component's concern — a scaffold with malformed frontmatter would surface as a `sync-manual` failure, not here |

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Alias/Adapter Map | `_NOTE_TYPE_ALIASES` dict + `normalize_note_type()` | `new_note.py:30-55` | Translates short, memorable CLI vocabulary (`ztl`/`lit`/`src`/`moc`) into the pipeline's canonical domain vocabulary (`permanent`/`literature`/`source`/`moc`) |
| Simple Factory / Dispatch-by-Type | `scaffold_manual_note()`'s if/elif chain over `normalized` | `new_note.py:224-379` | Routes to the correct vault-builder + path convention per note family from a single call surface |
| Result / Value Object | `NewNoteResult` dataclass | `new_note.py:41-46` | Encapsulates the outcome (path, type, frontmatter, optional warnings) as one typed return value instead of a tuple or dict, giving `cli.py` a stable, self-documenting contract |
| Guard Clause / Fail-Fast Validation | `normalize_note_type`, `normalize_source_id`, `_write_scaffold` | `new_note.py:49-55, 103-108, 143-147` | Rejects invalid type strings, empty identifiers, and accidental overwrites immediately with descriptive `ValueError`/`FileExistsError`, before any file I/O side effects occur |
| Soft-Fail / Warning Collector | Permanent-note source resolution | `new_note.py:322-336` | Distinguishes hard failures (bad input) from soft, recoverable gaps (an unresolved SRC reference) — returns a usable result plus a `warnings` list rather than aborting |
| Separation of I/O from Content Construction | `scaffold_manual_note()`/`_write_scaffold()` vs. `vault.build_*`/`vault.safe_write_note()` | `new_note.py` calling into `vault.py` | Keeps this component's domain logic (citekey rules, linkage rules, overwrite policy) decoupled from the low-level Markdown/YAML serialization and filesystem write mechanics, which live entirely in `vault.py` |
| Explicit Provenance Tagging | `origin: "manual"` set in every branch | `new_note.py:252, 296, 305, 317, 366` (via kwarg or literal dict entry) | Enables downstream `sync.py` to distinguish hand-authored notes from pipeline-generated ones without any separate tracking table |

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `cli.py:new_note()` (Typer wrapper) | Zero dedicated tests exist for the CLI command itself — no `tests/test_cli.py` in the repository — including its citekey→source_id aliasing logic (`cli.py:1043-1047`), which differs by note type and is not exercised by any test | A regression in the CLI-layer aliasing (e.g. breaking the "permanent"/"source" special-case) would not be caught by the existing test suite, which only calls `scaffold_manual_note()` directly |
| Medium | `provisional_citekey()` | Only the "surname + year" tier (of 4 fallback tiers) has a direct unit test; "surname only", "year only", and "neither" tiers are untested | Silent breakage in 3 of 4 citekey-derivation code paths would go undetected |
| Medium | `source_wikilink()` | The `path is None and title != ""` branch (`new_note.py:138-139`) is never exercised by any test in the suite — every caller either supplies `path` or supplies neither `path` nor `title` | A latent bug in this specific branch would not surface via existing tests |
| Medium | `normalize_source_id()` | The `ValueError` raised on an empty/whitespace-only identifier (`new_note.py:107`) has no direct unit test | An accidental change to the emptiness check would not be caught |
| Medium | `scaffold_manual_note()` | The final fallback `raise ValueError(f"Tipo nao suportado: {normalized}")` (`new_note.py:379`) is unreachable given `normalize_note_type()` already restricts `normalized` to exactly the 4 canonical values earlier in the same function | Dead code path; harmless but adds unreachable-branch noise to coverage reports |
| Medium | `scaffold_manual_note()` | 185-line function handling 4 distinct note-type branches with different parameter subsets (citekey resolution, biblio collection, path construction, body assembly all interleaved) | Any future note type or field addition must be threaded through one large function rather than an isolated unit, increasing the chance of cross-branch regressions |
| Medium | Permanent/MOC identity strategy | ULID-based `note_id`/`moc_id` generation means re-running `new-note ztl "<same title>"` twice without `--force` succeeds both times (different ULID -> different filename), silently producing two near-duplicate permanent notes rather than triggering the `FileExistsError` overwrite-protection rule | No safeguard against accidental duplicate permanent-note creation via repeated identical invocations; not covered by any test |
| Low | `_collect_biblio_fields()` | Of the 8 supported bibliographic fields (`place`, `publisher`, `doi`, `url`, `journal`, `edition`, `institution`, `pages`), only `place`, `publisher`, and `edition` are exercised together in the test suite; `doi`, `url`, `institution`, `pages` are never asserted in any test | Untested fields could silently be dropped or mis-typed without detection |
| Low | Literature type's implicit CLI special-case | The rule that `--source-id` is ignored for citekey derivation on `literature` notes (only `--citekey` applies) is not documented in any docstring in `new_note.py` and is only inferable by reading `cli.py`'s aliasing logic alongside this module | Contributors reading only `new_note.py` could misunderstand the CLI-level contract for this parameter |
| Low | Concurrency (TOCTOU) | `_write_scaffold()`'s `path.exists()` check and the subsequent `safe_write_note()` write are not atomic; two concurrent `new-note` invocations targeting the same computed path could both pass the existence check before either writes | Low real-world likelihood given the CLI's single-user, single-process usage pattern documented elsewhere in the project (web concurrency guard doesn't apply here since `new-note` is CLI-only) |

## 10. Test Coverage Analysis

Test file located at `tests/test_new_note.py` (16 test functions, all using a shared
`cfg` fixture that builds a temporary vault directory tree). No other test file in
the repository (`tests/test_gardener.py`, `tests/test_gardener_hub.py`) exercises
this component — string matches found there are unrelated (`new_notes_list` prompt
placeholder, not this component). No `tests/test_cli.py` exists to cover the Typer
command wrapper.

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|---------------------|----------|----------------|
| `normalize_note_type` | 2 (`test_normalize_note_type_aliases`, `test_normalize_note_type_invalid`) | 0 | Good | Covers all 4 aliased pairs and the invalid-type error message |
| `provisional_citekey` | 1 (`test_provisional_citekey_author_year`) | 0 | Partial | Only the "surname+year" tier is directly tested; the other 3 fallback tiers are untested |
| `scaffold_manual_note` — `source` branch | 4 (`test_scaffold_source_note`, `test_scaffold_source_with_biblio_and_explicit_citekey`, `test_scaffold_source_source_id_flag`, `test_scaffold_refuses_existing_file`/`test_scaffold_force_overwrites` shared) | 1 (`test_scaffold_source_sync_manual_adopts` — exercises `StateDB` + `run_sync_manual` + a fake `VectorIndex`) | Good | Solid frontmatter/body assertions; `doi`/`url`/`institution`/`pages` biblio fields never asserted |
| `scaffold_manual_note` — `literature` branch | 2 (`test_scaffold_literature_index`, `test_scaffold_literature_granular`) | 0 | Good | Covers both index and granular sub-modes, including `status="approved"` and managed-block presence; does not test `chunk_index`/`page` edge cases (e.g. 0 or negative values) |
| `scaffold_manual_note` — `permanent` branch | 4 (`test_scaffold_permanent_note`, `test_scaffold_permanent_with_existing_src`, `test_scaffold_permanent_provisional_src`, `test_scaffold_permanent_source_id_via_citekey`) | 0 | Good | Covers no-source, resolved-source, unresolved-source (warning), and citekey-as-source-id paths well |
| `scaffold_manual_note` — `moc` branch | 1 (`test_scaffold_moc`) | 0 | Minimal | Only the single happy path is tested; no error/edge cases apply to this simple branch, so this is proportionate |
| `_write_scaffold` (overwrite protection) | 2 (`test_scaffold_refuses_existing_file`, `test_scaffold_force_overwrites`) | 0 | Good | Both the block and the `--force` override are explicitly tested |
| `normalize_source_id` | 0 direct | Indirect only, via `source_id` flag tests | Partial | The `ValueError`-on-empty path is never triggered by any test |
| `resolve_src_in_vault` / `source_wikilink` | 0 direct | Indirect only, via the two `permanent`-branch source-linkage tests | Partial | Found/not-found cases are covered; the `source_wikilink(path=None, title=<non-empty>)` branch is never reached by any test |
| `cli.py:new_note()` (Typer command) | 0 | 0 | None | No CLI-level test exists in the repository for this command at all |

