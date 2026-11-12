"""Espelho da Estante (ADR-068): as notas LIV de ``60_Estante/`` em ``books`` / ``book_readings``.

O arquivo é a fonte da verdade; estas tabelas existem para filtrar, ordenar e
agregar sem reler cada YAML. ``library.mirror.refresh`` as mantém em dia.
"""

from __future__ import annotations

from typing import Any

from zettel.search_terms import fold
from zettel.state.base import StateBase, placeholders

_BOOK_COLUMNS = (
    "book_id",
    "path",
    "file_mtime_ns",
    "file_size",
    "schema_stamp",
    "code_number",
    "code",
    "title",
    "authors_json",
    "source_id",
    "isbn",
    "pages",
    "country",
    "original_language",
    "genres_json",
    "nature",
    "reading_mode",
    "format",
    "status",
    "cover",
    "rating_profile",
    "score",
    "favorite",
    "created_at",
    "frontmatter_json",
)

# Última data de término de cada livro (a "Data Término" da lista).
_LAST_END = "(SELECT MAX(end_date) FROM book_readings r WHERE r.book_id = b.book_id)"

BOOK_SORTS = {
    "code": "b.code_number IS NULL, b.code_number DESC",
    "title": "zfold(b.title)",
    "score": "b.score IS NULL, b.score DESC, zfold(b.title)",
    "finished": f"{_LAST_END} IS NULL, {_LAST_END} DESC, zfold(b.title)",
    "added": "b.created_at DESC",
}


class BooksMixin(StateBase):
    def upsert_book(
        self, row: dict[str, Any], readings: list[tuple[str | None, str | None]]
    ) -> None:
        """Grava a linha do livro e substitui as leituras dele, numa transação."""
        values = tuple(row.get(col) for col in _BOOK_COLUMNS)
        sets = ", ".join(f"{c}=excluded.{c}" for c in _BOOK_COLUMNS if c != "book_id")
        with self.conn:
            self.conn.execute(
                f"INSERT INTO books ({', '.join(_BOOK_COLUMNS)}) "
                f"VALUES ({placeholders(_BOOK_COLUMNS)}) "
                f"ON CONFLICT(book_id) DO UPDATE SET {sets}",
                values,
            )
            self.conn.execute("DELETE FROM book_readings WHERE book_id=?", (row["book_id"],))
            self.conn.executemany(
                "INSERT INTO book_readings (book_id, position, start_date, end_date) "
                "VALUES (?, ?, ?, ?)",
                [(row["book_id"], i, start, end) for i, (start, end) in enumerate(readings)],
            )

    def delete_books(self, book_ids: list[str]) -> None:
        if not book_ids:
            return
        with self.conn:
            self.conn.execute(
                f"DELETE FROM books WHERE book_id IN ({placeholders(book_ids)})", tuple(book_ids)
            )

    def book_file_index(self) -> dict[str, dict[str, Any]]:
        """``book_id -> {path, file_mtime_ns, file_size, schema_stamp}`` do que o espelho já leu."""
        rows = self._fetchall(
            "SELECT book_id, path, file_mtime_ns, file_size, schema_stamp FROM books"
        )
        return {r["book_id"]: r for r in rows}

    def get_book(self, book_id: str) -> dict | None:
        return self._fetchone(
            f"SELECT b.*, {_LAST_END} AS last_end FROM books b WHERE b.book_id=?", (book_id,)
        )

    def get_book_by_source(self, source_id: str) -> dict | None:
        return self._fetchone(
            "SELECT * FROM books WHERE source_id=? ORDER BY code_number LIMIT 1", (source_id,)
        )

    def list_book_identities(self) -> list[dict]:
        """O que identifica cada livro, para religar a LIV a uma fonte."""
        return self._fetchall(
            "SELECT book_id, path, source_id, isbn, title, authors_json, code_number, code "
            "FROM books"
        )

    def list_book_rows(
        self,
        *,
        status: str = "",
        genre: str = "",
        nature: str = "",
        format: str = "",
        country: str = "",
        reading_mode: str = "",
        year: int | None = None,
        min_score: float | None = None,
        query: str = "",
        favorite: bool = False,
        sort: str = "code",
    ) -> list[dict]:
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("nature", nature),
            ("format", format),
            ("reading_mode", reading_mode),
        ):
            if value:
                where.append(f"b.{column}=?")
                params.append(value)
        if country:
            where.append("zfold(COALESCE(b.country,''))=?")
            params.append(fold(country))
        if genre:
            where.append("EXISTS (SELECT 1 FROM json_each(b.genres_json) g WHERE zfold(g.value)=?)")
            params.append(fold(genre))
        if year:
            where.append(
                "EXISTS (SELECT 1 FROM book_readings r WHERE r.book_id=b.book_id "
                "AND substr(r.end_date,1,4)=?)"
            )
            params.append(f"{year:04d}")
        if min_score is not None:
            where.append("b.score >= ?")
            params.append(min_score)
        if favorite:
            where.append("b.favorite=1")
        if query:
            where.append(
                "(zfold(b.title) LIKE ? OR zfold(b.authors_json) LIKE ? "
                "OR zfold(COALESCE(b.code,'')) LIKE ?)"
            )
            like = f"%{fold(query)}%"
            params.extend([like, like, like])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = BOOK_SORTS.get(sort, BOOK_SORTS["code"])
        return self._fetchall(
            f"SELECT b.*, {_LAST_END} AS last_end FROM books b {clause} ORDER BY {order}",
            tuple(params),
        )

    def list_book_readings(self) -> list[dict]:
        """Toda leitura, com o que o painel agrega sobre o livro dela."""
        return self._fetchall(
            "SELECT r.book_id, r.position, r.start_date, r.end_date, b.pages, b.format, "
            "b.genres_json, b.country, b.nature, b.score FROM book_readings r "
            "JOIN books b ON b.book_id = r.book_id ORDER BY r.end_date"
        )

    def list_reflective_source_ids(self) -> list[str]:
        """Fontes cujo livro é lido em modo reflexão (prompts e filtros do ADR-069)."""
        rows = self._fetchall(
            "SELECT DISTINCT source_id FROM books "
            "WHERE reading_mode='reflexao' AND source_id IS NOT NULL AND source_id <> ''"
        )
        return [r["source_id"] for r in rows]
