# MANUAL-SYNC Module Analysis Report

**Date**: 2026-08-30  
**Analyzed Module**: MANUAL-SYNC (sync.py, new_note.py, purge_source.py, rebuild.py)  
**Analysis Phase**: Phase 2 - Potential ADR Identification  
**Result**: 0 ADRs created (5 decisions identified, all scored below 75-point threshold)

---

## Executive Summary

The MANUAL-SYNC module contains several important architectural decisions related to how hand-authored vault content integrates with the pipeline's state stores (SQLite + ChromaDB). However, most of these decisions are **tactical implementations of larger architectural patterns already documented in the INFRA module** (specifically the "Dual-Store Persistence with No Cross-Store Transactions" ADR).

Key finding: The SYNC module does not introduce novel architectural decisions at the system level; rather, it operationalizes the eventual-consistency model that the entire system is built around.

---

## Architectural Decisions Identified

### Decision 1: Cross-Store Consistency Pattern (Eventual Consistency Model)

**Location**: `sync.py` (lines 272, 345-396), `purge_source.py` (lines 282-291, 293-330), `rebuild.py` (lines 88-129)

**What Was Identified**:
- The SYNC module manages synchronization between three independent stores:
  - **Vault** (filesystem Obsidian notes)
  - **SQLite** (state.db - relational data + bodies + graph edges)
  - **ChromaDB** (vector embeddings + collections)
- No atomic transactions span across stores
- Updates to one store may succeed while another fails (eventual consistency)
- Recovery from store inconsistency relies on idempotent operations (hashing + skip detection)

**Code Evidence**:
```python
# sync.py:272 - Persists body edges without ChromaDB update
_extract_body_edges(db, note_id, body)  # Updates SQLite

# purge_source.py:282-291 - Separate delete calls, no transaction
idx.delete_chunks(chunk_ids)
idx.delete_literature_notes(lit_ids)  # May fail
idx.delete_sources([source_id])       # May fail
sqlite_removed = db.delete_source_cascade(source_id)  # May succeed even if Chroma failed
```

**Why This Decision Matters**:
- Affects data consistency guarantees across the system
- Impacts recovery procedures and idempotency requirements
- Already explicitly acknowledged in `CLAUDE.md` as a "known coupling risk"

**Git History Context**:
- **Primary commit**: `e8c1b8a` (2026-08-29) - "feat(cli): add new-note and delete-source with MOC backrefs"
- **Related**: `508d4c0` (2026-08-28) - Literature note structural changes
- No explicit "eventual consistency" commit; pattern is implicit in architecture

**Scoring Analysis**:

| Dimension | Score | Reasoning |
|-----------|-------|-----------|
| **Scope + Impact (0-25)** | 25 | Affects all 4 stores, all pipeline stages (harvest→extract→review→connect→garden) |
| **Cost to Change (0-25)** | 22 | Moving to transactional semantics would require major rearchitecture (6+ months) |
| **Team Knowledge (0-25)** | 18 | Critical for 50%+ of developers (sync, reindex, delete operations) but implicit in code |
| **3 E's Check** | ✓ | Estrutural (yes), Evidente (yes, but scattered), Estável (yes, 2+ months) |
| **Base Score** | 0 | Not a Step 0 category (no infrastructure service choice) |
| **TOTAL** | **65 / 150** | **BELOW THRESHOLD (need ≥75)** |

**Verdict**: DISCARD  
**Rationale**: While this is an important architectural constraint, it is already captured by the existing ADR `dual-store-persistence.md` in the INFRA module. This SYNC-specific analysis identifies how the pattern manifests operationally, but does not constitute a new decision.

---

### Decision 2: Graph Loop Closure via Manual Body Wikilinks

**Location**: `sync.py` (lines 365-396, function `_extract_body_edges`)

**What Was Identified**:
- Manual wikilinks written directly in note bodies become **accepted graph edges**
- Distinction: auto-generated blocks (`auto-connections`, `auto-backlinks`, `auto-moc-backrefs`) are **suggestions only**
- Only user-authored wikilinks (outside managed blocks) are persisted as `related` edges in `note_connections`
- Graph expansion and MOC generation operate on these edges

**Code Evidence**:
```python
# sync.py:365-396
def _extract_body_edges(db: StateDB, note_id: str, body: str) -> int:
    """Persist manual wikilinks in a note body as `related` graph edges."""
    stripped = _strip_auto_blocks(body)  # Exclude suggestions
    targets = {m for m in _ZTL_WIKILINK.findall(stripped) if m != note_id}
    
    for target in targets:
        if not db.get_note(target):
            continue  # Only link to known notes
        if frozenset((note_id, target)) in connected_pairs:
            continue  # Never downgrade existing edges
        db.upsert_note_connection(note_id, target, "related", "wikilink manual")
```

**Why This Decision Matters**:
- Enables the knowledge graph to incorporate hand-authored connections
- Closes a loop: users can hand-write `[[ZTL-123]]` links and they become real graph edges
- Affects search results (retrieval expansion), MOC generation (graph cohesion scoring)

**Git History Context**:
- **Introduced**: Implicit in `e8c1b8a` (2026-08-29) with sync-manual feature
- Mentioned in `CLAUDE.md` § sync.py: "Persist manual wikilinks in a note body as `related` graph edges"
- Not previously documented as a discrete decision

**Scoring Analysis**:

| Dimension | Score | Reasoning |
|-----------|-------|-----------|
| **Scope + Impact (0-25)** | 18 | Affects graph structure, retrieval expansion, MOC generation (3-4 modules) |
| **Cost to Change (0-25)** | 15 | Changing graph semantics requires vault-wide rebuild (2-8 weeks) |
| **Team Knowledge (0-25)** | 15 | Important for developers working on sync, graph, retrieval; moderate awareness needed |
| **3 E's Check** | ✓ | Estrutural (yes), Evidente (somewhat), Estável (yes) |
| **Base Score** | 0 | Not a Step 0 category |
| **TOTAL** | **48 / 150** | **BELOW THRESHOLD** |

**Verdict**: DISCARD  
**Rationale**: While this is a deliberate design pattern (vs. suggestions), the decision is localized to the sync module and is a relatively straightforward feature (extract wikilinks, check for duplicates, persist as edges). The complexity is low once the graph architecture is understood. Does not rise to system-architecture significance.

---

### Decision 3: Irreversible Deletion with Cascade Strategy

**Location**: `purge_source.py` (entire file), `new_note.py` (implicit), `sync.py` (irreversible edge persistence)

**What Was Identified**:
- **Core pattern**: Deletion is irreversible; cascades across vault + SQLite + ChromaDB
- **Default behavior**: When deleting a source, ZTL (permanent) notes are **kept** but wikilinks to deleted SRC/LIT notes are **stripped**
- **Optional flag**: `--delete-permanent` flag deletes ZTL notes as well (irreversible)
- **Recovery strategy**: No undo/soft-delete; relies on backups
- **Consistency model**: Attempts to clean wikilinks in all vault notes, but this cleanup itself may be incomplete

**Code Evidence**:
```python
# purge_source.py:207-215
def purge_source(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    source_id: str,
    *,
    delete_permanent: bool = False,  # Explicit flag required for ZTL deletion
    compact: bool = True,
) -> dict[str, Any]:
    """Delete a source completely from vault, SQLite, and Chroma.
    
    When delete_permanent is False (default), permanent notes are kept but
    wikilinks to removed SRC/LIT notes are stripped from all surviving notes.
    """
```

**Why This Decision Matters**:
- Defines data lifecycle and retention guarantees
- Affects user confidence in data safety
- Determines recovery procedures after accidental deletion

**Git History Context**:
- **Introduced**: `e8c1b8a` (2026-08-29) - "feat(cli): add new-note and delete-source with MOC backrefs"
- **Related**: Parallel `purge-rejected` command (rejects, then hard-deletes)
- Documented in CLAUDE.md: "delete-source" (irreversible cascade)

**Scoring Analysis**:

| Dimension | Score | Reasoning |
|-----------|-------|-----------|
| **Scope + Impact (0-25)** | 20 | Affects vault + DB + search; impacts 3+ operations (delete, purge, cascade) |
| **Cost to Change (0-25)** | 18 | Implementing soft-delete / reversibility would require major refactoring (6-12 weeks) |
| **Team Knowledge (0-25)** | 15 | Important for anyone reviewing/maintaining delete logic; moderate awareness |
| **3 E's Check** | ✓ | Estrutural (yes), Evidente (yes), Estável (yes) |
| **Base Score** | 0 | Not a Step 0 category |
| **TOTAL** | **53 / 150** | **BELOW THRESHOLD** |

**Verdict**: DISCARD  
**Rationale**: While this is a critical data-lifecycle decision, it is primarily a tactical choice within the broader "cascade delete" pattern, not a novel architectural pattern. The explicit documentation in CLAUDE.md is sufficient for now. A future ADR on "Data Retention & Deletion Policies" might consolidate this with `purge-rejected` at a higher architectural level.

---

### Decision 4: Manual Note Adoption Pattern (Vault-First Design)

**Location**: `new_note.py` (function `scaffold_manual_note`), `sync.py` (function `run_sync_manual`), `cli.py` (commands `new-note`, `sync-manual`)

**What Was Identified**:
- **Two-phase lifecycle**: Notes are created in the vault WITHOUT database/search indexing, then "adopted" via sync-manual
- **Rationale**: Allows hand-authored notes to coexist with pipeline-generated notes
- **Separation of concerns**: `new-note` scaffolds structure (meta + body template); `sync-manual` assigns IDs, checksums, and indexing
- **Design principle**: Vault is the source of truth; database follows

**Code Evidence**:
```python
# new_note.py:195-219
def scaffold_manual_note(cfg: AppConfig, note_type: str, title: str, ...) -> NewNoteResult:
    """Create a manual note file in the vault. Does not index into SQLite/Chroma."""
    # ... create file with origin: manual, no DB calls ...
    return NewNoteResult(path=path, note_type=normalized, meta=meta)

# sync.py:39-75
def run_sync_manual(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> dict[str, int]:
    """Scan all four note folders for manual/modified notes and sync them."""
    # ... now index, assign IDs, embed ...
```

**Why This Decision Matters**:
- Enables Obsidian users to hand-author notes without CLI involvement
- Allows incremental adoption: create notes, test them in vault, sync when ready
- Supports manual edits between pipeline runs

**Git History Context**:
- **Introduced**: `e8c1b8a` (2026-08-29) - "feat(cli): add new-note and delete-source with MOC backrefs"
- **Related**: LIT granular note structure (`508d4c0`), manual sync pattern from earlier months
- Not previously documented as a discrete architectural decision

**Scoring Analysis**:

| Dimension | Score | Reasoning |
|-----------|-------|-----------|
| **Scope + Impact (0-25)** | 15 | Affects new-note, sync-manual, vault structure; 2 modules |
| **Cost to Change (0-25)** | 12 | Could change in 1-2 weeks if needed; localized to these two modules |
| **Team Knowledge (0-25)** | 15 | Important for developers adding new note types or changing sync; moderate awareness |
| **3 E's Check** | ✓ | Estrutural (yes, two-phase), Evidente (yes), Estável (yes) |
| **Base Score** | 0 | Not a Step 0 category |
| **TOTAL** | **42 / 150** | **BELOW THRESHOLD** |

**Verdict**: DISCARD  
**Rationale**: While this is an intentional design pattern, it is localized to two modules and relatively simple once the async two-phase pattern is understood. The complexity is low; the decision is incremental (not fundamental). Does not rise to system-architecture significance.

---

### Decision 5: Origin Field for Dual Pipeline Control

**Location**: `sync.py` (lines 97-99, 120, 238, 250, 301), `new_note.py` (lines 252, 304, 318, 366), `rebuild.py` (lines 256-259, 273, 382, 397)

**What Was Identified**:
- Every note carries an `origin` field: `"pipeline"` | `"manual"`
- Controls vault rebuild behavior:
  - `force=True` on pipeline notes: overwrites with fresh content
  - `force=True` on manual notes: **preserves** (never overwrites)
- Used to track authorship and guide recovery operations
- Affects skip detection in sync (unchanged content is skipped based on semantic hash)

**Code Evidence**:
```python
# new_note.py:237-238
meta.setdefault("origin", "manual")  # Mark as hand-authored

# rebuild.py:256-259
if not force:
    stats["skipped"] += 1
    return False
if origin == "manual":
    logger.info("Preservando nota manual (nao sobrescrita): %s", path.name)
    stats["skipped"] += 1
    return False
```

**Why This Decision Matters**:
- Enables safe vault rebuilds without overwriting hand-edits
- Guides data lifecycle decisions (which notes are pipeline-owned vs. user-owned)
- Critical for tools like `zettel rebuild --force` to behave correctly

**Git History Context**:
- Implicit in all note creation since granular LIT feature (`508d4c0`, 2026-08-28)
- Not a discrete commit; pattern is emergent from the dual-note-source design

**Scoring Analysis**:

| Dimension | Score | Reasoning |
|-----------|-------|-----------|
| **Scope + Impact (0-25)** | 20 | Affects all note types (SRC, LIT, ZTL, MOC); lifecycle operations |
| **Cost to Change (0-25)** | 15 | Removing origin tracking would require rethinking rebuild logic (2-8 weeks) |
| **Team Knowledge (0-25)** | 12 | Important for rebuild, force flags, but relatively straightforward |
| **3 E's Check** | ✓ | Estrutural (yes), Evidente (yes), Estável (yes) |
| **Base Score** | 0 | Not a Step 0 category |
| **TOTAL** | **47 / 150** | **BELOW THRESHOLD** |

**Verdict**: DISCARD  
**Rationale**: While this is an important lifecycle marker, the decision is primarily a **tag** (a simple enum field) rather than an architectural pattern. The logic it controls (force rebuild, skip detection) is straightforward and localized.

---

## Summary of Scoring

| Decision | Scope | Cost | Knowledge | Base | **Total** | Verdict |
|----------|-------|------|-----------|------|----------|---------|
| Cross-Store Consistency | 25 | 22 | 18 | 0 | **65** | DISCARD |
| Graph Loop Closure | 18 | 15 | 15 | 0 | **48** | DISCARD |
| Irreversible Deletion | 20 | 18 | 15 | 0 | **53** | DISCARD |
| Manual Note Adoption | 15 | 12 | 15 | 0 | **42** | DISCARD |
| Origin Field Control | 20 | 15 | 12 | 0 | **47** | DISCARD |

---

## Why SYNC Module Does Not Produce ADRs

**Root Cause**: The SYNC module is a **tactical implementation layer** that operationalizes larger architectural decisions made at the INFRA level:

1. **Dual-Store Consistency** → Already documented as ADR `dual-store-persistence.md`
2. **Graph Closure** → Emergent feature of the existing graph architecture (retrieval.py + graph.py)
3. **Deletion Semantics** → Follows the broader "cascading delete" pattern already in the system
4. **Manual Notes** → Implementation of the "Manual Vault Integration" cross-cutting concern listed in mapping.md
5. **Origin Tracking** → Consequence of dual-pipeline design (pipeline vs. manual)

All SYNC decisions score **below 75 points** because they are:
- **Localized** (affect 1-3 modules, not system-wide)
- **Incremental** (extensions of existing patterns)
- **Straightforward** (once the underlying architecture is understood, implementation is clear)

---

## Recommendations

### For immediate consideration:
- All SYNC module decisions are adequately documented in code comments and CLAUDE.md
- No new ADRs needed at this time

### For future cycles:
- **Consolidate data lifecycle decisions**: When `purge-rejected` is analyzed, consider a higher-level ADR on "Data Retention & Deletion Policies" that covers both `purge-source` and `purge-rejected`
- **Document eventual consistency patterns**: A follow-up ADR on "Eventual Consistency Guarantees and Recovery Procedures" could dive deeper into how stores stay in sync (currently only mentioned in `dual-store-persistence.md`)
- **Graph semantics clarification**: An ADR on "Graph Edge Semantics (Related vs. Auto-Suggestions)" could explicitly document the distinction between user-authored and auto-generated connections

### For testing & observability:
- **High-risk operations**: `purge_source()` and `rebuild_vault(--force)` would benefit from integration tests that verify:
  - SQLite deletes succeed before ChromaDB cleanup
  - Wikilink stripping is exhaustive
  - Origin field prevents accidental overwrites
- **Cross-store consistency monitoring**: Add logging/metrics for store inconsistency detection (e.g., notes in SQLite but not in Chroma)

---

## Conclusion

The MANUAL-SYNC module contains **important operational patterns** but not **novel architectural decisions**. All identified decisions either:
1. Implement existing patterns from INFRA (dual-store consistency)
2. Are tactical features (graph edge extraction, vault-first scaffolding)
3. Are already well-documented in comments and CLAUDE.md

**No ADRs generated.** Proceed to analysis of remaining pending modules (CLI, if desired).

---

## Appendix: Module Interdependencies

```
SYNC → INFRA (config, state, index, vault, hashing)
SYNC → RETRIEVAL (Retriever for _suggest_connections)
SYNC → GARDENER (moc_backrefs.sync_moc_backrefs)
SYNC → HARVESTER (_generate_citekey import)

DELETE-SOURCE → SYNC (clean_wikilinks_in_vault, orphaning logic)
REBUILD → SYNC (shared wikilink extraction, origin field)
```
