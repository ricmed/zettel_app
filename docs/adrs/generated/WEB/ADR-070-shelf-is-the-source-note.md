# ADR-070: The Shelf Is the Source Note

**Status:** Accepted
**Date:** 2026-11-16
**Depends on:** [ADR-068: The Estante, Books as LIV Notes With a SQLite Mirror](./ADR-068-estante-books-as-liv-notes.md)
**Related to:** [ADR-069: Prompts Chosen by Reading Mode](../EXTRACT/ADR-069-reflective-prompts-by-reading-mode.md)

## Context

ADR-068 stored the reader's card in its own note and mirrored it in two tables. The bibliography already lived on the source note. Two files for one work drifted: a harvest rewrote the source and left the card behind, and a reset kept the card but minted a new source id.

## Decision

The source note is the shelf card.

- Reader fields (code, format, status, readings, genres, ratings, cover, summary, impressions) live in the source frontmatter and body. Title, authors, ISBN and the bibliography stay once.
- Harvest and rebuild rewrite the bibliographic header and put the author's sections back. They rebuild the reading-notes block from the source's literature and permanent notes.
- The shelf lists `livro`, `artigo_periodico`, `artigo_internet`, `tese`, `material_curso`, `relatorio` and `anais_evento`. `capitulo_livro` is harvested and is not listed. Two languages of one work stay two sources. `work_id` is out of this change.
- `init --reset` keeps every note with `type: source`. The next harvest of the same file reuses that source id, by file checksum or by a unique ISBN.
- The mirror is columns on `sources`, including readings. There is no `book_id`. Opening the database drops the old shelf tables only after `zettel migrate-acervo` has marked the move.
- `/books/{source_id}` is the card. `/sources/{source_id}` redirects there. Registering a paper book creates the source note immediately.
- A DOI in **Adicionar** is read from Crossref and shown for confirmation. Nothing is written on that GET. **Completar dados** fills only empty fields.
- `zettel migrate-acervo` copies each old card onto its source, or creates a manual source when the card had none. It does not overwrite a title, the authors, the ISBN or the bibliography. An empty reader field receives the old value; a filled field that differs is reported and that old card stays until `zettel migrate-acervo --accept`. If a copied value did not land, nothing is deleted.

## Consequences

Citation, the `[[SRC - ...]]` wikilink and extract keep the same source id. A later harvest does not erase `## Resumo` or `## Impressões`. A new vault does not create the old shelf folder.
