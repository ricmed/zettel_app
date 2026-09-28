"""Query-term facts shared by the lexical search, the topic index and the retriever.

A leaf module (no SQLite, no Chroma): what counts as "a term of the query" is a
pure text fact, so ``retrieval``, ``topic_index`` and ``zettel.state`` can all
import it without dragging each other in.
"""

from __future__ import annotations

import re
import unicodedata

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# High-frequency PT-BR closed-class words (articles, prepositions, conjunctions,
# pronouns, common verb forms). Without this filter, a token like "que" appears
# in nearly every note's body, so the OR-joined MATCH expression matches almost
# the entire corpus regardless of topic — which in turn defeats any bm25-based
# relevance signal (a "hit" stops meaning anything). Comparison is case-insensitive;
# entries are stored lowercase.
PT_STOPWORDS = frozenset(
    {
        "a",
        "o",
        "as",
        "os",
        "um",
        "uma",
        "uns",
        "umas",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "em",
        "na",
        "no",
        "nas",
        "nos",
        "por",
        "para",
        "com",
        "sem",
        "sobre",
        "entre",
        "ate",
        "apos",
        "e",
        "ou",
        "mas",
        "que",
        "se",
        "como",
        "quando",
        "onde",
        "porque",
        "pois",
        "eu",
        "tu",
        "ele",
        "ela",
        "voces",
        "eles",
        "elas",
        "seu",
        "sua",
        "seus",
        "suas",
        "este",
        "esta",
        "esse",
        "essa",
        "isso",
        "aquele",
        "aquela",
        "aquilo",
        "sao",
        "foi",
        "foram",
        "ser",
        "estar",
        "estao",
        "tem",
        "teve",
        "ha",
        "nao",
        "sim",
        "mais",
        "muito",
        "muitos",
        "muitas",
        "ja",
        "ainda",
        "tambem",
        "qual",
        "quais",
    }
)


def fold(text: str | None) -> str:
    """Accent- and case-insensitive key used to merge equivalent terms.

    Also registered on the SQLite connection as ``zfold`` so ``LIKE`` can run
    inside SQLite (ASCII-only case-insensitivity would miss ``função`` vs
    ``funcao``).
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[\s_-]+", " ", folded).strip()


def fts_query_terms(text: str, min_len: int = 2, max_tokens: int = 32) -> list[str]:
    """The distinct, lowercased terms a query sends to FTS5.

    The single definition behind :func:`fts_match_expr` and the retriever's
    bypass-coverage check, so the two can never disagree about what "the
    query's terms" means. Truncation at ``max_tokens`` happens *before* dedupe.
    """
    tokens = [
        t for t in _TOKEN_RE.findall(text) if len(t) >= min_len and t.lower() not in PT_STOPWORDS
    ][:max_tokens]
    return list(dict.fromkeys(t.lower() for t in tokens))


def fts_match_expr(text: str, min_len: int = 2, max_tokens: int = 32) -> str | None:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Each term is wrapped in double quotes, which neutralizes every FTS5
    operator (``-``, ``*``, ``NEAR``, ``:``, ``AND``/``OR``/``NOT``), so raw user
    text can never inject query syntax. Terms are joined with ``OR`` because a
    natural-language question rarely has *all* its terms in a single note — bm25
    ranks whoever matches more terms, and RRF fuses with the vector side.
    Stopwords (see ``PT_STOPWORDS``) are dropped so they can't turn "matches
    almost every note" into a false relevance signal.

    Returns ``None`` when there is no usable term (caller should treat as empty).
    """
    terms = fts_query_terms(text, min_len=min_len, max_tokens=max_tokens)
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)
