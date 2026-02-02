"""ChromaDB index management — collections, embeddings, queries."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)

# Collection names
COL_SOURCES = "sources"
COL_CHUNKS = "chunks"
COL_PERMANENT = "permanent_notes"
COL_MOCS = "mocs"


class VectorIndex:
    """Manages ChromaDB collections for the Zettelkasten pipeline."""

    def __init__(self, chroma_path: Path, embedding_provider: str = "openai",
                 embedding_model: str = "text-embedding-3-small",
                 device: str = "auto"):
        self.chroma_path = chroma_path
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        self.device = device

        self.client = chromadb.PersistentClient(
            path=str(chroma_path),
            settings=Settings(anonymized_telemetry=False),
        )

        self.embedding_fn = self._build_embedding_fn(embedding_provider, embedding_model)
        self._ensure_collections()

    def _build_embedding_fn(self, provider: str, model: str) -> Any:
        try:
            if provider == "openai":
                import os
                from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
                # ChromaDB procura CHROMA_OPENAI_API_KEY; fallback para OPENAI_API_KEY
                api_key = os.environ.get("CHROMA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
                return OpenAIEmbeddingFunction(model_name=model, api_key=api_key)
            elif provider == "sentence-transformers":
                from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
                from zettel.config import detect_device
                device = detect_device(self.device)
                logger.info("SentenceTransformers usando dispositivo: %s", device.upper())
                return SentenceTransformerEmbeddingFunction(
                    model_name=model, device=device,
                )
            else:
                logger.warning("Embedding provider '%s' desconhecido, usando default do ChromaDB", provider)
                return None
        except (ValueError, ImportError) as e:
            logger.warning("Embedding function indisponivel (%s). Coleções usarão default do ChromaDB.", e)
            return None

    def _get_or_create(self, name: str, **kwargs: Any) -> Any:
        """Get or create a collection, handling embedding function conflicts."""
        try:
            return self.client.get_or_create_collection(name, **kwargs)
        except ValueError as e:
            if "Embedding function conflict" in str(e):
                logger.warning(
                    "Conflito de embedding na colecao '%s'. "
                    "Recriando colecao (dados anteriores serao perdidos). "
                    "Para evitar isso, apague data/chroma/ antes de trocar o provider de embedding.",
                    name,
                )
                self.client.delete_collection(name)
                return self.client.get_or_create_collection(name, **kwargs)
            raise

    def _ensure_collections(self) -> None:
        kwargs: dict[str, Any] = {}
        if self.embedding_fn:
            kwargs["embedding_function"] = self.embedding_fn

        self.sources = self._get_or_create(COL_SOURCES, **kwargs)
        self.chunks = self._get_or_create(COL_CHUNKS, **kwargs)
        self.permanent = self._get_or_create(COL_PERMANENT, **kwargs)
        self.mocs_col = self._get_or_create(COL_MOCS, **kwargs)
        logger.debug("Coleções ChromaDB prontas")

    # ── Sources ────────────────────────────────────────────────────────

    def upsert_source(self, source_id: str, summary: str, metadata: dict[str, Any]) -> None:
        safe_meta = _sanitize_metadata(metadata)
        self.sources.upsert(ids=[source_id], documents=[summary], metadatas=[safe_meta])
        logger.debug("Index: upsert source %s", source_id)

    # ── Chunks ─────────────────────────────────────────────────────────

    def upsert_chunk(self, chunk_id: str, text: str, metadata: dict[str, Any]) -> None:
        safe_meta = _sanitize_metadata(metadata)
        self.chunks.upsert(ids=[chunk_id], documents=[text], metadatas=[safe_meta])
        logger.debug("Index: upsert chunk %s", chunk_id)

    # ── Permanent Notes ────────────────────────────────────────────────

    def upsert_permanent_note(self, note_id: str, embeddable_text: str,
                               metadata: dict[str, Any]) -> None:
        safe_meta = _sanitize_metadata(metadata)
        self.permanent.upsert(ids=[note_id], documents=[embeddable_text], metadatas=[safe_meta])
        logger.info("Index: upsert nota permanente %s", note_id)

    def query_similar_notes(self, query_text: str, n_results: int = 5,
                            exclude_id: str | None = None) -> list[dict]:
        """Find the most similar permanent notes to the given text."""
        results = self.permanent.query(
            query_texts=[query_text],
            n_results=min(n_results + (1 if exclude_id else 0), self.permanent.count() or 1),
        )
        output: list[dict] = []
        if not results or not results["ids"] or not results["ids"][0]:
            return output
        for i, nid in enumerate(results["ids"][0]):
            if exclude_id and nid == exclude_id:
                continue
            entry: dict[str, Any] = {"id": nid}
            if results["documents"] and results["documents"][0]:
                entry["document"] = results["documents"][0][i]
            if results["metadatas"] and results["metadatas"][0]:
                entry["metadata"] = results["metadatas"][0][i]
            if results["distances"] and results["distances"][0]:
                entry["distance"] = results["distances"][0][i]
            output.append(entry)
        return output[:n_results]

    # ── MOCs ───────────────────────────────────────────────────────────

    def upsert_moc(self, moc_id: str, summary: str, metadata: dict[str, Any]) -> None:
        safe_meta = _sanitize_metadata(metadata)
        self.mocs_col.upsert(ids=[moc_id], documents=[summary], metadatas=[safe_meta])
        logger.info("Index: upsert MOC %s", moc_id)

    # ── Utility ────────────────────────────────────────────────────────

    def get_all_permanent_embeddings(self) -> tuple[list[str], list[list[float]]]:
        """Retrieve all permanent note IDs and their embeddings."""
        data = self.permanent.get(include=["embeddings"])
        ids = data.get("ids", [])
        embeddings = data.get("embeddings", [])
        if embeddings is None:
            embeddings = []
        return ids, embeddings  # type: ignore[return-value]

    def count_permanent_notes(self) -> int:
        return self.permanent.count()


def _sanitize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """ChromaDB only accepts str/int/float/bool in metadata."""
    safe: dict[str, Any] = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            safe[k] = v
        elif isinstance(v, list):
            safe[k] = ", ".join(str(x) for x in v)
        else:
            safe[k] = str(v)
    return safe
