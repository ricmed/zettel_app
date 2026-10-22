"""What Prompt 2 sees besides the candidate: RAG context, distant analogies, images."""

from __future__ import annotations

import logging
import random

import numpy as np

from zettel.config import AppConfig
from zettel.hashing import sha256_hex
from zettel.index import VectorIndex
from zettel.retrieval import RetrievedNote, Retriever
from zettel.state import StateDB
from zettel.vault import permanent_wikilink

logger = logging.getLogger(__name__)

# ``(category label vectors, note_id -> category)``, built once per connect run.
Taxonomy = tuple[dict[str, np.ndarray], dict[str, str]]

_NO_NOTES = "Nenhuma nota existente encontrada."


# ── RAG context ───────────────────────────────────────────────────────


def _rag_note_line(db: StateDB, n: RetrievedNote, note_chars: int, extra: str = "") -> str:
    """Header line (id, wikilink, tags or relation) and the note's content, indented.

    ``document`` is ``note_content`` (#211). ``note_chars`` is
    ``linking.rag_note_chars``: an excerpt at its historical 150, a ceiling above
    any real note (#212).
    """
    title = n.title or n.metadata.get("title", "Sem titulo")
    row = db.get_note(n.note_id)
    wiki = permanent_wikilink(n.note_id, title, path=row.get("path") if row else None)
    suffix = extra or f" (tags: {n.metadata.get('tags', '')})"
    header = f"- note_id: {n.note_id} | **{wiki}**{suffix}"
    content = (n.document or "").strip()
    if len(content) > note_chars:
        content = content[:note_chars].rstrip() + "..."
    if not content:
        return header
    return header + "\n" + "\n".join(f"  {line}" if line else "" for line in content.splitlines())


def order_group(notes: list[RetrievedNote], key: str | None) -> list[RetrievedNote]:
    """Return ``notes`` in score order, or shuffled with a seed derived from ``key``.

    The same key always yields the same order. ``None`` keeps the order the
    retriever produced. Connect does not pass a key until the #286 rule says so.
    """
    if not key or len(notes) < 2:
        return list(notes)
    ordered = list(notes)
    # Deterministic order for a measurement, not a secret.
    random.Random(sha256_hex(key)).shuffle(ordered)  # noqa: S311
    return ordered


def build_rag_context(
    db: StateDB,
    similar_notes: list[RetrievedNote],
    distant_notes: list[RetrievedNote] | None = None,
    *,
    note_chars: int,
    order_key: str | None = None,
) -> str:
    """Build RAG context from retrieved notes, split by provenance.

    Search seeds (hop 0), graph neighbours (hop >= 1) and distant analogies
    (other taxonomy bucket) are separate headings so the LLM can weigh each
    differently. Distant hits are suggestions, not hard edges.
    """
    distant_notes = distant_notes or []
    if not similar_notes and not distant_notes:
        return _NO_NOTES

    embedding_hits = order_group(
        [n for n in similar_notes if n.hop == 0],
        f"{order_key}:embedding" if order_key else None,
    )
    graph_hits = order_group(
        [n for n in similar_notes if n.hop >= 1],
        f"{order_key}:graph" if order_key else None,
    )
    parts: list[str] = []

    if embedding_hits:
        parts.append("### Similares por embedding")
        parts.extend(_rag_note_line(db, n, note_chars) for n in embedding_hits)

    if graph_hits:
        parts.extend(["", "### Vizinhas por conexao no grafo"])
        for n in graph_hits:
            last_hop = n.via[-1] if n.via else {}
            rel = last_hop.get("relation_type", "related")
            anchor = last_hop.get("from", "")
            anchor_txt = f" a partir de note_id: {anchor}" if anchor else ""
            parts.append(_rag_note_line(db, n, note_chars, extra=f" (relacao: {rel}{anchor_txt})"))

    if distant_notes:
        if parts:
            parts.append("")
        parts.append("### Analogias distantes (outro dominio)")
        ordered_distant = order_group(distant_notes, f"{order_key}:distant" if order_key else None)
        parts.extend(
            _rag_note_line(db, n, note_chars, extra=" (analogia: outro bucket taxonomico)")
            for n in ordered_distant
        )

    return "\n".join(parts)


# ── Distant analogies ────────────────────────────────────────────────


def load_connect_taxonomy(cfg: AppConfig, idx: VectorIndex) -> Taxonomy:
    """Embed category labels once per connect run; map note_id -> category."""
    from zettel.gardener_assign import (
        assign_notes_to_categories,
        build_embeddings_by_id,
        category_pairs,
        embed_category_labels,
    )

    # Isolated from #295 on purpose: the analogies keep the plain label and no
    # threshold whatever gardener.category_label_mode / assign_min_similarity
    # say, so the attribution measurement cannot move connect's buckets.
    pairs = category_pairs(cfg.gardener)
    if not pairs:
        return {}, {}
    try:
        cat_vectors = embed_category_labels(
            idx, pairs, cfg.domain.name, cfg.gardener.category_label_template
        )
    except Exception as e:
        from zettel.llm import _raise_unavailable, is_llm_unavailable

        if is_llm_unavailable(e):
            _raise_unavailable(e, phase="embedding")
        logger.warning("Rotulos de categoria indisponiveis para analogias distantes: %s", e)
        return {}, {}
    ids, embeddings = idx.get_all_permanent_embeddings()
    if embeddings is None or not ids:
        return cat_vectors, {}
    buckets = assign_notes_to_categories(ids, build_embeddings_by_id(ids, embeddings), cat_vectors)
    note_to_cat = {nid: cat for cat, nids in buckets.items() for nid in nids}
    return cat_vectors, note_to_cat


def search_distant_analogies(
    cfg: AppConfig,
    idx: VectorIndex,
    retriever: Retriever,
    query_text: str,
    note_id: str,
    similar: list[RetrievedNote],
    taxonomy: Taxonomy | None,
) -> list[RetrievedNote]:
    """Notes outside the candidate's taxonomy bucket, above the local floor (ADR-043)."""
    if cfg.linking.distant_analogy_topk <= 0:
        return []
    cat_vectors, note_to_cat = taxonomy or ({}, {})
    if not cat_vectors:
        return []
    try:
        query_vec = idx.embed_texts([query_text], label="analogia-categoria")[0]
    except Exception as e:
        from zettel.llm import _raise_unavailable, is_llm_unavailable

        if is_llm_unavailable(e):
            _raise_unavailable(e, phase="embedding")
        logger.warning("Nao foi possivel embeddar o candidato para analogia distante: %s", e)
        return []
    from zettel.gardener_assign import assign_vector_to_category

    cand_cat = assign_vector_to_category(query_vec, cat_vectors)
    if not cand_cat:
        return []
    same_bucket = {nid for nid, cat in note_to_cat.items() if cat == cand_cat}
    already = {n.note_id for n in similar} | {note_id} | same_bucket
    return retriever.search_distant_analogies(
        query_text,
        exclude_id=note_id,
        exclude_ids=already,
        topk=cfg.linking.distant_analogy_topk,
        min_vector_similarity=cfg.linking.distant_analogy_min_similarity,
    )


# ── Images ────────────────────────────────────────────────────────────


def fallback_image_ids(db: StateDB, source_id: str, chunk_row: dict | None) -> list[str]:
    """Image ids referenced by path in the chunk text (when the LLM listed none)."""
    if not chunk_row:
        return []
    from zettel.assets import asset_ids_in_text

    return asset_ids_in_text(db, source_id, chunk_row.get("text") or "")


def resolve_images(db: StateDB, image_ids: list[str]) -> list[dict]:
    """Resolve asset ids into ``{path, description}`` for the ZTL body."""
    resolved: list[dict] = []
    for aid in image_ids:
        asset = db.get_asset(aid)
        if asset and asset.get("path"):
            resolved.append({"path": asset["path"], "description": asset.get("description") or ""})
    return resolved


def images_context(db: StateDB, image_ids: list[str]) -> str:
    """Describe relevant images for Prompt 2 (empty string when none)."""
    lines = [
        f"- {aid}: {asset.get('description') or '(sem descricao)'}"
        for aid in image_ids
        if (asset := db.get_asset(aid))
    ]
    if not lines:
        return ""
    return (
        "Figuras essenciais ao conceito (ja serao embutidas na nota; use-as na "
        "definicao/exemplo quando iluminarem o mecanismo):\n" + "\n".join(lines)
    )
