# ADR-044: Cross-Encoder Reranking Between RRF Fusion and the Relevance Floor

**Status:** Proposed (draft — number reserved, not yet accepted)  
**Date:** 2026-09-06  
**Depends on:** [ADR-003](../../../generated/INFRA/ADR-003-hybrid-dense-bm25-retrieval.md), [ADR-038](../../../generated/QA-WRITING/ADR-038-ask-trajectory-evals-offline-replay.md)  
**Related to:** [ADR-009](../../../generated/RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md), [ADR-010](../../../generated/RETRIEVAL/ADR-010-retrieval-result-transparency-hits-vs-candidates.md), [ADR-036](../../../generated/RETRIEVAL/ADR-036-topic-index-routing-not-representation.md), [ADR-043](../../../generated/RETRIEVAL/ADR-043-distant-analogies-as-suggestions.md)  
**Candidate model:** Qwen3-Reranker (0.6B / 4B / 8B), the cross-encoder counterpart of the `qwen3-embedding` already in use  

---

## Context and Problem Statement

The pipeline has no reranking stage. `Retriever.search_notes` runs dense kNN, BM25 and topic-index seeding, fuses them with Reciprocal Rank Fusion, gates the result through `_apply_relevance_floor`, and expands the survivors over the note graph. There is no point at which a model reads a query and a note *together*.

This is a known weakness, stated in the code itself:

> RRF's fused score is purely *positional* — the vector kNN side always returns the closest available notes regardless of whether any of them are actually relevant, so a totally off-topic query gets a similarly "confident-looking" score to a genuinely answerable one.
> — `zettel/retrieval.py`, `_apply_relevance_floor` docstring

The absolute relevance floor exists precisely because RRF cannot express relevance. But the floor is not a relevance judgement either: it is a **threshold on bi-encoder cosine similarity**, where query and note were embedded independently and never compared jointly. It answers "is this note near the query in the embedding space?", not "does this note answer the question?".

Three concrete symptoms follow.

**1. The floor's threshold is uncalibrated and has already drifted.** One knob, four values in the repository:

| Location | `min_vector_similarity` |
|---|---|
| `zettel/config.py:362` (Pydantic default) | `0.70` |
| `config/config.yaml:245` (operational) | `0.65` |
| `evals/configs/current-ask.yaml` (run identity) | `0.70` |
| `CLAUDE.md` (documentation) | `0.70` |

ADR-003 leaves an open `[NEEDS INPUT]` asking which corpus and embedding model produced `0.70` and `0.15`. No calibration record exists. The number carries the entire precision burden of retrieval.

**This drift was measured, and it is not cosmetic.** Probing the live store (47 permanent notes, `qwen3-embedding` @ 1024d, cosine via `1 - d/2`):

| Query class | Example | top-1 similarity |
|---|---|---|
| Off-domain | "receita de bolo de cenoura com cobertura de chocolate" | `0.669` |
| Off-domain | "como trocar o oleo do motor de um carro flex" | `0.670` |
| Off-domain | "sintomas de deficiencia de vitamina D em idosos" | `0.681` |
| Meta / adjacent | "o que e um piso de relevancia na recuperacao hibrida" | `0.727` |
| In-domain | "como escrever um bom prompt com exemplos" | `0.799` |
| In-domain | "raciocinio passo a passo em modelos de linguagem" | `0.818` |
| Self-match (note title as query, n=12) | — | `0.804`–`0.882`, median `0.837` |

At the **operational `0.65`, all three off-domain queries clear the floor** — a carrot-cake recipe retrieves prompt-engineering notes as evidence. At `0.70` all three are correctly rejected while in-domain queries pass. The documented default is the better-calibrated value and the shipped one is the regression: `config/config.yaml` is currently admitting noise into `ask`. That is a defect to fix on its own merits, independent of anything in this ADR.

Where the bi-encoder genuinely runs out of resolution is the **middle band**: the meta/adjacent query scores `0.727` while the *tenth* result of an in-domain query scores `0.733`. Seven thousandths separate "wrong topic, plausible words" from "right topic, marginal note". No scalar threshold discriminates there — and with `retrieval.ask.topk: 45`, that middle band is most of what `ask` actually assembles. This, rather than gross off-topic rejection, is the measured case for a cross-encoder.

Caveat, stated plainly: 47 notes in a single domain (prompt engineering), ~20 probe queries. A homogeneous corpus compresses the similarity range and inflates every number above. This is a signal that motivates measurement, not a calibration.

**2. The `ask` context is large and ordered by the wrong signal.** With `retrieval.ask.topk: 45`, `max_context_notes: 75` and `max_chars_per_note: 5000`, a single question assembles up to ~61 notes (45 floor-cleared seeds plus up to 16 graph neighbours) — roughly 300k characters, ~76k tokens, truncated by RRF-plus-graph score rather than by relevance. Precision at the top of that list is unmeasured.

**3. Improving the ranking currently means changing the embedding model**, which invalidates every stored vector and forces `zettel reindex --force` (`EmbeddingSpaceMismatch`, `zettel/index.py:122`). There is no way to improve ranking quality without a full reprocess.

A cross-encoder reranker is the standard second stage for exactly this: retrieve 50–100 candidates cheaply, then re-score them jointly. The Qwen3-Reranker family is the direct counterpart of the `qwen3-embedding` model already configured (`config/config.yaml:76-86`), and unlike an embedding swap it is **stateless** — nothing is persisted, so changing or removing it costs no reindex.

---

## Decision Drivers

* RRF provides rank, not relevance; the relevance floor provides a similarity threshold, not relevance. Neither is a query-document judgement.
* The single threshold carrying retrieval precision has no calibration record and four conflicting values — and the shipped one is measurably the wrong one.
* Measured, the bi-encoder separates off-domain (`~0.67`) from in-domain (`~0.80`) but not the middle band, where an off-topic query (`0.727`) and a marginal in-domain hit (`0.733`) are seven thousandths apart. That band is most of what `topk: 45` assembles.
* `ask` spends ~76k input tokens per question on a list that was never ordered by relevance — cost and precision are the same problem here.
* A reranker is stateless: adopting, tuning or reverting it requires no `reindex`, unlike every other lever on ranking quality.
* ADR-009's entire value is surfacing notes that embeddings **structurally cannot find** (`contradicts`, weight 1.0). Any reranking design must not undo it.
* ADR-038 forbids comparing two runs whose envelope differs. A reranker changes the envelope and must be represented in it.
* ADR-002 commits to local-first, single-VM, no server component.

---

## Considered Options

* **A. Status quo** — keep tuning `min_vector_similarity` and the graph weights.
* **B. Rerank the fused pool, before the floor** — score RRF candidates with a cross-encoder; the floor then gates on the rerank score instead of (or alongside) cosine similarity.
* **C. Rerank after graph expansion** — rerank the final `hits` list, seeds and neighbours together.
* **D. Replace the relevance floor entirely** with a rerank-score threshold.
* **E. Upgrade the embedding model instead** (larger Qwen3-Embedding, higher `dimensions`).

---

## Proposed Decision Outcome

**Chosen: option B, rerank the fused pool before the floor — applied to hop-0 seeds only, behind a config flag, defaulting off until measured.**

The reranker slots into `Retriever.search_notes` between `_rrf_fuse_notes` and `_apply_relevance_floor`:

```
kNN denso + BM25 + topic-index seeds
  -> _rrf_fuse_notes            (positional)
  -> [NEW] _rerank              (query-document relevance, hop 0 only)
  -> _apply_relevance_floor     (gates on rerank score when available)
  -> _expand_with_graph         (untouched — ADR-009)
```

Five constraints define the shape.

**1. Graph neighbours are never reranked.** ADR-009 exists because embeddings cannot find a note that *contradicts* the query's premise. A cross-encoder is still a semantic relevance scorer and would rank such a note low — reranking the post-expansion list would systematically delete the signal ADR-009 was built to add. Expansion runs after the rerank, on reranked seeds, and neighbours keep their graph weight. This is why option C is rejected.

**2. `search_distant_analogies` is exempt.** ADR-043's third RAG group deliberately seeks notes that are *far* — a relevance reranker is the wrong instrument by construction. The method takes an explicit `rerank=False`.

**3. Dedupe paths stay out.** Extractor dedupe and harvester layer-3 are calibrated on raw L2 and already documented as not using `Retriever`. That boundary is unchanged.

**4. The floor is kept, not replaced.** Option D is rejected for now: swapping one uncalibrated threshold for another uncalibrated threshold is not progress. Once a rerank score is present the floor gates on it; `absolute_min_similarity` stays as the vector-space backstop. Whether `min_vector_similarity` becomes redundant is an empirical question to settle **after** measurement, not in this ADR.

**5. Nothing ships until it can be measured.** The reranker is a research change to a subsystem whose current thresholds are admittedly uncalibrated. Adopting it on reputation would repeat exactly the mistake ADR-003 documents.

### Configuration shape

A reranker is neither an LLM phase (`llm.<phase>`) nor an embedding (`embedding.*`) — it is a third model class and needs its own block:

```yaml
retrieval:
  rerank:
    enabled: false          # default off until an eval says otherwise
    provider: transformers  # transformers | sentence-transformers | http
    model: Qwen/Qwen3-Reranker-0.6B
    base_url: null          # provider: http (sidecar / vLLM)
    input_top_k: 60         # how many fused candidates are scored
    top_n: 15               # how many survive to the floor
    max_chars_per_doc: 2000 # truncation before the cross-encoder
    min_score: null         # null = floor keeps using similarity
```

Unlike `embedding.*`, this block has **no drift check and no reindex**: no rerank output is persisted.

### Observability

`RetrievedNote` gains `rerank_score: float | None` and `rerank_rank: int | None`, alongside the existing `vector_rank` / `bm25_rank` / `hop` / `via` / `floor_reason` provenance (ADR-010). `ask --show-context` renders them. `AskResult.retrieval_params` records `rerank_enabled`, `rerank_model`, `input_top_k` and `top_n`.

---

## Prerequisites (blocking)

These come **before** any reranking code.

**P1 — A live eval runner.** The ADR-038 harness is replay-only: `condition` accepts any name but no ablation is implemented, and the live runner is declared as a follow-up in `evals/README.md`. Without it there is no baseline and no way to answer whether the reranker helps. This is the hard blocker.

**P2 — Rerank fields in the run identity.** `manifest.FLOOR_KEYS` and `AskResult.retrieval_params` must carry the rerank model id and `top_n` before the first measurement, or ADR-038's guardrail would silently compare a reranked run against a non-reranked one — the precise comparison the ADR forbids.

**P3 — Fix the threshold drift, and do it independently of this ADR. ✅ Done 2026-09-06.** The measurement in the Context section showed `config/config.yaml`'s `0.65` admitted off-domain noise that `0.70` correctly rejects. The operational value was raised to `0.70`, so all four locations now agree, and `scripts/probe_relevance_floor.py` was added so the number stops being folklore: it re-measures the bands, verifies normalisation, and fails loudly (exit 2) if the cosine assumption ever breaks. `tests/test_config.py` now pins the YAML to the schema default rather than to a literal, so the two cannot drift apart again silently.

This was a standalone defect fix that shipped independently of any reranker — deliberately, because a reranker measured against a floor that was letting carrot-cake recipes through would have been credited for repairing a misconfiguration.

---

## Runtime Options

**Ollama cannot serve this.** Ollama exposes no `/api/rerank`; `/api/embeddings` returns embeddings, not classification-head logits. The Qwen3-Reranker GGUFs on the registry (`dengcao/Qwen3-Reranker-0.6B`) can be pulled but cannot be driven as a reranker through the existing Ollama path. Upstream support ([ollama/ollama#16076](https://github.com/ollama/ollama/issues/16076)) remains an open request. This matters because `embedding.provider` is currently `ollama` — the reranker cannot reuse that transport.

Dependency state in `.venv` (verified): `torch` present, `transformers` present (both pulled in by Docling), `sentence_transformers` absent, `FlagEmbedding` absent.

| Option | Cost | Trade-off |
|---|---|---|
| `transformers` directly | no new dependency | in-process model load; the import must live inside the call, never at module scope (ADR-032 rule 3 keeps `zettel --help` free of heavy imports) |
| `sentence-transformers` `CrossEncoder` | one dependency | cleanest API; the `sentence-transformers` embedding provider in `index.py:341` is already declared but uninstalled, so this closes an existing gap |
| vLLM / HTTP sidecar | a service | best throughput; contradicts ADR-002's no-server-component stance |

Model size: with ~60 pairs per `ask`, only **0.6B** is viable for interactive use (MTEB Reranking 65.80, over 8 points above BGE-reranker-m3). 4B (69.76) and 8B are batch-only. The `torch` pin is already CUDA 12.6 (`pyproject.toml`), so GPU is available where present.

---

## Pros and Cons of the Options

### A. Status quo

* Good, because it adds no dependency, no latency and no new uncalibrated knob.
* Good, and this is stronger than it first looked: the measurement shows most of the observed damage comes from a *misconfigured* threshold (P3), not from the absence of a reranker. Fixing `0.65` to `0.70` is free and recovers off-domain rejection entirely.
* Bad, because it leaves the middle band unresolved — no scalar threshold separates `0.727` from `0.733`, and raising the floor further starts rejecting real in-domain hits.
* Bad, because `ask` keeps spending ~76k tokens on a list ordered by a positional score.

### B. Rerank the fused pool, before the floor (proposed)

* Good, because it inserts an actual query-document relevance judgement where the code says one is missing.
* Good, because it is stateless — no reindex to adopt, tune or revert.
* Good, because it leaves ADR-009, ADR-043 and the dedupe paths structurally untouched.
* Good, because cutting `input_top_k: 60` to `top_n: 15` reduces `ask` input tokens roughly 4x while raising top-of-list precision.
* Bad, because it adds latency to every `ask`, `connect` and `sync` retrieval.
* Bad, because two thresholds now sit in series (rerank `top_n`, then the floor) and only one of them will have been measured.
* Bad, because it introduces a third model class into a config schema built around two.

### C. Rerank after graph expansion

* Good, because seeds and neighbours would finally be scored on one comparable axis — today a hop-2 neighbour's `neigh.weight` is sorted against a seed's RRF score, which are not the same quantity.
* Bad, and disqualifying, because it would systematically down-rank `contradicts` neighbours — deleting the exact signal ADR-009 exists to supply.

### D. Replace the relevance floor with a rerank threshold

* Good, because it removes a threshold instead of adding one, ending the series problem.
* Bad, because it trades an uncalibrated threshold for an uncalibrated threshold.
* Bad, because it makes every retrieval hard-depend on the reranker being loaded — no graceful degradation, unlike the FTS5 fallback path.
* Deferred, not rejected: this becomes the right question once P1 exists.

### E. Upgrade the embedding model

* Good, because it needs no new stage, no new config block and no new failure mode.
* Bad, because a bi-encoder cannot express the joint judgement regardless of size — it moves the threshold, it does not change its nature.
* Bad, because every attempt costs a full `reindex --force`, making iteration expensive precisely where iteration is the point.

---

## Consequences

Retrieval gains a stage that can fail, be slow, or be absent. `Retriever` needs a degradation path — mirroring the existing `_warn_no_fts` behaviour — so a missing or broken reranker logs once and falls through to the current similarity floor rather than failing the query.

Two thresholds in series (`rerank.top_n`, then `min_vector_similarity`) is a worse configuration story than one, and this ADR does not resolve it. The honest position is that the floor and the reranker address the same problem by different means, and keeping both is a transitional state that should end once measurement exists. That decision is deliberately deferred rather than guessed.

Every `Retriever` consumer inherits the reranker implicitly — `ask`, `article`, `connect` RAG and `sync` suggestions — as ADR-036 already notes for the topic-index boost. `connect` runs at `linking.topk: 5`, so the reranker costs there without much to reorder; a per-consumer opt-out may be warranted.

`ask` latency rises by one cross-encoder pass over `input_top_k` pairs. On CPU with a 0.6B model this is likely seconds; the `max_chars_per_doc` truncation exists to bound it, and truncating a 5000-character note to 2000 for scoring means the reranker judges a prefix, not the note.

**Verified, and closed:** collections are created without an `hnsw:space` in `_collection_metadata` (`zettel/index.py:423`), so Chroma's default squared-L2 applies, and the floor's `1.0 - distance / 2.0` conversion is a valid cosine similarity **only** if the vectors are unit-normalised. They are. Measured end to end:

| Point in the chain | L2 norm |
|---|---|
| Ollama `/api/embed`, native 4096d | `1.000000` |
| Query path via `embedding_fn` (langchain_ollama, 1024d) | `1.000000` |
| Stored vectors, all four collections (sample n=5 each) | `1.000000` |
| *Naive* truncation 4096→1024 without renormalisation | `0.513645` |

The last row is the one that mattered: MRL truncation to `dimensions: 1024` would leave a norm of ~0.51, so something in the chain renormalises after truncating — and it does. `1 - d/2` is sound, and the similarity numbers throughout this ADR are real cosines. The missing `hnsw:space` is therefore latent rather than active: it is correct today only because the vectors happen to be normalised, and it would break silently under an embedding provider that does not normalise. Setting it explicitly is cheap insurance, not a bug fix.

---

## Open Questions

1. Does the reranker make `min_vector_similarity` redundant, or do both stages carry distinct signal? Answerable only after P1.
2. Should `connect` (topk 5) and `sync` opt out by default, given the cost-to-reorder ratio?
3. Should rerank scores reuse the `llm_cache` table? They are deterministic per `(query, doc, model)` and `connect` re-scores the same candidates across runs — but the cache is keyed by `compute_llm_call_checksum`, which assumes an LLM call shape.
4. ~~Is `qwen3-embedding` output unit-normalised through `langchain_ollama`?~~ **Answered: yes, at every point in the chain (see Consequences).** `1 - d/2` is a valid cosine. Remaining sub-question: should `hnsw:space` be set explicitly anyway, so the conversion stops depending on a provider behaviour nothing asserts?
5. `transformers` in-process, or a sidecar? The first respects ADR-002 and costs cold-start; the second inverts that trade.

---

## References

* `zettel/retrieval.py:78-128` — `search_notes`, where the rerank stage would sit
* `zettel/retrieval.py:213-296` — `_apply_relevance_floor`, whose docstring states the problem this ADR addresses
* `zettel/retrieval.py:130-160` — `search_distant_analogies` (must be exempt, ADR-043)
* `zettel/retrieval.py:385-413` — `_expand_with_graph` (must run after the rerank, ADR-009)
* `zettel/retrieval.py:34-49` — `RetrievedNote`, the provenance contract gaining `rerank_score`
* `zettel/config.py:358-384` — `RelevanceFloorConfig` / `RetrievalConfig`, where `rerank` would be added
* `zettel/config.py:140-159` — `EmbeddingConfig`, the two-model-class schema this extends
* `zettel/ask.py:101-118` — `retrieval_params`, which must carry the rerank envelope (P2)
* `zettel/index.py:423-431` — `_collection_metadata`, the missing `hnsw:space`
* `zettel/index.py:332-354` — `_build_embedding_fn`, including the declared-but-uninstalled `sentence-transformers` path
* `config/config.yaml:238-278` — operational retrieval block
* `evals/README.md` — the live runner declared as a follow-up (P1)
* `scripts/probe_relevance_floor.py` — reproduces every similarity/normalisation figure in this ADR. Measured 2026-09-06 against the live store (47 permanent notes, `ollama/qwen3-embedding@1024d`); no LLM calls, embeds only the probe queries. Run it after any embedding change, since the threshold means nothing across models.
* [Qwen3-Reranker overview](https://qwen-ai.com/qwen-embeddings/) — variants, MTEB Reranking scores, two-stage workflow
* [ollama/ollama#16076](https://github.com/ollama/ollama/issues/16076) — first-class reranker support, still open
