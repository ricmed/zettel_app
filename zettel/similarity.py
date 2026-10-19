"""One conversion from a Chroma distance to cosine similarity.

Collections are created with ``hnsw.space = cosine`` (ADR-058). Chroma then
returns ``1 - cos``. An older collection may still be squared L2 (``2 - 2·cos``
on unit vectors); callers pass the space the collection actually reports
instead of guessing.

Thresholds in config are cosine. Do not convert a distance at the call site.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# Declared on every collection this process creates.
HNSW_SPACE = "cosine"


def cosine_from_distance(distance: float, space: str) -> float:
    """Cosine similarity implied by a Chroma distance in ``space``.

    ``cosine``: distance is ``1 - cos``.
    ``l2``: distance is squared Euclidean; on unit vectors that is ``2 - 2·cos``.
    """
    if space == "cosine":
        return 1.0 - distance
    if space == "l2":
        return 1.0 - distance / 2.0
    raise ValueError(f"espaco de distancia desconhecido: {space}")


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Dot product of two L2-normalised vectors (the cosine).

    ``VectorIndex.embed_texts`` returns normalised vectors. A zero vector
    scores 0, so it never clears a positive duplicate threshold.
    """
    if len(left) != len(right):
        raise ValueError("vetores de tamanhos diferentes")
    dot = 0.0
    norm_l = 0.0
    norm_r = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        norm_l += a * a
        norm_r += b * b
    if norm_l == 0.0 or norm_r == 0.0:
        return 0.0
    return dot / math.sqrt(norm_l * norm_r)
