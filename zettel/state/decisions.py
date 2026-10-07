"""Shadow verdicts of the typed decision layer (ADR-055).

Write-mostly: the pipeline records what the decision model would have decided
next to what it did decide, and ``scripts/report_decision_shadow.py`` reads it
back. Nothing in the pipeline branches on these rows.
"""

from __future__ import annotations

import json
from typing import Any

from zettel.state.base import StateBase


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


class DecisionsMixin(StateBase):
    def get_decision_shadow(
        self, site: str, subject_id: str, state_checksum: str
    ) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM decision_shadow WHERE site=? AND subject_id=? AND state_checksum=?",
            (site, subject_id, state_checksum),
        )
        return _decode_shadow_row(row) if row else None

    def record_decision_shadow(
        self,
        *,
        site: str,
        subject_id: str,
        state_checksum: str,
        model: str,
        state: dict[str, Any],
        baseline: dict[str, Any],
        jev: dict[str, Any] | None,
        latency_ms: int | None,
        input_tokens: int | None,
        error: str = "",
    ) -> None:
        """Insert or refresh one verdict. A human label already recorded survives."""
        now = self._now()
        self.conn.execute(
            """INSERT INTO decision_shadow
               (site, subject_id, state_checksum, model, state_json, baseline_json,
                jev_json, latency_ms, input_tokens, error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(site, subject_id, state_checksum) DO UPDATE SET
                 model=excluded.model,
                 baseline_json=excluded.baseline_json,
                 jev_json=excluded.jev_json,
                 latency_ms=excluded.latency_ms,
                 input_tokens=excluded.input_tokens,
                 error=excluded.error,
                 updated_at=excluded.updated_at""",
            (
                site,
                subject_id,
                state_checksum,
                model,
                json.dumps(state, ensure_ascii=False, sort_keys=True),
                json.dumps(baseline, ensure_ascii=False, sort_keys=True),
                json.dumps(jev, ensure_ascii=False, sort_keys=True) if jev is not None else None,
                latency_ms,
                input_tokens,
                error or None,
                now,
                now,
            ),
        )
        self.conn.commit()

    def set_decision_shadow_human(self, site: str, subject_id: str, verdict: str) -> int:
        """Attach a human decision to every shadow row of one subject."""
        cur = self.conn.execute(
            "UPDATE decision_shadow SET human_json=?, updated_at=? WHERE site=? AND subject_id=?",
            (json.dumps({"verdict": verdict}), self._now(), site, subject_id),
        )
        self.conn.commit()
        return cur.rowcount

    def list_decision_shadow(self, site: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM decision_shadow"
        params: tuple = ()
        if site:
            sql += " WHERE site=?"
            params = (site,)
        sql += " ORDER BY site, created_at, subject_id"
        return [_decode_shadow_row(r) for r in self._fetchall(sql, params)]


def _decode_shadow_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "state": _loads(row.pop("state_json")),
        "baseline": _loads(row.pop("baseline_json")),
        "jev": _loads(row.pop("jev_json")),
        "human": _loads(row.pop("human_json")),
    }
