# Referência de comandos (CLI)

[← Voltar ao README](../README.md)

Todos os comandos do `zettel`, com as flags reais do pacote [`zettel/cli/`](../zettel/cli/) e o que cada uma faz. A CLI é construída com Typer + Rich ([ADR-026](adrs/generated/CLI/ADR-026-typer-rich-cli-framework.md)) e está organizada em módulos por fase do pipeline ([ADR-032](adrs/generated/CLI/ADR-032-cli-as-python-package.md)).

Os exemplos usam `python -m zettel ...`; prefixe com `uv run` se o ambiente não estiver ativado.

---

## Fluxo básico

```bash
# 1. Coloque arquivos PDF ou Markdown em data/inbox/
cp meu_artigo.pdf data/inbox/

# 2. Execute o pipeline completo
python -m zettel run-all

# 3. Abra o vault no Obsidian
#    Aponte para a pasta ./vault
```

O `run-all` encadeia `harvest → extract → review → connect → garden`. Em uso diário é comum rodar fase a fase, porque o `review` é um portão humano.

---

## Índice

| Comando | O que faz |
|---|---|
| [`init`](#init) | Cria/recria a estrutura do vault e das bases |
| [`harvest`](#harvest) | Escaneia o inbox, extrai texto, cria SRC + índice LIT + chunks |
| [`set-paging`](#set-paging) | Corrige a paginação de uma fonte já harvestada |
| [`rechunk`](#rechunk) | Re-chunka a partir do texto já extraído |
| [`extract`](#extract) | Prompt 1: gera drafts de LIT granular |
| [`review`](#review) | Aprova/rejeita os drafts (portão humano) |
| [`purge-rejected`](#purge-rejected) | Apaga definitivamente os chunks rejeitados |
| [`connect`](#connect) | Prompt 2: gera notas permanentes (ZTL) |
| [`garden`](#garden) | Clusteriza notas e gera/atualiza MOCs |
| [`ask`](#ask) | QA sobre o vault com recuperação híbrida |
| [`article`](#article) | Artigo longo a partir do vault (LangGraph) |
| [`skill`](#skill) | Exporta um recorte aprovado como Agent Skill plana |
| [`new-note`](#new-note) | Scaffold de notas manuais |
| [`sync-manual`](#sync-manual) | Adota notas escritas à mão no Obsidian |
| [`delete-source`](#delete-source) | Remove uma fonte por completo (irreversível) |
| [`dump-chunks`](#dump-chunks) | Exporta os chunks persistidos como markdown |
| [`dump-extraction`](#dump-extraction) | Exporta o Markdown extraído |
| [`reindex`](#reindex) | Reconstrói o ChromaDB a partir do SQLite |
| [`rebuild`](#rebuild) | Reconstrói o vault (`.md`) e/ou o Chroma |
| [`retry-failed`](#retry-failed) | Reprocessa chunks/imagens com falha |
| [`status`](#status) | Estatísticas do pipeline |
| [`doctor`](#doctor) | Diagnóstico de configuração e dependências |
| [`run-all`](#run-all) | Pipeline completo |

Todos aceitam `--config` / `-c` para apontar um YAML alternativo.

---

## `init`

```bash
# Recria o vault vazio (apaga ./vault). State DB, Chroma e cache permanecem
python -m zettel init

# Alem do vault, apaga State DB, ChromaDB e cache (pede confirmacao)
python -m zettel init --reset

# Overrides de caminho sem editar o YAML
python -m zettel init --vault /outro/vault --inbox /outro/inbox
```

| Flag | Efeito |
|---|---|
| `--reset` | Apaga **e** recria State DB, ChromaDB e cache. Pede confirmação. |
| `--vault` | Override do `vault_path` desta execução. |
| `--inbox` | Override do `inbox_path` desta execução. |

---

## `harvest`

Escaneia `data/inbox/`, extrai o texto (Docling para PDF, parser nativo para Markdown), infere metadados bibliográficos ABNT, resolve a paginação, cria a **SRC** e o **índice LIT**, e grava os chunks. Detalhes em [pipeline.md](pipeline.md#fase-1--harvest-coleta).

```bash
python -m zettel harvest
python -m zettel harvest --yes --skip-biblio
# --yes aplica a heuristica de paginacao (sem prompt): cap. 1, cabecalho/rodape, ou pages: 200-210
python -m zettel harvest --yes --skip-biblio --skip-paging
# --skip-paging forca arquivo p.1 = impressa p.1 (nao detecta miolo/revista)
python -m zettel harvest --content-start-file 35 --content-start-book 10
# livro: arquivo p.35 = impressa p.10; paginas anteriores nao geram chunks
python -m zettel harvest --content-start-file 1 --content-start-book 200
# artigo de revista: PDF comeca em p.1, numero impresso na revista e 200
python -m zettel harvest --skip-duplicates          # nunca reprocessa suspeitos
python -m zettel harvest --force                    # sempre trata suspeito como fonte nova
python -m zettel harvest --dump-chunks
python -m zettel harvest --dump-chunks --dump-dir ./tmp/chunks
python -m zettel harvest --dump-extraction
python -m zettel harvest --dump-extraction --dump-extraction-dir ./tmp/extraction
```

| Flag | Efeito |
|---|---|
| `--yes` / `-y` | Não-interativo: usa `harvest.non_interactive_duplicate_action` da config para duplicatas, aplica a heurística de paginação em silêncio e confirma reprocessamento se o embedding mudou. |
| `--skip-duplicates` | Não-interativo: **sempre pula** arquivos com suspeita de duplicidade. |
| `--force` | Não-interativo: **sempre trata** o arquivo suspeito como fonte nova. Mutuamente exclusivo com `--skip-duplicates`. |
| `--skip-biblio` | Segue com metadados bibliográficos incompletos (com aviso) em vez de pular o arquivo. |
| `--content-start-file N` | Página do **arquivo PDF** (1-based) onde o conteúdo começa. Ganha de qualquer heurística. |
| `--content-start-book M` | Número **impresso** nessa primeira página de conteúdo (default 1). |
| `--skip-paging` | Não detecta paginação: arquivo p.1 = impressa p.1. |
| `--dump-chunks` | Grava um markdown por fonte com os chunks persistidos (texto + paginação + `section_path` + overlap). |
| `--dump-dir DIR` | Diretório do dump de chunks (implica `--dump-chunks`; default `data/cache/chunk-dumps/`). |
| `--dump-extraction` | Grava o Markdown extraído assim que ele é persistido (antes dos embeddings). |
| `--dump-extraction-dir DIR` | Diretório do dump de extração (implica `--dump-extraction`; default `data/cache/extraction-dumps/`). |

---

## `set-paging`

Corrige a paginação de uma fonte **já harvestada**, recalculando o número impresso sem re-chamar o LLM. Renomeia as LIT granulares quando o token `pNNN` muda.

```bash
python -m zettel set-paging --source-id @Citekey --content-start-file 35 --content-start-book 10
python -m zettel set-paging --source-id @Citekey --content-start-file 1 --content-start-book 200 --drop-before-start
```

| Flag | Efeito |
|---|---|
| `--source-id` (obrigatória) | Fonte a corrigir (ex.: `@Citekey`). |
| `--content-start-file` (obrigatória) | Página do arquivo onde o conteúdo começa. |
| `--content-start-book` | Número impresso nessa página (default `1`). |
| `--drop-before-start` | Também remove chunks `awaiting_review`/aprovados anteriores ao início (por padrão só os `pending` são descartados). |
| `--yes` / `-y` | Confirma reprocessamento de embedding, se necessário. |

---

## `rechunk`

Re-aplica a configuração de chunking atual a partir do **texto extraído persistido**, sem reprocessar o arquivo original. Também completa harvest interrompido e re-resolve o `chapter_id` das imagens.

```bash
python -m zettel rechunk --all
python -m zettel rechunk --source-id @AutorAnoTitulo
python -m zettel rechunk --source-id @AutorAnoTitulo --dump-chunks
python -m zettel rechunk --all --dump-chunks --dump-dir ./tmp/chunks
```

Exige `--source-id` **ou** `--all`. Fontes sem texto extraído persistido são puladas (com aviso) — nesse caso é preciso reprocessar o arquivo original via `harvest`.

---

## `extract`

Processa cada chunk `pending` com o **Prompt 1** e escreve um draft de LIT granular em `00_Inbox/Review/{Citekey}/`. Também descreve as imagens pendentes com o modelo de visão.

```bash
python -m zettel extract
python -m zettel extract --auto-approve   # aprova drafts com confianca >= limiar
```

| Flag | Efeito |
|---|---|
| `--auto-approve` | Promove automaticamente drafts com `review_confidence >= literature_review.auto_approve_min_confidence`. |
| `--yes` / `-y` | Confirma reprocessamento se o embedding mudou. |

---

## `review`

Portão humano de aprovação seletiva. Obrigatório antes do `connect`, salvo auto-approve.

```bash
python -m zettel review
# Interativo: relatorio por faixa de confianca; a=aprovar >= limiar,
# d=reprovar (t=todos / b=baixissima / m=media / h=alta, com confirmacao),
# r=um a um (atalhos a/r/p/q), q=sair
# Limiar = literature_review.auto_approve_min_confidence (heuristica, ADR-017)
python -m zettel review --yes                      # aprova todos >= limiar (nao-interativo)
python -m zettel review --auto-approve             # idem, mantendo o restante pendente
python -m zettel review --low-confidence-only      # lista so os drafts abaixo do limiar
python -m zettel review --source-id @Citekey       # revisa apenas uma fonte
```

| Flag | Efeito |
|---|---|
| `--source-id` | Filtra os drafts por fonte. |
| `--yes` / `-y` | Não-interativo: aprova todos com confiança ≥ limiar. |
| `--auto-approve` | Aprova automaticamente os drafts ≥ limiar (também desliga o modo interativo). |
| `--low-confidence-only` | Lista apenas os drafts abaixo do limiar. |

Fluxo detalhado em [pipeline.md](pipeline.md#fase-2b--review-aprovacao-seletiva).

---

## `purge-rejected`

```bash
python -m zettel purge-rejected                # apaga rejected + VACUUM state.db/chroma.sqlite3
python -m zettel purge-rejected --yes          # sem confirmacao
python -m zettel purge-rejected --no-compact   # so apaga, sem compactar disco
python -m zettel purge-rejected --source-id @Citekey
```

Remove permanentemente os chunks `rejected` (SQLite `chunks`/`concepts`/FTS, Chroma `chunks` e `literature_notes` se houver). Não afeta notas permanentes nem LITs aprovadas. Veja [operacao.md](operacao.md#purge-rejected).

---

## `connect`

Gera as notas permanentes (ZTL) a partir dos conceitos aprovados no review.

```bash
python -m zettel connect
python -m zettel connect --topk 10               # top-k de notas similares no RAG
python -m zettel connect --dedupe-threshold 0.90 # limiar de deduplicacao
```

Se não houver candidato aprovado, o comando falha pedindo `extract` + `review` primeiro.

---

## `garden`

```bash
# Clusterizar notas e gerar MOCs (pipeline taxonomico)
python -m zettel garden

# MOCs hub (porta de entrada tematica; complementar ao pipeline taxonomico)
python -m zettel garden --hubs

# Apagar os MOCs do pipeline taxonomico (no vault, no banco e no indice) e regenerar
python -m zettel garden --recreate

# Idem, apenas para os MOCs hub
python -m zettel garden --hubs --recreate -y

# Sem prompt de confirmacao (util em scripts)
python -m zettel garden --recreate -y

# Ajustar o tamanho minimo de cluster nesta execucao
python -m zettel garden --min-cluster-size 3
```

| Flag | Efeito |
|---|---|
| `--hubs` | Executa o pipeline de MOCs ancorados em notas-hub em vez do taxonômico. |
| `--recreate` | Apaga os MOCs **gerados pelo pipeline** (vault + banco + índice) e regenera do zero. MOCs manuais são preservados; `--hubs --recreate` afeta só `origin='hub_pipeline'`. |
| `--min-cluster-size N` | Override de `gardener.min_cluster_size` nesta execução. |
| `--yes` / `-y` | Confirma o `--recreate` e o reprocessamento de embedding. |

---

## `ask`

```bash
python -m zettel ask "O que e RAG?"
python -m zettel ask "O que e RAG?" --show-context        # mostra as notas recuperadas
python -m zettel ask "O que e RAG?" --no-graph            # so busca hibrida, sem grafo
python -m zettel ask "O que e RAG?" --mode vector         # so busca vetorial (legado)
python -m zettel ask "O que e RAG?" --topk 20             # mais notas semente
python -m zettel ask "O que e RAG?" --save                # salva a resposta em .md (00_Inbox)
python -m zettel ask "O que e RAG?" --save-to nota.md     # salva em caminho especifico
python -m zettel ask "O que e RAG?" --no-save-prompt      # nao pergunta se deve salvar
```

| Flag | Efeito |
|---|---|
| `--topk N` | Número de notas semente (override de `retrieval.ask.topk`). |
| `--no-graph` | Desliga a expansão por grafo. |
| `--mode vector\|hybrid` | Override de `retrieval.mode`. |
| `--show-context` | Mostra a tabela de notas recuperadas **e** os parâmetros de recuperação usados. |
| `--save` / `--save-to PATH` | Salva a resposta como `.md` (padrão: `00_Inbox/`). |
| `--no-save-prompt` | Não pergunta se deve salvar (scripts). |

Detalhes da recuperação e do relatório em [recuperacao.md](recuperacao.md#perguntar-ao-acervo-zettel-ask).

---

## `article`

```bash
python -m zettel article "Tecnicas de Prompt Engineering" --style blog
python -m zettel article "Grafos de conhecimento" --style academic --personality serious_academic --save
python -m zettel article "RAG" --outline-only             # so o outline, sem redigir
python -m zettel article "RAG" --skip-context-review --skip-judge --no-save-prompt
```

| Flag | Efeito |
|---|---|
| `--style` / `-s` | `blog` (menções leves) ou `academic` (ABNT autor-data). |
| `--personality` / `-p` | Perfil de reescrita em `config/personalities.yaml` (`neutral` = sem reescrita). |
| `--style-notes` | Override textual de estilo. |
| `--topk`, `--no-graph`, `--mode`, `--show-context` | Mesmos controles de recuperação do `ask`. |
| `--outline-only` | Gera só o outline e encerra. |
| `--skip-context-review` | Pula o HITL de aprovação do pool de contexto. |
| `--skip-judge` | Pula o juiz automático de qualidade. |
| `--max-judge-iterations N` | Teto de ciclos draft ↔ judge. |
| `--save` / `--save-to` / `--no-save-prompt` | Persistência do artigo (`00_Inbox/ART - ....md`). |

Fluxo completo em [recuperacao.md](recuperacao.md#gerar-artigo-a-partir-do-vault-zettel-article).

---

## `skill`

Projeta um recorte **já aprovado** do vault no formato [Agent Skills](https://code.claude.com/docs/en/skills): um `SKILL.md` pequeno, sempre carregado, mais arquivos que o agente abre sob demanda. Determinístico — **não** chama LLM, não grava nada no SQLite/Chroma.

```bash
python -m zettel skill --source-id @Kahneman2011ThinkingFast
python -m zettel skill --moc-id 01HXYZ... --out ./packs
python -m zettel skill --topic "Redes Neurais" --overwrite
```

| Flag | Efeito |
|---|---|
| `--source-id` / `--moc-id` / `--topic` | Seletor do recorte. Exatamente **um**. |
| `--out` | Diretório que guarda os packs; o pack vai para `<out>/<slug>`. Default: `<vault>/.claude/skills`. |
| `--slug` | Nome do pack (default: derivado do seletor). |
| `--overwrite` | Regenera por cima; **limpa** o diretório antes. |
| `--include-excerpts` | Copia o trecho da fonte para os arquivos do pack (default: não copia). |

Layout gerado (plano, um nível — sem child skills):

```
<out>/<slug>/
  SKILL.md        # frontmatter + How to Use + Core + Topic Index + Note Index
  notes/*.md      # uma nota por arquivo, abertas sob demanda
  cheatsheet.md   # regras de decisão, anti-padrões, limites, tensões (contradicts)
  glossary.md     # termos -> nota que define
```

Detalhes que importam:

- **Só entra o que passou pelo portão humano.** ZTL aprovadas; LIT granular aprovada é o fallback quando a fonte ainda não tem nota permanente (só para `--source-id`).
- **Trecho da fonte fica de fora por padrão** — o pack é publicável. Citekey, localizador, tese e wikilinks permanecem.
- **Orçamento de contexto**: só a seção Core é limitada (~4000 tokens). Os dois índices são a tabela de roteamento e nunca são truncados; a CLI mostra a estimativa do `SKILL.md` para você perceber quando o recorte está grande demais.
- **`--overwrite` apaga o diretório do pack.** É saída gerada, não nota do vault: edite o vault e regenere.
- `--topic` que casa com mais de um tópico de MOC é **erro**, listando os candidatos.

Decisão e alternativas: [ADR-035](adrs/generated/CLI/ADR-035-flat-agent-skill-export.md).

---

## `new-note`

Cria o esqueleto de uma nota manual no vault (`origin: manual`) — **não** indexa. Rode `sync-manual` depois.

```bash
python -m zettel new-note ztl "Minha tese sobre RAG"
python -m zettel new-note src "Artigo sobre grafos" -a "Silva, João" -y 2024
python -m zettel new-note lit "Resumo do capitulo 3" -k Autor2024 -a Autor --granular -p 42
python -m zettel new-note moc "Mapa de recuperacao hibrida"
python -m zettel new-note ztl "Titulo" --force   # sobrescreve arquivo existente

# Da LIT para uma nota permanente:
python -m zettel new-note ztl --from-lit "vault/20_Literature/Kahneman2011.../LIT - ... .md"
python -m zettel new-note ztl --from-lit "@Kahneman2011ThinkingFast::manual::0001" --llm
```

Atenção: aqui `-y` é `--year`, **não** `--yes`.

A tabela completa de tipos, flags e do caminho `--from-lit` está em [notas-manuais.md](notas-manuais.md#scaffold-com-zettel-new-note).

---

## `sync-manual`

```bash
# Sincronizar notas manuais do vault com o índice (SRC, LIT, ZTL e MOCs)
python -m zettel sync-manual

# Re-derivar arestas do grafo a partir dos wikilinks no corpo das notas
python -m zettel sync-manual --rebuild-graph
```

| Flag | Efeito |
|---|---|
| `--rebuild-graph` | Antes do sync, re-deriva arestas `related` a partir dos wikilinks no corpo de **todas** as notas já persistidas no SQLite. |
| `--yes` / `-y` | Confirma reprocessamento se o embedding mudou. |

Veja [notas-manuais.md](notas-manuais.md).

---

## `delete-source`

```bash
# Apagar uma fonte por completo (vault + SQLite + Chroma; irreversivel)
python -m zettel delete-source '@Citekey'
python -m zettel delete-source '@Citekey' --yes              # sem confirmacao
python -m zettel delete-source '@Citekey' --delete-permanent # apaga ZTL ligadas
python -m zettel delete-source '@Citekey' --no-compact       # sem VACUUM
```

Escopo exato do que é removido e do que é mantido: [operacao.md](operacao.md#remover-fonte-com-zettel-delete-source).

---

## `dump-chunks`

```bash
# Exportar chunks ja persistidos como markdown (inspecao, sem reprocessar)
python -m zettel dump-chunks --source-id @AutorAnoTitulo
python -m zettel dump-chunks --all --dump-dir ./tmp/chunks
```

Exige `--source-id` ou `--all`. Default: `data/cache/chunk-dumps/`.

---

## `dump-extraction`

```bash
# Exportar o Markdown extraido (Docling/MD, headings H1-H6;
# PDF pode ter <!-- zettel:page-break -->)
python -m zettel dump-extraction --source-id @AutorAnoTitulo
python -m zettel dump-extraction --all --dump-dir ./tmp/extraction
```

Exige `--source-id` ou `--all`. Default: `data/cache/extraction-dumps/`.

---

## `reindex`

```bash
# Reconstruir o ChromaDB a partir do SQLite (sem chamadas de LLM).
# Apos troca de embedding, use --force (obrigatorio para regenerar sources/chunks).
python -m zettel reindex
python -m zettel reindex --force
python -m zettel reindex --collection chunks --force
python -m zettel reindex --force --yes
```

| Flag | Efeito |
|---|---|
| `--collection` | Reindexa apenas `sources`, `chunks`, `permanent_notes` ou `mocs`. |
| `--force` | Reseta a coleção antes de repovoar. Necessário após troca de embedding. |
| `--yes` / `-y` | Confirma sem prompt. |

Um `reindex` completo também reconstrói o índice lexical FTS5. Veja [operacao.md](operacao.md#retencao-e-reconstrucao).

---

## `rebuild`

```bash
# Reconstruir o vault (.md) e/ou o Chroma a partir do SQLite, sem reprocessar LLM
python -m zettel rebuild --what vault          # recria os .md
python -m zettel rebuild --what chroma
python -m zettel rebuild --what all --dry-run  # simula vault + chroma
python -m zettel rebuild --what vault --force  # sobrescreve (nunca notas manuais)
```

| Flag | Efeito |
|---|---|
| `--what vault\|chroma\|all` | O que reconstruir (default `vault`). |
| `--force` | Sobrescreve arquivos existentes — **nunca** notas `origin: manual`. |
| `--dry-run` | Simula sem escrever. |

---

## `retry-failed`

```bash
python -m zettel retry-failed                        # chunks com falha -> pending
python -m zettel retry-failed --source-id @Citekey   # apenas de uma fonte
python -m zettel retry-failed --assets               # imagens com falha de descricao -> pending
```

Depois de resetar, rode `extract` novamente para reprocessar.

---

## `status`

```bash
# Ver estatisticas do pipeline (alerta se houver chunking incompleto)
python -m zettel status
```

Mostra o funil (fontes, chunks por status, notas, MOCs), duplicatas detectadas por camada e a tabela de custos do último run.

---

## `doctor`

```bash
# Verificar configuracao, dependencias, cobertura de capitulos e espaco de embedding
python -m zettel doctor
```

Checa Docling, bibliotecas de clustering, disponibilidade de FTS5, integridade dos caminhos, cobertura de capítulos e drift do espaço de embedding.

---

## `run-all`

```bash
python -m zettel run-all
python -m zettel run-all --config ./minha_config.yaml
python -m zettel run-all --yes --skip-biblio   # flags de harvest valem aqui
python -m zettel run-all --dry-run             # simula sem escrever notas
```

Aceita as mesmas flags de duplicidade/bibliografia do `harvest` (`--yes`, `--skip-duplicates`, `--force`, `--skip-biblio`) e um `--dry-run`. A paginação é resolvida pela heurística (não há `--content-start-*` aqui).

---

## Opções comuns

```bash
# Usar arquivo de configuração alternativo
python -m zettel run-all --config ./minha_config.yaml

# Flags de harvest tambem valem em run-all (--yes aplica heuristica de paginacao)
python -m zettel run-all --yes --skip-biblio

# Ajustar top-k de notas similares
python -m zettel connect --topk 10

# Ajustar limiar de deduplicação
python -m zettel connect --dedupe-threshold 0.90

# Ajustar tamanho mínimo de cluster para MOCs
python -m zettel garden --min-cluster-size 3

# Dry run (simula sem escrever notas)
python -m zettel run-all --dry-run
```

> **Windows**: o console em cp1252 não renderiza setas Unicode e afins — por isso as strings de ajuda da CLI evitam esses caracteres.

---

## Ver também

- [Pipeline](pipeline.md) — o que cada fase faz por dentro
- [Operação](operacao.md) — reconstrução, purga e remoção de dados
- [Notas manuais](notas-manuais.md) — `new-note` e `sync-manual` em detalhe
- [Interface web](interface-web.md) — o que está exposto na web e o que é exclusivo da CLI
