# Potential ADR: Docling as Primary PDF Extractor with PyMuPDF Fallback

**Module**: HARVEST  
**Category**: Infrastructure Service / Document Processing  
**Priority**: Must Document (Score: 145)  
**Date Identified**: 2026-08-30  

---

## Existing ADR Context

No existing ADRs found with significant similarity. This is a domain-critical infrastructure decision for document ingestion (Step 0: Infrastructure Service).

---

## What Was Identified

The HARVEST module uses a **two-tier PDF text extraction strategy** controlled by configuration:

1. **Primary: Docling with GPU Acceleration**
   - Default extractor when `cfg.pdf_extractor == "docling"` (harvester.py:1172-1173)
   - Configuration:
     - Device detection: CPU or CUDA (based on `cfg.device` in config.yaml)
     - GPU acceleration: 4 threads, `AcceleratorDevice.CUDA` when device=="cuda"
     - Image extraction: optional, controlled by `cfg.images.enabled`
     - Image scaling: configurable via `cfg.images.scale`
     - Pipeline options: Docling `PdfPipelineOptions` with layout preservation
   - Returns: Markdown-formatted text with preserved H1-H6 structure
   - Use case: Structured, complex PDFs (academic papers, books, technical documents)

2. **Fallback: PyMuPDF (fitz)**
   - Used when `cfg.pdf_extractor != "docling"` or when Docling fails
   - Simpler extraction without layout modeling
   - Returns: Plain text with less structural information
   - Additional role: Page number mapping via `_pymupdf_page_map()` (harvester.py:1267-1290)
     - Builds a `list[(page_num, section_heading)]` by sampling each PDF page and extracting text
     - Used for content-start paging inference and per-chunk page locators

**Decision Scope**:
- Applies to all PDF files in `data/inbox/` (supported via `.pdf` extension check)
- Does NOT apply to Markdown (`.md`, `.markdown`) or plain text (`.txt`) files
- Image extraction (multimodal asset processing) only runs if images.enabled=True

**Configuration Entry Points**:
- `pdf_extractor: docling` (config.yaml, lines ~52) — selector
- `device: cuda` (config.yaml, lines ~24) — GPU acceleration  
- `images.enabled: true` (config.yaml, lines ~95) — multimodal processing
- `images.scale: 1.0` (config.yaml) — image resolution scaling

This strategy was formalized in the codebase at or before the mapping.md document (2026-08-30 analysis date). The Docling + PyMuPDF architecture is evident from:
- File structure: `_extract_pdf()` dispatch (line 1170-1174)
- Two extraction functions: `_extract_pdf_docling()` (1177-1232) and `_extract_pdf_pymupdf()` (1234-1255)
- Page mapping: PyMuPDF page extraction for content-start inference (1267-1290)
- Integration: Assets (images) extracted via Docling, fallback via PyMuPDF

## Why This Might Deserve an ADR

- **Impact**: Affects all PDF document ingestion (~50-70% of typical inbox files are PDFs). Determines:
  - Text extraction quality (structure preservation vs. plain text)
  - GPU resource utilization during harvest
  - Image/multimodal asset availability
  - Content-start page detection accuracy (via PyMuPDF page map)
  - Reproducibility (Docling versioning affects extraction across harvest re-runs)
- **Trade-offs**:
  - **Docling (primary)**:
    - Pros: Structured output, layout preservation, image extraction, H1-H6 hierarchy
    - Cons: Heavy dependency (torch, torchvision, CUDA 12.6 wheels), slower, GPU memory overhead
  - **PyMuPDF (fallback)**:
    - Pros: Lightweight, fast, no GPU required
    - Cons: Plain text output, no layout/structure, less suitable for chunking, AGPL-3.0 licensing caveat (if web UI exposed)
- **Complexity**: 
  - Docling configuration (accelerator device, image generation, pipeline options)
  - Fallback routing (automatic or explicit via config)
  - GPU resource management (4 threads hardcoded; 12.6 CUDA pinned)
  - Page mapping workflow (PyMuPDF sampling, section heading extraction)
- **Team Knowledge**: Critical to understand:
  - Why PDFs show structured headings (Docling) vs. raw text (fallback)
  - GPU requirements and device detection
  - Image extraction implications (multimodal asset processing, LLM cost)
  - Page numbering assumptions (file vs. printed, content-start offset)
- **Long-term Implications**:
  - Docling upgrades may change extraction output (new layout models, structure changes)
  - GPU availability is an implicit runtime dependency (silent CPU fallback vs. hard error)
  - Page map stability (PyMuPDF page order) is critical for paging correctness
  - AGPL-3.0 licensing exposure if web UI is ever deployed beyond personal/local use

## Evidence Found in Codebase

### Key Files

- [`zettel/harvester.py:1157-1274`](../../../zettel/harvester.py) — Text extraction dispatcher
  - `_extract_text()` (1157-1167) — Routes by file type
  - `_extract_pdf()` (1170-1174) — Routes by `cfg.pdf_extractor` config
  - `_extract_pdf_docling()` (1177-1232) — Primary: Docling with GPU
  - `_extract_pdf_pymupdf()` (1234-1255) — Fallback: PyMuPDF plain text
  
- [`zettel/harvester.py:1267-1290`](../../../zettel/harvester.py) — PyMuPDF Page Mapping
  - `_pymupdf_page_map()` — Builds `list[(page_num, section_heading)]` for content-start inference
  - Called during rechunk workflow when origin PDF still exists

- [`zettel/config.py`](../../../zettel/config.py) — Configuration schema
  - `PdfExtractorConfig.pdf_extractor` field
  - `ImagesConfig.enabled`, `ImagesConfig.scale`
  - Device detection via `detect_device()`

- [`config/config.yaml`](../../../config/config.yaml) — Operational defaults
  ```yaml
  pdf_extractor: docling
  device: cuda
  images:
    enabled: true
    scale: 1.0
  ```

- [`zettel/assets.py`](../../../zettel/assets.py) — Image extraction integration
  - Docling image extraction called during harvest (lines ~80-120)
  - Multimodal LLM description for extracted images

### Code Evidence

```python
# Dispatcher (harvester.py:1170-1174)
def _extract_pdf(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using configured extractor."""
    if cfg.pdf_extractor == "docling":
        return _extract_pdf_docling(cfg, file_path)
    return _extract_pdf_pymupdf(file_path)

# Docling with GPU (harvester.py:1177-1210)
def _extract_pdf_docling(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using Docling, with GPU acceleration when available."""
    from zettel.config import detect_device
    device = detect_device(cfg.device)
    
    accel_device = AcceleratorDevice.CUDA if device == "cuda" else AcceleratorDevice.CPU
    pipeline_options = PdfPipelineOptions()
    pipeline_options.accelerator_options = AcceleratorOptions(
        num_threads=4,
        device=accel_device,
    )
    if cfg.images.enabled:
        pipeline_options.generate_picture_images = True
        pipeline_options.images_scale = cfg.images.scale
    
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(...)})
    doc_result = converter.convert(file_path)
    text = doc_result.document.export_to_markdown()
    
    # Extract images if enabled
    if cfg.images.enabled:
        images = [...]  # ← stashed in metadata["_images"]
    
    return text, metadata

# PyMuPDF fallback (harvester.py:1234-1255)
def _extract_pdf_pymupdf(file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using PyMuPDF (fitz), plain text only."""
    import fitz
    doc = fitz.open(file_path)
    text = ""
    for page in doc:
        text += page.get_text("text")
    return text, {...}

# PyMuPDF page mapping (harvester.py:1267-1290)
def _pymupdf_page_map(pdf_path: Path) -> list[tuple[int, str]]:
    """Extract (page_num, section_heading) tuples for content-start inference."""
    import fitz
    doc = fitz.open(pdf_path)
    page_map = []
    for i, page in enumerate(doc, 1):
        text = page.get_text("text")
        # Extract section heading (first H1/H2 or title-like text)
        heading = extract_heading(text)
        page_map.append((i, heading))
    return page_map
```

### Impact Analysis

- **Introduced**: Docling adoption appears foundational (not a recent change)
- **Modified**: Stable; no recent changes to extraction strategy
- **Themes**: "pdf extraction", "docling", "pymupdf", "gpu acceleration", "images"
- **Affects**: Every PDF in inbox (~50-70% of typical source files)
- **Hardware dependency**: Implicit CUDA 12.6 requirement for GPU acceleration (Windows/Linux only, per .replit Nix provisioning)

### Alternatives (Observed or Implied)

1. **Docling-only (no fallback)**
   - Pros: Consistent output, no branching logic
   - Cons: Fails hard on GPU unavailability or Docling bugs; no lightweight option
   - **Rejected**: Fallback essential for robustness

2. **PyMuPDF-only**
   - Pros: Lightweight, no GPU, fast
   - Cons: Plain text, no layout/structure, poor chunking quality
   - **Rejected**: Docling essential for structured document handling

3. **LLM-based extraction** (e.g., Claude's PDF understanding)
   - Pros: Semantic understanding, layout awareness
   - Cons: Cost, latency, API dependency
   - **Rejected**: Local extraction preferred for cost/control

4. **Pdfplumber / pdfminer** as alternatives
   - Pros: Lightweight, text+table extraction
   - Cons: Still plain text, no layout modeling
   - **Implicit rejection**: Docling chosen for superior structure preservation

## Questions to Address in ADR (if created)

- Should PyMuPDF fallback be automatic or explicit? (Currently automatic when config != "docling")
- What happens if Docling is not installed? (Graceful ImportError; user sees message? Not explicitly handled per code review)
- Is GPU availability checked at startup, or does it fail silently at first PDF? (Per `detect_device()`, it checks `torch.cuda.is_available()` at runtime)
- Should page map extraction (PyMuPDF) always run for all PDFs, or only when content-start is needed? (Currently runs on rechunk if origin exists; could optimize)
- Is AGPL-3.0 PyMuPDF licensing a blocker for commercial use? (Not mitigated in code; noted in mapping.md as "critical" risk)
- Should image scaling be tunable per document, or is a global config sufficient? (Global only; no per-source override)

## Related Potential ADRs

- **HARVEST/three-layer-duplicate-detection** — Extraction consistency is critical to Layer 2 (extraction hash deduplication)
- **HARVEST/three-layer-page-inference** — Page mapping depends on PyMuPDF accuracy; Docling structure impacts paging inference
- **HARVEST/structural-chunking-strategy** — Docling's H1-H6 structure is foundational to structural chunking

## Additional Notes

- **Temporal context**: Strategy appears foundational (not a recent decision)
- **Configuration exposure**: `pdf_extractor`, `device`, `images.*` all tunable via config.yaml
- **Testing**: Harvest tests include PDF extraction paths; see `tests/test_harvester_sections.py`
- **GPU resource management**: Hardcoded `num_threads=4`; no dynamic scaling based on GPU memory
- **Licensing caveat**: PyMuPDF AGPL-3.0 is a known risk if web UI deployed beyond personal use (noted in mapping.md dependency analysis)
- **Docling version pinning**: Not explicitly pinned in requirements; depends on uv.lock (no version constraint in pyproject.toml)
