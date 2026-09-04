# ADR-036: A Topic Index for Routing, Fed Back Through the Relevance Floor

**Status**: Accepted (2026-09-03)

**Depends on:**
- [ADR-003: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)
- [ADR-010: Retrieval Result Transparency — Hits vs Candidates](./ADR-010-retrieval-result-transparency-hits-vs-candidates.md)

**Related to:**
- [ADR-009: Graph-Based Note Discovery with Weighted BFS](./ADR-009-graph-based-note-discovery-weighted-bfs.md)
- [ADR-034: Author-Judgement Fields on the Candidate, Optional by Construction](../EXTRACT/ADR-034-optional-author-judgement-fields.md)
- [ADR-035: `zettel skill` Projects a Vault Slice as a Flat Agent Skill](../CLI/ADR-035-flat-agent-skill-export.md)

## Context and Problem Statement

Retrieval and routing are different problems and the vault only solves one of them. `Retriever` decides *how relevant* a note is to a query — dense kNN, BM25, RRF, a relevance floor, graph expansion. Nothing decides *where a named thing lives*: there is no cheap map from "The 5 Whys" or "dropout" to the notes that answer it.

That gap shows up in three places. The skill export (ADR-034) needs a Topic Index in its `SKILL.md`. An agent browsing the vault has no small always-available map. And `ask` can miss a note whose jargon the embedding underrates and whose lexical match BM25 ranks too deep to matter.

The temptation is to treat the index as a better retriever — grep a chapter instead of running RRF. That is the mistake this ADR exists to avoid. The nearby research is explicit that routing metadata costs context and that a library-level index needs evidence before it earns its place; the honest scope here is K=1 (one source, one MOC), not a global index of the corpus.

## Decision Drivers

* Routing and representation are separate axes and must not be conflated: the index adds a *hint*, not a verdict.
* The relevance floor exists because RRF's score is purely positional. A bug already shipped here once — any BM25 presence bypassed the floor unconditionally, so a note sharing one common domain word passed on incidental evidence. A new "this note is in the index" signal must not recreate that.
* The same term-extraction rule has to serve the vault blocks and the skill export, or the two indexes will drift into disagreeing about what a note answers to.
* PT-BR stopwords are already a known false signal in the FTS layer; the index must not reintroduce them.
* Terms must be regenerated wholesale, not merged: a tag removed from a note must disappear from the index.
* A mature vault should not have to wait for its next `review`/`garden` to get an index at all.

## Considered Options

* Store the index only as Markdown and parse it at query time.
* Store it only in SQLite and render nothing in the vault.
* Store both: Markdown for humans and agents, a mirrored table for lookups.
* For the boost: inject the routed note directly as a passing hit.
* For the boost: give the routed note a synthetic BM25 rank so the existing bypass applies.
* For the boost: fetch the routed note's real vector distance and let the floor judge it.

## Decision Outcome

**The index lives on two surfaces.** An `auto-topic-index` managed block on the literature index (per source) and on each MOC is what a human or an agent reads; a mirrored `topic_index_terms` table is what `ask` queries, so a lookup never has to parse Markdown at query time. `topic_index.sync_topic_index` writes both in one call and **replaces** a scope's rows rather than merging them.

**`build_term_map` is the single definition of what a note answers to**, shared with ADR-034's export. Terms come from `named_frameworks` first (the author's own vocabulary, ADR-033), then tags, and fall back to the head of the thesis only when a note has neither — a truncated sentence is a worse key than a tag, so it never competes with one. Stopwords are dropped at the edges of a phrase but tolerated inside it: "que" alone is the false signal; "Dropout funciona como ensemble" is a phrase whose stopword is glue.

**Only permanent-note targets are routable.** A literature target appears in the block, because it routes a reader to the right granular note, but is stored with a null `note_id` and never seeds a search — the Retriever scores permanent notes, and a LIT wikilink is not one. This is why the source scope is a reading aid while the MOC scope is what feeds `ask`.

**The boost re-enters through the front door.** When a query contains an indexed term, the routed note is fetched from Chroma with the search space restricted to its id (`VectorIndex.query_notes_by_ids`), so it arrives carrying a **real distance** and faces `_apply_relevance_floor` on exactly the same evidence as any other candidate. It is then merged into the vector pool and re-sorted by distance, so it gets its true rank instead of being pinned to the tail.

Both rejected alternatives fail the same way. Injecting it as a passing hit is a bypass by definition. Giving it a synthetic BM25 rank routes it into the lexical bypass, where the absence of vector data means the floor cannot say no — the exact shape of the bug the rank cutoff was added to fix. Being in the index means someone once tagged the note with a word the query happens to contain; that is a reason to *look*, not a reason to *believe*.

`retrieval.topic_index_boost` (default true) turns the whole path off, and `topic_index_max_seeds` caps how many notes one query can pull in. A hit that arrived this way carries `RetrievedNote.origin = "topic_index"`, which `ask --show-context` renders, and `AskResult.retrieval_params` records both knobs.

### Positive Consequences

* A note whose jargon the embedding underrates and whose BM25 rank is too deep gets a second chance — while still having to clear the floor.
* The vault gains a readable term map on two surfaces, and the skill export stops being the only place one exists.
* `--show-context` distinguishes a routed seed from a searched one, so the effect of the boost is observable rather than inferred.
* `zettel reindex` backfills every scope, so an existing vault gets an index immediately rather than after its next pipeline run.

### Negative Consequences

* A matching query costs **one extra query embedding** (the restricted Chroma call). It only happens when a term matches, and `topic_index_boost: false` removes it.
* `match_topic_index` uses `instr()` over the folded query, so it is a scan of the term table per query. The table is bounded by notes × 6 terms and indexed on `term_folded`, but on a very large vault this is a linear cost per `ask`.
* Term quality is only as good as the tags and framework names in the notes. Nothing here validates a bad tag; it will simply route badly.
* The index has no bearing on `connect` or `sync` retrieval, which also go through `Retriever` and therefore get the boost implicitly. That is consistent but was not separately calibrated.

## Pros and Cons of the Options

### Markdown only, parsed at query time

* Good, because there is exactly one artifact and no sync to get wrong.
* Bad, because every `ask` would read and parse files to answer a lookup.
* Bad, because a hand-edited block would silently change retrieval behaviour.

### SQLite only

* Good, because lookups are trivial and always consistent.
* Bad, because the human and the agent lose the map, which was the main motivation.

### Both surfaces (chosen)

* Good, because each consumer reads the form that suits it.
* Good, because regeneration is wholesale, so the two cannot drift apart.
* Bad, because there are two artifacts to keep in step — mitigated by writing both from one function.

### Boost as a passing hit / as a synthetic BM25 rank

* Good, because it is simple and guarantees the routed note is used.
* Bad, because it is a floor bypass — the failure mode already fixed once, reintroduced under a new name.

### Boost with a real distance (chosen)

* Good, because the routed note is judged on the same evidence as everything else.
* Good, because the floor's existing reasoning and `floor_reason` text apply unchanged.
* Bad, because it costs one extra embedding when a term matches.

## Consequences

`sync_moc_backrefs` is the hook for the MOC scope, because it is the one function every MOC write already passes through (taxonomy pipeline, hub pipeline, manual sync); `clear_moc_backrefs` drops the scope's rows. `review._refresh_literature_index` is the hook for the source scope, and `delete_source_cascade` cleans up.

`topic_index._write_block` owns the `## Topic Index` section on every surface, so the MOC body builders and the literature index builder do not each have to scaffold it.

Deliberately out of scope: a corpus-wide library index (the research says only with evidence), a hierarchy of indexes, replacing BM25/RRF with the index, and any claim that the index "beats RAG".

## References

* `zettel/topic_index.py` — `build_term_map`, `sync_topic_index`, `render_topic_index_block`, `_write_block`, `fold`
* `zettel/retrieval.py` — `_add_topic_index_seeds`, `RetrievedNote.origin`, `_apply_relevance_floor` (unchanged)
* `zettel/index.py` — `query_notes_by_ids` (id-restricted similarity)
* `zettel/state.py` — `topic_index_terms`, `replace_topic_index_terms`, `match_topic_index`, `match_topic_index_scope`
* `zettel/moc_backrefs.py` — `_sync_moc_topic_index`, scope cleanup in `clear_moc_backrefs`
* `zettel/review.py` — `_refresh_source_topic_index`
* `zettel/rebuild.py` — `rebuild_topic_index` (backfill during `zettel reindex`)
* `tests/test_topic_index.py` — block lifecycle, routable vs listed targets, floor still rejects a routed note, boost off is a regression guard
