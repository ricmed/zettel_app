# ADR-035: `zettel skill` Projects a Vault Slice as a Flat Agent Skill

**Status**: Accepted (2026-09-03)

**Depends on:**
- [ADR-026: Typer and Rich as CLI Framework](./ADR-026-typer-rich-cli-framework.md)
- [ADR-032: CLI as Python Package](./ADR-032-cli-as-python-package.md)
- [ADR-034: Author-Judgement Fields on the Candidate, Optional by Construction](../EXTRACT/ADR-034-optional-author-judgement-fields.md)

**Related to:**
- [ADR-003: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)
- [ADR-015: Granular Per-Chunk Literature Notes with Readable Filenames](../EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md)
- [ADR-016: Post-Approval Concept Deduplication Timing](../REVIEW/ADR-016-post-approval-concept-deduplication-timing.md)
- [ADR-019: Taxonomy-First MOC Clustering](../GARDEN/ADR-019-taxonomy-first-moc-clustering.md)

## Context and Problem Statement

The vault is reachable through `zettel ask` and the web UI, both of which assume a person driving a session. A coding agent in the middle of a task cannot use either: it has no way to load a compact index of what the vault knows and then open exactly the note it needs.

Agent Skills solve that shape of problem — a small always-loaded `SKILL.md` plus files opened on demand. The question is not whether to adopt the format but what the Zettel version of it *is*. The nearby prior art (`book-to-skill`) compiles a book straight into a skill with an LLM-driven generator. Copying that would mean the agent generating notes, which breaks the deterministic cache, the tests, and the human approval gate that is the point of the project.

## Decision Drivers

* The review gate is load-bearing: a skill must contain only what a human already approved, and generating anything new at export time would route around it.
* The export must be reproducible — same vault state, same bytes — so a regenerated pack diffs cleanly and tests can assert on it.
* `SKILL.md` is loaded into every session the skill triggers; its size is a recurring cost, unlike the per-note files.
* A pack may be published (shared repo, `.claude/skills/` committed elsewhere), so copying source excerpts by default is a copyright problem, while the citekey and locator must survive for the academic contract.
* The progressive-disclosure literature treats a **flat** pack as the default to beat; nesting has to earn its place with evidence, which does not exist yet.
* The retrieval stack (RRF, floor, graph) is not being replaced. The pack is a second *surface*, not a second *engine*.

## Considered Options

* Generate the skill with an LLM at export time, `book-to-skill` style.
* Project deterministically from what SQLite and the vault already hold.
* Export a nested pack (a parent `SKILL.md` with child skills per chapter or MOC).
* Budget the whole `SKILL.md`, versus budgeting only the Core section.

## Decision Outcome

**`zettel skill` is a deterministic projection: no LLM call, no new state, no writes to SQLite or Chroma.** Everything in a pack was already approved by `review`, linked by `connect` or clustered by `garden`. That keeps the export honest — it cannot introduce a claim the vault does not make — and makes it cheap enough to re-run on every change.

The pack is **flat**: `SKILL.md`, `notes/<slug>-<id6>.md`, `cheatsheet.md`, `glossary.md`. One routing level, no child skills. The note graph already encodes the structure a nested pack would re-express, and nesting spends always-loaded context on navigation.

**Three mutually exclusive selectors** — `--source-id`, `--moc-id`, `--topic` — because those are the three slices the vault actually knows how to name. A topic matching more than one MOC topic is an error listing the candidates rather than a silent union: quietly merging two categories produces a pack whose description is false.

**Only the Core section is budgeted** against `SKILL_TOKEN_BUDGET` (4000 tokens, chars/4). The Topic Index and Note Index are the routing table; dropping rows from them would make notes unreachable, which is a worse failure than a longer file. Theses that do not fit the Core are front-loaded out, not lost — they stay one file-open away through the Note Index, and the CLI prints the resulting estimate so an oversized slice shows up as a number rather than a surprise.

**Source excerpts are excluded by default** (`--include-excerpts` opts in). The `auto-source-excerpt` block is replaced by a marker, while citekey, locator, thesis and wikilinks stay. A pack is publishable by default and complete on request.

**Permanent notes are preferred; approved granular LIT is the fallback, and only for a source slice.** A MOC or a topic *is* a set of permanent notes, so an empty one means the slice is empty — reaching for literature notes there would silently export material from a different scope.

`build_term_map` lives in its own module (`zettel/topic_index.py`) rather than inside the exporter. The topic index is wanted on two surfaces (this pack and, later, the vault's own literature/MOC index blocks), and two divergent implementations of "what terms does this note answer to" is exactly the drift worth preventing up front. Terms come from named frameworks first (the author's own vocabulary), then tags, and fall back to the thesis head only when a note has neither.

### Positive Consequences

* An agent can carry a reviewed slice of the vault into a coding session and open one note at a time.
* Regeneration is a clean diff: ordering is by `(relevance + weighted degree, note_id)`, so nothing moves without a reason.
* The cheatsheet is real judgement, not inference: `decision_rules` and `anti_patterns` (ADR-034), `## Limites`, and `contradicts` edges from the graph — the tension embeddings systematically miss.
* No LLM cost, no cache invalidation, no new rows.

### Negative Consequences

* A pack is a snapshot. Nothing marks it stale when the vault moves on; the operator re-runs with `--overwrite`.
* `--overwrite` clears the destination, so hand edits inside a pack directory are lost. That is deliberate — a pack is generated output, not a vault note — but it is a sharper edge than the vault's managed-block discipline.
* A slice large enough to push the two indexes past 4000 tokens produces a `SKILL.md` above the target. The remedy is a narrower selector, not truncation, and the CLI reports the estimate rather than silently deciding.
* Ranking uses `relevance_score + weighted degree` on raw scales. It is a reasonable ordering, not a calibrated one; a note-poor pack and a note-rich pack are not comparable on this number.

## Pros and Cons of the Options

### Deterministic projection (chosen)

* Good, because everything exported already passed the human gate.
* Good, because identical input yields identical bytes, which is testable and diffable.
* Good, because it costs nothing to re-run.
* Bad, because the pack's prose is only as good as the notes; the export cannot rescue a thin thesis.

### LLM generation at export time

* Good, because it could synthesize a smoother narrative than concatenated theses.
* Bad, because it would put generated claims into a pack that is supposed to represent reviewed material.
* Bad, because it breaks reproducibility, invalidates the deterministic cache, and makes the output untestable.

### Nested pack with child skills

* Good, because a very large corpus might route better with a second level.
* Bad, because there is no evidence for it, and the referenced research treats flat as the baseline to beat.
* Bad, because the parent index would spend always-loaded context restating structure the graph already holds.

### Budgeting the whole `SKILL.md`

* Good, because the always-loaded file would have a hard ceiling.
* Bad, because the ceiling would be reached by dropping index rows, making notes unreachable — the pack would silently lose material rather than get longer.

## Consequences

`zettel skill` is CLI-only, like `ask` and `article`: it is not exposed in the web UI, and `web_app.py` is untouched. It lives in its own command module (`zettel/cli/export.py`) under the ADR-032 package layout — the third way to consume the vault, next to `qa.py` (`ask`) and `writing.py` (`article`).

The default destination is `<vault>/.claude/skills/<slug>/`, which Claude Code discovers project-locally. `--out` names the directory that *holds* packs, so the pack always lands at `<out>/<slug>` and two exports never collide.

`StateDB.get_concepts_for_notes` was added to fetch the originating candidate for a set of notes in one query — used here for `relevance_score` and as the fallback source of judgement fields on notes written before ADR-034.

Future work this ADR deliberately leaves out: fold-in updates of an existing pack, publishing (`gh repo create`, `npx skills add`), a library-level index across packs, and re-generating theses with an LLM.

## References

* `zettel/skill_export.py` — `run_skill_export`, `resolve_slice`, `load_notes`, `render_skill_md`, `render_cheatsheet`, `write_pack`
* `zettel/topic_index.py` — `build_term_map`, `TermSource`, `TermEntry` (shared term extraction)
* `zettel/cli/export.py` — the `skill` command (registered in `zettel/cli/__init__.py`)
* `zettel/state.py` — `get_concepts_for_notes`
* `tests/test_skill_export.py` — selectors, layout, budget, excerpt policy, determinism, LIT fallback
