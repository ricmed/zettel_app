# ADR-XXX: Confidence-Band Human-in-the-Loop Approval Gate

**Status:** Accepted
**Date:** 2026-08-29, Resolved 2026-08-31
**Depends on:** [ADR-XXX: Granular Per-Chunk Literature Notes with Readable Filenames](../EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md)
**Used by:**
- [ADR-XXX: Post-Approval Concept Deduplication Timing](./ADR-016-post-approval-concept-deduplication-timing.md)
- [ADR-XXX: Web/CLI Auto-Approve Threshold Validation Asymmetry](./ADR-018-web-cli-validation-asymmetry.md)

**Related to:** [ADR-XXX: Dual-Store Persistence Without Cross-Store Transactions](../INFRA/ADR-005-dual-store-persistence.md)

## Context and Problem Statement

The REVIEW phase sits between literature-note extraction and connection into the permanent-note graph: every draft produced by the extractor carries an LLM-generated `review_confidence` score, and a human must decide whether each draft is trustworthy enough to persist. Neither fully automatic approval (no human check on LLM output) nor fully manual review (a human reads every single draft) fit the pipeline's throughput needs, since draft volume scales with how much literature is harvested.

The system instead partitions drafts into three confidence bands (very low `<= 0.4`, medium, and high `>= auto_approve_min_confidence`, default 0.7) and offers three review modes on top of that partition: batch-approve everything at or above threshold, batch-reject a chosen band with confirmation, and one-by-one review with per-draft shortcuts. Both the CLI and the web UI expose this same threshold semantics, and every decision — approve, reject, or skip — is persisted to SQLite immediately rather than staged in memory, so a review run can be interrupted and resumed. This pattern was introduced on 2026-08-29 (commit `5d9b504`, web interface implementation) and refined the same day to add purge-rejected and VACUUM support.

This coupling reaches beyond REVIEW itself: the extractor must keep producing a `review_confidence` value calibrated to these bands, and CONNECT downstream expects concepts to already be deduplicated and `approved` before it runs. Changing the approval strategy would require reworking the confidence-generation, CLI/web UX, and CONNECT's status expectations together.

**Threshold Calibration (Resolved 2026-08-31)**: The thresholds 0.4 (very-low cutoff) and 0.7 (default auto-approve) were established as initial heuristic values, not derived from an empirical analysis of the extractor's confidence distribution. They are treated as tunable, and operators should monitor actual operator workload and false-positive/false-negative rates to propose adjustments. A future phase could include a calibration analysis if confidence distribution shifts significantly with model updates.

## Decision Drivers

* Fully automatic approval would remove human oversight of LLM-generated literature notes entirely, which was rejected as unacceptable quality risk.
* Mandatory one-by-one review does not scale with extraction volume and would slow operators down even on high-confidence, likely-correct drafts.
* The extractor already computes a `review_confidence` score during Phase 2, so gating on it avoids introducing a new signal just for review.
* Concentrating operator attention on medium/low-confidence drafts, while letting high-confidence drafts move in bulk, is the core UX optimization the bands exist to provide.
* A review run must be resumable after a crash or interruption, which favors persisting each decision to SQLite immediately over holding decisions in memory until a batch commits.
* CONNECT consumes concepts only after they are deduplicated and marked `approved`, so the timing of dedup relative to approval affects when duplicate-concept LLM cost is paid.

## Considered Options

* Confidence-band gate with three review modes (batch approve above threshold, batch reject by band, one-by-one for the rest), decisions persisted immediately to SQLite.
* Fully automatic approval of all (or all above a fixed cutoff) drafts, with no human step.
* Mandatory one-by-one review of every draft regardless of confidence.

## Decision Outcome

Chosen option: "Confidence-band gate with three review modes," because it balances operator throughput against oversight quality — drafts the extractor is already confident about move through in bulk, while the medium- and very-low-confidence bands get the operator's individual attention where it is most needed. Reusing the extractor's existing `review_confidence` avoided building a second scoring mechanism, and immediate SQLite persistence (rather than in-memory staging) means a review session can be safely interrupted and resumed without losing already-made decisions.

**Threshold Calibration (Resolved 2026-08-31)**: The values 0.4 and 0.7 are initial estimates, not empirically calibrated. As of 2026-08-31, they remain the defaults, but operators are encouraged to monitor real-world impact and propose adjustments. A formal calibration pass (analyzing extractor confidence distribution) may be warranted in a future phase if this becomes a bottleneck or if upstream model changes shift the distribution significantly.

## Pros and Cons of the Options

### Confidence-band gate with three review modes (chosen)

* Good, because it lets operators triage by confidence instead of reading every draft with equal attention.
* Good, because it reuses the extractor's existing confidence signal rather than requiring new instrumentation.
* Good, because immediate per-decision persistence makes a review run resumable after a crash.
* Bad, because three modes across both CLI and web increase the test and UX surface area compared to a single flow.

### Fully automatic approval

* Good, because it removes the human bottleneck entirely.
* Bad, because it leaves no human check on any draft, risking incorrect literature notes reaching the permanent-note graph.
* Bad, because it was already considered and rejected in the codebase as an inferior option.

### Mandatory one-by-one review

* Good, because it guarantees maximum human oversight with no drafts skipped.
* Bad, because it slows operators down even on drafts the extractor is already highly confident about.
* Bad, because review time scales linearly with extraction volume with no bulk path.

## Consequences

The extractor's confidence score is now a load-bearing coupling point: any change to how `review_confidence` is computed shifts how many drafts fall into each band, changing operator workload and effective auto-approval rate without any direct code change in REVIEW itself. Developers modifying the extractor's confidence logic need to understand this downstream effect even though the two modules are not otherwise tightly coupled.

Because concept deduplication runs after human approval rather than at extraction time, duplicate concept definitions across chunks are only collapsed once a human has already approved the drafts that contain them, and CONNECT never sees pre-deduplication concepts. The cost and latency trade-off between the current post-approval timing and an alternative extraction-time dedup remains unquantified, but is not a blocking decision point.

**Web/CLI Asymmetry (Partially Resolved)**: A known asymmetry exists between the CLI and web entry points; see [ADR-018](./ADR-018-web-cli-validation-asymmetry.md) for the resolution.

## Addendum (2026-09-05): the score measures defects, not verbosity (issue #152)

**Status:** Accepted amendment — does not change the band mechanism above, replaces what feeds it.

*Consequences* called `review_confidence` a load-bearing coupling point. It was, and it was measuring the wrong thing. The formula in force until now spent 40% of its weight on the mean `definition` word count. Measured over the corpus, the gate it produced was exactly:

> `relevance_score == 4` **and** mean definition >= 47.5 words

Both halves are indefensible. One chunk failed at 47.0 words — half a word under the line — while carrying the same relevance and the same completeness as chunks that passed. And at `relevance_score == min_relevance_score` the ceiling was `0.30 + 0 + 0.40 = 0.70` against a 0.75 threshold: **no chunk at the relevance floor could ever auto-approve**, however clean, because the normalization `(rel - floor) / (5 - floor)` maps the floor to 0. That is a structural bar, not a strict standard, and it silently held back valid concepts (System Prompt vs. User Prompt, Zero-shot Prompting, Instruções Negativas — all scored 3 by the model).

Amendment — three terms, none of which is a length:

| term | weight | what it detects |
| --- | --- | --- |
| relevance | 0.50 | the only field that judges the *concept*. The filter floor earns `_RELEVANCE_FLOOR_CREDIT` (0.6) of the term, not 0 |
| integrity | 0.30 | the deterministic filter dropped candidates from this chunk |
| completeness | 0.20 | `intuition` / `limits` present — structural, not word count |

The floor credit is the substantive fix. The extract prompt **deliberately** compresses the scale: it instructs the model to pick the lower level when in doubt, calls 4 the "alvo preferencial" and reserves 5 for rare fundamental ideas, while `min_relevance_score` cuts 1–2. A floor score therefore means "valid technical concept, not surprising" — the scale's own words — and treating it as zero evidence was the bug.

The threshold is **no longer a percentile**. It sits at 0.75, just under the 0.80 a flawless chunk earns at the relevance floor, so every chunk with no detected defect auto-approves and every chunk with one (filter drop, or missing depth fields) goes to a human — and each rejection has a nameable cause. A percentile would guarantee a fixed rejection rate regardless of quality, which is the numerology this amendment removes. The Pydantic default was also 0.85 against the YAML's 0.75; both now read 0.75.

Measurement is reproducible offline: `scripts/calibrate_review_confidence.py` rescores every chunk from the persisted `summary_json` with **zero LLM and zero embedding calls**, reporting the old and new distributions, a threshold sweep, and a reachability table. Measured on the corpus available at the time (1 source, 13 scored chunks): old min 0.599 / median 0.746 / max 0.810, 46% approved; new min 0.800 / median 0.900 / max 0.900, 100% approved. **The 100% is honest, not a regression**: that corpus contains no chunk with a detectable defect, and a gate that fires anyway is measuring noise.

Two limitations an operator must carry forward:

* **One source is not a calibration.** The script prints a warning when the corpus has a single source, and 0.75 is a provisional default until it is re-run on a wider corpus.
* **The `integrity` term is only partly measurable from history.** `summary_json` persists the post-filter view, so `candidates` holds only survivors. The script reconstructs the *ratio* from `rejected_candidates`, which is enough to score, but not the dropped candidates' fields. Issue #153 has since added `anchor_quote` to that record, so corpora extracted from 2026-09-05 on can be audited for what was thrown away; anything extracted earlier carries only thesis and reason.

`tests/test_calibrate_review_confidence.py::test_every_relevance_level_is_reachable_at_the_configured_threshold` is the guardrail: any future reweighting that re-creates an unreachable relevance level fails the suite.

## References

* `zettel/extractor.py` — `_score_review_confidence`, `_candidate_completeness`, `_W_RELEVANCE` / `_W_INTEGRITY` / `_W_COMPLETENESS` / `_RELEVANCE_FLOOR_CREDIT` (2026-09-05 addendum)
* `scripts/calibrate_review_confidence.py` — offline rescoring harness (`legacy_confidence`, `reconstruct_output`, `reachability`)
* `tests/test_calibrate_review_confidence.py` — reachability guardrail and harness unit tests
* `zettel/review.py:70-76` — confidence-band classification (`chunk_confidence_band`)
* `zettel/review.py:79-88` — band-based filtering (`filter_chunks_by_band`)
* `zettel/review.py:194-206` — non-interactive/auto-approve threshold enforcement
* `zettel/web/review.py` — `review` (GET `/review`, confidence bands for display filtering)
* `zettel/web_app.py` — review job dispatch (batch approve/reject routing)
