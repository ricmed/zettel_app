# Component Deep Analysis Report — `article` (zettel/article.py)

## 1. Executive Summary

`zettel/article.py` is the **domain/helper layer** for the `zettel article "tema" --style blog|academic` CLI command — Zettel's long-form writing feature, as opposed to `ask.py`'s short Q&A. It does **not** contain the orchestration control flow itself (that is a LangGraph `StateGraph` in the sibling module `zettel/article_graph.py`, explicitly out of scope for this report per the analysis request); instead it supplies every stateful domain object and every pure/LLM-calling step that the graph's nodes invoke: building an evidence catalog from retrieved notes, expanding search queries, generating and sanitizing an outline, drafting sections, assembling them into a final Markdown document with citations/figures/references, rewriting tone via "personality" profiles, judging quality, verifying the result deterministically, and persisting it to the vault.

Key findings:

- **Two parallel entry points exist into the same graph.** `article.py:run_article()` (lines 137-177) is a thin wrapper around `article_graph.run_article_graph()` using callback-style HITL (`approve_outline`, `context_callback`). However, `zettel/cli.py`'s `article` command (the only production caller) imports and calls `run_article_graph` **directly**, bypassing `run_article` entirely and using the alternate `hitl_handler` parameter with LangGraph `interrupt()`. `run_article` is therefore exercised only by `tests/test_article.py`, never by the shipped CLI — a latent dual-API technical debt (see §10).
- The component enforces **evidence-grounding discipline** end-to-end: outlines may only reference `note_id`/`asset_id` values that exist in the catalog (`_sanitize_outline`), sections must self-report which sources they actually cited via a `<!-- cites: ... -->` machine-readable comment that is stripped before publication, and a final `verify_article` pass flags orphaned embeds/citations without ever raising.
- **Style is a first-class branching concern** everywhere: blog vs. academic changes citation mechanics (light narrative mention vs. ABNT parenthetical/reference list), figure caption formatting, and even which prompt file is loaded for section drafting (`article_section_blog.md` vs `article_section_academic.md`).
- All LLM calls in this component funnel through a single private helper, `_cached_llm`, which enforces a **deterministic SQLite response cache** keyed on prompt+input+model+temperature+language — identical to the caching contract used elsewhere in the codebase (extractor, connector, bibliography).
- Article notes are saved directly to `00_Inbox/ART - {timestamp} - {slug}.md` and are **deliberately never indexed** into ChromaDB/SQLite (confirmed: no other module references the `ART - ` filename prefix), making them a terminal, non-recycled artifact of the pipeline.
- The `article` command is **not exposed in the web UI** (confirmed no references in `web.py`/`web_app.py`) — it is CLI-only, consistent with its heavy Rich-console HITL design.

## 2. Data Flow Analysis

`article.py` does not itself sequence these steps (that is `article_graph.py`'s job) but each step below is a function in this component, called by a graph node of the same conceptual purpose:

```
1.  CLI `zettel article "tema"` (cli.py:1467) parses --style/--topk/--personality/etc.
2.  run_article_graph() (article_graph.py) builds initial graph state and invokes the StateGraph
3.  Node "query_enricher" -> article.enrich_search_queries()
       - Loads prompts/article_query_enrich.md, calls LLM via _cached_llm()
       - Merges user extra queries first, then LLM-suggested facets, dedupes, caps at `count`
4.  Node "vector_search_merge" -> Retriever.search_notes() (retrieval.py, external) per query
       - article.merge_retrieved_notes() folds hits into a running pool, keeping best score per note_id
       - article._merge_moc_notes() boosts notes linked from a matching MOC (found via db.find_moc_by_topic)
       - Optional extra graph.expand_notes() hop when article.max_hops > global graph_expansion.max_hops
5.  Node "context_review" -> HITL (approve / abort / enrich-with-extra-queries); article.parse_extra_queries()
       parses free-text extra queries typed by the user (CLI: rich Prompt.ask)
6.  Node "build_catalog" -> article.catalog_from_retrieved() -> article._populate_catalog()
       - Resolves each hit's source_id/body/title from StateDB, pulls asset embeds
         (article._assets_from_note_body()), truncates note bodies to max_chars_per_note,
         caps assets to max_figures by cross-note frequency
7.  Node "generate_outline" -> article.generate_outline()
       - Formats catalog as Markdown (article._format_notes_for_outline())
       - Calls LLM (prompts/article_outline.md) -> ArticleOutline (schemas.py)
       - article._sanitize_outline() drops unknown note_ids/asset_ids, enforces max_sections,
         guarantees at least one fallback section if the LLM returns none
8.  Node "outline_review" -> HITL (approve / regenerate-with-feedback / abort)
9.  Node "draft_sections" -> article.draft_sections()
       - Per section: article._pack_section() packs evidence/sources/figures text blocks
       - Loads prompts/article_section_blog.md or article_section_academic.md by style
       - Calls LLM per section (writer_temperature), collects raw Markdown + note_ids used
10. Node "assemble" -> article.assemble_article()
       - Strips <!-- cites: ... --> comments, resolves them to source_ids
       - Renumbers figure embeds, injects captions (style-dependent), builds
         "Para saber mais" (blog) or "Referencias" (academic, sorted ABNT strings) sections,
         appends "Origem no vault" provenance list of every catalog note
11. Node "personality" -> article.apply_personality_rewrite()
       - Loads config/personalities.yaml profile; no-op (no LLM call) if profile is
         "neutral" and no custom style notes were supplied
12. Node "judge" -> article.judge_article_body()
       - Calls LLM (prompts/article_judge.md); computes/validates average of 4 scores;
         forces REJECTED if average < judge_min_score regardless of LLM's own verdict field
       - route_after_judge (article_graph.py) loops back to draft_sections with judge_feedback
         up to max_judge_iterations, else finishes with a warning
13. Node "finish" -> article.verify_article() deterministic post-hoc checks (never raises)
14. CLI renders result, optionally prompts to save -> article.save_article_note()
       - build_article_note() fills default frontmatter (type/origin/topic/style/title/llm_model)
       - Writes 00_Inbox/ART - {YYYYmmdd-HHMMSS} - {slug}.md (render_frontmatter + body)
       - No Chroma/SQLite indexing — terminal artifact
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Validation | `--style` must be `blog` or `academic`, else CLI exits 1 | cli.py:1512-1515 |
| Grounding | Outline may only use `note_id`/`figure_asset_ids` present in the catalog | article.py:690-724 (`_sanitize_outline`) |
| Grounding | If an outline section resolves to zero known note_ids, fall back to top-3 catalog notes | article.py:697-700 |
| Grounding | If the LLM returns zero sections, synthesize one fallback "Desenvolvimento" section from up to 5 catalog notes | article.py:710-718 |
| Limit | Outline capped at `article.max_sections` (default 8) sections | article.py:696, config.py:191 |
| Limit | At most 2 figures per section in outline, and at most 2 resolved per drafted section | article.py:701, 776 |
| Limit | At most `article.max_figures` (default 6) distinct assets survive catalog population, ranked by cross-note frequency | article.py:604-612, config.py:192 |
| Limit | Each catalog note body truncated to `article.max_chars_per_note` (default 1200) chars with `...` ellipsis | article.py:585-587, config.py:189 |
| Limit | Note summary (used in outline prompt) truncated to 200 chars | article.py:582-583 |
| Style branching | Blog sections use narrative "mencao leve" (light mention); academic sections use ABNT parenthetical citation | article.py:70-82 (CatalogSource properties), prompts/article_section_blog.md vs article_section_academic.md |
| Style branching | Assembly appends "## Para saber mais" (blog, informal reading list) or "## Referencias" (academic, sorted ABNT strings) | article.py:354-399 |
| Style branching | Figure captions: academic gets numbered "**Figura N** — desc" + "Fonte: adaptado de ..."; blog gets italic "*Figura: desc*" or nothing | article.py:315-334 |
| Citation extraction | Sections self-report cited sources via a trailing `<!-- cites: @Citekey1,@Citekey2 -->` comment, stripped from the published body | article.py:298-299, 865-872 |
| Citation extraction (academic-only) | Additionally scans prose for parenthetical `(SURNAME[, et al.], YEAR)` patterns matched against catalog authors/year as a second-chance citation harvester | article.py:308-312, 894-910 |
| Fallback reference | Blog reading list falls back to *all* catalog sources (not just cited ones) if no `<!-- cites -->` were found anywhere | article.py:366-373 |
| Fallback reference | Academic reference with a missing/empty `abnt_reference` falls back to a minimal "{author}. {title}. {year|s.d.}." string | article.py:383-389 |
| Warning | Academic articles that resolve zero references append the warning "Nenhuma referencia ABNT resolvida..." | article.py:397-399 |
| Warning | Any section that ends up empty after cleaning triggers "Secao vazia: {heading}" | article.py:337-338 |
| Warning | Unknown citekey in a `<!-- cites: -->` comment triggers "Citekey desconhecida na secao N: {cid}" | article.py:305-306 |
| Verification | `verify_article` flags any `![[...]]` embed whose target file does not exist in the vault | article.py:432-437 |
| Verification | `verify_article` (academic only) flags any `(SURNAME..., YEAR)`-shaped parenthetical that doesn't match any catalog author surname — "possibly orphaned citation" | article.py:439-444 |
| Verification | Empty body or body identical to the canonical "no evidence" string is flagged and short-circuits further checks | article.py:428-430 |
| No-evidence gate | If retrieval returns zero notes, the graph aborts and returns the canonical PT-BR "Nao encontrei evidencia suficiente..." message — the LLM is never called for drafting | article.py:36-39; article_graph.py:231, 244-245, 496-501 |
| Personality no-op | `personality_id == "neutral"` **and** no custom style notes supplied skips the rewrite LLM call entirely (cost/latency optimization) | article.py:1082-1087 |
| Personality fallback | Unknown personality id falls back to the `neutral` profile, or a synthetic ad-hoc profile built from `custom_style_notes` if even `neutral` is missing from the YAML | article.py:1089-1094 |
| Judge scoring | `average` is server-recomputed as the arithmetic mean of the 4 sub-scores when the LLM doesn't supply one; **the LLM's own `average` is trusted only if present**, but the verdict is always forced to `REJECTED` if `average < judge_min_score` (default 7.0), overriding an `APPROVED` claim from the LLM | article.py:1141-1152 |
| Judge iteration | Rejected drafts loop back to `draft_sections` with the judge's feedback injected into the next attempt, up to `max_judge_iterations` (default 3); after that, the last draft is kept and a warning is appended instead of blocking | article_graph.py:451-461, 469-477 |
| Determinism / cost | Every LLM call in this component is content-addressed and cached in SQLite (`llm_cache`); an identical (prompt, filled text, model, temperature, language) tuple never re-calls the LLM | article.py:795-845 (`_cached_llm`) |
| Query enrichment | User-supplied "extra queries" always take priority (searched first, included verbatim) over LLM-suggested facets, and the original topic string is always force-inserted as a query if missing | article.py:1022-1028 |
| Query enrichment | Result list is capped at `max(count, len(extras) + 1)` — user extras are never truncated away even if they exceed the configured target count | article.py:1028 |
| Persistence | Saved article filenames are always `ART - {YYYYmmdd-HHMMSS} - {slug}.md` under `00_Inbox/`, with slug derived from title (fallback to topic, fallback to literal "artigo") | article.py:461-477 |
| Non-indexing | Articles are pure filesystem artifacts — never embedded into ChromaDB nor tracked in the SQLite `notes` table, unlike every other note type in the vault | article.py (absence of any index/db writes in `save_article_note`) |

### Detailed breakdown of the business rules

---

### Business Rule: Outline Grounding & Sanitization (`_sanitize_outline`)

**Overview**:
The LLM that plans the article's outline is instructed (via `prompts/article_outline.md`) to reference only `note_id`s and `asset_id`s that exist in the evidence catalog it was shown, and never to invent facts, sources, or IDs. Because LLM structured output cannot be trusted to obey free-text instructions perfectly, `_sanitize_outline` (article.py:690-724) re-validates the LLM's `ArticleOutline` against the actual `ArticleCatalog` before it is allowed to drive drafting.

**Detailed description**:
For every section the LLM proposed, the function intersects `sec.note_ids` with the known catalog note IDs (`known = set(catalog.notes.keys())`). Any hallucinated or stale ID is silently dropped rather than causing a hard failure — this keeps the pipeline resilient to a wide class of LLM structured-output drift without ever surfacing a raw exception to the user. If a section is left with *zero* valid notes after filtering, the function does not omit the section; instead it substitutes the first three catalog notes as a best-effort fallback, on the theory that some evidence (even if not the LLM's original picks) is safer for downstream section drafting than an empty evidence pack (which would itself trigger the "Secao vazia" warning downstream). The same filtering happens for `figure_asset_ids`, additionally hard-capped to 2 per section regardless of how many the LLM proposed (mirroring the prompt's own instruction, but enforced in code rather than trusted from the model).

At the outline level, `sections[:max_sections]` truncates any excess sections the LLM generated beyond the configured ceiling (default 8), and each section's `heading`/`goal` are defensively `.strip()`-ed with fallback placeholder text ("Secao", empty string) if the LLM left them blank. If the *entire* outline comes back with no sections at all (a degenerate LLM response), the function fabricates a single "Desenvolvimento" section pulling in up to 5 catalog notes, guaranteeing that `draft_sections` always has at least one section to iterate over — this is the last line of defense before the graph would otherwise crash on an empty `outline.sections` list.

**Rule workflow**:
```
LLM outline JSON
  -> ArticleOutline.model_validate() (pydantic type/shape check only)
  -> _sanitize_outline(outline, catalog, max_sections)
       for each section (up to max_sections):
         note_ids = intersect(section.note_ids, catalog.notes.keys())
         if empty: note_ids = first 3 catalog notes
         fig_ids  = intersect(section.figure_asset_ids, catalog.assets.keys())[:2]
         heading/goal defaulted if blank
       if no sections survived: synthesize 1 fallback section (<=5 notes)
  -> sanitized ArticleOutline returned to graph state
```

---

### Business Rule: Citation Mechanics Are Style-Dependent (Blog "Light Mention" vs. Academic ABNT)

**Overview**:
The component maintains two mutually exclusive citation regimes selected by the `style` parameter (`blog` | `academic`), enforced both in the prompts sent to the writer LLM and in the deterministic assembly/verification code that processes the LLM's output afterward.

**Detailed description**:
For `blog` style, `CatalogSource.light_mention` (article.py:78-82) produces phrasing like `"Alessandro Negro em *Knowledge Graphs and LLMs in Action*"` — a natural-language attribution with no parenthetical citation apparatus. The blog section prompt (`prompts/article_section_blog.md`) explicitly forbids `(SOBRENOME, ano)` formal citations and wikilinks in the body. For `academic` style, `CatalogSource.in_text_cite` (article.py:70-72, delegating to `bibliography.format_abnt_in_text`) produces ABNT NBR 10520 parenthetical citations such as `(NEGRO et al., 2026)`, and the academic section prompt instructs the model to use these exact forms. This means the same `CatalogSource` object exposes both formatting strategies as properties, and the correct one is selected implicitly by which prompt template was loaded — the domain object itself is style-agnostic, but the prompt contract and the assembly-time regex matching (`_match_parenthetical_sources`, academic-only) are not.

This split continues into `assemble_article`: blog articles get a "## Para saber mais" section with an informal bulleted reading list (`{author}. *{title}* ({year}).`), while academic articles get a "## Referencias" section built from each cited source's pre-computed `abnt_reference` string (populated at harvest time in `bibliography.py`, a separate component), sorted alphabetically by `.upper()` for ABNT-compliant ordering. If a cited academic source lacks a stored `abnt_reference` (e.g., incomplete bibliographic metadata upstream), assembly falls back to a minimal hand-built reference rather than omitting the citation outright — trading ABNT strictness for completeness. This is a deliberate degrade-gracefully choice: an incomplete reference is judged less harmful than a citation with no corresponding reference entry (which `verify_article`'s orphan-citation check would otherwise flag).

The distinction also governs `verify_article`'s post-hoc checks: only academic articles are scanned for "possibly orphaned" parenthetical citations (a citation-shaped string in the body that doesn't match any catalog author's surname), since blog articles by construction never emit that citation shape.

**Rule workflow**:
```
style == "academic":
  prompt = article_section_academic.md
  in-text: CatalogSource.in_text_cite -> "(SURNAME[, et al.], YEAR)"
  assembly: "## Referencias" from cited_source_ids, using abnt_reference
            (fallback minimal string if abnt_reference empty), sorted uppercase
  verify:   flag parenthetical citations with no matching catalog surname

style == "blog":
  prompt = article_section_blog.md
  in-text: CatalogSource.light_mention -> "{author} em *{title}*"
  assembly: "## Para saber mais" bulleted list; falls back to ALL catalog
            sources if no <!-- cites --> comments were captured anywhere
  verify:   no parenthetical-citation orphan check (not applicable to blog)
```

---

### Business Rule: `<!-- cites: ... -->` Machine-Readable Citation Contract

**Overview**:
Rather than trying to parse free-form prose to determine which sources a drafted section actually used, the writer prompts (both styles) require the LLM to append a hidden HTML comment listing the citekeys/source_ids it drew on. `assemble_article` treats this as the single source of truth for attribution bookkeeping.

**Detailed description**:
`_extract_cites_comment` (article.py:865-872) uses the regex `_CITES_COMMENT` to find a `<!-- cites: ... -->` line (case-insensitive, tolerant of an empty payload meaning "cited nothing"), splits the comma-separated payload, and strips whitespace. `assemble_article` extracts this comment from each raw section body *before* renumbering figures or doing anything else, then removes the comment from the visible text (`_CITES_COMMENT.sub("", text)`) so it never leaks into the published article. Each extracted citekey is resolved against a lookup table (`citekey_to_source`) built to accept either the bare citekey or the full `@citekey` `source_id` form — a defensive normalization against the LLM's inconsistent use of the `@` prefix (the prompts show `@Citekey1,@Citekey2` but nothing prevents a bare-citekey response). An ID the assembler cannot resolve produces a "Citekey desconhecida" warning rather than a hard error, and is silently dropped from `cited_source_ids`.

This contract is deliberately narrow: it is the *only* mechanism by which `cited_source_ids` is populated for blog articles, meaning a blog section that forgets or malforms its `<!-- cites -->` line will simply not credit its source in the "Para saber mais" list (mitigated by the catalog-wide fallback described above). For academic articles, the parenthetical-citation regex scan (`_match_parenthetical_sources`) is a *second, independent* attempt to recover citations the model expressed in-prose but forgot to declare in the comment — providing redundancy specifically for the stricter academic use case where a missing reference is more consequential (triggers an explicit warning) than in the blog case.

**Rule workflow**:
```
for each drafted section body:
  cite_ids = regex-extract <!-- cites: id1,id2 --> (may be empty)
  strip the comment from the visible body
  for each cite_id:
    resolve against citekey_to_source (tries as-is, then lstrip('@'))
    if resolved and not already in cited_source_ids: append
    if unresolved: warnings.append("Citekey desconhecida ...")
  if style == academic:
    also regex-scan prose for (SURNAME[...], YEAR) and cross-reference
    catalog authors/year -> append any additional matches
```

---

### Business Rule: Figure Embed Renumbering & Style-Dependent Captioning

**Overview**:
Any Obsidian embed (`![[90_Assets/...]]`) that survives from the drafted sections into the assembled article is deduplicated, sequentially renumbered, and captioned differently depending on style — turning raw LLM-emitted embeds into a coherent, non-repeating figure sequence.

**Detailed description**:
`assemble_article` walks every section body with `_WIKI_EMBED_ANY.sub(_renumber_fig, text)`, a regex substitution whose callback (`_renumber_fig`, article.py:315-334) closes over a `figure_counter` and a `seen_figures` set shared across the whole assembly pass (not per-section) — this is what guarantees a figure referenced by two different sections (e.g., because two sections used the same source note) is captioned only once, at its first occurrence, and re-emitted unchanged (no duplicate numbering) on subsequent references. The asset's `description` metadata is looked up via `_asset_by_path` against the `ArticleCatalog.assets` map populated earlier by `_populate_catalog`/`_assets_from_note_body`.

For academic style, the caption is `"**Figura {N}** — {description}"` plus, when the asset carries a known `source_id`, a second line `"Fonte: adaptado de {source.title or source.source_id}."` — mimicking the academic convention of crediting figure provenance explicitly. For blog style, only an italicized one-line caption (`*Figura: {description}*`) is added, and only if a description exists at all; blog figures with no description get no caption line, keeping the prose lighter. After the substitution pass, if a `vault_path` was supplied, every embed path collected in `seen_figures` is checked for existence on disk and a "Figura ausente no vault" warning is raised for any that don't resolve — this is a deterministic, non-LLM safety net against the writer LLM inventing or misremembering an asset path.

**Rule workflow**:
```
figure_counter = 0; seen_figures = {}
for each section (in order):
  for each ![[path]] embed found in section text:
    if path already in seen_figures: leave unchanged (already captioned)
    else:
      figure_counter += 1; seen_figures.add(path)
      lookup asset by path in catalog.assets
      if style == academic: append "**Figura N** — desc" (+ "Fonte: ..." if source known)
      elif desc present:    append "*Figura: desc*"
after assembly, if vault_path given:
  for each path in seen_figures: warn if file missing on disk
```

---

### Business Rule: Judge Verdict Is Server-Enforced, Never Fully Trusted from the LLM

**Overview**:
`judge_article_body` calls an LLM judge that scores the drafted article on 4 axes (fidelity, coverage, references, naturalness) and proposes its own `verdict`, but the function never lets a lenient LLM self-approval bypass the configured quality bar.

**Detailed description**:
The judge prompt (`prompts/article_judge.md`) asks the LLM to compute `average` as the arithmetic mean of the four 0-10 sub-scores and to emit `verdict: APPROVED|REJECTED` itself. `judge_article_body` (article.py:1113-1161) parses this but treats the LLM's `average` as optional — if absent, it is recomputed server-side from the four scores, closing a gap where a model that forgets the `average` field would otherwise silently pass validation with a default of `0`. More importantly, regardless of what `verdict` the LLM returned, the function *overrides* it to `"REJECTED"` whenever `average < art_cfg.judge_min_score` (default `7.0`), and only falls back to trusting an ambiguous/malformed verdict string (anything other than the two accepted literals) by re-deriving it from the same threshold. This means the single config knob `judge_min_score` is the true, code-enforced quality gate — the LLM's own verdict field is effectively advisory except when it agrees with the threshold-derived one.

This score is then consumed by `article_graph.py`'s `route_after_judge`, which loops the whole draft/assemble/personality/judge cycle again (carrying the judge's `feedback` text into the next `draft_sections` call as `judge_feedback`) for up to `max_judge_iterations` attempts (default 3, overridable per-call and via `--max-judge-iterations`). If the article still doesn't clear the bar after the iteration budget, the pipeline does **not** discard the work — it keeps the last drafted body and appends an explicit warning ("Judge nao aprovou apos max_judge_iterations...") to the saved note's frontmatter/warnings, prioritizing forward progress over strict gatekeeping. `--skip-judge` (and implicitly `--outline-only`) bypasses this entire mechanism with a synthetic `APPROVED`/`10.0` score, skipping the LLM call altogether.

**Rule workflow**:
```
scores = LLM judge call (fidelity, coverage, references, naturalness, [average], [verdict], [feedback])
average = scores.average if provided else mean(4 sub-scores)
if average < judge_min_score: verdict = REJECTED   # overrides LLM's own verdict
elif verdict not in {APPROVED, REJECTED}: verdict = APPROVED if average >= judge_min_score else REJECTED
return {..., verdict, feedback}

route_after_judge:
  if skip_judge: -> finish
  if verdict == REJECTED and iteration_count < max_judge_iterations:
     iteration_count += 1; judge_feedback = scores.feedback -> redraft (loop to draft_sections)
  else: -> finish (with a warning appended if it exhausted iterations while still REJECTED)
```

---

### Business Rule: Personality Rewrite Is a Conditional No-Op

**Overview**:
`apply_personality_rewrite` applies a configurable tone/voice rewrite to the assembled article body as a final LLM pass, but explicitly skips the LLM call entirely for the common case of no requested personalization — an intentional cost and latency optimization, not merely a caching hit.

**Detailed description**:
The function resolves the effective personality id from the explicit `personality_id` argument, falling back to `art_cfg.default_personality` (config default `"neutral"`), then to the hardcoded literal `"neutral"`. If the resolved id is exactly `"neutral"` **and** no `custom_style_notes` free-text override was supplied, the function returns the input body completely unchanged and reports `llm_called=False` — this is checked *before* even loading `config/personalities.yaml` from disk, so a neutral run incurs no I/O or LLM cost at all beyond the check itself. This matters because `apply_personality_rewrite` runs on *every* successful judge iteration in the graph (it sits between `assemble` and `judge` in the node chain), so without this short-circuit every redraft cycle would pay for a rewrite LLM call even when the user asked for no stylistic transformation.

When a rewrite is actually needed, `load_personalities` reads `config/personalities.yaml` (path configurable via `art_cfg.personalities_path`) and looks up the requested profile; an unknown id falls back to the file's own `neutral` entry, and if even that key is absent from the YAML (a malformed/edited file), the function fabricates an ad-hoc profile using the raw `custom_style_notes` text as the style prompt with a default temperature of `0.7` — ensuring the pipeline can still produce *some* output rather than crashing on a config file that doesn't match the expected schema. Each profile supplies its own `temperature`, decoupling voice from the deterministic drafting temperature used for `draft_sections`.

**Rule workflow**:
```
pid = personality_id or art_cfg.default_personality or "neutral"
notes = custom_style_notes.strip()
if pid == "neutral" and not notes:
    return (body, llm_called=False)          # short-circuit, no I/O
profiles = load_personalities(personalities_path)   # yaml.safe_load, cached per call only
profile = profiles.get(pid) or profiles.get("neutral") or {ad-hoc profile from notes}
call LLM (prompts/article_personality.md) at profile.temperature
return (rewritten_body, llm_called=True)
```

---

### Business Rule: No-Evidence Gate Short-Circuits LLM Usage

**Overview**:
If the hybrid/graph retrieval pipeline returns zero notes for the topic (across all enriched queries and any MOC boost), the article pipeline aborts immediately after the search step and never calls the outline, drafting, personality, or judge LLMs at all.

**Detailed description**:
`node_vector_search_merge` in `article_graph.py` sets `no_evidence = not existing` once all search queries have been executed and merged (article.py's `merge_retrieved_notes` is what performs that merge). `node_context_review` checks this flag first, before even consulting the HITL callback or the `skip_context_review` flag, and unconditionally routes to `abort`. `node_abort` then returns the module-level constant `_NO_EVIDENCE` (article.py:36-39) — a fixed PT-BR sentence stating no sufficient evidence was found — as the `final_body`, with `no_evidence=True` and `aborted=True` on the result. The CLI (`cli.py:1604-1607`) special-cases this by printing the message in a "Sem evidencia" panel and exiting cleanly (exit code 0, not an error), rather than attempting to save an empty/placeholder article note. This is the article-generation analogue of the `ask` command's identical no-evidence philosophy described in project-level documentation (`retrieval.py`'s absolute relevance floor): the system prefers an honest "nothing found" over fabricating content from a near-empty context window.

**Rule workflow**:
```
after all search queries run + MOC boost + optional extra graph hop:
  no_evidence = (merged retrieved_notes pool is empty)
context_review node:
  if no_evidence: -> abort node (skips outline/draft/personality/judge entirely)
abort node:
  final_body = _NO_EVIDENCE constant string
  no_evidence = True; aborted = True
CLI:
  if result.no_evidence: print panel, db.close(), exit(0)  # no save prompt offered
```

---

## 4. Component Structure

```
zettel/
├── article.py                       # THIS COMPONENT — domain helpers/orchestration API
│   ├── Data structures
│   │   ├── CatalogAsset              # asset_id/path/description/source_id
│   │   ├── CatalogSource             # bibliographic record + ABNT/light-mention properties
│   │   ├── CatalogNote                # retrieved-note projection used as evidence
│   │   ├── ArticleCatalog             # aggregate: notes + sources + assets + moc_ids + retrieval_params
│   │   └── ArticleResult              # final output: body/frontmatter/outline/warnings/ids/flags
│   ├── Public API
│   │   ├── run_article()              # thin wrapper -> article_graph.run_article_graph (test-only path)
│   │   ├── generate_outline()         # LLM call -> ArticleOutline, sanitized
│   │   ├── draft_sections()           # per-section LLM drafting loop
│   │   ├── assemble_article()         # merge drafts -> final Markdown + citations/figures/refs
│   │   ├── verify_article()           # deterministic post-hoc checks (never raises)
│   │   ├── build_article_note()       # frontmatter defaults
│   │   ├── save_article_note()        # writes 00_Inbox/ART - ...md
│   │   ├── format_outline_for_display()
│   │   ├── retrieved_note_to_dict() / dict_to_retrieved_note()   # graph-state (de)serialization
│   │   ├── merge_retrieved_notes()    # score-keeping merge across search rounds
│   │   ├── parse_extra_queries()      # free-text HITL input -> query list
│   │   ├── enrich_search_queries()    # LLM query expansion
│   │   ├── catalog_from_retrieved()   # builds ArticleCatalog from accumulated hits
│   │   ├── load_personalities()       # YAML profile loader
│   │   ├── apply_personality_rewrite()# conditional LLM tone rewrite
│   │   └── judge_article_body()       # LLM quality judge + server-side verdict enforcement
│   └── Internals (prefixed `_`)
│       ├── _wiki_link / _origin_label / _merge_moc_notes
│       ├── _populate_catalog / _assets_from_note_body
│       ├── _format_notes_for_outline / _sanitize_outline / _pack_section
│       ├── _cached_llm                # single LLM-call chokepoint w/ SQLite cache
│       ├── _format_outline_preview
│       ├── _extract_cites_comment / _asset_by_path / _unique_sources_from_notes
│       └── _match_parenthetical_sources / _parenthetical_matches_catalog
│
├── article_graph.py                  # LangGraph StateGraph orchestrator (separate component,
│                                      #   out of scope here) — imports this module as `art` and
│                                      #   calls its functions from graph nodes
├── cli.py                            # `article` Typer command (lines 1466-1651) — production
│                                      #   entry point; calls article_graph.run_article_graph
│                                      #   directly (bypasses article.run_article)
├── bibliography.py                   # External dependency: format_abnt_in_text/display_author_natural
├── retrieval.py                      # External dependency: Retriever, RetrievedNote, NoteSearchResult
├── graph.py                          # External dependency: expand_notes (graph hop expansion)
├── vault.py                          # External dependency: _slug, permanent_wikilink, render_frontmatter
├── llm.py                            # External dependency: call_llm/get_llm/fill_template/etc.
├── hashing.py                        # External dependency: compute_llm_call_checksum, sha256_hex
├── schemas.py                        # External dependency: ArticleOutline, ArticleOutlineSection
└── usage.py                          # External dependency: record_cache_hit (cost/telemetry)

config/
└── personalities.yaml                # Personality profiles: neutral, geek_philosopher,
                                       #   serious_academic, clear_teacher

prompts/
├── article_query_enrich.md
├── article_outline.md
├── article_section_blog.md
├── article_section_academic.md
├── article_anti_ai.md                # shared anti-robotic-prose instructions block
├── article_personality.md
└── article_judge.md

tests/
├── test_article.py                   # direct unit tests of this component
└── test_article_graph.py             # integration tests exercising this component via the graph
```

## 5. Dependency Analysis

```
Internal Dependencies (within zettel/):
cli.py (article command)
  -> article.parse_extra_queries, article.save_article_note   (direct imports)
  -> article_graph.run_article_graph                           (bypasses article.run_article)

article.run_article()  -> article_graph.run_article_graph()    (lazy import; test-only call path)

article_graph.py (node functions)
  -> article.enrich_search_queries, merge_retrieved_notes, dict_to_retrieved_note,
     _merge_moc_notes (private, `# noqa: SLF001`), catalog_from_retrieved,
     generate_outline, format_outline_for_display, draft_sections, assemble_article,
     apply_personality_rewrite, judge_article_body, verify_article,
     ArticleCatalog, ArticleResult, ArticleStyle, ApproveOutlineFn, clip_text (re-export),
     _NO_EVIDENCE (private, `# noqa: SLF001`)
  -> graph.expand_notes (graph hop expansion)
  -> retrieval.Retriever (hybrid search)

article.py itself
  -> bibliography.{display_author_natural, format_abnt_in_text}  (ABNT/author formatting)
  -> hashing.{compute_llm_call_checksum, normalize_text_for_hash, sha256_hex}  (LLM cache keys)
  -> llm.{call_llm, clip_text, extract_json, fill_template, get_llm, load_prompt_parts}
  -> retrieval.RetrievedNote  (type only, for (de)serialization helpers)
  -> schemas.{ArticleOutline, ArticleOutlineSection}  (pydantic structured LLM output)
  -> vault.{_slug, permanent_wikilink, render_frontmatter}  (filenames / wikilinks / frontmatter)
  -> usage.record_cache_hit  (lazy import inside _cached_llm, cost telemetry)
  -> StateDB (TYPE_CHECKING only at module top; concrete calls: get_note, get_source,
     get_assets_for_source, get_cached_llm_response, cache_llm_response, find_moc_by_topic)
  -> VectorIndex (TYPE_CHECKING only; not actually called anywhere in article.py — see §10)
  -> AppConfig (TYPE_CHECKING only; concrete access: cfg.retrieval.article.*, cfg.language,
     cfg.llm.*, cfg.prompts_path, cfg.vault_path)

External Dependencies:
- LangGraph (langgraph.checkpoint.memory.MemorySaver, langgraph.graph.StateGraph, langgraph.types.interrupt/Command)
  - Used by article_graph.py only, not by article.py directly, but defines this component's runtime contract
- LangChain (via llm.py's ChatOpenAI/ChatAnthropic/ChatGoogleGenerativeAI wrappers) - LLM provider abstraction
- PyYAML (yaml.safe_load) - config/personalities.yaml parsing (article.py:1057, 1067)
- Pydantic v2 - ArticleOutline/ArticleOutlineSection structured validation
- SQLite (via StateDB) - llm_cache table for deterministic response caching; notes/sources/assets/mocs lookups
- Rich (via cli.py, not article.py) - Table/Panel/Prompt for HITL rendering
- Filesystem (pathlib) - vault asset existence checks, article .md persistence
```

## 6. Afferent and Efferent Coupling

Unit of analysis: the public functions/dataclasses of `article.py` (Python module-level functions/classes, since this is not class-based OOP for the pipeline logic itself).

| Component | Afferent Coupling (called by) | Efferent Coupling (calls out to) | Critical |
|-----------|-------------------------------|-----------------------------------|----------|
| `_cached_llm` | 6 (generate_outline, draft_sections, enrich_search_queries, apply_personality_rewrite, judge_article_body — all internal) | 5 (hashing x3, llm.get_llm, llm.call_llm, StateDB cache get/set, usage.record_cache_hit) | High — single chokepoint for every LLM call; a bug here silently breaks caching/cost tracking for the whole component |
| `assemble_article` | 1 (article_graph.node_assemble) + tests | ~6 (_extract_cites_comment, _match_parenthetical_sources, _asset_by_path, _unique_sources_from_notes, Path.exists, CatalogSource properties) | High — sole place citation/figure/reference business rules converge |
| `_populate_catalog` | 1 (catalog_from_retrieved) | 3 (StateDB.get_note/get_source/get_assets_for_source, _assets_from_note_body) | High — every downstream note/source/figure fact originates here |
| `catalog_from_retrieved` | 1 (article_graph.node_build_catalog) + tests | 2 (dict_to_retrieved_note, _populate_catalog) | Medium |
| `generate_outline` | 1 (article_graph.node_generate_outline) | 4 (_format_notes_for_outline, llm.load_prompt_parts/fill_template, _cached_llm, _sanitize_outline) | High — gatekeeps everything drafting depends on |
| `_sanitize_outline` | 1 (generate_outline) | 0 (pure function over catalog + outline) | High — sole grounding safety net for outline hallucination |
| `draft_sections` | 1 (article_graph.node_draft_sections) | 2 (_pack_section, _cached_llm) | High — the most expensive/most-called node (N sections x M judge iterations) |
| `_pack_section` | 1 (draft_sections) | 0 (pure function over catalog + section) | Medium |
| `judge_article_body` | 1 (article_graph.node_judge) | 1 (_cached_llm) + _format_notes_for_outline | High — gates the redraft loop |
| `apply_personality_rewrite` | 1 (article_graph.node_personality) | 2 (load_personalities, _cached_llm conditionally) | Medium |
| `verify_article` | 1 (article_graph.node_finish) | 0 (pure, filesystem read-only for existence checks) | Medium — last safety net, never raises |
| `enrich_search_queries` | 1 (article_graph.node_query_enricher) | 1 (_cached_llm) | Medium |
| `merge_retrieved_notes` | 1 (article_graph.node_vector_search_merge) + tests | 2 (retrieved_note_to_dict internal loop) | Medium |
| `_merge_moc_notes` | 1 (article_graph.node_vector_search_merge, via `art._merge_moc_notes` private access) | 1 (StateDB.get_note) | Low-Medium — private API consumed cross-module (coupling smell, see §10) |
| `save_article_note` / `build_article_note` | 1 (cli.py) + tests | 2 (vault._slug, vault.render_frontmatter) | Medium — sole persistence path, no rollback if partial write |
| `run_article` | 0 in production (tests only) | 1 (article_graph.run_article_graph) | Low (production) / Medium (test surface) — see §10 dead-API finding |
| `CatalogSource` (dataclass) | Constructed by `_populate_catalog`; properties read by `_format_notes_for_outline`, `_pack_section`, `assemble_article` | 1 (bibliography.format_abnt_in_text via `.in_text_cite`) | High — central data carrier for all citation logic |
| `ArticleCatalog` (dataclass) | Threaded through nearly every function in this file plus `article_graph.ArticleRuntime.catalog` | 0 (pure container) | High — de facto shared mutable state across the whole pipeline run |

## 7. Endpoints

Not applicable — `article.py` exposes no network endpoints (REST/GraphQL/gRPC). It is invoked exclusively as an in-process Python API by `article_graph.py` and, indirectly, by the `zettel article` Typer CLI command (`cli.py:1467`). It is explicitly **not** wired into the FastAPI web UI (`web.py`/`web_app.py` — no references found).

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| StateDB (state.py, SQLite) | Internal Service | Note/source/asset lookups, MOC-by-topic search, LLM response cache read/write | In-process function calls | Python dicts / SQLite rows | No component-level try/except; a DB error propagates uncaught up through the graph node into `run_article_graph`'s bare `graph.invoke()` |
| LLM Provider (via `llm.get_llm`/`call_llm`) | External Service | Query enrichment, outline generation, section drafting, personality rewrite, quality judging | Provider SDK (OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible, per `llm.py`) | JSON-structured text (parsed via `extract_json` + `json.loads`) | No retry/backoff *within* article.py itself (delegated to `llm.call_llm`); a malformed JSON response raises `json.JSONDecodeError`/pydantic `ValidationError` uncaught — no try/except around `generate_outline`/`judge_article_body`'s `json.loads(extract_json(raw))` calls |
| SQLite `llm_cache` table | Internal (via StateDB) | Deterministic response cache keyed by `compute_llm_call_checksum` | In-process | JSON blob (system+user) + raw response text | Cache miss silently falls through to a live LLM call; no cache corruption handling beyond StateDB's own |
| Filesystem (vault) | Internal | Read: asset existence checks (`verify_article`, `assemble_article`); Write: final article `.md` | Direct file I/O (`pathlib.Path`) | Markdown + YAML frontmatter | `save_article_note` does not catch write failures (permission errors, disk full propagate); `verify_article`/figure checks are best-effort (missing files produce warnings, not exceptions) |
| `config/personalities.yaml` | Internal Config | Personality/tone profiles for rewrite step | Filesystem read | YAML | Missing file returns a hardcoded single "neutral" profile (article.py:1059-1066); malformed YAML would raise from `yaml.safe_load` uncaught |
| Prompt files (`prompts/article_*.md`) | Internal Config | System/user prompt templates per pipeline step | Filesystem read | Markdown with `<!-- zettel:user -->` split marker | Missing prompt file raises `FileNotFoundError` from `load_prompt_parts` uncaught (except `article_anti_ai.md`, which is explicitly guarded with `.exists()` and degrades to an empty string) |
| Retriever (retrieval.py) | Internal Service | Hybrid vector+BM25 note search feeding the evidence catalog | In-process (not called directly by article.py — called by article_graph.py, article.py only consumes its `RetrievedNote`/`NoteSearchResult` types) | Python dataclasses | N/A at this layer |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Facade / Domain Service Layer | `article.py` exposes a flat set of pure(-ish) functions that `article_graph.py` composes into graph nodes | article.py (whole module) | Keeps LangGraph orchestration concerns (state, interrupts, routing) separate from domain logic (catalog building, citation rules, assembly), so the domain logic is independently unit-testable without a graph runtime |
| Data Transfer Object / Aggregate | `ArticleCatalog`, `CatalogNote`, `CatalogSource`, `CatalogAsset` dataclasses | article.py:52-108 | Single in-memory evidence pack threaded through outline/draft/assemble/judge/verify, decoupling "what evidence exists" from "how it's rendered per style" |
| Serialization Bridge (dict <-> dataclass) | `retrieved_note_to_dict` / `dict_to_retrieved_note` | article.py:927-957 | LangGraph state must be JSON-serializable (checkpointed by `MemorySaver`); this pair bridges the rich `RetrievedNote` dataclass and the plain-dict graph state representation |
| Deterministic Cache-Aside | `_cached_llm` computing a checksum before every LLM call and checking `StateDB.get_cached_llm_response` first | article.py:795-845 | Cost control + reproducibility — identical inputs never re-trigger a paid LLM call, and results are reproducible across re-runs (e.g., after a judge-rejection redraft loop with unchanged inputs) |
| Strategy (implicit, via config-driven prompt selection) | `draft_sections` selects `article_section_blog.md` vs `article_section_academic.md` by `catalog.style`; `assemble_article` branches its whole back-half by the same field | article.py:220-224, 354-399 | Encapsulates the blog/academic writing strategies without a class hierarchy — a data value (`style: Literal["blog","academic"]`) selects behavior throughout |
| Graceful Degradation / Fail-Open Sanitization | `_sanitize_outline`, unresolved citekey handling, missing `abnt_reference` fallback, missing personality profile fallback | article.py:690-724, 301-306, 383-389, 1089-1094 | The component consistently prefers "produce something usable + a warning" over raising exceptions, appropriate for a long-running, potentially-iterating (judge loop) creative-writing pipeline |
| Command/Payload pattern for HITL | `run_article`'s `approve_outline: ApproveOutlineFn` callback vs. `article_graph`'s `hitl_handler` + LangGraph `interrupt()`/`Command(resume=...)` | article.py:129-131; article_graph.py (out of scope) | Two coexisting integration styles for pausing the pipeline for human input — see §10 for the resulting duplication risk |
| Single Responsibility per LLM step | Each LLM-calling function (`enrich_search_queries`, `generate_outline`, `draft_sections`, `apply_personality_rewrite`, `judge_article_body`) owns exactly one prompt file and one structured-output contract | article.py (whole module) | Makes each pipeline stage independently swappable/testable and keeps prompt-to-code mapping 1:1 and easy to audit |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| Medium | `run_article()` vs `cli.py` | `run_article` (article.py:137-177) is a production-shaped public API (documented, exported) that is **never called by the shipped CLI** — `cli.py`'s `article` command calls `article_graph.run_article_graph` directly with a different HITL mechanism (`hitl_handler` vs. `approve_outline`/`context_callback`). Only `tests/test_article.py` exercises `run_article`. | Two divergent code paths into the same graph must be kept in sync manually; a future signature change to `run_article_graph` could silently desync `run_article`'s wrapper without any production code catching it — only test breakage would reveal it, and even then only for the parameters tests happen to pass |
| Medium | `_cached_llm` and structured-output parsing | `generate_outline`, `judge_article_body`, and `enrich_search_queries` all call `json.loads(extract_json(raw))` with no try/except; a malformed/non-JSON LLM response (e.g., a provider content-filter refusal, or a truncated response hitting a token limit) raises an unhandled exception all the way up through the LangGraph node, aborting the whole run with a raw traceback rather than a graceful warning/retry | Poor resilience to LLM provider flakiness; user sees a stack trace instead of an actionable message for what is otherwise a heavily fail-open component |
| Low-Medium | `article_graph.py` accessing `article.py` private members | `art._merge_moc_notes` and `art._NO_EVIDENCE` are accessed from `article_graph.py` with explicit `# noqa: SLF001` suppression | Signals the module boundary between "orchestration" and "domain" is not fully clean — these two names are effectively part of the public contract between the modules but are named/marked as private, inviting confusion for future maintainers about what's safe to rename |
| Low-Medium | `VectorIndex` (`idx`) parameter | `run_article`/`run_article_graph` accept and thread through an `idx: VectorIndex` parameter, but nothing in `article.py` itself calls it directly — it is only used inside `article_graph.py`'s `Retriever(cfg, rt.db, rt.idx)` construction. `article.py`'s own `TYPE_CHECKING` import of `VectorIndex` is otherwise unused | Minor: dead type import in this module; the parameter threading is legitimate (owned by the graph), so this is a naming/documentation nit rather than a functional bug |
| Low | `_pack_section` / `_sanitize_outline` fallback overlap | Both `_sanitize_outline` (outline-level) and `_pack_section` (draft-level) independently implement "if note_ids resolve to empty, fall back to first N catalog notes" logic with slightly different N (3 vs 3, but different call sites and different comments — "Redistribute: mini search by heading" in `_pack_section` is aspirational text that doesn't actually search by heading, it just takes the first 3 catalog notes) | The `_pack_section` comment overstates what the code does (no heading-based search exists), which could mislead a future maintainer trying to locate or extend that "mini search" behavior |
| Low | Hardcoded PT-BR strings | `_NO_EVIDENCE`, all warning strings (`"Citekey desconhecida..."`, `"Secao vazia..."`, `"Figura ausente no vault..."`, etc.) are hardcoded Portuguese literals rather than driven by `cfg.language` or an i18n layer, despite `cfg.language` being threaded into every prompt template | Consistent with the project-wide PT-BR-by-default convention noted in project documentation, but means these specific strings would not adapt if `cfg.language` were ever changed away from `pt-BR` — inconsistent with the prompt templates, which do interpolate `{language}` |
| Low | No CLI-level automated test | The Typer `article` command in `cli.py` (argument validation, the `_hitl` Rich-console closure, save-prompt branching) has zero test coverage — there is no `tests/test_cli.py` at all in this project | The HITL wiring and CLI argument validation for `article` (e.g., the `--style` guard, `--save`/`--save-to`/`--no-save-prompt` interplay) is only verified manually; a regression here would not be caught by `pytest` |
| Low | `_populate_catalog` figure-frequency ranking after full ingestion | Assets are only capped to `max_figures` *after* all hits have been processed and every asset row queried via `db.get_assets_for_source` (article.py:604-612) — for a source with many images this does redundant DB work for assets that will be discarded moments later | Minor performance inefficiency, not correctness-affecting; only matters at scale (sources with very large asset counts) |

## 11. Test Coverage Analysis

| Component Area | Unit Tests (test_article.py) | Integration Tests (test_article_graph.py) | Coverage | Test Quality |
|-----------------|-------------------------------|----------------------------------------------|----------|----------------|
| `format_abnt_in_text` / `display_author_natural` (bibliography, re-exercised here) | 1 (`test_format_abnt_in_text_variants`) covering 1/2/3+/paged authors | 0 | Good — covers all author-count branches (1, 2, 3, >3-et al., with/without page) | Precise assertions on exact ABNT string output |
| `catalog_from_retrieved` / `_populate_catalog` | 1 (`test_catalog_from_retrieved_joins_source_and_assets`) | Indirectly via full-run tests | Adequate for the happy path (source+asset join); no direct test for the `max_figures` truncation-by-frequency path or the multi-source dedup path | Good assertions but doesn't exercise the asset-frequency capping branch (article.py:604-612) |
| `merge_retrieved_notes` | 1 (`test_merge_retrieved_notes_keeps_best_score`) | Exercised implicitly across multi-round search in `test_graph_context_enrich_loop` | Good — explicitly verifies "keep best score, keep title from the better hit" | Clear, minimal, well-targeted |
| `apply_personality_rewrite` | 1 (`test_personality_neutral_noop`) — only the no-op path | 0 direct | Gap — no test exercises an actual non-neutral personality rewrite (LLM-called branch), nor the "personality id unknown -> fallback to neutral" branch, nor the "neutral profile missing from YAML -> ad-hoc profile" branch | The one test present is precise but narrow |
| `assemble_article` | 2 (`test_assemble_academic_with_cites_and_figure`, `test_assemble_blog_light_reading_list`) | Indirectly via `test_run_article_full_mock` | Good for the two style happy paths (citation extraction, figure renumbering/captioning, reading-list/references generation) | No direct test for: unknown citekey warning, empty-section warning, missing-abnt-reference fallback, or the academic parenthetical-harvesting path (`_match_parenthetical_sources`) — these are only reachable indirectly |
| `format_outline_for_display` / `ArticleOutline` schema | 1 (`test_outline_schema_and_display`) | Exercised via every full-graph test (outline JSON round-trips) | Adequate | Simple structural assertion |
| `run_article` (no-evidence path) | 1 (`test_run_article_no_evidence`) | Equivalent path (`no_evidence`) not directly retested in test_article_graph.py, but the underlying `no_evidence` routing is graph-owned | Good for the specific no-evidence short-circuit | Mocks `Retriever.search_notes` and `call_llm`/`get_llm` cleanly |
| `run_article` (full happy path, blog) | 1 (`test_run_article_full_mock`) — exercises enrich -> outline -> 2 sections -> assemble -> save, with `skip_judge=True` | N/A (uses `run_article`, not the graph directly) | Good end-to-end coverage of the blog path with `save_article_note` verification | Well-constructed sequential mock (`responses.pop(0)`) simulating the LLM call sequence; does not cover the **academic** style through the full pipeline (only unit-tested at `assemble_article` level), nor a run where the judge is *not* skipped |
| `verify_article` | 1 (`test_verify_missing_embed`) — only the missing-embed case | 0 direct | Gap — the academic "orphaned parenthetical citation" branch of `verify_article` (article.py:439-444) has no dedicated test | Simple, clear assertion |
| `judge_article_body` | 0 direct unit test in test_article.py | Covered via `test_graph_judge_reject_then_approve` in test_article_graph.py (reject-then-approve cycle, verifying `route_after_judge`'s redraft loop) | Adequate via integration test, but the server-side "override LLM's own APPROVED verdict when average < judge_min_score" branch specifically is not isolated in a unit test | Integration test is realistic (drives two full judge cycles) but conflates judge logic with graph routing logic |
| `enrich_search_queries` | 0 direct unit test | Covered via `test_graph_context_enrich_loop` (extra-queries-first ordering, dedup) | Adequate via integration | No isolated test of the `count`/truncation edge case (`ordered[: max(count, len(extras) + 1)]`) |
| `draft_sections` / `_pack_section` | 0 direct unit test of `_pack_section`'s fallback branches (empty note_ids, empty figure_asset_ids redistribution) | Covered only implicitly (sections always have valid note_ids in test fixtures) | Gap — the "Redistribute" fallback path in `_pack_section` (article.py:731-734, 766-774) is never exercised by any test in either file | No test constructs a section with unresolvable `note_ids`/`figure_asset_ids` to verify the fallback behavior |
| `save_article_note` / `build_article_note` | 1 (assertions embedded at the end of `test_run_article_full_mock`) | 0 direct | Adequate for the filename/frontmatter happy path; no test for the `dest` override parameter or for a title/topic that produces an empty slug (`_slug` returning "") | — |
| `cli.py` `article` Typer command | 0 | 0 | **None** — no `tests/test_cli.py` exists in the project at all | This is a project-wide gap, not specific to `article`, but it means the CLI wiring (argument validation, `_hitl` closure, save-prompt flow) for this component is entirely unverified by automated tests |

---

**Report saved to:** `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-article-2026-08-30_10-22-26.md`
