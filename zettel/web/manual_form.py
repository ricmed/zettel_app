"""Pure parse / validation / preflight for the manual-note form. No FastAPI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

MODES = {"SRC", "LIT_INDEX", "LIT_GRANULAR", "ZTL_BLANK", "ZTL_FROM_LIT"}
_NOTE_TYPES = {"SRC", "LIT", "ZTL"}


def _text(form: Mapping[str, Any], name: str) -> str:
    return str(form.get(name) or "").strip()


def _optional(form: Mapping[str, Any], name: str) -> str | None:
    value = _text(form, name)
    return value or None


def _int(form: Mapping[str, Any], name: str, default: int | None = None) -> int | None:
    raw = _optional(form, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Valor numérico inválido em {name}.") from exc


def compose_mode(form: Mapping[str, Any]) -> str:
    note_type = _text(form, "note_type").upper()
    if note_type not in _NOTE_TYPES:
        raise ValueError("Informe um tipo e um título válidos.")
    if note_type == "SRC":
        return "SRC"
    if note_type == "LIT":
        return "LIT_GRANULAR" if _text(form, "granular") == "1" else "LIT_INDEX"
    if _text(form, "ztl_origin") == "from_lit" or _text(form, "from_lit") or _text(form, "from_lit_path"):
        return "ZTL_FROM_LIT"
    return "ZTL_BLANK"


def parse(form: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the posted form and return a normalised payload.

    ``title`` is required except for ``ZTL_FROM_LIT`` (the thesis lives in
    ``lit_thesis``). No field is treated as required by the HTML; this function
    is the only authority.
    """
    mode = compose_mode(form)
    title = _text(form, "title")
    if mode != "ZTL_FROM_LIT" and not title:
        raise ValueError("Informe um tipo e um título válidos.")

    payload: dict[str, Any] = {
        "mode": mode,
        "title": title,
        "citekey": _optional(form, "citekey"),
        "authors": [line.strip() for line in str(form.get("authors") or "").splitlines() if line.strip()],
        "year": _int(form, "year"),
        "document_type": _optional(form, "document_type"),
        "abnt_reference": _optional(form, "abnt_reference"),
        "publisher": _optional(form, "publisher"),
        "place": _optional(form, "place"),
        "doi": _optional(form, "doi"),
        "url": _optional(form, "url"),
        "journal": _optional(form, "journal"),
        "edition": _optional(form, "edition"),
        "institution": _optional(form, "institution"),
        "pages": _optional(form, "pages"),
        "source_id": _optional(form, "source_id"),
        "granular": mode == "LIT_GRANULAR",
        "chunk_index": _int(form, "chunk_index", default=1) or 1,
        "page": _int(form, "page_number"),
        "from_lit": _optional(form, "from_lit"),
        "from_lit_path": _optional(form, "from_lit_path"),
        "lit_thesis": _optional(form, "lit_thesis"),
        "use_llm": bool(form.get("use_llm")),
        "force": bool(form.get("force")) and mode != "SRC",
    }
    if mode in {"LIT_INDEX", "LIT_GRANULAR"} and not payload["source_id"]:
        raise ValueError("Selecione uma fonte existente para a nota LIT.")
    if mode == "ZTL_FROM_LIT":
        if bool(payload["from_lit"]) == bool(payload["from_lit_path"]):
            raise ValueError("Informe exatamente uma nota LIT de origem (identificador ou caminho).")
        if payload["use_llm"] is False and not title and not payload["lit_thesis"]:
            # thesis may still be derived from the LIT body in preflight
            pass
    elif payload["use_llm"]:
        raise ValueError("O uso de LLM requer uma nota LIT de origem.")
    return payload


def _under_literature(cfg: Any, relative: Path) -> Path:
    """Resolve a vault-relative path that must sit under ``20_Literature/``."""
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Caminho de nota LIT inválido.")
    vault = cfg.vault_path.resolve()
    literature = (vault / "20_Literature").resolve()
    target = (vault / relative).resolve()
    try:
        target.relative_to(literature)
    except ValueError as exc:
        raise ValueError("Caminho de nota LIT inválido.") from exc
    if not target.is_file() or target.suffix.lower() != ".md":
        raise ValueError("Caminho de nota LIT inválido.")
    return target


def resolve_from_lit(cfg: Any, db: Any, parsed: Mapping[str, Any]) -> str:
    """Return the worker ``ref``: a known ``chunk_id`` or a guarded file path."""
    chunk_id = parsed.get("from_lit")
    if chunk_id:
        chunk = db.get_chunk(chunk_id)
        if not chunk or not chunk.get("literature_note_path"):
            raise ValueError("Selecione uma nota LIT granular válida.")
        return str(chunk_id)
    rel = parsed.get("from_lit_path") or ""
    return str(_under_literature(cfg, Path(rel)))


def preflight_from_lit(cfg: Any, db: Any, parsed: Mapping[str, Any], ref: str, *, llm_ok: bool) -> str:
    """Validate the LIT before enqueueing. Returns the thesis to send to the job.

    Raises ``ValueError`` (400) or ``PreflightConflict`` (409).
    """
    from zettel.manual_lit import build_candidate_from_literature, resolve_literature_note
    from zettel.vault import parse_frontmatter

    path: Path
    if Path(ref).is_file():
        path = Path(ref)
        content = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(content)
        if str(meta.get("type") or "") != "literature" or not meta.get("chunk_id"):
            raise ValueError(
                f"{path.name} nao e uma nota de literatura granular "
                "(precisa de type: literature e chunk_id no frontmatter)."
            )
    else:
        path, meta, body, content = resolve_literature_note(db, ref)

    source_id = str(meta.get("source_id") or "")
    if parsed.get("use_llm"):
        if not db.get_source(source_id):
            raise PreflightConflict(
                "A fonte desta LIT ainda não está no SQLite. "
                "Sincronize as notas manuais no Pipeline antes de usar o LLM.",
                href="/pipeline",
            )
        if not llm_ok:
            raise PreflightConflict(
                "O provedor LLM não possui credencial configurada. "
                "Verifique Configuração / saúde.",
            )

    thesis = (parsed.get("lit_thesis") or "").strip()
    if not thesis:
        try:
            candidate = build_candidate_from_literature(meta, body, content)
        except (ValueError, KeyError) as exc:
            raise ValueError("Não foi possível derivar a tese desta LIT. Preencha a tese.") from exc
        thesis = (candidate.thesis or "").strip()
        if not thesis or thesis.startswith("_Preencha"):
            raise ValueError(
                "A tese derivada desta LIT ainda é o placeholder do scaffold. "
                "Preencha o resumo da LIT ou informe a tese."
            )
    return thesis


class PreflightConflict(Exception):
    """409: the form is valid but the job cannot run yet."""

    def __init__(self, message: str, href: str | None = None):
        super().__init__(message)
        self.href = href
