# ADR-046: Bibliographic Duplicate Layers Before the Semantic One

**Status:** Accepted
**Date:** 2026-09-07
**Extends:** [ADR-011](./ADR-011-three-layer-duplicate-detection.md) — three layers become five; layer ordering and the decision-step principle are preserved
**Related to:** [ADR-033](./ADR-033-invisible-unicode-sanitization-and-text-layer-probe.md)

## Context and Problem Statement

ADR-011's three layers catch byte-identical copies (file hash), cross-format re-exports (extraction hash), and reformatted near-duplicates (semantic chunk similarity). The same work acquired twice with *different text* — an OCR'd scan next to a clean PDF, a publisher's copy next to a preprint — falls through the first two and lands on layer 3.

Layer 3 carries two open complaints that ADR-011 records itself: its `0.88` threshold "was set empirically rather than derived analytically" with no procedure for detecting when recalibration is due, and accepting a match reuses the existing `source_id` with "no documented recovery path" if it turns out to be a false positive. `docs/adrs/RUNBOOK.md` adds a third: changing `chunk_size` re-triggers layer 3 on content the vault has already seen.

Meanwhile the harvest already extracts DOI, ISBN, title, authors and year (`build_bibliographic_metadata`, `prompts/bibliographic_metadata.md`) *before* layer 3 runs — and then uses none of it to decide identity. `generate_citekey` does the opposite: on collision it disambiguates (`Silva2020Obra` → `Silva2020Obraa`), manufacturing two sources out of one work.

DOI and ISBN were stored only inside the `bibliography_json` blob: no column, no index, unqueryable.

## Decision Outcome

**Chosen:** two new SQLite-only layers between the hashes and the semantic check, differing in kind and treated differently.

| # | Layer | Kind | Action |
|---|---|---|---|
| 1 | File hash | equality | reuse, no prompt |
| 2 | Extraction hash | equality | reuse, no prompt |
| **3** | **DOI / ISBN exact** | **identity** | **reuse, no prompt** |
| **4** | **Title + author** | **heuristic** | **always asks; never merges alone** |
| 5 | Semantic similarity | similarity | asks (ADR-011 unchanged) |

1. `sources.doi` and `sources.isbn` are promoted out of `bibliography_json` into indexed columns, stored **already normalized**. `normalize_doi` strips a `doi:` scheme or doi.org URL, lowercases (DOIs are case-insensitive by specification) and requires the `10.` registrant prefix — anything else is extractor noise that would merge unrelated works. `normalize_isbn` strips punctuation and requires 10 or 13 characters. DOI is checked before ISBN: a DOI identifies the work, an ISBN one edition of it.
2. Layer 4 requires normalized title **and** a shared author surname. Title alone matches a book against its own chapters; authors alone matches everything a prolific author wrote. `normalize_title` reuses `hashing.fold_for_match` (NFKC, accents stripped, punctuation collapsed) and drops the subtitle after `:`, where cataloguing diverges most. `author_surnames` compares last tokens, so "D. Kahneman" matches "Daniel Kahneman" **without introducing a fuzzy matcher and another uncalibrated threshold** — the exact complaint this ADR answers for layer 3.
3. A differing year is **reported, never used to reject**. A 2nd edition against a 1st is plausibly the same work to a reader and plausibly distinct to a bibliographer; that is the user's call, not the pipeline's.
4. Layer 4 **ignores `harvest.non_interactive_duplicate_action`** and defaults to `continue` without a TTY. This is the one place the layers disagree, and deliberately: merging is the irreversible direction (ADR-011: separating two documents again "is not a defined operation"), and title collisions between genuinely distinct works — book vs. chapter, edition vs. edition, translation vs. original — are common. A false positive silently loses a unique source; a false negative leaves two sources the user can `delete-source`.
5. Both layers run **after** `build_bibliographic_metadata` and **before** `generate_citekey`, so a duplicate never burns a disambiguated citekey on its way to being rejected.

### Why layer 5 stays — but ships disabled

The new layers do not replace semantic similarity; they run in front of it. Layer 5 is the only net for material whose metadata is unusable — a handout, a PDF with no cover page, a title the extractor guessed. Removing it would trade a documented-but-imperfect net for no net at all in exactly the cases the new layers cannot serve.

It nonetheless ships **off** (`harvest.semantic_duplicate_enabled: false`). Layer 5 answers one binary question per ingested file, and the price is embedding *every chunk of every source* to keep the target index alive: the cost scales with the corpus, the use with new files. Once layers 3 and 4 cover catalogued material deterministically, that trade is only worth making for a corpus dominated by metadata-less documents — which the operator knows and this ADR cannot.

The flag gates **write and read together**, and that coupling is the design, not an implementation detail: querying an index the pipeline stopped populating produces a *silent false negative*, not an error. `run_reindex` also leaves the `chunks` collection entirely alone while the flag is off — not even `--force` resets it, since resetting without repopulating would empty it as a side effect of a command whose contract is "rebuild". Turning the flag on requires `zettel reindex --collection chunks` to populate the target index from the existing corpus.

This also means `duplicate_chunk_threshold: 0.88` is inert by default. The uncalibrated knob ADR-011 flags is no longer on the default path.

### The scanned-PDF case

A scan never reaches any of these layers: ADR-033's `assert_pdf_has_text_layer` refuses it up front with an OCR suggestion. The duplicate appears only *after* the user OCRs it, where layers 1–2 miss (different bytes, different normalized text) and layers 3–4 catch it.

## Consequences

Layers 3 and 4 are pure SQLite and run before any embedding is computed, so the common duplicate case gets **cheaper**, not more expensive — the reordering is the performance argument, not just the precision one.

An exact DOI or ISBN is now an identity claim with no threshold to calibrate, answering ADR-011's first open question for the cases it covers. Layer 3's `0.88` remains uncalibrated for the cases it still owns; this ADR narrows its blast radius rather than fixing it.

Layer 5 being off by default is a **behavioural change** for anyone relying on it: two acquisitions of the same untitled handout now both ingest. That is the accepted cost of not paying for the index on every harvest, and it is reversible by one flag plus a reindex.

`db.record_duplicate` gains a `biblio` kind and `runs.duplicate_biblio_count`, surfaced in `zettel status`. `sources` gains two columns and two indexes; no backfill was written, as the vault is recreated from scratch.

Layer 4 scans `list_sources_with_authors()` linearly — a projection without the text blobs, since `extracted_text` and `lit_body` can be megabytes each. Fine at vault scale; if it ever is not, a normalized-title index is the next step.

## References

* `zettel/harvester/biblio_dedupe.py`, `zettel/harvester/pipeline.py` (`_process_file`)
* `zettel/harvester/chunking.py` (`chunk_and_persist`), `zettel/rebuild.py` (`run_reindex`) — the other two ends of the layer-5 flag
* `zettel/state.py` (`sources.doi`/`isbn`, `get_source_by_doi`, `get_source_by_isbn`, `list_sources_with_authors`, `record_duplicate`)
* `zettel/hashing.py` (`fold_for_match`, reused rather than duplicated)
* `tests/test_harvester_dedup.py`
