# Potential ADR: Bibliography ABNT Citation Formatting with Optional LLM-Merge

**Module**: QA-WRITING
**Category**: Data Architecture / Citation Management
**Priority**: Consider (Score: 62)
**Date Identified**: 2026-08-30

---

## What Was Identified

The `zettel article` command integrates **ABNT-formatted bibliographic citations** through a dedicated `bibliography.py` module (836 lines) that extracts source metadata, formats both in-text and end-of-article citations, and includes an **optional LLM-based merge path** when multiple sources need consolidation.

### The Bibliography Pattern

1. **Source Extraction** — Harvest phase extracts author, year, title, document type from PDF metadata (via Docling) or Markdown frontmatter
2. **Catalog Assembly** — During article generation, sources are collected into an `ArticleCatalog.sources` dictionary (keyed by `source_id`)
3. **Citation Formatting** — Two formats:
   - **In-text cites**: `(Author, Year)` via `format_abnt_in_text()` (e.g., "(Silva, 2023)")
   - **End-of-article references**: Full ABNT bibliography via `format_abnt_reference()` (includes publisher, DOI, etc.)
4. **Optional LLM Merge** — When articles cite multiple editions of the same work or require intelligent consolidation, an LLM can merge reference lists (introduced via commit 5910df1, 2026-07-30)

This pattern was introduced in commit 5910df1 (2026-07-30, "Add bibliographic metadata extraction and enhance harvest process") and refined in commit 64c5346 (2026-08-04, "Add article generation capabilities").

### ABNT Standard Integration

ABNT (Associação Brasileira de Normas Técnicas) is the Brazilian standards body. The citation format aligns with ABNT NBR 6023:2018:

```
Author, A. A. (Year). Title of work. Publisher.
```

Example from the vault:
```
Silva, J. P. (2023). Zettelkasten e gestão de conhecimento. Editora Conhecimento.
```

---

## Why This Might Deserve an ADR

- **Domain-Specific Infrastructure**: ABNT is a non-trivial citation standard (distinct from APA, Chicago, MLA). The implementation handles edge cases (multiple authors, corporate authors, DOIs, URLs)
- **Untested LLM Path**: The mapping.md explicitly notes "includes an LLM-merge path every test fixture disables" — an unverified code path exists but is not covered by tests
- **Cost to Change**: Switching citation standards (e.g., to APA or Chicago) would require rewriting format functions, template adjustments, and test fixtures
- **Team Knowledge**: Anyone generating academic articles needs to understand the citation workflow and potential LLM merge behavior
- **Temporal Stability**: 30 days in production (introduced 2026-07-30); stable since, with no regressions
- **Scope**: Affects not only article generation but also future document export features

---

## Evidence Found in Codebase

### Key Files
- `zettel/bibliography.py` (836 lines) — source extraction, formatting, optional LLM merge
- `zettel/article.py` (lines 53-96, 287-310) — `CatalogSource`, `CatalogAsset`, citation assembly
- `tests/test_bibliography.py` — formatting tests; LLM merge not tested

### Code Evidence: Citation Formatting

From `bibliography.py:200-240` (in-text cite generation):
```python
def format_abnt_in_text(authors: list[str], year: Optional[int]) -> str:
    """Format in-text citation per ABNT NBR 6023:2018.
    
    Examples:
    - Single author: (Silva, 2023)
    - Two authors: (Silva & Santos, 2023)
    - Three+ authors: (Silva et al., 2023)
    """
    if not authors or not year:
        return ""
    
    if len(authors) == 1:
        return f"({authors[0].split()[-1]}, {year})"
    elif len(authors) == 2:
        return f"({authors[0].split()[-1]} & {authors[1].split()[-1]}, {year})"
    else:
        return f"({authors[0].split()[-1]} et al., {year})"
```

From `bibliography.py:300-350` (full reference formatting):
```python
def format_abnt_reference(source: CatalogSource) -> str:
    """Build a complete ABNT NBR 6023:2018 bibliography entry.
    
    Handles:
    - Author order normalization (last name, initials)
    - Document type labels
    - Optional DOI / URL
    - Publisher and year
    """
    parts = []
    
    # Authors
    author_line = _format_author_list(source.authors)  # "Silva, J. P.; Santos, M. F."
    parts.append(author_line)
    
    # Year
    if source.year:
        parts.append(f"({source.year})")
    
    # Title (italicized in rendered form)
    parts.append(f"*{source.title}*")
    
    # Publisher
    if source.publisher:
        parts.append(source.publisher)
    
    # DOI (if available)
    if source.doi:
        parts.append(f"https://doi.org/{source.doi}")
    
    return ". ".join(parts) + "."
```

### Optional LLM-Merge Path (Untested)

From `bibliography.py:420-480` (merge logic):
```python
def merge_sources_with_llm(
    cfg: AppConfig,
    db: StateDB,
    sources_to_merge: list[CatalogSource],
    llm: BaseChatModel,
) -> CatalogSource:
    """Use LLM to intelligently merge multiple editions or variants of the same work.
    
    Example: Article cites both the 1st edition (2020) and 2nd edition (2023) of a book.
    The LLM decides whether to:
    1. Keep both separate
    2. Cite only the newest
    3. Merge into a single consolidated reference
    
    This path is NOT tested in fixtures (see test_bibliography.py:skip_llm_merge).
    """
    prompt = load_prompt_parts(cfg.prompts_path / "bibliography_merge.md")
    # ... LLM call logic
    return merged_source
```

#### Impact Analysis
- **Introduced**: 2026-07-30 19:02:20 (commit 5910df1)
- **Enhanced**: 2026-08-04 16:31:38 (commit 64c5346 — article integration)
- **Test coverage**: 
  - `format_abnt_in_text()` — 6 test cases (single author, multiple, corporate)
  - `format_abnt_reference()` — 5 test cases (complete reference, missing fields)
  - `merge_sources_with_llm()` — **ZERO test cases** (every test fixture skips via `skip_llm_merge=True`)
- **Commit themes**: "bibliographic metadata extraction", "enhance harvest process"
- **Files affected**: 3 (bibliography.py, article.py, harvester.py for metadata extraction)

### Consumers
- **article.py** (lines 287-310) — builds `CatalogSource` from harvested metadata, uses format functions during assembly
- **harvester.py** — extracts author, title, year, DOI from Docling/PyMuPDF output
- **tests/test_article.py** — verifies citation injection into assembled markdown
- **CLI** — `zettel article ... --style academic` triggers ABNT formatting in output

---

## Questions to Address in ADR (if created)

1. **Why ABNT specifically?** (Language/region choice — could generalize to APA, Chicago, etc.)
2. **What are the untested LLM-merge scenarios?** (Which edge cases warrant merge logic)
3. **How stable is ABNT NBR 6023 itself?** (Last revision 2018; updates may require code changes)
4. **Should citation formatting be pluggable?** (Config-driven format selection vs. hardcoded ABNT)
5. **How does the LLM-merge interact with cache keys?** (Each merge call has a unique checksum)

---

## Related Potential ADRs

- **QA-WRITING/langgraph-statgraph-article-orchestration** — Article generation calls bibliography functions
- **INFRA/layered-hashing-strategy** — Citation formatting affects LLM call caching
- **HARVEST/docling-pdf-extraction-with-pymupdf-fallback** — Metadata source for bibliography

---

## Additional Notes

- **Docstring Gap**: The `merge_sources_with_llm()` function includes a "not tested" comment but no explicit skip decorator in tests
- **ABNT Variant**: The implementation follows NBR 6023:2018, but does not cover all edge cases (e.g., government documents, legal references)
- **Internationalization**: Citation format is hardcoded to ABNT; no localization for other standards (potential future ADR if multi-format support is planned)
- **Config Integration**: Citation format is NOT configurable via `config.yaml` — hardcoded to ABNT; could be a consideration for Phase 3+ enhancements
