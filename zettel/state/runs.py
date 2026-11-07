"""Pipeline runs: status, cost/token totals and duplicate counters."""

from __future__ import annotations

import json
from contextvars import ContextVar

from zettel.state.base import StateBase, placeholders

# The web job whose worker is running in this context. The worker sets it
# around a job, so every run that job opens is linked to it at start (#311);
# a CLI process or another thread never sees the value.
current_web_job: ContextVar[str | None] = ContextVar("current_web_job", default=None)

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
        job_id = current_web_job.get()
        if job_id:
            self.conn.execute(
                "INSERT INTO web_job_runs (job_id, run_id) VALUES (?, ?)", (job_id, cur.lastrowid)
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

    def record_run_usage(self, run_id: int, rows: list[dict]) -> None:
        """Replace the per-model usage rows of ``run_id`` (``CostTracker.model_usage``)."""
        self.conn.execute("DELETE FROM run_usage WHERE run_id=?", (run_id,))
        self.conn.executemany(
            """INSERT INTO run_usage
                 (run_id, kind, provider, model, calls, tokens_prompt, tokens_completion,
                  cache_read_tokens, cache_write_tokens, tokens_embedding, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    row["kind"],
                    row.get("provider") or "",
                    row.get("model") or "",
                    int(row.get("calls") or 0),
                    int(row.get("tokens_prompt") or 0),
                    int(row.get("tokens_completion") or 0),
                    int(row.get("cache_read_tokens") or 0),
                    int(row.get("cache_write_tokens") or 0),
                    int(row.get("tokens_embedding") or 0),
                    float(row.get("cost_usd") or 0),
                )
                for row in rows
            ],
        )
        self.conn.commit()

    def usage_by_provider(self, run_ids: list[int] | None = None) -> list[dict]:
        """Cost and tokens per provider and model, for some runs or every run.

        Runs finished before ``run_usage`` existed have no rows here; their
        totals still live on ``runs``.
        """
        where, params = "", ()
        if run_ids is not None:
            if not run_ids:
                return []
            where, params = f"WHERE run_id IN ({placeholders(run_ids)})", tuple(run_ids)
        return self._fetchall(
            f"""SELECT provider, model, kind,
                       SUM(calls) AS calls,
                       SUM(tokens_prompt) AS tokens_prompt,
                       SUM(tokens_completion) AS tokens_completion,
                       SUM(cache_read_tokens) AS cache_read_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens,
                       SUM(tokens_embedding) AS tokens_embedding,
                       SUM(cost_usd) AS cost_usd
                FROM run_usage {where}
                GROUP BY provider, model, kind
                ORDER BY cost_usd DESC, provider, model""",
            params,
        )

    def phase_model_usage(self, phase: str, model: str) -> dict:
        """LLM totals of every finished run named ``phase`` that used ``model``."""
        row = self._fetchone(
            """SELECT COALESCE(SUM(u.calls), 0) AS calls,
                      COALESCE(SUM(u.tokens_prompt), 0) AS tokens_prompt,
                      COALESCE(SUM(u.tokens_completion), 0) AS tokens_completion,
                      COALESCE(SUM(u.cache_read_tokens), 0) AS cache_read_tokens
               FROM run_usage u JOIN runs ON runs.run_id = u.run_id
               WHERE runs.pipeline_signature = ? AND u.kind = 'llm' AND u.model = ?
                 AND runs.status IN ('completed', 'failed')""",
            (phase, model),
        )
        return dict(row) if row else {}

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

    def save_garden_report(self, run_id: int, pipeline: str, report_json: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO garden_reports (run_id, pipeline, report_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (run_id, pipeline, report_json, self._now()),
        )
        self.conn.commit()

    def latest_garden_report(self, pipeline: str) -> dict | None:
        """Newest persisted report of ``pipeline`` (``garden`` or ``garden_hubs``)."""
        row = self._fetchone(
            "SELECT report_json FROM garden_reports WHERE pipeline=? ORDER BY run_id DESC LIMIT 1",
            (pipeline,),
        )
        return json.loads(row["report_json"]) if row else None

    def get_recent_runs(self, limit: int = 30) -> list[dict]:
        """Newest-first run rows, including cost/token columns."""
        return self._fetchall(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (max(1, int(limit)),)
        )
