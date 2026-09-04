# Notas manuais e proveniência

[← Voltar ao README](../README.md)

Como escrever notas à mão no Obsidian e fazer o pipeline adotá-las com os mesmos direitos das notas geradas: `sync-manual`, `new-note`, adoção de LIT manual e o caminho LIT → ZTL.

Módulos: [`sync.py`](../zettel/sync.py), [`new_note.py`](../zettel/new_note.py), [`manual_lit.py`](../zettel/manual_lit.py), [`assets.py`](../zettel/assets.py).

---

## `zettel sync-manual`

Notas criadas à mão no Obsidian são adotadas pelo pipeline com **`zettel sync-manual`**, que varre as quatro pastas (`10_Sources`, `20_Literature`, `30_Permanent`, `40_MOCs`). Arquivos com `origin: pipeline` ou `hub_pipeline` (ZTL do `connect`, MOC do `garden`) são **`skipped`**: não adotam imagem, não re-embedam e não geram `auto-connections`. Quem já passou pelo pipeline não entra neste comando — para reindexar o vault use `zettel reindex`. LIT granular de pipeline só atualiza o path no SQLite se o arquivo foi movido:

- Notas sem `note_id`/`moc_id`/`source_id` recebem um id/citekey gerado, injetado no frontmatter.
- Cada nota ganha uma flag de proveniência `origin: manual | pipeline` (no frontmatter e no banco), permitindo distinguir o que foi escrito à mão do que foi gerado.
- SRC e LIT manuais deixam de ficar órfãos: são registrados no SQLite (e SRC é indexado no Chroma); uma LIT sem fonte resolvível cria uma fonte manual mínima para se vincular.
- **LIT granular manual é adotada por completo** ([ADR-030](adrs/generated/MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)): o sync cria a linha de `chunks` que uma nota escrita à mão nunca teve (mais um capítulo sintético `Manual` por fonte), embeda a nota em `literature_notes` e a adiciona ao bloco `auto-lit-index` do índice da fonte. A partir daí ela é indistinguível de uma LIT aprovada pelo pipeline: aparece em `ask`/`article` e pode virar uma ZTL. Notas manuais **não passam pelo portão de aprovação** — entram como `persisted` direto, porque o portão existe para conteúdo gerado por LLM.
- **Imagens coladas no Obsidian são adotadas no sync** ([ADR-031](adrs/generated/ASSETS/ADR-031-vault-first-image-adoption.md)): cole ou arraste a imagem na nota (`![[figura.png]]` ou `![alt](pasta/figura.png)`) e o `sync-manual` copia o arquivo para `90_Assets/` com nome por hash de conteúdo, reescreve a referência para o caminho canônico e registra a linha em `assets` — a mesma que o harvest cria. Duas ressalvas: o arquivo original **não é apagado** (fica uma cópia onde você o colocou), e a **descrição multimodal não roda no sync** — o asset fica `pending` e é descrito depois por `zettel extract` (ou pelo botão *retry assets* na web), respeitando `images.enabled`.

Além disso, o sync:

- Escreve sugestões de conexão num bloco gerenciado `auto-connections` (usando o mesmo `Retriever` do `connect`).
- Persiste como arestas `related` os `[[wikilinks]]` encontrados **no corpo** das notas, fora dos blocos gerenciados — sem nunca rebaixar uma aresta já tipada. Veja [recuperacao.md](recuperacao.md#fechando-o-ciclo-do-grafo-notas-manuais).
- Roda `repair_permanent_links`, que reescreve alvos `[[ZTL - ULID]]` nus ou com prefixo duplicado para o stem atual do arquivo, e reconstrói todo bloco `auto-backlinks` a partir do grafo.
- Chama `sync_moc_backrefs` para MOCs manuais ou editados à mão.

```bash
python -m zettel sync-manual
python -m zettel sync-manual --rebuild-graph   # re-deriva as arestas de todo o vault
```

> Nota importante sobre o dispatch: `sync._sync_literature` só encaminha para a adoção completa quando a nota tem `origin: manual`. LITs granulares do pipeline continuam no caminho leve: se o caminho e o `literature_id` já batem com o chunk, a nota é `skipped`; se o arquivo foi movido, só o path no SQLite é atualizado. O `status: approved` do frontmatter **não** é copiado para `chunks.status` — depois do `review` essa linha é `persisted`.

---

## Scaffold com `zettel new-note`

Para criar notas manuais com frontmatter e corpo padronizados (sem indexar ainda), use **`zettel new-note`**. O comando só grava o `.md` no vault com `origin: manual`; rode **`zettel sync-manual`** em seguida para registrar no SQLite/Chroma, sugerir conexões e sincronizar backrefs de MOC.

A página **Criar notas** (`/notes/new`) oferece o mesmo scaffold para SRC, LIT e ZTL, sempre sem sobrescrita forçada. Fontes e LITs granulares já conhecidas aparecem como seleções seguras. Na SRC, **Montar referência ABNT** preenche a citação a partir dos campos já digitados (sem LLM); **Completar com LLM** só dispara com DOI, URL ou trecho colado e devolve campos para você revisar — a nota só é gravada em **Criar nota**. Na LIT granular o formulário inclui trecho da fonte, resumo, conceitos-chave e um candidato a permanente; o índice da fonte continua só metadados (ele já nasce com a SRC). ZTL a partir de LIT é enfileirada; com LLM, ela reutiliza o connector e já é indexada. MOC continua exclusiva da CLI.

Tipos aceitos: `ztl`, `lit`, `src`, `moc` (aliases `permanent`, `literature`, `source`).

| Tipo | Destino | Comportamento |
|------|---------|---------------|
| `ztl` | `30_Permanent/` | ULID novo, secoes ZTL vazias + bloco `auto-connections` placeholder; `--source-id`/`-s` ou `--citekey`/`-k` vincula a uma SRC |
| `src` | `10_Sources/` | Citekey via `--citekey`/`-k` ou derivado de autor/ano/titulo; campos ABNT opcionais; secao no corpo para vincular ZTL. Cria tambem o **indice de literatura** da fonte, como o harvest faz |
| `lit` | `20_Literature/` | Indice na raiz (padrao) ou granular em `{Citekey}/` com `--granular`; `-s`/`-k` vincula a uma SRC existente |
| `moc` | `40_MOCs/` | ULID novo, secoes vazias para preencher links |

Flags úteis:

- **`--citekey` / `-k`**, **`--author` / `-a`** (repita para varios autores), **`--year` / `-y`**: metadados de SRC/LIT
- **`--source-id` / `-s`**: citekey explicito para SRC (alias de `-k`) ou vinculo de ZTL a uma SRC existente
- **`--document-type` / `-t`**, **`--abnt-reference`**, **`--publisher`**, **`--place`**, **`--doi`**, **`--url`**, **`--journal`**, **`--edition`**, **`--institution`**, **`--pages`**: campos bibliograficos da SRC; para ZTL, `-k` equivale a `--source-id`
- **`--source-id` / `-s`**: citekey da fonte (`@` opcional) para ZTL — preenche `source_id` no frontmatter e wikilink SRC na secao **Fonte**
- **`--granular`**: LIT por chunk em `20_Literature/{Citekey}/` (nao indice)
- **`--chunk-index`**, **`--page` / `-p`**: indice e pagina impressa da LIT granular
- **`--from-lit`** (so para `ztl`): cria a nota permanente **a partir de uma nota de literatura** — aceita o caminho do `.md` ou o `chunk_id`. Preenche `source_id`, `literature_ref` (apontando para a LIT granular, nao para o indice) e `source_locator` automaticamente
- **`--llm`** (com `--from-lit`): gera o conteudo com o LLM reusando o **Prompt 2 do connector** — mesmo RAG hibrido, mesma tipagem de relacoes, mesmos backlinks — e ja indexa a nota (`origin: manual`). Sem `--llm`, voce recebe um scaffold pre-preenchido para escrever e adotar depois com `sync-manual`. Nenhum dos dois caminhos passa por aprovacao
- **`--thesis`** (com `--from-lit`): tese explicita. Por padrao ela e deduzida da nota de literatura, nesta ordem: primeiro item de `## Candidatos a Nota Permanente`, senao o primeiro paragrafo de `## Resumo`
- **`--force`**: sobrescreve arquivo existente no mesmo caminho (padrao: erro se ja existir)

> Atenção: em `new-note`, `-y` é o atalho de `--year`, **não** de `--yes`.

Exemplo de fluxo:

```bash
python -m zettel new-note ztl "Heuristicas como atalhos mentais"
python -m zettel new-note ztl "Recuperacao hibrida" -s @Kahneman2011ThinkingFast
python -m zettel new-note src "Thinking, Fast and Slow" -a Kahneman -y 2011
python -m zettel new-note lit "Sistema 1" -s @Kahneman2011ThinkingFast --granular -p 20
# Edite a LIT no Obsidian (resumo, conceitos, trecho, imagens), depois:
python -m zettel sync-manual

# Da LIT para uma nota permanente:
python -m zettel new-note ztl --from-lit "vault/20_Literature/Kahneman2011ThinkingFast/LIT - Kahneman2011 - p020 - sistema-1-0001.md"
python -m zettel new-note ztl --from-lit "@Kahneman2011ThinkingFast::manual::0001" --llm
```

> Com `--from-lit` o titulo posicional e dispensavel: ele vem da tese derivada da nota de literatura (ou de `--thesis`).

---

## Por dentro da adoção (`manual_lit.py`)

A adoção completa de uma LIT escrita à mão ([ADR-030](adrs/generated/MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)):

- `adopt_manual_literature` sintetiza a linha de `chunks` que a nota nunca teve (mais um capítulo `{source_id}::ch000` "Manual" por fonte — `chunks.chapter_id` é `NOT NULL` com FK), reconstrói o `summary_json` a partir das seções `## Resumo` / `## Conceitos-chave` / `## Candidatos a Nota Permanente` da própria nota, tira o `chunks.text` do bloco `auto-source-excerpt`, grava `status='persisted'`, embeda via `review._literature_embed_text` em `literature_notes` e chama `review._refresh_literature_index`. É idempotente por checksum sobre excerto + corpo.
- Deliberadamente **não** escreve na coleção Chroma `chunks`: os limiares dessa coleção são calibrados para a dedupe do harvest sobre distância L2 crua.
- `create_permanent_from_literature` deriva um `PermanentNoteCandidate` da nota. Com `--llm`, persiste um concept `approved` e chama `connector.run_connect(..., origin="manual")` — mesmo Prompt 2, mesmo RAG, mesmas relações e backlinks. Sem `--llm`, escreve um scaffold pré-preenchido e **consome** conceitos `approved` já existentes daquele chunk/tese (`status=noted`), para que um `connect` posterior não duplique a nota. `sync-manual` faz o mesmo ao adotar uma ZTL escrita à mão.

---

## Ver também

- [Comandos](cli.md#new-note) — todas as flags de `new-note` e `sync-manual`
- [Notas geradas](notas.md) — o formato que suas notas manuais devem seguir
- [Recuperação](recuperacao.md) — como as notas manuais entram no grafo e no `ask`
- [Operação](operacao.md) — `rebuild` nunca sobrescreve uma nota `origin: manual`
