"""Tests for zettel.time — UTC SQLite vs vault timezone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from zettel.time import (
    format_local_datetime,
    now_filename_ts,
    now_utc_iso,
    now_vault_iso,
    vault_date_iso,
)


def test_now_utc_iso_has_offset():
    raw = now_utc_iso()
    dt = datetime.fromisoformat(raw)
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_now_vault_iso_america_sao_paulo_offset():
    raw = now_vault_iso("America/Sao_Paulo")
    dt = datetime.fromisoformat(raw)
    assert dt.tzinfo is not None
    # Standard time UTC-3 (DST may be -2; both are valid offsets)
    offset_hours = dt.utcoffset().total_seconds() / 3600
    assert offset_hours in (-3, -2)


def test_format_local_datetime_from_utc():
    utc = datetime(2026, 3, 15, 15, 30, tzinfo=UTC).isoformat()
    shown = format_local_datetime(utc, "America/Sao_Paulo", style="datetime")
    assert shown == "15/03/2026 12:30"


def test_format_local_datetime_date_style():
    utc = datetime(2026, 3, 15, 15, 30, tzinfo=UTC).isoformat()
    assert format_local_datetime(utc, "America/Sao_Paulo", style="date") == "2026-03-15"


def test_format_local_datetime_rejects_naive():
    with pytest.raises(ValueError, match="timezone-aware"):
        format_local_datetime("2026-03-15T12:00:00", "America/Sao_Paulo")


def test_now_filename_ts_uses_vault_zone():
    fixed = datetime(2026, 6, 10, 14, 5, 9, tzinfo=ZoneInfo("America/Sao_Paulo"))

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else datetime.now(UTC)

    import zettel.time as time_mod

    original = time_mod.datetime
    time_mod.datetime = _FixedDatetime  # type: ignore[misc, assignment]
    try:
        assert now_filename_ts("America/Sao_Paulo") == "20260610-140509"
    finally:
        time_mod.datetime = original


def test_vault_date_iso():
    fixed = datetime(2026, 1, 2, 23, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else datetime.now(UTC)

    import zettel.time as time_mod

    original = time_mod.datetime
    time_mod.datetime = _FixedDatetime  # type: ignore[misc, assignment]
    try:
        assert vault_date_iso("America/Sao_Paulo") == "2026-01-02"
    finally:
        time_mod.datetime = original
