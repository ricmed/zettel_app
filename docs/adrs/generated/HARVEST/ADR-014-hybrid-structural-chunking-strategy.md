# ADR-XXX: Hybrid Structural Chunking (H1-H6 Boundaries + Recursive Splitter)
**Status:** Accepted
**Date:** Unknown (foundational; predates tracked history)
**Depends on:** [ADR-XXX: Docling as Primary PDF Extractor with PyMuPDF Fallback](./ADR-012-docling-pdf-extraction-pymupdf-fallback.md)
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

Chunk boundaries are effectively permanent once a source is harvested: any change to `chunk_size`, `chunk_overlap`, or `min_section_chars` requires the `rechunk` workflow to regenerate chunks from the already-extracted text and re-index them, since there is no fine-grained way to invalidate only the chunks affected by a parameter change. This makes chunking configuration a decision with corpus-wide reprocessing cost attached to any later revision.

The overlap that preserves context across forced cuts also means the same text can appear, verbatim, in more than one chunk; downstream deduplication and layer-3 semantic duplicate detection (ADR-XXX, Layered Hashing Strategy) must tolerate this rather than treat overlapping chunks as erroneous duplicates. Documents lacking H3-H6 markup fall back to whole-chapter chunks subject only to the recursive splitter, so their chunk metadata carries no meaningful section path — a gap for any future feature that depends on section-level navigation or search. [NEEDS INPUT: Is there an accepted target or ceiling for the overlap-driven embedding cost overshoot, or is the current ~10-15% considered acceptable indefinitely?]

## Addendum (2026-09-02): Fenced code blocks are atomic

**Status:** Accepted amendment — does not replace the decision above, constrains where its two stages apply.

The structural stage treated every `#`-prefixed line as document structure, including headings **inside** CommonMark fenced blocks. A document embedding a template or code sample (a HLD skeleton inside a ```` ```markdown ```` fence, for example) was therefore split into several chunks whose `section_path` was invented from the template's illustrative headings rather than from the document's own hierarchy, and each fragment landed above `min_section_chars` so `merge_small_sections` did not fold it back. Downstream, extract and review produced literature notes about fragments of a template instead of about the document.

Amendment:

* Headings H1–H6 whose offset falls inside a fenced span do **not** partition chapters (`split_into_chapters`) or sections (`split_chapter_into_sections`). Headings outside fences keep partitioning exactly as before.
* Fences are located by a line-based scanner (`iter_fenced_spans`): an opening fence is up to 3 spaces of indent followed by 3+ backticks or tildes plus an optional info string; a closing fence must be the same marker family (a backtick fence never closes a tilde fence), at least as long as the opening marker, and carry no info string — so ```` ```json ```` cannot close ```` ```markdown ````. An unclosed fence spans to EOF.
* In `split_chapter_into_chunks` the fence is an atom: the `RecursiveCharacterTextSplitter` is applied only to the prose between fences, never across a fence.
* **Size exception:** when a fence is larger than `chunk_size` (1500 in the operational YAML), a single oversized chunk is emitted. Cutting a template or a code block at a `\n\n` boundary is worse than one large chunk, so the size ceiling asserted above is deliberately not enforced for fenced content. `chunk_size` itself is unchanged — the exception is per-fence, not a global relaxation.
* Out of scope of the scanner, and therefore still able to affect boundaries: indented code blocks (4 spaces), Markdown tables outside fences, and raw HTML.

Consistent with the migration cost already described in *Consequences*, this changes boundaries only for sources chunked after the amendment: already-harvested sources keep their old chunks until the operator runs `zettel rechunk`. There is no silent re-chunking.

## Addendum (2026-09-02): Heading ATX on the first chunk of each section

**Status:** Accepted amendment — does not replace the decision above. Complements the fence addendum.

The structural stage used H1–H6 as split markers and stored the path in `section_path` metadata, but **did not** copy the heading line into the persisted chunk `text`. The extract prompt already receives `section_path` as locator, yet the embedding, the chunk dump, and the LIT source excerpt saw only the body. A section whose body is a fenced template or diagram (heading + ` ```mermaid ` with no surrounding prose) was therefore an orphan in the vector: the words that named the section never entered the hash or the embedding.

Amendment:

* Each section carries the original ATX line(s) in a `headings` list. The size/fence splitter still runs on **body only**.
* After pieces are produced, the heading(s) are prefixed onto **the first piece only** (`heading + "\n\n" + piece`). Continuations of the same section stay body-only.
* Prefixing happens **after** fence atomization. Prefixing before the fence split would turn a fence-only section into two chunks (title + fence).
* H1/H2 is restored on the first chunk of the chapter (preamble or first subsection). Synthetic chapters (`Documento completo`, `Introdução`) do not invent a `#`.
* Forward merge of a short section concatenates `headings` onto the surviving section (all appear on that unit's first chunk). Trailing merge injects the carried heading at the join in `text`; a heading-only piece is glued onto the following piece so it cannot detach from a fence.
* `chunk_id` is a hash of `text`, so the prefix is part of identity: identical bodies under **different** headings no longer collapse. Already-harvested sources keep old chunks until `zettel rechunk`.

## Addendum (2026-09-03): Piso de tamanho pos-splitter (`min_chunk_chars`)

**Status:** Accepted amendment — does not replace the decision above, adds a third filtering stage after the recursive splitter.

`min_section_chars` folds undersized **sections** (H3+) forward before splitting, but nothing floored the **pieces** the recursive splitter itself emits. Measured on the corpus (`data/state.db`, 680 chunks): 116 chunks (17%) landed below 200 characters — mostly the tail of a size-based cut, or an isolated Markdown horizontal rule (`---`) left standing alone between two `\n\n` breaks — and **100%** of them were rejected by the extraction LLM (Prompt 1), each still costing one call. This is a distinct failure mode from undersized sections: it happens *after* structural splitting, on pieces the splitter itself produced.

Amendment:

* A new `_merge_short_pieces` pass runs after `_glue_orphan_heading`, on the same per-section `pieces` list, before the heading prefix is applied. A piece shorter than `chunking.min_chunk_chars` (default `200`, the same value proven safe for `min_section_chars`) is merged into the **previous** kept piece — a short piece is almost always the tail of a cut, so the missing context sits behind it. A short piece with no previous piece yet (the first piece of the section) carries forward and merges into the next one instead. If every piece in a section is short, they all collapse into that section's single chunk — the same "whole unit is small, keep it as-is" outcome `merge_small_sections` already produces at the section level.
* `min_chunk_chars` joins `chunk_size`/`chunk_overlap`/`min_section_chars` in `compute_docling_config_hash`, so the pipeline flags corpora that need `zettel rechunk` to benefit from the new floor. As with every other chunking knob, already-harvested sources keep their existing chunks — there is no silent re-chunking.
* 200 characters was chosen as the *safe* floor: it eliminates all 116 structurally-doomed chunks in the measured corpus without discarding a single chunk the LLM would have accepted. A more aggressive floor (600 chars) was evaluated and rejected for this default — it would additionally eliminate 301 calls (44%) but at the cost of 35 accepted notes, i.e., real signal, not just noise. Operators who want the aggressive tradeoff can raise `min_chunk_chars` themselves; the default optimizes for zero false negatives.

## Addendum (2026-09-03): overlap operacional é 16%, não 10-15%, e é assimétrico por seção

**Status:** Accepted amendment — corrects a factual claim in the original decision text, does not change any behavior.

The original decision above states the "default 200-character overlap" produces "roughly 10-15% additional embeddings per corpus". Two things were wrong with that claim, independent of each other:

* The Pydantic *default* (`ChunkingConfig.chunk_overlap: int = 200`, over `chunk_size: int = 1000`) never matched the *operational* value in `config/config.yaml` (`chunk_overlap: 400`, over `chunk_size: 2500`) — the gap this ADR itself calls out as a general problem in the min-chunk-chars addendum's own audit context. The Pydantic defaults have since been aligned to the YAML (`zettel/config.py`), so `AppConfig()` with no YAML now exercises the same values as production.
* At the corrected, actual operational ratio, overlap is `400 / 2500` = **16%**, not 10-15% — closer to the original estimate than the divergent Pydantic default was, but still a different number than the ADR states, and one that moves whenever `chunk_size`/`chunk_overlap` are retuned in the YAML without a matching edit here.

Separately, and not previously documented at all: **the overlap is not uniform across the corpus.** `chunk_overlap` only ever applies *inside* `_split_preserving_fences`/`RecursiveCharacterTextSplitter`, which only runs when a section's text exceeds `chunk_size` (`split_chapter_into_chunks`, the `len(text) <= cfg.chunking.chunk_size` branch). A section that fits in one chunk — the common case for most H3+ subsections — becomes a single chunk with **zero** overlap with its neighbors. Only sections long enough to be split internally carry the 16% duplication, and even then only between the pieces of that one section, never across a section boundary. Any consumer reasoning about "the corpus has ~16% overlap" (layer-3 semantic dedupe, the `overlap_prefix_len` diagnostic in chunk dumps, a future embedding-cost estimate) needs to read that as a per-oversized-section figure, not a corpus-wide constant.

This resolves the `[NEEDS INPUT]` above about an accepted ceiling for the overlap-driven cost: there is no single ceiling to accept, because the actual overshoot is a property of how many sections exceed `chunk_size` in a given corpus, not a fixed percentage.

## References

Paths refreshed 2026-09-02 (chunking addenda); 2026-09-03 (`min_chunk_chars` addendum, overlap-ratio correction). The original references pointed into the monolithic
`zettel/harvester.py`, which ADR-027 split into a package; symbols are cited instead of
line ranges, since the line ranges are what rotted.

* `zettel/harvester/chunking.py` — `split_into_chapters` (H1/H2 chapter boundaries), `split_chapter_into_sections` (H3-H6 sub-sections; builds the `section_path` carried in chunk metadata), `merge_small_sections` (`min_section_chars` folding), `split_chapter_into_chunks` (recursive-splitter fallback), `chunk_and_persist` (persistence and indexing)
* `zettel/harvester/chunking.py` — addendum: `iter_fenced_spans` (fence scanner), `_headings_outside_fences` (heading filter), `_split_preserving_fences` (atomic fence in the size split); heading-prefix addendum: `_glue_orphan_heading`, `headings` on section records, prefix on first piece in `split_chapter_into_chunks`
* `zettel/harvester/chunking.py` — `min_chunk_chars` addendum: `_merge_short_pieces` (post-splitter floor)
* `tests/test_harvester_sections.py` — section splitting and merge rules; addendum: fence atomicity, info-string/marker-family rules, unclosed fence, oversized fence; heading prefix on first chunk, fence-only section, merge heading placement, checksum identity; `min_chunk_chars` floor: merge-back, merge-forward, all-short collapse, integration via `split_chapter_into_chunks`
* `zettel/config.py` — `ChunkingConfig`: `chunk_size`, `chunk_overlap`, `min_section_chars`, `min_chunk_chars`
* `config/config.yaml` — operational chunking defaults (`chunking.*`)
* `zettel/paging.py` — `compute_docling_config_hash` (includes `min_chunk_chars`); page-inference helpers consumed by `chunk_and_persist` (see ADR-013) — the heading path itself is built in `zettel/harvester/chunking.py`, not here
* [ADR-027: Harvest Phase as Python Package](./ADR-027-harvest-phase-as-python-package.md) — the module extraction that moved this code
