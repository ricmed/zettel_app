# Component Deep Analysis Report — `harvester` (Phase 1: Harvest)

Analysis date: 2026-08-30
Component under analysis: `zettel/harvester.py` (paired with `zettel/paging.py`)
Analyzed by: Component Deep Analysis (read-only)

---

## 1. Executive Summary

`harvester.py` (1895 lines) plus its tightly-coupled sibling `paging.py` (251 lines) implement **Phase 1** of the Zettelkasten pipeline (`harvest → extract → review → connect → garden`). Its job is to turn raw files dropped in `data/inbox/` (PDF or Markdown) into the first durable artifacts of the system: a `SRC` note, an empty `20_Literature` index note, and a set of content-addressed, page-annotated text chunks persisted in SQLite (`StateDB`) and embedded into ChromaDB (`VectorIndex`). Every later phase (`extract`, `review`, `connect`, `garden`) operates exclusively on the chunks and sources this component produces.

The component is process-oriented (a single public entry point, `run_harvest`, orchestrating a long private call chain) rather than object-oriented — there is exactly one class defined (`HarvestAborted`, a control-flow exception). Its core responsibilities are:

1. **File discovery** — scan the inbox for supported extensions, or process a single `selected_file` (used by the web UI).
2. **Text extraction** — Docling (preferred, GPU-aware) or PyMuPDF fallback for PDF; native parsing + YAML frontmatter for Markdown.
3. **Three-layer duplicate detection** — file hash → extraction hash → semantic (ChromaDB) similarity — to avoid reprocessing the same intellectual content under a different filename or format.
4. **Bibliographic metadata resolution** — delegates to `bibliography.py` but owns the HITL confirmation/edit flow and the non-interactive `--skip-biblio` escape hatch.
5. **Citekey generation** — deterministic, tiered, collision-safe.
6. **Structural chunking** — H1/H2 chapters → H3-H6 sections → LangChain recursive splitter fallback, all content-addressed for idempotent re-runs.
7. **Page inference and content-start paging** — maps PDF/file pages to printed book pages so citations in later phases are human-usable (delegated heavily to `paging.py`).
8. **Vault + database persistence** — writes `SRC`/`LIT index` notes (via `vault.py`), embeds sources/chunks (via `index.py`), and tracks LLM/embedding cost (via `usage.py`).
9. **Resilience/repair utilities** — `run_rechunk` (reprocess persisted text under current chunking config), `run_set_paging` (patch paging without re-calling any LLM), `source_chunking_incomplete`/`_complete_incomplete_source` (detect and finish interrupted harvests).

**Key findings**:
- `_process_file` (harvester.py:527-816) is a ~290-line orchestration function with very high efferent coupling (see Section 6) — it is the component's single largest complexity and maintenance risk.
- The component is carefully designed for **idempotency and crash-resilience**: content-addressed chunk/chapter IDs, checksum-gated skips, an explicit "incomplete chunking" detector, and an ordering rule that writes vault notes *before* the potentially slow embedding step (harvester.py:707-711).
- Duplicate detection is genuinely three distinct algorithms with different cost/certainty tradeoffs, each independently tested (`tests/test_harvester_dedup.py`).
- Page/paging logic is cleanly separated into `paging.py`, which is pure and well unit-tested (`tests/test_paging.py`), in contrast to the harvester's imperative, side-effecting core.
- Interactive (Rich console) and non-interactive code paths are interleaved throughout `_process_file`, `_resolve_bibliography`, `_resolve_duplicate_decision`, and `_resolve_content_paging`, which is a deliberate design choice (CLI supports both HITL and scripted/web use) but increases branch complexity.

---

## 2. Data Flow Analysis

```
1. run_harvest(cfg, db, idx, ...) called from cli.py `harvest` command or web_app.py
   (operations "harvest" / "run_all")
2. Compute pipeline_signature (chunking + harvest + images config + pdf_extractor +
   docling_config_hash) -> db.start_run() opens a `runs` row; usage.begin_run() opens
   a cost-tracking context.
3. Resolve file list: cfg.inbox_path.rglob(*) filtered by SUPPORTED_EXTENSIONS,
   or a single `selected_file` (must resolve inside inbox).
4. For each file -> _process_file(...):
   a. file_sha256(file_path) computed; compare against `files` table.
      - Unchanged file -> skip (unless chunking was left incomplete by a prior
        interrupted run, in which case _complete_incomplete_source resumes it).
   b. Layer 1 dedupe: db.get_file_by_checksum() (renamed copy) -> reuse source_id,
      no reprocessing, db.record_duplicate(run_id, "file").
   c. _extract_text(cfg, file_path, origin_type):
      - PDF -> _extract_pdf -> _extract_pdf_docling (GPU-aware; extracts images via
        assets.extract_docling_images; builds page_map via PyMuPDF) or
        _extract_pdf_pymupdf (fallback; builds page_map inline).
      - Markdown -> _extract_markdown (YAML frontmatter -> title/authors/year/biblio
        fields; assets.extract_markdown_images for inline images).
   d. extraction_checksum = sha256_hex(normalize_text_for_hash(text)).
      Layer 2 dedupe: db.get_source_by_extraction_checksum() (cross-format identical
      content) -> reuse source, db.record_duplicate(run_id, "content").
   e. bibliography.build_bibliographic_metadata() + _resolve_bibliography()
      (HITL confirm/edit, or --skip-biblio in non-interactive mode) -> BibliographicMetadata
      or None (file skipped).
   f. _generate_citekey(db, authors, year, title) -> tiered, collision-checked citekey;
      source_id = f"@{citekey}".
   g. If source already exists with identical extraction_checksum -> skip rechunk
      (but still resume incomplete chunking if flagged).
   h. _split_into_chapters(text, origin_type) (H1/H2 split).
   i. Layer 3 dedupe: _find_semantic_duplicate_candidates() samples chunks, queries
      idx.find_similar_chunks(); above cfg.harvest.duplicate_chunk_threshold ->
      _resolve_duplicate_decision() (skip / continue / abort). "abort" raises
      HarvestAborted, unwound by run_harvest's try/except.
   j. _resolve_content_paging(page_map, ...) -> ContentPaging (content_start_file_page,
      content_start_book_page, confidence) — HITL prompt, CLI flags, or skip-paging
      default.
   k. db.upsert_file / db.upsert_source / db.update_source_texts(extracted_text) ->
      persisted BEFORE embeddings start.
   l. _create_vault_notes(...) -> vault.build_source_note + build_literature_index_note
      -> safe_write_note() writes SRC (10_Sources/) and LIT index (20_Literature/).
   m. idx.upsert_source(...) — source-level embedding into ChromaDB `sources` collection.
   n. assets.register_assets(db, source_id, chapters, images) if images were extracted.
   o. _chunk_and_persist(cfg, db, idx, source_id, chapters, page_map, paging):
      - Per chapter: skip if chapter_checksum unchanged; else _split_chapter_into_chunks
        (structural H3-H6 + LangChain RecursiveCharacterTextSplitter fallback);
        content-addressed chunk_id; page hint via paging.lookup_page_for_chunk +
        paging.extract_page_hint; stale chunks for a changed chapter removed from
        SQLite + ChromaDB.
      - Cross-chapter: paging.apply_page_inference() fills unknown pages by
        interpolation; chunks whose page_in_file < content_start_file_page are
        DELETED (not persisted); remaining chunks get page_in_book via
        paging.compute_page_in_book(); db.upsert_chunk() + idx.upsert_chunk()
        (embeddings only generated for genuinely new chunk_ids).
   p. _finalize_source_chunking() -> _prune_orphan_chapters (chapters absent from the
      current split are deleted, cascading their chunks) + assets.reresolve_asset_chapters.
   q. db.update_source_paging(..., processing_status="completed", total_chunks=N).
   r. usage.get_tracker() cost delta -> db.add_source_usage(); _create_vault_notes()
      called again to refresh SRC frontmatter with final page/cost totals.
5. run_harvest aggregates stats (text_len, chapters, chunks) and returns new source_ids.
6. usage.finish_pipeline_run(db, run_id, status) closes the `runs` row (completed/aborted).
```

Two side entry points bypass most of this and operate on already-persisted `extracted_text`:
- `run_rechunk` — re-derives chapters from `sources.extracted_text` and re-runs step (o)-(p) only, under the *current* chunking config; used when `chunking`/`min_section_chars` config changes.
- `run_set_paging` — does not touch `extracted_text` or chapters at all; only recomputes `page_in_book` on already-persisted chunks, drops out-of-range ones, renames granular LIT files, and patches vault frontmatter (no LLM call).

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Validation | Only `.pdf`, `.md`, `.markdown`, `.txt` are scanned from inbox | harvester.py:44, 115-118 |
| Validation | `selected_file` must resolve inside `cfg.inbox_path` | harvester.py:107-112 |
| Business Logic | Layer 1 dedupe: identical file bytes (different path) reuse `source_id` | harvester.py:562-575 |
| Business Logic | Layer 2 dedupe: identical normalized extracted text (cross-format) reuses `source_id` | harvester.py:587-600 |
| Business Logic | Layer 3 dedupe: semantic near-duplicate via ChromaDB, threshold-gated, skip/continue/abort | harvester.py:841-930 |
| Business Logic | Similarity derived from Chroma L2 distance as `1 - distance/2` | harvester.py:870 |
| Validation | Bibliographic metadata must be "complete" (or explicitly overridden) before SRC is written | harvester.py:933-1130 |
| Business Logic | Tiered citekey generation with collision suffixing (`a`,`b`,`c`...) | harvester.py:1415-1451 |
| Business Logic | Unchanged extraction checksum on an existing source skips rechunking | harvester.py:636-643 |
| Business Logic | Structural chunking hierarchy: H1/H2 chapters → H3-H6 sections → recursive splitter | harvester.py:1457-1584 |
| Business Logic | Small sections (< `min_section_chars`) are merged forward/backward | harvester.py:1525-1553 |
| Business Logic | Content-addressed chunk IDs collapse identical normalized text within a chapter | harvester.py:1642-1652 |
| Business Logic | Three-layer page inference: explicit page map → regex → interpolation | paging.py:67-143 |
| Business Logic | Content-start paging resolution (HITL / CLI flags / heuristic / skip) | harvester.py:1761-1831, paging.py:165-186 |
| Validation | Chunks whose `page_in_file` precedes `content_start_file_page` are discarded | harvester.py:1693-1710 |
| Business Logic | `page_in_book = page_in_file - start_file + start_book`; `None` if before start | paging.py:146-162 |
| Business Logic | Chapter-level incremental reprocessing by checksum; stale chunks pruned | harvester.py:1633-1668 |
| Business Logic | Orphan chapter pruning after a full chunk pass | harvester.py:459-481 |
| Business Logic | `docling_config_hash` mismatch on an unchanged file logs a rechunk warning (no auto-action) | harvester.py:551-556, paging.py:222-235 |
| Business Logic | `set-paging` drops `pending` chunks before new start unconditionally; other statuses only with `--drop-before-start` | harvester.py:262-282 |
| Business Logic | SRC + LIT index notes are written to the vault *before* embeddings are generated | harvester.py:707-711 |
| Business Logic | PDF extraction fallback chain: Docling → PyMuPDF on `ImportError`/exception | harvester.py:1258-1263 |
| Business Logic | Markdown YAML frontmatter is mined for title/authors/year/bibliographic fields | harvester.py:1339-1409 |
| Business Logic | Cost/token usage is accumulated per source and per run | harvester.py:775-788 |
| Business Logic | `HarvestAborted` unwinds the whole inbox loop but preserves already-processed sources | harvester.py:47-48, 144-147 |
| Business Logic | Image extraction is opt-in (`images.enabled`) with minimum-dimension filtering | config.py:93-106 (consumed via `assets.py`) |

### Detailed breakdown of the business rules

---

### Business Rule: Three-Layer Duplicate Detection

**Overview**:
Before any new source is created, `_process_file` runs three independent, increasingly expensive checks to determine whether the incoming file represents intellectual content already present in the vault. Each layer is cheaper and more certain than the next, and each is recorded distinctly via `db.record_duplicate(run_id, layer)` so `zettel status` can report how duplicates were caught.

**Detailed description**:
Layer 1 (harvester.py:562-575) compares the raw file's SHA-256 (`file_sha256`) against every previously seen path via `db.get_file_by_checksum(checksum, exclude_path=...)`. A hit means the same bytes exist under a different filename — a renamed or copied file — so the new path is simply associated with the existing `source_id` (`db.upsert_file`) and no extraction, chunking, or embedding work is repeated. This is the cheapest and most certain layer because it requires no text extraction at all.

Layer 2 (harvester.py:587-600) only runs after text has been extracted, because it operates on `extraction_checksum = sha256_hex(normalize_text_for_hash(text))`. `normalize_text_for_hash` (hashing.py) applies NFKC normalization, whitespace collapsing, and PDF dehyphenation, so a PDF export and a Markdown export of the *same* underlying document normalize to identical text and are detected as duplicates even though their raw bytes and origin_type differ completely. This is the mechanism referenced in the module docstring's "same paper re-exported PDF vs. Markdown" example. A hit reuses the existing source and skips SRC/LIT/chunk generation entirely.

Layer 3 (harvester.py:841-930) is the only probabilistic layer and the only one requiring a human or policy decision. `_sample_chunk_texts` reuses the exact same structural chunker that will eventually persist the file's chunks (guaranteeing the sample reflects what would actually be embedded), then evenly distributes `cfg.harvest.duplicate_sample_size` (default 5) samples across the document. `idx.find_similar_chunks` queries ChromaDB's `chunks` collection for near neighbors of each sample; results are converted from L2 distance to a `[0,1]`-ish similarity via `1 - distance/2` and aggregated by `source_id`, keeping only the *best* (highest) similarity per candidate source, filtered to `>= cfg.harvest.duplicate_chunk_threshold` (default 0.88). If any candidates survive, `_resolve_duplicate_decision` either prompts interactively (Rich table + `skip`/`continuar`/`abortar` choice) or applies the non-interactive policy: an explicit `duplicate_action` argument, or else `cfg.harvest.non_interactive_duplicate_action` (default `"skip"`). "abort" raises `HarvestAborted`, which `run_harvest` catches to stop the whole inbox loop (marking the run `"aborted"`) while preserving whatever sources were already committed in earlier iterations.

**Rule workflow**:
```
new file -> file_sha256
  -> [Layer 1] known checksum at different path? --yes--> reuse source_id, stop
  -> extract text -> extraction_checksum
  -> [Layer 2] known extraction_checksum? --yes--> reuse source_id, stop
  -> split into chapters -> sample chunks -> query Chroma
  -> [Layer 3] best-per-source similarity >= threshold?
       --no--> proceed as new source
       --yes--> interactive? --yes--> Rich prompt (pular/continuar/abortar)
                --no--> duplicate_action or config default
                skip -> stop (no source created)
                continue -> proceed as new source (duplicate recorded but ignored)
                abort -> raise HarvestAborted (stops entire harvest run)
```

---

### Business Rule: Bibliographic Metadata Resolution and Confirmation

**Overview**:
A file cannot become a persisted `SRC` note without bibliographic metadata that is either judged "complete" by `bibliography.is_complete()` or explicitly force-accepted by the operator (interactively) or by `--skip-biblio` (non-interactively).

**Detailed description**:
`build_bibliographic_metadata` (delegated to `bibliography.py`, out of scope for this component but a direct dependency) infers a `BibliographicMetadata` object from file metadata, extracted text, and optionally an LLM call. `_resolve_bibliography` (harvester.py:933-1130) then owns what happens next. `is_complete(biblio, cfg.harvest.biblio_confidence_threshold)` (default `0.7`) gates whether the metadata is trusted as-is. In non-interactive mode: complete metadata is accepted silently; incomplete metadata is accepted only if `skip_biblio=True` (logged as a warning naming the missing fields), otherwise the file is skipped entirely (`return None`) — the harvest run continues to the next file rather than failing.

In interactive mode the flow is materially richer: a Rich table preview is always shown (even when data is already complete), because the design intent is that a human should be able to see and correct what the pipeline inferred before an SRC note and its downstream embeddings are committed. If the metadata is complete, the user is still asked to confirm; declining routes into an edit path. The edit path branches on whether `document_type` is set/confident: it may prompt for document type selection from `DOCUMENT_TYPES`, then walks `missing_required(meta)` fields one by one with type-aware coercion (comma-split lists for `authors`/`chapter_authors`/`book_editors`, `int()` parsing for `year` with fallback to `None` on `ValueError`), and finally offers to fill optional fields. If required fields remain missing after editing, the user is asked "Continue anyway?" — declining aborts just that file (returns `None`), accepting force-raises `meta.confidence` to the threshold so it passes `is_complete` on any later inspection.

This rule matters architecturally because it is the *only* place bibliographic quality is enforced — once a `BibliographicMetadata` clears `_resolve_bibliography`, every downstream consumer (citekey generation, ABNT formatting, SRC frontmatter) trusts it unconditionally. It is also the reason `harvest` cannot be made fully autonomous without either high extraction confidence or `--skip-biblio`: a file with ambiguous authorship/year/type will silently stall the pipeline (skipped, not erroring) rather than produce a poorly-cited note.

**Rule workflow**:
```
biblio = build_bibliographic_metadata(...)
complete = is_complete(biblio, threshold)
interactive?
  no -> complete? --yes--> accept
              --no--> skip_biblio? --yes--> accept (warn) --no--> return None (file skipped)
  yes -> show preview
         complete? --yes--> confirm? --yes--> accept (confidence raised to threshold)
                                      --no--> fall through to edit path
                    --no--> edit path
         edit path: maybe pick document_type -> fill missing required fields
                     -> still missing? confirm "continue anyway?" --no--> return None
                     -> optionally fill optional fields -> final confirm --no--> return None
                     -> accept
```

---

### Business Rule: Tiered Citekey Generation

**Overview**:
`_generate_citekey` (harvester.py:1415-1451) produces a short, human-readable, collision-free identifier (`source_id = f"@{citekey}"`) from whatever subset of author/year/title metadata is actually available.

**Detailed description**:
The function first extracts a `surname` from the first author's last whitespace-delimited token (if any authors exist), then branches into four tiers based on which of `has_author`/`has_year` booleans are true. With both author and year, the base is `{surname}{year}{2-word title slug}` (e.g. `Silva2023EstruturasDados`); with author only, `{surname}{3-word slug}`; with year only, `{year}{3-word slug}`; with neither, a `{4-word slug}` alone. Each slug is built by stripping non-word characters from the title (`re.sub(r"[^\w\s]", "", title)`), splitting on whitespace, taking the first N words, and capitalizing each — falling back to the literal string `"Untitled"` if no words survive. The differing word counts per tier (2/3/3/4) are a deliberate compensation: the more identifying metadata is missing, the more of the title is folded into the key to keep it distinguishable from other sparse-metadata citekeys.

Collision resolution is a simple linear probe: while `db.get_source_by_citekey(citekey)` returns a hit, an alphabetic suffix is appended (`chr(96 + suffix_idx)` → `a`, `b`, `c`, ...). This means citekeys are stable only as long as the underlying title/author/year metadata doesn't change and no earlier-processed source with the same base key is later deleted and reprocessed out of order — there is no persistence of "reserved" keys beyond what already exists in the `sources` table. This function is a pure, easily unit-testable candidate (it only reads via `db.get_source_by_citekey`, no writes), though it is currently only exercised indirectly through `_process_file` integration tests, not directly.

**Rule workflow**:
```
surname = last token of first author's name (or "")
has_author = bool(surname); has_year = year is not None
tier = (has_author, has_year) -> word_count in {2,3,3,4} and template
slug = capitalize(first `word_count` alnum-stripped words of title) or "Untitled"
base = template(surname, year, slug)
citekey = base
while db.get_source_by_citekey(citekey) exists:
    citekey = base + next_letter_suffix
return citekey
```

---

### Business Rule: Structural Chunking Hierarchy (Chapters → Sections → Chunks)

**Overview**:
Text is decomposed in three nested passes — H1/H2 "chapters" (`_split_into_chapters`), H3-H6 "sections" with hierarchical path tracking (`_split_chapter_into_sections`), and finally size-bounded "chunks" (`_split_chapter_into_chunks`) — before anything is persisted or embedded.

**Detailed description**:
`_split_into_chapters` (harvester.py:1457-1480) is intentionally shallow: it only looks at `#`/`##` headings. Any text before the first such heading becomes an implicit `"Introdução"` chapter with locator `"preâmbulo"`; if there are no headings at all, the entire document becomes a single `"Documento completo"` chapter. This top level exists primarily to give `_chunk_and_persist` a stable, checksum-able unit of incremental work — a chapter whose text hasn't changed (by SHA-256 of its NFKC-normalized text) is skipped entirely on re-harvest/rechunk, which is what makes large-document reprocessing cheap.

`_split_chapter_into_sections` (harvester.py:1486-1522) then walks H3-H6 headings *within* a chapter using an explicit level stack, building a `" > "`-joined `section_path` (e.g. `"Capitulo 1 > Sub A > Sub A1"`) that survives into chunk metadata and ultimately into citation locators (`paging.format_source_locator`). Any section shorter than `cfg.chunking.min_section_chars` (default 200) is merged into the *following* section by `_merge_small_sections`, except a trailing short section, which is appended to the *previous* kept section instead — this avoids both leading and trailing "crumb" chunks that would otherwise carry too little context to be useful to the LLM extraction step.

Finally, `_split_chapter_into_chunks` (harvester.py:1556-1584) accepts each section as-is if it already fits `cfg.chunking.chunk_size` (default 1000 chars), or further divides it using `langchain_text_splitters.RecursiveCharacterTextSplitter` with `chunk_overlap` (default 200) and separators `["\n\n", "\n", ". ", " ", ""]`. Each resulting chunk is content-addressed: `chunk_id = f"{source_id}::{chapter_id}::{short_hash(chunk_checksum)}"`, where `chunk_checksum` is the SHA-256 of the *normalized* chunk text. This means two structurally different sections that happen to produce byte-identical normalized text collapse into a single stored/embedded chunk (verified by `test_chunk_and_persist_collapses_identical_content`), which both saves embedding cost and prevents duplicate LIT drafts downstream in `extract`.

**Rule workflow**:
```
text -> chapters (H1/H2 split; preamble -> "Introdução"; no headings -> single chapter)
for each chapter (skip if chapter_checksum unchanged):
    -> sections (H3-H6 split; hierarchical section_path; small sections merged)
    for each section:
        -> fits chunk_size? keep as one chunk : RecursiveCharacterTextSplitter.split_text()
        -> chunk_id = hash(chapter_id, normalized chunk text)
        -> identical chunk_id within chapter deduped (keep first occurrence)
```

---

### Business Rule: Three-Layer Page Inference

**Overview**:
`paging.py` resolves `page_in_file` for every chunk through three fallback layers of decreasing certainty — explicit page map, regex on chunk text, interpolation between known neighbors — documented explicitly in the module docstring (paging.py:1-11).

**Detailed description**:
The preferred source of truth is an explicit page map: a list of `(page_no, page_text)` tuples built either from Docling's page boundaries (recovered separately via PyMuPDF, since Docling's exported Markdown loses page breaks — harvester.py:1241-1251) or natively from PyMuPDF's per-page text (`build_page_map_from_texts`). `lookup_page_for_chunk` (paging.py:194-219) matches only the *first* ~120 characters of a chunk against each page's normalized text, so a chunk that spans a page boundary is deliberately attributed to the page where it *begins*, not where it ends or where most of its content lives — this is called out explicitly in both the docstring and the CLI's own paging prompt copy ("Chunk que cruza paginas usa a pagina do inicio do trecho"). When no exact substring match is found, a weaker word-overlap heuristic (shared-word count against the first 2000 characters of a page, requiring `score >= max(3, min_overlap // 10)`) picks a best-effort page.

When no page map exists at all (e.g., neither PyMuPDF nor Docling metadata provided one), `extract_page_hint` (paging.py:67-93) falls back to regex over the chunk's head/tail 200 characters, using four patterns tuned for common PDF page-number renderings (bare numbers on their own line, "Página N", or a number followed by a word). This regex fallback is explicitly disabled (`allow_regex=False`) whenever a page map *is* available, specifically to avoid false positives from stray numbers in the body — such as table-of-contents entries or in-text numbered references like "2 EPILOGUE" — being misread as the chunk's actual file page.

The final layer, `apply_page_inference` (paging.py:128-143) and its helper `infer_missing_page` (paging.py:96-125), runs once per `_chunk_and_persist` batch over all pending chunk specs: any chunk still lacking an explicit page after the first two layers gets a page linearly interpolated between the nearest chunks (in ordinal position) that *do* have explicit pages, rounded to the nearest integer; a chunk with no explicit neighbor on one side simply inherits the nearest known page. Every chunk's `page_confidence` field (`"explicit"` / `"inferred"` / `"unknown"`) is a first-class, persisted signal — later phases and vault frontmatter surface this so a reader can judge how much to trust a given citation locator.

**Rule workflow**:
```
for each chunk in a chapter's chunk batch:
    meta_page = page_map lookup (first-page-of-chunk substring match, or word-overlap fallback)
    hint = meta_page is not None ? explicit : (page_map exists ? unknown : regex-on-head/tail)
batch-wide:
    for each still-unknown hint: interpolate between nearest explicit neighbours -> "inferred"
    (no neighbours on either side -> stays "unknown")
```

---

### Business Rule: Content-Start Paging Resolution

**Overview**:
Before any chunk is persisted, the harvester must decide which file page the intellectual "content" of a document begins on, and what printed page number that corresponds to — because front matter (cover, TOC, preface) should not pollute chunking/citation and printed page numbers rarely equal PDF page indices.

**Detailed description**:
`_resolve_content_paging` (harvester.py:1761-1831) is the single decision point, called once per file after extraction but before chunking. If `--skip-paging` is passed with no explicit `content_start_file`, it short-circuits to `ContentPaging(1, 1, "skipped")` — meaning every page is processed and book page equals file page. Otherwise it first computes a heuristic suggestion via `paging.suggest_content_start`, which scans the page map for the first page whose first ~1200 characters match any of several regexes for "Capítulo 1" / "Chapter 1" / "1. " / "Introduction" / "Introdução" (paging.py:33-44, 165-186) — if found, confidence is `"heuristic"`; otherwise it defaults to file page 1 with `confidence="none"`.

If the caller passed explicit `--content-start-file`/`--content-start-book` (from the CLI or the web job payload), those values win unconditionally and are marked `confidence="confirmed"` — this is the only path that produces "confirmed" paging without going through the interactive prompt. In non-interactive mode without explicit flags, the function deliberately does **not** trust the heuristic automatically: it falls back to `ContentPaging(1, 1, "skipped")`, i.e. processing everything from page 1 with book page equal to file page — a conservative default that never silently drops content the heuristic might have mis-detected. Only in interactive mode does the heuristic surface as a *suggested default* in a Rich prompt, which the operator can accept or override; whatever the operator enters becomes `confidence="confirmed"`.

This resolved `ContentPaging` then feeds two downstream effects: (1) any chunk whose inferred `page_in_file` is strictly less than `content_start_file_page` is deleted from the pending batch entirely (harvester.py:1693-1710) — front matter never becomes a chunk, concept, or note; and (2) every surviving chunk's `page_in_book` is computed via `paging.compute_page_in_book`. The `zettel set-paging` command (`run_set_paging`, harvester.py:215-400) exists specifically to correct a wrong initial guess *after the fact*, without re-running extraction or any LLM call — it recomputes `page_in_book` on existing chunks, drops now-out-of-range `pending` chunks unconditionally, optionally drops `awaiting_review`/`approved` ones too (`--drop-before-start`), renames granular LIT files whose page token changed, and patches both SQLite and vault frontmatter.

**Rule workflow**:
```
skip_paging and no explicit start? -> ContentPaging(1,1,"skipped")
explicit content_start_file given? -> ContentPaging(explicit, explicit_or_1, "confirmed")
else:
    suggested = heuristic scan of page_map for chapter/intro markers
    skip_paging or non-interactive -> use heuristic if found else (1,1,"skipped")
    interactive -> prompt operator with heuristic as default -> ContentPaging(answer, answer, "confirmed")
downstream:
    chunk.page_in_file < start_file -> chunk discarded (not persisted, not embedded)
    else -> chunk.page_in_book = page_in_file - start_file + start_book
```

---

### Business Rule: Incremental Reprocessing and Interrupted-Harvest Recovery

**Overview**:
The component is designed to be safely re-run against an unchanged inbox (a no-op), a changed inbox (only reprocess what changed), and an inbox where a previous run was interrupted mid-chunking (finish exactly the missing part, without repeating already-embedded work or re-invoking any LLM/bibliography step).

**Detailed description**:
At the file level, `_process_file` first checks whether `file_sha256` matches the last-seen checksum for that path (harvester.py:547-560); if so, it is normally a pure no-op skip. But immediately before returning, it checks `source_chunking_incomplete(db, sid)` (harvester.py:403-413), which recomputes chapters from the persisted `extracted_text` and compares their expected `chapter_id` set against what's actually in the `chapters` table (`_chapters_fully_persisted`, harvester.py:429-436). If any expected chapter is missing, the file is *not* treated as fully processed — `_complete_incomplete_source` (harvester.py:484-521) is invoked instead, which re-derives chapters and paging bounds from already-persisted source fields and calls `_chunk_and_persist` to finish exactly the missing chapters, without touching bibliography, citekey, or file/extraction-checksum dedupe logic at all. This exact same "resume" path is reachable from Layer 1 and Layer 2 duplicate hits too (harvester.py:557-558, 573-574), so an interrupted harvest can be completed even if the operator's next action is to feed in a renamed copy or a cross-format re-export of the same file.

At the chapter level, `_chunk_and_persist` (harvester.py:1590-1758) checksums each chapter's normalized text and skips re-splitting/re-embedding entirely for unchanged chapters (harvester.py:1633-1636) — this is what makes `run_rechunk` (re-deriving chapters from persisted text under a *new* chunking config) cheap for documents where most chapters didn't structurally change. When a chapter's checksum *has* changed, its old chunks are deleted from both SQLite and ChromaDB (`db.delete_chunks_for_chapter`, keeping only chunk_ids in the freshly computed `keep_ids` set) before new ones are inserted, guaranteeing no stale chunk from a superseded chapter version lingers. A final cross-cutting step, `_finalize_source_chunking` → `_prune_orphan_chapters` (harvester.py:459-481), removes any chapter row whose `chapter_id` is no longer expected at all (e.g. a heading was deleted from the source document), cascading the deletion of that chapter's chunks from both stores — this is the mechanism that keeps `list_incomplete_sources`/`source_chunking_incomplete` accurate as a detector rather than accumulating false positives from genuinely-removed content.

The net effect is that three distinct "the file arrived again" scenarios — unchanged, changed, and previously-interrupted — are all funneled through the same checksum-gated chapter/chunk persistence logic, and none of them re-invokes the (potentially costly, HITL-dependent) bibliography resolution or citekey generation steps once a `source_id` already exists for that content.

**Rule workflow**:
```
file checksum unchanged?
  yes -> chunking complete for this source? yes -> skip (no-op)
                                             no  -> _complete_incomplete_source (resume chunking only)
  no  -> Layer 1/2 dedupe hit? -> same resume check as above
       -> new source path: full pipeline (biblio, citekey, chapters, chunking)
chapter checksum unchanged (within _chunk_and_persist)? -> skip re-split/re-embed for that chapter
chapter checksum changed? -> delete its old chunks (SQLite + Chroma) -> insert new ones
chapter no longer present in current split at all? -> _prune_orphan_chapters deletes it + its chunks
```

---

### Business Rule: Vault-Write-Before-Embedding Ordering

**Overview**:
`SRC` and the empty `LIT` index note are deliberately written to the vault immediately after `db.upsert_source`/`db.update_source_texts`, *before* the (potentially minutes-long) embedding of chunks begins (harvester.py:707-726), and then rewritten a second time at the very end with final totals (harvester.py:791-808).

**Detailed description**:
The comment at harvester.py:707-711 states this explicitly: "Grava SRC + indice LIT ANTES dos embeddings (podem demorar minutos)" — write SRC + LIT index before the embeddings, because they can take minutes. This is a resilience decision: if the process crashes, is killed, or hits an unrecoverable embedding-provider error partway through `_chunk_and_persist`, the vault already has a discoverable `SRC` note and a citekey-linked `LIT` index for that source, and `source_chunking_incomplete` will correctly detect and let a future `harvest`/`rechunk` run resume without re-asking for bibliography or re-generating the citekey.

The first write uses `processing_status="in_progress"` and omits `total_chunks`/`total_pages_book`/final cost figures (none of which are known yet). After chunking, orphan-pruning, and paging finalization complete, `_create_vault_notes` is called a second time with `processing_status="completed"`, `total_chunks=chunk_count`, computed `total_pages_book`, and the cost/token totals pulled from the active `CostTracker` via `usage.get_tracker().summary_for_source(source_id)`. Both calls go through the same `_create_vault_notes` function and the same `vault.safe_write_note`, which is documented elsewhere in the codebase as never clobbering manual edits outside managed blocks — so this two-phase write pattern is safe even if a human has already started annotating the SRC note's body between the two writes (though in practice the interval is normally seconds to low minutes within a single harvest run).

This rule is also why `db.update_source_texts(source_id, extracted_text=text)` happens *before* chunking starts (harvester.py:704) rather than after: persisting `extracted_text` early is precisely what allows `run_rechunk` and `_complete_incomplete_source` to reconstruct chapters without needing the original file to still be present or the extraction step (Docling/PyMuPDF) to be re-run.

**Rule workflow**:
```
extract text -> persist extracted_text to SQLite
-> write SRC (processing_status=in_progress) + LIT index note (vault)
-> upsert source-level embedding
-> register assets (if images)
-> chunk_and_persist (chapters -> sections -> chunks; embeddings per new chunk)
-> finalize (prune orphans, re-resolve asset chapters)
-> compute total_pages_book, pull cost tracker summary
-> re-write SRC (processing_status=completed, totals filled) + LIT index note (again)
```

---

### Business Rule: PDF Extraction Fallback Chain

**Overview**:
PDF text extraction always attempts the configured extractor first (`cfg.pdf_extractor`, typically `"docling"`) and falls back to PyMuPDF on `ImportError` or any other exception, so a missing/broken Docling installation degrades gracefully rather than failing the harvest.

**Detailed description**:
`_extract_pdf` (harvester.py:1170-1174) is a one-line dispatcher; the substantive logic is in `_extract_pdf_docling` (harvester.py:1177-1263). It builds a `DocumentConverter` with GPU/CPU acceleration chosen via `config.detect_device(cfg.device)`, optionally enables picture-image generation (`cfg.images.enabled`), converts the PDF, and exports Markdown. Two `except` clauses guard this: an `ImportError` (Docling not installed) logs a warning and calls `_extract_pdf_pymupdf` directly; any other `Exception` during conversion logs an error and does the same. This means a corrupt PDF, an unsupported PDF feature, or a Docling internal bug does not abort the harvest for that file — it silently degrades to plain-text extraction via PyMuPDF, which is far less structurally rich (no heading detection from layout, no image extraction) but robust.

Because Docling's exported Markdown loses explicit page boundaries, `_extract_pdf_docling` separately (and independently of whether the PyMuPDF fallback path is taken) tries to build a page map via `_pymupdf_page_map` purely for page-inference purposes (harvester.py:1241-1251) — this is wrapped in its own bare `except Exception` and only logged at `debug` level, since a missing page map merely downgrades page confidence to regex/interpolation rather than failing anything. Metadata enrichment follows a similar belt-and-suspenders pattern: Docling's `result.document.origin` is consulted first for title/author/date, and `_enrich_metadata_from_pymupdf` (harvester.py:1316-1336) is called afterward only to fill in whatever Docling didn't provide (`if not metadata["authors"] or not metadata["year"]`) — it never overwrites values Docling already supplied.

The PyMuPDF-only fallback path (`_extract_pdf_pymupdf`, harvester.py:1266-1302) itself has a final guard: if PyMuPDF is *also* not installed, it returns an empty string with minimal metadata rather than raising, which `_process_file` catches via `if not text.strip(): ... return None, empty_stats` (harvester.py:581-583) — the file is skipped with a warning rather than crashing the whole `harvest` run.

**Rule workflow**:
```
pdf_extractor == "docling"?
  yes -> try Docling conversion
           ImportError or Exception -> fallback to PyMuPDF extraction
         (independently) try building a PyMuPDF page map for page inference (best-effort)
         enrich missing author/year/title from PyMuPDF metadata (never overwrites Docling values)
  no  -> PyMuPDF extraction directly
PyMuPDF unavailable too? -> return "" (caller skips the file with a warning, no crash)
```

---

### Business Rule: Cost and Token Usage Accumulation

**Overview**:
Every `harvest` run and every source processed within it accumulates estimated LLM and embedding cost/token totals, surfaced both in the `runs` table and mirrored onto the `SRC` note's frontmatter.

**Detailed description**:
`run_harvest` opens a cost-tracking context via `usage.begin_run(run_id)` immediately after `db.start_run(signature)` and closes it via `usage.finish_pipeline_run(db, run_id, run_status)` regardless of whether the run completed normally or was aborted (the `finally`-like placement outside the try/except ensures this always runs). Within `_process_file`, `usage.set_source(source_id)` scopes subsequent cost attribution (LLM calls made during bibliography enrichment, embedding calls made during chunk/source upserts) to that specific source via Python contextvars, and is explicitly reset to `None` on every exit path (skip due to layer-2 dedupe, skip due to semantic dedupe, and normal completion) so that costs from one file are never misattributed to the next.

After chunking completes, `usage.get_tracker()` is queried for a `summary_for_source(source_id)` delta, which is persisted via `db.add_source_usage(source_id, delta)` — an additive accumulation rather than an overwrite, so partial/resumed processing (e.g. `_complete_incomplete_source` finishing a chapter set in a later run) correctly sums cost across multiple passes rather than losing earlier figures. The resulting totals (`cost_usd_total`, `cost_usd_llm`, `cost_usd_embedding`, `tokens_prompt`, `tokens_completion`, `tokens_embedding`) are read back from `db.get_source(source_id)` and passed into the second `_create_vault_notes` call so the `SRC` note's frontmatter reflects real spend — this is the same field set independently maintained by `vault.sync_source_costs_to_vault` for other phases.

Because SQLite-cached LLM calls (`llm_cache` table hits) are documented elsewhere as costing `$0`, and Ollama/unknown-model calls also cost `$0`, this accumulation is best read as an *estimate* using LiteLLM's public price map rather than a ledger of actual billed dollars — a distinction the report notes because it affects how much weight should be placed on the persisted cost figures for budgeting purposes.

**Rule workflow**:
```
run_harvest: usage.begin_run(run_id) -> ... -> usage.finish_pipeline_run(db, run_id, status)
per file: usage.set_source(source_id) at start of processing
          usage.set_source(None) on every exit path (dedupe skip, biblio skip, semantic skip, completion)
on completion: tracker.summary_for_source(source_id) -> db.add_source_usage() (additive)
             -> re-read totals from db -> pass into final _create_vault_notes() call
```

---

## 4. Component Structure

```
zettel/harvester.py                    # Phase 1 orchestration (1895 lines)
├── HarvestAborted (Exception)         # control-flow signal to stop run_harvest's inbox loop
├── Public API
│   ├── run_harvest()                  # main entry: scan inbox -> process each file -> aggregate stats
│   ├── run_rechunk()                  # reprocess persisted extracted_text under current chunking config
│   ├── run_set_paging()               # repair page_in_book on existing chunks, no LLM re-call
│   ├── source_chunking_incomplete()   # detector: persisted chapters vs. current H1/H2 split
│   └── list_incomplete_sources()      # all sources failing the above detector
├── Incomplete-harvest recovery
│   ├── _expected_chapter_ids() / _chapters_fully_persisted()
│   ├── _maybe_dump_chunks() / _maybe_dump_extraction()   # opt-in debug exports
│   ├── _finalize_source_chunking() / _prune_orphan_chapters()
│   └── _complete_incomplete_source()
├── File Processing
│   └── _process_file()                # per-file orchestrator: dedupe -> biblio -> citekey -> paging
│                                       #   -> vault notes -> chunking -> cost tracking (~290 lines)
├── Semantic Duplicate Detection
│   ├── _sample_chunk_texts()
│   ├── _find_semantic_duplicate_candidates()
│   └── _resolve_duplicate_decision()  # Rich-prompt or non-interactive policy
├── Bibliography HITL
│   └── _resolve_bibliography()        # confirm/edit flow around bibliography.py's inference (~200 lines)
├── Year Extraction Helpers
│   ├── _extract_year_from_pdf_date()
│   └── _extract_year_from_string()
├── Text Extraction
│   ├── _extract_text() / _extract_pdf()
│   ├── _extract_pdf_docling()         # GPU-aware, image extraction, page-map via PyMuPDF
│   ├── _extract_pdf_pymupdf()         # fallback extractor
│   ├── _pymupdf_page_map()
│   ├── _enrich_metadata_from_pymupdf()
│   └── _extract_markdown()            # YAML frontmatter -> title/authors/year/biblio fields
├── Citekey Generation
│   └── _generate_citekey()
├── Chapter/Section/Chunk Splitting
│   ├── _split_into_chapters()         # H1/H2
│   ├── _split_chapter_into_sections() # H3-H6, hierarchical section_path
│   ├── _merge_small_sections()
│   └── _split_chapter_into_chunks()   # LangChain RecursiveCharacterTextSplitter fallback
├── Chunking + Persistence
│   └── _chunk_and_persist()           # content-addressed IDs, page inference, SQLite + Chroma writes
├── Content-Start Paging Resolution
│   └── _resolve_content_paging()      # HITL / CLI flags / heuristic / skip
└── Vault Note Creation
    └── _create_vault_notes()          # SRC + LIT index via vault.py builders

zettel/paging.py                       # Page inference + content-start helpers (251 lines, pure functions)
├── PageHint (dataclass)
├── ContentPaging (dataclass)          # .page_offset property
├── extract_page_hint()                # metadata-first, optional regex fallback
├── infer_missing_page() / apply_page_inference()   # interpolation
├── compute_page_in_book()
├── suggest_content_start()            # heuristic Capítulo 1/Introduction detection
├── build_page_map_from_texts()
├── lookup_page_for_chunk()            # substring + word-overlap matching
├── compute_docling_config_hash()      # invalidation signature for ingestion knobs
└── format_source_locator()            # human-readable "p.N / section > path" builder
```

---

## 5. Dependency Analysis

```
Internal Dependencies (compile-time imports):

zettel.harvester
├── zettel.config           (AppConfig, detect_device)
├── zettel.hashing           (file_sha256, normalize_text_for_hash, sha256_hex, short_hash)
├── zettel.index             (VectorIndex — type hint + methods called)
├── zettel.paging            (ContentPaging, apply_page_inference, build_page_map_from_texts,
│                              compute_docling_config_hash, compute_page_in_book, extract_page_hint,
│                              lookup_page_for_chunk, suggest_content_start)
├── zettel.state             (StateDB — type hint + methods called)
├── zettel.vault             (build_literature_index_note, build_source_note,
│                              literature_index_filename, source_note_filename, safe_write_note,
│                              literature_chunk_filename_for_row, parse_frontmatter, compose_note)
├── zettel.bibliography      (lazy import inside _process_file / _resolve_bibliography:
│                              BibliographicMetadata, build_bibliographic_metadata,
│                              bibliography_dict, format_abnt, frontmatter_biblio_fields,
│                              primary_authors, primary_title, is_complete, missing_required,
│                              required_fields, DOCUMENT_TYPES, DOCUMENT_TYPE_LABELS,
│                              FIELD_LABELS, REQUIRED_FIELDS, BIBLIO_FRONTMATTER_FIELDS)
├── zettel.usage             (lazy import: begin_run, finish_pipeline_run, get_tracker, set_source)
├── zettel.progress          (lazy import: report())
├── zettel.assets            (lazy import: register_assets, reresolve_asset_chapters,
│                              extract_docling_images, extract_markdown_images)
├── zettel.chunk_dump        (lazy import: dump_source_chunks — opt-in debug export)
├── zettel.extraction_dump   (lazy import: dump_source_extraction — opt-in debug export)
└── zettel.review            (lazy import inside run_set_paging: _refresh_literature_index)

zettel.paging
└── zettel.config            (AppConfig — used only by compute_docling_config_hash)

Callers of zettel.harvester (afferent, internal):
├── zettel.cli               (harvest, rechunk, dump-chunks, dump-extraction, set-paging,
│                              status, doctor commands)
└── zettel.web_app           (WebApplication._dispatch: operations "harvest" and "run_all")

External Dependencies:
- docling (DocumentConverter, PdfPipelineOptions, AcceleratorOptions) — primary PDF extraction,
  GPU-accelerated Markdown export + optional picture-image generation. Import-guarded (fallback
  to PyMuPDF on ImportError).
- pymupdf (fitz) — fallback PDF text/metadata extractor AND the sole source of per-page text used
  to build page maps even when Docling is the primary extractor (Docling's Markdown export loses
  page boundaries). Import-guarded.
- langchain_text_splitters.RecursiveCharacterTextSplitter — size-bounded fallback chunk splitter
  for sections exceeding chunk_size.
- rich (Console, Prompt, Confirm, Table) — all interactive HITL surfaces (bibliography confirm/edit,
  duplicate-decision prompt, content-start paging prompt). Not import-guarded; a hard dependency
  whenever `interactive=True`.
- PyYAML (yaml.safe_load) — Markdown frontmatter parsing in _extract_markdown.
- ChromaDB (via zettel.index.VectorIndex) — semantic near-duplicate queries (find_similar_chunks),
  chunk/source embedding upserts.
- SQLite (via zettel.state.StateDB) — files/sources/chapters/chunks/runs persistence.
```

---

## 6. Afferent and Efferent Coupling

Coupling is measured at the function level (the natural "component" unit for this procedural module — there is only one class, `HarvestAborted`, which is a stateless exception with zero coupling of interest).

| Component (function) | Afferent Coupling | Efferent Coupling | Critical |
|---|---|---|---|
| `_process_file` | ~2 (`run_harvest`, direct test calls) | ~28 (dedupe layers, bibliography module, citekey, extraction, paging, vault notes, chunking, asset registration, usage tracking) | High |
| `_chunk_and_persist` | ~4 (`_process_file`, `run_rechunk`, `_complete_incomplete_source`, tests) | ~17 (StateDB read/write, VectorIndex read/write, `paging.*`, `_split_chapter_into_chunks`, hashing) | High |
| `run_set_paging` | ~2 (`cli.set_paging_cmd`, tests) | ~15 (StateDB paging/chunk methods, vault frontmatter I/O, `review._refresh_literature_index`, `_create_vault_notes`, `paging.compute_page_in_book`) | Medium |
| `_resolve_bibliography` | ~2 (`_process_file`, tests) | ~12 (8+ `bibliography.py` symbols, 4 `rich` primitives) | Medium |
| `_extract_pdf_docling` | 1 (`_extract_pdf`) | ~9 (docling classes, `config.detect_device`, `assets.extract_docling_images`, `_enrich_metadata_from_pymupdf`, `_pymupdf_page_map`, `_extract_year_from_string`) | Medium |
| `run_harvest` | 2 (`cli.harvest`, `web_app._dispatch`) | ~7 (`_process_file`, `_maybe_dump_chunks`, `StateDB.start_run`, `usage.begin_run`/`finish_pipeline_run`, `progress.report`) | High |
| `run_rechunk` | 2 (`cli.rechunk`, tests) | ~8 (`StateDB.get_source`/`list_sources`, `_split_into_chapters`, `ContentPaging`, `_pymupdf_page_map`, `_chunk_and_persist`, `_finalize_source_chunking`, `_maybe_dump_chunks`) | Medium |
| `_find_semantic_duplicate_candidates` | ~2 (`_process_file`, tests) | ~4 (`_sample_chunk_texts`, `idx.find_similar_chunks`, `db.get_source`, `cfg.harvest.*`) | Medium |
| `_split_chapter_into_chunks` | ~3 (`_chunk_and_persist`, `_sample_chunk_texts`, tests) | ~2 (`_split_chapter_into_sections`, LangChain splitter) | Medium |
| `_split_into_chapters` | ~5 (`_process_file`, `run_rechunk`, `_complete_incomplete_source`, `source_chunking_incomplete`, tests) | ~1 (`re`) | Medium |
| `_generate_citekey` | ~1 (`_process_file`) + tests indirectly | ~2 (`db.get_source_by_citekey`, `re`) | Low |
| `_resolve_duplicate_decision` | ~1 (`_process_file`) + tests | ~3 (rich primitives, logging) | Low |
| `_create_vault_notes` | ~2 (`_process_file` twice, `run_set_paging`) | ~4 (`vault.build_source_note`, `vault.build_literature_index_note`, `vault.safe_write_note`, `vault.compose_note`) | Medium |
| `paging.extract_page_hint` | ~2 (`_chunk_and_persist`, tests) | 0 (pure) | Low |
| `paging.compute_page_in_book` | ~3 (`_chunk_and_persist`, `run_set_paging`, tests) | 0 (pure) | Low |
| `paging.suggest_content_start` | ~2 (`_resolve_content_paging`, tests) | 0 (pure) | Low |

**Interpretation**: `_process_file` and `_chunk_and_persist` are the component's central hubs — both high afferent-relative-to-module-size (they are *the* entry points other logic composes through) and very high efferent coupling (they reach into nearly every other subsystem: config, hashing, state, index, vault, bibliography, assets, usage). This is expected for a pipeline orchestrator but is also where a future refactor would have the highest payoff — see Section 10. By contrast, `paging.py`'s exported functions are uniformly low-efferent (many are pure functions with zero side effects), which is why they were straightforward to unit test in isolation (`tests/test_paging.py`) without any database or filesystem fixtures.

---

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|---|---|---|---|---|---|
| Docling | External Library | Primary PDF text/structure extraction, GPU-accelerated, optional image extraction | In-process Python API | PDF bytes in, Markdown text + document object out | `ImportError` and generic `Exception` both fall back to PyMuPDF; logged at warning/error level |
| PyMuPDF (`pymupdf`) | External Library | Fallback PDF extraction; sole source of per-page text for page maps regardless of primary extractor | In-process Python API | PDF bytes in, per-page text + metadata dict out | `ImportError` returns empty text (caller skips file); other extraction errors not separately caught (would propagate) |
| ChromaDB (via `VectorIndex`) | Internal Service (embedded) | Source/chunk embedding storage; semantic near-duplicate queries | In-process API (Chroma client) | Text + metadata dict (str/int/float/bool only, via `_sanitize_metadata`) | No explicit retry in harvester; failures during embedding would propagate and could leave a chunk persisted in SQLite but not embedded (recoverable via re-run, since `idx.existing_ids` re-checks) |
| SQLite (`StateDB`) | Internal Database | Durable state for files/sources/chapters/chunks/runs | In-process SQLite (WAL mode) | Structured rows via `state.py` methods | No transaction wrapping visible around the full `_process_file` sequence — a crash mid-function can leave partially-written rows, which is exactly what `source_chunking_incomplete` is designed to detect and repair on the next run |
| Obsidian Vault (filesystem, via `vault.py`) | Internal Filesystem | Durable, human-readable `SRC`/`LIT index` notes | Markdown files with YAML frontmatter | Frontmatter dict + Markdown body | `safe_write_note` never overwrites manual edits outside managed blocks; no explicit retry on I/O errors |
| LangChain (`langchain_text_splitters`) | External Library | Fallback size-bounded text splitting for oversized sections | In-process Python API | Plain strings in/out | No error handling wraps this call; a splitter exception would propagate up through `_process_file` |
| Rich (`rich.console`/`rich.prompt`) | External Library | All interactive HITL prompts (bibliography, duplicate decision, content-start paging) | Terminal stdio (stderr console) | Text prompts/tables | Malformed numeric input (e.g. paging page numbers) caught via `try/except ValueError`, falling back to the suggested default |
| PyYAML | External Library | Markdown frontmatter parsing | In-process | YAML string → dict | Wrapped in a broad `except Exception: fm_meta = {}` — any YAML error silently discards frontmatter rather than failing the file |
| LiteLLM cost map (via `usage.py`/`pricing.py`) | External Library (indirect) | Estimated USD cost attribution for LLM/embedding calls made during bibliography enrichment | In-process | Token counts → cost estimate | Not directly handled in harvester; consumed as an opaque `CostTracker` summary |

---

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---|---|---|---|
| Strategy / Fallback Chain | `_extract_pdf_docling` → `_extract_pdf_pymupdf` on import/runtime failure | harvester.py:1170-1263 | Degrade gracefully when the preferred PDF extractor is unavailable or fails, without aborting the harvest |
| Content-Addressable Storage | `chunk_id = f"{source_id}::{chapter_id}::{short_hash(chunk_checksum)}"`, `chapter_id = f"{source_id}::ch{i:03d}"` | harvester.py:1627, 1645; hashing.py | Idempotent re-runs; identical content collapses to identical IDs, enabling cheap change detection and dedup |
| Checksum-Gated Incremental Processing | Chapter/chunk checksums compared before re-splitting/re-embedding | harvester.py:1633-1636, 636-643 | Avoid redundant LLM/embedding cost on unchanged content across repeated harvest/rechunk runs |
| Layered Validation / Chain of Responsibility | Three-layer duplicate detection (file → extraction → semantic) | harvester.py:562-887 | Cheapest, most certain checks run first; only fall through to expensive semantic search when necessary |
| Command Pattern (implicit, via Typer) | `run_harvest`/`run_rechunk`/`run_set_paging` each map 1:1 to a CLI subcommand | cli.py harvest/rechunk/set-paging commands | Decouples orchestration logic from the CLI framework; same functions reused by `web_app.py` |
| Two-Phase Commit-ish Write Ordering | SRC/LIT written before embeddings, then rewritten with final totals | harvester.py:707-726, 791-808 | Crash resilience: partial progress is discoverable and resumable |
| Observer / Progress Reporting | Optional `observer` parameter threaded through `run_harvest` → `progress.report()` | harvester.py:67, 120-129 | Decouples CLI console output from web job progress tracking without the harvester depending on either concretely |
| Dataclass Value Objects | `PageHint`, `ContentPaging` | paging.py:47-64 | Immutable-by-convention carriers for page/paging state, with `ContentPaging.page_offset` as a derived property |
| Lazy/Deferred Imports | `bibliography`, `usage`, `assets`, `chunk_dump`, `extraction_dump`, `review` imported inside functions rather than at module top | throughout harvester.py | Avoids import-time cost/circularity for subsystems only needed on specific code paths (e.g. `chunk_dump` only if `--dump-chunks` was passed) |
| HITL / Non-Interactive Dual-Mode Branching | Every user-facing decision point (`_resolve_bibliography`, `_resolve_duplicate_decision`, `_resolve_content_paging`) takes an `interactive: bool` and a non-interactive override/default | harvester.py throughout | Single code path serves both the interactive CLI and the always-non-interactive web job queue |

---

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|---|---|---|---|
| High | `_process_file` | ~290-line function with ~28 efferent dependencies mixing I/O, HITL prompting, dedupe policy, and persistence in one call chain | Hard to test in isolation without broad fixtures (as evidenced by the dedup tests needing a `FakeVectorIndex` stand-in); any change risks unintended interaction between unrelated concerns (e.g. a bibliography change affecting paging resolution ordering) |
| Medium | `_process_file` / `_chunk_and_persist` | No visible transaction boundary around the multi-step SQLite write sequence (`upsert_source` → `update_source_texts` → per-chapter `upsert_chapter`/`upsert_chunk` → `update_source_paging`) | A crash between steps can leave the source in a state where `processing_status="in_progress"` persists indefinitely if not for the separately-maintained `source_chunking_incomplete` detector — that detector is a compensating control, not a guarantee (it only checks chapter/chunk coverage, not paging/cost field consistency) |
| Medium | `_resolve_content_paging` (non-interactive branch) | When `skip_paging=False` but `interactive=False` and no explicit `content_start_file` is given, the heuristic suggestion is silently discarded in favor of `(1, 1, "skipped")` (harvester.py:1786-1788) | A non-interactive run (e.g. from the web UI, which always sets `interactive=False`) can never benefit from the "Capítulo 1" heuristic unless the caller explicitly forwards `content_start_file`/`content_start_book` — this is a deliberate conservative default per its own comment, but it means front-matter pages are *never* auto-excluded for web-submitted harvests unless the operator manually supplies the paging flags via `set-paging` afterward |
| Medium | `lookup_page_for_chunk` (paging.py) | Word-overlap fallback score threshold (`score >= max(3, min_overlap // 10)`) is a fixed heuristic constant with no configuration surface | Corpus-specific tuning (e.g. very short chunks, or documents with heavy word repetition) cannot be adjusted without a code change; mis-attributed pages degrade to `page_confidence="explicit"` even though the match was approximate (the function does not distinguish exact substring hits from word-overlap hits in its return value) |
| Medium | `_extract_markdown` | Frontmatter YAML errors are swallowed via a bare `except Exception: fm_meta = {}` | A malformed (but present) frontmatter block silently loses ALL bibliographic hints (title/authors/year included) rather than surfacing a warning to the operator, potentially triggering an avoidable `--skip-biblio` skip or citekey degradation |
| Low | `_generate_citekey` | No persisted "reservation" of a citekey before the source is actually written; the collision check queries `db.get_source_by_citekey` at generation time only | A race condition is only theoretically possible (the CLI/web job queue design elsewhere documents single-mutating-job-at-a-time), but the function itself carries no defensive locking, so it would not be safe to reuse concurrently |
| Low | Docling config hash mismatch handling | Detecting `docling_config_hash` drift on an unchanged file only logs a warning suggesting `rechunk --source-id` (harvester.py:551-556); nothing is done automatically | An operator who changes chunking-relevant config and re-runs `harvest` (rather than `rechunk`) on already-harvested files will see no rechunking occur and must notice the log line themselves |
| Low | Test coverage gap | `_generate_citekey`, `_extract_markdown`, `_extract_pdf_docling`/`_extract_pdf_pymupdf`, `_resolve_content_paging`, `_resolve_bibliography`, and `run_set_paging`'s LIT-rename/frontmatter-patch branch have no dedicated unit tests found under `tests/` (see Section 11) | Regressions in citekey tiering, frontmatter parsing edge cases, or the PDF extractor fallback chain would only surface via integration-level or manual testing |

---

## 10. Test Coverage Analysis

| Component (module/function area) | Unit Tests | Integration Tests | Coverage | Test Quality |
|---|---|---|---|---|
| Three-layer duplicate detection (`_process_file`, `_find_semantic_duplicate_candidates`, `_resolve_duplicate_decision`, `_sample_chunk_texts`) | 10 (`tests/test_harvester_dedup.py`) | Implicit (tests drive `_process_file` end-to-end against a real `StateDB` + `FakeVectorIndex`) | Good — all three layers, both dedupe outcomes (skip/continue/abort), threshold boundary, and aggregation-by-source all covered | Strong: uses a lightweight `FakeVectorIndex` stand-in to avoid real embeddings, asserts on `run["duplicate_*_count"]` fields as well as return values, covers the `HarvestAborted` exception path explicitly |
| Structural chunking (`_split_into_chapters`, `_split_chapter_into_sections`, `_merge_small_sections`, `_split_chapter_into_chunks`, `_chunk_and_persist`, `run_rechunk`, `source_chunking_incomplete`) | 13 (`tests/test_harvester_sections.py`) | Yes — `_chunk_and_persist`/`run_rechunk` tests use a real `StateDB` + `_FakeIdx` | Good — covers hierarchical section paths, small-section merge-forward and merge-backward, oversized-section splitting, content-dedup collapse, orphan-chunk removal on changed text, incomplete-chunking detection and resumption via `run_rechunk` | Strong: `test_incomplete_chunking_detected_and_rechunk_completes` specifically simulates an interrupted harvest (partial chapters + a mis-pointed asset) and verifies full recovery — a genuinely valuable regression test for the resilience design |
| Page inference (`paging.py`: `extract_page_hint`, `infer_missing_page`, `apply_page_inference`, `compute_page_in_book`, `suggest_content_start`, `lookup_page_for_chunk`, `format_source_locator`, `compute_docling_config_hash`) | 12 (`tests/test_paging.py`) | N/A (pure functions, no DB/filesystem needed) | Very good — every exported function in `paging.py` has at least one direct test, including edge cases (`before_start` returning `None`, interpolation midpoints, regex-disabled mode) | Strong: fast, isolated, no fixtures required; a model example of testing pure logic separated from I/O |
| `run_set_paging` (paging repair) | 1 dedicated test file, ≥1 test (`test_run_set_paging_updates_book_and_drops_pending`) + `tests/test_set_paging_filter.py` (`test_chunk_and_persist_skips_pages_before_content_start`) | Yes | Moderate — the primary "update + drop pending" path is covered; the LIT-file-rename branch (harvester.py:301-311), the `--drop-before-start` branch for `awaiting_review`/`approved` chunks, and the SRC-frontmatter-patch branch (harvester.py:344-388) are not clearly exercised based on file names/sizes alone (129 and 69 lines respectively — likely 1-3 tests each) | Needs verification — recommend confirming the rename-on-page-change and drop-before-start behaviors have explicit assertions, given they involve filesystem side effects (`path.replace`) that are easy to regress silently |
| Bibliographic metadata resolution (`_resolve_bibliography`) | 0 found directly in `tests/test_bibliography.py` (that file tests `bibliography.py`'s own functions: `is_complete`, `missing_required`, ABNT formatting, etc., not the harvester's HITL wrapper) | None found | Gap | The ~200-line interactive/non-interactive confirm-edit flow in harvester.py (933-1130) appears to have no direct test coverage; only its inputs/outputs (`bibliography.py` primitives) are unit-tested elsewhere |
| Citekey generation (`_generate_citekey`) | 0 found | Indirect only, via `_process_file` dedupe tests (which pass `skip_biblio=True` and implicitly exercise citekey generation as a side effect) | Gap | No test directly asserts the four-tier template logic, the capitalization/slug-building rules, or the collision-suffix (`a`/`b`/`c`) behavior |
| Text extraction (`_extract_pdf_docling`, `_extract_pdf_pymupdf`, `_extract_markdown`, `_enrich_metadata_from_pymupdf`, `_pymupdf_page_map`, year-extraction helpers) | 0 found | None found | Gap | No test file targets these directly; `_extract_markdown`'s frontmatter parsing (including the `_BIBLIO_KEYS` pass-through list) and both PDF extraction fallback paths are entirely uncovered by the automated suite as far as could be located |
| Chunk/extraction debug dumps (`_maybe_dump_chunks`, `_maybe_dump_extraction`) | Covered indirectly via `tests/test_chunk_dump.py` (243 lines) and `tests/test_extraction_dump.py` (227 lines), which test the delegated `chunk_dump.py`/`extraction_dump.py` modules rather than the harvester wrapper functions themselves | N/A | Good (for the delegated modules); the harvester's thin wrapper functions (`_maybe_dump_chunks`/`_maybe_dump_extraction`, 8 lines each) are trivial enough that this indirect coverage is adequate |
| Web job integration (`web_app.py` "harvest"/"run_all" operations calling `run_harvest`) | Covered via `tests/test_web_state.py` (135 lines) at the job-queue level | Yes | Moderate — exercises the dispatch/payload plumbing; does not appear to substitute a fake extractor, so it likely relies on small fixture files rather than exercising Docling/PyMuPDF code paths directly |

**Overall assessment**: The best-tested areas are exactly the ones the module's own docstrings flag as intentionally complex and resilience-critical — duplicate detection, structural chunking/incremental reprocessing, and page inference — all three have thorough, well-isolated unit tests with realistic fake collaborators. The weakest-tested areas are the HITL-heavy and I/O-heavy edges: bibliography confirmation/editing, citekey generation, and the PDF/Markdown extraction functions themselves (which would need file fixtures or mocked `docling`/`pymupdf` modules to test without real dependencies). `run_set_paging`'s filesystem side effects (LIT file renames, frontmatter patch) also warrant a closer look to confirm they are exercised beyond the primary happy path.

---

*End of report.*
