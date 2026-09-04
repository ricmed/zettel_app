# ADR-033: Invisible-Unicode Sanitization and a Text-Layer Probe Before Docling

**Status**: Accepted (2026-09-03)

**Depends on:** [ADR-012: Docling as the Mandatory PDF Extractor](./ADR-012-docling-pdf-extraction-pymupdf-fallback.md)

**Related to:**
- [ADR-007: Layered Checksums for Incremental Processing](../INFRA/ADR-007-layered-hashing-strategy.md)
- [ADR-011: Three-Layer Duplicate Detection](./ADR-011-three-layer-duplicate-detection.md)
- [ADR-017: Confidence-Band HITL Approval Gate](../REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)

## Context and Problem Statement

Extracted text is the only thing that reaches the LLM. It travels from a PDF or Markdown file into `sources.extracted_text`, into chunks, into Prompt 1, into the embedding, and finally into a vault note a human reviews. Two failure modes on that path were unhandled.

**Invisible characters survive the whole chain.** Zero-width joiners, bidi overrides and — most sharply — the Unicode tag block (U+E0000–U+E007F, which encodes arbitrary ASCII with no visible glyph) pass through NFKC normalization, chunking, the prompt and the vault renderer without appearing anywhere a reviewer can see them. A document can therefore carry text addressed to the extraction model that the HITL gate cannot inspect, because the gate is a human reading rendered Markdown. The review gate is the project's main safety property; a channel it structurally cannot see undermines it.

**A scanned PDF is discovered only after paying for it.** Docling runs at roughly 1.5s/page on CPU. A photographed book with no text layer produces an empty or near-empty extraction, and the harvest then either creates a source with junk chunks or logs "Nenhum texto extraido" after the whole conversion has already run. Neither outcome tells the operator what to do about it.

## Decision Drivers

* The review gate is a human reading the vault; anything invisible there must not exist in the persisted text.
* Sanitization must happen **before** `extraction_checksum`, or the same content in two encodings would stop colliding on duplicate-detection layer 2 (ADR-011).
* `hashing.normalize_text_for_hash` is the canonical normalizer for *visible* text and must stay the only place that rewrites it — a second normalizer competing over hyphens, NBSP or accents would make drift impossible to reason about.
* ADR-012 removed PyMuPDF deliberately; a cheap pre-Docling probe must not reintroduce it as a dependency.
* One unusable file in the inbox must not abort the batch: the operator's other documents should still land.
* The abort message has to be actionable — naming OCR — without the app taking on an OCR dependency.

## Considered Options

* Sanitize inside `hashing.normalize_text_for_hash`, next to the existing NFKC step.
* Sanitize in a dedicated module applied once at the extraction boundary.
* Sanitize at prompt-assembly time, in `llm.call_llm`.
* For the scan check: let Docling run and treat an empty result as the signal.
* For the scan check: reintroduce PyMuPDF for a page-text probe.
* For the scan check: probe with `pypdfium2`, already installed as a Docling dependency.

## Decision Outcome

**Sanitization lives in `zettel/text_sanitize.py` and is applied exactly once, in `harvester.extract.extract_text`** — the single funnel through which both PDF and Markdown extraction return. The persisted `extracted_text`, the extraction checksum, every chunk, every prompt and every embedding are computed from the cleaned string, so there is no path on which an invisible character can reappear downstream.

Folding it into `normalize_text_for_hash` was rejected: that function is applied to comparison inputs all over the codebase (chunk text, quotes, note bodies), so cleaning there would sanitize what is *hashed* while leaving what is *stored and sent to the LLM* dirty — the wrong half. Sanitizing at prompt-assembly time was rejected for the same asymmetry in the other direction: the vault would keep the invisible payload.

The strip list is deliberately narrow — zero-width and format controls, bidi embedding/override/isolate marks, BOM, and the tag block. NBSP, soft hyphens and every visible character are left to `normalize_text_for_hash`. The two normalizers therefore have disjoint jurisdictions: invisible characters belong to `text_sanitize`, visible ones to `hashing`.

**The scanned-PDF check is a `pypdfium2` probe of the first three pages, run before the Docling converter is built.** pdfium arrives with Docling, so this adds no dependency and does not reintroduce the PyMuPDF fallback ADR-012 removed — it is a *pre-flight read*, not a second extractor: it never produces text that reaches the pipeline. Fewer than 40 visible characters across those pages means there is nothing to extract, and `EmptyTextLayerError` is raised before any GPU/CPU time is spent. When pdfium is unavailable the probe silently returns; the post-extraction emptiness check in `extract_text` catches the same file, only later and more expensively.

`EmptyTextLayerError` subclasses `PdfExtractionError`, so it inherits ADR-012's fail-fast contract, and `run_harvest` catches `PdfExtractionError` **per file**: the failure is recorded as a `HarvestSkip` and the loop continues. `run_harvest` therefore returns a `HarvestOutcome` (`source_ids` + `skipped`) rather than a bare list, which is what lets the CLI exit non-zero while still reporting the sources it did create, and lets the web worker surface the actual reason in `web_job_events` instead of a generic "no source created".

### Positive Consequences

* An instruction hidden in a document cannot reach the extraction model through a channel the reviewer cannot see.
* A scanned PDF fails in milliseconds with a message naming `ocrmypdf`, instead of after a full conversion.
* Duplicate-detection layer 2 gets *stronger*: two files whose only difference is invisible padding now collide on the extraction checksum.
* A bad file no longer costs the operator the rest of the inbox.

### Negative Consequences

* `extraction_checksum` changes for any file whose text contained invisible characters. Sources harvested before this change are **not** migrated: they keep their old checksum until re-harvested. Silently rewriting them would invalidate every downstream chunk and LIT for no operator-visible reason.
* The 40-character / 3-page threshold is a heuristic. A document whose first three pages are genuinely a full-bleed cover image with a text-bearing body afterwards is rejected and needs `zettel harvest` on an OCR'd copy — accepted as the cheaper error, since the alternative is paying full conversion cost on every scan.
* `run_harvest`'s return type changed, so every caller (CLI `harvest` and `run-all`, the web worker's `harvest` and `run_all` jobs) had to be updated.

## Pros and Cons of the Options

### Sanitize once at the extraction boundary (chosen)

* Good, because every downstream consumer reads from the same cleaned string with no coordination.
* Good, because it precedes the extraction checksum, keeping layer-2 dedupe honest.
* Bad, because already-harvested sources are inconsistent with newly harvested ones until re-harvest.

### Sanitize inside `normalize_text_for_hash`

* Good, because there would be one normalization function instead of two.
* Bad, because it cleans what is hashed and compared, not what is stored and sent — the payload would still reach the model and the vault.
* Bad, because it entangles two jurisdictions (invisible vs. visible text) that have different rules and different reasons to change.

### Sanitize at prompt-assembly time

* Good, because it protects the model with a single choke point in `llm.py`.
* Bad, because the vault, the embeddings and the excerpt block keep the invisible payload.

### Let Docling report the empty result

* Good, because it needs no new code.
* Bad, because the cost is already paid by the time the answer arrives.
* Bad, because "empty extraction" does not distinguish a scan from a broken converter, so the message cannot be actionable.

### Reintroduce PyMuPDF for the probe

* Good, because its text API is convenient and was previously in the tree.
* Bad, because ADR-012 removed it on purpose; bringing back the import invites the fallback path back with it.
* Bad, because it adds a dependency for a check pdfium already covers.

## Consequences

Harvest gained a documented refusal: `HarvestOutcome.skipped` carries `empty_text_layer` (no usable text) and `extraction_failed` (Docling raised). The CLI prints the list and exits 1; the web worker raises a `UserFacingError` carrying the same message.

Any future extractor (EPUB, DOCX, HTML) gets sanitization for free by returning through `extract_text`, and should raise `EmptyTextLayerError` for its own equivalent of a scan rather than inventing a new refusal type.

Prompt-injection scanning of *generated* notes is a different problem at a different boundary and is deliberately out of scope here.

## References

* `zettel/text_sanitize.py` — `strip_invisible_unicode`, `sanitize_extracted_text`, `visible_char_count`, `INVISIBLE_RANGES`
* `zettel/harvester/extract.py` — `extract_text` (single sanitization point), `assert_pdf_has_text_layer`, `EmptyTextLayerError`
* `zettel/harvester/pipeline.py` — `HarvestOutcome`, `HarvestSkip`, per-file catch in `run_harvest`
* `zettel/hashing.py` — `normalize_text_for_hash` (visible-text normalizer, unchanged)
* `tests/test_text_sanitize.py` — strip/idempotency, checksum collision, probe thresholds, batch safety
