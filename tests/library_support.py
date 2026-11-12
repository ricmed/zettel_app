"""Shared setup for the Estante tests (ADR-068): a config on a throwaway vault."""

from __future__ import annotations

from pathlib import Path

from zettel.config import AppConfig
from zettel.state import StateDB

REPO = Path(__file__).resolve().parents[1]


def library_config(tmp_path: Path, **overrides) -> AppConfig:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        vault_path=vault,
        state_db_path=tmp_path / "state.db",
        library={"rating_path": REPO / "config" / "library.yaml", **overrides},
    )


def library_db(cfg: AppConfig) -> StateDB:
    return StateDB(cfg.state_db_path)
