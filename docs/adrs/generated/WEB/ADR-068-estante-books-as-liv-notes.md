# ADR-068: The Estante, Books as LIV Notes With a SQLite Mirror

**Status:** Accepted
**Date:** 2026-11-08
**Depends on:** [ADR-022: FastAPI Server-Rendered Web Interface](./ADR-022-fastapi-server-rendered-jinja2.md), [ADR-030: Manual Notes Adopted at Sync Without Review Gate](../MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md), [ADR-067: The Mesa, a Writing Desk Built From Permanent Notes](./ADR-067-mesa-writing-desk.md)
**Related to:** [ADR-046: Bibliographic Duplicate Layers](../HARVEST/ADR-046-bibliographic-duplicate-layers.md), [ADR-057: Bibliographic Field Provenance](../HARVEST/ADR-057-bibliographic-field-provenance.md), [ADR-069: Prompts Chosen by Reading Mode](../EXTRACT/ADR-069-reflective-prompts-by-reading-mode.md)

## Context and Problem Statement

A source is what the pipeline reads, not what a person read. Nothing recorded the reader's side of a book:

- whether it was read, when, in which format;
- the code written on the physical copy;
- its genres, its country, its cover;
- what the reader thought of it.

The author kept all of this in a Notion database. It had a 12-branch `ifs()` formula that weighted criteria by genre and by whether the book was Brazilian.

Two facts constrain where this data can live:

- `zettel init --reset` deletes the SQLite database and every pipeline SRC note, so ratings kept only in SQLite or only on a pipeline SRC note would be lost.
- A novel or a comic read on paper has no file for the harvest, but it still deserves reading notes.

## Decision Drivers

* The reader's data must survive a reset and a re-harvest. It must also be editable in Obsidian.
* A harvested book should appear on the shelf without a second step.
* The bibliography that citations read stays in one place.
* Lists, filters and statistics must not re-read every YAML file on every page.
* The rating rules belong to the author and change over time, so they are configuration, not code.

## Considered Options

1. A vault note per book (`LIV`), with a SQLite mirror rebuilt from the files (chosen).
2. New columns on `sources` plus the SRC frontmatter.
3. A separate database file outside `state.db`.

## Decision Outcome

Chosen option 1.

### The LIV note

* **Location.** The note is `60_Estante/LIV - <ULID> - <slug>.md`, with `type: book` and `origin: manual`. Because of `origin: manual`, `init --reset` keeps it.
* **Frontmatter.** It holds only the reader's data:
  * `code_number` and `code` (`0001 - F`);
  * `format`, `status`, `readings` (a list of `{start, end}`);
  * `genres` and `subjects`;
  * `country` and `original_language`;
  * `cover`, `ratings`, `rating_profile`, `score`, `favorite`;
  * `nature` and `reading_mode`;
  * an identity: `title`, `authors`, `isbn` and `source_id`.

  Keys are in English and values in PT-BR. Every key is always present, so Obsidian's property panel shows it.
* **Body.**
  * `## Resumo` and `## Impressões` are the author's text, kept verbatim.
  * `auto-book-notes` is a managed block that lists the source's LIT and ZTL notes.
* **Saving.** A save is refused when the file's hash changed since the page was rendered, as on the Mesa. The file is written in LF.
* **The code.** It joins a universal sequential number to the format letter (`library.format_letters`), so the next book after `0001 - F` is `0002 - D` if it is digital. The number is never reused. Changing the format changes only the letter.
* **Bibliography stays on the SRC and in `sources`** (ADR-057). The book page edits publisher, edition, place, translator and year by writing the SRC note and resyncing it through `sync.sync_source_note`.

### The mirror

* **Tables.** `books` and `book_readings` (`state/books.py`) mirror the files so that lists, filters and the dashboard can use SQL.
* **Refresh.** `library.mirror.refresh` re-reads only the files whose `mtime` or size changed, or all of them when `library.yaml` changed. It runs on every Estante page, in `sync-manual`, after a harvest, and before `extract`, `connect`, `ask` and `article`.
* **Derived fields.** The score and the effective reading mode are computed there, not trusted from the file.

### Linking to sources

* **Harvest creates the book.** A harvest whose `document_type` is in `library.document_types` either creates the LIV (`format: digital`) or links the LIV that already matches, by ISBN or by title plus surname.
* **Relinking after a reset.** A LIV whose `source_id` no longer resolves is relinked by ISBN, then by title plus author. With more than one candidate, nothing is chosen and the page lists the candidates.
* **Books without a file.** The first reading note on a book with no source creates a manual SRC for it, with the ISBN.

### Rating

* **Where the rules live.** `config/library.yaml` holds the genre vocabulary (each genre with its nature and whether it asks for a reflective reading), the criteria (English keys, PT-BR labels) and the profiles.
* **Profile choice.** The first profile whose `when` matches decides (`genres_any`, `translated`). A profile can also be forced per book with `rating_profile`.
* **Translation.** "Translated" follows the original language, and falls back to the country only when the language is missing.
* **The score** is `ceil(Σ weight·rating / Σ weight · 100) / 100` over the criteria actually rated. The page shows how complete it is. In the Notion formula an empty criterion counted as zero.
* **Validation.** An unknown criterion, a weight ≤ 0 or a duplicated profile name is refused when the vocabulary loads.

### The web (`/books`)

* **Pages.**
  * A list with filters and ready-made views, in three layouts: table, cover gallery, and a board by status.
  * A detail page with forms for metadata, readings, ratings, texts, cover and bibliography, and reading notes. Each form posts under the lock and with the file version.
  * A dashboard.
* **Routes and labels.** Routes are in English and the interface in PT-BR.
* **Covers.** A cover is an upload or the Open Library image for the ISBN (one explicit network call). It is stored content-addressed in `90_Assets/`, but **not** in the `assets` table, where the vision model would describe it.
* **Dashboard charts** are server-rendered SVG with attributes only, under the CSP of ADR-022 (see its addendum).

### Positive Consequences

* A reset and a re-harvest keep the reader's history and ratings, and relink them.
* The Notion branches are reproduced by data, so changing a weight is a YAML edit.
* Obsidian sees the same properties as the web and can edit them. The next page load picks the change up.

### Negative Consequences

* The LIV carries a copy of the title, authors and ISBN. A title fixed only on the SRC does not reach the LIV.
* A book linked to a pipeline SRC gets a new citekey after a reset if the harvest disambiguates differently. The relink by ISBN or by title is what keeps it attached.
* Every Estante page pays one `stat()` per book.

## Pros and Cons of the Options

### Option 2: columns on `sources`

* Good, because there is a single row per book.
* Bad, because a reset loses the data and a book with no file has no row.

### Option 3: a separate database

* Good, because it survives `state.db` resets.
* Bad, because Obsidian cannot read or edit it, and it is a second store to back up.

## References

* `zettel/library/book_file.py` — `create_book`, `save_book`, `format_code`, `replace_section`
* `zettel/library/schema.py`, `zettel/library/rating.py` — vocabulary and score
* `zettel/library/mirror.py` — `refresh`, `library_health`
* `zettel/library/linking.py` — `sync_library`, `ensure_books_for_sources`, `relink_orphans`
* `zettel/library/editing.py`, `zettel/library/covers.py`, `zettel/library/stats.py`, `zettel/library/reading_notes.py`
* `zettel/state/books.py`, `zettel/web/library.py`, `config/library.yaml`
* `tests/test_library_book_file.py`, `tests/test_library_rating.py`, `tests/test_library_mirror.py`, `tests/test_library_linking.py`, `tests/test_web_library.py`
