# Component Deep Analysis Report: hashing

## 1. Executive Summary

`zettel/hashing.py` is a single-file, stateless utility component that implements the **canonical hashing strategy** underpinning the entire zettel_app pipeline's drift-resistance and cost-control model. It has no classes, no external dependencies (only Python stdlib: `hashlib`, `json`, `re`, `unicodedata`, `pathlib`), and no side effects — every function is a pure transformation from input to a deterministic string output.

Despite its small size (114 lines, 8 public functions), the component is one of the most widely depended-upon modules in the codebase: 13 other `zettel/*.py` modules and 5 test files import from it. Its role is foundational to three distinct concerns:

1. **Change detection ("drift resistance")** — layered checksums (file → extraction → chapter → chunk → note-semantic) let the pipeline (`harvester.py`, `sync.py`, `rebuild.py`) determine whether a file, chapter, chunk, or note actually changed in *meaning*, as opposed to superficial formatting differences (line endings, whitespace, PDF hyphenation artifacts). This prevents expensive and unnecessary reprocessing/re-embedding/re-LLM-calls.
2. **Deterministic LLM response caching** — `compute_llm_call_checksum()` is the cache key generator used by every LLM-calling module (`extractor.py`, `connector.py`, `ask.py`, `article.py`, `bibliography.py`) to avoid paying for an identical LLM call twice (SQLite `llm_cache` table).
3. **Semantic content extraction for embeddings** — `extract_embeddable_text()` strips YAML frontmatter and managed auto-generated blocks (`auto-backlinks`, `auto-connections`, `auto-moc-backrefs`, etc.) from a note body so that only human/LLM-authored semantic content is hashed and embedded, preventing embedding churn caused purely by bookkeeping metadata changes.

Key findings:
- The component is textbook **pure-function utility design**: no I/O except `file_sha256` (file reads), no state, fully deterministic, trivially testable.
- It has very high **afferent coupling** (many callers) and effectively **zero efferent coupling** to the rest of the codebase (only internal calls between its own functions plus stdlib) — an ideal dependency-graph "leaf" and a low-risk component to reason about in isolation.
- Two of eight functions (`compute_llm_call_checksum`, `compute_pipeline_signature`) have **no dedicated unit tests** in `tests/test_hashing.py`, despite being on the critical path for cost control (LLM cache) and reprocessing signatures (pipeline config drift). This is flagged as a test-coverage risk in Section 11.
- The regex-based PDF dehyphenation heuristic (`(\w)-\n(\w)` → merge) is a **simple, intentionally narrow** rule — it will not catch multi-line hyphenation, hyphenated compound words that are legitimately hyphenated, or non-ASCII word characters depending on regex engine Unicode awareness. This is a documented but implicit business-rule limitation (see Section 3).

## 2. Data Flow Analysis

The component itself has no orchestration — it is called synchronously, inline, by other modules at specific pipeline checkpoints. Below are the representative data flows through its functions, traced from the calling modules:

**Flow A — File ingestion / drift detection (harvester.py):**
```
1. harvest() computes compute_pipeline_signature() from chunking/harvest/images config
   + docling_config_hash -> stored on the `runs` row (drift marker for the whole run)
2. _process_file() computes file_sha256(file_path) -> compared against stored file_checksum
   -> if identical, file is skipped entirely (no reprocessing)
3. If file changed/new: text is extracted (Docling/native), then
   extraction_checksum = sha256_hex(normalize_text_for_hash(text))
   -> compared via db.get_source_by_extraction_checksum() to catch cross-format duplicates
      (e.g. same paper as .pdf vs. re-exported .md)
4. Per chapter: chapter_checksum = sha256_hex(normalize_text_for_hash(chapter_text))
   -> unchanged chapters are skipped; changed chapters trigger re-chunking
5. Per chunk: chunk_checksum = sha256_hex(normalize_text_for_hash(chunk_text))
   -> chunk_id = f"{source_id}::{chapter_id}::{short_hash(chunk_checksum)}"
   -> content-identical chunks within the same chapter pass are deduped in-memory (keep_ids)
```

**Flow B — Deterministic LLM call caching (extractor.py / connector.py / ask.py / article.py / bibliography.py):**
```
1. Caller builds prompt_hash = sha256_hex(full_prompt_template)
2. Caller builds filled_hash = sha256_hex(normalize_text_for_hash(filled_prompt_or_chunk))
3. call_checksum = compute_llm_call_checksum(prompt_hash, filled_hash/chunk_checksum,
                                              model, temperature, language,
                                              rag_context_checksum)
4. db.get_cached_llm_response(call_checksum) is queried against SQLite `llm_cache`
5a. Cache hit -> response reused, $0 cost recorded (record_cache_hit)
5b. Cache miss -> call_llm() invoked -> response persisted via db.cache_llm_response()
```

**Flow C — Semantic embedding checksum (connector.py / sync.py / rebuild.py / review.py):**
```
1. Note markdown body is passed to extract_embeddable_text(body)
   -> strips YAML frontmatter block
   -> strips <!-- zettel:auto-*:start/end --> managed blocks
   -> runs normalize_text_for_hash() on the remainder
2. semantic_checksum = sha256_hex(embeddable_text)
3. emb_hash = compute_embedding_input_hash(semantic_checksum, embedding.provider, embedding.model)
4. Compared against stored note.embedding_input_hash
   -> match: skip re-embedding (idx.upsert_permanent_note not called)
   -> mismatch: re-embed and persist new emb_hash via db.update_note_embedding()
```

**Flow D — Concept identity (extractor.py `_compute_concept_id`):**
```
1. anchor_quote (if present) or thesis text -> normalize_text_for_hash() -> sha256_hex()
2. concept_key = sha256_hex(f"{source_id}|{chunk_id}|{anchor_or_thesis_hash}")
3. concept_id = f"{source_id}::concept::{short_hash(concept_key)}"
```

## 3. Business Rules & Logic

### Overview of the business rules:

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Normalization | Unicode NFKC canonicalization before any hash | hashing.py:25 |
| Normalization | CRLF/CR line endings collapsed to LF | hashing.py:26 |
| Normalization | Runs of spaces/tabs collapsed to a single space | hashing.py:27 |
| Normalization | Trailing/leading whitespace around newlines stripped | hashing.py:28 |
| Normalization | 3+ consecutive blank lines collapsed to exactly 1 blank line (2 newlines) | hashing.py:29 |
| Normalization | PDF hyphenation artifact repair: `word-\ncontinuation` -> `wordcontinuation` | hashing.py:31 |
| Normalization | Final result is `.strip()`-ped (no leading/trailing whitespace) | hashing.py:32 |
| Hashing | All text hashes use SHA-256 over UTF-8 bytes | hashing.py:37 |
| Hashing | File hashes are computed over raw bytes, streamed in 1 MiB blocks (never loaded fully into memory) | hashing.py:40-46 |
| Hashing | `short_hash` truncates a SHA-256 hex digest to `length` chars (default 8) for use in human-facing IDs | hashing.py:49-51 |
| Cache Key Construction | LLM call checksum composed of 6 pipe-joined fields in a fixed order (prompt_hash\|chunk_checksum\|model\|temperature\|language\|rag_context_checksum) | hashing.py:54-64 |
| Cache Key Construction | `rag_context_checksum` defaults to empty string when no RAG/image context feeds the call | hashing.py:60 |
| Drift Detection | Embedding input hash combines semantic content + provider + model, so a provider/model change forces re-embedding even if content is unchanged | hashing.py:67-73 |
| Drift Detection | Pipeline signature is a hash of a JSON-serialized config dict, canonicalized via `sort_keys=True` for order-independence | hashing.py:76-82 |
| Content Extraction | YAML frontmatter (delimited by `---` as the very first line) is excluded from embeddable/semantic text | hashing.py:94-101 |
| Content Extraction | Any block between `<!-- zettel:auto-*:start -->` and `<!-- zettel:auto-*:end -->` markers is excluded from embeddable/semantic text | hashing.py:102-110 |
| Content Extraction | The remaining body lines are still run through `normalize_text_for_hash` before being returned | hashing.py:113 |

### Detailed breakdown of the business rules:
---

### Business Rule: Canonical Text Normalization (`normalize_text_for_hash`)

**Overview**:
Before any piece of text participates in a checksum anywhere in the pipeline, it is passed through a five-step canonicalization function. The purpose is to make hashes reflect *meaningful content changes* rather than incidental representation differences — this is what the module docstring calls "drift-resistant" hashing.

**Detailed description**:
The function first applies Unicode NFKC (compatibility) normalization, which folds visually/semantically equivalent Unicode code points (e.g., full-width vs. half-width characters, certain ligatures, combining-character sequences) into a single canonical representation. This matters heavily for this project because source text originates from two very different extraction pipelines — Docling's PDF-to-Markdown conversion and native Markdown file reads — which can legitimately produce different Unicode encodings of the same visual character.

Next, all CRLF and lone-CR line endings are converted to LF, defending against files edited or exported on Windows vs. Unix systems producing different byte sequences for logically identical line breaks. Runs of spaces and tabs are collapsed to a single space, and whitespace surrounding newlines is trimmed, which absorbs decorative padding introduced by PDF layout extraction (e.g., column alignment spaces) or manual editing. Three or more consecutive blank lines are collapsed down to exactly one blank line (two `\n` characters), which normalizes inconsistent paragraph spacing between hand-written Markdown and Docling's auto-generated Markdown.

The most domain-specific rule is PDF dehyphenation: the regex `(\w)-\n(\w)` merges a hyphen-broken word across a line wrap (e.g., "experi-\nmental" -> "experimental"). This targets a well-known PDF-extraction artifact where justified text wraps mid-word with a visible hyphen that is not part of the original word. The rule is intentionally narrow — it only fires on a single line break directly adjacent to word characters on both sides, so it will not catch hyphenation spanning more than one physical line, nor distinguish between an artificial line-wrap hyphen and a legitimately hyphenated compound word that happens to fall at a line boundary (a known, accepted false-positive risk — there is no dictionary check). The final result is stripped of leading/trailing whitespace before being returned, guaranteeing a canonical form ready for hashing or direct comparison.

This function is a *dependency* for nearly every other checksum in the codebase (chapter, chunk, extraction, LLM-call, note-semantic) — it is never bypassed for any hashed text that originates from user/extracted content, only for structural/config data that already has a canonical machine representation (e.g., JSON in `compute_pipeline_signature`).

**Rule workflow**:
```
raw_text
  -> unicodedata.normalize("NFKC", raw_text)
  -> replace CRLF/CR with LF
  -> collapse consecutive space/tab runs to single space
  -> trim space/tab around each newline
  -> collapse 3+ newlines to exactly 2 (one blank line)
  -> merge "word-\nword" hyphenation artifacts
  -> .strip()
  -> canonical_text (input to sha256_hex or further processing)
```

---

### Business Rule: Layered Checksum Hierarchy for Drift Detection

**Overview**:
The pipeline relies on a chain of checksums at increasing granularity — file, extraction, chapter, chunk, LLM call, note-semantic, embedding-input — each one gating whether expensive downstream work is repeated. `hashing.py` provides the primitives for every layer except chunk/chapter identity construction, which is composed inline in `harvester.py` by combining `sha256_hex(normalize_text_for_hash(...))` with `short_hash(...)`.

**Detailed description**:
`file_sha256` hashes raw file bytes (not normalized text) in streamed 1 MiB blocks, which is deliberately the coarsest and cheapest check: if the file on disk is byte-identical to what was last harvested, the harvester skips it entirely without even opening it for text extraction (see `harvester.py:543-560`). Because this check operates on raw bytes rather than normalized text, saving a PDF with a single metadata change (e.g., different modification timestamp embedded in the file, or a different PDF producer tag) will trigger a full reprocessing, even though the human-visible content is unchanged — this coarse-grained behavior is deliberate and cheap to fall through to Layer 2.

Layer 2, the extraction checksum (`sha256_hex(normalize_text_for_hash(text))` computed by the caller in `harvester.py:585`), operates on the *extracted and normalized* text rather than raw bytes. This is what allows the three-layer duplicate detector to recognize that a PDF and a manually re-typed/re-exported Markdown version of the same paper are the same source (`db.get_source_by_extraction_checksum`), reusing the existing citekey/SRC/LIT/chunks rather than creating a duplicate source. This is a meaningful business capability: it prevents citekey collisions and duplicate vault entries when the same paper enters the inbox in two different formats.

Layers 3 and 4 (chapter and chunk checksums) are the finest-grained drift checks and are what make incremental re-harvesting cheap: when a source is re-harvested (e.g., after a config change or manual correction), only chapters whose normalized text checksum differs from the stored value are re-chunked, and within a changed chapter, only chunks whose checksum is new are persisted and queued for extraction — chunks with a checksum matching a previously-processed chunk are treated as already known. The chunk_id itself embeds a `short_hash` of the chunk checksum (`f"{source_id}::{chapter_id}::{short_hash(chunk_checksum)}"`), meaning the identity of a chunk *is* a function of its normalized content — two chunks with byte-different but normalization-equivalent text collapse to the same ID and are deduplicated in-memory during a single chunking pass (`keep_ids` set in `harvester.py:1646-1652`).

**Rule workflow**:
```
Layer 1 (file):        file_sha256(bytes) == stored file_checksum?
                          yes -> skip file entirely
                          no  -> go to Layer 1b (renamed-copy check via same checksum, different path)
Layer 1b (renamed):    file_checksum matches a DIFFERENT existing path?
                          yes -> reuse existing source_id, no reprocessing
                          no  -> extract text, go to Layer 2
Layer 2 (extraction):  sha256_hex(normalize_text_for_hash(text)) matches an existing source?
                          yes -> reuse that source (cross-format duplicate), no new citekey/chunks
                          no  -> new source; proceed to chaptering
Layer 3 (chapter):     sha256_hex(normalize_text_for_hash(chapter_text)) == stored chapter_checksum?
                          yes -> skip chapter (no re-chunking)
                          no  -> re-chunk chapter
Layer 4 (chunk):       chunk_id = f"{source_id}::{chapter_id}::{short_hash(sha256_hex(normalize_text_for_hash(chunk_text)))}"
                          already in keep_ids for this pass -> drop as in-pass duplicate
                          already in DB -> treated as existing chunk (no reprocessing)
                          new -> persisted as pending chunk for extraction
```

---

### Business Rule: Deterministic LLM Call Checksum and Response Caching

**Overview**:
`compute_llm_call_checksum` produces the primary key for the SQLite `llm_cache` table, giving the system an exact-match, content-addressed cache for every LLM call in the pipeline (extraction Prompt 1, connector Prompt 2, `ask`, `article`, `bibliography`). Its purpose is purely economic: avoid paying LLM provider costs twice for an identical request.

**Detailed description**:
The checksum is built by joining six values with a literal pipe (`|`) separator: `prompt_hash`, `chunk_checksum` (semantically, "the hash of whatever content varies per call" — a chunk checksum in extraction, or a hash of the fully-filled prompt in connector/ask/article/bibliography), `model`, `temperature`, `language`, and an optional `rag_context_checksum` that defaults to an empty string. Because the six fields are concatenated positionally rather than as a structured/keyed format, the function is sensitive to the *exact* string representation of `temperature` (a float) — e.g., `0.7` vs `0.70` would already be normalized identically by Python's float-to-str formatting in an f-string, so this is a low-risk but real implicit assumption (no explicit rounding/formatting policy is enforced by this function itself; the caller is trusted to pass a consistent value).

Every one of the five call sites (`extractor.py:192`, `connector.py:238`, `ask.py:152`, `article.py:814`, `bibliography.py:751`) follows the same two-step pattern: hash the static prompt template once (`sha256_hex(prompt_parts.full_template)`), then hash the fully-rendered/variable portion of the call (the per-chunk text, the per-concept filled prompt, the per-question filled prompt, etc.) after running it through `normalize_text_for_hash`. This means a cache hit requires the *exact same* prompt template, the *exact same* rendered content (after normalization — so incidental whitespace differences in a re-run do not defeat the cache), the same model, temperature, language, and RAG/image context. Any change to any of these — including a prompt file edit, a model swap, or a different set of retrieved RAG passages — produces a different checksum and forces a fresh (paid) LLM call. This is the mechanism that makes `zettel extract`/`zettel connect`/`zettel ask`/`zettel article` idempotent and cheap to re-run after a partial failure: work already paid for is never re-paid unless something that actually affects the LLM's input changed.

The caching lookup itself (`db.get_cached_llm_response`/`db.cache_llm_response`, in `state.py`) is outside this component's boundary, but the checksum computed here is the sole key driving cache hit/miss decisions system-wide, making this function a de-facto **cost-control gate** for the whole pipeline's LLM spend.

**Rule workflow**:
```
prompt_hash = sha256_hex(static_prompt_template)
variable_hash = sha256_hex(normalize_text_for_hash(filled_prompt_or_chunk_text))
                 (or: chunk_checksum, already normalized/hashed upstream, in extractor.py)
rag_context_checksum = sha256_hex(normalize_text_for_hash(images_or_rag_context)) if present else ""
call_checksum = sha256_hex(f"{prompt_hash}|{variable_hash}|{model}|{temperature}|{language}|{rag_context_checksum}")

if db.get_cached_llm_response(call_checksum) exists:
    reuse cached response, cost = $0, record_cache_hit()
else:
    call_llm(...) -> pay provider cost -> db.cache_llm_response(call_checksum, request, response)
```

---

### Business Rule: Embedding Input Hash Guards Against Redundant Re-embedding

**Overview**:
`compute_embedding_input_hash` combines a note's semantic-content checksum with the embedding provider and model name into a single hash, stored per-note as `embedding_input_hash`. This is the gate that decides whether a note needs to be re-sent to the embedding API and re-upserted into ChromaDB.

**Detailed description**:
Embedding calls have both a monetary cost (API-billed providers) and a consistency risk (re-embedding the same content with the same model should be a no-op, but doing so unnecessarily wastes cost and rate-limit budget, and for the `rebuild`/`sync-manual`/`connect` flows, doing it for every note on every run would make large vaults prohibitively slow to process incrementally). By hashing the triple of (semantic content checksum, provider, model), a change in *any* of the three forces re-embedding: editing the note's substantive Markdown content changes the semantic checksum; switching embedding provider (e.g., OpenAI to SentenceTransformers) or model (e.g. dimension change) changes the hash even if the underlying text is byte-identical, because a vector produced by a different model/provider is not comparable to the old one and must be regenerated to keep the vector index self-consistent.

This function is used identically across three otherwise-independent code paths that all need to decide "should this note be re-embedded": `connector.py` right after generating a new ZTL permanent note, `sync.py` when a manually-edited note is scanned in, and `rebuild.py`/`review.py`'s reindex path when rebuilding the whole ChromaDB index from SQLite-persisted note bodies. Centralizing the hash formula in `hashing.py` guarantees all three paths agree on what "unchanged" means, so a note embedded by one code path is correctly recognized as up-to-date by another without divergent logic.

**Rule workflow**:
```
semantic_checksum = sha256_hex(normalize_text_for_hash(extract_embeddable_text(note_body)))
emb_hash = sha256_hex(f"{embedding.provider}|{embedding.model}|{semantic_checksum}")

if stored_note.embedding_input_hash == emb_hash:
    skip re-embedding (idx.upsert_permanent_note NOT called)
else:
    idx.upsert_permanent_note(...) -> re-embed
    db.update_note_embedding(note_id, emb_hash, embedding.model)
```

---

### Business Rule: Pipeline Configuration Signature for Run-Level Drift Tracking

**Overview**:
`compute_pipeline_signature` hashes a canonicalized JSON representation of the subset of configuration that affects harvest output (chunking, harvest, images config, PDF extractor choice, Docling config hash), producing a signature stored on every `runs` row.

**Detailed description**:
Unlike the text-oriented checksums elsewhere in the module, this function hashes structured configuration data, not extracted content. It serializes the input `dict` via `json.dumps(config, sort_keys=True, ensure_ascii=False)` before hashing — the `sort_keys=True` is the critical correctness property here: Python dict key order is insertion-order but not semantically meaningful for a config object, so without sorting, two functionally identical config dicts built in a different order (e.g., from a different YAML key ordering) would hash differently, defeating the purpose of drift detection. `ensure_ascii=False` preserves non-ASCII characters (relevant for PT-BR config values) as literal UTF-8 rather than escaped `\uXXXX` sequences, which is a stylistic choice that does not affect correctness of the hash itself (either would be internally consistent) but does affect readability if the JSON were ever inspected directly (it is not persisted, only hashed).

This signature is computed once per `harvest` invocation (`harvester.py:89-96`) from `chunking`, `harvest`, `images` config sections plus `pdf_extractor` and a separately-computed `docling_config_hash`, and is passed to `db.start_run(signature)`. It is a run-level bookkeeping/audit signal — the codebase does not appear to branch on this signature to skip work (that responsibility lives with `docling_config_hash` comparisons at the source level, per `harvester.py:551-556`, which warns the operator to run `zettel rechunk` when it changes) — rather, it records what configuration was in effect for a given run, supporting after-the-fact auditing of why outputs differ between runs.

**Rule workflow**:
```
config_subset = {chunking, harvest, images, pdf_extractor, docling_config_hash}
canonical_json = json.dumps(config_subset, sort_keys=True, ensure_ascii=False)
signature = sha256_hex(canonical_json)
db.start_run(signature)   # stored for audit/drift-tracking, not used to gate execution
```

---

### Business Rule: Embeddable/Semantic Text Extraction (Frontmatter and Managed-Block Stripping)

**Overview**:
`extract_embeddable_text` is the gatekeeper that decides which portion of a note's Markdown body is "meaningful content" for embedding and semantic-checksum purposes, versus which portion is machine-generated bookkeeping that should never influence embeddings or trigger false-positive drift.

**Detailed description**:
The function performs a single top-to-bottom scan of the note body's lines with two pieces of state: `in_frontmatter` and `in_managed_block`. The very first line, if it is exactly `---`, opens a YAML frontmatter block that is skipped until a matching closing `---` is found; this assumes frontmatter (if present) always starts on line 0 — a body that has leading blank lines before its frontmatter delimiter would not have that frontmatter stripped, since the check is `i == 0` specifically, not "first non-blank line." Vault-wide, this is a reasonably safe assumption because the project's own writers (`vault.py`) always emit frontmatter as the literal first line, but it does mean the function is not a fully general Markdown-frontmatter stripper — it is coupled to this project's own file-writing convention.

The second concern is stripping "managed blocks" — regions the pipeline itself writes and rewrites automatically, delimited by HTML comments matching `<!-- zettel:auto-*:start -->` and `<!-- zettel:auto-*:end -->` (e.g., `auto-backlinks`, `auto-connections`, `auto-moc-backrefs`). These blocks are excluded from embedding and semantic-checksum computation because they are derived data (backlinks, suggested connections, MOC cross-references) that changes automatically as the surrounding graph evolves, entirely independent of whether a human or LLM actually edited the note's substantive content. Without this exclusion, every time `sync.py` or `moc_backrefs.py` updated a note's auto-backlinks block, the note's semantic checksum would change, triggering a spurious re-embedding cascade — this rule is what keeps embedding cost proportional to actual authored-content changes rather than to incidental graph-maintenance writes. After both types of exclusion, the remaining lines are rejoined and passed through `normalize_text_for_hash` before being returned, so the final embeddable text benefits from the same canonicalization as every other hashed text in the system.

**Rule workflow**:
```
for each line in markdown_body.split("\n"):
    if line_index == 0 and line.strip() == "---":
        enter frontmatter mode, skip line
    elif in frontmatter mode:
        if line.strip() == "---": exit frontmatter mode
        skip line
    elif line matches "<!-- zettel:auto-*:start -->":
        enter managed-block mode, skip line
    elif line matches "<!-- zettel:auto-*:end -->":
        exit managed-block mode, skip line
    elif in managed-block mode:
        skip line
    else:
        keep line

embeddable_text = normalize_text_for_hash(kept_lines joined by "\n")
```

---

## 4. Component Structure

```
zettel/
└── hashing.py                        # Entire component: 114 lines, 8 public functions, stdlib-only
    ├── normalize_text_for_hash()     # Canonical text normalization (NFKC, CRLF, whitespace, dehyphenation)
    ├── sha256_hex()                  # SHA-256 hex digest of a UTF-8 string
    ├── file_sha256()                 # SHA-256 hex digest of raw file bytes (streamed, 1 MiB blocks)
    ├── short_hash()                  # Truncated sha256_hex(), default 8 chars, for ID suffixes
    ├── compute_llm_call_checksum()   # Cache key for deterministic LLM response caching
    ├── compute_embedding_input_hash()# Hash of (semantic_checksum, provider, model) for re-embed gating
    ├── compute_pipeline_signature()  # Hash of sorted-JSON pipeline config, for run-level audit
    └── extract_embeddable_text()     # Strips YAML frontmatter + managed blocks from a note body

tests/
└── test_hashing.py                   # Direct unit tests: 10 test functions covering 6 of 8 functions
```

There is no internal sub-structure (no sub-package, no classes, no private helper functions beyond the public API itself) — every function is public and independently callable. This is a deliberate "utility module" shape: maximal cohesion (everything relates to hashing/normalization/checksums for the drift-resistance strategy) with zero internal coupling between the functions' *logic* (only `short_hash`, `compute_llm_call_checksum`, `compute_embedding_input_hash`, `compute_pipeline_signature`, and `extract_embeddable_text` call `sha256_hex`/`normalize_text_for_hash` as building blocks).

## 5. Dependency Analysis

```
Internal Dependencies (within hashing.py):
short_hash() -> sha256_hex()
compute_llm_call_checksum() -> sha256_hex()
compute_embedding_input_hash() -> sha256_hex()
compute_pipeline_signature() -> sha256_hex()
extract_embeddable_text() -> normalize_text_for_hash()

External Dependencies (Python standard library only):
- hashlib   - SHA-256 digest computation
- json      - Canonical (sort_keys) serialization for compute_pipeline_signature
- re        - Regex-based whitespace/hyphenation normalization, managed-block matching
- unicodedata - NFKC Unicode normalization
- pathlib.Path - Type hint for file_sha256's path parameter

Zero third-party dependencies. Zero dependencies on other zettel/*.py modules
(the module is a dependency-graph leaf/sink).

Downstream Consumers (modules that import from zettel.hashing):
zettel/harvester.py    -> file_sha256, normalize_text_for_hash, sha256_hex, short_hash, compute_pipeline_signature
zettel/extractor.py    -> compute_llm_call_checksum, normalize_text_for_hash, sha256_hex, short_hash
zettel/connector.py    -> compute_embedding_input_hash, compute_llm_call_checksum, extract_embeddable_text,
                           normalize_text_for_hash, sha256_hex
zettel/sync.py         -> compute_embedding_input_hash, extract_embeddable_text, normalize_text_for_hash, sha256_hex
zettel/rebuild.py      -> compute_embedding_input_hash, extract_embeddable_text, normalize_text_for_hash, sha256_hex
zettel/review.py       -> extract_embeddable_text (local import inside _literature_embed_text)
zettel/ask.py          -> compute_llm_call_checksum, normalize_text_for_hash, sha256_hex
zettel/article.py      -> compute_llm_call_checksum, normalize_text_for_hash, sha256_hex
zettel/bibliography.py -> compute_llm_call_checksum, normalize_text_for_hash, sha256_hex
zettel/assets.py       -> normalize_text_for_hash, sha256_hex, short_hash
zettel/gardener.py     -> sha256_hex
zettel/gardener_hub.py -> sha256_hex
zettel/web.py          -> file_sha256
```

## 6. Afferent and Efferent Coupling

Because this is a functional (non-OOP) module, "components" are the individual public functions rather than classes. Afferent coupling = number of distinct modules (excluding hashing.py itself) calling the function at least once. Efferent coupling = number of distinct hashing.py functions it calls internally.

| Component (function) | Afferent Coupling (calling modules) | Efferent Coupling (internal calls) | Critical |
|-----------------------|--------------------------------------|--------------------------------------|----------|
| `sha256_hex` | 11 (article, assets, ask, bibliography, connector, extractor, gardener, gardener_hub, harvester, rebuild, sync) + 4 internal callers | 0 | High |
| `normalize_text_for_hash` | 9 (article, assets, ask, bibliography, connector, extractor, harvester, rebuild, sync) + 1 internal caller | 0 | High |
| `extract_embeddable_text` | 4 (connector, rebuild, sync, review) | 1 (normalize_text_for_hash) | High |
| `compute_llm_call_checksum` | 5 (article, ask, bibliography, connector, extractor) | 1 (sha256_hex) | High |
| `compute_embedding_input_hash` | 3 (connector, rebuild, sync) | 1 (sha256_hex) | Medium |
| `short_hash` | 3 (assets, extractor, harvester) | 1 (sha256_hex) | Medium |
| `file_sha256` | 2 (harvester, web) | 0 | Medium |
| `compute_pipeline_signature` | 1 (harvester) | 1 (sha256_hex) | Low |

Interpretation: `sha256_hex` and `normalize_text_for_hash` are the two true "hub" primitives — nearly every other function and nearly every consuming module ultimately routes through them, which means a behavioral change to either (e.g., changing the normalization rules, or switching hash algorithms) would ripple through checksums, cache keys, chunk/concept IDs, and embedding-gating logic across the entire pipeline simultaneously. `compute_pipeline_signature` is the most isolated function (single caller, single internal dependency), making it the lowest-risk function to modify in isolation.

## 8. Integration Points

`hashing.py` has no direct external integrations (no network, no database, no message queue) — its only I/O is local filesystem reads inside `file_sha256`. Its "integration" role is entirely as a shared library consumed in-process by other components that do own the actual integrations:

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| Local filesystem | I/O (via `file_sha256`) | Read raw file bytes for checksum computation | Direct `open(path, "rb")` | Raw bytes, streamed 1 MiB blocks | None in hashing.py itself — `web.py:_file_needs_harvest` wraps the call in `try/except OSError`, treating an unreadable file as "needs harvest" (fail-open); `harvester.py` does not appear to guard the call with its own try/except, so an `OSError` there (e.g., permission denied, file deleted mid-scan) would propagate uncaught |
| SQLite `llm_cache` table (state.py) | Downstream consumer | `compute_llm_call_checksum()` output is the primary key | N/A (in-process) | TEXT primary key | Not hashing.py's concern — no collision handling exists beyond SHA-256's cryptographic collision resistance |
| SQLite `notes.embedding_input_hash` column (state.py) | Downstream consumer | `compute_embedding_input_hash()` output gates re-embedding | N/A (in-process) | TEXT column | N/A |
| ChromaDB metadata (`note_semantic_checksum`) | Downstream consumer | Semantic checksums stored as vector metadata for provenance | N/A (in-process) | str (ChromaDB metadata constraint) | N/A |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Pure Function / Stateless Utility Module | All 8 functions are side-effect-free transformations (except the file read in `file_sha256`) | hashing.py (entire file) | Maximizes testability and predictability; safe to call from any pipeline phase without ordering constraints |
| Layered Checksum / Content-Addressable Identity | file -> extraction -> chapter -> chunk -> semantic checksums, each coarser layer short-circuiting before the next is computed | hashing.py + harvester.py, connector.py, sync.py | Minimizes redundant expensive work (re-extraction, re-chunking, re-embedding, re-LLM-calls) by detecting "no meaningful change" as early and cheaply as possible |
| Deterministic Cache Key (Memoization) | `compute_llm_call_checksum` composes a stable, positional string key over exactly the inputs that affect an LLM response | hashing.py:54-64, consumed in extractor.py/connector.py/ask.py/article.py/bibliography.py | Enables exact-match response caching without a semantic/fuzzy cache, trading recall for zero false-cache-hit risk |
| Canonicalization Before Hashing | `normalize_text_for_hash` and `json.dumps(..., sort_keys=True)` both normalize representation before hashing | hashing.py:15-32, hashing.py:76-82 | Ensures hash equality reflects semantic/content equality, not incidental representation differences |
| Separation of Derived vs. Authored Content | `extract_embeddable_text` explicitly excludes frontmatter and "managed blocks" from what gets hashed/embedded | hashing.py:85-113 | Prevents automatically-regenerated bookkeeping (backlinks, connections) from triggering false-positive semantic drift |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|------------------|-------|--------|
| Medium | `compute_llm_call_checksum` | No dedicated unit test in `tests/test_hashing.py` — only indirectly exercised through integration-level tests of extractor/connector/ask (if any exist) | A regression in the cache-key composition (e.g., accidental field reordering, wrong separator) would not be caught by a fast, isolated unit test; could silently cause cache misses (extra LLM cost) or, worse, cache collisions if two conceptually different calls produced the same key |
| Medium | `compute_pipeline_signature` | No dedicated unit test in `tests/test_hashing.py` | Same category of risk as above, scoped to run-level audit signatures rather than cache correctness — lower operational impact since nothing appears to branch on this value for correctness (only for operator-facing warnings elsewhere) |
| Low-Medium | `normalize_text_for_hash` dehyphenation rule | The regex `(\w)-\n(\w)` is a heuristic, not a linguistically-aware dehyphenation algorithm — it will silently merge a legitimately hyphenated compound word that happens to fall at a PDF line-wrap boundary (e.g., "well-\nknown" merging to "wellknown" even when "well-known" is the intended orthography), and will not repair hyphenation spanning more than one line break | Could cause a small class of false-negative drift detection (two extractions of the same PDF producing slightly different dehyphenated text if line-wrap points differ) or minor semantic-text corruption feeding into embeddings; no dictionary or NLP-based validation exists to bound this |
| Low | `extract_embeddable_text` frontmatter detection | Only strips frontmatter if the very first line (index 0) is exactly `---`; a body with leading blank lines before the frontmatter delimiter would not have it stripped | Low likelihood given the project's own writers always emit frontmatter as line 0, but any future format-relaxation elsewhere in the vault-writing code could silently break this assumption without hashing.py raising any error |
| Low | `file_sha256` error handling | The function itself performs no exception handling around `open()`/`read()` — errors (missing file, permission denied, path traversal to a directory) propagate as raw `OSError`/`FileNotFoundError`/`IsADirectoryError` to the caller | Each of the two call sites handles this differently: `web.py` explicitly catches `OSError`; a grep of `harvester.py`'s call site shows no surrounding try/except at that exact line, meaning behavior on a mid-scan file deletion/permission change during `harvest` depends entirely on caller-level exception handling further up the stack (not verified as part of this analysis, since that logic lives outside the `hashing` component boundary) |
| Low | `compute_llm_call_checksum` field encoding | `temperature` (a float) is interpolated directly into the f-string; there is no explicit canonical formatting (e.g., fixed decimal places) enforced by this function — it relies entirely on Python's default `str(float)` behavior being stable across the process, which it is, but this is an implicit rather than documented contract | If a future caller ever passed `temperature` as a differently-typed value (e.g., a `Decimal`, or a string already) the resulting checksum would still be computed but its cross-call determinism guarantee would depend on that caller's own consistency — no validation exists in `hashing.py` to enforce a single canonical numeric representation |

## 11. Test Coverage Analysis

| Component (function) | Direct Unit Tests (test_hashing.py) | Indirect Coverage (other test files) | Test Quality |
|------------------------|----------------------------------------|-----------------------------------------|--------------|
| `normalize_text_for_hash` | 4 tests: whitespace collapse, CRLF handling, blank-line limiting, dehyphenation | `tests/test_harvester_dedup.py`, `tests/test_harvester_sections.py` (imported and used to construct expected chapter/chunk checksums) | Good direct coverage of each of the five normalization steps individually; no test combines multiple normalization concerns in one input (e.g., a string with both CRLF *and* hyphenation *and* excess blank lines), so interaction effects between the steps are untested |
| `sha256_hex` | 1 test: determinism + inequality for different inputs | Used pervasively as a comparison baseline in `test_harvester_sections.py` | Adequate — the function is a thin wrapper over `hashlib.sha256`, so minimal testing is proportionate to its risk |
| `short_hash` | 1 test: verifies output length matches requested `length` parameter | None found | Adequate for the function's simplicity, but does not test the default `length=8` path, nor verify the truncated value is a strict prefix of the full `sha256_hex` digest (implied by implementation but not asserted) |
| `file_sha256` | 0 direct tests in test_hashing.py | `tests/test_web.py:205-206` — computes checksums of a "completed" and an "incomplete" test file and compares them for use in `_file_needs_harvest` scenarios | No test in test_hashing.py verifies `file_sha256` in isolation (e.g., against a known SHA-256 fixture value, or verifying the 1 MiB streaming block-read logic against a large file); the only coverage is incidental to a web-layer test |
| `compute_llm_call_checksum` | 0 direct tests | None found (no direct test file constructs this checksum and asserts on its structure or determinism across all 6 fields, including the optional `rag_context_checksum`) | Gap — see Section 10 risk entry; this is the most cost-sensitive function in the module and has no isolated determinism/uniqueness test |
| `compute_embedding_input_hash` | 1 test: determinism + inequality when any of the 3 components (semantic checksum, provider, model) changes | None found | Good — exercises exactly the documented business rule (any component change alters the hash) with explicit negative assertions for each of the three fields |
| `compute_pipeline_signature` | 0 direct tests | None found | Gap — see Section 10 risk entry; no test verifies the `sort_keys=True` order-independence property that is the entire reason this function serializes via `json.dumps` instead of naive string concatenation |
| `extract_embeddable_text` | 2 tests: frontmatter stripping, managed-block stripping | `tests/test_vault.py:238` (used to compute embeddable text from a composed note body in a broader vault test) | Good coverage of the two primary exclusion rules individually; no test exercises a body containing *both* frontmatter and a managed block simultaneously, nor a body with multiple managed blocks of different types (`auto-backlinks` and `auto-connections` both present), nor the edge case of an unterminated managed block (`:start` with no matching `:end`) |

**Test file locations:**
- `tests/test_hashing.py` — 10 direct unit tests, the component's dedicated test suite (77 lines)
- `tests/test_harvester_dedup.py:15` — imports `normalize_text_for_hash`, `sha256_hex` to construct expected values for duplicate-detection scenarios
- `tests/test_harvester_sections.py:190,208,212` — imports `sha256_hex`, `normalize_text_for_hash` to construct expected chapter checksums for section-splitting tests
- `tests/test_vault.py:3,238` — imports `extract_embeddable_text` to verify vault note composition/embedding text extraction
- `tests/test_web.py:10,205-206` — imports `file_sha256` to test the web layer's `_file_needs_harvest` change-detection logic

**Overall assessment**: 6 of 8 public functions have direct, isolated unit tests; the remaining 2 (`compute_llm_call_checksum`, `compute_pipeline_signature`) are exercised only transitively through integration-style tests of their calling modules (if those modules' own test suites happen to cover the caching/signature code paths — not verified as part of this hashing-component-scoped analysis). No test file in the repository directly imports and asserts against `compute_llm_call_checksum` or `compute_pipeline_signature` by name.
