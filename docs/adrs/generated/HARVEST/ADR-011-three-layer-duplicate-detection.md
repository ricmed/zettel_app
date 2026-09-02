# ADR-XXX: Three-Layer Duplicate Detection Strategy for Source Ingestion
**Status:** Accepted
**Date:** 2026-07-04
**Depends on:**
- [ADR-XXX: Layered Hashing Strategy for Deterministic Caching and Drift Detection](../INFRA/ADR-007-layered-hashing-strategy.md)
- [ADR-XXX: Docling as Primary PDF Extractor with PyMuPDF Fallback](./needs-input/ADR-XXX-docling-primary-pdf-extractor-pymupdf-fallback.md)

**Related to:** [ADR-XXX: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)

## Context and Problem Statement

Every file dropped into the inbox has to be checked against everything already ingested before it is treated as a new source, because reprocessing a duplicate wastes LLM extraction calls, embedding budget, and vault space, and pollutes the graph with redundant notes. Duplicates show up at different fidelity levels: the exact same file saved at a new path, the same article re-exported in a different format (PDF vs. Markdown), and a lightly reformatted or edited copy of content already in the corpus. A single equality check cannot catch all three shapes at once.

A three-layer detection strategy was implemented in `_process_file()`, run in sequence before a file is accepted as a new source, each layer more expensive but more semantically meaningful than the last: a byte-level file checksum (renamed/moved copies), a normalized-extraction checksum (cross-format re-exports of the same content), and a ChromaDB embedding similarity search over sampled chunks (semantic near-duplicates). The first layer to match short-circuits the remaining checks and reuses the existing `source_id` without reprocessing; only a Layer 3 candidate requires a decision (interactive prompt or configured non-interactive default) because it is the only layer whose match is not unambiguous.

Every decision is recorded per run (`record_duplicate(run_id, layer)`) and surfaced in the `status` command, and both CLI (`harvest`, `run-all`) and the web harvest job route through the same three-layer path — the web job simply forces `interactive=False` and defers to the configured default.

## Decision Drivers

* Every source ingestion runs through this path, so its cost and correctness affect 100% of harvest runs and every downstream LLM/embedding call that a false negative would otherwise trigger.
* No single check level catches every duplicate shape: byte hashing misses cross-format re-exports, and content hashing misses semantically-edited or reformatted near-duplicates.
* Each layer's cost increases with the fidelity of what it detects (checksum, then extraction, then embedding), so ordering cheap-to-expensive avoids paying for a vector search when a cheaper layer already resolved the case.
* Layer 3 matches are inherently ambiguous — a near-duplicate is not the same guarantee as a hash match — so it is the only layer that needs a human decision or a configurable non-interactive default rather than an automatic reuse.
* Threshold tuning for semantic similarity is corpus- and embedding-model-specific, so the threshold and sample size were made configurable rather than hardcoded.
* Reusing an existing `source_id` on a false-positive match is effectively irreversible in practice, since re-extracting the document afterward is non-trivial, which raises the cost of getting the threshold wrong in either direction.

## Considered Options

* Three-layer sequential detection: file hash, extraction hash, semantic similarity (chosen)
* Single-layer detection using file hash only
* Vector-only (embedding similarity) detection, skipping the hash layers

## Decision Outcome

Chosen option: three-layer sequential detection, because each layer catches a duplicate shape that the cheaper layers structurally cannot — file hash only handles byte-identical copies, extraction hash additionally handles cross-format re-exports, and semantic similarity additionally handles reformatted or lightly edited near-duplicates — while running cheapest-first avoids incurring embedding cost whenever a cheaper layer already resolves the file as a known duplicate.

Because Layer 3 is a similarity match rather than an equality match, it is the only layer routed through a decision step (interactive prompt with a candidates table, or a configured non-interactive action) instead of an automatic reuse, keeping the irreversible action of merging into an existing source under explicit control.

## Pros and Cons of the Options

### Three-layer sequential detection (chosen)

* Good, because it catches renamed files, cross-format re-exports, and semantic near-duplicates, each with a check no more expensive than necessary for that fidelity level
* Good, because only the ambiguous case (Layer 3) requires a decision, keeping unambiguous matches (Layers 1-2) fully automatic
* Good, because every layer's cost is paid only when the cheaper layers before it fail to resolve the file
* Bad, because it is the most complex of the three options to reason about, with three independent checks and a separate decision-routing path for the last one

### Single-layer detection (file hash only)

* Good, because it is the simplest to implement and reason about, with no embedding cost at all
* Bad, because it would not catch a document re-exported into a different file format, causing duplicate processing of the same underlying content
* Bad, because it would not catch semantically identical or lightly edited content saved as a genuinely different file

### Vector-only detection (skip hash layers)

* Good, because it is a single deterministic query path with no layered branching
* Bad, because it would spend embedding budget on cases that a free byte-hash or extraction-hash comparison could resolve for free
* Bad, because it discards the unambiguous certainty that hash equality provides, routing even exact-copy cases through the same similarity threshold as genuine near-duplicates

## Consequences

Because Layer 3's threshold (`harvest.duplicate_chunk_threshold`, default 0.88) was set empirically rather than derived analytically, it needs corpus-specific recalibration if the embedding model or the nature of the corpus changes, and there is no documented procedure for detecting when recalibration is due. [NEEDS INPUT: What methodology or corpus was used to calibrate the 0.88 default, and is there a signal (e.g., a rising rate of user overrides) intended to trigger recalibration?]

Accepting a Layer 3 match reuses the existing `source_id` immediately, and there is no documented recovery path if a user later determines the match was a false positive — separating the two documents again is not a defined operation. [NEEDS INPUT: What is the intended recovery procedure if a Layer 3 duplicate decision is later found to be wrong?]

The three-layer path is stable and fully test-covered, so it functions as a fixed contract for the harvest phase: any future change to how sources are identified or re-ingested must preserve or deliberately revisit this sequencing, since downstream phases assume a `source_id` is a reliable identity for a single logical document.

## References

* `zettel/harvester/pipeline.py` — `_process_file`, orchestrates all three layers in sequence
* `zettel/harvester/duplicates.py` — `find_semantic_duplicate_candidates`, Layer 3 embedding query and candidate ranking; `resolve_duplicate_decision`, interactive/non-interactive decision routing; `sample_chunk_texts`, the chunk sample Layer 3 queries with; `HarvestAborted`, raised by the `abort` action
* `zettel/state.py` — `get_file_by_checksum`, `get_source_by_extraction_checksum`, `record_duplicate`
* `config/config.yaml` — `harvest.duplicate_chunk_threshold`, `harvest.duplicate_sample_size`, `harvest.non_interactive_duplicate_action`
