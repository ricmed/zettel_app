"""Canonical hashing utilities for drift-resistant Zettelkasten pipeline.

Implements layered hashing strategy:
  file_checksum -> extraction_checksum -> chapter_checksum ->
  chunk_checksum -> llm_call_checksum -> note_semantic_checksum
"""

import hashlib
import json
import re
import unicodedata
from pathlib import Path


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
    # Fix simple PDF hyphenation: "word-\ncontinuation" -> "wordcontinuation"
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)
    return t.strip()


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
) -> str:
    """Deterministic checksum for an LLM call, enabling response caching."""
    parts = f"{prompt_hash}|{chunk_checksum}|{model}|{temperature}|{language}|{rag_context_checksum}"
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
