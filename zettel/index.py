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
COL_LITERATURE = "literature_notes"

_ALL_COLLECTIONS = [COL_SOURCES, COL_CHUNKS, COL_PERMANENT, COL_MOCS, COL_LITERATURE]

_DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"
_SUPPORTED_PROVIDERS = ("openai", "sentence-transformers", "ollama")


class EmbeddingSpaceMismatch(Exception):
    """Raised when config embedding provider/model differs from Chroma collection metadata."""

    def __init__(
        self,
        stored_provider: str | None,
        stored_model: str | None,
        current_provider: str,
        current_model: str,
    ):
        self.stored_provider = stored_provider
        self.stored_model = stored_model
        self.current_provider = current_provider
        self.current_model = current_model
        super().__init__(
            f"Espaco vetorial incompativel: Chroma tem "
            f"{stored_provider}/{stored_model}, config pede "
            f"{current_provider}/{current_model}. "
            f"Rode 'zettel reindex --force' (ou confirme o reprocessamento)."
        )


def peek_stored_embedding_identity(
    chroma_path: Path,
) -> tuple[str | None, str | None]:
    """Read embedding provider/model markers from an existing Chroma store.

    Returns ``(None, None)`` when the path is missing, empty, or collections
    have no markers yet (fresh / legacy store).
    """
    if not chroma_path.exists():
        return None, None
    try:
        client = chromadb.PersistentClient(
            path=str(chroma_path),
            settings=Settings(anonymized_telemetry=False),
        )
    except Exception as e:
        logger.debug("Nao foi possivel abrir Chroma em %s: %s", chroma_path, e)
        return None, None
    return _identity_from_client(client)


def _identity_from_client(client: Any) -> tuple[str | None, str | None]:
    for name in (COL_PERMANENT, COL_CHUNKS, COL_SOURCES, COL_MOCS):
        try:
            col = client.get_collection(name)
        except Exception:
            continue
        meta = col.metadata or {}
        provider = meta.get("embedding_provider")
        model = meta.get("embedding_model")
        if provider is not None or model is not None:
            return (
                str(provider) if provider is not None else None,
                str(model) if model is not None else None,
            )
    return None, None


class VectorIndex:
    """Manages ChromaDB collections for the Zettelkasten pipeline."""

    def __init__(
        self,
        chroma_path: Path,
        embedding_provider: str = "openai",
        embedding_model: str = "text-embedding-3-small",
        device: str = "auto",
        allow_fallback: bool = False,
        base_url: str | None = None,
        reset_mismatched: bool = False,
    ):
        self.chroma_path = chroma_path
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        self.device = device
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.allow_fallback = allow_fallback
        self.base_url = base_url
        self.reset_mismatched = reset_mismatched

        self.client = chromadb.PersistentClient(
            path=str(chroma_path),
            settings=Settings(anonymized_telemetry=False),
        )

        self.embedding_fn = self._build_embedding_fn(embedding_provider, embedding_model)

        stored = self.get_stored_embedding_identity()
        if not self.embedding_space_matches(stored):
            if reset_mismatched:
                logger.warning(
                    "Resetando colecoes Chroma por troca de embedding: %s/%s -> %s/%s",
                    stored[0], stored[1],
                    self.embedding_provider, self.embedding_model,
                )
                self._delete_all_collections()
            else:
                raise EmbeddingSpaceMismatch(
                    stored[0], stored[1],
                    self.embedding_provider, self.embedding_model,
                )

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
                kwargs: dict[str, Any] = {"model_name": model, "api_key": api_key}
                if self.base_url:
                    kwargs["api_base"] = self.base_url
                return OpenAIEmbeddingFunction(**kwargs)
            elif provider == "sentence-transformers":
                from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
                from zettel.config import detect_device
                device = detect_device(self.device)
                logger.info("SentenceTransformers usando dispositivo: %s", device.upper())
                return SentenceTransformerEmbeddingFunction(
                    model_name=model, device=device,
                )
            elif provider == "ollama":
                # API OpenAI-compatible do Ollama — evita depender do pacote `ollama`.
                from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
                url = self.base_url or _DEFAULT_OLLAMA_URL
                if not url.rstrip("/").endswith("/v1"):
                    url = url.rstrip("/") + "/v1"
                logger.info("Ollama embeddings (OpenAI-compatible) em %s (modelo=%s)", url, model)
                return OpenAIEmbeddingFunction(
                    model_name=model,
                    api_key="ollama",  # Ollama nao valida a key
                    api_base=url,
                )
            else:
                if not self.allow_fallback:
                    raise ValueError(
                        f"Embedding provider desconhecido: '{provider}'. "
                        f"Use {', '.join(repr(p) for p in _SUPPORTED_PROVIDERS)}, "
                        f"ou ajuste embedding.allow_fallback: true para usar o default "
                        f"do ChromaDB."
                    )
                logger.warning(
                    "Embedding provider '%s' desconhecido, usando default do ChromaDB",
                    provider,
                )
                return None
        except ImportError as e:
            if not self.allow_fallback:
                raise
            logger.warning(
                "Embedding function indisponivel (%s). Colecoes usarao default do ChromaDB.",
                e,
            )
            return None

    def _collection_metadata(self) -> dict[str, Any]:
        """Provider marker stored on each collection to detect embedding-space drift."""
        return {
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
        }

    def get_stored_embedding_identity(self) -> tuple[str | None, str | None]:
        """Return ``(provider, model)`` markers from existing collections, if any."""
        return _identity_from_client(self.client)

    def embedding_space_matches(
        self, stored: tuple[str | None, str | None] | None = None,
    ) -> bool:
        """True when there is no stored marker, or it equals the current provider/model."""
        if stored is None:
            stored = self.get_stored_embedding_identity()
        sp, sm = stored
        if sp is None and sm is None:
            return True
        return sp == self.embedding_provider and sm == self.embedding_model

    def _delete_all_collections(self) -> None:
        for name in _ALL_COLLECTIONS:
            try:
                self.client.delete_collection(name)
            except Exception:
                pass

    def _get_or_create(self, name: str, **kwargs: Any) -> Any:
        """Get or create a collection.

        Refuses to silently drop data on an embedding-function conflict: the Chroma
        store is a rebuildable cache, so the fix is `zettel reindex --force`, not an
        automatic delete.
        """
        # Proactive metadata check (covers cases where Chroma does not raise EF conflict).
        try:
            existing = self.client.get_collection(name)
            meta = existing.metadata or {}
            sp, sm = meta.get("embedding_provider"), meta.get("embedding_model")
            if (sp is not None or sm is not None) and (
                sp != self.embedding_provider or sm != self.embedding_model
            ):
                raise EmbeddingSpaceMismatch(
                    str(sp) if sp is not None else None,
                    str(sm) if sm is not None else None,
                    self.embedding_provider,
                    self.embedding_model,
                )
        except EmbeddingSpaceMismatch:
            raise
        except Exception:
            pass

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
        self.literature = self._get_or_create(COL_LITERATURE, **kwargs)
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
            COL_LITERATURE: "literature",
        }.get(name)
        if attr:
            setattr(self, attr, col)
        return col

    # ── Sources ────────────────────────────────────────────────────────

    def upsert_source(self, source_id: str, summary: str, metadata: dict[str, Any]) -> None:
        safe_meta = _sanitize_metadata(metadata)
        self.sources.upsert(ids=[source_id], documents=[summary], metadatas=[safe_meta])
        self._record_embed_usage(summary, label=f"source:{source_id}")
        logger.debug("Index: upsert source %s", source_id)

    # ── Chunks ─────────────────────────────────────────────────────────

    def upsert_chunk(
        self,
        chunk_id: str,
        text: str,
        metadata: dict[str, Any],
        *,
        progress: tuple[int, int] | None = None,
    ) -> None:
        """Embed and upsert a chunk. Optional ``progress=(i, total)`` logs X/Y."""
        safe_meta = _sanitize_metadata(metadata)
        from zettel.llm import clip_text
        self._embed_call_count = getattr(self, "_embed_call_count", 0) + 1
        if progress:
            i, total = progress
            logger.info(
                "Embedding [%d] upsert chunk %d/%d %s | %s",
                self._embed_call_count, i, total, chunk_id, clip_text(text),
            )
        else:
            logger.info(
                "Embedding [%d] upsert chunk %s | %s",
                self._embed_call_count, chunk_id, clip_text(text),
            )
        self.chunks.upsert(ids=[chunk_id], documents=[text], metadatas=[safe_meta])
        if progress:
            i, tot = progress
            self._record_embed_usage(
                text, label=f"chunk:{chunk_id}", step=i, total=tot, kind="chunk",
            )
        else:
            self._record_embed_usage(text, label=f"chunk:{chunk_id}")

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
            COL_LITERATURE: self.literature,
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
        from zettel.llm import clip_text
        self._embed_call_count = getattr(self, "_embed_call_count", 0) + 1
        logger.info(
            "Embedding [%d] upsert nota %s | %s",
            self._embed_call_count,
            note_id,
            clip_text(embeddable_text),
        )
        self.permanent.upsert(ids=[note_id], documents=[embeddable_text], metadatas=[safe_meta])
        self._record_embed_usage(embeddable_text, label=f"note:{note_id}")
        logger.debug("Index: upsert nota permanente %s", note_id)

    def upsert_literature_note(
        self, literature_id: str, embeddable_text: str, metadata: dict[str, Any]
    ) -> None:
        """Index an approved granular literature note (only after review)."""
        safe_meta = _sanitize_metadata(metadata)
        from zettel.llm import clip_text
        self._embed_call_count = getattr(self, "_embed_call_count", 0) + 1
        logger.info(
            "Embedding [%d] upsert LIT %s | %s",
            self._embed_call_count,
            literature_id,
            clip_text(embeddable_text),
        )
        self.literature.upsert(
            ids=[literature_id], documents=[embeddable_text], metadatas=[safe_meta]
        )
        self._record_embed_usage(embeddable_text, label=f"lit:{literature_id}")
        logger.debug("Index: upsert literature_note %s", literature_id)

    def delete_literature_notes(self, literature_ids: list[str]) -> None:
        if literature_ids:
            self.literature.delete(ids=literature_ids)
            logger.debug("Index: %d literature_notes removidos", len(literature_ids))

    def delete_mocs(self, moc_ids: list[str]) -> None:
        if moc_ids:
            self.mocs_col.delete(ids=moc_ids)
            logger.debug("Index: %d mocs removidos", len(moc_ids))

    def query_similar_notes(self, query_text: str, n_results: int = 5,
                            exclude_id: str | None = None) -> list[dict]:
        """Find the most similar permanent notes to the given text."""
        from zettel.llm import clip_text
        self._embed_call_count = getattr(self, "_embed_call_count", 0) + 1
        logger.info(
            "Embedding [%d] busca notas | n=%d | query=%s",
            self._embed_call_count,
            n_results,
            clip_text(query_text),
        )
        results = self.permanent.query(
            query_texts=[query_text],
            n_results=min(n_results + (1 if exclude_id else 0), self.permanent.count() or 1),
        )
        self._record_embed_usage(query_text, label="query_notes")
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

        from zettel.llm import clip_text
        self._embed_call_count = getattr(self, "_embed_call_count", 0) + 1
        preview = " | ".join(clip_text(t, 40) for t in texts[:3])
        if len(texts) > 3:
            preview += f" (+{len(texts) - 3} textos)"
        logger.info(
            "Embedding [%d] busca chunks | amostras=%d n=%d | %s",
            self._embed_call_count,
            len(texts),
            n_results,
            preview,
        )

        raw = self.chunks.query(
            query_texts=texts,
            n_results=min(n_results, self.chunks.count()),
        )
        for t in texts:
            self._record_embed_usage(t, label="query_chunks")
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
        self._record_embed_usage(summary, label=f"moc:{moc_id}")
        logger.info("Index: upsert MOC %s", moc_id)

    def _record_embed_usage(
        self,
        text: str,
        *,
        label: str = "",
        step: int | None = None,
        total: int | None = None,
        kind: str | None = None,
    ) -> None:
        """Attribute estimated embedding tokens/cost to the active CostTracker."""
        from zettel.pricing import estimate_embed_cost, estimate_embed_tokens
        from zettel.usage import get_tracker, record_embed

        if get_tracker() is None:
            return
        tokens = estimate_embed_tokens(text)
        cost = estimate_embed_cost(
            self.embedding_model,
            tokens,
            provider=self.embedding_provider,
        )
        record_embed(
            model=self.embedding_model,
            tokens=tokens,
            cost_usd=cost,
            label=label,
            step=step,
            total=total,
            kind=kind,
        )

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

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed arbitrary texts with the same function used for Chroma collections."""
        if not texts:
            return []
        if self.embedding_fn is None:
            raise RuntimeError("Embedding function not configured")
        vectors = self.embedding_fn(texts)
        return [list(v) for v in vectors]


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
