# Component Deep Analysis Report — `prompts`

## 1. Executive Summary

The `prompts` component is a directory of 17 Markdown files (`prompts/*.md`) that hold every LLM prompt template used by the zettel_app pipeline. It is not executable code — it is the **prompt-engineering contract layer** between the deterministic Python pipeline (`harvest → extract → review → connect → garden`, plus `ask`, `article`, and bibliography enrichment) and the LLM providers wired up in `zettel/llm.py`.

Each file follows one structural convention: a stable **system block** (role, rejection/acceptance criteria, output schema, worked examples) followed by a `<!-- zettel:user -->` marker and a **user template** containing `{placeholder}` tokens that are filled per-call from pipeline state (`zettel/llm.py::fill_template`). This split exists purely for **provider prompt-caching**: the system half rarely changes across calls of the same type, so it is sent as `SystemMessage` and, for Anthropic, tagged with `cache_control: {"type": "ephemeral"}` (`zettel/llm.py::apply_prompt_cache_hints`); the user half is sent as `HumanMessage` and always varies.

Key findings:

- All 17 files are loaded exclusively through `zettel.llm.load_prompt_parts()` / `split_prompt_text()`; there is no other code path that reads prompt files directly.
- 12 of the 16 prompts wired into `zettel doctor`'s integrity check map to a corresponding Pydantic schema in `zettel/schemas.py` for strict structured-output validation; the remaining prompts (`ask.md`, `bibliographic_metadata.md`, `image_description.md`, `article_judge.md`, `article_query_enrich.md`, `article_section_*.md`, `article_personality.md`) are parsed with a looser `extract_json()` + manual `dict` access, or (for `ask.md`, `article_section_*.md`, `article_personality.md`) are free-text Markdown with no JSON contract at all.
- **`moc_hub_generation.md` and `moc_hub_incremental.md` are absent from the `zettel doctor` integrity checklist** (`zettel/cli.py:1761-1778`) even though they are actively loaded by `zettel/gardener_hub.py`. This is a real gap: `doctor` can report a healthy install while the hub-MOC prompts are missing or corrupted.
- **`ptbr_guard.md`'s stated output contract does not match how the code uses it.** The prompt's own instructions say "Retorne APENAS o texto corrigido, sem explicações adicionais" (free text), but `zettel/connector.py::_apply_ptbr_guard` feeds it a JSON blob and then does `json.loads(extract_json(corrected_raw))` expecting the same keys back. The prompt file never tells the model to preserve/return JSON structure — the current behavior relies on the model inferring that from the fenced `{text}` payload alone.
- PT-BR is enforced redundantly at three independent layers: (1) instructional — nearly every prompt states "TUDO em PT-BR" or "Responda em `{language}`"; (2) a dedicated corrective prompt (`ptbr_guard.md`) run conditionally over Prompt 2 output only, gated by a hard-coded English-marker heuristic; (3) none of this applies to Prompt 1 (`literature_note.md`) output, which is never run through the guard.
- Structured-output prompts are the majority (10 of 17); free-text prompts are `ask.md`, `image_description.md`, `article_personality.md`, `article_section_blog.md`, `article_section_academic.md`, and `ptbr_guard.md` (nominally).

## 2. Data Flow Analysis

Each prompt is consumed by exactly one pipeline stage, always through the same three-step choreography:

```
1. Pipeline module calls load_prompt_parts(cfg.prompts_path / "<name>.md")   [zettel/llm.py]
2. Module builds a `mapping: dict[str, str]` from live pipeline state
3. fill_template(system, mapping) / fill_template(user_template, mapping)   [zettel/llm.py]
4. call_llm(llm, user, system=system, provider=..., prompt_cache=...)       [zettel/llm.py]
   -> apply_prompt_cache_hints() tags system for Anthropic prefix caching
   -> llm.invoke([SystemMessage, HumanMessage])
5. Raw response text is parsed:
   - extract_json(text) -> json.loads() -> Pydantic .model_validate()  (structured prompts)
   - or used as-is (free-text prompts: ask.md, image_description.md, article_section_*.md, article_personality.md)
6. Deterministic LLM-call cache: StateDB.get_cached_llm_response(call_checksum)
   short-circuits steps 3-5 on a repeat call (compute_llm_call_checksum in hashing.py)
```

Per-prompt entry/exit points:

```
literature_note.md   : harvester chunk -> extractor._process_chunk -> LiteratureChunkOutput -> draft LIT note
permanent_note.md    : approved concept -> connector._process_candidate -> PermanentNoteLLMOutput -> ZTL note
dedupe_decision.md   : new candidate vs similar notes -> extractor (dedupe path) -> DedupeResult -> create/ignore/refine
ptbr_guard.md        : PermanentNoteLLMOutput text (if English heuristic trips) -> connector._apply_ptbr_guard -> corrected fields
moc_generation.md    : new note cluster -> gardener._create_new_moc -> MOCGenerationOutput -> new MOC file
moc_incremental.md   : new notes + existing MOC -> gardener._update_existing_moc -> MOCIncrementalOutput -> MOC file patch
moc_hub_generation.md: hub note + BFS neighborhood -> gardener_hub -> MOCHubGenerationOutput -> new hub MOC file
moc_hub_incremental.md: new notes + existing hub MOC -> gardener_hub -> MOCIncrementalOutput-shaped JSON -> MOC file patch
bibliographic_metadata.md: harvested file header sample -> bibliography.enrich_with_llm -> BibliographicMetadata -> SRC frontmatter
image_description.md : extracted image + surrounding text -> assets.describe_pending_assets -> free-text description -> asset caption
ask.md                : user question + retrieved notes -> ask.run_ask -> free-text cited answer -> ASK note
article_query_enrich.md: article topic -> article.enrich_search_queries -> {"queries": [...]} -> expanded retrieval queries
article_outline.md   : catalog of retrieved notes -> article.generate_outline -> ArticleOutline -> section plan
article_section_blog.md / article_section_academic.md: one outline section + evidence -> article.draft_sections -> free-text Markdown section
article_anti_ai.md   : NOT called directly — its raw text is spliced as {anti_ai} into article_section_*.md's user mapping
article_judge.md     : drafted article body + notes catalog -> article.judge_article_body -> score dict -> approve/reject/rewrite loop
article_personality.md: assembled article body -> article.apply_personality_rewrite -> rewritten Markdown (facts/headings preserved)
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Structural convention | Every prompt file MUST split on `<!-- zettel:user -->`; system half has no per-call placeholders, user half has all of them | `zettel/llm.py:370-384` (`split_prompt_text`), enforced by `tests/test_prompt_cache.py:49-68` for 2 of 17 files |
| Selectivity gate (Prompt 1) | `literature_note.md` must reject a chunk (`chunk_status="rejected"`, `candidates: []`) unless it is primarily conceptual, non-structural, non-promotional, non-trivial content | `prompts/literature_note.md:11-49` |
| Candidate acceptance (Prompt 1) | Every extracted candidate must satisfy 7 mandatory criteria + ≥2 of 4 preferential criteria, with a numeric relevance score in [1,5] gated at ≥3 | `prompts/literature_note.md:51-121`; enforced field-level by `PermanentNoteCandidate.relevance_score` (`ge=1, le=5`) in `zettel/schemas.py:50-54` |
| Anchor-quote rule | `anchor_quote` must be a literal 10-25 word excerpt copied verbatim from the source chunk | `prompts/literature_note.md:167-172` |
| Image-driven candidates | A chunk with only code/fragments must NOT be auto-rejected as "fragmented" if a conceptual figure is present in `images_context`; the candidate is instead derived from the figure description | `prompts/literature_note.md:209-223` |
| Note-generation gate (Prompt 2) | `permanent_note.md` rejects concepts that are promotional, contextually inseparable from the source, irremediably vague, or genuinely devoid of substance — but is explicitly more permissive than Prompt 1 ("majority of criteria", not "all") | `prompts/permanent_note.md:11-64` |
| Connection-quality cap | Connections proposed by Prompt 2 should number 0-3 and only be included for genuine conceptual relations, drawn from a fixed enum of 6 relation types | `prompts/permanent_note.md:115-138`; enforced by `RelationType` enum in `zettel/schemas.py:21-27` |
| PT-BR enforcement (soft, per-prompt) | Nearly every system prompt instructs "TUDO em PT-BR" or "Responda em `{language}`" | present in literature_note.md, permanent_note.md, moc_generation.md, moc_incremental.md, moc_hub_generation.md, moc_hub_incremental.md, ask.md, article_outline.md, article_judge.md, article_query_enrich.md, article_personality.md, article_section_*.md |
| PT-BR enforcement (hard, corrective) | A heuristic (`_needs_ptbr_fix`: ≥3 of 8 English stopword markers present) conditionally triggers a second LLM call (`ptbr_guard.md`) that rewrites `thesis/definition/intuition/example/limits` | `zettel/connector.py:578-584` (heuristic), `:281-284` (gate), `:585-620` (guard call) |
| PT-BR guard scope | The guard is applied only to Prompt 2 (permanent note) output; Prompt 1 (literature note) output is never checked or corrected | `zettel/connector.py:284` is the only call site of `_apply_ptbr_guard` |
| Dedupe decision space | `dedupe_decision.md` constrains the LLM to exactly 3 named outcomes (`create_new`, `ignore`, `refine_existing`); the 4th enum value `MERGE` exists in code but has no corresponding prompt instruction | `prompts/dedupe_decision.md:5-9` vs `DedupeDecision` enum in `zettel/schemas.py:14-18` (has `MERGE = "merge"` with no textual definition anywhere in the prompt) |
| MOC topic taxonomy constraint | `moc_generation.md`'s `topic` field MUST be one of the categories injected via `{allowed_topics_section}`; if `gardener.strict_topics=true` a topic outside the taxonomy is rejected downstream | `prompts/moc_generation.md:9-24`; validated in `zettel/gardener.py` against `resolve_allowed_topics()` (see CLAUDE.md Phase 4 description) |
| MOC hub topic freedom | `moc_hub_generation.md`'s `topic` is explicitly NOT constrained to the taxonomy — only used as reference context | `prompts/moc_hub_generation.md:9-17` |
| Exhaustive note placement | Every note listed in a MOC-related prompt's input (`moc_generation.md`, `moc_incremental.md`, `moc_hub_generation.md`, `moc_hub_incremental.md`) must appear in exactly one output subsection, or (incremental variants only) may be explicitly placed in `"ignorar"` | `prompts/moc_generation.md:30`, `moc_hub_generation.md:21`, `moc_incremental.md:8-10`, `moc_hub_incremental.md:8-9` |
| Wikilink exclusion in MOC prose | MOC `summary`/`description`/`hub_role` fields must never contain `[[ZTL - ...]]` wikilinks | `prompts/moc_generation.md:31`, `moc_hub_generation.md:22` |
| Alias-only note referencing | All MOC/outline prompts must reference notes only by the short alias (`N1`, `N2`, ...) provided in the input list, never by raw note IDs | `prompts/moc_generation.md:29`, `moc_incremental.md:11`, `moc_hub_generation.md:20`, `moc_hub_incremental.md:11`, `article_outline.md:13` |
| Ask.py grounding rule | `ask.md` forbids answering beyond the supplied context notes; if evidence is insufficient it must emit an exact fixed sentence | `prompts/ask.md:13-20`; enforced upstream too — `ask.py` skips the LLM call entirely when `.hits` is empty (deterministic no-evidence path, per CLAUDE.md) |
| Wikilink fidelity in answers | `ask.md` requires copying the provided wikilink literally, never inventing or altering it | `prompts/ask.md:15-17` |
| Bibliographic metadata non-fabrication | `bibliographic_metadata.md` requires `null`/omission for unknown fields rather than invented values; JSON-only output with no markdown fencing | `prompts/bibliographic_metadata.md:10-13,34` |
| Document-type driven field priority | Different document types (thesis, online article, course material) have different required-field emphasis baked into the prompt instructions | `prompts/bibliographic_metadata.md:16-20` |
| Image description format contract | Must be 2-4 sentences, PT-BR, must not start with "A imagem mostra", must end with a fixed-format `Conceitos: term1, term2` line (or `(nenhum)`) | `prompts/image_description.md:5-14` |
| Article outline evidence lock | Outline sections must reference only `note_id`/`asset_id` values present in the supplied catalog; ≤2 figures per section; every section needs ≥1 relevant note | `prompts/article_outline.md:10-16`; enforced post-hoc by `article._sanitize_outline()` (`zettel/article.py:207`) |
| Style-dependent citation rule | Academic sections MUST cite with the exact `citacao_abnt` string per claim; blog sections MUST use narrative mentions and MUST NOT use ABNT-style `(SOBRENOME, ano)` citations or wikilinks | `prompts/article_section_academic.md:11-19`, `prompts/article_section_blog.md:10-18` |
| Cited-sources footer contract | Every article section must end with a machine-parseable `<!-- cites: @Citekey1,@Citekey2 -->` comment (or `<!-- cites: -->` if none) | `prompts/article_section_academic.md:26-33`, `prompts/article_section_blog.md:26-33` |
| Personality rewrite non-negotiables | Facts, citations, author names, years, section headings, and Obsidian embeds (`![[...]]`) must survive a personality rewrite unchanged; bibliographic sections ("Para saber mais", "Referencias", "Origem no vault") must stay factually intact | `prompts/article_personality.md:3-19` |
| Judge scoring/verdict rule | Four 0-10 criteria (fidelity, coverage, references, naturalness) are averaged; `verdict` must be `APPROVED`/`REJECTED` | `prompts/article_judge.md:10-33`; the code independently overrides `verdict` to `REJECTED` whenever `average < judge_min_score`, regardless of what the LLM said | `zettel/article.py:1146-1149` |
| Query-enrichment ordering rule | User-supplied `extra_queries` must be included first (normalized) before LLM-generated queries fill the remaining budget, with no duplicates | `prompts/article_query_enrich.md:14-16`; re-enforced in code at `zettel/article.py:1022-1027` |
| Anti-AI style guard subordination | Prose-naturalness rules in `article_anti_ai.md` explicitly yield to factual fidelity to the vault — never invent anecdotes/data to sound human | `prompts/article_anti_ai.md:3-11` |

### Detailed breakdown of the business rules

---

### Business Rule: Maximum Selectivity for Literature Candidates (Prompt 1 gate)

**Overview**:
`literature_note.md` implements the harshest filter in the whole pipeline: it is explicitly designed to produce **zero** output for the majority of chunks, on the theory that a Zettelkasten literature note is a seed for a future permanent note, not a summary of the source.

**Detailed description**:
The prompt defines five categories of automatic rejection (structural/navigational content, generic narrative/introductory text, promotional/commercial content, trivial/common-knowledge content, and fragmented/incomplete content) and, symmetrically, a checklist of seven mandatory tests a surviving candidate must pass (conceptual density, atomicity, semantic autonomy, generalizability, a literal 10-25 word anchor quote, specificity beyond a vague definition, and a relevance score of 3 or higher on a 1-5 scale). A candidate must additionally satisfy at least two of four "preferential" traits (counter-intuitive, transferable, actionable, connectable) to be considered strong, though these are not gating.

The relevance scale itself is calibrated with worked PT-BR/EN examples at each level (1=trivial dictionary definition, 5=paradigm-defining idea), and the prompt instructs the model to round down when uncertain between two levels ("Quando em duvida entre dois niveis, escolha o menor"). This asymmetric rounding rule is a deliberate anti-inflation mechanism — it cannot be verified in code (the LLM enforces it internally), so its effectiveness depends entirely on model compliance; there is no server-side re-scoring or sampling-based calibration.

The rule's effect ripples downstream: `extractor.py` persists whatever the LLM returns as `awaiting_review` concepts (`PermanentNoteCandidate.relevance_score` is schema-validated to the 1-5 range but not floor-checked against 3 in Python), meaning the `>=3` cutoff is a prompt-level convention, not a code-level invariant. A model that ignores the instruction and returns a 1 or 2 will still pass Pydantic validation and reach the review UI's confidence-band display (`review.py`'s `<=0.4` / mid / `>=limiar` bands operate on `review_confidence`, a separate concept from `relevance_score` — see Ambiguity note in Technical Debt).

**Rule workflow**:
```
chunk arrives -> LLM applies 5 rejection categories
  -> if primarily rejectable: chunk_status="rejected", candidates=[]
  -> else: for each candidate concept in the chunk:
       apply 7 mandatory criteria (all must pass)
       apply "round down when uncertain" on relevance_score
       check >=2 of 4 preferential criteria (soft signal, not gating)
       require literal 10-25 word anchor_quote
     -> emit accepted candidates array (chunk_status="accepted")
  -> extractor.py persists candidates verbatim, checkpoints chunk to awaiting_review
```

---

### Business Rule: Image-Derived Candidate Extraction

**Overview**:
Prompt 1 explicitly overrides its own "fragmented content -> reject" rule when a chunk co-occurs with a conceptually rich figure, allowing candidates to originate from the *image description* rather than the chunk text.

**Detailed description**:
Three sub-rules govern this: first, if a figure is *essential* to understanding a candidate (a mechanism diagram, a pipeline, a data-model diagram), its `asset_id` must be listed in that candidate's `relevant_image_ids` — purely decorative images or bare code screenshots must not be included. Second, if the chunk text itself is thin (a code listing, a transition sentence) but the accompanying figure's *description* carries an atomizable concept (the prompt's own examples cite "step-back prompting" and "parent document retriever" as illustrative concepts), the candidate should be generated from the figure description, using a snippet of that description as the `anchor_quote` and pointing the `source_locator` at the figure/passage.

Third, and most consequentially, chunks that would otherwise be rejected under the "fragmented" category (pure code, no interpretation) must NOT be rejected if a conceptual figure exists in the `images_context` of the same chapter — the model is instructed to extract the concept from the figure instead of discarding the chunk.

This rule depends entirely on `extractor.py::_build_images_context` (`zettel/extractor.py:184-187`) correctly assembling per-chapter image descriptions before the chunk is dispatched; if image description generation (`assets.py::describe_pending_assets`) has not yet run for a source's images (e.g. rate-limited and left `pending`), `images_context` will be empty and this override path is silently unavailable for that harvest run — the chunk falls back to ordinary text-only rejection criteria with no explicit signal that a figure-based rescue was possible.

**Rule workflow**:
```
extractor._process_chunk builds images_context from already-described assets
  in the same chapter/page range
  -> if images_context non-empty: prompt instructs LLM to consider figure-derived candidates
  -> LLM either:
       a) tags an existing text-based candidate's relevant_image_ids, or
       b) fabricates a candidate whose anchor_quote/source_locator point at the figure
  -> extractor persists candidates identically regardless of origin (text vs figure)
```

---

### Business Rule: Balanced Rejection Criteria for Permanent Notes (Prompt 2 gate)

**Overview**:
`permanent_note.md` deliberately relaxes Prompt 1's "reject unless everything is perfect" posture into "reject only for explicit, unambiguous cause" — the prompt's own header calls this the "Principio do Equilibrio" (balance principle).

**Detailed description**:
Where Prompt 1 requires **all** seven mandatory criteria plus a relevance floor, Prompt 2 requires only that the candidate avoid four explicit disqualifiers (promotional content, real conceptual emptiness, context that cannot be separated from the source, and irremediable ambiguity) and then asks the model to confirm the **majority** (not all) of five acceptance criteria (explanatory substance, semantic autonomy, transferability, conceptual clarity, connection value). The prompt explicitly carves out an exception for "well-established technical concepts" — these are valid for the vault even though they are not novel to the literature, because "the value is in documenting and connecting them," not in originality.

A particularly fine-grained carve-out concerns figures: a concept illustrated by a diagram (the prompt names RAG pipelines, step-back prompting, parent-document retrievers as its own worked examples — mirroring Prompt 1's figure-rescue rule) is valid; only a concept that reduces to "see Figure 3.2" with no standalone statement is rejected. This mirrors, at the permanent-note layer, the same figure-tolerance built into the literature-note layer, keeping the two prompts' treatment of visual evidence consistent across the pipeline.

The category taxonomy for rejection (`promotional | generic | vague | context_dependent | redundant | low_density`) is shared between the accepted and rejected JSON shapes — the prompt's own output-format examples show `category` present even in the accepted branch, which is unusual (most reject-flow-only fields default to empty string on acceptance) and suggests the `category` field doubles as a lightweight editorial tag even for notes that pass.

**Rule workflow**:
```
approved concept (from review) arrives at connector._process_candidate
  -> LLM checks 4 disqualifiers (promotional / empty / inseparable / ambiguous)
     -> if ANY triggers unambiguously: status="rejected", categorized, reason given
  -> else: LLM checks 5 acceptance criteria (majority, not all, required)
     -> generates title/thesis/definition/intuition/example/limits/tags
     -> proposes 0-3 connections from rag_context, typed by RelationType enum
  -> connector discards note on status=="rejected" (zettel/connector.py:269-275)
  -> else: note_output flows to PT-BR guard check, then note is written to vault
```

---

### Business Rule: Conditional PT-BR Correction Pass

**Overview**:
A second, narrower LLM call (`ptbr_guard.md`) corrects English-language leakage in permanent-note text fields, but only fires when a cheap heuristic detects likely English content, and only for Prompt 2's output.

**Detailed description**:
The trigger heuristic (`zettel/connector.py:578-582`, `_needs_ptbr_fix`) concatenates `thesis + definition + intuition` and counts occurrences of eight common English function words (`"the "`, `"and "`, `"this "`, `"that "`, `"with "`, `"from "`, `"which "`, `"where "`); if three or more distinct markers appear, the guard runs. This is a coarse lexical signal, not a language-detection model — a PT-BR note that happens to quote three English technical terms in a row (e.g. an anchor quote containing "with the gradient from this layer") could false-positive and trigger an unnecessary LLM call, while a note using only non-listed English words would false-negative and slip through uncorrected.

When triggered, the guard prompt receives a JSON dump of exactly five fields (`thesis, definition, intuition, example, limits`) and — per the prompt's own text — is told to "Retorne APENAS o texto corrigido, sem explicacoes adicionais" (return only the corrected text). The connector code, however, immediately calls `json.loads(extract_json(corrected_raw))` and reads back the same five keys (`zettel/connector.py:617-620`). The prompt file contains no explicit instruction to preserve JSON structure or field names — the working assumption is that because the input was JSON, the model will mirror that shape in its "corrected text," but nothing in the prompt enforces this. If the model instead returns a plain corrected paragraph (arguably a more literal reading of the prompt's own instructions), `extract_json`/`json.loads` will raise, and the exception handling in `_apply_ptbr_guard` presumably falls back to the original (uncorrected) output — but this makes the fallback path effectively the *common* case whenever the model complies with the prompt's literal wording rather than the code's implicit expectation.

This corrective layer is scoped exclusively to Prompt 2 output; there is no equivalent invocation for Prompt 1 (`literature_note.md`) drafts, meaning literature notes with English leakage are never auto-corrected and rely solely on the "TUDO em PT-BR" instruction embedded in Prompt 1's own system text.

**Rule workflow**:
```
connector._process_candidate produces note_output (PermanentNoteLLMOutput)
  -> _needs_ptbr_fix(thesis+definition+intuition) counts English markers
     -> if count < 3: no-op, note proceeds as-is
     -> if count >= 3:
          dump 5 fields as JSON -> ptbr_guard.md user template {text}
          call_llm() -> corrected_raw
          try: json.loads(extract_json(corrected_raw)) -> overwrite the 5 fields
          except: silently keep original fields (implicit; not explicitly logged per current reading)
```

---

### Business Rule: Taxonomy-Constrained vs. Free-Form MOC Topics

**Overview**:
The two "top-down" MOC prompts (`moc_generation.md`, used by the taxonomy/clustering pipeline) and the two "hub" MOC prompts (`moc_hub_generation.md`, used by the graph-degree hub pipeline) apply opposite policies toward the `topic` field's vocabulary.

**Detailed description**:
`moc_generation.md` requires `topic` to be drawn from a category list injected via `{allowed_topics_section}` (built by `gardener.py::resolve_allowed_topics`), with a `topic_justification` field mandatory to explain the choice; if none of the categories is a good fit, the prompt tells the model to pick the closest one and explain the mismatch in the justification field rather than inventing a new category outright. This is the taxonomy-governance mechanism referenced in CLAUDE.md's Phase 4 description ("New MOC topics validated against categorias; rejected if strict_topics: true") — the prompt is the first line of defense, and code-side validation (outside this component) is the enforcement backstop for `strict_topics=true`.

`moc_hub_generation.md`, by contrast, explicitly tells the model the taxonomy is "apenas contexto" (context only) and that `topic` "nao precisa coincidir com uma categoria" (need not match a category) — hub MOCs are meant to capture emergent, graph-driven themes that a fixed taxonomy might not anticipate. This deliberate asymmetry means the two MOC-generation pipelines (`gardener.py` and `gardener_hub.py`) can diverge in vocabulary discipline: a taxonomy MOC's `topic` is always one of a known, finite set of strings, while a hub MOC's `topic` is unconstrained natural language chosen by the LLM per call.

Both incremental-update variants (`moc_incremental.md`, `moc_hub_incremental.md`) share an identical placement contract: every new note must be assigned to an *existing* subsection by exact case-sensitive title match, sent to `"ignorar"` if it fits nowhere, or grouped into a proposed `new_subsections` entry if several new notes form a coherent cluster. The exact-title-match requirement is a latent fragility: if the MOC file's persisted subsection titles ever drift from what is passed into `{existing_subsections}` (e.g. due to manual edits to the MOC in Obsidian), the LLM has no way to reconcile the mismatch other than treating the note as belonging to a "new" subsection that collides in name with an old one.

**Rule workflow**:
```
Taxonomy MOC path (gardener.py):
  cluster of notes -> resolve_allowed_topics() -> {allowed_topics_section, taxonomy_detail}
  -> LLM must choose topic in allowed_topics_section (or justify closest match)
  -> topic_justification always required

Hub MOC path (gardener_hub.py):
  hub note + BFS neighborhood -> taxonomy passed as reference only
  -> LLM free-chooses topic string; hub_role required instead of topic_justification

Incremental path (both variants):
  new notes + existing subsection titles -> LLM assigns each note to an EXACT
  existing title, "ignorar", or a brand-new subsection group
```

---

### Business Rule: Evidence-Only Grounding for `ask` and `article`

**Overview**:
Both `ask.md` and the entire `article_*.md` family enforce a strict "no knowledge outside the supplied context" constraint, but implement the failure mode differently — `ask` has a deterministic short-circuit, while `article` relies purely on prompt instruction plus a downstream judge.

**Detailed description**:
`ask.md` instructs the model to answer only from the retrieved notes, to cite the *exact* wikilink text found in the context (never inventing or reformatting one), and — critically — to emit one fixed, verbatim sentence ("Nao encontrei evidencia suficiente no vault para responder a essa pergunta.") when the context is insufficient. Per CLAUDE.md and confirmed by the pipeline's `ask.py`, this rule is actually enforced twice: once at the retrieval layer (if `Retriever.search_notes().hits` is empty, `ask.py` never calls the LLM at all and returns the fixed sentence deterministically), and once inside the prompt itself for the case where some hits exist but are not sufficient to answer the specific question asked. The prompt also introduces a provenance vocabulary the model must reason over ("busca" vs "conexao \<tipo\> a partir de [[...]]"), explicitly flagging `contradicts`/`extends` graph-neighbor notes as the most informative complementary evidence.

The `article` pipeline's grounding rule is comparatively softer: `article_outline.md`, `article_section_blog.md`, and `article_section_academic.md` all state "nao invente fatos" (do not invent facts) and constrain `note_id`/`asset_id` references to the supplied catalog, but there is no code-level short-circuit analogous to `ask.py`'s empty-hits bypass — an outline or section can still be generated from a thin or irrelevant catalog, and it falls to `article_judge.md`'s `fidelity` score (and the `judge_min_score` threshold enforced in `zettel/article.py:1147`) to catch and reject ungrounded output *after* generation, inside the `max_judge_iterations` retry loop. This means article grounding is a **post-hoc statistical check**, whereas ask grounding is a **pre-hoc deterministic gate** — a structural asymmetry between the two consumers of similarly-worded "don't invent facts" instructions.

**Rule workflow**:
```
ask.md path:
  Retriever.search_notes(question) -> NoteSearchResult(hits, candidates)
  -> if hits empty: return fixed "no evidence" sentence, LLM never called
  -> else: build cited context from hits -> call ask.md -> answer citing exact wikilinks
     -> if still insufficient per-question: LLM emits the same fixed sentence itself

article.md path:
  catalog of retrieved notes (always populated, no emptiness gate)
  -> outline/section prompts instructed not to invent facts/ids
  -> draft assembled regardless of catalog thinness
  -> article_judge.md scores fidelity/coverage/references/naturalness
  -> average < judge_min_score => verdict forced to REJECTED (code override)
  -> judge feedback loops back into outline/section regeneration, up to max_judge_iterations
```

---

### Business Rule: Style-Bifurcated Citation Contracts for Article Sections

**Overview**:
`article_section_academic.md` and `article_section_blog.md` share an almost identical scaffold (same placeholders, same `{anti_ai}` splice, same `<!-- cites: ... -->` footer contract) but enforce mutually exclusive citation styles, and the correct file is chosen entirely by a Python string comparison at call time.

**Detailed description**:
`zettel/article.py:220-224` selects `article_section_blog.md` when `catalog.style == "blog"` and `article_section_academic.md` otherwise — there is no third style, and a typo or unexpected `style` value would silently fall through to the academic template. The academic prompt mandates ABNT NBR 10520 author-date citations using the *exact* `citacao_abnt` string supplied per source (explicitly forbidding invented surnames or years) for every substantive claim, forbids wikilinks in the body, and requires formal, precise, objective tone. The blog prompt inverts several of these: it mandates *narrative* mentions ("Como observa Alessandro Negro em *Knowledge Graphs and LLMs in Action*...") built from separately-supplied `mencao_leve`/`autor_natural`/`titulo` fields, explicitly forbids the formal `(SOBRENOME, ano)` citation form, and asks for an accessible, jargon-light tone.

Both variants converge again on a machine-readable trailer: a single-line HTML comment `<!-- cites: @Citekey1,@Citekey2 -->` (or `<!-- cites: -->` if nothing was cited) that downstream code presumably parses to build the article's final "Referencias"/"Origem no vault" sections (per CLAUDE.md's description of `article.py`'s catalog/citation assembly) — making this comment a load-bearing, unenforced-by-schema contract between the LLM's free-text output and the Python assembly step. Nothing in the prompt or (from the files analyzed) code validates that the citekeys inside the comment actually match citekeys used in the cited-style prose above it — the two citation mechanisms (inline ABNT/narrative mentions vs. the trailer comment) are trusted to stay consistent purely by prompt instruction.

**Rule workflow**:
```
article.draft_sections() selects prompt_name by catalog.style ("blog" | else academic)
  -> for each outline section:
       pack evidence/sources/figures for this section only
       fill {anti_ai} from article_anti_ai.md raw text (not itself a `<!-- zettel:user -->` prompt)
       call_llm() -> section Markdown body
       body must start with "## {heading}" (verbatim from outline)
       body must end with <!-- cites: ... --> trailer
  -> bodies concatenated by article.py's assemble step (outside this component)
```

## 4. Component Structure

```
prompts/
├── ask.md                        # Prompt 3 (QA): answer strictly from retrieved vault notes, cite wikilinks verbatim
├── article_anti_ai.md            # Fragment (not a standalone LLM call) spliced into article_section_*.md via {anti_ai}
├── article_judge.md              # Scores a drafted article on fidelity/coverage/references/naturalness; APPROVED|REJECTED
├── article_outline.md            # Plans article title/thesis/sections from a note catalog (blog|academic style)
├── article_personality.md        # Stylistic rewrite of a finished article body; facts/headings/citations frozen
├── article_query_enrich.md       # Expands an article topic into N semantic search queries for retrieval
├── article_section_academic.md   # Drafts one ABNT-cited section of an academic-style article
├── article_section_blog.md       # Drafts one narratively-cited section of a blog-style article
├── bibliographic_metadata.md     # Extracts ABNT NBR 6023 metadata (authors/year/type/etc.) from a document's opening pages
├── dedupe_decision.md            # Decides create_new | ignore | refine_existing for a new note vs. similar existing notes
├── image_description.md          # Describes a technical figure/diagram/chart in 2-4 PT-BR sentences plus a concept tag line
├── literature_note.md            # Prompt 1: extracts atomic candidate concepts from one source chunk (harsh selectivity)
├── moc_generation.md             # Generates a new taxonomy-anchored MOC from a note cluster (topic must match a category)
├── moc_hub_generation.md         # Generates a new hub-anchored MOC around one high-degree note (topic free-form)
├── moc_hub_incremental.md        # Places new notes into an existing hub MOC's subsections, or proposes new ones
├── moc_incremental.md            # Places new notes into an existing taxonomy MOC's subsections, or proposes new ones
├── permanent_note.md             # Prompt 2: evaluates/generates the permanent (ZTL) note body from an approved concept
└── ptbr_guard.md                 # Corrective pass: rewrites English-leaked text fields back into PT-BR (JSON in/out, undocumented in prompt text)
```

All 17 files are flat (no subdirectories) and follow the same two-part Markdown convention. None contain executable code; they are pure text assets read via `pathlib.Path`.

## 5. Dependency Analysis

```
Internal Dependencies (consumer module -> prompt file -> schema):

zettel/extractor.py   -> literature_note.md      -> LiteratureChunkOutput / PermanentNoteCandidate (schemas.py)
zettel/extractor.py   -> dedupe_decision.md       -> DedupeResult (schemas.py)
zettel/connector.py   -> permanent_note.md        -> PermanentNoteLLMOutput / RelationshipResult (schemas.py)
zettel/connector.py   -> ptbr_guard.md            -> ad-hoc dict (5 keys), no schema
zettel/gardener.py    -> moc_generation.md        -> MOCGenerationOutput / MOCSubsection (schemas.py)
zettel/gardener.py    -> moc_incremental.md       -> MOCIncrementalOutput / MOCNotePlacement (schemas.py)
zettel/gardener_hub.py-> moc_hub_generation.md    -> MOCHubGenerationOutput (schemas.py)
zettel/gardener_hub.py-> moc_hub_incremental.md   -> parsed as MOCIncrementalOutput-shaped dict, NOT model_validate'd against a class (see Technical Debt)
zettel/bibliography.py-> bibliographic_metadata.md-> BibliographicMetadata (bibliography.py, not schemas.py)
zettel/assets.py      -> image_description.md     -> free text (no schema)
zettel/ask.py         -> ask.md                   -> free text (no schema)
zettel/article.py     -> article_outline.md       -> ArticleOutline / ArticleOutlineSection (schemas.py)
zettel/article.py     -> article_section_blog.md  -> free text Markdown
zettel/article.py     -> article_section_academic.md -> free text Markdown
zettel/article.py     -> article_anti_ai.md       -> raw text spliced into other prompts' user mapping (not an independent LLM call)
zettel/article.py     -> article_query_enrich.md  -> ad-hoc dict {"queries": [...]}, no schema
zettel/article.py     -> article_personality.md   -> free text Markdown
zettel/article.py     -> article_judge.md         -> ad-hoc dict (4 scores + verdict + feedback), no schema
zettel/cli.py          -> (all 16 of the above except moc_hub_*) -> existence-only check in `doctor`

zettel/llm.py is the sole loader for every file above (load_prompt_parts / split_prompt_text / fill_template / call_llm).

External Dependencies:
- LangChain message types (SystemMessage/HumanMessage) - carries the split prompt to the provider (langchain_core)
- LLM providers via langchain_openai / langchain_anthropic / langchain_ollama / langchain_google_genai - actual inference
- Pydantic v2 (zettel/schemas.py, bibliography.py::BibliographicMetadata) - structured-output validation for 11 of 17 prompts
- SQLite `llm_cache` table (zettel/state.py, via hashing.py::compute_llm_call_checksum) - deterministic response caching keyed partly on the prompt's own hash (sha256 of `PromptParts.full_template`)
```

## 6. Afferent and Efferent Coupling

Coupling measured at the level of individual prompt files (the natural "component" unit here) against their Python consumer modules and the schema classes they produce.

| Prompt File | Afferent Coupling (consumer call sites) | Efferent Coupling (schema/parsing deps) | Critical |
|-------------|------------------------------------------|-------------------------------------------|----------|
| literature_note.md | 1 (extractor.py) | 2 (LiteratureChunkOutput, PermanentNoteCandidate) | High |
| permanent_note.md | 1 (connector.py) | 2 (PermanentNoteLLMOutput, RelationshipResult) | High |
| dedupe_decision.md | 1 (extractor.py) | 1 (DedupeResult, incl. unused MERGE enum member) | Medium |
| ptbr_guard.md | 1 (connector.py, conditional) | 0 (ad-hoc dict, no schema) | Medium |
| moc_generation.md | 1 (gardener.py) | 2 (MOCGenerationOutput, MOCSubsection) | Medium |
| moc_incremental.md | 1 (gardener.py) | 2 (MOCIncrementalOutput, MOCNotePlacement) | Medium |
| moc_hub_generation.md | 1 (gardener_hub.py) | 1 (MOCHubGenerationOutput) | Medium (untested by doctor) |
| moc_hub_incremental.md | 1 (gardener_hub.py) | 0 (parsed ad-hoc, no dedicated class) | Medium (untested by doctor) |
| bibliographic_metadata.md | 1 (bibliography.py) | 1 (BibliographicMetadata) | Medium |
| image_description.md | 1 (assets.py) | 0 (free text) | Low |
| ask.md | 1 (ask.py) | 0 (free text + deterministic pre-gate) | High (user-facing) |
| article_outline.md | 1 (article.py) | 2 (ArticleOutline, ArticleOutlineSection) | Medium |
| article_section_blog.md | 1 (article.py, conditional on style) | 0 (free text) | Medium |
| article_section_academic.md | 1 (article.py, conditional on style) | 0 (free text) | Medium |
| article_anti_ai.md | 2 (spliced into both article_section_*.md mappings) | 0 (raw text fragment, not independently invoked) | Low |
| article_query_enrich.md | 1 (article.py) | 0 (ad-hoc dict) | Low |
| article_personality.md | 1 (article.py, conditional — skipped for neutral/no-notes) | 0 (free text) | Low |
| article_judge.md | 1 (article.py, inside judge/rewrite loop) | 0 (ad-hoc dict) | Medium |

Overall pattern: every prompt has exactly one direct caller module (fan-in of 1), so afferent coupling is uniformly low at the file level — the real coupling concentration is in `zettel/llm.py`, which is the shared efferent dependency of all 17 files (fan-out from `llm.py` = 17). `article_anti_ai.md` is the only file with fan-in of 2 (both article section prompts), but as a spliced text fragment rather than an independent prompt.

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| LLM Provider (OpenAI/Anthropic/Ollama/Gemini/OpenRouter/etc.) | External Service | Executes every prompt template as a chat completion | LangChain client (`ChatOpenAI`/`ChatAnthropic`/`ChatOllama`/`ChatGoogleGenerativeAI`) | SystemMessage + HumanMessage in, text out | `call_llm` has no built-in retry beyond the LangChain client's own `max_retries`; callers (extractor/connector/gardener/etc.) wrap calls in `try/except` and degrade gracefully (e.g. connector logs and returns `None` on failure) |
| SQLite `llm_cache` | Internal Store | Deterministic response cache keyed by `compute_llm_call_checksum(prompt_hash, ...)` | Direct SQLite via StateDB | JSON request blob + raw text response | Cache miss falls through to a live call; no cache corruption handling visible in this component's scope |
| `zettel doctor` CLI check | Internal Tooling | Verifies 16 of 17 prompt files exist on disk before a pipeline run | Filesystem `Path.exists()` | N/A (existence boolean) | Missing file surfaces as a failed check row, not a hard crash; `moc_hub_generation.md`/`moc_hub_incremental.md` are excluded from this check (gap) |
| `zettel/schemas.py` Pydantic models | Internal Contract | Validates/coerces LLM JSON output for 11 of 17 prompts | In-process `model_validate()` | JSON dict -> typed model | `model_validate` raises on schema violation; callers generally wrap the whole LLM-call+parse step in `try/except` |

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Template Method / Split-Template | `<!-- zettel:user -->` marker separating stable system instructions from variable user payload | All 17 prompt files; parsed by `zettel/llm.py::split_prompt_text` | Enables provider prompt-prefix caching (cheaper/faster repeat calls of the same prompt type) |
| Strategy (per-style prompt selection) | `article.draft_sections()` selects `article_section_blog.md` vs `article_section_academic.md` at runtime based on `catalog.style` | `zettel/article.py:220-224` | Lets one pipeline stage produce stylistically incompatible outputs without branching inside a single prompt |
| Prompt Fragment Composition | `article_anti_ai.md` is not called independently; its raw text is read and interpolated into `{anti_ai}` inside both article section prompts | `zettel/article.py:226-227` | Shares a cross-cutting style instruction across multiple prompts without duplicating it in each file |
| Structured Output via Schema Validation | 11 of 17 prompts' JSON output is parsed with `extract_json()` then validated through a matching Pydantic model | `zettel/schemas.py`, `zettel/bibliography.py::BibliographicMetadata` | Fails fast/typed on malformed LLM output instead of propagating loosely-typed dicts through the pipeline |
| Deterministic Response Caching | Every consumer computes a `compute_llm_call_checksum()` over the filled prompt (+ context) before calling the LLM, checking `StateDB.get_cached_llm_response()` first | `zettel/hashing.py`, called from extractor.py/connector.py/gardener.py/gardener_hub.py/bibliography.py/article.py | Idempotent re-runs (e.g. after a pipeline failure) do not re-pay for identical LLM calls |
| Conditional Corrective Pass (self-healing generation) | `ptbr_guard.md` runs only when a language-leak heuristic trips, as a second LLM call over the first call's output | `zettel/connector.py:578-620` | Cheap common case (no second call) with a fallback correction path for the rare failure mode |
| Judge / Generator-Evaluator Loop | `article_judge.md` scores a draft; a low score triggers regeneration with judge feedback fed back into `article_outline.md`/`article_section_*.md`'s `{judge_feedback}` placeholder, bounded by `max_judge_iterations` | `zettel/article.py` (judge loop), `prompts/article_judge.md`, `prompts/article_section_*.md:{judge_feedback}` | Automated quality gate for long-form generation without human-in-the-loop per section |
| Few-Shot Prompting | Worked accept/reject examples embedded directly in the system block | `literature_note.md`, `permanent_note.md` | Anchors the model's calibration for a subjective judgment call (relevance/rejection) that a schema alone cannot constrain |

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `zettel/connector.py::_apply_ptbr_guard` + `prompts/ptbr_guard.md` | The prompt's stated output contract ("Retorne APENAS o texto corrigido") does not instruct the model to preserve/return JSON, yet the calling code unconditionally does `json.loads(extract_json(...))` expecting the same 5 keys back | A compliant-with-the-prompt-text model response (plain corrected prose) will fail JSON parsing; the correction silently fails to apply whenever the model takes the prompt's literal wording rather than the code's implicit JSON-mirroring assumption |
| Medium | `zettel/cli.py:1761-1778` (`doctor` command) | `moc_hub_generation.md` and `moc_hub_incremental.md` are absent from the doctor's prompt-existence checklist despite being actively loaded by `zettel/gardener_hub.py` | `zettel doctor` can report a fully healthy install while `garden --hubs` is one missing/corrupted file away from a runtime crash |
| Medium | `zettel/schemas.py::DedupeDecision` vs `prompts/dedupe_decision.md` | The enum defines a 4th value `MERGE = "merge"` that is never mentioned, defined, or exemplified in the dedupe prompt's own instructions (only `create_new`/`ignore`/`refine_existing` are documented) | Either dead schema surface (if `merge` is never actually produced) or an undocumented LLM behavior the prompt gives the model no guidance to produce correctly |
| Medium | `zettel/gardener_hub.py` (`moc_hub_incremental.md` consumer) | Unlike its taxonomy counterpart (`moc_incremental.md` -> `MOCIncrementalOutput`), the hub-incremental response is not shown being validated against a dedicated Pydantic model in the paths reviewed — output shape discipline for this prompt relies more heavily on prompt-text conformance alone | Weaker structural guarantee for one of the two MOC-incremental code paths; schema drift here would surface later and less clearly than for the taxonomy path |
| Low-Medium | `prompts/permanent_note.md` output schema | The `category` field's enum (`promotional \| generic \| vague \| context_dependent \| redundant \| low_density`) is documented only for the rejection case in the prose ("Categorias de rejeicao"), yet the JSON example for the ACCEPTED case also includes a populated `category` field, with no explanation of what an "accepted" category value should mean | Ambiguous contract: unclear whether `category` is meaningful/queryable on accepted notes, or a copy-paste artifact of reusing one JSON template for both branches |
| Low | Test coverage / prompt-content drift | Unit tests for gardener/gardener_hub (`tests/test_gardener.py`, `tests/test_gardener_hub.py`) write synthetic minimal prompt files into a temp directory rather than exercising the real `prompts/moc_generation.md` / `moc_incremental.md` / `moc_hub_*.md` content | Refactors or copy edits to the real MOC prompt files' placeholder names could silently break production while all gardener unit tests continue to pass against their own synthetic stand-ins |
| Low | `zettel/connector.py:212-214` (in-code comment) | The connector module's own comment flags that `cand.thesis`/`cand.definition`/etc. originate from LLM output derived from user-uploaded files and recommends sanitizing prompt delimiters (`"---"`, `"</s>"`, `"###SYSTEM"`) before re-interpolating them into the Prompt 2 user template — this sanitization is not implemented | A source PDF/Markdown containing adversarial text designed to look like prompt syntax could, in principle, influence Prompt 2's behavior when its own Prompt-1-derived fields are echoed back into Prompt 2's `{thesis}`/`{definition}` placeholders (a second-order prompt-injection surface, already self-identified by the codebase's authors) |
| Low | `_needs_ptbr_fix` heuristic (`zettel/connector.py:578-582`) | Hard-coded list of 8 English stopwords with a fixed threshold of 3 occurrences is a coarse proxy for "needs PT-BR correction"; it operates on `thesis+definition+intuition` only, never on `example`/`limits`/`tags` even though the guard prompt itself corrects `example` and `limits` too | Both false positives (English terms inside a legitimate anchor quote or technical jargon) and false negatives (a genuinely English passage using none of the 8 listed words) are possible; the fields checked for triggering and the fields corrected are inconsistent sets |

## 10. Test Coverage Analysis

| Prompt / Area | Direct Unit Tests | Integration-style Tests | Coverage | Test Quality |
|----------------|--------------------|---------------------------|----------|----------------|
| Split mechanism (`<!-- zettel:user -->`, `fill_template`) | `tests/test_prompt_cache.py` (7 tests: split-with-marker, split-without-marker, fill_template, call_llm system+human wiring, Anthropic-only cache hints, usage extraction, provider aliasing) | — | Good for the generic mechanism | Solid, focused assertions; directly loads and asserts on 2 real files (`literature_note.md`, `permanent_note.md`) confirming their placeholders land in the correct half of the split |
| `literature_note.md` (content/business rules) | Only indirectly, via the 2 placeholder-location assertions above | `tests/test_review.py` (exercises the review pipeline that consumes extractor output) — operates on already-produced `PermanentNoteCandidate` fixtures, not on the live prompt | Low for the prompt's actual instructional content (selectivity criteria, relevance scale, figure-rescue rule) | No test drives the real prompt text through a mocked LLM to confirm the rejection/acceptance categories or the anchor-quote/relevance-score rules are honored end-to-end |
| `permanent_note.md` | 1 placeholder-location assertion (`test_prompt_cache.py`) | `tests/test_connector.py` — exercises `connector.py` logic (candidate processing, `_needs_ptbr_fix`, connection typing) with mocked LLM responses | Medium for surrounding code, low for prompt content itself | Connector-side logic (PT-BR gate, RAG context building) is likely well covered by mocks; the actual prompt wording/criteria are not asserted |
| `dedupe_decision.md` | `tests/test_dedupe_decision.py` (dedicated file) | — | Medium | Dedicated test file suggests decent coverage of the `DedupeResult` consumption path; whether it also documents/tests the unused `MERGE` enum value is unclear from the file name alone |
| `ptbr_guard.md` | None found directly | Indirectly reachable through `tests/test_connector.py` if it mocks `_needs_ptbr_fix`-triggering content | Low | No test observed that specifically exercises the JSON-in/JSON-out mismatch flagged in Technical Debt above |
| `moc_generation.md` / `moc_incremental.md` | `tests/test_gardener.py` (multiple tests, e.g. `test_update_existing_moc_no_new_notes`, `test_update_existing_moc_with_placements`, `test_incremental_ignores_notes`) | — | Medium, but against synthetic prompt stand-ins, not the shipped file | Well-structured tests (mock LLM `.invoke`, assert on resulting MOC file content) but they write their own minimal prompt template into `tmp_path`, so the real `prompts/moc_generation.md`/`moc_incremental.md` text is never exercised by these tests |
| `moc_hub_generation.md` / `moc_hub_incremental.md` | `tests/test_gardener_hub.py` (uses a synthetic `prompts_dir` similarly to test_gardener.py) | — | Medium for surrounding logic, none for real prompt content | Same synthetic-stand-in caveat as the taxonomy MOC prompts, compounded by the missing `doctor` check noted above |
| `bibliographic_metadata.md` | Not directly identified in the files reviewed | Likely covered by `tests/test_...` files exercising `bibliography.py` (not confirmed by name in this pass) | Unknown/Low confidence | Insufficient evidence gathered to confirm a dedicated test path for this prompt's parsing/merge logic |
| `image_description.md` | Not directly identified | — | Unknown/Low confidence | No test file matching assets/image description content was found among the grepped prompt-referencing tests |
| `ask.md` | `tests/test_ask.py` (writes a minimal prompt template into `tmp_path`, per the `cfg.prompts_path = tmp_path` pattern) | — | Medium for surrounding retrieval/no-evidence logic, low for real prompt text | Same synthetic-template caveat as the gardener tests — the shipped `ask.md` wording (wikilink-literal-copy rule, fixed no-evidence sentence) is not directly asserted against |
| `article_outline.md`, `article_section_*.md`, `article_judge.md`, `article_query_enrich.md`, `article_personality.md`, `article_anti_ai.md` | `tests/test_article.py`, `tests/test_article_graph.py` (both pass `prompts_path=Path("prompts")` or `root / "prompts"` in several tests, i.e. the **real** shipped files) | LangGraph flow tests in `test_article_graph.py` (outline -> draft -> judge -> personality -> assemble) | Best real-content coverage in this component, since these tests point at the actual `prompts/` directory rather than a synthetic copy | These are the only prompt families in the codebase demonstrably tested against their shipped file content end-to-end (subject to LLM calls being mocked/cached — not independently confirmed in this pass) |

**Overall assessment**: The generic prompt-splitting/caching *mechanism* (`llm.py`) is well tested. Coverage of individual prompts' *business-rule content* is uneven: the `article_*` family is the only group exercised against its real shipped files; the literature/permanent/dedupe/MOC/ask prompts are either tested only through synthetic stand-in templates or not directly exercised at all, meaning a wording regression in those files (e.g. accidentally deleting the anchor-quote requirement, or renaming a placeholder) would not be caught by the current test suite unless it also breaks a downstream Pydantic schema.

---

Absolute path to saved report: `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-prompts-2026-08-30_10-22-26.md`
Component analyzed: `prompts`
