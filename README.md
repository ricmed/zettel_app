# Zettelkasten — Pipeline Automatizado de Geração de Notas

Sistema em Python que lê arquivos (PDF, Markdown) e gera **Notas de Literatura** e **Notas Permanentes** seguindo rigorosamente o método Zettelkasten, com saída compatível com **Obsidian**.

## Visão Geral

O pipeline automatiza o ciclo completo do Zettelkasten:

```
Arquivo (PDF/MD)
    ↓ harvest
Texto extraído → Chunks (com pagina) → SRC + indice LIT
    ↓ extract
Drafts de LIT granular (1 por chunk) em 00_Inbox/Review
    ↓ review
LIT aprovadas em 20_Literature/{Citekey}/ + literature_notes no Chroma
    ↓ connect
Notas Permanentes (ZTL) com links e backlinks
    ↓ garden
MOCs (Mapas de Conteúdo) por clusterização semântica
```

### Princípios

- **Atomicidade real**: cada nota permanente = uma tese + explicação autônoma + limites
- **Rastreabilidade**: toda nota aponta para sua fonte (literatura + localizador)
- **Autonomia**: notas permanentes são compreensíveis sem consultar a fonte
- **Conectividade intencional**: links apenas quando há relação clara
- **Não-regressão**: atualizações automáticas não destroem edições manuais (blocos gerenciados)
- **Resistência a drift**: hashes em camadas + IDs estáveis + cache de LLM

## Arquitetura

```
zettel_app/
├── zettel/                  # Pacote principal
│   ├── cli.py               # Interface CLI (Typer + Rich)
│   ├── config.py            # Schema Pydantic + fallback; load_config le o YAML
│   ├── schemas.py           # Modelos Pydantic
│   ├── hashing.py           # Hashing canônico em camadas
│   ├── pricing.py           # Estimativa de custo via mapa LiteLLM (só calculadora)
│   ├── usage.py             # CostTracker por run/fonte (contextvars)
│   ├── llm.py               # get_llm / call_llm (com usage + custo)
│   ├── state.py             # SQLite — estado incremental
│   ├── vault.py             # I/O do vault Obsidian (frontmatter, blocos gerenciados)
│   ├── index.py             # ChromaDB — índice vetorial
│   ├── bibliography.py      # Metadados bibliograficos ABNT (tipos, inferencia, formatacao)
│   ├── harvester.py         # Fase 1: ingestão, paginas, chunking estrutural (+ rechunk)
│   ├── chunk_dump.py        # Dump markdown opt-in dos chunks (inspecao de chunking)
│   ├── extraction_dump.py   # Dump markdown opt-in do texto extraido (headings Docling/MD)
│   ├── paging.py            # Inferencia de pagina arquivo/livro + inicio do conteudo
│   ├── extractor.py         # Fase 2: Prompt 1 → drafts LIT granulares
│   ├── review.py            # Fase 2b: aprovacao seletiva de LIT antes do vetorial
│   ├── connector.py         # Fase 3: geração de notas permanentes
│   ├── gardener.py          # Fase 4: clusterização e MOCs
│   ├── retrieval.py         # Recuperação híbrida (vetor + BM25) com fusão RRF
│   ├── graph.py             # Expansão por grafo sobre as conexões tipadas (GraphRAG leve)
│   ├── ask.py               # Comando `ask`: QA sobre o vault com citações
│   ├── assets.py            # Extração e descrição multimodal de imagens
│   ├── rebuild.py           # Reconstrução do Chroma (reindex) e do vault (rebuild) a partir do SQLite
│   ├── sync.py              # Sincronização de notas manuais (SRC/LIT/ZTL/MOC)
│   ├── progress.py          # Protocolo ProgressObserver (CLI + web)
│   ├── web.py               # Interface web FastAPI (rotas, auth, templates)
│   ├── web_app.py           # Fila de jobs web e dispatch do pipeline
│   ├── templates/           # Jinja2 server-rendered (14 páginas)
│   └── static/              # CSS (app.css, mobile.css)
├── config/
│   ├── config.yaml          # Fonte operacional (todos os knobs do schema)
│   └── moc_topics.yaml      # Taxonomia hierarquica de topicos para MOCs
├── prompts/                     # Templates de prompts para o LLM
│   ├── bibliographic_metadata.md # Extracao de metadados bibliograficos (ABNT)
│   ├── literature_note.md       # Prompt 1: extracao de conceitos (c/ relevance_score)
│   ├── permanent_note.md        # Prompt 2: geracao de nota permanente (+ tipos de relacao)
│   ├── dedupe_decision.md       # Decisao de deduplicacao
│   ├── moc_generation.md        # Geracao de MOCs (c/ dominio e categorias)
│   ├── moc_incremental.md       # Classificacao incremental de notas em MOC existente
│   ├── ptbr_guard.md            # Guardrail de idioma PT-BR
│   ├── ask.md                   # Resposta a perguntas sobre o vault
│   ├── image_description.md     # Descricao multimodal de imagens (PT-BR)
│   ├── article_*.md             # Pipeline de artigo (outline, secoes, judge, etc.)
│   └── article_anti_ai.md       # Bloco anti-prosa generica no article
├── data/
│   ├── inbox/               # Arquivos para processar (drop zone)
│   ├── processed/           # Arquivos já processados
│   ├── cache/               # Cache intermediário (checkpointer article, dumps de chunks/extracao)
│   ├── chroma/              # ChromaDB (persistente)
│   └── state.db             # SQLite (estado do pipeline)
├── vault/                   # Vault do Obsidian (criado pelo init)
│   ├── 00_Inbox/
│   │   └── Review/          # Drafts de LIT granular (aguardando aprovacao)
│   ├── 10_Sources/          # Notas bibliográficas (SRC)
│   ├── 20_Literature/       # Indice LIT + pasta Citekey/LIT - AuthorYear - pNNN - topico.md
│   ├── 30_Permanent/        # Notas permanentes (ZTL)
│   ├── 40_MOCs/             # Mapas de Conteúdo
│   └── 90_Assets/           # Imagens extraídas de PDFs/Markdown (nome por hash)
├── tests/                   # Testes unitários
├── requirements.txt
└── README.md
```

## Instalação

### Pré-requisitos

- Python 3.10+
- Uma API key de LLM (OpenAI por padrão)

### Passo a passo

1. **Clone o repositório**:

```bash
git clone <repo-url>
cd zettel_app
```

2. **Crie e ative um ambiente virtual**:

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/macOS
source .venv/bin/activate
```

3. **Instale as dependências**:

```bash
pip install -r requirements.txt
```

```bash
Para usar GPU: pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

4. **Configure a API key**:

```bash
# Copie o arquivo de exemplo e edite com sua chave
cp .env.example .env
```

Edite o arquivo `.env` na raiz do projeto:

```env
OPENAI_API_KEY=sk-sua-chave-aqui
```

O sistema carrega automaticamente as variaveis do `.env` ao iniciar.

5. **Inicialize o sistema**:

```bash
python -m zettel init
```

### Dependências opcionais

Descomente no `requirements.txt` conforme necessário:

| Dependência | Finalidade |
|---|---|
| `langchain-anthropic` | Usar Claude como LLM |
| `langchain-google-genai` | Usar Gemini como LLM |
| `langchain-ollama` | Usar modelos locais via Ollama |
| `pymupdf` | Extração PDF alternativa (mais leve que Docling) |
| `umap-learn` | Clusterização avançada para MOCs |
| `hdbscan` | Clusterização densa para MOCs |

## Configuração

A **fonte operacional** é `config/config.yaml` — é o arquivo que o CLI carrega. `zettel/config.py` define o schema Pydantic e só aplica fallback se o YAML faltar, se uma chave for omitida, ou nos testes que instanciam `AppConfig()`. Segredos (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) ficam no `.env`, não no YAML.

Um teste (`tests/test_config.py`) exige que toda chave do schema exista no YAML, com exceção de `gardener.allowed_topics` (override de testes). Valores deste vault (Ollama, CUDA, limiares calibrados) estão no arquivo local; o bloco abaixo é o catálogo de knobs.

Edite `config/config.yaml`:

```yaml
# Caminhos
vault_path: ./vault
inbox_path: ./data/inbox

# LLM
llm:
  provider: openai           # openai | anthropic | ollama | gemini | openrouter | opencode
  model: gpt-4o-mini
  temperature: 0             # 0 = deterministico (reduz drift)
  top_p: 1                   # nucleus sampling (encaminhado a get_llm)
  max_retries: 2
  base_url: null             # gateways OpenAI-compatible
  prompt_cache: true         # prefix cache do provedor (System+Human)

# Embeddings
embedding:
  provider: openai                    # openai | sentence-transformers | ollama
  model: text-embedding-3-small
  base_url: null                      # opcional; ollama default http://localhost:11434/v1
  allow_fallback: false               # false = erro se faltar API key (evita vetores de 384 dims silenciosos)

# Chunking
chunking:
  chunk_size: 1000           # caracteres por chunk (nao tokens)
  chunk_overlap: 200         # sobreposicao
  min_section_chars: 200     # secoes (H3+) menores sao fundidas com a seguinte

# Imagens (extracao + descricao multimodal)
images:
  enabled: true              # extrai imagens de PDF (Docling) e Markdown e as descreve
  scale: 2.0                 # images_scale do Docling
  min_width: 64              # descarta imagens menores (icones/logos)
  min_height: 64
  context_chars: 600         # caracteres ao redor da imagem usados como contexto
  model: ""                  # vazio = usa llm.model (precisa ser multimodal, ex. gpt-4o-mini)
  min_interval_seconds: 0.4  # pacing entre descricoes (evita estourar TPM)
  rate_limit_max_retries: 8  # retries por imagem em 429 (nao marca failed)
  rate_limit_backoff_max: 60 # teto de espera (s)
  rate_limit_abort_after: 5  # 429 esgotados seguidos => pausa o lote (fica pending)

# Linkagem
linking:
  topk: 5                    # default do Retriever e RAG de connect/sync
  dedupe_threshold: 0.85     # similaridade; L2 = 2 * (1 - threshold) no extract

# Harvest (duplicatas + metadados bibliograficos ABNT)
harvest:
  duplicate_chunk_threshold: 0.88
  duplicate_sample_size: 5
  non_interactive_duplicate_action: skip   # skip | continue | abort
  biblio_confidence_threshold: 0.7         # abaixo disso, pede confirmacao do tipo
  biblio_llm_enabled: true                 # enriquece metadados via LLM apos heuristicas
  biblio_text_sample_chars: 5000           # amostra inicial (capa/folha de rosto) enviada ao LLM

# Literature review (aprovacao seletiva de LIT por chunk)
literature_review:
  auto_approve_min_confidence: 0.85
  batch_sample_size: 20
  drafts_subdir: 00_Inbox/Review

# Recuperacao (busca hibrida + GraphRAG leve)
retrieval:
  mode: hybrid               # hybrid (vetor + BM25) | vector (so Chroma, legado)
  rrf_k: 60                  # constante do Reciprocal Rank Fusion
  relevance_floor:
    enabled: true            # piso ABSOLUTO de relevancia (alem do ranking RRF)
    min_vector_similarity: 0.70   # similaridade coseno minima (calibrado empiricamente)
    bm25_hit_bypasses_floor: true # match lexical real (BM25) pode dispensar a similaridade
    bm25_bypass_max_rank: 5       # ... mas so quando o match lexical for forte (top-5 do BM25)
    absolute_min_similarity: 0.15 # piso rigido que nem o bypass do BM25 derruba
  graph_expansion:
    enabled: true            # expande resultados pelas conexoes tipadas entre notas
    max_hops: 1              # saltos no grafo (1 ja traz o valor principal)
    decay: 0.5               # atenuacao do score por salto
    max_neighbors: 10        # teto de vizinhos trazidos ao contexto
    relation_weights:        # omitir = DEFAULT_RELATION_WEIGHTS em config.py
      contradicts: 1.0
      extends: 0.9
      depends_on: 0.9
      supports: 0.8
      exemplifies: 0.7
      related: 0.5
  ask:
    topk: 8                  # notas semente do comando `ask`
    max_context_notes: 8     # teto de notas no contexto do LLM
    max_chars_per_note: 1500 # truncagem do corpo de cada nota no contexto
  article:
    topk: 20                 # notas semente do comando `article`
    max_context_notes: 24
    max_chars_per_note: 1200
    max_hops: 2              # expansao de grafo mais ampla que o ask
    max_sections: 8
    max_figures: 6
    chars_per_section_draft: 2500
    personalities_path: ./config/personalities.yaml
    default_personality: neutral
    enrich_query_count: 6
    max_judge_iterations: 3
    judge_min_score: 7.0
    writer_temperature: null # null = llm.temperature
    judge_temperature: 0.2
    enrich_temperature: 0.2

# Filtragem de candidatos a notas permanentes
extraction:
  min_relevance_score: 3     # score minimo de relevancia (1-5)
  min_thesis_words: 5        # palavras minimas na tese
  require_anchor_quote: true # exigir citacao-ancora
  min_definition_words: 10   # palavras minimas na definicao

# Gardener (MOCs)
gardener:
  min_cluster_size: 5        # notas minimas por cluster
  min_notes_for_moc: 3       # notas minimas para gerar MOC
  domain: "Ciencia de Dados" # dominio do acervo
  strict_topics: true        # rejeitar MOCs fora das categorias da taxonomia
  topics_path: ./config/moc_topics.yaml  # pilar > categoria > topicos
  cluster_within_category: true
  category_label_template: "{domain}: {categoria}"
  overlap_threshold: 0.4
  graph_cohesion_enabled: true
  graph_cohesion_min_ratio: 0.0
  umap_n_neighbors: null     # null = auto min(15, n-1)
  hdbscan_min_samples: null  # null = default HDBSCAN

# MOCs hub (use: zettel garden --hubs)
hub_mocs:
  selection_mode: percentile
  hub_percentile: 0.90
  top_n_hubs: 20
  min_weighted_degree: 8.0
  max_hops: 2
  max_neighbors: 25
  min_neighbors: 3
  decay: 0.5
  min_neighbor_weight: 0.3
  dedup_subset_threshold: 0.8

# PDF
pdf_extractor: docling       # docling | pymupdf

language: pt-BR
log_level: INFO
device: auto                 # auto | cpu | cuda
```

### Provedores de LLM suportados

**OpenAI** (padrão):
```yaml
llm:
  provider: openai
  model: gpt-4o-mini    # ou gpt-4o, gpt-4-turbo
  prompt_cache: true
```
Requer: `OPENAI_API_KEY`

**Gateways OpenAI-compatible** (OpenRouter, OpenCode, vLLM, LM Studio, Azure-compatible):
```yaml
llm:
  provider: openrouter   # ou opencode | compatible | azure
  model: openai/gpt-4o-mini
  base_url: https://openrouter.ai/api/v1
  prompt_cache: true
```
Usa `ChatOpenAI` com `base_url`. A chave segue o que o gateway espera (ex.: `OPENAI_API_KEY`).

**Anthropic**:
```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
  prompt_cache: true
```
Requer: `ANTHROPIC_API_KEY` e `pip install langchain-anthropic`

**Gemini**:
```yaml
llm:
  provider: gemini
  model: gemini-2.0-flash
  prompt_cache: true
```
Requer: `GOOGLE_API_KEY` (ou equivalente do SDK) e `pip install langchain-google-genai`

**Ollama (local)**:
```yaml
llm:
  provider: ollama
  model: llama3.1        # ou qualquer modelo local
```
Requer: Ollama rodando localmente e `pip install langchain-ollama`

### Prompt caching do provedor vs cache SQLite

Há **dois** mecanismos distintos:

1. **`llm_cache` (SQLite)** — se a mesma chamada (prompt + inputs + modelo + temperatura) se repetir, a resposta completa é reutilizada (`$0`, sem HTTP). É o `cache_hits` nos logs/`zettel status`.
2. **Prompt caching do provedor** — entre chamadas *diferentes* que compartilham o mesmo prefixo de instruções (ex.: N chunks no `extract`). Os templates em `prompts/` usam o marcador `<!-- zettel:user -->`: o trecho antes vira `SystemMessage` (estável) e o depois `HumanMessage` (payload da chamada). OpenAI/Gemini aproveitam o prefixo de forma implícita; Anthropic recebe `cache_control` explícito; Ollama só ganha reuso de KV (sem billing). Contadores: `prompt_cache_read_tokens` / `prompt_cache_write_tokens` nos logs COST (observabilidade; a estimativa USD via LiteLLM ainda usa tokens in/out totais — o desconto real aparece na fatura do provedor). Templates curtos podem ficar abaixo do mínimo do provedor (~1k–2k tokens) e não gerar hit.

Desligue hints/layout com `llm.prompt_cache: false` se precisar comparar.
### Provedores de embedding suportados

**OpenAI** (padrão):
```yaml
embedding:
  provider: openai
  model: text-embedding-3-small
```
Requer: `OPENAI_API_KEY`

**Sentence-Transformers** (local via PyTorch):
```yaml
embedding:
  provider: sentence-transformers
  model: all-MiniLM-L6-v2
```
Usa `device` da config (`auto` / `cpu` / `cuda`).

**Ollama** (local):
```yaml
embedding:
  provider: ollama
  model: qwen3-embedding
  # base_url: http://localhost:11434/v1   # opcional (API OpenAI-compatible)
```
Requer: Ollama rodando com o modelo de embedding puxado (`ollama pull qwen3-embedding`). Não precisa do pacote Python `ollama` — usa o endpoint `/v1` compatível com OpenAI.

### Trocar o modelo de embedding

O espaço vetorial é identificado pelo par `provider`/`model` gravado na metadata das coleções Chroma. Trocar qualquer um dos dois torna os vetores antigos incompatíveis.

1. Edite `embedding.provider` / `embedding.model` (e opcionalmente `base_url`) em `config/config.yaml`.
2. No próximo comando que abre o índice (`harvest`, `extract`, `connect`, `ask`, `reindex`, etc.), o CLI detecta o drift, avisa e pede confirmação.
3. Confirmando (ou passando `--yes`), roda automaticamente um `reindex --force`: regenera todos os embeddings a partir do SQLite, **sem** chamar o LLM e **sem** reescrever notas `.md`.

Você também pode forçar na mão:

```bash
python -m zettel reindex --force
python -m zettel reindex --force --yes   # sem prompt (scripts/CI)
```

Sem `--force` após uma troca de modelo, sources/chunks já indexados **não** seriam regenerados (só notas permanentes), misturando espaços vetoriais. Por isso, na detecção de drift, o `reindex` aplica `--force` automaticamente.

Depois da troca, se a qualidade da busca degradar, recalibre `retrieval.relevance_floor.min_vector_similarity` e os limiares de dedupe (`linking.dedupe_threshold`, `harvest.duplicate_chunk_threshold`) — são dependentes do modelo. O `zettel doctor` também reporta drift de embedding.

## Interface web

A interface FastAPI é server-rendered e não exige Node, bundler ou acesso direto
do navegador ao SQLite/ChromaDB.

```bash
# Segredo de instância (Replit Secrets, .env ou variável do processo — não vai no config.yaml)
# SESSION_SECRET=...

uvicorn zettel.web:app --host 0.0.0.0 --port "${PORT:-5000}"
```

Não há subcomando `zettel web`; a UI é um app FastAPI separado. Testes: `pytest tests/test_web.py tests/test_web_state.py -v`. Config alternativo: env `ZETTEL_CONFIG=/caminho/config.yaml`.

Abra o preview e entre com o valor de `SESSION_SECRET`. A navegação oferece:

- **Visão geral**: KPIs, funil, confiança, custos, runs, duplicatas e qualidade do grafo;
- **Documentos**: upload de PDF/Markdown/TXT (até 25 MB), decisões de duplicidade,
  bibliografia e paginação, e harvest **de um arquivo por vez** (não inbox inteiro);
- **Pipeline**: extract, connect, garden taxonômico, garden por hubs, sincronização
  manual e repetição segura de chunks/assets com falha;
- **Revisão**: filtros por fonte/confiança, trecho, candidatos e aprovação/rejeição
  em lote (sem auto-approve por limiar — use a CLI para `--yes` / bandas interativas);
- **Notas / MOCs**: listagem read-only e detalhes de notas permanentes, MOCs e fontes;
- **Execuções**: estado persistente, progresso (polling em `/api/jobs/{id}`), eventos,
  resultado e erro sanitizado;
- **Configuração / saúde**: FTS5, diretórios, identidade LLM/embedding (incl. drift
  de `dimensions`) — sem segredos.

### Persistência, concorrência e recuperação

- A implantação é de **instância única** e executa no máximo um trabalho mutante
  por vez. Não use múltiplos processos/workers Uvicorn.
- Preserve `data/` e `vault/` em armazenamento persistente. `data/state.db` contém
  a fila e os eventos; `data/chroma/` contém vetores; `vault/` contém as notas.
- Recarregar ou fechar a página não interrompe o trabalho. Ao reiniciar o servidor,
  jobs que estavam `running` viram `interrupted`; jobs ainda `queued` são retomados.
- Chamadas LLM/PDF em curso não são canceladas à força. A recuperação ocorre entre
  checkpoints seguros, executando novamente a fase quando necessário.
- Operações destrutivas (`init --reset`, purge, rebuild, reindex e garden recreate)
  não são expostas na primeira versão web e continuam disponíveis somente na CLI.
- Também só na CLI: `ask`, `article`, `run-all`, harvest de inbox inteiro, resolução
  interativa de duplicatas semânticas, `set-paging`, `rechunk`, dumps e `doctor`.
- A CLI permanece compatível e continua usando a apresentação Rich normalmente.

## Uso

### Fluxo básico

```bash
# 1. Coloque arquivos PDF ou Markdown em data/inbox/
cp meu_artigo.pdf data/inbox/

# 2. Execute o pipeline completo
python -m zettel run-all

# 3. Abra o vault no Obsidian
#    Aponte para a pasta ./vault
```

### Comandos individuais

```bash
# Recria o vault vazio (apaga ./vault). State DB, Chroma e cache permanecem
python -m zettel init

# Alem do vault, apaga State DB, ChromaDB e cache (pede confirmacao)
python -m zettel init --reset

# Escanear inbox e processar arquivos → SRC + indice LIT + chunks (com paginas)
python -m zettel harvest
python -m zettel harvest --yes --skip-biblio --skip-paging
python -m zettel harvest --content-start-file 35 --content-start-book 10
# arquivo p.35 = impressa p.10; paginas anteriores nao geram chunks
python -m zettel harvest --dump-chunks
python -m zettel harvest --dump-chunks --dump-dir ./tmp/chunks
python -m zettel harvest --dump-extraction
python -m zettel harvest --dump-extraction --dump-extraction-dir ./tmp/extraction
python -m zettel set-paging --source-id @Citekey --content-start-file 35 --content-start-book 10

# Extrair conceitos → drafts LIT granulares em 00_Inbox/Review
python -m zettel extract
python -m zettel extract --auto-approve   # aprova drafts com confianca >= limiar

# Revisar/aprovar LIT granulares (obrigatorio antes do connect, salvo auto-approve)
python -m zettel review
# Interativo: relatorio por faixa de confianca; a=aprovar >= limiar,
# d=reprovar (t=todos / b=baixissima / m=media / h=alta, com confirmacao),
# r=um a um (atalhos a/r/p/q), q=sair
python -m zettel review --yes             # aprova todos >= limiar (nao-interativo)
python -m zettel review --low-confidence-only
python -m zettel purge-rejected           # apaga rejected + VACUUM state.db/chroma.sqlite3
python -m zettel purge-rejected --yes     # sem confirmacao
python -m zettel purge-rejected --no-compact  # so apaga, sem compactar disco

# Gerar notas permanentes a partir dos conceitos aprovados
python -m zettel connect

# Clusterizar notas e gerar MOCs
python -m zettel garden

# MOCs hub (porta de entrada tematica; complementar ao pipeline taxonomico)
python -m zettel garden --hubs

# Regenerar MOCs do pipeline do zero (apaga vault + banco + indice; pede confirmacao)
python -m zettel garden --recreate

# Regenerar apenas MOCs hub
python -m zettel garden --hubs --recreate -y

# Sem prompt de confirmacao (util em scripts)
python -m zettel garden --recreate -y

# Perguntar ao acervo (QA com recuperacao hibrida + expansao por grafo)
python -m zettel ask "O que e RAG?"
python -m zettel ask "O que e RAG?" --show-context        # mostra as notas recuperadas
python -m zettel ask "O que e RAG?" --no-graph            # so busca hibrida, sem grafo
python -m zettel ask "O que e RAG?" --mode vector         # so busca vetorial (legado)
python -m zettel ask "O que e RAG?" --save                # salva a resposta em .md (00_Inbox)
python -m zettel ask "O que e RAG?" --save-to nota.md     # salva em caminho especifico

# Gerar artigo estruturado a partir do vault (outline interativo)
python -m zettel article "Tecnicas de Prompt Engineering" --style blog
python -m zettel article "Grafos de conhecimento" --style academic --personality serious_academic --save
python -m zettel article "RAG" --outline-only             # so o outline, sem redigir

# Sincronizar notas manuais do vault com o índice (SRC, LIT, ZTL e MOCs)
python -m zettel sync-manual

# Re-derivar arestas do grafo a partir dos wikilinks no corpo das notas manuais
python -m zettel sync-manual --rebuild-graph

# Re-chunkar fontes com a config atual (a partir do texto ja extraido, sem reprocessar o arquivo).
# Tambem completa harvest interrompido e re-resolve o chapter_id das imagens.
python -m zettel rechunk --all
python -m zettel rechunk --source-id @AutorAnoTitulo
python -m zettel rechunk --source-id @AutorAnoTitulo --dump-chunks

# Exportar chunks ja persistidos como markdown (inspecao, sem reprocessar)
python -m zettel dump-chunks --source-id @AutorAnoTitulo
python -m zettel dump-chunks --all --dump-dir ./tmp/chunks

# Exportar o Markdown extraido (Docling/MD, headings H1-H6 intactos; sem reprocessar)
python -m zettel dump-extraction --source-id @AutorAnoTitulo
python -m zettel dump-extraction --all --dump-dir ./tmp/extraction

# Reconstruir o ChromaDB a partir do SQLite (sem chamadas de LLM).
# Apos troca de embedding, use --force (obrigatorio para regenerar sources/chunks).
python -m zettel reindex
python -m zettel reindex --force
python -m zettel reindex --collection chunks --force

# Reconstruir o vault (.md) e/ou o Chroma a partir do SQLite, sem reprocessar LLM
python -m zettel rebuild --what vault          # recria os .md (nunca sobrescreve notas manuais)
python -m zettel rebuild --what all --dry-run  # simula vault + chroma

# Reprocessar itens com falha
python -m zettel retry-failed                  # chunks com falha -> pending
python -m zettel retry-failed --assets         # imagens com falha de descricao -> pending

# Ver estatisticas do pipeline (alerta se houver chunking incompleto)
python -m zettel status

# Verificar configuracao, dependencias, cobertura de capitulos e espaco de embedding
python -m zettel doctor
```

### Opções comuns

```bash
# Usar arquivo de configuração alternativo
python -m zettel run-all --config ./minha_config.yaml

# Flags de harvest tambem valem em run-all
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

## Como funciona cada fase

### Fase 1 — Harvest (Coleta)

1. Varre `data/inbox/` por arquivos `.pdf` e `.md`
2. Calcula checksum SHA-256 de cada arquivo (pula inalterados) e aplica **detecção de duplicatas em 3 camadas** (hash de arquivo → hash do texto extraído → similaridade semântica de chunks)
3. Extrai texto usando **Docling** (PDF) ou parser nativo (Markdown)
4. **Metadados bibliográficos (ABNT)**:
   - Infere o **tipo documental** (`livro`, `capitulo_livro`, `artigo_periodico`, `artigo_internet`, `material_curso`, `tese`, `anais_evento`, `relatorio`) e os campos tipados a partir de metadados do arquivo, heurísticas no texto e, se habilitado, LLM (`prompts/bibliographic_metadata.md`, com cache)
   - Campos obrigatórios variam por tipo (ex.: livro exige autores, título, cidade, editora, ano; artigo de internet exige título, URL e data de acesso)
   - Em modo interativo, **sempre** mostra preview (tabela + referência ABNT) e pede confirmação antes de gravar a SRC — mesmo quando os metadados já estão completos; dá para editar tipo/campos se recusar
   - Em modo não-interativo (`--yes`), bibliografia completa é aceita sem prompt; incompleta **pula o arquivo**, salvo `--skip-biblio` (segue com parcial + aviso)
   - Persiste `document_type`, `bibliography_json` e `abnt_reference` no SQLite; a nota **SRC** recebe os campos separados no frontmatter **e** a string `abnt_reference` pronta para citar
5. Gera **citekey** determinístico: `@SobrenomeAnoTituloSlug` (a partir dos metadados já enriquecidos)
6. Grava nota **SRC** (`SRC - AuthorYear - slug.md`) em `10_Sources/` e o **índice LIT** (`LIT - AuthorYear - slug.md`) em `20_Literature/` **antes** do chunking/embeddings (que podem demorar minutos); `processing_status=in_progress`. Pastas e arquivos **nao** usam `@` (o `@` fica so em `source_id` / CLI). Layout antigo (`@Citekey/`, `chunk_NNNN.md`, `*-index.md`) nao e lido; reescreva notas `pipeline` com `zettel rebuild --force` e apague leftovers a mao.
7. Se `images.enabled`, registra imagens já extraídas em `90_Assets/`
8. Resolve **inicio da paginacao** (HITL ou `--content-start-file` / `--content-start-book` / `--skip-paging`): pagina do PDF onde o conteudo comeca e o numero impresso nessa pagina; paginas anteriores nao geram chunks
9. Divide o texto em **capitulos**/secoes, indexa chunks no ChromaDB e no SQLite com `page_in_book = page_in_file - start_file + start_book` (chunk multi-pagina usa a primeira pagina)
10. Atualiza a SRC com inicio de conteudo/paginas/`total_chunks` e `processing_status=completed`
11. **Cobertura de capítulos**: harvest interrompido é completado no próximo `harvest` ou via `zettel rechunk`

### Fase 2 — Extract (Extracao → drafts granulares)

0. Descreve imagens pendentes com LLM multimodal (cache determinístico)
1. Para cada chunk `pending`, chama o LLM com o **Prompt 1**; localizador preferencial = `p.{page_in_book} / {section_path}`
2. Escreve um **draft** em `00_Inbox/Review/{Citekey}/LIT - AuthorYear - pNNN - topico-NNNN.md` (resumo, conceitos, candidatos, **trecho integral da fonte**, imagens; mesmo basename da nota aprovada)
3. Checkpoint no SQLite após **cada** chunk: `status=awaiting_review`, `summary_json`, `review_confidence`, `literature_note_path`
4. Concepts ficam em `awaiting_review` (não elegíveis ao `connect` ainda)
5. Filtragem estrutural de qualidade (relevance_score, tese, definição, âncora) — igual à versão anterior
6. `--auto-approve` pode promover drafts com confiança ≥ `literature_review.auto_approve_min_confidence`

### Fase 2b — Review (aprovacao seletiva)

1. `zettel review` lista drafts `awaiting_review` e um **relatorio de faixas** de `review_confidence` (baixissima `<=0.4`, media ate o limiar, alta `>= limiar`)
2. Modo interativo: `a` aprova lote `>= limiar` (abaixo do limiar permanecem pendentes); `d` abre submenu para rejeitar `t=todos` ou por faixa (`b`/`m`/`h`) apos confirmacao `s/n` (rejeicao parcial volta ao menu); `r` revisa um a um com atalhos `a/r/p/q`; `q` sai
3. **Approve**: move para `20_Literature/{Citekey}/LIT - AuthorYear - pNNN - topico-NNNN.md`, indexa na coleção Chroma **`literature_notes`**, atualiza o índice LIT (wikilinks com rótulo `p. N — tópico`), promove concepts para dedupe → `approved`
4. **Reject**: apaga draft, `status=rejected`, concepts rejeitados — **nunca** entram em `literature_notes` (o chunk permanece no SQLite/Chroma `chunks` ate `zettel purge-rejected`)
5. Deduplicação semântica contra permanentes roda **após** a aprovação (não no extract)
6. `zettel purge-rejected`: remove permanentemente chunks `rejected` (SQLite chunks+concepts+FTS, Chroma `chunks` e `literature_notes` se houver) e por padrao roda `VACUUM` em `state.db` e `chroma.sqlite3` (recupera disco; nao altera dados restantes; `--no-compact` pula)

### Fase 3 — Connect (Conexão)

1. Para cada candidato aprovado, busca **top-k notas similares** (RAG) — apenas para conexões
2. Chama o LLM com o **Prompt 2** (permanent_note.md):
   - Conceito + contexto RAG + opcionalmente `{images_context}` das figuras do candidato → nota permanente completa
3. Valida idioma PT-BR (guardrail automático)
4. Resolve imagens do candidato (`relevant_image_ids`, com o mesmo fallback por path no chunk se a lista estiver vazia) e cria arquivo **ZTL** em `30_Permanent/` com:
   - Frontmatter YAML (type, note_id, source_id, tags, origin, etc.)
   - Corpo: Tese → Definição → Intuição → Exemplo → Limites → **Figuras** (embeds Obsidian + legendas) → Fonte → Conexões
5. Atualiza **backlinks** nas notas relacionadas via blocos gerenciados:
   ```
   <!-- zettel:auto-backlinks:start -->
   - [[ZTL - ID - titulo]]
   <!-- zettel:auto-backlinks:end -->
   ```
6. Indexa no ChromaDB e registra no SQLite, **persistindo o corpo e o frontmatter completos** (`notes.body`/`frontmatter_json`) — o que permite recriar o `.md` sem reprocessar o LLM. O re-embedding é pulado quando o conteúdo semântico e o modelo não mudaram (`embedding_input_hash`). A chamada do Prompt 2 também é cacheada.

### Fase 4 — Garden (Jardim)

Pipeline **hibrido** (taxonomia → cluster por categoria → grafo → roteamento LLM):

1. Carrega embeddings de todas as notas permanentes
2. **Atribuicao taxonomia-first** (`gardener_assign.py`): embedda labels das categorias de `config/moc_topics.yaml` e agrupa cada nota no bucket de maior similaridade
3. **Clusterizacao por bucket**: UMAP + HDBSCAN (ou KMeans como fallback) **dentro de cada categoria**, alinhando clusters ao guarda-chuva da taxonomia
4. Extrai termos representativos via **TF-IDF**
5. **Roteamento inteligente** (`_process_cluster`) — no maximo **1 chamada LLM por cluster**:
   - Assinatura identica → skip (sem LLM)
   - Overlap de notas com MOC existente ≥ `overlap_threshold` → `moc_incremental` apenas
   - Categoria do bucket ja tem MOC → `moc_incremental` apenas
   - Coesao de grafo abaixo de `graph_cohesion_min_ratio` (se > 0) → cluster rejeitado, sem MOC novo
   - Caso contrario → `moc_generation` uma vez, com **categoria sugerida** pelo pipeline no prompt
6. **Validacao de topico** pos-geracao:
   - Substring match bidirecional contra os nomes das **categorias** do YAML
   - Se `strict_topics: true` e sem match: MOC rejeitado (com warning no log)
   - Se `strict_topics: false` e sem match: MOC aprovado (com info no log)
7. **Atualizacao incremental**: classifica notas novas nas subsecoes existentes (`moc_incremental.md`); pode criar subsecoes
8. Notas fora de MOC (ruido HDBSCAN) permanecem no vault e sao navegaveis via grafo/conexoes — sem fila de orfas

Parametros hibridos em `config.yaml` (`gardener.*`):

| Parametro | Proposito |
|-----------|-----------|
| `cluster_within_category` | Ativa pipeline taxonomia-first (default `true`) |
| `category_label_template` | Texto embeddavel por categoria (ex. `"{domain}: {categoria}"`) |
| `overlap_threshold` | Fracao do cluster ja presente em um MOC → update incremental direto |
| `graph_cohesion_enabled` | Calcula score interno do cluster via `note_connections` |
| `graph_cohesion_min_ratio` | `0` = so log; `>0` rejeita cluster fraco antes de criar MOC novo |
| `umap_n_neighbors` | `null` = auto |
| `hdbscan_min_samples` | Opcional; ajuste fino do HDBSCAN |

`zettel garden --recreate` apaga MOCs gerados pelo pipeline (`origin='pipeline'`) e regenera do zero, preservando MOCs manuais.

### Fase 4b — Garden Hub (porta de entrada tematica)

Complementar ao pipeline taxonomico: MOCs ancorados em **notas-hub** (alto grau ponderado em `note_connections`). Uma nota pode aparecer em MOC de categoria **e** em MOC hub.

```bash
python -m zettel garden --hubs              # MOCs hub (complementar)
python -m zettel garden --hubs --recreate -y
```

1. Ranqueia notas permanentes por **grau ponderado** no grafo (`DEFAULT_RELATION_WEIGHTS`)
2. Expande vizinhanca via BFS (`expand_notes`, `max_hops` configuravel)
3. Deduplica vizinhancas muito sobrepostas (`dedup_subset_threshold`)
4. Roteia cada hub (`gardener_hub.py`):
   - MOC existente com mesmo `hub_note_id` → `moc_hub_incremental`
   - Senao → `moc_hub_generation` (topic livre, derivado pelo LLM)
5. Persiste com `origin='hub_pipeline'`, frontmatter `hub_note_id` e secao **Porta de entrada**

| Parametro (`hub_mocs.*`) | Proposito |
|--------------------------|-----------|
| `selection_mode` | `percentile` (top %) ou `absolute` (limiar fixo) |
| `hub_percentile` | Percentil minimo no modo percentile (default `0.90`) |
| `min_weighted_degree` | Limiar absoluto de grau ponderado |
| `top_n_hubs` | Teto de hubs processados por run |
| `max_hops` / `decay` | Expansao BFS a partir do hub |
| `max_neighbors` / `min_neighbors` | Tamanho da vizinhanca |
| `min_neighbor_weight` | Filtra vizinhos fracos pos-BFS |
| `dedup_subset_threshold` | Descarta hub menor se vizinhanca >= N% contida em outra |

`garden --hubs --recreate` apaga apenas MOCs `origin='hub_pipeline'`; MOCs taxonomicos (`pipeline`) e manuais permanecem intactos.

## Estrutura das notas geradas

### Nota Bibliográfica (SRC)

Campos tipados conforme o `document_type` (cidade, editora, edição, URL, instituição, etc.) aparecem separados no frontmatter; `abnt_reference` agrupa a citação no padrão ABNT para copiar facilmente.

```markdown
---
type: source
source_id: "@Kahneman2011ThinkingFast"
document_type: livro
title: "Thinking, Fast and Slow"
author: ["Daniel Kahneman"]
year: 2011
place: "New York"
publisher: "Farrar, Straus and Giroux"
edition: "1. ed."
abnt_reference: "KAHNEMAN, Daniel. Thinking, Fast and Slow. 1. ed. New York: Farrar, Straus and Giroux, 2011."
origin_type: pdf
origin: pipeline
checksum: "a1b2c3..."
cost_usd_total: 0.012345
cost_usd_llm: 0.001234
cost_usd_embedding: 0.011111
tokens_prompt: 1200
tokens_completion: 400
tokens_embedding: 85000
---

# Thinking, Fast and Slow

**Autores**: Daniel Kahneman
**Ano**: 2011
**Tipo documental**: livro
**Tipo de arquivo**: pdf

## Referencia ABNT

KAHNEMAN, Daniel. Thinking, Fast and Slow. 1. ed. New York: Farrar, Straus and Giroux, 2011.

## Indice de Literatura
[[LIT - Kahneman2011 - thinking-fast-and-slow]]
```

Tipos suportados e campos obrigatórios principais:

| Tipo | Obrigatórios (resumo) |
|------|------------------------|
| `livro` | authors, title, place, publisher, year |
| `capitulo_livro` | chapter_authors, chapter_title, book_title, place, publisher, year, pages |
| `artigo_periodico` | authors, title, journal, year |
| `artigo_internet` | title, url, accessed_at |
| `material_curso` | title, institution (+ course/discipline opcionais) |
| `tese` | authors, title, year, institution, degree |
| `anais_evento` | authors, title, event_name, year, place |
| `relatorio` | title, year, institution |

### Custos LLM e embeddings

Cada chamada LLM (via `call_llm`) e cada upsert/query de embedding registra tokens e **USD estimado** usando o mapa público do pacote **LiteLLM** (`cost_per_token` — só calculadora; o runtime continua LangChain). Atualize preços/modelos com `pip install -U litellm`.

- **Por comando**: totais em `runs` (também no fim do log: `Custo do run: ...`). `zettel status` mostra a tabela do último run.
- **Por fonte**: colunas acumulativas em `sources` e frontmatter da SRC (`cost_usd_*`, `tokens_*`).
- **Por ZTL**: `llm_cost_usd` / tokens / `llm_cache_hit` no frontmatter + log na geração.
- **Cache SQLite** (`cache_hits`) = `$0`. **Prompt cache do provedor** = contadores `prompt_cache_read` / `prompt_cache_write` nos logs (não confundir com `cache_hits`).
- Ollama / modelos locais = `$0` (tokens ainda contados). Valores estimados são list price, não a fatura do provedor.
### Nota de Literatura (LIT)

Indice por fonte (`20_Literature/LIT - Kahneman2011 - thinking-fast-and-slow.md`):

```markdown
---
type: literature_index
source_id: "@Kahneman2011ThinkingFast"
citekey: Kahneman2011ThinkingFast
language: pt-BR
origin: pipeline
---

# Thinking, Fast and Slow — Indice de Literatura

← [[SRC - Kahneman2011 - thinking-fast-and-slow]]

## Notas de Literatura aprovadas

<!-- zettel:auto-lit-index:start -->
- [[Kahneman2011ThinkingFast/LIT - Kahneman2011 - p020 - sistema-1-0001|p. 20 — Sistema 1]]
<!-- zettel:auto-lit-index:end -->
```

Nota granular (`20_Literature/Kahneman2011ThinkingFast/LIT - Kahneman2011 - p020 - sistema-1-0001.md`):

```markdown
---
type: literature
source_id: "@Kahneman2011ThinkingFast"
citekey: Kahneman2011ThinkingFast
chunk_id: "@Kahneman2011ThinkingFast::ch001::abc12345"
chunk_index: 1
status: approved
language: pt-BR
origin: pipeline
---

# Sistema 1 (p. 20)

## Resumo
O Sistema 1 opera de forma automatica e rapida...

## Conceitos-chave
#heuristicas #vieses-cognitivos

## Trecho da fonte

<!-- zettel:auto-source-excerpt:start -->
The System 1 operates automatically and quickly, with little or no effort...
<!-- zettel:auto-source-excerpt:end -->
```

### Nota Permanente (ZTL)

```markdown
---
type: permanent
note_id: "01HXYZ..."
source_id: "@Kahneman2011ThinkingFast"
literature_ref: "[[Kahneman2011ThinkingFast/LIT - Kahneman2011 - p020 - sistema-1-0001]]"
source_locator: "p.20-25 / Capítulo 1"
tags: [heurísticas, cognição, sistema-1]
origin: pipeline
llm_cost_usd: 0.002100
llm_tokens_prompt: 1800
llm_tokens_completion: 420
llm_cache_hit: false
---

> **Tese**: Heurísticas cognitivas são atalhos mentais que o Sistema 1 usa para produzir julgamentos rápidos com mínimo esforço consciente.

## Definição

Heurísticas são regras simplificadas de processamento mental...

## Intuição

Imagine que você vê uma expressão facial irritada...

## Limites

Heurísticas são adaptativas em contextos familiares, mas falham sistematicamente...

## Figuras

![[90_Assets/img-a1b2c3d4e5f6.png]]

Diagrama do Sistema 1 versus Sistema 2 (quando o candidato marca a imagem como essencial).

## Fonte

- Ref. literatura: [[Kahneman2011ThinkingFast/LIT - Kahneman2011 - p020 - sistema-1-0001]]
- Localizador: p.20-25 / Capítulo 1

## Conexões

- [[ZTL - 01HABC... - vieses-cognitivos]]: estende (heurísticas como mecanismo gerador de vieses)

<!-- zettel:auto-backlinks:start -->
- [[ZTL - 01HDEF... - racionalidade-limitada]]
<!-- zettel:auto-backlinks:end -->
```

## Estratégia anti-drift

O sistema usa múltiplas camadas de proteção contra drift (mudanças indesejadas):

| Camada | Hash | Finalidade |
|---|---|---|
| Arquivo | `file_checksum` | Detectar alteração binária |
| Extração | `extraction_checksum` | Separar mudança binária de textual |
| Capítulo | `chapter_checksum` | Reprocessar só capítulos alterados |
| Chunk | `chunk_checksum` | Reprocessar só chunks alterados |
| LLM | `llm_call_checksum` | Cache de chamadas ao LLM (Prompt 1 e Prompt 2) |
| Nota | `note_semantic_checksum` | Detectar mudança de conteúdo da nota |
| Embedding | `embedding_input_hash` | Pular re-embed quando conteúdo + modelo não mudam |

### IDs estáveis

- `source_id`: derivado do citekey (`@AutorAnoSlug`)
- `chunk_id`: `source_id::chapter_id::short_hash(chunk_checksum)` — estável se texto não muda
- `concept_id`: baseado em âncora do texto-fonte (não no output do LLM)
- `note_id`: ULID, mapeado via `concept_id → note_id` — sem duplicação

### Blocos gerenciados

Atualizações automáticas ficam dentro de marcadores HTML:
```
<!-- zettel:auto-backlinks:start -->
...conteúdo auto-gerenciado...
<!-- zettel:auto-backlinks:end -->
```
Tudo fora desses blocos é preservado — edições manuais nunca são sobrescritas.

## Recuperação: busca híbrida + GraphRAG leve

A recuperação de notas (RAG do `connect`, sugestões do `sync-manual` e o comando `ask`) combina três sinais complementares:

1. **Busca vetorial (densa)** — similaridade semântica no ChromaDB (como antes).
2. **Busca lexical BM25** — índice full-text **SQLite FTS5** no próprio `state.db` (tokenizer `unicode61` com `remove_diacritics`, então "conexao" casa "conexão"). Cobre o ponto fraco do embedding: termos técnicos exatos, siglas e nomes próprios. Palavras funcionais de altíssima frequência em PT-BR (artigos, preposições, conjunções — ex. "que", "de", "para") são filtradas da consulta antes do MATCH: sem isso, uma palavra como "que" aparece em quase toda nota do acervo e o "match" lexical deixa de significar qualquer coisa.
3. **Expansão por grafo (GraphRAG leve)** — as **conexões tipadas** já geradas pelo pipeline (tabela `note_connections`: `supports`, `contradicts`, `extends`, `depends_on`, `exemplifies`, `related`) são percorridas 1 salto a partir das notas recuperadas. Vizinhos entram no contexto ponderados por tipo de relação (`contradicts`/`extends` pesam mais — trazem informação que a similaridade vetorial **não** captura) e por decaimento por salto.

As listas densa e lexical são fundidas por **Reciprocal Rank Fusion (RRF)**, que usa apenas o *ranking* de cada id (não os scores brutos), dispensando calibração entre escalas incompatíveis (distância L2 vs. bm25). Os ids são compartilhados entre Chroma e SQLite, então a fusão é direta.

**Manter as duas abordagens**: `retrieval.mode: vector` restaura o comportamento histórico (só Chroma); `hybrid` (padrão) ativa a fusão. `graph_expansion.enabled: false` desliga o grafo. Se o SQLite não tiver FTS5, o sistema **degrada automaticamente** para busca vetorial pura (com aviso). Rode `zettel doctor` para conferir a disponibilidade de FTS5.

> **Nota de calibração**: a deduplicação semântica (`extract`) e a detecção de duplicatas do `harvest` **não** usam a busca híbrida — seus limiares (`dedupe_threshold`, `duplicate_chunk_threshold`) são calibrados sobre a distância vetorial e permanecem no vetor puro.

#### Piso de relevância absoluto

O score do RRF é **posicional**, não uma medida absoluta de relevância: a busca vetorial (kNN) sempre devolve os N vizinhos mais próximos disponíveis no corpus, mesmo que nenhum seja de fato relevante — então uma pergunta totalmente fora do acervo recebe um score no mesmo patamar de uma pergunta genuinamente respondível. `retrieval.relevance_floor` corrige isso aplicando três verificações, nesta ordem, por nota recuperada:

1. **Piso rígido** (`absolute_min_similarity`, padrão `0.15`): se a similaridade coseno (derivada da distância vetorial) estiver abaixo desse valor, a nota é **sempre** rejeitada — nem um match lexical (BM25) a salva. É uma proteção contra o caso patológico de uma nota semanticamente quase ortogonal que por acaso compartilha um termo com a pergunta. É deliberadamente bem mais baixo que o piso normal, para não atrapalhar o caso de uso principal do BM25 (siglas/termos técnicos que o embedding às vezes subestima, mas cuja similaridade raramente é próxima de zero).
2. **Bypass por match lexical forte** (`bm25_hit_bypasses_floor: true` + `bm25_bypass_max_rank`, padrão `5`): se a nota também apareceu bem posicionada no ranking BM25 (posição ≤ `bm25_bypass_max_rank`), ela passa direto, **independente** da similaridade vetorial — sobreposição de termo real, bem ranqueada, já é evidência suficiente por si. Um match lexical **fraco** (achado só na cauda do pool de candidatos) não conta para o bypass; a nota cai para a checagem normal de similaridade.
3. **Piso normal de similaridade** (`min_vector_similarity`, padrão `0.70`, calibrado empiricamente neste projeto — ajuste para seu corpus/modelo de embedding): critério padrão para notas sem match lexical forte.

Resultados abaixo do piso não alimentam a resposta, mas continuam visíveis em `--show-context` para fins de transparência/depuração — junto com o **motivo exato** da decisão (ex.: `similaridade 0.67 abaixo do piso (0.70)` ou `match lexical forte (bm25 rank 3 <= 5)`).

### Perguntar ao acervo (`zettel ask`)

```bash
python -m zettel ask "Como heurísticas geram vieses?" --show-context
```

O comando recupera as notas relevantes (híbrido + grafo + piso de relevância), monta um contexto com citações e pede ao LLM uma resposta em PT-BR **baseada apenas no acervo** (se não houver evidência, ele diz isso, em vez de alucinar). Cada afirmação cita o `[[wikilink]]` exato da nota-fonte. Quando nenhuma nota recuperada passa do piso de relevância, o LLM **nem chega a ser chamado** — a resposta padrão de "não encontrei evidência" é determinística.

Com `--show-context`, o comando mostra dois relatórios extras:

- **Parâmetros de recuperação**: todos os valores configurados usados naquela consulta (modo, top-k, `rrf_k`, os três limiares do piso de relevância, e os parâmetros de expansão por grafo) — para você conferir exatamente sob quais regras a recuperação rodou, sem precisar abrir o `config.yaml`.
- **Notas recuperadas**: o top-k bruto, com Score RRF, Similaridade, Rank BM25 (posição no ranking lexical, ou "-" se não casou), Salto (0 = achada na busca, ≥1 = vizinha de grafo), se foi **usada** e o **motivo** exato da decisão (ex.: `match lexical forte (bm25 rank 3 <= 5)` ou `similaridade 0.67 abaixo do piso (0.70)`) — para auditar a recuperação mesmo quando a resposta é negativa.

A resposta pode ser salva como nota `.md` em `00_Inbox/` (`--save` ou `--save-to`), com frontmatter e uma seção **Fontes consultadas** que registra, para cada nota efetivamente usada, o wikilink, a origem na recuperação (busca vs. conexão de grafo, com o tipo da relação), o score e a fonte bibliográfica — rastreabilidade completa de onde veio cada informação.

### Gerar artigo a partir do vault (`zettel article`)

```bash
python -m zettel article "Tecnicas de Prompt Engineering" --style blog
python -m zettel article "Grafos de conhecimento e LLMs" --style academic --personality serious_academic --save
python -m zettel article "RAG" --outline-only
```

Orquestrado por **LangGraph** (`zettel/article_graph.py`), diferente do `ask` (QA curto). Fluxo:

1. **Query enricher** — expande o tema em varias queries semanticas
2. **Busca acumulativa** — Retriever hibrido + merge por `note_id` (queries extras do usuario somam, nao substituem)
3. **HITL de contexto** — aprovar pool, pedir queries extras (`e`) ou abortar
4. **Outline** — LLM + aprovacao interativa (`a` / `r`+feedback / `q`)
5. **Draft por secao** — blog (mencoes leves) ou academico (ABNT autor-data) + anti-padroes de prosa
6. **Assemble** — figuras, "Para saber mais" ou "Referencias" ABNT, "Origem no vault"
7. **Personality** — reescrita estilistica via `config/personalities.yaml` (`neutral` = no-op)
8. **Judge** — avalia fidelidade/cobertura/refs/naturalidade; rejeicao re-drafta ate `max_judge_iterations`

Flags: `--personality`, `--style-notes`, `--skip-context-review`, `--skip-judge`, `--max-judge-iterations`, `--outline-only`, `--save` / `--save-to`. Saida em `00_Inbox/ART - ....md` (nao indexa no Chroma).

### Fechando o ciclo do grafo (notas manuais)

Notas escritas à mão no Obsidian também alimentam o grafo: no `sync-manual`, os `[[wikilinks]]` presentes **no corpo** de uma nota permanente (fora dos blocos gerenciados `auto-connections`/`auto-backlinks`, que são sugestões automáticas, não conexões aceitas) são persistidos como arestas `related`. Uma aresta já tipada nunca é rebaixada. Use `zettel sync-manual --rebuild-graph` para re-derivar essas arestas de todo o vault a partir dos corpos já persistidos no SQLite.

## Retenção e reconstrução

O **SQLite é a fonte de verdade durável**: além do estado do pipeline, ele persiste tudo que é caro de reproduzir — o texto extraído completo de cada fonte, o corpo integral das notas LIT/ZTL/MOC (com frontmatter), os candidatos completos (`candidate_json`) e as descrições de imagens. Os **embeddings não** são guardados no SQLite (são baratos de recomputar via API ou modelo local).

Como consequência:

- **`zettel reindex`** reconstrói o ChromaDB inteiro a partir do SQLite, sem nenhuma chamada de LLM e sem reescrever o vault. O índice vetorial passa a ser um cache descartável. Um `reindex` completo também reconstrói o índice lexical FTS5 (`fts_notes`/`fts_chunks`), igualmente descartável. Após trocar `embedding.provider`/`model`, use **`--force`** (o CLI também detecta o drift e força o reset sob confirmação).
- **`zettel rebuild --what vault`** recria os arquivos `.md` do vault a partir dos corpos persistidos, também sem LLM. Nunca sobrescreve um arquivo existente sem `--force`, e nunca sobrescreve uma nota `origin: manual` (mesmo com `--force`).
- **`zettel rechunk`** re-aplica a configuração de chunking atual a partir do texto extraído persistido, sem reprocessar o arquivo original; completa capítulos faltantes após harvest interrompido e re-vincula imagens aos capítulos corretos. Com `--dump-chunks`, grava um markdown com todos os chunks (texto + metadados) em `data/cache/chunk-dumps/` (ou `--dump-dir`).
- **`zettel dump-chunks`** reexporta os chunks já persistidos no SQLite como markdown, sem rechunkar nem chamar o LLM. Use para inspecionar cortes, `section_path`, páginas e overlap antes de mudar `chunk_size` / `chunk_overlap` / `min_section_chars`.
- **`zettel dump-extraction`** reexporta o Markdown extraído (`sources.extracted_text`: saída do Docling em PDF, ou o corpo MD nativo) com headings H1–H6 intactos, sem rerodar o extrator. `harvest --dump-extraction` grava o mesmo arquivo assim que o texto é persistido (antes dos embeddings). Default: `data/cache/extraction-dumps/` (ou `--dump-extraction-dir` / `--dump-dir` no comando dedicado).

## Notas manuais e proveniência

Notas criadas à mão no Obsidian são adotadas pelo pipeline com **`zettel sync-manual`**, que varre as quatro pastas (`10_Sources`, `20_Literature`, `30_Permanent`, `40_MOCs`):

- Notas sem `note_id`/`moc_id`/`source_id` recebem um id/citekey gerado, injetado no frontmatter.
- Cada nota ganha uma flag de proveniência `origin: manual | pipeline` (no frontmatter e no banco), permitindo distinguir o que foi escrito à mão do que foi gerado.
- SRC e LIT manuais deixam de ficar órfãos: são registrados no SQLite (e SRC é indexado no Chroma); uma LIT sem fonte resolvível cria uma fonte manual mínima para se vincular.

## Testes

```bash
# Rodar todos os testes
python -m pytest tests/ -v

# Rodar teste específico
python -m pytest tests/test_hashing.py -v
```

## Resolução de problemas

### "Docling não instalado"
```bash
pip install docling
# Ou use PyMuPDF como alternativa:
pip install pymupdf
# E altere no config.yaml: pdf_extractor: pymupdf
```

### "Nenhum cluster encontrado" no garden
- Voce precisa de pelo menos `min_cluster_size` notas permanentes (padrao: 5)
- Ajuste com `--min-cluster-size 3`

### Chunks ficam "pending" apos extract
- Verifique os logs para erros de LLM
- Execute `python -m zettel doctor` para validar dependencias
- Verifique se a API key esta configurada

### Fonte com pouco conteudo / conceitos "sumiram" apos harvest
- Harvest interrompido no meio pode deixar so os primeiros capitulos no SQLite, enquanto o texto completo ja esta em `extracted_text`
- Sintoma: `doctor`/`status` reportam **chunking incompleto**; imagens apontam para `chapter_id` sem chunks
- Recuperacao: `python -m zettel rechunk --source-id @Citekey` e depois `extract` + `connect`
- O proximo `harvest` do mesmo arquivo tambem tenta completar automaticamente

### ZTL sem secao Figuras
- Figuras dependem de `relevant_image_ids` no candidato (Prompt 1) ou do **fallback** (path `90_Assets/...` presente no texto do chunk)
- Se a imagem esta em outro capitulo/chunk, o fallback nao a anexa — o LLM precisa marca-la via `{images_context}` do mesmo capitulo
- Confira o bloco `## Imagens` da LIT e se o asset tem `status: described`

### Poucos candidatos aprovados apos extract
- O sistema agora filtra candidatos por qualidade. Verifique os logs por mensagens de "candidatos rejeitados"
- Ajuste os thresholds em `config/config.yaml` na secao `extraction:`:
  - `min_relevance_score: 2` para ser mais permissivo (padrao: 3)
  - `min_thesis_words: 3` para aceitar teses mais curtas
  - `min_definition_words: 5` para aceitar definicoes mais curtas
  - `require_anchor_quote: false` para nao exigir citacao-ancora

### MOCs sendo rejeitados
- Se `strict_topics: true`, MOCs cujo `topic` nao casa com uma **categoria** de `config/moc_topics.yaml` serao rejeitados
- Verifique os logs por "MOC rejeitado: topico ... fora da lista"
- Opcoes:
  - Adicione/ajuste a categoria em `config/moc_topics.yaml`
  - Use `strict_topics: false` para aprovar todos os topicos (com aviso no log)
  - Confirme que `gardener.topics_path` aponta para o YAML correto

### MOCs duplicados
- O sistema agora detecta MOCs existentes com o mesmo topico e atualiza incrementalmente em vez de criar duplicados
- Se ainda houver duplicatas de execucoes anteriores, remova manualmente os MOCs duplicados do vault e do StateDB
- Verifique os logs por "MOC existente para topico" para confirmar que o update incremental esta funcionando

## Personalizacao dos prompts

Os prompts em `prompts/` sao templates Markdown com placeholders `{variavel}`. Voce pode edita-los para ajustar:

- **Estilo das notas**: mais academico, mais informal, etc.
- **Idioma**: altere para outro idioma (ajuste tambem `language` no config)
- **Profundidade**: mais ou menos detalhes por nota
- **Tags**: criterios para sugestao de tags
- **Seletividade**: regras de relevancia e filtragem em `literature_note.md`
- **Imagens → candidatos/ZTL**: criterios de `relevant_image_ids` e extracao a partir de diagramas em `literature_note.md`; tom da descricao em `image_description.md`; uso de figuras no Prompt 2 em `permanent_note.md`
- **Taxonomia de MOCs**: edite `config/moc_topics.yaml` (pilares, categorias e topicos)
- **Dominio e categorias**: ajuste `{domain}` e `{allowed_topics_section}` em `moc_generation.md` (preenchidos automaticamente a partir do YAML)
- **Classificacao incremental**: edite `moc_incremental.md` para ajustar como novas notas sao classificadas em MOCs existentes

O sistema detecta automaticamente quando um prompt muda e reprocessa apenas os artefatos afetados.

### Taxonomia de topicos para MOCs

O arquivo [`config/moc_topics.yaml`](config/moc_topics.yaml) e a **fonte unica** da taxonomia (pilar > categoria > topicos). As **categorias** sao a whitelist do campo `topic` do MOC; pilares agrupam; topicos-folha orientam subsecoes no prompt.

Para personalizar:

1. Edite `config/moc_topics.yaml`
2. Ajuste `gardener.topics_path` em `config/config.yaml` se o arquivo estiver em outro caminho
3. Ajuste `domain` para refletir a area do seu acervo

Se `strict_topics: true` (padrao), MOCs com `topic` fora das categorias serao rejeitados. Use `strict_topics: false` para permitir topicos fora da lista (com aviso no log).

## Licença

MIT
