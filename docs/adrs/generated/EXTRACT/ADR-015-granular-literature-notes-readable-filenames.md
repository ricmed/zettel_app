# ADR-XXX: Granular Per-Chunk Literature Notes with Readable Filenames

**Status:** Accepted
**Date:** 2026-08-28
**Used by:** [ADR-XXX: Confidence-Band Human-in-the-Loop Approval Gate](../REVIEW/needs-input/ADR-017-confidence-band-hitl-approval-gate.md)
**Related to:**
- [ADR-XXX: ChromaDB Embedded Client as Vector Store](../INFRA/ADR-002-chromadb-embedded-vector-store.md)
- [ADR-XXX: Dual-Store Persistence Without Cross-Store Transactions](../INFRA/ADR-005-dual-store-persistence.md)

## Context and Problem Statement

The EXTRACT phase must turn each processed chunk of a source into a literature note that a human can review and that later phases (CONNECT) can consult. The system previously used a monolithic model: one literature index note per source, merging every chunk's extraction into a single file. This made per-chunk confidence tracking and selective HITL approval impossible — review was all-or-nothing per source, and it was hard to trace which part of a note came from which passage.

Commit 508d4c0 (2026-08-28) replaced this with a granular, chunk-per-literature-note design. Each chunk now produces its own draft note under `00_Inbox/Review/{Citekey}/`, promoted on approval to `20_Literature/{Citekey}/`. Filenames are human-readable (`LIT - AuthorYear - pNNN - topic-slug-HASH.md`, topic slug derived from the LLM-generated summary) rather than generic (`chunk_001.md`), and citekeys stay out of vault paths entirely — the citekey is the folder name, not embedded with an `@` in the filename. Each approved note also carries a `zettel:auto-source-excerpt` managed block containing the original chunk text, so a reviewer can compare the LLM's interpretation against the source passage without embedding raw source text into the semantic index.

This is a structural decision affecting vault organization, note uniqueness guarantees, the REVIEW approval workflow, and when literature notes become available to CONNECT. Sources harvested before this change require a re-run of `extract` + `review`; no automatic migration exists.

## Decision Drivers

* Per-source monolithic notes prevented independent confidence tracking and selective approval at the chunk level.
* Reviewers need to judge LLM extraction quality against the original passage without permanently embedding raw source text in the semantic index.
* Generic chunk identifiers (`chunk_001.md`) give a human browsing the vault no indication of a note's content without opening it.
* Filenames must be unique within a source even when two chunks produce similar or identical topic slugs.
* Unapproved drafts should not pollute the `literature_notes` embedding collection, so indexing must be deferred past extraction.
* The `LiteratureChunkOutput` summary is already produced by the LLM during extraction, making it available for reuse as a filename source without an extra model call.

## Considered Options

1. Granular chunk-per-note with readable filenames and a source-excerpt managed block (chosen)
2. Monolithic LIT-per-source note merging all chunks (legacy, removed)
3. Granular chunk-per-note with generic, non-descriptive filenames (e.g., `chunk_001.md`)

## Decision Outcome

Chosen option: granular chunk-per-note with readable filenames and a source-excerpt managed block, because it gives each chunk independent processing metadata (`literature_id`, `review_confidence`, timing) and lets REVIEW approve or reject chunks individually rather than an entire source at once, while the readable filename and preserved excerpt make the vault navigable and auditable by a human without any tooling. Reusing the LLM's own summary for the topic slug avoids a second extraction pass, and moving Chroma indexing to REVIEW (`review.approve_chunk()`, after the human decision) keeps the `literature_notes` collection free of unapproved drafts.

[NEEDS INPUT: What motivated prioritizing human-readable filenames specifically — was there observed friction (support requests, reviewer complaints) with the prior monolithic or a generic chunk-numbered scheme, or was this a preemptive design choice made without end-user feedback?]

### Positive Consequences

* Confidence, approval status, and processing metadata are tracked per chunk instead of per source, enabling partial approval.
* A reviewer can open a note's filename alone and infer its topic and source page without opening the file.
* The `auto-source-excerpt` managed block lets HITL review compare LLM output to the original text without that text living in the vector index.
* Literature notes are embedded to Chroma only after human approval, keeping the `literature_notes` collection free of unvetted drafts.

### Negative Consequences

* One file per chunk multiplies the number of vault files compared to one file per source.
* The change is breaking for sources harvested under the old model; they must be re-run through `extract` + `review` with no automated migration path.
* Filename generation is non-trivial: it depends on slugifying an LLM summary and appending a short hash for uniqueness, rather than a simple counter.

## Pros and Cons of the Options

### Granular with readable filenames + excerpt block (chosen)

* Good, because filenames convey content and page location at a glance.
* Good, because per-chunk approval and confidence tracking are possible.
* Good, because source excerpts support review without embedding raw text.
* Bad, because filename quality depends on LLM summary quality — a short or generic summary produces a poor slug.

### Monolithic LIT-per-source (legacy, rejected)

* Good, because it produced fewer vault files per source.
* Good, because filename generation was simpler (no slug or hash needed).
* Bad, because it lost per-chunk confidence tracking entirely.
* Bad, because HITL review was all-or-nothing for an entire source's chunks.

### Granular with generic filenames (rejected)

* Good, because filename generation would avoid slug extraction from an LLM summary entirely.
* Good, because collision avoidance would be simpler (sequential index or hash only).
* Bad, because a filename like `chunk_001.md` gives no indication of the note's content.
* Bad, because locating a specific topic among many chunk files would require opening each one.

## Consequences

Because the topic slug is derived from the LLM-generated summary, filename quality is coupled to LLM output quality: a terse or generic summary produces an uninformative slug, and there is no fallback heuristic when this happens. The short hash appended for uniqueness has a collision probability that scales with the number of chunks per source, though this has not caused an observed incident in the two days since introduction.

Manual renaming or moving of a literature note file is possible but leaves `literature_note_path` in StateDB out of sync until `zettel sync-manual` is run; this coupling is not enforced by tooling and depends on the operator remembering to resync. Legacy sources harvested before commit 508d4c0 remain on the monolithic model until manually re-run through `extract` + `review` — there is no scheduled or automatic migration, so the vault can contain a mix of both models indefinitely.

[NEEDS INPUT: Is a migration tool planned for legacy monolithic sources, or is a manual re-run of `extract` + `review` per source the permanently accepted approach?]

[NEEDS INPUT: Has the short-hash collision probability been evaluated for sources with a large number of chunks (e.g., 100+), or is the current entropy considered sufficient without a formal analysis?]

## References

* `zettel/extractor.py:296-404` — draft literature note creation, ULID assignment, `_write_literature_draft()`
* `zettel/vault.py` — `literature_chunk_filename()`, `build_literature_chunk_note()`, `literature_source_dirname()`
* `zettel/review.py:387-481` — `approve_chunk()`: draft promotion, managed-block excerpt insertion, Chroma upsert
* `zettel/schemas.py` — `LiteratureChunkOutput`, `PermanentNoteCandidate`
