"""Aviso antes de gravar uma nota numa edição que tem irmãs (ADR-071).

O que se compara é o trecho colado, uma embedding ``RETRIEVAL_QUERY``, contra os
chunks da outra edição. Título, tese e resumo não entram. Quem passa do piso de
relevância das notas é provável; a passagem mais próxima abaixo do piso também
aparece, porque um par traduzido pode ficar aquém de um piso medido na mesma
língua. Não há chave nova de configuração.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from zettel.library.works import labels_for, members, sibling_source_ids

logger = logging.getLogger(__name__)

# Uma consulta só. O teto alcança a vizinha abaixo do piso sem uma chave nova.
_NEIGHBORS = 15
_MAX_PROBABLE = 5


@dataclass(frozen=True)
class SimilarPassage:
    title: str
    edition: str
    href: str
    probable: bool
    chunk_id: str


@dataclass(frozen=True)
class EditionMatch:
    """O resultado da comparação. ``alone`` grava direto, como a ficha sem obra."""

    alone: bool = False
    needs_excerpt: bool = False
    unchecked: bool = False
    passages: tuple[SimilarPassage, ...] = ()

    @property
    def clear(self) -> bool:
        """Nada a avisar: pode gravar neste POST."""
        return self.alone or not (self.needs_excerpt or self.unchecked or self.passages)


def compare_excerpt(cfg: object, db: object, source_id: str, excerpt: str) -> EditionMatch:
    """Compara ``excerpt`` com os chunks das edições irmãs.

    Sem irmãs, ``alone``. Sem trecho, ``needs_excerpt`` e o índice não abre.
    Embedding indisponível ou uma falha na busca: ``unchecked``.
    """
    siblings = sibling_source_ids(db, source_id)
    if not siblings:
        return EditionMatch(alone=True)
    text = (excerpt or "").strip()
    if not text:
        return EditionMatch(needs_excerpt=True)
    index = _open_index(cfg)
    if index is None:
        return EditionMatch(unchecked=True)
    try:
        return _search(cfg, db, source_id, text, siblings, index)
    except Exception as exc:
        logger.warning("Nao comparei a edicao vinculada de %s: %s", source_id, exc)
        return EditionMatch(unchecked=True)
    finally:
        index.close()


def _open_index(cfg: object):
    from zettel.credentials import embedding_ready
    from zettel.index import VectorIndex, index_kwargs

    if not embedding_ready(cfg):
        return None
    try:
        return VectorIndex(**index_kwargs(cfg))
    except Exception as exc:
        logger.warning("Indice indisponivel para comparar edicoes: %s", exc)
        return None


def _search(
    cfg: object,
    db: object,
    source_id: str,
    excerpt: str,
    siblings: list[str],
    index,
) -> EditionMatch:
    floor = cfg.retrieval.relevance_floor
    minimum = floor.min_vector_similarity
    scored: dict[str, float] = {}
    for sibling in siblings:
        for hit in index.find_similar_chunks([excerpt], _NEIGHBORS, where={"source_id": sibling}):
            distance = hit.get("distance")
            if distance is None:
                continue
            chunk_id = str(hit.get("id") or "")
            if not chunk_id:
                continue
            similarity = float(index.to_cosine(float(distance)))
            previous = scored.get(chunk_id)
            if previous is None or similarity > previous:
                scored[chunk_id] = similarity
    if not scored:
        return EditionMatch()
    ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    if floor.enabled:
        probable_ids = [cid for cid, sim in ranked if sim >= minimum][:_MAX_PROBABLE]
        below = next((item for item in ranked if item[1] < minimum), None)
    else:
        probable_ids = [cid for cid, _sim in ranked][:_MAX_PROBABLE]
        below = None
    chosen: list[tuple[str, bool]] = [(cid, True) for cid in probable_ids]
    if below is not None and below[0] not in probable_ids:
        chosen.append((below[0], False))
    labels = _edition_labels(db, source_id)
    passages: list[SimilarPassage] = []
    seen: set[str] = set()
    for chunk_id, probable in chosen:
        for passage in _passages(db, chunk_id, labels, probable):
            if passage.href in seen:
                continue
            seen.add(passage.href)
            passages.append(passage)
    return EditionMatch(passages=tuple(passages))


def _edition_labels(db: object, source_id: str) -> dict[str, str]:
    row = db.get_source(source_id) or {}
    group = members(db, str(row.get("work_id") or ""))
    return labels_for(group)


def _passages(
    db: object,
    chunk_id: str,
    labels: dict[str, str],
    probable: bool,
) -> list[SimilarPassage]:
    from zettel.vault import literature_chunk_wikilink_for_row

    chunk = db.get_chunk(chunk_id)
    if not chunk:
        return []
    source_id = str(chunk.get("source_id") or "")
    edition = labels.get(source_id) or source_id
    found: list[SimilarPassage] = []
    if chunk.get("status") in ("approved", "persisted"):
        source = db.get_source(source_id) or {}
        link = literature_chunk_wikilink_for_row(
            str(source.get("citekey") or ""), chunk, with_alias=True
        )
        title = link.split("|", 1)[1].rstrip("]") if "|" in link else link.strip("[]")
        found.append(
            SimilarPassage(
                title=title or "Nota de literatura",
                edition=edition,
                href=f"/books/{source_id}#notas",
                probable=probable,
                chunk_id=chunk_id,
            )
        )
    for note in db.get_notes_for_chunk(chunk_id):
        found.append(
            SimilarPassage(
                title=str(note.get("title") or note["note_id"]),
                edition=edition,
                href=f"/notes/{note['note_id']}",
                probable=probable,
                chunk_id=chunk_id,
            )
        )
    return found
