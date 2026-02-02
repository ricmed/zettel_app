A boa estratégia “sem drift” tem 3 pilares:

1. **Hashes em camadas (binário → texto canônico → semântico)**
2. **IDs estáveis derivados do *texto-fonte* (não do LLM)**
3. **Assinatura de pipeline (prompt/modelo/parâmetros) para reprocessar só o necessário**

Abaixo vai um desenho completo e bem implementável.

---

## 1) O que é “drift” no seu caso (e como conter)

* **Drift de fonte**: o arquivo mudou (bytes diferentes), mas o texto extraído pode ou não mudar.
* **Drift de extração**: mesma fonte, mas o extractor (Docling versão/config) gerou texto diferente.
* **Drift de chunking**: pequenos deslocamentos mudam limites dos chunks, gerando IDs novos e duplicação em cascata.
* **Drift de LLM**: mesma entrada, o LLM varia levemente e cria “conceitos” novos/alterados.
* **Drift de conexão (RAG/links)**: novas notas surgem e mudam os “top-k”; se você reescrever tudo, bagunça backlinks e edição manual.

A resposta: **detectar mudanças com hashes**, **amarrar identidades ao texto-fonte**, e **atualizar só blocos gerenciados**.

---

## 2) Hashes/Checksums em camadas

### 2.1 `file_checksum` (binário, exato)

* **O que**: hash dos bytes do arquivo original (PDF/MD/AUDIO).
* **Serve para**: detectar alteração real do arquivo (renomear/mover não muda).
* **Algoritmo**: `SHA-256` (padrão) ou `BLAKE3` (mais rápido).
* **Quando muda**: sempre que o arquivo mudar.

### 2.2 `extraction_checksum` (texto canônico global da fonte)

* **O que**: hash do **texto extraído já normalizado** (não do PDF bruto).
* **Serve para**: detectar “mudança de conteúdo textual” e separar de “mudança binária”.
* **Por que**: PDF pode ter metadata/ordem alterada sem mudar o texto “útil”; ou o contrário.
* **Inclui também**: um checksum do *manifesto de assets* (imagens + descrições), se você extrair.

### 2.3 `chapter_checksum` (texto canônico por capítulo/seção)

* **O que**: hash por unidade do nível 1 (capítulo/seção/heading).
* **Serve para**: reprocessar só capítulos alterados.
* **Vantagem**: evita reprocessamento “all-or-nothing”.

### 2.4 `chunk_checksum` (texto canônico por chunk final)

* **O que**: hash do texto do chunk (já normalizado).
* **Serve para**:

  * decidir se o chunk precisa ser re-enviado ao LLM
  * manter IDs estáveis (ver seção 3)

### 2.5 `llm_call_checksum` (assinatura determinística do chamado ao LLM)

* **O que**: hash do *input efetivo* do LLM:

  * `prompt_hash + chunk_checksum + modelo + parâmetros (temp/top_p) + idioma`
  * (e opcionalmente: `rag_context_checksum` no Prompt 2)
* **Serve para**: cache forte. Se repetir, **reutiliza output antigo** e elimina drift do LLM.

### 2.6 `note_semantic_checksum` (para embeddings/RAG)

* **O que**: hash do “texto embedável” da nota (sem frontmatter, sem blocos automáticos se você quiser).
* **Serve para**: re-embed apenas quando o conteúdo semântico mudar.

---

## 3) IDs estáveis (para update sem duplicar)

A regra de ouro: **IDs devem ser derivados do texto-fonte** + localizador, não do output do LLM.

### 3.1 `source_id`

* Derivado de: `citekey` (ex: `@Kahneman2011`) + hash curto do caminho original (opcional) para evitar colisões de citekey.

### 3.2 `chunk_id` (estável mesmo com reorder mínimo)

Recomendação prática (boa e simples):

* `chunk_id = "{source_id}::{chapter_id}::" + short_hash(chunk_checksum)`
* Se colisão (raro): sufixo `-01`, `-02`.

Isso é **muito estável** porque:

* se o texto do chunk não mudou → `chunk_checksum` igual → `chunk_id` igual.
* se só mudou a paginação, o chunk continua reconhecido.

> Observação: seu chunking por tokens com overlap é “sensível” a inserções no início do capítulo.
> Para reduzir drift de chunking, eu sugiro um ajuste:

* use **capítulo/seção como “content island”** e faça chunking sempre dentro do capítulo
* e **garanta normalização idêntica antes do splitter** (seção 4).

### 3.3 `concept_id` (candidato atômico) — baseado em âncora de fonte

O maior ponto de drift é o “conceito” variar.
A solução mais rígida é pedir ao LLM (Prompt 1) um **anchor_quote** curto (10–25 palavras) retirado do chunk *como evidência*.

Então:

* `anchor_hash = hash(normalize(anchor_quote))`
* `concept_key = hash(source_id + chunk_id + anchor_hash)`
* `concept_id = concept_key` (ou `source_id::concept::<short>`)

Vantagens:

* Se rodar de novo com mesmas entradas, o LLM tende a escolher a mesma âncora (com temp=0 + cache).
* Se a fonte não mudou, o anchor é encontrável.
* Se o texto muda, o conceito “realmente” mudou: faz sentido gerar outro concept_id ou atualizar.

Fallback (quando não houver âncora):

* `concept_key = hash(source_id + chunk_id + thesis_normalized_hash)`

### 3.4 `note_id` (Nota Permanente) — “uma vez criada, não muda”

* Ao criar a nota pela primeira vez, gere `note_id = ULID`.
* Salve no frontmatter e no `state.db`.
* Mantenha uma tabela de mapping:

  * `(concept_id -> note_id)`
* Se reprocessar e o `concept_id` já existir, você **atualiza a mesma nota**.

Isso remove duplicação mesmo se o título/slugs mudarem.

### 3.5 `moc_id`

* Mesma ideia: ULID + tabela `(cluster_signature -> moc_id)`.

---

## 4) Normalização canônica: a parte mais importante

Sem uma normalização consistente, seus hashes vão “oscilar” e gerar drift falso.

### 4.1 `normalize_text_for_hash(text)` (sugestão)

Aplicar sempre antes de gerar checksums:

* Unicode NFKC
* normalizar quebras de linha: `\r\n` → `\n`
* remover whitespace “decorativo”:

  * colapsar múltiplos espaços
  * remover trailing spaces
* corrigir hifenização comum de PDF:

  * `palavra-\ncontinua` → `palavracontinua` (com cuidado)
* remover cabeçalho/rodapé repetidos (heurística por frequência)
* opcional: padronizar aspas e bullets

### 4.2 “Texto embedável” (para embeddings)

Crie um segundo normalizador:

* remover YAML frontmatter
* remover blocos automáticos (se você não quer que backlinks afetem embedding)
* manter apenas:

  * título declarativo + tese + definição + intuição + limites

Isso estabiliza o vetor e reduz drift de links.

---

## 5) Assinatura de pipeline (reprocessamento mínimo e explícito)

Você precisa de um hash global da configuração que impacta resultado:

### 5.1 `pipeline_signature`

Hash de um JSON canônico com:

* versões: `docling_version`, `langchain_version`, `chroma_version`, `embedding_model_version`
* parâmetros:

  * chunk_size, chunk_overlap, splitter_type
  * thresholds dedupe/link (0.85 etc.)
* prompts:

  * `prompt1_hash`, `prompt2_hash`, etc.
* LLM:

  * provider, model_name
  * temperature, top_p, seed (se suportado)

Armazenar:

* no `state.db` (tabela `runs`)
* e opcionalmente no frontmatter das notas (`prompt_versions`, `embedding_model`)

**Regra de reprocessamento:**

* Se `file_checksum` mudou → re-extrair fonte
* Se `extraction_checksum` mudou → re-chunk do capítulo afetado
* Se `chunk_checksum` mudou → re-executar Prompt 1 naquele chunk
* Se `prompt_hash` mudou (Prompt 1 ou 2) → reprocessar só os artefatos dependentes
* Se `embedding_model` mudou → re-embed tudo, mas sem reescrever notas
* Se `threshold` mudou → só refazer dedupe/links (não reescrever corpo)

---

## 6) “Atualizar sem destruir”: blocos gerenciados + hashes por bloco

Para permitir updates sem estragar edição manual:

* No arquivo da nota, separe áreas:

  * **Corpo humano / manual**
  * **Blocos automáticos** (backlinks, conexões sugeridas, metadados derivados)

Exemplo de blocos:

* `<!-- zettel:auto-connections:start --> ... <!-- zettel:auto-connections:end -->`
* `<!-- zettel:auto-backlinks:start --> ... <!-- zettel:auto-backlinks:end -->`

Então calcule:

* `auto_block_checksum` = hash do conteúdo dentro dos blocos auto
* Salve no frontmatter: `auto_checksum: ...`

**Regra de update segura**

* Se o usuário editou **fora** dos blocos auto → ok, o sistema não mexe
* Se o usuário editou **dentro** do bloco auto:

  * detecta (checksum diferente do esperado)
  * não sobrescreve; cria um bloco “auto-backlinks-v2” ou gera um relatório em `99_System/Conflicts`

---

## 7) Esquema mínimo no SQLite para suportar tudo isso

### Tabela `files`

* `path`
* `file_checksum`
* `origin_type`
* `source_id`
* `last_seen_at`

### Tabela `sources`

* `source_id`
* `citekey`
* `file_checksum`
* `extraction_checksum`
* `docling_signature` (versão + config)
* `created_at`, `updated_at`

### Tabela `chapters`

* `chapter_id`
* `source_id`
* `chapter_checksum`
* `locator` (capítulo/heading/páginas)

### Tabela `chunks`

* `chunk_id`
* `source_id`
* `chapter_id`
* `chunk_checksum`
* `locator` (páginas/timestamps)
* `llm_prompt1_hash`
* `llm_call_checksum_prompt1`
* `status`

### Tabela `concepts`

* `concept_id`
* `source_id`
* `chunk_id`
* `anchor_hash`
* `thesis_hash`
* `concept_embedding_hash` (opcional)
* `note_id` (FK)

### Tabela `notes`

* `note_id`
* `path`
* `note_semantic_checksum`
* `auto_checksum`
* `embedding_input_hash`
* `embedding_model`
* `updated_at`

### Tabela `mocs`

* `moc_id`
* `path`
* `cluster_signature`
* `embedding_input_hash`

---

## 8) Configurações práticas para reduzir drift do LLM

Mesmo com cache, vale blindar:

* `temperature=0` (ou o mínimo suportado)
* `top_p=1` (ou padrão)
* `seed` fixo se o provider suportar
* caching por `llm_call_checksum` (armazenar request/response)

---

## 9) Snippets curtos (Python) para hashing canônico

```python
import re
import unicodedata
import hashlib

def normalize_text_for_hash(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)          # colapsa espaços
    t = re.sub(r" *\n *", "\n", t)         # remove espaços ao redor de newline
    t = re.sub(r"\n{3,}", "\n\n", t)       # limita linhas em branco
    # (opcional) desfazer hifenização simples
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)
    return t.strip()

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
```

---

## 10) Regras objetivas de “quando atualizar o quê”

* **Arquivo mudou** (`file_checksum` diferente)

  * re-extrair → comparar `extraction_checksum`
  * se `extraction_checksum` igual: não reprocessar chunks
* **Texto extraído mudou** (`extraction_checksum` diferente)

  * recalcular `chapter_checksum`
  * só capítulos mudados geram novos chunks
* **Chunk mudou** (`chunk_checksum` diferente)

  * reexecuta Prompt 1 naquele chunk → atualiza LIT e candidatos
* **Prompt 1 mudou**

  * reexecuta Prompt 1 para todos chunks (ou só os “ainda relevantes”)
* **Prompt 2 mudou**

  * não precisa refazer Prompt 1
  * refaz apenas geração do corpo das notas permanentes, mantendo `note_id` e respeitando blocos auto
* **Embedding model mudou**

  * re-embed tudo (Chroma upsert), sem reescrever Markdown
* **Notas manuais novas**

  * gerar `note_id` se faltar → `note_semantic_checksum` → embed → indexar → sugerir conexões em blocos auto

---

Se você quiser, eu também posso te sugerir **um esquema de “content-defined chunking”** (CDC) para reduzir ainda mais drift quando um capítulo recebe pequenas inserções no começo — mas a estratégia acima já funciona muito bem para livros/artigos estáveis e evita duplicação/reescrita desnecessária.
A boa estratégia “sem drift” tem 3 pilares:

1. **Hashes em camadas (binário → texto canônico → semântico)**
2. **IDs estáveis derivados do *texto-fonte* (não do LLM)**
3. **Assinatura de pipeline (prompt/modelo/parâmetros) para reprocessar só o necessário**

Abaixo vai um desenho completo e bem implementável.

---

## 1) O que é “drift” no seu caso (e como conter)

* **Drift de fonte**: o arquivo mudou (bytes diferentes), mas o texto extraído pode ou não mudar.
* **Drift de extração**: mesma fonte, mas o extractor (Docling versão/config) gerou texto diferente.
* **Drift de chunking**: pequenos deslocamentos mudam limites dos chunks, gerando IDs novos e duplicação em cascata.
* **Drift de LLM**: mesma entrada, o LLM varia levemente e cria “conceitos” novos/alterados.
* **Drift de conexão (RAG/links)**: novas notas surgem e mudam os “top-k”; se você reescrever tudo, bagunça backlinks e edição manual.

A resposta: **detectar mudanças com hashes**, **amarrar identidades ao texto-fonte**, e **atualizar só blocos gerenciados**.

---

## 2) Hashes/Checksums em camadas

### 2.1 `file_checksum` (binário, exato)

* **O que**: hash dos bytes do arquivo original (PDF/MD/AUDIO).
* **Serve para**: detectar alteração real do arquivo (renomear/mover não muda).
* **Algoritmo**: `SHA-256` (padrão) ou `BLAKE3` (mais rápido).
* **Quando muda**: sempre que o arquivo mudar.

### 2.2 `extraction_checksum` (texto canônico global da fonte)

* **O que**: hash do **texto extraído já normalizado** (não do PDF bruto).
* **Serve para**: detectar “mudança de conteúdo textual” e separar de “mudança binária”.
* **Por que**: PDF pode ter metadata/ordem alterada sem mudar o texto “útil”; ou o contrário.
* **Inclui também**: um checksum do *manifesto de assets* (imagens + descrições), se você extrair.

### 2.3 `chapter_checksum` (texto canônico por capítulo/seção)

* **O que**: hash por unidade do nível 1 (capítulo/seção/heading).
* **Serve para**: reprocessar só capítulos alterados.
* **Vantagem**: evita reprocessamento “all-or-nothing”.

### 2.4 `chunk_checksum` (texto canônico por chunk final)

* **O que**: hash do texto do chunk (já normalizado).
* **Serve para**:

  * decidir se o chunk precisa ser re-enviado ao LLM
  * manter IDs estáveis (ver seção 3)

### 2.5 `llm_call_checksum` (assinatura determinística do chamado ao LLM)

* **O que**: hash do *input efetivo* do LLM:

  * `prompt_hash + chunk_checksum + modelo + parâmetros (temp/top_p) + idioma`
  * (e opcionalmente: `rag_context_checksum` no Prompt 2)
* **Serve para**: cache forte. Se repetir, **reutiliza output antigo** e elimina drift do LLM.

### 2.6 `note_semantic_checksum` (para embeddings/RAG)

* **O que**: hash do “texto embedável” da nota (sem frontmatter, sem blocos automáticos se você quiser).
* **Serve para**: re-embed apenas quando o conteúdo semântico mudar.

---

## 3) IDs estáveis (para update sem duplicar)

A regra de ouro: **IDs devem ser derivados do texto-fonte** + localizador, não do output do LLM.

### 3.1 `source_id`

* Derivado de: `citekey` (ex: `@Kahneman2011`) + hash curto do caminho original (opcional) para evitar colisões de citekey.

### 3.2 `chunk_id` (estável mesmo com reorder mínimo)

Recomendação prática (boa e simples):

* `chunk_id = "{source_id}::{chapter_id}::" + short_hash(chunk_checksum)`
* Se colisão (raro): sufixo `-01`, `-02`.

Isso é **muito estável** porque:

* se o texto do chunk não mudou → `chunk_checksum` igual → `chunk_id` igual.
* se só mudou a paginação, o chunk continua reconhecido.

> Observação: seu chunking por tokens com overlap é “sensível” a inserções no início do capítulo.
> Para reduzir drift de chunking, eu sugiro um ajuste:

* use **capítulo/seção como “content island”** e faça chunking sempre dentro do capítulo
* e **garanta normalização idêntica antes do splitter** (seção 4).

### 3.3 `concept_id` (candidato atômico) — baseado em âncora de fonte

O maior ponto de drift é o “conceito” variar.
A solução mais rígida é pedir ao LLM (Prompt 1) um **anchor_quote** curto (10–25 palavras) retirado do chunk *como evidência*.

Então:

* `anchor_hash = hash(normalize(anchor_quote))`
* `concept_key = hash(source_id + chunk_id + anchor_hash)`
* `concept_id = concept_key` (ou `source_id::concept::<short>`)

Vantagens:

* Se rodar de novo com mesmas entradas, o LLM tende a escolher a mesma âncora (com temp=0 + cache).
* Se a fonte não mudou, o anchor é encontrável.
* Se o texto muda, o conceito “realmente” mudou: faz sentido gerar outro concept_id ou atualizar.

Fallback (quando não houver âncora):

* `concept_key = hash(source_id + chunk_id + thesis_normalized_hash)`

### 3.4 `note_id` (Nota Permanente) — “uma vez criada, não muda”

* Ao criar a nota pela primeira vez, gere `note_id = ULID`.
* Salve no frontmatter e no `state.db`.
* Mantenha uma tabela de mapping:

  * `(concept_id -> note_id)`
* Se reprocessar e o `concept_id` já existir, você **atualiza a mesma nota**.

Isso remove duplicação mesmo se o título/slugs mudarem.

### 3.5 `moc_id`

* Mesma ideia: ULID + tabela `(cluster_signature -> moc_id)`.

---

## 4) Normalização canônica: a parte mais importante

Sem uma normalização consistente, seus hashes vão “oscilar” e gerar drift falso.

### 4.1 `normalize_text_for_hash(text)` (sugestão)

Aplicar sempre antes de gerar checksums:

* Unicode NFKC
* normalizar quebras de linha: `\r\n` → `\n`
* remover whitespace “decorativo”:

  * colapsar múltiplos espaços
  * remover trailing spaces
* corrigir hifenização comum de PDF:

  * `palavra-\ncontinua` → `palavracontinua` (com cuidado)
* remover cabeçalho/rodapé repetidos (heurística por frequência)
* opcional: padronizar aspas e bullets

### 4.2 “Texto embedável” (para embeddings)

Crie um segundo normalizador:

* remover YAML frontmatter
* remover blocos automáticos (se você não quer que backlinks afetem embedding)
* manter apenas:

  * título declarativo + tese + definição + intuição + limites

Isso estabiliza o vetor e reduz drift de links.

---

## 5) Assinatura de pipeline (reprocessamento mínimo e explícito)

Você precisa de um hash global da configuração que impacta resultado:

### 5.1 `pipeline_signature`

Hash de um JSON canônico com:

* versões: `docling_version`, `langchain_version`, `chroma_version`, `embedding_model_version`
* parâmetros:

  * chunk_size, chunk_overlap, splitter_type
  * thresholds dedupe/link (0.85 etc.)
* prompts:

  * `prompt1_hash`, `prompt2_hash`, etc.
* LLM:

  * provider, model_name
  * temperature, top_p, seed (se suportado)

Armazenar:

* no `state.db` (tabela `runs`)
* e opcionalmente no frontmatter das notas (`prompt_versions`, `embedding_model`)

**Regra de reprocessamento:**

* Se `file_checksum` mudou → re-extrair fonte
* Se `extraction_checksum` mudou → re-chunk do capítulo afetado
* Se `chunk_checksum` mudou → re-executar Prompt 1 naquele chunk
* Se `prompt_hash` mudou (Prompt 1 ou 2) → reprocessar só os artefatos dependentes
* Se `embedding_model` mudou → re-embed tudo, mas sem reescrever notas
* Se `threshold` mudou → só refazer dedupe/links (não reescrever corpo)

---

## 6) “Atualizar sem destruir”: blocos gerenciados + hashes por bloco

Para permitir updates sem estragar edição manual:

* No arquivo da nota, separe áreas:

  * **Corpo humano / manual**
  * **Blocos automáticos** (backlinks, conexões sugeridas, metadados derivados)

Exemplo de blocos:

* `<!-- zettel:auto-connections:start --> ... <!-- zettel:auto-connections:end -->`
* `<!-- zettel:auto-backlinks:start --> ... <!-- zettel:auto-backlinks:end -->`

Então calcule:

* `auto_block_checksum` = hash do conteúdo dentro dos blocos auto
* Salve no frontmatter: `auto_checksum: ...`

**Regra de update segura**

* Se o usuário editou **fora** dos blocos auto → ok, o sistema não mexe
* Se o usuário editou **dentro** do bloco auto:

  * detecta (checksum diferente do esperado)
  * não sobrescreve; cria um bloco “auto-backlinks-v2” ou gera um relatório em `99_System/Conflicts`

---

## 7) Esquema mínimo no SQLite para suportar tudo isso

### Tabela `files`

* `path`
* `file_checksum`
* `origin_type`
* `source_id`
* `last_seen_at`

### Tabela `sources`

* `source_id`
* `citekey`
* `file_checksum`
* `extraction_checksum`
* `docling_signature` (versão + config)
* `created_at`, `updated_at`

### Tabela `chapters`

* `chapter_id`
* `source_id`
* `chapter_checksum`
* `locator` (capítulo/heading/páginas)

### Tabela `chunks`

* `chunk_id`
* `source_id`
* `chapter_id`
* `chunk_checksum`
* `locator` (páginas/timestamps)
* `llm_prompt1_hash`
* `llm_call_checksum_prompt1`
* `status`

### Tabela `concepts`

* `concept_id`
* `source_id`
* `chunk_id`
* `anchor_hash`
* `thesis_hash`
* `concept_embedding_hash` (opcional)
* `note_id` (FK)

### Tabela `notes`

* `note_id`
* `path`
* `note_semantic_checksum`
* `auto_checksum`
* `embedding_input_hash`
* `embedding_model`
* `updated_at`

### Tabela `mocs`

* `moc_id`
* `path`
* `cluster_signature`
* `embedding_input_hash`

---

## 8) Configurações práticas para reduzir drift do LLM

Mesmo com cache, vale blindar:

* `temperature=0` (ou o mínimo suportado)
* `top_p=1` (ou padrão)
* `seed` fixo se o provider suportar
* caching por `llm_call_checksum` (armazenar request/response)

---

## 9) Snippets curtos (Python) para hashing canônico

```python
import re
import unicodedata
import hashlib

def normalize_text_for_hash(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)          # colapsa espaços
    t = re.sub(r" *\n *", "\n", t)         # remove espaços ao redor de newline
    t = re.sub(r"\n{3,}", "\n\n", t)       # limita linhas em branco
    # (opcional) desfazer hifenização simples
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)
    return t.strip()

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
```

---

## 10) Regras objetivas de “quando atualizar o quê”

* **Arquivo mudou** (`file_checksum` diferente)

  * re-extrair → comparar `extraction_checksum`
  * se `extraction_checksum` igual: não reprocessar chunks
* **Texto extraído mudou** (`extraction_checksum` diferente)

  * recalcular `chapter_checksum`
  * só capítulos mudados geram novos chunks
* **Chunk mudou** (`chunk_checksum` diferente)

  * reexecuta Prompt 1 naquele chunk → atualiza LIT e candidatos
* **Prompt 1 mudou**

  * reexecuta Prompt 1 para todos chunks (ou só os “ainda relevantes”)
* **Prompt 2 mudou**

  * não precisa refazer Prompt 1
  * refaz apenas geração do corpo das notas permanentes, mantendo `note_id` e respeitando blocos auto
* **Embedding model mudou**

  * re-embed tudo (Chroma upsert), sem reescrever Markdown
* **Notas manuais novas**

  * gerar `note_id` se faltar → `note_semantic_checksum` → embed → indexar → sugerir conexões em blocos auto

---


