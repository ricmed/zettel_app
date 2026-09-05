"""Run identity: what was evaluated, against what, under which rules.

A score is meaningless without the envelope it was measured in. This module
makes that envelope explicit and hashable: identical inputs must produce an
identical identity, and a run missing any required field must fail to build
rather than silently compare against something else.

Nothing here reads the environment or a `.env` file. Secrets have no business
in an artifact meant to be committed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

# The retrieval knobs a condition is actually judged against. Anything outside
# this list can change without invalidating a comparison.
FLOOR_KEYS: tuple[str, ...] = (
    "mode",
    "topk",
    "rrf_k",
    "relevance_floor_enabled",
    "min_vector_similarity",
    "absolute_min_similarity",
    "bm25_hit_bypasses_floor",
    "bm25_bypass_max_rank",
    "topic_index_boost",
    "graph_expansion_used",
    "graph_max_hops",
)

_REQUIRED = (
    "condition",
    "fixture_hash",
    "question_set_hash",
    "llm_model",
    "embedding_model",
)


class ManifestError(ValueError):
    """A manifest is missing a required field or carries something it should not."""


@dataclass(frozen=True)
class RunManifest:
    """Everything needed to say two runs are comparable."""

    condition: str
    fixture_hash: str
    question_set_hash: str
    llm_model: str
    embedding_model: str
    retrieval_params: dict = field(default_factory=dict)
    commit_sha: str = ""
    max_calls: int = 0
    max_input_tokens: int = 0
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


def hash_files(paths: list[Path]) -> str:
    """Stable digest of a set of files, keyed by relative name and content.

    Line endings are normalised to LF first. Fixtures are committed text, and
    git rewrites them per platform on checkout — without this, the same fixture
    hashes differently on Windows and Linux and the committed baseline in
    ``evals/results/`` could never match on both.
    """
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def hash_json(payload: object) -> str:
    """Stable digest of any JSON-serialisable payload."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def current_commit_sha() -> str:
    """Repo HEAD, or empty when git is unavailable (a tarball, a sandbox)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def build_manifest(
    *,
    condition: str,
    fixture_hash: str,
    question_set_hash: str,
    llm_model: str,
    embedding_model: str,
    retrieval_params: dict | None = None,
    commit_sha: str | None = None,
    max_calls: int = 0,
    max_input_tokens: int = 0,
) -> RunManifest:
    """Build a manifest, refusing anything incomplete or secret-bearing."""
    manifest = RunManifest(
        condition=condition,
        fixture_hash=fixture_hash,
        question_set_hash=question_set_hash,
        llm_model=llm_model,
        embedding_model=embedding_model,
        retrieval_params=_floor_subset(retrieval_params or {}),
        commit_sha=current_commit_sha() if commit_sha is None else commit_sha,
        max_calls=max_calls,
        max_input_tokens=max_input_tokens,
    )
    missing = [f for f in _REQUIRED if not getattr(manifest, f)]
    if missing:
        raise ManifestError(
            f"Manifesto incompleto: campo(s) obrigatorio(s) ausente(s): {', '.join(missing)}"
        )
    _reject_secrets(manifest)
    return manifest


def _floor_subset(params: dict) -> dict:
    """Keep only the knobs a condition is judged against, in a fixed order."""
    return {key: params[key] for key in FLOOR_KEYS if key in params}


# Matched against *values only*. Field names are not evidence: `max_input_tokens`
# is a legitimate budget knob, not a credential.
_SECRET_MARKERS = ("api_key", "apikey", "secret", "password", "bearer ", "sk-", "ghp_")


def _reject_secrets(manifest: RunManifest) -> None:
    """Fail loudly rather than commit a key into `evals/results/`."""
    for value in _iter_values(manifest.as_dict()):
        folded = value.casefold()
        for marker in _SECRET_MARKERS:
            if marker in folded:
                raise ManifestError(
                    f"Manifesto contem valor parecido com segredo ({marker!r}). "
                    "Manifestos sao commitaveis; segredos ficam no .env."
                )


def _iter_values(payload: object):
    """Yield every string value in a nested payload (keys are skipped)."""
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _iter_values(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from _iter_values(item)
    elif isinstance(payload, str):
        yield payload


def manifest_identity(manifest: RunManifest) -> str:
    """Digest of the manifest — two runs with the same identity are comparable.

    ``commit_sha`` is part of it on purpose: the same questions against the same
    fixture can behave differently after a code change, so a comparison across
    commits should be visible as a different identity, not assumed away.
    """
    return hash_json(manifest.as_dict())
