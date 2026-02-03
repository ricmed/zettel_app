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
│   ├── harvester.py         # Fase 1: ingestão e chunking
│   ├── extractor.py         # Fase 2: extração de conceitos via LLM
│   ├── connector.py         # Fase 3: geração de notas permanentes
│   ├── gardener.py          # Fase 4: clusterização e MOCs
│   └── sync.py              # Sincronização de notas manuais
├── config/
│   └── config.yaml          # Configuração principal
├── prompts/                     # Templates de prompts para o LLM
│   ├── literature_note.md       # Prompt 1: extracao de conceitos (c/ relevance_score)
│   ├── permanent_note.md        # Prompt 2: geracao de nota permanente
│   ├── dedupe_decision.md       # Decisao de deduplicacao
│   ├── relationship.md          # Classificacao de relacionamentos
│   ├── moc_generation.md        # Geracao de MOCs (c/ dominio e topicos)
│   ├── moc_incremental.md       # Classificacao incremental de notas em MOC existente
│   ├── moc_topics_taxonomy.md   # Taxonomia de topicos para MOCs (24 categorias)
│   └── ptbr_guard.md            # Guardrail de idioma PT-BR
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
│   └── 90_Assets/
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

# Chunking
chunking:
  chunk_size: 1000           # tokens por chunk
  chunk_overlap: 200         # sobreposicao

# Linkagem
linking:
  topk: 5                    # notas similares para RAG
  dedupe_threshold: 0.85     # limiar de deduplicacao

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

# Extrair conceitos dos chunks via LLM
python -m zettel extract

# Gerar notas permanentes a partir dos conceitos
python -m zettel connect

# Clusterizar notas e gerar MOCs
python -m zettel garden

# Sincronizar notas manuais do vault com o índice
python -m zettel sync-manual

# Ver estatísticas do pipeline
python -m zettel status

# Verificar configuração e dependências
python -m zettel doctor
```

### Opções comuns

```bash
# Usar arquivo de configuração alternativo
python -m zettel run-all --config ./minha_config.yaml

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
2. Calcula checksum SHA-256 de cada arquivo (pula inalterados)
3. Extrai texto usando **Docling** (PDF) ou parser nativo (Markdown)
4. Gera **citekey** determinístico: `@SobrenomeAnoTituloSlug`
5. Cria notas **SRC** (bibliográfica) em `10_Sources/` e **LIT** (literatura-mestre) em `20_Literature/`
6. Divide o texto em **capítulos** (por headings) e depois em **chunks semânticos** (RecursiveCharacterTextSplitter)
7. Indexa chunks no ChromaDB e registra no SQLite

### Fase 2 — Extract (Extracao)

1. Para cada chunk pendente, chama o LLM com o **Prompt 1** (literature_note.md)
2. O LLM retorna:
   - Resumo do chunk
   - Conceitos-chave
   - Lista de **candidatos atomicos** a notas permanentes (com tese, definicao, ancora, localizador, **relevance_score**)
3. Valida a saida com Pydantic; se falhar, tenta retry
4. Anexa resultados a nota **LIT** (via blocos gerenciados)
5. **Filtragem de qualidade** (duas camadas):
   - **Camada 1 (LLM)**: O prompt instrui o LLM a atribuir um `relevance_score` (1-5) e retornar `candidates: []` para chunks sem conceitos relevantes
   - **Camada 2 (Codigo)**: `_filter_candidates()` aplica regras estruturais configuraveis:
     - `relevance_score` >= threshold (padrao: 3)
     - Tese com minimo de palavras (padrao: 5)
     - Definicao com minimo de palavras (padrao: 10)
     - Presenca de citacao-ancora (configuravel)
6. Executa **deduplicacao semantica**: compara candidatos aprovados com notas existentes via ChromaDB
7. O LLM decide: `create_new` | `ignore` | `refine_existing` | `merge`

### Fase 3 — Connect (Conexão)

1. Para cada candidato aprovado, busca **top-k notas similares** (RAG) — apenas para conexões
2. Chama o LLM com o **Prompt 2** (permanent_note.md):
   - Conceito + contexto RAG → nota permanente completa
3. Valida idioma PT-BR (guardrail automático)
4. Cria arquivo **ZTL** em `30_Permanent/` com:
   - Frontmatter YAML (type, note_id, source_id, tags, etc.)
   - Corpo: Tese → Definição → Intuição → Exemplo → Limites → Fonte → Conexões
5. Atualiza **backlinks** nas notas relacionadas via blocos gerenciados:
   ```
   <!-- zettel:auto-backlinks:start -->
   - [[ZTL - ID - titulo]]
   <!-- zettel:auto-backlinks:end -->
   ```
6. Indexa no ChromaDB e registra no SQLite

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

```markdown
---
type: source
source_id: "@Kahneman2011ThinkingFast"
title: "Thinking, Fast and Slow"
author: ["Daniel Kahneman"]
year: 2011
origin_type: pdf
checksum: "a1b2c3..."
---

# Thinking, Fast and Slow

**Autores**: Daniel Kahneman
**Ano**: 2011

## Nota de Literatura
[[LIT - @Kahneman2011ThinkingFast - thinking-fast-and-slow]]
```

### Nota de Literatura (LIT)

```markdown
---
type: literature
source_id: "@Kahneman2011ThinkingFast"
language: pt-BR
---

# Thinking, Fast and Slow

## Resumo
...

## Conceitos-chave
...

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
---

> **Tese**: Heurísticas cognitivas são atalhos mentais que o Sistema 1 usa para produzir julgamentos rápidos com mínimo esforço consciente.

## Definição

Heurísticas são regras simplificadas de processamento mental...

## Intuição

Imagine que você vê uma expressão facial irritada...

## Limites

Heurísticas são adaptativas em contextos familiares, mas falham sistematicamente...

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
| LLM | `llm_call_checksum` | Cache de chamadas ao LLM |
| Nota | `note_semantic_checksum` | Re-embed apenas quando conteúdo muda |

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
