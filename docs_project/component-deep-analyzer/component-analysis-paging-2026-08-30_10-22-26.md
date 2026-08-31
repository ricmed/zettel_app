# Component Deep Analysis Report — `paging`

Analysis date: 2026-08-30
Component under analysis: `zettel/paging.py` (252 lines)
Primary consumers analyzed for context: `zettel/harvester.py`, `zettel/extractor.py`, `zettel/cli.py`, `zettel/web.py`, `zettel/web_app.py`
Analyzed by: Component Deep Analysis (read-only)

---

## 1. Executive Summary

`zettel/paging.py` is the page-inference and content-start-paging engine of the harvester pipeline (Phase 1). Its single responsibility is to answer two related questions for every chunk of extracted text: *"which physical page of the source file does this text come from?"* and *"what printed page number would a human reader see there?"* It also backs the resilience command `zettel set-paging`, which lets an operator correct a book's page mapping after the fact without re-invoking any LLM.

The module is a **pure, side-effect-free functional core**: no file I/O, no database access, no network calls, and only one external dependency (`zettel.config.AppConfig`, used solely to build a config-invalidation hash). Every function is a deterministic transformation of its inputs. All I/O, persistence, and HITL (human-in-the-loop) prompting is delegated to its consumers — chiefly `zettel/harvester.py`, which is the only "imperative shell" that calls it, plus a light touch from `zettel/extractor.py` for display formatting.

Three closely related sub-problems are solved by clearly separated functions:

1. **`page_in_file` resolution** — a three-tier fallback chain: explicit page metadata (from a PyMuPDF page map) → regex pattern matching on chunk text → linear interpolation between neighbouring known pages (`extract_page_hint`, `infer_missing_page`, `apply_page_inference`).
2. **Content-start detection and book-page mapping** — heuristically suggesting where "real" content begins in a PDF (skipping cover/TOC/preface) and converting file pages to printed book pages via a simple offset formula (`suggest_content_start`, `compute_page_in_book`, `ContentPaging`).
3. **Chunk-to-page attribution and display formatting** — matching a chunk's opening text against a full page map (`lookup_page_for_chunk`) and rendering a human-readable locator string for citations (`format_source_locator`).

A fourth, unrelated responsibility — `compute_docling_config_hash` (a stable hash of ingestion/chunking config used to detect stale mid-flight resumes) — also lives in this file; it has nothing to do with page inference and is a cohesion outlier (see Technical Debt §10).

**Key findings**:

- The module is thoroughly and directly unit-tested (`tests/test_paging.py`, 11 tests covering nearly every public function's happy path), but several of its own edge cases (extrapolation-only-one-side, empty page maps, hash sensitivity to changed config) are untested.
- A genuine **dead-code / logic bug** was found in the module's sole consumer of its heuristic suggestion path: `harvester._resolve_content_paging` (harvester.py:1782-1789) computes a heuristic-based content-start suggestion for non-interactive harvests, but an unconditional early return one line above it means that branch can never execute given the current call graph. In practice, **any non-interactive `zettel harvest` run (including the web UI's default harvest job and `run-all`) silently ignores the Capítulo-1/Introduction heuristic and always assumes file page 1 = printed page 1**, unless `--content-start-file` is passed explicitly. This directly affects the correctness of `page_in_book` values used throughout the vault (citations, ZTL `literature_ref`, ABNT locators). This is documented in detail in Business Rule 7 and Technical Debt Risk 1.
- The module cleanly implements the same "layered, cheaper-to-more-certain fallback" architectural idiom used elsewhere in this codebase (cf. harvester's three-layer duplicate detection, retrieval.py's relevance floor) — each page-resolution layer is tried only when the previous, more trustworthy layer is unavailable.
- `paging.py` itself has essentially zero efferent coupling to the rest of the system (stdlib + one config type); all coupling is inbound (afferent) from `harvester.py` and, to a lesser degree, `extractor.py`. `cli.py`, `state.py`, `vault.py`, and `web.py`/`web_app.py` never import it directly — they only persist/relay the integer fields it produces.

---

## 2. Data Flow Analysis

Two related flows exist: (A) the per-chunk page inference during harvest/rechunk, and (B) the content-start resolution that gates which chunks are even kept. Both funnel into the same output fields (`page_in_file`, `page_in_book`, `page_confidence`) that later phases (`extractor.py`, `vault.py`, `set-paging`) consume.

**A. Per-chunk page inference (inside `harvester._chunk_and_persist`, harvester.py:1590-1758)**

```
1. PDF extraction (_extract_pdf_docling / _extract_pdf_pymupdf) builds a
   page_map: list[(page_no, page_text)] via PyMuPDF, independent of the
   text extractor used for chunking (Docling markdown loses page
   boundaries, so PyMuPDF is always used just for this map).
2. For each structural chunk of chapter text:
   a. paging.lookup_page_for_chunk(chunk_text, page_map) -> best-matching
      file page for the *start* of the chunk (substring match, else
      fuzzy word-overlap fallback), or None if no page_map exists.
   b. paging.extract_page_hint(chunk_text, page_from_meta=<result of a>,
      allow_regex=<True only if no page_map at all>) -> PageHint
      (explicit if metadata hit or regex hit; unknown otherwise).
3. After all chunks in the current batch are hinted:
   paging.apply_page_inference(hints) -> fills every "unknown" hint by
   paging.infer_missing_page() linear interpolation between the nearest
   explicit neighbours in the SAME batch; confidence becomes "inferred"
   (or stays "unknown" if no explicit neighbour exists on either side).
4. paging.compute_page_in_book(page_in_file, start_file, start_book) is
   applied per chunk using the source's resolved ContentPaging bounds.
   Chunks whose page_in_file < start_file are DROPPED (never persisted;
   see Business Rule 9).
5. Kept chunks are persisted to StateDB.chunks (page_in_file, page_in_book,
   page_confidence) and embedded into ChromaDB with page metadata.
6. Downstream: extractor.py reads page_in_book/page_in_file back off the
   chunk row and calls paging.format_source_locator() to build the
   locator string shown in LLM prompts (Prompt 1) and stored as
   "candidates" locators; vault.py's literature_chunk_filename() uses
   page_in_book (falling back to page_in_file) to build the "pNNN" token
   in the granular LIT filename.
```

**B. Content-start resolution (gates step 4 above; `harvester._resolve_content_paging`, harvester.py:1761-1831)**

```
1. Before chunking begins, harvester._process_file calls
   paging.suggest_content_start(page_map) -> a heuristic guess
   (first page matching a Capitulo-1/Introduction pattern) used only as
   a DEFAULT/fallback value, never as the final answer on its own.
2. harvester._resolve_content_paging combines that suggestion with the
   caller's mode and explicit CLI/web flags to produce a final
   ContentPaging(content_start_file_page, content_start_book_page,
   confidence) with confidence in {confirmed, heuristic, skipped}:
   - --content-start-file given (CLI or web form)      -> "confirmed"
   - --skip-paging with no explicit file                -> "skipped" (1,1)
   - interactive & no explicit file                      -> Rich prompt,
     defaulting to the heuristic suggestion; user answer -> "confirmed"
   - non-interactive & no --skip-paging & no explicit    -> "skipped" (1,1)
     (the heuristic-return line for this case is DEAD CODE; see
     Business Rule 7 / Technical Debt Risk 1)
3. The resolved ContentPaging is threaded through
   _chunk_and_persist (flow A step 4), _create_vault_notes (SRC
   frontmatter: content_start_file_page/book_page, page_offset,
   page_offset_confidence), and StateDB.update_source_paging.
4. zettel set-paging (harvester.run_set_paging) re-enters this same
   ContentPaging model directly from CLI flags (no heuristic, no
   prompt) to REPAIR an already-harvested source: recomputes
   page_in_book for every existing chunk via compute_page_in_book,
   drops pending chunks before the new start unconditionally, drops
   awaiting_review/approved chunks before the new start only if
   --drop-before-start, renames granular LIT files whose "pNNN" token
   changed, patches their frontmatter, and refreshes the literature
   index — all without calling the LLM again.
5. zettel rechunk (harvester.run_rechunk) and the "incomplete chunking"
   auto-resume path (_complete_incomplete_source) both REUSE the
   source's already-persisted content_start_file_page/book_page
   (wrapped back into a ContentPaging) rather than re-resolving via
   suggest_content_start/_resolve_content_paging — paging decisions,
   once made, are stable across reprocessing.
```

---

## 3. Business Rules & Logic

### Overview of the business rules:

| # | Rule Type | Rule Description | Location |
|---|-----------|-------------------|----------|
| 1 | Validation/Priority | `page_in_file` resolved by priority: explicit metadata > regex > interpolation > unknown | paging.py:67-93 |
| 2 | Business Logic | Regex fallback is suppressed entirely when a page map is available (`allow_regex=False`) | paging.py:71-82; harvester.py:1607,1654 |
| 3 | Business Logic | Regex patterns scan only the first/last 200 chars of a chunk, in a fixed pattern-priority order | paging.py:26-31, 84-93 |
| 4 | Business Logic | Missing pages are linearly interpolated between nearest explicit neighbours; forward-only extrapolation when only one side is known | paging.py:96-125 |
| 5 | Business Logic | Confidence is downgraded to "inferred" for interpolated pages; only interpolation-eligible hints (non-explicit) are touched | paging.py:128-143 |
| 6 | Business Logic / Formula | `page_in_book = page_in_file - start_file + start_book`; pages before `start_file` map to `None` | paging.py:146-162 |
| 7 | Business Logic | Content-start file/book page resolved by strict precedence: explicit flag > `--skip-paging` > interactive HITL > non-interactive silent default (heuristic branch for the last case is unreachable dead code) | harvester.py:1761-1831 |
| 8 | Business Logic | Content-start heuristic only scans the first 1200 chars of each page, in page order, for chapter/intro markers; always suggests printed page 1 | paging.py:33-44, 165-186 |
| 9 | Business Logic | Chunks that begin before the resolved content-start page are discarded outright during harvest/rechunk | harvester.py:1690-1710 |
| 10 | Business Logic | Chunk-to-page attribution uses only the chunk's opening ~120-200 chars (start-of-chunk wins for multi-page chunks); substring match first, then a minimum-threshold word-overlap fallback | paging.py:194-219 |
| 11 | Business Logic | `set-paging` repair: pending chunks before the new start are always dropped; awaiting_review/approved chunks before the new start are dropped only with `--drop-before-start`; all other kept chunks get `page_in_book` recomputed | harvester.py:262-341 |
| 12 | Business Logic | `set-paging` renames a chunk's granular LIT file and patches its frontmatter whenever the recomputed page token changes; no LLM call is made | harvester.py:292-321; vault.py:291-328 |
| 13 | Business Logic | `rechunk` and incomplete-harvest auto-resume reuse the source's already-persisted content-start bounds instead of re-resolving them | harvester.py:159-212, 480-521 |
| 14 | Formatting | Display locator prefers printed book page over file page, then appends the section path; empty when neither is available | paging.py:238-251 |
| 15 | Business Logic (secondary/unrelated) | A stable SHA-256 fingerprint of ingestion config (`pdf_extractor`, chunking, image settings) is computed here to detect stale mid-flight resumes | paging.py:222-235 |

## Detailed breakdown of the business rules:

---

### Business Rule: Three-tier `page_in_file` resolution

**Overview**:
Every chunk needs a `page_in_file` value before it can be given a printed `page_in_book` value. `extract_page_hint` (paging.py:67-93) is the entry point: it prefers an explicit page number supplied by the caller (`page_from_meta`, sourced from a PyMuPDF page map lookup), falls back to regex pattern matching over the chunk's own text only when explicitly allowed, and otherwise reports "unknown" for later interpolation.

**Detailed description**:
The function signature encodes the priority order directly: `page_from_meta` is checked first, and if it is a positive integer the function returns immediately with `confidence="explicit"` — no regex is even attempted. This reflects the module's core trust hierarchy: a page map built by directly parsing the PDF's own page boundaries via PyMuPDF is unambiguous ground truth, whereas any number found by scanning chunk text is a guess that could be a footnote, a citation year, a table value, or a stray digit.

Only when no metadata page is supplied does the function consider regex matching, and even then only if the caller passes `allow_regex=True`. The `allow_regex` flag is not a tuning knob left to chance — `harvester._chunk_and_persist` computes it deterministically as `not bool(page_map)` (harvester.py:1607): regex scanning is used exclusively for documents where no page map could be built at all (e.g., Markdown sources, or a PDF where PyMuPDF failed). This prevents a subtle correctness bug: if a page map exists but a particular chunk simply doesn't match any of its pages via `lookup_page_for_chunk`, the function does *not* fall through to guessing a page number from stray digits in the chunk body (which the module's own docstring calls out explicitly — "TOC entries, '2 EPILOGUE', etc." — as a source of false positives). In that scenario the hint is deliberately left `unknown` and handed to interpolation instead, which is judged more reliable than a regex false positive.

When regex is allowed, `extract_page_hint` only inspects the chunk's first and last 200 characters (`head, tail = chunk_text[:200], chunk_text[-200:]`), on the theory that a page break marker (an isolated printed page number) would appear at a chunk boundary, not buried mid-paragraph. If no pattern matches in either region, the hint is `unknown`, which flows into the interpolation stage (Business Rule 4) rather than being left permanently blank.

**Rule workflow**:
```
extract_page_hint(chunk_text, page_from_meta, allow_regex):
    if page_from_meta is a positive int:
        return PageHint(page_from_meta, "explicit")   # ground truth wins, no regex tried
    if not allow_regex:
        return PageHint(None, "unknown")               # page map exists but no match: don't guess
    for each PAGE_PATTERNS (in fixed order):
        for region in (first 200 chars, last 200 chars):
            if pattern matches and captured group is a valid int:
                return PageHint(match, "explicit")
    return PageHint(None, "unknown")                   # goes to interpolation next
```

---

### Business Rule: Regex pattern set and scan boundaries

**Overview**:
When regex fallback is permitted, `PAGE_PATTERNS` (paging.py:26-31) defines four increasingly permissive patterns tried in a fixed order against only the head/tail 200-character windows of the chunk.

**Detailed description**:
The four patterns are, in order: (1) a line containing *only* 1-4 digits (`^\s*(\d{1,4})\s*$`, multiline) — the strongest, least ambiguous signal, matching an isolated page-number line; (2) the same idea but requiring surrounding blank lines (`\n\s*(\d{1,4})\s*\n`) — catches numbers embedded between paragraphs without anchoring to true line boundaries; (3) an explicit "Página N" / "Pagina N" marker (case-insensitive) — a textual page label sometimes present in scanned/OCR'd Portuguese documents; and (4) a number immediately followed by a word on the same line (`^\s*(\d{1,4})\s+\w`, multiline) — the weakest and most permissive pattern, intended to catch a page number that leads directly into body text (e.g., a running header). Because patterns are tried in this exact order and the first match wins, a cleaner, more isolated number always takes precedence over one embedded in a numeral-plus-text pattern, minimizing false positives from prices, list numbers, or footnote markers that happen to be followed by a word.

Because the scan only covers 200 characters at each end of the chunk (not the whole chunk body), a page-number token that happens to appear mid-chunk (for instance, a citation like "(see p. 42)" deep inside a long paragraph) will never be mistaken for the chunk's own page — this is a deliberate boundary restriction, not an oversight, and keeps the regex fallback focused on genuine page-break artifacts.

This layered pattern ordering exists only as a fallback of a fallback: it is reached solely when there is no page map at all for the document (`allow_regex=True`), which in practice means Markdown-origin sources or a PDF whose PyMuPDF page map failed to build. For the majority of PDF sources (Docling or PyMuPDF extraction, both of which always attempt to build a page map — harvester.py:1241-1251, 1288), this pattern set is never invoked at all because `allow_regex` is `False`.

**Rule workflow**:
```
for pattern in [isolated-digit-line, blank-line-wrapped-digits,
                "Pagina N" label, digit-followed-by-word]:
    for region in [chunk_head_200, chunk_tail_200]:
        if pattern.search(region) succeeds and group(1) parses as int:
            return that page number, confidence="explicit"
# no pattern matched in either window -> confidence="unknown"
```

---

### Business Rule: Linear interpolation for missing pages (with forward-only extrapolation)

**Overview**:
`infer_missing_page` (paging.py:96-125) fills in a chunk's unknown `page_in_file` by linearly interpolating between the nearest explicit page **before** and **after** it in the same ordered sequence; if only one side has a known page, that value is copied forward without extrapolating a slope; if neither side is known, the result is `None`.

**Detailed description**:
The function walks backward from `chunk_index` to find the nearest prior index with a non-`None` page (`prev_idx`, `prev_page`), and forward to find the nearest subsequent one (`next_idx`, `next_page`). When both exist, it computes `progress = (chunk_index - prev_idx) / span` where `span = next_idx - prev_idx`, and returns `round(prev_page + (next_page - prev_page) * progress)` — a straightforward proportional interpolation assuming chunks are evenly spaced across the page range between the two known anchors. This is a reasonable approximation given that chunks within a chapter are typically similarly sized, but it does not account for chunks of very different lengths (e.g., one 3000-character chunk followed by three 200-character chunks would still be evenly divided across the page span).

When only a previous page is known (no subsequent explicit page exists in the sequence, e.g. the tail of a chapter or document), the function returns `prev_page` unchanged — it does not extrapolate forward by estimating an average pages-per-chunk rate. This means a long run of trailing chunks with no further explicit page markers will all be assigned the *same* file page as the last known one, which is a conservative (if occasionally page-locked/under-counted) choice. Symmetrically, when only a following page is known and no preceding explicit page exists, `prev_page` is still `None` at the final `return prev_page` statement, so the result is `None` (unknown) rather than backward-extrapolating from the *next* known page — chunks before the very first explicitly-known page in a document are simply never assigned a page by this function.

The edge case `span <= 0` (which should not occur given the search directions but is defensively checked) returns `prev_page` directly rather than dividing by zero or a negative span, protecting against a degenerate ordering.

**Rule workflow**:
```
infer_missing_page(chunk_index, pages):
    if pages[chunk_index] is not None: return it unchanged
    prev_page = nearest non-None page at index < chunk_index (or None)
    next_page = nearest non-None page at index > chunk_index (or None)
    if both known:
        span = next_idx - prev_idx
        if span <= 0: return prev_page
        progress = (chunk_index - prev_idx) / span
        return round(prev_page + (next_page - prev_page) * progress)
    else:
        return prev_page   # None if no prior known page exists either
```

---

### Business Rule: Confidence downgrade during batch-wide inference

**Overview**:
`apply_page_inference` (paging.py:128-143) is the orchestrator that turns a list of raw `PageHint`s into a fully-filled list, preserving `"explicit"` hints untouched and marking every filled-in gap as `"inferred"` (or leaving it `"unknown"` if interpolation could not resolve it).

**Detailed description**:
The function iterates the hint list once, extracting a parallel `raw` list of just the `page_in_file` integers (or `None`) to pass to `infer_missing_page` for each index. Any hint that is both non-`None` and already `confidence == "explicit"` is passed through as-is without modification — this guards against accidentally "smoothing over" an already-certain value. Every other hint (in practice, always `confidence == "unknown"` at this stage, since `extract_page_hint` never itself returns `"inferred"`) is replaced by calling `infer_missing_page(i, raw)`; if that returns a page number, a new `PageHint(estimated, "inferred")` is created, and if it returns `None`, the hint becomes `PageHint(None, "unknown")` — i.e., the confidence label always accurately reflects whether the module could actually assign a page or not.

Critically, this function only ever sees the hints belonging to the **current chunking batch** — the list of chunks freshly produced for chapters that changed in this harvest/rechunk pass (`harvester._chunk_and_persist`, harvester.py:1620, 1683-1688 passes `pending_specs`' hints, not the full set of persisted chunks for the source). This means interpolation cannot "reach across" into already-persisted, unchanged sibling chapters to borrow a neighbouring known page. For a source being incrementally re-chunked (a changed chapter surrounded by untouched chapters), a chunk at the very start or end of the changed chapter with no explicit page hint of its own will not benefit from the known pages of the adjacent, already-persisted chapters, and may resolve to `"unknown"` even though a nearby true page value already exists in the database. This is flagged as a medium-risk discrepancy in Section 10 (the function's caller's own docstring claims "plus already-persisted pages for the source," which is not actually implemented).

**Rule workflow**:
```
apply_page_inference(hints):
    raw = [h.page_in_file for h in hints]
    for i, hint in enumerate(hints):
        if hint.page_in_file is not None and hint.confidence == "explicit":
            keep hint unchanged
        else:
            estimated = infer_missing_page(i, raw)   # raw = THIS BATCH ONLY
            if estimated is not None: hint = PageHint(estimated, "inferred")
            else: hint = PageHint(None, "unknown")
```

---

### Business Rule: `page_in_book` formula and pre-content exclusion

**Overview**:
`compute_page_in_book` (paging.py:146-162) converts a file page into the printed book page a human reader would see, using the formula `page_in_book = page_in_file - content_start_file_page + content_start_book_page`, and explicitly returns `None` for any file page that falls before the configured content start.

**Detailed description**:
The formula is a simple linear offset: once the file page where "real" content begins (`content_start_file_page`) and the printed number appearing on that exact page (`content_start_book_page`) are both known, every subsequent file page maps to a printed page by preserving the same offset. Both bound arguments default to `1` via `int(... or 1)` if `None` is passed, so the function degrades gracefully to `page_in_book == page_in_file` when no content-start configuration exists at all (the "skipped" confidence case throughout the harvester).

The explicit guard `if page_in_file < start_file: return None` is a hard business rule, not an incidental side effect: front matter (cover, table of contents, preface, dedication) that precedes the declared content start is defined as *out of scope* for book-page numbering — it has no meaningful printed page in the eventual citation scheme, and the caller (`harvester._chunk_and_persist`, harvester.py:1693-1701) uses exactly this `None` result as the signal to discard that chunk entirely rather than persist it with a nonsensical or negative book page. Chunks whose `page_in_file` itself could not be determined (`None`, i.e., "unknown" confidence) are treated permissively — the harvester's comment "Unknown page: keep (cannot prove it is before content start)" (harvester.py:1695) makes this an explicit business decision: absence of evidence is not evidence of being out-of-scope, so unknown-page chunks are always retained rather than speculatively dropped.

This function is reused identically by three distinct call sites with three different lifecycles: the initial harvest (`_process_file`/`_chunk_and_persist`), the `set-paging` repair command (recomputing `page_in_book` for every already-persisted chunk after new bounds are set), and `rechunk`/incomplete-harvest resume (reapplying the source's existing bounds). This consistency guarantees the same mapping rule applies whether a book's paging is being resolved for the first time or corrected months later.

**Rule workflow**:
```
compute_page_in_book(page_in_file, content_start_file_page, content_start_book_page):
    if page_in_file is None: return None
    start_file  = content_start_file_page or 1
    start_book  = content_start_book_page or 1
    if page_in_file < start_file: return None     # pre-content: caller must drop/exclude
    return page_in_file - start_file + start_book
```

---

### Business Rule: Content-start resolution precedence (and a dead-code anomaly)

**Overview**:
`harvester._resolve_content_paging` (harvester.py:1761-1831) is the single decision point that turns a heuristic suggestion, CLI/web flags, and interactivity mode into the final `ContentPaging` used for an entire source. Careful reading of its branching reveals that one of its five intended outcomes — auto-applying the heuristic suggestion during a non-interactive harvest — can never actually be reached given how the function is called today.

**Detailed description**:
The function's precedence, as written, is meant to be: (1) if `--skip-paging` was passed with no explicit start page, always skip (file page 1 = book page 1); (2) if `content_start_file` was passed explicitly (CLI `--content-start-file`/web form field), always honour it as `"confirmed"`, regardless of interactivity; (3) if running interactively with no explicit page, prompt the user via Rich, defaulting to the heuristic suggestion, and record whatever they confirm as `"confirmed"`; (4) if running non-interactively with `--skip-paging`, apply the heuristic silently if one was found (marking it `"heuristic"`), else fall back to page-1 (`"skipped"`); and (5) if running non-interactively without `--skip-paging` and without an explicit page, apply the same heuristic-if-found rule.

However, tracing the actual control flow shows outcomes (4) and (5) collapse into a single unreachable branch. By the time execution reaches `if skip_paging or not interactive:` (harvester.py:1782), `content_start_file` is guaranteed to be `None` (any non-`None` value already returned at line 1780), and `skip_paging` is guaranteed to be `False` (any `True` value with `content_start_file is None` already returned `(1, 1, "skipped")` at line 1771). So the block can only be entered with `skip_paging=False`, meaning the subsequent inner check `if not interactive and not skip_paging:` (line 1786) is equivalent to just `if not interactive:` — and since the outer condition `skip_paging or not interactive` was only true because `not interactive` was true, this inner check is *always* satisfied whenever the outer block executes. The line above it that computes `conf` (potentially `"heuristic"`) and the final `return ContentPaging(sug_file if conf == "heuristic" ...)` statement below it can therefore never execute — every non-interactive harvest lacking both `--skip-paging` and an explicit `--content-start-file` unconditionally returns `ContentPaging(1, 1, "skipped")`, silently discarding whatever `suggest_content_start` detected.

The practical consequence is significant: `zettel harvest --yes` (or any other non-interactive invocation), the web UI's harvest job (which defaults `content_start_file`/`content_start_book` to `None` unless the user fills the form — `zettel/web.py:324-358`), and `run-all` in non-interactive mode (`skip_paging=not interactive`, `cli.py:1286`, which sets `skip_paging=True` only when *not* interactive, itself routed through the same dead branch since `content_start_file` is never passed by `run-all` at all) all end up treating every page of the PDF as content, with printed page numbers equal to file page numbers — even for a 300-page book whose actual chapter 1 the heuristic could have correctly located at, say, file page 35. This silently degrades `page_in_book` accuracy for every citation, LIT filename ("pNNN" token), and ABNT locator derived from that source, and the operator has no in-band signal that the heuristic was computed and then discarded — the log line for `"heuristic"`/`"skipped"` confidence at harvester.py:679-686 only ever shows `"skipped"` for these runs. The only escape hatches are running `harvest` interactively, passing `--content-start-file` explicitly, or repairing after the fact with `zettel set-paging`.

**Rule workflow**:
```
_resolve_content_paging(page_map, interactive, content_start_file, content_start_book, skip_paging):
    if skip_paging and content_start_file is None:
        return (1, 1, "skipped")                      # explicit opt-out
    suggested = suggest_content_start(page_map)        # always computed either way
    if content_start_file is not None:
        return (content_start_file, content_start_book or 1, "confirmed")  # explicit wins
    if skip_paging or not interactive:
        # reachable ONLY with skip_paging=False, interactive=False (both prior branches
        # already returned otherwise)
        conf = "heuristic" if suggested.confidence == "heuristic" else "skipped"  # computed...
        if not interactive and not skip_paging:        # ALWAYS true on this path
            return (1, 1, "skipped")                    # ...but this always fires first
        return (sug_file, sug_book, conf)               # DEAD: unreachable given the above
    # interactive, no explicit file: Rich prompt, default = suggested; user answer -> "confirmed"
```

---

### Business Rule: Content-start heuristic detection (chapter/introduction markers)

**Overview**:
`suggest_content_start` (paging.py:165-186) scans a document's page map in page order and returns the first page whose opening ~1200 characters match one of nine chapter/introduction regex patterns, always proposing printed page 1 as the corresponding book start.

**Detailed description**:
`CHAPTER_START_PATTERNS` (paging.py:33-44) covers nine variants intended to catch both Markdown-heading and plain-text page layouts, in Portuguese and English: headed forms like `# Capítulo 1`, `## Chapter 1`, a numbered `1.`/`1)` heading, `# Introduction`/`# Introdução`, and the same four textual markers without a leading Markdown heading marker (for plain-text page maps, since Docling's markdown loses page boundaries but PyMuPDF's raw text does not carry heading syntax at all). The function iterates pages in the order given (expected ascending by page number) and, for each page, tests all nine patterns against only the first 1200 characters of that page's text — the first page/pattern combination that matches wins immediately, and no further pages are inspected.

When a match is found, the function returns a dict with `content_start_file_page` set to that page number, `content_start_book_page` **hard-coded to `1`** (the heuristic never attempts to read or guess an actual printed page number from the page content), `confidence: "heuristic"`, `needs_confirmation: True`, and an `anchor_page_in_file` used purely for the interactive prompt's informational message. If no page matches any pattern across the entire document, it returns `content_start_file_page: 1`, `confidence: "none"`, signalling "no heuristic evidence" rather than merely "book starts at page 1" — the caller (`_resolve_content_paging`) treats `"none"` the same as failing to find a heuristic when deciding what default to show, but the caller's own dead-code path (see previous rule) means this distinction is currently only visible in the interactive prompt's message, not in any persisted confidence value for non-interactive runs.

The 1200-character window is a fixed constant with no configuration knob; a chapter heading appearing later on a page dense with front matter (e.g., an epigraph or dedication preceding "Capítulo 1" on the same physical page) would not be detected, silently falling through to the next page or, if no later page matches either, to `"none"`. This heuristic is explicitly a *suggestion* only — every consumer (interactive prompt default, and, if it were reachable, the dead non-interactive branch) treats its output as a starting point requiring confirmation, never as an authoritative answer on its own.

**Rule workflow**:
```
suggest_content_start(page_map):
    for (page_no, text) in page_map:            # assumes ascending page order
        head = text[:1200]
        for pattern in CHAPTER_START_PATTERNS:   # 9 patterns, PT/EN, MD/plain-text
            if pattern.search(head):
                return {file_page: page_no, book_page: 1, confidence: "heuristic",
                        needs_confirmation: True, anchor_page_in_file: page_no}
    return {file_page: 1, book_page: 1, confidence: "none", needs_confirmation: True,
            anchor_page_in_file: None}
```

---

### Business Rule: Pre-content chunk exclusion during chunking

**Overview**:
Chunks whose resolved `page_in_file` falls before the source's `content_start_file_page` are never persisted — they are computed, then deleted from both SQLite and ChromaDB in the same pass (`harvester._chunk_and_persist`, harvester.py:1690-1710), rather than being stored with a null or negative book page.

**Detailed description**:
After `apply_page_inference` fills in every hint for the current chunking batch, `_chunk_and_persist` iterates the resulting specs and applies `compute_page_in_book` (Business Rule 6) to each; any spec whose file page is known and less than `start_file` is added to a `skipped_ids` list and excluded from `pending_specs` entirely — it is never written to `db.upsert_chunk` nor embedded via `idx.upsert_chunk`. If any chunk IDs were already persisted from a prior run under different (or absent) paging bounds, they are actively removed via `db.delete_chunks(skipped_ids)` and `idx.delete_chunks(skipped_ids)`, and an informational log line reports the count skipped versus kept. This means changing a source's content-start bounds (e.g., correcting it via `set-paging`, discussed next) can retroactively cause previously-kept chunks to be purged if the new start moved later than before.

As already noted in Business Rule 6, chunks whose page could not be determined at all (`page_in_file is None`) are deliberately exempted from this exclusion — the code comment "cannot prove it is before content start" makes explicit that the system errs on the side of retaining ambiguous chunks rather than silently losing content. This asymmetry (known-early pages are excluded; unknown pages are always kept) means a document with poor page-map coverage could end up with front-matter noise surviving into the harvested chunk set, trading recall for precision.

This exclusion happens exactly once, during the batch that first produces or re-produces the affected chunks (initial harvest, `rechunk`, or incomplete-harvest resume) — it is not re-evaluated for chunks from unrelated, unchanged chapters that happen to already be persisted with different content-start bounds from a much earlier run, unless that source is later put through `set-paging` (which applies its own, separate exclusion logic — see next rule) or a full `rechunk`.

**Rule workflow**:
```
for each spec in pending_specs (this batch only):
    page_book = compute_page_in_book(spec.page_in_file, start_file, start_book)
    if spec.page_in_file is not None and spec.page_in_file < start_file:
        mark spec for deletion (skipped_ids)
    else:
        spec.page_in_book = page_book             # None here means "unknown page, kept anyway"
        keep spec
db.delete_chunks(skipped_ids); idx.delete_chunks(skipped_ids)
persist/embed remaining kept specs
```

---

### Business Rule: Chunk-to-page attribution via page map (start-of-chunk wins)

**Overview**:
`lookup_page_for_chunk` (paging.py:194-219) attributes a chunk that may span a page boundary to the single file page where it *begins*, using only the first ~120-200 characters of the chunk, via a direct substring match first and a minimum-overlap word-scoring fallback second.

**Detailed description**:
The function first normalizes whitespace in the chunk's opening 200 characters into a single "needle" string and bails out with `None` immediately if that needle is shorter than 20 characters or if no page map was supplied at all — too little text to reliably match against a full page's content. It then takes just the first 120 characters of the needle as a `probe` and iterates the page map in list order (again assuming ascending page order matters for "first match wins" semantics): for each page, it whitespace-normalizes the page's full text and checks whether `probe` appears verbatim as a substring; the **first page in list order containing that substring returns immediately** as the chunk's page. This deliberately implements the module's documented "first page wins for a chunk that spans a boundary" rule — since a chunk's *opening* text can only physically appear on the page where the chunk begins (or, in rare duplicate-phrasing cases, on an even earlier page, which the ascending-order-first-match strategy would incorrectly prefer, though this is not explicitly guarded against).

If no page contains that exact substring (e.g., due to extraction differences between the chunking source text and the PyMuPDF-derived page map, hyphenation differences, or OCR noise), the function falls back to word-overlap scoring: it computes the set of lowercase words in the 120-character probe and compares against the set of lowercase words in the page's first 2000 characters, tracking the page with the highest intersection count. A candidate only overtakes the current best if its score is strictly greater than the running best **and** clears a minimum absolute threshold, `max(3, min_overlap // 10)` — with the default `min_overlap=40`, this evaluates to `4`, meaning at least 4 shared distinct words are required before a page is considered a plausible fuzzy match at all. Unlike the substring path, the fallback scans the *entire* page map (no early exit), always returning the single globally-best-scoring page (or `None` if no page ever clears the threshold).

Because the fallback threshold is a small fixed constant with no per-corpus tuning and no tie-breaking rule beyond "first page to achieve a given score" (a later page with an identical score is not preferred even if it might be adjacently more plausible), overlapping generic vocabulary across nearby pages of narrative or technical prose can occasionally cause a chunk to be attributed to a page other than its true origin. This risk is bounded, however, by the fact that the fallback is only reached when the exact-substring path fails — the common case (unmodified re-extraction of the same PDF) uses the substring path and is exact.

**Rule workflow**:
```
lookup_page_for_chunk(chunk_text, page_map, min_overlap=40):
    needle = whitespace_normalize(chunk_text[:200])
    if len(needle) < 20 or page_map is empty: return None
    probe = needle[:120]
    best_page, best_score = None, 0
    for (page_no, page_text) in page_map:               # ascending order assumed
        normalized = whitespace_normalize(page_text)
        if probe in normalized:
            return page_no                                # exact substring: first hit wins, exits early
        score = |words(probe) ∩ words(normalized[:2000])|
        if score > best_score and score >= max(3, min_overlap // 10):
            best_page, best_score = page_no, score
    return best_page                                       # None if nothing cleared the threshold
```

---

### Business Rule: `set-paging` repair — differentiated drop policy by review status

**Overview**:
`harvester.run_set_paging` (harvester.py:215-400) repairs an already-harvested source's page mapping without any LLM call, applying a status-sensitive drop policy: `pending` chunks before the new start are **always** dropped, while `awaiting_review`/`approved` chunks before the new start are dropped **only** when the operator explicitly passes `--drop-before-start`.

**Detailed description**:
For every existing chunk of the source, the function computes `before_start = page_in_file is not None and page_in_file < start_file` under the *new* bounds being applied. If a chunk is `before_start` and still in `pending` status (i.e., it has not yet been through `extract`'s LLM call at all), it is unconditionally queued for deletion — there is no reason to preserve a not-yet-processed chunk that the corrected paging now identifies as front matter, and no operator confirmation is required since no downstream artifact (draft note, approved LIT file) yet depends on it. If a chunk is `before_start` but has already progressed past `pending` (i.e., `awaiting_review` after `extract`, or `approved`/further after `review`), it is only dropped when `drop_before_start=True` was explicitly requested; without that flag, such chunks are left alone entirely (their `page_in_book` is *not* even recomputed, since the code path for updating pages runs only for chunks that are **not** dropped, and this branch continues past both drop checks).

This asymmetry embodies a "don't destroy human/LLM work silently" principle: an `awaiting_review` draft represents completed LLM extraction work with a written literature note in the Review folder, and an `approved` chunk represents work a human has already vetted and moved into the permanent vault (`20_Literature/`) — deleting either without explicit confirmation would be a much more destructive, harder-to-reverse action than discarding an unprocessed `pending` chunk. For chunks that are *not* dropped (the ordinary "corrected but kept" case, or the intentionally-preserved `before_start` non-pending case when the flag is absent), the function recomputes `page_in_book` via `compute_page_in_book` and calls `db.update_chunk_pages`, incrementing `stats["updated"]`.

Deletion, when it does occur, is thorough: dropped chunk IDs are removed from `db.delete_chunks` (SQLite + FTS + concepts) and `idx.delete_chunks` (ChromaDB), and if a dropped chunk had already produced a draft/approved literature-note file on disk, that file is unlinked from the vault (`Path(lit).unlink(missing_ok=True)`) before the chunk row itself disappears — preventing orphaned Markdown files with no backing chunk record.

**Rule workflow**:
```
for each existing chunk of the source:
    before_start = page_in_file is known and page_in_file < new_start_file
    if before_start and status == "pending":
        queue for deletion                     # always, no confirmation needed
    elif before_start and drop_before_start flag:
        queue for deletion; count as "dropped_other"   # opt-in only
    else:
        recompute page_in_book; db.update_chunk_pages(...); count as "updated"
        # (also triggers the LIT-file rename/frontmatter-patch rule below)
delete queued chunk rows + their vault draft/approved files (if any)
```

---

### Business Rule: `set-paging` LIT filename rename and frontmatter patch on page-token change

**Overview**:
Whenever `set-paging` recomputes a kept chunk's `page_in_book` and that chunk already has an associated literature note on disk, the function checks whether the note's filename would change under the new page value and, if so, renames the file on the filesystem and patches its YAML frontmatter — all without invoking the LLM.

**Detailed description**:
Granular LIT filenames encode a page token (`pNNN`, from `vault._page_token`, preferring `page_in_book` over `page_in_file`) directly in their basename (`vault.literature_chunk_filename_for_row`, vault.py:320-328). After updating a chunk's `page_in_book` in the database, `run_set_paging` builds an `updated_row` copy of the chunk dict with the new `page_in_book` and computes what the filename *would* be under the new value; if it differs from the note's current path, the file is physically moved (`path.replace(new_path)`) and `db.update_chunk_review(chunk_id, literature_note_path=str(path))` is called so the database's pointer stays in sync with the renamed file — preventing the common failure mode of a stale DB path pointing at a file that no longer exists. If the target directory doesn't yet exist (unlikely, since it should already contain the note being renamed, but guarded defensively), it is created first; if any `OSError` occurs during the move, the function conservatively `continue`s to the next chunk without patching frontmatter for this one, in effect leaving that note's on-disk content in its pre-repair state until manually resolved.

After the (possibly renamed) file is confirmed to exist, its YAML frontmatter is parsed and three page-related fields are overwritten: `page_in_file` (unconditionally, to the actual current value, which may be unchanged), `page_in_book` (the newly computed value), and `page_confidence` (only if a value was already stored on the chunk). The note is then rewritten via `vault.safe_write_note`, which is the same safe-write primitive used throughout the codebase to guarantee that hand-edited body content outside managed blocks is never clobbered — only frontmatter fields and designated managed blocks are touched. Each successful patch increments `stats["notes_patched"]`, which is surfaced back to the operator in the CLI's confirmation message.

This entire rename/patch cycle deliberately never touches the note's *content* (the LLM-authored interpretation, summary, or source excerpt) — only its identity (filename) and a handful of frontmatter metadata fields are updated, which is exactly what makes `set-paging` a zero-LLM-cost repair: the expensive part of literature-note creation (the actual extraction call) is fully preserved and reused as-is.

**Rule workflow**:
```
for each kept, updated chunk with a literature_note_path that still exists on disk:
    new_filename = literature_chunk_filename_for_row(citekey, updated_row_with_new_page_in_book)
    if new_filename != current_filename:
        move file to new path; db.update_chunk_review(literature_note_path=new_path)
    parse existing frontmatter
    frontmatter.page_in_file = current page_in_file
    frontmatter.page_in_book = newly computed page_in_book
    frontmatter.page_confidence = chunk.page_confidence (if set)
    safe_write_note(path, updated_frontmatter, unchanged_body)   # body/content never touched
    stats.notes_patched += 1
```

---

### Business Rule: Paging stability across `rechunk` and incomplete-harvest resume

**Overview**:
`harvester.run_rechunk` and the incomplete-chunking auto-resume path (`_complete_incomplete_source`, harvester.py:480-521) both reconstruct a `ContentPaging` directly from the source's already-persisted `content_start_file_page`/`content_start_book_page`/`page_offset_confidence` columns, rather than re-invoking `suggest_content_start`/`_resolve_content_paging` — once a source's paging decision has been made (whether via heuristic-in-interactive-mode, explicit flags, or a `set-paging` correction), it is treated as durable state that survives any later reprocessing that doesn't explicitly ask to change it.

**Detailed description**:
Both functions read `src.get("content_start_file_page")` and `src.get("content_start_book_page")` (each defaulting to `1` if unset) and `src.get("page_offset_confidence")` (defaulting to `"skipped"`), and construct `ContentPaging(...)` directly from those values before calling `_chunk_and_persist`. This is a deliberate architectural choice distinct from the initial-harvest path: `rechunk` exists specifically to reapply the *current chunking configuration* (e.g., a changed `chunk_size` or newly recognized structural heading pattern) to text that has already been extracted and whose paging has already been settled — it is not an opportunity to redo the content-start decision, interactively or otherwise. If an operator's paging decision was wrong, the sanctioned path to fix it is `zettel set-paging`, not `rechunk`.

The same reuse-not-rediscover principle applies to `_complete_incomplete_source`, which resumes a harvest that was interrupted after chapters/assets were registered but before all chunks were persisted (`source_chunking_incomplete`); since the source's `content_start_file_page`/`book_page` were already recorded in the initial (possibly interrupted) pass, resuming must use those exact values to avoid producing inconsistent page numbering between chunks persisted before and after the interruption.

Both functions do still attempt to rebuild a fresh `page_map` via `_pymupdf_page_map` when the original PDF file is still present at its recorded `origin_path` — so while the content-start *bounds* are frozen, the underlying page-text map used for chunk-to-page attribution (`lookup_page_for_chunk`) is recomputed fresh each time, ensuring that any newly-produced or re-chunked text still gets accurate per-chunk file-page attribution even though the book's start/offset is fixed.

**Rule workflow**:
```
run_rechunk(source_id) / _complete_incomplete_source(source_id):
    paging = ContentPaging(
        content_start_file_page = src.content_start_file_page or 1,
        content_start_book_page = src.content_start_book_page or 1,
        confidence = src.page_offset_confidence or "skipped",
    )                                            # REUSED, never re-resolved
    page_map = rebuild via _pymupdf_page_map(origin_path) if PDF still exists else []
    _chunk_and_persist(..., page_map=page_map, paging=paging)   # same downstream rules apply
```

---

### Business Rule: Human-readable locator formatting precedence

**Overview**:
`format_source_locator` (paging.py:238-251) builds the citation-style locator string shown in LLM prompts and candidate displays, preferring the printed book page over the raw file page, and appending a structural section path when available; it returns an empty string when no locator information exists at all.

**Detailed description**:
The function builds a list of string parts in strict order: if `page_in_book` is not `None`, it contributes `"p.{page_in_book}"`; only if `page_in_book` is `None` does it fall back to `page_in_file`, formatted distinctly as `"p.arquivo.{page_in_file}"` (Portuguese for "file page") — this label difference is intentional and important: it signals to a human reading the locator (in a review UI, an LLM prompt payload, or a candidate table) that the number is a raw PDF page rather than a citable printed page number, preventing a reader from mistaking an unresolved file-page fallback for a book-accurate citation. If `section_path` (the chunk's structural heading breadcrumb, e.g. `"Cap 2 > Sec 2.1"`) is non-empty, it is appended as a separate part regardless of whether a page part was added. The final parts are joined with `" / "`; if no parts were ever added (both pages `None` and `section_path` empty), the function returns an empty string rather than a malformed separator-only string.

This function is called from two locations in `extractor.py`: once (extractor.py:202-209) to build the locator attached to the interpretation candidates surfaced during LLM Prompt 1 processing (falling back to the raw `locator` field if the formatted string is empty), and once (extractor.py:279-283) for the structural locator embedded per-candidate in the extraction output. Both call sites pass the same three chunk-row fields (`page_in_book`, `section_path`/`locator`, `page_in_file`) directly off the persisted chunk record, meaning any correction applied via `set-paging` (which recomputes `page_in_book` in the database) automatically improves future locator strings the next time a chunk is re-processed or re-displayed, without any change needed in `extractor.py` itself.

Because the function has no dependency on `ContentPaging` or any paging-resolution state — it operates purely on already-resolved page values — it is the one function in this module that is equally usable regardless of which page-resolution path (explicit, inferred, or the "skipped"/1:1 default) produced those values; it simply renders whatever numbers it is given.

**Rule workflow**:
```
format_source_locator(page_in_book, section_path, page_in_file):
    parts = []
    if page_in_book is not None: parts.append(f"p.{page_in_book}")
    elif page_in_file is not None: parts.append(f"p.arquivo.{page_in_file}")   # explicitly labeled as raw file page
    if section_path: parts.append(section_path)
    return " / ".join(parts) if parts else ""
```

---

### Business Rule: Ingestion config fingerprint for stale-resume detection (secondary responsibility)

**Overview**:
`compute_docling_config_hash` (paging.py:222-235) produces a stable, order-independent 16-character SHA-256 prefix over the ingestion knobs (`pdf_extractor`, chunk size/overlap/min-section-chars, image extraction settings) that affect how a document is extracted and chunked, used to detect when a source's persisted config has drifted from the current live configuration.

**Detailed description**:
Unlike every other function in this module, this one has nothing to do with page numbers — it is a general ingestion-config fingerprinting utility that happens to live in `paging.py`. The payload dict is built from exactly seven `AppConfig` fields and serialized via `json.dumps(payload, sort_keys=True, ensure_ascii=True)` before hashing, guaranteeing the same logical configuration always produces the same hash regardless of dict insertion order — a prerequisite for using it as a stable comparison key stored alongside each source (`sources.docling_config_hash`). The hash is computed and stored at initial harvest time (`harvester._process_file`, harvester.py:702) and recomputed on every subsequent encounter with the same file (harvester.py:545, 551-556): if the stored hash no longer matches the freshly computed one, a warning is logged advising the operator to run `zettel rechunk --source-id ...` to reapply the now-different chunking configuration — the harvester does not do this automatically, since re-chunking is a potentially expensive, deliberate operation.

This function is also called from `run_set_paging` indirectly not at all — `set-paging` does not touch or need `docling_config_hash`, reinforcing that this is a distinct, chunking-lifecycle concern layered on top of (but architecturally unrelated to) page inference. Its presence in `paging.py` rather than in `harvester.py` itself (where it is the sole consumer) or a small `ingest_config.py` is flagged as a cohesion issue in Section 10, Technical Debt.

The hash is deliberately truncated to 16 hex characters (64 bits) rather than the full 64-character SHA-256 digest — more than sufficient collision resistance for a per-source config comparison key that is never used as a security boundary, while keeping the stored/logged value short.

**Rule workflow**:
```
compute_docling_config_hash(cfg):
    payload = {pdf_extractor, chunk_size, chunk_overlap, min_section_chars,
               images_enabled, images_scale, images_min_width, images_min_height}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)   # order-independent
    return sha256(raw)[:16]                                          # stable comparison key
```

---

## 4. Component Structure

`paging.py` is a single-file module (no sub-package); its internal organization groups cleanly by responsibility even though there is only one file:

```
zettel/paging.py                          # 252 lines, pure functions, stdlib-only
├── Module docstring (lines 1-11)         # documents the 3-layer page_in_file model
│                                          # and the page_in_book formula up front
├── Constants
│   ├── PAGE_PATTERNS (26-31)             # 4 regex patterns for page-number fallback
│   └── CHAPTER_START_PATTERNS (33-44)    # 9 regex patterns for content-start heuristic
├── Value objects (dataclasses)
│   ├── PageHint (47-50)                  # page_in_file + confidence (explicit|inferred|unknown)
│   └── ContentPaging (53-64)             # content_start_file_page/book_page + confidence
│                                          #   (confirmed|heuristic|skipped) + derived page_offset
├── Layer 1+2: per-chunk page_in_file resolution
│   ├── extract_page_hint() (67-93)       # metadata-first, optional regex fallback
│   └── (uses PAGE_PATTERNS)
├── Layer 3: interpolation
│   ├── infer_missing_page() (96-125)     # nearest-neighbour linear interpolation
│   └── apply_page_inference() (128-143)  # batch orchestrator; sets "inferred" confidence
├── Book-page mapping
│   └── compute_page_in_book() (146-162)  # offset formula + pre-content exclusion signal
├── Content-start heuristic
│   └── suggest_content_start() (165-186) # scans page_map for Cap.1/Introduction markers
├── Page-map construction & chunk attribution
│   ├── build_page_map_from_texts() (189-191)  # trivial (page_no, text) pairing helper
│   └── lookup_page_for_chunk() (194-219)      # substring match + word-overlap fallback
├── Unrelated secondary responsibility
│   └── compute_docling_config_hash() (222-235)  # ingestion-config fingerprint (SRP outlier)
└── Display formatting
    └── format_source_locator() (238-251) # book/file page + section path -> citation string
```

No classes beyond the two `@dataclass` value objects; the module exports 11 public names, all listed in `tests/test_paging.py`'s import statement, confirming the module's intended public surface matches exactly what is exercised by direct unit tests.

---

## 5. Dependency Analysis

```
Internal Dependencies:

zettel.config.AppConfig
   -> used ONLY by compute_docling_config_hash() (paging.py:222)
      (reads cfg.pdf_extractor, cfg.chunking.*, cfg.images.*)

No other internal zettel.* imports exist in paging.py.
paging.py does NOT import zettel.state, zettel.vault, zettel.hashing,
zettel.index, zettel.harvester, or zettel.extractor — it has zero
knowledge of persistence, the vault, or any caller.

External Dependencies (all Python stdlib, no third-party packages):

- hashlib   -> compute_docling_config_hash (sha256)
- json      -> compute_docling_config_hash (stable payload serialization)
- logging   -> module-level logger (declared but not actually used for
               any log call within this file itself — see Technical
               Debt, minor)
- re        -> PAGE_PATTERNS / CHAPTER_START_PATTERNS compilation and
               matching throughout
- dataclasses -> PageHint, ContentPaging
- typing    -> Any, Sequence (type hints only)

Inbound (afferent) dependents:

zettel/harvester.py   -> the ONLY heavy consumer; imports ContentPaging,
                          apply_page_inference, build_page_map_from_texts,
                          compute_docling_config_hash, compute_page_in_book,
                          extract_page_hint, lookup_page_for_chunk,
                          suggest_content_start (8 of 11 public names)
zettel/extractor.py   -> imports only format_source_locator (2 call sites,
                          for LLM-facing locator strings)
tests/test_paging.py         -> direct unit tests of nearly the full public API
tests/test_set_paging_filter.py -> imports ContentPaging for an integration test
                                     of harvester._chunk_and_persist
tests/test_set_paging.py     -> exercises paging.py's logic ONLY indirectly,
                                 through harvester.run_set_paging

zettel/cli.py, zettel/state.py, zettel/vault.py, zettel/web.py,
zettel/web_app.py -> do NOT import paging.py at all. They only read/write
                      the already-resolved integer/string fields
                      (page_in_file, page_in_book, page_confidence,
                      content_start_file_page, content_start_book_page,
                      page_offset, page_offset_confidence) that harvester.py
                      persisted after calling into paging.py.
```

The near-total absence of efferent coupling (one config type, all stdlib) combined with a single heavy consumer is the module's strongest architectural property: it can be tested, reasoned about, and modified in complete isolation from the database schema, the vault file format, or any LLM/network concern.

---

## 6. Afferent and Efferent Coupling

Coupling is measured at the function/dataclass level (the natural "component" unit for a procedural Python module), counting cross-file references (afferent) and calls to other names inside/outside the module (efferent).

| Component | Afferent Coupling (external call sites) | Efferent Coupling (names it depends on) | Critical |
|-----------|:---:|:---:|----------|
| `ContentPaging` | 17 (harvester.py: 13, tests: 2, itself: 2 internal uses) | 0 (pure dataclass; `page_offset` property uses only its own fields) | High — the shared state object threaded through nearly every paging-aware harvester function |
| `compute_page_in_book` | 9 (harvester.py: 3, harvester tests: 1, test_paging.py: 5 assertions in 1 test) | 0 (pure arithmetic) | High — sole source of the book-page mapping formula, reused by harvest, set-paging, and rechunk |
| `compute_docling_config_hash` | 6 (harvester.py: 3 call sites, test_paging.py: 1 test) | `AppConfig` fields, `json`, `hashlib` | Medium — unrelated concern (see Technical Debt) but load-bearing for resume-safety |
| `format_source_locator` | 6 (extractor.py: 2 call sites, test_paging.py: 1 test) | 0 (pure string building) | Low — display-only, no correctness impact if wrong (cosmetic) |
| `extract_page_hint` | 6 (harvester.py: 1 call site, test_paging.py: 3 tests) | `PAGE_PATTERNS`, `PageHint` | High — the entry point of the core page-resolution chain |
| `suggest_content_start` | 4 (harvester.py: 1 call site x2 logical uses, test_paging.py: 1 test) | `CHAPTER_START_PATTERNS` | Medium — feeds only a UI default/dead-code branch (see Business Rule 7); low actual runtime impact today, high intended impact |
| `apply_page_inference` | 4 (harvester.py: 1 call site, test_paging.py: 1 test) | `infer_missing_page`, `PageHint` | High — the batch orchestrator for the whole interpolation layer |
| `lookup_page_for_chunk` | 4 (harvester.py: 1 call site, test_paging.py: 1 test) | 0 (pure regex/string ops) | High — determines chunk-to-page attribution accuracy for every PDF source with a page map |
| `PageHint` | 4 (paging.py internal: 3, tests: 1 direct construction) | 0 | Medium — internal value object, not directly consumed outside the module except in tests |
| `infer_missing_page` | 3 (`apply_page_inference` internal call, test_paging.py: 1 test, harvester tests indirectly) | 0 (pure) | Medium — isolated, single caller within the module |
| `build_page_map_from_texts` | 3 (harvester.py: 2 call sites, no direct unit test) | 0 (trivial one-liner) | Low — trivial helper, but silently untested in isolation |

Notes:
- `ContentPaging` and `compute_page_in_book` are the two highest-criticality nodes: nearly every business rule in Section 3 ultimately reads or writes through one of them.
- `suggest_content_start`'s "Medium" criticality reflects a gap between *intended* importance (it is the entire heuristic detection layer) and *actual* runtime impact today, since its output is discarded on the one path (non-interactive harvest) where it was meant to apply automatically (Business Rule 7 / Technical Debt Risk 1).
- No component in this module has efferent coupling into any other `zettel.*` module besides `AppConfig` — the near-zero efferent coupling across the board is a direct, measurable expression of the "functional core" design described in the Executive Summary.

---

## 7. Integration Points

`paging.py` has no direct external integrations of its own (no network, no database, no filesystem I/O) — every integration is mediated by its single heavy consumer, `harvester.py`, and secondarily by `extractor.py`. The table below documents the *effective* integration surface as exercised through those consumers, since that is the only way this component's logic reaches users, storage, or other subsystems.

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| PyMuPDF page map (`harvester._pymupdf_page_map`, `_extract_pdf_pymupdf`) | Library call (internal) | Supplies the `page_map: list[tuple[int, str]]` that `extract_page_hint`/`lookup_page_for_chunk` treat as ground truth | In-process function call | Python list of `(int, str)` tuples | Caller wraps PyMuPDF calls in `try/except Exception`, degrading to an empty page map (all regex fallback) on failure; `paging.py` itself never raises |
| StateDB `sources`/`chunks` tables (`zettel/state.py`) | Internal persistence | Final resting place for `page_in_file`, `page_in_book`, `page_confidence`, `content_start_file_page`, `content_start_book_page`, `page_offset`, `page_offset_confidence` | SQLite via `sqlite3` | Typed columns (`INTEGER`, `TEXT`) | `paging.py` produces plain Python values; `state.py`'s `COALESCE`-based updates (e.g. `update_source_paging`, `update_chunk_pages`) ensure a `None` from paging logic never overwrites a previously-known value unintentionally |
| Vault LIT frontmatter/filenames (`zettel/vault.py`) | Internal persistence | `page_in_book`/`page_in_file` drive the `pNNN` filename token and are written into note YAML frontmatter | Filesystem (Markdown + YAML frontmatter) | Markdown file with YAML header | `run_set_paging` guards file moves/reads with `try/except OSError`, skipping (not failing the whole repair) individual notes that can't be moved or read |
| CLI `harvest` command (`zettel/cli.py`, `--content-start-file`/`--content-start-book`/`--skip-paging`) | CLI flag surface | Lets an operator supply explicit content-start bounds or opt out of paging entirely, bypassing/overriding the heuristic | Typer CLI options | Ints/bool | Invalid interactive prompt input (non-integer) is caught and falls back to the heuristic-suggested default with a logged warning (harvester.py:1821-1830) |
| CLI `set-paging` command (`zettel/cli.py`) | CLI command | Dedicated repair entrypoint into `run_set_paging`; required flags, no interactivity | Typer CLI options | Ints/bool | `ValueError` (unknown source) is caught in the CLI layer and surfaced as a red-printed message + `typer.Exit(1)` |
| Web UI harvest form (`zettel/web.py` `/documents/harvest`, `zettel/web_app.py`) | HTTP form + background job | Same `content_start_file`/`content_start_book`/`skip_paging` inputs as the CLI, submitted via an HTML form and processed asynchronously by the job worker | HTTP POST (form-encoded) -> in-process job payload dict | Optional ints/bool in a JSON-serializable payload dict | CSRF + auth checked before the job is enqueued; the job itself runs `run_harvest` non-interactively, which is the exact path affected by the dead-code anomaly in Business Rule 7 |
| Rich console prompts (`harvester._resolve_content_paging`) | Interactive I/O | Presents the heuristic suggestion as a default and asks the operator to confirm/override both page numbers | `rich.console.Console` / `rich.prompt.Prompt` on stderr | Free-text input parsed as `int` | Non-integer answers are caught (`ValueError`) and replaced by the heuristic-suggested default, with a warning logged |

Because this component performs no I/O of its own, every row above is best understood as "how `harvester.py`/`extractor.py` wire this module's pure functions into the rest of the system" rather than as a direct dependency of `paging.py`.

---

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Functional Core, Imperative Shell | All of `paging.py` is pure/deterministic; `harvester.py` is the sole imperative shell performing I/O and calling into it | paging.py (whole file) vs. harvester.py | Makes page-inference logic trivially unit-testable without mocking DB/filesystem/LLM; matches `tests/test_paging.py`'s pure, fixture-free tests |
| Layered fallback chain ("cheaper/more-certain first") | Explicit metadata > regex > interpolation for `page_in_file`; substring match > word-overlap for chunk attribution | `extract_page_hint`, `apply_page_inference`; `lookup_page_for_chunk` | Mirrors the same architectural idiom used elsewhere in this codebase (harvester's three-layer duplicate detection; retrieval.py's relevance-floor bm25-bypass-then-similarity chain) — a recurring, deliberate house style for graduated confidence |
| Value Object (immutable-by-convention dataclass) | `PageHint`, `ContentPaging` | paging.py:47-64 | Bundles a value with its confidence/provenance label as a single unit passed by value through the pipeline, avoiding parallel "page" and "confidence" arguments/return values threaded separately |
| Derived/computed property | `ContentPaging.page_offset` | paging.py:61-64 | Avoids storing a redundant, potentially-inconsistent offset field alongside the two bounds it is derived from |
| Confidence-graded degrade pattern | `PageHint.confidence` (`explicit`/`inferred`/`unknown`), `ContentPaging.confidence` (`confirmed`/`heuristic`/`skipped`) | Throughout | Same "graded confidence, never silently promoted to certainty" idiom documented for `retrieval.py`'s `floor_reason`/`passed_floor` and `ask.py`'s `AskResult` elsewhere in this codebase — a consistent project-wide convention for surfacing *how sure* a derived value is, not just the value itself |
| Idempotent/stable hashing for cache invalidation | `compute_docling_config_hash` — `sort_keys=True` JSON serialization before hashing | paging.py:222-235 | Guarantees the same logical config always yields the same fingerprint regardless of dict construction order, used as a durable comparison key in SQLite |

---

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `harvester._resolve_content_paging` (harvester.py:1782-1789), consuming `paging.suggest_content_start` | The non-interactive/heuristic-return branch is unreachable dead code: an unconditional early return (`if not interactive and not skip_paging: return ContentPaging(1,1,"skipped")`) always fires before the heuristic-based `return` statement below it can execute, given the guaranteed values of `skip_paging`/`content_start_file` on every path that reaches this block | Every non-interactive `zettel harvest` (CI, `--yes`, `run-all`, and the web UI's default harvest job) silently treats file page 1 as printed page 1 for ALL PDF sources unless `--content-start-file` is passed explicitly — even though `suggest_content_start` correctly detects the true content-start page and is computed on every call. This produces systematically wrong `page_in_book` values (citations, LIT filenames, ABNT locators) for any book harvested without an explicit flag or interactive confirmation, with no operator-visible warning distinguishing this from a genuine "no heuristic found" case |
| Medium | `harvester._chunk_and_persist` docstring vs. `paging.apply_page_inference` implementation | The function's own comment ("Infer missing pages across the newly written batch using only this batch's hints plus already-persisted pages for the source") describes behavior that is not implemented — `apply_page_inference` only ever receives hints from the current batch (`pending_specs`), never merged with already-persisted sibling-chapter pages | Incremental re-chunking of a single changed chapter can leave boundary chunks (adjacent only to untouched, already-persisted chapters) with `page_confidence="unknown"` even though a true neighbouring page value already exists in the database and could have been used for interpolation |
| Medium | `paging.compute_docling_config_hash` | Cohesion/SRP violation: an ingestion-config fingerprinting utility, unrelated to page inference, is defined in the paging module rather than alongside `harvester.py` (its sole consumer) or a dedicated config-hashing helper | Increases `paging.py`'s reasons-to-change (any future ingestion-knob addition touches the "paging" module) and slightly obscures the module's otherwise single, clear responsibility for a reader encountering it for the first time |
| Low | `paging.suggest_content_start` | Only the first 1200 characters of each page are scanned for chapter/intro markers, and the printed book-start page is always hard-coded to `1` regardless of confidence | A chapter heading following unusually long front matter on the same physical page is missed; and for any book whose printed numbering does not restart at 1 at the detected chapter, the heuristic's book-page guess is guaranteed wrong and always requires manual correction (interactive prompt edit or a later `set-paging` call) |
| Low | `paging.infer_missing_page` | Uses Python's built-in `round()` (banker's rounding, round-half-to-even) with no documented rationale for that specific rounding mode | Extremely low-impact, undocumented micro-source of a possible off-by-one at exact `.5` interpolation midpoints; unlikely to matter in practice but is an unexplained implementation detail a future maintainer could stumble on |
| Low | `paging.lookup_page_for_chunk` | The word-overlap fallback's acceptance threshold (`max(3, min_overlap // 10)`, defaulting to 4 shared words) is a small, hardcoded constant with no per-corpus tuning and no tie-breaking rule for equal scores beyond "first in page-map order" | For documents with high shared generic vocabulary between nearby pages (common in narrative/technical prose), a chunk can occasionally be mis-attributed to a nearby-but-wrong page when the exact-substring path fails (e.g., due to extraction inconsistencies) |
| Low | `harvester._resolve_content_paging` (test coverage, not code) | Zero automated tests exist for this function despite five distinct branches and an operator-facing Rich prompt; its behavior is only ever exercised indirectly through CLI/web integration paths that assert no particular paging outcome | The dead-code anomaly documented above (High risk) went undetected specifically because no test asserts what `ContentPaging.confidence`/bounds result from a non-interactive harvest with a page map that would heuristically match |
| Low | `paging.py` module-level `logger` | A `logging.getLogger(__name__)` is declared at paging.py:24 but never used anywhere in the file — every log statement related to paging decisions is emitted from `harvester.py` instead | Minor dead declaration; not a functional issue, but a stray artifact suggesting either removed logging or copy-paste boilerplate |

---

## 10. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|:---:|:---:|----------|---------------|
| `paging.py` public API (11 names: `PageHint`, `ContentPaging`, `extract_page_hint`, `infer_missing_page`, `apply_page_inference`, `compute_page_in_book`, `suggest_content_start`, `build_page_map_from_texts`, `lookup_page_for_chunk`, `compute_docling_config_hash`, `format_source_locator`) | 11 (`tests/test_paging.py`) | 2 (`tests/test_set_paging.py`, `tests/test_set_paging_filter.py`, exercised via `harvester._chunk_and_persist`/`run_set_paging`) | High for the pure-happy-path of every function except `build_page_map_from_texts` (never directly tested — only indirectly reachable via literal tuples passed in `test_set_paging_filter.py`, not via the function itself) | Good, focused positive-path assertions with realistic values (e.g., `compute_page_in_book`'s test asserts 5 distinct scenarios in one function). Missing negative/edge cases: `infer_missing_page`'s one-sided-neighbour and zero-span branches; `suggest_content_start`'s "no match found" (`confidence: "none"`) path; `lookup_page_for_chunk`'s word-overlap fallback branch and its `<20`-char / empty-page-map short-circuits; `format_source_locator`'s file-page-fallback and empty-string branches; `compute_docling_config_hash`'s sensitivity to a *changed* config (only same-config stability is tested, not that a different config produces a different hash) |
| `harvester._resolve_content_paging` (harvester.py:1761-1831) | 0 | 0 (only reached indirectly through CLI/web harvest calls in other test files, none of which assert on the resulting `ContentPaging`) | None — this is the function containing the High-risk dead-code anomaly documented in Section 9, and it has no dedicated coverage of any kind | Untested; the five intended branches (explicit flag / skip-paging opt-out / interactive prompt / non-interactive+skip-paging / non-interactive silent-default) have no test asserting which one fires for a given combination of inputs, which is precisely why the dead-code bug was not caught by the existing test suite |
| `harvester.run_set_paging` (harvester.py:215-400) | 0 | 1 (`tests/test_set_paging.py::test_run_set_paging_updates_book_and_drops_pending`) | Covers the core happy path: a `pending` chunk before the new start is dropped, an `awaiting_review` chunk is kept with recomputed `page_in_book`, its LIT draft file is renamed, and its frontmatter is patched; also asserts the source's `content_start_file_page`/`content_start_book_page`/`page_offset` are updated | Realistic fixture (real `StateDB`, real file writes via `safe_write_note`, a fake `VectorIndex`). Missing scenarios: `--drop-before-start` for `approved`/`awaiting_review` chunks (the `dropped_other` counter path is entirely untested); a chunk whose `literature_note_path` points at a file that no longer exists (`path.exists()` False branch); a rename collision or `OSError` during `path.replace()` |
| `harvester._chunk_and_persist` content-start filtering (harvester.py:1590-1758) | 0 | 1 (`tests/test_set_paging_filter.py::test_chunk_and_persist_skips_pages_before_content_start`) | Covers the core exclusion rule: a chunk mapped (via page map) to a file page before `content_start_file_page` is excluded from the persisted result, and a kept chunk's `page_in_book` is correctly computed | Good realistic two-chapter fixture with distinct page-map entries. Missing: the "unknown page, kept anyway" permissive branch (a chunk with no page-map match at all); the cross-batch interpolation gap documented as Medium risk in Section 9; re-persisting a source where some chunks already existed (the `existing_all`/`next_index` reuse logic) |

Overall: the pure computational core of `paging.py` is well-covered for its primary use cases, but (a) several of its own defensive/edge branches are untested, and (b) its single most consequential integration point — `harvester._resolve_content_paging`, which decides whether the heuristic is ever actually applied — has no test coverage at all, which is the direct and most likely reason the dead-code bug documented in Section 9 has gone unnoticed.
