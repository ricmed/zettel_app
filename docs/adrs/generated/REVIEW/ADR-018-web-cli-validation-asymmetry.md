# ADR-XXX: Web/CLI Auto-Approve Threshold Validation Asymmetry

**Status:** Accepted
**Date:** 2026-08-29, Resolved 2026-08-31
**Depends on:** [ADR-XXX: Confidence-Band Human-in-the-Loop Approval Gate](./ADR-017-confidence-band-hitl-approval-gate.md)
**Related to:** [ADR-XXX: Dual-Store Persistence Without Cross-Store Transactions](../INFRA/ADR-005-dual-store-persistence.md)

## Context and Problem Statement

The literature review phase gates automatic approval of extracted chunks behind a single configuration value, `literature_review.auto_approve_min_confidence` (default 0.7). The CLI's non-interactive review path enforces this threshold server-side: `run_review()` only calls `approve_chunk` when a chunk's `review_confidence` meets or exceeds the configured limiar, otherwise the chunk is skipped (`zettel/review.py:194-206`).

The web UI does not apply the same guarantee. The `/review` GET route uses the threshold only to bucket chunks into low/medium/high confidence bands for display filtering (`zettel/web.py:458-462`); it performs no server-side re-check. The `/review/action` POST endpoint accepts a list of `chunk_ids` and enqueues them for approval or rejection without validating their confidence against the threshold at all (`zettel/web.py:472-477`), and the job handler that ultimately calls `approve_chunk` for each id also performs no confidence check (`zettel/web_app.py` review operation handler). As a result, the same configuration key produces two different guarantees depending on which interface issues the approval: a contract on the CLI, a display hint on the web.

This asymmetry has existed unchanged since the web UI was introduced (commit 5d9b504, 2026-08-29) and was flagged in the codebase mapping as unresolved. On 2026-08-31, a decision was made to resolve it by enforcing the threshold server-side in the web path as well, eliminating the asymmetry and closing the bypass vector.

## Decision Drivers

* The threshold is meant to function as a single configurable gate, but only one of the two interfaces (CLI) actually enforces it as such.
* The `/review/action` POST endpoint accepts any submitted `chunk_ids` and approves them regardless of confidence, so a direct request to that endpoint can approve chunks that would be skipped by the CLI's own auto-approve logic.
* No code comment or documentation states whether the web path's lighter validation is an intentional convenience for manual override or an oversight.
* Approved chunks are recorded identically in SQLite whether they passed the threshold or not, so there is no way to distinguish a threshold-gated approval from one that bypassed it.
* Both approval paths converge on the same `approve_chunk` function, so any server-side check added there (or in the web job handler) would apply uniformly without duplicating logic per interface.
* No test fixtures currently exercise `/review/action`'s validation behavior, so the current asymmetry is not protected against accidental regression either way.

## Considered Options

* Keep the current asymmetry: CLI enforces server-side, web relies on client-side filtering only (legacy)
* Enforce the threshold server-side in the web approval path as well, mirroring the CLI's contract (chosen 2026-08-31)
* Document the asymmetry explicitly as intended behavior without changing enforcement

## Decision Outcome (Updated 2026-08-31)

**Chosen option: Enforce the threshold server-side in the web approval path**, because leaving `/review/action` unvalidated means the configured gate can be bypassed by any request to that endpoint, which contradicts the threshold's stated purpose. The decision prioritizes security and consistency over flexibility.

The web UI's looser validation was likely an oversight, not a deliberate override mechanism. Enforcing server-side eliminates the bypass path and makes `literature_review.auto_approve_min_confidence` a uniform gate across both CLI and web. 

**Implementation details** (to be addressed in code):
- Add a server-side confidence check in `web_app.py`'s review job handler before calling `approve_chunk`
- Verify that the threshold is re-checked even when a web operator manually selects individual chunk_ids for approval
- Ensure consistency: both CLI and web route through the same validated approval path
- Consider whether a future explicit "force approve" mechanism is needed for edge cases (unlikely, but document if not added)

### Positive Consequences (Resolved)

* The configured threshold becomes a real, interface-independent gate rather than a display-only hint on the web.
* A single enforcement point removes the risk of a developer assuming both interfaces already validate uniformly.
* Closes the specific bypass path identified: a crafted POST to `/review/action` can no longer approve below-threshold chunks silently.
* Both CLI and web now enforce the same contract: only chunks meeting the threshold can be auto-approved.

### Negative Consequences (Addressed)

* Removes the web UI's current (if undocumented) ability to approve any selected chunk regardless of confidence. 
* No explicit override mechanism is provided; if a future use case requires overriding the threshold, it should be added as a deliberate, auditable action (not implicitly via an unvalidated endpoint).
* Decision: Accepted trade-off. If override is needed later, add as an explicit "Force Approve (admin)" action that logs the override for audit.

## Pros and Cons of the Options

### Keep current asymmetry

* Good, because it preserves an implicit override path for a web operator reviewing chunks individually.
* Good, because it requires no changes to `web.py` or `web_app.py`.
* Bad, because the same configuration key yields different guarantees depending on the interface used.
* Bad, because there is no audit flag distinguishing a threshold-gated approval from a bypassed one.

### Enforce threshold server-side in web path (chosen)

* Good, because `literature_review.auto_approve_min_confidence` becomes a uniform gate across CLI and web.
* Good, because the check can be added once, in the shared `approve_chunk` call path or the web review job handler.
* Bad, because it eliminates the web UI's current implicit flexibility to approve any selected chunk during manual review.
* Answered 2026-10-19 (issue #275): the limiar applies only to automatic approval. A human selecting chunk or concept ids on `/review` is approved as submitted and recorded as `mode=web_batch`. See the addendum below. The 2026-08-31 choice to enforce the limiar on that selection is superseded.

### Document the asymmetry explicitly

* Good, because it resolves the ambiguity without any code change.
* Good, because it is the lowest-effort way to close the "undocumented gap" finding.
* Bad, because it leaves the bypass path in place if the asymmetry turns out to be unintentional.
* Bad, because it does not address the missing audit trail for threshold-bypassed approvals.

## Consequences (Updated 2026-08-31)

With server-side threshold enforcement now chosen:

* Every future change to the review approval flow — CLI or web — must route through the same validated path.
* Any new web action that approves chunks in bulk must respect the threshold check, preventing reintroduction of a bypass.
* Existing approved chunks that were approved via the web path without server-side validation remain in their current state; no retroactive audit distinguishes them from threshold-gated approvals. This is an acceptable historical artifact.
* The threshold configuration now carries uniform semantics across both interfaces, reducing cognitive load on operators and developers who reason about the review pipeline.
* Future feature requests to override the threshold (e.g., for manual, deliberate approval of low-confidence chunks) must go through an explicit, auditable mechanism rather than an implicit endpoint bypass.

## References

* `zettel/review.py` — `run_review` (CLI non-interactive path, server-side threshold enforcement)
* `zettel/web/review.py` — `review` (GET `/review`, threshold used for display filtering only)
* `zettel/web/review.py` — `review_action` (`POST /review/action`, no confidence validation before enqueue)
* `zettel/web_app.py` — web job handler for the `review` operation, approves submitted chunk_ids without a threshold check

## Addendum (2026-10-19): duplicate queue and run-all source scope (issue #274)

**Status:** Accepted. Pending same-source duplicates (`dedupe_pending`) are decided on `GET /review?queue=duplicates`. Keep and discard call `keep_duplicate` / `discard_duplicate`, which write the human label onto `decision_shadow`. The page itself never approves by the confidence limiar.

`run-all` passes `source_ids` from that harvest into `run_review`. An empty harvest approves nothing. With `--auto-approve` (CLI) or the web checkbox, only drafts of those sources at or above the limiar are approved, and the result reports `approved_threshold`. Drafts from any other source stay `awaiting_review`.

## Addendum (2026-10-19): the limiar gates automatic approval only (issue #275)

**Status:** Accepted. Supersedes the 2026-08-31 decision to re-check `auto_approve_min_confidence` on a web operator's manual selection. The score does not separate keep from discard (AUC 0.49, ADR-017), so it must not block a human who read a draft and chose it. The limiar applies to automatic approval: `zettel review --yes` / `--auto-approve`, `zettel extract --auto-approve`, and `run-all --auto-approve` (the web checkbox). A selection on `/review` is approved as submitted and recorded in `review_events` with `mode=web_batch` and `surface=web` (#270). `mode=manual` is the CLI one-by-one path. Neither mode is a threshold bypass to close; both are the human decision the gate exists to collect.
