# Component Deep Analysis Report — `ask`

## 1. Executive Summary

The `ask` component implements `zettel ask "..."`, a question-answering (QA) feature that lets a user query the Zettelkasten vault in natural language and receive an answer **grounded exclusively in the vault's own permanent notes**, with full citation provenance.

Its single implementation file, `zettel/ask.py`, exposes:

- `run_ask(cfg, db, idx, question, topk=None, use_graph=None, mode=None) -> AskResult` — the orchestration entry point.
- `AskResult` / `AskSource` — dataclasses carrying the answer plus per-note provenance.
- `build_ask_note_body(result) -> (dict, str)` and `save_ask_note(result, vault_path, dest=None) -> Path` — persist the answer as a Markdown note in `00_Inbox/`.

Architecturally, `ask` is a thin, stateless orchestration layer: it does not implement retrieval, LLM invocation, hashing, or vault I/O itself — it composes four pieces of shared infrastructure (`retrieval.Retriever`, `llm.call_llm`/`get_llm`, `hashing.compute_llm_call_checksum`, `vault` renderers) into one deterministic, cache-aware pipeline. The one piece of genuine business logic it owns is the **"no evidence, no LLM call" gate**: when nothing in the vault clears the hybrid retriever's relevance floor, `ask` short-circuits to a fixed PT-BR message without spending any LLM tokens, while still surfacing the raw candidate pool for debugging via `--show-context`.

Key findings:

- **Deterministic and cost-safe**: the LLM is called at most once per question, only when there is retrieved evidence, and reuses the same SQLite `llm_cache` mechanism as the `connector` (Phase 3) component, keyed by a checksum of prompt + filled template + model + temperature + language.
- **Full provenance by design**: every note used to answer carries its retrieval origin (`busca` vs. graph `conexao ...`), RRF score, vector similarity, BM25 rank, and floor verdict/reason — both in the CLI's `--show-context` table and in the saved answer note's "Fontes consultadas" section.
- **CLI-only**: `ask` is deliberately not exposed through the web UI (per `web.py`/`web_app.py`, confirmed by absence of any reference), only reachable via `zettel ask` in `cli.py`.
- **Read-only with one side effect**: `ask` never writes to `StateDB`/`ChromaDB` except for the `runs` bookkeeping row and the `llm_cache` insert; its only vault mutation is the optional saved `.md` answer note.
- No dedicated business-rule module — the validation/business logic lives entirely inside `run_ask` and the prompt template `prompts/ask.md`.

---

## 2. Data Flow Analysis

```
1.  CLI: `zettel ask "<question>"` (zettel/cli.py:1343 `ask()`)
2.  cli.ask() resolves AppConfig/StateDB/VectorIndex via _load_deps/_get_db/_get_idx
3.  cli.ask() calls ask.run_ask(cfg, db, idx, question, topk, use_graph, mode)
4.  run_ask() starts a `runs` row (db.start_run("ask")) and a CostTracker (usage.begin_run)
5.  run_ask() resolves effective params from cfg.retrieval.ask / cfg.retrieval (topk, mode, use_graph)
6.  run_ask() builds a Retriever(cfg, db, idx) and calls retriever.search_notes(question, topk, mode, expand_graph)
      5a. Retriever fuses dense (Chroma `query_similar_notes`) + BM25 (`StateDB.search_notes_fts`) via RRF
      5b. Retriever applies the absolute relevance floor (_apply_relevance_floor) -> passed_floor/floor_reason per hit
      5c. Retriever expands surviving seeds over the note graph (graph.expand_notes), 1..N hops
      5d. Returns NoteSearchResult(hits=[...], candidates=[...])
7.  run_ask() truncates hits to ask_cfg.max_context_notes
8.  run_ask() builds `retrieval_params` snapshot (every threshold used this call)
9.  run_ask() builds AskResult.sources / .candidates by mapping each RetrievedNote -> AskSource (_to_ask_source)
10. DECISION POINT: if hits is empty ->
      10a. result.answer = _NO_EVIDENCE (fixed PT-BR string)
      10b. finish_pipeline_run(db, run_id); RETURN — no LLM call, no prompt built
11. ELSE (hits non-empty):
      11a. _build_context(db, hits, max_chars_per_note) renders each hit into a "### Nota N" block
           with title, exact wikilink (vault.permanent_wikilink), retrieval origin, and truncated body
      11b. load_prompt_parts(cfg.prompts_path / "ask.md") splits system vs. user template on
           the `<!-- zettel:user -->` marker
      11c. fill_template() substitutes {language}, {question}, {context_notes} into both parts
      11d. compute_llm_call_checksum(prompt_hash, filled_hash, model, temperature, language)
      11e. db.get_cached_llm_response(checksum) — SQLite llm_cache lookup
      11f. CACHE HIT -> usage.record_cache_hit(); result.answer = cached (no network call)
           CACHE MISS -> get_llm(cfg); call_llm(llm, user, system=system, provider, prompt_cache)
                          -> db.cache_llm_response(checksum, request_json, answer)
                          -> result.answer = answer; result.llm_called = True
12. finish_pipeline_run(db, run_id) — persists CostTracker totals on the `runs` row, resets context
13. run_ask() RETURNS AskResult to cli.ask()
14. cli.ask() renders: answer Panel, optional retrieval-params table (--show-context),
    optional candidates table (always shown if any candidates exist, or --show-context)
15. cli.ask() optionally persists via save_ask_note()/build_ask_note_body() ->
    writes `00_Inbox/ASK - <timestamp> - <slug>.md` with YAML frontmatter + "Fontes consultadas"
16. cli.ask() prints the saved path (relative to vault_path when possible)
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Business Logic | No relevant evidence -> deterministic "no evidence" answer, LLM never called | ask.py:131-137 |
| Business Logic | Effective `topk`/`mode`/`use_graph` fall back to `cfg.retrieval.ask.topk` / `cfg.retrieval.mode` / `cfg.retrieval.graph_expansion.enabled` when not passed explicitly | ask.py:90-95 |
| Business Logic | Retrieved hits are hard-capped to `ask_cfg.max_context_notes` before building the LLM context, even if the retriever returned more via graph expansion | ask.py:100 |
| Business Logic | Each note's body is truncated to `max_chars_per_note` characters (with a `...` marker) before entering the prompt context | ask.py:139, 202-217 |
| Business Logic | LLM responses are deterministically cached by a checksum of (prompt template hash, filled-template hash, model, temperature, language); a cache hit skips the network call and is recorded as a zero-cost usage event | ask.py:150-176 |
| Business Logic | Every pipeline run (cache hit or miss, and even the no-evidence short-circuit) is wrapped in a `runs` row (`start_run`/`finish_pipeline_run`) for cost/usage bookkeeping | ask.py:87-88, 136, 178 |
| Validation / Domain Constraint | The prompt (`prompts/ask.md`) instructs the LLM to answer only from supplied context, to cite the *exact* wikilink verbatim, to say "Nao encontrei evidencia suficiente..." when context is insufficient, and to surface `contradicts`/`extends` tension between notes | prompts/ask.md:11-24 |
| Business Logic | The human-readable retrieval "origin" label is `"busca"` for direct hits (`hop == 0` or no graph path) and `"conexao <relation_type> a partir de [[ZTL - <anchor>]]"` for graph-expanded hits | ask.py:185-192 |
| Business Logic | Every `AskSource`'s `source_id`/`path` is filled from the retrieval hit's metadata first, falling back to a `StateDB.get_note()` lookup only when metadata is missing either field | ask.py:221-233 |
| Business Logic | Cosine similarity is derived from Chroma L2 distance as `1 - distance/2`, rounded to 4 decimals, and is `None` when no vector distance is available (pure BM25/graph hit) | ask.py:229-233 |
| Business Logic | The saved answer note's default destination is always `<vault>/00_Inbox/ASK - <YYYYMMDD-HHMMSS> - <slug-of-question>.md`, unless an explicit `dest` path is supplied | ask.py:298-314 |
| Business Logic | The saved note's provenance section lists every `AskSource` with origin, RRF score, similarity (when known), BM25 rank (when known), source id (when known), and floor reason (when non-empty) — or a literal "(nenhuma nota recuperada)" placeholder when `sources` is empty | ask.py:277-292 |

### Detailed breakdown of the business rules

---

### Business Rule: No-Evidence Short-Circuit (Deterministic "No Evidence" Answer)

**Overview**:
When the hybrid `Retriever` returns zero hits that clear the relevance floor, `run_ask` never constructs a prompt or calls the LLM. It immediately sets `result.answer` to the fixed string `"Nao encontrei evidencia suficiente no vault para responder a essa pergunta."` (module constant `_NO_EVIDENCE`, ask.py:31), marks `llm_called = False`, closes out the `runs` bookkeeping row, and returns.

**Detailed description**:
This is the component's central design decision and the reason the module's docstring emphasizes "no evidence rather than hallucinating." Retrieval in this project always returns *some* nearest neighbours from Chroma's kNN search — a totally off-topic question will still get back the "closest available" notes, just with low similarity. Relying on the LLM itself to notice irrelevance would be both non-deterministic (depends on prompt-following) and wasteful (every off-topic question would still cost an LLM call). Instead, `ask` treats the retriever's `NoteSearchResult.hits` field as the sole gate: `hits` only contains candidates that passed the absolute relevance floor (`Retriever._apply_relevance_floor`, retrieval.py:148-239) plus any graph neighbours of those seeds. If that list is empty, there is — by construction — no evidence worth showing the LLM.

Critically, the raw ranked pool is not discarded: `NoteSearchResult.candidates` (always populated when the corpus is non-empty) is copied into `AskResult.candidates` regardless of whether `hits` was empty, via the same `_to_ask_source` mapping used for `sources`. This lets `zettel ask --show-context` (and the always-shown "Notas recuperadas" table when any candidates exist) display exactly what was closest and *why* it was rejected (`floor_reason`), without ever feeding that content to the LLM as fact. The distinction between `sources` (what fed the answer) and `candidates` (what was merely close) is preserved end-to-end into the saved note: `build_ask_note_body` only iterates `result.sources`.

This rule interacts directly with cost control: since `finish_pipeline_run` is called on both branches, every `ask` invocation produces a `runs` row with accurate cost totals (zero for the no-evidence path, since `CostTracker` records nothing), so the `zettel status`/usage reporting is not skewed by "empty" calls appearing as full LLM calls.

**Rule workflow**:
```
search_notes() -> NoteSearchResult(hits, candidates)
if hits is empty:
    answer = _NO_EVIDENCE
    llm_called = False
    sources = []            (from empty hits)
    candidates = [...]      (from non-empty candidates, each still carrying floor_reason)
    finish_pipeline_run()
    return AskResult          # no prompt built, no get_llm()/call_llm() call
else:
    proceed to prompt construction and LLM/cache path
```

---

### Business Rule: Deterministic LLM Response Caching

**Overview**:
Before calling the LLM, `run_ask` computes a deterministic checksum over the exact inputs that would influence the answer, and looks it up in `StateDB.llm_cache`. A hit reuses the cached text verbatim and records a zero-cost usage event instead of invoking the provider.

**Detailed description**:
The checksum is built from five components via `hashing.compute_llm_call_checksum(prompt_hash, filled_hash, model, temperature, language)`: `prompt_hash` is `sha256_hex` of the *raw* prompt template (`prompt_parts.full_template`, i.e. `prompts/ask.md` reconstructed from its system/user split); `filled_hash` is `sha256_hex(normalize_text_for_hash(...))` of the fully-substituted system+user text actually sent to the model (so the exact question and exact retrieved context are part of the cache key — two different questions, or the same question against a vault that has since changed enough to alter retrieved context, produce different checksums and thus different cache entries). This mirrors the caching pattern used by `connector.py` (Phase 3) exactly, per the module's own docstring ("Uses the same deterministic LLM cache as connector").

This gives `ask` two practical properties: repeated identical questions against an unchanged vault cost nothing after the first call (useful during iterative prompt/config tuning or when a user re-runs a question with `--show-context` after already answering it), and the caching is *not* fuzzy — any change to the prompt template, the model, the temperature, the language, or the retrieved context (which shifts if the vault's notes or their embeddings change) invalidates the cache key naturally, since it's baked into the hash rather than tracked via an explicit invalidation mechanism.

A cache hit still goes through `usage.record_cache_hit(label="ask", model=cfg.llm.model)`, which is distinct from `llm_called=False` in the no-evidence case: a cache hit *is* considered "answering with evidence," it just avoided the network round-trip. `result.llm_called` therefore only distinguishes "a fresh network call happened" from "cache or no-evidence," and callers wanting to know whether real evidence was used should check `result.sources`, not `llm_called`.

**Rule workflow**:
```
prompt_hash  = sha256_hex(full_template)                      # stable per prompts/ask.md content
filled_hash  = sha256_hex(normalize_text_for_hash(system+user))  # varies with question/context
checksum     = compute_llm_call_checksum(prompt_hash, filled_hash, model, temperature, language)
cached       = db.get_cached_llm_response(checksum)
if cached is not None:
    record_cache_hit(label="ask", model=cfg.llm.model)
    answer = cached
    llm_called stays False
else:
    llm = get_llm(cfg)
    answer = call_llm(llm, user, system=system, provider=cfg.llm.provider, prompt_cache=cfg.llm.prompt_cache)
    db.cache_llm_response(checksum, json({"system","user"}), answer)
    llm_called = True
```

---

### Business Rule: Context Assembly Limits (`max_context_notes`, `max_chars_per_note`)

**Overview**:
Two independent caps from `AskConfig` (`config.py:176-181`, operational values in `config/config.yaml:163-166`) bound how much vault content reaches the LLM per call: at most `max_context_notes` notes (default 8) are used, and each note's body is truncated to `max_chars_per_note` characters (default 1500) before insertion into the prompt.

**Detailed description**:
`hits = result_pool.hits[: ask_cfg.max_context_notes]` (ask.py:100) applies the note-count cap *after* the retriever has already produced its full `hits` list (which can include seeds plus graph-expanded neighbours, potentially more than `topk` if `expand_graph=True`). This means the cap is the final word on context size regardless of how generous graph expansion was — it protects the prompt from unbounded growth when a query's seeds are richly interconnected. Because `hits` is sorted by fused score (with graph-boosted seeds reinforced, see `Retriever._expand_with_graph`), truncation here always keeps the highest-scoring notes and drops the tail, not an arbitrary subset.

`_build_context` then truncates each surviving note's body independently: `if len(body) > max_chars: body = body[:max_chars].rstrip() + "..."` (ask.py:208-210). This is a hard character cut, not a sentence- or paragraph-aware truncation — it can end mid-sentence. The two limits compound multiplicatively to bound worst-case prompt size (`max_context_notes * max_chars_per_note` plus fixed overhead per note for the wikilink/origin headers), which keeps the `ask` prompt's cost bounded and predictable per call regardless of how large or richly connected the underlying vault becomes.

Neither limit is configurable per-call through the CLI (`zettel ask` exposes `--topk` and `--mode`/`--no-graph`, but not `--max-context-notes` or `--max-chars-per-note`) — they can only be changed via `config/config.yaml`'s `retrieval.ask` block or by constructing a custom `AppConfig` programmatically (as the test suite does).

**Rule workflow**:
```
hits = retriever_hits[:ask_cfg.max_context_notes]     # count cap, highest-score-first
for hit in hits:
    body = hit.document.strip()
    if len(body) > max_chars_per_note:
        body = body[:max_chars_per_note].rstrip() + "..."
    render "### Nota N: <title>\n- Wikilink...\n- Origem...\n\n<body>"
```

---

### Business Rule: Effective Parameter Resolution (topk / mode / graph)

**Overview**:
`run_ask` accepts `topk`, `mode`, and `use_graph` as optional overrides; any left as `None` falls back to configuration defaults rather than a hardcoded value.

**Detailed description**:
```python
topk = topk if topk is not None else ask_cfg.topk                       # cfg.retrieval.ask.topk (default 8)
mode = mode or cfg.retrieval.mode                                        # "hybrid" | "vector" (default hybrid)
if use_graph is None:
    use_graph = cfg.retrieval.graph_expansion.enabled                    # default True
```
This three-way resolution lets the CLI (`zettel ask --topk N --mode vector --no-graph`) override per-invocation behavior while the underlying `AppConfig` remains the source of truth for defaults, and lets programmatic callers (e.g. the test suite, or a future consumer) omit arguments entirely and get the configured operational behavior. Note the subtlety in the `mode` resolution: `mode or cfg.retrieval.mode` uses Python truthiness, so an explicitly passed empty string `""` would also fall back to the config default (not treated as an explicit "no mode" choice) — only `None` and non-empty strings are distinguished for `topk`/`use_graph` (which use `is not None` / identity checks), while `mode` uses looser truthiness. This is a minor inconsistency in the parameter-resolution style across the three parameters, worth noting though it has no observed behavioral impact since callers only ever pass `None` or a valid non-empty mode string (`"vector"`/`"hybrid"`).

These resolved values (plus the floor and graph-expansion knobs, which are *not* overridable at the `run_ask` call boundary) are captured verbatim into `retrieval_params`, which becomes the single source of truth for what a specific `ask` invocation actually did — surfaced to the user via `--show-context`'s "Parametros de recuperacao" table.

**Rule workflow**:
```
effective_topk    = topk if topk is not None else cfg.retrieval.ask.topk
effective_mode    = mode or cfg.retrieval.mode
effective_graph   = use_graph if use_graph is not None else cfg.retrieval.graph_expansion.enabled
retriever.search_notes(question, topk=effective_topk, mode=effective_mode, expand_graph=effective_graph)
```

---

### Business Rule: Full-Provenance Answer Persistence

**Overview**:
When the user opts to save an answer (`--save`, `--save-to`, or interactive confirmation), `build_ask_note_body`/`save_ask_note` produce a self-contained Markdown note whose frontmatter and body fully describe how the answer was produced — question, answer, retrieval mode, graph-expansion flag, model, and a per-source citation breakdown.

**Detailed description**:
`build_ask_note_body` (ask.py:253-295) is pure (no I/O): it returns a `(meta, body)` tuple. `meta` always contains `type: ask_answer`, the verbatim `question`, an ISO `created_at` timestamp, `origin: ask`, `retrieval_mode`, `graph_expansion`, and `llm_model` — enough to know *how* the answer was generated without re-reading the body. The body is hand-assembled Markdown (not a template file): a `# Pergunta` heading with the question, a `## Resposta` heading with the trimmed answer text (which may itself contain `[[ZTL - ...]]` wikilinks the LLM was instructed to copy verbatim from the context), and a `## Fontes consultadas` section.

The sources section iterates `result.sources` (never `result.candidates` — rejected notes are deliberately excluded from the persisted provenance, even though they're shown transiently in the CLI's debug table). For each source it renders the wikilink and title, then a nested detail line combining origin, RRF score, similarity (only if not `None`), BM25 rank (only if not `None`), and source id (only if truthy) — and a second nested line with `floor_reason` if non-empty. When `result.sources` is empty (i.e., the no-evidence path was saved anyway), the section falls back to a literal `"- (nenhuma nota recuperada)"` line rather than an empty section, so the note is never ambiguous about whether sources were considered and found empty versus never checked.

`save_ask_note` (ask.py:298-314) resolves the destination path: an explicit `dest` is used as-is; otherwise it builds `00_Inbox/ASK - <YYYYMMDD-HHMMSS> - <slug>.md` where `<slug>` comes from `vault._slug(question)` (falling back to the literal string `"pergunta"` if the slug is empty, e.g. for a question containing only punctuation/emoji). It always ensures the parent directory exists (`mkdir(parents=True, exist_ok=True)`) before writing, and writes with `encoding="utf-8"`. The choice of `00_Inbox/` (rather than e.g. `30_Permanent/`) is deliberate per the docstring: it's an unreviewed, ephemeral artifact of a QA session, not a first-class Zettelkasten note — the user is expected to manually promote/incorporate any lasting insight, consistent with the project's Inbox-first convention (mirrored by `article.py`'s `ART - ...md` notes, per CLAUDE.md).

**Rule workflow**:
```
meta = {type: ask_answer, question, created_at=now(), origin: ask,
         retrieval_mode, graph_expansion, llm_model}
body = "# Pergunta\n\n{question}\n\n## Resposta\n\n{answer}\n\n## Fontes consultadas\n\n"
for src in result.sources:
    body += "- {wiki_link} — {title}\n"
    body += "    - origem: {origin} | score RRF: {rrf_score}"
             " [| similaridade: X] [| rank BM25: Y] [| fonte: source_id]\n"
    if src.floor_reason:
        body += "    - motivo: {floor_reason}\n"
if not result.sources:
    body += "- (nenhuma nota recuperada)\n"

dest = dest or vault_path/"00_Inbox"/f"ASK - {timestamp} - {slug(question) or 'pergunta'}.md"
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text(render_frontmatter(meta) + "\n" + body + "\n", encoding="utf-8")
```

---

### Business Rule: Retrieval Origin Labeling

**Overview**:
Every retrieved note is tagged with a human-readable "origin" string that distinguishes a direct search match from a note surfaced only via graph expansion, and — for the latter — names the relation type and anchor note that brought it in.

**Detailed description**:
`_origin_label(hit)` (ask.py:185-192) inspects `hit.hop` and `hit.via` (both populated by `graph.expand_notes` inside the `Retriever`). A hit with `hop == 0` or an empty `via` list is labeled `"busca"` (a direct RRF/vector/BM25 seed). Otherwise, the *last* step of the `via` path (`hit.via[-1]`) is read for its `relation_type` (defaulting to `"related"` if missing) and its `from` anchor id, producing a string like `"conexao depends_on a partir de [[ZTL - 01ABC]]"`. This label is used in three places for consistency: inline in the LLM's own context (`_build_context`, so the model itself can weigh graph-derived context differently per the prompt's rule 4 about `contradicts`/`extends`), in the CLI's `--show-context` table, and in the persisted note's provenance section. Because it's derived fresh from `hit.via`/`hit.hop` each time (not stored), the label is always consistent with the actual graph path that produced the hit in that specific run — it cannot drift out of sync with the retrieval result it describes.

**Rule workflow**:
```
if hit.hop == 0 or not hit.via:
    origin = "busca"
else:
    step   = hit.via[-1]
    rel    = step.get("relation_type", "related")
    anchor = step.get("from", "")
    origin = f"conexao {rel} a partir de [[ZTL - {anchor}]]"
```

---

## 4. Component Structure

`ask` is a single-file component with two direct collaborators (its prompt template and its test suite):

```
zettel/
└── ask.py                          # Entire component: run_ask, AskResult, AskSource,
                                     # build_ask_note_body, save_ask_note, context builders
prompts/
└── ask.md                          # PT-BR system+user prompt template (LLM instructions
                                     # + {question}/{context_notes} placeholders)
tests/
└── test_ask.py                     # Unit tests for run_ask (empty-vault, below-floor,
                                     # wikilink propagation) and note persistence
```

Internal organization of `zettel/ask.py` (by section, per its own `# ──` comment banners):

```
zettel/ask.py
├── Module docstring + imports (hashing, llm, retrieval, vault)
├── Constant: _NO_EVIDENCE                     # fixed PT-BR "no evidence" string
├── Dataclass: AskSource                       # one cited note + provenance fields
├── Dataclass: AskResult                       # question/answer/sources/candidates/params
├── Public API
│   └── run_ask(cfg, db, idx, question, ...)   # orchestration entry point
├── Context building
│   ├── _origin_label(hit)                     # "busca" vs "conexao <rel> a partir de ..."
│   ├── _wiki_link(db, note_id, title)          # resolves permanent_wikilink via StateDB
│   ├── _build_context(db, hits, max_chars)     # renders LLM-facing "### Nota N" blocks
│   └── _to_ask_source(db, hit)                 # RetrievedNote -> AskSource mapping
└── Saving the answer as a provenance-rich note
    ├── build_ask_note_body(result)             # pure: -> (frontmatter dict, body str)
    └── save_ask_note(result, vault_path, dest) # writes .md file, returns Path
```

---

## 5. Dependency Analysis

```
Internal Dependencies:

zettel/cli.py (ask command)
    -> zettel.ask.run_ask()
    -> zettel.ask.save_ask_note()

zettel/ask.py (run_ask)
    -> zettel.usage.begin_run / finish_pipeline_run / record_cache_hit   (cost tracking)
    -> zettel.state.StateDB.start_run / get_cached_llm_response /
                     cache_llm_response / get_note / (indirectly) search_notes_fts
    -> zettel.retrieval.Retriever.search_notes()                        (hybrid retrieval)
        -> zettel.graph.expand_notes()                                  (graph expansion)
        -> zettel.index.VectorIndex.query_similar_notes()               (dense search)
    -> zettel.llm.load_prompt_parts / fill_template / get_llm / call_llm
    -> zettel.hashing.sha256_hex / normalize_text_for_hash /
                       compute_llm_call_checksum
    -> zettel.vault.permanent_wikilink / _slug / render_frontmatter
    -> zettel.config.AppConfig (types only, TYPE_CHECKING)

External Dependencies:
- LangChain-based LLM clients (via zettel.llm.get_llm) - OpenAI/Anthropic/Gemini/
  Ollama/OpenAI-compatible gateways, provider chosen by cfg.llm.provider
- LiteLLM (indirectly, via zettel.pricing/usage for cost estimation of the call_llm result)
- ChromaDB (via VectorIndex.query_similar_notes) - dense vector search over permanent_notes
- SQLite (via StateDB) - FTS5 BM25 search, note_connections graph, llm_cache, runs bookkeeping
- Filesystem - prompts/ask.md read; 00_Inbox/ASK - ...md written
```

---

## 6. Afferent and Efferent Coupling

Coupling counted at the function/class granularity within and around `ask.py` (Python module, not OOP-heavy — most "components" here are top-level functions/dataclasses).

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| `run_ask` | 1 (cli.ask, tests) | 9 (Retriever, StateDB x4, usage x3, llm x4, hashing x3) | High |
| `AskResult` | 3 (run_ask, cli.ask, tests) | 0 (pure dataclass) | Medium |
| `AskSource` | 2 (_to_ask_source, tests) | 0 (pure dataclass) | Low |
| `_to_ask_source` | 1 (run_ask) | 3 (StateDB.get_note, _wiki_link, RetrievedNote fields) | Medium |
| `_build_context` | 1 (run_ask) | 2 (_wiki_link, _origin_label) | Medium |
| `_origin_label` | 2 (_build_context, _to_ask_source) | 0 | Low |
| `_wiki_link` | 2 (_build_context, _to_ask_source) | 2 (StateDB.get_note, vault.permanent_wikilink) | Low |
| `build_ask_note_body` | 2 (save_ask_note, tests) | 0 (pure) | Low |
| `save_ask_note` | 2 (cli.ask, tests) | 2 (build_ask_note_body, vault._slug) | Low |

`run_ask` is the component's single point of highest coupling in both directions — it is the only function the CLI calls, and it fans out to nearly every shared-infrastructure module in the project (`retrieval`, `llm`, `hashing`, `vault`, `state`, `usage`). This is expected and appropriate for an orchestration function at a pipeline command's entry point, but it does mean `run_ask` is the single place where a breaking change in any of those five modules' public APIs would surface.

---

## 7. Endpoints

Not applicable — `ask` is not a REST/GraphQL/gRPC service. It is exposed exclusively as a Typer CLI subcommand.

| Interface | Command | Description |
|-----------|---------|-------------|
| CLI (Typer) | `zettel ask "<question>" [--topk N] [--no-graph] [--mode vector\|hybrid] [--show-context] [--save] [--save-to PATH] [--no-save-prompt] [--yes/-y] [--config/-c PATH]` | Answers a question from the vault using hybrid retrieval + graph expansion; optionally shows retrieval internals and/or saves the answer as a note |

Confirmed **not exposed** in the web UI: neither `zettel/web.py` nor `zettel/web_app.py` reference `zettel.ask` in any form (grep found zero matches), consistent with the project's documented "Not exposed in web" list (CLAUDE.md), which explicitly names `ask` among CLI-only commands.

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| Configured LLM provider (OpenAI/Anthropic/Gemini/Ollama/compatible) | External Service | Generate the grounded answer | HTTPS (via LangChain client) | Text prompt in / text out | No explicit try/except in ask.py; errors propagate up through `call_llm`/`llm.get_llm` to the CLI caller |
| ChromaDB (`permanent_notes` collection) | Embedded Vector DB | Dense nearest-neighbour note search | In-process (ChromaDB client) | Vectors + metadata dicts | Handled inside `Retriever._vector_notes` (catches and logs, returns `[]` on failure) — outside `ask.py` itself |
| SQLite `state.db` (FTS5, `note_connections`, `llm_cache`, `runs`) | Embedded Relational DB | BM25 lexical search, graph edges, response cache, cost bookkeeping | In-process (sqlite3) | Rows / JSON blobs (`llm_cache.response_json`) | FTS gracefully degrades to vector-only (`Retriever._warn_no_fts`) when FTS5 unsupported; no error handling for SQLite failures inside `ask.py` itself |
| Filesystem — `prompts/ask.md` | Local File | Source of system/user prompt template | File read | Markdown with `{placeholder}` tokens + `<!-- zettel:user -->` split marker | No explicit handling in `ask.py`; a missing/malformed file would raise from `load_prompt_parts`/`load_prompt` |
| Filesystem — vault `00_Inbox/` | Local File | Persist the answer note | File write | Markdown with YAML frontmatter | `dest.parent.mkdir(parents=True, exist_ok=True)` guards missing directories; no retry/atomic-write logic; overwrites silently if `dest` collides with an existing file (though the timestamp-based default filename makes collisions unlikely) |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Facade / Orchestrator | `run_ask` composes Retriever + LLM + cache + vault helpers behind one function call | ask.py:75-179 | Single entry point hides multi-module coordination from the CLI |
| Strategy (implicit) | `mode` parameter (`vector` vs `hybrid`) selects retrieval strategy inside `Retriever`, transparently passed through | ask.py:92, retrieval.py:78-128 | Lets `ask` degrade gracefully when FTS5 unavailable, without branching in `ask.py` itself |
| Null Object / Sentinel Result | `_NO_EVIDENCE` constant returned as a full, valid `AskResult` (not an exception or `None`) when no evidence is found | ask.py:31, 131-137 | Callers (CLI) always get a uniform `AskResult` shape to render, regardless of whether the LLM ran |
| Data Transfer Object | `AskSource`/`AskResult` dataclasses decouple internal `RetrievedNote` representation from the CLI/note-writing consumers | ask.py:34-69 | Keeps `retrieval.py`'s internal fields (`vector_rank`, `metadata`, etc.) from leaking verbatim into the CLI/persistence layer |
| Deterministic Cache Key (content-addressed caching) | `compute_llm_call_checksum` over prompt hash + filled-context hash + model/temperature/language | ask.py:150-154 | Avoids repeat LLM spend for identical (prompt, context, model) tuples; shared pattern with `connector.py` |
| Template Method (prompt-level) | `prompts/ask.md`'s `<!-- zettel:user -->` split separates stable "system" instructions from per-call "user" payload | ask.py:140, llm.py:355-397 | Enables provider-side prompt caching (Anthropic `cache_control`) without `ask.py` knowing about provider specifics |
| Transparent/"show your work" reporting | `candidates` (raw pool) vs `sources` (used pool) kept as separate lists throughout, never merged | ask.py:56-60, 100-137 | Lets `--show-context` explain *why* a note wasn't used, rather than silently hiding it |

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| Low | `run_ask` parameter resolution | `mode = mode or cfg.retrieval.mode` uses truthiness (an explicit `""` falls back to config) while `topk`/`use_graph` use `is not None` checks — inconsistent null-handling convention across three sibling parameters | Purely cosmetic today (no caller passes `""`), but a future caller passing an empty string expecting "explicit override to falsy" would get silently overridden |
| Low | `_build_context` truncation | Body truncation is a raw character cut (`body[:max_chars]`), not sentence/paragraph-aware | Can hand the LLM a context block ending mid-sentence, which may (rarely) confuse the model or produce an awkward citation boundary |
| Low | `save_ask_note` | No collision/atomic-write handling — `dest.write_text` overwrites unconditionally if the resolved path already exists (relevant mainly for `--save-to` with a fixed filename, since the default timestamp-based name is effectively unique) | Silent overwrite of a prior manual edit to a previously-saved `--save-to` file |
| Low | `run_ask` — LLM/DB error paths | No explicit `try/except` around `get_llm`/`call_llm`/`db.cache_llm_response`; a provider outage or malformed API response propagates as a raw exception into the CLI's `console.status(...)` context | User sees a raw traceback rather than a friendly "LLM unavailable" message (though this matches the rest of the pipeline's error-handling style, which is generally non-defensive at the command layer) |
| Informational | Test coverage | No test currently exercises `run_ask` on a cache-hit path (`db.get_cached_llm_response` returning non-`None`) | A regression in the cache-hit branch (e.g. `record_cache_hit` call signature drift) would not be caught by the current suite |
| Informational | Test coverage | No test exercises the `--show-context`/`retrieval_params` CLI rendering path (that logic lives in `cli.py`, outside `ask.py`, but `AskResult.retrieval_params`'s exact key set is a de facto contract with `cli.py:1386-1408` that only a CLI-level test would protect) | A key rename in `retrieval_params` (ask.py:104-118) would silently break the CLI table without any test failing |

---

## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage (qualitative) | Test Quality |
|-----------|------------|--------------------|--------------------------|---------------|
| `run_ask` (no-evidence path) | 1 (`test_run_ask_empty_vault_no_llm`) | 0 | Good for this path | Asserts both the answer text and that `call_llm` is never invoked (via a monkeypatched `_boom` that raises `AssertionError` if called) — a strong negative assertion |
| `run_ask` (evidence path, prompt/context building) | 1 (`test_run_ask_passes_wikilimks_to_prompt`, ask.py naming: `test_run_ask_passes_wikilinks_to_prompt`) | 0 | Good — verifies the exact wikilink text reaches the LLM's user prompt | Uses a monkeypatched `Retriever.search_notes` and `call_llm`, plus a real `AppConfig`/temp prompt file, closely mirroring production wiring |
| `run_ask` (below-floor path with populated `candidates`) | 1 (`test_run_ask_below_floor_shows_candidates_but_no_llm_call`) | 0 | Good | Confirms `sources == []`, `candidates` populated with `passed_floor=False`, and no LLM call — directly exercises the "transparency without evidence" contract |
| `build_ask_note_body` | 1 (`test_build_ask_note_body_provenance`) | 0 | Good for the populated-sources case | Checks frontmatter fields, wikilink presence for both a direct and a graph-hop source, `@Paper2024` source id, `conexao depends_on` origin text, and conditional `similaridade:` inclusion |
| `build_ask_note_body` (empty-sources fallback `"(nenhuma nota recuperada)"`) | 0 | 0 | Not covered | No test asserts the `"(nenhuma nota recuperada)"` fallback line when `result.sources` is empty |
| `save_ask_note` (default location) | 1 (`test_save_ask_note_default_location`) | 0 | Good for the default-path case | Verifies `00_Inbox` parent dir, `ASK - ` filename prefix, and frontmatter/body presence in the written file |
| `save_ask_note` (explicit `dest` override) | 0 | 0 | Not covered | No test exercises `save_to`/explicit `dest` argument path |
| `_origin_label` / `_wiki_link` / `_to_ask_source` (as standalone units) | 0 direct | 0 | Covered only indirectly through `run_ask` tests | Reasonable given they are private helpers, but a future refactor of `_to_ask_source`'s source_id/path fallback logic (ask.py:222-228) has no dedicated unit test |
| Cache-hit path (`db.get_cached_llm_response` returns non-`None`) | 0 | 0 | Not covered | See Technical Debt table above |
| CLI wiring (`zettel ask` command, `--show-context` table, `--save`/`--save-to`/`--no-save-prompt` flags) | 0 | 0 (no `test_cli.py` matches found for `ask`) | Not covered at the CLI layer | Only `tests/test_ask.py` exists; no CLI-level test invokes the `ask` Typer command via `CliRunner` or equivalent |

Test file location: `D:\projetos\zettel_app\tests\test_ask.py` (159 lines, 6 test functions, all colocated in one file — no separate integration test suite for `ask`).
