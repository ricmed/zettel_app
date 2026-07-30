# Zettelkasten — Pipeline Automatizado de Geração de Notas

Sistema em Python que lê arquivos (PDF, Markdown) e gera **Notas de Literatura** e **Notas Permanentes** seguindo rigorosamente o método Zettelkasten, com saída compatível com **Obsidian**.

## Visão Geral

O pipeline automatiza o ciclo completo do Zettelkasten:

```
Arquivo (PDF/MD)
    ↓ harvest
Texto extraído → Chunks → Notas SRC + LIT
    ↓ extract
Conceitos atômicos (candidatos a notas permanentes)
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
│   ├── config.py            # Carregamento e validação de config
│   ├── schemas.py           # Modelos Pydantic
│   ├── hashing.py           # Hashing canônico em camadas
│   ├── state.py             # SQLite — estado incremental
│   ├── vault.py             # I/O do vault Obsidian (frontmatter, blocos gerenciados)
│   ├── index.py             # ChromaDB — índice vetorial
│   ├── bibliography.py      # Metadados bibliograficos ABNT (tipos, inferencia, formatacao)
│   ├── harvester.py         # Fase 1: ingestão e chunking estrutural (+ rechunk)
│   ├── extractor.py         # Fase 2: extração de conceitos via LLM
│   ├── connector.py         # Fase 3: geração de notas permanentes
│   ├── gardener.py          # Fase 4: clusterização e MOCs
│   ├── retrieval.py         # Recuperação híbrida (vetor + BM25) com fusão RRF
│   ├── graph.py             # Expansão por grafo sobre as conexões tipadas (GraphRAG leve)
│   ├── ask.py               # Comando `ask`: QA sobre o vault com citações
│   ├── assets.py            # Extração e descrição multimodal de imagens
│   ├── rebuild.py           # Reconstrução do Chroma (reindex) e do vault (rebuild) a partir do SQLite
│   └── sync.py              # Sincronização de notas manuais (SRC/LIT/ZTL/MOC)
├── config/
│   └── config.yaml          # Configuração principal
├── prompts/                     # Templates de prompts para o LLM
│   ├── bibliographic_metadata.md # Extracao de metadados bibliograficos (ABNT)
│   ├── literature_note.md       # Prompt 1: extracao de conceitos (c/ relevance_score)
│   ├── permanent_note.md        # Prompt 2: geracao de nota permanente
│   ├── dedupe_decision.md       # Decisao de deduplicacao
│   ├── relationship.md          # Classificacao de relacionamentos
│   ├── moc_generation.md        # Geracao de MOCs (c/ dominio e topicos)
│   ├── moc_incremental.md       # Classificacao incremental de notas em MOC existente
│   ├── moc_topics_taxonomy.md   # Taxonomia de topicos para MOCs (24 categorias)
│   ├── ptbr_guard.md            # Guardrail de idioma PT-BR
│   ├── ask.md                   # Resposta a perguntas sobre o vault
│   └── image_description.md     # Descricao multimodal de imagens (PT-BR)
├── data/
│   ├── inbox/               # Arquivos para processar (drop zone)
│   ├── processed/           # Arquivos já processados
│   ├── cache/               # Cache de candidatos e intermediários
│   ├── chroma/              # ChromaDB (persistente)
│   └── state.db             # SQLite (estado do pipeline)
├── vault/                   # Vault do Obsidian (criado pelo init)
│   ├── 00_Inbox/
│   ├── 10_Sources/          # Notas bibliográficas (SRC)
│   ├── 20_Literature/       # Notas de literatura (LIT)
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
| `langchain-ollama` | Usar modelos locais via Ollama |
| `pymupdf` | Extração PDF alternativa (mais leve que Docling) |
| `umap-learn` | Clusterização avançada para MOCs |
| `hdbscan` | Clusterização densa para MOCs |

## Configuração

Edite `config/config.yaml`:

```yaml
# Caminhos
vault_path: ./vault
inbox_path: ./data/inbox

# LLM
llm:
  provider: openai           # openai | anthropic | ollama
  model: gpt-4o-mini
  temperature: 0             # 0 = deterministico (reduz drift)

# Embeddings
embedding:
  provider: openai
  model: text-embedding-3-small
  allow_fallback: false      # false = erro se faltar API key (evita vetores de 384 dims silenciosos)

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

# Linkagem
linking:
  topk: 5                    # notas similares para RAG
  dedupe_threshold: 0.85     # limiar de deduplicacao

# Harvest (duplicatas + metadados bibliograficos ABNT)
harvest:
  duplicate_chunk_threshold: 0.88
  duplicate_sample_size: 5
  non_interactive_duplicate_action: skip   # skip | continue | abort
  biblio_confidence_threshold: 0.7         # abaixo disso, pede confirmacao do tipo
  biblio_llm_enabled: true                 # enriquece metadados via LLM apos heuristicas
  biblio_text_sample_chars: 5000           # amostra inicial (capa/folha de rosto) enviada ao LLM

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
  ask:
    topk: 8                  # notas semente do comando `ask`
    max_context_notes: 8     # teto de notas no contexto do LLM
    max_chars_per_note: 1500 # truncagem do corpo de cada nota no contexto

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
  strict_topics: true        # rejeitar MOCs fora da lista de topicos
  allowed_topics:             # lista de topicos permitidos para MOCs
    - "Machine Learning Classico"
    - "Deep Learning e Modelos Neurais"
    - "NLP Moderno e LLMs"
    # ... (24 topicos no total — veja config.yaml)

# PDF
pdf_extractor: docling       # docling | pymupdf
```

### Provedores de LLM suportados

**OpenAI** (padrão):
```yaml
llm:
  provider: openai
  model: gpt-4o-mini    # ou gpt-4o, gpt-4-turbo
```
Requer: `OPENAI_API_KEY`

**Anthropic**:
```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
```
Requer: `ANTHROPIC_API_KEY` e `pip install langchain-anthropic`

**Ollama (local)**:
```yaml
llm:
  provider: ollama
  model: llama3.1        # ou qualquer modelo local
```
Requer: Ollama rodando localmente e `pip install langchain-ollama`

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
# Inicializar vault e dependências
python -m zettel init

# Apaga os banco de dados, mas não apaga o vault
python -m zettel init --reset

# Escanear inbox e processar arquivos → SRC + LIT + chunks
python -m zettel harvest

# Harvest nao-interativo (duplicatas usam o default da config)
python -m zettel harvest --yes
python -m zettel harvest --skip-duplicates   # sempre pula suspeitas de duplicata
python -m zettel harvest --force             # trata suspeitas como nova fonte
# Bibliografia incompleta: --yes sozinho ABORTA o arquivo; use --skip-biblio para seguir parcial
python -m zettel harvest --yes --skip-biblio

# Extrair conceitos dos chunks via LLM
python -m zettel extract

# Gerar notas permanentes a partir dos conceitos
python -m zettel connect

# Clusterizar notas e gerar MOCs
python -m zettel garden

# Perguntar ao acervo (QA com recuperacao hibrida + expansao por grafo)
python -m zettel ask "O que e RAG?"
python -m zettel ask "O que e RAG?" --show-context        # mostra as notas recuperadas
python -m zettel ask "O que e RAG?" --no-graph            # so busca hibrida, sem grafo
python -m zettel ask "O que e RAG?" --mode vector         # so busca vetorial (legado)
python -m zettel ask "O que e RAG?" --save                # salva a resposta em .md (00_Inbox)
python -m zettel ask "O que e RAG?" --save-to nota.md     # salva em caminho especifico

# Sincronizar notas manuais do vault com o índice (SRC, LIT, ZTL e MOCs)
python -m zettel sync-manual

# Re-derivar arestas do grafo a partir dos wikilinks no corpo das notas manuais
python -m zettel sync-manual --rebuild-graph

# Re-chunkar fontes com a config atual (a partir do texto ja extraido, sem reprocessar o arquivo).
# Tambem completa harvest interrompido e re-resolve o chapter_id das imagens.
python -m zettel rechunk --all
python -m zettel rechunk --source-id @AutorAnoTitulo

# Reconstruir o ChromaDB a partir do SQLite (sem chamadas de LLM)
python -m zettel reindex
python -m zettel reindex --collection chunks --force

# Reconstruir o vault (.md) e/ou o Chroma a partir do SQLite, sem reprocessar LLM
python -m zettel rebuild --what vault          # recria os .md (nunca sobrescreve notas manuais)
python -m zettel rebuild --what all --dry-run  # simula vault + chroma

# Reprocessar itens com falha
python -m zettel retry-failed                  # chunks com falha -> pending
python -m zettel retry-failed --assets         # imagens com falha de descricao -> pending

# Ver estatisticas do pipeline (alerta se houver chunking incompleto)
python -m zettel status

# Verificar configuracao, dependencias e cobertura de capitulos vs. texto extraido
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
   - Em modo interativo, se o tipo estiver incerto ou faltarem campos obrigatórios, o CLI **solicita** apenas o que falta e mostra um preview da referência ABNT
   - Em modo não-interativo (`--yes`), bibliografia incompleta **pula o arquivo**, salvo `--skip-biblio` (segue com parcial + aviso)
   - Persiste `document_type`, `bibliography_json` e `abnt_reference` no SQLite; a nota **SRC** recebe os campos separados no frontmatter **e** a string `abnt_reference` pronta para citar
5. Gera **citekey** determinístico: `@SobrenomeAnoTituloSlug` (a partir dos metadados já enriquecidos)
6. Cria notas **SRC** (bibliográfica) em `10_Sources/` e **LIT** (literatura-mestre) em `20_Literature/`
7. Divide o texto em **capítulos** (headings H1/H2) e depois em **seções estruturais** (H3-H6) com um `section_path` hierárquico ("Capítulo > Subseção > Subsubseção"); seções menores que `min_section_chars` são fundidas, e seções grandes ainda passam pelo RecursiveCharacterTextSplitter
8. Indexa chunks no ChromaDB e registra no SQLite (com o texto extraído completo persistido, viabilizando `rechunk`/`rebuild` sem reprocessar o arquivo)
9. Se `images.enabled`, extrai **imagens** do PDF (Docling) e do Markdown (referências locais), salva em `90_Assets/` com nome por hash de conteúdo, reescreve as referências no texto e registra os assets no SQLite (com `chapter_id` resolvido pelo path no texto do capítulo)
10. **Cobertura de capítulos**: ao final, valida se todos os capítulos H1/H2 do texto extraído foram persistidos. Se um harvest anterior ficou pela metade (ex.: interrompido), o próximo `harvest` (mesmo com arquivo inalterado) ou um `rechunk` **completa** os capítulos faltantes e re-resolve o `chapter_id` das imagens. O `doctor`/`status` alertam quando a cobertura está incompleta.

### Fase 2 — Extract (Extracao)

0. Se houver imagens pendentes, descreve cada uma com um **LLM multimodal** (usando o texto ao redor como contexto), com cache determinístico; as descrições alimentam o Prompt 1 e o bloco `auto-imagens` da nota LIT
1. Para cada chunk pendente, chama o LLM com o **Prompt 1** (literature_note.md), incluindo `{images_context}` (descrições das imagens do capítulo)
2. O LLM retorna:
   - Resumo do chunk
   - Conceitos-chave
   - Lista de **candidatos atomicos** a notas permanentes (com tese, definicao, ancora, localizador, **relevance_score**, **relevant_image_ids**)
   - Pode gerar candidatos a partir da **descricao de uma figura** quando o texto do chunk for fino mas o diagrama trouxer o conceito
3. Valida a saida com Pydantic; se falhar, tenta retry
4. Anexa resultados a nota **LIT** (via blocos gerenciados)
5. **Filtragem de qualidade** (duas camadas):
   - **Camada 1 (LLM)**: O prompt instrui o LLM a atribuir um `relevance_score` (1-5) e retornar `candidates: []` para chunks sem conceitos relevantes
   - **Camada 2 (Codigo)**: `_filter_candidates()` aplica regras estruturais configuraveis:
     - `relevance_score` >= threshold (padrao: 3)
     - Tese com minimo de palavras (padrao: 5)
     - Definicao com minimo de palavras (padrao: 10)
     - Presenca de citacao-ancora (configuravel)
6. Se `relevant_image_ids` vier vazio, **fallback deterministico**: anexa assets cujo path `90_Assets/...` aparece no texto do chunk
7. Executa **deduplicacao semantica**: compara candidatos aprovados com notas existentes via ChromaDB
8. O LLM decide: `create_new` | `ignore` | `refine_existing` | `merge`
9. Persiste cada candidato completo no SQLite (`concepts.candidate_json` + `status`), de modo que o `connect` possa rodar a partir do banco mesmo sem o `data/cache/candidates.json`

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

1. Carrega embeddings de todas as notas permanentes
2. Reduz dimensionalidade com **UMAP** e clusteriza com **HDBSCAN** (ou KMeans como fallback)
3. Extrai termos representativos via **TF-IDF**
4. Para cada cluster com notas suficientes, gera um **MOC** via LLM:
   - O prompt inclui o **dominio** do acervo e a **lista de topicos permitidos**
   - Uma **taxonomia detalhada** (24 categorias com subtopicos) e carregada de `prompts/moc_topics_taxonomy.md` como referencia para o LLM
   - O LLM deve mapear o cluster para um topico da lista e justificar em `topic_justification`
5. **Validacao de topico** pos-geracao:
   - Substring match bidirecional contra a lista de `allowed_topics`
   - Se `strict_topics: true` e sem match: MOC rejeitado (com warning no log)
   - Se `strict_topics: false` e sem match: MOC aprovado (com info no log)
6. **Atualizacao incremental de MOCs**: se ja existe um MOC com o mesmo topico, em vez de criar um duplicado:
   - Parseia a estrutura do MOC existente (subsecoes e notas)
   - Identifica quais notas do cluster sao realmente novas
   - Chama o LLM com `moc_incremental.md` para classificar cada nota nova na subsecao adequada (ou ignorar se nao se encaixa)
   - Reconstroi o MOC com as notas novas inseridas nas subsecoes corretas
   - Pode criar novas subsecoes se o LLM sugerir
7. Se nao existe MOC para o topico, cria arquivo **MOC** novo em `40_MOCs/` com subsecoes e links organizados

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
---

# Thinking, Fast and Slow

**Autores**: Daniel Kahneman
**Ano**: 2011
**Tipo documental**: livro
**Tipo de arquivo**: pdf

## Referencia ABNT

KAHNEMAN, Daniel. Thinking, Fast and Slow. 1. ed. New York: Farrar, Straus and Giroux, 2011.

## Nota de Literatura
[[LIT - @Kahneman2011ThinkingFast - thinking-fast-and-slow]]
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
### Nota de Literatura (LIT)

```markdown
---
type: literature
source_id: "@Kahneman2011ThinkingFast"
language: pt-BR
origin: pipeline
---

# Thinking, Fast and Slow

## Resumo
...

## Conceitos-chave
...

## Imagens
<!-- zettel:auto-imagens:start -->
![[90_Assets/img-a1b2c3d4e5f6.png]]

Gráfico de barras comparando o tempo de resposta do Sistema 1 e do Sistema 2.
<!-- zettel:auto-imagens:end -->

<!-- zettel:auto-chunks-log:start -->
### Chunk: @Kahneman2011::ch001::abc12345
**Resumo**: O Sistema 1 opera de forma automática e rápida...
**Conceitos**: heurísticas, vieses cognitivos
**Candidatos a notas permanentes**:
- Heurísticas cognitivas são atalhos mentais automáticos
<!-- zettel:auto-chunks-log:end -->
```

### Nota Permanente (ZTL)

```markdown
---
type: permanent
note_id: "01HXYZ..."
source_id: "@Kahneman2011ThinkingFast"
literature_ref: "[[LIT - @Kahneman2011ThinkingFast - thinking-fast-and-slow]]"
source_locator: "p.20-25 / Capítulo 1"
tags: [heurísticas, cognição, sistema-1]
origin: pipeline
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

- Ref. literatura: [[LIT - @Kahneman2011ThinkingFast - thinking-fast-and-slow]]
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

### Fechando o ciclo do grafo (notas manuais)

Notas escritas à mão no Obsidian também alimentam o grafo: no `sync-manual`, os `[[wikilinks]]` presentes **no corpo** de uma nota permanente (fora dos blocos gerenciados `auto-connections`/`auto-backlinks`, que são sugestões automáticas, não conexões aceitas) são persistidos como arestas `related`. Uma aresta já tipada nunca é rebaixada. Use `zettel sync-manual --rebuild-graph` para re-derivar essas arestas de todo o vault a partir dos corpos já persistidos no SQLite.

## Retenção e reconstrução

O **SQLite é a fonte de verdade durável**: além do estado do pipeline, ele persiste tudo que é caro de reproduzir — o texto extraído completo de cada fonte, o corpo integral das notas LIT/ZTL/MOC (com frontmatter), os candidatos completos (`candidate_json`) e as descrições de imagens. Os **embeddings não** são guardados no SQLite (são baratos de recomputar via API).

Como consequência:

- **`zettel reindex`** reconstrói o ChromaDB inteiro a partir do SQLite, sem nenhuma chamada de LLM. O índice vetorial passa a ser um cache descartável. Um `reindex` completo também reconstrói o índice lexical FTS5 (`fts_notes`/`fts_chunks`), igualmente descartável.
- **`zettel rebuild --what vault`** recria os arquivos `.md` do vault a partir dos corpos persistidos, também sem LLM. Nunca sobrescreve um arquivo existente sem `--force`, e nunca sobrescreve uma nota `origin: manual` (mesmo com `--force`).
- **`zettel rechunk`** re-aplica a configuração de chunking atual a partir do texto extraído persistido, sem reprocessar o arquivo original; completa capítulos faltantes após harvest interrompido e re-vincula imagens aos capítulos corretos.

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
- Se `strict_topics: true`, MOCs com topicos fora da lista `allowed_topics` serao rejeitados
- Verifique os logs por "MOC rejeitado: topico ... fora da lista"
- Opcoes:
  - Adicione o topico a `allowed_topics` em `config.yaml`
  - Use `strict_topics: false` para aprovar todos os topicos (com aviso no log)
  - Edite a taxonomia em `prompts/moc_topics_taxonomy.md` para cobrir mais areas

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
- **Taxonomia de MOCs**: edite `moc_topics_taxonomy.md` para ajustar as categorias e subtopicos disponiveis para organizacao dos MOCs
- **Dominio e topicos**: ajuste `{domain}` e `{allowed_topics_section}` em `moc_generation.md` (preenchidos automaticamente via config)
- **Classificacao incremental**: edite `moc_incremental.md` para ajustar como novas notas sao classificadas em MOCs existentes

O sistema detecta automaticamente quando um prompt muda e reprocessa apenas os artefatos afetados.

### Taxonomia de topicos para MOCs

O arquivo `prompts/moc_topics_taxonomy.md` contem uma taxonomia com **24 categorias de nivel superior** e seus subtopicos. Ele e carregado automaticamente no prompt de geracao de MOCs como referencia para o LLM. Para personalizar:

1. Edite `prompts/moc_topics_taxonomy.md` com suas categorias e subtopicos
2. Atualize `allowed_topics` em `config/config.yaml` com os nomes das categorias de nivel superior
3. Ajuste `domain` para refletir a area do seu acervo

Se `strict_topics: true` (padrao), MOCs com topicos fora da lista serao rejeitados. Use `strict_topics: false` para permitir topicos fora da lista (com aviso no log).

## Licença

MIT
