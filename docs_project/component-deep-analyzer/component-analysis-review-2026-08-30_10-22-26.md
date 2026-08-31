# Component Deep Analysis Report — `review` (zettel/review.py)

## 1. Executive Summary

`zettel/review.py` implements **Phase 2b** of the Zettelkasten pipeline (`harvest → extract → review → connect → garden`): a Human-In-The-Loop (HITL) selective-approval gate sitting between `extract` (which drafts granular Literature notes from chunks via an LLM) and `connect` (which turns approved concepts into Permanent notes).

Its purpose is to prevent low-quality or hallucinated LLM extractions from ever reaching the permanent, connected part of the vault. No literature note is embedded into the `literature_notes` Chroma collection, and no concept becomes eligible for `connect`, until a human (or an explicit non-interactive policy) approves it here.

Key findings:

- The component is a **pure orchestration/business-logic module** with no persistent state of its own — it reads/writes exclusively through `StateDB` (SQLite), `VectorIndex` (ChromaDB), and the vault filesystem (via `zettel.vault`).
- It exposes three consumption surfaces that must stay behaviorally consistent: the **Typer CLI** (`zettel review`, `zettel purge-rejected`), the **`run-all`/`run_all` orchestrated pipeline** (CLI and web), and the **Web UI** (`/review`, `/review/action`, dispatched through `WebWorker._dispatch("review", ...)`). The web surface reimplements a reduced (batch-only, no confidence bands, no interactive prompts) version of the same approve/reject primitives rather than calling `run_review` directly.
- The core business rule is a single **confidence threshold** (`literature_review.auto_approve_min_confidence`, default `0.85`) that gates both the non-interactive auto-approve path and the interactive "aprovar todos" (approve-all) command; a secondary fixed **`0.4` cut** subdivides "below-threshold" drafts into `very_low` / `medium` bands purely for reporting and batch-rejection UX.
- Approval and rejection are **irreversible workflow transitions** with side effects across three storage systems (vault file move + managed block, SQLite `chunks`/`concepts` row updates, Chroma `literature_notes` upsert/delete) — but they are not committed atomically; a crash mid-`approve_chunk` can leave these three stores inconsistent (see Technical Debt).
- `purge_rejected` is a separate, deliberately irreversible cleanup operation (hard delete + optional VACUUM of both SQLite and Chroma) exposed only via CLI, never via web.
- Test coverage for the pure/deterministic logic (band classification, decision parsing, approve/reject/purge happy paths, the four interactive branches of `run_review`) is strong (18 tests in `tests/test_review.py`). The **web dispatch path for individual `approve_chunk`/`reject_chunk` calls (`web_app.py:304-334`) and the `/review/action` HTTP endpoint have no dedicated tests** — only the unrelated `run_all` aggregate flow exercises `review.run_review` under mock.

## 2. Data Flow Analysis

There are three distinct entry paths into the same underlying primitives (`approve_chunk`, `reject_chunk`, `_dedupe_approved_concepts`). All three converge on the same read/write contract with `StateDB`, `VectorIndex`, and the vault.

**A. CLI interactive review (`zettel review`, no `--yes`/`--auto-approve`):**
```
1.  cli.review() → run_review(cfg, db, idx, interactive=True)
2.  db.get_chunks_by_status("awaiting_review", source_id=...) — load candidate drafts
3.  Rich table + confidence_band_counts() + format_confidence_report() — HITL summary
4.  Prompt.ask("Modo") loop: a (approve-all>=limiar) | d (reject submenu) | r (one-by-one) | q (quit)
5a. mode "a"  → approve_chunk() for every chunk with review_confidence >= limiar
5b. mode "d"  → confidence_band_counts/filter_chunks_by_band() → confirm → reject_chunk() per target
5c. mode "r"  → ask_review_decision() per chunk → approve_chunk()/reject_chunk()/skip
6.  _dedupe_approved_concepts() → extractor.deduplicate_candidates() → concepts: extracted → approved/duplicate
7.  usage.finish_pipeline_run() persists cost/run bookkeeping; stats returned to CLI and printed
```

**B. Non-interactive / auto-approve (CLI `--yes`/`--auto-approve`, or `run-all`, or web `run_all`):**
```
1.  run_review(..., auto_approve=True, interactive=False)
2.  For each awaiting_review chunk: review_confidence >= limiar → approve_chunk(); else → skipped (stays awaiting_review)
3.  _dedupe_approved_concepts() runs once after the loop
4.  usage.finish_pipeline_run()
```

**C. Web granular review (`GET /review`, `POST /review/action` → `WebWorker._dispatch("review", ...)`):**
```
1.  GET /review: db.get_chunks_by_status("awaiting_review") → enrich with summary_json → optional
    client-selected confidence-band filter (low/medium/high, same 0.4/limiar cuts) → paginate (20/page)
2.  User selects chunk_ids via checkboxes, POSTs action=approve|reject to /review/action
3.  web.review_action() → _post_job(request, "review", {action, chunk_ids}, csrf) → enqueued as a web_jobs row
4.  WebWorker._dispatch("review", payload): resolves chunk_ids directly, or via confidence_below threshold
5.  Per chunk_id: approve_chunk() or reject_chunk() (no band/limiar gating — the web UI itself decided what to send)
6.  If action == approve and any succeeded: finalize_approved_concepts() → _dedupe_approved_concepts()
7.  db.start_run("review")/finish_pipeline_run() wraps the whole batch as one run row
```

**Approval side-effect fan-out (`approve_chunk`, common to all three paths):**
```
1. Load chunk + source from StateDB
2. Resolve/derive destination path under 20_Literature/{Citekey}/
3. If draft file exists: patch frontmatter status=approved, updated_at; safe_write_note(); unlink draft
   Else: rebuild the note from summary_json via build_literature_chunk_note() (draft-loss fallback)
4. safe_update_managed_blocks() writes the auto-source-excerpt block (verbatim source text)
5. _literature_embed_text(): strip frontmatter + managed blocks → embeddable text (excerpt excluded)
6. idx.upsert_literature_note(lit_id, embed_text, metadata) → Chroma "literature_notes" collection
7. db.update_chunk_review(status="persisted", literature_note_path=dest_path)
8. Concepts for this chunk: awaiting_review → extracted (eligible for post-approval dedupe)
9. _refresh_literature_index(): rewrite/patch the source's LIT index note's auto-lit-index block with
   a sorted, aliased wikilink list of all approved/persisted chunks; mirror body into StateDB sources.lit_body
```

**Rejection side-effect fan-out (`reject_chunk`):**
```
1. Load chunk; must be awaiting_review
2. Delete the draft file from 00_Inbox/Review/{Citekey}/ (best-effort; OSError logged, not raised)
3. If a literature_id was assigned, best-effort idx.delete_literature_notes([lit_id]) (normally a no-op —
   rejection happens before embedding)
4. db.update_chunk_review(status="rejected", literature_note_path=None)
5. db.update_concepts_status_for_chunk(chunk_id, "rejected") — cascades to all concepts of that chunk
```

**Purge side-effect fan-out (`purge_rejected`, CLI-only, separate from the above):**
```
1. db.get_chunks_by_status("rejected", source_id=...)
2. db.delete_chunks(chunk_ids) — SQLite: DELETE concepts WHERE chunk_id=...; DELETE chunks; drop from FTS
3. idx.delete_chunks(chunk_ids) — Chroma "chunks" collection (harvest/dedupe index)
4. idx.delete_literature_notes(lit_ids) for any literature_id present (normally empty set)
5. If compact and something was removed: db.vacuum() + idx.vacuum() (WAL checkpoint + SQLite VACUUM on
   state.db and chroma.sqlite3); before/after MB sizes captured for the CLI report
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Validation | Only chunks with `status == "awaiting_review"` are approvable/rejectable | review.py:392, review.py:488 |
| Business Logic | Auto-approve threshold: `review_confidence >= literature_review.auto_approve_min_confidence` (default 0.85) | review.py:180, config.py:88 |
| Classification | Confidence banding: `very_low` (0.00–0.40 inclusive), `medium` (0.40 < conf < limiar), `high` (conf >= limiar) | review.py:31, review.py:70-76 |
| Business Logic | Missing/`None` `review_confidence` treated as `0.0` (falls into `very_low`) | review.py:87, review.py:103, review.py:196 |
| Business Logic | Batch "approve all" mode approves only chunks `>= limiar`; sub-threshold chunks remain `awaiting_review` (never silently rejected) | review.py:313-331 |
| UX Constraint | Interactive review table/one-by-one sampling is capped at `literature_review.batch_sample_size` (default 20) drafts per pass | review.py:213, config.py:89 |
| UX Rule | One-by-one prompt default choice is `a` (approve) when `conf >= limiar`, else `p` (skip) — never defaults to reject | review.py:145 |
| Business Logic | Batch rejection is scoped by confidence band (`t`=all, `b`=very_low, `m`=medium, `h`=alta) and requires an explicit `s/n` confirmation before executing | review.py:255-311 |
| Business Logic | Approval moves the draft file, rewrites its frontmatter `status=approved`, and — if the draft file is missing — reconstructs the note from `summary_json` instead of failing | review.py:411-448 |
| Business Logic | The verbatim source excerpt is written into a `zettel:auto-source-excerpt` managed block, separately from the LLM-authored body | review.py:450-451 |
| Business Logic | Only the summary/interpretation is embedded into `literature_notes`, never the raw source excerpt (extractable via managed-block stripping) | review.py:454, review.py:596-602 |
| Business Logic | Approval promotes only concepts currently `awaiting_review` for that chunk to `extracted` (idempotency guard against double-processing) | review.py:475-477 |
| Business Logic | Rejection cascades to ALL concepts for the chunk regardless of their current status | review.py:508 |
| Business Logic | Rejection deletes any already-embedded `literature_notes` entry defensively, even though normal ordering (review precedes embedding) should make this a no-op | review.py:500-505 |
| Business Logic | The per-source literature index's approved-links block only lists chunks with status `approved` or `persisted`, sorted by `chunk_index` | review.py:611-619 |
| Business Logic | Post-approval concept dedupe only considers concepts with status `extracted`, optionally scoped to one `source_id` | review.py:640-642 |
| Business Logic | Dedupe silently skips (does not raise) concepts whose `candidate_json` is missing, empty, or fails `PermanentNoteCandidate` validation | review.py:648-654 |
| Business Logic (external, `extractor.deduplicate_candidates`) | A candidate with no similar existing note, or whose closest existing note is farther than `2*(1-dedupe_threshold)` L2 distance, is auto-approved without an LLM call | extractor.py:550-560 |
| Business Logic (external) | Otherwise an LLM dedupe-decision call classifies CREATE_NEW / IGNORE / REFINE_EXISTING / MERGE; only IGNORE is dropped from `connect` eligibility | extractor.py:585-592 |
| Error Handling | Any exception during a single candidate's LLM dedupe call is swallowed and the candidate is approved (fail-open) rather than lost | extractor.py:580-583 |
| Irreversibility | `purge_rejected` permanently deletes SQLite `chunks`+`concepts` rows and Chroma embeddings for `status=rejected` chunks; no soft-delete or undo | review.py:513-593 |
| Business Logic | `purge_rejected` never touches permanent notes, MOCs, or approved/persisted literature — scope is strictly `status="rejected"` | review.py:532 (docstring), review.py:534 |
| Business Logic | VACUUM (SQLite + Chroma) after purge is opt-out (`compact=True` default) and only runs when at least one row was actually deleted | review.py:574 |
| Business Logic | `low_confidence_only` flag pre-filters the review queue to `review_confidence < limiar` before any interactive/auto path runs | review.py:182-186 |
| Consistency Rule | The web "review" job action bypasses the confidence gate entirely for individual approve/reject calls — whatever chunk_ids the client submits are processed unconditionally | web_app.py:322-328 |

### Detailed breakdown of the business rules

---

### Business Rule: Auto-Approve Confidence Threshold

**Overview**:
A chunk's LLM-produced literature draft carries a `review_confidence` score (set upstream in `extract`). This rule is the single gate that decides whether a draft can be promoted to `20_Literature/` without a human explicitly clicking/typing "approve" for that specific item.

**Detailed description**:
The threshold lives at `cfg.literature_review.auto_approve_min_confidence` (default `0.85`) and is read fresh on every `run_review` call — it is never cached, so changing `config.yaml` between runs immediately changes behavior for chunks still `awaiting_review`. The comparison is a plain `>=`, applied uniformly in four call sites: the non-interactive/`auto_approve` branch of `run_review` (review.py:194-206), the interactive "aprovar todos" (`mode == "a"`) branch (review.py:313-331), `approve_high_confidence` (review.py:366-377, an alternate entry point used elsewhere in the codebase), and implicitly by the one-by-one prompt's default suggestion (`ask_review_decision`, review.py:141-157) which pre-fills `a` when `conf >= limiar` and `p` otherwise — though the human can always override the suggested default.

Critically, failing this threshold is **not** rejection. A chunk below the limiar is simply left in `awaiting_review` (`stats["skipped"] += 1`), so it reappears in the next review session. This is a deliberate three-state design (approved / rejected / still-pending) rather than a binary auto-approve/auto-reject gate — the system never auto-rejects a low-confidence draft on the strength of the score alone; only an explicit human "reprovar" action (or the web client explicitly submitting those chunk_ids for the `reject` action) can reject it. `None`/missing `review_confidence` is coerced to `0.0` via `chunk.get("review_confidence") or 0`, so any chunk lacking a score is treated as maximally low-confidence and never auto-approved.

**Rule workflow**:
```
review_confidence (chunk) ──┐
                             ├─► >= limiar? ──yes──► approve_chunk() ──► status=persisted
literature_review           │
.auto_approve_min_confidence┘
                             └──no──► stats.skipped++ ──► status stays awaiting_review
```

---

### Business Rule: Confidence Banding (very_low / medium / high)

**Overview**:
Independent of the auto-approve limiar, drafts are grouped into three human-facing bands to drive the interactive confidence report and the batch-rejection submenu.

**Detailed description**:
`chunk_confidence_band()` (review.py:70-76) applies two cuts: a fixed `_LOW_CONFIDENCE_MAX = 0.4` (module-level constant, not configurable) below/at which a chunk is `very_low`; and the configurable `limiar` above/equal to which a chunk is `high`; everything strictly between `0.4` and `limiar` is `medium`. Because `_LOW_CONFIDENCE_MAX` is hard-coded rather than derived from config, lowering `auto_approve_min_confidence` below `0.4` in config.yaml would make the `medium` band empty (any `limiar <= 0.4` collapses `medium` to zero width) — an edge case the code does not guard against or warn about.

This banding serves exactly two consumers: `confidence_band_counts()` produces the aggregate `{very_low, medium, high, total}` dict rendered by `format_confidence_report()` in the CLI's cyan/yellow status lines, and shown identically (recomputed with the same 0.4/limiar cuts) as a client-side filter dropdown in the web `/review` page (`web.py:458-462`, `templates/review.html`). `filter_chunks_by_band()` is the only place a band selection is converted back into a concrete chunk subset — used both by the CLI's `d` (reject-by-band) submenu and, indirectly, by the web page's confidence query-param filter. The `BAND_ALL` sentinel (`"all"`) always returns every chunk unfiltered, used for the CLI's "reject all" (`t`) choice.

Boundary semantics are inclusive at the low end and exclusive at the high-mid boundary: a chunk exactly at `0.40` is `very_low`, and a chunk exactly at `limiar` is `high` (not `medium`) — verified explicitly in `test_confidence_band_counts` (tests/test_review.py:163-192) with `0.84` classified `medium` and `0.85` (== limiar) classified `high`.

**Rule workflow**:
```
conf <= 0.40           → very_low
0.40 < conf < limiar   → medium
conf >= limiar         → high
(conf is None → treated as 0.0 → very_low)
```

---

### Business Rule: Interactive Batch Rejection by Confidence Band

**Overview**:
The CLI's `d` (reprovar) menu lets an operator reject an entire confidence tier in one confirmed action, rather than rejecting drafts one at a time.

**Detailed description**:
Selecting `d` re-renders the current confidence-band report (recomputed against the *live* remaining chunk list, since prior actions in the same session may have already changed it), then prompts for a scope via `_REJECT_SCOPE_ALIASES` — accepting either single-letter shortcuts (`t/b/m/h/c`) or full PT-BR words (`todos/baixissima/media/alta/cancelar`), case-insensitively, via `normalize_reject_scope()`. Choosing `c`/`cancelar`, or any unrecognized input outside the allowed `choices=` list (which Rich's `Prompt.ask` itself re-prompts for), aborts the batch and returns to the top-level mode menu without side effects.

Once a scope is chosen, `filter_chunks_by_band()` computes the target set; if it's empty, the operator is told so and returned to the menu (no wasted confirmation prompt). Otherwise a mandatory `s/n` confirmation (`default="n"` — an operator must actively type `s` to proceed, never a bare Enter) gates the actual `reject_chunk()` calls. After execution, the in-memory `chunks` list is filtered to drop the now-rejected ids, the sample and band counts are recomputed, and the loop continues at the top-level menu rather than exiting — allowing multiple successive band-rejections (e.g., reject `very_low`, then separately reject `medium`) within a single `zettel review` invocation, as exercised by `test_run_review_mode_d_reject_band_keeps_others`.

This same band/scope logic is *not* reachable from the web UI: the web page instead lets the human pick individual checkboxes (optionally pre-filtered client-side by band) and always submits explicit `chunk_ids`, so "reject an entire band in one click" is a CLI-only capability of this rule.

**Rule workflow**:
```
mode=d ─► show band counts ─► prompt scope [t/b/m/h/c]
    │                              │
    │                          c/invalid ─► cancel, return to menu
    │
    └─► scope resolved ─► filter_chunks_by_band(chunks, scope, limiar)
             │
             empty? ─► inform, return to menu
             │
             non-empty ─► confirm [s/n] (default n)
                    │
                n ─► cancel, return to menu
                    │
                s ─► reject_chunk() for each target
                     └─► update running chunks list, re-show report, loop to menu
```

---

### Business Rule: One-by-One Review Decision Shortcuts

**Overview**:
The `r` mode walks through each sampled draft individually, accepting either single-letter shortcuts or full PT-BR words for the decision, with a confidence-aware default.

**Detailed description**:
`ask_review_decision()` loops on `Prompt.ask` until `normalize_review_decision()` recognizes the input (via `_DECISION_ALIASES`: `a/aprovar`, `r/rejeitar`, `p/pular`, `q/sair`, case-insensitive and whitespace-trimmed — verified for inputs like `"  r  "` and `"A"` in `test_normalize_review_decision`). Because `Prompt.ask` is itself constrained to the `choices` list, in practice only the recognized keys ever reach `normalize_review_decision`, making the `while True` retry loop mostly a defensive belt for direct/programmatic callers of `ask_review_decision`.

The default suggestion is confidence-aware, not fixed: `default = "a" if conf >= limiar else "p"`. This means a human doing one-by-one review is nudged toward silently accepting the auto-approve recommendation for high-confidence items (just press Enter) but must make an active choice for anything below the limiar (default is "skip", not "approve" or "reject" — the system never nudges toward destructive rejection by default). Choosing `q` (`sair`) breaks out of the one-by-one loop immediately, leaving any un-reviewed drafts in the current sample untouched (`awaiting_review`); it does not abort the whole `run_review` call — dedupe and run-finalization still execute afterward for whatever was decided so far.

Only the first `batch_sample_size` chunks (config default 20) are offered in one-by-one mode per invocation — there is no automatic continuation to a second page within the same `r` loop; the operator must re-run `zettel review` to see the next batch (the newly-approved/rejected chunks are gone, so a re-run naturally surfaces the next slice).

**Rule workflow**:
```
for chunk in sample[:batch_sample_size]:
    display chunk_id, confidence, summary excerpt (300 chars)
    default = "a" if conf>=limiar else "p"
    ask_review_decision() → aprovar | rejeitar | pular | sair
        sair     → break loop (remaining sample untouched)
        aprovar  → approve_chunk()
        rejeitar → reject_chunk()
        pular    → no-op, counted as skipped
```

---

### Business Rule: Draft Promotion and Reconstruction on Approval

**Overview**:
Approving a chunk must produce a persisted, correctly-located Markdown note under `20_Literature/{Citekey}/` even in the edge case where the original draft file has been lost or was never written.

**Detailed description**:
The primary path reads the existing draft (path stored in `chunk.literature_note_path`), parses its frontmatter/body, flips `status` to `"approved"`, refreshes `updated_at`, writes it to the computed destination path (`literature_chunk_filename_for_row`), and deletes the original draft file (best-effort — an `OSError` on unlink is caught and ignored, since the file having already moved/vanished should not fail the approval). This preserves whatever body content the LLM/extract phase actually produced, including any manual edits a reviewer might have made to the draft file before approving.

The fallback path activates when `literature_note_path` is unset or the file no longer exists on disk — a scenario that can arise if the draft was manually deleted outside the pipeline, or if `chunk.literature_note_path` was never populated for some historical/migrated data. In that case the note is *reconstructed from `summary_json`* via `build_literature_chunk_note()`, using whatever `summary`, `key_concepts`, and `candidates` were checkpointed at extract time, plus the chunk's own `text` field as the source excerpt. This means the approved note's *body* can differ between the two paths (e.g., a manually-edited draft's edits are lost if it went missing and had to be rebuilt) — an implicit assumption that `summary_json` is always a faithful, complete backup of what extract produced.

In both paths, after the note file exists at its destination, `safe_update_managed_blocks()` unconditionally (re)writes the `auto-source-excerpt` managed block with the chunk's stored source text — so even a hand-edited draft's excerpt block gets normalized to the DB's copy of the source text at approval time, not whatever a human may have typed into that specific block in the draft.

**Rule workflow**:
```
approve_chunk(chunk_id):
    chunk = db.get_chunk(chunk_id); require status == awaiting_review
    dest = 20_Literature/{Citekey}/{filename}
    if draft_path exists:
        parse draft → patch status=approved, updated_at → write to dest → unlink draft
    else:
        rebuild note from summary_json + chunk.text → write to dest
    overwrite auto-source-excerpt block with chunk.text
    embed (summary-only) into literature_notes
    db: chunk.status = persisted; concepts awaiting_review→extracted
    refresh literature index for the source
```

---

### Business Rule: Source-Excerpt / Embeddable-Text Separation

**Overview**:
The raw source excerpt (potentially copyrighted/verbatim text) is always kept out of the vector embedding sent to the LLM-adjacent `literature_notes` collection; only the LLM's own summary/interpretation is embedded.

**Detailed description**:
`_literature_embed_text()` reads the just-written note file, strips frontmatter, and calls `hashing.extract_embeddable_text()`, which specifically skips any block delimited by `<!-- zettel:auto-*:start/end -->` (the pattern matches `auto-source-excerpt`, `auto-lit-index`, `auto-connections`, `auto-backlinks`, `auto-moc-backrefs` uniformly). This is asserted directly in `test_approve_moves_draft_and_embeds`: the destination file's raw text contains `"texto do chunk"`, but the value returned by `_literature_embed_text()` does not — only `"Um resumo"` (the LLM summary) survives.

This separation exists for two compounding reasons evident from the surrounding architecture: (1) architectural — RAG/connect retrieval should surface a note's *interpretation*, not re-surface the verbatim source text as if it were original synthesis, and (2) practical — embedding large verbatim excerpts wastes embedding-provider tokens/cost and pollutes similarity search with near-duplicate raw text across many notes citing the same source. The separation is enforced structurally (both texts live in the same file, split by a managed-block boundary) rather than by maintaining two separate files, which keeps the note human-readable as a single artifact while still letting the embedding pipeline exclude the excerpt deterministically via regex, without needing a second templating pass.

**Rule workflow**:
```
note file (frontmatter + body):
    body:
       ## Resumo / Conceitos-chave / Candidatos  ← embedded
       <!-- zettel:auto-source-excerpt:start -->
       verbatim source text                       ← excluded from embedding
       <!-- zettel:auto-source-excerpt:end -->
extract_embeddable_text(body) → strips frontmatter + all auto-* blocks
idx.upsert_literature_note(lit_id, stripped_text, metadata)
```

---

### Business Rule: Concept Status Cascade on Approve/Reject

**Overview**:
A chunk's approval or rejection must propagate to every `concepts` row extracted from that chunk, keeping the concept lifecycle (`awaiting_review → extracted → approved/duplicate`, or `→ rejected`) consistent with its parent chunk's fate.

**Detailed description**:
On approval, only concepts *currently* `awaiting_review` are advanced to `extracted` (review.py:475-477) — a defensive idempotency check (`if concept.get("status") == "awaiting_review"`) that prevents re-processing a concept that might already be past this stage (e.g., if `approve_chunk` were somehow invoked twice, or a concept was independently manipulated). `extracted` is not yet `connect`-eligible; it is a holding status consumed by `_dedupe_approved_concepts`, which promotes it further to `approved` (connect-eligible) or `duplicate` (dropped) via `extractor.deduplicate_candidates`.

On rejection, `update_concepts_status_for_chunk()` unconditionally sets **every** concept for that chunk to `rejected`, regardless of prior status — a broader, unconditional cascade compared to approval's status-gated promotion. This asymmetry is intentional: rejection is meant to fully discard a chunk's extracted concepts with no possibility of partial survival, whereas approval's per-concept gate protects against double-promoting concepts that dedupe or a later phase may have already touched.

Because dedupe (`_dedupe_approved_concepts`) is invoked once per `run_review` call (after the whole batch of approvals/rejections in that call), a concept only reaches `approved`/`duplicate` after its sibling chunk's approval has already landed in this same pipeline pass — there is no separate scheduled job that revisits `extracted` concepts left behind by, e.g., a web single-chunk `approve` job that never calls `finalize_approved_concepts` (the web path does call it conditionally: `if action == "approve" and stats["approved"]:`, so this gap is closed for the web surface too).

**Rule workflow**:
```
approve_chunk(chunk_id):
    for concept in concepts_for_chunk:
        if concept.status == "awaiting_review": concept.status = "extracted"

reject_chunk(chunk_id):
    ALL concepts_for_chunk → status = "rejected"   (no status check)

(later, once per run) _dedupe_approved_concepts():
    concepts where status == "extracted" [optionally source_id-scoped]
    → extractor.deduplicate_candidates() → status ∈ {approved, duplicate}
```

---

### Business Rule: Post-Approval Semantic Deduplication

**Overview**:
Before a newly-approved concept becomes eligible for `connect` (Phase 3), it is checked against existing Permanent notes to avoid creating near-duplicate atomic notes.

**Detailed description**:
`_dedupe_approved_concepts()` collects all `extracted` concepts (optionally filtered to one `source_id` for the granular web/CLI single-source flows), deserializes each `candidate_json` into a `PermanentNoteCandidate`, and delegates to `extractor.deduplicate_candidates()`. Rows with missing or unparsable `candidate_json` are silently skipped (`except Exception: continue`) — they remain stuck at `extracted` and never reach `approved`, a state the review component itself has no mechanism to detect or surface (see Technical Debt: no dedicated retry/alert for corrupt `candidate_json`).

The dedupe algorithm itself (external to `review.py` but load-bearing for this rule) is a two-tier check: a cheap vector-distance pre-filter (`idx.query_similar_notes`) skips the LLM entirely when either there are no similar notes at all, or the closest one is farther than `2 * (1 - cfg.linking.dedupe_threshold)` in raw L2 distance — approving the candidate outright as `CREATE_NEW`-equivalent. Only when a genuinely close match exists does an LLM call (`prompts/dedupe_decision.md`) classify the relationship as `CREATE_NEW`, `IGNORE` (candidate dropped, concept marked `duplicate`), or `REFINE_EXISTING`/`MERGE` (candidate kept but tagged with `refines_note_id`/`refine_reason` for `connect` to act on). Any exception during this LLM call is treated as fail-open: the candidate is still approved, on the reasoning that a broken dedupe check should never silently destroy a legitimately reviewed candidate.

This step is a genuine LLM-cost-incurring extension of the review phase — `review.py` triggers it (via `get_llm(cfg)` + `deduplicate_candidates`) but does not gate or throttle it beyond scoping by `source_id`; every `run_review`/web-approve call that approves at least one chunk re-scans and re-dedupes all outstanding `extracted` concepts (not just the ones just approved), which is simple but means dedupe cost scales with the size of the whole backlog, not the increment.

**Rule workflow**:
```
_dedupe_approved_concepts(source_id):
    rows = concepts where status == "extracted" [and source_id == given, if any]
    for row: parse candidate_json → PermanentNoteCandidate (skip on failure)
    deduplicate_candidates(candidates):
        for each candidate:
            similar = idx.query_similar_notes(thesis+definition)
            if none, or closest_distance > 2*(1-dedupe_threshold): approve, no LLM call
            else: LLM dedupe_decision →
                CREATE_NEW            → approve
                IGNORE                → drop (concept → duplicate)
                REFINE_EXISTING/MERGE → approve, tag refines_note_id
    db.update_concept_status(cid, "approved" | "duplicate")
```

---

### Business Rule: Rejected-Chunk Purge is a Separate, Irreversible, CLI-Only Operation

**Overview**:
Rejecting a chunk (`reject_chunk`) is a soft, reversible-in-principle state transition (the row survives with `status="rejected"`); `purge_rejected` is the explicit, separate, hard-delete step that actually reclaims storage.

**Detailed description**:
This two-step design (reject now, purge later/separately) exists so that a rejection decision can be audited or reconsidered (the row and its history remain queryable) before the irreversible cleanup runs. `purge_rejected` only ever targets rows with `status == "rejected"` — it will not touch `awaiting_review`, `approved`, or `persisted` chunks even if requested with a `source_id` filter that includes them; `test_purge_rejected_empty` confirms an `awaiting_review` chunk survives an unscoped purge call untouched.

The deletion itself cascades through three storage layers in a fixed order: SQLite `chunks` (which internally cascades to `concepts` for those chunk_ids and drops FTS rows, via `StateDB.delete_chunks`), the Chroma `chunks` collection (the harvest/dedupe raw-text index — distinct from `literature_notes`), and defensively the Chroma `literature_notes` collection for any `literature_id` present (normally empty, since a `rejected` chunk should never have reached embedding — this is a belt-and-suspenders cleanup, not the expected path). Optional compaction (`compact=True` by default) runs `db.vacuum()` and `idx.vacuum()` — real SQLite `VACUUM` operations reclaiming disk space, not merely logical no-ops — but only when at least one row was actually deleted, avoiding pointless VACUUM churn on empty purges. Before/after file sizes for both `state.db` and `chroma.sqlite3` are captured and returned so the CLI can report reclaimed space; this reporting only happens if `compact=True`, otherwise the size fields are left at `0.0`.

This operation is deliberately **not exposed via the web UI** (per the module docstring at the top of `CLAUDE.md`'s Web UI section, `purge-rejected` is explicitly CLI-only), consistent with its irreversible, low-frequency, maintenance-style nature — the web surface only ever calls `approve_chunk`/`reject_chunk`, never `purge_rejected`.

**Rule workflow**:
```
purge_rejected(source_id?, compact=True):
    rows = chunks where status == "rejected" [and source_id, if given]
    if empty → return zeroed result, no-op
    chunk_ids = [...]; lit_ids = [r.literature_id for r in rows if present]
    removed = db.delete_chunks(chunk_ids)          # SQLite: chunks + concepts + FTS
    idx.delete_chunks(chunk_ids)                    # Chroma "chunks"
    if lit_ids: idx.delete_literature_notes(lit_ids)  # Chroma "literature_notes" (defensive)
    if compact and removed:
        capture before-sizes → db.vacuum(); idx.vacuum() → capture after-sizes
    return {chunks, literature_notes, compacted, state_mb_before/after, chroma_mb_before/after}
```

---

## 4. Component Structure

`review.py` is a single flat module (no sub-package) — it is one of the pipeline "phase" modules that sit alongside `harvester.py`, `extractor.py`, `connector.py`, `gardener.py` at the `zettel/` package root. It has thin integration points in three other files.

```
zettel/
├── review.py                       # THE COMPONENT — Phase 2b: HITL approve/reject
│   ├── chunk_confidence_band()          # pure: classify a confidence float into a band
│   ├── filter_chunks_by_band()          # pure: filter chunk dicts by band
│   ├── confidence_band_counts()         # pure: aggregate band counts + total
│   ├── format_confidence_report()       # pure: PT-BR text rendering of band counts
│   ├── normalize_reject_scope()         # pure: shortcut/word → reject scope
│   ├── normalize_review_decision()      # pure: shortcut/word → decision
│   ├── ask_review_decision()            # I/O: Rich prompt, one-by-one decision
│   ├── run_review()                     # ORCHESTRATOR: CLI/run-all entry point (all modes)
│   ├── approve_high_confidence()        # alternate entry: approve-only sweep, no interactivity
│   ├── finalize_approved_concepts()     # thin wrapper: exposed for web's granular flow
│   ├── approve_chunk()                  # core mutation: draft → 20_Literature + embed + promote
│   ├── reject_chunk()                   # core mutation: discard draft + cascade concepts
│   ├── purge_rejected()                 # maintenance: hard-delete + VACUUM, CLI-only
│   ├── _literature_embed_text()         # helper: strip frontmatter/managed-blocks for embedding
│   ├── _refresh_literature_index()      # helper: rewrite per-source LIT index approved-links block
│   └── _dedupe_approved_concepts()      # helper: bridge to extractor.deduplicate_candidates
│
├── cli.py                          # Typer commands: `review`, `purge-rejected`, and the
│                                    # `run-all` command's Phase 2b block (calls run_review)
├── web_app.py                      # WebWorker._dispatch("review", ...) — granular approve/reject
│                                    # job handler; also calls run_review inside "run_all" dispatch
├── web.py                          # HTTP routes: GET /review (list+filter+paginate),
│                                    # POST /review/action (enqueue approve/reject job)
├── templates/review.html           # Jinja2 template: filters, batch action form, draft cards
│
├── config.py                       # LiteratureReviewConfig: auto_approve_min_confidence,
│                                    # batch_sample_size, drafts_subdir
├── vault.py                        # Note builders/movers consumed by approve/reject:
│                                    # build_literature_chunk_note, build_literature_index_note,
│                                    # literature_chunk_filename_for_row/_wikilink_for_row,
│                                    # literature_index_filename, literature_source_dirname,
│                                    # parse_frontmatter, safe_write_note, safe_update_managed_blocks,
│                                    # compose_note
├── state.py                        # StateDB methods consumed: get_chunks_by_status, get_chunk,
│                                    # update_chunk_review, get_concepts_for_chunk,
│                                    # update_concept_status, update_concepts_status_for_chunk,
│                                    # get_concepts_by_status, get_source, get_chunks_for_source,
│                                    # update_source_texts, delete_chunks, vacuum, start_run
├── index.py                        # VectorIndex methods consumed: upsert_literature_note,
│                                    # delete_literature_notes, delete_chunks, vacuum,
│                                    # query_similar_notes (via extractor)
├── extractor.py                    # deduplicate_candidates() — invoked by _dedupe_approved_concepts
├── hashing.py                      # extract_embeddable_text() — invoked by _literature_embed_text
├── schemas.py                      # PermanentNoteCandidate — deserialized from candidate_json
├── usage.py                        # begin_run/finish_pipeline_run — run-cost bookkeeping wrapper
└── llm.py                          # get_llm() — LLM client used only inside _dedupe_approved_concepts

tests/
└── test_review.py                  # 18 tests covering the pure logic + approve/reject/purge/run_review
```

## 5. Dependency Analysis

```
Internal Dependencies (import-time, module level):
review.py → zettel.config (AppConfig)
review.py → zettel.index (VectorIndex — type hint only)
review.py → zettel.llm (get_llm — used inside _dedupe_approved_concepts)
review.py → zettel.schemas (PermanentNoteCandidate)
review.py → zettel.state (StateDB — type hint only)
review.py → zettel.vault (build_literature_index_note, compose_note,
                           literature_chunk_filename_for_row, literature_chunk_wikilink_for_row,
                           literature_index_filename, literature_source_dirname,
                           parse_frontmatter, safe_update_managed_blocks, safe_write_note)

Internal Dependencies (deferred/local imports, deliberately lazy):
run_review()               → zettel.usage (begin_run, finish_pipeline_run)
run_review()  (interactive)→ rich.console.Console, rich.prompt.Prompt, rich.table.Table
ask_review_decision()      → rich.prompt.Prompt
approve_chunk()            → zettel.vault.build_literature_chunk_note (fallback reconstruction)
_literature_embed_text()   → zettel.hashing.extract_embeddable_text
_dedupe_approved_concepts()→ zettel.extractor.deduplicate_candidates, zettel.llm.get_llm

Callers of review.py (inbound / afferent):
zettel/cli.py       → run_review, purge_rejected                (commands `review`, `purge-rejected`, `run-all`)
zettel/web_app.py   → run_review, approve_chunk, reject_chunk,
                       finalize_approved_concepts                (WebWorker._dispatch: "run_all", "review")

External Dependencies:
- rich (Console, Prompt, Table)            — interactive CLI rendering; lazily imported so non-interactive
                                              paths (web, auto-approve) never require a TTY
- PyYAML (via zettel.vault)                — frontmatter parse/render (transitive, not imported directly)
- SQLite (via zettel.state.StateDB)        — chunks/concepts persistence, FTS5, VACUUM
- ChromaDB (via zettel.index.VectorIndex)  — literature_notes collection embed/delete, chunks delete, VACUUM
- LLM provider (via zettel.llm.get_llm)    — only reached inside dedupe when a close semantic match exists
- Filesystem (pathlib.Path)                — vault note read/write/unlink under 00_Inbox/Review and 20_Literature
```

## 6. Afferent and Efferent Coupling

Coupling measured at function granularity (this is a functions-in-a-module design, not class-based OOP), counting call edges within the repository (test files excluded from the afferent count; import-only references not counted as calls).

| Component (function) | Afferent Coupling | Efferent Coupling | Critical |
|-----------------------|-------------------|--------------------|----------|
| `approve_chunk` | 6 (run_review×3 call sites, approve_high_confidence, web_app.py×2 — run_all + review dispatch) | 7 (get_chunk, get_source, parse_frontmatter, safe_write_note, build_literature_chunk_note, safe_update_managed_blocks, upsert_literature_note, update_chunk_review, get_concepts_for_chunk, update_concept_status, _literature_embed_text, _refresh_literature_index — grouped) | High |
| `reject_chunk` | 4 (run_review×3 call sites, web_app.py review dispatch) | 4 (get_chunk, delete_literature_notes, update_chunk_review, update_concepts_status_for_chunk) | High |
| `run_review` | 3 (cli.review, cli.run_all, web_app._dispatch "run_all") | 9 (get_chunks_by_status, begin_run, finish_pipeline_run, approve_chunk, reject_chunk, ask_review_decision, confidence_band_counts, filter_chunks_by_band, _dedupe_approved_concepts) | High |
| `purge_rejected` | 1 (cli.purge_rejected_cmd) | 5 (get_chunks_by_status, delete_chunks (SQLite), delete_chunks (Chroma), delete_literature_notes, vacuum×2) | Medium |
| `_dedupe_approved_concepts` | 3 (run_review×3 call sites internal to the module) | 3 (get_concepts_by_status, get_llm, deduplicate_candidates) | Medium |
| `finalize_approved_concepts` | 1 (web_app._dispatch "review") | 1 (_dedupe_approved_concepts) | Low |
| `approve_high_confidence` | 0 (not called elsewhere in the analyzed tree — see Technical Debt) | 2 (get_chunks_by_status, approve_chunk) | Low |
| `_refresh_literature_index` | 1 (approve_chunk, internal) | 4 (get_source, literature_chunk_wikilink_for_row, safe_update_managed_blocks/build_literature_index_note+safe_write_note, update_source_texts) | Medium |
| `_literature_embed_text` | 1 (approve_chunk, internal) | 2 (parse_frontmatter, extract_embeddable_text) | Low |
| `chunk_confidence_band` / `confidence_band_counts` / `filter_chunks_by_band` | 3-4 each (run_review interactive branches, web.py's independent reimplementation of the same cuts) | 0 (pure functions) | Low |
| `ask_review_decision` / `normalize_review_decision` | 1-2 each (run_review "r" mode, tested directly) | 0-1 | Low |

`approve_chunk` and `reject_chunk` are the highest-risk nodes: they are the only functions reachable from all three consumption surfaces (CLI, web dispatch, `run-all`) and each touches three storage systems non-atomically.

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| SQLite (`state.db`) via `StateDB` | Internal datastore | chunks/concepts status machine, FTS5 index maintenance | Direct SQL (sqlite3) | Row dicts | Exceptions propagate (no try/except around DB calls in review.py itself); `OSError` on file unlink is caught locally |
| ChromaDB (`chroma.sqlite3` + collections) via `VectorIndex` | Internal datastore | `literature_notes` embed/delete on approve/reject/purge; `chunks` delete on purge | Chroma client API | Documents + metadata dicts (`_sanitize_metadata`d) | `delete_literature_notes` wrapped in bare `except Exception: pass` at both call sites in `reject_chunk`/`purge_rejected` (best-effort, never blocks the primary transition) |
| Vault filesystem (`00_Inbox/Review/`, `20_Literature/`) | Internal filesystem | Draft storage, approved note storage, managed-block excerpt/index updates | Direct file I/O (pathlib) | Markdown + YAML frontmatter | `unlink()` OSError caught and logged (both approve's draft cleanup and reject's draft cleanup); `safe_update_managed_blocks` no-ops with a warning log if the target path does not exist |
| LLM provider (via `zettel.llm.get_llm` / `extractor.deduplicate_candidates`) | External service | Semantic dedupe decision (CREATE_NEW/IGNORE/REFINE/MERGE) for concepts close to an existing note | Provider SDK (LangChain wrapper) | Structured JSON output (`DedupeResult`) | Any exception during the call is caught in `extractor.py` (fail-open: candidate approved, error logged) — review.py does not add its own error handling around this call |
| Rich console (stderr) | Terminal I/O | Interactive prompts/tables for `zettel review` | In-process function calls | Rich renderables / plain strings | N/A (blocking on TTY input; `interactive=False` paths never construct a `Console`) |
| Web job queue (`web_jobs`/`web_job_events` via `WebWorker`) | Internal async boundary | Decouples HTTP request from long-running approve/reject/dedupe work | In-process thread + SQLite queue | JSON-serializable payload/result dicts | Exceptions inside `_dispatch("review", ...)` call `finish_pipeline_run(db, review_run_id, status="failed")` before re-raising, so the run row always reflects failure |

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| State Machine | `chunks.status`: `pending → awaiting_review → {persisted \| rejected}`; `concepts.status`: `awaiting_review → extracted → {approved \| duplicate}` or `→ rejected` | review.py (status transitions throughout), state.py (columns) | Encodes the HITL gate as explicit, queryable status values rather than implicit flags |
| Facade / Orchestrator function | `run_review()` wraps table rendering, band computation, the mode loop, and post-processing behind one call | review.py:160-363 | Single entry point usable identically from CLI and `run-all`, with `interactive`/`auto_approve` toggling behavior |
| Strategy via boolean flags | `auto_approve` / `interactive` combination selects one of three execution strategies (auto-approve sweep, full interactive menu, implicit non-interactive) inside one function rather than three separate functions | review.py:194, review.py:244 | Avoids duplicating the shared setup (chunk loading, threshold resolution, run bookkeeping) across strategies |
| Managed Block / Idempotent Merge | `safe_update_managed_blocks` rewrites only content between `<!-- zettel:X:start/end -->` markers, preserving any manual edits elsewhere in the file | vault.py:142-164, used by approve_chunk (excerpt) and _refresh_literature_index (index links) | Lets pipeline-owned content coexist with human-owned prose in the same Markdown file |
| Alias/Shortcut Normalization Table | `_REJECT_SCOPE_ALIASES`, `_DECISION_ALIASES` map both single-letter and full-word PT-BR inputs to canonical values | review.py:38-67 | Consistent CLI ergonomics; canonical values decouple prompt UI text from internal branching |
| Fail-Open Defensive Deletion | `reject_chunk`'s `idx.delete_literature_notes` call wrapped in bare `except Exception: pass` for an operation expected to normally be a no-op | review.py:502-505 | Prevents a defensive cleanup call from ever blocking the primary state transition (chunk marked rejected) |
| Reconstruction Fallback | `approve_chunk` rebuilds the note from `summary_json` when the draft file is missing, instead of failing the approval | review.py:421-448 | Resilience against external interference with the `00_Inbox/Review/` folder (manual deletion, sync issues) |
| Command dispatch via string keys | `WebWorker._dispatch(operation, payload)` uses `if operation == "review": ...` chains rather than a registry/handler map | web_app.py:220-353 | Consistent with the rest of the pipeline's web dispatch; keeps all operations in one function for easy tracing, at the cost of a growing if/elif chain |

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `approve_chunk` | Vault write, SQLite update, and Chroma embed are three independent operations with no transaction/rollback across them; a crash between steps (e.g., after `safe_write_note` but before `db.update_chunk_review`) leaves the chunk still `awaiting_review` in SQLite while the approved file already exists in `20_Literature/`, and the stale draft may already be unlinked | Re-running review would attempt to re-approve, potentially re-embedding or overwriting the already-moved file; manual reconciliation needed |
| Medium | `reject_chunk` / `purge_rejected` | Chroma `delete_literature_notes` failures are swallowed silently (`except Exception: pass` with no log in `reject_chunk`, a `logger.warning` in `purge_rejected`) — a persistent Chroma outage would leave orphaned embeddings indefinitely with only `purge_rejected`'s path surfacing a warning | Silent data drift between SQLite state and Chroma content; `reject_chunk` gives no operator-visible signal at all |
| Medium | `_dedupe_approved_concepts` | Concepts with unparsable/missing `candidate_json` are silently dropped from the `candidates` list (`except Exception: continue`) and never transition out of `status="extracted"` | Such concepts become permanently invisible to `connect` with no retry mechanism, alert, or CLI surface to detect them (would require an ad hoc SQL query to find) |
| Medium | `chunk_confidence_band` | `_LOW_CONFIDENCE_MAX = 0.4` is a hard-coded module constant, decoupled from the configurable `auto_approve_min_confidence` | If an operator configures `auto_approve_min_confidence <= 0.4`, the `medium` band becomes permanently empty/inverted without any validation or warning at config-load time |
| Medium | Web vs. CLI parity | The web `/review/action` → `WebWorker._dispatch("review", ...)` path calls `approve_chunk`/`reject_chunk` directly on client-submitted `chunk_ids` with **no server-side confidence-threshold re-validation** — the confidence-band filter in `/review` (GET) is purely a client-visible query-string filter, not enforced on the POST | A web client (or a replayed/forged request) can approve a `very_low`-confidence draft that the CLI's `auto_approve` path would never approve; the confidence gate is effectively UI-only on the web surface |
| Low | `approve_high_confidence` | Function has no callers anywhere in the analyzed source tree (cli.py, web_app.py, review.py's own internal calls, tests) | Dead/orphaned code path — either an unused alternate entry point kept for external/future use, or genuinely obsolete |
| Low | Test coverage | No test exercises `WebWorker._dispatch("review", ...)` (web_app.py:304-334) directly, nor the `/review` GET route or `/review/action` POST route in `web.py` beyond a smoke-level page-loads assertion (`test_navigation_and_retry_job_flow`) | The web-specific behavior differences noted above (no threshold re-validation, `confidence_below` payload branch) are entirely untested |
| Low | `run_review` | The interactive loop's `mode == "d"` → confirm → reject flow re-renders the *entire* band report and re-slices `sample` after every batch action, but the `Table` (initial listing) itself is never refreshed — an operator doing multiple passes sees a stale table after the first action | Minor UX inconsistency; does not affect correctness of the underlying data |
| Low | `purge_rejected` | `db.vacuum()`/`idx.vacuum()` require exclusive-ish access and can be slow on large vaults; there is no timeout, progress indicator, or way to run compaction asynchronously from the CLI command that invokes it | Long-blocking CLI call on large vaults; no equivalent operation exists on the (compaction-less) web surface at all |

## 10. Test Coverage Analysis

| Component (function/behavior) | Unit Tests | Integration Tests | Coverage | Test Quality |
|--------------------------------|------------|---------------------|----------|----------------|
| `chunk_confidence_band` / `confidence_band_counts` / `filter_chunks_by_band` / `format_confidence_report` | 1 combined test (`test_confidence_band_counts`, tests/test_review.py:163-192) exercising boundary values (0.4, limiar-1, limiar) | — | Good — covers inclusive/exclusive boundaries explicitly | Assertions check exact counts and band membership; no test for `None`/negative confidence beyond the one `None` entry included in the fixture list |
| `normalize_reject_scope` / `normalize_review_decision` | 2 parametrized tests (11 + 12 cases, tests/test_review.py:195-233) | — | Excellent — covers every alias, case-insensitivity, whitespace, and invalid input | Clear one-assertion-per-case parametrization |
| `ask_review_decision` | 2 tests (default-approve-when-high-conf, shortcut-acceptance) | — | Good for the two branches of the `default` ternary | Uses `patch("rich.prompt.Prompt.ask", ...)` — verifies the `default`/`show_choices` kwargs passed to Rich, not just the return value |
| `approve_chunk` | 1 direct test (`test_approve_moves_draft_and_embeds`) + exercised indirectly by every `run_review`/`purge_rejected` test | Yes — full fixture builds a real `StateDB` + vault tree | Good for the primary (draft-exists) path | Does **not** cover the fallback path where the draft file is missing/deleted and the note is reconstructed from `summary_json` (review.py:421-448) — an untested branch |
| `reject_chunk` | 2 direct tests (deletes draft; refuses when chunk not `awaiting_review`) | Yes | Good | Does not assert on the defensive `idx.delete_literature_notes` call when a `literature_id` is present (fixture's `reject_chunk` test chunk has no assigned lit_id embedded yet) |
| `run_review` — mode `a` (approve-all) | 1 test, threshold-respecting | Yes (full StateDB + fake index) | Good | Confirms sub-threshold chunks stay `awaiting_review`, not silently rejected |
| `run_review` — mode `d` (reject submenu) | 3 tests: confirm-rejects-all, reject-by-band-keeps-others, cancel-returns-to-menu-then-quit | Yes | Good — covers all three sub-branches (all/band/cancel) | Uses `Prompt.ask` `side_effect` sequences to drive the multi-step prompt state machine; verifies remaining-chunk status after multi-round interaction |
| `run_review` — non-interactive auto-approve | 1 test | Yes | Adequate | Only checks approved/skipped counts and one sub-threshold chunk's persisted status; does not test `source_id` scoping or `low_confidence_only` |
| `purge_rejected` | 2 tests (removes rejected + empty no-op) | Yes | Good for the SQLite+Chroma deletion path | `compact=False` used in both tests — the VACUUM/compaction branch (`compact=True`, before/after MB sizes) is **not exercised by any test in this file** (a separate, lower-level `test_state_vacuum_reclaims_freelist` in the same file tests `StateDB.vacuum()` directly, but not through `purge_rejected`'s wrapping logic or `idx.vacuum()`) |
| `finalize_approved_concepts` / `approve_high_confidence` | 0 direct tests | — | None | Both are thin wrappers, but `approve_high_confidence` in particular has no caller and no test — fully unverified |
| `_dedupe_approved_concepts` | 0 direct tests in test_review.py (patched out via `patch("zettel.review._dedupe_approved_concepts")` in most `run_review` tests, or exercised trivially since fixtures leave no `extracted`-status concepts) | — | Weak — this function's own logic (candidate_json parsing failures, source_id scoping) is untested in isolation | The dedupe algorithm it delegates to (`extractor.deduplicate_candidates`) may be covered in `tests/test_extractor.py` (not reviewed as part of this component boundary) |
| `WebWorker._dispatch("review", ...)` (web_app.py:304-334) | 0 dedicated tests | 0 | None | Only reachable indirectly through the unrelated `"run_all"` dispatch test (`test_run_all_dispatches_every_phase_in_order`, tests/test_web_state.py), which mocks `review.run_review` entirely and never touches the `"review"` operation branch (individual approve/reject) |
| `GET /review`, `POST /review/action` (web.py) | 1 smoke test (`test_navigation_and_retry_job_flow`, asserts the page loads and contains "Revisão humana") | 0 | Weak | No test submits `/review/action`, no test verifies the confidence-band client-side filter, pagination, or the `chunk_ids` payload shape sent to the job queue |

**Test file location**: `tests/test_review.py` (432 lines, 18 test functions/parametrized groups). Related but out-of-boundary coverage: `tests/test_web_state.py` (`WebWorker._dispatch` for `run_all`), `tests/test_web.py` (page-load smoke test only).

---

**Component analyzed**: `review` (`zettel/review.py`)
**Report saved to**: `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-review-2026-08-30_10-22-26.md`
