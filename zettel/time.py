"""Timestamps: UTC for SQLite, vault_timezone for Obsidian frontmatter."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def now_utc_iso() -> str:
    """ISO 8601 UTC for SQLite and operational rows."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def now_vault_iso(vault_timezone: str) -> str:
    """ISO 8601 with explicit offset for vault frontmatter."""
    return datetime.now(ZoneInfo(vault_timezone)).isoformat(timespec="seconds")


def now_filename_ts(vault_timezone: str) -> str:
    """Compact local timestamp for note filenames (ASK/ART)."""
    return datetime.now(ZoneInfo(vault_timezone)).strftime("%Y%m%d-%H%M%S")


def vault_date_iso(vault_timezone: str) -> str:
    """Calendar date in vault_timezone (skill export headers)."""
    return datetime.now(ZoneInfo(vault_timezone)).date().isoformat()


def format_local_datetime(
    iso_text: str,
    vault_timezone: str,
    *,
    style: str = "datetime",
) -> str:
    """Render a timezone-aware ISO string in vault_timezone for the UI."""
    dt = datetime.fromisoformat(iso_text)
    if dt.tzinfo is None:
        msg = f"timestamp must be timezone-aware: {iso_text!r}"
        raise ValueError(msg)
    local = dt.astimezone(ZoneInfo(vault_timezone))
    if style == "date":
        return local.date().isoformat()
    return local.strftime("%d/%m/%Y %H:%M")
