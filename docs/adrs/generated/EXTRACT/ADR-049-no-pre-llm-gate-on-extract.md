# ADR-049: No Pre-LLM Gate on Extract — the Signal Is Real, the Saving Is Not

**Status**: Accepted (2026-09-11) — the decision is **not** to build the gate.

**Depends on:** [ADR-015: Granular Per-Chunk Literature Notes with Readable Filenames](./ADR-015-granular-literature-notes-readable-filenames.md)

**Related to:**
- [ADR-011: Three-Layer Duplicate Detection Strategy for Source Ingestion](../HARVEST/ADR-011-three-layer-duplicate-detection.md)
- [ADR-017: Confidence-Band Human-in-the-Loop Approval Gate](../REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)
- [ADR-037: Pre-Flight Cost Estimate as a Pure Function](../CLI/ADR-037-llm-cost-preflight-estimate.md)
- [ADR-046: Bibliographic Duplicate Layers Before the Semantic One](../HARVEST/ADR-046-bibliographic-duplicate-layers.md)

## Context and Problem Statement

`extract` fires one Prompt 1 call per pending chunk, and roughly a quarter of those chunks come back rejected — a table of contents, a reference list, a narrative aside. The call is paid for either way. The obvious optimisation, and the one the literature keeps demonstrating (Hu et al., *ACS Omega* 2025, cut a paper-mining pipeline from USD 7.40 to USD 1.96 by filtering candidates with embeddings before the expensive model), is to predict the rejection cheaply and skip the call.

Issue #66 prototyped exactly that: a logistic regression over chunk embeddings, trained on the verdict `extract` already persists in `summary_json.chunk_status`. The spike was never adopted, and never rejected either — it sat in `scripts/` with no ADR.

Two things had to be fixed before the question could even be asked (issue #173, commit `1cf6aa3`):

* The spike's headline metric was wrong. `calls_avoided_pct` was `(fp + tn) / n`, which counts `fp` — chunks the gate *sent* to the LLM — as savings. That is the base rate of rejection: constant across thresholds, blind to the predictions it claimed to measure. It reported 23.5% where the real saving was 1.7%.
* Validation split by chunk, not by source. Neighbouring chunks of a chapter are near-duplicates in embedding space, so a same-book split measures memorisation. Under that split the gate looked like an 11.6% saving at zero loss; grouped by source, it is 5.4%.

## Decision Drivers

* The chunk itself is never lost — a `chunks` row survives any status, `StateDB.reset_chunks_to_pending` takes an arbitrary status string, and only the opt-in `purge-rejected` deletes anything. What a gate destroys is subtler and worse: **the note the reader never learns should have existed**. There is no diff to inspect, no error to notice, and no way to tell a chunk the gate was right about from one it was wrong about without paying for the extraction the gate skipped. That asymmetry, not data loss, is what pins the operating point at zero measured loss.
* The vault's whole economic argument for automation is that a permanent note costs cents. That cuts both ways: it also caps what any optimisation of that cost can be worth.
* Precedent: issue #154 rejected a fence-ratio pre-LLM gate after measuring it. The bar for adopting one now is a measurement, not an intuition.
* Chunk embeddings are not free by default. `harvest.semantic_duplicate_enabled` is off (ADR-011/ADR-046), so `chunk_and_persist` does not populate the `chunks` collection and `reindex` skips it. A production gate needs its own embedding path.
* Criteria had to be fixed **before** seeing the numbers, or the exercise becomes a search for a threshold that justifies the work.

## Considered Options

* Logistic regression over chunk embeddings, one global threshold in config.
* The cheap deterministic heuristic already in the spike (length floor, alphanumeric ratio, table-line density).
* A hand-written rule targeting the dominant rejection category (`structural`).
* No gate.

## Decision Outcome

**No gate.** Measured 2026-09-11 over 611 labeled chunks from 5 sources (`ollama/qwen3-embedding@1024d`), leave-one-source-out, the pre-committed criteria came out:

| Criterion | Bar | Measured | |
|---|---|---|---|
| Accepted notes lost | `0%` | `0%` at threshold 0.352 | pass |
| Calls avoided at that loss | `>= 15%` | **5.4%** (USD 0.03 over the whole corpus) | **fail** |
| Catches more than `structural` | required | `structural 33/115`; `narrative 0/31`, `fragmented 0/10`, `promotional 0/2`, `trivial 0/4` | **fail** |

Two of three fail, so the pre-commitment decides it. The interesting part is *why*, because the naive reading ("the classifier does not work") is wrong.

**The signal is real.** Per-fold AUC is 0.752–1.000, and every fold separates accepted from rejected well above chance:

| Test fold | n | accepted / rejected | AUC | avoided @ 0% loss |
|---|---|---|---|---|
| `@Latorre2021AnaliseDe` | 19 | 13 / 6 | 1.000 | 31.6% |
| `@Huang2023TowardsReasoning` | 47 | 25 / 22 | 0.940 | 27.7% |
| `@Pate2026ReplicatingHuman` | 59 | 33 / 26 | 0.752 | 16.9% |
| `@Kim2022KantAnd` | 456 | 349 / 107 | 0.818 | 8.1% |
| `@Instrutor2023IniciandoCom` | 30 | 29 / 1 | 0.897 | 0.0% |

Three of five folds individually clear the 15% bar; pooled under one threshold they yield 5.4%. That is the honest production number, because one scalar in `config.yaml` is what production would actually have.

What fails is not the discrimination, it is **the shape of the zero-loss constraint under a single global threshold**. Zero loss means the threshold must sit below the lowest-scoring *accepted* chunk in the whole corpus, so one outlier fixes the operating point for everything else. That is an extreme-value constraint, not an average-quality one, and it behaves like one:

| Tolerated loss | Threshold | Calls avoided |
|---|---|---|
| 0 notes | 0.349 | 5.1% |
| **1 note** (of 449) | 0.377 | **8.3%** |
| 5 notes | 0.388 | 9.7% |

One accepted chunk out of 449 nearly doubles the saving. (This table reads the threshold as a strict `<` over the observed probabilities, so it reports 0.349 / 5.1% where the script's `operating_points` — which scans with `>=` — reports 0.352 / 5.4%. Boundary tie handling, immaterial to the decision.)

**The dominant source is not the cause.** The obvious suspicion — that a corpus 75% composed of one book drags the aggregate down — does not survive measurement. The chunk that sets the threshold belongs to `@Instrutor2023IniciandoCom`, the 30-chunk handout (p=0.349); the book's worst accepted chunk is only fourth in line (p=0.380). Removing the book from the corpus entirely moves the result from 5.1% to **6.5%**. What the book dominates is the arithmetic of the percentage and the training set of every other fold — not the binding constraint.

**Per-source thresholds do not rescue it either.** Giving each source its own zero-loss threshold — computed from that source's own labels, which production cannot do for a document it has not extracted yet — yields 10.6%. Even the oracle sits below the bar.

**At a safe operating point the classifier is a `structural` detector.** It catches 33 of 115 structural rejections and *nothing* else — not one of the 31 `narrative`, 10 `fragmented`, 2 `promotional` or 4 `trivial` rejections. Those are judgements about what the passage *says*; `structural` is a judgement about how it is *formatted*. Spending a 1024-dimension embedding to notice that a page is a table of contents is the wrong instrument for the only thing it reliably notices.

**The deterministic escape hatch does not work either.** Criterion 3 anticipated this outcome and pointed at a hand-written rule instead. Measured: a rule keyed on dotted leaders, numbered outlines and short-line density catches 10 of 115 structural rejections and costs 34 accepted notes. The spike's existing heuristic avoids 4.4% of calls but loses 1.78% of accepted notes — it is not a safe gate at any fixed point. Formatting-based rejection is not as separable from prose as it looks.

**And the money is not there.** USD 0.03 across 611 chunks at zero loss; USD 0.12 even at an unacceptable 4.9% loss. That is the same order of magnitude as issue #154's USD 0.006, against a new config knob, a new terminal chunk state, a model artifact tied to an embedding space, and a recovery path for chunks the gate got wrong.

**Nor is the time, and that argument inverts.** Wall-clock is independent of cost and could have gone the other way — a 5% saving on a long book is worth more in minutes than in cents. Measured, it is negative, because the gate must embed **every** chunk to score it while skipping the call on a few:

| | measured |
|---|---|
| One `extract` call | **2.37 s** (run 7: 495 calls in 1174 s) |
| Embedding one chunk | **845 ms** (`ollama/qwen3-embedding@1024d`, local, batch of 100) |

For the 456-chunk book at the global threshold: 24 calls skipped saves 0.9 min, embedding 456 chunks costs 6.4 min, net **-5.5 min** on a run that takes 20. The gate makes the largest source in the corpus 28% slower.

This generalises into the rule worth keeping, since it survives changes of model and provider that the percentages above do not:

> The gate wins wall-clock only when `calls avoided % > t_embed / t_call`.

At 845 ms against 2.37 s the break-even is **35.7% avoidance**. The best figure measured anywhere in this study is 31.6% — one 19-chunk fold, under its own oracle threshold. Under this configuration the gate cannot win on time even in the most favourable case that exists, let alone at the 5.3% it actually delivers.

### Positive Consequences

* `extract` keeps one code path. No `gated` status to thread through `zettel status`, the web dashboard, `preflight` and a reset command; no model artifact whose validity silently expires when `embedding.model` changes.
* No second embedding path competing with the `chunks` collection, so ADR-011/ADR-046's flag keeps governing writes and reads together — the property that keeps layer 5 from producing silent false negatives.
* The training label stays clean. A gate writing `chunk_status: rejected` for its own predictions would have made the next calibration train on its own output, entrenching its errors in a closed loop. Not building it avoids designing around that trap.
* The instrument survives the decision. `scripts/calibrate_pre_llm_gate.py` is now correct, grouped by source, and embeds missing vectors on demand, so re-running it costs nothing but local embedding time.

### Negative Consequences

* Roughly a quarter of extract calls will keep being paid for on chunks that come back rejected. At current prices this is cents per book, and it is a deliberate purchase of simplicity.
* Three of five folds showed 17–32% avoidable at zero loss, and a per-source oracle reaches 10.6% overall. Rejecting the global-threshold design leaves that on the table. Capturing it would require a threshold calibrated per source, and the labels for that only exist after paying for the extraction the gate was supposed to avoid.
* The measurement is one embedding model and 5 sources. It does not transfer across embedding changes. More labeled data would help the training side — holding the book out as a fixed test fold, avoided@0-loss climbs 4.0% -> 7.0% -> 7.2% -> 8.1% as training grows 38 -> 155 chunks, still rising at the maximum available — so these numbers are a floor for the model, not a ceiling.

## What Would Reopen This

* **Extract cost per chunk rising by an order of magnitude.** The decision is a cost/benefit at ~USD 0.003/chunk; a substantially pricier extraction model changes the arithmetic, not the measurement.
* **`harvest.semantic_duplicate_enabled` being turned on for its own reasons.** The gate's marginal cost is dominated by embedding chunks it would otherwise not embed. If those vectors already exist for dedupe, `t_embed` falls to ~0, the break-even rule is satisfied by any positive avoidance, and the gate becomes a small pure win (~0.9 min of 20 on the book).
* **A materially faster embedding path.** The 845 ms above is local Ollama; a batched API at ~50 ms/chunk would move the break-even from 35.7% to ~2%. Note this is not a free tuning knob: changing `embedding.provider`/`model`/`dimensions` invalidates every stored vector (`EmbeddingSpaceMismatch`), forces `zettel reindex --force`, and requires re-measuring `min_vector_similarity`, `chapter_floor` and the dedupe thresholds. Cheaper to check first whether the local path has batching or concurrency headroom.
* **A slower extraction model.** The break-even is a ratio, so it cuts both ways: a thinking model at ~10 s/call moves it to ~8%, close to what the book's own fold already delivers.
* **A substantially larger labeled corpus.** The learning curve above has not saturated, so the model side should improve. Note the counterweight, which is why this is not a promise: every new accepted chunk is another draw on the low tail, and the zero-loss threshold is a minimum over all of them. The two effects pull in opposite directions.
* **A loss budget that is not zero, paired with the two mechanisms that would make it honest.** This is the only lever the data actually offers, and it is a change of policy and design rather than of measurement — the model's discrimination is fixed; what moves is where one is allowed to sit on its curve (1 note tolerated: 8.3%; 4.9%: 18%).

  **Recoverability** is nearly free and already built. A `gated` status would plug into `reset_chunks_to_pending("gated", ...)` with no new persistence code, exactly as `retry-failed --rejected` already resets extract verdicts and drops the LLM cache so the retry is not a free replay. But recoverability alone is circular: identifying *which* gated chunks were mistakes requires extracting them, which spends what the gate saved.

  **An exploration arm** is what breaks the circle, and its absence is the deeper flaw in the design considered here. A deployed gate never obtains a verdict for what it skips, so those chunks never become labels: the next calibration trains only on the region the gate already accepts, and its errors become invisible and self-confirming. Letting a random sample of predicted-rejects through anyway — say one in ten — keeps generating labels inside the skipped region, which buys the false-negative rate *as observed in production* instead of an offline number assumed to hold, and training data not shaped by the gate's own past decisions. It costs a fraction of the saving and converts a one-shot bet into something measurable over time.

  Note what neither mechanism buys: reach. Even at a 4.9% budget the classifier catches 84/115 `structural` against 3/31 `narrative`. A looser budget purchases more table-of-contents detection, not comprehension. That ceiling belongs to the signal, not to the policy.

Re-measure before reopening — the numbers above are pinned to `ollama/qwen3-embedding@1024d` and to this corpus:

```bash
.venv/Scripts/python.exe scripts/calibrate_pre_llm_gate.py
```

The script aborts below three distinct labeled sources rather than reporting the leaky chunk-level split.
