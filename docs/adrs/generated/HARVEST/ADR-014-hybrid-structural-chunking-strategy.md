# ADR-XXX: Hybrid Structural Chunking (H1-H6 Boundaries + Recursive Splitter)
**Status:** Accepted
**Date:** Unknown (foundational; predates tracked history)
**Depends on:** [ADR-XXX: Docling as Primary PDF Extractor with PyMuPDF Fallback](./needs-input/ADR-XXX-docling-primary-pdf-extractor-pymupdf-fallback.md)
**Related to:**
- [ADR-XXX: Layered Hashing Strategy for Deterministic Caching and Drift Detection](../INFRA/ADR-007-layered-hashing-strategy.md)
- [ADR-XXX: Three-Layer Page Inference Strategy for Chunk Page Metadata](./ADR-013-three-layer-page-inference-strategy.md)

## Context and Problem Statement

Every document harvested into the vault must be broken into chunks small enough to embed and retrieve individually, yet large enough to remain coherent units of meaning for the extraction LLM. A document's own heading hierarchy carries real structural intent — chapters, sections, subsections — that a purely size-based splitter would ignore, potentially cutting a chunk mid-argument or mid-list.

The harvest pipeline uses a two-stage hybrid strategy: documents are first split into chapters at H1/H2 boundaries, then each chapter is split into chunks by first honoring H3-H6 sub-section boundaries, and only when a sub-section still exceeds the configured maximum size is a recursive character-based splitter (paragraph, then line, then word, then character separators, with configurable overlap) applied on top. Chunk metadata records the heading path a chunk came from, its index within the chapter, and page-inference data derived separately from these same boundaries.

This decision affects every chunk of every source ever harvested — thousands of chunks per corpus — because chunk boundaries drive extraction prompt size, retrieval granularity, embedding count and cost, and the density of the resulting note graph. The strategy has been stable since the codebase's inception, with min/max/overlap exposed as tunable configuration but the two-stage structural-then-recursive logic itself unchanged.

## Decision Drivers

* Preserving a document's own heading hierarchy avoids splitting a conceptual unit (a section or subsection) across chunk boundaries, which a size-only splitter cannot guarantee.
* A single sizing strategy cannot fit all documents: sections vary from a few dozen to many thousands of characters, so oversized sections still need a fallback splitter.
* Overlap between splitter-generated chunks preserves context across a cut but re-embeds the overlapping text, directly increasing embedding cost per corpus.
* Chunk boundaries, once persisted, are effectively immutable in practice — changing chunking configuration requires a full rechunk and re-embedding of affected sources, with no partial-invalidation path.
* Documents without any H3-H6 markup (flat Markdown, plain text) must still produce usable chunks, so the strategy needs a graceful single-chapter/single-section fallback.

## Considered Options

* Hybrid structural (H1-H6) splitting with recursive-splitter fallback and overlap (chosen)
* Single-stage semantic chunking: recursive splitter only, no structural boundary detection
* Full-document chunking: one chunk per document, no sub-division

## Decision Outcome

Chosen option: hybrid structural splitting with a recursive-splitter fallback, because it preserves document-authored structure as the primary chunk boundary while still guaranteeing every chunk stays within a bounded size range, and the fallback's overlap mitigates the context loss that a hard size cut would otherwise introduce at section boundaries. [NEEDS INPUT: Was the choice of global (rather than per-source or per-document-type) chunking configuration a deliberate simplification, or has adaptive configuration by document type been evaluated and rejected for a specific reason?]

Because the structural stage runs first and the recursive splitter only engages when a structural unit is still too large, the two stages compose without conflicting: structure is respected wherever it exists, and size bounds are enforced everywhere else.

## Pros and Cons of the Options

### Hybrid structural + recursive-splitter fallback (chosen)

* Good, because it keeps chunk boundaries aligned with the document's own section structure whenever that structure is present
* Good, because the fallback splitter guarantees no chunk silently exceeds the configured maximum size
* Good, because overlap in the fallback path preserves context across a forced cut
* Bad, because overlap duplicates text across adjacent chunks, adding roughly 10-15% additional embeddings per corpus at the default 200-character overlap

### Single-stage semantic chunking (recursive splitter only)

* Good, because the logic is simpler, with one splitting pass and uniform sizing behavior
* Good, because chunk sizes are more predictable and cost is easier to estimate up front
* Bad, because it discards document structure entirely, risking mid-sentence or mid-argument cuts inside a section
* Bad, because heading-derived metadata (section path) would no longer be available for navigation or future section-scoped search

### Full-document chunking (one chunk per document)

* Good, because it eliminates chunking overhead and maximizes context available per extraction call
* Bad, because retrieval precision collapses — a single matching sentence forces the whole document into scope
* Bad, because extraction prompts and embeddings become large and expensive, particularly for long sources

## Consequences

Chunk boundaries are effectively permanent once a source is harvested: any change to `min_chars_per_chunk`, `max_chars_per_chunk`, or `chunk_overlap` requires the `rechunk` workflow to regenerate chunks from the already-extracted text and re-index them, since there is no fine-grained way to invalidate only the chunks affected by a parameter change. This makes chunking configuration a decision with corpus-wide reprocessing cost attached to any later revision.

The overlap that preserves context across forced cuts also means the same text can appear, verbatim, in more than one chunk; downstream deduplication and layer-3 semantic duplicate detection (ADR-XXX, Layered Hashing Strategy) must tolerate this rather than treat overlapping chunks as erroneous duplicates. Documents lacking H3-H6 markup fall back to whole-chapter chunks subject only to the recursive splitter, so their chunk metadata carries no meaningful section path — a gap for any future feature that depends on section-level navigation or search. [NEEDS INPUT: Is there an accepted target or ceiling for the overlap-driven embedding cost overshoot, or is the current ~10-15% considered acceptable indefinitely?]

## References

* `zettel/harvester.py:1400-1450` — chapter splitting on H1/H2 boundaries
* `zettel/harvester.py:1570-1635` — hybrid chunk splitting (H3-H6 boundaries, recursive-splitter fallback)
* `zettel/config.py` — chunking configuration schema (min/max chars, overlap)
* `config/config.yaml` — operational chunking defaults
* `zettel/paging.py:128-143` — heading-path tracking used in chunk metadata
