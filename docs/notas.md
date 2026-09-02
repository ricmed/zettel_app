# Estrutura das notas geradas

[← Voltar ao README](../README.md)

O formato exato de cada tipo de nota escrita no vault: nota bibliográfica (SRC), índice de literatura, nota de literatura granular (LIT) e nota permanente (ZTL).

Convenção de nomes: `PREFIXO - IDENTIFICADOR - slug.md`. SRC e índice LIT usam `AuthorYear`; a LIT granular é `LIT - AuthorYear - pNNN - topico-NNNN.md`; ZTL e MOC usam ULID. O `@` só existe em `source_id` e na CLI — nunca em caminhos.

---

## Nota Bibliográfica (SRC)

Fica em `10_Sources/`. Campos tipados conforme o `document_type` (cidade, editora, edição, URL, instituição, etc.) aparecem separados no frontmatter; `abnt_reference` agrupa a citação no padrão ABNT para copiar facilmente.

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

Os campos `cost_usd_*` / `tokens_*` são espelhados do SQLite por `sync_source_costs_to_vault` — veja [arquitetura.md](arquitetura.md#custos-de-llm-e-embeddings).

### Tipos documentais e campos obrigatórios

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

A inferência do tipo e o preenchimento dos campos acontecem no harvest — veja [pipeline.md](pipeline.md#fase-1--harvest-coleta).

---

## Nota de Literatura (LIT)

Existem **dois** artefatos com o prefixo `LIT`: o **índice por fonte** (na raiz de `20_Literature/`) e as **notas granulares** (uma por chunk, em `20_Literature/{Citekey}/`).

### Índice por fonte

`20_Literature/LIT - Kahneman2011 - thinking-fast-and-slow.md`:

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

O bloco `auto-lit-index` é mantido pelo `review` (e pela adoção de LIT manual no `sync-manual`): só entram notas **aprovadas**, com rótulo `p. N — tópico`.

### Nota granular (uma por chunk)

`20_Literature/Kahneman2011ThinkingFast/LIT - Kahneman2011 - p020 - sistema-1-0001.md`:

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

Pontos importantes:

- O **draft** gerado pelo `extract` fica em `00_Inbox/Review/{Citekey}/` com o **mesmo basename** da nota aprovada — aprovar é mover, não regravar.
- O bloco `auto-source-excerpt` guarda o trecho integral da fonte. Ele é **removido do texto embeddado** em `literature_notes`: o índice vetorial guarda a interpretação, não o texto-fonte cru (que já vive na coleção `chunks`).
- Uma LIT granular escrita à mão pode ser adotada pelo pipeline — veja [notas-manuais.md](notas-manuais.md).

---

## Nota Permanente (ZTL)

Fica em `30_Permanent/`.

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

<!-- zettel:auto-moc-backrefs:start -->
- [[MOC - 01HJKL... - heuristicas-e-vieses]]
<!-- zettel:auto-moc-backrefs:end -->
```

Leitura dos campos:

- `literature_ref` aponta para a **LIT granular** do chunk que originou a nota (fallback: o índice da fonte).
- `source_locator` é o localizador humano (`p.{page_in_book} / {section_path}`).
- `origin: pipeline | manual` distingue o que foi gerado do que foi escrito à mão.
- `## Conexões` é escrito pelo LLM com o **tipo da relação** (`supports`, `contradicts`, `extends`, `depends_on`, `exemplifies`, `related`); esses tipos alimentam o grafo usado pela [expansão por grafo](recuperacao.md) e pelos [MOCs hub](pipeline.md#fase-4b--garden-hub-porta-de-entrada-tematica).
- Os três blocos `auto-*` são gerenciados: qualquer coisa fora deles é preservada em atualizações. Veja [arquitetura.md](arquitetura.md#blocos-gerenciados).

---

## Outras saídas no vault

| Arquivo | Onde | Gerado por |
|---|---|---|
| `MOC - ULID - topico.md` | `40_MOCs/` | `garden` / `garden --hubs` (frontmatter carrega `origin` e, nos hubs, `hub_note_id`) |
| Resposta de pergunta | `00_Inbox/` | `zettel ask --save` (com seção **Fontes consultadas**) |
| `ART - ....md` | `00_Inbox/` | `zettel article --save` (não é indexado no Chroma) |
| Imagens | `90_Assets/` | `harvest` (Docling/Markdown) e `sync-manual` (adoção de imagens coladas) |

---

## Ver também

- [Pipeline](pipeline.md) — quem escreve cada arquivo, e quando
- [Arquitetura](arquitetura.md#blocos-gerenciados) — blocos gerenciados e IDs estáveis
- [Notas manuais](notas-manuais.md) — escrever essas mesmas notas à mão e adotá-las
