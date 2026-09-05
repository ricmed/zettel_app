"""The Harvester — file detection, text extraction, chunking, SRC/LIT creation.

Supports: PDF (Docling, mandatory), Markdown.
"""

from .biblio_hitl import resolve_bibliography
from .chunking import (
    chunk_and_persist,
    iter_fenced_spans,
    merge_small_sections,
    split_chapter_into_chunks,
    split_chapter_into_sections,
    split_into_chapters,
)
from .citekey import generate_citekey
from .duplicates import (
    HarvestAborted,
    find_semantic_duplicate_candidates,
    resolve_duplicate_decision,
    sample_chunk_texts,
)
from .extract import (
    EmptyTextLayerError,
    PdfExtractionError,
    assert_pdf_has_text_layer,
    docling_num_pages,
    docling_page_map_by_export,
    extract_markdown,
    extract_pdf,
    extract_pdf_docling,
    extract_text,
    extract_year_from_pdf_date,
    extract_year_from_string,
    page_map_for_source,
)
from .pipeline import (
    HarvestOutcome,
    HarvestSkip,
    list_incomplete_sources,
    run_harvest,
    run_rechunk,
    source_chunking_incomplete,
)
from .set_paging import run_set_paging

__all__ = [
    "EmptyTextLayerError",
    # Exceptions
    "HarvestAborted",
    # Result objects
    "HarvestOutcome",
    "HarvestSkip",
    "PdfExtractionError",
    # Extraction (used by tests, pipeline)
    "assert_pdf_has_text_layer",
    "chunk_and_persist",
    "docling_num_pages",
    "docling_page_map_by_export",
    "extract_markdown",
    "extract_pdf",
    "extract_pdf_docling",
    "extract_text",
    "extract_year_from_pdf_date",
    "extract_year_from_string",
    # Duplicates (used by pipeline, duplicates checks)
    "find_semantic_duplicate_candidates",
    # Citekey
    "generate_citekey",
    "iter_fenced_spans",
    "list_incomplete_sources",
    "merge_small_sections",
    "page_map_for_source",
    # Bibliography
    "resolve_bibliography",
    "resolve_duplicate_decision",
    # Public API (pipeline)
    "run_harvest",
    "run_rechunk",
    "run_set_paging",
    "sample_chunk_texts",
    "source_chunking_incomplete",
    "split_chapter_into_chunks",
    "split_chapter_into_sections",
    # Chunking (used by tests, duplicates, other modules)
    "split_into_chapters",
]

SUPPORTED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}
