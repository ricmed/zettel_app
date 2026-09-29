"""Files seen in the inbox and the sources they became."""

from __future__ import annotations

import json

from zettel.search_terms import fold
from zettel.state.base import StateBase

# Identity fields of a source — never the multi-megabyte text blobs
# (``extracted_text`` / ``lit_body``). Shared by the web picker and the
# title + author duplicate layer, which read exactly these.
SOURCE_IDENTITY_COLS = "source_id, citekey, title, authors, year"


def escape_like(text: str) -> str:
    """Neutralize LIKE metacharacters so a search for ``%`` does not match everything."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SourcesMixin(StateBase):
    # ── Files ──────────────────────────────────────────────────────────

    def upsert_file(
        self,
        path: str,
        file_checksum: str,
        origin_type: str,
        source_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO files (path, file_checksum, origin_type, source_id, last_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 file_checksum=excluded.file_checksum,
                 origin_type=excluded.origin_type,
                 source_id=COALESCE(excluded.source_id, files.source_id),
                 last_seen_at=excluded.last_seen_at""",
            (path, file_checksum, origin_type, source_id, self._now()),
        )
        self.conn.commit()

    def get_file(self, path: str) -> dict | None:
        return self._fetchone("SELECT * FROM files WHERE path=?", (path,))

    def get_file_by_checksum(
        self, file_checksum: str, exclude_path: str | None = None
    ) -> dict | None:
        """Find any known file (regardless of path/name) with the same raw-byte checksum.

        Used to detect a renamed/copied duplicate dropped into the inbox under a
        different filename.
        """
        return self._fetchone(
            "SELECT * FROM files WHERE file_checksum=? AND (? IS NULL OR path<>?) "
            "ORDER BY last_seen_at ASC LIMIT 1",
            (file_checksum, exclude_path, exclude_path),
        )

    # ── Sources ────────────────────────────────────────────────────────

    def upsert_source(
        self,
        source_id: str,
        citekey: str,
        title: str,
        authors: list[str],
        year: int | None,
        file_checksum: str,
        origin_path: str,
        origin_type: str,
        extraction_checksum: str | None = None,
        origin: str = "pipeline",
        document_type: str | None = None,
        bibliography_json: str | None = None,
        doi: str | None = None,
        isbn: str | None = None,
        abnt_reference: str | None = None,
        total_pages_file: int | None = None,
        total_pages_book: int | None = None,
        page_offset: int | None = None,
        page_offset_confidence: str | None = None,
        content_start_file_page: int | None = None,
        content_start_book_page: int | None = None,
        processing_status: str | None = None,
        last_chunk_processed: int | None = None,
        total_chunks: int | None = None,
        docling_config_hash: str | None = None,
    ) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO sources (source_id, citekey, title, authors, year, file_checksum,
                                    extraction_checksum, origin_path, origin_type, origin,
                                    document_type, bibliography_json, doi, isbn,
                                    abnt_reference,
                                    total_pages_file, total_pages_book, page_offset,
                                    page_offset_confidence, content_start_file_page,
                                    content_start_book_page, processing_status,
                                    last_chunk_processed, total_chunks, docling_config_hash,
                                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                 title=excluded.title, authors=excluded.authors, year=excluded.year,
                 file_checksum=excluded.file_checksum,
                 extraction_checksum=excluded.extraction_checksum,
                 origin=excluded.origin,
                 document_type=COALESCE(excluded.document_type, sources.document_type),
                 bibliography_json=COALESCE(excluded.bibliography_json, sources.bibliography_json),
                 doi=COALESCE(excluded.doi, sources.doi),
                 isbn=COALESCE(excluded.isbn, sources.isbn),
                 abnt_reference=COALESCE(excluded.abnt_reference, sources.abnt_reference),
                 total_pages_file=COALESCE(excluded.total_pages_file, sources.total_pages_file),
                 total_pages_book=COALESCE(excluded.total_pages_book, sources.total_pages_book),
                 page_offset=COALESCE(excluded.page_offset, sources.page_offset),
                 page_offset_confidence=COALESCE(
                     excluded.page_offset_confidence, sources.page_offset_confidence
                 ),
                 content_start_file_page=COALESCE(
                     excluded.content_start_file_page, sources.content_start_file_page
                 ),
                 content_start_book_page=COALESCE(
                     excluded.content_start_book_page, sources.content_start_book_page
                 ),
                 processing_status=COALESCE(
                     excluded.processing_status, sources.processing_status
                 ),
                 last_chunk_processed=COALESCE(
                     excluded.last_chunk_processed, sources.last_chunk_processed
                 ),
                 total_chunks=COALESCE(excluded.total_chunks, sources.total_chunks),
                 docling_config_hash=COALESCE(
                     excluded.docling_config_hash, sources.docling_config_hash
                 ),
                 updated_at=excluded.updated_at""",
            (
                source_id,
                citekey,
                title,
                json.dumps(authors),
                year,
                file_checksum,
                extraction_checksum,
                origin_path,
                origin_type,
                origin,
                document_type,
                bibliography_json,
                doi,
                isbn,
                abnt_reference,
                total_pages_file,
                total_pages_book,
                page_offset,
                page_offset_confidence,
                content_start_file_page,
                content_start_book_page,
                processing_status or "completed",
                last_chunk_processed,
                total_chunks,
                docling_config_hash,
                now,
                now,
            ),
        )
        self.conn.commit()

    def update_source_texts(
        self,
        source_id: str,
        extracted_text: str | None = None,
        lit_body: str | None = None,
    ) -> None:
        """Persist the full extracted text and/or the LIT index snapshot for a source.

        Only overwrites columns whose argument is not None, so callers can update
        one field without clobbering the other. This is the durable retention layer
        that lets `rechunk` and `rebuild` run without reprocessing the source file.
        ``lit_body`` stores the literature *index* note.
        """
        self.conn.execute(
            """UPDATE sources SET
                 extracted_text=COALESCE(?, extracted_text),
                 lit_body=COALESCE(?, lit_body),
                 updated_at=?
               WHERE source_id=?""",
            (extracted_text, lit_body, self._now(), source_id),
        )
        self.conn.commit()

    def update_source_paging(
        self,
        source_id: str,
        *,
        total_pages_file: int | None = None,
        total_pages_book: int | None = None,
        page_offset: int | None = None,
        page_offset_confidence: str | None = None,
        content_start_file_page: int | None = None,
        content_start_book_page: int | None = None,
        processing_status: str | None = None,
        last_chunk_processed: int | None = None,
        total_chunks: int | None = None,
        docling_config_hash: str | None = None,
    ) -> None:
        """Update paging / processing checkpoint fields for a source."""
        self.conn.execute(
            """UPDATE sources SET
                 total_pages_file=COALESCE(?, total_pages_file),
                 total_pages_book=COALESCE(?, total_pages_book),
                 page_offset=COALESCE(?, page_offset),
                 page_offset_confidence=COALESCE(?, page_offset_confidence),
                 content_start_file_page=COALESCE(?, content_start_file_page),
                 content_start_book_page=COALESCE(?, content_start_book_page),
                 processing_status=COALESCE(?, processing_status),
                 last_chunk_processed=COALESCE(?, last_chunk_processed),
                 total_chunks=COALESCE(?, total_chunks),
                 docling_config_hash=COALESCE(?, docling_config_hash),
                 updated_at=?
               WHERE source_id=?""",
            (
                total_pages_file,
                total_pages_book,
                page_offset,
                page_offset_confidence,
                content_start_file_page,
                content_start_book_page,
                processing_status,
                last_chunk_processed,
                total_chunks,
                docling_config_hash,
                self._now(),
                source_id,
            ),
        )
        self.conn.commit()

    def update_source_summary(
        self,
        source_id: str,
        summary: str,
        summary_topics: list[str],
        summary_checksum: str,
        summary_model: str,
        updated_at: str,
    ) -> None:
        """Persist the source-level summary. Not embedded — see ADR-047."""
        self.conn.execute(
            """UPDATE sources
                  SET summary=?, summary_topics=?, summary_checksum=?,
                      summary_model=?, summary_updated_at=?
                WHERE source_id=?""",
            (
                summary,
                json.dumps(summary_topics, ensure_ascii=False),
                summary_checksum,
                summary_model,
                updated_at,
                source_id,
            ),
        )
        self.conn.commit()

    def add_source_usage(self, source_id: str, usage: dict) -> None:
        """Accumulate cost/token deltas onto a source row."""
        self.conn.execute(
            """UPDATE sources SET
                 cost_usd_total = cost_usd_total + ?,
                 cost_usd_llm = cost_usd_llm + ?,
                 cost_usd_embedding = cost_usd_embedding + ?,
                 tokens_prompt = tokens_prompt + ?,
                 tokens_completion = tokens_completion + ?,
                 tokens_embedding = tokens_embedding + ?,
                 updated_at=?
               WHERE source_id=?""",
            (
                float(usage.get("cost_usd_total", 0) or 0),
                float(usage.get("cost_usd_llm", 0) or 0),
                float(usage.get("cost_usd_embedding", 0) or 0),
                int(usage.get("tokens_prompt", 0) or 0),
                int(usage.get("tokens_completion", 0) or 0),
                int(usage.get("tokens_embedding", 0) or 0),
                self._now(),
                source_id,
            ),
        )
        self.conn.commit()

    def delete_source_cascade(self, source_id: str) -> dict[str, int]:
        """Remove a source and all dependent rows (not permanent notes).

        Deletes chunks (+ concepts per chunk + FTS), chapters (+ summary FTS),
        orphan concepts, assets, files rows and the sources row.
        """
        by_source = (source_id,)
        chunk_ids = [
            r["chunk_id"]
            for r in self._fetchall("SELECT chunk_id FROM chunks WHERE source_id=?", by_source)
        ]
        removed_chunks = self.delete_chunks(chunk_ids)
        self._fts_delete_chapter_summaries("source_id=?", by_source)
        tables = ("chapters", "concepts", "assets", "files", "sources")
        removed = {
            table: self.conn.execute(f"DELETE FROM {table} WHERE source_id=?", by_source).rowcount
            for table in tables
        }
        self.conn.commit()
        return {"chunks": removed_chunks, **removed}

    def get_source(self, source_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM sources WHERE source_id=?", (source_id,))

    def get_source_by_citekey(self, citekey: str) -> dict | None:
        return self._fetchone("SELECT * FROM sources WHERE citekey=?", (citekey,))

    def get_source_by_extraction_checksum(
        self, extraction_checksum: str, exclude_source_id: str | None = None
    ) -> dict | None:
        """Find any existing source with the same normalized extracted-text checksum.

        Used to detect the same article saved in a different format (e.g. PDF and
        Markdown) that extracts to textually identical content.
        """
        if not extraction_checksum:
            return None
        return self._fetchone(
            "SELECT * FROM sources WHERE extraction_checksum=? AND (? IS NULL OR source_id<>?) "
            "ORDER BY created_at ASC LIMIT 1",
            (extraction_checksum, exclude_source_id, exclude_source_id),
        )

    def get_source_by_doi(self, doi: str) -> dict | None:
        """Exact match on the normalized DOI — an identity, not a similarity.

        Callers must pass the value already normalized by
        ``harvester.biblio_dedupe.normalize_doi``; the column stores it that way.
        """
        if not doi:
            return None
        return self._fetchone(
            "SELECT * FROM sources WHERE doi=? ORDER BY created_at ASC LIMIT 1", (doi,)
        )

    def get_source_by_isbn(self, isbn: str) -> dict | None:
        """Exact match on the normalized ISBN (see :meth:`get_source_by_doi`)."""
        if not isbn:
            return None
        return self._fetchone(
            "SELECT * FROM sources WHERE isbn=? ORDER BY created_at ASC LIMIT 1", (isbn,)
        )

    def list_sources_with_authors(self) -> list[dict]:
        """Rows needed to match a work by title + author, without the text blobs."""
        return self._fetchall(f"SELECT {SOURCE_IDENTITY_COLS} FROM sources ORDER BY created_at ASC")

    def list_sources(self) -> list[dict]:
        return self._fetchall("SELECT * FROM sources ORDER BY created_at DESC")

    def search_sources(self, query: str = "", limit: int = 20) -> list[dict]:
        """Picker lookup: citekey/title/authors, never ``extracted_text`` / ``lit_body``.

        ``authors`` is stored as JSON text, so ``kahneman`` and ``daniel kahneman``
        match but ``kahneman, daniel`` (reordered) does not. Empty ``query``
        returns the most recently created sources.
        """
        limit = max(1, min(int(limit), 50))
        query = (query or "")[:200]
        cols = SOURCE_IDENTITY_COLS
        if not query.strip():
            return self._fetchall(
                f"SELECT {cols} FROM sources ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        folded = fold(query)
        if not folded:
            return []
        pattern = f"%{escape_like(folded)}%"
        return self._fetchall(
            f"SELECT {cols} FROM sources "
            f"WHERE zfold(citekey) LIKE ? ESCAPE '\\' "
            f"OR zfold(title) LIKE ? ESCAPE '\\' "
            f"OR zfold(authors) LIKE ? ESCAPE '\\' "
            f"ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (pattern, pattern, pattern, limit),
        )
