"""Read-only inspection: ``status``, ``doctor`` and ``db-report``.

None of these commands write anything, and none call an LLM. They answer three
different questions:

* ``status`` — *what is in the pipeline right now?* Entity counts, the chunks
  waiting at each gate, duplicate detections from the last harvest, and what the
  last pipeline session cost.
* ``doctor`` — *would a run succeed?* Every precondition that fails late and
  expensively is checked here, cheaply and up front.
* ``db-report`` — *how much do the stores occupy, and what is inside them?*
  File sizes, per-table occupancy, heavy columns, and Chroma collections.

Each ``doctor`` check exists because of a specific failure mode:

* **Prompt files** — a missing template raises ``FileNotFoundError`` in the middle
  of a phase, after earlier chunks have already been billed. The list comes from
  ``llm.REQUIRED_PROMPTS`` so it cannot drift from what the pipeline loads.
* **SQLite FTS5** — its absence is silent: hybrid retrieval degrades to
  vector-only and search quality drops with no error anywhere.
* **LLM provider per phase** — an unsupported provider name in ``llm.<phase>`` only
  surfaces when that phase runs, which may be phase four of five.
* **MOC taxonomy** — with ``strict_topics``, an invalid YAML means every new MOC
  topic is rejected and ``garden`` quietly produces nothing.
* **Embedding space** — the config and the Chroma markers disagreeing means
  queries are compared against vectors from a different space: no error, just
  confidently wrong results. This is the check that sends you to ``reindex --force``.
* **Chunking coverage** — a harvest interrupted between extraction and chunking
  leaves a source whose text is persisted but only partly chunked, so ``extract``
  silently processes an incomplete document. ``rechunk`` repairs it.
"""

from __future__ import annotations

from pathlib import Path

from rich.table import Table

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, load_deps
from zettel.cli.formatting import fmt_bytes, fmt_embedding_id, print_cost_by_phase
from zettel.cli.options import ConfigOption


@app.command()
def status(config: ConfigOption = None):
    """Exibir estatísticas do pipeline."""
    cfg = load_deps(config)
    db = get_db(cfg)

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
        # Yellow marks a queue with work in it: something is waiting for a command.
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

    from zettel.usage import latest_pipeline_session, pipeline_phase_name

    # Prefer the harvest of the latest pipeline session; fall back to the most
    # recent harvest of any session, so a vault whose last activity was `ask` or
    # `connect` still shows its duplicate history instead of an empty table.
    recent_runs = db.get_recent_runs()
    session = latest_pipeline_session(recent_runs)
    harvest_run = next(
        (
            row
            for row in reversed(session)
            if pipeline_phase_name(row.get("pipeline_signature")) == "harvest"
        ),
        None,
    )
    if harvest_run is None:
        harvest_run = next(
            (
                row
                for row in recent_runs
                if pipeline_phase_name(row.get("pipeline_signature")) == "harvest"
            ),
            None,
        )
    if harvest_run:
        dup_table = Table(title="Duplicatas — Ultima Execucao do Harvest")
        dup_table.add_column("Tipo", style="bold")
        dup_table.add_column("Quantidade", justify="right")
        dup_table.add_row(
            "Por hash de arquivo (copia renomeada)",
            str(harvest_run.get("duplicate_file_count", 0)),
        )
        dup_table.add_row(
            "Por conteudo extraido (cross-formato)",
            str(harvest_run.get("duplicate_content_count", 0)),
        )
        dup_table.add_row(
            "Por metadados (DOI/ISBN ou titulo+autor)",
            str(harvest_run.get("duplicate_biblio_count", 0)),
        )
        dup_table.add_row(
            "Por similaridade semantica",
            str(harvest_run.get("duplicate_semantic_count", 0)),
        )
        dup_table.add_row("Status da execução", str(harvest_run.get("status", "-")))
        console.print(dup_table)

    print_cost_by_phase(db)

    db.close()


@app.command()
def doctor(config: ConfigOption = None):
    """Verificar configuração, dependências e integridade."""
    cfg = load_deps(config)

    # (name, ok, detail) — rendered as one table at the end so a slow check does
    # not leave the user staring at a half-drawn report.
    checks: list[tuple[str, bool, str]] = []

    # Config file
    config_path = Path(config) if config else Path("config/config.yaml")
    checks.append(("Config file", config_path.exists(), str(config_path)))

    # Vault
    checks.append(("Vault path", cfg.vault_path.exists(), str(cfg.vault_path)))

    # Inbox
    checks.append(("Inbox path", cfg.inbox_path.exists(), str(cfg.inbox_path)))

    # Prompts: the canonical list lives next to the loader (zettel.llm), so this
    # check and tests/test_prompts.py cannot drift apart from what the pipeline
    # actually opens at runtime.
    from zettel.llm import REQUIRED_PROMPTS

    for pf in REQUIRED_PROMPTS:
        p = cfg.prompts_path / pf
        checks.append((f"Prompt: {pf}", p.exists(), str(p)))

    # FTS5 (BM25 hybrid search) availability in the SQLite build
    db_check = get_db(cfg)
    try:
        checks.append(
            (
                "SQLite FTS5 (busca hibrida)",
                db_check.fts_enabled,
                "disponivel" if db_check.fts_enabled else "indisponivel (usa vetor puro)",
            )
        )
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
        ("docling", "docling"),
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
    checks.append(
        (
            "PyTorch",
            gpu["torch_version"] != "nao instalado",
            f"v{gpu['torch_version']}"
            if gpu["torch_version"] != "nao instalado"
            else "nao instalado",
        )
    )
    if gpu["available"]:
        checks.append(
            (
                "GPU (CUDA)",
                True,
                f"{gpu['device_name']} ({gpu['vram_gb']} GB, CUDA {gpu['cuda_version']})",
            )
        )
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

    # MOC taxonomy YAML: only a failure when strict_topics would actually enforce it.
    topics_path = cfg.gardener.topics_path
    if topics_path is None:
        checks.append(
            (
                "MOC taxonomy",
                not cfg.gardener.strict_topics,
                "topics_path nao configurado",
            )
        )
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

    examples_path = cfg.domain.examples_path
    if examples_path is None:
        checks.append(("Domain examples", False, "examples_path nao configurado"))
    elif examples_path.exists():
        try:
            from zettel.domain_examples import load_domain_examples

            loaded = load_domain_examples(examples_path)
            n_lit = sum(1 for v in loaded.literature_note.model_dump().values() if str(v).strip())
            checks.append(
                ("Domain examples", True, f"{examples_path.name} ({n_lit} secoes de extract)")
            )
        except Exception as e:
            checks.append(("Domain examples", False, f"invalida: {e}"))
    else:
        checks.append(("Domain examples", False, f"arquivo nao encontrado: {examples_path}"))

    # Embedding space: config vs Chroma collection markers
    from zettel.index import peek_stored_embedding_identity

    stored_p, stored_m, stored_d = peek_stored_embedding_identity(cfg.chroma_path)
    cfg_p, cfg_m, cfg_d = (
        cfg.embedding.provider,
        cfg.embedding.model,
        cfg.embedding.dimensions,
    )
    cfg_label = fmt_embedding_id(cfg_p, cfg_m, cfg_d)
    if stored_p is None and stored_m is None and stored_d is None:
        checks.append(
            (
                "Embedding space",
                True,
                f"config={cfg_label} (Chroma sem marcador ou vazio)",
            )
        )
    elif stored_p == cfg_p and stored_m == cfg_m and stored_d == cfg_d:
        checks.append(
            (
                "Embedding space",
                True,
                cfg_label,
            )
        )
    else:
        stored_label = fmt_embedding_id(stored_p, stored_m, stored_d)
        checks.append(
            (
                "Embedding space",
                False,
                (
                    f"drift: Chroma={stored_label} -> config={cfg_label}; "
                    f"rode `zettel reindex --force`"
                ),
            )
        )

    # Chunking coverage vs extracted_text (interrupted harvest recovery)
    try:
        db = get_db(cfg)
        from zettel.harvester import list_incomplete_sources

        incomplete = list_incomplete_sources(db)
        db.close()
        if incomplete:
            checks.append(
                (
                    "Chunking coverage",
                    False,
                    f"incompleto em {len(incomplete)} fonte(s); rode `zettel rechunk`",
                )
            )
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


@app.command("db-report")
def db_report(config: ConfigOption = None):
    """Relatorio de tamanho e conteudo das bases (SQLite + Chroma)."""
    cfg = load_deps(config)
    db = get_db(cfg)
    try:
        from zettel.db_report import collect_db_report

        report = collect_db_report(cfg, db)
        _print_db_report(report)
    finally:
        db.close()


def _fmt_opt_bytes(value: int | None) -> str:
    return "-" if value is None else fmt_bytes(value)


def _fmt_opt_int(value: int | None) -> str:
    return "-" if value is None else str(value)


def _print_db_report(report) -> None:
    disk = Table(title="Zettelkasten — Disco")
    disk.add_column("Store", style="bold")
    disk.add_column("Arquivo", justify="right")
    disk.add_column("WAL", justify="right")
    disk.add_column("SHM", justify="right")
    disk.add_column("Total", justify="right")
    disk.add_column("Ocioso (VACUUM)", justify="right")

    state = report.state
    unused = state.pragmas.unused_bytes if state.pragmas else 0
    unused_style = "yellow" if unused else ""
    disk.add_row(
        "state.db",
        fmt_bytes(state.file_bytes),
        fmt_bytes(state.wal_bytes),
        fmt_bytes(state.shm_bytes),
        fmt_bytes(state.total_bytes),
        fmt_bytes(unused),
        style=unused_style,
    )

    chroma = report.chroma
    chroma_sql = chroma.sqlite
    chroma_unused = 0
    if chroma_sql is not None and chroma_sql.pragmas is not None:
        chroma_unused = chroma_sql.pragmas.unused_bytes
    disk.add_row(
        "chroma/",
        fmt_bytes(chroma_sql.file_bytes if chroma_sql else 0),
        fmt_bytes(chroma_sql.wal_bytes if chroma_sql else 0),
        fmt_bytes(chroma_sql.shm_bytes if chroma_sql else 0),
        fmt_bytes(chroma.dir_total_bytes),
        fmt_bytes(chroma_unused),
        style="yellow" if chroma_unused else "",
    )
    console.print(disk)

    _print_sqlite_store("state.db", state, columns=True)
    _print_chroma(chroma)


def _print_sqlite_store(
    title: str,
    store,
    *,
    columns: bool,
    caption: str | None = None,
) -> None:
    if store.pragmas is not None:
        pragma = Table(title=f"{title} — PRAGMA")
        pragma.add_column("Chave", style="bold")
        pragma.add_column("Valor", justify="right")
        pragma.add_row("page_size", str(store.pragmas.page_size))
        pragma.add_row("page_count", str(store.pragmas.page_count))
        pragma.add_row("freelist_count", str(store.pragmas.freelist_count))
        pragma.add_row("journal_mode", store.pragmas.journal_mode)
        pragma.add_row("bytes ociosos", fmt_bytes(store.pragmas.unused_bytes))
        if not store.dbstat_available:
            pragma.add_row("dbstat", "indisponivel (so payload LENGTH)")
        console.print(pragma)

    tables = Table(title=f"{title} — Tabelas")
    if caption:
        tables.caption = caption
    tables.add_column("Nome", style="bold")
    tables.add_column("Tipo")
    tables.add_column("Linhas", justify="right")
    tables.add_column("Disco", justify="right")
    tables.add_column("Payload", justify="right")
    for row in store.tables:
        tables.add_row(
            row.name,
            row.kind,
            _fmt_opt_int(row.rows),
            _fmt_opt_bytes(row.disk_bytes),
            _fmt_opt_bytes(row.payload_bytes),
        )
    console.print(tables)

    if not columns or not store.columns:
        return
    heavy = Table(title=f"{title} — Colunas pesadas")
    heavy.add_column("Tabela", style="bold")
    heavy.add_column("Coluna")
    heavy.add_column("Linhas", justify="right")
    heavy.add_column("Nao-nulas", justify="right")
    heavy.add_column("Total", justify="right")
    heavy.add_column("Media", justify="right")
    heavy.add_column("Max", justify="right")
    for col in store.columns:
        heavy.add_row(
            col.table,
            col.column,
            str(col.rows),
            str(col.rows_nonnull),
            fmt_bytes(col.bytes),
            fmt_bytes(round(col.avg_bytes)),
            fmt_bytes(col.max_bytes),
        )
    console.print(heavy)


def _print_chroma(chroma) -> None:
    if not chroma.exists:
        console.print(f"[yellow]Chroma ausente: {chroma.path}[/yellow]")
        return

    if chroma.client_error:
        console.print(
            f"[yellow]Chroma aberto so no disco "
            f"(cliente indisponivel: {chroma.client_error})[/yellow]"
        )
    else:
        identity = fmt_embedding_id(
            chroma.embedding_provider,
            chroma.embedding_model,
            chroma.embedding_dimensions,
        )
        console.print(f"[dim]Embedding persistido: {identity}[/dim]")

    collections = Table(title="Chroma — Colecoes")
    collections.add_column("Colecao", style="bold")
    collections.add_column("Vetores", justify="right")
    for col in chroma.collections:
        if col.error:
            collections.add_row(col.name, f"erro: {col.error}")
        else:
            collections.add_row(col.name, _fmt_opt_int(col.count))
    if chroma.collections:
        console.print(collections)

    if chroma.segments:
        segments = Table(title="Chroma — Segmentos HNSW")
        segments.add_column("Diretorio", style="bold")
        segments.add_column("Arquivos", justify="right")
        segments.add_column("Tamanho", justify="right")
        for seg in chroma.segments:
            segments.add_row(seg.name, str(seg.files), fmt_bytes(seg.bytes))
        segments.add_row("Total HNSW", "", fmt_bytes(chroma.hnsw_bytes), style="bold")
        console.print(segments)

    if chroma.sqlite is not None:
        _print_sqlite_store(
            "chroma.sqlite3",
            chroma.sqlite,
            columns=False,
            caption="Metadados internos do Chroma, nao o schema do pipeline.",
        )
