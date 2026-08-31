# Component Deep Analysis Report — `bibliography`

## 1. Executive Summary

`zettel/bibliography.py` is a self-contained domain module responsible for everything related to **ABNT-style bibliographic metadata**: modeling it (`BibliographicMetadata`), inferring it cheaply from file metadata/text heuristics, enriching it with an LLM call (cached deterministically), validating completeness against per-document-type required fields, and rendering it into two distinct citation surfaces:

- **Full ABNT reference-list entries** (`format_abnt` — ABNT NBR 6023 style, one formatter per of 8 document types), used for the `SRC` vault note body/frontmatter and for the "Referencias" section of academic-style articles.
- **Parenthetical in-text citations** (`format_abnt_in_text` — ABNT NBR 10520 author-date style, e.g. `(SANTOS, 2020)`, `(SILVA; SOUZA, 2019)`, `(NEGRO et al., 2026, p. 42)`), used by `article.py`'s `CatalogSource.in_text_cite` for both blog and academic article styles.

The component has no runtime state of its own; it is a pure library of Pydantic models + functions called from three producers of bibliographic data — `harvester.py` (automatic pipeline harvest with heuristics + LLM + optional HITL), `new_note.py` (manual scaffolding via CLI flags), `sync.py` (adoption of hand-written frontmatter during `sync-manual`) — and one consumer of its formatting helpers — `article.py` (`CatalogSource`, used by both the blog and academic writing paths in the `article`/`article_graph` LangGraph pipeline). `vault.py` and `state.py` persist/render the data this module produces but do not import it directly.

Key findings:
- The module is well-isolated: it depends only on `config.py`, `hashing.py`, `llm.py`, `state.py` (for the LLM cache), and the stdlib/pydantic — it has no circular or reverse dependencies from lower-level infrastructure modules.
- Business logic is dense and almost entirely deterministic string-formatting plus a small heuristic classifier; the only non-deterministic external call (LLM enrichment) is fully wrapped in a deterministic-cache pattern shared with the rest of the codebase (`compute_llm_call_checksum`).
- Test coverage is good for the pure functions (formatting, heuristics, merge) in `tests/test_bibliography.py`, and integration coverage exists via `test_article.py` (in-text citation + `CatalogSource`), `test_new_note.py` (manual scaffold path), and `test_harvester_dedup.py` / `test_extraction_dump.py` (harvest path with `skip_biblio`). No dedicated unit tests exist for `enrich_with_llm`, `_merge_biblio`, or `_coerce_llm_dict` (the LLM-facing half of the module) — see Technical Debt.

## 2. Data Flow Analysis

There are three distinct entry paths into this component, and one output-only consumption path.

### 2a. Automatic harvest path (`harvest` command → `harvester.py::_process_file`)

```
1. harvester._extract_text() produces (text, metadata) from the PDF/MD file
2. harvester._process_file() calls bibliography.build_bibliographic_metadata(cfg, db, metadata, text, filename)
   2.1 infer_from_file_metadata(metadata, text_sample, filename)   — cheap heuristics -> seed BibliographicMetadata
   2.2 enrich_with_llm(cfg, db, seed, text_sample, filename)       — optional LLM call, deterministic cache via db.get_cached_llm_response
       2.2.1 cache hit  -> parse cached response_text
       2.2.2 cache miss -> call_llm(...) -> db.cache_llm_response(...)
       2.2.3 _coerce_llm_dict() normalizes LLM JSON -> BibliographicMetadata.model_validate()
       2.2.4 _merge_biblio(seed, llm_meta) -> merged BibliographicMetadata (LLM wins on non-empty fields)
3. harvester._resolve_bibliography(file_path, biblio, interactive, skip_biblio, cfg)
   3.1 is_complete(biblio, threshold) checked against harvest.biblio_confidence_threshold
   3.2 non-interactive: complete -> accept; incomplete + skip_biblio -> accept with warning; else -> return None (file skipped)
   3.3 interactive: Rich-console preview/edit loop (type selection, required-field fill-in, optional-field fill-in), always ends in a fresh BibliographicMetadata or None (user aborts)
4. If None: file skipped entirely (no SRC/LIT/chunks created)
5. If metadata resolved: primary_title() / primary_authors() derive the canonical title/authors
   (capitulo_livro prefers chapter_authors/chapter_title over the book-level authors/title)
6. format_abnt(biblio) -> abnt_reference string (used for SRC frontmatter + body)
7. bibliography_dict(biblio) -> JSON-serializable dict -> stored as sources.bibliography_json
8. frontmatter_biblio_fields(biblio) -> flat dict of type-specific fields (minus core title/authors/year/document_type)
9. citekey generated from (authors, year, title) — bibliography output feeds citekey generation
10. db.upsert_source(..., document_type=biblio.document_type, bibliography_json=biblio_json, abnt_reference=abnt_reference)
11. vault.build_source_note(..., document_type=..., biblio_fields=biblio_fm, abnt_reference=...) -> SRC note frontmatter + "## Referencia ABNT" body block
```

### 2b. Manual scaffold path (`zettel new-note src` → `new_note.py`)

```
1. CLI flags (--document-type, --abnt-reference, --place, --publisher, --edition, --doi, --url, --journal, --institution, --pages, ...)
2. new_note._collect_biblio_fields() assembles a flat dict from the CLI flags (no BibliographicMetadata instance is built here;
   the module's typed model is bypassed — raw dict flows straight to vault.build_source_note)
3. vault.build_source_note(document_type=..., biblio_fields=collected, abnt_reference=...) -> SRC note written to vault
4. Later, `zettel sync-manual` reads the frontmatter back (see 2c)
```

### 2c. Manual sync-adoption path (`zettel sync-manual` → `sync.py`)

```
1. sync.py scans 10_Sources/*.md, parses frontmatter
2. biblio_payload assembled from a fixed allowlist of frontmatter keys
   ("document_type", "subtitle", "edition", "place", "publisher", ... ) plus authors/year/title fallbacks
3. db.upsert_source(document_type=meta.get("document_type"), bibliography_json=json.dumps(biblio_payload), abnt_reference=meta.get("abnt_reference"))
   -- Note: sync.py does NOT re-run format_abnt() or re-validate against BibliographicMetadata; it trusts whatever
      abnt_reference string is already in the note's frontmatter (hand-written or previously generated).
```

### 2d. Article-writing consumption path (`article.py` → `CatalogSource`)

```
1. article._build_catalog() reads db.get_source(source_id) rows (already containing abnt_reference/document_type
   written by path 2a/2b/2c) and wraps them into CatalogSource dataclass instances
2. CatalogSource.in_text_cite     -> bibliography.format_abnt_in_text(authors, year)          (per-mention parenthetical cite)
3. CatalogSource.author_natural   -> bibliography.display_author_natural(authors)              (blog "light mention" author name)
4. CatalogSource.light_mention    -> f"{author_natural} em *{title}*"                           (blog style)
5. Academic style "## Referencias" section sorts and emits src.abnt_reference verbatim (falls back to a minimal
   "Author. Title. Year." string if abnt_reference is empty)
6. Blog style "## Para saber mais" section uses author_natural/title/year directly (no ABNT formatting)
```

## 3. Business Rules & Logic

## Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Domain schema | 8 fixed ABNT document types with per-type required-field sets | bibliography.py:20-65 |
| Validation | A source is "complete" only if `document_type` is set, `confidence >= threshold`, and no required field is empty | bibliography.py:204-209 |
| Formatting | Author-name inversion to ABNT surname-first form (`SURNAME, Given`) | bibliography.py:252-261 |
| Formatting | 1-3 authors listed with "; "; >3 authors collapsed to "first et al." (reference list) | bibliography.py:264-270 |
| Formatting | In-text citation collapses to surname-only, up to 3 authors joined by "; ", 4+ collapses to "et al." | bibliography.py:278-310 |
| Formatting | Missing year renders as "s.d." (sem data) in both reference and in-text citation | bibliography.py:289, 310 |
| Formatting | `capitulo_livro` prefers chapter-level author/title over book-level author/title everywhere | bibliography.py:212-221 |
| Formatting | Per-document-type ABNT reference assembly (8 distinct handlers) | bibliography.py:345-577 |
| Formatting | Access-date normalization to "DD mon. AAAA" PT-BR abbreviations | bibliography.py:325-338 |
| Heuristic classification | Keyword/regex-based document-type inference with fixed confidence scores per rule | bibliography.py:595-644 |
| Heuristic classification | Existing valid `document_type` in source frontmatter overrides heuristics and forces confidence >= 0.85 | bibliography.py:642-644 |
| LLM enrichment gate | LLM enrichment is entirely skippable via `harvest.biblio_llm_enabled` config flag | bibliography.py:736-737 |
| LLM enrichment gate | Missing prompt file silently no-ops (returns seed unchanged), only logs a warning | bibliography.py:739-742 |
| Caching | LLM bibliographic calls are cached deterministically (prompt+sample+model+temperature+language+seed hash) | bibliography.py:744-763 |
| Merge precedence | LLM output overrides heuristic seed field-by-field only where the LLM provided a non-empty value | bibliography.py:701-725 |
| Merge precedence | Confidence is bumped to >= 0.85 if all required fields end up filled, else >= 0.6 if only `document_type` is known | bibliography.py:720-724 |
| Data coercion | LLM JSON is defensively coerced: unknown `document_type` values nulled, year regex-extracted, confidence force-cast to float, city/editora aliases normalized | bibliography.py:802-822 |
| Harvest gating | Incomplete bibliography blocks the file from being harvested at all, unless `--skip-biblio` is passed (non-interactive) or the user confirms "continue anyway" (interactive) | harvester.py:958-978, 1092-1100 |
| Serialization | `bibliography_dict()` strips `confidence` and all empty/None/blank values before JSON-serializing for SQLite storage | bibliography.py:224-237 |
| Frontmatter shaping | `frontmatter_biblio_fields()` excludes the four "core" fields already written explicitly elsewhere (document_type, title, authors, year) and restricts output to the known `BIBLIO_FRONTMATTER_FIELDS` allowlist | bibliography.py:240-246 |

## Detailed breakdown of the business rules

---

### Business Rule: Eight-type ABNT document taxonomy with per-type required fields

**Overview**:
The component hard-codes a closed taxonomy of eight ABNT-relevant document types (`livro`, `capitulo_livro`, `artigo_periodico`, `artigo_internet`, `material_curso`, `tese`, `anais_evento`, `relatorio`), each with its own tuple of required fields in `REQUIRED_FIELDS` and its own human label in `DOCUMENT_TYPE_LABELS`.

**Detailed description**:
This taxonomy is the backbone of the entire component: every other function — heuristic inference, LLM enrichment, completeness checking, and reference formatting — dispatches on `document_type`. A book (`livro`) requires `authors, title, place, publisher, year`; a book chapter (`capitulo_livro`) requires the extended set `chapter_authors, chapter_title, book_title, place, publisher, year, pages`; a thesis (`tese`) requires `authors, title, year, institution, degree`; and so on for the remaining five types. `document_type=None` is itself a valid state meaning "type unknown," in which case `required_fields(None)` returns the single sentinel field `["document_type"]`, so a record with no type is always "missing" exactly one thing — the type itself — regardless of how many other fields happen to be filled in.

Because `format_abnt()` uses a `handlers` dispatch dict keyed by `document_type` with no default case, calling it with an unrecognized (or `None`) type would raise a `KeyError` — the caller (`format_abnt`) guards this only by short-circuiting to `""` when `document_type` is falsy (line 343-344), which means any string that isn't `None`/empty but also isn't one of the eight literals will crash the pipeline. In practice this can only happen if `_coerce_llm_dict` fails to null out an invalid value (it does null anything not in `DOCUMENT_TYPES`, so the LLM path is safe) or if a hand-edited vault note's frontmatter is fed back in with a typo'd `document_type` — the `sync.py` adoption path stores whatever string is present without validating it against `DOCUMENT_TYPES`.

**Rule workflow**:
`infer_from_file_metadata` picks an initial type from keyword heuristics → optionally overridden by `enrich_with_llm`/`_merge_biblio` if the LLM is confident → `required_fields(document_type)` and `missing_required(meta)` are then used both for the machine gate (`is_complete`) and for the interactive HITL prompt's list of fields to fill in.

---

### Business Rule: Completeness gate (`is_complete` / `missing_required`)

**Overview**:
A `BibliographicMetadata` record is considered "complete" only when it has a recognized `document_type`, a `confidence` at or above a configurable threshold (`harvest.biblio_confidence_threshold`, default 0.7), and none of that type's required fields are empty.

**Detailed description**:
`_field_empty()` defines "empty" per field type: `document_type` is empty if falsy; list fields (`authors`, `chapter_authors`, `book_editors`) are empty if the list is empty; everything else is empty if `None` or (for strings) blank after stripping. `missing_required()` returns `["document_type"]` immediately if the type itself is unknown — it does not also report other missing fields in that case, since without a type there is no way to know which fields are even required. `is_complete()` layers three independent conditions (type known, confidence above threshold, no missing fields) — all three must hold, so a well-populated record with low confidence is still treated as incomplete, and a high-confidence record with a genuinely missing required field is also incomplete. This gate is the single choke point that decides whether a harvested file proceeds to SRC/LIT/chunk creation at all (see harvester.py `_resolve_bibliography`), making it one of the highest-impact rules in the whole pipeline — a false "incomplete" verdict silently drops a file from processing in non-interactive mode.

The confidence threshold is deliberately duplicated as a purpose: raising it not only affects whether the record passes but also determines when the interactive prompt treats the record as "low confidence" (bypassing the yes/no "alterar tipo documental?" choice and forcing the type-selection menu open unconditionally).

**Rule workflow**:
Heuristic/LLM pipeline produces `(document_type, confidence, fields...)` → `is_complete(meta, cfg.harvest.biblio_confidence_threshold)` → harvester branches on interactive/non-interactive and on `skip_biblio`.

---

### Business Rule: Author-name inversion and et al. collapsing for reference lists

**Overview**:
`invert_author_name` converts "Given Middle Surname" to ABNT's "SURNAME, Given Middle" form; `format_authors_abnt` joins up to three inverted names with "; " and collapses four-or-more to `"{first author inverted} et al."`.

**Detailed description**:
The inversion rule is naive by design: it splits on whitespace and treats the *last* whitespace-separated token as the surname, upper-cased, with everything before it kept as the given name(s) in original order — e.g. "João Silva Santos" → "SANTOS, João Silva". A single-token name (e.g. "Platão") is simply upper-cased with no comma, since there is no distinguishable surname to invert. This is a hard heuristic with no support for compound surnames (e.g. Portuguese "da Silva" or Spanish double surnames), particles, or suffixes — any of those would be misparsed as part of the given name or, worse, isolated as if they were the surname when they are the trailing token. The codebase does not appear to special-case this anywhere; it is accepted as a known simplification (see Technical Debt).

The three-vs-four-plus threshold matches the ABNT NBR 6023 convention for reference lists: works with up to three authors list every author; four or more collapse to the first author followed by "et al." — the ABNT-mandated Latin abbreviation for multi-author works cited in bibliographies. Empty/blank author strings are filtered out before counting, so a list like `["", "  ", "Real Author"]` is treated as a single-author list, not a three-element list with two blanks.

**Rule workflow**:
`format_abnt_*` per-type handlers call `format_authors_abnt(meta.authors)` (or `meta.chapter_authors` for `capitulo_livro`) as the very first element of the assembled reference string; the same underlying `invert_author_name` is reused when formatting `book_editors` in the `capitulo_livro` handler ("In: {editors} (org.). {book}.").

---

### Business Rule: In-text (parenthetical) citation formatting — distinct rules from the reference list

**Overview**:
`format_abnt_in_text` implements ABNT NBR 10520's author-date parenthetical citation style, which has different collapsing thresholds than the reference-list style: 1, 2, or 3 authors are all listed by surname only (no given names, no inversion needed since only the surname is shown); 4+ collapses to "et al." Missing year renders as the literal string "s.d." unless there are also no authors, in which case the whole citation returns "(s.d.)" only if a year truthily exists, otherwise an empty string.

**Detailed description**:
This is a genuinely separate business rule from `format_authors_abnt`, not a thin wrapper around it — ABNT distinguishes the *reference-list* et al. threshold (>3 authors) from the *in-text* one (>3 as well here, but the in-text form never inverts names or shows given names, only surnames joined by semicolons). The function has an explicit edge case: `if not cleaned: return f"({year_str})" if year else ""` — meaning a citation with authors stripped down to nothing (all blank) but a year present degrades to just `(2020)`, while one with neither authors nor year silently degrades to an empty string. This empty-string fallback is significant because `article.py`'s `CatalogSource.in_text_cite` property calls this unconditionally and article-drafting prompts embed the result inline in generated prose (`citacao_abnt: {src.in_text_cite or '(indisponivel)'}`) — the `or '(indisponivel)'` fallback in `article.py:756` exists specifically to catch this empty-string case for LLM-facing context, but the in-prose citation itself (when the LLM inserts `in_text_cite` directly into a sentence) has no equivalent safety net if the LLM copies an empty string verbatim.

An optional `pages` argument, when provided, is appended after the year with no reformatting or validation — the caller is responsible for pre-formatting it (e.g. "p. 42"); the function only checks that the stripped string is non-empty.

**Rule workflow**:
`CatalogSource.in_text_cite` (article.py) → `format_abnt_in_text(self.authors, self.year)` (no page support wired through the dataclass property — pages would need direct calls, but no caller in the current codebase passes `pages` outside `tests/test_article.py`'s direct function-level test).

---

### Business Rule: LLM enrichment merge precedence — "LLM wins on non-empty, seed survives on LLM-empty"

**Overview**:
`_merge_biblio(seed, llm_meta)` starts from a deep copy of the heuristic `seed` and overwrites each field with the LLM's value only when that LLM value is non-None, a non-empty list, or a non-blank string; otherwise the seed's original value is preserved.

**Detailed description**:
This is a conservative merge strategy that treats the LLM as strictly additive/corrective rather than authoritative — if the LLM's JSON response omits a field (or the model legitimately doesn't know it and returns `null`/empty string), the heuristic-derived seed value for that field is never destroyed. The `document_type` field gets special-cased on top of the generic loop: even though the generic loop already handles it (a non-empty string overwrites), lines 715-716 redundantly re-check `if llm_meta.document_type: merged.document_type = llm_meta.document_type`, which is a no-op given the loop already ran — likely leftover/defensive code (see Technical Debt).

Confidence is merged as `max(seed.confidence, llm_meta.confidence or 0.0)`, then immediately re-computed as `max(merged.confidence, llm_meta.confidence)` on the next line (also redundant — same value), and finally boosted based on completeness of the *merged* result: if the merged record has a `document_type` and no `missing_required` fields, confidence floors at 0.85 regardless of what the LLM or heuristic actually reported; if only the type is known but fields are still missing, confidence floors at 0.6. This means confidence is not purely a measure of the LLM's/heuristic's own certainty — it is partly a derived function of how complete the final record happens to be, which conflates "the model is sure" with "the record has no blanks." A record could have all required fields trivially filled with low-quality placeholder text and still receive a 0.85 confidence floor, silently clearing the `is_complete()` gate.

**Rule workflow**:
`build_bibliographic_metadata` → `infer_from_file_metadata` (seed) → `enrich_with_llm` (may call LLM or hit cache) → `_merge_biblio(seed, llm_meta)` → returned to harvester, which then applies the harvest-level completeness gate independently.

---

### Business Rule: Deterministic LLM-call caching for bibliographic enrichment

**Overview**:
Every LLM call for bibliographic enrichment is preceded by computing a deterministic checksum over the prompt template, the text sample, the model/temperature/language, and the seed JSON; if a cached response exists for that exact checksum, the LLM is never called and the cached text is parsed instead.

**Detailed description**:
This reuses the shared `compute_llm_call_checksum` helper from `hashing.py`, the same pattern used by `connector.py` and `ask.py`. The cache key incorporates `rag_context_checksum=seed_checksum` — i.e. the heuristic seed (title/authors/year/etc. already inferred) is treated as part of the "context" for cache-key purposes, so if the heuristic seed changes (e.g. because upstream extraction produced different metadata), the cache key changes too and a fresh LLM call is triggered even though the prompt template and text sample are identical. Cache hits are explicitly recorded via `zettel.usage.record_cache_hit(label=f"biblio:{filename}", ...)` so that cost dashboards can distinguish "this cost nothing because it was cached" from "this genuinely cost $0 because the provider is free" — consistent with the project-wide convention documented in CLAUDE.md that "SQLite `llm_cache` hits are $0."

If the prompt file `prompts/bibliographic_metadata.md` is missing entirely, the function does not raise — it logs a warning and returns the unmodified `seed`, meaning bibliographic enrichment degrades gracefully to "heuristics only" rather than failing the harvest. Any exception during the LLM call itself (network error, provider error) is caught broadly (`except Exception`) and also degrades to returning `seed` with a warning log — no retry logic exists at this layer.

**Rule workflow**:
`enrich_with_llm` → checksum computed → `db.get_cached_llm_response(call_checksum)` → cache hit: use cached text; cache miss: `call_llm(...)` then `db.cache_llm_response(...)` → parse JSON (`extract_json` + `json.loads`) → `_coerce_llm_dict` → `BibliographicMetadata.model_validate` → `_merge_biblio`.

---

### Business Rule: Heuristic document-type inference is a strict if/elif priority chain

**Overview**:
`infer_from_file_metadata` classifies the document type using a fixed-priority chain of keyword/regex checks against a lower-cased text sample and filename, each branch assigning both a `document_type` and a hard-coded confidence value; the chain always terminates in a `livro` default if nothing else matches.

**Detailed description**:
The priority order is: thesis keywords ("tese (", "disserta", "trabalho de conclus" or "tese" in filename) → course-material keywords ("disciplina", "plano de aula", "material did" or "aula"/"curso"/"disciplina" in filename) → conference-proceedings keywords ("anais", "congresso", "simpósio"/"simposio", "proceedings") → book-chapter markers (regex `\bin:\s` or "capítulo"/"capitulo" in text) → periodical-article keywords ("revista", "journal", "vol.", "v. ", "n. ", "doi:") → web-article condition (has a URL AND (".html" in filename OR "http" appears in the first 500 chars OR metadata carries a URL)) → technical-report keywords ("relatório", "relatorio tecnico", "technical report") → fallback `livro` with confidence 0.4 if both authors and year are already known, else 0.3. Because this is a strict if/elif chain (not a scored multi-signal classifier), a document that matches multiple categories' keywords is silently assigned to whichever branch is checked first — e.g. a thesis that also happens to cite "revista" sources heavily in its opening pages would still correctly classify as `tese` (checked first), but a conference paper that also contains the word "revista" prominently would be misclassified as `artigo_periodico` before ever reaching the `anais_evento` check... actually the reverse: `anais_evento` keywords are checked *before* `artigo_periodico`, so this specific example is safe, but the general risk (arbitrary keyword collision resolved purely by branch order, not by evidence strength) applies to every pair of branches in the chain.

Confidence values are hard-coded per branch (0.55 for tese, 0.5 for material_curso/anais_evento/artigo_internet, 0.45 for capitulo_livro/relatorio, 0.4/0.3 for the livro fallback depending on whether authors+year are known) and are not derived from how many keywords matched or how strongly — a single keyword hit produces the same confidence as multiple hits. This heuristic confidence is what feeds directly into the `is_complete()` gate before any LLM enrichment runs, and if `biblio_llm_enabled=False` (as in most of the test fixtures), the heuristic confidence is the *final* confidence used for the completeness decision.

A hard override exists: if the incoming `metadata` dict (typically YAML frontmatter on a Markdown source) already carries a `document_type` value that is one of the eight valid `DOCUMENT_TYPES`, that value replaces whatever the heuristic chain determined, and confidence is forced to at least 0.85 — this is the mechanism by which a manually-authored Markdown file with `document_type: capitulo_livro` in its frontmatter skips heuristic guessing entirely and is treated as high-confidence "already known."

**Rule workflow**:
`build_bibliographic_metadata` → `infer_from_file_metadata(metadata, text_sample, filename)` (heuristic seed, possibly overridden by frontmatter) → passed to `enrich_with_llm` as the seed to correct/complete.

---

### Business Rule: `capitulo_livro` field precedence — chapter-level over book-level

**Overview**:
For document type `capitulo_livro` (book chapter), `primary_authors()` and `primary_title()` both prefer the chapter-specific fields (`chapter_authors`, `chapter_title`) over the book-level fields (`authors`, `title`) whenever the chapter fields are non-empty; for every other document type, the book/generic fields are used directly.

**Detailed description**:
This rule exists because a book chapter genuinely has two distinct sets of bibliographic actors — the chapter's own author(s) and the book's organizer(s)/editor(s) (`book_editors`) — and the ABNT citation for a chapter must credit the chapter author, not the book editor, as the primary citable author. `primary_authors()` falls back to `meta.authors` if `chapter_authors` is empty (e.g., the heuristic/LLM only managed to fill the generic `authors` field even though the type was classified as `capitulo_livro`), and similarly `primary_title()` falls back through `meta.title`, and finally to a caller-supplied `fallback` string if both are empty. This same book-vs-chapter distinction is repeated independently inside `_abnt_capitulo()` (`format_authors_abnt(meta.chapter_authors or meta.authors)`), meaning the precedence logic is duplicated in two places rather than centralized — a change to the precedence rule in one location would not automatically propagate to the other (see Technical Debt).

Downstream, `primary_authors()`/`primary_title()` results become the values written into `sources.authors`/`sources.title` in SQLite and the SRC note's `title`/`author` frontmatter — meaning for a `capitulo_livro` source, the *source-level* title in the vault is the chapter's title, not the book's title, even though `book_title` is separately preserved in the biblio JSON and frontmatter for reference formatting.

**Rule workflow**:
`harvester._process_file` → `primary_title(biblio, fallback=...)` / `primary_authors(biblio)` → feeds `citekey` generation, `db.upsert_source(title=..., authors=...)`, and `vault.build_source_note(title=..., authors=...)` — independently, `format_abnt(biblio)` → `_abnt_capitulo(meta)` re-derives the same chapter-vs-book precedence for the full reference string.

---

### Business Rule: Serialization strips empties for compact storage; frontmatter further restricts to a type-appropriate allowlist

**Overview**:
`bibliography_dict()` drops `confidence` entirely and omits any field that is `None`, an empty list, or a blank string, producing the JSON blob stored in `sources.bibliography_json`. `frontmatter_biblio_fields()` additionally removes the four "core" fields (`document_type`, `title`, `authors`, `year` — written explicitly elsewhere) and restricts the result to keys present in the fixed `BIBLIO_FRONTMATTER_FIELDS` tuple.

**Detailed description**:
This two-stage filtering exists because the full `BibliographicMetadata` model has ~28 fields covering all eight document types simultaneously (a book's `publisher`/`isbn` fields coexist in the same model as a thesis's `advisor`/`degree` fields), but any single record only ever populates the subset relevant to its own `document_type`. Storing every populated non-empty field (regardless of whether it's "required" for that type) means optional/type-mismatched-but-present fields still get persisted — e.g. a `livro` record that happens to have a `doi` filled in (unusual but not impossible) keeps that `doi` in `bibliography_json` and, since `doi` is in `BIBLIO_FRONTMATTER_FIELDS`, also surfaces in the SRC note's frontmatter even though `doi` is not part of `REQUIRED_FIELDS["livro"]`. The allowlist in `BIBLIO_FRONTMATTER_FIELDS` (26 entries) is a manually maintained superset covering fields from all eight types combined; it is not derived programmatically from `REQUIRED_FIELDS`, so adding a new field to a document type's model requires remembering to also add it to this allowlist for it to ever reach vault frontmatter (a field could be silently invisible in the vault while still present in SQLite's `bibliography_json` if this list isn't updated in lockstep).

**Rule workflow**:
`build_bibliographic_metadata` result → `harvester._process_file`: `bibliography_dict(biblio)` → `json.dumps` → `db.upsert_source(bibliography_json=...)`; separately `frontmatter_biblio_fields(biblio)` → `vault.build_source_note(biblio_fields=...)` → merged into SRC frontmatter (each non-empty key/value pair added directly to `meta`).

---

### Business Rule: Access-date normalization for `artigo_internet` and `material_curso` references

**Overview**:
`_fmt_accessed()` converts an `accessed_at` string in either ISO (`YYYY-MM-DD`) or slash/dot-delimited (`DD/MM/YYYY` or `DD.MM.YYYY`) format into ABNT's "DD mon. AAAA" display form using a fixed PT-BR month-abbreviation table (`_MONTHS_PT`); any string not matching either regex is returned unmodified.

**Detailed description**:
The `_MONTHS_PT` tuple is 1-indexed with a blank sentinel at index 0 so that `_MONTHS_PT[month]` can be indexed directly by a 1-12 month number without an off-by-one adjustment. Only two input formats are recognized; a value like "June 15, 2024" or a Unix timestamp would fail both regexes and pass through completely unconverted, meaning `_fmt_accessed` provides no correctness guarantee for arbitrary `accessed_at` inputs — it depends entirely on `accessed_at` already being either machine-formatted (ISO, likely from the LLM per the prompt's instruction to prefer `YYYY-MM-DD`) or a common Brazilian slash-date format (likely from manual/heuristic entry). This function is only invoked from `_abnt_artigo_internet` and `_abnt_material_curso` — the only two document types with an `accessed_at` field.

**Rule workflow**:
`format_abnt(meta)` → `_abnt_artigo_internet(meta)` or `_abnt_material_curso(meta)` → `_fmt_accessed(meta.accessed_at)` → appended as "Acesso em: {formatted}." in the final reference string.

---

## 4. Component Structure

```
zettel/
├── bibliography.py                 # This component — single file, ~840 lines
│   ├── DocumentType / DOCUMENT_TYPES / DOCUMENT_TYPE_LABELS   # ABNT type taxonomy
│   ├── REQUIRED_FIELDS                                        # per-type required-field tuples
│   ├── BIBLIO_FRONTMATTER_FIELDS / FIELD_LABELS               # frontmatter allowlist + PT-BR labels
│   ├── BibliographicMetadata (Pydantic BaseModel)              # the typed record (~28 fields)
│   ├── Field helpers: required_fields, missing_required, is_complete,
│   │                  primary_authors, primary_title, bibliography_dict,
│   │                  frontmatter_biblio_fields
│   ├── Author/ABNT formatting: invert_author_name, format_authors_abnt,
│   │                            format_abnt_in_text, display_author_natural,
│   │                            _fmt_accessed, format_abnt (dispatcher) +
│   │                            8 private _abnt_<type> handlers
│   ├── Heuristic inference: _parse_year, infer_from_file_metadata,
│   │                         _str_or_none, _as_str_list
│   └── LLM enrichment: _merge_biblio, enrich_with_llm, _coerce_llm_dict,
│                        build_bibliographic_metadata (top-level orchestrator)
│
├── harvester.py                    # Producer: build_bibliographic_metadata + _resolve_bibliography (HITL)
├── new_note.py                     # Producer: CLI-flag-driven manual scaffold (bypasses BibliographicMetadata)
├── sync.py                         # Producer: adopts frontmatter of hand-edited SRC notes
├── vault.py                        # Renders biblio_fields/abnt_reference into SRC note frontmatter+body
├── article.py                      # Consumer: CatalogSource wraps format_abnt_in_text/display_author_natural
├── state.py                        # Persistence: sources.document_type/bibliography_json/abnt_reference columns
└── config.py                       # HarvestConfig: biblio_confidence_threshold, biblio_llm_enabled,
                                     #                biblio_text_sample_chars

prompts/
└── bibliographic_metadata.md       # LLM prompt template consumed by enrich_with_llm

config/
└── config.yaml                     # Operational values for the three biblio_* HarvestConfig keys

tests/
├── test_bibliography.py            # Dedicated unit + harvest-integration tests for this component
├── test_article.py                 # format_abnt_in_text / display_author_natural / CatalogSource tests
├── test_new_note.py                # Manual scaffold path (document_type/abnt_reference CLI flags)
├── test_harvester_dedup.py         # Harvest dedup tests exercising skip_biblio=True paths
└── test_extraction_dump.py         # HarvestConfig(biblio_llm_enabled=False) fixture usage
```

Note: this is a single-module component with no sub-package structure. All 8 ABNT-type formatters and both field-helper and LLM-enrichment logic live in the one file with only light internal section comments (`# ── Field helpers ──`, `# ── Author / ABNT formatting ──`, `# ── Heuristic inference ──`, `# ── LLM enrichment ──`) delimiting responsibilities.

## 5. Dependency Analysis

```
Internal Dependencies (imports FROM bibliography.py):
bibliography.py → config.AppConfig                      (type hint only, for enrich_with_llm/build_bibliographic_metadata signatures)
bibliography.py → hashing.{compute_llm_call_checksum, normalize_text_for_hash, sha256_hex}
bibliography.py → llm.{call_llm, extract_json, fill_template, get_llm, load_prompt_parts}
bibliography.py → state.StateDB                          (type hint only; used via db.get_cached_llm_response/db.cache_llm_response)
bibliography.py → usage.record_cache_hit                 (deferred import inside enrich_with_llm, avoids import cycle)

Internal Dependencies (imports OF bibliography.py, i.e. consumers):
harvester.py    → bibliography.{build_bibliographic_metadata, bibliography_dict, format_abnt,
                                 frontmatter_biblio_fields, primary_authors, primary_title,
                                 BIBLIO_FRONTMATTER_FIELDS, DOCUMENT_TYPE_LABELS, DOCUMENT_TYPES,
                                 FIELD_LABELS, REQUIRED_FIELDS, BibliographicMetadata,
                                 is_complete, missing_required, required_fields}
                                 (all imports are function-local / deferred, not module-level)
article.py      → bibliography.{display_author_natural, format_abnt_in_text}     (module-level import)

Internal Dependencies (data-contract only, no direct import):
vault.py        → consumes biblio_fields dict / abnt_reference str / document_type str as plain parameters
                   (build_source_note has no dependency on BibliographicMetadata or any bibliography.py symbol)
sync.py         → reconstructs a biblio_payload dict directly from frontmatter keys, independent of
                   BibliographicMetadata / bibliography_dict — a parallel, unvalidated re-implementation
new_note.py     → CLI flags assembled ad hoc into a biblio_fields dict via _collect_biblio_fields();
                   does not import or instantiate BibliographicMetadata at all
state.py        → sql columns document_type / bibliography_json / abnt_reference are opaque TEXT storage;
                   no import of bibliography.py

External Dependencies:
- pydantic (BaseModel, Field)              — BibliographicMetadata schema + validation
- Python stdlib: json, logging, re, pathlib.Path, typing
- LiteLLM / LangChain (via llm.py)         — indirect, only through get_llm/call_llm
- SQLite (via state.py StateDB)            — indirect, only for the LLM response cache table (llm_cache)
```

## 6. Afferent and Efferent Coupling

Analysis unit: functions/classes within `bibliography.py` and their module-level relationships (Python module granularity for cross-file coupling; function granularity for intra-file fan-in of the central orchestrator).

| Component | Afferent Coupling (Ca) | Efferent Coupling (Ce) | Critical |
|-----------|------------------------|-------------------------|----------|
| `bibliography.py` (module) | 4 (harvester.py, article.py, new_note.py — indirectly via contract, sync.py — indirectly via contract) | 4 (config.py, hashing.py, llm.py, state.py) | High — sits on the critical path of every harvest; a break here blocks all ingestion |
| `BibliographicMetadata` (class) | 3 (harvester.py, new_note.py tests, article.py's CatalogSource is structurally similar but does not subclass it) | 0 (pure Pydantic model, no outward calls) | High — schema change ripples to 3 producers + SQLite JSON shape |
| `build_bibliographic_metadata` (function) | 1 (harvester.py, single call site) | 2 (`infer_from_file_metadata`, `enrich_with_llm`) | High — sole public orchestrator entry point |
| `format_abnt` (function) | 2 (harvester.py for SRC note, article.py's academic "Referencias" section reads `abnt_reference` already computed by harvester, not calling `format_abnt` directly — so effectively 1 direct caller) | 8 (one call per `_abnt_<type>` handler) | High — reference-list correctness for every SRC note in the vault |
| `format_abnt_in_text` (function) | 1 (article.py `CatalogSource.in_text_cite`) | 1 (`_author_surname`) | Medium — affects only article generation, not the core harvest pipeline |
| `enrich_with_llm` (function) | 1 (`build_bibliographic_metadata`) | 6 (hashing x3, llm.py x4 combined, state.py x2, usage.py x1) | Medium — highest efferent fan-out in the module; most likely point of failure (network/provider errors), but fails soft |
| `infer_from_file_metadata` (function) | 1 (`build_bibliographic_metadata`) | 3 (`_parse_year`, `_str_or_none`, `_as_str_list`) | Medium |
| `is_complete` / `missing_required` / `required_fields` | 3 each (harvester.py interactive+non-interactive paths, tests) | 1 each (mutually call into `_field_empty`/`REQUIRED_FIELDS`) | High — the harvest go/no-go gate |
| `_merge_biblio` (private) | 1 (`enrich_with_llm`) | 1 (`missing_required`) | Medium — not unit-tested directly (see Test Coverage) |
| `primary_authors` / `primary_title` (functions) | 1 each (harvester.py) | 0 | Medium — duplicated precedence logic vs. `_abnt_capitulo` |

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| LLM provider (via `llm.get_llm`/`call_llm`) | External Service | Enrich heuristic bibliographic seed with a structured JSON extraction | Provider-specific (OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible, per `llm.py`) | JSON (parsed via `extract_json` + `json.loads`) | Broad `except Exception` around the call and around JSON parsing; both degrade to returning the unmodified `seed` with a `logger.warning` — no retry, no partial-failure surfacing to the caller |
| `prompts/bibliographic_metadata.md` | Local file (prompt template) | Supplies the system/user prompt split (`<!-- zettel:user -->`) for LLM enrichment | Filesystem read via `load_prompt_parts` | Markdown with `{placeholder}` template variables | Missing file: caught explicitly (`if not prompt_path.exists()`), logs a warning, returns `seed` unchanged (no exception) |
| `StateDB.llm_cache` table (via `state.py`) | Internal persistence | Deterministic caching of LLM bibliographic-enrichment responses, keyed by `compute_llm_call_checksum` | SQLite (WAL mode) | Cached value is the raw LLM response text (parsed again on every cache hit) | No explicit error handling around cache read/write beyond what `StateDB` itself provides; a cache write failure would propagate as an uncaught exception from `enrich_with_llm` |
| `zettel.usage.record_cache_hit` | Internal (cost tracking) | Records that an LLM call was served from cache (for cost dashboards) | In-process function call (contextvar-backed `CostTracker`) | N/A | Deferred import inside the function body to avoid a circular import between `bibliography.py` and `usage.py` |
| SQLite `sources` table columns (`document_type`, `bibliography_json`, `abnt_reference`) | Internal persistence | Long-term storage of the resolved bibliographic record | SQLite | `bibliography_json` is a JSON string of `bibliography_dict()`'s output; `document_type`/`abnt_reference` are plain TEXT | No validation on read — `sync.py`/`harvester.py` read these columns back as opaque strings; a malformed `bibliography_json` would raise `json.JSONDecodeError` at the read site, not inside this component |
| Vault SRC note frontmatter/body (via `vault.build_source_note`) | Internal persistence (Markdown files) | Human-readable + Obsidian-navigable bibliographic record, including a "## Referencia ABNT" section | Filesystem (YAML frontmatter + Markdown body) | Flat key/value frontmatter pairs (from `frontmatter_biblio_fields`) + plain-text ABNT string | `build_source_note` silently omits any field that's `None`/empty/blank — no error path, just omission |

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Strategy / Dispatch Table | `format_abnt()`'s `handlers` dict mapping `document_type` string → one of 8 `_abnt_<type>` functions | bibliography.py:345-355 | Cleanly separates per-document-type formatting logic without a large if/elif chain; trade-off is a `KeyError` risk for unrecognized types (see Technical Debt) |
| Seed-then-enrich (progressive refinement) | `build_bibliographic_metadata` = `infer_from_file_metadata` (cheap/local) → `enrich_with_llm` (expensive/remote), with `_merge_biblio` reconciling the two | bibliography.py:825-836 | Keeps the pipeline functional (degraded but non-blocking) even when the LLM is disabled, unreachable, or the prompt file is missing |
| Deterministic-cache-backed external call | `compute_llm_call_checksum` + `db.get_cached_llm_response`/`cache_llm_response` around the sole LLM call | bibliography.py:744-789 | Reuses the project-wide LLM caching convention (shared with `connector.py`, `ask.py`) for idempotent re-runs and cost control |
| Typed domain model via Pydantic | `BibliographicMetadata(BaseModel)` with `Literal[DocumentType]` and `Field(default_factory=list)` | bibliography.py:135-173 | Structural validation of LLM JSON output (`model_validate`) and of interactively-edited data (`model_copy(deep=True)` + re-`model_validate` round-trip in `harvester._resolve_bibliography`) |
| Defensive input coercion / adapter | `_coerce_llm_dict` normalizes LLM JSON before Pydantic validation (nulls invalid enums, coerces year/confidence types, maps aliases like `city`→`place`) | bibliography.py:802-822 | Protects the strict Pydantic schema from loosely-typed/aliased LLM output without weakening the schema itself |
| Non-destructive merge (last-writer-wins only on non-empty) | `_merge_biblio` | bibliography.py:701-725 | Prevents an LLM's incomplete response from erasing already-known heuristic data |
| Graceful degradation on optional external dependency | Every failure mode in `enrich_with_llm` (disabled flag, missing prompt, LLM exception, JSON parse exception) returns the unmodified `seed` rather than raising | bibliography.py:736-799 | The harvest pipeline never hard-fails due to bibliographic LLM enrichment specifically — worst case is a lower-confidence record that then hits the separate `is_complete` gate |

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `format_abnt` dispatch (bibliography.py:341-355) | The `handlers[meta.document_type](meta)` call has no `.get()`/default branch; any `document_type` string that is truthy but not one of the 8 known literals raises `KeyError`. The only path where this is reachable in practice is `sync.py`'s adoption of hand-edited SRC frontmatter, which stores `document_type` from the note's YAML with no validation against `DOCUMENT_TYPES` before it round-trips back through anything calling `format_abnt` on that record | A single mistyped `document_type` in a manually edited vault note can crash any later code path that calls `format_abnt` on that source (not evidenced as currently called from `sync.py` itself, but a latent landmine for future callers) |
| Medium | `invert_author_name` (bibliography.py:252-261) | Naive last-whitespace-token-as-surname heuristic; no support for compound surnames, particles ("da", "de", "von"), suffixes, or non-Latin name orders | Author formatting in ABNT reference lists can be silently wrong for a large fraction of real-world Portuguese/Spanish names (a documented, project-wide-acceptable simplification, but unmitigated by any fallback or manual-override path in this module) |
| Medium | `_merge_biblio` (bibliography.py:701-725) | Dead/redundant code: `if llm_meta.document_type: merged.document_type = llm_meta.document_type` duplicates work the preceding generic loop already performed; `merged.confidence = max(merged.confidence, llm_meta.confidence)` immediately follows an equivalent `max()` on the previous line | No functional bug, but the redundancy suggests intent drift or an incomplete prior edit; a reader / future maintainer could reasonably (but incorrectly) assume the two lines do different things |
| Medium | `primary_authors`/`primary_title` (bibliography.py:212-221) vs. `_abnt_capitulo` (bibliography.py:394-423) | The "chapter fields take precedence over book fields for `capitulo_livro`" rule is implemented independently in two places rather than centralized in one helper | A future change to this precedence rule (e.g. adding a third fallback tier) requires remembering to update both call sites; they could silently diverge |
| Medium | Confidence-floor coupling to completeness (bibliography.py:720-724) | `_merge_biblio` boosts `confidence` to >= 0.85 purely because all required fields are non-empty, regardless of whether the LLM or heuristic ever expressed that level of certainty | A record fully "completed" with low-quality or wrong data (e.g. a heuristic guess that happens to fill every slot) can pass the `is_complete()` gate at inflated confidence, undermining the gate's intended purpose of blocking low-quality metadata |
| Low | `sync.py` biblio_payload reconstruction | `sync.py` rebuilds a biblio-like dict directly from a fixed frontmatter-key allowlist, bypassing `BibliographicMetadata`/`bibliography_dict()`/`frontmatter_biblio_fields()` entirely — a second, unvalidated parallel implementation of "what a biblio JSON blob should contain" | The allowlist in `sync.py` and `BIBLIO_FRONTMATTER_FIELDS` in `bibliography.py` must be kept in sync manually; no shared source of truth enforces this today |
| Low | `_fmt_accessed` (bibliography.py:325-338) | Only recognizes ISO (`YYYY-MM-DD`) and `DD/MM/YYYY`/`DD.MM.YYYY` date formats; anything else (including the natural-language dates an LLM might occasionally emit despite prompt instructions) passes through unconverted | Reference-list "Acesso em:" dates can appear in an inconsistent, non-ABNT-compliant format if the LLM or a manual entry deviates from the two supported patterns |
| Low | `new_note.py` manual scaffold path | Bypasses `BibliographicMetadata` entirely — CLI flags flow straight into a raw dict via `_collect_biblio_fields()`, so none of this component's validation (`required_fields`, `is_complete`, type-literal enforcement) applies to manually scaffolded sources | A manually created SRC note can have an inconsistent or incomplete bibliography with no warning, unlike the harvest path which actively gates on completeness |

## 10. Test Coverage Analysis

| Component Area | Unit Tests | Integration Tests | Coverage | Test Quality |
|------------------|------------|---------------------|----------|----------------|
| `invert_author_name` / `format_authors_abnt` | 2 (`test_invert_author_name`, `test_format_authors_abnt_et_al`) — tests/test_bibliography.py:29-36 | 0 | Good for the happy path (multi-word name, single-word name, 4-author et al. collapse) | No negative test for compound surnames or empty-string-only author lists |
| `required_fields` / `missing_required` / `is_complete` | 2 (`test_required_fields_livro`, `test_is_complete_requires_confidence_and_fields`) — tests/test_bibliography.py:39-59 | Indirectly via `test_process_file_skips_incomplete_biblio_noninteractive` / `test_process_file_persists_biblio_with_complete_frontmatter` | Good — covers both the missing-fields path and the confidence-threshold path independently | No direct test for `missing_required` returning `["document_type"]` when the type itself is unset |
| `format_abnt` (all 8 handlers) | 6 direct tests covering `livro`, `artigo_periodico`, `artigo_internet`, `material_curso`, `tese` — tests/test_bibliography.py:65-156 | 1 (`test_build_source_note_includes_abnt_and_fields` exercises the `livro` handler through the vault-note path) | Good breadth but incomplete — **no dedicated test for `_abnt_capitulo`, `_abnt_anais`, or `_abnt_relatorio`** (3 of 8 handlers have zero direct assertions on their output string) | Existing tests use precise substring assertions (e.g. `"2 ed." in ref`, `"DOI: 10.1234/rbia.2021." in ref`), which is a reasonable style for string-builder functions |
| `format_abnt_in_text` / `display_author_natural` | 4 assertions in one test function (`test_format_abnt_in_text_variants`) — tests/test_article.py:112-130 | Indirectly via `CatalogSource.in_text_cite`/`.author_natural` property tests in test_article.py:205-270 | Good — covers 1/2/4-author collapsing and the `pages` argument | No test for the zero-author, zero-year edge case (empty string return) or the "authors present but all blank" edge case |
| `infer_from_file_metadata` (heuristics) | 3 (`test_infer_tese_from_keywords`, `test_infer_artigo_internet_from_url`, `test_infer_respects_frontmatter_document_type`) — tests/test_bibliography.py:162-197 | 0 direct | Adequate for 3 of the 8 branches (tese, artigo_internet, and the frontmatter-override shortcut) | **No tests for the `material_curso`, `anais_evento`, `capitulo_livro`, `artigo_periodico`, `relatorio`, or plain `livro`-fallback heuristic branches** — 5 of 8 keyword-classification paths are untested |
| `enrich_with_llm` / `_merge_biblio` / `_coerce_llm_dict` | **0 direct unit tests** | 0 — every integration test in `tests/test_bibliography.py`'s fixtures explicitly sets `biblio_llm_enabled=False`, so the LLM-enrichment code path is never exercised by the test suite at all | **Poor / untested** — this is the highest-efferent-fan-out, highest-risk function in the module (network call, cache read/write, JSON parsing, exception handling) and has zero coverage of any branch: cache-hit, cache-miss, missing-prompt-file, LLM-exception, or malformed-JSON-response | N/A — no assertions exist to evaluate |
| `bibliography_dict` / `frontmatter_biblio_fields` | 1 (`test_frontmatter_biblio_fields_omits_core`) — tests/test_bibliography.py:230-242 | 1 (`test_build_source_note_includes_abnt_and_fields` implicitly exercises `bibliography_dict`'s empty-stripping via the harvest fixtures) | Adequate for the "omit core fields" contract | No direct test of `bibliography_dict`'s empty-list/blank-string stripping behavior in isolation |
| Harvest integration (`_resolve_bibliography`, `_process_file`) | 0 pure-unit | 5 (`test_resolve_bibliography_noninteractive_skips_without_flag`, `test_resolve_bibliography_noninteractive_allows_with_skip_biblio`, `test_process_file_skips_incomplete_biblio_noninteractive`, `test_process_file_persists_biblio_with_complete_frontmatter`, `test_process_file_skip_biblio_persists_partial`) — tests/test_bibliography.py:287-388; further exercised (with `skip_biblio=True` shortcuts) in tests/test_harvester_dedup.py and tests/test_extraction_dump.py | Good for the non-interactive gate logic (skip/allow/persist-partial) | **No test exercises the interactive HITL branch of `_resolve_bibliography`** (Rich `Prompt`/`Confirm` flow, lines 980-1130) — that ~150-line block (type-selection menu, required-field fill-in loop, optional-field fill-in loop, re-preview/confirm) has zero automated coverage anywhere in the test suite found |
| Manual scaffold path (`new_note.py`) | 0 (this component's model is bypassed) | 3 (`test_scaffold_source_with_biblio_and_explicit_citekey`, `test_scaffold_source_source_id_flag`, `test_scaffold_source_sync_manual_adopts`) — tests/test_new_note.py:63-136 | Adequate for the flat-dict CLI-flag path, but since this path never touches `BibliographicMetadata`, it provides no coverage of this component's own validation logic | Confirms end-to-end frontmatter/body rendering and `sync-manual` adoption round-trip |

Overall assessment: the pure, deterministic formatting functions (author inversion, et al. collapsing, in-text citation, 5 of 8 ABNT handlers) are well tested with precise assertions. The two highest-risk areas with essentially no test coverage are (1) the LLM-enrichment code path (`enrich_with_llm`/`_merge_biblio`/`_coerce_llm_dict`) — every fixture in the test suite disables it — and (2) the interactive HITL editing flow inside `harvester._resolve_bibliography`, which is a substantial, business-logic-dense block that only runs when a human is present at a terminal and is therefore inherently hard to exercise without mocking `rich.prompt.Prompt`/`Confirm`, which no test currently does.

---

**Component analyzed**: `bibliography`
**Report path**: `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-bibliography-2026-08-30_10-22-26.md`
