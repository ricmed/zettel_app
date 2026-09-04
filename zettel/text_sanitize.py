"""Invisible-Unicode hygiene for the document -> LLM chain.

Extracted text travels from a PDF/Markdown file into the prompt, the vault and
the embedding. Zero-width characters and the Unicode tag block (U+E0000-U+E007F)
survive every one of those hops while being invisible to the reviewer, so a
document can smuggle instructions past the HITL gate. They are stripped once, at
extraction time, before the extraction checksum is computed.

Deliberately conservative: only characters with no visible rendering are removed.
NBSP, hyphens and ordinary punctuation are left to
``hashing.normalize_text_for_hash``, which is the canonical normalizer and must
stay the only place that rewrites visible text.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# Zero-width, bidi and format controls with no visible glyph, plus the Unicode
# tag block used to encode hidden ASCII inside otherwise clean-looking text.
INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x200B, 0x200F),  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    (0x202A, 0x202E),  # bidi embedding / override
    (0x2060, 0x2064),  # word joiner, invisible operators
    (0x2066, 0x2069),  # bidi isolates
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
    (0xE0000, 0xE007F),  # Unicode tag block
)

_INVISIBLE_RE = re.compile(
    "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in INVISIBLE_RANGES) + "]"
)
_WHITESPACE_RE = re.compile(r"\s+")


def strip_invisible_unicode(text: str) -> tuple[str, int]:
    """Remove invisible Unicode from ``text``.

    Returns ``(clean_text, n_removed)``. Idempotent: a second pass removes zero.
    """
    if not text:
        return text, 0
    return _INVISIBLE_RE.subn("", text)


def sanitize_extracted_text(text: str, label: str) -> str:
    """Strip invisible Unicode and log how much was removed for ``label``."""
    clean, removed = strip_invisible_unicode(text)
    if removed:
        logger.info("sanitize: removed %d invisible chars from %s", removed, label)
    return clean


def visible_char_count(text: str) -> int:
    """Number of characters left after dropping invisibles and whitespace."""
    clean, _ = strip_invisible_unicode(text)
    return len(_WHITESPACE_RE.sub("", clean))
