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
    return VectorIndex(cfg.chroma_path, cfg.embedding.provider, cfg.embedding.model, cfg.device)


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
):
    """Escanear inbox, extrair texto, criar notas SRC/LIT e chunks."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

    interactive, duplicate_action = _resolve_duplicate_flags(yes, skip_duplicates, force)

    from zettel.harvester import run_harvest
    if interactive:
        with console.status("[bold blue]Coletando arquivos do inbox...", spinner="dots"):
            new_sources = run_harvest(cfg, db, idx, interactive=True)
    else:
        console.print(f"[dim]Modo nao-interativo — duplicatas suspeitas: '{duplicate_action}'[/dim]")
        new_sources = run_harvest(cfg, db, idx, interactive=False, duplicate_action=duplicate_action)

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

    # Load candidates from cache
    import json
    from zettel.schemas import PermanentNoteCandidate
    cache_file = cfg.cache_path / "candidates.json"
    if not cache_file.exists():
        console.print("[red]Nenhum candidato encontrado. Execute 'extract' primeiro.[/red]")
        db.close()
        raise typer.Exit(1)

    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    candidates = []
    for entry in raw:
        entry["candidate"] = PermanentNoteCandidate(**entry["candidate"])
        candidates.append(entry)

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
):
    """Resetar chunks com falha para 'pending', permitindo re-execução do extract."""
    cfg = _load_deps(config)
    db = _get_db(cfg)

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


# ── sync-manual ───────────────────────────────────────────────────────


@app.command(name="sync-manual")
def sync_manual(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Sincronizar notas manuais do vault com o índice vetorial."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

    from zettel.sync import run_sync_manual
    with console.status("[bold blue]Sincronizando notas manuais...", spinner="dots"):
        stats = run_sync_manual(cfg, db, idx)

    table = Table(title="Sync Manual")
    table.add_column("Métrica", style="bold")
    table.add_column("Valor", justify="right")
    for k, v in stats.items():
        table.add_row(k.capitalize(), str(v))
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
):
    """Executar pipeline completo: harvest > extract > connect > garden."""
    cfg = _load_deps(config)
    db = _get_db(cfg)
    idx = _get_idx(cfg)

    interactive, duplicate_action = _resolve_duplicate_flags(yes, skip_duplicates, force)

    # Phase 1: Harvest
    console.rule("[bold blue]Fase 1 — Harvest")
    from zettel.harvester import run_harvest
    new_sources = run_harvest(cfg, db, idx, interactive=interactive, duplicate_action=duplicate_action)
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

    console.print(table)

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
                    "ptbr_guard.md"]
    for pf in prompt_files:
        p = cfg.prompts_path / pf
        checks.append((f"Prompt: {pf}", p.exists(), str(p)))

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
