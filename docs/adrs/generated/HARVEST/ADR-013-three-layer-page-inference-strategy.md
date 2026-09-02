# ADR-XXX: Three-Layer Page Inference Strategy for Chunk Page Metadata

**Status:** Accepted
**Date:** 2024-08-30
**Depends on:** [ADR-XXX: Docling as Primary PDF Extractor with PyMuPDF Fallback](./ADR-012-docling-pdf-extraction-pymupdf-fallback.md)
**Related to:** [ADR-XXX: Hybrid Structural Chunking (H1-H6 Boundaries + Recursive Splitter)](./ADR-014-hybrid-structural-chunking-strategy.md)

## Context and Problem Statement

The harvest pipeline splits every source document into text chunks, and each chunk needs a page number so that permanent notes can carry precise, page-level citations back to the original PDF or Markdown file. Not every chunk carries a clean, ready-to-use page number by the time it reaches this stage: PDF page metadata is not always cleanly extractable, plain-text/Markdown sources have no page metadata at all, and OCR or non-uniform layouts introduce noise into any pattern-based detection. The pipeline needed a way to assign a `page_in_file` (and, from it, a `page_in_book`) to as many chunks as possible, while being explicit about how certain each assignment is, and without letting a page-detection failure block the rest of the harvest.

This is a foundational decision: page metadata is computed once per chunk during harvest and persisted (SQLite and Chroma), feeding the reading experience, citation accuracy, and retrieval context for the lifetime of that chunk. Every PDF and Markdown source harvested goes through this logic, so the trade-off between coverage, accuracy, and complexity affects the entire corpus rather than an edge case.

Chunk-level page resolution runs during harvest, before a fallback pass fills any remaining gaps using neighboring chunks that already have a page value; the resulting file-page is then converted into a book-page using a separately resolved content-start offset. The combination determines what page number, if any, a reader ultimately sees attached to a permanent note's citation.

## Decision Drivers

* Precise page citations are core to the page-aware reading and citation experience in permanent notes, so most chunks need some page value rather than none.
* No single detection method achieves full coverage on its own: explicit PDF page metadata is the most accurate but is not always extractable; text-pattern matching works without metadata but is prone to false positives; interpolation can fill remaining gaps but only holds under a linear chunk-to-page assumption.
* A page-detection failure must not block the harvest of a source — a chunk without a confident page should still be indexed and usable, just marked accordingly.
* Downstream consumers (citations, retrieval context, cost heuristics) need to distinguish an exact page from an estimated one, which requires recording a confidence level per chunk rather than a bare page number.
* Manual, per-document page annotation does not scale across a growing personal corpus, so the default path needs to be automatic.

## Considered Options

* Three-layer cascade: explicit PDF metadata, then text-pattern matching, then interpolation between known pages (chosen)
* Single layer: explicit PDF metadata only, no fallback for chunks it cannot resolve
* Text-pattern matching only, skipping PDF metadata extraction entirely

## Decision Outcome

Chosen option: "Three-layer cascade", because it maximizes the share of chunks that receive a page value while preserving a strict accuracy ordering — explicit PDF page metadata is always preferred when available, text-pattern matching is used only when metadata is absent, and interpolation is used only to fill remaining gaps between chunks that already have a page from one of the first two layers. Each chunk records which layer produced its value as a confidence level (`explicit`, `inferred`, or `unknown`), so a downstream reader or feature can tell an exact page from an estimate instead of treating all page values as equally trustworthy.

A related but separate mechanism, the content-start offset (mapping a file-local page to a book-local page), is resolved independently per source and applied after the file-page is known; it is not part of this three-layer decision but consumes its output.

## Pros and Cons of the Options

### Three-layer cascade (chosen)

* Good, because it covers PDF, Markdown, and OCR-derived sources instead of leaving non-PDF or metadata-poor sources entirely without page data.
* Good, because the accuracy ordering is explicit: a later layer only fires when an earlier, more trustworthy one has nothing to offer.
* Good, because per-chunk confidence lets consumers of page data choose whether to trust an "inferred" value or treat it as approximate.
* Bad, because it is three independent mechanisms with different failure modes, which adds surface area to reason about and to test end-to-end.
* Bad, because the interpolation layer assumes a roughly linear chunk-to-page mapping, which degrades on documents with non-uniform chunk sizes or heavy structural variation (e.g., footnote-dense academic text).

### Explicit metadata only

* Good, because it is fully deterministic with no risk of false positives from misread numerals.
* Good, because it is the simplest of the three options to reason about and test.
* Bad, because a large share of chunks are left with no page value at all whenever clean PDF page metadata is unavailable, which includes every Markdown source by construction.
* Bad, because it offers no fallback for OCR or scanned documents where metadata extraction is unreliable.

### Text-pattern matching only

* Good, because it does not depend on PDF-specific processing and works directly on chunk text.
* Good, because it is comparatively fast to run over a corpus.
* Bad, because it has a high false-positive rate against OCR noise, margin annotations, and page-like numerals that appear in body text.
* Bad, because it forgoes the most reliable source of page data (PDF metadata) even when that data is available.

## Consequences

Because page values are computed once at harvest time and persisted per chunk, correcting a wrong content-start offset or a bad page inference later requires reprocessing the affected chunks rather than a simple metadata edit — page data is effectively immutable once a source has been harvested. Chunks that fall through all three layers are indexed with an "unknown" confidence rather than being blocked, which keeps harvest resilient but means some chunks silently carry no page citation unless a caller checks the confidence field.

The interpolation layer's linear assumption means inference quality tracks how uniformly a document was chunked; documents that chunk unevenly (long structural sections, heavy footnoting) can produce "inferred" pages that look complete but are less reliable than their `explicit` counterparts, without any additional signal beyond the recorded confidence level to flag this to a reader.

[NEEDS INPUT: The text-pattern layer's regex set is currently fixed in code and tuned for Portuguese-language page markers (e.g., "página"). Should these patterns become configurable per corpus or language, or is a fixed, curated set an acceptable long-term constraint?]

## References

* `zettel/paging.py` — `extract_page_hint`, the dispatcher that prefers explicit page metadata and falls back to text-pattern matching; `lookup_page_for_chunk`, the Docling page-map lookup that supplies that metadata
* `zettel/paging.py` — `infer_missing_page` and `apply_page_inference`, the interpolation that fills remaining gaps between chunks with a known page
* `zettel/paging.py` — `PAGE_PATTERNS`, the text-pattern definitions used as the fallback detection layer; `PAGE_BREAK_MARKER` / `page_map_from_marked_markdown`, the Docling page-break map that made the regex layer a last resort
* `zettel/harvester/chunking.py` — `chunk_and_persist`, per-chunk page assignment and the file-to-book offset (`compute_page_in_book`)
* `zettel/paging.py` — `resolve_content_paging` and `suggest_content_start`, content-start offset resolution consumed by the offset computation
* `zettel/harvester/set_paging.py` — `run_set_paging`, repairs the printed offset on an existing source without re-extracting
