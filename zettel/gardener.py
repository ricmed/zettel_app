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
from zettel.llm import call_llm, extract_json, fill_template, get_llm, load_prompt_parts
from zettel.schemas import MOCGenerationOutput, MOCIncrementalOutput
from zettel.state import StateDB
from zettel.taxonomy import TaxonomyLoadError, resolve_allowed_topics
from zettel.vault import (
    note_filename,
    permanent_wikilink,
    safe_write_note,
    parse_frontmatter,
)

logger = logging.getLogger(__name__)

_MOC_FALLBACK_SUBSECTION = "Outras notas do cluster"


# ── Public API ─────────────────────────────────────────────────────────


def run_garden(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, *, recreate: bool = False,
) -> list[str]:
    """Cluster permanent notes and generate/update MOCs. Returns moc_ids."""
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("garden")
    begin_run(run_id)

    if recreate:
        removed = purge_pipeline_mocs(cfg, db, idx)
        logger.info("Recriacao: %d MOC(s) do pipeline removidos", removed)

    # Fail fast if taxonomy file is required but missing/invalid.
    try:
        resolve_allowed_topics(
            cfg.gardener.topics_path,
            cfg.gardener.allowed_topics,
            strict=cfg.gardener.strict_topics,
        )
    except TaxonomyLoadError as e:
        logger.error("Taxonomia de MOCs indisponivel: %s", e)
        finish_pipeline_run(db, run_id, "failed")
        raise

    note_count = idx.count_permanent_notes()
    if note_count < cfg.gardener.min_cluster_size:
        logger.info(
            "Poucas notas para clusterização (%d < %d)", note_count, cfg.gardener.min_cluster_size
        )
        finish_pipeline_run(db, run_id)
        return []

    ids, embeddings = idx.get_all_permanent_embeddings()
    if embeddings is None or len(embeddings) < cfg.gardener.min_cluster_size:
        logger.info("Embeddings insuficientes para clusterização")
        finish_pipeline_run(db, run_id)
        return []

    embeddings_array = np.array(embeddings)

    clusters = _cluster_embeddings(embeddings_array, ids, cfg.gardener.min_cluster_size)
    if not clusters:
        logger.info("Nenhum cluster encontrado")
        finish_pipeline_run(db, run_id)
        return []

    logger.info("Clusters encontrados: %d", len(clusters))

    llm = get_llm(cfg)
    moc_ids: list[str] = []

    for cluster_ids in clusters:
        if len(cluster_ids) < cfg.gardener.min_notes_for_moc:
            continue
        moc_id = _generate_moc(cfg, db, idx, llm, cluster_ids)
        if moc_id:
            moc_ids.append(moc_id)

    finish_pipeline_run(db, run_id)
    return moc_ids


def purge_pipeline_mocs(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> int:
    """Delete pipeline MOCs from SQLite, ChromaDB and the vault. Returns count removed."""
    removed = db.delete_pipeline_mocs()
    if not removed:
        return 0

    idx.delete_mocs([m["moc_id"] for m in removed])

    for moc in removed:
        path = _moc_vault_path(cfg, moc)
        if path.is_file():
            path.unlink()
            logger.debug("Arquivo MOC removido: %s", path)

    return len(removed)


def _moc_vault_path(cfg: AppConfig, moc: dict) -> Path:
    path_str = moc.get("path")
    if path_str:
        return Path(path_str)
    return cfg.vault_path / "40_MOCs" / note_filename("MOC", moc["moc_id"], moc["topic"])


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

    n_samples = embeddings.shape[0]
    n_neighbors = min(15, n_samples - 1)
    if n_neighbors < 2:
        return [ids]

    n_components = min(5, n_samples - 2)
    if n_components < 2:
        logger.warning("Poucas amostras para UMAP (%d). Usando KMeans.", n_samples)
        return _cluster_kmeans(embeddings, ids, min_cluster_size)
    init_method = "spectral" if n_samples > n_components + 2 else "random"

    try:
        reducer = umap.UMAP(
            n_neighbors=n_neighbors,
            n_components=n_components,
            metric="cosine",
            init=init_method,
        )
        reduced = reducer.fit_transform(embeddings)
    except Exception as e:
        logger.warning("UMAP falhou (%s). Usando KMeans como fallback.", e)
        return _cluster_kmeans(embeddings, ids, min_cluster_size)

    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean")
    labels = clusterer.fit_predict(reduced)

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
    cfg: AppConfig, db: StateDB, idx: VectorIndex, llm: Any, note_ids: list[str],
) -> str | None:
    """Generate or update a MOC for a cluster of notes."""
    sorted_ids = sorted(note_ids)
    cluster_signature = sha256_hex("|".join(sorted_ids))

    existing_moc = db.get_moc_by_signature(cluster_signature)
    if existing_moc:
        logger.debug("MOC já existe para esta assinatura: %s", existing_moc["moc_id"])
        return existing_moc["moc_id"]

    notes_list_text = _build_notes_list(db, note_ids, _build_note_alias_map(note_ids))
    cluster_terms = _extract_cluster_terms(db, note_ids)

    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_generation.md")

    domain = cfg.gardener.domain or "Geral"
    try:
        allowed_topics, taxonomy_detail = resolve_allowed_topics(
            cfg.gardener.topics_path,
            cfg.gardener.allowed_topics,
            strict=cfg.gardener.strict_topics,
        )
    except TaxonomyLoadError as e:
        logger.error("Taxonomia de MOCs indisponivel: %s", e)
        return None

    if allowed_topics:
        topics_section = "\n".join(f"- {t}" for t in allowed_topics)
    else:
        topics_section = "_(Nenhuma lista de categorias definida — escolha livremente.)_"

    mapping = {
        "domain": domain,
        "allowed_topics_section": topics_section,
        "taxonomy_detail": taxonomy_detail,
        "notes_list": notes_list_text,
        "cluster_terms": ", ".join(cluster_terms) if cluster_terms else "N/A",
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)

    try:
        response = call_llm(
            llm,
            user,
            system=system or None,
            provider=cfg.llm.provider,
            prompt_cache=cfg.llm.prompt_cache,
        )
        moc_output = _parse_moc_output(response)
    except Exception as e:
        logger.error("Erro ao gerar MOC: %s", e)
        return None

    if not _validate_moc_topic(cfg, moc_output):
        return None

    existing_topic_moc = db.find_moc_by_topic(moc_output.topic)
    if existing_topic_moc:
        logger.info(
            "MOC existente para topico '%s': %s", moc_output.topic, existing_topic_moc["moc_id"]
        )
        return _update_existing_moc(
            cfg, db, idx, llm, existing_topic_moc, note_ids, cluster_signature
        )

    moc_id = str(ULID())
    topic = moc_output.topic

    from datetime import datetime
    now = datetime.now().isoformat()
    meta = {
        "type": "moc",
        "moc_id": moc_id,
        "topic": topic,
        "cluster_signature": cluster_signature,
        "origin": "pipeline",
        "created_at": now,
        "updated_at": now,
    }

    body = _build_moc_body(
        db,
        topic,
        moc_output.summary,
        moc_output.subsections,
        _allowed_note_ids(db, note_ids),
        _build_note_alias_map(note_ids),
    )

    filename = note_filename("MOC", moc_id, topic)
    moc_path = cfg.vault_path / "40_MOCs" / filename
    safe_write_note(moc_path, meta, body)

    # Retencao: guarda corpo + frontmatter do MOC para rebuild sem reprocessar o LLM.
    db.upsert_moc(
        moc_id, topic, str(moc_path), cluster_signature,
        body=body, frontmatter_json=json.dumps(meta, ensure_ascii=False), origin="pipeline",
    )
    idx.upsert_moc(moc_id, _moc_embeddable(topic, moc_output.summary), {
        "topic": topic, "note_count": len(note_ids),
    })

    logger.info("MOC criado: %s — %s (%d notas)", moc_id, topic, len(note_ids))
    return moc_id


# ── MOC Topic Validation ─────────────────────────────────────────────


def _validate_moc_topic(cfg: AppConfig, moc_output: MOCGenerationOutput) -> bool:
    """Validate that the MOC topic matches an allowed category name."""
    try:
        allowed, _ = resolve_allowed_topics(
            cfg.gardener.topics_path,
            cfg.gardener.allowed_topics,
            strict=cfg.gardener.strict_topics,
        )
    except TaxonomyLoadError as e:
        logger.error("Taxonomia de MOCs indisponivel na validacao: %s", e)
        return False

    if not allowed:
        return True

    topic = moc_output.topic
    topic_lower = topic.lower()

    for allowed_topic in allowed:
        allowed_lower = allowed_topic.lower()
        if allowed_lower in topic_lower or topic_lower in allowed_lower:
            logger.debug("Topico '%s' corresponde a '%s'", topic, allowed_topic)
            return True

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
    """Parse an existing MOC file and extract its structure."""
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
        if line.startswith("# ") and not line.startswith("## "):
            in_summary = True
            continue

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

        if in_summary:
            summary_lines.append(line)
            continue

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
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    existing_moc: dict,
    note_ids: list[str],
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
        body_snap, fm_snap = _snapshot_moc_file(moc_path)
        db.upsert_moc(
            moc_id, existing_moc["topic"], str(moc_path), cluster_signature,
            body=body_snap, frontmatter_json=fm_snap, origin="pipeline",
        )
        return moc_id

    logger.info("MOC %s: %d notas novas a classificar", moc_id, len(truly_new))

    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_incremental.md")

    alias_to_id = _build_note_alias_map(truly_new)

    sub_parts: list[str] = []
    for sub in structure["subsections"]:
        sub_parts.append(f"#### {sub['title']}")
        if sub["description"]:
            sub_parts.append(sub["description"])
        for nid in sub["note_ids"]:
            link = _note_wikilink(db, nid)
            if link:
                sub_parts.append(link.rstrip())
        sub_parts.append("")
    existing_subsections_text = "\n".join(sub_parts) if sub_parts else "_(Nenhuma subsecao)_"

    new_notes_text = _build_notes_list(db, truly_new, alias_to_id)

    mapping = {
        "moc_topic": structure["topic"],
        "moc_summary": structure["summary"],
        "existing_subsections": existing_subsections_text,
        "new_notes_list": new_notes_text,
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)

    try:
        response = call_llm(
            llm,
            user,
            system=system or None,
            provider=cfg.llm.provider,
            prompt_cache=cfg.llm.prompt_cache,
        )
        incremental_output = _parse_incremental_output(response)
    except Exception as e:
        logger.error("Erro ao classificar notas incrementais: %s", e)
        return None

    _apply_incremental_placements(
        db, moc_path, structure, incremental_output,
        _allowed_note_ids(db, truly_new), alias_to_id,
    )

    body_snap, fm_snap = _snapshot_moc_file(moc_path)
    db.upsert_moc(
        moc_id, existing_moc["topic"], str(moc_path), cluster_signature,
        body=body_snap, frontmatter_json=fm_snap, origin="pipeline",
    )
    idx.upsert_moc(moc_id, _moc_embeddable(structure["topic"], structure["summary"]), {
        "topic": existing_moc["topic"],
        "note_count": len(existing_ids) + len(truly_new),
    })

    placed_count = sum(
        1 for p in incremental_output.placements if p.subsection.lower() != "ignorar"
    )
    new_sub_count = len(incremental_output.new_subsections)
    logger.info(
        "MOC %s atualizado: %d notas classificadas, %d ignoradas, %d novas subsecoes",
        moc_id, placed_count, len(truly_new) - placed_count, new_sub_count,
    )
    return moc_id


def _parse_incremental_output(text: str) -> MOCIncrementalOutput:
    """Parse LLM response into MOCIncrementalOutput."""
    json_text = extract_json(text)
    data = json.loads(json_text)
    return MOCIncrementalOutput(**data)


def _apply_incremental_placements(
    db: StateDB,
    moc_path: Path,
    structure: dict,
    incremental_output: MOCIncrementalOutput,
    allowed_ids: set[str],
    alias_to_id: dict[str, str],
) -> None:
    """Reconstruct and write the MOC file with new notes placed into subsections."""
    content = moc_path.read_text(encoding="utf-8")
    meta, _ = parse_frontmatter(content)

    from datetime import datetime
    meta["updated_at"] = datetime.now().isoformat()

    placement_map: dict[str, list[str]] = {}
    placed: set[str] = set()
    existing_titles = {sub["title"] for sub in structure["subsections"]}

    for p in incremental_output.placements:
        nid = _resolve_note_ref(p.note_id, allowed_ids, alias_to_id)
        if not nid or nid in placed:
            continue
        if p.subsection.lower() == "ignorar":
            placed.add(nid)
            continue
        if p.subsection not in existing_titles:
            continue
        placement_map.setdefault(p.subsection, []).append(nid)
        placed.add(nid)

    new_subsections: list[tuple[str, str, list[str]]] = []
    for new_sub in incremental_output.new_subsections:
        resolved: list[str] = []
        for ref in new_sub.note_ids:
            nid = _resolve_note_ref(ref, allowed_ids, alias_to_id)
            if not nid or nid in placed:
                continue
            resolved.append(nid)
            placed.add(nid)
        if resolved:
            new_subsections.append((new_sub.title, new_sub.description, resolved))

    missing = allowed_ids - placed
    if missing:
        logger.info(
            "MOC incremental: %d nota(s) reconciliada(s) em '%s'",
            len(missing), _MOC_FALLBACK_SUBSECTION,
        )

    body = f"# {structure['topic']}\n\n{structure['summary']}\n\n"
    fallback_lines: list[str] = []

    for sub in structure["subsections"]:
        title = sub["title"]
        body += f"## {title}\n\n"
        if sub["description"]:
            body += f"{sub['description']}\n\n"
        subsection_ids = list(sub["note_ids"])
        subsection_ids.extend(placement_map.get(title, []))
        if title == _MOC_FALLBACK_SUBSECTION:
            subsection_ids.extend(sorted(missing))
            missing = set()
        body += _format_note_links(db, subsection_ids)
        body += "\n"

    for title, description, note_ids in new_subsections:
        body += f"## {title}\n\n"
        if description:
            body += f"{description}\n\n"
        body += _format_note_links(db, note_ids)
        body += "\n"

    if missing:
        body += f"## {_MOC_FALLBACK_SUBSECTION}\n\n"
        body += _format_note_links(db, sorted(missing))
        body += "\n"

    safe_write_note(moc_path, meta, body)


# ── Helpers ───────────────────────────────────────────────────────────


def _moc_embeddable(topic: str, summary: str) -> str:
    """Canonical embeddable text for a MOC (unified across gardener and sync)."""
    return f"{topic}\n\n{summary}".strip()


def _snapshot_moc_file(moc_path: Path) -> tuple[str | None, str | None]:
    """Read a MOC file and return (body_without_frontmatter, frontmatter_json).

    Used to persist the MOC content into the DB so `zettel rebuild` can recreate the
    .md file without reprocessing the LLM. Returns (None, None) if the file is missing.
    """
    if not moc_path.exists():
        return None, None
    meta, body = parse_frontmatter(moc_path.read_text(encoding="utf-8"))
    return body, json.dumps(meta, ensure_ascii=False)


def _build_notes_list(
    db: StateDB, note_ids: list[str], alias_to_id: dict[str, str] | None = None,
) -> str:
    id_to_alias = {nid: alias for alias, nid in (alias_to_id or {}).items()}
    parts: list[str] = []
    for nid in note_ids:
        note = db.get_note(nid)
        if note:
            title = note.get("title", "Sem título")
            label = id_to_alias.get(nid, nid)
            parts.append(f"- **{label}**: {title}")
    return "\n".join(parts) if parts else "Nenhuma nota encontrada."


def _build_note_alias_map(note_ids: list[str]) -> dict[str, str]:
    """Map short aliases (N1, N2, ...) to note IDs for LLM prompts."""
    return {f"N{i}": nid for i, nid in enumerate(note_ids, start=1)}


def _allowed_note_ids(db: StateDB, note_ids: list[str]) -> set[str]:
    """Note IDs from the cluster that exist in StateDB and can be linked."""
    return {nid for nid in note_ids if db.get_note(nid)}


def _resolve_note_ref(
    ref: str, allowed_ids: set[str], alias_to_id: dict[str, str],
) -> str | None:
    """Resolve an LLM note reference (alias or ULID) to a known cluster ID."""
    token = ref.strip()
    if not token:
        return None

    if token in alias_to_id:
        return alias_to_id[token]
    if token in allowed_ids:
        return token

    fuzzy = _fuzzy_match_note_id(token, allowed_ids)
    if fuzzy:
        logger.warning("MOC: referencia '%s' corrigida para '%s' (typo)", token, fuzzy)
        return fuzzy

    logger.warning("MOC: referencia '%s' descartada (fora do cluster)", token)
    return None


def _fuzzy_match_note_id(ref: str, allowed_ids: set[str]) -> str | None:
    """Return the sole allowed ID within edit distance 1, if unambiguous."""
    matches = [nid for nid in allowed_ids if _within_edit_distance_one(ref, nid)]
    if len(matches) == 1:
        return matches[0]
    return None


def _within_edit_distance_one(a: str, b: str) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = diffs = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            diffs += 1
            if diffs > 1:
                return False
            j += 1
    return diffs + (lb - j) <= 1


def _note_wikilink(db: StateDB, note_id: str) -> str | None:
    note = db.get_note(note_id)
    if not note:
        logger.warning("MOC: nota %s ausente no banco, link omitido", note_id)
        return None
    link = permanent_wikilink(
        note_id, note.get("title", ""), path=note.get("path"),
    )
    return f"- {link}"


def _format_note_links(db: StateDB, note_ids: list[str]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for nid in note_ids:
        if nid in seen:
            continue
        seen.add(nid)
        link = _note_wikilink(db, nid)
        if link:
            lines.append(link)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _build_moc_body(
    db: StateDB,
    topic: str,
    summary: str,
    subsections: list,
    allowed_ids: set[str],
    alias_to_id: dict[str, str],
) -> str:
    """Build a MOC body, filtering ghost IDs and reconciling omitted notes."""
    placed: set[str] = set()
    body = f"# {topic}\n\n{summary}\n\n"

    for sub in subsections:
        body += f"## {sub.title}\n\n"
        if sub.description:
            body += f"{sub.description}\n\n"
        subsection_ids: list[str] = []
        for ref in sub.note_ids:
            nid = _resolve_note_ref(ref, allowed_ids, alias_to_id)
            if not nid or nid in placed:
                continue
            subsection_ids.append(nid)
            placed.add(nid)
        body += _format_note_links(db, subsection_ids)
        body += "\n"

    missing = allowed_ids - placed
    if missing:
        logger.info(
            "MOC: %d nota(s) reconciliada(s) em '%s'",
            len(missing), _MOC_FALLBACK_SUBSECTION,
        )
        body += f"## {_MOC_FALLBACK_SUBSECTION}\n\n"
        body += _format_note_links(db, sorted(missing))
        body += "\n"

    return body


def _parse_moc_output(text: str) -> MOCGenerationOutput:
    json_text = extract_json(text)
    data = json.loads(json_text)
    return MOCGenerationOutput(**data)
