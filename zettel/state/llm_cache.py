"""Deterministic LLM response cache, keyed by ``compute_llm_call_checksum``."""

from __future__ import annotations

from zettel.state.base import StateBase, placeholders


class LlmCacheMixin(StateBase):
    def get_cached_llm_response(self, call_checksum: str) -> str | None:
        row = self._fetchone(
            "SELECT response_json FROM llm_cache WHERE call_checksum=?", (call_checksum,)
        )
        return row["response_json"] if row else None

    def cache_llm_response(self, call_checksum: str, request_json: str, response_json: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO llm_cache
               (call_checksum, request_json, response_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (call_checksum, request_json, response_json, self._now()),
        )
        self.conn.commit()

    def delete_llm_cache(self, call_checksums: list[str]) -> int:
        """Drop cached LLM responses by checksum. Empty strings are ignored."""
        unique = [c for c in dict.fromkeys(call_checksums) if c]
        if not unique:
            return 0
        cur = self.conn.execute(
            f"DELETE FROM llm_cache WHERE call_checksum IN ({placeholders(unique)})",
            tuple(unique),
        )
        self.conn.commit()
        return cur.rowcount
