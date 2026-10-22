"""Shared test doubles. A helper module, not a test file: test files import from here,
never from each other.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

OFFLINE_EMBEDDING_PROVIDER = "test-token-hash"


class TokenHashEmbeddings:
    """Bag of lowercased word tokens hashed into a small space.

    Deterministic, offline and cheap; texts that share words get close vectors,
    which is all a retrieval or dedupe test needs from an embedding.
    """

    dims = 64

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for token in re.findall(r"\w+", text.lower()):
            vec[int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.dims] += 1.0
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def token_hash_ef_builder(_index: Any, model: str) -> Any:
    """A ``zettel.index._EF_BUILDERS`` entry: unit vectors, as the floor assumes (ADR-052)."""
    from zettel.index import _LangChainChromaEF

    class _TokenHashEF(_LangChainChromaEF):
        normalize = True

        @staticmethod
        def name() -> str:
            return "zettel_test_token_hash"

        @classmethod
        def _client(cls, config: dict[str, Any]) -> _LangChainChromaEF:
            return cls(TokenHashEmbeddings(), model=config["model"])

    return _TokenHashEF(TokenHashEmbeddings(), model=model)


def unit_token_hash(text: str) -> list[float]:
    """``TokenHashEmbeddings`` vector scaled to unit norm, as the real adapter returns it."""
    vec = TokenHashEmbeddings().embed_query(text)
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


COLLECTIONS = ("sources", "chunks", "permanent_notes", "chapter_summaries")


def calls_to(index: FakeVectorIndex, method: str) -> list[Any]:
    """First argument of every call to ``method``, in order."""
    return [arg for name, arg in index.calls if name == method]


def deleted(index: FakeVectorIndex, collection: str) -> list[str]:
    """Every id deleted from ``collection``, flattened, in order."""
    return [i for ids in calls_to(index, f"delete_{collection}") for i in ids]


class FakeVectorIndex:
    """In-memory stand-in for ``zettel.index.VectorIndex`` (#327).

    Every public method of the real index exists here with the same signature,
    which ``tests/test_fakes_contract.py`` checks against the real class, so a
    change to ``VectorIndex`` breaks this fake instead of passing silently.

    Upserts and deletes act on per-collection stores (``store[collection][id]``
    holds ``(document, metadata)``). Searches return only what the test
    configured -- ``notes``, ``chunks`` and ``chapters`` map an id to the cosine
    distance (``1 - cos``, ADR-058) the search reports -- so a test states what
    the search "found" instead of relying on an embedding. ``vectors`` maps a
    text to the vector ``embed_texts`` returns; any other text gets a unit
    token-hash vector. ``fail_on`` maps a method name to the call number that
    raises (1-based), for crash-resume tests: an ``int`` raises ``RuntimeError``,
    ``(n, exception)`` raises that exception. ``calls`` records
    ``(method, first argument)`` in order; ``calls_to`` and ``deleted`` read it.

    A test that needs different behaviour subclasses it and overrides methods
    with the real parameter names (also checked by the contract test).
    """

    distance_space = "cosine"

    def __init__(
        self,
        *,
        notes: dict[str, float] | None = None,
        chunks: dict[str, float] | None = None,
        chapters: dict[str, float] | None = None,
        vectors: dict[str, list[float]] | None = None,
        fail_on: dict[str, int | tuple[int, BaseException]] | None = None,
    ) -> None:
        self.store: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {c: {} for c in COLLECTIONS}
        self.ranked = {
            "permanent_notes": dict(notes or {}),
            "chunks": dict(chunks or {}),
            "chapter_summaries": dict(chapters or {}),
        }
        self.vectors = dict(vectors or {})
        self.fail_on = dict(fail_on or {})
        self.calls: list[tuple[str, Any]] = []
        self.reset_calls: list[str] = []

    def _record(self, method: str, arg: Any = None) -> None:
        self.calls.append((method, arg))
        count = sum(1 for name, _ in self.calls if name == method)
        spec = self.fail_on.get(method)
        at, error = spec if isinstance(spec, tuple) else (spec, None)
        if at == count:
            raise error or RuntimeError(f"falha injetada em {method} (chamada {count})")

    def _hits(self, collection: str, n_results: int, *, exclude_id=None, where=None, ids=None):
        out: list[dict] = []
        for hid, distance in sorted(self.ranked[collection].items(), key=lambda kv: kv[1]):
            if hid == exclude_id or (ids is not None and hid not in ids):
                continue
            document, metadata = self.store[collection].get(hid, (f"doc {hid}", {}))
            if where and any(metadata.get(k) != v for k, v in where.items()):
                continue
            out.append(
                {
                    "id": hid,
                    "document": document,
                    "metadata": metadata,
                    "distance": distance,
                    "distance_space": self.distance_space,
                }
            )
        return out[:n_results]

    # -- identity and maintenance ------------------------------------------

    def collection_space(self, collection: Any) -> str:
        return self.distance_space

    def to_cosine(self, distance: float) -> float:
        return 1.0 - distance

    def embedding_space_matches(
        self, stored: tuple[str | None, str | None, int | None] | None = None
    ) -> bool:
        return True

    def get_stored_embedding_identity(self) -> tuple[str | None, str | None, int | None]:
        return ("fake", "fake", None)

    def reset_collection(self, name: str) -> Any:
        self._record("reset_collection", name)
        self.reset_calls.append(name)
        self.store[name] = {}
        return None

    def vacuum(self) -> Path | None:
        self._record("vacuum")
        return None

    def close(self) -> None:
        self._record("close")

    def existing_ids(self, collection_name: str, ids: list[str]) -> set[str]:
        if collection_name not in self.store:
            raise ValueError(f"Colecao desconhecida: {collection_name}")
        return {i for i in ids if i in self.store[collection_name]}

    # -- writes --------------------------------------------------------------

    def upsert_source(self, source_id: str, summary: str, metadata: dict[str, Any]) -> None:
        self._record("upsert_source", source_id)
        self.store["sources"][source_id] = (summary, dict(metadata))

    def upsert_chunk(
        self,
        chunk_id: str,
        text: str,
        metadata: dict[str, Any],
        *,
        progress: tuple[int, int] | None = None,
    ) -> None:
        self._record("upsert_chunk", chunk_id)
        self.store["chunks"][chunk_id] = (text, dict(metadata))

    def upsert_permanent_note(
        self, note_id: str, embeddable_text: str, metadata: dict[str, Any]
    ) -> None:
        self._record("upsert_permanent_note", note_id)
        self.store["permanent_notes"][note_id] = (embeddable_text, dict(metadata))

    def upsert_chapter_summary(
        self, chapter_id: str, embeddable_text: str, metadata: dict[str, Any]
    ) -> None:
        self._record("upsert_chapter_summary", chapter_id)
        self.store["chapter_summaries"][chapter_id] = (embeddable_text, dict(metadata))

    def _delete(self, collection: str, ids: list[str]) -> None:
        self._record(f"delete_{collection}", list(ids))
        for item in ids:
            self.store[collection].pop(item, None)

    def delete_sources(self, source_ids: list[str]) -> None:
        self._delete("sources", source_ids)

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        self._delete("chunks", chunk_ids)

    def delete_permanent_notes(self, note_ids: list[str]) -> None:
        self._delete("permanent_notes", note_ids)

    def delete_chapter_summaries(self, chapter_ids: list[str]) -> None:
        self._delete("chapter_summaries", chapter_ids)

    # -- reads ---------------------------------------------------------------

    def chunk_ids_for_source(self, source_id: str) -> set[str]:
        return {
            cid
            for cid, (_doc, meta) in self.store["chunks"].items()
            if meta.get("source_id") == source_id
        }

    def count_permanent_notes(self) -> int:
        return len(self.store["permanent_notes"])

    def embed_texts(self, texts: list[str], *, label: str = "embed_texts") -> list[list[float]]:
        self._record("embed_texts", list(texts))
        return [list(self.vectors.get(t) or unit_token_hash(t)) for t in texts]

    def get_all_permanent_embeddings(self) -> tuple[list[str], list[list[float]]]:
        ids = list(self.store["permanent_notes"])
        return ids, self.embed_texts([self.store["permanent_notes"][i][0] for i in ids])

    def query_similar_notes(
        self,
        query_text: str,
        n_results: int = 5,
        exclude_id: str | None = None,
        *,
        purpose: str = "densa",
        where: dict[str, Any] | None = None,
    ) -> list[dict]:
        self._record("query_similar_notes", query_text)
        return self._hits("permanent_notes", n_results, exclude_id=exclude_id, where=where)

    def query_notes_by_ids(
        self,
        query_text: str,
        note_ids: list[str],
        *,
        purpose: str = "topic index",
    ) -> list[dict]:
        self._record("query_notes_by_ids", list(note_ids))
        return self._hits("permanent_notes", len(note_ids), ids=set(note_ids))

    def query_chapter_summaries(self, query_text: str, n_results: int = 20) -> list[dict]:
        self._record("query_chapter_summaries", query_text)
        return self._hits("chapter_summaries", n_results)

    def find_similar_chunks(self, texts: list[str], n_results: int = 3) -> list[dict]:
        self._record("find_similar_chunks", list(texts))
        return [hit for _t in texts for hit in self._hits("chunks", n_results)]
