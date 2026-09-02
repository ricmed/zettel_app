# ADR-XXX: Docling as Primary PDF Extractor with PyMuPDF Fallback
**Status:** Accepted
**Date:** 2024-08-30, Resolved 2026-08-31
**Used by:**
- [ADR-XXX: Hybrid Structural Chunking (H1-H6 Boundaries + Recursive Splitter)](../ADR-014-hybrid-structural-chunking-strategy.md)
- [ADR-XXX: Three-Layer Page Inference Strategy for Chunk Page Metadata](../ADR-013-three-layer-page-inference-strategy.md)
- [ADR-XXX: Three-Layer Duplicate Detection Strategy for Source Ingestion](../ADR-011-three-layer-duplicate-detection.md)

**Related to:** [ADR-XXX: Layered Hashing Strategy for Deterministic Caching and Drift Detection](../../INFRA/ADR-007-layered-hashing-strategy.md)

## Context and Problem Statement

The HARVEST module ingests PDF files from the inbox and must turn them into text suitable for structural chunking, page-locator inference, and downstream literature-note generation. PDFs make up the majority of typical inbox files, so the extraction strategy chosen here directly determines whether headings, page structure, and embedded images survive into the rest of the pipeline, or whether only plain, unstructured text is available.

The system uses a two-tier extraction strategy selected by configuration: Docling runs as the primary extractor, optionally GPU-accelerated, producing Markdown-formatted text with heading hierarchy preserved and an optional image-extraction path for multimodal assets. PyMuPDF (`fitz`) serves as the fallback extractor when Docling is not selected, returning plain text with no layout information; it is also reused independently to build a page-to-heading map used for content-start paging inference, regardless of which extractor produced the chunked text.

Both extractors were historically wired in with fallback; however, a decision was made on 2026-08-31 to make Docling the primary and only extractor, eliminating the PyMuPDF fallback entirely to remove the AGPL-3.0 licensing risk that would block distribution beyond personal/local use. This choice prioritizes distribution flexibility over the robustness of an automatic plain-text fallback.

## Decision Drivers

* The majority of inbox source files are PDFs, so extraction quality here sets a ceiling on chunking and page-inference quality for most of the corpus.
* Docling's Markdown output with heading hierarchy is a prerequisite for structural (H1-H6) chunking and for accurate content-start page inference.
* A fallback extractor is needed so harvest does not hard-fail when Docling is unavailable, misconfigured, or GPU acceleration cannot be used.
* PyMuPDF's page-to-heading map is used for paging regardless of which extractor produced the chunked text, making its accuracy a shared dependency for both paths.
* Local, self-hosted extraction avoids per-document API cost and external service dependency compared to a cloud/LLM-based alternative.
* GPU acceleration is detected at runtime rather than required at startup, so extraction quality can silently degrade on machines without a usable GPU.

## Considered Options

* Docling as primary extractor (GPU-accelerated, structured Markdown) with automatic PyMuPDF fallback (legacy)
* Docling as the sole extractor, removing PyMuPDF fallback entirely (chosen 2026-08-31)
* PyMuPDF as the sole extractor for all PDFs
* LLM-based PDF extraction (e.g., a multimodal model reading the document directly)

## Decision Outcome (Updated 2026-08-31)

**Updated Chosen Option: Docling as sole extractor** (removing PyMuPDF fallback), decided after resolution of licensing constraints:

The PyMuPDF fallback was eliminated on 2026-08-31 because its AGPL-3.0 license creates a viral licensing obligation that would prevent distribution or commercial use of zettel_app beyond personal/local deployments. While PyMuPDF's page-to-heading map was useful for paging inference on failed Docling runs, the licensing risk outweighs the robustness benefit.

Docling is now mandatory; if Docling extraction fails, harvest fails explicitly rather than silently degrading to plain text. This choice trades robustness for distribution flexibility, accepting that Docling availability and quality become critical paths rather than having a fallback. The assumption is that Docling (backed by Hugging Face infrastructure) is sufficiently stable for most deployments, and a hard failure is preferable to an unmitigated AGPL exposure.

## Pros and Cons of the Options (Updated)

### Docling as sole extractor (chosen 2026-08-31)

* Good, because it removes PyMuPDF's AGPL-3.0 viral licensing, enabling distribution and commercial use
* Good, because Docling's structured Markdown output with heading hierarchy feeds directly into structural chunking and page inference
* Good, because image/multimodal asset extraction is available
* Bad, because a Docling failure now causes harvest to fail completely, with no fallback
* Bad, because it pulls in a heavy dependency footprint (torch, torchvision, pinned CUDA wheels) and GPU memory overhead
* Bad, because paging inference on a failed Docling extraction loses the PyMuPDF page-to-heading map signal

### Docling primary with PyMuPDF fallback (legacy)

* Good, because the automatic fallback keeps harvest operational when Docling fails
* Bad, because the fallback path carries PyMuPDF's AGPL-3.0 licensing terms, creating a blocking constraint for distribution

### PyMuPDF as sole extractor

* Good, because it is lightweight, fast, and requires no GPU
* Bad, because plain-text-only output loses the layout and heading information structural chunking depends on

### LLM-based extraction

* Good, because a multimodal model could offer semantic and layout understanding without a local ML stack
* Bad, because it introduces per-document API cost and latency across the entire inbox
* Bad, because it adds an external service availability dependency to a core ingestion step
* Bad, because output would be less reproducible across re-harvests than a deterministic local extractor

## Consequences (Updated 2026-08-31)

**Docling Version Pinning (Resolved)**: Docling is now pinned to a specific version in `pyproject.toml` / `uv.lock` to protect extraction reproducibility. An upgrade to Docling can change extraction output (new layout models, different Markdown formatting), which would shift extraction checksums and chunk boundaries for previously harvested sources. Version changes are deliberate, not silent, and old sources should be re-harvested if Docling output is suspected to have changed.

**GPU Acceleration Handling (Resolved)**: GPU acceleration remains soft/optional — on a machine without a usable GPU, Docling runs on CPU without an explicit warning. This is accepted as an operational trade-off: the performance difference is acceptable, and a startup GPU check would add complexity for minimal benefit on single-VM personal deployments.

**AGPL Licensing (Resolved)**: The PyMuPDF fallback has been removed entirely, eliminating the AGPL-3.0 licensing risk. zettel_app is now free to be distributed commercially or used in closed-source contexts without licensing conflicts. The cost is that Docling is now a mandatory, hard dependency — harvest fails if Docling is unavailable rather than degrading to plain text.

## References

* `zettel/harvester/extract.py` — `extract_text`, extraction dispatcher routing by file type; `extract_pdf` / `extract_pdf_docling`, the Docling path (GPU acceleration, image extraction, Markdown output); `PdfExtractionError`, the hard failure that replaced the fallback
* `zettel/harvester/extract.py` — `docling_page_map_by_export` and `docling_num_pages`, the Docling page map used for content-start paging inference (see ADR-013)
* `zettel/config.py` — `ImagesConfig` (image-extraction settings) and `device`; there is no extractor selector any more, since Docling is the only path
* `pyproject.toml` / `uv.lock` — the Docling version pin discussed in *Consequences*
