# ADR-067: The Mesa, a Writing Desk Built From Permanent Notes

**Status:** Accepted
**Date:** 2026-11-08
**Depends on:** [ADR-022: FastAPI Server-Rendered Web Interface](./ADR-022-fastapi-server-rendered-jinja2.md), [ADR-051: Citation Provenance on the Permanent Note](../QA-WRITING/ADR-051-citation-provenance-on-permanent-note.md), [ADR-066: Obsidian as an Offline Authoring Surface](../MANUAL/ADR-066-obsidian-offline-authoring.md)
**Related to:** [ADR-028: LangGraph StateGraph for Article Orchestration](../QA-WRITING/ADR-028-langgraph-stategraph-article-orchestration.md), [ADR-056: Web Acervo for Consult and Produce](./ADR-056-web-acervo-consult-and-produce.md), [ADR-060: MOCs and Hubs Are Entry Points](../GARDEN/ADR-060-mocs-and-hubs-are-entry-points.md)

## Context and Problem Statement

The pipeline writes self-contained permanent notes, and the vault can be asked (`ask`), catalogued (`catalog`) or turned into an LLM-written text (`article`). The step in the middle of Luhmann's method had no place: take notes out of the archive, put them on the desk, put them in order, and write from that order. A researcher looking at "machine learning for credit scoring" could search, but could not keep what was found, arrange it, see what the graph says about the arrangement, or take it out of the app with its citations and figures.

The web also hid what the notes already carried. Figures did not render (no route served `90_Assets`, and the sanitizer removed `img`). Connections had no direction and showed rejected edges. A MOC link was plain text. The search in `/notes` was lexical only.

## Decision Drivers

* The author structures, the machine assembles. The text should be the notes in the author's order, with gaps marked where the author still has to write. It should not be an LLM rewrite.
* The structure must survive the tools around it: be editable in Obsidian, survive `zettel init --reset`, and be read without the app.
* No canvas engine under the CSP of [ADR-022](./ADR-022-fastapi-server-rendered-jinja2.md). Obsidian already has one.
* "Conversing with the archive" means following typed links and being surprised. That is the graph `connect` already builds, so it needs no model call.
* What leaves the app is a manuscript. It carries citations with page, figures, and one reference per source. It carries no wikilinks.

## Considered Options

1. A project note in the vault, a linear outline on the web, suggestions from the graph, export through Pandoc (chosen).
2. Project rows in SQLite.
3. A free 2D canvas in the browser.
4. Feed the selection to `article` and let the model write the text.

## Decision Outcome

Chosen option 1.

* **The project is a vault note.** It lives at `50_Projetos/PRJ - <ULID> - <slug>.md` with `type: project` and `origin: manual`, so `init --reset` keeps it. `sync-manual` does not scan the folder, and there is no SQLite row.
* **The body is a line grammar** (`zettel/project_file.py`):
  * a level-2 heading outside a fence opens a section;
  * a list item whose only content is a permanent-note link is a card;
  * indented lines right under a card are its annotation;
  * every other line is the author's text, kept verbatim.

  `render_body(parse_body(t)) == t` holds for any text, which Hypothesis checks. A project edited by hand never loses a character.
* **Every web edit is a form POST.** It carries CSRF, runs under the operation lock, and sends the file's version (a hash of its bytes). A file changed since the page was rendered, for example in Obsidian, gets a 409, not an overwrite. Adding cards only appends, so it skips the version check.
* **Cards land in "Para organizar".** That is where cards added from the note detail, the search by meaning, a MOC, the catalog or an ask go.
* **Drag and drop submits the same form as the arrows.** It is a progressive enhancement in `static/project-board.js`.
* **The conversation comes from the graph** (`zettel/project_suggest.py`). It reads only traversable edges and calls no LLM. It lists:
  * tensions (`contradicts`);
  * the one-hop frontier ranked by edge weight;
  * corroborations from another source;
  * cards with no edge inside their section;
  * sections that lean on one source.

  "Search for this section" is the one paid call: a hybrid search over the section title and its cards' theses.
* **The manuscript is built by code** (`zettel/manuscript.py`).
  * Each card gives its thesis, with the note's own citation placed before the final period, and its definition.
  * The literal passage, the limits and an appendix are optional.
  * Figures are numbered in reading order and copied once, with a caption and a "Fonte: adaptado de" credit.
  * Gaps (introduction, section opening, the transition between two adjacent cards, conclusion) become `[Complementar: ...]` in a Pandoc div with the `Complementar` style.
  * `## Referências` closes the text. It holds every source behind an exported card, each once, in alphabetical order with accents and case folded (`bibliography.reference_entries`). The text is the stored ABNT reference, else one rebuilt from `bibliography_json`, else a bare fallback that the export reports as a warning.
* **The exported files** (`zettel/manuscript_export.py`):
  * The Markdown bundle holds `manuscrito.md`, `figuras/` and `referencias.bib`.
  * The DOCX is that Markdown through the Pandoc that `pypandoc-binary` ships, with `zettel/resources/manuscript-reference.docx` as the reference document (`scripts/build_manuscript_reference.py` rebuilds it). TeX becomes Word equations.
  * The canvas is a one-way JSON Canvas beside the project: sections become groups, cards become file nodes, the author's text becomes text nodes, and walkable edges become arrows.
* **The reading fixes on the web come with it** (see the ADR-022 addendum): `/assets/{name}`, images in rendered notes, MOC links, directed connections and the source block. `/notes` also gains a search by meaning and a tag filter.

### Positive Consequences

* A text starts as the author's argument made of cited notes. It is not a model's paraphrase of them.
* The project is a plain Markdown file. Obsidian, git and a text editor can all read it.
* Exporting costs nothing and needs no credential. Only the section search and the search by meaning spend an embedding.
* `article` now shares the reference list. Its references are deduplicated and sorted the same way.

### Negative Consequences

* The grammar is strict about one thing: a card is a list item with nothing but the link. A link written inside a sentence stays text, so it is cited only as prose, never as a card.
* The ULIDs of pipeline notes change after `init --reset` and a new run. A project kept across a reset shows those cards as missing notes.
* `pypandoc-binary` adds about 30 MB to an install.
* The canvas is one-way. Changes made in Obsidian's Canvas do not come back to the project.

## References

* `zettel/project_file.py` — `parse_body`, `render_body` and the edits
* `zettel/projects.py` — `load_project`, `save_project` (version check), `card_views`
* `zettel/project_suggest.py` — `converse`, `section_query`
* `zettel/manuscript.py` — `build_manuscript`
* `zettel/manuscript_export.py` — `write_bundle`, `bibtex_entry`, `to_docx`, `to_canvas`
* `zettel/bibliography.py` — `source_reference`, `reference_entries`
* `zettel/web/projects.py` — the `/projetos` routes
* `zettel/cli/export.py` — `manuscript`
* `tests/test_project_file.py`, `tests/test_manuscript.py`, `tests/test_manuscript_export.py`, `tests/test_web_projects.py`
