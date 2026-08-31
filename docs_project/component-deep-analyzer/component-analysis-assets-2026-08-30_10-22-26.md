# Component Deep Analysis Report — `assets`

**Component**: `zettel/assets.py`
**Analysis date**: 2026-08-30
**Scope**: Entire project root (`D:/projetos/zettel_app`), excluding `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, `.pytest_cache`.

---

## 1. Executive Summary

`zettel/assets.py` (581 lines) is the image/attachment subsystem of the Zettelkasten pipeline. It is not a standalone service but a **library module** consumed by four pipeline stages (`harvester.py`, `extractor.py`, `connector.py`, `article.py`) plus the web UI (`web_app.py`/`web.py`) and CLI (`cli.py`). It has exactly two responsibilities, both stated in its module docstring:

1. **Extraction (harvest-time)** — pull images out of source documents (PDF via Docling, local references in Markdown), save them content-addressed under the vault's `90_Assets/` folder, rewrite the source text to reference the saved files, and hand back metadata (`checksum`, `path`, `context_snippet`) for DB registration.
2. **Multimodal description (extract-time)** — for every `pending` asset row, call a multimodal LLM (image + surrounding text context) to produce a short PT-BR description, cached deterministically so re-runs never re-pay for an already-described image, and resilient to provider rate limits (429/TPM) without polluting the corpus with false `failed` states.

The component owns one database table (`assets` in `state.py`, though the CRUD methods themselves live on `StateDB`, not in this module) and one vault folder convention (`90_Assets/`). It has no HTTP endpoints of its own; it is invoked in-process. Its output (`asset_id`, `path`, `description`) becomes an input to three downstream generation paths: Prompt 1 (`extractor.py`, `images_context` in `literature_note.md`), Prompt 2 (`connector.py`, `relevant_image_ids` → `## Figuras` in ZTL notes), and the long-form `article.py` writer (`figure_asset_ids` → embedded figures).

**Key findings**:
- Content-addressing (SHA-256 of image bytes → filename) gives free deduplication and deterministic re-runs — a deliberate design choice stated in the module docstring and validated by `test_identical_images_dedup_to_same_file`.
- The rate-limit handling in `describe_pending_assets` is unusually mature for this codebase area: provider-hint parsing, exponential backoff, a "never mark `failed` on 429" rule, and a circuit-breaker (`rate_limit_abort_after`) that stops the whole batch after N consecutive exhausted retries so a saturated TPM window doesn't cascade into false failures.
- The component is entirely feature-gated by `cfg.images.enabled` (default `false` in the Pydantic schema, `true` in the operational `config/config.yaml`) — every public entry point no-ops immediately when disabled.
- Chapter resolution (`_resolve_chapter_id`) is a linear substring search over chapter text repeated per image, and `reresolve_asset_chapters` repeats it per asset — both are used to correct orphaned `chapter_id`s after interrupted/incomplete harvests or rechunks.
- Test coverage of the module itself (`tests/test_assets.py`) is strong (14 focused tests covering every public function and the rate-limit state machine), but downstream integration (how `images_context`/`relevant_image_ids`/article figures actually get consumed) is covered in the *consumers'* test files, not here.

---

## 2. Data Flow Analysis

Two independent flows exist — extraction (write path) and description (read-modify path) — plus a set of read-only lookups used by later phases.

### 2.1 Extraction flow — PDF (Docling) via harvest

```
1. harvester.py: DocumentConverter.convert() (Docling) produces `result.document` + Markdown text
2. harvester.py: if cfg.images.enabled -> assets.extract_docling_images(cfg, result.document, text)
3. assets.py: iterate result.document.pictures in order
4.   -> assets._docling_pil(pic, document): picture.get_image(document) -> PIL.Image
5.   -> size filter: drop if width/height < cfg.images.min_width/min_height (placeholder removed)
6.   -> assets._png_bytes(pil_image): PIL -> PNG bytes
7.   -> assets.sha256_hex_bytes(data): content checksum
8.   -> assets._save_image(vault_path, data, ".png"): write to 90_Assets/img-<short_hash>.png (idempotent — skip write if file exists)
9.   -> assets._context_snippet(text, placeholder_pos, cfg.images.context_chars): normalized text window around the `<!-- image -->` placeholder
10.  -> replace the placeholder with `![Imagem](90_Assets/img-....png)` (document order)
11. harvester.py: metadata["_images"] = images; text (with rewritten refs) persisted as sources.extracted_text
12. harvester.py: chapters computed from the rewritten text -> assets.register_assets(db, source_id, chapters, images)
13. assets.py: for each image, assets._resolve_chapter_id() finds which chapter's text contains the image path
14. assets.py: db.upsert_asset(...) — one row per image, status='pending'
```

### 2.2 Extraction flow — local Markdown images via harvest

```
1. harvester.py: reads Markdown source body
2. harvester.py: assets.extract_markdown_images(cfg, body, source_file)
3. assets.py: regex `_MD_IMAGE_RE` finds every ![alt](ref) in body
4.   -> skip remote refs (http/https) unchanged
5.   -> resolve ref relative to source_file.parent; skip silently if missing/unreadable
6.   -> read bytes, sha256_hex_bytes, assets._save_image() into 90_Assets/
7.   -> record {checksum, path, context_snippet} (context = _context_snippet(body, match.start(), cfg.images.context_chars))
8.   -> rewrite reference to `![Imagem](90_Assets/img-....<ext>)`
9. harvester.py: same register_assets() DB registration path as 2.1 step 12-14
```

### 2.3 Description flow — extract phase

```
1. extractor.py: run_extract() calls assets.describe_pending_assets(cfg, db, observer=observer) before/around chunk processing
2. assets.py: db.get_pending_assets() -> all assets with status='pending' (across all sources)
3. assets.py: load prompts/image_description.md once (load_prompt_parts), compute prompt_hash
4. assets.py: instantiate multimodal LLM once via _get_multimodal_llm(cfg, model) (provider from cfg.llm / cfg.images.model override)
5. per asset (loop):
   a. compute call_checksum = sha256(prompt_hash | image_checksum | normalized(context) | model)
   b. db.get_cached_llm_response(call_checksum) -> if hit: record_cache_hit(), db.update_asset_description(cached), continue (no LLM call)
   c. else: pacing (cfg.images.min_interval_seconds) via time.sleep if needed
   d. _describe_with_rate_limit_retry() -> _describe_one(): base64-encode image, fill prompt template with {context}, build SystemMessage+HumanMessage(text+image_url), apply_prompt_cache_hints(), llm.invoke()
   e. usage/cost recorded via zettel.usage.record_llm + zettel.pricing.estimate_llm_cost
   f. db.cache_llm_response(call_checksum, ..., description); db.update_asset_description(asset_id, description, call_checksum, status='described')
   g. on RateLimitExhausted: leave status='pending', track consecutive_exhausted, abort whole batch after cfg.images.rate_limit_abort_after consecutive exhaustions
   h. on any other exception: db.update_asset_description(asset_id, "", call_checksum, status='failed')
6. assets.py: usage.clear_progress(); return count described
```

### 2.4 Consumption flow — downstream readers (not part of this component, but its direct clients)

```
1. extractor.py::_build_images_context(): db.get_assets_for_source() filtered by chapter_id + page proximity (±1 page) -> textual "Imagens disponiveis..." block injected into Prompt 1's {images_context} -> LLM may reference asset_id in relevant_image_ids
2. extractor.py::_images_for_chunk() / asset_ids_in_text(): fallback deterministic path — if the LLM left relevant_image_ids empty, scan the chunk text for any asset path literally present in it
3. connector.py::_resolve_images(): resolves relevant_image_ids (or the same text-scan fallback) into {path, description} dicts consumed by vault.build_permanent_note_body() -> "## Figuras" section with ![[path]] embeds
4. article.py: db.get_assets_for_source() -> CatalogAsset objects -> ranked by frequency, capped at max_figures, embedded into generated ART notes via figure_asset_ids
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Feature Gate | All image extraction/description is a no-op unless `cfg.images.enabled` is true | assets.py:79, assets.py:119, assets.py:342 |
| Content Addressing | Image filename is `img-{short_hash(sha256(bytes), 16)}{ext}`; identical bytes always map to the same path | assets.py:41-43, assets.py:46-54 |
| Idempotent Write | An image file is only written to disk if it does not already exist at its content-addressed path | assets.py:52 |
| Remote Image Exclusion | Markdown images referenced via `http://`/`https://` are never downloaded or copied | assets.py:86-87 |
| Missing/Unreadable Local Image | A Markdown image reference to a non-existent file, non-file path, or unreadable file is left untouched in the text | assets.py:89-94 |
| Minimum Picture Size | Docling pictures smaller than `cfg.images.min_width` × `cfg.images.min_height` are dropped (icons/logos excluded), and their `<!-- image -->` placeholder is removed | assets.py:144-148 |
| Failed Picture Extraction | A Docling picture whose `get_image()` call fails (exception) is silently skipped (`skipped_fail` counter, placeholder left as-is) | assets.py:140-143, 176-183 |
| Placeholder Replacement Order | Docling `<!-- image -->` placeholders are replaced strictly in document order, matching pictures 1:1 to placeholders as encountered | assets.py:137-163 |
| Orphan Image Fallback | If no `<!-- image -->` placeholder remains for a picture, its reference is appended to the end of the text instead | assets.py:154, 162-163 |
| Context Snippet | Each extracted image gets a normalized text window (`cfg.images.context_chars` on each side) around its position, used both for chapter resolution search space and as multimodal LLM grounding | assets.py:62-65 |
| Chapter Resolution | An image is assigned to the first chapter whose text contains the image's content-addressed path (unambiguous because filenames are content-unique) | assets.py:256-262 |
| Unresolved Chapter | If no chapter text contains the image path, `chapter_id` is stored as `None` (image still registered, just unbound from a chapter) | assets.py:256-262 (implicit `None` return) |
| Chapter Re-resolution | After rechunk/heading changes, every asset of a source is re-bound to whichever chapter currently contains its path; only assets whose resolved chapter actually changed are updated (write-minimization) | assets.py:221-241 |
| Deterministic LLM Cache Key | A description call is cached by hash of (prompt template, image content checksum, normalized context, model name) — changing any of these forces a fresh LLM call | assets.py:382-384 |
| Cache Hit = Zero Cost | A cached description is applied without any LLM invocation and recorded as a cache hit (`$0` cost) for cost tracking | assets.py:385-392 |
| Pacing Between Calls | A minimum interval (`cfg.images.min_interval_seconds`) is enforced between successive multimodal LLM calls to avoid bursting the provider's tokens-per-minute (TPM) limit | assets.py:394-397 |
| Rate-Limit Detection | A rate-limit error is recognized by inspecting the exception's class name, message text (`rate_limit`, `429`, `tokens per min`, `tpm`+`limit`), and by walking `__cause__`/`__context__` chains to unwrap provider SDK wrapping | assets.py:278-303 |
| Rate-Limit Wait Time | The wait before retrying prefers the provider's own "try again in Xms/Xs/Xm" hint (parsed via regex), floored at 0.6s, else falls back to exponential backoff (`2^attempt` seconds), always capped at `cfg.images.rate_limit_backoff_max` | assets.py:306-329 |
| Never Mark Rate-Limited Asset as Failed | An asset that exhausts all retries on rate limits is left in `pending` status, not `failed` — so no manual `retry-failed --assets` is required, just re-running `extract` | assets.py:418-424 |
| Batch Circuit Breaker | After `cfg.images.rate_limit_abort_after` consecutive rate-limit exhaustions (across different assets), the entire remaining batch is aborted rather than continuing to hammer a saturated TPM window | assets.py:425-433 |
| Cooldown After Exhaustion | Before moving to the next asset following a rate-limit exhaustion, a fixed cooldown (`min(backoff_max, 5.0)` seconds) is applied | assets.py:434-436 |
| Retry Budget Per Image | Each image gets `cfg.images.rate_limit_max_retries + 1` total attempts before being counted as a consecutive exhaustion | assets.py:462-481 |
| Non-Rate-Limit Failure | Any exception that is not recognized as a rate limit marks the asset `status='failed'` immediately (single attempt, no retry) and resets the consecutive-exhaustion counter | assets.py:437-440 |
| Missing Image File at Describe Time | If the vault-relative image path no longer exists on disk when its turn comes to be described, the asset is marked `failed` immediately, without any LLM call | assets.py:376-379 |
| Provider Selection for Multimodal | The multimodal LLM provider/model is resolved from `cfg.images.model` (if set) else `cfg.llm.model`, and instantiated per the provider family (OpenAI-compatible, Anthropic, Ollama, Gemini) with SDK-level retries disabled (`max_retries=0`) because this module owns retry logic itself | assets.py:353, 484-516 |
| Unsupported Provider | Any `cfg.llm.provider` outside {OpenAI-compatible, anthropic, ollama, gemini} raises `ValueError` when attempting multimodal description | assets.py:516 |
| Text-Presence Fallback for Relevance | When the LLM leaves `relevant_image_ids` empty for a chunk/concept, downstream callers (extractor, connector) fall back to `asset_ids_in_text`, which matches any asset whose vault-relative path literally appears in the given text | assets.py:244-253 |

### Detailed breakdown of the business rules

---

### Business Rule: Feature Gate (`cfg.images.enabled`)

**Overview**:
Every public entry point in `assets.py` — `extract_markdown_images`, `extract_docling_images`, and `describe_pending_assets` — checks `cfg.images.enabled` as its very first statement and returns a no-op result (`(body, [])`, `(text, [])`, or `0`) when the flag is off.

**Detailed description**:
This is a single boolean switch (Pydantic default `False` in `zettel/config.py:96`, but overridden to `True` in the operational `config/config.yaml`) that determines whether the entire image subsystem participates in a harvest/extract run at all. When disabled, Docling's `generate_picture_images` pipeline option is never turned on (see `harvester.py:1197-1199`, itself gated on the same flag), so no picture objects are even produced by Docling in the first place — the gate is checked twice, once to avoid the (expensive) Docling image generation and again inside `extract_docling_images` as a defensive no-op. For Markdown, the gate is checked once inside `extract_markdown_images` since there is no separate "generation" step to skip.

The practical effect is that projects that don't need multimodal support pay zero extra cost: no image files are written to `90_Assets/`, no `assets` rows are created, and `describe_pending_assets` finds nothing to do (or is effectively skipped by callers that check the flag first, as `extractor.py` still calls it but it returns `0` immediately). This also means a vault created with images disabled can retroactively enable them — after flipping the flag, only *newly harvested* sources will get image extraction; previously harvested sources need `rechunk`/`set-paging`-style reprocessing (not automatic) to pick up images from the same source files.

**Rule workflow**:
`cfg.images.enabled == False` → `extract_markdown_images`/`extract_docling_images` return unchanged text and an empty image list → `register_assets` receives an empty list and logs "Nenhuma imagem para registrar" → no `assets` rows are ever created for that source → `describe_pending_assets` always finds `db.get_pending_assets()` empty for that vault (unless another config enabled the flag between harvests) → all downstream `images_context`/`relevant_image_ids`/`figure_asset_ids` mechanisms degrade gracefully to "no images" since their queries return empty lists.

---

### Business Rule: Content-Addressed Storage and Deduplication

**Overview**:
Images are never named after their source position or an incrementing counter; they are named after the SHA-256 hash of their own bytes, truncated via `short_hash(checksum, 16)`, e.g. `90_Assets/img-1a2b3c4d5e6f7089.png`.

**Detailed description**:
`_save_image` computes `sha256_hex_bytes(data)`, derives the relative path via `_asset_relpath`, and only calls `dest.write_bytes(data)` if the destination does not already exist (`if not dest.exists()`). This has three compounding consequences documented explicitly in the module docstring: (1) re-running harvest on the same file is deterministic — the rewritten Markdown text hashes identically run over run, which matters for the extraction-checksum duplicate-detection layer in `harvester.py`; (2) two visually identical images appearing at different positions in the same or different documents collapse to one file on disk, verified directly by `test_identical_images_dedup_to_same_file`; (3) the `assets` table can still hold two distinct rows (different `asset_id = f"{source_id}::img::{short_hash(checksum)}"`, since `asset_id` is namespaced per source) pointing at the same physical file, which is intentional — asset identity is per-source-occurrence, but physical storage is deduplicated globally.

Because `asset_id_for` embeds `short_hash(image_checksum)` (default 8-char) while the file path uses `short_hash(image_checksum, 16)`, the two hashes are of different lengths but derived from the same checksum — collisions in the (much shorter) `asset_id` suffix are only a display/ID concern, not a storage-integrity concern, since the file path always uses the longer, lower-collision-risk hash.

**Rule workflow**:
Image bytes read → `sha256_hex_bytes` → `_asset_relpath(checksum, ext)` builds `90_Assets/img-{16-char hash}{ext}` → if file exists at that path, skip the write (content already stored, whether from this source or a previously harvested one) → return the relpath either way → caller records `{checksum, path, context_snippet}` regardless of whether a new file was actually written.

---

### Business Rule: Minimum Picture Size Filter (Docling)

**Overview**:
Docling-extracted pictures smaller than `cfg.images.min_width` × `cfg.images.min_height` pixels (defaults 64×64) are treated as noise (icons, logos, decorative rules) and are dropped entirely — not saved, not registered, and their placeholder text is removed from the document.

**Detailed description**:
PDF exports frequently contain small decorative graphics that Docling detects as "pictures" but that carry no informational content worth describing with an LLM call (which has a real dollar cost per the multimodal pricing path). The size check happens after the PIL image has already been successfully extracted (`_docling_pil` succeeded) but before any bytes are hashed or written, so undersized images incur no disk I/O and no DB row. The placeholder removal (`text = text[:idx] + text[idx + len(_DOCLING_PLACEHOLDER):]`) is important: if this were skipped, the literal string `<!-- image -->` would leak into the final extracted/chunked text and eventually into LIT/ZTL note bodies as visible clutter.

The counters `skipped_small` and `skipped_fail` are aggregated and logged once per document (`"Docling: imagens concluidas — %d salvas, %d pequenas ignoradas, %d falhas"`), giving operators visibility into how many pictures were filtered without needing per-image debug logging.

**Rule workflow**:
Picture extracted as PIL image → `pil_image.width < min_width or pil_image.height < min_height` → increment `skipped_small` → if a placeholder exists at the current search position, excise it from the text → `continue` to the next picture (this picture consumes no further processing and is not registered as an asset).

---

### Business Rule: Chapter Resolution by Content-Addressed Path Search

**Overview**:
Each registered image is bound to the chapter whose extracted text contains that image's exact vault-relative path string; this is possible only because paths are content-addressed and therefore unique per distinct image content.

**Detailed description**:
`_resolve_chapter_id` iterates `chapters` in order and returns the first `f"{source_id}::ch{ch_idx:03d}"` whose `chapter["text"]` contains `image_path` as a substring. This is a deliberate simplification enabled by content addressing: because the rewritten Markdown text (post `extract_docling_images`/`extract_markdown_images`) already contains the literal string `90_Assets/img-....png` at the image's original position, and because that string is guaranteed unique to this specific image content, a substring search is sufficient — there's no need for position tracking, offsets, or a separate mapping table. If an image's path does not appear in any chapter's text (e.g., a chunking bug, or an image caught in a header stripped from chapter bodies), `_resolve_chapter_id` returns `None`, and `register_assets` still creates the asset row but with `chapter_id=None`, meaning the image would not surface in Prompt 1's per-chapter `images_context` (see `_build_images_context` filtering) although it remains retrievable by `asset_ids_in_text` and by `article.py`'s corpus-wide asset queries.

`reresolve_asset_chapters` reuses the same resolution function to repair `chapter_id`s after structural changes — most importantly, interrupted harvests. The docstring calls out a concrete failure mode: a harvest that registered assets and then crashed partway through `_chunk_and_persist` could leave images pointing at `chapter_id`s like `ch026` when only `ch000`-`ch012` ever got persisted; without re-resolution those orphaned images would silently never appear in `images_context` because `_build_images_context`'s chapter-id equality filter would never match. `reresolve_asset_chapters` is invoked automatically at the end of every full chunking pass (`_finalize_source_chunking` in `harvester.py`) and only writes an update when the newly resolved chapter differs from the stored one, minimizing DB writes.

**Rule workflow**:
`register_assets`/`reresolve_asset_chapters` called with `(source_id, chapters, images_or_existing_assets)` → for each image/asset, walk `chapters` in list order → return the index of the first chapter whose `text` contains the image's `path` → format as `{source_id}::ch{idx:03d}` → `register_assets` persists this directly via `upsert_asset`; `reresolve_asset_chapters` compares against the asset's current `chapter_id` and only calls `db.update_asset_chapter` if it changed, incrementing a return counter for logging.

---

### Business Rule: Deterministic, Cost-Free Description Caching

**Overview**:
Every multimodal description call is keyed by a checksum of `(prompt template hash, image content checksum, normalized context, model name)`, stored in the shared `llm_cache` table, so identical inputs never trigger a second paid LLM call.

**Detailed description**:
`call_checksum = sha256_hex(f"{prompt_hash}|{image_checksum}|{sha256_hex(normalize_text_for_hash(context))}|{model}")` composes four independent variability sources into one cache key: the prompt template (so editing `prompts/image_description.md` invalidates all cached descriptions, forcing regeneration under the new instructions); the image's own content hash (so the same physical image asked about via two different `asset_id`s — e.g., appearing under two different sources — shares a cached description if the surrounding context also matches); the *normalized* context snippet (whitespace/PDF-dehyphenation-normalized via `normalize_text_for_hash`, so trivial formatting differences in the surrounding text don't force a needless re-describe); and the model name (switching multimodal models invalidates the cache, since different models produce different quality/style descriptions).

On a cache hit, `db.get_cached_llm_response(call_checksum)` returns the previously stored description text, `record_cache_hit(label=..., model=model)` logs it to the cost tracker as a `$0` operation (distinguishing it from a real paid call in run-level cost reporting), and `db.update_asset_description` is called exactly as it would be after a live LLM call — meaning cached and live paths converge to identical final DB state, with the only observable difference being cost/timing. This is what makes `zettel extract` idempotent with respect to images: re-running extract after a partial failure, or after `retry-failed --assets` resets some assets to `pending`, never re-pays for images whose describing conditions haven't changed.

**Rule workflow**:
Asset dequeued from `get_pending_assets()` → compute `call_checksum` from the four inputs → `db.get_cached_llm_response(call_checksum)` → if present: `record_cache_hit`, `db.update_asset_description(asset_id, cached, call_checksum)` (status defaults to `'described'`), `described += 1`, `consecutive_exhausted` reset to 0, loop continues to next asset with **no LLM invocation and no pacing sleep**; if absent: proceed through the pacing/rate-limit/LLM-call path, and on success call `db.cache_llm_response(call_checksum, "(image)", description)` before updating the asset, populating the cache for future runs.

---

### Business Rule: Rate-Limit Resilience — Retry, Backoff, and Circuit Breaker

**Overview**:
Multimodal description calls are wrapped in a three-tier resilience mechanism: per-image retry with provider-aware backoff, a "leave pending, never fail" policy specifically for exhausted rate limits, and a batch-level circuit breaker that halts the run after too many consecutive exhaustions.

**Detailed description**:
The problem this rule addresses is stated directly in a code comment: "Visao consome TPM rapido" (vision consumes tokens-per-minute fast) — multimodal calls are token-heavy (base64 image payloads), so they exhaust provider rate limits far faster than text-only calls, and naively marking every 429 as a permanent `failed` status would pollute the corpus with images that are actually fine, just temporarily throttled. `_is_rate_limit_error` performs pattern matching across the exception's type name and string representation, and explicitly walks both `__cause__` and `__context__` exception-chaining attributes (with a `seen` set to guard against reference cycles) because provider SDKs (OpenAI, Anthropic, etc.) frequently wrap the underlying HTTP 429 in one or more layers of custom exception classes.

Within a single image's retry loop (`_describe_with_rate_limit_retry`), each of up to `max_retries + 1` attempts that raises a recognized rate-limit error triggers `_rate_limit_wait_seconds`, which prefers the provider's own textual hint (e.g., "Please try again in 392ms", parsed by `_RETRY_AFTER_RE` and unit-converted in `_parse_retry_after_seconds`) but enforces a **0.6-second floor** over that hint — the code comment explains why: provider hints are often sub-second, but the actual TPM window that caused the throttle may still be full, so honoring a sub-second hint literally would just re-hit the same wall immediately. If no hint is present, the wait falls back to `2^attempt` seconds. Both paths are capped at `cfg.images.rate_limit_backoff_max`. Only after all attempts for one image are exhausted does the function raise `RateLimitExhausted`.

At the batch level, `describe_pending_assets` tracks `consecutive_exhausted` — incremented each time an image's full retry budget is exhausted by rate limits, and reset to 0 on any success (cached or live) or any non-rate-limit failure. Once this counter reaches `cfg.images.rate_limit_abort_after` (default 5), the entire remaining batch is abandoned with a `break` and an error-level log telling the operator to "rode extract de novo apos a janela TPM" (run extract again after the TPM window clears) — the remaining images stay `pending` and are naturally retried on the next `extract` invocation, no manual intervention needed. This differs sharply from the non-rate-limit failure path, where a single unrecognized exception marks that one asset `failed` immediately (no retry) and does *not* abort the batch, since an unrelated per-image error (e.g., a corrupted file) shouldn't stop progress on the rest of the queue.

**Rule workflow**:
LLM call raises exception → `_is_rate_limit_error` classifies it → if not rate-limit or retries exhausted and not rate-limit: re-raise as-is (caught by outer `except Exception` → asset marked `failed`) → if rate-limit and retries exhausted: raise `RateLimitExhausted` (caught by outer `except RateLimitExhausted` → asset stays `pending`, `consecutive_exhausted += 1`) → if `consecutive_exhausted >= abort_after`: log and `break` out of the whole `for idx, asset in enumerate(pending)` loop, ending the function early with whatever `described` count was accumulated so far → otherwise: sleep a fixed cooldown (`min(backoff_max, 5.0)`) and continue to the next asset → if rate-limit and retries remain: sleep `_rate_limit_wait_seconds(...)` and retry the same image.

---

### Business Rule: Remote and Missing Image Handling in Markdown

**Overview**:
`extract_markdown_images` only ever copies *local* image references that resolve to an existing, readable file relative to the source Markdown file; remote URLs and broken/missing local references are passed through completely unmodified.

**Detailed description**:
The regex `_MD_IMAGE_RE` matches any `![alt](ref)` construct regardless of what `ref` is, but the replacement callback immediately short-circuits and returns the original matched text unchanged (`match.group(0)`) in three cases: the reference starts with `http://` or `https://` (a deliberate policy — this module never performs network I/O to fetch remote assets, keeping harvest fully offline-capable and avoiding uncontrolled external fetches during document processing); the resolved local path (`source_file.parent / ref`, resolved) does not exist or is not a regular file; or reading the file raises an `OSError` (permissions, transient FS issues, etc.). In all three cases, no entry is added to the `images` list, meaning no `assets` row will ever be created for that reference and no vault file is written — the original Markdown reference (however broken or however remote) survives verbatim into the harvested source text.

This is a conservative, fail-open design: a bad or unreachable image reference never blocks or crashes the harvest of the surrounding document; it just means that particular image is not part of the multimodal pipeline. There's no logging of skipped remote/missing images in this function (unlike Docling's `skipped_small`/`skipped_fail` counters), which is a minor observability gap noted in the Technical Debt section.

**Rule workflow**:
Regex matches `![alt](ref)` → `ref.strip()` → starts-with http(s) check → if true, return unchanged, no side effects → else resolve `(source_file.parent / ref).resolve()` → existence + is-file check → if false, return unchanged → else `read_bytes()` wrapped in try/except OSError → on success, hash, save, append to `images`, and return the rewritten `![Imagem](90_Assets/...)` replacement text; on `OSError`, return unchanged.

---

## 4. Component Structure

`assets.py` is a single flat module (no sub-package) organized into five clearly delimited sections via comment banners:

```
zettel/
├── assets.py                      # THIS COMPONENT — image extraction + multimodal description
│   ├── § IDs and paths            # asset_id_for, _asset_relpath, _save_image, sha256_hex_bytes, _context_snippet
│   ├── § Markdown image extraction # extract_markdown_images (+ _MD_IMAGE_RE regex)
│   ├── § Docling (PDF) image extraction # extract_docling_images, _docling_pil, _png_bytes (+ _DOCLING_PLACEHOLDER)
│   ├── § DB registration (chapter resolution) # register_assets, reresolve_asset_chapters, asset_ids_in_text, _resolve_chapter_id
│   └── § Multimodal description   # describe_pending_assets, _describe_with_rate_limit_retry, _get_multimodal_llm,
│                                   #   _describe_one, RateLimitExhausted, _is_rate_limit_error,
│                                   #   _parse_retry_after_seconds, _rate_limit_wait_seconds
│
├── state.py                       # owns the `assets` SQL table + all CRUD (upsert_asset, get_asset,
│                                   #   get_assets_for_source, update_asset_chapter, get_pending_assets,
│                                   #   reset_failed_assets, update_asset_description) — NOT in assets.py itself
├── config.py                      # ImagesConfig (cfg.images.*) — the feature-gate and tuning knobs
├── harvester.py                   # calls extract_docling_images / extract_markdown_images / register_assets /
│                                   #   reresolve_asset_chapters (via _finalize_source_chunking)
├── extractor.py                   # calls describe_pending_assets; builds images_context (_build_images_context);
│                                   #   falls back to asset_ids_in_text when LLM leaves relevant_image_ids empty
├── connector.py                   # resolves relevant_image_ids -> {path, description} for ZTL "## Figuras";
│                                   #   same asset_ids_in_text fallback
├── article.py                     # queries get_assets_for_source for figure catalog (CatalogAsset), ranks by
│                                   #   frequency, embeds top figures into generated articles
├── purge_source.py                # deletes on-disk asset files (via get_assets_for_source) when a source is purged
├── vault.py                       # renders `images`/figures into note bodies (build_literature_chunk_note,
│                                   #   build_permanent_note_body) as Obsidian ![[path]] embeds
├── web_app.py                     # "retry_assets" web job -> db.reset_failed_assets()
├── web.py                         # exposes retry_assets as an allowed pipeline operation
├── cli.py                         # `zettel retry-failed --assets` -> db.reset_failed_assets(); doctor checks
│                                   #   prompts/image_description.md exists; status shows "assets" count
├── templates/dashboard.html       # KPI tile for "Assets" count (db.get_stats())
├── templates/pipeline.html        # "Reprocessar assets" pipeline action button
├── config/config.yaml             # operational `images:` block (enabled: true, sizes, pacing, retry knobs)
└── prompts/image_description.md   # the PT-BR system+user prompt template used by describe_pending_assets
```

No test subfolder exists inside the component itself; tests live centrally under `tests/`.

---

## 5. Dependency Analysis

### Internal Dependencies

```
harvester.py ────────► assets.extract_docling_images ─┐
                        assets.extract_markdown_images ├─► assets._save_image ─► filesystem (90_Assets/)
                        assets.register_assets ────────┘        │
                        assets.reresolve_asset_chapters          └─► assets.sha256_hex_bytes / hashing.short_hash

extractor.py ─────────► assets.describe_pending_assets ─► assets._get_multimodal_llm ─► llm.is_openai_compatible /
                                                            │                             llm.normalize_llm_provider
                                                            └─► assets._describe_one ─► llm.apply_prompt_cache_hints /
                                                                                        llm._extract_usage /
                                                                                        llm._resolve_model_name /
                                                                                        pricing.estimate_llm_cost /
                                                                                        usage.record_llm

extractor.py / connector.py ──► assets.asset_ids_in_text ─► state.StateDB.get_assets_for_source

article.py ───────────► state.StateDB.get_assets_for_source (direct, bypasses assets.py's own asset_ids_in_text)

purge_source.py ──────► state.StateDB.get_assets_for_source (direct, for on-disk file deletion)

cli.py / web_app.py ──► state.StateDB.reset_failed_assets (direct — does not call into assets.py)

assets.py ────────────► config.AppConfig (cfg.images.*, cfg.vault_path, cfg.llm.*, cfg.prompts_path)
assets.py ────────────► hashing.normalize_text_for_hash, hashing.sha256_hex, hashing.short_hash
assets.py ────────────► state.StateDB (upsert_asset, get_assets_for_source, update_asset_chapter,
                                        get_pending_assets, update_asset_description) — as a passed-in instance
assets.py ────────────► llm.fill_template, llm.load_prompt_parts, llm.is_openai_compatible,
                         llm.normalize_llm_provider, llm.apply_prompt_cache_hints, llm._extract_usage,
                         llm._resolve_model_name (all imported lazily, function-local, to avoid import cycles)
assets.py ────────────► pricing.estimate_llm_cost
assets.py ────────────► usage.set_progress / clear_progress / record_cache_hit / record_llm
assets.py ────────────► progress.report (observer pattern for web job progress)
```

### External Dependencies

- **Docling** (`docling_document`, `PdfPipelineOptions`, `AcceleratorOptions` — used by `harvester.py`, consumed indirectly by `assets.py` via the `docling_document.pictures` duck-typed interface and `picture.get_image(document)`) — PDF-to-structured-document conversion + picture extraction.
- **Pillow (PIL)** — implicit dependency via `picture.get_image()` return type and `_png_bytes` (`pil_image.save(buf, format="PNG")`); not imported at module top level (kept as an implicit type via `Any`).
- **langchain-openai / langchain-anthropic / langchain-ollama / langchain-google-genai** — one of these is imported lazily inside `_get_multimodal_llm` depending on `cfg.llm.provider`, to construct the multimodal chat model.
- **langchain-core** (`HumanMessage`, `SystemMessage`) — message construction for the multimodal call in `_describe_one`.
- **hashlib** (stdlib) — `sha256_hex_bytes`.
- **base64** (stdlib) — encoding image bytes for the `image_url` data-URI payload sent to the LLM.
- **SQLite** (via `StateDB`) — persistence of the `assets` table; not a direct dependency of `assets.py` (which only calls `StateDB` methods) but is the storage backend.
- **LiteLLM** (via `pricing.estimate_llm_cost`) — cost-per-token pricing lookups for multimodal usage tracking.

---

## 6. Afferent and Efferent Coupling

Analysis unit: Python module/function-group within `assets.py`, plus the surrounding modules that call into it (afferent) or that it calls into (efferent). Since this is a functional module (no classes besides the `RateLimitExhausted` exception), coupling is measured at the function/module level rather than class level.

| Component | Afferent Coupling (callers) | Efferent Coupling (calls out to) | Criticality |
|-----------|------------------------------|-----------------------------------|-------------|
| `assets.py` (module, aggregate) | 7 modules (`harvester`, `extractor`, `connector`\*, `article`\*, `purge_source`\*, `cli`\*, `web_app`\*) | 6 modules (`config`, `hashing`, `state`, `llm`, `pricing`, `usage`/`progress`) | High |
| `extract_markdown_images` | 1 (`harvester.py`) | 3 (`_save_image`, `sha256_hex_bytes`, `_context_snippet`) | Medium |
| `extract_docling_images` | 1 (`harvester.py`) | 4 (`_docling_pil`, `_png_bytes`, `_save_image`, `_context_snippet`) | Medium |
| `register_assets` | 1 (`harvester.py`) | 2 (`_resolve_chapter_id`, `StateDB.upsert_asset`) | Medium |
| `reresolve_asset_chapters` | 1 (`harvester.py`, via `_finalize_source_chunking`) | 2 (`_resolve_chapter_id`, `StateDB.update_asset_chapter`) | Low |
| `asset_ids_in_text` | 2 (`extractor.py`, `connector.py`) | 1 (`StateDB.get_assets_for_source`) | Medium |
| `describe_pending_assets` | 1 (`extractor.py`) | ~9 (`StateDB.*`, `llm.*`, `usage.*`, `progress.report`, `_get_multimodal_llm`, `_describe_with_rate_limit_retry`) | High |
| `_describe_one` | 1 (`_describe_with_rate_limit_retry`, internal) | 5 (`llm.fill_template`, `llm.apply_prompt_cache_hints`, `llm._extract_usage`, `pricing.estimate_llm_cost`, `usage.record_llm`) | High |
| `_is_rate_limit_error` / `_parse_retry_after_seconds` / `_rate_limit_wait_seconds` | 1 (`_describe_with_rate_limit_retry`, internal) | 0 | Low (isolated, pure logic — directly unit-tested) |
| `asset_id_for` | 3 (`register_assets`, tests, `purge_source`-adjacent flows via asset rows) | 1 (`hashing.short_hash`) | Low |
| `StateDB` `assets`-table CRUD (in `state.py`, not this module) | 6+ (`assets.py`, `article.py`, `purge_source.py`, `cli.py`, `web_app.py`, tests) | 0 (leaf persistence layer) | High |

`describe_pending_assets` and `_describe_one` carry the highest combined coupling and criticality: they touch cost tracking, caching, provider abstraction, and progress reporting simultaneously, and a regression here silently affects run cost accounting project-wide. The isolated rate-limit-parsing helpers are, by contrast, low-risk — pure functions with no I/O, directly covered by `test_parse_retry_after_seconds`.

\* These modules call `StateDB.get_assets_for_source`/`StateDB.reset_failed_assets` directly rather than through a function in `assets.py`, so they are afferent on the `assets` **table/schema** (an implicit contract) rather than on the `assets.py` **module** itself. This is noted as a coupling-boundary observation in Technical Debt (§10).

---

## 7. Endpoints

Not applicable — `assets.py` exposes no REST/GraphQL/gRPC endpoints of its own. It is invoked in-process by pipeline modules and by two indirect web-facing surfaces:

| Surface | Type | How it reaches this component |
|---------|------|-------------------------------|
| `POST /pipeline/retry_assets` (web.py) | HTTP (FastAPI, internal web UI) | Enqueues a `web_jobs` row with `operation="retry_assets"`; `web_app.py::_dispatch` calls `db.reset_failed_assets()` directly (bypasses `assets.py` functions entirely — see §10) |
| `zettel retry-failed --assets` (cli.py) | CLI | Same underlying `db.reset_failed_assets()` call |
| `zettel extract` / web "extract" job | CLI + HTTP | Transitively invokes `assets.describe_pending_assets` as part of `run_extract` |
| `zettel harvest` / web "harvest" job | CLI + HTTP | Transitively invokes `assets.extract_docling_images`/`extract_markdown_images`/`register_assets` as part of `run_harvest` |

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| Docling `DocumentConverter` / `docling_document.pictures` | Internal library (in-process) | Extract embedded pictures from converted PDF documents | Python object API (duck-typed `picture.get_image(document)`) | PIL Image → PNG bytes | Exceptions from `get_image()` caught broadly in `_docling_pil`, logged at debug level, image skipped (`skipped_fail` counter) |
| Multimodal LLM provider (OpenAI/Anthropic/Ollama/Gemini via LangChain) | External Service | Generate a PT-BR textual description of each image using surrounding context | HTTPS (via provider SDK), base64 data-URI image payload | JSON request/response (LangChain `HumanMessage`/`SystemMessage` → provider-specific wire format) | Custom retry/backoff/circuit-breaker in `_describe_with_rate_limit_retry` + `describe_pending_assets`; SDK-level `max_retries=0` to avoid double-retry; provider-specific exceptions unified via `_is_rate_limit_error` |
| `state.py` SQLite `assets` table | Internal Database | Durable registry of every extracted image: path, checksum, chapter binding, description, status | SQLite (via `sqlite3`/`StateDB` wrapper) | Relational rows (`asset_id`, `source_id`, `chapter_id`, `path`, `image_checksum`, `context_snippet`, `description`, `description_call_checksum`, `status`, `page_in_file`) | `ON CONFLICT(asset_id) DO UPDATE` upsert semantics with `COALESCE` to avoid clobbering non-null `chapter_id`/`page_in_file` on re-registration |
| `state.py` `llm_cache` table (shared with all LLM call sites) | Internal Database | Deterministic caching of multimodal description responses keyed by `call_checksum` | SQLite | JSON-ish text columns (`request_json`, `response_json`) | Cache miss falls through to a live call; no explicit cache invalidation — keyed checksum naturally "invalidates" on any input change |
| Vault filesystem `90_Assets/` | Internal Filesystem | Physical storage of content-addressed image files referenced from Markdown notes via Obsidian `![[path]]` embeds | Local filesystem I/O | Binary (PNG, or original extension for local Markdown images) | Idempotent write guarded by `dest.exists()`; directory auto-created via `mkdir(parents=True, exist_ok=True)`; no checksum verification on read (an existing file at the computed path is trusted) |
| `prompts/image_description.md` | Internal Config/Template | System + user prompt template for the multimodal description call | Markdown with `<!-- zettel:user -->` split marker | Text template with `{context}` placeholder | `doctor` command verifies the file's existence as a health check; no runtime handling if missing (would raise on `load_prompt_parts`) |
| Cost/usage tracking (`usage.py`, `pricing.py`) | Internal Service | Attribute LLM token cost of image description calls to the active run/source cost totals | In-process function calls (contextvars-based `CostTracker`) | Structured call args (`model`, `tokens_in/out`, `cost_usd`, `label`, cache-read/write tokens) | No error handling needed — pure accounting side effects, not fallible I/O |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Content-Addressable Storage | `_save_image`/`_asset_relpath` derive the filename from `sha256(bytes)` | assets.py:41-54 | Free deduplication + deterministic re-runs |
| Idempotent Write | `if not dest.exists(): dest.write_bytes(data)` | assets.py:52-53 | Avoid redundant disk I/O across repeated harvests of overlapping content |
| Deterministic Cache Key (memoization) | `call_checksum = sha256(prompt_hash|image_checksum|context_hash|model)` + `llm_cache` table lookup | assets.py:382-392 | Zero-cost idempotent re-execution of expensive/paid LLM calls |
| Circuit Breaker | `consecutive_exhausted >= abort_after` → `break` out of the batch loop | assets.py:425-433 | Protect the whole extract run from a saturated provider rate-limit window |
| Retry with Exponential Backoff + Provider Hint | `_rate_limit_wait_seconds` (hint-first, backoff fallback, capped) | assets.py:306-329 | Resilience to transient 429s without a fixed, possibly-too-short or too-long wait |
| Strategy / Provider Abstraction | `_get_multimodal_llm` branches on `normalize_llm_provider(cfg.llm.provider)` to instantiate the right LangChain chat class | assets.py:484-516 | Support multiple LLM backends behind one call interface (`_describe_one`) |
| Observer Pattern | `observer=None` parameter + `progress.report(observer, ...)` calls | assets.py:332, 372-375 | Decouple progress reporting from the caller (CLI Rich output vs. web job `JobProgress`) |
| Fail-Open / Defensive Parsing | Markdown extraction returns text unchanged on missing/unreadable/remote images rather than raising | assets.py:86-94 | Harvest must never abort due to a single broken image reference |
| Sentinel/Placeholder Substitution | Docling's literal `<!-- image -->` marker is matched positionally and substituted in document order | assets.py:31, 137-163 | Bridges Docling's picture-object list back to the flattened Markdown text it doesn't itself annotate with picture positions |
| Exception Unwrapping / Chain Walking | `_is_rate_limit_error` walks `__cause__`/`__context__` with a `seen` set | assets.py:278-303 | Detect rate limits regardless of how deeply a provider SDK wraps the underlying HTTP error |
| Lazy/Deferred Import | `from zettel.llm import ...` etc. inside function bodies rather than at module top | assets.py:349, 366-367, 486, 532-539 | Avoid import cycles between `assets.py`, `llm.py`, `usage.py`, `progress.py` at package load time |

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|------------------|-------|--------|
| Medium | Coupling boundary | `article.py`, `purge_source.py`, `cli.py`, and `web_app.py` call `StateDB.get_assets_for_source`/`reset_failed_assets` directly instead of through a function exported by `assets.py` | The `assets` table schema/semantics are an implicit contract shared by 5+ modules with no single module owning read access; a schema change to the `assets` table requires auditing call sites outside this component |
| Medium | `_resolve_chapter_id` / `reresolve_asset_chapters` | O(images × chapters) substring search, repeated per-asset on every `reresolve_asset_chapters` call (itself invoked after every full chunking pass) | Scales linearly with both image count and chapter count per source; likely fine at typical Zettelkasten source sizes, but a source with hundreds of images/chapters would incur a quadratic-ish resolution cost on every rechunk |
| Low | `extract_markdown_images` | No logging/counting of skipped remote or missing/unreadable local images (unlike Docling's `skipped_small`/`skipped_fail` counters) | An operator cannot tell from logs alone whether a Markdown source had broken image references without manually diffing text; reduces observability parity with the Docling path |
| Low | `_docling_pil` | Broad `except Exception` swallows all failure modes from `picture.get_image(document)` at debug-log level only | A systematic Docling API incompatibility (e.g., after a Docling upgrade) could silently drop every image in every document with no visible error unless someone inspects debug logs |
| Low | Web UI (`pipeline.html`) | The "Reprocessar assets" (`retry_assets`) button's `unavailable` condition does not check `stats.assets_failed` (or equivalent), unlike the analogous `retry_chunks` button which is disabled when `stats.chunks_failed` is falsy | Users can trigger a `retry_assets` job even when there are zero failed assets to reset (harmless — `reset_failed_assets` returns 0 — but inconsistent UX versus the chunk-retry affordance) |
| Low | `describe_pending_assets` | `db.get_pending_assets()` fetches pending assets across **all sources** in the vault, not scoped to the source currently being extracted | In a multi-source vault, a single `extract` invocation's image-description phase processes every vault's pending images each time, which is likely intentional (global backlog draining) but is not obviously scoped/documented as such in the function's docstring |
| Low | `asset_id_for` | `asset_id` uses `short_hash(image_checksum)` at the default (8-char) length while the file path uses a 16-char hash of the same checksum | Collision probability at 8 hex chars (~32 bits) is non-negligible at large corpus scale for two *different* images checksums colliding in the ID suffix within the same source namespace; a collision would cause one `upsert_asset` to silently overwrite another asset's row under the same `asset_id` (ON CONFLICT UPDATE) |
| Informational | `_get_multimodal_llm` | Raises a bare `ValueError` for unsupported providers, with no test coverage in `test_assets.py` for this branch | Any test of provider-fallback behavior is implicit/absent; a future provider addition to `llm.py`'s general LLM handling could be forgotten here, causing multimodal description to fail even though text-only LLM calls work fine for that provider |

---

## 11. Test Coverage Analysis

Direct unit tests for this component live in `tests/test_assets.py` (14 test functions). Additional coverage of the *consumption* side (chapter resolution edge cases, downstream rendering, web job wiring) is distributed across several other test files.

| Component / File | Unit Tests | Integration Tests | Coverage (functional) | Test Quality |
|--------------------|------------|--------------------|------------------------|----------------|
| `assets.py` — Markdown extraction (`extract_markdown_images`) | 3 (`test_markdown_local_image_extracted_and_rewritten`, `test_markdown_remote_image_ignored`, `test_disabled_images_noop`) | — | High for the documented behaviors (local copy+rewrite, remote skip, disabled no-op) | Good assertions on both the rewritten body and the saved file's existence; no explicit test for the `OSError`-on-read branch (unreadable file) |
| `assets.py` — Content addressing / dedup | 1 (`test_identical_images_dedup_to_same_file`) | — | Covers the core dedup guarantee directly | Good — asserts path equality for two distinct source files with identical bytes |
| `assets.py` — Chapter resolution (`register_assets`, `_resolve_chapter_id`) | 1 (`test_register_assets_resolves_chapter`) | 1 (`tests/test_harvester_sections.py`, orphan-chapter-id scenario) | High — both the direct-registration and the interrupted-harvest re-resolution scenarios are exercised | Good — the harvester-level test specifically reproduces the documented "ch026 orphan" failure mode this function exists to fix |
| `assets.py` — `reresolve_asset_chapters` | 1 (`test_reresolve_asset_chapters_updates_orphan_ids`) | 1 (`test_harvester_sections.py`) | High | Good — asserts both the updated count and the corrected `chapter_id` |
| `assets.py` — `asset_ids_in_text` | 1 (`test_asset_ids_in_text_matches_paths`) | 2 (`tests/test_connector.py` lines ~228-245) | High | Good — direct test plus connector-level fallback-path integration tests |
| `assets.py` — `describe_pending_assets` happy path + caching | 1 (`test_describe_pending_assets_uses_cache`) | — | High for the cache-hit/cache-miss distinction | Good — explicitly forces a status reset to `pending` mid-test to prove the second run is served from cache with `calls["n"]` unchanged |
| `assets.py` — Rate-limit retry/backoff/circuit-breaker | 4 (`test_describe_retries_rate_limit_then_succeeds`, `test_describe_rate_limit_exhausted_keeps_pending`, `test_describe_non_rate_limit_error_marks_failed`, `test_describe_aborts_batch_after_consecutive_rate_limits`) | — | High — covers success-after-retry, full exhaustion (stays pending), non-rate-limit failure (marked failed), and the multi-asset circuit breaker | Very good — asserts on `sleeps` list to confirm actual wait behavior (including the 0.6s floor), and on final per-asset status across a 3-asset abort scenario |
| `assets.py` — `_parse_retry_after_seconds` | 1 (`test_parse_retry_after_seconds`) | — | High for the regex/unit-conversion logic (ms, s tested) | Good, but does not test the `minutes`/`m` unit branch (`_rate_limit_wait_seconds`'s `unit.startswith("m")` path) despite that branch existing in the source |
| `assets.py` — Docling extraction (`extract_docling_images`, `_docling_pil`, size filtering) | **0 dedicated tests** | — | **Gap** — no test in `test_assets.py` mocks a `docling_document`/`pictures` object to exercise placeholder replacement, size filtering, or the orphan-placeholder-append fallback | This is the largest coverage gap in the component: the PDF/Docling image path (arguably the primary real-world use case, since most sources are PDFs) is entirely untested at the unit level; only manual/integration verification would catch a regression here |
| `assets.py` — `_get_multimodal_llm` provider branching | **0 dedicated tests** | — | **Gap** — all `describe_pending_assets` tests monkeypatch `_get_multimodal_llm` itself, bypassing real provider instantiation logic entirely | The `is_openai_compatible`/`anthropic`/`ollama`/`gemini`/unsupported-provider branches are unverified by any automated test |
| `state.py` — `assets` table CRUD | 1 (`test_state.py::test_assets_crud`) + schema-migration check (`test_state.py:174`) | — | High for basic CRUD | Good — covers pending→described status transition and `get_pending_assets` emptying |
| `purge_source.py` — asset file deletion on source purge | 0 direct asset-specific assertions found in `test_purge_source.py` beyond directory-listing setup (line 56) | Indirect | Low/Unclear — the grep found only vault directory scaffolding referencing `90_Assets`, not an assertion that `_delete_vault_source_files`'s `removed["assets"]` count or actual file deletion is verified | Potential gap — recommend confirming (out of scope for this read-only analysis) whether a dedicated test exercises the asset-file-deletion branch of `purge_source.py:132-139` |
| `web.py` / `web_app.py` — `retry_assets` job | 3 assertions in `tests/test_web.py` (CSRF check, job enqueue, result payload `{"assets_reset": 0}`) | Yes (via `client.post`) | Medium — confirms the HTTP wiring and CSRF gate, and that the job dispatches to `reset_failed_assets`, but only exercises the zero-failed-assets case | Adequate for wiring verification; does not test the web path with actual failed assets present |
| `connector.py` — `_resolve_images` (image resolution into ZTL `## Figuras`) | 1+ (`test_connector.py` line ~183) | Yes | Medium-High | Good — asserts the rendered `![[path]]` embed appears in the built note body |
| `article.py` — asset/figure catalog integration | Multiple (`test_article.py`, `test_article_graph.py` — asset seeding, `CatalogAsset`, figure embedding, missing-asset handling at line 424) | Yes | Medium-High | Good — includes a negative case for a missing/nonexistent asset path (`test_article.py:424`) |

**Overall assessment**: The module's own unit tests are thorough for the Markdown-image and multimodal-description/rate-limit logic (arguably the most behaviorally complex parts of the file), but there is a clear, specific gap around the **Docling/PDF image extraction path** (`extract_docling_images`, `_docling_pil`, `_png_bytes`) having zero dedicated unit tests, and around the **multimodal LLM provider-instantiation branching** (`_get_multimodal_llm`) being bypassed via monkeypatching in every existing test rather than exercised directly.

---

## 12. Report Metadata

- **Component analyzed**: `assets` (`zettel/assets.py`)
- **Files read/inspected for this analysis**: `zettel/assets.py`, `tests/test_assets.py`, `zettel/state.py`, `zettel/config.py`, `zettel/harvester.py`, `zettel/extractor.py`, `zettel/connector.py`, `zettel/purge_source.py`, `zettel/vault.py`, `zettel/article.py`, `zettel/web.py`, `zettel/web_app.py`, `zettel/cli.py`, `zettel/llm.py`, `zettel/hashing.py`, `zettel/schemas.py`, `zettel/templates/dashboard.html`, `zettel/templates/pipeline.html`, `config/config.yaml`, `prompts/image_description.md`, plus `tests/test_state.py`, `tests/test_purge_source.py`, `tests/test_connector.py`, `tests/test_web.py`, `tests/test_harvester_sections.py`, `tests/test_article.py`, `tests/test_article_graph.py`, `tests/test_review.py`.
- **Excluded per `ignore-folders`**: `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, `.pytest_cache`.
