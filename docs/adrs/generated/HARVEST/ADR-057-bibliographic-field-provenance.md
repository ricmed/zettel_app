# ADR-057: Precedence and provenance of bibliographic fields

**Status:** Accepted
**Date:** 2026-10-17
**Related to:** [ADR-046](./ADR-046-bibliographic-duplicate-layers.md) — layers 3 and 4 read these fields; [ADR-045](../LLM/ADR-045-fail-fast-on-llm-unavailability.md) — `LLMUnavailableError` still aborts

## Context

Harvest builds a bibliographic record from the file, then optionally from a model, then asks the user when the record looks incomplete. Three defects stacked. The model overwrote frontmatter the user had already written, because `_merge_biblio` let any non-empty model value win. `confidence` rose to 0.85 whenever the required fields were filled, so a non-interactive run (`--yes`, the web worker, `run-all`) accepted a complete hallucination. The document-type heuristic matched substrings, so `Discurso do método.pdf` became `material_curso` and a filename containing `hipótese` became `tese`.

## Decision

Each filled field records one origin in `BibliographicMetadata.provenance`, persisted inside `bibliography_json`:

| origin | source |
|---|---|
| `user` | Markdown frontmatter, or a value the person confirmed in the CLI or the web review |
| `file_metadata` | title, authors or year read from the PDF's own document origin |
| `llm` | the harvest bibliographic model |
| `heuristic` | filename, body cues, a URL found in the text |

Precedence is `user`, then `file_metadata`, then `llm`, then `heuristic`. The model fills empty fields and may replace a heuristic. It does not replace `user` or `file_metadata`. The manual note form keeps `prefer_seed`: every non-empty seed field stays, whatever its origin.

`confidence` is still a completeness score, including the bump when the required fields are present. Non-interactive acceptance adds a second rule: if every required field, including `document_type`, is `llm`, the file is not accepted. `--skip-biblio` still continues with the partial record. Interactive review shows the origin next to each field (CLI table column, web label). Confirming a field in either surface marks it `user`.

The type heuristic matches whole words (`(?<!\w)…(?!\w)`), not substrings. A failure while reading the PDF origin is logged at warning; it is not swallowed.

An authoritative catalog (Crossref for a DOI, Open Library for an ISBN) is not consulted here. That would add a network call and an offline policy, and it is left for a later issue. Until then there is no origin between `user` and `llm` except `file_metadata`.

## Consequences

* A Markdown file whose frontmatter already has authors and year keeps those values when the model returns different ones.
* A record the model filled by itself no longer passes `--yes` just because every box is full.
* `Discurso` is not classified as course material, and `hipótese` is not classified as a thesis, by the filename heuristic.
* Already stored `bibliography_json` blobs have no `provenance` key. They are not rewritten. A missing origin is not treated as `llm`, so an old complete source is not suddenly rejected on a later comparison. New harvests write the key.
