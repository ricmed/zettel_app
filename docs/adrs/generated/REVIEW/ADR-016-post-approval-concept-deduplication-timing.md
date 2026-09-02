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
* `zettel/state.py` — `get_concepts_by_status()`, `update_concept_status()`, backing the status-driven handoff to CONNECT
