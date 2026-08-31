# ADR-XXX: Layered Hashing Strategy for Deterministic Caching and Drift Detection
**Status:** Accepted
**Date:** 2025-02-28
**Used by:** [ADR-XXX: Three-Layer Duplicate Detection Strategy for Source Ingestion](../HARVEST/ADR-011-three-layer-duplicate-detection.md)
**Related to:**
- [ADR-XXX: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](./ADR-003-hybrid-dense-bm25-retrieval.md)
- [ADR-XXX: Hybrid Structural Chunking (H1-H6 Boundaries + Recursive Splitter)](../HARVEST/ADR-014-hybrid-structural-chunking-strategy.md)
- [ADR-XXX: Docling as Primary PDF Extractor with PyMuPDF Fallback](../HARVEST/needs-input/ADR-XXX-docling-primary-pdf-extractor-pymupdf-fallback.md)
- [ADR-XXX: Single LLM Call Per Cluster with Intelligent Routing](../GARDEN/ADR-021-single-llm-call-per-cluster-routing.md)
- [ADR-XXX: System+Human Prompt Split for Provider-Agnostic Prompt Caching](../LLM/ADR-XXX-system-human-prompt-split-for-provider-agnostic-caching.md)

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

## References

* `zettel/hashing.py` (114 lines) — `normalize_text_for_hash`, `sha256_hex`, `compute_llm_call_checksum`, `compute_embedding_input_hash`
* `zettel/harvester.py:543,585,1624-1637` — file, extraction, and chapter checksum computation and comparison during harvest
* `zettel/extractor.py:189-196` — `llm_call_checksum` construction for deterministic response caching
* `zettel/connector.py:238-241,376-379` — `llm_call_checksum` and `compute_embedding_input_hash` (embed-skip) usage
* `zettel/state.py:195` — `llm_cache` table storing checksum-keyed cached responses
