# ADR-051: Citation Provenance on the Permanent Note

**Status**: Accepted (2026-09-21)

**Depends on:** [ADR-013: Three-Layer Page Inference Strategy for Chunk Page Metadata](../HARVEST/ADR-013-three-layer-page-inference-strategy.md)

**Related to:**
- [ADR-028: LangGraph StateGraph for Article Orchestration](./ADR-028-langgraph-stategraph-article-orchestration.md)
- [ADR-034: Author-Judgement Fields on the Candidate, Optional by Construction](../EXTRACT/ADR-034-optional-author-judgement-fields.md)
- [ADR-043: Distant Analogies as Suggestions, Not Graph Edges](../RETRIEVAL/ADR-043-distant-analogies-as-suggestions.md)

**Issue:** [#187](https://github.com/ricmed/zettel_app/issues/187)

## Context and Problem Statement

Academic writing from a Zettelkasten needs every permanent note to be citable mechanically: `Segundo Kahneman (2011, p. 45)...` for a paraphrase, `"..." (KAHNEMAN, 2011, p. 45)` for a direct quote. The three-layer model (literature note with the source and page, a permanent note in the writer's words pointing back to it, and a manuscript that turns the pointers into citations) was already in place: a ZTL carries `source_id`, `literature_ref`, a structural `page` and `chunk_id`, and the SRC carries the ABNT reference.

Three gaps kept the last step from being mechanical:

1. **The verbatim passage never reached the ZTL.** `anchor_quote` is extracted per candidate and checked by `quote_is_grounded`, but it lived only in the LIT draft (`auto-candidate-quotes`) and in `concepts.candidate_json`.
2. **The page could be wrong.** `chunks.page_in_book` is the *first* page a chunk touches (ADR-013). When a chunk crosses a page break, the anchor can sit on the next page, and the citation would name the wrong one.
3. **`zettel article` cited at source granularity.** `_pack_section` gave the drafting model one `citacao_abnt` per source, without a page. The page existed only inside the note body, which `max_chars_per_note` can truncate, and there was no safe path for a direct quote.

## Decision Drivers

* A citation is a fact about provenance. It must not be something a model can get wrong.
* The anchor is already the right passage: it is chosen **per thesis**, not per chunk, and grounding has already proven it verbatim.
* A ZTL is retrieved by its concept. Quoted source text in the embedding would pull retrieval toward the author's phrasing.
* No new LLM call and no schema migration.

## Decision Outcome

**Provenance is copied by code, never through the LLM**, following the same rule as the structural `page` and ADR-034's judgement lists. `zettel/citation.py` is the single definition of how a ZTL is cited. `resolve_citation(source, chunk, anchor_quote)` returns a `NoteCitation` (`page`, `pages`, `cite`, `page_confidence`). Both `connect` and the manual `new-note ztl --from-lit` scaffold call it.

**The page is the page the anchor sits on.** `paging.locate_quote_pages` searches the Docling page map in `sources.extracted_text`, the only surface that still carries `<!-- zettel:page-break -->`. It starts at the chunk's `page_in_file` and looks up to three pages further. The anchor's first and last five folded words (`fold_for_match`) are located separately. This yields `p. 42-43` when the anchor crosses a break, and it tolerates an editorial ellipsis inside the quote. The file page is then mapped to the printed page by `compute_page_in_book`. When there is no page map or no match, the citation falls back to the chunk's `page_in_book`. The frontmatter records which rule applied in `citation_page_confidence`: `quote` or `chunk`. Native Markdown has no pages (ADR-013), so its citation carries author and year only.

**The ZTL carries the citation in two places.**
- Frontmatter: `page`, `citation_page_confidence`, `citation` (for example `(KAHNEMAN, 2011, p. 45)`) and `anchor_quote`. Each key is omitted rather than written as null when it is unknown.
- Body: an `auto-evidence` managed block under `## Fonte`, holding the citation and the verbatim passage.

Because it is a managed block, `extract_embeddable_text` strips it. The note stays conceptual for retrieval, which is the same double-counting argument ADR-034 makes about judgement text.

**The article cites per note, and direct quotes are checked.**
- `CatalogNote` gains `cite` and `anchor_quote`, both read from `notes.frontmatter_json` rather than from the truncated body.
- `_pack_section` gives each evidence note its own `citacao_abnt` (with page) and, when the note has one, a `citacao_direta`.
- `prompts/article_section_academic.md` makes paraphrase the default. It allows text between quotes only when it is a verbatim `citacao_direta` followed by that note's citation.
- `verify_article` flags, for academic style, any quoted span of four or more words that is not a substring of some catalog anchor. It uses the same folding and the same warnings list as the existing orphan-citation check.

A note without a `citation` key (for example a blank manual ZTL) falls back to its source's author-date citation.

### Positive Consequences

* From a ZTL to `(AUTOR, ano, p. X)` is a lookup, not a judgement. It works for paraphrase and for direct quotation.
* A chunk crossing a page break no longer silently mis-cites.
* A direct quote in a generated article can be traced to a grounded anchor, or it is flagged.
* No new LLM call, no SQLite column and no Chroma change. The new keys live in `frontmatter_json`, so `zettel rebuild` reproduces them.

### Negative Consequences

* The code checks that the anchor is **literal**, not that it **supports** the thesis. That choice is still the model's, and the human verifies it in `review`, where the `auto-candidate-quotes` block exists for exactly this purpose.
* The anchor is short (10 to 25 words, up to 37 with tolerance). A long block quotation needs another field and is out of scope.
* Location depends on the page map. Sources exported without page-break markers, or an anchor split by dehyphenation across a break, fall back to the chunk's first page. The frontmatter says so (`citation_page_confidence: chunk`), so it is not silent.
* `citation` is computed when the note is written. If a source's bibliographic metadata is corrected later, existing ZTLs keep the old string until the next `connect` or rewrite. Development vaults are reset rather than migrated.
* Existing ZTLs gain the fields only when they are rewritten.
