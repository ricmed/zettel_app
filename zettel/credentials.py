"""Which env vars hold each provider's key -- shared by the web UI and `zettel doctor`.

Readiness only: a credential being present says nothing about it being valid.
Providers absent from these tables (ollama, sentence-transformers) need none.
"""

from __future__ import annotations

import os
from typing import Any

LLM_PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
}

EMBEDDING_PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "openai": ("CHROMA_OPENAI_API_KEY", "OPENAI_API_KEY"),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


def has_credential(required: tuple[str, ...] | None) -> bool:
    """True when no credential is required or any of ``required`` is set."""
    return required is None or any(os.getenv(name) for name in required)


def embedding_credential_env(cfg: Any) -> tuple[str, ...] | None:
    """Env vars that can hold the embedding provider's key, or None when it needs none."""
    return EMBEDDING_PROVIDER_ENV.get(cfg.embedding.provider)


def embedding_ready(cfg: Any) -> bool:
    """True when the configured embedding provider has a credential (or needs none)."""
    return has_credential(embedding_credential_env(cfg))
