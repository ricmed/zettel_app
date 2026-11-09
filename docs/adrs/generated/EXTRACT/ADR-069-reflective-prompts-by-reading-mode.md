# ADR-069: Prompts Chosen by Reading Mode

**Status:** Accepted (provisional until `evals/preregistration/396-leitura-reflexiva.md` is scored)
**Date:** 2026-11-08
**Depends on:** [ADR-068: The Estante, Books as LIV Notes With a SQLite Mirror](../WEB/ADR-068-estante-books-as-liv-notes.md), [ADR-034: Author-Judgement Fields on the Candidate, Optional by Construction](./ADR-034-optional-author-judgement-fields.md)
**Related to:** [ADR-042: Domain as First-Class Config](../INFRA/ADR-042-domain-as-first-class-config.md), [ADR-043: Distant Analogies Are Suggestions](../RETRIEVAL/ADR-043-distant-analogies-as-suggestions.md), [ADR-051: Citation Provenance on the Permanent Note](../QA-WRITING/ADR-051-citation-provenance-on-permanent-note.md)

## Context and Problem Statement

`prompts/literature_note.md` extracts concepts. It:

- rejects narrative passages (`narrative`);
- asks for theses that generalise;
- forbids "segundo X".

`prompts/permanent_note.md` keeps the work out of the note. For a novel, a short story or a comic, that leaves almost nothing.

Yet the author wants exactly those books in the vault for something else: a metaphor, a thought, an analogy, a philosophical question, a character's situation. Each should be usable later as an illustration ("Assim como Bilbo em *O Hobbit*...", "Conforme Tolstói em *Guerra e Paz*...") in a blog post or an answer.

## Decision Drivers

* The purpose differs, so the prompts differ. One prompt with a "fiction mode" paragraph would dilute both.
* Nothing changes for a source without a book on the shelf, or for a technical book.
* Grounding is not relaxed: the anchor stays literal.
* A reflective note must never pass as evidence for a factual claim.

## Decision Outcome

### The reading mode

`library.reading_mode.effective_reading_mode` decides it, in this order:

1. the LIV's `reading_mode`;
2. otherwise `reflexao` when the book's nature is fiction, or when a genre is marked `reflective` in `config/library.yaml` (philosophy, psychology, biography, essay, memoir);
3. otherwise `conceito`.

A source with no LIV is `conceito`.

### Two prompts per phase

* **Extract.** It uses `prompts/literature_note_reflective.md` for sources in `reflexao` mode (`extractor.Prompt1Set`). That prompt:
  * looks for metaphors, thoughts, analogies, philosophical questions, characters and scenes;
  * accepts narrative passages;
  * rejects pure plot transitions as `narrative`;
  * keeps the anchor contract (10–25 literal words, `quote_is_grounded`).
* **Connect.** It uses `prompts/permanent_note_reflective.md` (`ConnectSession.prompt_for`). The note:
  * states the idea outside the work;
  * shows it through the work and the characters;
  * says how to use it as an analogy.

  `exemplifies` is the natural edge to a conceptual note. `corroborates` stays out of the prompt (pinned by `tests/test_prompts.py`).
* **What does not change.** Both prompts fill the same placeholders as the conceptual ones. Their few-shots are separate sections of `config/domain_examples.yaml` (`literature_note_reflective`, `permanent_note_reflective`). The prompt file is part of the cache key, so the two modes never share an answer.

### Reflective fields on the candidate

* **The fields.** `reflection_kind` (`metafora`, `pensamento`, `analogia`, `questao_filosofica`, `personagem`, `cena`) and `characters`.
* **Optional, as in ADR-034.** They default to empty, take no part in `_check_candidate`, and are cleaned instead of rejected.
* **Copied by code.** They go to the ZTL frontmatter together with `reading_mode: reflexao` (`vault.reflection_frontmatter`), never through the LLM.
* **Manual notes.** A reading note written on the shelf carries them in its LIT frontmatter. `manual_lit.build_candidate_from_literature` reads them back.

### Use in writing

* **The retriever.** It takes `exclude_source_ids`, which drops a source's notes from the pool, from the topic-index seeds and from the graph neighbours.
* **Academic articles** leave reflective sources out, through the search, the MOC boost and the extra hops, unless `retrieval.article.academic_include_reflective` is set.
* **Blog articles** keep them. Each note is marked `leitura: reflexiva` and carries a `mencao_literaria` (characters, title, author). The section prompt says it is an analogy, never proof.
* **`ask`** names the work and the author of every note and marks the reflective ones, with the same rule.

### Failure mode

A broken `library.yaml` must not stop the pipeline. `extract`, `connect` and `ask` then log a warning and treat every source as `conceito`, which is the behaviour from before the shelf existed.

### Positive Consequences

* A novel produces notes that a blog post can actually use, with the work cited by name.
* Technical books and sources outside the shelf keep their prompts, their cache and their measurements.

### Negative Consequences

* Two more prompts to maintain. They are covered by the `reflective-reading` entry of `evals/measurement_map.yaml`.
* The garden's taxonomy has no literary pillar, so reflective ZTL may land in `_unassigned`.
* The decision is provisional. Pre-registration 396 asks whether the reflective prompt beats the conceptual one on the same passages, judged by blind labels. Until it is scored, the prompts are kept by explicit decision.

## References

* `zettel/library/reading_mode.py` — `effective_nature`, `effective_reading_mode`
* `zettel/extractor.py` — `Prompt1Set`, `PROMPT1_BY_MODE`, `_reading_modes`
* `zettel/connector/note.py` — `ConnectSession.prompt_for`; `zettel/connector/run.py` — `_reflective_prompt`
* `zettel/schemas.py` — `REFLECTION_KINDS`, `PermanentNoteCandidate.reflection_kind` / `characters`
* `zettel/retrieval.py` — `Retriever.search_notes(exclude_source_ids=)`
* `zettel/article_graph/search.py` — `excluded_sources`, `drop_sources`; `zettel/article.py` — `literary_mention`
* `prompts/literature_note_reflective.md`, `prompts/permanent_note_reflective.md`, `evals/preregistration/396-leitura-reflexiva.md`
* `tests/test_library_reflective.py`, `tests/test_library_writing.py`
