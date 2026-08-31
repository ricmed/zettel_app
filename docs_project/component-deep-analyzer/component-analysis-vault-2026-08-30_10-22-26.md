# Component Deep Analysis Report — `vault`

**Module analyzed**: `zettel/vault.py` (758 lines)
**Analysis date**: 2026-08-30
**Scope note**: Per instructions, only the code module `zettel/vault.py` was analyzed. The actual vault *content* directory (`vault/`) at the project root is a runtime data artifact, not this component's source code, and was excluded from scope.

---

## 1. Executive Summary

`zettel/vault.py` is the sole I/O boundary between the Zettelkasten pipeline (harvest → extract → review → connect → garden) and the physical Obsidian-compatible Markdown vault on disk. It has no knowledge of SQLite, ChromaDB, or LLMs — it is a pure(ish) file-format and file-system layer, and every other component (`harvester.py`, `extractor.py`, `review.py`, `connector.py`, `gardener.py`, `gardener_hub.py`, `moc_backrefs.py`, `sync.py`, `new_note.py`, `purge_source.py`, `rebuild.py`, `ask.py`, `article.py`, `chunk_dump.py`, `extraction_dump.py`, `cli.py`) imports from it.

Its responsibilities fall into four groups:

1. **Frontmatter parsing/rendering** — round-tripping YAML frontmatter + Markdown body (`parse_frontmatter`, `render_frontmatter`, `compose_note`).
2. **Managed-block editing** — a "safe zone" convention (`<!-- zettel:NAME:start -->` … `:end -->`) that lets the pipeline rewrite specific sections of a note (backlinks, connections, MOC backrefs, source excerpts) without touching manually-authored prose elsewhere in the file (`read_managed_block`, `upsert_managed_block`, `safe_update_managed_blocks`).
3. **Filename/wikilink derivation** — deterministic, collision-resistant naming rules for SRC, LIT-index, granular LIT, ZTL, and MOC notes, plus the inverse (`author_year_label`) needed to keep filenames in sync with citekeys generated elsewhere (`harvester._generate_citekey`).
4. **Note builders** — pure functions that assemble the (frontmatter, body) tuple for each note type (`build_source_note`, `build_literature_index_note`, `build_literature_chunk_note`, `build_permanent_note_body`), plus one function that mutates an existing SRC file's cost fields (`sync_source_costs_to_vault`).

**Key findings**:
- The component is almost entirely stateless/pure functions operating on strings and `Path` objects; the only functions that touch the filesystem are `safe_write_note`, `safe_update_managed_blocks`, `init_vault`, and `sync_source_costs_to_vault`.
- `init_vault` is destructive (`shutil.rmtree`) and has **no direct unit test** and no direct caller outside `cli.py`'s `init` command — this is the highest-risk function in the file.
- The "never overwrite manual edits outside managed blocks" guarantee is enforced by construction (`upsert_managed_block` string-splices only inside the tag pair) rather than by any diff/lock mechanism, so it depends entirely on every writer using the managed-block API instead of blind overwrites.
- Filename derivation logic (author-year labels, page tokens, topic slugs) is tightly coupled, by convention rather than by import, to `harvester.py`'s citekey generation — `author_year_label` is documented in its own docstring as "inverse of harvester `_generate_citekey`", a coupling that only a code comment enforces.
- 26 of 33 public/semi-public symbols in the file have direct unit test coverage in `tests/test_vault.py`; `init_vault`, `sync_source_costs_to_vault`, `strip_matching_wikilinks`, and `_wikilink_target_matches` do not (see §11).

---

## 2. Data Flow Analysis

`vault.py` has no single "request", since it is a library of pure functions called from many places. Below are the representative data flows through it, per calling phase.

**Flow A — Harvest: creating a new source (harvester.py)**
```
1. harvester.py computes citekey, title, authors, checksum, paging metadata
2. vault.build_source_note()        -> (SRC frontmatter dict, SRC body str)
3. vault.build_literature_index_note() -> (LIT-index frontmatter dict, LIT-index body str)
4. vault.safe_write_note()          -> writes both files under 10_Sources/ and 20_Literature/
5. vault.compose_note()             -> re-serializes the LIT-index text for db.update_source_texts()
```

**Flow B — Extract: writing a draft literature chunk note (extractor.py)**
```
1. extractor.py runs LLM Prompt 1 on a pending chunk, gets summary/key_concepts/candidates
2. vault.literature_chunk_filename_for_row() -> deterministic draft filename
3. vault.build_literature_chunk_note()       -> (frontmatter, body) with embedded source excerpt
4. vault.safe_write_note()                   -> writes under 00_Inbox/Review/{Citekey}/
5. vault.sync_source_costs_to_vault()        -> patches SRC frontmatter cost/token fields in place
```

**Flow C — Review: approving/rejecting a draft (review.py)**
```
1. review.py reads the draft file, vault.parse_frontmatter() splits meta/body
2. Approve path: vault.safe_write_note() moves content to 20_Literature/{Citekey}/
3. vault.literature_chunk_wikilink_for_row() builds the index entry link
4. vault.build_literature_index_note() rebuilds the index body with the new link list
   OR vault.safe_update_managed_blocks() patches only the auto-lit-index block
5. vault.compose_note() re-serializes for db.update_source_texts()
```

**Flow D — Connect: writing a permanent note and updating backlinks (connector.py)**
```
1. connector.py resolves RAG context, calls Prompt 2, gets thesis/definition/intuition/etc.
2. vault.build_permanent_note_body()   -> ZTL body text (no frontmatter — connector builds meta itself)
3. vault.note_filename("ZTL", ...)     -> filename
4. vault.safe_write_note()             -> writes under 30_Permanent/
5. vault.permanent_wikilink()          -> builds [[...]] links for connections/backlinks
6. vault.read_managed_block() / vault.safe_update_managed_blocks() -> patches auto-backlinks block
   on the *other* note being linked to
7. vault.sync_source_costs_to_vault()  -> patches SRC cost fields
```

**Flow E — Garden: MOC generation + backref sync (gardener.py, gardener_hub.py, moc_backrefs.py)**
```
1. gardener.py clusters notes, calls LLM for MOC topic/body
2. vault.note_filename("MOC"/"HUB", ...) -> filename
3. vault.safe_write_note()               -> writes under 40_MOCs/
4. vault.parse_frontmatter()              -> re-reads previous MOC body for diffing (incremental mode)
5. moc_backrefs.sync_moc_backrefs() calls vault.read_managed_block()/safe_update_managed_blocks()
   on every permanent note gained/lost by the MOC, writing the auto-moc-backrefs block
```

**Flow F — Delete: purging a source (purge_source.py)**
```
1. purge_source.py enumerates vault files potentially linking to the deleted source
2. vault.parse_frontmatter() splits each candidate note
3. vault.strip_matching_wikilinks() removes dead [[wikilinks]] from the body
4. vault.compose_note() re-serializes and the file is rewritten in place
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Naming | SRC/LIT filenames use `{Author}{Year}` label, never the full citekey or `@` | vault.py:208-236 |
| Naming | Granular LIT filename encodes page token + topic slug + zero-padded chunk index for uniqueness | vault.py:291-304 |
| Naming | Page token prefers printed/book page (`pNNN`); falls back to chunk-index token (`cNNNN`) when no page is known | vault.py:244-252 |
| Naming | Topic slug prefers the last segment of `section_path`; falls back to first 8 words of the LLM summary; falls back to literal `"nota"` | vault.py:255-289 |
| Naming | Generic section labels ("documento completo", empty string) are treated as "no topic" and skipped | vault.py:196, 255-261 |
| Linking | Granular LIT wikilinks are always path-qualified (`Citekey/LIT - ...`) to avoid stem collisions across sources | vault.py:331-354 |
| Linking | Wikilink stripping matches both the full path-qualified target and the bare filename stem | vault.py:50-55 |
| Linking | Removing a dead wikilink leaves a placeholder text on an otherwise-emptied "Ref. literatura:" bullet instead of a blank line | vault.py:76-84 |
| Data integrity | `parse_frontmatter` never raises on malformed YAML — it silently degrades to an empty metadata dict | vault.py:28-31 |
| Data integrity | A note with `content.startswith("---")` but fewer than 3 `---`-delimited parts is treated as having no frontmatter at all | vault.py:23-27 |
| Data integrity | `upsert_managed_block` only ever rewrites text strictly between its own start/end tags; everything else in the file is byte-for-byte preserved | vault.py:111-128 |
| Data integrity | `safe_update_managed_blocks` is a no-op (skips the disk write) when the new block content produces no textual change | vault.py:156-157 |
| Data integrity | `updated_at` in frontmatter is bumped to "now" only when the file content actually changed, and only if the note already has frontmatter | vault.py:159-163 |
| Data integrity | `safe_update_managed_blocks` silently returns (logs a warning, does nothing) if the target path does not exist | vault.py:148-150 |
| Destructive operation | `init_vault` unconditionally deletes the entire vault directory tree before recreating the fixed folder structure | vault.py:181-192 |
| Cost accounting | `sync_source_costs_to_vault` falls back to scanning every `SRC - *.md` file for a matching `source_id` in frontmatter if the filename-derived path does not exist | vault.py:530-544 |
| Cost accounting | All cost figures written to frontmatter are rounded to 6 decimal places; token counts are coerced to `int` | vault.py:547-552 |
| Content composition | Optional SRC frontmatter/body fields (document_type, biblio fields, paging, cost/tokens) are omitted entirely when `None`/empty rather than written as null/blank | vault.py:447-489 |
| Content composition | Bibliographic fields (`biblio_fields`) skip `None`, empty list, and blank/whitespace-only string values | vault.py:449-457 |
| Content composition | `build_permanent_note_body` renders a `RelationType` enum's `.value` rather than its `str()` form, to avoid literal `"RelationType.X"` leaking into notes | vault.py:722-729 |
| Content composition | Sections with no content (`intuition`, `example`, `limits`, `images`, `connections`) are omitted from the ZTL body rather than rendered empty | vault.py:702-721 |
| Filename safety | `_slug` strips all non-word/non-space/non-hyphen characters, collapses whitespace/underscores to hyphens, lowercases, and truncates (default 100 chars, 40 for chunk topics), trimming a trailing hyphen | vault.py:200-205 |

### Detailed breakdown of the business rules

---

### Business Rule: Author-Year Label Derivation

**Overview**:
All vault-facing filenames for sources (SRC and LIT-index notes) use a short `{Surname}{Year}` label rather than the full citekey, which typically also carries a title slug (e.g. `Negro2026KnowledgeGraphs`).

**Detailed description**:
`author_year_label()` strips a leading `@` (the CLI/ID convention for citekeys) and applies the regex `^([A-Za-z]+\d{4})` to capture a leading run of letters immediately followed by exactly four digits. If the citekey matches (e.g. `Negro2026KnowledgeGraphs` → `Negro2026`), only that prefix is used in file and link names; anything after the year (the title slug added by the harvester for uniqueness) is dropped from the vault-facing name. This keeps `SRC - Negro2026 - knowledge-graphs-and-llms-in-action.md` short and human-scannable, since the title itself already appears in the slug portion of the filename.

If the citekey does not match the pattern — no digits, digits not exactly 4, or digits not immediately following the letters — the function returns the citekey unchanged (after `@`-stripping). This covers both truly malformed citekeys and legitimately short ones like `Book2024` or `UntitledOnly` (the latter has no year at all and is returned verbatim). There is no validation or error raised for a citekey that doesn't fit either case; the function degrades gracefully by using the whole string as the label.

This function is explicitly documented as the *inverse* of `harvester.py`'s `_generate_citekey`, meaning correctness depends on the two functions' conventions staying in sync — a coupling enforced only by a docstring comment and by the shared test suite (`test_author_year_label` in `tests/test_vault.py`), not by any shared constant or schema. If the harvester's citekey format ever changes shape, this regex must be updated in lockstep or filenames will silently include unwanted trailing text.

**Rule workflow**:
```
citekey (possibly "@"-prefixed)
  → strip leading "@"
  → regex match ^([A-Za-z]+\d{4})
      match found  → return matched group (surname+year)
      no match     → return the stripped citekey unchanged
```

---

### Business Rule: Granular Literature Chunk Filename Uniqueness

**Overview**:
Each granular literature note (one per processed chunk) must have a filename that is both human-readable (shows page and topic) and guaranteed unique within a source, even when multiple chunks share the same page or the same section heading.

**Detailed description**:
`literature_chunk_filename()` composes four parts into `LIT - {label} - {page} - {topic}-{chunk_index:04d}.md`. The **page token** (`_page_token`) prefers `page_in_book` (the printed/logical page number, computed by the harvester as `file_page - content_start_file + content_start_book`) over `page_in_file` (the raw PDF page index), and only falls back to a chunk-index-based token (`c0007`) when neither page number is available — this happens for native Markdown sources that have no PDF pagination at all. The **topic slug** (`_topic_slug`) prefers the last breadcrumb segment of `section_path` (e.g. `"Cap 2 > Sistema 1 > Intuição"` → `"intuicao"`), but treats generic/non-informative segments — literally `"documento completo"` or an empty string, defined in `_GENERIC_SECTION_TOPICS` — as no topic at all, in which case it falls back to slugifying the first portion of the LLM-generated summary, and finally falls back to the literal string `"nota"` if no summary is available either.

Critically, the chunk index is *always* appended as a 4-digit zero-padded suffix regardless of whether the page/topic combination is already unique. This is a deliberate defensive design: two chunks on the same page under the same section heading (e.g. a long section split by the LangChain splitter into multiple chunks) would otherwise produce identical filenames and silently overwrite one another on disk. Because chunk indices are assigned sequentially and never reused for a given source, appending them guarantees filename uniqueness even in the worst case where page, section, and summary all collide.

This same filename (not a copy) is reused unchanged between the draft location (`00_Inbox/Review/{Citekey}/`) and the approved location (`20_Literature/{Citekey}/`) — `build_literature_chunk_note`'s caller in `extractor.py` and `review.py` must derive the identical filename via `literature_chunk_filename_for_row` so that "approving" a draft is a file *move*, not a rename, and any external references created during the draft phase remain valid after approval.

**Rule workflow**:
```
chunk row (page_in_book, page_in_file, section_path, summary, chunk_index)
  → page_token = page_in_book if set, else page_in_file if set, else f"c{chunk_index:04d}"
  → topic:
       section_path last segment, if present and not generic → slugify (max 40 chars)
       else summary first ~8 words → slugify (max 40 chars)
       else "nota"
  → filename = f"LIT - {author_year_label} - {page_token} - {topic}-{chunk_index:04d}.md"
```

---

### Business Rule: Managed Block Isolation (Manual-Edit Safety)

**Overview**:
The pipeline must be able to programmatically refresh derived sections of a note (backlinks, connections, MOC backrefs, lit-index entries, source excerpts) on every run without ever destroying content a human added elsewhere in that same file.

**Detailed description**:
Every "auto"-managed section in a note is wrapped in an HTML-comment pair `<!-- zettel:{name}:start -->` … `<!-- zettel:{name}:end -->`. `upsert_managed_block()` finds the block by exact tag substring match: if the start tag is absent, it *appends* a new block to the end of the file (ensuring a trailing newline first); if the start tag exists but the end tag is missing (a corrupted/manually-truncated block), it also appends a fresh block rather than attempting a risky partial repair; if both tags are found, it replaces exactly the substring between them, leaving everything before the start tag and everything after the end tag untouched, byte for byte. This means a user can freely edit prose, add their own headings, or add manual wikilinks anywhere in the file outside the tagged region and the pipeline will never revert or duplicate that content on a subsequent write.

`safe_update_managed_blocks()` layers three more guarantees on top of `upsert_managed_block`: (1) it refuses to operate on a path that doesn't exist, logging a warning instead of raising, so a deleted or moved note doesn't crash a batch backref-sync job; (2) it computes the full new content in memory first and compares it byte-for-byte against the original — if nothing changed (the common case when re-running `garden` or `sync` produces identical suggestions), it performs **no disk write at all**, which both avoids unnecessary filesystem churn and avoids bumping `updated_at` for a no-op; (3) only when content did change does it re-parse the frontmatter and set `updated_at` to the current timestamp — but only if the note *has* frontmatter (`if meta:`), so a manually-created stub note without YAML front matter is patched in the body only, without acquiring frontmatter it never had.

This idempotency check has a direct behavioral consequence tested explicitly in `test_safe_update_managed_blocks_idempotent_keeps_updated_at`: calling the same update twice with identical content leaves `updated_at` at its original value, which downstream consumers (e.g. anything sorting notes by recency) rely on to distinguish "genuinely edited" from "recomputed with the same result."

**Rule workflow**:
```
safe_update_managed_blocks(path, {block_name: new_inner, ...}):
  if not path.exists(): warn and return
  original = read(path)
  content = original
  for each (block_name, inner): content = upsert_managed_block(content, block_name, inner)
  if content == original: return   # no-op, no write, no updated_at bump
  meta, body = parse_frontmatter(content)
  if meta: meta["updated_at"] = now(); content = compose_note(meta, body)
  write(path, content)
```

---

### Business Rule: Frontmatter Parsing Fault Tolerance

**Overview**:
Every note in the vault may, in principle, be hand-edited or produced by an external tool, so frontmatter parsing must never crash the pipeline on malformed YAML or an atypical file layout.

**Detailed description**:
`parse_frontmatter()` first checks the cheapest possible precondition: does the content start with the literal string `"---"`? If not, the file is treated as having no frontmatter whatsoever, and the entire content is returned as the body with an empty metadata dict — this covers plain Markdown files dropped into the vault by hand. If the file does start with `---`, the content is split on `"---"` into at most 3 parts (`str.split("---", 2)`); if fewer than 3 parts result (i.e., there's no closing `---` delimiter at all), the file is again treated as having no frontmatter, protecting against a note where the author started typing a horizontal rule or frontmatter block but never closed it.

When a closing delimiter *is* found, the YAML between the two markers is parsed with `yaml.safe_load` inside a `try/except yaml.YAMLError` — a syntax error in the YAML (bad indentation, unescaped colon, etc.) degrades to an empty metadata dict rather than raising, so one broken note cannot abort a whole `sync`/`garden` batch run. Note that this exception handling only catches `yaml.YAMLError`; a `yaml.safe_load` result that is a non-dict scalar (e.g. frontmatter content that's just a bare string) would pass through as `meta = <that value> or {}`, which could produce a non-dict `meta` for callers that assume `.get()` works — this is an implicit assumption on well-formed frontmatter shape that is not itself validated here.

The body is returned with leading newlines stripped (`.lstrip("\n")`) so that downstream `compose_note()` round-trips predictably regardless of how many blank lines separated the frontmatter's closing `---` from the first line of content in the original file.

**Rule workflow**:
```
parse_frontmatter(content):
  if not content.startswith("---"): return ({}, content)
  parts = content.split("---", 2)
  if len(parts) < 3: return ({}, content)
  try: meta = yaml.safe_load(parts[1]) or {}
  except yaml.YAMLError: meta = {}
  body = parts[2].lstrip("\n")
  return (meta, body)
```

---

### Business Rule: Wikilink Removal on Source Deletion

**Overview**:
When a source is permanently purged (`zettel delete-source`), every dangling `[[wikilink]]` to that source's literature notes (index or granular) must be scrubbed from the rest of the vault, without leaving orphaned Markdown list artifacts.

**Detailed description**:
`strip_matching_wikilinks()` accepts a set of "link targets" — bare filename stems and/or path-qualified targets (`Citekey/LIT - ...`) — and removes every `[[...]]` occurrence whose target resolves to one of them. Matching is deliberately lenient: `_wikilink_target_matches()` first normalizes backslashes to forward slashes and strips whitespace, then checks for an exact match against the target set; if that fails, it also checks whether just the *last path segment* (the bare filename, ignoring any `Citekey/` prefix) is in the set. This dual check exists because a caller may know the source's granular notes only by bare stem or may know them fully path-qualified, and links elsewhere in the vault may have been written in either form (aliased links `[[target|alias]]` are handled by the regex's non-greedy target-only capture group, which ignores the alias when checking whether to strip).

After the regex substitution empties out matched wikilinks in place, `strip_matching_wikilinks` performs a second, line-oriented cleanup pass: any line that, after stripping, is exactly a bare bullet artifact (`"-"`, `"- ()"`, `"←"`, `"← "` — the leftover shape when the *only* content of a backlink or "Ref." bullet was the now-removed link) is dropped entirely rather than left as visual clutter. A special case exists for the ZTL note's "Fonte" section format specifically: a line matching `^-\s*Ref\. literatura:\s*$` (a "Ref. literatura:" label with nothing after it, because the value was a stripped wikilink) is *not* deleted but rewritten to append the placeholder text `"Ref. literatura: _fonte removida_"`, so the note retains a visible, honest record that its literature reference existed and was deliberately removed rather than silently vanishing.

This function is invoked only by `purge_source.py`'s `_clean_note_file`, which re-derives `updated_at` and rewrites the file only if either the body changed or the note's `source_id` frontmatter field pointed at the deleted source (in which case `source_id` is also popped from frontmatter) — `strip_matching_wikilinks` itself has no knowledge of frontmatter or the database; it is a pure text transform.

**Rule workflow**:
```
strip_matching_wikilinks(text, link_targets):
  if no link_targets or empty text: return text unchanged
  regex-replace every [[target]] or [[target|alias]] whose target
    (normalized, or its last path segment) is in link_targets  → ""
  for each resulting line:
    if stripped line in {"-", "- ()", "←", "← "}: drop the line
    elif line matches "^-\s*Ref\. literatura:\s*$":
        rewrite to "- Ref. literatura: _fonte removida_"
    else: keep line (right-trimmed)
  rejoin and return
```

---

### Business Rule: Destructive Vault Initialization

**Overview**:
`init_vault()` (backing `zettel init`) provisions the fixed top-level Obsidian folder structure the rest of the pipeline assumes exists, but does so by first deleting anything already at that path.

**Detailed description**:
If the target `vault_path` already exists — as a file, an empty directory, or a fully populated vault with years of notes — `init_vault` calls `shutil.rmtree(vault_path)` unconditionally before recreating the seven fixed directories in `VAULT_DIRS` (`00_Inbox`, `00_Inbox/Review`, `10_Sources`, `20_Literature`, `30_Permanent`, `40_MOCs`, `90_Assets`). There is no confirmation prompt, dry-run flag, backup step, or check for existing content inside this function; the entire safety burden falls on the caller (`cli.py`'s `init` command) to gate this behind an explicit user action.

This function has no automated test coverage in `tests/test_vault.py` or elsewhere in the suite (confirmed by the absence of any `init_vault` reference in `tests/`), which for a `shutil.rmtree`-driven function is a materially higher risk than for the file's other, purely additive/text-transform functions — a regression here (e.g. a future edit that changes `vault_path` resolution or introduces a path-traversal bug) would not be caught by the existing suite and would manifest as vault-destroying behavior in production. See Technical Debt §10 and Test Coverage §11.

**Rule workflow**:
```
init_vault(vault_path):
  if vault_path.exists(): shutil.rmtree(vault_path)   # unconditional, no confirmation
  for each dir in VAULT_DIRS: mkdir(vault_path/dir, parents=True, exist_ok=True)
```

---

### Business Rule: Cost/Token Synchronization Fallback Lookup

**Overview**:
After an LLM or embedding call attributable to a source, `sync_source_costs_to_vault()` mirrors the running cost/token totals from SQLite onto that source's SRC note frontmatter, so a human browsing the vault sees up-to-date spend without querying the database.

**Detailed description**:
The function first attempts the cheap path: derive the expected SRC filename from `citekey`/`title` via `source_note_filename()` and check whether a file exists at that exact computed path under `10_Sources/`. This works whenever the citekey and title recorded in SQLite still agree with what was used to originally name the file. If that direct lookup misses — which can legitimately happen if a title was corrected after harvest, or the note was manually renamed — the function falls back to an O(n) scan: it globs every `SRC - *.md` file in the sources directory, parses each one's frontmatter, and looks for the file whose `source_id` field matches. This fallback is a deliberate resilience measure trading performance for correctness in an operation that runs per LLM call inside `connector.py` and `extractor.py`, on a directory that is expected to stay in the hundreds-of-files range rather than a scale where a linear scan would be prohibitive.

If both the fast path and the fallback scan fail to locate a file — a genuinely orphaned or deleted SRC note — the function returns `False` without raising, and the caller (`connector.py`/`extractor.py`) is expected to treat cost sync as best-effort rather than a hard requirement of the pipeline; a failed sync does not lose the underlying cost data, which remains authoritative in SQLite (`sources.cost_usd_total` etc.) — this function is purely a display/mirroring convenience.

When a match is found, six fields are written: three cost figures rounded to 6 decimal places (`cost_usd_total`, `cost_usd_llm`, `cost_usd_embedding`) and three token counts coerced to `int` (`tokens_prompt`, `tokens_completion`, `tokens_embedding`), all defaulting to `0` via `row.get(...) or 0` if the corresponding SQLite column is `None`. `updated_at` is unconditionally bumped on every successful sync, in contrast to `safe_update_managed_blocks`'s idempotency check — there is no content-diff short-circuit here, so calling this function twice with identical costs still rewrites the file and bumps its timestamp both times.

**Rule workflow**:
```
sync_source_costs_to_vault(cfg, db, source_id):
  row = db.get_source(source_id); if not row: return False
  citekey = row.citekey or source_id without "@"
  path = 10_Sources / source_note_filename(citekey, row.title)
  if not path.exists():
      scan all "SRC - *.md" under 10_Sources for meta.source_id == source_id
      if none found: return False
  meta, body = parse_frontmatter(path.read_text())
  meta[cost_usd_total/llm/embedding] = round(row value or 0, 6)
  meta[tokens_prompt/completion/embedding] = int(row value or 0)
  meta.updated_at = now()
  write path <- render_frontmatter(meta) + body
  return True
```

---

### Business Rule: Optional-Field Frontmatter Omission

**Overview**:
Note builders (`build_source_note`, `build_literature_chunk_note`) accept many optional parameters (paging confidence, document type, per-note LLM metadata, cost figures) that are only known at certain pipeline stages; frontmatter must not accumulate `null`/empty placeholders for data that was never computed.

**Detailed description**:
Rather than always writing every possible frontmatter key with a `None`/default sentinel, `build_source_note` uses a long sequence of `if value is not None:` (or, for strings, `if value:`) guards before adding each optional key to the `meta` dict. This keeps a source harvested without page-offset detection, for instance, free of `page_offset: null` clutter, and keeps a source's frontmatter shape a truthful reflection of what pipeline stages have actually run on it (a `zettel status` or manual inspection can tell, from the *presence* of `cost_usd_total`, whether cost tracking has ever populated that source, without needing a sentinel value convention).

The same pattern extends into `biblio_fields`, a caller-supplied dict of bibliographic metadata (from `bibliography.py`): each value is individually checked and skipped if it is `None`, an empty list, or a blank/whitespace-only string, before being merged into `meta`. This is stricter than the top-level optional-parameter checks because `biblio_fields` values arrive from LLM-driven bibliographic extraction, which is more likely to produce structurally-present-but-semantically-empty values (e.g. an empty `[]` for a "series" field that doesn't apply) than the deterministic, code-computed parameters elsewhere in the function.

The same philosophy governs `build_permanent_note_body`'s body sections: `intuition`, `example`, `limits`, `images`, and `connections` are each wrapped in `if <value>:` before their corresponding Markdown heading is emitted at all, so a permanent note whose LLM output omitted an "Intuição" section (Prompt 2 may not always produce one) doesn't get a stray empty `## Intuição` heading with nothing beneath it.

**Rule workflow**:
```
for each optional field in build_source_note / build_literature_chunk_note:
  if field is not None (numeric/enum) OR field is truthy (string/list):
      meta[key] = field   # possibly transformed (round, int cast)
  else:
      key omitted entirely from meta

for each optional body section in build_permanent_note_body:
  if section content is truthy: append "## Heading\n\n{content}\n"
  else: section omitted from body entirely
```

---

## 4. Component Structure

`vault.py` is a single flat module (no sub-package) organized into five clearly delimited sections via comment banners:

```
zettel/vault.py                          # 758 lines, single module, no classes
├── Frontmatter (lines 18–45)
│   ├── parse_frontmatter()              # YAML+body split, fault-tolerant
│   ├── render_frontmatter()             # dict -> "---\n...\n---\n"
│   └── compose_note()                   # frontmatter + body -> full document
├── Wikilink helpers (lines 47–85)
│   ├── _WIKILINK_RE                     # compiled regex for [[target|alias]]
│   ├── _wikilink_target_matches()       # exact or basename match
│   └── strip_matching_wikilinks()       # remove dead links + cleanup bullets
├── Managed Blocks (lines 88–128)
│   ├── _block_pattern()                 # start/end HTML-comment tag pair
│   ├── read_managed_block()             # extract inner text or None
│   └── upsert_managed_block()           # insert-or-replace, preserves rest
├── Safe File I/O (lines 131–192)
│   ├── safe_write_note()                # full-file write, mkdir -p
│   ├── safe_update_managed_blocks()     # patch blocks only, bump updated_at
│   ├── VAULT_DIRS                       # fixed 7-folder vault layout
│   └── init_vault()                     # DESTRUCTIVE: rmtree + recreate
├── Filename / wikilink derivation (lines 195–393)
│   ├── _slug()                          # generic text -> URL/filename-safe slug
│   ├── author_year_label()              # citekey -> "{Surname}{Year}"
│   ├── literature_source_dirname()
│   ├── source_note_filename() / source_note_stem()
│   ├── literature_index_filename() / literature_index_stem()
│   ├── _page_token() / _section_topic() / literature_chunk_topic() / _topic_slug()
│   ├── literature_chunk_filename() / literature_chunk_filename_for_row()
│   ├── literature_chunk_wikilink() / literature_chunk_wikilink_for_row()
│   ├── literature_index_link_label()
│   └── _summary_from_chunk()            # JSON/dict-safe summary extraction
└── Note Builders (lines 395–758)
    ├── build_source_note()              # SRC frontmatter + body
    ├── sync_source_costs_to_vault()     # patches an existing SRC's cost fields
    ├── build_literature_index_note()    # LIT-index frontmatter + body
    ├── build_literature_chunk_note()    # granular LIT frontmatter + body
    ├── build_permanent_note_body()      # ZTL body only (caller builds meta)
    ├── note_filename()                  # generic "PREFIX - ID - slug.md"
    └── permanent_wikilink()             # ZTL wikilink, path-stem-aware
```

No `__init__.py` re-export or package boundary exists — all consumers import directly from `zettel.vault` (or, in `ask.py`/`article.py`, via the relative `.vault` form). There are no classes; every symbol is a module-level function or constant, and the module holds no mutable global state beyond the `logger`.

---

## 5. Dependency Analysis

**Internal Dependencies** (within `zettel/vault.py` itself — call graph among its own functions):
```
build_source_note            -> literature_index_stem -> author_year_label
source_note_filename         -> note_filename -> _slug
source_note_stem             -> source_note_filename
literature_index_filename    -> author_year_label, _slug
literature_index_stem        -> literature_index_filename
literature_chunk_filename    -> author_year_label, _page_token, _topic_slug
_topic_slug                  -> _section_topic, _slug
literature_chunk_topic       -> _section_topic
literature_chunk_filename_for_row -> literature_chunk_filename, _summary_from_chunk
literature_chunk_wikilink    -> literature_source_dirname, literature_chunk_filename
literature_chunk_wikilink_for_row -> literature_chunk_wikilink, literature_index_link_label, _summary_from_chunk
literature_index_link_label  -> literature_chunk_topic
build_literature_index_note  -> source_note_stem
build_literature_chunk_note  -> literature_index_stem, literature_chunk_topic
sync_source_costs_to_vault   -> source_note_filename, parse_frontmatter, render_frontmatter
safe_write_note              -> compose_note
safe_update_managed_blocks   -> upsert_managed_block, parse_frontmatter, compose_note
upsert_managed_block         -> _block_pattern
read_managed_block           -> _block_pattern
strip_matching_wikilinks     -> _wikilink_target_matches
permanent_wikilink           -> _slug
```

**External Dependencies** (third-party / stdlib):
```
- PyYAML (yaml)         - YAML frontmatter parse/dump — safe_load only, no arbitrary object loading
- pathlib.Path (stdlib) - all filesystem path handling
- shutil (stdlib)       - rmtree in init_vault (destructive)
- datetime (stdlib)     - updated_at / created_at ISO-8601 timestamps
- re (stdlib)           - wikilink regex, author-year regex, slugification
- json (stdlib)         - defensive parsing of summary_json in _summary_from_chunk
- logging (stdlib)      - module-level logger, debug/warning/info calls
```

`vault.py` imports **nothing** from any other `zettel.*` module — it is a leaf dependency with respect to the rest of the codebase (confirmed: no `from zettel import` or `from .` other-module import appears in the file). This makes it safe to unit-test in complete isolation, which the existing `tests/test_vault.py` does (no mocking of collaborators is required).

---

## 6. Afferent and Efferent Coupling

Since `vault.py` is function-based (no classes), coupling is measured at the function/module level: **afferent coupling (Ca)** = number of distinct external call sites (across the rest of `zettel/`) that import/call the symbol; **efferent coupling (Ce)** = number of other symbols *within this module* that the symbol calls.

| Component (function/symbol) | Afferent Coupling (external call sites) | Efferent Coupling (intra-module calls) | Critical |
|---|---|---|---|
| `parse_frontmatter` | 9 modules (gardener, harvester, moc_backrefs, new_note, purge_source, review, sync, connector-indirect, ask/article via render_frontmatter pair) | 0 | High |
| `compose_note` | 7 modules (chunk_dump, extraction_dump, harvester, purge_source, rebuild, review, sync) | 1 (render_frontmatter, via string concat — not a call but conceptual pairing) | High |
| `safe_write_note` | 8 modules (connector, extractor, gardener, gardener_hub, harvester, new_note, review — 3 call sites, rebuild uses compose_note instead) | 1 (compose_note) | High |
| `safe_update_managed_blocks` | 5 modules (connector, moc_backrefs, rebuild, review, sync) | 3 (upsert_managed_block, parse_frontmatter, compose_note) | High |
| `note_filename` | 5 modules (connector, gardener, gardener_hub, moc_backrefs, new_note, rebuild — 6 call sites) | 1 (_slug) | Medium |
| `permanent_wikilink` | 6 modules (article, ask, connector, gardener, gardener_hub, purge_source, sync) | 1 (_slug) | Medium |
| `read_managed_block` | 2 modules (connector, moc_backrefs) | 1 (_block_pattern) | Medium |
| `build_source_note` | 3 modules (harvester, new_note, rebuild) | 1 (literature_index_stem) | Medium |
| `build_literature_index_note` | 3 modules (harvester, new_note, review) | 1 (source_note_stem) | Medium |
| `build_permanent_note_body` | 2 modules (connector, new_note) | 0 | Medium |
| `sync_source_costs_to_vault` | 2 modules (connector, extractor) | 3 (source_note_filename, parse_frontmatter, render_frontmatter) | Medium |
| `literature_chunk_wikilink_for_row` | 2 modules (connector, review) | 3 (literature_chunk_wikilink, literature_index_link_label, _summary_from_chunk) | Medium |
| `strip_matching_wikilinks` | 1 module (purge_source) | 1 (_wikilink_target_matches) | Low-Medium (single caller, but irreversible-deletion path) |
| `render_frontmatter` | 4 modules (article, ask, sync_source_costs_to_vault internal, plus compose_note internal) | 0 | Medium |
| `literature_chunk_filename_for_row` | 1 module (purge_source) | 2 (literature_chunk_filename, _summary_from_chunk) | Low |
| `literature_source_dirname` | 2 modules (purge_source; also used internally by literature_chunk_wikilink) | 0 | Low |
| `literature_index_filename` | 1 module (purge_source) | 2 (author_year_label, _slug) | Low |
| `source_note_filename` | 2 modules (bibliography — referenced in comment only, sync_source_costs_to_vault internal) | 1 (note_filename) | Low |
| `upsert_managed_block` | 0 direct external callers (only via safe_update_managed_blocks) | 1 (_block_pattern) | Low (internal helper) |
| `init_vault` | 1 module (cli.py, single call site) | 0 | High (destructive, no test) |
| `_slug`, `_block_pattern`, `_wikilink_target_matches`, `_page_token`, `_section_topic`, `_topic_slug`, `_summary_from_chunk`, `_AUTHOR_YEAR`, `_GENERIC_SECTION_TOPICS` | 0 (private, module-internal only; `_slug` is imported directly by tests) | varies (leaf helpers) | Low |

**Interpretation**: `parse_frontmatter`, `compose_note`, and `safe_write_note` are the module's highest-afferent-coupling symbols — nearly every pipeline phase touches at least one of them — making them the functions where a breaking signature change would have the widest blast radius. `init_vault` has low afferent coupling (a single call site) but is flagged High-criticality because of its destructive, untested nature rather than its coupling breadth.

---

## 7. Endpoints

Not applicable — `zettel/vault.py` exposes no REST/GraphQL/gRPC/CLI-argument surface of its own. It is an internal library module consumed by `cli.py` (Typer commands) and `web.py` indirectly through the pipeline modules; the endpoints/commands that ultimately trigger vault.py code are documented in those components' own analyses, not here.

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|----------------|
| Local filesystem (vault directory tree) | Local I/O | Read/write Markdown note files, create/destroy directory structure | Direct `pathlib.Path` calls | Markdown + YAML frontmatter (UTF-8) | `safe_update_managed_blocks` logs+returns on missing path; `parse_frontmatter` degrades to empty meta on YAML errors; `init_vault`, `safe_write_note` raise on OS-level failures (permissions, disk full) — no retry/backoff |
| PyYAML library | In-process library | Parse/serialize frontmatter | Function calls (`yaml.safe_load`, `yaml.dump`) | YAML | `yaml.YAMLError` caught in `parse_frontmatter` only; `render_frontmatter`'s `yaml.dump` call is unguarded — a non-serializable value in `metadata` (e.g. a raw enum instance) would raise uncaught |
| Consumers within `zettel/*` (harvester, extractor, review, connector, gardener, gardener_hub, moc_backrefs, sync, new_note, purge_source, rebuild, ask, article, chunk_dump, extraction_dump, cli) | In-process function calls | Note construction, filename/link derivation, managed-block updates | Direct Python imports | Python dict/str/Path in-memory objects | No error boundary — exceptions propagate to the caller; each pipeline phase is responsible for its own try/except around vault calls |

No network, database, or external-service integrations exist in this module — by design, per the CLAUDE.md architecture description, `vault.py` is pure Obsidian I/O and never touches `StateDB` or `VectorIndex` directly (`sync_source_costs_to_vault` receives an already-opened `db` object as a parameter and only calls `db.get_source()`, a single read, rather than importing `state.py`).

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Pure function / functional core | Nearly all functions take inputs and return new values with no side effects | Frontmatter, wikilink, and filename-derivation sections (lines 18–393) | Testability without filesystem or mocking; safe to call from any pipeline phase without ordering constraints |
| Builder pattern (functional variant) | `build_source_note`, `build_literature_index_note`, `build_literature_chunk_note` each return a `(metadata, body)` tuple assembled from many optional inputs | Note Builders section (lines 398–736) | Centralizes the exact shape/ordering of each note type's frontmatter and body so all writers produce consistent, parseable notes |
| Sentinel-tag content region (a lightweight variant of "protected regions" code generation) | `<!-- zettel:{name}:start/end -->` HTML comment pairs, read/written via `read_managed_block`/`upsert_managed_block` | Managed Blocks section (lines 88–128) | Lets generated content coexist with hand-edited prose in the same file, the same problem protected-region markers solve in code generators |
| Idempotent write / no-op short circuit | `safe_update_managed_blocks` diffs new vs. original content before writing | Lines 156–157 | Avoids unnecessary disk writes and `updated_at` churn when re-running pipeline phases produces identical output |
| Fail-soft / graceful degradation | `parse_frontmatter`'s YAML-error handling, `safe_update_managed_blocks`'s missing-path handling, `sync_source_costs_to_vault`'s not-found fallback-then-`False` | Multiple | Keeps batch operations (sync, garden, purge) from aborting on one bad/missing file |
| Naming-convention-as-contract (inverse function pairing) | `author_year_label` documented as the inverse of `harvester._generate_citekey` | Lines 208-217 | Avoids storing a redundant "display label" field by deriving it deterministically — but couples two modules through documentation/tests only, not a shared type |
| Fallback chain (a "chain of increasingly desperate defaults") | `_topic_slug`, `_page_token`, `sync_source_costs_to_vault`'s path lookup | Lines 244-289, 530-544 | Ensures a usable filename/topic/file-match always exists even when the "ideal" input (page number, section heading, exact filename) is missing |

No object-oriented design patterns (Strategy, Factory, Observer, etc.) are present — the module deliberately avoids classes, consistent with the "functional core" style used for the frontmatter/naming logic throughout this codebase's vault layer.

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|----------------|-------|--------|
| High | `init_vault` | Unconditional `shutil.rmtree` with zero unit test coverage and no confirmation/dry-run built into the function itself | A caller-side regression (wrong path resolved, a future refactor of `cli.py`'s init flow) could delete a populated vault with no automated test to catch it |
| Medium | `render_frontmatter` | `yaml.dump(metadata, ...)` is not wrapped in error handling anywhere in the module; a non-YAML-serializable value placed in a `meta` dict (e.g. a raw `RelationType` enum, a `Path` object, a `datetime` object instead of its `.isoformat()` string) raises uncaught | A single bad metadata value from an upstream caller can crash a whole pipeline run instead of degrading gracefully, asymmetric with `parse_frontmatter`'s fault tolerance on the read side |
| Medium | `parse_frontmatter` | If frontmatter YAML parses to a non-dict scalar (e.g. a plain string) rather than raising `yaml.YAMLError`, `meta` becomes that scalar rather than `{}`, and every caller that does `meta.get(...)` or `meta["x"] = ...` immediately downstream would raise `AttributeError` | Malformed-but-valid-YAML frontmatter (e.g. a note that starts with `---\njust text\n---`) is not defended against, despite being just as plausible a manual-edit mistake as invalid YAML |
| Medium | `author_year_label` / harvester coupling | The "inverse of `_generate_citekey`" relationship is enforced only by a docstring and shared tests, not a shared constant, regex, or schema | A future change to the harvester's citekey format (e.g. adding a disambiguation suffix before the title slug) could silently break filename derivation across the vault without any compiler/import-level signal |
| Low-Medium | `sync_source_costs_to_vault` | The fallback path glob-scans and parses every `SRC - *.md` file's frontmatter on every miss, and is called once per LLM/embedding call from `connector.py`/`extractor.py` | On a vault with a very large number of sources, and if the fast path frequently misses (e.g. titles routinely revised post-harvest), this becomes an O(n) file-parse per LLM call rather than O(1) |
| Low | `strip_matching_wikilinks` bullet-cleanup heuristics | The set of "now-empty bullet" patterns to strip is a hardcoded literal list (`"-"`, `"- ()"`, `"←"`, `"← "`) rather than a more general "line became empty of meaningful content" check | A new managed-block or manual-note bullet style not in this literal list (e.g. a different arrow glyph, or a numbered-list marker) would leave a visibly empty bullet after purge instead of being cleaned up |
| Low | `_wikilink_target_matches` | Basename-only fallback match (`base in link_targets`) could, in principle, match a same-named file under a *different* citekey's folder if two sources ever produced identically-named granular chunk files | Given the chunk-index suffix makes filenames unique per-source in practice, this is a theoretical rather than observed risk, but the function does not disambiguate by folder unless the full path-qualified form is what's in `link_targets` |
| Low | Whole module | No `__all__` defined and several "private" helpers (`_slug`, `_block_pattern`, `_wikilink_target_matches`, `_summary_from_chunk`) are imported directly by other modules or tests despite the underscore convention (e.g. `ask.py`/`article.py` import `_slug` directly) | The underscore-prefix convention for "internal use only" is not actually enforced; renaming or changing the signature of a nominally-private helper is a de facto breaking change |

---

## 11. Test Coverage Analysis

Primary test file: `tests/test_vault.py` (272 lines, 22 test functions), located at the project root's `tests/` directory (not colocated with `zettel/vault.py`). Additional indirect coverage exists through integration-style tests in other files that exercise vault.py functions as part of a larger pipeline flow.

| Symbol / Area | Direct Unit Tests (test_vault.py) | Indirect Coverage (other test files) | Test Quality |
|---|---|---|---|
| `parse_frontmatter` | 2 (`test_parse_frontmatter_basic`, `test_parse_frontmatter_no_frontmatter`) | Exercised transitively by `test_new_note.py`, `test_review.py`, `test_purge_source.py`, `test_moc_backrefs.py`, `test_sync.py` (they read back written files) | Good for the two documented code paths; no test for malformed-YAML degradation (`meta = {}` on `yaml.YAMLError`) or non-dict-scalar frontmatter |
| `compose_note` | 1 (`test_compose_note`) | Used as a fixture-builder in nearly every other vault-adjacent test | Adequate — the function is trivial (string concat) so a single roundtrip test is proportionate |
| `read_managed_block` / `upsert_managed_block` | 4 (`test_read_managed_block`, `test_read_managed_block_not_found`, `test_upsert_managed_block_insert`, `test_upsert_managed_block_replace`) | `test_moc_backrefs.py`, `test_connector.py` exercise these via `safe_update_managed_blocks` | Good — covers insert, replace, and not-found; does not explicitly test the "start tag present but end tag missing" (corrupted block) branch (lines 122-124) |
| `safe_update_managed_blocks` | 2 (`test_safe_update_managed_blocks_bumps_updated_at`, `test_safe_update_managed_blocks_idempotent_keeps_updated_at`) | `test_gardener.py`, `test_gardener_hub.py`, `test_moc_backrefs.py`, `test_review.py`, `test_purge_source.py`, `test_set_paging.py` (via `safe_write_note` co-occurrence) | Good — the two most important behaviors (timestamp bump on change, no-op on idempotent call) are both explicitly tested; missing-path warning branch (lines 148-150) is not directly tested |
| `_slug` | 1 (`test_slug`, imports the private `_slug` directly) | N/A | Covers basic slugification and length truncation; does not test the trailing-hyphen-strip edge case explicitly (e.g. input ending in punctuation) beyond what the two assertions imply |
| `note_filename` | 1 (`test_note_filename`) | Used throughout `test_gardener.py`, `test_gardener_hub.py`, `test_new_note.py`, `test_rebuild.py` | Adequate |
| `author_year_label` | 1 (`test_author_year_label`, 4 assertions covering matched/`@`-prefixed/short-year/no-year cases) | N/A | Good — covers the documented edge cases directly |
| `literature_source_dirname` | 1 (`test_literature_source_dirname_strips_at`) | `test_purge_source.py` | Adequate |
| `source_note_filename` | 1 (`test_source_note_filename_uses_author_year`) | `test_bibliography.py` (via `build_source_note`) | Adequate |
| `literature_index_filename` | 1 (`test_literature_index_filename_no_at_no_index_suffix`) | N/A direct; harvester/review integration tests write index files but don't assert the filename shape by name | Adequate for the primary case |
| `literature_chunk_filename` | 3 (page+section, same-section-differs-by-index, falls-back-to-summary-slug) | N/A | Good — covers the two-tier fallback (section → summary) and the uniqueness-by-index guarantee explicitly; does not test the final "nota" fallback when both section and summary are absent |
| `literature_chunk_wikilink` | 1 (`test_literature_chunk_wikilink_is_path_qualified`) | `test_connector.py`, `test_review.py` (via `literature_chunk_wikilink_for_row`) | Adequate |
| `literature_index_link_label` | 1 (`test_literature_index_link_label`) | N/A | Adequate for the page+topic case; no test for the no-page fallback (`return topic`, line 368) |
| `build_literature_chunk_note` | 2 (`test_literature_chunk_note_includes_source_excerpt`, `test_literature_chunk_note_empty_source_placeholder`) | `test_extractor.py`, `test_review.py` (produce and consume draft notes end-to-end) | Good — the excerpt/embeddable-text separation (a security/prompt-hygiene-relevant behavior) is explicitly asserted via `extract_embeddable_text` |
| `permanent_wikilink` | 2 (`test_permanent_wikilink_prefers_path_stem`, `test_permanent_wikilink_falls_back_to_title`) | `test_connector.py`, `test_gardener.py`, `test_ask.py`, `test_article.py` | Good |
| `build_source_note` | 0 direct in test_vault.py | `test_bibliography.py` exercises it with biblio fields; `test_new_note.py`, integration-style, via `new_note.py` | Covered only indirectly; no direct test in `test_vault.py` for the many `if value is not None` optional-field branches (paging, cost/tokens, docling_config_hash) |
| `build_literature_index_note` | 0 direct | `test_review.py` (approval flow rebuilds the index) | Covered indirectly only; no direct assertion on `approved_links=None` vs. populated list distinction |
| `build_permanent_note_body` | 0 direct in test_vault.py | `test_connector.py`, `test_new_note.py` | Covered indirectly; the `RelationType.value` vs. `str()` rendering fix (lines 726-729) is not directly unit-tested against a real `RelationType` enum instance in `test_vault.py` itself |
| `sync_source_costs_to_vault` | **0** | **0** — no reference found in any test file under `tests/` | **Gap**: neither the fast-path filename match nor the fallback glob-scan-by-`source_id` branch, nor the not-found `False` return, has any automated test coverage |
| `strip_matching_wikilinks` / `_wikilink_target_matches` | **0** in test_vault.py | `test_purge_source.py` exercises it indirectly through `purge_source._clean_note_file` | Not directly unit-tested in isolation; the "Ref. literatura:" placeholder-rewrite special case and the empty-bullet-cleanup list are only as well-tested as `test_purge_source.py`'s scenarios happen to cover |
| `init_vault` | **0** | **0** — no reference found in any test file under `tests/` | **Gap**: the most destructive function in the module (`shutil.rmtree`) has zero automated test coverage of any kind |

**Overall assessment**: Core text-manipulation logic (frontmatter parsing, managed blocks, filename/wikilink derivation) is well covered with focused, fast, no-mocking-required unit tests (`tests/test_vault.py`, 22 tests). The two functions with the highest real-world risk profile — `init_vault` (irreversible mass deletion) and `sync_source_costs_to_vault` (silent-failure fallback scanning) — have no test coverage at all, direct or indirect. `strip_matching_wikilinks` is only exercised through a higher-level integration test (`test_purge_source.py`) rather than directly, meaning its many small text-cleanup edge cases (the four literal bullet-artifact strings, the "Ref. literatura:" special case) are undertested in isolation.

---

**Component analyzed**: `vault` (`zettel/vault.py`)
**Report saved to**: `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-vault-2026-08-30_10-22-26.md`
