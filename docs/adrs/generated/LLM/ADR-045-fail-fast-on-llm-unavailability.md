# ADR-045: Fail-Fast on LLM Unavailability

**Status:** Accepted
**Date:** 2026-09-09
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
