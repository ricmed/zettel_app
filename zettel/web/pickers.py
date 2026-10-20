"""JSON pickers for the manual-note combobox. Read-only GET, no CSRF."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from zettel.web.labels import lit_label, source_label
from zettel.web.rendering import service
from zettel.web.security import authenticated

router = APIRouter()

_MAX_Q = 200
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50


def _clamp_limit(raw: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_LIMIT
    return max(1, min(value, _MAX_LIMIT))


def _authors(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _source_item(row: dict, next_index: int | None = None) -> dict:
    item = {
        "id": row["source_id"],
        "source_id": row["source_id"],
        "citekey": row.get("citekey") or "",
        "title": row.get("title") or "",
        "authors": _authors(row.get("authors")),
        "year": row.get("year"),
        "label": source_label(row),
    }
    if next_index is not None:
        item["next_chunk_index"] = next_index
    return item


def _lit_item(row: dict) -> dict:
    chunk_id = row["chunk_id"]
    return {
        "id": chunk_id,
        "ref": chunk_id,
        "source_id": row.get("source_id") or "",
        "section_path": row.get("section_path") or "",
        "locator": row.get("locator") or "",
        "page_in_book": row.get("page_in_book"),
        "chunk_index": row.get("chunk_index"),
        "label": lit_label(row),
    }


@router.get("/api/pickers/sources")
def picker_sources(request: Request, q: str = "", limit: int = _DEFAULT_LIMIT):
    if not authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    q = (q or "")[:_MAX_Q]
    limit = _clamp_limit(limit)
    db = service(request).db()
    try:
        rows = db.search_sources(q, limit + 1)
        truncated = len(rows) > limit
        items = []
        for row in rows[:limit]:
            items.append(_source_item(row, db.next_manual_chunk_index(row["source_id"])))
    finally:
        db.close()
    return {"items": items, "truncated": truncated}


@router.get("/api/pickers/literature")
def picker_literature(
    request: Request,
    q: str = "",
    source_id: str = "",
    limit: int = _DEFAULT_LIMIT,
):
    if not authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not source_id:
        return JSONResponse({"error": "source_id_required"}, status_code=400)
    q = (q or "")[:_MAX_Q]
    limit = _clamp_limit(limit)
    db = service(request).db()
    try:
        meta = db.search_literature_chunks(q, source_id=source_id, limit=limit)
        seen = {row["chunk_id"] for row in meta}
        extra: list[dict] = []
        if q.strip() and len(meta) < limit:
            for row in db.search_literature_chunks_fts(q, source_id=source_id, limit=limit):
                if row["chunk_id"] not in seen:
                    extra.append(row)
                    seen.add(row["chunk_id"])
                if len(meta) + len(extra) >= limit:
                    break
        combined = meta + extra
        truncated = len(combined) > limit
        items = [_lit_item(row) for row in combined[:limit]]
    finally:
        db.close()
    return {"items": items, "truncated": truncated}
