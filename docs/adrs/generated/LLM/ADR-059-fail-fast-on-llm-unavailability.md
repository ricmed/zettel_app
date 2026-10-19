# ADR-059: Fail-Fast on LLM Unavailability

**Status:** Accepted
**Date:** 2026-09-09
**Number:** Written on 2026-09-09 as ADR-045. Renumbered **059** on 2026-10-19 (issue #275): that number already belonged to the corroboration decision of 2026-09-07 ([ADR-045](../REVIEW/ADR-045-cross-source-overlap-is-corroboration.md)). The decision text is unchanged.
**Depends on:** [ADR-024: Pluggable Multi-Provider LLM Strategy](./ADR-024-multi-provider-llm-strategy.md)
**Related to:** [ADR-033: Invisible Unicode Sanitization and Text-Layer Probe](../HARVEST/ADR-033-invisible-unicode-sanitization-and-text-layer-probe.md), [ADR-023: SQLite-Backed Job Queue](../WEB/ADR-023-sqlite-backed-job-queue-single-worker.md)

## Context and Problem Statement

`call_llm` forwarded raw LangChain exceptions. Extract marked every remaining chunk `failed`. Connect swallowed the error and tried the next concept. Harvest bibliography fell back to a seed and kept walking the inbox. `get_llm` ImportError left the `runs` row unfinished.

A down provider is not a content defect. Marking the whole queue `failed` forces `retry-failed`. Trying every item burns the LangChain retry budget N times.

Image description already classifies 429s and stops the batch (`images.rate_limit_abort_after`). Harvest isolates *Docling* failures per file (ADR-033). Neither rule covers a dead chat model.

ADR-024 forbids switching provider at call time. The remaining choice is how the current provider's outage stops the phase.

## Decision Outcome

**Transport, auth, and missing-package errors become `LLMUnavailableError` in `get_llm` / `call_llm`.** Parse and schema failures stay per-item.

1. `llm.max_retries` is the budget for **one** HTTP call (LangChain client; Ollama is wrapped because `ChatOllama` has no such field).
2. After that budget is exhausted, harvest / extract / connect **stop**. They do not try the next SRC, LIT chunk, or ZTL candidate.
3. The item in flight is not marked `failed` — chunk stays `pending`, concept stays `approved` without a note, SRC is not written. Re-run the same command.
4. CLI prints the message and exits 1. The web worker treats it like `UserFacingError`. `finish_pipeline_run(..., "failed")` always runs.

Docling `PdfExtractionError` is unchanged: one bad file is skipped, the inbox continues.

## Consequences

An outage is visible and cheap to resume. A malformed JSON on one chunk still marks that chunk `failed` and the batch continues. There is no second provider and no health probe in `doctor` or preflight (ADR-037).

## References

* `zettel/llm.py` (`LLMUnavailableError`, `is_llm_unavailable`, `call_llm`, `get_llm`)
* `zettel/extractor.py`, `zettel/connector/run.py`, `zettel/connector/note.py`, `zettel/harvester/pipeline.py`, `zettel/bibliography.py`

## Addendum (2026-10-18): review dedupe fails fast (issue #272)

**Status:** Accepted amendment. `deduplicate_candidates` propagates `LLMUnavailableError` (and any exception `is_llm_unavailable` classifies). Concepts in that call stay `extracted`; the review run is recorded `failed` and the CLI exits 1 with the same message as extract. A parse error on one candidate leaves that concept `extracted` and lets the others continue. `get_llm` runs only when a candidate actually reaches the model. A concept whose `candidate_json` does not parse is logged with its id and counted by `zettel status` as `concepts_extracted`.

## Addendum (2026-10-18): vision is part of the same fail-fast (issue #258)

**Status:** Accepted amendment. `LLMUnavailableError` raised while describing images propagates out of `describe_pending_assets` and out of `run_extract`. Images that were not described stay `pending`. They are not marked `failed`. A rate-limit abort still leaves the rest of the batch `pending`, and a chunk whose neighborhood (same chapter, page ± 1) still has a pending image stays `pending` and is not sent to Prompt 1. Images disabled in config do not hold chunks back.

## Addendum (2026-10-18): a raw vision transport error is classified too

**Status:** Accepted amendment. Image description calls `llm.invoke` directly, so a dead provider arrives as `ConnectionError`, a timeout, or a provider exception, not as `LLMUnavailableError`. `describe_pending_assets` runs that exception through `is_llm_unavailable` / `_raise_unavailable(phase="images")` before the per-asset `failed` mark — the same boundary `call_llm` already uses. The asset stays `pending` and the extract run stops. A 429 still becomes `RateLimitExhausted` and keeps the existing retry-and-abort-the-batch path. A content error (empty answer, unreadable file) still marks that one image `failed`.

## Addendum (2026-10-19): a Prompt 2 rejection is per concept (issue #284)

**Status:** Accepted amendment. `ConnectRejected` and a Prompt 2 response that
does not parse do not fail the connect run and do not mark the concept `failed`.
A rejection stores `connect_rejected` plus the reason and category, and the next
`connect` does not call the model for it. `connect --requeue-rejected` returns
those concepts to `approved`; `--force-rejected` also asks the model to write
the note. A parse error is recorded in `llm_failures` (`phase='connect'`) and
the cached response is deleted, so the same broken JSON is not replayed. One
invalid connection (unknown type, duplicate, outside the RAG block, or past the
cap of 4) is dropped. The note is still written. `LLMUnavailableError` remains
the failure that stops the phase.

## Addendum (2026-10-19): embedding outages fail the connect run (issue #282)

**Status:** Accepted amendment. A dead embedding service is the same class of fault as a dead chat model. `Retriever(strict_embedding=True)` — what `run_connect` builds — turns a transport, auth or quota error in the vector search into `LLMUnavailableError` (`phase="embedding"`) instead of returning an empty hit list. The same classification wraps `upsert_permanent_note`. Prompt 2 is not called when the search already failed. The concept is marked `connecting` once its `note_id` is chosen and becomes `noted` only after the vector and the edges are stored, so the next `connect` reuses the file and the SQLite prompt cache. `ask` does not set `strict_embedding`: a vector failure there still falls through to BM25. Any exception that leaves `run_connect`, not only `LLMUnavailableError`, stores the run as `failed`.

## Addendum (2026-10-19): the PT-BR guard is on the same fail-fast (issue #289)

**Status:** Accepted amendment. `apply_ptbr_guard` goes through `cached_call_llm`. A replay of Prompt 2 does not pay the guard again. `LLMUnavailableError` and any exception `is_llm_unavailable` classifies are re-raised, so a dead provider during the translation does not leave an English body as if the guard had merely "failed". A parse error on the guard still keeps the original text.
