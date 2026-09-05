"""Human-in-the-loop (HITL) bibliographic metadata resolution with Rich UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zettel.bibliography import (
    BIBLIO_FRONTMATTER_FIELDS,
    DOCUMENT_TYPE_LABELS,
    DOCUMENT_TYPES,
    FIELD_LABELS,
    REQUIRED_FIELDS,
    BibliographicMetadata,
    format_abnt,
    is_complete,
    missing_required,
    required_fields,
)
from zettel.config import AppConfig


def resolve_bibliography(
    file_path: Path,
    biblio: Any,
    interactive: bool,
    skip_biblio: bool,
    cfg: AppConfig,
) -> Any | None:
    """Confirm/complete bibliographic metadata. Returns meta or None to skip file.

    In interactive mode always shows a preview (even when already complete) so the
    user can confirm or edit before SRC is written and embeddings start.
    """
    import logging

    logger = logging.getLogger(__name__)

    threshold = cfg.harvest.biblio_confidence_threshold
    complete = is_complete(biblio, threshold)

    if not interactive:
        if complete:
            logger.info(
                "Biblio completa para '%s' (tipo=%s, confidence=%.2f) — "
                "aceita sem prompt (modo nao-interativo)",
                file_path.name,
                biblio.document_type,
                biblio.confidence,
            )
            return biblio
        if skip_biblio:
            logger.warning(
                "Metadados bibliograficos incompletos para '%s' "
                "(faltando: %s; confidence=%.2f). Seguindo por --skip-biblio.",
                file_path.name,
                ", ".join(missing_required(biblio)) or "tipo incerto",
                biblio.confidence,
            )
            return biblio
        return None

    from rich.console import Console
    from rich.prompt import Confirm, Prompt
    from rich.table import Table

    console = Console(stderr=True)
    meta = biblio.model_copy(deep=True)

    def _show_preview(m: Any, title: str = "Campos inferidos") -> None:
        console.print(f"\n[bold]Metadados bibliograficos: {file_path.name}[/bold]")
        table = Table(title=title)
        table.add_column("Campo")
        table.add_column("Valor")
        table.add_row("document_type", m.document_type or "(ausente)")
        table.add_row("confidence", f"{m.confidence:.2f}")
        preview_fields = (
            required_fields(m.document_type) if m.document_type else ["title", "authors", "year"]
        )
        for field in preview_fields:
            if field == "document_type":
                continue
            value = getattr(m, field, None)
            if isinstance(value, list):
                display = ", ".join(value) if value else "(vazio)"
            else:
                display = str(value) if value not in (None, "") else "(vazio)"
            table.add_row(FIELD_LABELS.get(field, field), display)
        console.print(table)
        if m.document_type:
            abnt = format_abnt(m)
            if abnt:
                console.print(f"\n[bold]Referencia ABNT:[/bold]\n{abnt}")

    _show_preview(meta)

    # Complete: still ask confirmation; decline -> edit path below.
    force_edit = False
    if complete:
        if Confirm.ask(
            "Metadados completos. Confirmar e gravar SRC?",
            default=True,
            console=console,
        ):
            meta.confidence = max(meta.confidence, threshold)
            return BibliographicMetadata.model_validate(meta.model_dump())
        console.print("[cyan]Edicao dos metadados:[/cyan]")
        force_edit = True

    low_confidence = not meta.document_type or meta.confidence < threshold
    if low_confidence or not meta.document_type or not complete or force_edit:
        ask_type = (
            (not meta.document_type)
            or low_confidence
            or Confirm.ask(
                "Alterar tipo documental?",
                default=not bool(meta.document_type),
                console=console,
            )
        )
        if ask_type:
            console.print("Tipos disponiveis:")
            for i, dtype in enumerate(DOCUMENT_TYPES, 1):
                console.print(f"  {i}. {dtype} — {DOCUMENT_TYPE_LABELS[dtype]}")
            default_idx = (
                str(DOCUMENT_TYPES.index(meta.document_type) + 1)
                if meta.document_type in DOCUMENT_TYPES
                else "1"
            )
            choice = Prompt.ask(
                "Tipo documental",
                choices=[str(i) for i in range(1, len(DOCUMENT_TYPES) + 1)],
                default=default_idx,
                console=console,
            )
            meta.document_type = DOCUMENT_TYPES[int(choice) - 1]
            meta.confidence = max(meta.confidence, threshold)

    to_fill = [f for f in missing_required(meta) if f != "document_type"]
    if to_fill:
        console.print("[cyan]Preencha os campos obrigatorios faltantes (Enter deixa vazio):[/cyan]")
    elif Confirm.ask(
        "Revisar campos obrigatorios ja preenchidos?",
        default=False,
        console=console,
    ):
        to_fill = [f for f in required_fields(meta.document_type) if f != "document_type"]

    for field in to_fill:
        current = getattr(meta, field, None)
        if isinstance(current, list):
            default = ", ".join(current) if current else ""
        elif current is None:
            default = ""
        else:
            default = str(current)

        label = FIELD_LABELS.get(field, field)
        answer = Prompt.ask(label, default=default or "", console=console)
        answer = answer.strip()
        if field in ("authors", "chapter_authors", "book_editors"):
            setattr(
                meta,
                field,
                [a.strip() for a in answer.split(",") if a.strip()] if answer else [],
            )
        elif field == "year":
            try:
                setattr(meta, field, int(answer) if answer else None)
            except ValueError:
                setattr(meta, field, None)
        else:
            setattr(meta, field, answer or None)

    still_missing = missing_required(meta)
    if still_missing:
        console.print(
            f"[yellow]Ainda faltam campos obrigatorios: {', '.join(still_missing)}[/yellow]"
        )
        if Confirm.ask("Continuar mesmo assim?", default=False, console=console):
            meta.confidence = max(meta.confidence, threshold)
        else:
            return None

    if Confirm.ask("Preencher campos opcionais?", default=False, console=console):
        optional = [
            f
            for f in BIBLIO_FRONTMATTER_FIELDS
            if f not in REQUIRED_FIELDS.get(meta.document_type, ())
        ]
        for field in optional:
            current = getattr(meta, field, None)
            if isinstance(current, list) and current:
                continue
            if isinstance(current, str) and current.strip():
                continue
            if current not in (None, "", []):
                continue
            label = FIELD_LABELS.get(field, field)
            answer = Prompt.ask(f"{label} (opcional)", default="", console=console)
            answer = answer.strip()
            if not answer:
                continue
            if field in ("authors", "chapter_authors", "book_editors"):
                setattr(meta, field, [a.strip() for a in answer.split(",") if a.strip()])
            else:
                setattr(meta, field, answer)

    _show_preview(meta, title="Metadados finais")
    if not Confirm.ask("Confirmar e gravar SRC?", default=True, console=console):
        return None

    meta.confidence = max(meta.confidence, threshold)
    return BibliographicMetadata.model_validate(meta.model_dump())
