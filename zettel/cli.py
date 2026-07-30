"""CLI — Typer + Rich interface for the Zettelkasten pipeline.

Commands:
  init        — Initialize vault, database, and vector store
  harvest     — Scan inbox, extract text, create SRC/LIT, chunk
  extract     — Process chunks with LLM (Prompt 1), generate candidates
  connect     — Generate permanent notes from candidates (Prompt 2)
  garden      — Cluster notes and generate/update MOCs
  sync-manual — Sync manual notes from vault to index
  run-all     — Execute harvest → extract → connect → garden
  status      — Show pipeline statistics
  doctor      — Validate configuration and dependencies
"""

from __future__ import annotations

import logging
import sys
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


def _get_idx(cfg):
    from zettel.index import VectorIndex
    return VectorIndex(
        cfg.chroma_path, cfg.embedding.provider, cfg.embedding.model, cfg.device,
        allow_fallback=cfg.embedding.allow_fallback,
    )


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
    db.close()

    idx = _get_idx(cfg)

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
        help="Modo nao-interativo: usa o comportamento padrao configurado para duplicatas suspeitas",
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
    """Escanear inbox, extrair texto, criar notas SRC/LIT e chunks."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

    interactive, duplicate_action = _resolve_duplicate_flags(yes, skip_duplicates, force)

    from zettel.harvester import run_harvest
    if interactive:
        # Nao usar console.status aqui: prompts interativos (bibliografia / duplicatas)
        # precisam do terminal livre; o spinner engole o Prompt.ask e parece travado.
        console.print(
            "[dim]Coletando arquivos do inbox "
            "(pode solicitar metadados bibliograficos)...[/dim]"
        )
        new_sources = run_harvest(cfg, db, idx, interactive=True, skip_biblio=skip_biblio)
    else:
        console.print(f"[dim]Modo nao-interativo — duplicatas suspeitas: '{duplicate_action}'[/dim]")
        if skip_biblio:
            console.print("[dim]Bibliografia incompleta permitida (--skip-biblio)[/dim]")
        new_sources = run_harvest(
            cfg, db, idx, interactive=False, duplicate_action=duplicate_action,
            skip_biblio=skip_biblio,
        )

    if new_sources:
        console.print(f"[green]Fontes processadas: {len(new_sources)}[/green]")
        for sid in new_sources:
            console.print(f"  - {sid}")
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


# ── extract ───────────────────────────────────────────────────────────


@app.command()
def extract(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Processar chunks pendentes com LLM (Prompt 1), gerar candidatos."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

    from zettel.extractor import run_extract
    with console.status("[bold blue]Extraindo conceitos dos chunks...", spinner="dots"):
        candidates = run_extract(cfg, db, idx)

    console.print(f"[green]Candidatos extraídos: {len(candidates)}[/green]")

    # Store candidates in cache for the connect phase
    import json
    cache_file = cfg.cache_path / "candidates.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    # Serialize candidates
    serializable = []
    for c in candidates:
        entry = {
            "concept_id": c["concept_id"],
            "source_id": c["source_id"],
            "chunk_id": c["chunk_id"],
            "candidate": c["candidate"].model_dump(),
        }
        if "refines_note_id" in c:
            entry["refines_note_id"] = c["refines_note_id"]
        if "refine_reason" in c:
            entry["refine_reason"] = c["refine_reason"]
        serializable.append(entry)
    cache_file.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[dim]Candidatos salvos em: {cache_file}[/dim]")

    db.close()


# ── connect ───────────────────────────────────────────────────────────


@app.command()
def connect(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    topk: Optional[int] = typer.Option(None, "--topk", help="Top-k notas similares"),
    dedupe_threshold: Optional[float] = typer.Option(None, "--dedupe-threshold"),
):
    """Gerar notas permanentes a partir dos candidatos extraídos."""
    cfg = _load_deps(config)
    if topk:
        cfg.linking.topk = topk
    if dedupe_threshold:
        cfg.linking.dedupe_threshold = dedupe_threshold

    db = _get_db(cfg)
    idx = _get_idx(cfg)

    # Load candidates: prefer candidates.json (debug artifact); fall back to the DB
    # (concepts approved but not yet noted), which is the durable source of truth.
    import json
    from zettel.schemas import PermanentNoteCandidate
    cache_file = cfg.cache_path / "candidates.json"
    candidates = []
    if cache_file.exists():
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        for entry in raw:
            entry["candidate"] = PermanentNoteCandidate(**entry["candidate"])
            candidates.append(entry)
    else:
        console.print("[dim]candidates.json ausente — carregando candidatos aprovados do banco.[/dim]")
        for concept in db.get_concepts_by_status("approved", without_notes=True):
            if not concept.get("candidate_json"):
                continue
            candidates.append({
                "concept_id": concept["concept_id"],
                "source_id": concept["source_id"],
                "chunk_id": concept["chunk_id"],
                "candidate": PermanentNoteCandidate.model_validate_json(concept["candidate_json"]),
            })

    if not candidates:
        console.print("[red]Nenhum candidato encontrado. Execute 'extract' primeiro.[/red]")
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
):
    """Clusterizar notas e gerar/atualizar MOCs."""
    cfg = _load_deps(config)
    if min_cluster_size:
        cfg.gardener.min_cluster_size = min_cluster_size

    db = _get_db(cfg)
    idx = _get_idx(cfg)

    from zettel.gardener import run_garden
    with console.status("[bold blue]Cultivando o jardim de notas...", spinner="dots"):
        moc_ids = run_garden(cfg, db, idx)

    if moc_ids:
        console.print(f"[green]MOCs gerados/atualizados: {len(moc_ids)}[/green]")
        for mid in moc_ids:
            console.print(f"  - {mid}")
    else:
        console.print("[yellow]Nenhum MOC gerado (notas insuficientes ou já atualizados).[/yellow]")

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
):
    """Re-chunkar fontes a partir do texto extraido persistido (aplica config atual)."""
    if not source_id and not all_sources:
        console.print("[red]Informe --source-id <id> ou --all.[/red]")
        raise typer.Exit(1)

    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

    from zettel.harvester import run_rechunk
    with console.status("[bold blue]Re-chunkando fontes...", spinner="dots"):
        stats = run_rechunk(cfg, db, idx, source_id if source_id else None)

    console.print(
        f"[green]Rechunk concluido:[/green] {stats['sources']} fonte(s), "
        f"{stats['chunks']} chunk(s), {stats['skipped']} pulada(s)."
    )
    if stats["skipped"]:
        console.print(
            "[yellow]Fontes puladas nao tem texto extraido persistido (anteriores a Fase 0). "
            "Reprocesse o arquivo original via harvest.[/yellow]"
        )
    db.close()


# ── sync-manual ───────────────────────────────────────────────────────


@app.command(name="sync-manual")
def sync_manual(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    rebuild_graph: bool = typer.Option(
        False, "--rebuild-graph",
        help="Re-deriva arestas 'related' dos wikilinks no corpo de todas as notas",
    ),
):
    """Sincronizar notas manuais do vault com o índice vetorial."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

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
):
    """Reconstruir o ChromaDB a partir do SQLite (sem chamadas de LLM)."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

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
        idx = _get_idx(cfg)
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
        help="Modo nao-interativo: usa o comportamento padrao configurado para duplicatas suspeitas",
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
    """Executar pipeline completo: harvest > extract > connect > garden."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

    interactive, duplicate_action = _resolve_duplicate_flags(yes, skip_duplicates, force)

    # Phase 1: Harvest
    console.rule("[bold blue]Fase 1 — Harvest")
    from zettel.harvester import run_harvest
    new_sources = run_harvest(
        cfg, db, idx, interactive=interactive, duplicate_action=duplicate_action,
        skip_biblio=skip_biblio,
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
    candidates = run_extract(cfg, db, idx)
    console.print(f"  Candidatos: {len(candidates)}")

    if dry_run:
        console.print("[yellow]Dry run — parando antes da geração de notas.[/yellow]")
        db.close()
        return

    # Phase 3: Connect
    console.rule("[bold blue]Fase 3 — Connect")
    from zettel.connector import run_connect
    note_ids = run_connect(cfg, db, idx, candidates)
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
):
    """Responder uma pergunta usando as notas do vault (recuperacao hibrida + grafo)."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

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
        "chapters": "Capítulos",
        "chunks": "Chunks (total)",
        "chunks_pending": "Chunks pendentes",
        "concepts": "Conceitos",
        "notes": "Notas Permanentes",
        "mocs": "MOCs",
    }
    for key, label in labels.items():
        val = stats.get(key, 0)
        style = "red" if key == "chunks_pending" and val > 0 else ""
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
        dup_table = Table(title="Duplicatas — Última Execução do Harvest")
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

    # Prompts
    prompt_files = ["literature_note.md", "permanent_note.md", "dedupe_decision.md",
                    "relationship.md", "moc_generation.md", "moc_incremental.md",
                    "ptbr_guard.md", "image_description.md", "ask.md"]
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
    main()
