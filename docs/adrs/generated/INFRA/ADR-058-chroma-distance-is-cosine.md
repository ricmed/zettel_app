# ADR-058: Chroma distance is cosine, and every threshold means cosine

**Status:** Accepted
**Date:** 2026-10-18
**Issue:** #267

## Context

`_LangChainChromaEF.default_space` returns `"cosine"`, so collections built with that adapter store **cosine distance** (`d = 1 − cos`). The rest of the code assumed squared L2 on unit vectors (`d = 2 − 2·cos`) and converted with `1 − d/2`, which is `(1 + cos)/2`. A collection created with no embedding function and no `configuration` is Chroma's default `l2`; that is not the production path.

Measured on a copy of `data/chroma/` (chromadb 1.5.9): for one note the index returned 0.0673 to a neighbour whose hand-computed cosine is 0.9327. `2 − 2·cos` would have been 0.1346. Same result on the next five neighbours (`docs/artigo/03-review.md` §4.7).

With `linking.dedupe_threshold = 0.85` the two dedupe passes therefore used different cuts:

| pass | what the code compared | effective cosine |
|---|---|---|
| against existing notes (`d ≤ 2·(1−τ)` on cosine distance) | | **≥ 0.70** |
| intra-batch (`math.dist` = √(2−2·cos) against the same bound) | | **≥ 0.955** |

The retrieval floors and the corroboration cut were calibrated **on the `(1+cos)/2` numbers** the probe printed. They work as cuts. The label "cosine" was wrong.

Same-source nearest neighbour, 781 notes (audit, document side of the embedding — an approximation of the query side):

| cosine of nearest same-source neighbour ≥ | 0.70 | 0.80 | 0.85 | 0.90 | 0.955 |
|---|---|---|---|---|---|
| fraction of notes | 99.9% | 99.4% | 87.2% | 42.5% | 7.0% |

## Decision

1. `zettel/similarity.py` is the only conversion. `cosine_from_distance(distance, space)` implements `cosine` (`1 − d`) and `l2` (`1 − d/2`, squared L2 on unit vectors). Call sites do not divide by 2.
2. Every collection this process creates passes `configuration={"hnsw": {"space": "cosine"}}`. `VectorIndex.distance_space` reads the space back. A collection that is still `l2` logs that `zettel reindex --force` is required; thresholds below assume cosine.
3. Intra-batch dedupe compares the dot product of the normalised vectors to the same `dedupe_threshold` as the pass against existing notes.
4. **`dedupe_threshold` stays 0.85**, now meaning cosine in both passes. That is the number the configuration already declared. It was not replaced: the audit shows 0.85 still sends 87% of same-source neighbours to the LLM, and no cross-source percentile was published as a new cut. Picking 0.90 because "87% is a lot" would be inventing a number. A later calibration can move it; this change only makes 0.85 mean 0.85.
5. Floors that were measured on `(1+cos)/2` are converted by `s_new = 2·s_old − 1` and not re-measured:

| knob | old `(1+cos)/2` | cosine |
|---|---|---|
| `retrieval.relevance_floor.min_vector_similarity` | 0.70 | **0.40** |
| `retrieval.chapter_floor.min_vector_similarity` | 0.70 | **0.40** |
| `retrieval.*.absolute_min_similarity` | 0.15 | **−0.70** |
| `linking.distant_analogy_min_similarity` | 0.40 | **−0.20** |
| `linking.corroborates_min_similarity` | 0.88 | **0.76** |
| `decision.corroborates_band_min` | 0.75 | **0.50** |
| `harvest.duplicate_chunk_threshold` | 0.88 | **0.76** |

The last one is inert while layer 5 is off. It is converted so turning the flag on does not silently tighten the cut.

`decision_shadow` rows written before this change store similarity on the old scale. New rows store cosine. Score a recording against the scale it was written in.

## Reindex

Not required for a vault whose collections were built with the embedding adapter: they are already cosine (the audit measured that), and `get_or_create` does not change the space of an existing collection. Required when a collection reports a space other than `cosine` (the startup warning names `zettel reindex --force`). Creating the collections with an explicit space does not rewrite stored vectors.

## Consequences

* A configured 0.85 is cosine 0.85 in dedupe, retrieval and corroboration.
* The physical retrieval and corroboration cuts are unchanged. The dedupe cut against existing notes moves from cosine 0.70 to 0.85; the intra-batch cut moves from 0.955 to 0.85. Pairs the old filter let through below 0.85 no longer reach the LLM. Rebuilding the `extends` edges those pairs produced is #273, not this decision.
* `scripts/probe_relevance_floor.py` reports cosine. A new probe run will print numbers about `2·old − 1` relative to the 2026-09 measurements. Compare bands, not the raw printout, to those notes.
