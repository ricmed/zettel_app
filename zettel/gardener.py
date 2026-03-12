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
from zettel.schemas import MOCGenerationOutput, MOCIncrementalOutput
from zettel.state import StateDB
from zettel.vault import (
    note_filename,
    safe_write_note,
    safe_update_managed_blocks,
    parse_frontmatter,
    render_frontmatter,
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

    # Spectral init requires k < N where k = n_components + 1; use random for small datasets
    n_components = min(5, n_samples - 2)
    if n_components < 2:
        logger.warning("Poucas amostras para UMAP (%d). Usando KMeans.", n_samples)
        return _cluster_kmeans(embeddings, ids, min_cluster_size)
    init_method = "spectral" if n_samples > n_components + 2 else "random"

    try:
        reducer = umap.UMAP(n_neighbors=n_neighbors, n_components=n_components, metric="cosine", init=init_method)
        reduced = reducer.fit_transform(embeddings)
    except Exception as e:
        logger.warning("UMAP falhou (%s). Usando KMeans como fallback.", e)
        return _cluster_kmeans(embeddings, ids, min_cluster_size)

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

    # Fill domain and topics placeholders
    domain = cfg.gardener.domain or "Geral"
    allowed_topics = cfg.gardener.allowed_topics
    if allowed_topics:
        topics_section = "\n".join(f"- {t}" for t in allowed_topics)
    else:
        topics_section = "_(Nenhuma lista de topicos definida — escolha livremente.)_"

    # Load taxonomy detail
    taxonomy_path = cfg.prompts_path / "moc_topics_taxonomy.md"
    if taxonomy_path.exists():
        taxonomy_detail = taxonomy_path.read_text(encoding="utf-8")
    else:
        taxonomy_detail = "_(Taxonomia detalhada nao disponivel.)_"

    filled = prompt_template.replace("{domain}", domain)
    filled = filled.replace("{allowed_topics_section}", topics_section)
    filled = filled.replace("{taxonomy_detail}", taxonomy_detail)
    filled = filled.replace("{notes_list}", notes_list_text)
    filled = filled.replace("{cluster_terms}", ", ".join(cluster_terms) if cluster_terms else "N/A")

    try:
        response = _call_llm(llm, filled)
        moc_output = _parse_moc_output(response)
    except Exception as e:
        logger.error("Erro ao gerar MOC: %s", e)
        return None

    # Validate topic against allowed list
    if not _validate_moc_topic(cfg, moc_output):
        return None

    # Check if a MOC with this topic already exists — update incrementally
    existing_topic_moc = db.find_moc_by_topic(moc_output.topic)
    if existing_topic_moc:
        logger.info("MOC existente para topico '%s': %s", moc_output.topic, existing_topic_moc["moc_id"])
        return _update_existing_moc(cfg, db, idx, llm, existing_topic_moc, note_ids, cluster_signature)

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


# ── MOC Topic Validation ─────────────────────────────────────────────


def _validate_moc_topic(cfg: AppConfig, moc_output: MOCGenerationOutput) -> bool:
    """Validate that the MOC topic matches the allowed_topics list.

    Returns True if approved, False if rejected.
    """
    allowed = cfg.gardener.allowed_topics
    if not allowed:
        return True

    topic = moc_output.topic
    topic_lower = topic.lower()

    for allowed_topic in allowed:
        allowed_lower = allowed_topic.lower()
        # Bidirectional substring match
        if allowed_lower in topic_lower or topic_lower in allowed_lower:
            logger.debug("Topico '%s' corresponde a '%s'", topic, allowed_topic)
            return True

    # No match found
    justification = moc_output.topic_justification
    if cfg.gardener.strict_topics:
        logger.warning(
            "MOC rejeitado: topico '%s' fora da lista permitida. Justificativa: %s",
            topic, justification or "(nenhuma)",
        )
        return False
    else:
        logger.info(
            "MOC aprovado (modo permissivo): topico '%s' fora da lista. Justificativa: %s",
            topic, justification or "(nenhuma)",
        )
        return True


# ── Incremental MOC Update ────────────────────────────────────────


def _parse_moc_structure(moc_path: Path) -> dict | None:
    """Parse an existing MOC file and extract its structure.

    Returns dict with keys: topic, summary, subsections, all_note_ids.
    Each subsection is {title, description, note_ids}.
    """
    if not moc_path.exists():
        logger.warning("MOC file not found: %s", moc_path)
        return None

    content = moc_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(content)

    topic = meta.get("topic", "")
    note_id_re = re.compile(r"\[\[ZTL\s*-\s*(\S+)\s*-\s*[^\]]*\]\]")

    lines = body.split("\n")
    summary_lines: list[str] = []
    subsections: list[dict] = []
    current_sub: dict | None = None
    in_summary = False

    for line in lines:
        # Top-level heading: # Topic
        if line.startswith("# ") and not line.startswith("## "):
            in_summary = True
            continue

        # Subsection heading
        if line.startswith("## "):
            in_summary = False
            if current_sub is not None:
                subsections.append(current_sub)
            current_sub = {
                "title": line[3:].strip(),
                "description": "",
                "note_ids": [],
            }
            continue

        # Collect summary (lines between # heading and first ##)
        if in_summary:
            summary_lines.append(line)
            continue

        # Inside a subsection
        if current_sub is not None:
            match = note_id_re.search(line)
            if match:
                current_sub["note_ids"].append(match.group(1))
            elif line.strip() and not line.strip().startswith("-"):
                if current_sub["description"]:
                    current_sub["description"] += " " + line.strip()
                else:
                    current_sub["description"] = line.strip()

    if current_sub is not None:
        subsections.append(current_sub)

    all_note_ids: set[str] = set()
    for sub in subsections:
        all_note_ids.update(sub["note_ids"])

    summary = "\n".join(summary_lines).strip()

    return {
        "topic": topic,
        "summary": summary,
        "subsections": subsections,
        "all_note_ids": all_note_ids,
    }


def _update_existing_moc(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    llm: Any, existing_moc: dict, note_ids: list[str],
    cluster_signature: str,
) -> str | None:
    """Incrementally update an existing MOC with new notes."""
    moc_id = existing_moc["moc_id"]
    moc_path = Path(existing_moc["path"]) if existing_moc.get("path") else None

    if not moc_path or not moc_path.exists():
        logger.warning("MOC file not found for %s, skipping incremental update", moc_id)
        return None

    structure = _parse_moc_structure(moc_path)
    if structure is None:
        return None

    existing_ids = structure["all_note_ids"]
    truly_new = [nid for nid in note_ids if nid not in existing_ids]

    if not truly_new:
        logger.info("MOC %s: nenhuma nota nova, atualizando apenas assinatura", moc_id)
        db.upsert_moc(moc_id, existing_moc["topic"], str(moc_path), cluster_signature)
        return moc_id

    logger.info("MOC %s: %d notas novas a classificar", moc_id, len(truly_new))

    # Build incremental prompt
    prompt_template = _load_prompt(cfg.prompts_path / "moc_incremental.md")

    # Build existing subsections text
    sub_parts: list[str] = []
    for sub in structure["subsections"]:
        sub_parts.append(f"#### {sub['title']}")
        if sub["description"]:
            sub_parts.append(sub["description"])
        for nid in sub["note_ids"]:
            note = db.get_note(nid)
            title = note["title"] if note else nid
            sub_parts.append(f"- [[ZTL - {nid} - {_slug(title)}]]")
        sub_parts.append("")
    existing_subsections_text = "\n".join(sub_parts) if sub_parts else "_(Nenhuma subsecao)_"

    # Build new notes list
    new_notes_text = _build_notes_list(db, truly_new)

    filled = prompt_template.replace("{moc_topic}", structure["topic"])
    filled = filled.replace("{moc_summary}", structure["summary"])
    filled = filled.replace("{existing_subsections}", existing_subsections_text)
    filled = filled.replace("{new_notes_list}", new_notes_text)

    try:
        response = _call_llm(llm, filled)
        incremental_output = _parse_incremental_output(response)
    except Exception as e:
        logger.error("Erro ao classificar notas incrementais: %s", e)
        return None

    # Apply placements
    _apply_incremental_placements(db, moc_path, structure, incremental_output)

    # Update state and index
    db.upsert_moc(moc_id, existing_moc["topic"], str(moc_path), cluster_signature)
    idx.upsert_moc(moc_id, f"{structure['topic']}: {structure['summary']}", {
        "topic": existing_moc["topic"],
        "note_count": len(existing_ids) + len(truly_new),
    })

    placed_count = sum(1 for p in incremental_output.placements if p.subsection.lower() != "ignorar")
    new_sub_count = len(incremental_output.new_subsections)
    logger.info(
        "MOC %s atualizado: %d notas classificadas, %d ignoradas, %d novas subsecoes",
        moc_id, placed_count, len(truly_new) - placed_count, new_sub_count,
    )
    return moc_id


def _parse_incremental_output(text: str) -> MOCIncrementalOutput:
    """Parse LLM response into MOCIncrementalOutput."""
    json_text = _extract_json(text)
    data = json.loads(json_text)
    return MOCIncrementalOutput(**data)


def _apply_incremental_placements(
    db: StateDB, moc_path: Path,
    structure: dict, incremental_output: MOCIncrementalOutput,
) -> None:
    """Reconstruct and write the MOC file with new notes placed into subsections."""
    content = moc_path.read_text(encoding="utf-8")
    meta, _ = parse_frontmatter(content)

    # Update timestamp
    from datetime import datetime
    meta["updated_at"] = datetime.now().isoformat()

    # Build a map: subsection title -> list of new note_ids to add
    placement_map: dict[str, list[str]] = {}
    for p in incremental_output.placements:
        if p.subsection.lower() == "ignorar":
            continue
        placement_map.setdefault(p.subsection, []).append(p.note_id)

    # Reconstruct body
    body = f"# {structure['topic']}\n\n{structure['summary']}\n\n"

    for sub in structure["subsections"]:
        body += f"## {sub['title']}\n\n"
        if sub["description"]:
            body += f"{sub['description']}\n\n"
        # Existing notes
        for nid in sub["note_ids"]:
            note = db.get_note(nid)
            title = note["title"] if note else nid
            body += f"- [[ZTL - {nid} - {_slug(title)}]]\n"
        # New notes placed in this subsection
        new_in_sub = placement_map.get(sub["title"], [])
        for nid in new_in_sub:
            note = db.get_note(nid)
            title = note["title"] if note else nid
            body += f"- [[ZTL - {nid} - {_slug(title)}]]\n"
        body += "\n"

    # New subsections from LLM
    for new_sub in incremental_output.new_subsections:
        body += f"## {new_sub.title}\n\n"
        if new_sub.description:
            body += f"{new_sub.description}\n\n"
        for nid in new_sub.note_ids:
            note = db.get_note(nid)
            title = note["title"] if note else nid
            body += f"- [[ZTL - {nid} - {_slug(title)}]]\n"
        body += "\n"

    safe_write_note(moc_path, meta, body)


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
