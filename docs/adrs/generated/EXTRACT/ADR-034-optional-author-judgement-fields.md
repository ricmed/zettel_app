# ADR-034: Author-Judgement Fields on the Candidate, Optional by Construction

**Status**: Accepted (2026-09-03)

**Depends on:** [ADR-015: Granular Per-Chunk Literature Notes with Readable Filenames](./ADR-015-granular-literature-notes-readable-filenames.md)

**Related to:**
- [ADR-006: Pydantic v2 for Configuration Schema and LLM-Backed DTOs](../INFRA/ADR-006-pydantic-v2-config-dtos.md)
- [ADR-016: Post-Approval Concept Deduplication Timing](../REVIEW/ADR-016-post-approval-concept-deduplication-timing.md)
- [ADR-025: System+Human Prompt Split for Provider-Agnostic Prompt Caching](../LLM/ADR-025-prompt-caching-system-human-split.md)

## Context and Problem Statement

A literature note and the permanent note derived from it both answer *what the concept is*: thesis, definition, intuition, limits. Almost nothing in the pipeline captures *how the author would decide* — "when X, do Y, because Z" — even when the source states it outright, in those words.

That gap has three downstream consumers. A cheatsheet in an exported Agent Skill has nothing to build decision rules from. A MOC cheatsheet, if one is ever built, has the same problem. And a reader of the vault gets an encyclopedia where the source was a manual.

The obvious fix — ask Prompt 1 for decision rules — carries a real risk. The extraction prompt is built around **maximum selectivity**: returning zero candidates is explicitly the right answer for many chunks. Adding a field the model wants to fill invites exactly the padding the prompt spends a page arguing against. A model asked for "the decision rule in this passage" will produce one whether or not the passage contains one.

## Decision Drivers

* Most chunks genuinely state no rule. The common case must be `[]`, and `[]` must be free of consequences.
* The fields must never influence acceptance: a chunk that only defines a concept is a valid candidate, and the extraction filter must not learn otherwise.
* A framework's name is the author's, not the pipeline's — translating "The 5 Whys" destroys the thing that makes it a citation.
* `concepts.candidate_json` is a JSON document, so new fields must not need a column migration.
* Whatever holds these fields on the permanent note has to be readable by the export without re-parsing a Markdown draft.
* Changing a prompt invalidates the deterministic LLM cache (ADR-025's checksum covers the filled prompt). That cost is acceptable but must be stated, not discovered.

## Considered Options

* Fields on `LiteratureChunkOutput` (chunk level).
* Fields on `PermanentNoteCandidate` (candidate level).
* A separate LLM pass over approved literature notes.
* For the permanent note: frontmatter keys, a rendered body section, or both.

## Decision Outcome

**Three optional list fields — `decision_rules`, `anti_patterns`, `named_frameworks` — live on `PermanentNoteCandidate`**, not on `LiteratureChunkOutput`. A rule belongs to *a thesis*, and the project's atomicity contract is one candidate = one thesis; hanging them off the chunk would associate a rule with whichever concepts happened to share a passage. A separate LLM pass was rejected outright: it would double the extraction cost to enrich material the same call already has in context.

**Optionality is enforced structurally, not by instruction.** The fields default to `[]`; `_check_candidate` is untouched, so they cannot participate in acceptance; and a `field_validator` drops blanks and truncates at three items rather than raising. Truncation matters: a `ValidationError` here would fail the whole chunk and burn an LLM retry over material the note does not depend on. The cap is also a density guard — the same "density over padding" reasoning that shapes the selectivity rules.

The prompts carry the other half. `literature_note.md` gains a subsection specializing the existing "Acionável" preferred criterion, with BOM/RUIM examples for each field, an explicit instruction to fill them only when the passage **enunciates** the rule, and an explicit boundary against `limits`: a caveat about when the thesis does not hold is `limits`; an anti-pattern is a **practice** someone performs that fails. `named_frameworks` is required to preserve the author's exact wording — no translation, no acronym expansion, no case normalization.

**On the permanent note, the fields travel as frontmatter, copied verbatim from the candidate — not through the LLM.** Prompt 2 *reads* them (so `definition`, `example` and `limits` can be concrete instead of generic), but what lands in `notes.frontmatter_json` is the author's text as extracted. Frontmatter over a body section: the export reads structured lists with no Markdown parsing, `zettel rebuild` reconstructs the file from `frontmatter_json` for free, and Obsidian renders list properties natively. Doing both would put the same text in two places in one file, with no rule for which wins when they diverge.

**On the literature note, the fields render as an `auto-decision` managed block** under a `## Julgamento do autor` heading, written only when at least one candidate states something — so a note whose chunk stated nothing looks exactly as it did before. Items are deduplicated across the chunk's candidates, since two candidates from one passage frequently name the same framework.

### Positive Consequences

* A source that *is* a manual now produces notes that read like one, without loosening the selectivity that keeps the vault small.
* The export (`zettel skill`) has a real cheatsheet input instead of inferring rules from `limits`.
* No migration: `concepts.candidate_json` is a JSON document and old rows parse with the fields absent.
* Being a managed block, the literature-note rendering survives manual edits through `safe_update_managed_blocks`.

### Negative Consequences

* Both prompts changed, so the deterministic LLM cache misses for every chunk and every concept processed after this. Already-processed chunks are **not** reprocessed automatically; they simply keep empty judgement fields until re-extracted.
* Managed blocks are stripped by `extract_embeddable_text`, so the judgement text does **not** reach the embedding. Accepted: it is structured metadata derived from chunk text that is already embedded, and re-embedding it would double-count the same passage.
* Whether models fill these fields honestly rather than obligingly is an empirical question this ADR cannot settle. The mitigation is structural — nothing depends on them being present — and the first corpus pass should be spot-checked for invented rules.

## Pros and Cons of the Options

### Fields on the candidate (chosen)

* Good, because a rule attaches to the thesis it qualifies, preserving atomicity.
* Good, because it costs no extra LLM call: the passage is already in context.
* Bad, because it enlarges the per-candidate output schema, which is the schema the model is asked to be most disciplined about.

### Fields on the chunk output

* Good, because it is one place per call instead of one per candidate.
* Bad, because a chunk with two candidates leaves the rule's owner ambiguous.
* Bad, because it invites chunk-level summarization, which is what granular notes exist to avoid.

### A separate enrichment pass

* Good, because Prompt 1 stays untouched and the cache stays warm.
* Bad, because it doubles the cost of extraction for a bonus field.
* Bad, because the second pass sees the note, not the source, so it would infer rules rather than quote them — the exact failure mode being guarded against.

### Body section on the permanent note (instead of frontmatter)

* Good, because prose reads better than a YAML list in Obsidian's property panel.
* Bad, because the export would need to parse Markdown to recover a list it could have read directly.
* Bad, because the note already renders `## Limites`, and two adjacent sections with overlapping content invite the duplication the prompt is told to avoid.

## Consequences

Anything that consumes candidate data gets these fields automatically: `chunks.summary_json` (which stores `model_dump()` of the approved candidates) already carries them, which is what lets a literature-only export read judgement data without touching the vault.

A future MOC cheatsheet should read from the same two places — ZTL frontmatter and `chunks.summary_json` — rather than re-deriving rules from note bodies.

If a later round finds models inventing rules, the fix is prompt-side (sharper BOM/RUIM examples, or requiring an anchor quote per rule), not schema-side: nothing in the pipeline depends on these fields being populated.

## References

* `zettel/schemas.py` — `PermanentNoteCandidate` (the three fields), `JUDGEMENT_FIELDS`, `_clean_judgement_list`
* `prompts/literature_note.md` — "Acionável como regra de decisão", per-field BOM/RUIM rules
* `prompts/permanent_note.md` — "Julgamento do autor (entrada opcional)" and the user payload keys
* `zettel/vault.py` — `render_decision_block`, `judgement_frontmatter`, `auto-decision` block in `build_literature_chunk_note`
* `zettel/connector.py` — `_format_judgement` (Prompt 2 payload), verbatim frontmatter copy
* `zettel/extractor.py` — `_check_candidate` (deliberately unchanged)
* `tests/test_judgement_fields.py` — legacy parse, truncation, filter neutrality, block rendering, frontmatter
