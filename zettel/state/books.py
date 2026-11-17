"""Espelho da estante na linha ``sources``: leitura reconstruída do SRC.

O arquivo é a fonte da verdade. Estas colunas existem para filtrar, ordenar e
agregar sem reler cada YAML. ``library.mirror.refresh`` as mantém em dia.
As leituras ficam em ``readings_json``, por ``source_id``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from zettel.search_terms import fold
from zettel.state.base import StateBase, placeholders

_SHELF_COLUMNS = (
    "code_number",
    "code",
    "format",
    "status",
    "readings_json",
    "genres_json",
    "subjects_json",
    "nature",
    "reading_mode",
    "country",
    "original_language",
    "original_publication_year",
    "original_title",
    "series",
    "series_number",
    "pages",
    "cover",
    "ratings_json",
    "rating_profile",
    "score",
    "favorite",
    "work_id",
    "shelf_mtime_ns",
    "shelf_size",
    "shelf_schema_stamp",
)

_LAST_END = (
    "(SELECT MAX(json_extract(r.value, '$.end')) FROM json_each(COALESCE(s.readings_json, '[]')) r)"
)

# ``code`` ("0254 - D") vale quando ``code_number`` ainda não foi espelhado.
# O CAST do SQLite lê o prefixo numérico e ignora a letra.
_CODE_SORT_KEY = "COALESCE(s.code_number, CAST(s.code AS INTEGER))"
SHELF_SORTS = {
    "code": f"{_CODE_SORT_KEY} IS NULL OR {_CODE_SORT_KEY} = 0, {_CODE_SORT_KEY} DESC",
    "title": "zfold(s.title)",
    "score": "s.score IS NULL, s.score DESC, zfold(s.title)",
    "finished": f"{_LAST_END} IS NULL, {_LAST_END} DESC, zfold(s.title)",
    "added": "s.created_at DESC",
}


class BooksMixin(StateBase):
    """Consultas da estante sobre ``sources``. O nome do mixin fica, para o MRO."""

    def upsert_shelf(self, source_id: str, fields: dict[str, Any]) -> None:
        """Grava os campos de leitor desta fonte. A linha precisa já existir."""
        sets = ", ".join(f"{column}=?" for column in _SHELF_COLUMNS)
        values = tuple(fields.get(column) for column in _SHELF_COLUMNS)
        with self.conn:
            self.conn.execute(
                f"UPDATE sources SET {sets} WHERE source_id=?",
                (*values, source_id),
            )

    def sources_by_work(self, work_id: str) -> list[dict]:
        """Fontes que declaram este ``work_id``. Vazio não agrupa ninguém."""
        if not str(work_id or "").strip():
            return []
        return self._fetchall(
            "SELECT * FROM sources WHERE work_id=? ORDER BY zfold(title), source_id",
            (work_id.strip(),),
        )

    def shelf_stamps(self) -> dict[str, dict[str, Any]]:
        """``source_id ->`` o carimbo com que o espelho leu o arquivo da última vez."""
        rows = self._fetchall(
            "SELECT source_id, shelf_mtime_ns, shelf_size, shelf_schema_stamp FROM sources"
        )
        return {row["source_id"]: row for row in rows}

    def list_shelf_identities(self, document_types: Sequence[str]) -> list[dict]:
        """O que identifica cada item da estante, para achar um ISBN ou um título."""
        if not document_types:
            return []
        return self._fetchall(
            "SELECT source_id, isbn, title, authors, code_number, code, document_type "
            f"FROM sources WHERE document_type IN ({placeholders(document_types)})",
            tuple(document_types),
        )

    def list_shelf_rows(
        self,
        document_types: Sequence[str],
        *,
        status: str = "",
        genre: str = "",
        nature: str = "",
        format: str = "",
        country: str = "",
        reading_mode: str = "",
        document_type: str = "",
        year: int | None = None,
        min_score: float | None = None,
        query: str = "",
        favorite: bool = False,
        sort: str = "code",
    ) -> list[dict]:
        if not document_types:
            return []
        where = [f"s.document_type IN ({placeholders(document_types)})"]
        params: list[Any] = list(document_types)
        for column, value in (
            ("status", status),
            ("nature", nature),
            ("format", format),
            ("reading_mode", reading_mode),
            ("document_type", document_type),
        ):
            if value:
                where.append(f"s.{column}=?")
                params.append(value)
        if country:
            where.append("zfold(COALESCE(s.country,''))=?")
            params.append(fold(country))
        if genre:
            where.append("EXISTS (SELECT 1 FROM json_each(s.genres_json) g WHERE zfold(g.value)=?)")
            params.append(fold(genre))
        if year:
            where.append(
                "EXISTS (SELECT 1 FROM json_each(COALESCE(s.readings_json, '[]')) r "
                "WHERE substr(json_extract(r.value, '$.end'), 1, 4)=?)"
            )
            params.append(f"{year:04d}")
        if min_score is not None:
            where.append("s.score >= ?")
            params.append(min_score)
        if favorite:
            where.append("s.favorite=1")
        if query:
            where.append(
                "(zfold(s.title) LIKE ? OR zfold(s.authors) LIKE ? "
                "OR zfold(COALESCE(s.code,'')) LIKE ?)"
            )
            like = f"%{fold(query)}%"
            params.extend([like, like, like])
        order = SHELF_SORTS.get(sort, SHELF_SORTS["code"])
        return self._fetchall(
            f"SELECT s.*, s.authors AS authors_json, {_LAST_END} AS last_end "
            f"FROM sources s WHERE {' AND '.join(where)} ORDER BY {order}",
            tuple(params),
        )

    def list_shelf_readings(self, document_types: Sequence[str]) -> list[dict]:
        """Toda leitura dos itens da estante, com o que o painel agrega."""
        if not document_types:
            return []
        return self._fetchall(
            "SELECT s.source_id, r.key AS position, "
            "json_extract(r.value, '$.start') AS start_date, "
            "json_extract(r.value, '$.end') AS end_date, "
            "s.pages, s.format, s.genres_json, s.country, s.nature, s.score "
            "FROM sources s, json_each(COALESCE(s.readings_json, '[]')) r "
            f"WHERE s.document_type IN ({placeholders(document_types)}) "
            "ORDER BY end_date",
            tuple(document_types),
        )

    def list_reflective_source_ids(self) -> list[str]:
        """Fontes lidas em modo reflexão (prompts e filtros do ADR-069)."""
        rows = self._fetchall(
            "SELECT source_id FROM sources "
            "WHERE reading_mode='reflexao' AND source_id IS NOT NULL AND source_id <> ''"
        )
        return [row["source_id"] for row in rows]

    def mark_shelf_migrated(self) -> None:
        """A migração da estante terminou: a próxima abertura pode derrubar o espelho antigo."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO app_meta (key, value) VALUES ('shelf_unified', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )

    def shelf_migrated(self) -> bool:
        row = self._fetchone("SELECT value FROM app_meta WHERE key='shelf_unified'")
        return bool(row and row["value"] == "1")

    def drop_legacy_shelf_tables(self) -> None:
        """Derruba o espelho antigo. Sem efeito enquanto a migração não marcou o fim."""
        if not self.shelf_migrated():
            return
        with self.conn:
            self.conn.execute("DROP TABLE IF EXISTS book_readings")
            self.conn.execute("DROP TABLE IF EXISTS books")
