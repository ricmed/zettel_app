"""Images extracted from sources or adopted from the vault (``assets``)."""

from __future__ import annotations

from zettel.state.base import StateBase


class AssetsMixin(StateBase):
    def upsert_asset(
        self,
        asset_id: str,
        source_id: str,
        path: str,
        image_checksum: str,
        chapter_id: str | None = None,
        context_snippet: str = "",
        status: str = "pending",
        page_in_file: int | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO assets (asset_id, source_id, chapter_id, path, image_checksum,
                                   context_snippet, status, page_in_file, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                 chapter_id=COALESCE(excluded.chapter_id, assets.chapter_id),
                 path=excluded.path,
                 context_snippet=excluded.context_snippet,
                 page_in_file=COALESCE(excluded.page_in_file, assets.page_in_file)""",
            (
                asset_id,
                source_id,
                chapter_id,
                path,
                image_checksum,
                context_snippet,
                status,
                page_in_file,
                self._now(),
            ),
        )
        self.conn.commit()

    def get_asset(self, asset_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM assets WHERE asset_id=?", (asset_id,))

    def get_assets_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM assets WHERE source_id=?", (source_id,))

    def get_pending_assets(self) -> list[dict]:
        return self._fetchall("SELECT * FROM assets WHERE status='pending'")

    def update_asset_chapter(self, asset_id: str, chapter_id: str | None) -> None:
        """Set chapter_id explicitly (including NULL) after rechunk re-resolution."""
        self.conn.execute("UPDATE assets SET chapter_id=? WHERE asset_id=?", (chapter_id, asset_id))
        self.conn.commit()

    def update_asset_description(
        self,
        asset_id: str,
        description: str,
        call_checksum: str,
        status: str = "described",
    ) -> None:
        self.conn.execute(
            """UPDATE assets SET description=?, description_call_checksum=?, status=?
               WHERE asset_id=?""",
            (description, call_checksum, status, asset_id),
        )
        self.conn.commit()

    def reset_failed_assets(self) -> int:
        """Reset failed image descriptions back to pending. Returns count reset."""
        cur = self.conn.execute("UPDATE assets SET status='pending' WHERE status='failed'")
        self.conn.commit()
        return cur.rowcount
