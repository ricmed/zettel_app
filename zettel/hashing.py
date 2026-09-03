"""Canonical hashing utilities for drift-resistant Zettelkasten pipeline.

Implements layered hashing strategy:
  file_checksum -> extraction_checksum -> chapter_checksum ->
  chunk_checksum -> llm_call_checksum -> note_semantic_checksum
"""

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


_HYPHEN_LINEBREAK_RE = re.compile(r"(\w)-[ \t]*\n[ \t]*(\w)")


def dehyphenate_pdf_linebreaks(text: str) -> str:
    """Merge a PDF's line-break hyphenation: ``"pala-\\nvra"`` -> ``"palavra"``.

    Only meaningful for PDF-originated text; native Markdown may end a line
    on a legitimate hyphen and must not be touched (callers are responsible
    for only applying this to PDF extraction output).

    The character right after the break is checked: lowercase means this is
    almost always the tail of a word split by the PDF's line layout, so the
    hyphen and line break are removed. Uppercase is a weak but cheap signal
    of a genuine hyphenated compound (a heading, a proper noun) and is left
    untouched. This does **not** catch a genuine compound whose continuation
    happens to be lowercase (e.g. ``"bem-\\nvindo"`` still merges into the
    wrong ``"bemvindo"``) — a known limitation with no fix short of a
    dictionary lookup, not attempted here.

    Idempotent: re-running on already-merged text is a no-op.
    """
    def _join(m: re.Match[str]) -> str:
        before, after = m.group(1), m.group(2)
        if after.isupper():
            return m.group(0)
        return f"{before}{after}"
    return _HYPHEN_LINEBREAK_RE.sub(_join, text)


def normalize_text_for_hash(text: str) -> str:
    """Normalize text canonically before hashing to prevent false drift.

    Steps:
      1. Unicode NFKC normalization
      2. Line break normalization (CRLF -> LF)
      3. Collapse decorative whitespace
      4. Fix common PDF hyphenation artifacts
      5. Limit consecutive blank lines
    """
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = dehyphenate_pdf_linebreaks(t)
    return t.strip()


def fold_for_match(text: str) -> str:
    """Fold text for fuzzy quote-grounding comparisons.

    Applies `normalize_text_for_hash` first, then strips accents, lowercases
    and collapses everything that is not alphanumeric to a single space —
    tolerating case, accent and editorial punctuation differences without
    duplicating the canonical hash normalizer.
    """
    t = normalize_text_for_hash(text)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def quote_is_grounded(quote: str, chunk_text: str, min_ratio: float = 0.85) -> bool:
    """True if `quote` is verbatim (or near-verbatim) inside `chunk_text`.

    Exact substring match on the folded text first; if that fails, sums the
    sizes of every matching block between the two (not just the longest
    one) so an editorial ellipsis ("[...]", "(...)") splitting an otherwise
    verbatim quote into two grounded halves still clears `min_ratio`, while
    a paraphrase — which shares little more than common short words —
    stays far below it.
    """
    q = fold_for_match(quote)
    if not q:
        return False
    c = fold_for_match(chunk_text)
    if q in c:
        return True
    matcher = SequenceMatcher(None, q, c, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return (matched / len(q)) >= min_ratio


def sha256_hex(s: str) -> str:
    """Return SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Return SHA-256 hex digest of a file's raw bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def short_hash(text: str, length: int = 8) -> str:
    """Return a short hash suitable for ID suffixes."""
    return sha256_hex(text)[:length]


def compute_llm_call_checksum(
    prompt_hash: str,
    chunk_checksum: str,
    model: str,
    temperature: float,
    language: str,
    rag_context_checksum: str = "",
    provider: str = "",
    top_p: float | None = None,
) -> str:
    """Deterministic checksum for an LLM call, enabling response caching.

    ``provider`` and ``top_p`` are part of the payload because a ``model``
    string can be shared across providers/gateways (OpenAI-compatible), and
    ``top_p`` is forwarded to the client exactly like ``temperature`` --
    either one differing means the call is not the same, even if every other
    field matches.
    """
    parts = (
        f"{prompt_hash}|{chunk_checksum}|{model}|{temperature}|{language}|"
        f"{rag_context_checksum}|{provider}|{top_p if top_p is not None else ''}"
    )
    return sha256_hex(parts)


def compute_embedding_input_hash(semantic_checksum: str, provider: str, model: str) -> str:
    """Deterministic hash of what a note's vector was built from.

    Lets callers skip re-embedding when the semantic content and the embedding
    provider/model are unchanged.
    """
    return sha256_hex(f"{provider}|{model}|{semantic_checksum}")


def compute_pipeline_signature(config: dict) -> str:
    """Hash the pipeline configuration that affects outputs.

    Includes: prompt hashes, model info, chunking params, thresholds.
    """
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False)
    return sha256_hex(canonical)


def extract_embeddable_text(markdown_body: str) -> str:
    """Strip frontmatter and managed blocks, returning only semantic content."""
    lines = markdown_body.split("\n")
    result_lines: list[str] = []
    in_frontmatter = False
    in_managed_block = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip YAML frontmatter
        if i == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        # Skip managed blocks
        if re.match(r"<!--\s*zettel:auto-.*:start\s*-->", stripped):
            in_managed_block = True
            continue
        if re.match(r"<!--\s*zettel:auto-.*:end\s*-->", stripped):
            in_managed_block = False
            continue
        if in_managed_block:
            continue
        result_lines.append(line)

    return normalize_text_for_hash("\n".join(result_lines))
