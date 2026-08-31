# Potential ADR: Granular Literature Notes with Readable Names and Source Excerpts

**Module**: EXTRACT  
**Category**: Data Architecture / Vault Structure  
**Priority**: Must Document (Score: 115)  
**Date Identified**: 2026-08-30  

---

## Existing ADR Context

No existing ADRs found with significant similarity. This is a structural design decision affecting vault organization and note naming conventions across the EXTRACT, REVIEW, and CONNECT phases.

---

## What Was Identified

The EXTRACT module implements a **granular, chunk-per-literature-note design** with **readable, human-friendly filenames** and **managed blocks for source excerpts**. This was a deliberate architectural shift from an earlier monolithic LIT-per-source model.

### Design Overview

**Vault Structure**:
- Each chunk processed through EXTRACT produces one draft literature note
- Draft path: `00_Inbox/Review/{Citekey}/LIT - AuthorYear - pNNN - topic-NNNN.md`
- Approved path: `20_Literature/{Citekey}/LIT - AuthorYear - pNNN - topic-NNNN.md`
- No `@` symbol in vault paths (citekey is the folder identifier, not file content)

**Filename Convention** (`literature_chunk_filename()`):
- `LIT - {AuthorYear} - p{page_in_book|NULL} - {topic_slug}-{short_hash}.md`
- Topic slug extracted from the literature note's summary (first 5 words, slugified)
- Short hash (`short_hash()`) ensures uniqueness within a source
- Example: `LIT - Smith2023 - p47 - machine-learning-foundations-a3k2.md`

**Managed Blocks for Source Preservation**:
- Each approved literature note includes `zettel:auto-source-excerpt` managed block
- Contains the full chunk text from the original source
- Allows HITL review of LLM extraction against original passage without embedding raw text
- Managed block lifecycle: inserted during approval (`review.approve_chunk()`), updated on note changes

**Per-Chunk Processing**:
- One literature note per chunk (not per source)
- Each note has its own `literature_id` (ULID), `review_confidence` score, processing metadata
- Structured output (`LiteratureChunkOutput`) with summary, key_concepts, candidates (PermanentNoteCandidates)
- Lazy embedding: literature notes are indexed to Chroma `literature_notes` collection only **after** approval (in REVIEW phase), not during EXTRACT

### Contrast with Earlier Model

The **legacy monolithic model** (now removed):
- One literature index per source: `LIT - AuthorYear.md`
- All chunks of a source merged into a single note
- Harder to track per-chunk confidence and status
- Less granular HITL review (all-or-nothing per source)

**Breaking change introduced in commit 508d4c0** (2026-08-28):
> "Replace generic @Citekey/chunk_NNNN paths with AuthorYear + page + topic filenames, keep @ out of vault folders, and attach the full chunk text in a managed block so review can judge the LLM against the original passage without embedding the raw source."

Sources harvested before this change require a re-run of `extract` + `review` to migrate to granular notes.

## Why This Might Deserve an ADR

- **Impact**: Architectural foundation of the literature-note subsystem; affects:
  - Vault organization (folder structure under `20_Literature/`)
  - Note naming and uniqueness guarantees
  - HITL review workflow (per-chunk approval)
  - Graph structure (literature notes → permanent notes linkage)
  - Indexing strategy (when/how literature notes are embedded)

- **Trade-offs**:
  - **Pro**: Fine-grained confidence tracking per chunk; allows selective approval; human-readable filenames
  - **Pro**: Source excerpts in managed blocks enable side-by-side review (LLM output vs. original)
  - **Pro**: Easier to track processing metadata (review_confidence, llm_model, processing_time_ms)
  - **Con**: More files in the vault (one per chunk, not one per source)
  - **Con**: Breaking change for existing sources (migration required)
  - **Con**: More complex filename generation (topic extraction from summary, hash uniqueness)

- **Complexity**: 
  - Filename generation (`literature_chunk_filename()`) is non-trivial: extracts topic slug from LLM summary, computes hash for uniqueness
  - Vault builders (`build_literature_chunk_note()`) encode multiple concerns (metadata, managed blocks, page locators)
  - Naming collision handling requires short hash strategy (4-char hash from file-level short_hash)

- **Team Knowledge**: Essential for:
  - Understanding why `20_Literature/` has `{Citekey}/` subdirectories with multiple granular notes
  - Knowing how to manually move/rename literature notes (impacts `literature_note_path` in StateDB)
  - Understanding why EXTRACT doesn't index to Chroma immediately (deferred to REVIEW)
  - Migrating legacy sources from monolithic to granular model
  - Troubleshooting file-path mismatches when notes are manually edited

- **Long-term Implications**:
  - Topic-slug extraction depends on LLM-generated summary quality; poor summaries → poor slugs
  - Short hash collision probability scales with source size (more chunks = more hashes)
  - Filename length constraints (OS filesystem limits) on very long topic slugs
  - Manual vault edits (moving/renaming notes) require StateDB sync (`zettel sync-manual`)

## Evidence Found in Codebase

### Key Files

- [`zettel/extractor.py:296-404`](../../../zettel/extractor.py) — Draft literature note creation
  - Lines 296-300: ULID generation + draft path creation
  - Lines 370-404: `_write_literature_draft()` builds note via vault builder

- [`zettel/vault.py`](../../../zettel/vault.py) — Vault I/O and naming
  - `build_literature_chunk_note()` — Structured note builder with managed blocks
  - `literature_chunk_filename()` — Filename generation with topic slug + hash
  - `literature_source_dirname()` — Folder structure per source
  - `safe_write_note()` — Safe atomic file writing (never overwrites manual edits outside managed blocks)

- [`zettel/review.py:387-481`](../../../zettel/review.py) — Approval workflow
  - `approve_chunk()` — Moves draft from `00_Inbox/Review/` to `20_Literature/` + embeds to Chroma
  - Lines 451: `safe_update_managed_blocks()` inserts `auto-source-excerpt` on approval
  - Lines 454-467: Chroma upsert (`idx.upsert_literature_note()`) post-approval

- [`zettel/schemas.py`](../../../zettel/schemas.py) — Structured output
  - `LiteratureChunkOutput` — Per-chunk extraction result (summary, key_concepts, candidates)
  - `PermanentNoteCandidate` — Each candidate has source_locator (page reference)

### Code Evidence

```python
# Filename generation (vault.py)
def literature_chunk_filename(
    citekey: str,
    chunk_index: int = 0,
    page_in_book: int | None = None,
    page_in_file: int | None = None,
    section_path: str = "",
    summary: str = "",
) -> Path:
    """Generate readable filename: LIT - AuthorYear - pNNN - topic-HASH.md"""
    page_suffix = ""
    if page_in_book is not None:
        page_suffix = f"p{page_in_book}"
    elif page_in_file is not None:
        page_suffix = f"pf{page_in_file}"
    
    # Topic slug from summary (first ~5 words)
    topic_slug = slugify(summary.split()[:5])  # e.g., "machine-learning-foundations"
    
    # Hash for uniqueness within source
    content_hash = short_hash(f"{citekey}|{chunk_index}|{page_suffix}|{topic_slug}")
    
    filename_parts = [f"LIT - {citekey}"]
    if page_suffix:
        filename_parts.append(f"- {page_suffix}")
    filename_parts.append(f"- {topic_slug}-{content_hash}.md")
    
    return Path(" ".join(filename_parts))

# Draft creation in EXTRACT (extractor.py:296-404)
literature_id = str(ULID())
draft_path = _write_literature_draft(
    cfg, db, chunk_row, output, literature_id, confidence, elapsed_ms,
    candidates=approved_cands,
)

# Approval workflow in REVIEW (review.py:387-481)
def approve_chunk(...) -> bool:
    # Move draft to 20_Literature/{Citekey}/
    dest_path = (
        cfg.vault_path / "20_Literature" / literature_source_dirname(citekey)
    ) / literature_chunk_filename_for_row(citekey, chunk)
    
    safe_write_note(dest_path, meta, body)
    
    # Insert source excerpt managed block
    safe_update_managed_blocks(dest_path, {"auto-source-excerpt": excerpt})
    
    # Embed to Chroma literature_notes collection (post-approval)
    idx.upsert_literature_note(lit_id, embed_text, metadata)
```

### Impact Analysis

- **Introduced**: Commit 508d4c0 (2026-08-28 20:01:06)
  - Replaced generic chunk paths with readable names
  - Added managed-block source excerpts
  - Modified 13 files (vault.py +219 lines, extractor.py +18, review.py +43, and 10 others)
  
- **Modified**: Stable since introduction (2 days old); part of 0.5.0 release
  - No regressions reported
  - Breaking change for existing sources (documented in CLAUDE.md)

- **Themes**: "vault structure", "readability", "metadata preservation", "HITL review"

- **Affects**: 
  - Entire EXTRACT phase (100% of literature note creation)
  - REVIEW phase (approval routing, managed block insertion)
  - CONNECT phase (permanent note generation consults approved literature notes)
  - Vault sync and manual editing workflows

### Alternatives (Observed or Documented)

1. **Monolithic LIT-per-source** (legacy, removed)
   - One index note per source, all chunks merged
   - Simpler file structure, fewer vault files
   - **Rejected**: Lost per-chunk confidence tracking; harder HITL review

2. **Generic chunk-based filenames** (e.g., `chunk_001.md`)
   - Smaller filenames, easier collision avoidance
   - Less human-readable
   - **Rejected**: Poor user experience; hard to understand note content from filename

3. **Topic extraction via heuristics instead of LLM summary**
   - Faster filename generation, no LLM dependency
   - Lower quality slugs (no semantic understanding)
   - **Not chosen**: LLM summary is already generated; reusing it is efficient

4. **Embed literature notes during EXTRACT, not REVIEW**
   - Faster indexing, earlier availability
   - **Rejected**: Unapproved drafts would pollute embeddings; separation of concerns (extract vs. review)

## Questions to Address in ADR (if created)

- How are topic slugs extracted from summaries? (First ~5 words, slugified via `slugify()`)
- What happens if two chunks generate the same topic slug + hash? (Hash collision handled by `short_hash()` with sufficient entropy)
- Can a user manually rename a literature note? (Yes, but requires `zettel sync-manual` to update StateDB)
- Why are literature notes embedded only after approval? (Design choice: don't index drafts; separation of concerns)
- How is the breaking change managed for legacy sources? (CLAUDE.md documents re-run requirement; no automatic migration)

## Related Potential ADRs

- **REVIEW/post-approval-semantic-deduplication** — Deduplication runs on approved literature notes before CONNECT
- **INFRA/vault-managed-blocks-safe-io** — Architecture for preserving user edits outside managed blocks (if ADR created)

## Additional Notes

- **Temporal context**: Recently introduced (2026-08-28); represents significant vault-structure shift
- **Configuration exposure**: Vault paths are hardcoded (00_Inbox/Review, 20_Literature); not configurable via config.yaml
- **Testing**: Test updates in `test_vault.py` (+127 lines), `test_review.py` (+42 lines), `test_set_paging.py` (+25 lines)
- **Known limitation**: Topic slug quality depends on LLM summary quality; very short or generic summaries produce poor filenames
- **Observability**: Filename generation is logged at INFO level; StateDB tracks `literature_note_path` for all chunks

