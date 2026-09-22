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

## Resumo geral

<!-- zettel:auto-source-summary:start -->
_Sem resumo geral. Rode `zettel summarize`._
<!-- zettel:auto-source-summary:end -->

## Mapa de capitulos

<!-- zettel:auto-chapter-map:start -->
### Parte I — Dois Sistemas
p. 19-30 - 1 nota permanente

_Sem resumo. Rode `zettel summarize`._

**Notas de literatura**
- [[Kahneman2011ThinkingFast/LIT - Kahneman2011 - p020 - sistema-1-0001|p. 20 — Sistema 1]]

**Notas permanentes**
- [[ZTL - 01HXYZ... - heuristicas-cognitivas|Heurísticas cognitivas]]
<!-- zettel:auto-chapter-map:end -->
```

O mapa de capítulos é o **único** lugar onde o índice lista as LIT aprovadas (e as ZTL que cada capítulo gerou), um link por linha. `review` o reescreve a cada aprovação (e a adoção de LIT manual no `sync-manual` também), `connect` atualiza a contagem de notas permanentes e `summarize` preenche os resumos.

O índice LIT **não** leva `## Topic Index` — nem ele nem os MOCs ([ADR-036](adrs/generated/RETRIEVAL/ADR-036-topic-index-routing-not-representation.md), emendas 2026-09-10 e 2026-09-22). O mapa termo → nota que alimenta o `ask` vive só no SQLite.

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

## Julgamento do autor

<!-- zettel:auto-decision:start -->
**Regras de decisão**

- Quando a decisão for repetitiva e de baixo risco, deixe o Sistema 1 agir, porque o custo de deliberar excede o do erro
<!-- zettel:auto-decision:end -->

## Trecho da fonte

<!-- zettel:auto-source-excerpt:start -->
The System 1 operates automatically and quickly, with little or no effort...
<!-- zettel:auto-source-excerpt:end -->

## Notas permanentes geradas

<!-- zettel:auto-lit-permanent:start -->
- [[ZTL - 01HXYZ... - heuristicas-cognitivas]]
<!-- zettel:auto-lit-permanent:end -->
```

Pontos importantes:

- O **draft** gerado pelo `extract` fica em `00_Inbox/Review/{Citekey}/` com o **mesmo basename** da nota aprovada — aprovar é mover, não regravar.
- O bloco `auto-source-excerpt` guarda o trecho integral da fonte, para auditoria lado a lado com o que o LLM produziu. A nota LIT **não é embeddada**: o texto-fonte já vive na coleção `chunks` e nada nunca consultou uma coleção de literatura.
- O bloco `auto-lit-permanent` aparece na aprovação (com um aviso enquanto nenhuma ZTL existe) e o `connect` o atualiza ao escrever as notas permanentes daquele chunk. O ULID da LIT (`literature_id`) e o da ZTL são diferentes, então é por aqui que se vai da LIT à nota.
- Uma LIT granular escrita à mão pode ser adotada pelo pipeline — veja [notas-manuais.md](notas-manuais.md).
- O bloco `auto-decision` só aparece quando algum candidato do chunk **enunciou** uma regra de decisão, um anti-padrão ou um framework nomeado. É gerenciado: edições fora dele sobrevivem. Como todo bloco `auto-*`, fica **fora** do texto embeddado ([ADR-034](adrs/generated/EXTRACT/ADR-034-optional-author-judgement-fields.md)).

---

## Nota Permanente (ZTL)

Fica em `30_Permanent/`.

```markdown
---
type: permanent
note_id: "01HXYZ..."
source_id: "@Kahneman2011ThinkingFast"
chunk_id: "@Kahneman2011ThinkingFast::ch001::a1b2c3d4"
page: 20
citation_page_confidence: quote
tags: [heurísticas, cognição, sistema-1]
decision_rules:
  - Quando a decisão for repetitiva e de baixo risco, deixe o Sistema 1 agir, porque o custo de deliberar excede o do erro
named_frameworks:
  - System 1 / System 2
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

- Ref. literatura: [[Kahneman2011ThinkingFast/LIT - Kahneman2011 - p020 - sistema-1-0001|p. 20 — Sistema 1]]
- Página: 20
- Localizador: p.20-25 / Capítulo 1

## Conexões

- [[ZTL - 01HABC... - vieses-cognitivos]] (extends) -- heurísticas como mecanismo gerador de vieses
- [[ZTL - 01HGHI... - dois-sistemas-de-raciocinio]] (corroborates) -- Outra fonte sustenta a mesma ideia

<!-- zettel:auto-backlinks:start -->
- [[ZTL - 01HDEF... - racionalidade-limitada]]
<!-- zettel:auto-backlinks:end -->

<!-- zettel:auto-moc-backrefs:start -->
- [[MOC - 01HJKL... - heuristicas-e-vieses]]
<!-- zettel:auto-moc-backrefs:end -->
```

Leitura dos campos:

- `literature_ref` aponta para a **LIT granular** do chunk que originou a nota (fallback: o índice da fonte). O alias carrega a página, então o link se lê sozinho.
- `page` é a **página impressa estrutural**, lida de `chunks.page_in_book` — é o campo para voltar ao material original. Ele também vai para o frontmatter, junto de `chunk_id`, então a nota tem trilha estrutural até o chunk. Fonte em Markdown nativo não tem página ([ADR-013](adrs/generated/HARVEST/ADR-013-three-layer-page-inference-strategy.md)): o campo é **omitido**, nunca escrito como nulo.
- `source_locator` é o localizador humano (`p.{page_in_book} / {section_path}`), **escrito pelo LLM** no Prompt 1 — o extractor só o preenche quando vem vazio ou com menos de 3 caracteres. Por isso ele é descritivo (útil para `section_path`), e não a página de referência: use `page`.
- `origin: pipeline | manual` distingue o que foi gerado do que foi escrito à mão.
- `## Conexões` é escrito pelo LLM com o **tipo da relação** (`supports`, `contradicts`, `extends`, `depends_on`, `exemplifies`, `related`) mais o `corroborates` que o **código** injeta quando outra fonte sustenta a mesma ideia (ver [pipeline.md](pipeline.md#fase-3--connect)); esse tipo não é oferecido ao LLM, e um `corroborates` que ele emita mesmo assim é rebaixado para `supports`; esses tipos alimentam o grafo usado pela [expansão por grafo](recuperacao.md) e pelos [MOCs hub](pipeline.md#fase-4b--garden-hub-porta-de-entrada-tematica). Analogias distantes **não** entram aqui: vão para o bloco `auto-connections` até o autor endossá-las na prosa.
- Os blocos `auto-*` são gerenciados: qualquer coisa fora deles é preservada em atualizações. Veja [arquitetura.md](arquitetura.md#blocos-gerenciados).

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
