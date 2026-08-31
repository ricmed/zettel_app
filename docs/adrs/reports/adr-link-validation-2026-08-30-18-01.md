# ADR Link Validation Report

**Generated:** 2026-08-30 18:01
**Scope:** `docs/adrs/generated/` — all 26 ADRs, post-update validation

## Summary

```
Checked: 74 links (37 bidirectional pairs: 14 Depends-on/Used-by + 23 Related-to)
Valid: 74 (100%)
Broken: 0
Orphaned: 0
Non-clickable / malformed: 0
Reciprocity failures: 0
Circular Depends-on chains: 0
Conflicting relationship types on the same pair: 0
Status/relationship inconsistencies: 0
```

## Validation Methodology

Each of the 26 updated files was re-parsed programmatically:

1. **Link extraction** — every `[ADR-XXX: Title](path)` link inside each file's header block (everything before the first `## ` heading) was extracted, per relationship field (`Depends on`, `Used by`, `Related to`).
2. **Target resolution** — each link's relative path was resolved against its source file's directory (accounting for same-module, cross-module, and `needs-input` subdirectory paths) and checked for existence on disk.
3. **Reciprocity check** — for every `Depends on` edge A→B, verified B carries a matching `Used by` entry pointing back to A (and vice versa); for every `Related to` edge A→B, verified B carries a matching `Related to` entry pointing back to A.
4. **Cycle detection** — built a directed graph of `Depends on` edges only and checked for cycles.
5. **Conflict check** — checked whether any pair of ADRs carries more than one relationship type simultaneously (e.g. both `Supersedes` and `Depends on`), which would require a priority resolution.
6. **Status consistency** — checked that any ADR carrying a `Superseded by` relationship has `Status: Superseded`. (Not applicable this run: no Supersedes/Superseded-by relationships exist in this corpus.)
7. **MADR format compliance** — verified every modified file still opens with `# ADR-XXX: Title`, followed immediately by `**Status:**` and `**Date:**`, and that the first `## ` heading (`## Context and Problem Statement`) and all content below it are byte-identical to the pre-update file (only header lines between `**Date:**` and the first `##` were touched).

## Results by Check

### 1–2. Link extraction and target resolution

26 files scanned, 74 links found, all 74 resolved to an existing file on disk. No broken links, no orphaned link targets (i.e. no link pointing at a file that isn't one of the 26 known ADRs).

### 3. Reciprocity

All 37 unique relationship pairs are fully bidirectional:

- 14 `Depends on` ↔ `Used by` pairs — all reciprocated correctly.
- 23 `Related to` ↔ `Related to` pairs — all reciprocated correctly.

No unidirectional relationship was found (i.e. no case of A linking to B without B linking back).

### 4. Circular dependencies

No cycles detected in the `Depends on` graph. The longest dependency chain is 3 hops: e.g. `LLM system-human-prompt-split-for-provider-agnostic-caching` → `LLM pluggable-multi-provider-llm-strategy` → `INFRA yaml-first-configuration`, and `GARDEN hub-anchored-moc-pipeline` → `RETRIEVAL graph-based-note-discovery-weighted-bfs` → `INFRA hybrid-dense-bm25-retrieval`.

### 5. Conflicting relationship types

No ADR pair carries more than one relationship type. No Supersedes relationships exist in this corpus, so no Supersedes/Depends-on conflict was possible.

### 6. Status consistency

No ADR carries a `Superseded by` relationship, so the "Status must be Superseded" rule does not apply to any file in this run. All 26 ADRs retain their original `Status` value (25 `Accepted`, 1 `Proposed` — `REVIEW/needs-input/ADR-XXX-web-cli-auto-approve-threshold-validation-asymmetry.md`) unchanged.

### 7. MADR format / content preservation

All 26 files verified: `# ADR-XXX: Title` / `**Status:**` / `**Date:**` header lines preserved verbatim, only new relationship lines inserted between `**Date:**` and the first `## ` heading; every content section (`## Context and Problem Statement` onward, including `## Decision Drivers`, `## Considered Options`, `## Decision Outcome`, `## Pros and Cons of the Options`, `## Consequences`, `## References`, and any `[NEEDS INPUT: ...]` markers) is unchanged from the pre-update version.

## Cap-Exception Flags (not errors)

| ADR | Field | Count | Note |
|---|---|---|---|
| `INFRA/ADR-XXX-layered-hashing-strategy.md` | Related to | 5 | Exceeds the recommended max of 3; all 5 are manual or reciprocals of another ADR's manual "Related ADRs" hint, exempted from the cap per the manual-relationship preservation rule. Flagged inline in the file. |

No other ADR exceeds the max-3 cap for `Depends on` or `Related to`.

## Per-File Link Count

| File | Depends on | Used by | Related to |
|---|---|---|---|
| CLI/ADR-XXX-typer-rich-cli-framework.md | 0 | 0 | 2 |
| EXTRACT/ADR-XXX-granular-literature-notes-readable-filenames.md | 0 | 1 | 2 |
| GARDEN/ADR-XXX-hub-anchored-moc-pipeline.md | 2 | 0 | 1 |
| GARDEN/ADR-XXX-single-llm-call-per-cluster-routing.md | 0 | 1 | 3 |
| GARDEN/ADR-XXX-taxonomy-first-moc-clustering.md | 0 | 0 | 3 |
| HARVEST/ADR-XXX-hybrid-structural-chunking-strategy.md | 1 | 0 | 2 |
| HARVEST/ADR-XXX-three-layer-duplicate-detection.md | 2 | 0 | 1 |
| HARVEST/ADR-XXX-three-layer-page-inference-strategy.md | 1 | 0 | 1 |
| HARVEST/needs-input/ADR-XXX-docling-primary-pdf-extractor-pymupdf-fallback.md | 0 | 3 | 1 |
| INFRA/ADR-XXX-chromadb-embedded-vector-store.md | 0 | 0 | 1 |
| INFRA/ADR-XXX-dual-store-persistence.md | 0 | 0 | 3 |
| INFRA/ADR-XXX-hybrid-dense-bm25-retrieval.md | 0 | 2 | 3 |
| INFRA/ADR-XXX-layered-hashing-strategy.md | 0 | 1 | 5 (flagged) |
| INFRA/ADR-XXX-pydantic-v2-config-dtos.md | 0 | 0 | 1 |
| INFRA/ADR-XXX-repository-pattern-data-access.md | 0 | 0 | 3 |
| INFRA/ADR-XXX-sqlite-wal-fts5-primary-persistence.md | 0 | 1 | 1 |
| INFRA/ADR-XXX-yaml-first-configuration.md | 0 | 1 | 1 |
| LLM/ADR-XXX-pluggable-multi-provider-llm-strategy.md | 1 | 1 | 0 |
| LLM/ADR-XXX-system-human-prompt-split-for-provider-agnostic-caching.md | 1 | 0 | 2 |
| RETRIEVAL/ADR-XXX-graph-based-note-discovery-weighted-bfs.md | 1 | 1 | 3 |
| RETRIEVAL/ADR-XXX-retrieval-result-transparency-hits-vs-candidates.md | 1 | 0 | 1 |
| REVIEW/ADR-XXX-post-approval-concept-deduplication-timing.md | 1 | 0 | 0 |
| REVIEW/needs-input/ADR-XXX-confidence-band-hitl-approval-gate.md | 1 | 2 | 1 |
| REVIEW/needs-input/ADR-XXX-web-cli-auto-approve-threshold-validation-asymmetry.md | 1 | 0 | 1 |
| WEB/ADR-XXX-fastapi-server-rendered-jinja2-no-spa.md | 0 | 0 | 3 |
| WEB/ADR-XXX-sqlite-backed-job-queue-single-worker.md | 1 | 0 | 1 |

**Totals:** 14 Depends on, 14 Used by, 46 Related to = 74 links across 26 files.

## Verdict

**Validation: OK.** All links are valid, clickable Markdown, fully bidirectional, free of cycles and conflicts. One documented, justified exception to the max-3 cap (manual-relationship preservation on `layered-hashing-strategy`). No errors.
