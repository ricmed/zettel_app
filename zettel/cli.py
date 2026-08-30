"""CLI — Typer + Rich interface for the Zettelkasten pipeline.

Commands:
  init        — Initialize vault, database, and vector store
  harvest     — Scan inbox, extract text, create SRC + LIT index, chunk (+ pages)
  dump-chunks — Export persisted chunks as markdown for chunking inspection
  dump-extraction — Export persisted extraction Markdown (Docling/MD headings)
  extract     — Process chunks with LLM (Prompt 1), write LIT drafts
  review      — Approve/reject granular literature notes
  purge-rejected — Delete rejected chunks from SQLite + Chroma
  delete-source — Delete a source completely from vault + databases
  connect     — Generate permanent notes from approved candidates (Prompt 2)
  garden      — Cluster notes and generate/update MOCs
  ask         — QA over the vault (hybrid retrieval + graph)
  article     — LangGraph long-form article (blog/academic + HITL + judge)
  new-note    — Scaffold a manual note in the vault (sync-manual indexes later)
  sync-manual — Sync manual notes from vault to index
  run-all     — Execute harvest → extract → review → connect → garden
  status      — Show pipeline statistics
  doctor      — Validate configuration and dependencies
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    name="zettel",
    help="Zettelkasten — Pipeline automatizado de geração de notas",
    add_completion=False,
)
console = Console()


def _load_deps(config_path: str | None = None):
    """Load config, state DB, and vector index."""
    from zettel.config import AppConfig, load_config, setup_logging

    cfg = load_config(config_path)
    setup_logging(cfg.log_level)
    return cfg


def _get_db(cfg):
    from zettel.state import StateDB
    return StateDB(cfg.state_db_path)


def _load_approved_candidates(db) -> list[dict]:
    """Load approved concepts without notes from SQLite for ``connect``."""
    from zettel.schemas import PermanentNoteCandidate

    candidates: list[dict] = []
    for concept in db.get_concepts_by_status("approved", without_notes=True):
        raw = concept.get("candidate_json")
        if not raw:
            continue
        candidates.append({
            "concept_id": concept["concept_id"],
            "source_id": concept["source_id"],
            "chunk_id": concept["chunk_id"],
            "candidate": PermanentNoteCandidate.model_validate_json(raw),
        })
    return candidates


def _fmt_embedding_id(provider: str | None, model: str | None, dimensions: int | None) -> str:
    base = f"{provider}/{model}"
    if dimensions is not None:
        return f"{base}@{dimensions}d"
    return base


def _idx_kwargs(cfg, *, reset_mismatched: bool = False) -> dict:
    return {
        "chroma_path": cfg.chroma_path,
        "embedding_provider": cfg.embedding.provider,
        "embedding_model": cfg.embedding.model,
        "device": cfg.device,
        "allow_fallback": cfg.embedding.allow_fallback,
        "base_url": cfg.embedding.base_url,
        "dimensions": cfg.embedding.dimensions,
        "reset_mismatched": reset_mismatched,
    }


def _warn_embedding_mismatch(exc) -> None:
    """Print a clear warning when the configured embedding differs from Chroma."""
    stored = _fmt_embedding_id(
        exc.stored_provider, exc.stored_model, getattr(exc, "stored_dimensions", None),
    )
    current = _fmt_embedding_id(
        exc.current_provider, exc.current_model, getattr(exc, "current_dimensions", None),
    )
    console.print(Panel(
        f"[yellow]O modelo de embedding mudou.[/yellow]\n\n"
        f"  Chroma (atual): [bold]{stored}[/bold]\n"
        f"  Config (novo):  [bold]{current}[/bold]\n\n"
        f"Os vetores existentes sao incompativeis com o novo espaco. "
        f"E necessario regenerar TODOS os embeddings a partir do SQLite "
        f"([bold]zettel reindex --force[/bold]).\n"
        f"Isso [bold]nao[/bold] reescreve notas .md nem chama o LLM.\n"
        f"Apos a troca, considere recalibrar "
        f"`retrieval.relevance_floor.min_vector_similarity` e os limiares de dedupe "
        f"se a qualidade da busca degradar.",
        title="Troca de embedding",
        border_style="yellow",
    ))


def _confirm_embedding_reprocess(yes: bool) -> bool:
    if yes:
        return True
    return typer.confirm("Reprocessar todos os embeddings agora?", default=False)


def _get_idx(cfg, db=None, yes: bool = False):
    """Open VectorIndex; on embedding-space drift, warn, confirm, and reindex.

    Args:
        db: StateDB used to repopulate Chroma after a confirmed reprocess.
            Opened temporarily if omitted.
        yes: skip interactive confirmation (CI / ``--yes``).
    """
    from zettel.index import EmbeddingSpaceMismatch, VectorIndex

    try:
        return VectorIndex(**_idx_kwargs(cfg))
    except EmbeddingSpaceMismatch as exc:
        _warn_embedding_mismatch(exc)
        if not _confirm_embedding_reprocess(yes):
            console.print(
                "[red]Abortado.[/red] Para regenerar os vetores depois: "
                "[bold]zettel reindex --force[/bold] "
                "(ou passe --yes em scripts)."
            )
            raise typer.Exit(1) from exc

        own_db = db is None
        if own_db:
            db = _get_db(cfg)
        try:
            idx = VectorIndex(**_idx_kwargs(cfg, reset_mismatched=True))
            from zettel.rebuild import run_reindex
            with console.status(
                "[bold blue]Regenerando embeddings (reindex --force)...",
                spinner="dots",
            ):
                stats = run_reindex(cfg, db, idx, force=True)
            table = Table(title="Reindex (troca de embedding)")
            table.add_column("Colecao", style="bold")
            table.add_column("Vetores", justify="right")
            for k, v in stats.items():
                table.add_row(k, str(v))
            console.print(table)
            return idx
        finally:
            if own_db and db is not None:
                db.close()


# ── init ──────────────────────────────────────────────────────────────


@app.command()
def init(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Caminho para config.yaml"),
    vault: Optional[str] = typer.Option(None, "--vault", help="Caminho do vault (override)"),
    inbox: Optional[str] = typer.Option(None, "--inbox", help="Caminho do inbox (override)"),
    reset: bool = typer.Option(False, "--reset", help="Apagar e recriar todas as bases de dados"),
):
    """Inicializar o vault, banco de dados e índice vetorial."""
    cfg = _load_deps(config)
    if vault:
        cfg.vault_path = Path(vault).resolve()
    if inbox:
        cfg.inbox_path = Path(inbox).resolve()

    if reset:
        import shutil

        confirmed = typer.confirm(
            "Isso vai APAGAR o State DB, ChromaDB e cache. Continuar?"
        )
        if not confirmed:
            console.print("[yellow]Reset cancelado.[/yellow]")
            raise typer.Exit(0)

        # Remove State DB
        if cfg.state_db_path.exists():
            cfg.state_db_path.unlink()
            # WAL/SHM companion files
            for suffix in ("-wal", "-shm"):
                companion = cfg.state_db_path.with_name(cfg.state_db_path.name + suffix)
                if companion.exists():
                    companion.unlink()
            console.print(f"[red]State DB removido:[/red] {cfg.state_db_path}")

        # Remove ChromaDB
        if cfg.chroma_path.exists():
            shutil.rmtree(cfg.chroma_path)
            console.print(f"[red]ChromaDB removido:[/red] {cfg.chroma_path}")

        # Remove cache
        if cfg.cache_path.exists():
            shutil.rmtree(cfg.cache_path)
            console.print(f"[red]Cache removido:[/red] {cfg.cache_path}")

    from zettel.vault import init_vault
    init_vault(cfg.vault_path)

    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=False)
    db.close()

    # Ensure directories
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    cfg.cache_path.mkdir(parents=True, exist_ok=True)

    from zettel.config import detect_device, get_gpu_info
    device = detect_device(cfg.device)
    gpu = get_gpu_info()
    device_line = f"[green]Device:[/green] {device.upper()}"
    if gpu["available"]:
        device_line += f" ({gpu.get('device_name', '?')} — {gpu.get('vram_gb', '?')} GB VRAM)"

    reset_line = "\n[red]Bases de dados recriadas do zero.[/red]" if reset else ""
    console.print(Panel(
        f"[green]Vault inicializado em:[/green] {cfg.vault_path}\n"
        f"[green]Inbox:[/green] {cfg.inbox_path}\n"
        f"[green]State DB:[/green] {cfg.state_db_path}\n"
        f"[green]ChromaDB:[/green] {cfg.chroma_path}\n"
        f"{device_line}{reset_line}",
        title="Zettelkasten -- Init",
        border_style="green",
    ))


# ── harvest ───────────────────────────────────────────────────────────


@app.command()
def harvest(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    move_processed: bool = typer.Option(False, "--move-processed", help="Mover arquivos processados"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Modo nao-interativo: default da config para duplicatas; confirma reprocessamento se embedding mudou",
    ),
    skip_duplicates: bool = typer.Option(
        False, "--skip-duplicates",
        help="Modo nao-interativo: sempre pula arquivos com suspeita de duplicidade",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Modo nao-interativo: sempre trata arquivos suspeitos como novas fontes",
    ),
    skip_biblio: bool = typer.Option(
        False, "--skip-biblio",
        help="Modo nao-interativo: permite seguir com metadados bibliograficos incompletos",
    ),
    content_start_file: Optional[int] = typer.Option(
        None, "--content-start-file",
        help="Pagina do arquivo (PDF) onde o conteudo comeca (1-based)",
    ),
    content_start_book: Optional[int] = typer.Option(
        None, "--content-start-book",
        help="Numero impresso nessa primeira pagina de conteudo (default 1)",
    ),
    skip_paging: bool = typer.Option(
        False, "--skip-paging",
        help="Nao detectar paginacao; arquivo p.1 = impressa p.1 (ignora heuristica)",
    ),
    dump_chunks: bool = typer.Option(
        False, "--dump-chunks",
        help="Salvar markdown com todos os chunks da fonte para inspecao",
    ),
    dump_dir: Optional[str] = typer.Option(
        None, "--dump-dir",
        help="Diretorio do dump de chunks (implica --dump-chunks; default: cache/chunk-dumps)",
    ),
    dump_extraction: bool = typer.Option(
        False, "--dump-extraction",
        help="Salvar Markdown extraido (Docling/MD, headings H1-H6) para inspecao",
    ),
    dump_extraction_dir: Optional[str] = typer.Option(
        None, "--dump-extraction-dir",
        help="Diretorio do dump de extracao (implica --dump-extraction; default: cache/extraction-dumps)",
    ),
):
    """Escanear inbox, extrair texto, criar SRC + indice LIT e chunks."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    interactive, duplicate_action = _resolve_duplicate_flags(yes, skip_duplicates, force)
    chunk_dump_dir = _resolve_chunk_dump_dir(cfg, dump_chunks, dump_dir)
    extraction_dump_dir = _resolve_extraction_dump_dir(
        cfg, dump_extraction, dump_extraction_dir,
    )

    from zettel.harvester import run_harvest
    if interactive:
        # Nao usar console.status aqui: prompts interativos (bibliografia / duplicatas)
        # precisam do terminal livre; o spinner engole o Prompt.ask e parece travado.
        console.print(
            "[dim]Coletando arquivos do inbox "
            "(pode solicitar metadados bibliograficos / inicio de paginacao)...[/dim]"
        )
        new_sources = run_harvest(
            cfg, db, idx, interactive=True, skip_biblio=skip_biblio,
            content_start_file=content_start_file,
            content_start_book=content_start_book,
            skip_paging=skip_paging,
            dump_dir=chunk_dump_dir,
            extraction_dump_dir=extraction_dump_dir,
        )
    else:
        console.print(f"[dim]Modo nao-interativo — duplicatas suspeitas: '{duplicate_action}'[/dim]")
        if skip_biblio:
            console.print("[dim]Bibliografia incompleta permitida (--skip-biblio)[/dim]")
        new_sources = run_harvest(
            cfg, db, idx, interactive=False, duplicate_action=duplicate_action,
            skip_biblio=skip_biblio,
            content_start_file=content_start_file,
            content_start_book=content_start_book,
            skip_paging=skip_paging,
            dump_dir=chunk_dump_dir,
            extraction_dump_dir=extraction_dump_dir,
        )

    if new_sources:
        console.print(f"[green]Fontes processadas: {len(new_sources)}[/green]")
        for sid in new_sources:
            console.print(f"  - {sid}")
        if chunk_dump_dir:
            console.print(f"[dim]Dump de chunks gravado em: {chunk_dump_dir}[/dim]")
        if extraction_dump_dir:
            console.print(f"[dim]Dump de extracao gravado em: {extraction_dump_dir}[/dim]")
    else:
        console.print("[yellow]Nenhum arquivo novo encontrado no inbox.[/yellow]")

    last_run = db.get_last_run()
    if last_run:
        dup_total = (
            last_run.get("duplicate_file_count", 0)
            + last_run.get("duplicate_content_count", 0)
            + last_run.get("duplicate_semantic_count", 0)
        )
        if dup_total:
            console.print(
                f"[yellow]Duplicatas detectadas nesta execucao: {dup_total}[/yellow] "
                f"(arquivo: {last_run.get('duplicate_file_count', 0)}, "
                f"conteudo: {last_run.get('duplicate_content_count', 0)}, "
                f"semantica: {last_run.get('duplicate_semantic_count', 0)})"
            )

    if move_processed and new_sources:
        processed_dir = cfg.inbox_path.parent / "processed"
        processed_dir.mkdir(exist_ok=True)
        console.print(f"[dim]Arquivos processados podem ser movidos manualmente para: {processed_dir}[/dim]")

    db.close()


def _resolve_duplicate_flags(
    yes: bool, skip_duplicates: bool, force: bool
) -> tuple[bool, Optional[str]]:
    """Translate --yes/--skip-duplicates/--force into (interactive, duplicate_action)."""
    if skip_duplicates and force:
        console.print("[red]--skip-duplicates e --force sao mutuamente exclusivos.[/red]")
        raise typer.Exit(1)
    if skip_duplicates:
        return False, "skip"
    if force:
        return False, "continue"
    if yes:
        return False, None
    return True, None


def _resolve_chunk_dump_dir(
    cfg, dump_chunks: bool, dump_dir: Optional[str]
) -> Optional[Path]:
    """Resolve --dump-chunks / --dump-dir into a directory, or None when dump is off."""
    if dump_dir:
        return Path(dump_dir).expanduser().resolve()
    if dump_chunks:
        from zettel.chunk_dump import default_dump_dir
        return default_dump_dir(cfg)
    return None


def _resolve_extraction_dump_dir(
    cfg, dump_extraction: bool, dump_extraction_dir: Optional[str]
) -> Optional[Path]:
    """Resolve --dump-extraction / --dump-extraction-dir, or None when dump is off."""
    if dump_extraction_dir:
        return Path(dump_extraction_dir).expanduser().resolve()
    if dump_extraction:
        from zettel.extraction_dump import default_dump_dir
        return default_dump_dir(cfg)
    return None


# ── extract ───────────────────────────────────────────────────────────


@app.command()
def extract(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente o reprocessamento se o embedding mudou",
    ),
    auto_approve: bool = typer.Option(
        False, "--auto-approve",
        help="Aprovar automaticamente drafts com confianca >= limiar (literature_review)",
    ),
):
    """Processar chunks pendentes com LLM (Prompt 1), gerar drafts de LIT granular."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    from zettel.extractor import run_extract
    with console.status("[bold blue]Extraindo conceitos dos chunks...", spinner="dots"):
        candidates = run_extract(cfg, db, idx, auto_approve=auto_approve)

    console.print(
        f"[green]Candidatos em awaiting_review: {len(candidates)}[/green] "
        "(use `zettel review` antes do connect)"
    )

    db.close()


@app.command()
def review(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    source_id: Optional[str] = typer.Option(None, "--source-id", help="Filtrar por fonte"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Nao-interativo: aprova todos com confianca >= limiar",
    ),
    auto_approve: bool = typer.Option(
        False, "--auto-approve",
        help="Aprovar automaticamente drafts com confianca >= limiar",
    ),
    low_confidence_only: bool = typer.Option(
        False, "--low-confidence-only",
        help="Listar apenas drafts abaixo do limiar",
    ),
):
    """Aprovar/rejeitar Notas de Literatura granulares antes do connect."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    from zettel.review import run_review
    interactive = not (yes or auto_approve)
    stats = run_review(
        cfg, db, idx,
        source_id=source_id,
        auto_approve=auto_approve or yes,
        interactive=interactive,
        low_confidence_only=low_confidence_only,
    )
    console.print(
        f"[green]Aprovados: {stats['approved']}[/green] | "
        f"[red]Rejeitados: {stats['rejected']}[/red] | "
        f"[yellow]Pulados: {stats['skipped']}[/yellow]"
    )
    db.close()


@app.command(name="purge-rejected")
def purge_rejected_cmd(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    source_id: Optional[str] = typer.Option(None, "--source-id", help="Filtrar por fonte"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar exclusao permanente sem prompt",
    ),
    no_compact: bool = typer.Option(
        False, "--no-compact",
        help="Nao rodar VACUUM apos o purge (nao recupera espaco em disco)",
    ),
):
    """Apagar chunks rejeitados do SQLite e do Chroma (irreversivel).

    Remove linhas em chunks/concepts, embeddings em Chroma chunks e
    literature_notes associados. Por padrao compacta state.db e chroma.sqlite3
    com VACUUM (nao altera dados logicos restantes). Nao afeta notas permanentes
    nem LITs aprovadas.
    """
    cfg = _load_deps(config)
    db = _get_db(cfg)
    pending = db.get_chunks_by_status("rejected", source_id=source_id)
    n = len(pending)
    if n == 0:
        console.print("[yellow]Nenhum chunk rejected para purge.[/yellow]")
        db.close()
        return

    console.print(
        f"[yellow]{n} chunk(s) rejected serao apagados do SQLite e do Chroma.[/yellow]"
    )
    if not no_compact:
        console.print(
            "[dim]Apos a exclusao, VACUUM compactara state.db e chroma.sqlite3 "
            "(seguro para o conteudo restante; pode levar alguns minutos).[/dim]"
        )
    if not yes and not typer.confirm("Excluir permanentemente?", default=False):
        console.print("[dim]Purge cancelado.[/dim]")
        db.close()
        return

    idx = _get_idx(cfg, db=db, yes=yes)
    from zettel.review import purge_rejected
    result = purge_rejected(
        cfg, db, idx, source_id=source_id, compact=not no_compact,
    )
    console.print(
        f"[green]Removidos: {result['chunks']} chunks "
        f"({result['literature_notes']} literature_ids no Chroma).[/green]"
    )
    if result.get("compacted"):
        console.print(
            f"[green]Compactado: state.db "
            f"{result['state_mb_before']}→{result['state_mb_after']} MB; "
            f"chroma.sqlite3 "
            f"{result['chroma_mb_before']}→{result['chroma_mb_after']} MB.[/green]"
        )
    db.close()


@app.command(name="delete-source")
def delete_source_cmd(
    source_id: str = typer.Argument(..., help="Fonte a apagar (ex. @Citekey)"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar exclusao permanente sem prompt",
    ),
    delete_permanent: bool = typer.Option(
        False, "--delete-permanent",
        help="Apagar tambem notas permanentes (ZTL) ligadas a fonte",
    ),
    no_compact: bool = typer.Option(
        False, "--no-compact",
        help="Nao rodar VACUUM apos a exclusao (nao recupera espaco em disco)",
    ),
):
    """Apagar uma fonte por completo do vault, SQLite e Chroma (irreversivel).

    Remove SRC, indice LIT, notas granulares e drafts em Review. Por padrao
    mantem notas permanentes (ZTL) mas limpa wikilinks mortos no restante do
    vault. Use --delete-permanent para remover ZTL ligadas a fonte.
    """
    from zettel.purge_source import normalize_source_id, purge_source

    cfg = _load_deps(config)
    db = _get_db(cfg)
    sid = normalize_source_id(source_id)
    source = db.get_source(sid)
    if not source:
        console.print(f"[red]Fonte nao encontrada: {sid}[/red]")
        db.close()
        raise typer.Exit(1)

    chunks = db.get_chunks_for_source(sid)
    permanent = db.get_note_ids_for_source(sid)
    console.print(
        f"[yellow]Fonte {sid} ({source.get('title') or source.get('citekey')}):[/yellow] "
        f"{len(chunks)} chunk(s), {len(permanent)} nota(s) permanente(s) ligada(s)."
    )
    if delete_permanent and permanent:
        console.print(
            f"[red]{len(permanent)} nota(s) permanente(s) serao apagadas.[/red]"
        )
    elif permanent:
        console.print(
            "[dim]Notas permanentes serao mantidas; wikilinks mortos serao limpos.[/dim]"
        )
    if not no_compact:
        console.print(
            "[dim]Apos a exclusao, VACUUM compactara state.db e chroma.sqlite3.[/dim]"
        )
    if not yes and not typer.confirm("Excluir fonte permanentemente?", default=False):
        console.print("[dim]Exclusao cancelada.[/dim]")
        db.close()
        return

    idx = _get_idx(cfg, db=db, yes=yes)
    result = purge_source(
        cfg, db, idx, sid,
        delete_permanent=delete_permanent,
        compact=not no_compact,
    )
    vault = result["vault"]
    console.print(
        f"[green]Fonte {sid} removida:[/green] "
        f"SRC={vault['src']}, LIT index={vault['lit_index']}, "
        f"granulares={vault['lit_granular']}, drafts={vault['review_drafts']}, "
        f"assets={vault['assets']}."
    )
    sqlite = result["sqlite"]
    console.print(
        f"[green]SQLite:[/green] {sqlite.get('chunks', 0)} chunks, "
        f"{sqlite.get('chapters', 0)} chapters, "
        f"{sqlite.get('concepts', 0)} concepts."
    )
    console.print(
        f"[green]Chroma:[/green] {result['chunks_chroma']} chunks, "
        f"{result['literature_chroma']} literature_notes, 1 source."
    )
    if result["permanent_deleted"]:
        console.print(
            f"[green]Permanentes apagadas:[/green] {result['permanent_deleted']}"
        )
    if result["wikilinks_cleaned"]:
        console.print(
            f"[green]Wikilinks limpos em {result['wikilinks_cleaned']} arquivo(s).[/green]"
        )
    if result.get("compacted"):
        console.print(
            f"[green]Compactado: state.db "
            f"{result['state_mb_before']}->{result['state_mb_after']} MB; "
            f"chroma.sqlite3 "
            f"{result['chroma_mb_before']}->{result['chroma_mb_after']} MB.[/green]"
        )
    db.close()


# ── connect ───────────────────────────────────────────────────────────


@app.command()
def connect(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    topk: Optional[int] = typer.Option(None, "--topk", help="Top-k notas similares"),
    dedupe_threshold: Optional[float] = typer.Option(None, "--dedupe-threshold"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente o reprocessamento se o embedding mudou",
    ),
):
    """Gerar notas permanentes a partir dos candidatos aprovados no review."""
    cfg = _load_deps(config)
    if topk:
        cfg.linking.topk = topk
    if dedupe_threshold:
        cfg.linking.dedupe_threshold = dedupe_threshold

    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    candidates = _load_approved_candidates(db)

    if not candidates:
        console.print(
            "[red]Nenhum candidato aprovado. Execute 'extract' e 'review' primeiro.[/red]"
        )
        db.close()
        raise typer.Exit(1)

    from zettel.connector import run_connect
    with console.status("[bold blue]Gerando notas permanentes...", spinner="dots"):
        note_ids = run_connect(cfg, db, idx, candidates)

    console.print(f"[green]Notas permanentes criadas: {len(note_ids)}[/green]")
    for nid in note_ids:
        console.print(f"  - {nid}")

    db.close()


# ── garden ────────────────────────────────────────────────────────────


@app.command()
def garden(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    min_cluster_size: Optional[int] = typer.Option(None, "--min-cluster-size"),
    hubs: bool = typer.Option(
        False, "--hubs",
        help="Gerar MOCs ancorados em notas-hub do grafo (complementar ao pipeline taxonomico)",
    ),
    recreate: bool = typer.Option(
        False, "--recreate",
        help="Apagar MOCs gerados pelo pipeline e regenerar do zero",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente (--recreate e reprocessamento de embedding)",
    ),
):
    """Clusterizar notas e gerar/atualizar MOCs."""
    cfg = _load_deps(config)
    if min_cluster_size:
        cfg.gardener.min_cluster_size = min_cluster_size

    if recreate and not yes:
        target = "hub" if hubs else "taxonomia"
        if not typer.confirm(
            f"Apaga todos os MOCs do pipeline ({target}) (vault, banco e indice) e regenera. Continuar?",
            default=False,
        ):
            raise typer.Exit(0)

    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    if hubs:
        from zettel.gardener_hub import run_garden_hubs
        with console.status("[bold blue]Cultivando MOCs hub...", spinner="dots"):
            moc_ids = run_garden_hubs(cfg, db, idx, recreate=recreate)
        if recreate:
            console.print("[dim]MOCs hub do pipeline foram removidos antes da geracao.[/dim]")
    else:
        from zettel.gardener import run_garden
        with console.status("[bold blue]Cultivando o jardim de notas...", spinner="dots"):
            moc_ids = run_garden(cfg, db, idx, recreate=recreate)
        if recreate:
            console.print("[dim]MOCs do pipeline foram removidos antes da geracao.[/dim]")

    if moc_ids:
        console.print(f"[green]MOCs gerados/atualizados: {len(moc_ids)}[/green]")
        for mid in moc_ids:
            console.print(f"  - {mid}")
    else:
        console.print("[yellow]Nenhum MOC gerado (notas insuficientes ou ja atualizados).[/yellow]")

    db.close()


# ── retry-failed ──────────────────────────────────────────────────────


@app.command(name="retry-failed")
def retry_failed(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    source_id: Optional[str] = typer.Option(None, "--source-id", help="Filtrar por source_id"),
    assets: bool = typer.Option(False, "--assets", help="Resetar imagens com falha de descricao"),
):
    """Resetar chunks (ou imagens) com falha para 'pending', permitindo reprocessar."""
    cfg = _load_deps(config)
    db = _get_db(cfg)

    if assets:
        n = db.reset_failed_assets()
        if n:
            console.print(
                f"[green]{n} imagem(ns) resetada(s) para 'pending'. "
                f"Execute 'extract' para redescreve-las.[/green]"
            )
        else:
            console.print("[yellow]Nenhuma imagem com falha encontrada.[/yellow]")
        db.close()
        return

    failed = db.get_failed_chunks(source_id if source_id else None)
    count = len(failed)

    if count == 0:
        console.print("[yellow]Nenhum chunk com falha encontrado.[/yellow]")
        db.close()
        return

    for chunk in failed:
        db.update_chunk_status(chunk["chunk_id"], "pending")

    console.print(
        f"[green]{count} chunk(s) resetado(s) para 'pending'. "
        f"Execute 'extract' para reprocessar.[/green]"
    )
    db.close()


# ── rechunk ───────────────────────────────────────────────────────────


@app.command()
def rechunk(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    source_id: Optional[str] = typer.Option(None, "--source-id", help="Rechunk apenas esta fonte"),
    all_sources: bool = typer.Option(False, "--all", help="Rechunk de todas as fontes"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente o reprocessamento se o embedding mudou",
    ),
    dump_chunks: bool = typer.Option(
        False, "--dump-chunks",
        help="Salvar markdown com todos os chunks da fonte para inspecao",
    ),
    dump_dir: Optional[str] = typer.Option(
        None, "--dump-dir",
        help="Diretorio do dump de chunks (implica --dump-chunks; default: cache/chunk-dumps)",
    ),
):
    """Re-chunkar fontes a partir do texto extraido persistido (aplica config atual)."""
    if not source_id and not all_sources:
        console.print("[red]Informe --source-id <id> ou --all.[/red]")
        raise typer.Exit(1)

    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)
    chunk_dump_dir = _resolve_chunk_dump_dir(cfg, dump_chunks, dump_dir)

    from zettel.harvester import run_rechunk
    with console.status("[bold blue]Re-chunkando fontes...", spinner="dots"):
        stats = run_rechunk(
            cfg, db, idx, source_id if source_id else None, dump_dir=chunk_dump_dir,
        )

    console.print(
        f"[green]Rechunk concluido:[/green] {stats['sources']} fonte(s), "
        f"{stats['chunks']} chunk(s), {stats['skipped']} pulada(s)."
    )
    if chunk_dump_dir and stats["sources"]:
        console.print(f"[dim]Dump de chunks gravado em: {chunk_dump_dir}[/dim]")
    if stats["skipped"]:
        console.print(
            "[yellow]Fontes puladas nao tem texto extraido persistido (anteriores a Fase 0). "
            "Reprocesse o arquivo original via harvest.[/yellow]"
        )
    db.close()


# ── dump-chunks ───────────────────────────────────────────────────────


@app.command(name="dump-chunks")
def dump_chunks_cmd(
    source_id: Optional[str] = typer.Option(None, "--source-id", help="Exportar apenas esta fonte"),
    all_sources: bool = typer.Option(False, "--all", help="Exportar todas as fontes"),
    dump_dir: Optional[str] = typer.Option(
        None, "--dump-dir",
        help="Diretorio de saida (default: cache/chunk-dumps)",
    ),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Exportar chunks persistidos como markdown para inspecionar o chunking."""
    if not source_id and not all_sources:
        console.print("[red]Informe --source-id <id> ou --all.[/red]")
        raise typer.Exit(1)

    cfg = _load_deps(config)
    db = _get_db(cfg)
    dest = Path(dump_dir).expanduser().resolve() if dump_dir else None

    from zettel.chunk_dump import default_dump_dir, run_dump_chunks
    dest = dest or default_dump_dir(cfg)
    try:
        stats = run_dump_chunks(
            cfg, db, source_id if source_id else None, dump_dir=dest,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        db.close()
        raise typer.Exit(1)

    console.print(
        f"[green]Dump concluido:[/green] {stats['sources']} fonte(s) em {dest}"
    )
    db.close()


# ── dump-extraction ───────────────────────────────────────────────────


@app.command(name="dump-extraction")
def dump_extraction_cmd(
    source_id: Optional[str] = typer.Option(None, "--source-id", help="Exportar apenas esta fonte"),
    all_sources: bool = typer.Option(False, "--all", help="Exportar todas as fontes"),
    dump_dir: Optional[str] = typer.Option(
        None, "--dump-dir",
        help="Diretorio de saida (default: cache/extraction-dumps)",
    ),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Exportar o Markdown extraido (Docling/MD) para inspecionar headings H1-H6."""
    if not source_id and not all_sources:
        console.print("[red]Informe --source-id <id> ou --all.[/red]")
        raise typer.Exit(1)

    cfg = _load_deps(config)
    db = _get_db(cfg)
    dest = Path(dump_dir).expanduser().resolve() if dump_dir else None

    from zettel.extraction_dump import default_dump_dir, run_dump_extraction
    dest = dest or default_dump_dir(cfg)
    try:
        stats = run_dump_extraction(
            cfg, db, source_id if source_id else None, dump_dir=dest,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        db.close()
        raise typer.Exit(1)

    console.print(
        f"[green]Dump concluido:[/green] {stats['sources']} fonte(s) em {dest}"
    )
    if stats.get("skipped"):
        console.print(
            f"[yellow]{stats['skipped']} fonte(s) pulada(s) sem texto extraido persistido.[/yellow]"
        )
    db.close()


# ── set-paging ────────────────────────────────────────────────────────


@app.command(name="set-paging")
def set_paging_cmd(
    source_id: str = typer.Option(..., "--source-id", help="Fonte a corrigir (ex. @Citekey)"),
    content_start_file: int = typer.Option(
        ..., "--content-start-file",
        help="Pagina do arquivo (PDF) onde o conteudo comeca (1-based)",
    ),
    content_start_book: int = typer.Option(
        1, "--content-start-book",
        help="Numero impresso nessa primeira pagina de conteudo",
    ),
    drop_before_start: bool = typer.Option(
        False, "--drop-before-start",
        help="Tambem remove chunks awaiting_review/aprovados antes do inicio",
    ),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente reprocessamento de embedding se necessario",
    ),
):
    """Corrigir paginacao de uma fonte ja harvestada (sem re-chamar o LLM)."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    from zettel.harvester import run_set_paging
    try:
        stats = run_set_paging(
            cfg, db, idx, source_id,
            content_start_file=content_start_file,
            content_start_book=content_start_book,
            drop_before_start=drop_before_start,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[green]Paginacao atualizada para {source_id}:[/green] "
        f"arquivo p.{content_start_file} = impressa p.{content_start_book}\n"
        f"  chunks atualizados: {stats['updated']}\n"
        f"  pending removidos (antes do inicio): {stats['dropped_pending']}\n"
        f"  outros removidos (--drop-before-start): {stats['dropped_other']}\n"
        f"  notas LIT patchadas: {stats['notes_patched']}"
    )
    db.close()


# ── new-note ──────────────────────────────────────────────────────────


@app.command(name="new-note")
def new_note(
    note_type: str = typer.Argument(
        ...,
        help="Tipo: ztl|lit|src|moc (ou permanent|literature|source)",
    ),
    title: str = typer.Argument(..., help="Titulo da nota"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    citekey: Optional[str] = typer.Option(
        None, "--citekey", "-k",
        help="Citekey para SRC/LIT (sem @); alias de --source-id para SRC/ZTL",
    ),
    source_id: Optional[str] = typer.Option(
        None, "--source-id", "-s",
        help="source_id (@Citekey) explicito para SRC ou vinculo de ZTL a uma SRC",
    ),
    author: Optional[list[str]] = typer.Option(
        None, "--author", "-a",
        help="Autor(es) para SRC/LIT (repita a opcao para varios)",
    ),
    year: Optional[int] = typer.Option(None, "--year", "-y", help="Ano para SRC/LIT"),
    document_type: Optional[str] = typer.Option(
        None, "--document-type", "-t",
        help="Tipo documental ABNT para SRC (ex.: livro, artigo_periodico)",
    ),
    abnt_reference: Optional[str] = typer.Option(
        None, "--abnt-reference",
        help="Referencia ABNT pronta para copiar (SRC)",
    ),
    publisher: Optional[str] = typer.Option(None, "--publisher", help="Editora (SRC)"),
    place: Optional[str] = typer.Option(None, "--place", help="Local de publicacao (SRC)"),
    doi: Optional[str] = typer.Option(None, "--doi", help="DOI (SRC)"),
    url: Optional[str] = typer.Option(None, "--url", help="URL (SRC)"),
    journal: Optional[str] = typer.Option(None, "--journal", help="Periodico (SRC)"),
    edition: Optional[str] = typer.Option(None, "--edition", help="Edicao (SRC)"),
    institution: Optional[str] = typer.Option(None, "--institution", help="Instituicao (SRC)"),
    pages: Optional[str] = typer.Option(None, "--pages", help="Paginas (SRC)"),
    granular: bool = typer.Option(
        False, "--granular",
        help="LIT granular em 20_Literature/{citekey}/ (padrao: indice na raiz)",
    ),
    chunk_index: int = typer.Option(
        1, "--chunk-index",
        help="Indice do chunk para LIT granular (padrao: 1)",
    ),
    page: Optional[int] = typer.Option(
        None, "--page", "-p",
        help="Pagina impressa para LIT granular",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Sobrescrever arquivo existente no mesmo caminho",
    ),
):
    """Criar esqueleto de nota manual no vault (indexar depois com sync-manual)."""
    cfg = _load_deps(config)

    from zettel.new_note import normalize_note_type, scaffold_manual_note

    try:
        normalized = normalize_note_type(note_type)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    effective_source_id = source_id
    if not effective_source_id and citekey and normalized == "permanent":
        effective_source_id = citekey
    if not effective_source_id and citekey and normalized == "source":
        effective_source_id = citekey

    try:
        result = scaffold_manual_note(
            cfg,
            note_type,
            title,
            citekey=citekey,
            source_id=effective_source_id,
            authors=list(author or []),
            year=year,
            document_type=document_type,
            abnt_reference=abnt_reference,
            place=place,
            publisher=publisher,
            doi=doi,
            url=url,
            journal=journal,
            edition=edition,
            institution=institution,
            pages=pages,
            granular=granular,
            chunk_index=chunk_index,
            page=page,
            force=force,
        )
    except FileExistsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Nota criada:[/green] {result.path}")
    if result.warnings:
        for warning in result.warnings:
            console.print(f"[yellow]Aviso:[/yellow] {warning}")
    console.print("[dim]Indexe com: zettel sync-manual[/dim]")


# ── sync-manual ───────────────────────────────────────────────────────


@app.command(name="sync-manual")
def sync_manual(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    rebuild_graph: bool = typer.Option(
        False, "--rebuild-graph",
        help="Re-deriva arestas 'related' dos wikilinks no corpo de todas as notas",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente o reprocessamento se o embedding mudou",
    ),
):
    """Sincronizar notas manuais do vault com o índice vetorial."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    from zettel.sync import rebuild_manual_edges, run_sync_manual

    if rebuild_graph:
        with console.status("[bold blue]Reconstruindo grafo de conexoes...", spinner="dots"):
            gstats = rebuild_manual_edges(db)
        console.print(
            f"[green]Grafo:[/green] {gstats['edges_created']} aresta(s) nova(s) "
            f"de {gstats['notes_scanned']} nota(s) com corpo."
        )

    with console.status("[bold blue]Sincronizando notas manuais...", spinner="dots"):
        stats = run_sync_manual(cfg, db, idx)

    table = Table(title="Sync Manual")
    table.add_column("Métrica", style="bold")
    table.add_column("Valor", justify="right")
    for k, v in stats.items():
        table.add_row(k.capitalize(), str(v))
    console.print(table)

    db.close()


# ── reindex ───────────────────────────────────────────────────────────


@app.command()
def reindex(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    collection: Optional[str] = typer.Option(
        None, "--collection", help="Reindexar apenas: sources|chunks|permanent_notes|mocs"
    ),
    force: bool = typer.Option(False, "--force", help="Resetar a colecao antes de repovoar"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente o reprocessamento se o embedding mudou",
    ),
):
    """Reconstruir o ChromaDB a partir do SQLite (sem chamadas de LLM).

    Se o provider/modelo de embedding no config diferir do marcado no Chroma,
    --force e aplicado automaticamente (aviso + confirmacao, ou --yes).
    Sem --force apos troca de modelo, sources/chunks antigos nao seriam regenerados.
    """
    from zettel.index import EmbeddingSpaceMismatch, VectorIndex, peek_stored_embedding_identity

    cfg = _load_deps(config)
    db = _get_db(cfg)

    stored = peek_stored_embedding_identity(cfg.chroma_path)
    drift = (
        any(x is not None for x in stored)
        and (
            stored[0] != cfg.embedding.provider
            or stored[1] != cfg.embedding.model
            or stored[2] != cfg.embedding.dimensions
        )
    )
    if drift:
        exc = EmbeddingSpaceMismatch(
            stored[0], stored[1], cfg.embedding.provider, cfg.embedding.model,
            stored_dimensions=stored[2],
            current_dimensions=cfg.embedding.dimensions,
        )
        _warn_embedding_mismatch(exc)
        if not force and not _confirm_embedding_reprocess(yes):
            console.print(
                "[red]Abortado.[/red] Use [bold]zettel reindex --force[/bold] "
                "ou passe --yes."
            )
            db.close()
            raise typer.Exit(1)
        force = True
        console.print("[dim]Troca de embedding detectada — aplicando --force.[/dim]")
        idx = VectorIndex(**_idx_kwargs(cfg, reset_mismatched=True))
    else:
        idx = VectorIndex(**_idx_kwargs(cfg))

    from zettel.rebuild import run_reindex
    with console.status("[bold blue]Reconstruindo indice vetorial...", spinner="dots"):
        stats = run_reindex(cfg, db, idx, collection, force)

    table = Table(title="Reindex")
    table.add_column("Colecao", style="bold")
    table.add_column("Vetores", justify="right")
    for k, v in stats.items():
        table.add_row(k, str(v))
    console.print(table)
    db.close()


# ── rebuild ───────────────────────────────────────────────────────────


@app.command()
def rebuild(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    what: str = typer.Option("vault", "--what", help="vault | chroma | all"),
    force: bool = typer.Option(
        False, "--force", help="Sobrescrever arquivos existentes (nunca notas manuais)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simular sem escrever"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente o reprocessamento se o embedding mudou",
    ),
):
    """Reconstruir o vault (.md) e/ou o ChromaDB a partir do SQLite, sem reprocessar LLM."""
    cfg = _load_deps(config)
    db = _get_db(cfg)

    if what not in ("vault", "chroma", "all"):
        console.print("[red]--what deve ser: vault | chroma | all[/red]")
        db.close()
        raise typer.Exit(1)

    if what in ("vault", "all"):
        from zettel.rebuild import run_rebuild_vault
        with console.status("[bold blue]Reconstruindo vault a partir do banco...", spinner="dots"):
            vstats = run_rebuild_vault(cfg, db, force=force, dry_run=dry_run)
        table = Table(title="Rebuild vault" + (" (dry-run)" if dry_run else ""))
        table.add_column("Metrica", style="bold")
        table.add_column("Valor", justify="right")
        for k, v in vstats.items():
            table.add_row(k, str(v))
        console.print(table)

    if what in ("chroma", "all"):
        idx = _get_idx(cfg, db=db, yes=yes)
        from zettel.rebuild import run_reindex
        with console.status("[bold blue]Reconstruindo indice vetorial...", spinner="dots"):
            rstats = run_reindex(cfg, db, idx, force=force)
        table = Table(title="Rebuild chroma")
        table.add_column("Colecao", style="bold")
        table.add_column("Vetores", justify="right")
        for k, v in rstats.items():
            table.add_row(k, str(v))
        console.print(table)

    db.close()


# ── run-all ───────────────────────────────────────────────────────────


@app.command(name="run-all")
def run_all(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simular sem escrever"),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Modo nao-interativo: default da config para duplicatas; confirma reprocessamento se embedding mudou",
    ),
    skip_duplicates: bool = typer.Option(
        False, "--skip-duplicates",
        help="Modo nao-interativo: sempre pula arquivos com suspeita de duplicidade",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Modo nao-interativo: sempre trata arquivos suspeitos como novas fontes",
    ),
    skip_biblio: bool = typer.Option(
        False, "--skip-biblio",
        help="Modo nao-interativo: permite seguir com metadados bibliograficos incompletos",
    ),
):
    """Executar pipeline completo: harvest > extract > review > connect > garden."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    interactive, duplicate_action = _resolve_duplicate_flags(yes, skip_duplicates, force)

    # Phase 1: Harvest
    console.rule("[bold blue]Fase 1 — Harvest")
    from zettel.harvester import run_harvest
    new_sources = run_harvest(
        cfg, db, idx, interactive=interactive, duplicate_action=duplicate_action,
        skip_biblio=skip_biblio,
        skip_paging=False,
    )
    console.print(f"  Fontes: {len(new_sources)}")
    last_run = db.get_last_run()
    if last_run:
        dup_total = (
            last_run.get("duplicate_file_count", 0)
            + last_run.get("duplicate_content_count", 0)
            + last_run.get("duplicate_semantic_count", 0)
        )
        if dup_total:
            console.print(f"  [yellow]Duplicatas detectadas: {dup_total}[/yellow]")

    # Phase 2: Extract
    console.rule("[bold blue]Fase 2 — Extract")
    from zettel.extractor import run_extract
    candidates = run_extract(cfg, db, idx, auto_approve=False)
    console.print(f"  Drafts / candidatos: {len(candidates)}")

    # Phase 2b: Review
    console.rule("[bold blue]Fase 2b — Review")
    from zettel.review import run_review
    rev = run_review(
        cfg, db, idx,
        auto_approve=yes or not interactive,
        interactive=interactive and not yes,
    )
    console.print(
        f"  Aprovados: {rev['approved']} | Rejeitados: {rev['rejected']} | "
        f"Pulados: {rev['skipped']}"
    )

    if dry_run:
        console.print("[yellow]Dry run — parando antes da geracao de notas.[/yellow]")
        db.close()
        return

    # Phase 3: Connect (from DB approved concepts)
    console.rule("[bold blue]Fase 3 — Connect")
    from zettel.connector import run_connect
    connect_cands = _load_approved_candidates(db)
    note_ids = run_connect(cfg, db, idx, connect_cands)
    console.print(f"  Notas permanentes: {len(note_ids)}")

    # Phase 4: Garden
    console.rule("[bold blue]Fase 4 — Garden")
    from zettel.gardener import run_garden
    moc_ids = run_garden(cfg, db, idx)
    console.print(f"  MOCs: {len(moc_ids)}")

    console.rule("[bold green]Pipeline completo!")
    db.close()


# ── ask ───────────────────────────────────────────────────────────────


@app.command()
def ask(
    question: str = typer.Argument(..., help="Pergunta sobre o vault"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    topk: Optional[int] = typer.Option(None, "--topk", help="Numero de notas semente"),
    no_graph: bool = typer.Option(False, "--no-graph", help="Desliga expansao por grafo"),
    mode: Optional[str] = typer.Option(None, "--mode", help="vector | hybrid"),
    show_context: bool = typer.Option(
        False, "--show-context", help="Exibe as notas recuperadas (debug)"
    ),
    save: bool = typer.Option(
        False, "--save", help="Salva a resposta em .md no local padrao (sem perguntar)"
    ),
    save_to: Optional[str] = typer.Option(
        None, "--save-to", help="Salva a resposta em .md no caminho informado"
    ),
    no_save_prompt: bool = typer.Option(
        False, "--no-save-prompt", help="Nao perguntar se deve salvar (para scripts)"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente o reprocessamento se o embedding mudou",
    ),
):
    """Responder uma pergunta usando as notas do vault (recuperacao hibrida + grafo)."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    from zettel.ask import run_ask, save_ask_note

    with console.status("[bold blue]Consultando o acervo...", spinner="dots"):
        result = run_ask(
            cfg, db, idx, question,
            topk=topk,
            use_graph=not no_graph,
            mode=mode,
        )

    console.print(Panel(result.answer.strip() or "(sem resposta)", title="Resposta"))

    # Parameters actually used for this run — shown only with --show-context,
    # since it's a debug/internals view, not part of the default UX.
    if show_context and result.retrieval_params:
        p = result.retrieval_params
        params_table = Table(title="Parametros de recuperacao")
        params_table.add_column("Parametro", style="bold")
        params_table.add_column("Valor", justify="right")
        rows = [
            ("Modo", p["mode"]),
            ("Top-k sementes", p["topk"]),
            ("Max. notas no contexto", p["max_context_notes"]),
            ("RRF k", p["rrf_k"]),
            ("Piso de relevancia ativo", "sim" if p["relevance_floor_enabled"] else "nao"),
            ("Similaridade minima (piso)", f"{p['min_vector_similarity']:.2f}"),
            ("Similaridade minima absoluta", f"{p['absolute_min_similarity']:.2f}"),
            ("Bypass do BM25 ativo", "sim" if p["bm25_hit_bypasses_floor"] else "nao"),
            ("Rank max. para bypass do BM25", p["bm25_bypass_max_rank"]),
            ("Expansao por grafo", "sim" if p["graph_expansion_used"] else "nao"),
            ("Grafo: max. saltos", p["graph_max_hops"]),
            ("Grafo: decaimento por salto", p["graph_decay"]),
            ("Grafo: max. vizinhos", p["graph_max_neighbors"]),
        ]
        for label, value in rows:
            params_table.add_row(label, str(value))
        console.print(params_table)

    # `candidates` is the raw ranked pool (before the relevance floor), always
    # shown so the user can see what was closest even when nothing was relevant
    # enough to answer from (in which case `sources` is empty).
    if show_context or result.candidates:
        ctx_table = Table(title="Notas recuperadas")
        ctx_table.add_column("Nota", style="bold")
        ctx_table.add_column("Score RRF (posicao)", justify="right")
        ctx_table.add_column("Similaridade", justify="right")
        ctx_table.add_column("Rank BM25", justify="right")
        ctx_table.add_column("Salto", justify="right")
        ctx_table.add_column("Usada?")
        ctx_table.add_column("Motivo")
        ctx_table.add_column("Origem")
        for src in result.candidates:
            sim = f"{src.vector_similarity:.2f}" if src.vector_similarity is not None else "-"
            bm25 = str(src.bm25_rank) if src.bm25_rank is not None else "-"
            ctx_table.add_row(
                src.title or src.note_id,
                f"{src.rrf_score:.4f}",
                sim,
                bm25,
                str(src.hop),
                "sim" if src.passed_floor else "nao",
                src.floor_reason,
                src.origin,
                style="" if src.passed_floor else "dim",
            )
        console.print(ctx_table)

    # Save the answer with full provenance.
    saved_path = None
    if save_to:
        saved_path = save_ask_note(result, cfg.vault_path, Path(save_to))
    elif save:
        saved_path = save_ask_note(result, cfg.vault_path)
    elif not no_save_prompt:
        from rich.prompt import Confirm
        try:
            if Confirm.ask("Salvar esta resposta como nota .md?", default=False):
                saved_path = save_ask_note(result, cfg.vault_path)
        except (EOFError, KeyboardInterrupt):
            pass

    if saved_path:
        try:
            rel = saved_path.relative_to(cfg.vault_path)
            console.print(f"[green]Resposta salva em:[/green] {rel}")
        except ValueError:
            console.print(f"[green]Resposta salva em:[/green] {saved_path}")

    db.close()


# ── article ───────────────────────────────────────────────────────────


@app.command()
def article(
    topic: str = typer.Argument(..., help="Tema do artigo"),
    style: str = typer.Option(
        "blog", "--style", "-s", help="blog | academic"
    ),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    topk: Optional[int] = typer.Option(None, "--topk", help="Numero de notas semente"),
    no_graph: bool = typer.Option(False, "--no-graph", help="Desliga expansao por grafo"),
    mode: Optional[str] = typer.Option(None, "--mode", help="vector | hybrid"),
    personality: Optional[str] = typer.Option(
        None, "--personality", "-p", help="Perfil em config/personalities.yaml"
    ),
    style_notes: Optional[str] = typer.Option(
        None, "--style-notes", help="Override textual de estilo"
    ),
    show_context: bool = typer.Option(
        False, "--show-context", help="Exibe notas recuperadas (debug)"
    ),
    outline_only: bool = typer.Option(
        False, "--outline-only", help="Gera so o outline e encerra"
    ),
    skip_context_review: bool = typer.Option(
        False, "--skip-context-review", help="Pula revisao humana do contexto"
    ),
    skip_judge: bool = typer.Option(
        False, "--skip-judge", help="Pula o juiz automatico de qualidade"
    ),
    max_judge_iterations: Optional[int] = typer.Option(
        None, "--max-judge-iterations", help="Max. ciclos de reescrita do juiz"
    ),
    save: bool = typer.Option(
        False, "--save", help="Salva o artigo em .md no local padrao (sem perguntar)"
    ),
    save_to: Optional[str] = typer.Option(
        None, "--save-to", help="Salva o artigo em .md no caminho informado"
    ),
    no_save_prompt: bool = typer.Option(
        False, "--no-save-prompt", help="Nao perguntar se deve salvar (para scripts)"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Confirmar automaticamente o reprocessamento se o embedding mudou",
    ),
):
    """Gerar artigo estruturado (blog ou academico) via LangGraph."""
    style_norm = (style or "blog").strip().lower()
    if style_norm not in ("blog", "academic"):
        console.print("[red]--style deve ser 'blog' ou 'academic'[/red]")
        raise typer.Exit(1)

    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg, db=db, yes=yes)

    from rich.prompt import Prompt

    from zettel.article import parse_extra_queries, save_article_note
    from zettel.article_graph import run_article_graph

    def _hitl(payload: dict) -> dict:
        itype = payload.get("type")
        if itype == "context_review":
            notes = payload.get("notes") or []
            table = Table(title="Notas recuperadas (contexto)")
            table.add_column("#", justify="right")
            table.add_column("Titulo")
            table.add_column("Score", justify="right")
            table.add_column("Hop", justify="right")
            table.add_column("Fonte")
            for i, n in enumerate(notes, 1):
                meta = n.get("metadata") or {}
                table.add_row(
                    str(i),
                    (n.get("title") or n.get("note_id") or "")[:60],
                    f"{float(n.get('score') or 0):.4f}",
                    str(n.get("hop") or 0),
                    str(meta.get("source_id") or "-"),
                )
            console.print(table)
            qs = payload.get("executed_queries") or []
            if qs:
                console.print("[dim]Queries usadas: " + ", ".join(qs) + "[/dim]")
            choice = Prompt.ask(
                "Contexto: [a]aprovar / [e]extras / [q]quit",
                choices=["a", "e", "q"],
                default="a",
            )
            if choice == "a":
                return {"context_decision": "approve", "extra_queries": []}
            if choice == "q":
                return {"context_decision": "abort", "extra_queries": []}
            raw = Prompt.ask(
                "Queries extras (separadas por ; ou linhas)", default=""
            )
            extras = parse_extra_queries(raw)
            if not extras:
                return {"context_decision": "approve", "extra_queries": []}
            return {"context_decision": "enrich", "extra_queries": extras}

        if itype == "outline_review":
            console.print(
                Panel(str(payload.get("preview") or ""), title="Outline proposto")
            )
            choice = Prompt.ask(
                "Outline: [a]provar / [r]egenerar / [q]uit",
                choices=["a", "r", "q"],
                default="a",
            )
            if choice == "a":
                return {"outline_decision": "approve", "outline_feedback": ""}
            if choice == "q":
                return {"outline_decision": "abort", "outline_feedback": ""}
            feedback = Prompt.ask(
                "Feedback para regenerar (opcional)", default=""
            )
            return {
                "outline_decision": "regenerate",
                "outline_feedback": feedback.strip(),
            }
        return {}

    console.print("[dim]Pipeline de artigo (LangGraph)...[/dim]")
    result = run_article_graph(
        cfg, db, idx, topic,
        style=style_norm,  # type: ignore[arg-type]
        topk=topk,
        use_graph=not no_graph,
        mode=mode,
        outline_only=outline_only,
        personality=personality,
        custom_style_notes=style_notes,
        skip_context_review=skip_context_review or outline_only,
        skip_judge=skip_judge or outline_only,
        max_judge_iterations=max_judge_iterations,
        hitl_handler=_hitl,
    )

    if result.no_evidence:
        console.print(Panel(result.body, title="Sem evidencia"))
        db.close()
        raise typer.Exit(0)

    if result.aborted:
        console.print("[yellow]Geracao abortada pelo usuario.[/yellow]")
        db.close()
        raise typer.Exit(0)

    if outline_only:
        console.print(Panel(result.body, title="Outline"))
        db.close()
        raise typer.Exit(0)

    console.print(Panel(result.body.strip() or "(vazio)", title=result.title or "Artigo"))

    for w in result.warnings:
        console.print(f"[yellow]Aviso:[/yellow] {w}")

    if show_context and result.note_ids:
        table = Table(title="Notas usadas no artigo")
        table.add_column("note_id")
        for nid in result.note_ids:
            table.add_row(nid)
        console.print(table)

    saved_path = None
    if save_to:
        saved_path = save_article_note(result, cfg.vault_path, Path(save_to))
    elif save:
        saved_path = save_article_note(result, cfg.vault_path)
    elif not no_save_prompt:
        from rich.prompt import Confirm
        try:
            if Confirm.ask("Salvar este artigo como nota .md?", default=True):
                saved_path = save_article_note(result, cfg.vault_path)
        except (EOFError, KeyboardInterrupt):
            pass

    if saved_path:
        try:
            rel = saved_path.relative_to(cfg.vault_path)
            console.print(f"[green]Artigo salvo em:[/green] {rel}")
        except ValueError:
            console.print(f"[green]Artigo salvo em:[/green] {saved_path}")

    db.close()


# ── status ────────────────────────────────────────────────────────────


@app.command()
def status(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Exibir estatísticas do pipeline."""
    cfg = _load_deps(config)
    db = _get_db(cfg)

    stats = db.get_stats()

    table = Table(title="Zettelkasten — Status")
    table.add_column("Entidade", style="bold")
    table.add_column("Quantidade", justify="right")

    labels = {
        "files": "Arquivos",
        "sources": "Fontes (SRC)",
        "chapters": "Capitulos",
        "chunks": "Chunks (total)",
        "chunks_pending": "Chunks pendentes (extract)",
        "chunks_awaiting_review": "Chunks aguardando review",
        "chunks_approved": "Chunks aprovados",
        "chunks_rejected": "Chunks rejeitados",
        "chunks_failed": "Chunks falhos",
        "concepts": "Conceitos",
        "notes": "Notas Permanentes",
        "mocs": "MOCs",
        "assets": "Assets",
    }
    for key, label in labels.items():
        val = stats.get(key, 0)
        style = ""
        if key in ("chunks_pending", "chunks_awaiting_review", "chunks_failed") and val > 0:
            style = "yellow"
        table.add_row(label, str(val), style=style)

    table.add_row("Conexoes (grafo)", str(db.count_note_connections()))

    console.print(table)

    from zettel.harvester import list_incomplete_sources
    incomplete = list_incomplete_sources(db)
    if incomplete:
        console.print(
            f"\n[yellow]Chunking incompleto em {len(incomplete)} fonte(s): "
            f"{', '.join(incomplete)}. Rode `zettel rechunk` para completar.[/yellow]"
        )

    last_run = db.get_last_run()
    if last_run:
        dup_table = Table(title="Duplicatas — Ultima Execucao do Harvest")
        dup_table.add_column("Tipo", style="bold")
        dup_table.add_column("Quantidade", justify="right")
        dup_table.add_row(
            "Por hash de arquivo (copia renomeada)", str(last_run.get("duplicate_file_count", 0))
        )
        dup_table.add_row(
            "Por conteudo extraido (cross-formato)", str(last_run.get("duplicate_content_count", 0))
        )
        dup_table.add_row(
            "Por similaridade semantica", str(last_run.get("duplicate_semantic_count", 0))
        )
        dup_table.add_row("Status da execução", str(last_run.get("status", "-")))
        console.print(dup_table)

        cost_table = Table(title="Custo — Ultimo Run")
        cost_table.add_column("Metrica", style="bold")
        cost_table.add_column("Valor", justify="right")
        cost_table.add_row("USD total", f"{float(last_run.get('cost_usd_total') or 0):.6f}")
        cost_table.add_row("USD LLM", f"{float(last_run.get('cost_usd_llm') or 0):.6f}")
        cost_table.add_row("USD embeddings", f"{float(last_run.get('cost_usd_embedding') or 0):.6f}")
        cost_table.add_row("Tokens prompt", str(last_run.get("tokens_prompt", 0) or 0))
        cost_table.add_row("Tokens completion", str(last_run.get("tokens_completion", 0) or 0))
        cost_table.add_row("Tokens embedding", str(last_run.get("tokens_embedding", 0) or 0))
        cost_table.add_row("LLM calls", str(last_run.get("llm_calls", 0) or 0))
        cost_table.add_row("Cache hits", str(last_run.get("cache_hits", 0) or 0))
        console.print(cost_table)

    db.close()


# ── doctor ────────────────────────────────────────────────────────────


@app.command()
def doctor(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Verificar configuração, dependências e integridade."""
    cfg = _load_deps(config)

    checks: list[tuple[str, bool, str]] = []

    # Config file
    config_path = Path(config) if config else Path("config/config.yaml")
    checks.append(("Config file", config_path.exists(), str(config_path)))

    # Vault
    checks.append(("Vault path", cfg.vault_path.exists(), str(cfg.vault_path)))

    # Inbox
    checks.append(("Inbox path", cfg.inbox_path.exists(), str(cfg.inbox_path)))

    # Prompts (must match files actually loaded by the pipeline)
    prompt_files = [
        "literature_note.md",
        "permanent_note.md",
        "dedupe_decision.md",
        "moc_generation.md",
        "moc_incremental.md",
        "ptbr_guard.md",
        "image_description.md",
        "ask.md",
        "bibliographic_metadata.md",
        "article_outline.md",
        "article_section_blog.md",
        "article_section_academic.md",
        "article_anti_ai.md",
        "article_query_enrich.md",
        "article_personality.md",
        "article_judge.md",
    ]
    for pf in prompt_files:
        p = cfg.prompts_path / pf
        checks.append((f"Prompt: {pf}", p.exists(), str(p)))

    # FTS5 (BM25 hybrid search) availability in the SQLite build
    db_check = _get_db(cfg)
    try:
        checks.append((
            "SQLite FTS5 (busca hibrida)",
            db_check.fts_enabled,
            "disponivel" if db_check.fts_enabled else "indisponivel (usa vetor puro)",
        ))
    finally:
        db_check.close()

    # Dependencies
    deps = [
        ("typer", "typer"),
        ("rich", "rich"),
        ("pydantic", "pydantic"),
        ("yaml", "pyyaml"),
        ("chromadb", "chromadb"),
        ("langchain_core", "langchain-core"),
        ("langchain_text_splitters", "langchain-text-splitters"),
    ]
    for module, package in deps:
        try:
            __import__(module)
            checks.append((f"Dep: {package}", True, "instalado"))
        except ImportError:
            checks.append((f"Dep: {package}", False, "NÃO instalado"))

    # Optional deps
    opt_deps = [
        ("langchain_openai", "langchain-openai"),
        ("langchain_anthropic", "langchain-anthropic"),
        ("docling", "docling"),
        ("pymupdf", "pymupdf"),
        ("umap", "umap-learn"),
        ("hdbscan", "hdbscan"),
        ("sklearn", "scikit-learn"),
    ]
    for module, package in opt_deps:
        try:
            __import__(module)
            checks.append((f"Opt: {package}", True, "instalado"))
        except ImportError:
            checks.append((f"Opt: {package}", False, "nao instalado (opcional)"))

    # GPU / Device
    from zettel.config import get_gpu_info
    gpu = get_gpu_info()
    checks.append((
        "PyTorch",
        gpu["torch_version"] != "nao instalado",
        f"v{gpu['torch_version']}" if gpu["torch_version"] != "nao instalado" else "nao instalado",
    ))
    if gpu["available"]:
        checks.append(("GPU (CUDA)", True, f"{gpu['device_name']} ({gpu['vram_gb']} GB, CUDA {gpu['cuda_version']})"))
    else:
        checks.append(("GPU (CUDA)", False, "nenhuma GPU detectada (usara CPU)"))

    checks.append(("Device config", True, f"device: {cfg.device}"))

    from zettel.config import LLM_PHASES, llm_phase
    from zettel.llm import is_supported_llm_provider
    for phase in LLM_PHASES:
        spec = llm_phase(cfg, phase)
        ok = is_supported_llm_provider(spec.provider)
        detail = f"{spec.provider} / {spec.model}"
        if not ok:
            detail = f"provider nao suportado: {detail}"
        checks.append((f"LLM {phase}", ok, detail))

    # MOC taxonomy YAML
    topics_path = cfg.gardener.topics_path
    if topics_path is None:
        checks.append(("MOC taxonomy", not cfg.gardener.strict_topics,
                        "topics_path nao configurado"))
    elif topics_path.exists():
        try:
            from zettel.taxonomy import allowed_topic_names, load_moc_taxonomy
            tax = load_moc_taxonomy(topics_path)
            n_cat = len(allowed_topic_names(tax))
            checks.append(("MOC taxonomy", True, f"{topics_path.name} ({n_cat} categorias)"))
        except Exception as e:
            checks.append(("MOC taxonomy", False, f"invalida: {e}"))
    else:
        checks.append(("MOC taxonomy", False, f"arquivo nao encontrado: {topics_path}"))

    # Embedding space: config vs Chroma collection markers
    from zettel.index import peek_stored_embedding_identity
    stored_p, stored_m, stored_d = peek_stored_embedding_identity(cfg.chroma_path)
    cfg_p, cfg_m, cfg_d = (
        cfg.embedding.provider, cfg.embedding.model, cfg.embedding.dimensions,
    )
    cfg_label = _fmt_embedding_id(cfg_p, cfg_m, cfg_d)
    if stored_p is None and stored_m is None and stored_d is None:
        checks.append((
            "Embedding space",
            True,
            f"config={cfg_label} (Chroma sem marcador ou vazio)",
        ))
    elif stored_p == cfg_p and stored_m == cfg_m and stored_d == cfg_d:
        checks.append((
            "Embedding space",
            True,
            cfg_label,
        ))
    else:
        stored_label = _fmt_embedding_id(stored_p, stored_m, stored_d)
        checks.append((
            "Embedding space",
            False,
            f"drift: Chroma={stored_label} -> config={cfg_label}; "
            f"rode `zettel reindex --force`",
        ))

    # Chunking coverage vs extracted_text (interrupted harvest recovery)
    try:
        db = _get_db(cfg)
        from zettel.harvester import list_incomplete_sources
        incomplete = list_incomplete_sources(db)
        db.close()
        if incomplete:
            checks.append((
                "Chunking coverage",
                False,
                f"incompleto em {len(incomplete)} fonte(s); rode `zettel rechunk`",
            ))
        else:
            checks.append(("Chunking coverage", True, "todas as fontes cobertas"))
    except Exception as e:
        checks.append(("Chunking coverage", False, f"erro ao verificar: {e}"))

    # Display
    table = Table(title="Zettelkasten — Doctor")
    table.add_column("Verificação", style="bold")
    table.add_column("Status")
    table.add_column("Detalhe")
    for name, ok, detail in checks:
        status_str = "[green]OK[/green]" if ok else "[red]FALHA[/red]"
        table.add_row(name, status_str, detail)

    console.print(table)

    failed = sum(1 for _, ok, _ in checks if not ok)
    if failed:
        console.print(f"\n[red]{failed} verificação(ões) falharam.[/red]")
    else:
        console.print("\n[green]Tudo em ordem![/green]")


# ── Entry point ───────────────────────────────────────────────────────


def main():
    app()


if __name__ == "__main__":
    # Logar o tempo de execucao
    start_time = time.time()
    main()
    end_time = time.time()
    console.print(f"Tempo de execucao total: {(end_time - start_time)/60} minutos")
