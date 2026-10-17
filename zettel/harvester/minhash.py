"""MinHash over word shingles. Not consulted by harvest (#251).

Layer 5 embeds every chunk of every source. This sketch estimates Jaccard
similarity of two documents in linear time, with no embedding. Harvest still
uses the five layers in ``duplicates`` / ``biblio_dedupe``. The numbers that
would justify a threshold were measured on constructed pairs, not on the vault.
"""

from __future__ import annotations

import hashlib

from zettel.hashing import fold_for_match

SHINGLE_WORDS = 5
NUM_HASHES = 128
_PRIME = (1 << 61) - 1


def word_shingles(text: str, n: int = SHINGLE_WORDS) -> set[tuple[str, ...]]:
    """Contiguous ``n``-word windows of ``fold_for_match(text)``."""
    words = fold_for_match(text).split()
    if not words:
        return set()
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def jaccard(left: set[tuple[str, ...]], right: set[tuple[str, ...]]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def containment(part: set[tuple[str, ...]], whole: set[tuple[str, ...]]) -> float:
    """Share of ``part``'s shingles that also occur in ``whole``."""
    if not part:
        return 0.0
    return len(part & whole) / len(part)


def _shingle_hash(shingle: tuple[str, ...]) -> int:
    digest = hashlib.sha256(" ".join(shingle).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _coefficients(k: int, seed: int) -> list[tuple[int, int]]:
    """Deterministic hash coefficients. Not a secret; the seed only fixes the sketch."""
    coeffs: list[tuple[int, int]] = []
    for index in range(k):
        digest = hashlib.sha256(f"{seed}:{index}".encode()).digest()
        factor = int.from_bytes(digest[:8], "big") % (_PRIME - 1) + 1
        shift = int.from_bytes(digest[8:16], "big") % _PRIME
        coeffs.append((factor, shift))
    return coeffs


def minhash_signature(
    shingles: set[tuple[str, ...]],
    k: int = NUM_HASHES,
    seed: int = 1,
) -> tuple[int, ...]:
    """``k`` minimum hashes. Empty input is ``k`` zeros."""
    coeffs = _coefficients(k, seed)
    if not shingles:
        return tuple(0 for _ in range(k))
    hashed = [_shingle_hash(shingle) for shingle in shingles]
    signature: list[int] = []
    for factor, shift in coeffs:
        signature.append(min((factor * value + shift) % _PRIME for value in hashed))
    return tuple(signature)


def minhash_jaccard(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """Fraction of equal slots. Estimates Jaccard, with seed fixed at build time."""
    if len(left) != len(right) or not left:
        return 0.0
    return sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
