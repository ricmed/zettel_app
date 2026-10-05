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
| **Revisão** (`/review`) | Duas filas. Drafts: filtros por fonte/confiança, trecho, candidatos e aprovação/rejeição **em lote**. Rejeitados pelo extract (`?queue=rejected`): categoria, motivo e trecho **completos** (o chunk nunca virou LIT). O lote reenfileira para uma segunda opinião, marca extração obrigatória (`force_extract`: o próximo extract gera o draft sem a trava de rejeição) ou apaga os selecionados do banco (SQLite e índice de chunks). Um draft ou uma rejeição do revisor não entra nessa ação. Sem auto-approve por limiar — use a CLI para `--yes`, faixas e a leitura um a um (`e`/`f`/`m`/`p`) |
| **Notas / MOCs** (`/notes`, `/notes/{id}`, `/mocs/{id}`, `/sources/{id}`) | Busca por título/corpo das ZTLs e MOCs indexados, filtros de tipo, origem, fonte e autor, ordenação, paginação e páginas de detalhe; botões para copiar ou baixar o Markdown original do vault |
| **Criar notas** (`/notes/new`) | Scaffolds manuais SRC, LIT (índice ou granular) e ZTL; busca de fonte/LIT com combobox (a partir de 3 letras; fallback `<select>`); SRC monta a referência ABNT no form (`POST /notes/new/biblio-preview`, sem job) para revisão antes de criar; LIT granular aceita trecho, resumo, conceitos e candidato no próprio form; ZTL a partir de LIT enfileira `manual-ztl-from-lit` com ou sem LLM |
| **Execuções** (`/runs`, `/jobs/{id}`) | Estado persistente, progresso (polling em `/api/jobs/{id}`), eventos, resultado e erro sanitizado |
| **Configuração / saúde** (`/settings`) | FTS5, diretórios, identidade LLM/embedding (incluindo drift de `dimensions`) — sem segredos |

Os filtros de **fonte e autor** usam a relação da ZTL com a SRC indexada; MOCs não têm fonte ou autor direto, portanto não aparecem quando esses filtros estão ativos. A busca usa o índice SQLite: após editar ou criar notas manualmente no vault, execute **Sync manual** no Pipeline para atualizar os resultados. Nas listagens e páginas de detalhe, **Copiar** e **Baixar .md** leem o arquivo atual do vault (com frontmatter), não a cópia do corpo no banco. Se o arquivo tiver sido removido ou estiver fora do vault, a exportação retorna erro em vez de entregar uma cópia desatualizada.

Na aba **Documentos**, envie um PDF, Markdown ou TXT (máximo de 25 MB, sem sobrescrever) e selecione explicitamente um arquivo pendente. As opções por arquivo ficam visíveis mesmo com o inbox vazio, mas só são habilitadas após a seleção. Duplicatas suspeitas são **puladas** por padrão (ou podem continuar/abortar); permitir bibliografia incompleta é opcional. Para PDF, a paginação padrão tenta detectar o início do conteúdo e usa a página 1 se não houver detecção confiável. Alternativamente, informe a página inicial **do arquivo** e a página **impressa** correspondente (páginas anteriores ficam fora dos chunks), ou escolha começar pela página 1 sem detecção. Markdown/TXT não têm paginação de PDF. Os diagnósticos opcionais de chunks e Markdown extraído vão para o cache. Combinações incompatíveis são rejeitadas antes de enfileirar; sem seleção não há harvest do inbox inteiro nesta ação. Ao selecionar **Preparar e revisar bibliografia**, a extração e inferência rodam em uma execução curta; ao terminar, use **Continuar** nessa execução (ou encontre-a em **Execuções**) para revisar tipo, campos e referência ABNT. A confirmação inicia o harvest do arquivo; cancelar não cria SRC. A preparação permanece disponível após recarregar a página, mas se o arquivo ou a configuração de extração mudar é preciso prepará-lo novamente. Uma execução interrompida é retomada ao reiniciar o aplicativo; após uma falha, a revisão pode ser aberta para corrigir e tentar novamente sem repetir a inferência. Se uma fonte já existente tiver bibliografia diferente, a operação falha explicitamente em vez de descartar as correções. Com **Permitir bibliografia incompleta**, marque também a confirmação explícita na tela de revisão se restarem campos obrigatórios vazios. **Executar pipeline completo** é uma operação independente e não herda as escolhas deste formulário.

### Operações enfileiráveis

`prepare_harvest` (extrai e infere um arquivo para revisão), `harvest` (um arquivo confirmado do inbox, com dumps opcionais), `manual-ztl-from-lit`, `run_all`, `extract`, `review`, `connect`, `garden`, `garden` + hubs, `sync`, `retry_chunks`, `retry_assets`.

Antes de enfileirar, a rota valida pré-condições e responde **409** com uma mensagem legível — por exemplo, `extract` sem chunks pendentes, `connect` sem candidatos aprovados, `garden` sem notas permanentes, ou provedor de LLM sem credencial configurada.

### Exclusivo da CLI

Operações destrutivas e interativas **não** são expostas na web:

- `init --reset`, `delete-source`, `purge-rejected`, `reindex`, `rebuild`, `garden --recreate`
- criação de MOC, `ask`, `article`, `skill`, `suggest-links`
- resolução interativa de duplicatas semânticas e o HITL de paginação
- `set-paging`, `rechunk`, execução isolada de `dump-chunks`/`dump-extraction`, `doctor`, `status`

A CLI permanece compatível e continua usando a apresentação Rich normalmente.

---

## Persistência, concorrência e recuperação

- A implantação é de **instância única** e executa no máximo um trabalho mutante por vez (`queued`/`running`); um segundo submit recebe **409**. Não use múltiplos processos/workers Uvicorn.
- A fila vive no SQLite (`web_jobs`, `web_job_events` em [`state/web.py`](../zettel/state/web.py)) e é servida por uma thread daemon.
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
