"""Pipeline runs: status, cost/token totals and duplicate counters."""

from __future__ import annotations

from zettel.state.base import StateBase

# Duplicate-detection layer -> counter column on the run row.
_DUPLICATE_COLUMNS = {
    "file": "duplicate_file_count",
    "content": "duplicate_content_count",
    "biblio": "duplicate_biblio_count",
    "semantic": "duplicate_semantic_count",
}


class RunsMixin(StateBase):
    def start_run(self, pipeline_signature: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (pipeline_signature, started_at, status) VALUES (?, ?, 'running')",
            (pipeline_signature, self._now()),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def finish_run(
        self,
        run_id: int,
        status: str = "completed",
        usage: dict | None = None,
    ) -> None:
        if not usage:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=? WHERE run_id=?",
                (self._now(), status, run_id),
            )
            self.conn.commit()
            return
        self.conn.execute(
            """UPDATE runs SET
                 finished_at=?, status=?,
                 cost_usd_total=?, cost_usd_llm=?, cost_usd_embedding=?,
                 tokens_prompt=?, tokens_completion=?, tokens_embedding=?,
                 llm_calls=?, cache_hits=?,
                 prompt_cache_read_tokens=?, prompt_cache_write_tokens=?
               WHERE run_id=?""",
            (
                self._now(),
                status,
                float(usage.get("cost_usd_total", 0) or 0),
                float(usage.get("cost_usd_llm", 0) or 0),
                float(usage.get("cost_usd_embedding", 0) or 0),
                int(usage.get("tokens_prompt", 0) or 0),
                int(usage.get("tokens_completion", 0) or 0),
                int(usage.get("tokens_embedding", 0) or 0),
                int(usage.get("llm_calls", 0) or 0),
                int(usage.get("cache_hits", 0) or 0),
                int(usage.get("prompt_cache_read_tokens", 0) or 0),
                int(usage.get("prompt_cache_write_tokens", 0) or 0),
                run_id,
            ),
        )
        self.conn.commit()

    def record_duplicate(self, run_id: int, kind: str) -> None:
        """Increment a duplicate counter on the run row.

        kind: one of "file", "content", "biblio", "semantic".
        """
        column = _DUPLICATE_COLUMNS.get(kind)
        if not column:
            raise ValueError(f"Tipo de duplicidade desconhecido: {kind}")
        self.conn.execute(f"UPDATE runs SET {column} = {column} + 1 WHERE run_id=?", (run_id,))
        self.conn.commit()

    def get_run(self, run_id: int) -> dict | None:
        return self._fetchone("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def get_last_run(self) -> dict | None:
        return self._fetchone("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1")

    def get_recent_runs(self, limit: int = 30) -> list[dict]:
        """Newest-first run rows, including cost/token columns."""
        return self._fetchall(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (max(1, int(limit)),)
        )
