"""The Gardener — clustering, topic extraction, MOC generation.

Uses UMAP + HDBSCAN for clustering and TF-IDF for topic extraction.
Falls back gracefully if dependencies are missing.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
from ulid import ULID

from zettel.config import AppConfig
from zettel.hashing import sha256_hex
from zettel.index import VectorIndex
from zettel.schemas import MOCGenerationOutput
from zettel.state import StateDB
from zettel.vault import (
    note_filename,
    safe_write_note,
    safe_update_managed_blocks,
    _slug,
)

logger = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────


def run_garden(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> list[str]:
    """Cluster permanent notes and generate/update MOCs. Returns moc_ids."""
    note_count = idx.count_permanent_notes()
    if note_count < cfg.gardener.min_cluster_size:
        logger.info("Poucas notas para clusterização (%d < %d)", note_count, cfg.gardener.min_cluster_size)
        return []

    ids, embeddings = idx.get_all_permanent_embeddings()
    if embeddings is None or len(embeddings) < cfg.gardener.min_cluster_size:
        logger.info("Embeddings insuficientes para clusterização")
        return []

    embeddings_array = np.array(embeddings)

    # Cluster
    clusters = _cluster_embeddings(embeddings_array, ids, cfg.gardener.min_cluster_size)
    if not clusters:
        logger.info("Nenhum cluster encontrado")
        return []

    logger.info("Clusters encontrados: %d", len(clusters))

    # Get note details for each cluster
    llm = _get_llm(cfg)
    moc_ids: list[str] = []

    for cluster_ids in clusters:
        if len(cluster_ids) < cfg.gardener.min_notes_for_moc:
            continue
        moc_id = _generate_moc(cfg, db, idx, llm, cluster_ids)
        if moc_id:
            moc_ids.append(moc_id)

    return moc_ids


# ── Clustering ────────────────────────────────────────────────────────


def _cluster_embeddings(
    embeddings: np.ndarray, ids: list[str], min_cluster_size: int
) -> list[list[str]]:
    """Cluster embeddings using UMAP + HDBSCAN."""
    try:
        import umap
        import hdbscan
    except ImportError:
        logger.warning("umap-learn ou hdbscan não instalados. Usando clusterização simples (KMeans).")
        return _cluster_kmeans(embeddings, ids, min_cluster_size)

    # UMAP reduction
    n_samples = embeddings.shape[0]
    n_neighbors = min(15, n_samples - 1)
    if n_neighbors < 2:
        return [ids]

    reducer = umap.UMAP(n_neighbors=n_neighbors, n_components=min(5, n_samples - 1), metric="cosine", random_state=42)
    reduced = reducer.fit_transform(embeddings)

    # HDBSCAN clustering
    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean")
    labels = clusterer.fit_predict(reduced)

    # Group by cluster (ignore noise label -1)
    clusters: dict[int, list[str]] = {}
    for i, label in enumerate(labels):
        if label == -1:
            continue
        clusters.setdefault(label, []).append(ids[i])

    return list(clusters.values())


def _cluster_kmeans(
    embeddings: np.ndarray, ids: list[str], min_cluster_size: int
) -> list[list[str]]:
    """Simple KMeans fallback when UMAP/HDBSCAN unavailable."""
    try:
        from sklearn.cluster import KMeans
    except ImportError:
        logger.error("scikit-learn não instalado. Não é possível clusterizar.")
        return []

    n = len(ids)
    k = max(2, n // min_cluster_size)
    k = min(k, n)

    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = kmeans.fit_predict(embeddings)

    clusters: dict[int, list[str]] = {}
    for i, label in enumerate(labels):
        clusters.setdefault(label, []).append(ids[i])

    return [c for c in clusters.values() if len(c) >= min_cluster_size]


# ── Topic Extraction ──────────────────────────────────────────────────


def _extract_cluster_terms(db: StateDB, note_ids: list[str], n_terms: int = 10) -> list[str]:
    """Extract representative terms for a cluster using TF-IDF."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        return []

    texts: list[str] = []
    for nid in note_ids:
        note = db.get_note(nid)
        if note:
            texts.append(note.get("title", ""))

    if not texts:
        return []

    vectorizer = TfidfVectorizer(max_features=n_terms, stop_words=None)
    try:
        tfidf = vectorizer.fit_transform(texts)
        terms = vectorizer.get_feature_names_out()
        return list(terms)
    except Exception:
        return []


# ── MOC Generation ────────────────────────────────────────────────────


def _generate_moc(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    llm: Any, note_ids: list[str],
) -> str | None:
    """Generate or update a MOC for a cluster of notes."""
    # Build cluster signature
    sorted_ids = sorted(note_ids)
    cluster_signature = sha256_hex("|".join(sorted_ids))

    # Check if MOC already exists for this signature
    existing_moc = db.get_moc_by_signature(cluster_signature)
    if existing_moc:
        logger.debug("MOC já existe para esta assinatura: %s", existing_moc["moc_id"])
        return existing_moc["moc_id"]

    # Gather note info
    notes_list_text = _build_notes_list(db, note_ids)
    cluster_terms = _extract_cluster_terms(db, note_ids)

    # Load and fill prompt
    prompt_template = _load_prompt(cfg.prompts_path / "moc_generation.md")
    filled = prompt_template.replace("{notes_list}", notes_list_text)
    filled = filled.replace("{cluster_terms}", ", ".join(cluster_terms) if cluster_terms else "N/A")

    try:
        response = _call_llm(llm, filled)
        moc_output = _parse_moc_output(response)
    except Exception as e:
        logger.error("Erro ao gerar MOC: %s", e)
        return None

    # Create MOC note
    moc_id = str(ULID())
    topic = moc_output.topic

    from datetime import datetime
    now = datetime.now().isoformat()
    meta = {
        "type": "moc",
        "moc_id": moc_id,
        "topic": topic,
        "cluster_signature": cluster_signature,
        "created_at": now,
        "updated_at": now,
    }

    body = f"# {topic}\n\n{moc_output.summary}\n\n"
    for sub in moc_output.subsections:
        body += f"## {sub.title}\n\n{sub.description}\n\n"
        for nid in sub.note_ids:
            note = db.get_note(nid)
            title = note["title"] if note else nid
            body += f"- [[ZTL - {nid} - {_slug(title)}]]\n"
        body += "\n"

    filename = note_filename("MOC", moc_id, topic)
    moc_path = cfg.vault_path / "40_MOCs" / filename
    safe_write_note(moc_path, meta, body)

    # Persist in state and index
    db.upsert_moc(moc_id, topic, str(moc_path), cluster_signature)
    idx.upsert_moc(moc_id, f"{topic}: {moc_output.summary}", {
        "topic": topic, "note_count": len(note_ids),
    })

    logger.info("MOC criado: %s — %s (%d notas)", moc_id, topic, len(note_ids))
    return moc_id


# ── Helpers ───────────────────────────────────────────────────────────


def _build_notes_list(db: StateDB, note_ids: list[str]) -> str:
    parts: list[str] = []
    for nid in note_ids:
        note = db.get_note(nid)
        if note:
            title = note.get("title", "Sem título")
            parts.append(f"- **{nid}**: {title}")
    return "\n".join(parts) if parts else "Nenhuma nota encontrada."


def _get_llm(cfg: AppConfig) -> Any:
    if cfg.llm.provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=cfg.llm.model, temperature=cfg.llm.temperature)
    elif cfg.llm.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=cfg.llm.model, temperature=cfg.llm.temperature)
    elif cfg.llm.provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=cfg.llm.model, temperature=cfg.llm.temperature)
    else:
        raise ValueError(f"LLM provider não suportado: {cfg.llm.provider}")


def _call_llm(llm: Any, prompt: str) -> str:
    from langchain_core.messages import HumanMessage
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content


def _load_prompt(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt não encontrado: {path}")
    return path.read_text(encoding="utf-8")


def _parse_moc_output(text: str) -> MOCGenerationOutput:
    json_text = _extract_json(text)
    data = json.loads(json_text)
    return MOCGenerationOutput(**data)


def _extract_json(text: str) -> str:
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    raise ValueError("Nenhum JSON encontrado na resposta do LLM")
