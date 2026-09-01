# ADR Runbook — Quick Reference Guide

**Purpose**: Answer "Which ADRs should I read?" for common tasks  
**How to use**: Search by task type, then consult linked ADRs  
**Updated**: 2026-09-01

---

## By Task Type

### Ingestion & Extraction

#### "I want to add support for a new document format (Word, RTF, etc.)"
```
Read ADRs:
  1. ADR-012 (Docling is mandatory PDF extractor)
     → Docling handles PDF extraction + page inference
  2. ADR-014 (Hybrid chunking strategy)
     → Text is split at H3-H6 boundaries, fallback recursive splitter
  3. ADR-007 (Layered hashing)
     → New format must produce deterministic hashes

Questions to answer:
  - Can Docling extract this format, or do you need a separate extractor?
  - How does your format handle heading hierarchy? (H1-H6)
  - How do you assign page numbers? (relevant for citations)
  - Is extraction deterministic across re-runs?
```

#### "PDF extraction is failing / producing bad chunks"
```
Read ADRs:
  1. ADR-012 (Docling + fallback)
     → Docling is primary. If it fails, harvest fails (no fallback).
  2. ADR-013 (3-layer page inference)
     → Page detection: explicit metadata → text pattern → interpolation
  3. ADR-014 (Hybrid chunking)
     → Check if document has H3-H6 headings (structural chunking needs them)

Debug steps:
  - Check Docling version (pinned, not floating)
  - Verify GPU available (soft dependency, CPU fallback ok)
  - Test with `zettel dump-extraction --source-id @citekey` 
    to see extracted text before chunking
  - Check page-break markers in extracted text (<!-- zettel:page-break -->)
```

#### "I want to change chunk size or overlap"
```
Read ADRs:
  1. ADR-014 (Hybrid chunking strategy)
     → Global config, not per-document
  2. ADR-007 (Layered hashing)
     → Changing chunk boundaries invalidates stored checksums
     → Old sources must be re-harvested

Impact:
  - ⚠️ All previously harvested sources will have different chunk hashes
  - ⚠️ Dedup layer-3 (semantic) will re-trigger on "previously seen" content
  - ⚠️ No automatic migration; operator must decide which sources to rechunk

Before changing:
  - Document why change is necessary
  - Plan re-harvest of critical sources
  - Test on subset first
```

---

### Literature Notes & Review

#### "Confidence thresholds (0.4 / auto-approve) seem wrong"
```
Read ADRs:
  1. ADR-017 (Confidence-band HITL approval gate)
     → Thresholds are initial heuristic estimates, not empirically calibrated
     → YAML is the operational source for auto_approve_min_confidence
       (may differ from the ADR historical default 0.7)
  2. ADR-016 (Post-approval dedup timing)
     → Dedup happens after approval, not before

Also see:
  - CLAUDE.md Phase 2b (review.py) — same heuristic intent
  - config/config.yaml literature_review.auto_approve_min_confidence
  - review.py _LOW_CONFIDENCE_MAX = 0.4 (hard-coded very-low cut)

Action:
  - Monitor real-world impact: how many drafts fall into each band
    after harvest/extract?
  - If imbalanced (too many in "medium"? too many auto-approved FP?):
    → Propose new thresholds via GitHub issue
  - Document why change is needed (e.g., "extraction model improved,
    now 80% are high-conf")
  - Significant extract-model change → schedule a formal calibration
    pass (future phase; do not retune in code without evidence)

Example:
  Current (illustrative): 0.4 (very-low, review.py) | 0.4–limiar (medium)
                          | limiar+ (high; read the YAML, not ADR 0.7)
  Proposed: 0.3 (very-low) | 0.3-0.8 (medium) | 0.8+ (high)
  Reason: New extractor model has higher overall confidence distribution
```

#### "Web review UI should approve below-threshold chunks"
```
Read ADRs:
  1. ADR-018 (Web/CLI validation asymmetry)
     → Server-side enforcement: both web and CLI respect threshold
  2. ADR-017 (Confidence bands)
     → Threshold is a uniform gate, not a display hint

Status:
  - ✅ ADR-018 resolved: threshold now enforced server-side
  - ❌ No implicit override path (was a security gap)
  - 💡 If override needed in future: explicit "Force Approve (admin)" 
       button with audit trail (not yet implemented)

If urgent:
  - Create GitHub issue: "Need explicit force-approve mechanism for review UI"
  - Reference ADR-018 for context
```

#### "Literature notes are not being indexed into Chroma"
```
Read ADRs:
  1. ADR-015 (Granular per-chunk literature notes)
     → Notes created during EXTRACT, indexed during REVIEW approval
  2. ADR-002 (ChromaDB embedded vector store)
     → 5 collections: sources, chunks, permanent_notes, mocs, literature_notes

Debug:
  - Did you REVIEW the literature notes? (REVIEW phase is when indexing happens)
  - Check `literature_notes` collection in Chroma
  - Verify note is marked `status=approved` (only approved notes are indexed)
```

---

### Permanent Notes & Graph

#### "Permanent notes are not retrievable via hybrid search"
```
Read ADRs:
  1. ADR-003 (Hybrid dense+BM25 retrieval with RRF + relevance floor)
     → Dense embedding + BM25 + relevance floor + graph expansion
  2. ADR-010 (Result transparency: hits vs candidates)
     → Results show both high-confidence hits and near-miss candidates
  3. ADR-009 (Graph-based expansion)
     → Optional BFS expansion finds conceptually-opposite notes (contradicts edge)

Debug:
  - Run `zettel ask "your query" --show-context` to see what retrieval finds
  - Check `candidates` list: was the note near-miss but below floor?
  - If so, check `floor_reason` for why it was rejected
  - Try graph expansion: `zettel ask "..." --hop-depth 2`
```

#### "I want to change note relation types (contradicts, supports, etc.)"
```
Read ADRs:
  1. ADR-008 (Repository pattern: StateDB and VectorIndex)
     → Relation types stored in note_connections table
  2. ADR-009 (Graph expansion)
     → Each relation type has a weight (contradicts highest priority)
  3. ADR-003 (Hybrid retrieval)
     → Graph expansion uses these weights to score neighbors

Before changing:
  - Update weights in DEFAULT_RELATION_WEIGHTS (config.py)
  - Re-run `zettel sync-manual --rebuild-graph` if you change existing edges
  - Test retrieval with new weights
```

---

### MOC Generation & Clustering

#### "MOC clusters don't match my taxonomy"
```
Read ADRs:
  1. ADR-019 (Taxonomy-first MOC clustering with UMAP+HDBSCAN)
     → Primary: embed category labels, assign notes to categories, cluster within
  2. ADR-004 (YAML-first configuration)
     → moc_topics.yaml defines category labels
  3. ADR-021 (Single LLM call per cluster routing)
     → Each cluster routed through signature match → overlap → category → generation

Debug:
  - Check moc_topics.yaml: are your categories defined?
  - Run `zettel garden --recreate` to force regeneration
  - Check config.gardener.cluster_within_category (should be true for taxonomy-first)
  
If labels don't match:
  - Edit moc_topics.yaml
  - Re-run garden (ADR-021 will reuse existing MOCs if overlap >= threshold)
```

#### "Hub MOCs are empty / not generating"
```
Read ADRs:
  1. ADR-020 (Hub-anchored MOC generation)
     → Complementary to taxonomy-first; opt-in via `--hubs` flag
  2. ADR-021 (Single-call routing)
     → Each hub cluster routed through same decision tree
  3. ADR-009 (Graph expansion)
     → Hub neighborhoods discovered via BFS over note_connections

Debug:
  - Are notes actually connected? (check note_connections table)
  - Run `zettel garden --hubs --recreate` to force regeneration
  - Check config.gardener_hub settings (percentile vs absolute ranking)
  - Verify notes are "approved" status (only approved notes included)
```

#### "MOC generation is taking too long"
```
Read ADRs:
  1. ADR-021 (Single LLM call per cluster routing)
     → Hard ceiling: one LLM call per cluster
  2. ADR-007 (Layered hashing)
     → Signature matching avoids redundant LLM calls
  3. ADR-003 (Hybrid retrieval)
     → Graph expansion adds query cost (bounded by max_neighbors)

Optimization:
  - Check how many clusters are being generated
  - If many identical clusters: ADR-021 routing should skip LLM via signature match
  - If overlap detection is slow: check overlap_threshold (may be too strict)
  - If graph expansion is slow: reduce max_hops or hop_decay
```

---

### LLM & Caching

#### "LLM calls are expensive / I want to reduce cost"
```
Read ADRs:
  1. ADR-025 (System+Human prompt split)
     → Stable system instructions → prefix reuse on OpenAI/Gemini/Ollama
     → Anthropic explicit cache_control hint
  2. ADR-007 (Layered hashing)
     → Deterministic LLM call caching: same input → cached response
  3. ADR-024 (Multi-provider strategy)
     → Can switch providers via config (OpenAI/Anthropic/Gemini/Ollama)

Cost-saving levers:
  - ✅ Prompt caching: already on (ADR-025, config.llm.prompt_cache)
  - ✅ Deterministic caching: already on (ADR-007, llm_cache table)
  - ✅ Confidence-band review: only approve high-conf → less downstream LLM calls
  - ✅ Single-call routing: MOC generation limited to 1 call per cluster
  - 🔧 Switch provider: edit config.yaml llm.provider
  - 🔧 Reduce garden frequency: garden only when new notes added
```

#### "I want to switch from OpenAI to Anthropic (or vice versa)"
```
Read ADRs:
  1. ADR-024 (Multi-provider LLM strategy)
     → Provider chosen at startup via config.yaml
  2. ADR-025 (System+Human prompt split)
     → Works with any provider; Anthropic gets explicit cache hints

Steps:
  1. Edit config/config.yaml:
     llm:
       provider: anthropic  # was: openai
       model: claude-3-5-sonnet-20241022
  2. Set ANTHROPIC_API_KEY in .env
  3. Re-run pipeline (all LLM calls go through new provider)
  4. ⚠️ Note: llm_cache checksums don't change, so cached responses reused
     (this is good — no cost spike on switch)
```

#### "I want to use a local LLM (Ollama)"
```
Read ADRs:
  1. ADR-024 (Multi-provider strategy)
     → Ollama supported via LangChain ChatOllama client
  2. ADR-025 (Prompt caching)
     → Ollama works but no explicit cache hints (still benefits from split)

Setup:
  1. Start Ollama locally: ollama serve
  2. Edit config/config.yaml:
     llm:
       provider: ollama
       model: llama2  # or your model
       base_url: http://localhost:11434
  3. Test: .venv/Scripts/python.exe -m zettel status
  4. Run pipeline (will use local Ollama)

Note:
  - Cost tracking will show $0 (local LLM)
  - Prompt cache doesn't apply (Ollama limitation)
```

---

### Web UI & Job Queue

#### "Web review job is stuck / taking too long"
```
Read ADRs:
  1. ADR-023 (SQLite-backed persistent job queue)
     → Single worker thread; one job at a time (409 on second submit)
  2. ADR-022 (FastAPI server-rendered)
     → SSE streams progress; no bidirectional client state

Debug:
  - Check web_jobs table for status (queued/running/completed/failed/interrupted)
  - View web_job_events for progress checkpoints
  - If running: wait (single worker, no parallelism)
  - If stuck: restart server (jobs marked interrupted on startup)
  
Limits:
  - No concurrent jobs (by design, to protect shared SQLite/vault)
  - Single Uvicorn worker (no multiprocessing)
```

#### "Web UI validation is too strict / too loose"
```
Read ADRs:
  1. ADR-018 (Web/CLI validation asymmetry)
     → Both now enforce threshold server-side (was a gap)
  2. ADR-022 (FastAPI server-rendered)
     → All form validation is server-side + client-side hints
  3. ADR-004 (YAML-first config)
     → Thresholds configured globally

To adjust:
  - Edit config/config.yaml: literature_review.auto_approve_min_confidence
  - The new number is still a heuristic (ADR-017), not a calibrated contract
  - Both CLI and web will enforce the new threshold
  - No bypass path (security by design)
  - Changing the 0.4 very-low cut requires a code change in review.py
    (propose via GitHub issue; see "Confidence thresholds" above)
```

---

### Persistence & Dual-Store

#### "SQLite and ChromaDB are out of sync"
```
Read ADRs:
  1. ADR-005 (Dual-store persistence without cross-store transactions)
     → SQLite and ChromaDB write separately; inconsistency window exists
  2. ADR-001 (SQLite with WAL+FTS5)
     → SQLite is source of truth for state
  3. ADR-008 (Repository pattern)
     → StateDB and VectorIndex are the access layer

Recovery:
  1. Run `zettel reindex` to rebuild ChromaDB from SQLite
     (reads all notes/chunks from SQLite, re-embeds into Chroma)
  2. Or `zettel sync-manual --rebuild-graph` (re-indexes only manual notes)
  
Prevention:
  - Single worker job queue (ADR-023) limits concurrent mutations
  - Phase-based checkpointing (status fields) tolerates partial failure
  - No guaranteed protection; manual reconciliation is the safety net
```

#### "I'm getting 'file already exists' on harvest"
```
Read ADRs:
  1. ADR-011 (Three-layer duplicate detection)
     → File hash → extraction hash → semantic similarity (in order)
  2. ADR-007 (Layered hashing)
     → Each layer produces checksums for comparison

Debug:
  - Layer 1 (file hash): Is the exact same file in inbox twice?
  - Layer 2 (extraction hash): Did you export the same PDF in different format?
  - Layer 3 (semantic): Is this a reformatted version of an existing source?

Action:
  - Remove duplicate file from inbox
  - Or use `--force` to re-harvest (careful!)
  - Or use `--skip-duplicates` to skip all duplicates
```

---

### Configuration & Schema

#### "I want to add a new configuration option"
```
Read ADRs:
  1. ADR-004 (YAML-first configuration)
     → All keys must be in config.yaml (Pydantic defaults are fallback only)
  2. ADR-006 (Pydantic v2 config DTOs)
     → config.py defines schema; YAML provides values

Steps:
  1. Add field to config.py schema (nested class, typed)
  2. Add field to config/config.yaml with a value
  3. Access via config.<path>.<to>.<field>
  
Example:
  config.py:
    class GardenerConfig(BaseModel):
      new_option: str = Field(default="value")
  
  config.yaml:
    gardener:
      new_option: value
  
  In code:
    cfg = AppConfig()
    print(cfg.gardener.new_option)
```

#### "I want to change how configs are loaded"
```
Read ADRs:
  1. ADR-004 (YAML-first configuration)
     → Single source of truth is config.yaml
  2. ADR-006 (Pydantic v2)
     → Validation is strict; all keys checked
  3. ADR-001 (SQLite state)
     → Some runtime state is persisted (separate from config)

⚠️ Before changing:
  - Config loading is used by 25+ modules
  - Tests rely on Field defaults when config.yaml is missing
  - Changing validation could break unrelated code
  
Recommended:
  - Keep YAML-first pattern
  - Add a --validate-config flag if needed
  - Document any breaking changes in migration guide
```

---

## By Module (What ADRs Govern This Code?)

| Module | Key ADRs | Governance |
|--------|----------|-----------|
| **harvester/** (package) | 011, 012, 013, 014, 027 | Extraction strategy, chunking, dedup, paging, package layout |
| **extractor.py** | 015, 016, 025 | Literature note format, dedup timing, prompting |
| **review.py** | 016, 017, 018 | Approval gate, thresholds, validation |
| **connector.py** | 003, 009, 010, 025 | Retrieval (RAG), graph expansion, prompting |
| **retrieval.py** | 003, 009, 010 | Hybrid fusion, floor, graph expansion |
| **gardener.py** | 019, 021, 025 | Taxonomy clustering, routing, prompting |
| **gardener_hub.py** | 020, 021, 025 | Hub MOCs, routing, prompting |
| **web.py, web_app.py** | 022, 023, 018 | Server rendering, job queue, validation |
| **config.py** | 004, 006 | YAML-first, Pydantic schema |
| **state.py** | 001, 005, 007, 008 | SQLite persistence, hashing, repository pattern |
| **index.py** | 002, 008 | ChromaDB, repository pattern |
| **llm.py** | 024, 025 | Multi-provider, prompt caching |
| **article.py** | 028, 003, 009, 010, 024, 025 | Article domain helpers: catalog, outline, drafting, assembly, judge |
| **article_graph/** (package) | 028, 029 | LangGraph orchestration (13 nodes, HITL interrupts, judge loop), package layout |
| **ask.py** | 003, 009, 010 | Hybrid retrieval, relevance floor, graph expansion |
| **cli.py** | 026 | Typer/Rich framework (all commands routed through) |

---

## Troubleshooting Flowchart

```
"Something is broken"
  │
  ├─ Ingestion/Harvest issue?
  │  └─ Check: ADR-011, ADR-012, ADR-013, ADR-014
  │
  ├─ Literature notes / Review issue?
  │  └─ Check: ADR-015, ADR-016, ADR-017, ADR-018
  │
  ├─ Retrieval / Search issue?
  │  └─ Check: ADR-003, ADR-009, ADR-010
  │
  ├─ MOC generation issue?
  │  └─ Check: ADR-019, ADR-020, ADR-021
  │
  ├─ Web UI / Job queue issue?
  │  └─ Check: ADR-022, ADR-023
  │
  ├─ Data consistency issue?
  │  └─ Check: ADR-001, ADR-005, ADR-007, ADR-008
  │
  ├─ Configuration issue?
  │  └─ Check: ADR-004, ADR-006
  │
  └─ LLM / Cost issue?
     └─ Check: ADR-024, ADR-025
```

---

## When to Create a New ADR

You should open a GitHub issue for a new ADR if you find yourself:
- Designing a new feature that affects architecture (not just tactics)
- Making a choice between multiple technical strategies
- Setting a pattern that future code will follow
- Accepting a trade-off that's not obvious

**Don't** open an ADR for:
- Bug fixes (document in PR)
- Behavior-preserving refactoring inside a module (document in commit message)
- One-off feature tweaks

**Exception — structural refactors do get an ADR.** When a refactor changes module boundaries or the
public import surface (splitting a monolith into a package, moving a public API), record it: future
readers need to know why the layout is what it is, and the import rules are a pattern future code
follows. Precedents: [ADR-027](./generated/HARVEST/ADR-027-harvest-phase-as-python-package.md)
(`harvester.py` → `harvester/`) and
[ADR-029](./generated/QA-WRITING/ADR-029-article-graph-as-python-package.md)
(`article_graph.py` → `article_graph/`).

---

**Last updated**: 2026-09-01  
**Maintained by**: Architecture team  
**Related documents**: [ADR-INDEX.md](./ADR-INDEX.md), [ACTION-PLAN-2026-08-31.md](./ACTION-PLAN-2026-08-31.md)
