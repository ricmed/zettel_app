# ADR-047: Chapter Summaries as a Library-Level Routing Index

**Status:** Accepted
**Date:** 2026-09-07
**Depends on:** [ADR-003](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md), [ADR-010](./ADR-010-retrieval-result-transparency-hits-vs-candidates.md)
**Amends the scope of:** [ADR-036](./ADR-036-topic-index-routing-not-representation.md) — its deferred "corpus-wide library index"
**Related to:** [ADR-015](../EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md), [ADR-034](../EXTRACT/ADR-034-optional-author-judgement-fields.md), [ADR-043](./ADR-043-distant-analogies-as-suggestions.md), [ADR-013](../HARVEST/ADR-013-three-layer-page-inference-strategy.md), [ADR-037](../CLI/ADR-037-llm-cost-preflight-estimate.md)

## Context and Problem Statement

The vault answers "which **idea** responds to this question?" — `ask` retrieves
permanent notes, scores them, cites them. It cannot answer "which **material**
treats this subject?", which is the question a researcher asks *before* reading:
give me the books, the chapters worth opening, and how much I already extracted
from each.

Nothing in the schema supported it. `chapters` had five columns
(`chapter_id`, `source_id`, `title`, `chapter_checksum`, `locator`) and none of
them was a summary; `sources` had `extracted_text` (raw) and `lit_body` (the
rendered index note), no summary either. `Retriever` was hardwired to
`permanent_notes` on both the dense and the lexical side.

[ADR-036](./ADR-036-topic-index-routing-not-representation.md) closed this door
on purpose:

> Deliberately out of scope: a corpus-wide library index (the research says only
> with evidence), a hierarchy of indexes, replacing BM25/RRF with the index, and
> any claim that the index "beats RAG".

This ADR reopens exactly that item, and only that item. The other three
exclusions stand.

## Decision Drivers

* Three prior decisions constrain any answer here, and each of them is a way to
  get this wrong:
  * **ADR-015's amendment** removed the `literature_notes` collection because
    *nothing read it back*. Any new embedded surface must ship with a named
    reader.
  * **ADR-034** refuses to re-embed text derived from already-embedded chunks —
    double-counting the same passage.
  * **ADR-043** holds that an LLM artifact driving retrieval is a *guess*, and a
    guess written without a human gate must stay a suggestion.
* The floor at `min_vector_similarity: 0.70` was measured on note-vs-query
  cosines. A number measured on one text distribution says nothing about another.
* A count of notes per chapter is a fact in SQLite. Nothing should be able to
  turn it into a model's opinion.
* A chapter that produced *no* permanent note is invisible to every existing
  retrieval path, and it is precisely the chapter a reader might still want.

## Considered Options

* **Vault placement:** managed blocks on the existing literature index note / a
  new note type per chapter / reuse ZTL or granular LIT.
* **Search unit:** the source / the chapter / both.
* **Search signal:** group existing note hits by chapter / embed chapter
  summaries / both.
* **Summary input:** the chapter's real text / the chunk summaries `extract`
  already stored.
* **Floor:** reuse `retrieval.relevance_floor` / a separate `chapter_floor`.

## Decision Outcome

**Chosen: the chapter is the search unit, its summary is a routing artifact, and
the catalog fuses it with the note signal already available.**

### 1. Routing, not representation — the rule that makes this safe

A chapter summary tells a reader **where to look**. It is never quoted as
support for a claim: evidence still comes from the permanent note or the source
excerpt. That single boundary answers all three inherited objections.

* Against **ADR-043**: the failure mode of a bad summary is a chapter ranked
  wrongly in a catalog listing — recoverable, visible, and bounded. It cannot
  corrupt the graph, cannot fabricate a citation, and cannot put an unendorsed
  claim into an answer. The distinction ADR-043 asks us to preserve is *a
  distant analogy is a guess about mechanism and can be wrong*; a summary is a
  guess about emphasis, and it never crosses into evidence.
* Against **ADR-034**: the topic index is also derived from already-embedded
  text and is legitimate *because it routes*. Same argument, same conclusion.
* Against **ADR-015's amendment**: the reader is `zettel/catalog.py`, written in
  the same change. `zettel catalog` is the only consumer, and it exists.

The boundary is enforced, not just documented: `tests/test_catalog.py` parses
`zettel/ask.py` and fails if it ever reaches `search_chapter_summaries`,
`query_chapter_summaries`, or the chapter floor — the same shape as
`tests/test_prompts.py` pinning the absence of `corroborates`.

### 2. Two artifacts, two jobs

| Artifact | Built from | Embedded? | Job |
|---|---|---|---|
| **Chapter summary** | the chapter's real text (`chunks.text`) | **yes** — Chroma `chapter_summaries` + `fts_chapter_summaries` | unit of search |
| **Source summary** | a *reduce* over the chapter summaries | **no** | reading, and composition |

The chapter is the search unit because a 400-page book "treats" almost
everything and does not discriminate; the book falls out by grouping its
chapters. The source summary is deliberately **not** embedded — nothing would
read it, and a second write-only collection is the defect this ADR is trying not
to repeat. It is one cheap call that never re-reads the book.

Summaries come from the **real chapter text**, not from the `summary_json` that
`extract` already stored. Those chunk summaries are capped at 280 chars and
`schemas.py` calls them "navigation material, not a semantic contract"; worse,
they only exist for chunks the pipeline *accepted*, so a chapter whose chunks
were all rejected as `fragmented` would summarize to nothing — and that is one of
the cases this feature exists to cover. A chapter above
`summarize.max_input_chars` is map-reduced, its partials folded back through the
same prompt.

### 3. Two signals, fused at the chapter level

`zettel catalog` calls **no LLM at all**. The answer is a ranked table plus a SQL
aggregate; routing it through `prompts/ask.md` would ask a model to enumerate a
count it could hallucinate.

* **Signal A — notes.** `Retriever.search_notes` already works; each hit is
  walked back `note -> concepts.chunk_id -> chunks.chapter_id`. This is the
  strongest evidence in the vault (human-approved, already calibrated) and costs
  nothing new. A chapter reached this way **does not face the chapter floor** —
  its evidence is a note that already cleared the note floor.
* **Signal B — summaries.** `Retriever.search_chapter_summaries`. Covers the
  chapter with zero yield, and the source harvested but not yet connected.

Every row records which signals found it (`via_notes`, `via_summary`,
`matched_notes`, `summary_similarity`, `floor_reason`), and `CatalogResult`
carries `sources` **and** `candidates` — ADR-010's contract, so an over-strict
floor is observable instead of returning a silent nothing.

### 4. A separate floor, measured on its own distribution

`retrieval.chapter_floor` is its own `RelevanceFloorConfig`. Inheriting the note
floor would be a category error regardless of the number: a chapter summary is
longer and more diffuse than a note, so it scores lower against the same query.
`_apply_relevance_floor` takes its config as a parameter — identical reasoning,
independently measured numbers — and the note path is untouched.

**Measured 2026-09-07** with
`scripts/probe_relevance_floor.py --collection chapter_summaries` over 71
summarized chapters (`ollama/qwen3-embedding@1024d`):

| band | n | min | median | max |
|---|---|---|---|---|
| off-domain | 6 | 0.676 | 0.685 | **0.693** |
| self-match | 20 | **0.745** | 0.821 | 0.883 |

A scalar does separate the two (margin 0.052), so the floor belongs in
`(0.693, 0.72]`. **Set to `0.70`** — above every measured off-domain query, and
0.045 below the easiest retrieval there is, leaving headroom for a real question
(which scores below self-match by construction). The initial `0.60` let all six
off-domain queries through; after the change they return zero chapters by
summary.

That `0.70` equals the note floor is **two independent measurements agreeing, not
inheritance** — `tests/test_config.py` pins the two as separate objects and
deliberately does *not* assert their values differ, since a future probe could
legitimately land on the same number again.

Re-measure after any embedding change: the number does not transfer between
models. The caveat the probe prints itself stands — 71 chapters from a narrow
corpus compress the range, so this motivates a value rather than settling one.

### 5. Vault placement: managed blocks, no new note type

`auto-source-summary` and `auto-chapter-map` on the **existing** literature index
note (`20_Literature/LIT - AuthorYear - slug.md`), beside `auto-lit-index` and
`auto-topic-index`. `summarize._write_blocks` owns the `## Resumo geral` and
`## Mapa de capitulos` headings, mirroring `topic_index._write_block` so the note
builders never scaffold them.

Rejected: a ZTL (atomic by contract, would enter `permanent_notes` and pollute
dedupe, gardener clustering, corroboration edges and the note floor); a granular
LIT (per *chunk*, bound to a `chunks` row with a checksum contract, and the HITL
review unit — a chapter summary is an aggregate over chunks, reviewed by nobody);
a new note type per chapter (a new prefix, builder, sync path, purge path and
`new-note` alias, multiplying files ADR-015 already counts as a cost).

The chapter map is what makes a chapter "uma porta de entrada": per chapter it
renders the title, page range, the **note count**, the summary, and wikilinks to
that chapter's granular LIT and derived ZTL. Page range is omitted, never
rendered as null, for a page-less source (ADR-013).

### 6. Cost and freshness are separated

`generate_summaries` spends LLM calls and is gated by `chapter_checksum` — the
gate that already exists to skip re-chunking. A chapter whose
`summary_checksum` still equals its `chapter_checksum` is skipped without a
call, so re-running on a settled vault is free. `refresh_chapter_map` is
deterministic and free, so `connect` calls it after writing notes and the counts
stay live without regenerating any text — the same pattern as `sync_moc_backrefs`
after a MOC write.

A stale summary is **not deleted or hidden**: it is marked as stale in the vault
block and in the catalog output. Deleting on drift would lose a good summary to a
trivial re-chunk.

`preflight.estimate_summarize` is a pure function per ADR-037; `zettel summarize`
gates on it, and `--yes` or a non-TTY passes through. In `run-all`, `summarize`
runs **after** the `--dry-run` gate, so `--dry-run` still spends nothing.

## Consequences

### Positive

* The vault answers a library question it structurally could not answer before,
  and returns the book, its relevant chapters, and a real count per chapter.
* A chapter with zero extraction yield becomes findable — the one case no
  existing retrieval path covered.
* The note count is a SQL aggregate over data that already existed; no new
  writes were needed to make it correct.
* `refresh_chapter_map` gives `connect` a free way to keep the vault current.

### Negative

* **The chapter floor is measured, but the margin is thin.** At `0.70` the
  separating band is only 0.052 wide, and in practice a few off-topic chapters
  land just over the line (observed: 0.707-0.723 for chapters of an unrelated
  source on an in-domain query). Raising toward the `0.72` upper bound would
  start costing real recall — the probe says so explicitly. Mitigated by shipping
  `candidates` with `floor_reason`, so a borderline hit is inspectable rather
  than invisible.
* **`catalog` has no LLM triage, so it is fully exposed to the note floor.**
  Signal A inherits `search_notes` verbatim. `ask` absorbs a leaky floor because
  `prompts/ask.md` makes the model triage the context and discard what is
  irrelevant; `catalog`, having no LLM by design, prints what the floor gave it.
  That exposure is structural and remains.

  It first showed up as the BM25 bypass admitting off-domain notes on a single
  shared common word (*fazer*, *carro*). **Resolved 2026-09-09** in the floor
  itself rather than catalog-side, as this ADR argued it should be: the
  [ADR-003 addendum](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md) adds
  `bm25_bypass_min_coverage`, and off-domain hits fell 31 -> 1 with zero
  in-domain loss for every consumer. The general point stands — a floor
  regression reaches `catalog` undiluted, so `catalog` is the canary for the
  shared floor.
* A fifth Chroma collection to reset, reindex and reason about on an embedding
  change. `zettel reindex --collection chapter_summaries` covers it, and
  `_reindex_chapter_summaries` reuses `summarize`'s own document builder so the
  reindexed bytes match what `summarize` wrote.
* Two new prompts change nothing that exists, but summaries are LLM output and
  their honesty is an empirical question this ADR cannot settle — the same
  caveat ADR-034 records about judgement fields.
* Ranking (`note_score + summary_score`, sources by summed chapter score) is a
  reasonable ordering, not a calibrated one.
* A manual source's only chapter is ADR-030's synthetic `ch000 "Manual"`, so its
  "chapter summary" is really a source summary. Harmless, but not meaningful.

### Deliberately out of scope

* Web UI exposure. CLI-only, like `ask`, `article` and `skill`.
* A `chapter` scope in `topic_index_terms`. The catalog has its own BM25 table;
  reusing the topic index would blur ADR-036's contract that only permanent-note
  targets are routable.
* Resurrecting the Chroma `sources` collection, which still embeds only
  `f"{title} -- {authors}"` and is still read by nobody. It is the same
  write-only defect that removed `literature_notes` and deserves its own
  decision — this ADR neither fixes nor worsens it. For whoever takes it up:
  the complete set of writers is three (`harvester/pipeline.py`,
  `rebuild.py:_reindex_sources`, `sync.py` adopting a hand-written SRC), and
  `index.py` exposes no query helper against it. Note that attaching the
  *source summary* there is the option this ADR already rejected: the chapter is
  the search unit, and a source-level embedding would be a second surface with
  no reader.

## Amendment (2026-09-10)

The literature index no longer sits beside an `auto-topic-index`. That block was the source-scope reading aid [ADR-036](./ADR-036-topic-index-routing-not-representation.md) has now dropped. The three remaining managed blocks on the index note are `auto-lit-index`, `auto-source-summary` and `auto-chapter-map`. `summarize._write_blocks` still owns the last two headings.

## References

* `zettel/summarize.py` (`generate_summaries`, `refresh_chapter_map`,
  `chapter_summary_document`, `source_summary_checksum`)
* `zettel/catalog.py` (`run_catalog`, `ChapterMatch`, `SourceMatch`)
* `zettel/retrieval.py` (`RetrievedChapter`, `ChapterSearchResult`,
  `search_chapter_summaries`, `_apply_relevance_floor` now parameterised)
* `zettel/state.py` (`chapters.summary*`, `sources.summary*`,
  `fts_chapter_summaries`, `get_chapter_note_counts`, `get_chapters_for_note`,
  `get_chapters_needing_summary`, `get_chapter_page_ranges`)
* `zettel/index.py` (`COL_CHAPTER_SUMMARIES`, `query_chapter_summaries`)
* `zettel/preflight.py` (`estimate_summarize`), `zettel/rebuild.py`
  (`_reindex_chapter_summaries`), `zettel/connector/run.py`
  (`refresh_chapter_maps`)
* `prompts/chapter_summary.md`, `prompts/source_summary.md`
* `tests/test_summarize.py`, `tests/test_catalog.py`, `tests/test_config.py`
