# ADR-XXX: Layered Hashing Strategy for Deterministic Caching and Drift Detection
**Status:** Accepted
**Date:** 2025-02-28
**Used by:** [ADR-XXX: Three-Layer Duplicate Detection Strategy for Source Ingestion](../HARVEST/ADR-011-three-layer-duplicate-detection.md)
**Related to:**
- [ADR-XXX: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](./ADR-003-hybrid-dense-bm25-retrieval.md)
- [ADR-XXX: Hybrid Structural Chunking (H1-H6 Boundaries + Recursive Splitter)](../HARVEST/ADR-014-hybrid-structural-chunking-strategy.md)
- [ADR-XXX: Docling as Primary PDF Extractor with PyMuPDF Fallback](../HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md)
- [ADR-XXX: Single LLM Call Per Cluster with Intelligent Routing](../GARDEN/ADR-021-single-llm-call-per-cluster-routing.md)
- [ADR-XXX: System+Human Prompt Split for Provider-Agnostic Prompt Caching](../LLM/ADR-025-prompt-caching-system-human-split.md)

<!-- NOTE: Related-to count (5) exceeds the recommended max of 3 — all five are manual/reciprocal-manual relationships preserved per the manual-relationship exception. -->

## Context and Problem Statement

The pipeline needs a way to know, cheaply and deterministically, whether two pieces of content are "the same" at several different granularities: the same file re-uploaded, the same extracted text produced from a different file format, the same chunk seen twice, the same LLM call issued twice, and the same note body unchanged since its last embedding. Each of these checks backs a real cost-saving or correctness mechanism — skipping re-processing on harvest, skipping duplicate chunks in dedupe, reusing a cached LLM response instead of calling the model again, and skipping re-embedding of an unchanged note.

A layered checksum strategy was implemented: `file_checksum -> extraction_checksum -> chapter_checksum -> chunk_checksum -> llm_call_checksum -> note_semantic_checksum`, each a SHA-256 digest, with `chapter_checksum` and `chunk_checksum` computed over canonically normalized text rather than raw bytes. Normalization (NFKC Unicode form, CRLF-to-LF, whitespace collapsing, PDF hyphenation repair, blank-line limiting) exists specifically to prevent false drift: the same logical content extracted from a PDF versus a native Markdown file, or re-saved with different line endings, must still hash identically.

The chapter-level layer, which the evidence gathered for this decision treated as unconfirmed, is in fact implemented (`harvester.py`, chapter upsert path) and follows the same normalize-then-hash pattern as the chunk layer. The strategy has been stable since introduction; the only later addition, `note_semantic_checksum` for embed-skip logic, followed the same normalization convention rather than introducing a new one.

## Decision Drivers

* Harvest's layer-3 semantic duplicate detection and deterministic LLM-response caching both depend on checksums matching exactly for identical content, across at least eight modules (harvester, extractor, connector, gardener, ask, article, sync, rebuild).
* Hashing raw bytes or raw text would register whitespace, Unicode variant forms, and PDF-specific hyphenation artifacts as content drift, causing unnecessary reprocessing and cache misses.
* Different consumers need different invalidation granularities: a whole-file skip is too coarse for chunk-level dedupe, and chunk-level is too coarse for detecting whether a note's embeddable text changed.
* Normalization must be applied identically wherever hashing happens, so that content arriving through different extraction paths (PDF via Docling, native Markdown) becomes comparable.
* Once chosen, the hash algorithm and normalization rules become effectively load-bearing: changing either invalidates every previously stored checksum with no defined migration path.

## Considered Options

* Layered SHA-256 checksums over canonically normalized text (chosen)
* Hashing raw, unnormalized text/bytes at each layer

## Decision Outcome

Chosen option: layered SHA-256 checksums over canonically normalized text, because it gives every consumer a cheap, deterministic equality check at the granularity it needs, while the shared normalization step keeps checksums stable across formatting noise and across extraction formats (PDF vs. Markdown) for the same underlying content. [NEEDS INPUT: Why SHA-256 specifically rather than a faster non-cryptographic hash such as xxHash — deduplication and cache-keying here do not require cryptographic collision resistance, only low accidental-collision probability.]

Because normalization is shared code (`normalize_text_for_hash`) rather than reimplemented per layer, all six checksum layers stay consistent with each other by construction — a chunk and the extraction it came from will not silently diverge in how whitespace or hyphenation is treated.

## Pros and Cons of the Options

### Layered SHA-256 over normalized text (chosen)

* Good, because deterministic LLM-call caching (extractor, connector, gardener, ask, article) avoids redundant model calls when the same prompt, chunk, model, temperature, language, and RAG context recur
* Good, because normalization prevents PDF-vs-Markdown extraction of the same source, or re-saved line endings, from being treated as different content
* Good, because six independent granularities let each consumer skip exactly the work it doesn't need to redo (file skip, chunk dedupe, embed skip)
* Bad, because `llm_call_checksum` includes `temperature` but not `top_p`, so a `top_p`-only configuration change silently reuses a stale cached response
* Bad, because changing the normalization rules or hash algorithm invalidates every stored checksum at once, with no reconciliation strategy defined

### Raw, unnormalized hashing per layer

* Good, because it removes the shared normalization step entirely, reducing code to maintain
* Bad, because whitespace variance, Unicode form differences, and PDF hyphenation artifacts would register as false content drift
* Bad, because PDF and Markdown extraction of the same source would produce different `extraction_checksum` values despite identical semantic content, defeating harvest's cross-format duplicate detection

## Consequences

All checksum-dependent behavior — harvest idempotency, chunk dedupe, LLM-response caching, and embed-skip — relies on one shared normalization function, so any future change to normalization (for example, a different treatment of PT-BR diacritics) is a breaking, cross-cutting migration rather than a local one. [NEEDS INPUT: What is the intended handling for orphaned `llm_cache` and chunk-checksum entries if normalization rules or the hash algorithm ever change — delete, migrate, or retain for rollback?]

Normalization intentionally preserves PT-BR diacritics, which diverges from the separate `unicode61 remove_diacritics` normalization SQLite FTS5 uses for lexical search — the two normalization schemes are calibrated for different purposes (drift-safe hashing vs. lexical matching) and are not meant to converge, but the divergence is not documented anywhere near either implementation. [NEEDS INPUT: Should this divergence between hashing normalization and FTS5 normalization be explicitly documented as intentional, given it could otherwise look like an inconsistency to a future maintainer?]

The `top_p` gap in `llm_call_checksum` is a live cache-correctness edge case rather than a hypothetical: any run that changes only `top_p` between calls will incorrectly serve a cached response computed under the old value.

## Addendum (2026-09-03): PDF hyphenation repair moved into the extraction path

**Status:** Accepted amendment — does not replace the decision above, extends where the repair runs.

Until this amendment, `normalize_text_for_hash`'s hyphenation repair (`"word-\ncontinuation"` -> `"wordcontinuation"`) only ever ran at hash time, computed over `extraction_checksum`/`chapter_checksum`/`chunk_checksum` — never applied to the text actually persisted in `sources.extracted_text` / `chunks.text`. The broken hyphen reached the extraction LLM prompt, the embedding, and the `anchor_quote` field verbatim, which both hurt retrieval and polluted an `anchor_quote` that is supposed to be a literal copy of the source. It also meant two texts differing only by a hyphenated line break collided at `extraction_checksum` (harvest's layer-2 duplicate detection, [ADR-011](../HARVEST/ADR-011-three-layer-duplicate-detection.md)) while still producing different chunks and embeddings downstream — the checksum layer and the persisted content silently diverged on this one point.

Amendment:

* The regex moved out of `normalize_text_for_hash` and into a new public function, `hashing.dehyphenate_pdf_linebreaks`, called from **both** places: once by `extract_pdf_docling` (`zettel/harvester/extract.py`) right after the Docling markdown export, before the text is ever persisted or hashed; and once (idempotently) by `normalize_text_for_hash` itself, so the hashing path stays correct even for text that reaches it from elsewhere without going through PDF extraction first.
* Refined while extracting: the original regex removed the hyphen unconditionally, silently merging a genuine hyphenated compound (`"bem-\nvindo"` -> `"bemvindo"`) exactly like a line-wrapped word tail (`"pala-\nvra"` -> `"palavra"`). The new function preserves the hyphen when the character after the break is uppercase — a cheap, deliberately weak signal of a genuine compound (a heading, a proper noun) rather than a split word tail. A genuine compound whose continuation happens to be lowercase is still merged incorrectly; there is no cheap fix for that short of a dictionary lookup, and none was attempted.
* PDF-only by construction: the call site is inside `extract_pdf_docling`, never on the Markdown extraction path, so a native Markdown file's legitimate end-of-line hyphen is untouched.
* `extraction_checksum` changes for a PDF re-harvested after this amendment (the persisted text itself is now different, not just the hash normalization of it). Already-harvested sources do not change on their own — the corpus needs an explicit re-harvest, not `rechunk`, since `extracted_text` is the origin data this addendum affects. No silent migration.

## References

* `zettel/hashing.py` — `normalize_text_for_hash`, `sha256_hex`, `file_sha256`, `short_hash`, `compute_llm_call_checksum`, `compute_embedding_input_hash`; addendum: `dehyphenate_pdf_linebreaks` (single implementation reused by both the extraction and the hashing path)
* `zettel/harvester/extract.py` — addendum: `extract_pdf_docling` calls `dehyphenate_pdf_linebreaks` on the assembled text before returning it for persistence
* `zettel/harvester/pipeline.py` — `_process_file`, file checksum (`file_sha256`) and extraction checksum computation and comparison during harvest
* `zettel/harvester/chunking.py` — `chunk_and_persist`, chapter and chunk checksums (the chapter checksum is what skips unchanged chapters)
* `zettel/extractor.py` — `compute_llm_call_checksum` construction for deterministic response caching
* `zettel/connector.py` — `compute_llm_call_checksum` and `compute_embedding_input_hash` (embed-skip) usage
* `zettel/state.py` — `llm_cache` table storing checksum-keyed cached responses; `get_cached_llm_response` / `cache_llm_response`
