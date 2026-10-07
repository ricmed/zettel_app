# ADR-XXX: Post-Approval Concept Deduplication Timing

**Status:** Accepted
**Date:** 2026-08-29
**Depends on:** [ADR-XXX: Confidence-Band Human-in-the-Loop Approval Gate](./ADR-017-confidence-band-hitl-approval-gate.md)

## Context and Problem Statement

The pipeline generates a candidate permanent note (concept) for every approved chunk during EXTRACT, without checking whether that candidate duplicates a concept already produced elsewhere in the same source. Concepts sit as `awaiting_review` until a human approves or rejects their source chunk in REVIEW, at which point approved concepts move to `extracted`. CONNECT then reads only concepts with `status=approved` to generate permanent notes, so something has to collapse equivalent concept definitions before CONNECT runs, or CONNECT will produce multiple overlapping notes for the same idea.

The system resolves this by running semantic deduplication once, after chunk approval and before CONNECT: `_dedupe_approved_concepts()` collects every concept in `extracted` status (scoped to the source being reviewed), and delegates to the same LLM-based merge logic that extraction-time dedup would have used, promoting survivors to `approved` and eliminating merge losers. This ties dedup timing directly to the human review step rather than to either end of the pipeline.

The ordering is deliberate: dedup happens between REVIEW and CONNECT, never inside EXTRACT. It has been stable since it was introduced (commit 5d9b504, 2026-08-29), with the intent made explicit in a code comment on the function itself.

## Decision Drivers

* Deferring dedup until after chunk approval avoids paying LLM merge cost on candidate concepts a reviewer later rejects, since REVIEW filters out low-confidence drafts before dedup ever runs.
* CONNECT's contract requires `status=approved` concepts only, so some merge step must run before CONNECT reads that status, or it will generate multiple overlapping permanent notes for the same idea.
* Running dedup inside EXTRACT would process every candidate concept regardless of whether a human ever approves its source chunk, spending LLM calls on drafts that get discarded.
* Running dedup inside CONNECT would be too late, because permanent notes would already exist for each unmerged duplicate by the time an overlap was detected.
* The post-approval step reuses the same merge logic extraction-time dedup would have used, so this decision only changes when dedup runs, not how it decides equivalence.
* The dependency is hard, not advisory: if dedup does not run to completion after approval, concepts never reach `approved` and CONNECT silently sees nothing for that source.

## Considered Options

* Post-approval deduplication: dedup runs once after chunk approval, before CONNECT (chosen)
* Extraction-time deduplication: dedup runs during EXTRACT on every generated candidate
* Deduplication inside CONNECT: dedup runs at note-generation time, after candidates are already being turned into notes

## Decision Outcome

Chosen option: post-approval deduplication, because it lets human review filter out low-confidence or rejected chunks before any dedup LLM call is made, so cost is paid only for concepts a human has already judged worth keeping, while still guaranteeing CONNECT never reads an unmerged duplicate since dedup is the only path from `extracted` to `approved`.

The status chain `awaiting_review` → `extracted` → `approved` (or eliminated as a merge loser) makes this ordering explicit in the data model: CONNECT's `get_concepts_by_status("approved", ...)` query only returns concepts that already passed through the merge step, so the pipeline cannot accidentally skip dedup without CONNECT also seeing zero eligible concepts.

## Pros and Cons of the Options

### Post-approval deduplication (chosen)

* Good, because LLM dedup cost is paid only for concepts from chunks a human already approved, not for every candidate EXTRACT generates
* Good, because CONNECT's input is guaranteed duplicate-free without CONNECT itself needing any merge logic
* Good, because it reuses extraction-time dedup's existing merge algorithm, adding no new logic to maintain
* Bad, because it creates a hard ordering dependency — if REVIEW is interrupted before dedup completes, concepts are stranded in `extracted` and never reach CONNECT

### Extraction-time deduplication

* Good, because duplicates would be caught earlier, before any human review effort is spent on redundant drafts
* Bad, because it would deduplicate every generated candidate, including chunks a human later rejects, wasting LLM cost on drafts that never survive review
* Bad, because it decouples dedup from the approval decision, so a chunk's approval status could no longer be used to scope which concepts need merging

### Deduplication inside CONNECT

* Good, because it would keep REVIEW focused solely on chunk approval, with no dedup responsibility
* Bad, because permanent notes could already be generated for unmerged duplicates before CONNECT detects the overlap, requiring note-level cleanup instead of a concept-level merge
* Bad, because it would place LLM dedup cost on the critical path of note generation rather than as a discrete step after review

## Consequences

Because CONNECT depends entirely on the `approved` status being reachable only through this dedup step, any future change to REVIEW's approval flow (batch approve, reject submenu, one-by-one review) must continue to call `_dedupe_approved_concepts()` on every path that promotes concepts out of `extracted`, or CONNECT will silently receive no concepts for that source. The three approval code paths in `review.py` already share this call, but the coupling is implicit rather than enforced by a type or contract.

The dedup step's reliability directly determines corpus quality: an LLM merge that is too aggressive collapses genuinely distinct concepts, while one that is too conservative lets duplicate permanent notes reach CONNECT. [NEEDS INPUT: What is the observed false-positive/false-negative rate of `deduplicate_candidates()` in production, and has it been evaluated separately from extraction-time dedup's calibration?]

If dedup fails partway through a batch (LLM error, timeout), the current code returns early without persisting partial progress beyond what `deduplicate_candidates()` itself commits, leaving affected concepts in `extracted` until REVIEW is re-run for that source. [NEEDS INPUT: Is there a defined retry or resume procedure for a failed post-approval dedup batch, or does it require manually re-invoking review for the source?]

## References

* `zettel/review.py:636-671` — `_dedupe_approved_concepts()`, collects `extracted` concepts and delegates to the shared merge logic
* `zettel/review.py:475-477` — status transition from `awaiting_review` to `extracted` on chunk approval
* `zettel/extractor.py` — `deduplicate_candidates()`, the LLM-based merge logic shared with extraction-time dedup
* `zettel/state/concepts.py` — `get_concepts_by_status()`, `update_concept_status()`, backing the status-driven handoff to CONNECT

## Amendment (2026-09-07)

The **timing** decided here is unchanged: dedupe still runs post-approval, still gates the `extracted` → `approved` transition, and every approval path must still call `_dedupe_approved_concepts()`.

Its **scope** is superseded by [ADR-045](./ADR-045-cross-source-overlap-is-corroboration.md): the comparison is now filtered to the candidate's own `source_id`. Note that this ADR never decided the scope — its Context describes candidate collection as "scoped to the source being reviewed", while the *comparison* was global purely because `permanent_notes` is one collection. A hit from another source is corroboration, gets a typed edge at `connect`, and never reaches the LLM here.

A `refine_existing` / `merge` verdict now crosses into `connect` through `concepts.dedupe_json` and becomes an `extends` edge. Before 2026-09-24 it was silently lost at the review/connect boundary; see the [ADR-045 amendment](./ADR-045-cross-source-overlap-is-corroboration.md#amendment-2026-09-24-refine_existing-now-reaches-connect).

## Addendum (2026-10-06): shadow verdict and reviewer label

Timing and scope are unchanged. After the dedupe LLM decides, `extractor.deduplicate_candidates` also calls `decision.shadow.shadow_dedupe`. The typed decision layer then asks two questions over exactly the same-source notes the LLM saw:

- a `choice` between `create_new`, `ignore` and `link`. `refine_existing` and `merge` collapse into `link`, since they have the same effect;
- a `choice` of target note, which includes a `none` option.

The answer is written to `decision_shadow` and never read to decide ([ADR-055](../LLM/ADR-055-typed-decision-layer-shadow.md)). When the reviewer resolves a `dedupe_pending` concept, `review.keep_duplicate` and `review.discard_duplicate` attach `not_ignore` or `ignore` to that row. This is the first human label this decision has had. It covers only what the LLM itself flagged.

## Addendum (2026-10-07): the dedupe LLM sees whole notes (#209)

Until now `extractor._format_existing_notes` showed the LLM the first 200 characters of each same-source note. Every note starts with `> **Tese**: `, so 270 of 781 theses were cut, and the definition never appeared. Each existing note now goes into the prompt in full, through `extractor.existing_note_contents` and `zettel/note_content.py`: thesis, definition, intuition, example and limits, read from SQLite. Managed blocks and `## Conexões` stay out. The only cap is `linking.dedupe_note_chars` (6000), set above any real note. The L2 distance the prompt used to print is gone, because it is not a calibrated signal for this judgement.

Measured before switching (`evals/preregistration/209-dedupe-texto-completo.md`, `scripts/probe_dedupe_context.py`). The setup:

- the 64 items of the #206 dedupe sheet;
- the same prompt, model (`openai/gpt-4o-mini` @ 0.2), candidate and set of existing notes in both conditions;
- two runs per condition.

| condition | correct | population-weighted | target correct |
|---|---|---|---|
| 200-char excerpt (`trunc-a`) | 25/64 | 0.49 | 0.52 |
| full content (`full-a`) | **43/64** | **0.69** | **0.63** |

- **Effect:** exact McNemar p = 0.0005 (22 items only `full-a` gets right, 4 only `trunc-a`).
- **Stability:** repeat runs agree on 98–100% of items.
- **Validity:** no invalid answers.
- **Pre-registered rule:** non-inferior and valid, so the change was adopted.

**Correction (2026-10-07): the table above was scored against score-based labels; against manual labels the result reverses.** The first sheet followed a 0–1 similarity score with fixed cuts: below 0.30 "new", 0.30–0.48 "develops", above 0.48 "repeats". The labeller then relabelled the same 64 items by meaning, item by item, yielding 38 "new", 23 "develops" and 3 "repeats" (`evals/gold/dedupe-rotulos.json`, `method: manual_blind`). The 256 recorded answers were rescored against those labels with no new call (`evals/results/dedupe-context-209.json`):

| condition | correct | population-weighted | "develops" answered |
|---|---|---|---|
| 200-char excerpt (`trunc-a`) | **40/64** | **0.65** | 26 |
| full content (`full-a`) | 26/64 | 0.42 | 53 |
| Jev shadow (200-char excerpt) | 48/64 | 0.72 | — |

- **Effect:** exact McNemar trunc-a × full-a p = 0.0125 (21 items only the excerpt gets right, 7 only the full content). Repeat runs are stable.
- **Where it goes wrong:** with whole notes the LLM turns 29 of the 38 "new" items into "develops". It always finds some link between notes of one work, and the prompt pushes it there ("an author returning to a concept is expanding it").
- **Pre-registered outcome:** the non-inferiority rule fails, so the pre-registered outcome was **not to adopt** the full content and to fix the prompt first.

**Decision (deviation, recorded):** the full content **stays** in production, by explicit decision, paired with a prompt fix ([#218](https://github.com/ricmed/zettel_app/issues/218)). The reasoning: the excerpt scores better partly because it hides the context that triggers over-linking, not because it judges better. A judge should compare whole notes with criteria that do not read "same topic" as "develops". Until #218 lands, the dedupe LLM over-links against the manual labels, and this is a known, measured regression. #218's acceptance bar is to recover at least the excerpt's 40/64 with the full content, validated on fresh labels.
