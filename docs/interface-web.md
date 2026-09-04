# Interface web

[← Voltar ao README](../README.md)

A UI FastAPI é server-rendered (Jinja2) e não exige Node, bundler ou acesso direto do navegador ao SQLite/ChromaDB. Não existe subcomando `zettel web`: é um app separado.

Módulos: [`zettel/web/`](../zettel/web/) (rotas, auth, templates; ADR-039), [`web_app.py`](../zettel/web_app.py) (fila de jobs e dispatch), [`progress.py`](../zettel/progress.py) (progresso compartilhado com a CLI), [`markdown.py`](../zettel/markdown.py) (render seguro de Markdown). Decisões: [ADR-022](adrs/generated/WEB/ADR-022-fastapi-server-rendered-jinja2.md), [ADR-023](adrs/generated/WEB/ADR-023-sqlite-backed-job-queue-single-worker.md), [ADR-039](adrs/generated/WEB/ADR-039-web-as-python-package.md), [ADR-040](adrs/generated/WEB/ADR-040-json-pickers-progressive-enhancement.md).

---

## Subir o servidor

```bash
uvicorn zettel.web:app --host 0.0.0.0 --port 5000

# Em hospedagens que injetam a porta (Replit e afins):
uvicorn zettel.web:app --host 0.0.0.0 --port "${PORT:-5000}"
```

Antes disso, defina o **segredo de instância** no ambiente do processo (Replit Secrets, `.env` ou variável de ambiente — **não** vai no `config.yaml`):

```env
SESSION_SECRET=...
```

Sem `SESSION_SECRET`, nenhuma sessão é emitida e o login não funciona. Para apontar a um YAML alternativo, use `ZETTEL_CONFIG=/caminho/config.yaml`.

Testes:

```bash
uv run pytest tests/test_web.py tests/test_web_state.py tests/test_web_package.py -v
```

---

## Autenticação

- O login compara o segredo informado com o `SESSION_SECRET` da instância usando `hmac.compare_digest`.
- A sessão é um cookie `zettel_session` assinado por HMAC.
- Todos os POSTs exigem token CSRF.
- A página de configuração/saúde nunca exibe segredos.

---

## Páginas

| Página | O que oferece |
|---|---|
| **Visão geral** (`/`) | KPIs, funil, confiança, custos, runs, duplicatas e qualidade do grafo |
| **Documentos** (`/documents`) | Upload, harvest de um arquivo, opções de bibliografia/paginação e dumps seguros de chunks/Markdown extraído, além do pipeline completo |
| **Pipeline** (`/pipeline`) | `extract`, `connect`, garden taxonômico, garden por hubs, sincronização manual e repetição segura de chunks/assets com falha |
| **Revisão** (`/review`) | Filtros por fonte/confiança, trecho, candidatos e aprovação/rejeição **em lote** (sem auto-approve por limiar — use a CLI para `--yes` / bandas interativas) |
| **Notas / MOCs** (`/notes`, `/notes/{id}`, `/mocs/{id}`, `/sources/{id}`) | Listagem read-only e páginas de detalhe de notas permanentes, MOCs e fontes |
| **Criar notas** (`/notes/new`) | Scaffolds manuais SRC, LIT (índice ou granular) e ZTL; busca de fonte/LIT com combobox (a partir de 3 letras; fallback `<select>`); SRC monta a referência ABNT no form (`POST /notes/new/biblio-preview`, sem job) para revisão antes de criar; LIT granular aceita trecho, resumo, conceitos e candidato no próprio form; ZTL a partir de LIT enfileira `manual-ztl-from-lit` com ou sem LLM |
| **Execuções** (`/runs`, `/jobs/{id}`) | Estado persistente, progresso (polling em `/api/jobs/{id}`), eventos, resultado e erro sanitizado |
| **Configuração / saúde** (`/settings`) | FTS5, diretórios, identidade LLM/embedding (incluindo drift de `dimensions`) — sem segredos |

### Operações enfileiráveis

`harvest` (um arquivo do inbox, com dumps opcionais), `manual-ztl-from-lit`, `run_all`, `extract`, `review`, `connect`, `garden`, `garden` + hubs, `sync`, `retry_chunks`, `retry_assets`.

Antes de enfileirar, a rota valida pré-condições e responde **409** com uma mensagem legível — por exemplo, `extract` sem chunks pendentes, `connect` sem candidatos aprovados, `garden` sem notas permanentes, ou provedor de LLM sem credencial configurada.

### Exclusivo da CLI

Operações destrutivas e interativas **não** são expostas na web:

- `init --reset`, `delete-source`, `purge-rejected`, `reindex`, `rebuild`, `garden --recreate`
- criação de MOC, `ask`, `article`, `skill`
- resolução interativa de duplicatas semânticas e o HITL de paginação
- `set-paging`, `rechunk`, execução isolada de `dump-chunks`/`dump-extraction`, `doctor`, `status`

A CLI permanece compatível e continua usando a apresentação Rich normalmente.

---

## Persistência, concorrência e recuperação

- A implantação é de **instância única** e executa no máximo um trabalho mutante por vez (`queued`/`running`); um segundo submit recebe **409**. Não use múltiplos processos/workers Uvicorn.
- A fila vive no SQLite (`web_jobs`, `web_job_events` em [`state.py`](../zettel/state.py)) e é servida por uma thread daemon.
- Preserve `data/` e `vault/` em armazenamento persistente. `data/state.db` contém a fila e os eventos; `data/chroma/` contém vetores; `vault/` contém as notas.
- Recarregar ou fechar a página não interrompe o trabalho. Ao reiniciar o servidor, jobs que estavam `running` viram `interrupted`; jobs ainda `queued` são retomados.
- Chamadas LLM/PDF em curso não são canceladas à força. A recuperação ocorre entre checkpoints seguros, executando novamente a fase quando necessário.

> **Nota de implementação**: a web e a CLI abrem o `VectorIndex` pela mesma função, `index.index_kwargs(cfg)`. Antes havia uma cópia em cada lado e elas divergiram — a do `web_app.py` omitia `embedding.dimensions`, de modo que os dois caminhos gravavam vetores de larguras diferentes no mesmo Chroma. Não crie uma nova cópia.

A assimetria deliberada de validação entre web e CLI está documentada em [ADR-018](adrs/generated/REVIEW/ADR-018-web-cli-validation-asymmetry.md).

---

## Ver também

- [Instalação](instalacao.md) — variáveis de ambiente
- [Comandos](cli.md) — o equivalente de cada operação na CLI
- [Operação](operacao.md) — backup e reconstrução dos dados que a web usa
