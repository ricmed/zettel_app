"""Web job queue (``web_jobs`` / ``web_job_events``) and the aggregate counters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from zettel.state.base import StateBase, placeholders

_COUNTED_TABLES = ("files", "sources", "chapters", "chunks", "concepts", "notes", "mocs", "assets")

# Chunk status -> counter key; `persisted` counts as approved for display.
_CHUNK_STATUS_KEYS = {
    "pending": "chunks_pending",
    "awaiting_review": "chunks_awaiting_review",
    "approved": "chunks_approved",
    "persisted": "chunks_approved",
    "rejected": "chunks_rejected",
    "failed": "chunks_failed",
}

_HUB_LIMIT = 10


def _decode_web_job(row: dict) -> dict:
    """Replace the stored JSON columns with decoded ``payload`` / ``result``."""
    row["payload"] = json.loads(row.pop("payload_json", None) or "{}")
    result = row.pop("result_json", None)
    row["result"] = json.loads(result) if result else None
    return row


class WebMixin(StateBase):
    # ── Job queue ──────────────────────────────────────────────────────

    def recover_web_jobs(self) -> int:
        """Mark jobs left running by a process restart as interrupted.

        Queued work remains queued and is picked up by the new worker.
        """
        cur = self.conn.execute(
            "UPDATE web_jobs SET state='interrupted', phase='interrupted', "
            "message='Interrompido pela reinicializacao da aplicacao', finished_at=? "
            "WHERE state='running'",
            (self._now(),),
        )
        self.conn.commit()
        return cur.rowcount

    def create_web_job(self, job_id: str, operation: str, payload: dict) -> bool:
        """Atomically enqueue a job, allowing one mutating operation at a time."""
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            active = self.conn.execute(
                "SELECT job_id FROM web_jobs WHERE state IN ('queued','running') LIMIT 1"
            ).fetchone()
            if active:
                self.conn.rollback()
                return False
            self.conn.execute(
                "INSERT INTO web_jobs "
                "(job_id, operation, payload_json, state, phase, created_at) "
                "VALUES (?, ?, ?, 'queued', 'queued', ?)",
                (job_id, operation, json.dumps(payload), self._now()),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def claim_web_job(self, job_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE web_jobs SET state='running', phase='starting', started_at=? "
            "WHERE job_id=? AND state='queued'",
            (self._now(), job_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_web_job(self, job_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM web_jobs WHERE job_id=?", (job_id,))
        return _decode_web_job(row) if row else None

    def list_web_jobs(self, limit: int = 50) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM web_jobs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        return [_decode_web_job(row) for row in rows]

    def update_web_job(
        self,
        job_id: str,
        *,
        state: str | None = None,
        phase: str | None = None,
        current_item: str | None = None,
        current_index: int | None = None,
        total_items: int | None = None,
        message: str | None = None,
        result: dict | None = None,
        error_message: str | None = None,
        run_id: int | None = None,
        finished: bool = False,
    ) -> None:
        """COALESCE-update a job; an unknown ``job_id`` is a no-op."""
        self.conn.execute(
            "UPDATE web_jobs SET state=COALESCE(?,state), phase=COALESCE(?,phase), "
            "current_item=COALESCE(?,current_item), current_index=COALESCE(?,current_index), "
            "total_items=COALESCE(?,total_items), message=COALESCE(?,message), "
            "result_json=COALESCE(?,result_json), error_message=COALESCE(?,error_message), "
            "run_id=COALESCE(?,run_id), finished_at=COALESCE(?,finished_at) "
            "WHERE job_id=?",
            (
                state,
                phase,
                current_item,
                current_index,
                total_items,
                message,
                json.dumps(result) if result is not None else None,
                error_message,
                run_id,
                self._now() if finished else None,
                job_id,
            ),
        )
        self.conn.commit()

    def add_web_job_event(
        self,
        job_id: str,
        phase: str,
        *,
        current_item: str | None = None,
        current_index: int | None = None,
        total_items: int | None = None,
        message: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO web_job_events "
            "(job_id,phase,current_item,current_index,total_items,message,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, phase, current_item, current_index, total_items, message, self._now()),
        )
        self.conn.commit()

    def list_web_job_events(self, job_id: str, after_id: int = 0) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM web_job_events WHERE job_id=? AND event_id>? ORDER BY event_id",
            (job_id, after_id),
        )

    # ── Aggregates ─────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, int]:
        """Row counts per table, chunks per status and pending dedupe decisions."""
        stats = {t: self._count(f"SELECT COUNT(*) FROM {t}") for t in _COUNTED_TABLES}
        stats.update(dict.fromkeys(_CHUNK_STATUS_KEYS.values(), 0))
        for row in self._fetchall("SELECT status, COUNT(*) AS n FROM chunks GROUP BY status"):
            key = _CHUNK_STATUS_KEYS.get(row["status"])
            if key:
                stats[key] += row["n"]
        stats["concepts_dedupe_pending"] = self._count(
            "SELECT COUNT(*) FROM concepts WHERE status='dedupe_pending'"
        )
        return stats

    def get_web_dashboard(
        self,
        *,
        low_max: float,
        limiar: float,
        relation_weights: Mapping[str, float],
    ) -> dict[str, Any]:
        """Return aggregate operational metrics without loading note bodies.

        Confidence bands cover drafts still awaiting review only, with the same
        cut points as ``zettel review`` (``low_max`` inclusive, then ``limiar``).
        Hubs are ranked by the same weighted degree ``garden --hubs`` uses.
        """
        stats = self.get_stats()
        stats.update(
            {
                "lit_index": stats["sources"],
                "lit_drafts": stats["chunks_awaiting_review"],
                "lit_approved": stats["chunks_approved"],
                "permanent_notes": self.count_permanent_notes(),
                "manual_notes": self._count("SELECT COUNT(*) FROM notes WHERE origin='manual'"),
                "isolated_notes": self._count(
                    "SELECT COUNT(*) FROM notes n WHERE NOT EXISTS "
                    "(SELECT 1 FROM note_connections c "
                    "WHERE c.source_note_id=n.note_id OR c.target_note_id=n.note_id)"
                ),
                "incomplete_sources": self._count(
                    "SELECT COUNT(*) FROM sources WHERE COALESCE(document_type,'')='' "
                    "OR COALESCE(abnt_reference,'')=''"
                ),
            }
        )
        return {
            "counts": stats,
            "confidence": self._fetchall(
                "SELECT CASE WHEN review_confidence <= ? THEN 'baixissima' "
                "WHEN review_confidence < ? THEN 'media' ELSE 'alta' END band, COUNT(*) c "
                "FROM chunks WHERE status='awaiting_review' AND review_confidence IS NOT NULL "
                "GROUP BY band",
                (low_max, limiar),
            ),
            "relations": self._fetchall(
                "SELECT relation_type, COUNT(*) c FROM note_connections "
                "GROUP BY relation_type ORDER BY c DESC"
            ),
            "origins": self._fetchall(
                "SELECT origin, COUNT(*) c FROM notes GROUP BY origin ORDER BY c DESC"
            ),
            "documents": self._fetchall(
                "SELECT COALESCE(document_type,'incompleto') document_type, COUNT(*) c "
                "FROM sources GROUP BY document_type ORDER BY c DESC"
            ),
            "sources_cost": self._fetchall(
                "SELECT source_id, title, cost_usd_total, tokens_prompt, tokens_completion "
                "FROM sources ORDER BY cost_usd_total DESC LIMIT 20"
            ),
            "hubs": self._top_hubs(relation_weights),
            "runs": self._fetchall(
                "SELECT run_id,pipeline_signature,started_at,finished_at,status,"
                "cost_usd_total,tokens_prompt,tokens_completion,cache_hits,"
                "duplicate_file_count,duplicate_content_count,duplicate_biblio_count,"
                "duplicate_semantic_count "
                "FROM runs ORDER BY run_id DESC LIMIT 10"
            ),
        }

    def _top_hubs(self, relation_weights: Mapping[str, float]) -> list[dict]:
        """The best-connected notes, titled with one query over just those ids."""
        ranked = sorted(
            self.get_weighted_note_degrees(relation_weights).items(),
            key=lambda item: item[1],
            reverse=True,
        )[:_HUB_LIMIT]
        ids = [note_id for note_id, _ in ranked]
        titles = {
            r["note_id"]: r["title"]
            for r in self._fetchall(
                f"SELECT note_id, title FROM notes WHERE note_id IN ({placeholders(ids)})",
                tuple(ids),
            )
        }
        return [
            {"note_id": note_id, "title": titles.get(note_id, note_id), "degree": degree}
            for note_id, degree in ranked
        ]
