# ADR-027: Harvest Phase as Python Package

**Status**: Accepted (2026-08-31)  
**Depends on**: ADR-011, ADR-012, ADR-013, ADR-014  
**Relates to**: Issue #48

## Context

The harvest module has grown into a monolithic file exceeding 1,776 lines, violating the single-responsibility principle and making it difficult for agents and humans to navigate:

- Text extraction (PDF + Markdown) co-located with chunking logic
- Bibliographic metadata HITL mixed with semantic dedup
- Multiple levels of page inference intertwined with file processing
- Citekey generation buried in a large function

The file's size creates cognitive load, hinders testing of individual concerns, and makes it impossible for LLM agents to fetch focused context without loading the entire 1776-line file.

## Decision

Extract `zettel/harvester.py` into a Python package `zettel/harvester/` with 8 focused modules:

```
zettel/harvester/
├── __init__.py              # Public API re-exports
├── extract.py               # PDF/MD extraction + year parsing (~410 lines)
├── chunking.py              # H1–H6 splitting, persistence (~420 lines)
├── duplicates.py            # 3-layer dedup + HarvestAborted (~130 lines)
├── biblio_hitl.py           # Rich HITL for metadata (~210 lines)
├── citekey.py               # Citekey generation (~46 lines)
├── pipeline.py              # Orchestration: run_harvest, _process_file (~530 lines)
└── set_paging.py            # Paging repair without re-extraction (~190 lines)
```

### Public API (from `__init__.py`)

Maintained for backward compatibility:

```python
from zettel.harvester import (
    run_harvest,
    run_rechunk,
    run_set_paging,
    list_incomplete_sources,
    source_chunking_incomplete,
    HarvestAborted,
)
```

### Submodule Responsibilities

| Module | Exports | Key Dependencies |
|--------|---------|------------------|
| **extract.py** | `extract_text`, `extract_pdf`, `extract_markdown`, `extract_year_from_*`, helpers | config, paging, assets |
| **chunking.py** | `chunk_and_persist`, `split_into_chapters`, `split_chapter_into_sections`, helpers | config, state, index, paging |
| **duplicates.py** | `find_semantic_duplicate_candidates`, `resolve_duplicate_decision`, `HarvestAborted` | config, state, index, chunking |
| **biblio_hitl.py** | `resolve_bibliography` | config, bibliography, Rich |
| **citekey.py** | `generate_citekey` | state |
| **pipeline.py** | `run_harvest`, `run_rechunk`, orchestration helpers | all submodules + external |
| **set_paging.py** | `run_set_paging` | config, state, index, paging, pipeline |

### Import Rules

**Submodules must:**
- Import siblings via relative imports: `from . import chunking`
- Import external modules via absolute imports: `from zettel.config import AppConfig`
- Never import back from pipeline.py (no circular deps)
- Never expose private helpers in `__init__.py` (only public API)

**External code (sync.py, cli.py, tests):**
- Import from `__init__.py` when possible (stability)
- Or import directly from submodules for testing isolated functionality
- Example: `from zettel.harvester.citekey import generate_citekey`

## Consequences

### Advantages

✓ **Progressive disclosure**: Agents can fetch ~400-line modules instead of 1776-line monolith  
✓ **Clear concerns**: Each module has a single, discoverable responsibility  
✓ **Testability**: Unit tests can target individual modules without loading unrelated code  
✓ **Maintainability**: Citekey logic (`sync.py` uses it) is now in a dedicated module  
✓ **Readability**: No file exceeds ~530 lines; easier for humans to understand  
✓ **Extensibility**: New extraction methods, dedup strategies can be added to their modules  

### Trade-offs

⚠ **Test migration**: Existing tests importing private functions must update paths  
⚠ **Circular import risk**: Low but present—mitigated by relative imports and natural DAG structure  
⚠ **Breaking change for private consumers**: Code importing `_generate_citekey` must use `generate_citekey`  

### No Architectural Changes

The behavior and public API remain identical:
- `from zettel.harvester import run_harvest` still works
- Pipeline orchestration unchanged (same file processing order)
- Chunking strategy identical
- Dedup detection unchanged

### File Deletion Order (Critical on Windows)

```bash
# Step 1: Commit new package files first
git add zettel/harvester/__init__.py zettel/harvester/*.py
git commit -m "Extract harvester package modules"

# Step 2: Update imports across codebase
git add zettel/sync.py tests/*.py
git commit -m "Update imports for harvester package"

# Step 3: Only then delete old monolithic file
git rm zettel/harvester.py
git commit -m "Remove monolithic harvester.py (migrated to package)"
```

Reason: Windows file locks; git tracking clarity.

## Alternatives Considered

1. **Keep monolithic, add comments** (~1776 lines)  
   - Rejected: Doesn't solve cognitive load or agent context limits

2. **Merge harvest concerns into existing modules** (extractor, gardener, bibliography)  
   - Rejected: No clear fit; would create larger mixed-concern modules

3. **Extract as separate `harvest_*.py` files** (e.g., harvest_extract.py, harvest_chunking.py)  
   - Rejected: Siblings in flat namespace don't scale; import hell for circular deps

4. **Package structure with subpackages** (harvest.extract, harvest.chunking)  
   - Rejected: Over-engineered; flat package is sufficient for 8 modules

## Acceptance Criteria

- [ ] `zettel/harvester.py` deleted; `zettel/harvester/` package exists with 8 modules
- [ ] No file exceeds ~550 lines (pipeline.py is the ceiling)
- [ ] Public API maintained: `from zettel.harvester import run_harvest, ...` works
- [ ] CLI/web signatures unchanged (run_harvest, run_rechunk, run_set_paging)
- [ ] Tests updated to new import paths; all harvest tests pass
- [ ] ADR-027 documented; ADR-011–014 updated with submodule references
- [ ] CLAUDE.md Phase 1 section describes the package and modules
- [ ] No circular imports; DAG validated

## Related ADRs

- **ADR-011** (3-layer dedup): Now scoped to `duplicates.py`
- **ADR-012** (Docling/PyMuPDF): Logic in `extract.py`
- **ADR-013** (page inference): Logic in `chunking.py` and `paging.py`
- **ADR-014** (structural H1-H6 split): Core logic in `chunking.py`

## Timeline

- Phase 1 (chunking.py): ~2 hours
- Phase 2 (set_paging.py): ~1 hour
- Phase 3 (pipeline.py): ~3 hours
- Phase 4 (init + imports + tests): ~2 hours
- **Total: ~8 hours**

---

**Decided by**: Ricardo Medeiros  
**Date**: 2026-08-31  
**Implementation**: Issue #48
