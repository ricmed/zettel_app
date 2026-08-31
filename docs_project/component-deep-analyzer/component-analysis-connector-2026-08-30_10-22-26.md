# Component Deep Analysis Report — `connector`

Analyzed path: `zettel/connector.py` (635 lines)
Supporting collaborators inspected for boundary accuracy: `zettel/retrieval.py`, `zettel/vault.py`, `zettel/state.py`, `zettel/schemas.py`, `zettel/hashing.py`, `zettel/llm.py`, `zettel/usage.py`, `zettel/assets.py`, `zettel/cli.py` (`connect` command + `run-all`), `zettel/web_app.py` (job dispatch), `prompts/permanent_note.md`, `prompts/ptbr_guard.md`, `tests/test_connector.py`, `tests/test_web_state.py`.

---

## 1. Executive Summary

`connector.py` implements **Phase 3 ("connect")** of the Zettelkasten pipeline: it turns LLM-extracted, human-approved concepts (`PermanentNoteCandidate` rows, status `approved`, no `note_id` yet) into finished **ZTL Permanent Notes** written to the Obsidian vault (`30_Permanent/`).

Its single entry point, `run_connect(cfg, db, idx, candidates, observer=None)`, is invoked from three places: the `zettel connect` CLI command, the `run-all` orchestrator, and the web UI's job dispatcher (`web_app.py`). For each candidate it:

1. Resolves the literature reference the note should cite (granular chunk LIT, or source index as fallback).
2. Runs hybrid RAG (`Retriever.search_notes`) to find semantically/graph-related existing notes.
3. Calls **Prompt 2** (`prompts/permanent_note.md`) via a deterministic, checksum-cached LLM call to get a structured `PermanentNoteLLMOutput` (title, thesis, definition, intuition, example, limits, typed `connections`, tags) — the LLM can still reject the concept at this late stage.
4. Applies a heuristic **PT-BR language guard**, re-translating the note via a second LLM call if too many English markers are detected.
5. Resolves typed connections into vault wikilinks, builds the Markdown note body, writes the `.md` file, persists it to SQLite (byte-for-byte body + frontmatter, for `rebuild`), and embeds it into ChromaDB (skipped if content-hash unchanged).
6. Persists each typed connection as a graph edge (`note_connections`) and back-propagates an inverse-relation backlink into the **`auto-backlinks`** managed block of the target note(s) on disk.

The component is the pipeline's most expensive step per-item (the "Prompt 2" LLM call is explicitly called out in-code as "a chamada mais cara do pipeline") and is the point where the note graph — later consumed by `retrieval.py`'s graph expansion and `gardener.py`'s MOC clustering — is actually populated. It has no HTTP/RPC endpoints; it is a pure library module invoked by CLI/web orchestration layers.

Key findings:
- Business logic is concentrated in one large orchestration function (`_process_candidate`, ~230 lines) with clear single-responsibility helper functions around it (good cohesion for a pipeline stage, but the central function itself is a long procedural sequence — see Technical Debt).
- The module correctly reuses the shared LLM cache/hash infrastructure (`hashing.py`, `llm_cache` table) so a `connect` re-run after a crash does not re-pay for already-completed candidates.
- Failure handling is candidate-scoped: an LLM parse error or an explicit `"rejected"` status for a single candidate is logged and skipped (returns `None`), never aborting the whole run.
- There is an explicit, code-documented, **unresolved prompt-injection risk** (see Technical Debt §10 and Business Rule "LLM Input Sanitization Gap").

---

## 2. Data Flow Analysis

```
1.  CLI `zettel connect` / web job "connect" / `run-all` step 4
      → cli._load_approved_candidates(db) or web_app._load_candidates(db)
      → SQLite: SELECT * FROM concepts WHERE status='approved' AND note_id IS NULL
      → deserialize candidate_json into PermanentNoteCandidate (Pydantic)

2.  connector.run_connect(cfg, db, idx, candidates, observer)
      → db.start_run("connect")               [runs table row created]
      → usage.begin_run(run_id)               [CostTracker bound to contextvar]
      → llm = get_llm(cfg)                    [LangChain chat client from config]
      → prompt_parts = load_prompt_parts(prompts/permanent_note.md)
      → retriever = Retriever(cfg, db, idx)   [one instance reused for all candidates]
      → loop over candidates (Rich progress bar + observer.report callbacks)

3.  connector._process_candidate(...) — per candidate:
   a. Resolve/reuse note_id
        - db.get_concept(concept_id) → existing note_id? reuse (update path) : new ULID()
   b. Resolve literature_ref
        - db.get_source(source_id) → citekey/title
        - connector._literature_ref_for_chunk()
            → db.get_chunk(chunk_id) → status in (approved, persisted)?
                YES → vault.literature_chunk_wikilink_for_row(citekey, chunk)
                NO  → f"[[{vault.literature_index_stem(citekey, title_src)}]]"
   c. Resolve relevant images
        - cand.relevant_image_ids (LLM-provided) else
        - connector._fallback_image_ids() → assets.asset_ids_in_text(db, source_id, chunk.text)
   d. RAG context
        - query_text = f"{cand.thesis} {cand.definition}"
        - retriever.search_notes(query_text, topk=cfg.linking.topk, exclude_id=note_id).hits
        - connector._build_rag_context(db, hits) → Markdown grouped by hop 0 (embedding) / hop>=1 (graph)
   e. Prompt assembly
        - mapping{thesis, definition, intuition, limits, source_id, source_locator,
                  literature_ref, rag_context, images_context}
        - llm.fill_template() on prompt_parts.system / .user_template
   f. Deterministic LLM cache lookup
        - hashing.sha256_hex(prompt_template) + sha256_hex(normalize_text_for_hash(filled))
        - hashing.compute_llm_call_checksum(prompt_hash, filled_hash, model, temperature, language)
        - db.get_cached_llm_response(call_checksum)
            HIT  → usage.record_cache_hit(); response_text = cached
            MISS → llm.call_llm(...) → db.cache_llm_response(call_checksum, request_json, response_text)
   g. Parse response → connector._parse_permanent_note_output() → llm.extract_json + PermanentNoteLLMOutput(**data)
        - status == "rejected" → log + return None (candidate dropped, no note created)
   h. PT-BR guard (heuristic) → connector._needs_ptbr_fix() → connector._apply_ptbr_guard()
        (second, uncached LLM call using prompts/ptbr_guard.md, tolerant of failure)
   i. Cost delta bookkeeping via usage.get_tracker() before/after snapshot
   j. Connection assembly
        - connections = note_output.connections (typed RelationshipResult list)
        - refines_note_id (from extractor's "refine_existing" flow) → inject synthetic
          "extends" connection if not already present
        - connector._resolve_connections(db, connections) → db.get_note() per target →
          vault.permanent_wikilink() (uses file path stem when known, else ID/title slug)
        - connector._resolve_images(db, image_ids) → db.get_asset() per id
   k. Vault write
        - vault.build_permanent_note_body(thesis, definition, intuition, example, limits,
              connections, literature_ref, source_locator, images)
        - filename = vault.note_filename("ZTL", note_id, title)
        - path = cfg.vault_path / "30_Permanent" / filename
        - vault.safe_write_note(path, meta, body)   [overwrite-in-place on update]
   l. SQLite persistence
        - hashing.extract_embeddable_text(body) → sha256_hex(normalize) = semantic_checksum
        - db.upsert_note(note_id, source_id, path, title, semantic_checksum, embedding_model,
              body=<full markdown>, frontmatter_json=<json.dumps(meta)>, origin="pipeline")
        - db.upsert_concept(concept_id, source_id, chunk_id, note_id=note_id, status="noted")
   m. Conditional re-embedding
        - emb_hash = hashing.compute_embedding_input_hash(semantic_checksum, provider, model)
        - if db.get_note(note_id).embedding_input_hash != emb_hash:
              idx.upsert_permanent_note(note_id, embeddable, {title, source_id, tags, checksum})
              db.update_note_embedding(note_id, emb_hash, model)
   n. Graph + backlinks
        - connector._persist_and_backlink(cfg, db, note_id, title, connections)
            for each connection:
              db.upsert_note_connection(source=new_note_id, target=related_id, relation_type, description)
              target = db.get_note(target_id); skip if missing path or file absent
              inverse = connector._inverse_relation(relation_type)   [PT-BR label]
              new_link = f"- {wikilink} ({inverse})" [+ " -- description"]
              vault.safe_update_managed_blocks(target_path, {"auto-backlinks": merged_block})
                (connector._merge_backlink dedupes against existing block content)
   o. usage.clear_progress(); return note_id

4.  run_connect() loop end
      → usage.get_tracker().sources_touched() → db.add_source_usage(sid, summary)
      → vault.sync_source_costs_to_vault(cfg, db, sid)   [mirrors cost fields onto SRC frontmatter]
      → usage.finish_pipeline_run(db, run_id)            [closes `runs` row with totals]
      → returns list[note_id] created/updated this run
```

Two independent side-effect surfaces are written per candidate: the **vault filesystem** (new/updated `.md` files under `30_Permanent/` and managed-block edits to *other* existing notes) and **two persistence stores** (SQLite `notes`/`concepts`/`note_connections`, ChromaDB `permanent_notes` collection). A crash between steps (k) and (n) is recoverable because `run_connect` is idempotent per-candidate (see Business Rules, Idempotent Note Identity).

---

## 3. Business Rules & Logic

## Overview of the business rules:

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Query gate | Only concepts with `status='approved'` AND `note_id IS NULL` are eligible for connect | `zettel/cli.py:59-71` (`_load_approved_candidates`) via `StateDB.get_concepts_by_status` in `zettel/state.py:1022` |
| Idempotency | Reprocessing a concept that already has a `note_id` reuses that ID instead of minting a new ULID | `zettel/connector.py:185-190` |
| Literature reference resolution | Cite the granular chunk-level LIT only if that chunk's status is `approved` or `persisted`; otherwise fall back to the source-level LIT index | `zettel/connector.py:79-94` |
| Late-stage rejection | The LLM (Prompt 2) can still reject a pre-approved concept; a `"rejected"` status yields no note, no DB writes, no vault writes | `zettel/connector.py:268-275` |
| Image resolution precedence | LLM-declared `relevant_image_ids` take precedence; only when empty does the pipeline fall back to scanning the chunk's raw text for asset paths | `zettel/connector.py:199-204`, `zettel/connector.py:407-416` |
| PT-BR language guard | If the generated note text contains ≥3 of 8 English filler-word markers, trigger a second LLM call to re-translate the structured fields | `zettel/connector.py:578-625` |
| Deterministic LLM cache | The Prompt 2 call is cached by a checksum of (prompt template hash, filled-prompt hash, model, temperature, language); an unchanged candidate re-run never re-pays the LLM | `zettel/connector.py:232-267` |
| Synthetic "extends" connection for refinements | When a candidate carries `refines_note_id` (from the extractor's dedupe/refine flow), an `extends` connection to that note is force-injected if the LLM didn't already propose one | `zettel/connector.py:308-316` |
| Conditional re-embedding | The note is only re-embedded into ChromaDB when its semantic-content hash + embedding model differ from what's already recorded | `zettel/connector.py:374-384` |
| Typed connections + inverse backlinks | Every `RelationshipResult` from the LLM becomes a directed graph edge (`note_connections`) AND a reverse-labeled backlink line written into the target note's `auto-backlinks` managed block | `zettel/connector.py:520-560` |
| Backlink write skip conditions | A backlink is not written if the target note has no DB row, no `path`, or the file no longer exists on disk | `zettel/connector.py:539-545` |
| Backlink deduplication | A backlink line is not duplicated into the managed block if its trimmed text already appears there | `zettel/connector.py:563-572` |
| Wikilink stability | Wikilinks to a note prefer the actual on-disk filename stem (`Path(path).stem`) over a freshly-recomputed title slug, so links survive title edits | `zettel/vault.py:745-758` |
| RAG provenance segregation | Retrieved related notes are split into "Similares por embedding" (hop 0) vs. "Vizinhas por conexao no grafo" (hop ≥ 1) sections in the LLM prompt, each rendered differently | `zettel/connector.py:464-514` |
| Cost/usage accounting per note | Token/cost deltas are computed by snapshotting the active `CostTracker` before and after each LLM call, then stored on note frontmatter and aggregated per source | `zettel/connector.py:241-304`, `zettel/connector.py:337-352` |
| Concept terminal status | On successful note creation, the source concept's status becomes `"noted"` (distinct from the extractor's `approved`/`awaiting_review`/`rejected`) | `zettel/connector.py:370-372` |
| Relation-type value normalization | `RelationType` is a `str, Enum` hybrid; the module always extracts `.value` rather than trusting `isinstance(x, str)`, since f-string interpolation of the raw enum renders `"RelationType.SUPPORTS"` | `zettel/connector.py:66-76` |
| Prompt-injection exposure (unmitigated) | Candidate fields (`thesis`, `definition`, etc.) originate from LLM output derived from user-uploaded files and are interpolated into Prompt 2 without delimiter sanitization | `zettel/connector.py:212-215` (explicit code comment) |

## Detailed breakdown of the business rules:

---

### Business Rule: Approved-Without-Notes Eligibility Gate

**Overview**:
`connect` never scans the vault or re-derives eligibility on its own — it trusts a single SQL predicate as the sole source of truth for "what is ready to become a permanent note."

**Detailed description**:
The eligibility query lives outside `connector.py` itself, in `StateDB.get_concepts_by_status("approved", without_notes=True)` (`zettel/state.py:1022-1032`), and is invoked by both the CLI (`_load_approved_candidates` in `cli.py:56-71`) and the web job dispatcher (`web_app._load_candidates`). The predicate is `status='approved' AND note_id IS NULL`. This design deliberately decouples "connect" from any particular caller: whether a concept became `approved` via the interactive `zettel review` HITL flow, via `--auto-approve`/`--yes` non-interactive review, or via the extractor's own high-confidence auto-approval, connect treats all of them identically. It also means connect is naturally re-runnable/incremental — running it twice in a row processes zero candidates the second time, because every successfully processed concept exits the `note_id IS NULL` predicate (rule "Idempotent Note Identity" governs what happens to concepts whose note creation failed).

This is what data-flow docs call "connect loads `get_concepts_by_status('approved', without_notes=True)` from SQLite only" — i.e. connect has zero dependency on ChromaDB or the vault filesystem to determine its work queue, only SQLite. This matters operationally: a corrupted or stale vault file does not affect what candidates get processed (though it can affect literature-reference resolution and backlink writing for specific candidates, see below), and a `sync-manual` or `garden` failure elsewhere in the pipeline cannot silently make connect skip or duplicate a candidate.

A concept whose Prompt 2 call returns `status: "rejected"` is *not* re-flagged in this predicate — it keeps `status='approved'` and `note_id IS NULL` in the `concepts` table (connector.py never calls `db.update_concept_status` on rejection), so it will be **re-attempted on every future `connect` run** until either the LLM eventually accepts it or an operator intervenes. This is a deliberate-by-omission behavior worth flagging: see Technical Debt.

**Rule workflow**:
1. Concept reaches `status='approved'` via `extract`/`review` (outside this component's scope).
2. `connect` invocation (CLI/web/run-all) calls `get_concepts_by_status("approved", without_notes=True)`.
3. Each returned row's `candidate_json` is deserialized into `PermanentNoteCandidate`.
4. Candidate is handed to `_process_candidate`; success sets `note_id` on the concept (removing it from future eligibility) — failure/rejection leaves the row eligible again next run.

---

### Business Rule: Idempotent Note Identity (Reuse vs. Mint)

**Overview**:
Before generating anything, `_process_candidate` checks whether the concept already has an associated permanent note, and if so, reuses that note's ULID rather than minting a new one — turning a second `connect` pass over the same concept into an *update* rather than a *duplicate creation*.

**Detailed description**:
`existing_concept = db.get_concept(concept_id)`; if `existing_concept.get("note_id")` is truthy, `note_id = existing_concept["note_id"]` and a debug log records "Conceito ... ja tem nota ..., atualizando." Otherwise a fresh `ULID()` is minted. This matters because the note's filename (`vault.note_filename("ZTL", note_id, title)`) and its vault path are both derived from `note_id`, so reusing the ID means the *same file* is overwritten via `safe_write_note` (a full overwrite of the file's frontmatter+body, not a managed-block patch) rather than a second file being created alongside it.

In practice this path is reached only when a concept was previously connected but something forced the pipeline to run it again while its DB row still records the old `note_id` — for instance an operator manually resetting `concepts.status` back to `approved` and clearing `note_id`... but note that clearing `note_id` would make `db.get_concept()` return a note_id of `NULL`, taking the "mint new ULID" branch instead. The reuse branch is actually reached when the *concept*'s `note_id` remains set but something re-enqueues it as a candidate anyway — e.g. a caller building the candidate list directly (bypassing the `without_notes=True` filter) as `run-all`'s web dispatch test double demonstrates is structurally possible (`test_web_state.py`'s stubbed `_load_candidates` returns a fixed candidate list unconditionally). This makes the reuse branch a defensive idempotency guard for non-CLI callers rather than a commonly-hit path in normal CLI usage.

Because the vault write is a full overwrite (not a merge), any manual edits a user made directly to a pipeline-generated ZTL file's body would be lost if that concept were somehow reprocessed. The frontmatter always carries `origin: "pipeline"`, distinguishing these notes from `origin: "manual"` notes created via `zettel new-note`, which `connect` never touches.

**Rule workflow**:
1. `db.get_concept(concept_id)` fetched.
2. `note_id` present on that row → reuse (update path, same filename/path recomputed).
3. `note_id` absent → `note_id = str(ULID())` (create path).
4. Downstream `safe_write_note` always overwrites the target path unconditionally either way.

---

### Business Rule: Literature Reference Resolution (Granular-First, Index-Fallback)

**Overview**:
Every permanent note must cite a literature note. `connect` prefers the most specific citation available — the exact approved chunk-level LIT note the concept was extracted from — and only falls back to the coarser per-source LIT index when that specific chunk isn't in a citable state.

**Detailed description**:
`_literature_ref_for_chunk` (`connector.py:79-94`) receives the candidate's `chunk_id` (may be `None` for candidates without a resolvable chunk). If present, it fetches the chunk row (`db.get_chunk(chunk_id)`) and checks `chunk.get("status") in ("approved", "persisted")`. Only in that case does it build a wikilink to the granular chunk file via `vault.literature_chunk_wikilink_for_row(citekey, chunk)`, which encodes the chunk's page token and topic slug into the filename-derived link (see `vault.py:320-329`, `literature_chunk_filename_for_row`). If the chunk_id is missing, or the chunk exists but hasn't reached `approved`/`persisted` (e.g. still `awaiting_review` or was `rejected`), the function instead returns a wikilink to the source-level index: `f"[[{literature_index_stem(citekey, title_src)}]]"`.

This two-tier fallback exists because of an inherent race in the pipeline: a source can have many chunks in different review states simultaneously, and `connect` runs on *concepts*, which are tied to a specific chunk but can, per the module's own docstring elsewhere in the codebase (`extractor.py`), sometimes be processed even when their originating chunk's LIT draft hasn't cleared review yet (e.g. via edge cases in re-processing or partial batches). Rather than blocking note creation on a specific chunk's review state, `connect` degrades gracefully to citing the whole source's index note — which always exists once `harvest` has run — guaranteeing the produced ZTL note is never left with a broken or missing literature citation.

The literature reference is not just descriptive: it's baked into the persisted note frontmatter (`meta["literature_ref"]`) and the rendered body's "## Fonte" section (`vault.build_permanent_note_body`), and it becomes part of what `db.upsert_note` records — meaning downstream consumers (manual vault browsing, `sync-manual`'s edge extraction, human readers in Obsidian) always have a working citation trail back to source material, even under partial-approval conditions.

**Rule workflow**:
1. `chunk_id` from candidate dict; if falsy → skip straight to index fallback.
2. `db.get_chunk(chunk_id)` → if falsy or `status` not in `{approved, persisted}` → index fallback.
3. Otherwise → `literature_chunk_wikilink_for_row(citekey, chunk)` (granular link, page-token + topic slug encoded).
4. Fallback: `[[{literature_index_stem(citekey, title_src)}]]` (per-source index, always resolvable once harvested).

---

### Business Rule: Late-Stage LLM Rejection

**Overview**:
Human/auto-approval during `review` is not the final gate — Prompt 2 itself can still decide a concept doesn't merit a permanent note and reject it, in which case `connect` produces absolutely no artifact for that candidate.

**Detailed description**:
`prompts/permanent_note.md` (loaded as `prompt_parts`) instructs the LLM to output either `{"status": "accepted", ...}` or `{"status": "rejected", "reason": ..., "category": ...}` using explicit rejection categories (`promotional`, `generic`, `vague`, `context_dependent`, `redundant`, `low_density`) documented in the prompt with worked examples. `_process_candidate` parses this via `_parse_permanent_note_output` into a `PermanentNoteLLMOutput`, and immediately checks `note_output.status == "rejected"`: if so, it logs a warning with the concept id and the LLM's stated reason, calls `clear_progress()`, and returns `None` — no vault file, no `db.upsert_note`, no `db.upsert_concept` status change, no embedding, no connections. The concept row is left exactly as it was (`status='approved'`, `note_id IS NULL`), meaning it re-enters the eligibility pool on the very next `connect` run (see the gap noted under Technical Debt: this can create a costly infinite-reprocessing loop for a genuinely rejectable concept, subject only to the LLM cache — since the request/response pair is cached by checksum, repeated runs *do not* re-pay the LLM cost for byte-identical retries, but they do keep re-querying and re-logging every time).

This double-gate design (human/auto-approval at `review`, then a second independent LLM judgment at `connect`) reflects that the review-stage LLM (Prompt 1, extraction) and the connect-stage LLM (Prompt 2, note composition) apply different, stricter criteria — Prompt 2's system prompt explicitly frames rejection as reserved for "genuinely empty, promotional, or inseparable from context" content, essentially a second-opinion quality gate right before something becomes a permanent, interconnected unit of the Zettelkasten.

**Rule workflow**:
1. Prompt 2 response parsed into `PermanentNoteLLMOutput`.
2. `status == "rejected"` → log warning (concept_id + reason) → `clear_progress()` → return `None`.
3. Caller (`run_connect`) does not append this candidate's id to `created_ids`; loop continues to next candidate.
4. Concept row remains eligible for a future `connect` run (no terminal state is recorded for LLM-level rejection).

---

### Business Rule: Image Resolution Precedence (LLM-Declared, Then Text-Scanned Fallback)

**Overview**:
A permanent note can embed figures from the source material; which images to embed is decided first by what the earlier extraction LLM flagged as essential, and only secondarily by a deterministic text scan of the originating chunk.

**Detailed description**:
`cand.relevant_image_ids` (a list of `asset_id`s set by the *extractor's* Prompt 1 output, carried through on `PermanentNoteCandidate`) is checked first (`connector.py:200-204`). If empty, `_fallback_image_ids(db, cand_dict)` is invoked, which loads the chunk's raw text (`db.get_chunk(chunk_id)`) and calls `assets.asset_ids_in_text(db, source_id, chunk_text)` — a deterministic substring match of each known asset's vault-relative path against the chunk text (`assets.py:244-253`). Any IDs found this way are written back onto `cand.relevant_image_ids` in memory (not persisted back to the candidate's `candidate_json` row) so that both `_build_candidate_images_context` (which describes the images to the LLM in Prompt 2) and the later `_resolve_images` (which builds the note's "## Figuras" section) see a consistent list.

This two-tier approach exists because the LLM performing initial concept extraction may not always populate `relevant_image_ids` reliably (structured-output omission, or the concept simply wasn't judged to need a figure at extraction time even though the chunk visually contains one) — the deterministic fallback is a safety net that guarantees images physically present in the source chunk's Markdown are not silently dropped from the note purely due to an LLM formatting miss. Because the fallback is purely a substring match on asset path strings already embedded in the chunk text (as Markdown image syntax from Docling extraction), it cannot introduce spurious images not actually referenced in that chunk.

**Rule workflow**:
1. `image_ids = cand.relevant_image_ids` (from extractor output).
2. If empty: `image_ids = _fallback_image_ids(db, cand_dict)` (asset-path substring scan of `chunk.text`).
3. If fallback found anything, mutate `cand.relevant_image_ids = image_ids` for prompt-context consistency.
4. `_build_candidate_images_context` renders `image_ids` as bullet descriptions for the LLM prompt.
5. `_resolve_images(db, image_ids)` resolves each id to `{path, description}` for the final note body's "## Figuras" section (only images with a resolvable `db.get_asset()` row and a `path` survive).

---

### Business Rule: PT-BR Language Guard (Heuristic Trigger, Structured Correction)

**Overview**:
Because the project mandates PT-BR output but LLMs occasionally slip into English mid-generation, `connect` runs a cheap heuristic check after every successful Prompt 2 call and, if triggered, spends a second LLM call to translate the structured note fields back to PT-BR while preserving their JSON shape.

**Detailed description**:
`_needs_ptbr_fix(text)` (`connector.py:578-582`) concatenates `thesis + definition + intuition` from the just-generated note and counts case-insensitive occurrences of eight English filler markers (`"the "`, `"and "`, `"this "`, `"that "`, `"with "`, `"from "`, `"which "`, `"where "`). If three or more distinct markers appear anywhere in that combined text, the guard fires. This is a coarse but cheap heuristic — deliberately over-inclusive of any English-language leakage rather than attempting deep language detection, trading occasional false positives (rare technical English phrases coincidentally containing several such words) for near-certainty of catching real language drift.

When triggered, `_apply_ptbr_guard` (`connector.py:585-625`) loads a dedicated prompt (`prompts/ptbr_guard.md`), serializes the five affected fields (`thesis`, `definition`, `intuition`, `example`, `limits`) as a JSON object, and asks the LLM to return the same JSON shape with corrected PT-BR text — explicitly instructed to preserve Markdown structure and not alter meaning, and to keep well-established English technical terms (e.g. "machine learning", "framework") when there's no natural PT-BR equivalent. The guard call is **not** run through the deterministic LLM cache (unlike Prompt 2) and is wrapped in a broad `try/except`: any failure (network error, malformed JSON, missing keys) is caught, logged as a warning, and the *original* (potentially English-tainted) output is returned unchanged rather than aborting note creation. This means the guard is best-effort quality improvement, not a hard correctness gate — a persistently PT-BR-violating note can still be written to the vault if this second call fails.

**Rule workflow**:
1. After Prompt 2 succeeds, concatenate `thesis + definition + intuition` from `note_output`.
2. Count occurrences of 8 English marker substrings (case-insensitive).
3. If count ≥ 3 → call `_apply_ptbr_guard(cfg, llm, note_output)`.
4. Guard serializes 5 fields to JSON, calls LLM with `prompts/ptbr_guard.md`, parses corrected JSON.
5. On any exception → log warning, return `note_output` unmodified.
6. On success → overwrite `output.thesis/definition/intuition/example/limits` with corrected values (using `.get(key, original)` per-field, so partial LLM responses degrade gracefully field-by-field).

---

### Business Rule: Deterministic LLM Response Caching for Prompt 2

**Overview**:
The most expensive LLM call in the entire pipeline (composing the full permanent note) is cached by a checksum over the complete, fully-rendered prompt content plus generation parameters, so an identical re-invocation (e.g. after a crash mid-run) never re-pays for that call.

**Detailed description**:
The cache key (`compute_llm_call_checksum`, from `hashing.py`) is built from: a hash of the *raw prompt template* (`prompt_parts.full_template`, i.e. the prompt file's content before variable substitution — changes to `prompts/permanent_note.md` itself invalidate all cache entries), a hash of the *fully filled* prompt text (system + user, after `fill_template` substitution and passed through `normalize_text_for_hash` for canonical whitespace/Unicode handling), plus `cfg.llm.model`, `cfg.llm.temperature`, and `cfg.language`. This means the cache key changes if: the prompt template is edited, the candidate's thesis/definition/RAG-context/images-context text differs even slightly, the configured model changes, the temperature changes, or the target language changes — any of which correctly forces a fresh LLM call rather than serving a stale cached response.

On a cache hit, `db.get_cached_llm_response(call_checksum)` returns the previously-generated raw response text; the module records this via `usage.record_cache_hit(label=f"connect:{concept_id}", model=cfg.llm.model)` (so cost dashboards can distinguish "free" cache hits from paid calls) and skips the network call entirely. On a miss, `call_llm(...)` is invoked and its response immediately persisted via `db.cache_llm_response(call_checksum, request_json, response_text)` before parsing — meaning even if JSON parsing subsequently fails (malformed LLM output) or the note write fails, the *raw response* is already durably cached, so a retry after fixing a downstream bug does not need to re-call the LLM for a response that was already successfully obtained.

Notably, the RAG context (`rag_context`) is part of the filled-prompt hash. Since RAG results depend on the current state of the note graph and vector index, this makes the cache key content-sensitive to concurrent graph growth — a valuable property, but it also means a `connect` run right after other notes were added (changing what "similar notes" look like) will *not* reuse an earlier cache entry for an otherwise-identical candidate, correctly forcing fresh (and potentially better-connected) generation.

**Rule workflow**:
1. `prompt_hash = sha256_hex(prompt_parts.full_template)`.
2. `filled_hash = sha256_hex(normalize_text_for_hash(f"{system}\n{user}"))`.
3. `call_checksum = compute_llm_call_checksum(prompt_hash, filled_hash, model, temperature, language)`.
4. `db.get_cached_llm_response(call_checksum)` — hit → reuse + record_cache_hit(); miss → `call_llm()` + `db.cache_llm_response()`.
5. Parse cached-or-fresh `response_text` identically either way.

---

### Business Rule: Synthetic "extends" Connection for Refined Concepts

**Overview**:
When a candidate is flagged as refining a pre-existing permanent note (a dedupe/merge outcome decided upstream in the extractor), `connect` guarantees the graph records that relationship even if the LLM's own proposed connections happen to omit it.

**Detailed description**:
`cand_dict.get("refines_note_id")` surfaces a note_id set by the extractor when it determined the current concept is a refinement/elaboration of an already-connected note rather than a wholly new one. After Prompt 2 returns its own `connections` list, `_process_candidate` checks `any(c.related_note_id == refines_note_id for c in connections)`; if the LLM didn't already propose a connection to that specific note, the code force-appends a `RelationshipResult(related_note_id=refines_note_id, relation_type="extends", description=cand_dict.get("refine_reason", "Refina nota existente"))`. This is a deterministic guarantee layered on top of a probabilistic LLM decision: the pipeline's own upstream judgment (this candidate refines that note) is treated as ground truth that must appear in the graph regardless of what the downstream LLM call independently concludes, while still allowing the LLM's own additional connections (potentially of any relation type, to any other notes) to coexist.

This connection then flows through the same resolution/persistence path as any LLM-proposed connection: it is resolved to a wikilink via `_resolve_connections`, persisted as a graph edge via `db.upsert_note_connection`, and produces an inverse backlink ("estendido por") written into the *original* note's `auto-backlinks` block — closing the loop so a human browsing the original note in Obsidian can navigate forward to its refinement.

**Rule workflow**:
1. `refines_note_id = cand_dict.get("refines_note_id")` (may be `None`).
2. If set and not already present among `note_output.connections` (by `related_note_id` equality) → append a synthetic `RelationshipResult(relation_type="extends", ...)`.
3. Synthetic connection flows through `_resolve_connections` / `_persist_and_backlink` identically to LLM-proposed ones.

---

### Business Rule: Conditional Re-Embedding by Content Hash

**Overview**:
A note is only pushed into ChromaDB's vector index when its semantically-relevant text content or the configured embedding model has actually changed since the last time it was embedded — avoiding redundant, cost-incurring embedding calls on unchanged notes.

**Detailed description**:
After the note body is finalized and persisted to SQLite, `extract_embeddable_text(body)` (from `hashing.py`) strips frontmatter and managed blocks to isolate the note's true semantic content, and `sha256_hex(normalize_text_for_hash(...))` produces `semantic_checksum`. `compute_embedding_input_hash(semantic_checksum, cfg.embedding.provider, cfg.embedding.model)` combines that with the *provider and model identity* into `emb_hash` — meaning a config change (e.g. switching embedding providers) correctly forces re-embedding of every note even if none of their text changed. The code then compares this against `db.get_note(note_id).get("embedding_input_hash")`; only a mismatch (or a missing prior note record) triggers `idx.upsert_permanent_note(...)` followed by `db.update_note_embedding(...)`.

Because this check happens on every `_process_candidate` call — including the "reuse existing note_id" (update) branch — it means updating an existing pipeline note (e.g. via a hypothetical re-run) does not always trigger a costly re-embedding; only genuine content drift or model/provider changes do. This mirrors the same content-hash-gated pattern used elsewhere in the codebase (e.g. `sync.py`, `harvester.py`) for cost control on embedding-provider API calls.

**Rule workflow**:
1. Compute `semantic_checksum` from the newly-built note body (frontmatter/managed-blocks stripped).
2. Compute `emb_hash = compute_embedding_input_hash(semantic_checksum, embedding.provider, embedding.model)`.
3. Fetch `existing_note = db.get_note(note_id)` (post-`upsert_note`, so this reflects the just-written row).
4. If `existing_note` is falsy or `existing_note["embedding_input_hash"] != emb_hash` → call `idx.upsert_permanent_note()` and `db.update_note_embedding()`.
5. Otherwise skip re-embedding entirely (no ChromaDB call, no cost).

---

### Business Rule: Typed Connections Produce Bidirectional, Asymmetric-Label Graph Edges

**Overview**:
Every connection a permanent note declares to another note is recorded as a single directed graph edge in SQLite, but is rendered as a *bidirectional* navigational aid in the vault — the new note shows the forward relation in its body, and the target note receives an automatically-maintained backlink showing the semantically inverse relation label.

**Detailed description**:
`RelationshipResult` supports six relation types (`RelationType` enum): `supports`, `contradicts`, `extends`, `depends_on`, `exemplifies`, `related`. `_persist_and_backlink` (`connector.py:520-560`) iterates every connection on the newly-created/updated note and, for each: (a) upserts a single directed edge `source_note_id=new_note_id, target_note_id=target_id, relation_type, description` via `db.upsert_note_connection` — the DB unique constraint is `(source_note_id, target_note_id, relation_type)`, so re-running the same connection is an update-in-place (description/timestamp refresh), not a duplicate row; (b) looks up the target note's on-disk path, and — only if the target has a DB record with a `path` *and that file still exists* — computes the PT-BR inverse label via `_inverse_relation(relation_type)` (a fixed dict: e.g. `extends` → `"estendido por"`, `contradicts` → `"contradiz"` (self-inverse, since contradiction is symmetric), `related` → `"relacionado"`, with any unrecognized relation type also defaulting to `"relacionado"`) and writes a new backlink line into the target's `auto-backlinks` managed block via `safe_update_managed_blocks`.

The backlink write goes through `_merge_backlink`, which reads the target file's *current* `auto-backlinks` block content, and only appends the new line if its stripped text is not already present verbatim — a simple substring-based deduplication that prevents the block from accumulating duplicate lines across repeated `connect` runs that happen to re-propose the same connection. Because `safe_update_managed_blocks` only touches content between the block's start/end markers and leaves everything else in the file untouched (including any manually-written prose outside managed blocks), this backlink propagation cannot clobber a user's manual edits to the target note — a deliberate design invariant enforced by `vault.py`'s managed-block contract.

One asymmetry: the graph edge in SQLite is directional and *not* automatically mirrored as a second inverse edge (e.g. a `supports` edge from A→B does not also create a `suportado por`/`supported_by` edge from B→A in `note_connections`) — the inverse relationship exists only as vault-rendered prose in the backlink, not as queryable graph structure. This matters for `graph.py`'s `expand_notes` BFS, which treats `note_connections` as undirected per its own docstring ("undirected, weighted by relation type") — meaning the single directed row is sufficient for graph traversal in both directions, but any consumer that queries `note_connections` for "what connections does note X have" filtering only on `source_note_id` would miss edges where X is the `target_note_id`.

**Rule workflow**:
1. For each `connection` in the final `connections` list (LLM-proposed + any synthetic "extends"):
   a. `db.upsert_note_connection(new_note_id, target_id, relation_type, description)` — always executed regardless of target validity.
   b. `target_record = db.get_note(target_id)`; if missing or no `path` → skip backlink write (edge still persisted).
   c. `target_path = Path(target_record["path"])`; if file doesn't exist on disk → skip backlink write.
   d. `inverse = _inverse_relation(relation_type)` (dict lookup, default `"relacionado"`).
   e. Build `new_link` string (wikilink + inverse label [+ description]).
   f. `_merge_backlink(target_path, new_link)` — dedupe against existing block content.
   g. `safe_update_managed_blocks(target_path, {"auto-backlinks": merged})` — writes only if content actually changed (bumps `updated_at`).

---

### Business Rule: Relation-Type Enum Value Normalization

**Overview**:
Because `RelationType` is implemented as a hybrid `str, Enum`, naive string interpolation of an enum member produces the wrong, non-canonical text (`"RelationType.SUPPORTS"` instead of `"supports"`); the module centralizes correct extraction through one helper used everywhere a relation type is rendered.

**Detailed description**:
Python's `str, Enum` pattern makes `isinstance(RelationType.SUPPORTS, str)` evaluate `True`, which can mislead code that branches on `isinstance` checks into treating the enum member as an already-safe string — but an f-string (`f"{RelationType.SUPPORTS}"`) still renders the qualified name `"RelationType.SUPPORTS"`, not the underlying value `"supports"`, because `Enum.__str__` takes precedence over `str.__str__` in the MRO for this hybrid pattern. `_relation_type_value` (`connector.py:66-76`) is the single normalization point: it explicitly checks `isinstance(relation_type, Enum)` (not `str`) and calls `.value` in that branch, falling back to `str(relation_type or "related")` for genuine plain strings or falsy input. This helper is used both when persisting/rendering LLM-proposed connections (`_persist_and_backlink`, `_resolve_connections`) and is defensively re-applied inside `vault.build_permanent_note_body` itself (checking `hasattr(rtype, "value")`) — a second independent safety net in a different module, guarding against any future caller that bypasses `_relation_type_value`.

This is a genuine, previously-encountered bug class (evidenced by the explicit regression test `test_relation_type_value_from_enum`, which literally asserts `f"{RelationType.SUPPORTS}" == "RelationType.SUPPORTS"` as documentation of the footgun being guarded against) — Pydantic v2 will often leave `relation_type` as an actual `RelationType` enum instance (not a plain str) after validating `RelationshipResult` from the LLM's JSON, since the field is typed `relation_type: RelationType`.

**Rule workflow**:
1. Any code needing a relation-type string calls `_relation_type_value(value)`.
2. `isinstance(value, Enum)` → return `value.value` (canonical lowercase string, e.g. `"supports"`).
3. Otherwise → `str(value or "related")` (handles both plain strings and falsy/`None` input).
4. `vault.build_permanent_note_body` independently re-checks `hasattr(rtype, "value")` as a second guard at render time.

---

### Business Rule: LLM Input Sanitization Gap (Acknowledged, Unmitigated Risk)

**Overview**:
The module contains an explicit in-code security note acknowledging that candidate fields interpolated into Prompt 2 are LLM-derived text ultimately traceable to user-uploaded documents, and that no prompt-delimiter sanitization is currently applied before that interpolation.

**Detailed description**:
At `connector.py:212-215`, immediately before building the Prompt 2 `mapping` dict, a comment reads: *"SECURITY NOTE: cand.thesis, cand.definition, and other candidate fields originate from LLM output derived from user-supplied files. Sanitize prompt delimiters (e.g. strip '---', '\</s\>', '###SYSTEM') before interpolation if untrusted input is expected, to reduce prompt-injection risk."* No such sanitization is implemented in this module — `fill_template` (from `llm.py`) performs a literal, un-escaped string substitution of every mapping value into the prompt template's `{key}` placeholders. Because the *upstream* extractor's Prompt 1 already passed this text through an LLM once, a successful prompt-injection payload embedded in a harvested source document would need to survive that first LLM pass verbatim (or be regenerated by it) to reach Prompt 2 unmodified — a nontrivial but not impossible bar, especially for payloads phrased as plausible-sounding "quoted" content the first LLM might faithfully reproduce inside `thesis`/`definition`/`anchor_quote` fields.

The practical impact is bounded by what Prompt 2's system prompt permits the LLM to do — it only ever returns a fixed JSON schema (title/thesis/definition/etc.) that is then further structurally validated by `PermanentNoteLLMOutput` (Pydantic), so a successful injection could at most influence *what content* appears in a generated note (e.g., steering `status` to `"accepted"` for genuinely low-value content, or injecting biased/off-topic `connections`), not achieve arbitrary code execution or escape the JSON response contract. Nonetheless, this is a documented, currently-open gap rather than a mitigated one.

**Rule workflow**:
1. Candidate fields (`thesis`, `definition`, `intuition`, `limits`, `source_locator`) come from `PermanentNoteCandidate`, itself LLM-derived from harvested document text.
2. `mapping` dict built directly from these fields with no delimiter stripping/escaping.
3. `fill_template(prompt_parts.system/user_template, mapping)` performs literal substring substitution.
4. No validation step rejects or neutralizes prompt-control-sequence-like substrings (e.g., `---`, `</s>`, `###SYSTEM`) before this point.

---

## 4. Component Structure

`connector.py` is a single flat module (no sub-package) organized into clearly delimited comment-banner sections:

```
zettel/
└── connector.py                       # Phase 3 — RAG linking, note generation, backlinking
    ├── _INVERSE_RELATION (dict)       # PT-BR inverse-relation label table
    ├── _inverse_relation()            # dict lookup w/ "relacionado" default
    ├── _relation_type_value()         # Enum-safe relation-type string extraction
    ├── _literature_ref_for_chunk()    # granular-LIT-first, index-fallback citation resolver
    │
    ├── run_connect()                  # PUBLIC ENTRY POINT — batch orchestration, Rich progress,
    │                                  #   run/cost bookkeeping, returns created note_ids
    │
    ├── _process_candidate()           # single-candidate pipeline: RAG → Prompt2 → PT-BR guard →
    │                                  #   vault write → SQLite/Chroma persist → backlink
    │
    ├── _resolve_images()              # asset_id list -> [{path, description}]
    ├── _fallback_image_ids()          # chunk-text asset-path substring scan
    ├── _build_candidate_images_context() # Prompt-2 "figures" context block
    ├── _resolve_connections()         # RelationshipResult list -> vault-ready dicts w/ wikilinks
    │
    ├── _build_rag_context()           # hop-0 vs hop>=1 grouped Markdown context for Prompt 2
    │
    ├── _persist_and_backlink()        # graph edge upsert + inverse-relation backlink propagation
    ├── _merge_backlink()              # dedupe-aware managed-block content merge
    │
    ├── _needs_ptbr_fix()              # English-marker heuristic
    ├── _apply_ptbr_guard()            # second LLM call, JSON-in/JSON-out re-translation
    │
    └── _parse_permanent_note_output() # extract_json + PermanentNoteLLMOutput(**data)
```

No `__init__.py` re-exports are involved; other modules import directly via `from zettel.connector import run_connect` (the only symbol imported externally, per `cli.py`, `web_app.py`, and `tests/test_web_state.py`). Test-only internals (`_build_rag_context`, `_fallback_image_ids`, `_inverse_relation`, `_relation_type_value`, `_resolve_connections`, `_resolve_images`) are imported directly by name in `tests/test_connector.py`, indicating the module's private helpers are treated as a de facto semi-public, individually-testable surface.

---

## 5. Dependency Analysis

```
Internal Dependencies (compile-time imports):

connector.py
 ├── zettel.config          → AppConfig (type-only usage: cfg.linking.topk, cfg.llm.*,
 │                              cfg.embedding.*, cfg.prompts_path, cfg.vault_path, cfg.language)
 ├── zettel.hashing          → compute_embedding_input_hash, compute_llm_call_checksum,
 │                              extract_embeddable_text, normalize_text_for_hash, sha256_hex
 ├── zettel.index            → VectorIndex (type-only usage: idx.upsert_permanent_note)
 ├── zettel.llm              → PromptParts, call_llm, extract_json, fill_template, get_llm,
 │                              load_prompt_parts
 ├── zettel.retrieval        → RetrievedNote, Retriever  (RAG composition point)
 │       └── zettel.graph    → expand_notes (BFS) [transitive, via Retriever]
 ├── zettel.schemas          → PermanentNoteCandidate, PermanentNoteLLMOutput, RelationshipResult
 ├── zettel.state            → StateDB (type-only; ~15 distinct method calls, see §6)
 ├── zettel.vault            → build_permanent_note_body, note_filename, permanent_wikilink,
 │                              safe_update_managed_blocks, safe_write_note,
 │                              literature_chunk_wikilink_for_row, literature_index_stem (lazy import),
 │                              read_managed_block (lazy import, inside _merge_backlink)
 ├── zettel.assets           → asset_ids_in_text (lazy import, inside _fallback_image_ids)
 ├── zettel.usage            → begin_run, finish_pipeline_run, get_tracker, set_source,
 │                              record_cache_hit, clear_progress, set_progress,
 │                              format_progress_from_context (all lazy-imported inside functions)
 └── zettel.progress         → report() (lazy import, inside run_connect)

Downstream consumers (who imports connector.py):
 ├── zettel/cli.py            → `connect` Typer command; `run-all` orchestrator (step 4/5)
 ├── zettel/web_app.py        → WebWorker._dispatch (enqueued "connect" job) and run_all path
 └── tests/test_web_state.py  → monkeypatches connector.run_connect for run-all dispatch testing

External Dependencies:
 - ulid-py (`from ulid import ULID`)     - Note ID generation (ULIDs, not UUIDs, for lexical sortability)
 - rich (Progress, SpinnerColumn, etc.)  - CLI progress bar rendering during run_connect
 - Pydantic (via zettel.schemas)         - Structured validation of PermanentNoteLLMOutput/
                                            RelationshipResult/PermanentNoteCandidate
 - LangChain chat client (via zettel.llm.get_llm) - Actual LLM invocation (provider-configurable:
                                            OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible)
 - SQLite (via StateDB)                  - concepts/notes/note_connections/llm_cache/runs tables
 - ChromaDB (via VectorIndex)            - permanent_notes collection (dense vector storage)
 - Filesystem (vault .md files)          - 30_Permanent/ writes + managed-block edits to targets
```

Note: several imports (`zettel.usage`, `zettel.assets`, `zettel.progress`, and parts of `zettel.vault`) are **function-local ("lazy") imports** rather than module-top-level imports — a pattern used consistently elsewhere in this codebase, likely to avoid import cycles between `connector`, `usage`, and `vault`/`state`, and/or to keep CLI startup light by deferring heavier submodules until actually needed.

---

## 6. Afferent and Efferent Coupling

Analysis unit: top-level functions in `connector.py` (the module has no classes; Python function-level coupling is the natural analysis granularity here). Afferent = number of distinct call sites *within this module* invoking the function (a proxy for internal reuse/fan-in); Efferent = number of distinct external symbols (other modules' functions/classes) the function calls directly (fan-out / dependency surface). `run_connect`'s afferent coupling additionally counts external call sites (CLI, web_app) since it is the module's only public API.

| Function | Afferent Coupling | Efferent Coupling | Critical |
|----------|-------------------|--------------------|----------|
| `run_connect` | 3 (cli.py, web_app.py x2, run-all path) | 9 (`StateDB.start_run`, `usage.begin_run`, `llm.get_llm`, `llm.load_prompt_parts`, `Retriever.__init__`, `progress.report`, `_process_candidate`, `usage.get_tracker`, `vault.sync_source_costs_to_vault`) | High |
| `_process_candidate` | 1 (`run_connect`) | ~20 (`StateDB.get_concept/get_source/get_chunk/get_asset/get_note/upsert_note/upsert_concept/update_note_embedding`, `hashing.*` x4, `llm.*` x3, `Retriever.search_notes`, `vault.build_permanent_note_body/note_filename/safe_write_note`, `usage.*` x4, `_literature_ref_for_chunk`, `_fallback_image_ids`, `_build_rag_context`, `_build_candidate_images_context`, `_resolve_connections`, `_resolve_images`, `_needs_ptbr_fix`/`_apply_ptbr_guard`, `_parse_permanent_note_output`, `_persist_and_backlink`, `ULID()`) | Very High |
| `_literature_ref_for_chunk` | 1 (`_process_candidate`) | 3 (`StateDB.get_chunk`, `vault.literature_chunk_wikilink_for_row`, `vault.literature_index_stem`) | Medium |
| `_build_rag_context` | 1 (`_process_candidate`) | 2 (`StateDB.get_note`, `vault.permanent_wikilink`) | Medium |
| `_resolve_connections` | 1 (`_process_candidate`) | 2 (`StateDB.get_note`, `vault.permanent_wikilink`) | Medium |
| `_resolve_images` | 1 (`_process_candidate`) | 1 (`StateDB.get_asset`) | Low |
| `_fallback_image_ids` | 1 (`_process_candidate`) | 2 (`StateDB.get_chunk`, `assets.asset_ids_in_text`) | Low |
| `_build_candidate_images_context` | 1 (`_process_candidate`) | 1 (`StateDB.get_asset`) | Low |
| `_persist_and_backlink` | 1 (`_process_candidate`) | 4 (`StateDB.upsert_note_connection`, `StateDB.get_note`, `vault.permanent_wikilink`, `vault.safe_update_managed_blocks`, `_inverse_relation`, `_merge_backlink`) | High |
| `_merge_backlink` | 1 (`_persist_and_backlink`) | 1 (`vault.read_managed_block`) + filesystem read | Low |
| `_inverse_relation` | 2 (`_persist_and_backlink`; also exported for tests) | 0 (pure dict lookup) | Low |
| `_relation_type_value` | 2 (`_resolve_connections`, `_persist_and_backlink`) | 0 (stdlib `enum.Enum` only) | Medium |
| `_needs_ptbr_fix` | 1 (`_process_candidate`) | 0 (pure string logic) | Low |
| `_apply_ptbr_guard` | 1 (`_process_candidate`) | 3 (`llm.load_prompt_parts/fill_template/call_llm`, `llm.extract_json`) | Medium |
| `_parse_permanent_note_output` | 1 (`_process_candidate`) | 2 (`llm.extract_json`, `PermanentNoteLLMOutput(**data)`) | Medium |

**Reading the table**: `_process_candidate` is the module's clear hotspot — very high efferent coupling (it is the orchestration hub touching nearly every dependency listed in §5) but low afferent coupling (called from exactly one place). This is expected and appropriate for a "workflow orchestrator" function in a pipeline stage, but its size (~230 lines, one long linear sequence with several early-return branches) is itself a maintainability concern independent of the coupling metric — see Technical Debt. `_persist_and_backlink` is the second-highest-risk node: it is the sole point where the note graph (SQLite `note_connections`) and vault backlink prose can drift out of sync if one write succeeds and the other doesn't (no transaction spans both).

---

## 7. Endpoints

Not applicable — `connector.py` exposes no REST/GraphQL/gRPC/CLI-flag-parsing surface of its own. It is a pure Python library module whose only public entry point is the function `run_connect(cfg, db, idx, candidates, *, observer=None) -> list[str]`, invoked in-process by `zettel/cli.py`'s `connect` Typer command and `run-all` orchestrator, and by `zettel/web_app.py`'s job dispatcher (itself behind FastAPI routes documented in the `web` component's own analysis, not this one).

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| LLM provider (via `zettel.llm.call_llm`) | External Service | Prompt 2 — compose the structured permanent note (accept/reject + full body) | Provider-native SDK (LangChain wrapper: OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible per config) | JSON (structured `PermanentNoteLLMOutput` schema, extracted from possibly-fenced Markdown response) | Whole-candidate `try/except`: any exception during call/parse is logged and the candidate is skipped (returns `None`), run continues to next candidate |
| LLM provider (PT-BR guard, second call) | External Service | Best-effort re-translation of note fields flagged as English-contaminated | Same as above | JSON in/out (subset of fields) | Isolated `try/except` inside `_apply_ptbr_guard`; failure logs a warning and silently keeps the original (uncorrected) output — does not abort note creation |
| SQLite (`StateDB`) | Internal Datastore | Source of truth for candidate eligibility, chunk/source/note/asset lookups, connection graph edges, LLM response cache, cost/run accounting | In-process SQLite via sqlite3 (no network) | Row dicts / SQL | No explicit retry; SQLite errors propagate as uncaught exceptions (would surface as the broad `except Exception` around the LLM-call block only if raised there; DB writes after that block are unguarded) |
| ChromaDB (`VectorIndex`) | Internal Datastore | Dense vector storage/query for note similarity (`permanent_notes` collection); consumed via `Retriever` for RAG context, written via `idx.upsert_permanent_note` | In-process/embedded Chroma client | Embeddings + metadata dict (str/int/float/bool only, per project convention) | `Retriever._vector_notes` wraps Chroma query in `try/except`, logs+degrades to empty results; the *write* path (`idx.upsert_permanent_note`) has no local guard in `connector.py` — a Chroma write failure would propagate uncaught |
| Vault filesystem (Obsidian `.md` files) | Internal Integration | Persist new/updated ZTL notes (`30_Permanent/`); patch `auto-backlinks` managed blocks on *other* existing notes | Local filesystem I/O (`pathlib.Path`) | Markdown + YAML frontmatter | `_persist_and_backlink` defensively skips backlink writes when target has no path or the file is missing (`if not target_path.exists(): continue`) — a broken/missing target note silently drops that one backlink without failing the run; the primary note write (`safe_write_note`) has no existence/collision guard (always overwrites) |
| Hybrid Retriever (`zettel.retrieval.Retriever`) | Internal Integration | RAG context assembly: fuses ChromaDB dense search + SQLite FTS5 BM25 (+ optional graph expansion) to surface related existing notes before Prompt 2 is called | In-process function calls | `NoteSearchResult.hits: list[RetrievedNote]` | Delegated to `Retriever`'s own internal guards (vector/BM25 sub-searches individually wrapped and degrade to empty lists); `connector.py` does not add its own error handling around `retriever.search_notes()` — an unexpected exception there would propagate uncaught |
| Cost/usage tracking (`zettel.usage`) | Internal Integration | Per-note and per-run LLM cost/token accounting, cache-hit bookkeeping, progress reporting to CLI/web observers | In-process contextvars + StateDB writes | Dict snapshots (`CostTracker.summary().as_dict()`) | No explicit error handling; assumed always-available (tracker is initialized in `run_connect` before the loop) |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Pipeline / Batch Processor | `run_connect` iterates candidates, delegating each to `_process_candidate`, aggregating results | `connector.py:100-156` | Decouples per-item logic from batch bookkeeping (progress, cost, run lifecycle) |
| Facade / Composition Root | `Retriever` hides ChromaDB + SQLite FTS5 + graph BFS behind one `search_notes()` call | `zettel/retrieval.py`, consumed at `connector.py:207-209` | Single point of truth for "find related notes" logic shared with `ask`/`sync` |
| Checksum-Keyed Cache (memoization via DB) | `compute_llm_call_checksum` + `db.get_cached_llm_response`/`cache_llm_response` around the Prompt 2 call | `connector.py:232-267` | Deterministic, crash-safe LLM cost avoidance — cache key derived purely from inputs, not wall-clock/run-id |
| Content-Hash Gate (skip-if-unchanged) | `compute_embedding_input_hash` comparison before `idx.upsert_permanent_note` | `connector.py:374-384` | Avoids redundant embedding-provider API calls when note content and embedding config are unchanged |
| Managed Block / Safe Merge | `safe_update_managed_blocks` + `_merge_backlink` (dedupe-on-substring) for `auto-backlinks` | `connector.py:520-572`, `zettel/vault.py:142-165` | Lets the pipeline auto-maintain specific sections of a note file while guaranteeing manual edits outside those markers are never overwritten |
| Idempotent Upsert | `db.upsert_note_connection` keyed on `(source_note_id, target_note_id, relation_type)` with `ON CONFLICT ... DO UPDATE` | `zettel/state.py:1168-1179` | Re-running `connect` on the same candidate updates rather than duplicates graph edges |
| Enum-Value Normalization Guard | `_relation_type_value` (module) + defensive `hasattr(rtype, "value")` re-check (vault.py) | `connector.py:66-76`, `zettel/vault.py:726-729` | Two independent layers guard against the `str, Enum` f-string footgun; documents a previously-real bug via regression tests |
| Graceful Degradation / Fallback Chain | Literature ref (granular → index), image ids (LLM → text-scan), PT-BR guard (best-effort, swallow failure) | Multiple (`connector.py:79-94`, `199-204`, `578-625`) | Prioritizes pipeline completion (a note is still produced) over strict correctness of secondary fields when a preferred data source is unavailable |
| Structured LLM Output / Schema-Validated Parsing | `extract_json` (fence-stripping) → `PermanentNoteLLMOutput(**data)` (Pydantic validation) | `connector.py:631-636` | Enforces a hard contract on LLM responses; malformed JSON or schema violations surface as a caught exception per candidate rather than corrupting downstream state |
| Context-Var-Scoped Cost Tracking | `usage.begin_run`/`get_tracker`/`set_source` around the batch loop, before/after snapshot diffing per candidate | `connector.py:106-152`, `241-304` | Attributes LLM cost/tokens to individual notes and sources without threading an explicit tracker object through every function signature |

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `_process_candidate` | Acknowledged, unmitigated prompt-injection surface: LLM-derived candidate text is interpolated into Prompt 2 with no delimiter sanitization (explicit code comment at `connector.py:212-215`) | A crafted source document could, in principle, steer Prompt 2's JSON output (e.g. force `status: accepted` for low-value content, inject misleading `connections`) if the payload survives the extractor's first LLM pass |
| High | LLM-rejection loop | A concept whose Prompt 2 call returns `status: "rejected"` is never marked with a terminal status (`db.update_concept_status` is not called) — it remains `approved` + `note_id IS NULL` and is re-selected by every future `connect` run indefinitely | Wasted repeated processing (query + prompt-fill + logging) for concepts the LLM has already judged unworthy; only the deterministic cache prevents repeated *paid* LLM calls for byte-identical retries, but any RAG-context drift (new notes added elsewhere) invalidates that cache and re-triggers a paid call |
| Medium | `_process_candidate` size/complexity | Single function spans ~230 lines covering literature resolution, image resolution, RAG, prompt assembly, cache lookup, LLM call, PT-BR guard, cost accounting, connection resolution, vault write, SQLite persistence, conditional re-embedding, and backlink propagation, with several early-return branches | High cognitive load for maintenance/onboarding; a bug fix or feature addition risks unintended interaction between unrelated concerns bundled in one function body |
| Medium | Cross-store atomicity | No transaction spans the vault file write (`safe_write_note`), SQLite writes (`upsert_note`, `upsert_concept`), ChromaDB upsert, and graph/backlink writes (`_persist_and_backlink`) — a crash or exception between these steps can leave the vault file, SQLite, and ChromaDB in mutually inconsistent states (e.g. file written but `upsert_concept` never runs, so the concept remains eligible and would be reprocessed, potentially rewriting the same file with a fresh LLM call and new content under the same or a different note_id-derived filename) | Partial-failure recovery relies entirely on the idempotency of individual steps (`upsert_*` calls, "reuse existing note_id" branch) rather than an explicit compensating-transaction or rollback mechanism |
| Medium | `_persist_and_backlink` | Backlink writes are individually best-effort per target note (skipped silently if the target file is missing/no path) but the graph edge in SQLite is always written regardless — meaning `note_connections` can reference a target note that has no corresponding vault backlink, a state a maintainer cannot detect just by reading the vault | Silent graph/vault drift; no logged warning when a backlink is skipped due to a missing target file (unlike other skip paths in the module, which do log) |
| Medium | PT-BR guard heuristic | `_needs_ptbr_fix` is a naive substring count over 8 fixed English filler words with no word-boundary checking (e.g. `"and "` also matches inside other words that happen to contain that substring followed by a space, though rare) and no allowance for legitimate technical English terms the prompt itself explicitly permits (e.g. "framework", "feedback") | Possible false positives triggering an unnecessary second LLM call (cost), or false negatives (fewer than 3 markers) letting genuinely English-contaminated text through undetected |
| Low | `_apply_ptbr_guard` | The corrective LLM call is not covered by the deterministic cache used for Prompt 2, and its `try/except` swallows all exception types uniformly (including, e.g., a `KeyError` from malformed corrected JSON or a genuine network timeout) without differentiating transient vs. permanent failure classes | Every guard-triggering candidate always pays for this second call even on repeated identical re-runs; failure telemetry is coarse (a single warning log line regardless of failure cause) |
| Low | `_needs_ptbr_fix` / `_apply_ptbr_guard` scope | The heuristic only inspects `thesis + definition + intuition`; `example`, `limits`, and `tags`/`title` are excluded from the trigger check even though `_apply_ptbr_guard` does correct `example` and `limits` when the guard does fire | A candidate with English contamination confined to `example` or `limits` alone (all three checked fields clean) will never trigger the guard for those fields |
| Low | Hardcoded relation semantics | `_INVERSE_RELATION` is a fixed dict; adding a new `RelationType` enum member (in `schemas.py`) without also updating this dict silently defaults the new relation's backlink label to `"relacionado"` rather than failing loudly | Silent semantic degradation on schema extension — no compile-time or runtime enforcement that the two are kept in sync |

---

## 11. Test Coverage Analysis

| Component Area | Unit Tests | Integration Tests | Coverage (qualitative) | Test Quality |
|-----------------|------------|--------------------|-------------------------|---------------|
| `_inverse_relation` / `_relation_type_value` (relation normalization) | 3 (`test_inverse_relation_mapping`, `test_inverse_relation_unknown_falls_back`, `test_relation_type_value_from_enum`) | 0 | Good — covers all 6 known relation types, the unknown-fallback path, and the specific enum-vs-str footgun with a documented regression assertion | High: the regression test explicitly asserts the *wrong* behavior (`f"{RelationType.SUPPORTS}" == "RelationType.SUPPORTS"`) as a form of executable documentation of the bug class being guarded against |
| `_resolve_connections` | 3 (`test_resolve_connections_with_known_note`, `test_resolve_connections_with_unknown_note`, `test_resolve_connections_normalizes_enum_relation_type`) | 0 (uses a hand-rolled `_FakeDB` stub, not a real `StateDB`) | Good for the wikilink-resolution branch logic (known vs. unknown note, enum normalization); does not exercise real SQLite integration | Good assertions on both the happy path and the enum-normalization edge case; stub-based isolation is appropriate for pure resolution logic |
| `build_permanent_note_body` rendering (via `vault.py`, exercised from connector's test file) | 4 (`test_build_permanent_note_body_with_enum_relation_type`, `..._with_connections`, `..._without_connections`, `..._with_figures`) | 0 | Good coverage of connection rendering (with/without description, with/without any connections) and image/figure embedding | Good: asserts exact substrings expected in rendered Markdown, including a specific line-ending check (`contradicts_line.endswith("(contradicts)")`) to catch trailing-whitespace/format regressions |
| `_build_rag_context` | 3 (`test_build_rag_context_two_groups`, `..._only_seeds_no_graph_heading`, `..._empty`) | 0 (constructs `RetrievedNote` dataclasses directly, `_FakeDB` for note lookups) | Good — covers both provenance groups present, seeds-only (graph heading absence asserted), and the empty-result sentinel string | Good: explicitly asserts the *absence* of a heading, not just presence of expected content — a meaningful negative assertion |
| `_fallback_image_ids` / `_resolve_images` | 2 (`test_fallback_image_ids_from_chunk_text`, `test_fallback_image_ids_empty_when_no_paths`) | 2 (same two tests use a **real** `StateDB` against a `tmp_path` SQLite file, exercising `upsert_source`/`upsert_chapter`/`upsert_chunk`/`upsert_asset`) | Good for the two documented branches (image path present in chunk text vs. absent); does not test the "LLM already provided `relevant_image_ids`" precedence branch (that path is only exercised implicitly, not directly asserted) at this test file's level | Good: uses real `StateDB` rather than a stub for this pair, giving genuine integration coverage of the SQL joins involved (`get_chunk`, `get_assets_for_source` via `asset_ids_in_text`) |
| `run_connect` / `_process_candidate` (full orchestration: LLM call, cache, PT-BR guard, vault write, embedding, backlink propagation) | 0 | 0 | **Not covered** — no test in `tests/test_connector.py` exercises `run_connect` or `_process_candidate` directly; the only reference elsewhere is `tests/test_web_state.py::test_run_all_dispatches_every_phase_in_order`, which **monkeypatches `connector.run_connect` entirely** (replacing it with a stub lambda) purely to assert dispatch ordering in `run-all`, not to test connector's actual behavior | This is the highest-risk coverage gap: the core orchestration function — LLM call/cache interplay, the late-stage rejection path, the PT-BR guard trigger/fallback, the conditional re-embedding gate, and the full `_persist_and_backlink` write sequence — has zero direct automated test coverage as of this analysis. All verified behavior for these paths is inferred from source reading, not confirmed by a passing test suite |
| `_literature_ref_for_chunk` (granular-vs-index fallback) | 0 | 0 | **Not covered** — no test file references this function by name | Untested branch logic for one of the module's more important business rules (literature citation resolution) |
| `_needs_ptbr_fix` / `_apply_ptbr_guard` (PT-BR guard heuristic + correction call) | 0 | 0 | **Not covered** | The heuristic's marker-count threshold and the guard's JSON round-trip/failure-swallowing behavior are entirely unverified by tests |
| `_persist_and_backlink` / `_merge_backlink` (graph edge + backlink propagation, dedupe) | 0 | 0 | **Not covered** directly (only `_resolve_connections`, a sibling function, is tested) | The dedupe logic in `_merge_backlink` and the file-existence/path-presence skip conditions in `_persist_and_backlink` have no dedicated test |
| Deterministic LLM cache path (`compute_llm_call_checksum` cache hit/miss for Prompt 2) | 0 (in this component's test file) | 0 | **Not covered** at the connector level (the `hashing.py` functions themselves may have their own unit tests in `tests/test_hashing.py`, outside this component's scope, but the cache *integration* within `_process_candidate` is untested) | — |

**Test file locations**:
- `tests/test_connector.py` (249 lines) — the component's dedicated unit test file; exclusively tests pure/stubbed helper functions (`_inverse_relation`, `_relation_type_value`, `_resolve_connections`, `_resolve_images`, `_fallback_image_ids`, `_build_rag_context`) and the `vault.build_permanent_note_body` rendering function it depends on. It deliberately does not construct a real LLM client, `VectorIndex`, or exercise `run_connect`/`_process_candidate`.
- `tests/test_web_state.py` (`test_run_all_dispatches_every_phase_in_order`, around line 72) — references `connector.run_connect` only to monkeypatch it as an inert stub, verifying the web `run-all` dispatcher calls it in the correct pipeline order and forwards the expected `candidates` argument; provides zero coverage of connector's actual internal logic.

**Summary**: coverage is strong and well-designed for the module's pure, easily-isolated helper functions (relation normalization, connection/image resolution, RAG context formatting), including good real-`StateDB` integration coverage for the image-fallback path. However, the module's central orchestration logic — everything inside `_process_candidate` beyond the pieces that are extracted into separately-tested helpers (the LLM call/cache flow, the late rejection path, the PT-BR guard trigger, the vault-write-then-persist-then-backlink sequence, and conditional re-embedding) — has no direct test coverage in this codebase as inspected. This is a materially higher-risk gap than the isolated helper functions, given this is also the function flagged in the coupling analysis (§6) as "Very High" efferent coupling.

---

## Absolute path of this report

`D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-connector-2026-08-30_10-22-26.md`

Component analyzed: **connector** (`zettel/connector.py`)
