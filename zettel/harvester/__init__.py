"""The Harvester — file detection, text extraction, chunking, SRC/LIT creation.

Supports: PDF (Docling / PyMuPDF), Markdown.
"""

from .biblio_hitl import resolve_bibliography
from .chunking import (
    chunk_and_persist,
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
    docling_num_pages,
    docling_page_map_by_export,
    enrich_metadata_from_pymupdf,
    extract_markdown,
    extract_pdf,
    extract_pdf_docling,
    extract_pdf_pymupdf,
    extract_text,
    extract_year_from_pdf_date,
    extract_year_from_string,
    page_map_for_source,
    pymupdf_page_map,
    scan_printed_page_numbers,
)
from .pipeline import (
    list_incomplete_sources,
    run_harvest,
    run_rechunk,
    source_chunking_incomplete,
)
from .set_paging import run_set_paging

__all__ = [
    # Exceptions
    "HarvestAborted",
    # Public API (pipeline)
    "run_harvest",
    "run_rechunk",
    "run_set_paging",
    "list_incomplete_sources",
    "source_chunking_incomplete",
    # Chunking (used by tests, duplicates, other modules)
    "split_into_chapters",
    "split_chapter_into_sections",
    "merge_small_sections",
    "chunk_and_persist",
    "split_chapter_into_chunks",
    # Extraction (used by tests, pipeline)
    "extract_text",
    "extract_pdf",
    "extract_markdown",
    "extract_pdf_docling",
    "extract_pdf_pymupdf",
    "extract_year_from_pdf_date",
    "extract_year_from_string",
    "pymupdf_page_map",
    "docling_num_pages",
    "docling_page_map_by_export",
    "enrich_metadata_from_pymupdf",
    "page_map_for_source",
    "scan_printed_page_numbers",
    # Bibliography
    "resolve_bibliography",
    # Citekey
    "generate_citekey",
    # Duplicates (used by pipeline, duplicates checks)
    "find_semantic_duplicate_candidates",
    "resolve_duplicate_decision",
    "sample_chunk_texts",
]

SUPPORTED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}
