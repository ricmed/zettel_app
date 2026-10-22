"""Shared test doubles. A helper module, not a test file: test files import from here,
never from each other.
"""

from __future__ import annotations

import hashlib
import re
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
