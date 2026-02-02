A seguir está um **fluxo detalhado, em bullet points**, para um sistema em **Python** que gera **Notas de Literatura** e **Notas Permanentes** de forma **bem rígida no Zettelkasten**, usando **Docling + LangChain + ChromaDB** (e adicionando o que for necessário para fechar o ciclo: controle de estado, ids, sync de notas manuais, clustering/MOCs, etc.).

> Onde eu mencionar capacidades específicas:
>
> * **Docling**: conversão/extração e opção de VLM para descrições. ([GitHub][1])
> * **LangChain**: splitters com `chunk_size` e `chunk_overlap`. ([Docs do LangChain][2])
> * **ChromaDB**: coleções, necessidade de IDs únicos e persistência. ([Chroma Docs][3])
> * **Obsidian**: frontmatter/Properties em YAML no topo do arquivo. ([Obsidian Help][4])

---

## 0) Princípios “rígidos” de Zettelkasten que o sistema deve impor

* **Atomicidade real**

  * Uma Nota Permanente = **uma tese** (1 ideia) + explicação autônoma + exemplo/aplicação + limites.
  * Se um chunk gerar “3 coisas”, o pipeline **separa em 3 objetos** (com schema) e só então vira notas.
* **Rastreabilidade obrigatória**

  * Toda Nota Permanente deve apontar para **uma Nota de Literatura** (fonte) + **localizador** (página/seção/posição).
* **Autonomia**

  * A Nota Permanente não deve depender de “neste capítulo…”, “o autor disse…”.
* **Conectividade intencional**

  * Links só quando existir relação clara (suporta/contradiz/dependência/…).
* **Não-regressão**

  * Atualizações automáticas **não podem destruir edições manuais** fora de “blocos gerenciados” (marcadores).

---

## 1) Estrutura de projeto recomendada

### 1.1 Estrutura do repositório (código)

* `config/`

  * `config.yaml` (paths, modelos, thresholds, chunking, etc.)
* `prompts/`

  * `literature_note.md` (Prompt 1 melhorado)
  * `permanent_note.md` (Prompt 2 melhorado)
  * `relationship.md` (decidir tipo de relação entre notas)
  * `dedupe_decision.md` (merge/ignore/refine/update)
  * `moc_generation.md` (gerar/atualizar MOCs)
  * `ptbr_guard.md` (checagem/ajuste de idioma PT-BR)
* `data/`

  * `inbox/` (pdf/md/audio dropados pelo usuário)
  * `processed/` (opcional: cópia/versionamento)
  * `cache/` (transcrições, intermediários, debug)
* `src/`

  * `cli.py` (Typer + Rich)
  * `pipeline/` (orquestração por fases)
  * `harvester/` (Docling + loaders)
  * `extractor/` (LLM + parsing + validações)
  * `connector/` (RAG + links + backlinking)
  * `gardener/` (UMAP + HDBSCAN + MOCs)
  * `vault/` (IO Obsidian + YAML frontmatter + safe edits)
  * `index/` (ChromaDB collections + embeddings)
  * `state/` (SQLite/JSON para incrementalidade)
  * `schemas/` (Pydantic: Source, Chunk, Candidate, Note, MOC)
* `tests/` (mínimo: parsing YAML, ids, dedupe, safe edit blocks)

---

## 2) Estrutura proposta do Vault do Obsidian

> Objetivo: separar **fontes**, **literatura**, **permanentes**, **MOCs** e **assets** com estabilidade de paths.

* `00_Inbox/`

  * notas temporárias (revisão humana, pendências)
* `10_Sources/`

  * **Notas bibliográficas** (pai) por arquivo: `SRC - @Citekey - Titulo.md`
* `20_Literature/`

  * **Nota-mestre de Literatura** por arquivo: `LIT - @Citekey - Titulo.md`
* `30_Permanent/`

  * Notas Permanentes atômicas: `ZTL - <ulid> - titulo-slug.md`
* `40_MOCs/`

  * Mapas de Conteúdo: `MOC - Tema.md`
* `90_Assets/`


  * templates, logs de execução, relatórios (opcional)

---

## 3) Identificadores, rastreabilidade e frontmatter YAML (padrão único)

### 3.1 IDs (para update confiável)

* `source_id` (ex.: `@Kahneman2011` + hash do path para colisão)
* `chunk_id` (ex.: `@Kahneman2011::ch03::0007` ou ULID + metadados)
* `note_id` (ULID recomendado: ordenável por tempo, único)
* `moc_id` (ULID ou slug estável + ULID)

### 3.2 Campos de frontmatter mínimos (padrão)

No topo do Markdown, sempre YAML/Properties do Obsidian. ([Obsidian Help][4])

**Para SRC (bibliográfica)**

* `type: source`
* `source_id: "@Kahneman2011"`
* `title: ...`
* `author: [...]`
* `year: 2011`
* `origin_path: ...`
* `origin_type: pdf|md|audio`
* `created_at: ...`
* `updated_at: ...`
* `checksum: ...` (do arquivo original)

**Para LIT (literatura)**

* `type: literature`
* `source_id: ...`
* `literature_id: <ulid>`
* `language: pt-BR`
* `prompts: { literature_note: <hash> }`
* `created_at / updated_at`

**Para ZTL (permanente)**

* `type: permanent`
* `note_id: <ulid>`
* `source_id: ...`
* `literature_ref: "[[LIT - ...]]"`
* `source_locator: "p.12-14 / seção 3.2" (ou fallback)`
* `tags: [...]`
* `created_at / updated_at`
* `checksum: ...` (do corpo da nota)
* `embedding_model: ...`
* `prompt_versions: ...`

**Para MOC**

* `type: moc`
* `moc_id: <ulid>`
* `topic: ...`
* `cluster_signature: ...` (para re-identificar clusters ao atualizar)
* `created_at / updated_at`

---

## 4) ChromaDB: coleções e estratégia de indexação

> Use ChromaDB em modo persistente, com coleções separadas por “tipo de objeto”. IDs precisam ser únicos. ([Chroma Docs][3])

* `sources`

  * 1 item por fonte (metadados + resumo curto)
* `chunks`

  * 1 item por chunk (texto do chunk + metadados: capítulo/páginas)
* `permanent_notes`

  * 1 item por nota permanente (texto da nota + tags + links)
* `mocs`

  * 1 item por MOC (resumo do MOC + lista de notas)

**Embeddings**

* Embedding do chunk: texto “limpo” do chunk (sem boilerplate).
* Embedding da nota permanente: preferir `Tese + Definição + Intuição + Limites` (não precisa do frontmatter).
* Embedding do MOC: resumo + tópicos + títulos.

---

## 5) Fluxo detalhado por fases (com correções/melhorias nos seus passos)

### Fase 1 — Configuração (Setup / Bootstrap)

* **CLI: `init`**

  * Ler/gerar `config.yaml`
  * Validar:

    * `data/inbox` existe
    * `vault_path` existe e é gravável
  * Criar estrutura do vault (pastas acima)
  * Inicializar ChromaDB (pasta `data/chroma/`)
  * Inicializar `state.db` (SQLite) com tabelas:

    * `files` (path, checksum, status, last_processed_at)
    * `sources` (source_id, citekey, path)
    * `chunks` (chunk_id, source_id, checksum, status)
    * `notes` (note_id, path, checksum)
    * `mocs` (moc_id, path, signature)
  * Registrar `embedding_model`, `llm_model`, `prompt_hashes` no state

---

### Fase 2 — Ingestão e Criação da Fonte (The Harvester)

#### 2.1 Detecção automática de arquivos

* **CLI: `harvest`**

  * Varre `data/inbox/` por extensões:

    * `.pdf`, `.md`, `.wav/.mp3/.m4a` (audio)
  * Para cada arquivo:

    * calcular `checksum`
    * se checksum igual ao já processado em `state.db`: pular (incremental)
    * senão: marcar como “to_process”

#### 2.2 Extração de texto e metadados

* PDFs (Docling)

  * Usar Docling para:

    * extrair texto com estrutura (ordem de leitura, seções)
    * extrair tabelas/fórmulas quando disponível
    * extrair as referências da imagem, descrição. NÃO preciso salvar as imagens
  * Se você ativar o “modo VLM” para descrição de conteúdo visual, Docling tem exemplos de instalação com suporte VLM. ([docling-project.github.io][5])
* Markdown

  * Ler como texto + headings
  * Normalizar (remover frontmatter do arquivo original, se houver)
* Áudio

  * Transcrever (recomendação: `faster-whisper` ou `whisper.cpp`)
  * (Opcional) diarização + timestamps (se quiser rastreio temporal)
  * Gerar “source_locator” como `t=mm:ss-mm:ss`

#### 2.3 Geração da chave de citação (citekey)

* Criar `citekey` determinístico:

  * `@{SobrenomePrincipal}{Ano}{SlugCurtoTitulo}`
  * Resolver colisão: adicionar sufixo `a/b/c` ou hash curto
* Armazenar em `state.db` e em `sources` (Chroma)

#### 2.4 Criação da Nota Bibliográfica (SRC) e Nota-mestre de Literatura (LIT)

* Criar `SRC - @Citekey - Titulo.md` em `10_Sources/`

  * Frontmatter com metadados, path original, checksum, etc.
  * Corpo:

    * referência completa
    * link para LIT
* Criar `LIT - @Citekey - Titulo.md` em `20_Literature/`

  * Frontmatter `type: literature`, `source_id`, `language: pt-BR`
  * Corpo começa “vazio”, mas com seções:

    * `## Resumo`
    * `## Conceitos-chave`
    * `## Potenciais Notas Permanentes (em lista)`
    * `## Log de chunks processados` (para auditoria)

#### 2.5 Chunking hierárquico (com melhorias)

* **Nível 1: por capítulos/seções**

  * PDF: usar estrutura detectada; fallback: heurística por “Chapter/Capítulo”, headings, sumário
  * MD: `#`, `##`, `###`
* **Nível 2: chunks semânticos**

  * Usar `RecursiveCharacterTextSplitter` (LangChain) com:

    * `chunk_size ~ 1000 tokens`
    * `chunk_overlap ~ 200 tokens` (overlap reduz perda de contexto) ([Docs do LangChain][2])
  * **Melhoria recomendada**: “limpeza” antes do split

    * remover cabeçalhos/rodapés repetitivos
    * desfazer hifenização de quebra de linha
    * normalizar whitespace
* Persistir cada chunk como objeto:

  * `chunk_id`, `source_id`, `chapter`, `pages/timestamps`, `text`, `checksum`
* Indexar chunks na coleção `chunks` (Chroma) para buscas e auditoria

---

### Fase 3 — Processamento e Extração Atômica (The Extractor)

#### 3.1 Execução por chunk com Prompt 1

* **CLI: `extract`**

  * Para cada chunk “pendente”:

    * carregar `prompts/literature_note.md`
    * chamar LLM via LangChain
    * **output deve conter**:

      * um bloco Markdown para anexar ao LIT
      * uma lista JSON (ou YAML) de “candidatos a nota permanente” (atômicos)
    * validar com Pydantic:

      * se falhar: retry com “corrigir JSON” (1 tentativa)
      * se falhar: enviar chunk para `00_Inbox/Review`

#### 3.2 Agregação rígida no LIT (nota-mestre por arquivo)

* O sistema **não cria várias notas de literatura por chunk** (como você pediu).
* Ele **anexa** ao `LIT - ...`:

  * `## Chunk <capítulo> | <páginas>` + conteúdo resumido/conceitos
  * e no final de LIT mantém uma seção consolidada:

    * “Conceitos-chave globais”
    * “Lista global de candidatos a permanentes (com ids temporários)”

#### 3.3 Deduplicação semântica antes de virar permanente

* Para cada candidato (ideia atômica):

  * gerar embedding “temporário” do candidato (tese + definição)
  * consultar `permanent_notes` no Chroma:

    * obter top-k (ex.: 5)
    * aplicar threshold:

      * ex.: `similarity > 0.85` (ajustável)
  * Se passar do threshold:

    * rodar prompt `dedupe_decision.md`:

      * decide: `ignore` | `refine_existing` | `merge` | `create_new_anyway`
    * se “refine/merge”:

      * o sistema prepara uma **atualização mínima** no arquivo alvo
      * sem apagar partes manuais (ver “blocos gerenciados” abaixo)

---

### Fase 4 — Linkagem e Armazenamento (The Connector)

#### 4.1 RAG para links (somente para conectar)

* **CLI: `connect`**

  * Para cada candidato aprovado:

    * buscar top-5 notas similares em `permanent_notes` (Chroma)
    * montar `rag_context_list` com:

      * título
      * 2–3 linhas de resumo (pode vir do próprio corpo da nota)
      * tags

#### 4.2 Geração da Nota Permanente com Prompt 2 (o melhorado)

* Chamar LLM com:

  * conceito alvo + evidência + localizador + referência à LIT
  * RAG somente para conexões (regra anti-contaminação)
* Validar:

  * idioma PT-BR (checagem automática; se falhar, rodar “ptbr_guard”)
  * atomicidade (heurística: tamanho + número de teses/conectivos)
  * presença de:

    * `> Tese`
    * seção `Limites`
    * seção `Fonte` com link para LIT

#### 4.3 Escrita no disco (persistência segura)

* Criar arquivo em `30_Permanent/`:

  * `ZTL - <note_id> - <slug>.md`
* Inserir frontmatter YAML padronizado (Properties). ([Obsidian Help][4])

#### 4.4 Backlinking físico (sem destruir edição manual)

* Em vez de editar “na raça”, usar **marcadores** idempotentes:

  * No final do arquivo alvo, criar/atualizar bloco:

    * `<!-- zettel:auto-backlinks:start -->`
    * lista gerada
    * `<!-- zettel:auto-backlinks:end -->`
* Para cada nota antiga relacionada:

  * inserir link de volta para a nova nota **somente dentro do bloco gerenciado**
* Vantagem: você pode editar a nota manualmente e o sistema não estraga.

#### 4.5 Atualização do índice vetorial

* Inserir/atualizar no Chroma:

  * id = `note_id`
  * documento = texto “embed-friendly”
  * metadata = path, tags, source_id, created_at, etc. ([Chroma Docs][3])

---

### Fase 5 — Organização emergente (The Gardener / MOCs)

#### 5.1 Trigger e batch

* **CLI: `garden`**

  * roda manualmente ou por gatilho:

    * “fim do livro”
    * “a cada N notas novas”
    * “por capítulo”

#### 5.2 Clusterização

* Carregar embeddings de `permanent_notes`
* Rodar:

  * UMAP (redução)
  * HDBSCAN (clusters densos + ruído)
* Para cada cluster:

  * extrair termos representativos (c-TF-IDF)
  * gerar `cluster_signature` (hash de ids + termos) para reprocessamento estável

#### 5.3 Gestão de MOCs

* Se já existe MOC com assinatura compatível:

  * atualizar apenas a seção gerenciada (links/estrutura)
* Se não existe:

  * criar `MOC - Tema.md` em `40_MOCs/` com `moc_id`
* LLM gera:

  * resumo do tema (PT-BR)
  * sub-seções (quando fizer sentido)
  * links para notas do cluster com 1-linha de orientação
* Indexar MOC na coleção `mocs`

---

## 6) Incorporação de notas manuais do vault (bidirecional)

### 6.1 Detecção

* **CLI: `sync-manual`**

  * varrer `30_Permanent/` e `40_MOCs/` por arquivos `.md`
  * para cada arquivo:

    * se não tem `note_id/moc_id`: criar (sem mudar o corpo)
    * se tem id mas não está no Chroma: indexar
    * se checksum mudou desde último sync: re-embed + atualizar metadados

### 6.2 “Vinculação” de links

* Para notas manuais recém-integradas:

  * rodar RAG e sugerir conexões
  * **modo seguro**:

    * por padrão só escreve no bloco `auto-backlinks`
    * opcional: gerar um “Relatório de sugestões” em `99_System/`

---

## 7) Pipeline de execução end-to-end (com parâmetros)

### 7.1 Comandos principais (CLI/TUI agradável)

Implementar com **Typer + Rich** (menu com opções).

* `zettel init --vault /caminho/do/vault --inbox ./data/inbox`
* `zettel harvest [--move-processed] [--include-audio]`
* `zettel extract --llm <provider/model> --prompts ./prompts`
* `zettel connect --topk 5 --dedupe-threshold 0.85`
* `zettel garden --min-cluster-size 8`
* `zettel sync-manual`
* `zettel run-all` (orquestra 2→5 na ordem)
* `zettel status` (mostra quantos arquivos/chunks/notas pendentes)
* `zettel doctor` (verifica config, vault, permissões, coleções)

### 7.2 Parâmetros que valem expor

* `--vault-path`
* `--inbox-path`
* `--chroma-path`
* `--chunk-size`, `--chunk-overlap` (LangChain) ([Docs do LangChain][2])
* `--topk-links`
* `--dedupe-threshold`
* `--language pt-BR` (fixo por padrão)
* `--embed-model`
* `--llm-model`
* `--dry-run` (gera relatório sem escrever)
* `--concurrency N`
* `--log-level`

---

## 8) Ajustes importantes nos seus passos (o que eu mudaria/ acrescentaria)

* **Adicionar um “The Registrar” (state + incrementalidade)**

  * Sem isso você vai reprocessar tudo sempre e vai dar drift.
* **RAG só para conectar**

  * Se você não for rígido aqui, o modelo mistura conteúdo de notas antigas e “cria” fatos.
* **Blocos gerenciados para updates/backlinks**

  * Evita destruir edição manual e reduz conflito.
* **Fila de revisão**

  * Quando JSON quebrar, quando idioma falhar, quando atomicidade falhar: joga em `00_Inbox/Review`.
* **Guardrail de PT-BR**

  * Mesmo com prompt, às vezes sai trecho em inglês; faça um pós-check e “conserte”.
* **Localizador de fonte obrigatório**

  * PDF: página/seção; áudio: timestamp; MD: heading + linha aproximada.
* **Separar coleções Chroma**

  * `chunks` ≠ `permanent_notes`. Misturar prejudica dedupe e RAG.

---

## 9) “Estrutura lógica” pronta para alimentar o Claude Code (tarefas claras)

Se você for usar o Claude Code para implementar, passe como backlog:

* **(A) Core**

  * Implementar `config.yaml` + loader + validação
  * Implementar `state.db` (SQLite) + migrações simples
* **(B) Vault IO**

  * Parser/escritor de frontmatter YAML
  * Escrita segura com blocos gerenciados
* **(C) Harvester**

  * Docling PDF/MD/AUDIO loader + extração de texto ([GitHub][1])
  * MD loader + headings
  * Audio transcriber + timestamps
  * Citekey generator + SRC/LIT creators
  * Chunking hierárquico + splitters LangChain ([Docs do LangChain][2])
* **(D) Extractor**

  * Runner de Prompt 1 + parse estruturado (Pydantic)
  * Agregador no LIT
* **(E) Index**

  * Chroma client + coleções + upsert + queries (ids únicos) ([Chroma Docs][3])
  * Embedding provider pluggável
* **(F) Connector**

  * Runner de Prompt 2 + links
  * Backlinking físico com marcadores
* **(G) Gardener**

  * UMAP + HDBSCAN
  * c-TF-IDF
  * Runner de MOC prompt + update seguro
* **(H) Sync manual**

  * Scan vault + index + sugestões
* **(I) CLI**

  * Typer + Rich menu, `run-all`, `doctor`, `status`

---

