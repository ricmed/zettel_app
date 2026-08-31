# Component Deep Analysis Report — `schemas` (zettel/schemas.py)

## 1. Executive Summary

`zettel/schemas.py` is the single Pydantic v2 contract module for the entire Zettelkasten pipeline. It defines every structured object that crosses a trust boundary in the system: the shape of what LLM prompts are expected to return (Prompt 1 literature extraction, dedupe decisions, Prompt 2 permanent-note generation, MOC generation/incremental-update, hub-MOC generation, article outlines), plus two domain enums (`DedupeDecision`, `RelationType`) that encode fixed vocabularies used across extraction, connection, and gardening logic.

The module contains no behavior of its own — no methods beyond Pydantic defaults, no I/O, no validation logic beyond field constraints (`ge=1, le=5` on `relevance_score`). Its role is purely declarative: a shared vocabulary that `extractor.py`, `connector.py`, `gardener.py`, `gardener_hub.py`, `article.py`/`article_graph.py`, `review.py`, `cli.py`, and `web_app.py` all import from, instead of passing around loosely-typed dicts. Because `call_llm` in this project is not wired to LangChain's native structured-output APIs (no `with_structured_output`/`response_format` calls exist anywhere in the codebase), every one of these models is populated the same way: raw LLM text → `extract_json()` (in `llm.py`) → `json.loads()` → `Model(**data)` / `Model.model_validate_json(...)`. `schemas.py` is therefore the last line of defense that turns an LLM's free-text JSON blob into a typed, checked Python object before business logic touches it.

A second, less obvious role is **persistence contract**: `PermanentNoteCandidate` is not just an LLM output shape — its `.model_dump_json()` is written into SQLite's `concepts.candidate_json` column at harvest/extract time, and later re-hydrated via `.model_validate_json()` (in `cli.py`, `web_app.py`) or a bare `Model(**json.loads(raw))` (in `review.py`) at review/connect time. Any backward-incompatible field change to `PermanentNoteCandidate` therefore also breaks deserialization of previously-persisted rows — a schema-evolution concern this module's docstring/comments do not flag.

Key findings:
- Zero business logic inside the module itself; all enforcement (relevance thresholds, minimum word counts, anchor-quote requirement, PT-BR language guard, MOC topic taxonomy matching) lives in the consumer modules and merely operates *on* these typed objects.
- No dedicated test file (`tests/test_schemas.py` does not exist); coverage is entirely incidental, exercised through the consumer modules' own tests.
- Two inconsistent LLM-output parsing/error-handling patterns coexist across consumers (raise-on-invalid in `cli.py`/`extractor.py`/`gardener.py` vs. swallow-and-skip `try/except: continue` in `review.py`), even though all of them are hydrating the same `PermanentNoteCandidate` model from the same `candidate_json` column.
- `RelationType(str, Enum)` has a documented pitfall (f-string interpolation renders the enum name, not its value) that every consumer must remember to route through `.value` / a defensive normalizer — the module offers no safeguard against this footgun itself.

## 2. Data Flow Analysis

`schemas.py` is a passive library — it does not initiate data flow. Below are the distinct flows in which its models act as the validation/typing gate, traced end to end through the modules that own the actual logic.

**Flow A — Literature extraction (Prompt 1), `extractor.py`:**
```
1. Chunk text + prompt template rendered (extractor.py: _process_chunk)
2. call_llm() returns raw text
3. _parse_literature_output() -> extract_json() -> json.loads() -> LiteratureChunkOutput(**data)
4. output.candidates: list[PermanentNoteCandidate] validated field-by-field (types, relevance_score bounds)
5. _filter_candidates()/_check_candidate() apply extraction.min_relevance_score / min_thesis_words /
   min_definition_words / require_anchor_quote against the typed PermanentNoteCandidate
6. Each surviving PermanentNoteCandidate.model_dump_json() persisted to SQLite concepts.candidate_json
7. _write_literature_draft() consumes output.summary/output.key_concepts + [c.model_dump() for c in candidates]
   to render the LIT draft markdown via vault.build_literature_chunk_note()
```

**Flow B — Deduplication decision, `extractor.py::deduplicate_candidates`:**
```
1. PermanentNoteCandidate re-hydrated from candidate_json (dict wrapper carries source_id/chunk_id/concept_id)
2. dedupe_decision.md prompt filled and sent to LLM
3. _parse_dedupe_result() -> DedupeResult(**data), decision typed as DedupeDecision enum
4. decision == CREATE_NEW / IGNORE / REFINE_EXISTING / MERGE branches update concept status in SQLite
```

**Flow C — Candidate reload for review/connect, `cli.py` / `web_app.py` / `review.py`:**
```
1. SQLite concepts.candidate_json (raw JSON text written in Flow A)
2. PermanentNoteCandidate.model_validate_json(raw)  [cli.py, web_app.py — raises on invalid JSON/schema]
   -- or --
   PermanentNoteCandidate(**json.loads(raw))        [review.py — wrapped in try/except, silently skips row]
3. Rehydrated candidate feeds run_connect() (Flow D) or the review confidence-band report
```

**Flow D — Permanent note generation (Prompt 2), `connector.py::_process_candidate`:**
```
1. PermanentNoteCandidate (from Flow C) + RAG context (Retriever.hits) fill permanent_note.md
2. call_llm() -> _parse_permanent_note_output() -> extract_json/json.loads -> PermanentNoteLLMOutput(**data)
3. note_output.status == "rejected" short-circuits (no note written)
4. note_output.connections: list[RelationshipResult] merged with an injected "extends" RelationshipResult
   when the candidate is a refine_existing target
5. _relation_type_value() normalizes RelationType enum -> plain str .value before it reaches vault text
6. _resolve_connections() maps each RelationshipResult.related_note_id to a wikilink + relation label
7. build_permanent_note_body() renders the ZTL markdown; note persisted to vault + SQLite + Chroma
```

**Flow E — MOC generation (taxonomy pipeline), `gardener.py`:**
```
1. Cluster of note_ids + moc_generation.md prompt -> call_llm()
2. _parse_moc_output() -> MOCGenerationOutput(**data), subsections: list[MOCSubsection]
3. _validate_moc_topic() checks moc_output.topic against the taxonomy allowed list (substring match);
   rejects when strict_topics=True and no match
4. On acceptance, MOCSubsection.note_ids / .description render the MOC body sections
```

**Flow F — MOC incremental update, `gardener.py`:**
```
1. Existing MOC structure + new note_ids -> moc_incremental prompt -> call_llm()
2. _parse_incremental_output() -> MOCIncrementalOutput(**data)
3. Each MOCNotePlacement.subsection == "ignorar" (case-insensitive) sentinel excludes a note from placement
4. Placements matching an existing subsection title are grouped; placements matching neither
   "ignorar" nor an existing title are silently dropped and reconciled into a fallback subsection
5. new_subsections: list[MOCSubsection] appended as new MOC sections
```

**Flow G — Hub MOC generation, `gardener_hub.py`:**
```
1. Hub note + BFS-expanded neighborhood -> moc_hub_generation prompt -> call_llm()
2. _parse_hub_moc_output() -> MOCHubGenerationOutput(**data) (adds hub_role vs. MOCGenerationOutput)
3. subsections: list[MOCSubsection] render the hub MOC body; persisted with origin='hub_pipeline'
```

**Flow H — Article outline, `article.py` / `article_graph.py`:**
```
1. Catalog of retrieved notes/assets -> outline prompt -> call_llm() (or LangGraph node)
2. json.loads(...) -> ArticleOutline.model_validate(data); sections: list[ArticleOutlineSection]
3. _sanitize_outline() drops unknown note_ids/asset_ids, caps figure_asset_ids to 2, caps sections to
   max_sections, and injects a fallback "Desenvolvimento" section if the sanitized list is empty
4. HITL outline-decision loop re-validates edited outlines via ArticleOutline.model_validate() at each
   checkpoint (article_graph.py lines 310/349/368/386/575)
5. Per-section drafting reads ArticleOutlineSection.note_ids/figure_asset_ids to select cited evidence
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Validation | `relevance_score` must be an integer in `[1, 5]` | schemas.py:50-54 |
| Validation | Candidate rejected if `chunk_status == "rejected"` | extractor.py:498-503 |
| Validation | Candidate rejected if `relevance_score < extraction.min_relevance_score` (default 3) | extractor.py:504-505, config.py:79 |
| Validation | Candidate rejected if thesis word count `< extraction.min_thesis_words` (default 5) | extractor.py:506-508, config.py:80 |
| Validation | Candidate rejected if definition word count `< extraction.min_definition_words` (default 10) | extractor.py:509-511, config.py:82 |
| Validation | Candidate rejected if `require_anchor_quote=True` (default) and `anchor_quote` is blank | extractor.py:512-513, config.py:81 |
| Business Logic | Heuristic `review_confidence` score in `[0,1]` combines summary length, key_concepts count, approved-candidate relevance average, and anchor-quote coverage | extractor.py:427-445 |
| Business Logic | Auto-approve gate: chunk auto-approved only if `review_confidence >= literature_review.auto_approve_min_confidence` (default 0.85) | config.py:88, review.py (auto-approve path) |
| Business Logic | `DedupeDecision` drives 4-way branching (create_new / ignore / refine_existing / merge) that determines whether a new ZTL note is created, discarded, or attached as a refinement of an existing note | extractor.py:585-591 |
| Business Logic | `PermanentNoteLLMOutput.status == "rejected"` short-circuits permanent-note creation entirely (no vault write, no SQLite/Chroma upsert) | connector.py:269-275 |
| Business Logic | `refines_note_id` candidates get an auto-injected `RelationshipResult(relation_type="extends")` connection if the LLM didn't already propose one to the same note | connector.py:308-316 |
| Business Logic | `RelationType` values must be read via `.value`, never via `str()`/f-string, to avoid leaking `"RelationType.SUPPORTS"` into vault text | connector.py:66-76 |
| Business Logic | PT-BR language guard: if thesis+definition+intuition text is detected as non-PT-BR, `PermanentNoteLLMOutput` is regenerated via a corrective LLM call | connector.py:281-284 |
| Business Logic | MOC topic must substring-match an entry in the taxonomy's allowed-topics list; hard rejection when `gardener.strict_topics=True` and no match found | gardener.py:400-408, 422-451 |
| Business Logic | `MOCNotePlacement.subsection == "ignorar"` (case-insensitive) is a sentinel meaning "do not place this note anywhere" | gardener.py:660-662 |
| Business Logic | A placement whose `subsection` matches neither `"ignorar"` nor an existing subsection title is silently dropped and the note is reconciled into a fallback subsection | gardener.py:663-666, 680-685 |
| Business Logic | Article outline sanitization: unknown `note_ids` fall back to the top-3 catalog notes; `figure_asset_ids` capped to 2; `sections` capped to `max_sections`; empty result forces a default "Desenvolvimento" section | article.py:690-724 |
| Business Logic | Candidate persistence round-trip: `PermanentNoteCandidate.model_dump_json()` written to `concepts.candidate_json`; later reloaded via `model_validate_json()` (strict) or `Model(**json.loads(raw))` inside `try/except` (lenient, silently drops bad rows) | cli.py:71, web_app.py:118, review.py:648-654 |
| Domain Constraint | `DedupeDecision` is a closed, fixed vocabulary of exactly 4 members; no dynamic extension | schemas.py:14-18, tests/test_dedupe_decision.py:17 |
| Domain Constraint | `RelationType` is a closed, fixed vocabulary of 6 members, each with a PT-BR inverse-relation label used when rendering backlinks | schemas.py:21-27, connector.py `_INVERSE_RELATION` |

### Detailed breakdown of the business rules

---

### Business Rule: Candidate Quality Gate (`_check_candidate`)

**Overview**:
Every `PermanentNoteCandidate` produced by the literature-extraction LLM call (Prompt 1) passes through a deterministic, code-side quality gate before it is allowed to become a permanent-note candidate eligible for deduplication and note generation. The gate is not part of `schemas.py` itself (the schema only enforces `relevance_score` bounds via Pydantic `Field(ge=1, le=5)`); it is implemented in `extractor.py::_check_candidate` and applied via `_filter_candidates`.

**Detailed description**:
The gate runs four sequential checks, any one of which is sufficient to reject the candidate outright: (1) if the LLM itself marked `chunk_status == "rejected"` on the candidate (a self-reported signal that the chunk yielded nothing extractable — e.g., boilerplate, references list, table of contents), the candidate is rejected with the LLM's own `rejection_reason`/`rejection_category` echoed into the rejection message; (2) the `relevance_score` (an LLM-assigned 1–5 integer, already bounded by the Pydantic field constraint) must meet or exceed `extraction.min_relevance_score` (default 3), filtering out candidates the model itself considered marginal; (3) the `thesis` field, split on whitespace, must contain at least `extraction.min_thesis_words` (default 5) words — this catches degenerate or truncated theses; (4) the `definition` field must similarly meet `extraction.min_definition_words` (default 10); (5) if `extraction.require_anchor_quote` is true (the default), a candidate with a blank/whitespace-only `anchor_quote` is rejected, since the anchor quote is the traceability link back to the literal source text used later for citation and quality auditing.

All four thresholds are configurable per-deployment via `config.yaml`'s `extraction` block, meaning the same schema (`PermanentNoteCandidate`) can be subjected to stricter or looser gates without any code change — the schema defines the *shape*, the config defines the *bar*. Rejected candidates are not deleted; `deduplicate_candidates`/`review.py` still see them in SQLite but flagged for exclusion, and the reason is logged at debug level (`extractor.py:490`) with the first 60 characters of the offending thesis, which is useful for tuning thresholds but is not surfaced anywhere in the vault or CLI output as a structured report.

This rule matters architecturally because it is the only quality checkpoint between an LLM's raw, potentially noisy structured output and the corpus of concepts that will eventually consume an expensive Prompt 2 call (`connector.py`) to become a permanent note. A loose gate wastes LLM budget generating notes from weak candidates; a strict gate (e.g., `require_anchor_quote=True` combined with a low-recall extraction prompt) can silently starve the pipeline of candidates from sources whose content doesn't lend itself to short quotable anchors (e.g., heavily paraphrased or translated material).

**Rule workflow**:
```
LiteratureChunkOutput.candidates (raw from LLM, Pydantic-validated for types/bounds only)
        │
        ▼
for each PermanentNoteCandidate:
   chunk_status == "rejected"?  ──yes──▶ reject (reason = LLM's own rejection_reason/category)
        │no
        ▼
   relevance_score < min_relevance_score?  ──yes──▶ reject
        │no
        ▼
   len(thesis.split()) < min_thesis_words?  ──yes──▶ reject
        │no
        ▼
   len(definition.split()) < min_definition_words?  ──yes──▶ reject
        │no
        ▼
   require_anchor_quote and not anchor_quote.strip()?  ──yes──▶ reject
        │no
        ▼
   approved (persisted to concepts table, candidate_json stored, eligible for dedupe/connect)
```

---

### Business Rule: Heuristic Review Confidence Scoring (`_score_review_confidence`)

**Overview**:
A deterministic, non-LLM heuristic converts a `LiteratureChunkOutput` into a single `[0, 1]` confidence score used to gate the `--auto-approve` fast path during Phase 2/2b, sparing a human reviewer from having to look at every chunk.

**Detailed description**:
The score starts at a fixed base of 0.4 for any non-rejected chunk (an explicitly `rejected` chunk short-circuits to a flat 0.1 and never reaches the additive logic). It then accumulates up to 0.15 for a summary of at least 20 words (a proxy for the LLM having engaged substantively with the chunk rather than producing a token response), up to 0.15 for `key_concepts` presence (0.05 per concept, capped), and — only when at least one candidate survives the quality gate described above (`_filter_candidates`) — up to 0.1 for the average `relevance_score` of approved candidates (scaled against the maximum of 5) and up to 0.2 for the proportion of approved candidates that carry a non-blank `anchor_quote`. A chunk that produced zero candidates is capped at 0.55 regardless of how good its summary was, and a chunk whose candidates were all filtered out by the quality gate is capped at 0.45 — both caps exist so that "no usable content extracted" chunks cannot slip past the `auto_approve_min_confidence` threshold (default 0.85) purely on the strength of a well-written summary.

This heuristic is deliberately not an LLM call (no cost, no latency, no cache dependency) and is fully reproducible for a given `LiteratureChunkOutput` + `AppConfig` pair, which matters for the `--auto-approve` CLI flag being auditable and for tests to assert exact scores. Because the formula reads fields straight off the Pydantic models (`output.chunk_status`, `output.summary`, `output.key_concepts`, `output.candidates`, and each candidate's `relevance_score`/`anchor_quote`), any change to those field names or semantics in `schemas.py` requires a corresponding audit of this scoring function — the coupling is implicit (by field name) rather than enforced by any interface contract.

**Rule workflow**:
```
chunk_status == "rejected"? ──yes──▶ score = 0.1 (terminal)
        │no
        ▼
score = 0.4
+0.15 if len(summary.split()) >= 20
+min(0.15, 0.05 * len(key_concepts))
        │
        ▼
candidates empty? ──yes──▶ score = min(score, 0.55) (terminal)
        │no
        ▼
approved, _ = _filter_candidates(candidates)     [reuses the Candidate Quality Gate above]
approved empty? ──yes──▶ score = min(score, 0.45) (terminal)
        │no
        ▼
+0.1 * (avg(relevance_score for approved) / 5.0)
+0.2 * (count(approved with non-blank anchor_quote) / len(approved))
score = round(min(1.0, score), 3)
```

---

### Business Rule: Dedupe Decision Routing (`DedupeDecision` / `DedupeResult`)

**Overview**:
`DedupeResult.decision`, a closed 4-member enum (`create_new`, `ignore`, `refine_existing`, `merge`), is the single output that determines what happens to a candidate concept once it is compared (via LLM) against semantically similar existing permanent notes.

**Detailed description**:
`deduplicate_candidates` (extractor.py) runs a dedicated LLM prompt (`dedupe_decision.md`) per candidate once retrieval has surfaced a plausibly-similar existing note, and parses the response strictly into `DedupeResult`. The four-way branch in `extractor.py:585-591` treats `CREATE_NEW` as "proceed to Prompt 2 as an independent new note," `IGNORE` as "drop the candidate entirely — it is a duplicate of existing knowledge with nothing new to add," and both `REFINE_EXISTING` and `MERGE` as "attach to an existing note" — the two are handled identically in the branch shown (both fall into the same `elif` arm), with the distinction currently only preserved in the `reason`/`target_note_id` fields, not in materially different downstream behavior. `target_note_id` is `Optional[str]`, populated only for the refine/merge paths and validated at usage time against the notes table (a `None`/missing target on a `refine_existing` decision would need to be handled defensively downstream, since the schema itself does not enforce "target_note_id required when decision is REFINE_EXISTING or MERGE" as a cross-field constraint).

This decision directly controls whether the corpus grows monotonically (every candidate becomes a new note) or self-consolidates (near-duplicate ideas fold into a single, richer note over time) — a core design goal of a Zettelkasten system, where atomic notes should not be duplicated across sources. Because the decision comes from an LLM call rather than a pure similarity threshold, its quality is bounded by prompt quality and model capability, and `reason` (free text) is the only audit trail for why a given fork was taken; it is logged but not currently surfaced in any vault-visible artifact.

**Rule workflow**:
```
Retriever finds semantically-similar existing note(s) for a candidate
        │
        ▼
dedupe_decision.md prompt filled with candidate + similar-note context -> call_llm()
        │
        ▼
DedupeResult(**json.loads(extract_json(text)))   [raises if 'decision' isn't one of the 4 enum values]
        │
        ├─ decision == CREATE_NEW        ──▶ candidate proceeds unmodified to Prompt 2 (connect)
        ├─ decision == IGNORE            ──▶ candidate dropped, concept marked "duplicate"
        └─ decision in {REFINE_EXISTING, ──▶ candidate proceeds to Prompt 2 with refines_note_id set;
                        MERGE}                connector.py injects an auto "extends" RelationshipResult
                                               pointing at target_note_id if the LLM didn't propose one
```

---

### Business Rule: Permanent Note Status Gate and Auto-Injected Extension Link

**Overview**:
`PermanentNoteLLMOutput.status` acts as a second, later-stage rejection gate (distinct from the candidate-quality gate above), and `refines_note_id` propagation from the dedupe decision guarantees a graph edge is never silently lost even if the LLM omits it from `connections`.

**Detailed description**:
After a candidate clears the quality gate and the dedupe check, it still reaches Prompt 2 (`permanent_note.md`), which can itself decide the material doesn't warrant a standalone note (`status == "rejected"`, with `reason` explaining why). This is a second, independent rejection point layered on top of the pre-filtering already done in extraction — the two prompts have different context (Prompt 1 sees only the raw chunk; Prompt 2 sees the candidate plus RAG context of existing related notes), so Prompt 2 can catch redundancy or triviality that only becomes apparent once surrounding context is visible. On rejection, `_process_candidate` returns `None` without writing to the vault, SQLite `notes` table, or Chroma — the concept remains without a `note_id` and will be retried on a subsequent `connect` run unless its status is otherwise changed.

Separately, when a candidate arrived at `connector.py` carrying a `refines_note_id` (set upstream when `DedupeDecision` was `REFINE_EXISTING`/`MERGE`), the code checks whether `note_output.connections` (the LLM's own proposed `RelationshipResult` list) already contains a connection to that same `related_note_id`; if not, it appends a synthetic `RelationshipResult(relation_type="extends", description=<refine_reason>)`. This is a defensive guarantee: the dedupe decision already committed to "this candidate extends note X," and the system does not want that structural fact to depend on the second LLM call remembering to reproduce it — it is enforced in code, not left to prompt compliance.

**Rule workflow**:
```
PermanentNoteLLMOutput parsed from Prompt 2 response
        │
        ▼
status == "rejected"? ──yes──▶ log reason, return None (no note written, concept stays note_id=None)
        │no
        ▼
connections = list(note_output.connections)
        │
        ▼
refines_note_id set (from upstream DedupeDecision)?
        │yes                                   │no
        ▼                                       ▼
already_connected = any(c.related_note_id       skip
   == refines_note_id for c in connections)
        │
        ▼
not already_connected? ──yes──▶ append RelationshipResult(relation_type="extends", ...)
        │
        ▼
resolved_connections = _resolve_connections(connections)  [note_id -> wikilink + relation label]
        │
        ▼
ZTL note written to vault + SQLite + Chroma
```

---

### Business Rule: RelationType Value Normalization

**Overview**:
`RelationType` is a `str, Enum` hybrid, which creates a well-documented Python footgun: `isinstance(x, str)` is `True` for its members, but f-string/`str()` interpolation of a member renders `"RelationType.SUPPORTS"` rather than `"supports"`. `connector.py::_relation_type_value` is the single sanctioned normalizer, and every code path that turns a `RelationshipResult.relation_type` into vault-visible text is required to route through it.

**Detailed description**:
Pydantic, when validating a `RelationshipResult` from LLM JSON, may leave `relation_type` as either the plain string it parsed from JSON or coerce it to the `RelationType` enum member (behavior can vary with Pydantic's enum-coercion path and whichever code constructs the object directly, e.g. the synthetic `RelationshipResult(relation_type="extends", ...)` constructed in `connector.py:312-316` passes a plain str literal, not an enum member, while a value parsed from LLM JSON through the schema is coerced to the enum). `_relation_type_value` handles both cases explicitly: if the value `isinstance(relation_type, Enum)`, it returns `.value`; otherwise it coerces to `str` (with a `"related"` fallback for falsy values). This function is exercised directly by `tests/test_connector.py` (`test_relation_type_value_resolves_enum_value`, `test_resolve_connections_normalizes_enum_type_to_value`), which also assert the raw pitfall (`f"{RelationType.SUPPORTS}" == "RelationType.SUPPORTS"`) as living documentation of why the normalizer exists.

The business impact of skipping this normalization would be a corrupted vault artifact: the literal string `"RelationType.SUPPORTS"` leaking into a rendered wikilink's relation label or a backlink block in a permanent note — a defect class that is easy to introduce accidentally anywhere a developer writes `f"{conn.relation_type}"` instead of `conn.relation_type.value` or the shared normalizer, since both spellings type-check identically under the `str, Enum` hybrid.

**Rule workflow**:
```
RelationshipResult.relation_type (RelationType enum member OR plain str, depending on construction path)
        │
        ▼
_relation_type_value(relation_type):
    isinstance(relation_type, Enum)?  ──yes──▶ return relation_type.value
        │no
        ▼
    return str(relation_type or "related")
        │
        ▼
Used in _resolve_connections() and vault rendering to guarantee plain lowercase strings
   ("supports", "contradicts", "extends", "depends_on", "exemplifies", "related")
   ever reach markdown output — never the Python repr of the enum member
```

---

### Business Rule: MOC Topic Taxonomy Validation

**Overview**:
`MOCGenerationOutput.topic` (and, transitively, `MOCHubGenerationOutput.topic`) must correspond to an entry in the project's configured category taxonomy before a MOC can be created under that topic name, enforced by `gardener.py::_validate_moc_topic` / `_topic_matches_allowed`.

**Detailed description**:
The taxonomy (loaded via `resolve_allowed_topics(cfg.gardener.topics_path, cfg.gardener.allowed_topics, strict=cfg.gardener.strict_topics)`) is a list of allowed category label strings. Validation is a case-insensitive, bidirectional substring match: the LLM-proposed topic passes if it contains any allowed topic as a substring, or if an allowed topic contains the proposed topic as a substring — a deliberately loose match designed to tolerate minor LLM phrasing variance (e.g., "Aprendizado por Reforço" vs. "Reforço") without requiring exact string equality. If no allowed topic exists at all (`allowed` is empty), validation trivially passes — the taxonomy gate is opt-in via configuration. When `cfg.gardener.strict_topics` is `True` and no match is found, the MOC is rejected outright and `moc_output.topic_justification` (an LLM-provided free-text rationale for why it chose an off-taxonomy topic) is logged as a warning for human review; when `strict_topics` is `False`, the behavior (not fully shown in the excerpt but implied by the code structure) is presumably to allow the new topic through with a warning, expanding the effective taxonomy organically.

This rule is the primary control point preventing MOC topic sprawl — without it, every clustering run could invent a slightly different label for a conceptually identical topic (e.g., "Redes Neurais" vs. "Deep Learning" vs. "Neural Networks"), fragmenting what should be a single navigational hub. Because the match is substring-based rather than embedding-similarity-based, it can both over-match (an allowed topic "IA" would substring-match almost anything containing "ia") and under-match (a topic phrased with a genuine synonym rather than a shared substring, e.g. "PLN" vs. "Processamento de Linguagem Natural", would fail unless both forms are separately listed) — a known-brittle heuristic that is not accompanied by any explicit test in the retrieved code for these edge cases.

**Rule workflow**:
```
MOCGenerationOutput.topic (from LLM)
        │
        ▼
resolve_allowed_topics(topics_path, allowed_topics, strict=strict_topics) -> allowed: list[str]
        │
        ▼
allowed empty? ──yes──▶ valid (pass)
        │no
        ▼
for allowed_topic in allowed:
    allowed_topic.lower() in topic.lower()  OR  topic.lower() in allowed_topic.lower()
        │match found ──▶ valid (pass)
        ▼no match after loop
strict_topics == True? ──yes──▶ reject; log topic_justification as warning
        │no
        ▼
(non-strict path: MOC allowed through; taxonomy treated as advisory)
```

---

### Business Rule: MOC Incremental Placement Sentinel and Reconciliation Fallback

**Overview**:
When incrementally updating an existing MOC (`gardener.py::_apply_incremental_placements`), each `MOCNotePlacement.subsection` value is interpreted against a three-way rule: the literal sentinel `"ignorar"` (case-insensitive) means "deliberately exclude," a match against an existing subsection title means "place here," and anything else is treated as an invalid/unrecognized placement that is silently dropped and reconciled via a fallback mechanism rather than causing a hard failure.

**Detailed description**:
This is a resilience-oriented design choice: LLM output for `subsection` is free text (`Field(description="Titulo da subsecao existente ou 'ignorar'")` in schemas.py:114), so it is expected to occasionally not exactly match any of the existing subsection titles supplied in the prompt (due to paraphrasing, capitalization, or the LLM inventing a subsection name that overlaps with, but isn't identical to, an existing one). Rather than treating a non-matching subsection as a validation error that would abort the whole incremental update, the code tracks every note_id it successfully "placed" (whether into an explicit subsection or the "ignorar" sentinel) in a `placed: set[str]`, and at the end computes `missing = allowed_ids - placed` — any note the LLM was supposed to account for but didn't cleanly place anywhere. These `missing` notes are not dropped from the vault; they are logged and, based on the code structure, routed into a `_MOC_FALLBACK_SUBSECTION` catch-all so that no note silently vanishes from its MOC due to an imperfect placement response.

The `new_subsections` list (each a `MOCSubsection` with its own `note_ids`) is applied afterward, independently re-resolving each referenced note through `_resolve_note_ref` and re-checking the `placed` set to prevent a note from being placed twice (once via `placements` and again via a newly proposed subsection referencing the same id) — order of application (`placements` first, `new_subsections` second) determines which assignment wins for a note referenced in both.

This rule matters because it makes the entire incremental-MOC-update path tolerant of imperfect LLM output without requiring a retry loop or a hard pipeline failure — a design tradeoff that favors availability (every note ends up *somewhere* in the MOC) over precision (a note might land in a generic fallback bucket rather than the "correct" thematic subsection the LLM intended).

**Rule workflow**:
```
for p in incremental_output.placements (list[MOCNotePlacement]):
    nid = _resolve_note_ref(p.note_id, allowed_ids, alias_to_id)
    nid missing or already placed? ──yes──▶ skip
        │no
        ▼
    p.subsection.lower() == "ignorar"? ──yes──▶ mark placed, no subsection assignment
        │no
        ▼
    p.subsection not in existing_titles? ──yes──▶ skip (leaves nid unplaced -> falls into `missing`)
        │no
        ▼
    placement_map[p.subsection].append(nid); mark placed

for new_sub in incremental_output.new_subsections (list[MOCSubsection]):
    for ref in new_sub.note_ids:
        resolve + dedupe against `placed`; collect into this new subsection if not already placed

missing = allowed_ids - placed
missing non-empty? ──yes──▶ log count; notes appended under _MOC_FALLBACK_SUBSECTION when the MOC body
                              is reconstructed subsection-by-subsection
```

---

### Business Rule: Article Outline Sanitization

**Overview**:
`ArticleOutline`/`ArticleOutlineSection` objects produced by the outline-generation LLM step (or edited by a human via the HITL loop in `article_graph.py`) are passed through `_sanitize_outline` before being used to drive per-section drafting, guaranteeing every reference the outline makes is resolvable against the actual retrieved-note/asset catalog.

**Detailed description**:
Three independent corrections are applied. First, every `note_ids` entry on a section is filtered against `catalog.notes.keys()` — an LLM can hallucinate a note_id that doesn't exist in the retrieved evidence set, and any such id is silently dropped; if filtering leaves a section with zero valid note_ids, a fallback assigns the first three catalog notes (by insertion/ranking order) so that no section is ever drafted with literally nothing to cite. Second, `figure_asset_ids` are similarly filtered against `catalog.assets.keys()` and additionally hard-capped to the first two survivors, a compositional constraint (at most 2 figures per section) that exists independent of what the LLM proposed. Third, the outline's total `sections` list is truncated to `max_sections` (a caller-supplied limit, presumably tied to a config value or article length target), and if truncation (or upstream filtering) leaves zero sections at all, a single default section titled "Desenvolvimento" is synthesized with a generic goal and the first five catalog notes, guaranteeing the article-generation pipeline always has at least one section to draft regardless of how degenerate the LLM's outline was.

Every field is also defensively `.strip()`-ed with fallback text (`heading.strip() or "Secao"`, `outline.title.strip() or catalog.topic`), meaning even a well-formed but empty-string outline field never propagates an empty heading/title into the rendered article. This sanitization step is what allows the downstream HITL/judge loop (`article_graph.py`) to always operate on a structurally sound `ArticleOutline`, decoupling "the LLM produced a plausible outline" from "the outline is safe to draft against" — the two are not the same guarantee, and `schemas.py` alone (field types only) cannot provide the second.

**Rule workflow**:
```
ArticleOutline (raw, from LLM or human edit) + ArticleCatalog + max_sections
        │
        ▼
for sec in outline.sections[:max_sections]:
    note_ids = [n for n in sec.note_ids if n in catalog.notes]
    note_ids empty? ──yes──▶ note_ids = first 3 catalog note_ids
    fig_ids = [a for a in sec.figure_asset_ids if a in catalog.assets][:2]
    append ArticleOutlineSection(heading.strip() or "Secao", goal.strip() or "", note_ids, fig_ids)
        │
        ▼
sections still empty after loop? ──yes──▶ sections = [default "Desenvolvimento" section, first 5 notes]
        │
        ▼
return ArticleOutline(title.strip() or catalog.topic, thesis.strip(), sections, style_notes or "")
```

---

## 4. Component Structure

`schemas.py` is a flat, single-file module — 175 lines, no submodules, no `__init__` re-exports beyond the package-level `zettel/__init__.py` (which does not appear to special-case it based on the import style seen everywhere: `from zettel.schemas import X` / `from .schemas import X`).

```
zettel/
└── schemas.py                       # Sole file for this component
    ├── Enums
    │   ├── DedupeDecision            # 4-member closed vocabulary (create_new/ignore/refine_existing/merge)
    │   └── RelationType              # 6-member closed vocabulary (supports/contradicts/extends/
    │                                 #   depends_on/exemplifies/related)
    ├── LLM Extraction Outputs (Prompt 1 / dedupe / Prompt 2 connections)
    │   ├── PermanentNoteCandidate    # atomic concept extracted from a chunk; also the SQLite
    │   │                             #   candidate_json persistence contract
    │   ├── LiteratureChunkOutput     # top-level Prompt 1 response (wraps candidates)
    │   ├── DedupeResult              # dedupe_decision.md prompt response
    │   └── RelationshipResult        # one typed edge between permanent notes (Prompt 2 output field)
    ├── LLM MOC Output (taxonomy pipeline)
    │   ├── MOCSubsection             # shared building block: title + note_ids + description
    │   └── MOCGenerationOutput       # full-MOC-from-scratch prompt response
    ├── MOC Incremental Update
    │   ├── MOCNotePlacement          # single note -> subsection (or "ignorar") assignment
    │   └── MOCIncrementalOutput      # incremental-update prompt response (placements + new_subsections)
    ├── Hub MOC Output
    │   └── MOCHubGenerationOutput    # hub-anchored MOC prompt response (adds hub_role field)
    ├── Article generation
    │   ├── ArticleOutlineSection     # one section of an article outline
    │   └── ArticleOutline            # full outline (title/thesis/sections/style_notes)
    └── Permanent Note LLM Output
        └── PermanentNoteLLMOutput    # Prompt 2 structured response (the ZTL note body + connections)
```

No configuration file, no test file, and no `__init__.py` entry are dedicated to this component — it is consumed purely via direct module import.

## 5. Dependency Analysis

```
Internal Dependencies (schemas.py's own imports):
schemas.py → (stdlib only: enum.Enum, typing.Optional)
schemas.py → pydantic (BaseModel, Field)

Internal Dependencies (consumers of schemas.py — this component has no outgoing dependency
on any other zettel/*.py module; it is a leaf/foundation module):

cli.py            → schemas.PermanentNoteCandidate            (candidate reload for `connect`)
web_app.py        → schemas.PermanentNoteCandidate             (candidate reload for web-triggered connect)
review.py         → schemas.PermanentNoteCandidate             (candidate reload for review confidence report)
extractor.py      → schemas.{DedupeDecision, DedupeResult,     (Prompt 1 + dedupe parsing/filtering)
                              LiteratureChunkOutput, PermanentNoteCandidate}
connector.py      → schemas.{PermanentNoteCandidate,           (Prompt 2 parsing, connection resolution)
                              PermanentNoteLLMOutput, RelationshipResult}
gardener.py       → schemas.{MOCGenerationOutput,              (MOC generation + incremental update parsing)
                              MOCIncrementalOutput}
gardener_hub.py   → schemas.MOCHubGenerationOutput              (hub MOC generation parsing)
article.py        → schemas.{ArticleOutline, ArticleOutlineSection}  (outline sanitization/rendering)
article_graph.py  → schemas.ArticleOutline                      (LangGraph state re-validation at checkpoints)
vault.py          → (no import; only a comment referencing RelationType's str-Enum pitfall — no runtime dependency)

External Dependencies:
- pydantic (>= v2, uses model_validate/model_validate_json/model_dump/model_dump_json — v2 API surface)
  - Purpose: declarative schema definition, JSON (de)serialization, field-level validation
- Python stdlib `enum` — backing for DedupeDecision / RelationType
- Python stdlib `typing` — Optional[str] on DedupeResult.target_note_id
```

No database, network, filesystem, or other I/O dependency exists inside `schemas.py` itself — all such dependencies belong to the consumer modules that use these types as data-in/data-out contracts.

## 6. Afferent and Efferent Coupling

Coupling measured at the class (Pydantic model / Enum) level, counting distinct consumer modules (afferent, Ca) that import/instantiate/reference the class, and distinct other schema classes each class structurally depends on via field types (efferent, Ce).

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| PermanentNoteCandidate | 7 modules (cli, web_app, review, extractor, connector, + tests) | 0 | High |
| LiteratureChunkOutput | 1 module (extractor) | 1 (PermanentNoteCandidate) | Medium |
| DedupeDecision | 1 module (extractor) + DedupeResult | 0 | Medium |
| DedupeResult | 1 module (extractor) | 1 (DedupeDecision) | Medium |
| RelationshipResult | 2 modules (connector, + tests) | 1 (RelationType) | High |
| RelationType | 2 modules (connector, vault-comment only) + RelationshipResult | 0 | High |
| MOCSubsection | 3 consumers (MOCGenerationOutput, MOCIncrementalOutput, MOCHubGenerationOutput) | 0 | Medium |
| MOCGenerationOutput | 1 module (gardener) | 1 (MOCSubsection) | Low |
| MOCNotePlacement | 1 module (gardener, via MOCIncrementalOutput) | 0 | Low |
| MOCIncrementalOutput | 1 module (gardener) | 2 (MOCNotePlacement, MOCSubsection) | Medium |
| MOCHubGenerationOutput | 1 module (gardener_hub) | 1 (MOCSubsection) | Low |
| ArticleOutlineSection | 2 modules (article, article_graph via ArticleOutline) | 0 | Medium |
| ArticleOutline | 2 modules (article, article_graph) | 1 (ArticleOutlineSection) | Medium |
| PermanentNoteLLMOutput | 1 module (connector) | 1 (RelationshipResult) | High |

Notes on criticality: `PermanentNoteCandidate` and `RelationshipResult`/`RelationType` are marked **High** because they sit on the persistence round-trip (SQLite `candidate_json`) and on the enum-value footgun respectively — a breaking change to either has effects beyond a single call site and has already required a dedicated defensive helper (`_relation_type_value`) and inconsistent reload error-handling across three modules. `PermanentNoteLLMOutput` is High because it is the terminal gate for the most expensive LLM call in the pipeline (Prompt 2) — any schema drift here silently breaks note generation.

## 7. Endpoints

Not applicable — `schemas.py` is a pure data-modeling module with no REST/GraphQL/gRPC/CLI surface of its own. (Endpoints that transitively use these models, e.g. the web UI's job-enqueue routes in `web.py`, belong to those components' own analyses, not this one.)

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|--------------|-----------------|
| LLM providers (via llm.py::call_llm) | External Service (indirect) | Source of raw text later validated into these schemas | HTTPS (LangChain client) | Free text expected to contain a JSON block | `extract_json` + `json.loads` + `Model(**data)`; a malformed/missing JSON block raises at the `json.loads`/Pydantic-validation step, which propagates as an uncaught exception in most call sites (e.g. `_parse_literature_output`, `_parse_dedupe_result`, `_parse_moc_output`, `_parse_hub_moc_output`, `_parse_incremental_output`, `_parse_permanent_note_output`) — none of these parse functions catch validation errors themselves |
| SQLite `state.db` (`concepts.candidate_json`) | Internal Persistence | Round-trip storage of `PermanentNoteCandidate` between extract and review/connect phases | SQLite TEXT column | JSON (via `model_dump_json()` / re-parsed via `model_validate_json()` or `Model(**json.loads(raw))`) | Inconsistent: `cli.py`/`web_app.py` call `model_validate_json` unguarded (raises on corrupt/incompatible row); `review.py` wraps the equivalent reload in `try/except: continue`, silently dropping unreadable rows from the review report |
| vault markdown builders (`vault.py`) | Internal | Consumes `.model_dump()`/typed fields of `PermanentNoteCandidate`, `RelationshipResult` (via resolved dict), MOC subsections, and article outline sections to render frontmatter + body text | In-process function calls | Python dict / str | No explicit error handling in schemas.py; consumers assume well-formed input since it already passed Pydantic validation |
| LangGraph state (`article_graph.py`) | Internal | `ArticleOutline.model_validate(state["outline"])` re-validates the outline at every HITL checkpoint, since LangGraph state is stored as a plain dict between graph steps | In-process (LangGraph `StateGraph`) | dict → Pydantic model | Re-validation raises if a human-edited outline (or a corrupted checkpoint) no longer matches the schema; no visible fallback for a failed re-validation in the excerpts reviewed |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Data Transfer Object (DTO) / Value Object | All classes in schemas.py | zettel/schemas.py | Typed, self-describing boundary objects for LLM I/O and persistence, replacing loosely-typed dicts |
| Closed Enumeration (Fixed Vocabulary) | `DedupeDecision`, `RelationType` | schemas.py:14-27 | Constrain LLM output and downstream branching to a known, exhaustive set of values |
| Schema-as-Contract for LLM Structured Output (manual variant) | `_parse_*` functions across extractor.py/connector.py/gardener.py/gardener_hub.py combined with `extract_json` + `Model(**data)` | Multiple consumer modules | Achieves the effect of LangChain's `with_structured_output` without depending on provider-specific structured-output APIs — portable across OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible gateways per this project's multi-provider `llm.py` design |
| Serialization Round-Trip / Persistence-via-Model | `PermanentNoteCandidate.model_dump_json()` written to SQLite, `model_validate_json()`/`Model(**json.loads(...))` read back | extractor.py, cli.py, web_app.py, review.py | Avoids a second, hand-maintained persistence schema for candidates; the Pydantic model doubles as the storage format |
| Defensive Normalization Wrapper | `_relation_type_value()` in connector.py | connector.py:66-76 | Works around the `str, Enum` hybrid's f-string/`str()` rendering pitfall documented on `RelationType` |
| Sanitize-After-Generate | `_sanitize_outline()` (article.py), `_filter_candidates()`/`_check_candidate()` (extractor.py), `_validate_moc_topic()` (gardener.py) | article.py:690-724, extractor.py:480-514, gardener.py:422-460 | Treats LLM-populated schema instances as untrusted input requiring a second, code-side pass before being trusted as ground truth for pipeline decisions |
| Reconciliation Fallback | `_apply_incremental_placements()`'s `missing = allowed_ids - placed` + fallback subsection | gardener.py:637-701 | Guarantees no domain entity (note) is lost due to imperfect LLM structured output, favoring completeness over strict adherence to the LLM's stated intent |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `PermanentNoteCandidate` persistence round-trip | Three different reload sites (`cli.py`, `web_app.py`, `review.py`) use two different error-handling strategies for the identical `candidate_json` → model operation — two raise on invalid data, one silently skips the row | A schema migration (e.g., adding a required field, renaming a field) will cause `zettel connect` / the web "connect" job to hard-crash while `review`'s confidence report silently under-reports pending concepts, producing inconsistent operator-visible behavior from the same root cause |
| High | `RelationType(str, Enum)` hybrid | The type is easy to misuse (`f"{x}"` renders the enum's Python name, not its value); currently only `connector.py` defends against this via `_relation_type_value`, and nothing in `schemas.py` prevents a *new* consumer from reintroducing the bug elsewhere | A future call site (e.g., a new report or export feature) that interpolates `relation_type` directly could leak `"RelationType.SUPPORTS"` into user-facing vault content without any test catching it, since the schema module offers no built-in safeguard (e.g., a `__str__` override) |
| Medium | No dedicated test file for `schemas.py` | All coverage of these models is incidental — exercised only through consumer-module tests (`test_extractor.py`, `test_connector.py`, `test_gardener.py`, `test_dedupe_decision.py`, `test_article.py`) | Field-level validation behavior (e.g., what happens when `relevance_score` is out of `[1,5]`, or a required field like `PermanentNoteLLMOutput.status`/`.title` is missing from LLM JSON) is untested in isolation; a Pydantic version upgrade or a subtle field-constraint regression could pass all consumer tests that happen to always supply well-formed data while still breaking on genuinely malformed LLM output |
| Medium | `DedupeDecision.REFINE_EXISTING` vs `.MERGE` | Both decisions are routed through the same code branch (`extractor.py:589`) with no observable behavioral difference beyond what's carried in `reason`/`target_note_id` | The schema advertises two distinct semantic outcomes that the implementation currently treats as one; if a future change needs to differentiate them (e.g., MERGE should combine tags/content while REFINE_EXISTING should only add a relationship), the current code offers no scaffolding for that distinction, and it is unclear from the code alone whether the duplication is intentional or an implementation gap |
| Medium | Cross-field validation absent | `DedupeResult` does not enforce "`target_note_id` must be non-null when `decision` is `REFINE_EXISTING` or `MERGE`"; `MOCNotePlacement.subsection` relies on the string literal `"ignorar"` as an implicit sentinel rather than a modeled `Optional[str]`/explicit boolean field | Pydantic validation alone cannot catch an LLM response like `{"decision": "refine_existing", "target_note_id": null}` — it will validate successfully and only fail (or behave unexpectedly) downstream when code tries to use a `None` target; the "ignorar" sentinel is a magic string not documented anywhere except a field description comment, tests, and the one branch that checks it |
| Low | Field defaults masking missing LLM output | Many fields default to `""` or `[]` (e.g., `intuition`, `limits`, `tags`, `rejection_reason`) rather than being required | An LLM response that omits a field entirely (vs. explicitly returning an empty string) is indistinguishable after validation from one that deliberately returned empty content, which slightly weakens the ability to detect a genuinely malformed/incomplete LLM response versus a legitimately sparse one |
| Low | No schema versioning | `PermanentNoteCandidate.model_dump_json()` is persisted to SQLite with no schema-version tag alongside it | Since CLAUDE.md itself documents "no backward compatibility with legacy code" as a project convention, any breaking field change to this model invalidates all previously-persisted `candidate_json` rows with no migration path or detection mechanism beyond a crash at reload time |

## 11. Test Coverage Analysis

There is no `tests/test_schemas.py`. Coverage of these models is entirely indirect, via the tests of the modules that consume them.

| Component (model/enum) | Direct Unit Tests | Indirect/Integration Coverage | Test Quality |
|--------------------------|--------------------|-------------------------------|----------------|
| `DedupeDecision` / `DedupeResult` | `tests/test_dedupe_decision.py` (4 tests: enum membership, enum count, `DedupeResult` construction from dict for `refine_existing`/`create_new`, and from a raw JSON string for `ignore`) | none beyond direct tests | Good for enum-membership/basic construction; does not test invalid/missing `decision` value, does not test `MERGE`, does not exercise the actual `_parse_dedupe_result` parsing function (only the model constructor directly) |
| `PermanentNoteCandidate` | `tests/test_extractor.py` (helper `_make_candidate()` builds valid instances used across all `_filter_candidates`/`_check_candidate` tests) | Also touched by `tests/test_review.py`, `tests/test_web.py`/`test_web_state.py` (per grep in Section "Dependency Analysis") for round-trip persistence flows | Good coverage of the *business-rule gate* (`_check_candidate`) through valid/overridden instances; no test directly asserts Pydantic-level validation behavior (e.g., constructing with `relevance_score=0` or `6` to confirm the `ge=1, le=5` constraint actually raises) |
| `RelationshipResult` / `RelationType` | `tests/test_connector.py` (`test_relation_type_value_resolves_enum_value`, the `str, Enum` pitfall demonstration test, `test_resolve_connections_normalizes_enum_type_to_value`, plus construction tests around lines 51/68/94/115) | none beyond direct tests | Good — this is the best-tested schema group, explicitly covering the enum footgun that motivated `_relation_type_value`'s existence |
| `MOCGenerationOutput` / `MOCSubsection` | `tests/test_gardener.py` (`_make_moc_output` helper, direct `MOCSubsection` construction at line 727) | none found beyond direct tests | Adequate; exercises the shape used by `_validate_moc_topic`/MOC-writing tests, though no test isolates `MOCGenerationOutput`/`MOCSubsection` field validation itself (e.g., malformed subsections) |
| `MOCIncrementalOutput` / `MOCNotePlacement` | `tests/test_gardener.py` (lines 353, 749-773 — includes a test with a `"GHOST"` note_id to exercise the missing/dangling-reference reconciliation path) | none beyond direct tests | Good — explicitly tests the reconciliation-fallback edge case (unresolvable note reference), which is one of the more failure-prone business rules identified above |
| `MOCHubGenerationOutput` | No direct construction found in the retrieved test excerpts; `tests/test_gardener_hub.py` exists but its content around this schema was not confirmed in this pass | Likely indirect via `_parse_hub_moc_output`/hub-generation flow tests | Unconfirmed — flagged as a gap needing direct verification; not enough evidence in the reviewed excerpts to assert coverage exists |
| `ArticleOutline` / `ArticleOutlineSection` | `tests/test_article.py` (direct construction at lines 230-299, exercising multiple outline shapes) | `tests/test_article_graph.py` exists and likely exercises `ArticleOutline.model_validate()` re-validation at LangGraph checkpoints, but was not opened in this pass | Good direct coverage in test_article.py; the LangGraph re-validation checkpoints (article_graph.py lines 310/349/368/386/575) are a plausible gap if test_article_graph.py does not specifically test a checkpoint receiving a malformed/edited outline dict |
| `PermanentNoteLLMOutput` | No direct construction found via grep in the reviewed test files (only referenced through `connector.py` internals: `_apply_ptbr_guard`, `_parse_permanent_note_output`) | Likely indirect via `tests/test_connector.py`'s broader connector flow tests | Unconfirmed as a standalone-model test; given this model gates the most expensive LLM call in the pipeline (Prompt 2) and carries the `status == "rejected"` short-circuit business rule, this is the single highest-value gap identified — no evidence surfaced of a test asserting behavior when `status="rejected"` or when `connections` is malformed |

**Overall test coverage assessment**: The schema module benefits from solid *incidental* coverage for the models most entangled with tricky business rules (`RelationType`'s enum footgun, `MOCIncrementalOutput`'s dangling-reference reconciliation, `PermanentNoteCandidate`'s quality-gate rejection paths) because those consumer modules' own test suites needed to exercise those rules directly. The weakest points are (a) no isolated test of Pydantic field-constraint enforcement on any model (e.g., confirming `relevance_score` bounds actually raise `ValidationError`), and (b) `PermanentNoteLLMOutput` and `MOCHubGenerationOutput`, which — based on the evidence gathered — lack confirmed direct test coverage despite gating high-cost/high-impact pipeline decisions (Prompt 2 note creation, hub MOC generation).

---

*End of report.*
