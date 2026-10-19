"""Human review decisions kept as labels (issue #270).

The pipeline writes one row per decision and never reads it to decide. Purge
and delete-source leave the rows in place: the snapshot is the chunk text and
the candidates at the moment of the decision.
"""

from __future__ import annotations

import json
from typing import Any

from zettel.state.base import StateBase
from zettel.time import now_utc_iso


class ReviewsMixin(StateBase):
    def record_review_event(
        self,
        *,
        source_id: str,
        chunk_id: str,
        decision: str,
        mode: str,
        surface: str,
        snapshot: dict[str, Any],
        concept_id: str | None = None,
        review_confidence: float | None = None,
        run_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO review_events (
                   source_id, chunk_id, concept_id, decision, mode,
                   review_confidence, surface, run_id, snapshot_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                chunk_id,
                concept_id,
                decision,
                mode,
                review_confidence,
                surface,
                run_id,
                json.dumps(snapshot, ensure_ascii=False),
                now_utc_iso(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_review_events(self, chunk_id: str | None = None) -> list[dict[str, Any]]:
        if chunk_id:
            rows = self._fetchall(
                "SELECT * FROM review_events WHERE chunk_id=? ORDER BY event_id",
                (chunk_id,),
            )
        else:
            rows = self._fetchall("SELECT * FROM review_events ORDER BY event_id")
        return [_decode_review_event(row) for row in rows]

    def review_label_summary(self, limiar: float) -> list[dict[str, Any]]:
        """Per source: manual approvals, threshold approvals, rejects above the limiar."""
        rows = self._fetchall(
            """SELECT source_id,
                      SUM(CASE WHEN decision='approve' AND mode='manual' THEN 1 ELSE 0 END)
                        AS manual_approve,
                      SUM(CASE WHEN decision='approve' AND mode='threshold' THEN 1 ELSE 0 END)
                        AS threshold_approve,
                      SUM(CASE WHEN decision='reject' AND review_confidence >= ? THEN 1 ELSE 0 END)
                        AS reject_above,
                      SUM(CASE WHEN decision IN ('approve', 'reject')
                                AND review_confidence >= ? THEN 1 ELSE 0 END)
                        AS decided_above
                 FROM review_events
                GROUP BY source_id
                ORDER BY source_id""",
            (limiar, limiar),
        )
        out = []
        for row in rows:
            decided = int(row["decided_above"] or 0)
            rejected = int(row["reject_above"] or 0)
            out.append(
                {
                    "source_id": row["source_id"],
                    "manual_approve": int(row["manual_approve"] or 0),
                    "threshold_approve": int(row["threshold_approve"] or 0),
                    "reject_above": rejected,
                    "reject_above_rate": (rejected / decided) if decided else 0.0,
                }
            )
        return out


def _decode_review_event(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    raw = item.get("snapshot_json")
    try:
        item["snapshot"] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        item["snapshot"] = {}
    return item
