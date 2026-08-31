# Component Deep Analysis Report — `extractor` (zettel/extractor.py)

## 1. Executive Summary

`zettel/extractor.py` implements **Phase 2** of the Zettelkasten pipeline (`harvest → extract → review → connect → garden`). Its sole responsibility is to turn each `pending` text chunk produced by the harvester into a candidate **Literature Note (LIT) draft** by calling an LLM ("Prompt 1", `prompts/literature_note.md`), then checkpointing the result into SQLite so the next phase (`review`) can approve or reject it.

Key characteristics:

- **Stateless orchestration, stateful checkpointing.** The component holds no long-lived state of its own; every decision (chunk status, review confidence, candidate list) is persisted to `StateDB` after each chunk, making the pipeline resumable mid-run.
- **LLM-centric with deterministic caching.** Every LLM call is gated by `compute_llm_call_checksum()` against the SQLite `llm_cache` table, so re-running `extract` on unchanged inputs (same prompt, chunk text, model, temperature, language, and image-context) costs nothing and reproduces the same output.
- **Draft-first, review-gated.** Drafts are written under `00_Inbox/Review/{Citekey}/`, not into the permanent vault tree — nothing is promoted to `20_Literature/` or embedded into `literature_notes` until `zettel review` approves it (`review.approve_chunk`). This is a deliberate separation the whole codebase leans on ("Data flow between phases" in the architecture docs).
- **Own quality gate, not the shared `Retriever`.** `extractor.py` implements two independent filtering layers — a rule-based `_filter_candidates` (relevance/length/anchor checks) and a heuristic `_score_review_confidence` (drives `--auto-approve`) — plus its own semantic-dedupe routine (`deduplicate_candidates`) calibrated on **raw ChromaDB L2 distance**, deliberately not migrated to the RRF/relevance-floor `Retriever` used elsewhere (per project convention, dedupe thresholds are tuned differently from retrieval thresholds).
- **Optional auto-approval.** `--auto-approve` immediately promotes chunks whose heuristic confidence clears `literature_review.auto_approve_min_confidence` via `zettel.review.approve_high_confidence`, short-circuiting the manual review step for high-confidence chunks only.
- **Cost/usage instrumented.** Every LLM call and cache hit is recorded on the active `CostTracker` (`zettel/usage.py`) and rolled up per source into `runs`/`sources` and mirrored to SRC frontmatter.

The component sits between `harvester.py` (produces `pending` chunks) and `review.py` (consumes `awaiting_review` chunks / `approved` concepts), and reuses `assets.py` for multimodal image context, `vault.py` for note construction/writing, and `llm.py` for the LLM call plumbing.

---

## 2. Data Flow Analysis

```
1.  CLI `zettel extract` (cli.py:418) or Web job "extract" (web_app.py:298)
       → run_extract(cfg, db, idx, auto_approve, observer)

2.  run_extract() (extractor.py:56)
    a. Starts a `runs` row (db.start_run("extract")) + CostTracker context
    b. describe_pending_assets() (assets.py) — multimodal image descriptions,
       so image context is available before any chunk LLM call
    c. Loads LLM client (get_llm) and Prompt 1 template (load_prompt_parts)
    d. db.get_pending_chunks() — fetches all chunks with status='pending'
    e. For each chunk (progress-tracked via Rich + ProgressObserver):
         → _process_chunk(...)

3.  _process_chunk() (extractor.py:160) — per chunk:
    a. Build images_context from db.get_assets_for_source() filtered by
       chapter_id / page proximity (_build_images_context)
    b. compute_llm_call_checksum(prompt_hash, chunk_checksum, model,
       temperature, language, images_ctx_checksum)
    c. db.get_cached_llm_response(checksum):
         HIT  → reuse cached response_text (record_cache_hit, $0 cost)
         MISS → fill_template(system/user) → call_llm() → db.cache_llm_response()
                On LLM exception: db.update_chunk_status(chunk_id, "failed"); abort chunk
    d. _parse_literature_output(response_text) — extract_json + Pydantic validation
         On parse failure → one retry with a "fix this JSON" prompt
         On retry failure  → db.update_chunk_status(chunk_id, "failed"); abort chunk
    e. _score_review_confidence(output, cfg) — heuristic confidence score
    f. format_source_locator() backfills cand.source_locator when the LLM
       left it blank/weak
    g. _filter_candidates(output.candidates, cfg) → (approved, rejected)
    h. _write_literature_draft(...) → build_literature_chunk_note() (vault.py)
       → safe_write_note() writes markdown to
         00_Inbox/Review/{Citekey}/LIT - AuthorYear - pNNN - topic-NNNN.md
    i. db.update_chunk_review(chunk_id, status="awaiting_review",
       literature_note_path, literature_id, review_confidence, summary_json,
       llm_prompt1_hash, llm_call_checksum)
    j. For each approved candidate:
         - back-fill relevant_image_ids via asset_ids_in_text() if the LLM
           did not set them
         - _compute_concept_id() — deterministic id from anchor/thesis hash
         - db.upsert_concept(concept_id, ..., status="awaiting_review")
    k. Returns list of candidate dicts (in-memory, for the caller) — NOT
       used to decide correctness; SQLite is the source of truth

4.  run_extract() continuation:
    a. db.update_source_paging(source_id, last_chunk_processed=...)
       (checkpoint, enables resume)
    b. After the loop: cost tracker totals rolled into db.add_source_usage()
       and sync_source_costs_to_vault() (mirrors cost onto SRC frontmatter)
    c. If auto_approve: zettel.review.approve_high_confidence(cfg, db, idx)
         → promotes chunks with review_confidence >= literature_review
           .auto_approve_min_confidence via review.approve_chunk() (moves
           draft to 20_Literature/, embeds into `literature_notes`, and then
           runs deduplicate_candidates() from THIS module against approved
           concepts of that source)
    d. finish_pipeline_run(db, run_id) — closes the `runs` row with cost totals

5.  Deduplication (deduplicate_candidates(), extractor.py:520) — invoked
    later by review.py (`_dedupe_approved_concepts`), not directly by
    run_extract() unless auto-approve triggers it:
    a. For each approved concept: idx.query_similar_notes() against Chroma
       `permanent_notes` collection
    b. Raw L2 distance gate: closest_distance > 2*(1-dedupe_threshold) → auto
       CREATE_NEW (no LLM call)
    c. Otherwise: call_llm() with dedupe_decision.md prompt →
       DedupeResult(decision, target_note_id, reason)
    d. decision routing: CREATE_NEW/REFINE_EXISTING/MERGE → kept (REFINE/MERGE
       annotate refines_note_id); IGNORE → dropped
    e. db.update_concept_status(concept_id, "approved" | "duplicate")
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Workflow gate | Only chunks with `status='pending'` are processed by extract | `extractor.py:86` (`db.get_pending_chunks`) |
| Workflow gate | Drafts are written to `00_Inbox/Review/`, never directly to `20_Literature/` | `extractor.py:392-403`, `config.py:90` (`drafts_subdir`) |
| Workflow gate | Concepts always start `status="awaiting_review"`, never auto-`approved` inside extract | `extractor.py:342-345` |
| Caching / determinism | LLM Prompt 1 responses are cached by a checksum of (prompt, chunk, model, temperature, language, image-context) | `extractor.py:192-201`, `hashing.py:54-64` |
| Validation | Candidates must meet `relevance_score >= extraction.min_relevance_score` (default 3) | `extractor.py:504`, `config.py:79` |
| Validation | Candidates must meet `thesis` word count >= `extraction.min_thesis_words` (default 5) | `extractor.py:506-508`, `config.py:80` |
| Validation | Candidates must meet `definition` word count >= `extraction.min_definition_words` (default 10) | `extractor.py:509-511`, `config.py:82` |
| Validation | Candidates must have a non-empty `anchor_quote` when `extraction.require_anchor_quote=True` (default True) | `extractor.py:512-513`, `config.py:81` |
| Validation | A candidate is unconditionally rejected if the LLM itself marked `chunk_status="rejected"` on that candidate | `extractor.py:498-503` |
| Business logic | Chunks/candidates deemed structural, narrative, promotional, trivial, or fragmented must be rejected by the LLM before any code-level filtering happens | `prompts/literature_note.md` (rejection taxonomy) |
| Business logic | Heuristic `review_confidence` combines summary length, key-concept count, average candidate relevance, and anchor-quote presence into a [0,1] score | `extractor.py:427-445` |
| Business logic | `chunk_status="rejected"` output forces `review_confidence=0.1` regardless of other signals | `extractor.py:429-430` |
| Business logic | A chunk with a valid summary but zero surviving candidates caps confidence at 0.55 (no candidates) or 0.45 (all candidates filtered out) | `extractor.py:436-440` |
| Business logic | Structural locator (`p.X / section`) is back-filled onto a candidate only if the LLM's own locator is missing, starts with `p.?`, or is shorter than 3 chars | `extractor.py:284-287` |
| Business logic | Auto-approval only fires when `auto_approve=True` AND `review_confidence >= literature_review.auto_approve_min_confidence` (default 0.85) | `extractor.py:144-147`, `review.py:366-377` |
| Business logic | Image relevance is a two-step process: the LLM may set `relevant_image_ids`; if it left the list empty, a deterministic fallback (`asset_ids_in_text`) scans the raw chunk text for embedded asset paths | `extractor.py:325-328`, `assets.py:244-253` |
| Business logic | Images are only offered to the LLM as context if they belong to the same `chapter_id` and are within 1 page of the chunk's `page_in_file` | `extractor.py:448-474`, `extractor.py:407-424` |
| Identity / dedup | `concept_id` is deterministic: hashed from `source_id`, `chunk_id`, and either the `anchor_quote` hash (preferred) or the `thesis` hash (fallback) | `extractor.py:606-615` |
| Resilience | A malformed LLM JSON response gets exactly one repair retry (with a corrective prompt and `prompt_cache=False`); a second failure marks the chunk `failed` (not `pending`, not silently dropped) | `extractor.py:248-273` |
| Resilience | Any LLM call exception (not just parse errors) marks the chunk `failed` immediately, without a retry | `extractor.py:242-246` |
| Semantic dedup | Distance gate: a new candidate whose nearest permanent note is farther than `2 * (1 - linking.dedupe_threshold)` (raw Chroma L2) skips the LLM entirely and is approved as distinct | `extractor.py:556-560` |
| Semantic dedup | Only candidates within the distance gate are sent to an LLM dedupe judgment (`dedupe_decision.md`); `CREATE_NEW`/`REFINE_EXISTING`/`MERGE` keep the candidate, `IGNORE` drops it | `extractor.py:585-592` |
| Semantic dedup | If the dedupe LLM call itself fails, the candidate fails open (approved), never silently dropped | `extractor.py:580-583` |
| Semantic dedup | `REFINE_EXISTING`/`MERGE` decisions annotate the candidate with `refines_note_id`/`refine_reason` rather than merging it themselves — actual merging is left to a downstream phase | `extractor.py:589-592` |
| Checkpointing | `last_chunk_processed` is updated on the source after every chunk (success or failure), enabling harvest/extract resumption without reprocessing | `extractor.py:132-135` |

### Detailed breakdown of the business rules

---

### Business Rule: Candidate Quality Filtering (`_filter_candidates` / `_check_candidate`)

**Overview**:
Every atomic concept ("candidate") returned by the LLM must pass four independent, deterministic checks before it is allowed into a draft LIT note or becomes an `awaiting_review` concept. This is the code-level backstop for the extensive selectivity rules the LLM is instructed to follow in `prompts/literature_note.md`.

**Detailed description**:
`_check_candidate()` runs the checks in a fixed order and returns the first failing reason (or `None` if the candidate passes all of them); `_filter_candidates()` partitions the incoming list into `approved`/`rejected` accordingly. The first check simply trusts the LLM's own self-assessment: if the LLM marked an individual candidate's `chunk_status` as `"rejected"` (as opposed to the overall chunk), the candidate is dropped with the LLM's own `rejection_reason`/`rejection_category` logged. This matters because the schema allows the LLM to accept a chunk overall (with a valid summary) while still flagging specific extracted candidates as not meeting the bar — the code has to honor that per-candidate signal, not just the chunk-level one.

The next three checks are purely quantitative and configured via `ExtractionConfig` (`config.yaml`'s `extraction:` section): `min_relevance_score` (default 3 on a 1-5 scale — the same scale documented exhaustively in the prompt, where 1-2 is "trivial/basic" and 3+ is "valid technical concept" or better), `min_thesis_words` (default 5, guarding against a candidate whose thesis is just a topic label rather than a full declarative claim), and `min_definition_words` (default 10, guarding against a definition that merely restates the thesis without elaboration). Finally, `require_anchor_quote` (default `True`) rejects any candidate whose `anchor_quote` is empty or whitespace-only — this enforces that every permanent-note candidate can be traced back to a literal 10-25 word quote in the source text, which is both an auditability requirement and, per the prompt's instructions, evidence that the concept isn't fabricated or paraphrased into inaccuracy.

Because all four checks are independent and code-enforced (not relying on the LLM's cooperation), this rule acts as a hard, config-tunable floor beneath the LLM's own judgment — an operator can loosen or tighten these thresholds without touching the prompt, and the checks are unit-tested in isolation (`tests/test_extractor.py`).

**Rule workflow**:
```
LLM returns PermanentNoteCandidate
  → chunk_status == "rejected"?  → YES → reject (reason: LLM self-rejection)
  → relevance_score < min_relevance_score?      → YES → reject
  → word_count(thesis) < min_thesis_words?       → YES → reject
  → word_count(definition) < min_definition_words? → YES → reject
  → require_anchor_quote AND anchor_quote.strip() == ""? → YES → reject
  → otherwise → approved
```

---

### Business Rule: LLM Response Caching / Determinism (`compute_llm_call_checksum`)

**Overview**:
Every Prompt-1 LLM call is preceded by a deterministic checksum lookup against SQLite's `llm_cache` table; an identical checksum means an identical response is returned without spending money or tokens.

**Detailed description**:
The checksum is computed from six inputs concatenated and hashed: the prompt template's own hash (`prompt_hash`, from `sha256_hex(prompt_parts.full_template)`), the chunk's content checksum (`chunk_checksum`, computed upstream during harvest/chunking), the configured LLM `model` and `temperature`, the target `language` (PT-BR by default), and — specific to this component — an `images_ctx_checksum` derived from the same `_build_images_context()` string that will be injected into the prompt. Including the image-context checksum is important: if new images get described (or existing descriptions change) between two `extract` runs, the same chunk with a changed image context must NOT hit a stale cache entry, since the LLM's answer legitimately depends on what images it was told about.

This rule interacts directly with the "checkpoint after every chunk" behavior: because `db.update_chunk_review()` persists `llm_call_checksum` alongside the resulting draft, a partially-completed `extract` run (interrupted by a crash, Ctrl-C, or a rate limit abort) can be safely re-invoked — chunks whose checksum is unchanged and already cached will not re-call the LLM, and chunks that were never reached will still show `status='pending'`. Cache hits are explicitly logged as \$0 cost via `record_cache_hit()`, distinguishing them in the `CostTracker` from genuine paid calls, which feeds into the `runs`/`sources` cost reporting used by `zettel status`.

A secondary consequence of this rule is prompt evolution safety: because `prompt_hash` is part of the checksum, editing `prompts/literature_note.md` automatically invalidates every previously cached response for every chunk — there is no manual cache-busting step required when the prompt changes.

**Rule workflow**:
```
checksum = sha256(prompt_hash | chunk_checksum | model | temperature | language | images_ctx_checksum)
db.get_cached_llm_response(checksum)
  → HIT:  reuse response_text; record_cache_hit(); no LLM call, $0 cost
  → MISS: call_llm(system, user); db.cache_llm_response(checksum, request, response)
```

---

### Business Rule: Heuristic Review Confidence Scoring (`_score_review_confidence`)

**Overview**:
Each processed chunk is assigned a `review_confidence` float in `[0, 1]`, computed independently of the LLM's own text, that quantifies how "safe" the extraction is likely to be for unattended (auto-)approval.

**Detailed description**:
The score starts from a base of `0.4` and is adjusted upward by four signals, each capped so no single signal can dominate. A summary of at least 20 words adds `0.15` (a very short summary suggests the LLM engaged shallowly with the chunk). The presence of `key_concepts` adds up to `0.15`, scaled at `0.05` per concept — rewarding chunks where the LLM identified multiple distinct ideas rather than none. If the chunk was rejected outright (`chunk_status == "rejected"`), the function short-circuits and returns a fixed `0.1`, regardless of any other field, because a rejected chunk should never accidentally qualify for auto-approval. If there are no candidates at all, the score is capped at `0.55` — permissive enough for interactive review "looks fine, nothing to approve" workflows, but not high enough to clear the default 0.85 auto-approve limiar. If candidates existed but all were filtered out by `_filter_candidates`, the cap drops further to `0.45`, treating "candidates existed but none survived quality gates" as a stronger warning sign than "no candidates were proposed at all".

When at least one candidate survives filtering, two additional signals are added: up to `0.1` scaled by the average `relevance_score` of the surviving candidates (divided by the max possible score of 5), and up to `0.2` scaled by the fraction of surviving candidates that have a non-empty `anchor_quote`. The final score is clamped to `[0, 1]` and rounded to 3 decimals. This design deliberately weights the presence of anchor quotes (`0.2` max) more heavily than raw relevance (`0.1` max) — anchor-quote presence is treated as a stronger signal of extraction fidelity (i.e., "the LLM is not hallucinating") than a self-reported relevance number.

This heuristic is the sole gate for `--auto-approve` (both the CLI flag on `extract` and the config-level `literature_review.auto_approve_min_confidence`), meaning its calibration directly controls how much manual review workload the pipeline can skip. It is not used for anything else (it does not affect vault content, only the `status`/routing decision).

**Rule workflow**:
```
if chunk_status == "rejected": return 0.1
score = 0.4
score += 0.15 if len(summary.split()) >= 20 else 0
score += min(0.15, 0.05 * len(key_concepts))
if no candidates: return min(score, 0.55)
approved, _ = filter_candidates(candidates)
if no approved: return min(score, 0.45)
score += 0.1 * (avg(relevance_score for approved) / 5.0)
score += 0.2 * (fraction of approved with non-empty anchor_quote)
return round(clamp(score, 0, 1), 3)
```

---

### Business Rule: Image Context Injection and Image-to-Candidate Attribution

**Overview**:
Extraction is multimodal-aware: the LLM is told which described images are near the current chunk, may cite them per-candidate, and a deterministic fallback fills in image references the LLM missed.

**Detailed description**:
Before the Prompt-1 call, `_build_images_context()` selects assets belonging to the same `chapter_id` as the chunk and within one page (`abs(page_in_file - page) <= 1`) of the chunk's page, then renders each as a bullet with its `asset_id` and stored description (populated earlier by `describe_pending_assets()`, itself gated by `cfg.images.enabled`). This same filtering logic is duplicated (not shared) between `_build_images_context()` (for the prompt string) and `_images_for_chunk()` (for the images actually embedded into the written LIT note body) — both apply chapter-id and page-proximity gates independently, meaning a change to one filter's threshold does not automatically apply to the other.

The prompt instructs the LLM, when a figure is essential to understanding a candidate (a mechanism diagram, pipeline, or data model), to populate that candidate's `relevant_image_ids` with the corresponding `asset_id` — and conversely to derive a candidate directly from a figure's description when the chunk's own text is too thin (code listing, transition text) to justify one on its own; such a chunk should not be auto-rejected as "fragmented" if an accompanying figure carries a genuine concept. After parsing, if a given candidate's `relevant_image_ids` is empty, `asset_ids_in_text()` runs a deterministic string-containment fallback: it checks whether any of the source's known asset paths literally appear inside the raw chunk text, and if so attaches those asset_ids. This fallback only fires per-candidate when the LLM field is empty — it never overrides an LLM-provided list, even a partial one.

The combined effect is a two-tier attribution system: LLM judgment is primary and can point at semantically-relevant-but-not-textually-embedded images (e.g., described but only loosely near the text), while the deterministic fallback guarantees that at minimum, any image whose Markdown embed literally sits inside the chunk's text is not silently dropped from a candidate's image references even if the LLM failed to mention it.

**Rule workflow**:
```
assets = get_assets_for_source(source_id)
context_assets = [a for a in assets if same_chapter(a) and abs(page(a) - chunk.page) <= 1]
images_context = render(context_assets)  # sent to LLM
...
for candidate in approved_candidates:
    if not candidate.relevant_image_ids:
        candidate.relevant_image_ids = [a.asset_id for a in assets if a.path in chunk_text]
```

---

### Business Rule: Semantic Deduplication Against Permanent Notes (`deduplicate_candidates`)

**Overview**:
Before an approved concept becomes a real Permanent Note downstream, `extractor.py` provides a semantic-similarity gate against existing `permanent_notes` in Chroma, using a two-stage design (cheap distance filter, then LLM judgment only when needed).

**Detailed description**:
For each candidate, a query string (`thesis + " " + definition`) is embedded and compared against the `permanent_notes` Chroma collection via `idx.query_similar_notes()`, retrieving up to `linking.topk` nearest notes. If there are no existing notes at all, or if the closest match's raw L2 distance exceeds `2 * (1 - linking.dedupe_threshold)`, the candidate is approved with no LLM call — this is a deliberate design choice documented in the codebase's architecture notes: extractor/harvester dedupe thresholds are calibrated on **raw L2 distance**, not on the RRF-fused, floor-gated scores the `Retriever` uses elsewhere, because these thresholds were tuned empirically for a different purpose (semantic redundancy detection, not query relevance).

When the distance gate is not cleared (i.e., an existing note is suspiciously close), the candidate and its nearest neighbors are sent to an LLM with the `dedupe_decision.md` prompt, which must return one of four decisions: `create_new` (sufficiently distinct despite the proximity), `ignore` (identical or trivially redundant — drop it), `refine_existing` (the candidate adds nuance to an existing note — keep it but tag it with `refines_note_id`), or `merge` (similar handling to refine). Both `refine_existing` and `merge` are treated identically in code — the candidate survives, annotated with `refines_note_id`/`refine_reason` for the downstream connector phase to act on; the actual merge/refine mechanics are not implemented in this module. If the LLM call itself throws, the candidate fails open (approved) rather than being silently lost, on the theory that a failed dedupe check should never destroy a candidate that might be legitimate.

Finally, regardless of path, every candidate that went through the function (approved or not) has its `concepts` row status updated: `"approved"` if it survived, `"duplicate"` otherwise. This function is invoked by `review.py`'s `_dedupe_approved_concepts` after chunk approval (both the manual `review` flow and `run_extract`'s own `--auto-approve` path go through `approve_chunk` → `_dedupe_approved_concepts` → `deduplicate_candidates`), meaning deduplication always happens strictly after a chunk is approved, never before or during the initial draft-writing stage of `_process_chunk`.

**Rule workflow**:
```
for candidate in approved_concepts:
    similar = query_similar_notes(f"{thesis} {definition}", topk)
    if not similar or closest_distance > 2*(1 - dedupe_threshold):
        decision = CREATE_NEW   # no LLM call
    else:
        decision = call_llm(dedupe_decision_prompt) → DedupeResult
    if decision in (CREATE_NEW, REFINE_EXISTING, MERGE): keep candidate
    if decision == IGNORE: drop candidate
    update_concept_status(concept_id, "approved" if kept else "duplicate")
```

---

### Business Rule: Failure Isolation and Retry (Parse Errors vs. LLM Errors)

**Overview**:
The component distinguishes between two failure classes — transport/provider failures and malformed-response failures — applying a different retry policy to each, but converging on the same terminal state (`status="failed"`) so no chunk is ever silently lost or left ambiguously `pending`.

**Detailed description**:
If `call_llm()` itself raises (network error, provider error, rate limit exhaustion not otherwise handled, etc.), the chunk is immediately marked `failed` via `db.update_chunk_status()` with **no retry** inside `_process_chunk` — the reasoning being that transport failures are typically better retried at the run level (re-running `zettel extract`, which will pick the chunk up again if it were still `pending`; note, however, that marking it `failed` here means it will NOT be picked up by `get_pending_chunks()` on a subsequent run without an explicit `retry_chunks` operation, which is one of the web app's exposed operations). By contrast, if the LLM call succeeds but `_parse_literature_output()` fails (the response is not valid JSON matching `LiteratureChunkOutput`), the code makes exactly one repair attempt: it re-sends the malformed text back to the LLM with an explicit "fix this JSON" instruction, using `prompt_cache=False` (since this is a one-off repair, not a stable reusable prompt) and a distinct `label` (`extract-retry:{chunk_id}`) for cost-tracking purposes. Only if this second attempt also fails to parse does the chunk get marked `failed`.

Both failure branches call `clear_progress()` before returning, ensuring the shared progress-tracking context (used for cost/attribution logging elsewheer in `usage.py`) doesn't leak stale step/total state into subsequent chunks. Both also return `([], None)` — an empty candidate list and no output — signaling to the caller (`run_extract`'s loop) that this chunk contributed nothing, but without raising, so the loop continues to the next pending chunk rather than aborting the whole `extract` run.

**Rule workflow**:
```
try: response = call_llm(...)
except: update_chunk_status(failed); return ([], None)

try: output = parse(response)
except:
    try:
        response = call_llm(repair_prompt)
        output = parse(response)
    except:
        update_chunk_status(failed); return ([], None)
```

---

## 4. Component Structure

```
zettel/
├── extractor.py                  # THIS COMPONENT — Phase 2 orchestration
│   ├── run_extract()             # public entry point (CLI + web)
│   ├── _process_chunk()          # per-chunk LLM call, parse, filter, persist
│   ├── _write_literature_draft() # builds + writes the draft LIT markdown file
│   ├── _images_for_chunk()       # asset selection for embedding in the note body
│   ├── _score_review_confidence()# heuristic confidence for auto-approve
│   ├── _build_images_context()   # asset selection for the LLM prompt
│   ├── _filter_candidates()      # code-level quality gate (relevance/length/anchor)
│   ├── _check_candidate()        # single-candidate rule evaluation
│   ├── deduplicate_candidates()  # semantic dedupe vs. permanent_notes (used by review.py)
│   ├── _compute_concept_id()     # deterministic concept id (anchor/thesis hash)
│   ├── _parse_literature_output()# JSON extraction + Pydantic validation (Prompt 1)
│   ├── _parse_dedupe_result()    # JSON extraction + Pydantic validation (dedupe prompt)
│   └── _format_existing_notes()  # renders similar-notes block for dedupe prompt
│
├── schemas.py                    # LiteratureChunkOutput, PermanentNoteCandidate,
│                                  # DedupeDecision, DedupeResult (Pydantic contracts)
├── config.py                     # ExtractionConfig, LiteratureReviewConfig, LinkingConfig
├── hashing.py                    # compute_llm_call_checksum, normalize_text_for_hash,
│                                  # sha256_hex, short_hash
├── llm.py                        # get_llm, call_llm, load_prompt_parts, fill_template,
│                                  # extract_json (shared LLM plumbing, not extractor-specific)
├── paging.py                     # format_source_locator (shared with connector/vault)
├── vault.py                      # build_literature_chunk_note, literature_chunk_filename,
│                                  # literature_source_dirname, safe_write_note
├── state.py                      # StateDB — chunks/concepts/llm_cache/runs persistence
├── index.py                      # VectorIndex — query_similar_notes (Chroma permanent_notes)
├── assets.py                     # describe_pending_assets, asset_ids_in_text (image pipeline)
├── review.py                     # DOWNSTREAM consumer: approve_chunk, approve_high_confidence,
│                                  # _dedupe_approved_concepts (calls extractor.deduplicate_candidates)
├── cli.py                        # `zettel extract` command wiring (+ run-all)
└── web_app.py                    # web job dispatch for "extract" operation

prompts/
├── literature_note.md            # Prompt 1 — system instructions + {placeholders}
└── dedupe_decision.md            # Dedupe prompt — system instructions + {placeholders}

tests/
├── test_extractor.py             # _filter_candidates unit tests (candidate quality gate)
├── test_extraction_dump.py       # tests a DIFFERENT module (extraction_dump.py / harvester
│                                  # dump-extraction feature) — name overlap only, not this component
└── test_web_state.py             # web_app run-all dispatch order (mocks extractor.run_extract)
```

---

## 5. Dependency Analysis

```
Internal Dependencies (extractor.py imports):
  zettel.config      → AppConfig (ExtractionConfig, LiteratureReviewConfig, LinkingConfig, LLMConfig)
  zettel.hashing     → compute_llm_call_checksum, normalize_text_for_hash, sha256_hex, short_hash
  zettel.index       → VectorIndex (type-only; used for idx.query_similar_notes in dedupe)
  zettel.llm         → PromptParts, call_llm, extract_json, fill_template, get_llm, load_prompt_parts
  zettel.paging      → format_source_locator
  zettel.schemas     → DedupeDecision, DedupeResult, LiteratureChunkOutput, PermanentNoteCandidate
  zettel.state       → StateDB (type-only; all persistence goes through its methods)
  zettel.vault       → build_literature_chunk_note, literature_chunk_filename,
                        literature_source_dirname, safe_write_note
  zettel.assets      → describe_pending_assets, asset_ids_in_text (imported lazily, inside functions)
  zettel.usage       → begin_run, finish_pipeline_run, get_tracker, set_source,
                        clear_progress, set_progress, record_cache_hit (lazy imports)
  zettel.progress    → report() (lazy import)
  zettel.review      → approve_high_confidence (lazy import, only when auto_approve=True)
  ulid.ULID          → literature_id generation
  rich.progress      → Progress/SpinnerColumn/etc. (CLI progress bar)

Internal Dependents (who imports extractor.py):
  zettel.cli         → run_extract (extract command, run-all command)
  zettel.web_app     → run_extract (web "extract" job, run-all dispatch)
  zettel.review      → deduplicate_candidates (via _dedupe_approved_concepts, aliased
                        _deduplicate_candidates for backwards compatibility)

External Dependencies:
  - Pydantic (v2)      — schema validation for LLM structured outputs
  - LangChain (via llm.py) — LLM client abstraction (OpenAI/Anthropic/Gemini/Ollama/compatible)
  - python-ulid        — ULID generation for literature_id
  - Rich               — CLI progress bars/spinners
  - ChromaDB (via index.py) — permanent_notes vector similarity search (dedupe)
  - SQLite (via state.py)   — chunks/concepts/llm_cache/runs persistence, WAL mode
```

---

## 6. Afferent and Efferent Coupling

Analyzed at the function/class level within `extractor.py` (Python module — no classes are defined in this file; coupling is measured per top-level function plus the module's Pydantic schema dependencies).

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| `run_extract()` | 3 (cli.py, web_app.py — 2 call sites) | 9 (StateDB, VectorIndex, llm.get_llm/load_prompt_parts, assets.describe_pending_assets, usage.*, vault.sync_source_costs_to_vault, review.approve_high_confidence, `_process_chunk`) | High |
| `_process_chunk()` | 1 (run_extract) | 11 (hashing.*, llm.call_llm/fill_template, paging.format_source_locator, vault.safe helpers via `_write_literature_draft`, `_parse_literature_output`, `_score_review_confidence`, `_filter_candidates`, `_compute_concept_id`, assets.asset_ids_in_text, StateDB 4 methods) | High |
| `_filter_candidates()` / `_check_candidate()` | 3 (`_process_chunk`, `_score_review_confidence`, `tests/test_extractor.py`) | 1 (ExtractionConfig fields) | Medium |
| `deduplicate_candidates()` | 1 (`review.py::_dedupe_approved_concepts`) | 6 (VectorIndex.query_similar_notes, llm.load_prompt_parts/fill_template/call_llm, schemas.DedupeResult/DedupeDecision, StateDB.update_concept_status) | High |
| `_write_literature_draft()` | 1 (`_process_chunk`) | 4 (vault.build_literature_chunk_note, literature_source_dirname, literature_chunk_filename, safe_write_note) | Medium |
| `_score_review_confidence()` | 1 (`_process_chunk`) | 1 (`_filter_candidates`) | Medium |
| `_compute_concept_id()` | 1 (`_process_chunk`) | 1 (hashing.sha256_hex/short_hash/normalize_text_for_hash) | Low |
| `_build_images_context()` / `_images_for_chunk()` | 1 each (`_process_chunk` / `_write_literature_draft`) | 1 (StateDB.get_assets_for_source) | Low |
| `LiteratureChunkOutput` / `PermanentNoteCandidate` (schemas.py) | High — every LLM response parsed here, also consumed by `vault.py`, `review.py` | 0 (leaf Pydantic models) | High (schema drift breaks parsing across 3+ modules) |

**Notes**: `run_extract()` and `_process_chunk()` show the highest efferent coupling because they act as the orchestration seams of the phase — this is expected for a pipeline-stage entry point, but it also means any change to `StateDB`'s chunk/concept schema, `vault.py`'s note-builder signature, or `usage.py`'s tracker API has a high blast radius through these two functions. `deduplicate_candidates()` is architecturally distinct (invoked from `review.py`, not from `run_extract()`'s main loop) yet still lives in this module — a structural note rather than a defect, reflecting the documented design choice that dedupe logic and its L2-distance calibration belong with extraction rather than with `review.py` or the shared `Retriever`.

---

## 7. Endpoints

Not applicable — `extractor.py` exposes no network endpoints (REST/GraphQL/gRPC). It is invoked in-process by two synchronous interfaces:

| Interface | Trigger | Entry Point |
|-----------|---------|--------------|
| CLI | `zettel extract [--auto-approve] [--yes]` | `cli.py::extract()` → `run_extract()` |
| CLI | `zettel run-all` (phase 2 of the full pipeline) | `cli.py` (~line 1301) → `run_extract(cfg, db, idx, auto_approve=False)` |
| Web (background job) | Dashboard/Pipeline page enqueues an `"extract"` job | `web_app.py:298-302` → `run_extract(cfg, db, idx, auto_approve=False, observer=progress)` |
| Web (background job) | `"run_all"` job, phase 2/5 | `web_app.py:232-247` → same call, with progress checkpoints |

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| LLM provider (OpenAI/Anthropic/Gemini/Ollama/compatible) | External Service | Prompt 1 extraction + JSON-repair retry + dedupe decisions | HTTPS (via LangChain client) | JSON-in-text (parsed via `extract_json`) | One JSON-repair retry on parse failure; chunk marked `failed` on exhausted retries or transport exception; SQLite `llm_cache` short-circuits repeat calls |
| SQLite (`state.db`, via `StateDB`) | Embedded Database | Chunk/concept status tracking, LLM response cache, run/cost accounting | In-process SQL (WAL mode) | Rows (dict-like) | Explicit `.commit()` per write; no transactions spanning multiple chunks — a crash mid-chunk leaves at most one chunk in an inconsistent (still-`pending` or already-`failed`) state, never corrupts prior chunks |
| ChromaDB `permanent_notes` collection (via `VectorIndex`) | Embedded Vector DB | Semantic similarity lookup for candidate deduplication | In-process (Chroma client) | Embeddings + metadata dict | Distance-gate short-circuits most lookups without an LLM call; LLM-call failures during dedupe fail open (candidate approved) |
| Vault filesystem (`00_Inbox/Review/{Citekey}/`) | Local File I/O | Persist LIT draft markdown files | Filesystem (UTF-8 text) | YAML frontmatter + Markdown body, managed `zettel:auto-source-excerpt` block | `safe_write_note()` creates parent dirs; no atomic-write/rollback — a crash mid-write could leave a partial file, though the SQLite checkpoint (`update_chunk_review`) only commits after the write returns |
| `zettel.assets` (image description pipeline) | Internal Module | Supplies multimodal image context/descriptions for the prompt and note body | In-process function calls | dict rows (asset_id, path, description, page_in_file, chapter_id) | Gated entirely by `cfg.images.enabled`; assets pipeline manages its own rate-limit/retry policy independently of extractor |
| `zettel.review` (auto-approve path) | Internal Module | Immediately promotes high-confidence chunks past manual review | In-process function call | N/A | Failures inside `approve_high_confidence`/`approve_chunk` are not caught by `run_extract` — an exception there would propagate up through the CLI/web job |
| `zettel.usage` (CostTracker) | Internal Module | Aggregates LLM/embedding cost and token usage per run/source | In-process contextvars | dict summaries | Best-effort; tracker absence (`if tracker:`) is tolerated |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|------------------|----------|---------|
| Pipeline / Phase-checkpoint | `run_extract()` iterates chunks, checkpointing status to SQLite after every unit of work | `extractor.py:102-135` | Resumable, crash-safe batch processing without external job orchestration |
| Cache-Aside | `db.get_cached_llm_response()` / `db.cache_llm_response()` around every LLM call | `extractor.py:196-237` | Deterministic, cost-free re-runs on unchanged inputs |
| Strategy-via-Config | `ExtractionConfig` fields parameterize `_check_candidate()`'s rules without code changes | `extractor.py:497-514`, `config.py:78-82` | Tunable quality bar per deployment/corpus without redeploying code |
| Fail-Open vs Fail-Closed (asymmetric error handling) | Transport/parse errors on the *primary* extraction call fail **closed** (`status=failed`); dedupe LLM errors fail **open** (candidate approved) | `extractor.py:242-246`, `extractor.py:580-583` | Protects against silently losing content in the primary path while avoiding false-positive duplicate suppression in the secondary path |
| Two-Tier Filter (cheap-then-expensive) | Distance-gate before LLM dedupe call | `extractor.py:556-560` | Minimizes LLM spend — most candidates are obviously novel and never need a judgment call |
| Deterministic ID derivation | `_compute_concept_id()` hashes `(source_id, chunk_id, anchor|thesis)` instead of using a random UUID | `extractor.py:606-615` | Idempotent re-extraction of the same chunk maps to the same `concept_id`, avoiding duplicate concept rows on reprocessing |
| Draft/Publish separation | Drafts live under `00_Inbox/Review/`; only `review.py::approve_chunk` moves them to `20_Literature/` | `extractor.py:392-403` vs `review.py:387-420` | Human-in-the-loop gate before content is considered part of the permanent vault |
| Observer / Progress reporting | Optional `observer` parameter threaded through to `zettel.progress.report()` | `extractor.py:56-154` | Same code path serves both a Rich CLI progress bar and web-job progress events without branching logic |
| Backwards-compatible alias | `_deduplicate_candidates = deduplicate_candidates` | `extractor.py:603` | Preserves an old private-name import path for callers written against the pre-rename API |

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|------------------|-------|--------|
| Medium | `_process_chunk()` primary LLM failure path | An LLM/transport exception marks the chunk `failed` with zero retries (unlike the JSON-parse path, which gets one retry) | Transient network/rate-limit errors permanently stall a chunk until an operator runs the separate `retry_chunks` operation; no distinction is made between a transient error (worth retrying) and a permanent one (bad prompt/model) |
| Medium | `deduplicate_candidates()` / raw L2 distance calibration | Threshold (`linking.dedupe_threshold`) is explicitly documented as tuned to the current corpus/embedding model and deliberately not using the shared `Retriever`'s relevance-floor machinery | Any change to the embedding model/provider silently invalidates the calibration (distance scale can shift), and there is no automated check that would catch a miscalibrated threshold — could silently over- or under-merge concepts |
| Medium | `_write_literature_draft()` file write | `safe_write_note()` is a plain `path.write_text()`, not an atomic write (no temp-file+rename) | A crash or forced kill during the write could leave a truncated/corrupt draft markdown file, though the SQLite checkpoint only records success after the call returns, so the inconsistency is at least detectable (file present but chunk still shows an earlier status until the next successful run) |
| Low-Medium | Duplicated image-selection filters | `_build_images_context()` and `_images_for_chunk()` independently reimplement the same chapter/page-proximity filtering logic | Changing the proximity window (currently hardcoded `<= 1` page) in one function without the other creates a silent mismatch between what the LLM was told and what images are actually embedded in the note |
| Low | `run_extract()` exception handling around `approve_high_confidence` | No try/except around the auto-approve call; an exception there aborts the whole `extract` run after all chunks were already successfully checkpointed | A single bad auto-approval (e.g., a vault write failure) can make the CLI/web job report `extract` as failed even though every chunk was correctly processed and persisted |
| Low | Hardcoded magic numbers in `_score_review_confidence()` | Weights (0.4 base, 0.15/0.15/0.1/0.2 caps) are inline literals, not configuration | Recalibrating the confidence heuristic requires a code change and redeploy, unlike the `ExtractionConfig` thresholds which are YAML-tunable |
| Low | Config default drift | `LinkingConfig.dedupe_threshold` Pydantic default is `0.90` (`config.py:64`) but the operational `config/config.yaml` sets `0.85` (`config.yaml:61`) | Not a bug (YAML always wins per `load_config`), but a maintainer reading only `config.py` would see a different effective threshold than what's actually running — a documentation/consistency risk |
| Low | Test file naming | `tests/test_extraction_dump.py` sounds like it might cover `extractor.py` but actually tests an unrelated module (`zettel/extraction_dump.py`, part of the harvester's dump feature) | Discoverability risk: someone searching for "extractor tests" may believe coverage is broader than it is |

---

## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|---------------------|----------|----------------|
| `_filter_candidates` / `_check_candidate` | 6 (`tests/test_extractor.py`) | 0 | Good for this function in isolation | Clear, well-named tests covering each rejection reason (relevance, thesis length, definition length, anchor-required, anchor-not-required) plus a "all pass" happy path; uses a `_make_candidate`/`_make_config` builder pattern for readability |
| `run_extract()` / `_process_chunk()` | 0 direct | 1 indirect (`tests/test_web_state.py::test_run_all_dispatches_every_phase_in_order` — monkeypatches `extractor.run_extract` entirely, asserting only call order/kwargs, not internal behavior) | **None** for the actual orchestration, LLM-call flow, caching, or draft-writing logic | The only test touching `run_extract` replaces it with a stub — it verifies wiring (phase ordering in `run-all`), not extractor correctness |
| `_score_review_confidence` | 0 | 0 | **None** | No test locks in the heuristic's weighting/behavior; a future refactor could silently change auto-approve behavior with no test failure |
| `deduplicate_candidates` / `_parse_dedupe_result` | 0 | 0 | **None** | No test exercises the distance-gate threshold, the LLM-fail-open path, or the `IGNORE`/`REFINE_EXISTING`/`MERGE` routing |
| `_compute_concept_id` | 0 | 0 | **None** | No test verifies determinism/idempotency of concept IDs across re-extraction of the same chunk |
| `_build_images_context` / `_images_for_chunk` | 0 | 0 | **None** | No test covers the chapter/page-proximity filtering or the divergence risk noted in Technical Debt |
| `_write_literature_draft` / draft file naming | 0 direct | Indirect only, via `vault.py`'s own tests (not located during this analysis) for `build_literature_chunk_note`/`literature_chunk_filename` | Unknown — outside this component's boundary | Not assessed here; would require reviewing `tests/test_vault.py` if present |
| `_parse_literature_output` (JSON parsing + retry) | 0 | 0 | **None** | No test simulates a malformed-JSON response to verify the one-retry-then-fail behavior |
| Caching (`compute_llm_call_checksum` usage) | 0 in this file | Indirect coverage possible via `hashing.py`'s own tests (`tests/test_hashing.py`, referenced in CLAUDE.md build commands) for the checksum function itself, but not for extractor's cache-hit/miss branching | Partial (checksum function only) | The cache-hit code path (`record_cache_hit`, skipping `call_llm`) inside `_process_chunk` has no test |

**Summary**: Test coverage for `extractor.py` is concentrated almost entirely on the single pure, dependency-free function `_filter_candidates`/`_check_candidate` — which is well-tested. Every other function in the module, including the core orchestration (`run_extract`, `_process_chunk`), the confidence heuristic, the deduplication logic, concept-id derivation, image-context building, and error/retry handling, has **no direct unit or integration test** found anywhere under `tests/`. The one test that references `extractor.run_extract` (`tests/test_web_state.py`) stubs it out completely and only verifies that `run-all` invokes it in the right order with the right keyword arguments — it provides no confidence about extractor's internal correctness. This is a significant coverage gap for a component that gates what content enters the vault and that mixes LLM-call orchestration, caching, filtering, and file I/O.

---
