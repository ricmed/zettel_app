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
                 device: str = "auto", allow_fallback: bool = False):
        self.chroma_path = chroma_path
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        self.device = device
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.allow_fallback = allow_fallback

        self.client = chromadb.PersistentClient(
            path=str(chroma_path),
            settings=Settings(anonymized_telemetry=False),
        )

        self.embedding_fn = self._build_embedding_fn(embedding_provider, embedding_model)
        self._ensure_collections()

    def _build_embedding_fn(self, provider: str, model: str) -> Any:
        """Build the embedding function, failing fast unless allow_fallback is set.

        Silent fallback to ChromaDB's default (384-dim MiniLM) would mix incompatible
        vector spaces, so by default a missing key / unknown provider raises instead.
        """
        try:
            if provider == "openai":
                import os
                from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
                # ChromaDB procura CHROMA_OPENAI_API_KEY; fallback para OPENAI_API_KEY
                api_key = os.environ.get("CHROMA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
                if not api_key and not self.allow_fallback:
                    raise RuntimeError(
                        "Sem API key para embeddings OpenAI (defina OPENAI_API_KEY). "
                        "Para usar o embedding local padrao do ChromaDB, ajuste "
                        "embedding.allow_fallback: true no config.yaml."
                    )
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
                if not self.allow_fallback:
                    raise ValueError(
                        f"Embedding provider desconhecido: '{provider}'. "
                        f"Use 'openai' ou 'sentence-transformers', ou ajuste "
                        f"embedding.allow_fallback: true para usar o default do ChromaDB."
                    )
                logger.warning("Embedding provider '%s' desconhecido, usando default do ChromaDB", provider)
                return None
        except ImportError as e:
            if not self.allow_fallback:
                raise
            logger.warning("Embedding function indisponivel (%s). Coleções usarão default do ChromaDB.", e)
            return None

    def _collection_metadata(self) -> dict[str, Any]:
        """Provider marker stored on each collection to detect embedding-space drift."""
        return {
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
        }

    def _get_or_create(self, name: str, **kwargs: Any) -> Any:
        """Get or create a collection.

        Refuses to silently drop data on an embedding-function conflict: the Chroma
        store is a rebuildable cache, so the fix is `zettel reindex --force`, not an
        automatic delete.
        """
        try:
            return self.client.get_or_create_collection(name, **kwargs)
        except ValueError as e:
            if "Embedding function conflict" in str(e):
                raise RuntimeError(
                    f"Conflito de embedding na colecao '{name}': o provider/modelo atual "
                    f"difere do que gerou os vetores existentes. O ChromaDB e reconstruivel "
                    f"a partir do SQLite -- rode 'zettel reindex --force' para regerar os "
                    f"vetores com o embedding atual."
                ) from e
            raise

    def _ensure_collections(self) -> None:
        kwargs: dict[str, Any] = {"metadata": self._collection_metadata()}
        if self.embedding_fn:
            kwargs["embedding_function"] = self.embedding_fn

        self.sources = self._get_or_create(COL_SOURCES, **kwargs)
        self.chunks = self._get_or_create(COL_CHUNKS, **kwargs)
        self.permanent = self._get_or_create(COL_PERMANENT, **kwargs)
        self.mocs_col = self._get_or_create(COL_MOCS, **kwargs)
        logger.debug("Coleções ChromaDB prontas")

    def reset_collection(self, name: str) -> Any:
        """Delete and recreate a collection (used by `reindex --force`)."""
        try:
            self.client.delete_collection(name)
        except Exception:
            pass
        kwargs: dict[str, Any] = {"metadata": self._collection_metadata()}
        if self.embedding_fn:
            kwargs["embedding_function"] = self.embedding_fn
        col = self.client.get_or_create_collection(name, **kwargs)
        # Refresh the cached handle so subsequent upserts hit the new collection.
        attr = {
            COL_SOURCES: "sources", COL_CHUNKS: "chunks",
            COL_PERMANENT: "permanent", COL_MOCS: "mocs_col",
        }.get(name)
        if attr:
            setattr(self, attr, col)
        return col

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

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        """Remove chunks from the index (e.g. orphans after a re-chunk)."""
        if chunk_ids:
            self.chunks.delete(ids=chunk_ids)
            logger.debug("Index: %d chunks removidos", len(chunk_ids))

    def existing_ids(self, collection_name: str, ids: list[str]) -> set[str]:
        """Return the subset of `ids` already present in the given collection.

        Used to skip re-embedding chunks/notes whose content-addressed id is
        already indexed (identical content => identical id => nothing to do).
        ChromaDB rejects duplicate ids in a single get(), so callers may pass
        a list with repeats (e.g. two chunks hashing to the same content id).
        """
        if not ids:
            return set()
        collection = {
            COL_SOURCES: self.sources,
            COL_CHUNKS: self.chunks,
            COL_PERMANENT: self.permanent,
            COL_MOCS: self.mocs_col,
        }.get(collection_name)
        if collection is None:
            raise ValueError(f"Colecao desconhecida: {collection_name}")
        unique_ids = list(dict.fromkeys(ids))
        got = collection.get(ids=unique_ids)
        return set(got.get("ids") or [])

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

    def find_similar_chunks(self, texts: list[str], n_results: int = 3) -> list[dict]:
        """Find already-indexed chunks similar to a sample of newly extracted chunks.

        Mirrors `query_similar_notes` but runs over the `chunks` collection, and
        supports multiple query texts at once (a representative sample of a new
        file's chunks). Returns a flat list of match dicts, one per (query, hit)
        pair, each with id/document/metadata/distance.
        """
        results: list[dict] = []
        texts = [t for t in texts if t and t.strip()]
        if not texts or self.chunks.count() == 0:
            return results

        raw = self.chunks.query(
            query_texts=texts,
            n_results=min(n_results, self.chunks.count()),
        )
        ids_lists = raw.get("ids") or []
        for qi, ids in enumerate(ids_lists):
            for i, cid in enumerate(ids):
                entry: dict[str, Any] = {"id": cid}
                if raw.get("documents") and raw["documents"][qi]:
                    entry["document"] = raw["documents"][qi][i]
                if raw.get("metadatas") and raw["metadatas"][qi]:
                    entry["metadata"] = raw["metadatas"][qi][i]
                if raw.get("distances") and raw["distances"][qi]:
                    entry["distance"] = raw["distances"][qi][i]
                results.append(entry)
        return results

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
