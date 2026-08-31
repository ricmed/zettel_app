# Component Deep Analysis Report — `article_graph`

## 1. Executive Summary

`zettel/article_graph.py` is the LangGraph **orchestration layer** behind the `zettel article` CLI command (Phase "article" — a long-form writing feature distinct from and running parallel to the core `harvest → extract → review → connect → garden` pipeline). It does not implement business logic itself; it wires a `StateGraph` of nodes that each delegate to pure domain functions in `zettel/article.py` (the sibling module explicitly named "Domain helpers... live here" in its own docstring). The component's job is strictly **control flow**: node sequencing, conditional routing, human-in-the-loop (HITL) interrupt handling, iteration/loop bounding, and state accumulation across a multi-step LLM writing pipeline (query enrichment → incremental hybrid search → context review → catalog build → outline generation/review → section drafting → assembly → personality rewrite → quality-judge loop → verification/finish).

Key architectural facts:
- Built on `langgraph.graph.StateGraph` with a `TypedDict` state (`ArticleGraphState`, `total=False`) and compiled with an in-memory checkpointer (`MemorySaver`) per invocation (one throwaway thread per call — no cross-run persistence).
- Two HITL surfaces exist: `context_review` (after search) and `outline_review` (after outline generation). Each can be satisfied three ways, in priority order: (1) a direct Python callback (`context_callback` / `approve_outline`, used by tests and non-interactive callers), (2) a `hitl_handler` that resolves LangGraph's native `interrupt()` mechanism (used by the CLI for interactive Rich prompts), or (3) if neither is supplied, the interrupt loop in `run_article_graph` auto-approves.
- The component carries no direct database/vector-store code of its own; all such I/O flows through the `ArticleRuntime` dataclass (`cfg`, `db`, `idx`) threaded through `RunnableConfig["configurable"]["runtime"]`, and through calls into `zettel.article`, `zettel.retrieval.Retriever`, and `zettel.graph.expand_notes`.
- The judge loop is bounded (`max_judge_iterations`, default 3) and always terminates — via approval, exhausting the iteration budget (with a warning appended), or an early abort when no evidence is found.
- Cost/usage tracking is wired at the graph-run boundary only (`begin_run`/`finish_pipeline_run` in `run_article_graph`), not per node — individual nodes report LLM-call state back via a `llm_called` flag threaded through `ArticleRuntime.llm_called` and the state dict.

The component is small (715 lines), self-contained, and cleanly separates orchestration (this file) from domain logic (`article.py`), which is a deliberate design choice documented in both files' module docstrings.

## 2. Data Flow Analysis

End-to-end path for a single `zettel article "tema"` invocation (CLI-driven, interactive HITL):

```
1.  CLI `article()` command (zettel/cli.py:1467) parses flags, loads AppConfig/StateDB/VectorIndex,
    defines a Rich-based `_hitl(payload)` handler, and calls `run_article_graph(...)`.
2.  run_article_graph() (article_graph.py:604) starts a usage-tracking run (`db.start_run("article")`,
    `begin_run(run_id)`), builds an ArticleRuntime, compiles build_article_graph() with a MemorySaver
    checkpointer, and invokes the graph with an initial ArticleGraphState.
3.  node_query_enricher (article_graph.py:102) calls art.enrich_search_queries() -> LLM Prompt
    "article_query_enrich.md" -> JSON {"queries": [...]}, deduplicated and topic-prefixed.
    (On loop-back from context enrichment, it reuses only the new `extra_queries`, skipping the LLM.)
4.  node_vector_search_merge (article_graph.py:123) iterates pending queries against
    Retriever.search_notes() (hybrid RRF + relevance floor + optional graph expansion), merging
    hits into an accumulating pool by note_id (best score wins), capped at `max_context_notes`.
    Adds a one-time MOC boost (art._merge_moc_notes) and, if the article's own `max_hops` exceeds
    the global graph_expansion max_hops, performs an extra BFS expansion via note_graph.expand_notes().
    Emits a `retrieval_params` snapshot (mode, topk, rrf_k, floor thresholds, graph settings) for
    later provenance/debugging.
5.  node_context_review (article_graph.py:242) short-circuits to "abort" if no notes were found
    (`no_evidence`), auto-approves if `skip_context_review`, delegates to `context_callback` if
    set, or otherwise calls LangGraph `interrupt()` with a `context_review` payload (notes +
    executed_queries) and waits for a resume value carrying `context_decision`
    (approve/enrich/abort) and optional `extra_queries`.
6.  route_after_context (article_graph.py:266) sends the graph back to `query_enricher` (loop) on
    "enrich" with extras present, to `build_catalog` on "approve"/default, or to `abort` on
    "abort"/no_evidence.
7.  node_build_catalog (article_graph.py:277) calls art.catalog_from_retrieved() to build an
    ArticleCatalog (notes, sources, assets) from the accumulated retrieved-note dicts. Aborts
    with `no_evidence=True` if the catalog ends up with zero notes (defensive re-check).
8.  node_generate_outline (article_graph.py:294) calls art.generate_outline() -> LLM Prompt
    "article_outline.md" -> JSON ArticleOutline, sanitized against the known note/asset universe
    (art._sanitize_outline: drops unknown IDs, back-fills empty sections with top notes, caps
    section count at `max_sections`).
9.  node_outline_review (article_graph.py:308) auto-approves if `outline_only`; otherwise
    delegates to `outline_callback` or raises `interrupt()` with an `outline_review` payload
    (outline JSON + a human-readable preview) and awaits `outline_decision`
    (approve/regenerate/abort) plus optional `outline_feedback`.
10. route_after_outline (article_graph.py:337) routes to `outline_only_finish` (if outline_only),
    back to `generate_outline` (regenerate, carrying feedback into the next LLM call), to
    `draft_sections` (approve), or to `abort`.
11. node_draft_sections (article_graph.py:365) calls art.draft_sections() once per outline
    section -> LLM Prompt "article_section_blog.md" or "article_section_academic.md" (chosen by
    catalog.style), packing each section's evidence/sources/figures via art._pack_section(), and
    threading any prior `judge_feedback` into the prompt on redraft cycles.
12. node_assemble (article_graph.py:383) calls art.assemble_article() to merge section bodies into
    final Markdown: strips `<!-- cites: ... -->` comments while resolving them to source_ids,
    renumbers figure embeds, appends a style-specific bibliography ("Para saber mais" for blog,
    ABNT "Referencias" for academic) and a "Origem no vault" provenance appendix. Frontmatter is
    updated with llm_model/topic/style/origin/personality.
13. node_personality (article_graph.py:411) calls art.apply_personality_rewrite() -> LLM Prompt
    "article_personality.md" using a personality profile loaded from
    `config/personalities.yaml` (no LLM call at all when personality is "neutral" and no custom
    style notes are given).
14. node_judge (article_graph.py:424) — skipped (auto "APPROVED", score 10.0) if `skip_judge`;
    otherwise calls art.judge_article_body() -> LLM Prompt "article_judge.md" -> JSON scores
    (fidelity/coverage/references/naturalness/average/verdict/feedback). If REJECTED, increments
    `iteration_count` and carries `judge_feedback` forward.
15. route_after_judge (article_graph.py:451) finishes on APPROVED or `skip_judge`; loops back to
    `draft_sections` (redraft) while `iteration_count < max_judge_iterations`; otherwise finishes
    with a warning appended.
16. node_finish (article_graph.py:464) picks styled_body (fallback draft_body), appends a
    "judge did not approve" warning if the loop was exhausted, runs art.verify_article()
    (deterministic checks: empty body, missing embeds, orphan academic citations), and stamps
    judge_average/judge_verdict/executed_queries into frontmatter.
17. run_article_graph()'s while-loop resolves any `__interrupt__` in the returned state by calling
    `hitl_handler(payload)` and resuming the graph with `Command(resume=resume_val)`, repeating
    until the graph reaches END.
18. _result_from_state() (article_graph.py:570) packages the final ArticleGraphState into an
    art.ArticleResult (title, body, frontmatter, outline, warnings, llm_called, note_ids,
    source_ids, no_evidence, aborted).
19. run_article_graph()'s `finally` block calls finish_pipeline_run(db, run_id) to persist
    accumulated LLM/embedding cost totals on the `runs` row.
20. Back in cli.py: on no_evidence/aborted, prints a panel and exits; otherwise prints the body,
    warnings, optional context table, and (via Rich Confirm or --save/--save-to flags) calls
    art.save_article_note() to write `00_Inbox/ART - <timestamp> - <slug>.md`.
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Termination | Search+context loop aborts immediately if zero notes are retrieved (`no_evidence`) | article_graph.py:231, :244, node_build_catalog:289 |
| HITL Precedence | Callback (context_callback/approve_outline) takes priority over interrupt(); if neither is set, callback-driven runs auto-skip context review | article_graph.py:248-249, 644-646 |
| HITL Precedence | `hitl_handler` resolves native `interrupt()`; absent handler auto-approves any stray interrupt | article_graph.py:695-711 |
| Loop bound | Context-review "enrich" loop only re-enters `query_enricher` when `extra_queries` is non-empty | article_graph.py:272-273 |
| Loop bound | Outline "regenerate" loop has no explicit iteration cap (unbounded until user approves/aborts) | article_graph.py:343-344 |
| Loop bound | Judge "redraft" loop is capped at `max_judge_iterations` (default 3); exceeding it still finishes, with a warning, using the best available draft | article_graph.py:451-461, node_finish:469-477 |
| Query dedup | On loop-back after context enrichment, only new `extra_queries` are searched — previously executed queries are never re-run | article_graph.py:107-114, 138 |
| Merge/scoring | Retrieved notes are merged by `note_id` keeping the highest score across queries, then capped at `max_context_notes` | article.py:960-975 |
| Merge/scoring | A MOC (Map of Content) matching the topic is applied once as a boost: any note wikilinked from the MOC body gets `score = max(existing, 1.0)` | article.py:503-531, article_graph.py:163-172 |
| Graph expansion | Extra BFS graph hops beyond the global config only trigger when the article-specific `max_hops` exceeds `retrieval.graph_expansion.max_hops` | article_graph.py:176 |
| Outline sanitation | Any note_id/asset_id referenced by the LLM outline that isn't in the retrieved catalog is silently dropped | article.py:690-709 |
| Outline sanitation | A section left with zero valid note_ids after filtering falls back to the catalog's first 3 notes | article.py:697-700 |
| Outline sanitation | An outline with zero valid sections falls back to a single synthetic "Desenvolvimento" section using up to 5 catalog notes | article.py:710-718 |
| Outline sanitation | Sections beyond `max_sections` are truncated | article.py:696 |
| Personality no-op | `personality == "neutral"` with no custom style notes skips the LLM call entirely (deterministic pass-through) | article.py:1086-1087 |
| Judge scoring | `average` is computed as the arithmetic mean of the 4 sub-scores when the LLM doesn't supply one | article.py:1141-1143 |
| Judge scoring | `verdict` is forced to REJECTED whenever `average < judge_min_score`, regardless of what the LLM said | article.py:1147-1148 |
| Judge scoring | An invalid/missing `verdict` string is normalized against the same `judge_min_score` threshold | article.py:1149-1152 |
| Citation extraction | Section citations are parsed from a `<!-- cites: @Citekey1, @Citekey2 -->` HTML comment, then stripped from the visible text | article.py:298-299, 865-872 |
| Citation extraction | Academic style additionally attempts to match parenthetical `(SURNAME, YEAR)` citations against the catalog by surname+year regex | article.py:309-312, 894-910 |
| Citation extraction | Unknown citekeys referenced in a section produce a warning but do not fail the run | article.py:305-306 |
| Figure handling | Figure embeds (`![[90_Assets/...]]`) are deduplicated and renumbered sequentially across the assembled article | article.py:314-334 |
| Figure handling | Assets are capped at `max_figures`, ranked by cross-note reference frequency when the catalog exceeds the cap | article.py:604-612 |
| Bibliography style | Blog style renders a "Para saber mais" informal reading list; academic style renders an ABNT-sorted "Referencias" section | article.py:354-399 |
| Bibliography fallback | Blog style falls back to listing all catalog sources (not just cited ones) if no citations were extracted, still recording them as cited | article.py:366-373 |
| Bibliography fallback | Academic style falls back to a minimal `Author. Title. Year.` reference when `abnt_reference` is missing, and emits a warning if zero references resolve | article.py:383-399 |
| Verification | Deterministic post-hoc checks flag empty article bodies, missing figure embeds on disk, and academic citations with no matching catalog surname — all as warnings, never exceptions | article.py:421-446 |
| LLM caching | Every LLM call in the article pipeline is content-addressed (prompt+system+user+model+temperature+language hash) and served from `state.db`'s `llm_cache` when available, at $0 cost | article.py:795-845 |
| Cost tracking | `llm_called` is tracked per-node and OR-accumulated onto both the returned state and `ArticleRuntime.llm_called`, giving the final result an accurate "did this run actually call an LLM" signal even across cache hits | article_graph.py:92-96, all node functions |
| Outline-only shortcut | `--outline-only` forces `skip_judge=True` and skips context/outline HITL, producing only a formatted outline preview as the final body | article_graph.py:648, 312-313, node_outline_only_finish:348-362 |
| Run bookkeeping | Every graph run opens and closes a `runs` row (cost/usage totals) regardless of success, abort, or exception, via a `try/finally` | article_graph.py:631-632, 714-715 |

### Detailed breakdown of the business rules

---

### Business Rule: No-Evidence Abort

**Overview**:
If, after all search queries execute (including any HITL-driven enrichment), the accumulated retrieved-note pool is empty, the pipeline refuses to proceed to outline/drafting and returns a fixed "insufficient evidence" message instead of asking the LLM to write from nothing.

**Detailed description**:
This is the article pipeline's equivalent of the `ask` command's "no evidence, don't call the LLM" guarantee described in CLAUDE.md, reapplied to long-form writing. `node_vector_search_merge` sets `no_evidence = not existing` purely from whether any note survived retrieval + relevance floor + graph merge (article_graph.py:231). `node_context_review` checks this flag first, ahead of any HITL logic, and immediately routes to `abort` (article_graph.py:244-245), so a user is never shown a context-review prompt for an empty pool. As a second line of defense, `node_build_catalog` re-checks `catalog.notes` after catalog construction and also flags `no_evidence`/`aborted` if the catalog ends up empty (article_graph.py:289-291) — this guards against a retrieval bug where hits existed but catalog population silently dropped every one (e.g., all notes deleted from the vault between search and catalog build).

When `no_evidence` is set, `route_after_context` (and, if catalog-triggered, no route is even needed since the state was already marked) sends control to `node_abort`, which returns `art._NO_EVIDENCE` — a hardcoded PT-BR string ("Não encontrei evidência suficiente no vault...") — as the `final_body`, with `aborted=True` and empty frontmatter/warnings (article_graph.py:496-504). Critically, no LLM call happens anywhere in this path: `llm_called` stays false unless a prior query-enrichment call already flipped it. The CLI surfaces this by printing the body in a "Sem evidência" panel and exiting 0 (cli.py:1604-1607) rather than treating it as an error.

This rule interacts with the loop-back rule: a context-review "enrich" decision only fires when the *current* pool is non-empty (an empty pool short-circuits to abort before the decision is even evaluated), so a user cannot "enrich" their way out of a truly empty first search — they would need to restart with a different topic entirely.

**Rule workflow**:
```
search all queries -> pool empty? --yes--> no_evidence=True -> context_review short-circuits
                                                              -> route_after_context -> abort
                                                              -> final_body = _NO_EVIDENCE
                    --no---> context_review proceeds normally -> build_catalog
                                                                -> catalog.notes empty? --yes--> no_evidence=True, aborted=True
                                                                                        --no---> continue to outline
```

---

### Business Rule: HITL Resolution Precedence (Callback > Interrupt > Auto-approve)

**Overview**:
Both human-in-the-loop checkpoints (context review, outline review) support three mutually exclusive resolution strategies chosen at `run_article_graph()` call time, in a strict precedence order, so the same graph definition serves synchronous test code, programmatic callers, and the interactive CLI without branching the graph itself.

**Detailed description**:
`node_context_review` and `node_outline_review` each check, in order: (1) is a direct Python callback present (`rt.context_callback` / `rt.outline_callback`)? If so, call it synchronously with the current state/outline and return its result directly — no LangGraph interrupt is ever raised (article_graph.py:248-249, 315-316). (2) If no callback, raise `interrupt()` with a structured payload and block until the graph is resumed with a `Command(resume=...)` (article_graph.py:251-263, 322-334). This is what the CLI uses in `_hitl()` (cli.py:1526-1586) to drive Rich-based interactive prompts. (3) If the payload returned by either path is malformed (not a dict), a safe default of `{"context_decision": "approve", "extra_queries": []}` or `{"outline_decision": "approve"}` is substituted, so a broken caller can never wedge the graph.

`run_article_graph()` itself adds a further fallback at the top level: `outline_cb` defaults to an always-approve lambda when *both* `approve_outline` and `hitl_handler` are `None` (article_graph.py:639-641) — this is what lets tests and library callers omit HITL entirely and get a fully automated run. Separately, `effective_skip_context` is forced to `True` whenever neither `hitl_handler` nor `context_callback` is supplied (article_graph.py:644-646), meaning context review is bypassed by default for non-interactive callers rather than raising an unresolvable interrupt. The CLI is the only caller that supplies `hitl_handler` (not the two callbacks), so it is also the only caller that goes through LangGraph's native `interrupt()`/`Command(resume=...)` resumption cycle end-to-end.

Finally, even in the `hitl_handler` path, `run_article_graph()`'s resume loop has a defensive branch: if `result_state.get("__interrupt__")` is truthy but `hitl_handler` is somehow `None` (a state that should not occur given the guards above, but is defended against explicitly), it auto-approves by inspecting the interrupt's `type` field and supplying the matching approve-shaped resume dict (article_graph.py:695-707). This triple-layered fallback (per-node default -> top-level callback default -> resume-loop default) means the graph can never truly deadlock waiting on a human that isn't there.

**Rule workflow**:
```
node needs a HITL decision
  -> callback set (context_callback / outline_callback)? --yes--> call it directly, use its return
  -> no callback --> raise interrupt(payload)
       -> hitl_handler set?  --yes--> resume loop calls hitl_handler(payload), Command(resume=...)
                              --no ---> resume loop auto-approves based on interrupt type
  -> payload malformed at any stage --> fall back to a safe "approve" default
```

---

### Business Rule: Query Enrichment Deduplication Across the Enrich Loop

**Overview**:
When a user (or callback) sends the context review back into "enrich" mode with extra search queries, the pipeline re-enters `query_enricher` but avoids both re-running the LLM query-expansion prompt and re-executing already-searched queries.

**Detailed description**:
`node_query_enricher` distinguishes two situations using `state.get("executed_queries")` as a marker of "has at least one search round already happened." On the very first pass (no `executed_queries` yet), it calls `art.enrich_search_queries()`, which is an LLM call that expands the raw topic into up to `enrich_query_count` (default 6) related search queries, always prefixing the literal topic string itself (article.py:990-1028). On any subsequent pass reached via the "enrich" edge, if `extra_queries` (the user-supplied additions from the context-review interrupt) is non-empty, the node skips the LLM entirely and treats `extras` as the full query list for this round (article_graph.py:107-109) — this is a deliberate cost-control decision: re-expanding the whole topic on every enrich cycle would be wasteful and would likely re-suggest queries already tried.

Inside `node_vector_search_merge`, an additional guard filters `pending = [q for q in queries if q and q not in executed]` (article_graph.py:138), so even if the same query string appeared twice across enrichment rounds, it is only ever searched once per graph run. Each successfully searched query is appended to `executed_queries`, which persists in graph state across the whole run (including subsequent enrich loops), giving the context-review interrupt payload an accurate "here's everything we already searched" list for the user to see before deciding whether to add more queries (article_graph.py:254-256, cli.py:1546-1548).

This rule matters for cost and latency control: without it, a user who calls "enrich" three times in a row could trigger three full LLM query-expansions plus redundant vector searches against the same terms — with it, each enrich round strictly adds incremental search coverage.

**Rule workflow**:
```
first pass: no executed_queries -> LLM enrich_search_queries() -> queries (deduped, topic-prefixed)
enrich loop N>1: executed_queries present AND extra_queries non-empty
    -> skip LLM, queries = extra_queries only
vector_search_merge: pending = queries - executed_queries (set difference)
    -> search only `pending`, append each to executed_queries
```

---

### Business Rule: Score-Based Note Merge with a One-Time MOC Boost

**Overview**:
As multiple search queries accumulate hits across the enrich loop, notes are deduplicated by `note_id` keeping the best score seen, capped to a configured maximum pool size; separately, notes linked from a matching topic MOC are boosted to a maximal score exactly once per run.

**Detailed description**:
`art.merge_retrieved_notes()` (article.py:960-975) is the core accumulation function: it treats the existing pool as a `dict[note_id -> serialized hit]`, and for each new hit, keeps whichever of the old/new score is higher (`d["score"] >= float(prev.get("score") or 0.0)`), never averaging or summing scores across queries. The merged pool is then re-sorted descending by score and truncated to `art_cfg.max_context_notes` (default 24) — meaning a note that scored well on an early query but poorly on later ones can still fall out of the pool if enough *other* higher-scoring notes accumulate from subsequent queries, even though the note itself was never re-scored downward.

Separately, `node_vector_search_merge` performs a one-time MOC (Map of Content) lookup: `if not moc_ids: moc = rt.db.find_moc_by_topic(state["topic"])` (article_graph.py:164-165). This check is gated on `moc_ids` being empty, meaning it only runs on the very first search round of a given graph run (once `moc_ids` is populated it never re-triggers, even across enrich loops). If a MOC matching the topic exists, every note wikilinked from its body is force-boosted: `art._merge_moc_notes()` sets any already-present note's score to `max(existing, 1.0)`, and synthesizes a fresh `RetrievedNote` with `score=1.0` for any MOC-linked note not otherwise found by search (article.py:503-531) — 1.0 is effectively the highest possible RRF-fused score, guaranteeing these notes dominate the top of the merged pool regardless of how they scored on lexical/vector relevance. This is a deliberate curation signal: a human (or the gardener pipeline) having already organized notes under a topical MOC is treated as stronger evidence of topical relevance than ad-hoc query matching.

The extra graph-hop expansion (`art_cfg.max_hops > gcfg.max_hops`) is applied after the MOC boost and works similarly — it adds neighbors not already present in the pool, assigns them the graph-walk's computed weight as their score, tags them with `hop`/`via`/a fixed `floor_reason` of "vizinho de grafo (article max_hops)", and re-sorts/truncates the combined pool once more (article_graph.py:174-210).

**Rule workflow**:
```
for each pending query:
    hits = retriever.search_notes(query)
    existing = merge_retrieved_notes(existing, hits, max_context_notes)  # keep max score per note_id

if moc_ids empty (first round only):
    moc = find_moc_by_topic(topic)
    if moc: boost every MOC-linked note to score >= 1.0; re-merge, re-cap

if use_graph and article.max_hops > global.max_hops:
    expand_notes() from hop-0 seeds; add unseen neighbors with graph-weight score
    re-sort all notes by score desc; cap at max_context_notes
```

---

### Business Rule: Outline Sanitization Against the Retrieved-Note Universe

**Overview**:
The LLM-generated `ArticleOutline` is never trusted as-is: every note_id and asset_id it references is validated against what was actually retrieved, with deterministic fallbacks for empty/invalid sections so drafting never crashes on a hallucinated reference.

**Detailed description**:
`art._sanitize_outline()` (article.py:690-724) is invoked unconditionally at the end of `generate_outline()`, immediately after parsing the LLM's JSON response, before the outline is ever handed to a human for review or to the drafting nodes. For each section (truncated to at most `max_sections`, default 8), it filters `sec.note_ids` down to only IDs present in `catalog.notes` and `sec.figure_asset_ids` down to only IDs present in `catalog.assets` (capped at 2 figures per section) — this guards against the well-known LLM failure mode of inventing plausible-looking IDs or citing notes from a different retrieval round. If filtering leaves a section with zero note_ids, it is *not* dropped; instead it is silently backfilled with the first 3 notes in catalog iteration order (`list(catalog.notes.keys())[:3]`), so every section is guaranteed to have at least some evidentiary grounding, even if it's not the grounding the LLM originally intended for that specific section.

If sanitization leaves the outline with zero valid sections at all (e.g., the LLM returned an empty `sections` list, or every section had unparseable content), a single synthetic fallback section titled "Desenvolvimento" ("Development") is created with a generic goal ("Sintetizar as ideias principais do acervo sobre o tema") and up to 5 catalog notes (article.py:710-718). The outline's `title` also falls back to the raw `catalog.topic` string if the LLM left it blank, and `thesis`/`style_notes` are simply `.strip()`-ed without a fallback (an empty thesis is legal and handled downstream by `assemble_article`, which simply omits the thesis paragraph when blank — article.py:342-349).

This sanitization step runs on *every* call to `generate_outline()`, including on the "regenerate" loop-back from outline review — so user feedback fed back into the LLM (`outline_feedback` -> the prompt's `{feedback}` slot) still produces a sanitized outline each time, and a user cannot get stuck reviewing a broken/hallucinated outline no matter how many regeneration cycles occur.

**Rule workflow**:
```
LLM outline JSON -> ArticleOutline.model_validate()
for each section (up to max_sections):
    note_ids = filter(sec.note_ids, in catalog.notes)
    if note_ids empty: note_ids = first 3 catalog notes
    figure_ids = filter(sec.figure_asset_ids, in catalog.assets)[:2]
if zero sections survived: sections = [synthetic "Desenvolvimento" section, first 5 notes]
title = outline.title or catalog.topic
```

---

### Business Rule: Bounded Judge/Redraft Loop with Threshold-Forced Verdict

**Overview**:
After personality rewriting, an LLM "judge" scores the article on four axes; a REJECTED verdict triggers a full redraft of every section (not incremental patching), bounded by `max_judge_iterations`, with the loop's numeric threshold enforced deterministically regardless of what the LLM's own verdict field says.

**Detailed description**:
`art.judge_article_body()` (article.py:1113-1161) sends the full styled article body plus the notes catalog to the `article_judge.md` prompt, which asks for four 0-10 scores (fidelity, coverage, references, naturalness) plus a self-reported `average` and `verdict`. The function does not trust the LLM's verdict in isolation: it recomputes `average` as the arithmetic mean of the four sub-scores whenever the LLM omits it, and — critically — it *overrides* `verdict` to `"REJECTED"` whenever `average < art_cfg.judge_min_score` (default 7.0), even if the LLM explicitly said "APPROVED" (article.py:1146-1148). If the LLM returns some other malformed verdict string, it's normalized by the same threshold comparison. This makes `judge_min_score` a hard deterministic gate rather than an LLM-advisory signal — the LLM's `verdict` field can only make the outcome stricter than its own scores would imply, never looser.

`node_judge` in article_graph.py wraps this: when `state.get("skip_judge")` is set (via `--skip-judge` or implicitly by `--outline-only`), it bypasses the LLM entirely and injects a synthetic `{"verdict": "APPROVED", "average": 10.0, "feedback": "judge skipped"}` (article_graph.py:426-434) — so downstream code (frontmatter stamping, warning logic) always has a well-formed scores dict to read, whether or not judging actually ran. On a real REJECTED verdict, `iteration_count` is incremented and `judge_feedback` (the LLM's own textual critique) is carried forward into `node_draft_sections`'s next invocation, where it's injected into the `{judge_feedback}` template slot of the section-writing prompt (article.py:228, 251) — meaning the redraft doesn't just re-run drafting from scratch, it re-runs it *informed* by exactly what the judge said was wrong.

`route_after_judge` (article_graph.py:451-461) is the loop-bound enforcement: it finishes immediately on any non-REJECTED verdict or when judging was skipped; otherwise it compares `iteration_count` against `max_judge_iterations` (default 3, configurable per-call and via `ArticleConfig.max_judge_iterations`) and only loops back to `draft_sections` while under budget. Once the budget is exhausted, the graph still reaches `finish` (not `abort`) — the article is saved/returned with whatever the last draft was, but `node_finish` appends an explicit warning ("Judge nao aprovou apos max_judge_iterations; salvando melhor rascunho disponivel") and stamps the failing `judge_average`/`judge_verdict` into frontmatter so the discrepancy is visible to the reader/reviewer rather than silently hidden (article_graph.py:469-477, 483-487).

Note that every redraft re-runs `assemble` and `personality` too (the edge is `draft_sections -> assemble -> personality -> judge`), so a redraft is a full re-pass through citation extraction, figure renumbering, and personality rewriting — not merely a re-invocation of the section-writer in isolation. This means personality-rewrite LLM calls (when personality != "neutral") are repeated on every judge iteration, which is a real, uncached-unless-content-identical cost multiplier worth noting for cost-sensitive callers.

**Rule workflow**:
```
judge_article_body(): scores = LLM(4 axes) 
    average = LLM.average or mean(4 axes)
    verdict = "REJECTED" if average < judge_min_score else (LLM.verdict if valid else threshold-derived)
route_after_judge:
    skip_judge -> finish
    verdict != REJECTED -> finish
    iteration_count < max_judge_iterations -> redraft (draft_sections, feedback carried forward)
    else -> finish_with_warning (best available draft kept, warning + failing scores recorded)
```

---

### Business Rule: Citation Extraction and Style-Specific Bibliography Assembly

**Overview**:
Each drafted section is expected to embed a machine-readable `<!-- cites: @Citekey -->` comment naming the sources it drew on; `assemble_article` parses and strips these, resolves them against the catalog, and produces a bibliography whose format and fallback behavior differ by writing style (blog vs. academic).

**Detailed description**:
The section-writer prompts (`article_section_blog.md` / `article_section_academic.md`, selected by `catalog.style` in `draft_sections()`) instruct the LLM to append a trailing HTML comment listing the citekeys used in that section. `assemble_article()` extracts this via `_CITES_COMMENT` regex, splits on commas, and looks each token up (with or without a leading `@`) against a `citekey_to_source` map built from `catalog.sources` (article.py:287-306). A resolved citekey is added to the running `cited_source_ids` list (order-preserving, deduplicated); an unresolved one produces a warning ("Citekey desconhecida na secao N") but does not stop assembly — the article is still produced, just flagged. For academic style specifically, a second, independent citation-discovery pass (`_match_parenthetical_sources`) scans the visible section text for ABNT-style parenthetical citations (either an exact match of the source's formatted `in_text_cite`, or a looser surname+year regex) and adds any additional matches to `cited_source_ids` (article.py:308-312, 894-910) — this catches cases where the LLM cited a source in-text without also emitting the machine-readable comment.

The bibliography section itself branches entirely on `catalog.style`. Blog style produces an informal "Para saber mais" list using only `author_natural`/title/year for each cited source; if `cited_source_ids` ended up empty (no comments were ever successfully parsed), it falls back to listing *every* source reachable from any catalog note (`_unique_sources_from_notes`), and — notably — back-fills `cited_source_ids` with those fallback sources too, so the frontmatter's citation record stays consistent with what's actually printed (article.py:366-373). Academic style instead produces an ABNT-formatted "Referencias" list sorted alphabetically by the pre-rendered `abnt_reference` string (falling back to a minimal `Author. Title. Year.` construction when that field is missing), and — unlike blog style — does *not* silently backfill from all catalog sources when nothing was cited; instead it prints "(Nenhuma referencia citada.)" and raises an explicit warning ("Nenhuma referencia ABNT resolvida...") so a genuinely uncited academic article is visibly flagged rather than papered over (article.py:375-399).

Independently of style, figure embeds (`![[90_Assets/...]]`) appearing anywhere in section text are deduplicated and renumbered sequentially across the whole assembled document via `_WIKI_EMBED_ANY.sub(_renumber_fig, text)`, with academic style additionally appending a "Figura N — description" caption plus a "Fonte: adaptado de ..." attribution line when the asset's source is known (article.py:314-334). Finally, a "## Origem no vault" provenance appendix listing every catalog note's wikilink is always appended regardless of style, giving a full-transparency trail back to the source notes independent of what got formally cited (article.py:402-406).

**Rule workflow**:
```
for each section body:
    extract <!-- cites: ... --> -> resolve against catalog.sources -> append to cited_source_ids
    (unresolved citekey -> warning, does not fail)
    if academic: additionally scan parenthetical (SURNAME, YEAR) text -> add matches
    renumber figure embeds sequentially; academic adds captions+attribution

if style == blog:
    bibliography = cited sources (natural-author format)
    if none cited: fallback = ALL sources from catalog notes; also backfill cited_source_ids
else (academic):
    bibliography = cited sources sorted by ABNT reference (alphabetical)
    if none cited: print placeholder + emit warning (no silent backfill)

always append "Origem no vault" section listing every catalog note
```

---

### Business Rule: Deterministic LLM Response Caching Across the Article Pipeline

**Overview**:
Every LLM call made anywhere in the article pipeline (query enrichment, outline generation, section drafting, personality rewrite, judging) is content-addressed and cached in SQLite's `llm_cache` table, so identical inputs (same prompt template, filled content, model, temperature, and language) never re-invoke the LLM or incur cost on a repeated run.

**Detailed description**:
`art._cached_llm()` (article.py:795-845) is the single choke point every node-level helper (`enrich_search_queries`, `generate_outline`, `draft_sections`, `apply_personality_rewrite`, `judge_article_body`) routes through. It computes a `call_checksum` from `compute_llm_call_checksum(prompt_hash, filled_hash, model, temperature, language)`, where `prompt_hash` is a SHA-256 of the raw prompt *template* text and `filled_hash` is a SHA-256 of the normalized, fully-filled system+user text (via `normalize_text_for_hash`, shared with the rest of the codebase's hashing layer per CLAUDE.md). If `db.get_cached_llm_response(call_checksum)` returns a hit, the function returns immediately with `llm_called=False` and records a `$0` cache-hit event on the active `CostTracker` (`record_cache_hit`) — no network call, no `get_llm()` construction even occurs on the hit path. On a miss, it constructs the LLM client, calls it, persists the raw system/user payload plus the response into `llm_cache`, and returns `llm_called=True`.

This has a direct, observable effect on the judge/redraft loop and on the graph's own `llm_called` bookkeeping: because `_mark_llm()` in article_graph.py only sets `rt.llm_called = True` when the underlying helper reports `called=True` (article_graph.py:92-96), a fully cache-hit run (e.g., replaying the exact same topic/style/personality with an unchanged vault) can complete an entire article generation — including a full judge-approved pass — while `ArticleResult.llm_called` remains `False` end-to-end, which is the signal the CLI/tests use to distinguish "genuinely generated content" from "served entirely from cache." This caching is orthogonal to (and does not interact with) the graph's own state/checkpointing — `MemorySaver` only persists *graph state* for the duration of one run's interrupt/resume cycle, while `llm_cache` persists *LLM responses* indefinitely in `state.db`, surviving across process restarts and separate `zettel article` invocations entirely.

**Rule workflow**:
```
_cached_llm(prompt_template, system, user, model, temperature, language):
    checksum = hash(sha256(prompt_template), sha256(normalize(system+user)), model, temp, language)
    cached = db.get_cached_llm_response(checksum)
    if cached: record_cache_hit(); return (cached_text, called=False)
    else: response = call_llm(...); db.cache_llm_response(checksum, ...); return (response, called=True)
```

---

## 4. Component Structure

`article_graph.py` is a single flat module (no sub-package). Its internal organization by responsibility:

```
zettel/article_graph.py
├── Module docstring / imports          # LangGraph (StateGraph, interrupt, Command, MemorySaver),
│                                          zettel.article (as `art`), zettel.graph (as `note_graph`),
│                                          zettel.retrieval.Retriever, zettel.schemas.ArticleOutline
├── ArticleGraphState (TypedDict)        # lines 35-74 — the full graph state schema (total=False)
├── ArticleRuntime (dataclass)           # lines 77-85  — non-serializable run context (cfg/db/idx/
│                                                          catalog/callbacks/llm_called), threaded via
│                                                          RunnableConfig["configurable"]["runtime"]
├── _rt() / _mark_llm()                  # lines 88-96  — runtime accessor + llm_called bookkeeping helper
├── ── Nodes ──                          # lines 99-510 — one function per graph node (13 total):
│   ├── node_query_enricher              # LLM query expansion (or extras-only on loop-back)
│   ├── node_vector_search_merge         # hybrid search, merge, MOC boost, extra graph hops
│   ├── node_context_review              # HITL #1 — approve/enrich/abort the retrieved context
│   ├── route_after_context              # conditional-edge router for context_review
│   ├── node_build_catalog               # ArticleCatalog construction + defensive empty-check
│   ├── node_generate_outline            # LLM outline generation (+ sanitization)
│   ├── node_outline_review              # HITL #2 — approve/regenerate/abort the outline
│   ├── route_after_outline              # conditional-edge router for outline_review
│   ├── node_outline_only_finish         # terminal node for `--outline-only` shortcut
│   ├── node_draft_sections              # per-section LLM drafting
│   ├── node_assemble                    # Markdown assembly, citations, bibliography, figures
│   ├── node_personality                 # optional LLM personality rewrite
│   ├── node_judge                       # LLM quality judge (or skip stub)
│   ├── route_after_judge                # conditional-edge router for the judge/redraft loop
│   ├── node_finish                      # terminal success path — verification + frontmatter stamping
│   └── node_abort                       # terminal failure path — no-evidence / user-abort body
├── ── Graph build / run ──              # lines 513-715
│   ├── build_article_graph()            # wires all 13 nodes + edges + 3 conditional-edge maps
│   ├── _result_from_state()             # ArticleGraphState -> art.ArticleResult conversion
│   └── run_article_graph()              # public entry point: compiles+invokes the graph, owns the
│                                           run/cost lifecycle (begin_run/finish_pipeline_run), and
│                                           drives the __interrupt__ resume loop via hitl_handler
```

Closely related files (outside this component, but essential context since `article_graph.py` is pure orchestration over them):

```
zettel/article.py            # Domain logic: ArticleCatalog/Result dataclasses, catalog population,
                              # search-query enrichment, outline generation, section drafting,
                              # assembly, personality rewrite, judge scoring, verification, save.
zettel/retrieval.py          # Retriever.search_notes() — hybrid RRF + relevance floor (consumed by
                              # node_vector_search_merge)
zettel/graph.py              # expand_notes() — BFS graph expansion (consumed for the article's
                              # extra max_hops beyond the global config)
zettel/schemas.py            # ArticleOutline / ArticleOutlineSection Pydantic models (the LLM's
                              # structured output contract for the outline step)
zettel/config.py             # ArticleConfig (topk, max_context_notes, max_hops, max_sections,
                              # max_figures, judge_min_score, max_judge_iterations, temperatures, ...)
zettel/cli.py (article cmd)  # The only production caller of run_article_graph(); supplies the
                              # interactive Rich-based hitl_handler
prompts/article_*.md         # The 6 LLM prompt templates driving each LLM-calling node
config/personalities.yaml    # Personality profiles consumed by apply_personality_rewrite()
```

## 5. Dependency Analysis

```
Internal Dependencies:

article_graph.run_article_graph()
    -> article_graph.build_article_graph()  (constructs the StateGraph, no external calls)
    -> zettel.usage.begin_run / finish_pipeline_run   (cost tracking lifecycle)
    -> StateDB.start_run("article")                    (run bookkeeping row)
    -> langgraph MemorySaver checkpointer               (in-process interrupt/resume state)

article_graph.node_query_enricher
    -> zettel.article.enrich_search_queries()
        -> zettel.llm.{load_prompt_parts, fill_template, get_llm, call_llm, extract_json, clip_text}
        -> zettel.article._cached_llm() -> zettel.hashing.{compute_llm_call_checksum, sha256_hex,
                                             normalize_text_for_hash}
        -> StateDB.get_cached_llm_response / cache_llm_response

article_graph.node_vector_search_merge
    -> zettel.retrieval.Retriever(cfg, db, idx).search_notes()   (hybrid RRF + relevance floor)
    -> zettel.article.{merge_retrieved_notes, dict_to_retrieved_note, _merge_moc_notes}
    -> StateDB.find_moc_by_topic / get_note / get_connections_for_notes (via expand_notes)
    -> zettel.graph.expand_notes()   (extra graph hops beyond global config)

article_graph.node_context_review / node_outline_review
    -> langgraph.types.interrupt() / Command(resume=...)   (native HITL mechanism)
    -> ArticleRuntime.context_callback / outline_callback   (test/programmatic bypass)

article_graph.node_build_catalog
    -> zettel.article.catalog_from_retrieved() -> zettel.article._populate_catalog()
        -> StateDB.{get_note, get_source, get_assets_for_source}

article_graph.node_generate_outline
    -> zettel.article.generate_outline() -> zettel.article._sanitize_outline()
        -> zettel.schemas.ArticleOutline / ArticleOutlineSection (Pydantic validation)

article_graph.node_draft_sections
    -> zettel.article.draft_sections() -> zettel.article._pack_section()

article_graph.node_assemble
    -> zettel.article.assemble_article()
        -> zettel.bibliography.{display_author_natural, format_abnt_in_text}
        -> zettel.vault.{_slug, permanent_wikilink, render_frontmatter}

article_graph.node_personality
    -> zettel.article.apply_personality_rewrite() -> zettel.article.load_personalities()
        (reads config/personalities.yaml via PyYAML)

article_graph.node_judge
    -> zettel.article.judge_article_body()

article_graph.node_finish
    -> zettel.article.verify_article()

article_graph._result_from_state
    -> zettel.article.ArticleResult (dataclass construction only)

External Dependencies:
- langgraph (StateGraph, START/END, add_conditional_edges, interrupt, Command, MemorySaver checkpointer)
- langchain_core.runnables.RunnableConfig (typed config passthrough carrying the ArticleRuntime)
- PyYAML (indirectly, via zettel.article.load_personalities for config/personalities.yaml)
- SQLite (state.db) — via StateDB: sources/notes/assets/mocs/note_connections/llm_cache/runs tables
- ChromaDB — indirectly via VectorIndex, consumed inside Retriever.search_notes()
- LLM provider (OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible, per zettel.llm.get_llm) — invoked
  by every _cached_llm() cache-miss across query enrichment, outline, drafting, personality, judge
```

Note: `article_graph.py` itself imports no database or vector-store client directly — every I/O dependency is mediated through `zettel.article`, `zettel.retrieval.Retriever`, `zettel.graph.expand_notes`, or the `StateDB`/`VectorIndex` instances handed to it by the caller (`cli.py`) and stored on `ArticleRuntime`. This is a clean inversion-of-control boundary: the orchestration layer never constructs its own infrastructure clients.

## 6. Afferent and Efferent Coupling

Analysis unit: top-level functions/classes within `article_graph.py` (Python module — no classes beyond the two data containers `ArticleGraphState`/`ArticleRuntime`, so coupling is measured per function). "Afferent" = number of distinct call sites within the module (or the compiled graph edges) that depend on it; "Efferent" = number of distinct external symbols/modules it calls into.

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|-------------------|----------|
| `run_article_graph()` | 3 (cli.py, article.py's `run_article`, tests) | 7 (build_article_graph, usage.begin_run/finish_pipeline_run, StateDB.start_run, MemorySaver, uuid, `_result_from_state`, langgraph Command/graph.invoke) | High |
| `build_article_graph()` | 2 (run_article_graph, tests monkeypatch it directly) | 14 (all 13 node functions + StateGraph/START/END) | High |
| `node_vector_search_merge` | 1 (graph edge from query_enricher) | 6 (Retriever, art.merge_retrieved_notes, art.dict_to_retrieved_note, art._merge_moc_notes, note_graph.expand_notes, StateDB via rt.db) | High |
| `node_context_review` | 1 (graph edge) + 2 downstream routes depend on its output shape | 3 (interrupt, context_callback, route_after_context reads its output) | High |
| `route_after_context` | 1 (conditional edge registration) | 0 (pure function of state) | Medium |
| `node_generate_outline` | 2 (graph edges: from build_catalog, and self-loop from outline_review "regenerate") | 2 (art.generate_outline, ArticleOutline) | Medium |
| `node_outline_review` | 1 (graph edge) | 3 (interrupt, outline_callback, ArticleOutline.model_validate) | High |
| `route_after_outline` | 1 (conditional edge registration) | 0 (pure function of state) | Medium |
| `node_draft_sections` | 2 (graph edges: from outline_review "draft", and self-loop from judge "redraft") | 2 (art.draft_sections, ArticleOutline) | High |
| `node_assemble` | 1 (graph edge) | 2 (art.assemble_article, ArticleOutline) | Medium |
| `node_personality` | 1 (graph edge) | 1 (art.apply_personality_rewrite) | Low |
| `node_judge` | 1 (graph edge) | 1 (art.judge_article_body) | Medium |
| `route_after_judge` | 1 (conditional edge registration) | 0 (pure function of state) | High (loop-bound correctness) |
| `node_finish` | 1 (graph edge, both "finish" and "finish_with_warning" targets) | 1 (art.verify_article) | Medium |
| `node_abort` | 1 (2 conditional-edge targets route here: context "end", outline "end") | 1 (art._NO_EVIDENCE constant) | Low |
| `node_outline_only_finish` | 1 (conditional edge target) | 1 (art.format_outline_for_display) | Low |
| `ArticleGraphState` (TypedDict) | 13 (every node reads/returns dict shapes matching it) | 0 | High (schema-central — any field rename ripples through all 13 nodes) |
| `ArticleRuntime` (dataclass) | 13 (every node calls `_rt(config)` to fetch it) | 3 (holds refs to AppConfig, StateDB, VectorIndex) | High |
| `_mark_llm()` | 5 (query_enricher, generate_outline, draft_sections, personality, judge) | 0 | Medium |

Interpretation: `ArticleGraphState` and `ArticleRuntime` are the two highest-afferent-coupling elements — every node depends on their shape — making them the component's true "load-bearing" contracts; a change to either requires auditing all 13 node functions. `route_after_judge` is flagged High-critical despite modest coupling counts because it is the sole guarantor that the judge/redraft cycle terminates (an off-by-one or missing `skip_judge` check here would produce an infinite loop). `build_article_graph()` and `run_article_graph()` are the natural integration seams (also the ones tests monkeypatch directly, per `test_hitl_handler_receives_interrupt_payload`).

## 7. Integration Points

This component exposes no network-facing endpoints (no REST/GraphQL/gRPC) — it is invoked in-process, either from `zettel/cli.py`'s `article` Typer command or programmatically via `zettel.article.run_article()` (a thin re-export wrapper that just forwards to `run_article_graph`). No section 7 (Endpoints) applies, consistent with the report format's instruction to omit that section when a component exposes none.

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|----------------|
| LLM provider (via `zettel.llm.get_llm`/`call_llm`) | External Service | Query enrichment, outline generation, section drafting, personality rewrite, quality judging | Provider SDK (OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible, via LangChain clients) | JSON-structured prompts/responses (parsed with `extract_json`) | No explicit retry/circuit-breaker in this component; a raw provider exception propagates up through `run_article_graph()` — the `finally: finish_pipeline_run()` still records partial cost/usage before the exception surfaces to the CLI |
| `state.db` (SQLite, via `StateDB`) | Internal Datastore | Notes/sources/assets/MOC/connection lookups, LLM response cache, run cost bookkeeping | Direct SQLite calls (no ORM) | Row dicts / JSON-serialized fields | Cache misses transparently fall through to a live LLM call; no exception handling for DB errors within this component (propagates to caller) |
| ChromaDB (via `VectorIndex`, indirectly through `Retriever`) | Internal Datastore | Dense vector similarity search feeding hybrid retrieval | Chroma client API | Embeddings + metadata dicts | Delegated entirely to `Retriever`/`VectorIndex`; not handled in this component |
| LangGraph runtime (`interrupt()`/`Command(resume=...)`) | In-process Framework | Human-in-the-loop suspension/resumption of the graph across context and outline review | Python function call / exception-based control flow | `dict` payload (interrupt) / `dict` resume value | Malformed resume payloads are defensively defaulted to "approve" at both node level and in the `run_article_graph` resume loop |
| `config/personalities.yaml` | Local Config File | Personality profile lookup (name/temperature/style_prompt) for the rewrite step | Filesystem read + PyYAML parse | YAML | Missing file returns a built-in single "neutral" profile default (`load_personalities`); unknown `personality_id` falls back to the "neutral" profile or a synthetic ad-hoc one built from `custom_style_notes` |
| `prompts/article_*.md` | Local Config File | System/user prompt templates for each LLM-calling node | Filesystem read | Markdown with `<!-- zettel:user -->` splitter | No fallback if a prompt file is missing — `load_prompt_parts` would raise, propagating to the caller (not caught in this component) |
| CLI (`zettel/cli.py` `article` command) | Internal Caller | Sole production entry point; supplies the interactive `hitl_handler` | Direct Python function call | `ArticleGraphState`-shaped kwargs in, `ArticleResult` dataclass out | CLI wraps the call and inspects `result.no_evidence`/`result.aborted`/`result.warnings` post-hoc; no exception handling around `run_article_graph()` itself in cli.py |

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| State Machine / Orchestrator (StateGraph) | `build_article_graph()` — 13 nodes, 3 conditional-edge routers, 2 explicit loop-back edges | article_graph.py:516-567 | Encodes the entire multi-step writing pipeline (including two loops) as declarative graph edges rather than nested `if`/`while` control flow |
| Strategy Pattern (HITL resolution) | Callback vs. `interrupt()` vs. auto-approve, selected per-call via which optional args are supplied | article_graph.py:248-263, 315-334, 639-646 | Lets the same graph serve fully-automated (tests, library callers) and fully-interactive (CLI) execution modes without branching the graph structure itself |
| Command Pattern (LangGraph resume) | `Command(resume=resume_val)` re-invoked against the same compiled graph/thread_id | article_graph.py:706, 711 | Standard LangGraph idiom for resuming a suspended graph with externally-supplied data |
| Dependency Injection via typed config | `ArticleRuntime` stashed in `RunnableConfig["configurable"]["runtime"]`, retrieved by every node via `_rt(config)` | article_graph.py:77-89 | Threads non-serializable dependencies (StateDB, VectorIndex, AppConfig, mutable catalog/callbacks) through LangGraph's config channel, which is the framework's sanctioned side-channel for non-state data |
| Bounded Retry Loop | Judge/redraft cycle capped by `max_judge_iterations`, tracked via `iteration_count` in graph state | article_graph.py:438-461 | Guarantees termination of an LLM-quality-gated generation loop while still allowing several self-correction passes |
| Idempotent / Content-Addressed Caching | `_cached_llm()` keyed on a SHA-256 checksum of prompt+content+model+temperature+language | article.py:795-845 | Makes repeated runs with identical inputs free and deterministic; shared infrastructure with the rest of the pipeline (`hashing.py`'s `compute_llm_call_checksum`) |
| Defensive Sanitization / Fail-Safe Defaults | `_sanitize_outline()`, malformed-interrupt-payload fallback, `no_evidence` short-circuit | article.py:690-724; article_graph.py:258-259, 329-330, 244-245 | Prevents LLM hallucination or malformed external input from crashing or silently corrupting the pipeline; every failure mode degrades to a safe, explainable default rather than an exception |
| Facade / Layered Module Split | `article.py` (domain logic) vs. `article_graph.py` (pure orchestration), explicitly stated in both docstrings | article.py:1-7, article_graph.py:1-5 | Keeps LangGraph-specific plumbing separable from business logic that could in principle be reused without LangGraph (and is, in fact, unit-testable independently — see `test_article.py`) |
| Accumulator Pattern | `retrieved_notes` merged incrementally across enrich-loop iterations, never reset | article_graph.py:134-153, article.py:960-975 | Preserves search investment across HITL enrichment rounds instead of discarding prior results |

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|----------------|-------|--------|
| Medium | `node_outline_review` / `route_after_outline` | The outline "regenerate" loop has no iteration cap (unlike the judge loop's `max_judge_iterations`) — a user (or a misbehaving `outline_callback`) can request "regenerate" indefinitely | Potential unbounded LLM cost accumulation in a programmatic/scripted caller that always returns "regenerate"; no equivalent of `max_judge_iterations` protects this path |
| Medium | `run_article_graph()` interrupt resume loop | The `while result_state.get("__interrupt__")` loop has no maximum iteration count either — if `hitl_handler` (or the CLI's `_hitl`) always returns a resume value that re-triggers another interrupt of the same type, the loop runs indefinitely | Same class of risk as above, but at the framework-resume layer rather than the graph-edge layer |
| Medium | `node_vector_search_merge` | The "extra graph hops" condition (`art_cfg.max_hops > gcfg.max_hops`) silently does nothing when the article-specific config is set *equal to or below* the global setting, even though `ArticleConfig.max_hops` defaults to 2 specifically described as "expansao de grafo mais ampla que o ask" (broader than ask's) — if an operator sets `retrieval.graph_expansion.max_hops` to 2 or higher globally, the article-specific broader expansion silently becomes a no-op with no warning logged | Configuration drift between `ArticleConfig.max_hops` and the global `GraphExpansionConfig.max_hops` can silently disable a documented feature; no log line signals when this branch is skipped |
| Low-Medium | `_cached_llm()` (article.py, but central to every graph node) | Cache key normalizes filled prompt text but does not include the `article.style` or other structural context beyond what's already baked into the filled template — relies entirely on template correctness to avoid cross-style cache collisions | If a future prompt-template change fails to interpolate `style` correctly, a blog-style cached response could theoretically be served for an academic request (currently mitigated because `style` is always part of the filled mapping, but this is an implicit invariant, not an enforced one) |
| Medium | `node_judge` redraft loop | A full redraft re-runs `assemble` and `personality` (LLM call) on every judge rejection cycle, not just `draft_sections` — for a non-"neutral" personality this means the personality-rewrite LLM call is repeated on every iteration even though only the underlying section content changed | Multiplies LLM cost/latency for personality-styled academic/blog articles that fail the judge more than once; no incremental-personality-rewrite optimization exists |
| Low | `node_context_review` / `node_outline_review` | If a supplied callback raises an exception, it propagates uncaught out of the node and, ultimately, out of `graph.invoke()` — there is no try/except wrapping around `rt.context_callback(...)` / `rt.outline_callback(...)` | A buggy caller-supplied callback can crash the entire run mid-pipeline after several LLM calls (and their cost) have already been incurred; `finally: finish_pipeline_run()` in `run_article_graph` does still record partial usage, but the run itself surfaces as an unhandled exception to the CLI |
| Low | `ArticleGraphState` (TypedDict, `total=False`) | Every field is optional and accessed via `state.get(...)` with inline defaults scattered across 13 node functions — there is no single source of truth for a field's default value (e.g., `iteration_count` defaults to `0` in three separate `int(state.get("iteration_count") or 0)` call sites) | Repeated default-value literals risk drifting out of sync if one call site is updated (e.g., a future change to the default judge-iteration count) without updating the others |
| Low | `node_finish` | `catalog` is read from `rt.catalog` (mutable runtime state set by `node_build_catalog`), not from the graph's own `ArticleGraphState` — meaning `node_finish`'s behavior depends on in-process object identity/mutation order rather than purely on graph state | Breaks LangGraph's usual state-purity assumption; would silently misbehave if `ArticleRuntime` were ever made per-node-instantiated rather than shared-by-reference across the whole run (e.g., under a hypothetical distributed/checkpoint-restored execution rather than the current single-process `MemorySaver`) |
| Low | Test coverage gap | No test exercises the "regenerate" outline loop, the "MOC boost" code path (`_merge_moc_notes` via the graph), or the "extra graph hops" branch (`art_cfg.max_hops > gcfg.max_hops`) directly through `run_article_graph` | These branches are covered indirectly at best (via `test_article.py`'s unit tests of `article.py` helpers) but not through the graph's own routing/state-threading, leaving regression risk in the orchestration wiring itself |

## 10. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|--------------------|----------|--------------|
| `article_graph.py` (graph orchestration, all 13 nodes + 3 routers) | 0 dedicated pure-unit tests (no node function is tested standalone) | 3 full-graph tests in `tests/test_article_graph.py` (426 lines total file; graph-specific tests span lines 25-287) | Covers: judge reject-then-approve redraft loop, context-review enrich loop with a real `context_callback`, and HITL-handler interrupt-payload unwrapping (via a monkeypatched `FakeCompiled`/`FakeBuilder`) — moderate happy-path coverage of the two most complex loops; no coverage of `outline_review`'s "regenerate"/"abort" branches, `route_after_context`'s "abort" branch, `node_abort`, `node_outline_only_finish`, or the MOC-boost/extra-graph-hop branches inside `node_vector_search_merge` | Good: tests exercise real `run_article_graph()` end-to-end with monkeypatched `call_llm`/`get_llm` and a real `StateDB`, asserting on actual pipeline outputs (`result.body`, `result.frontmatter["judge_verdict"]`) rather than mocking internals — gives genuine confidence in the wiring. The HITL test specifically guards a real historical bug shape ("must unwrap `__interrupt__[0].value` before calling `hitl_handler`"), indicating regression-driven test authorship. Gap: no assertions on `result.warnings` content for the judge-exhausted-iterations path, and no negative/failure-path tests (LLM raising, malformed JSON from the LLM, callback raising) |
| `article.py` (domain helpers consumed by every graph node) | Extensive — `tests/test_article.py` is 426 lines covering `format_abnt_in_text`, `assemble_article` (citations, figures, bibliography for both styles), `catalog_from_retrieved`, `verify_article`, `save_article_note`, `retrieved_note_to_dict`, and `run_article` (the thin wrapper) | Indirect, via the same file's fixtures (`seeded` — a real `StateDB` with source/notes/assets) | High for the pure-function domain logic (assembly, verification, catalog building) that every graph node ultimately calls into; this is where most of the business-rule correctness is actually pinned down | Good: uses real `StateDB` fixtures rather than mocks for catalog/asset resolution, and asserts on precise output strings (e.g., exact ABNT citation formatting) — high-value regression protection for the business rules documented in Section 3 |
| Judge threshold-forcing logic (`judge_article_body`'s `average < judge_min_score -> REJECTED` override) | Indirectly exercised only through the graph-level `test_graph_judge_reject_then_approve` test (which supplies pre-scored REJECTED/APPROVED JSON directly, never exercising the override arithmetic itself) | — | Low — no test supplies a scenario where the LLM's own `verdict` says "APPROVED" but the numeric average is below `judge_min_score` (the exact override behavior documented in Section 3's judge business rule) | Gap: this specific deterministic-override behavior, which the codebase clearly intends as a hard guarantee, has no direct unit test isolating it |
| Outline sanitization (`_sanitize_outline`) | Not directly unit-tested in either test file found (no test constructs an `ArticleOutline` with unknown note_ids/asset_ids or zero sections to verify the fallback behavior) | Indirectly plausible via graph tests supplying valid outlines only | Low | Gap: the "unknown ID gets dropped" and "empty outline gets a synthetic Desenvolvimento section" fallbacks are undocumented-by-test business rules; a regression here would not be caught by the current suite |
| CLI `article` command (`zettel/cli.py:1467`) | No `tests/test_cli.py` reference to the `article` subcommand was found in this analysis's search scope | — | None found | Risk: the interactive `_hitl()` Rich-prompt payload shapes (context_review/outline_review dict contracts) are only implicitly validated by the graph tests' synthetic payloads, not by any CLI-level test invoking the actual Typer command |

Test file locations (relative to repo root):
- `tests/test_article_graph.py` — graph-orchestration tests (primary test surface for this component)
- `tests/test_article.py` — domain-helper tests for the sibling `zettel/article.py` module, which every graph node depends on
- No other test file in the repository (outside `.venv`) references `article_graph` or `run_article_graph` (confirmed via repository-wide search excluding ignored folders)

---

**Report metadata**: Component analyzed: `article_graph` (`zettel/article_graph.py`). Analysis scope: entire project root, excluding `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, `.pytest_cache`. No project files were modified during this analysis.
