# ADR-071: Editions of the Same Work Share a Declared work_id

**Status:** Accepted
**Date:** 2026-11-17
**Depends on:** [ADR-070: The Shelf Is the Source Note](./ADR-070-shelf-is-the-source-note.md), [ADR-045: Cross-Source Overlap Is Corroboration, Not Duplication](../REVIEW/ADR-045-cross-source-overlap-is-corroboration.md)
**Related to:** [ADR-058: Chroma Distance Is Cosine](../INFRA/ADR-058-chroma-distance-is-cosine.md)

## Context

ADR-070 kept two languages of one work as two source notes and left `work_id` out. The English PDF and the Portuguese print are different editions: ISBN, pages and text do not merge, and each note stays cited on the edition of its passage. What was missing is a way to say they are the same work. Title, ISBN and Wikidata do not decide that. *Building Applications* and *Construindo Aplicações* do not match, and a guess would join different works.

## Decision

A person declares the link. Nothing is grouped by title, ISBN or Wikidata.

- `work_id` is a ULID on the source frontmatter, in `READER_FIELDS`. Empty means the source stands alone. Harvest already keeps a reader field it would overwrite with a blank, so the next harvest does not drop the link.
- The mirror column is `sources.work_id`, added with the other reader columns when an existing database opens. A blank in the file becomes `NULL`.
- On `/books/{source_id}`, **Mesma obra** lists the other editions and a selector of the rest of the shelf. The POST takes the `estante` lock and checks `version` (409 when the file changed). Two ungrouped sources receive a new id. One grouped source adopts the other. Two groups merge into the target's id, and the selector shows the target's other editions before submit. Unlinking removes this source; if one remains, its `work_id` is cleared too. `library_health` warns when a `work_id` has a single source.
- Each card and its `auto-book-notes` block list **Desta edição**, then each sibling with its label (title and format; ISBN when the titles match). The note keeps one `source_id`. Rewriting one member's block rewrites the siblings'.
- `select_corroborating_ids` skips a candidate whose source is a sibling. `supports` and `related` that the model proposes stay. Linking deletes derived `corroborates` between the group's sources. Unlinking does not recreate them. The rebuild that already calls the function follows the same rule.
- Before a reading note or a ZTL is written, the pasted passage (the passage already stored on the LIT, for **Gerar ZTL**) is one `RETRIEVAL_QUERY` embedding against the linked edition's chunks (`where` on `source_id`). The title, the thesis and the summary are not the query. With no passage, the page asks for it. A hit that clears the existing note relevance floor is **provável**. The nearest hit below that floor is shown too. The first POST does not write; confirmation does, on the edition being read. With no `work_id`, it writes immediately. If the embedding does not answer, the page says it did not check and writes only with the same confirmation. Nothing is discarded on its own. The citation stays on the edition being read. Pipeline dedupe stays per `source_id`. `ask` and the manuscript do not change.

## Consequences

Two editions list the same notes, grouped by edition, and do not corroborate each other. A translated passage can sit below a floor measured in one language, so the warning shows that neighbour instead of hiding it. There is no new config key and no new prompt.

## References

* `zettel/library/works.py` (`link_editions`, `unlink_edition`, `sibling_source_ids`, `edition_label`)
* `zettel/library/edition_match.py` (`compare_excerpt`)
* `zettel/library/source_body.py` (`READER_FIELDS`, `merge_frontmatter`)
* `zettel/library/linking.py` (`source_notes_lines`, `note_groups`)
* `zettel/state/schema.py` (`SOURCE_READER_COLUMNS`)
* `zettel/connector/links.py` (`select_corroborating_ids`)
* `zettel/index.py` (`find_similar_chunks`)
* `zettel/web/library.py`
