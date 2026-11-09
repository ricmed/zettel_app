# Notas manuais e proveniência

[← Voltar ao README](../README.md)

Como escrever notas à mão no Obsidian e fazer o pipeline adotá-las com os mesmos direitos das notas geradas: `sync-manual`, `new-note`, `suggest-links`, adoção de LIT manual e o caminho LIT → ZTL.

Módulos: [`sync.py`](../zettel/sync.py), [`new_note.py`](../zettel/new_note.py), [`manual_lit.py`](../zettel/manual_lit.py), [`assets.py`](../zettel/assets.py).

---

## `zettel sync-manual`

Notas criadas à mão no Obsidian são adotadas pelo pipeline com **`zettel sync-manual`**, que varre as quatro pastas (`10_Sources`, `20_Literature`, `30_Permanent`, `40_MOCs`). Arquivos com `origin: pipeline` ou `hub_pipeline` (ZTL do `connect`, MOC do `garden`) são **`skipped`**: não adotam imagem, não re-embedam e não geram `auto-connections`. Quem já passou pelo pipeline não entra neste comando — para reindexar o vault use `zettel reindex`. LIT granular de pipeline só atualiza o path no SQLite se o arquivo foi movido:

- Notas sem `note_id`/`moc_id`/`source_id` recebem um id/citekey gerado, injetado no frontmatter.
- Cada nota ganha uma flag de proveniência `origin: manual | pipeline` (no frontmatter e no banco), permitindo distinguir o que foi escrito à mão do que foi gerado.
- SRC e LIT manuais deixam de ficar órfãos: são registrados no SQLite (e SRC é indexado no Chroma); uma LIT sem fonte resolvível cria uma fonte manual mínima para se vincular.
- **Uma SRC editada no Obsidian volta ao banco** (#346). Quando título, autores, ano, DOI, editora ou outro campo bibliográfico do frontmatter muda, o sync atualiza a fonte no SQLite.
  - Só mudam as colunas bibliográficas. Extração, paginação, capítulos, índice e custos de uma fonte do pipeline ficam como estão.
  - Os campos editados ficam marcados `user` em `bibliography_json.provenance`: o que você escreveu prevalece (ADR-057).
  - A fonte mínima que uma LIT criou antes da SRC ser sincronizada recebe os autores e o título da SRC.
  - Se autores ou ano mudam, a citação ABNT das ZTL dessa fonte é recalculada: em `notes.provenance_json` e no bloco `auto-evidence` da nota.
  - Se o título ou os autores mudam, o vetor da fonte é refeito. Offline, ele fica pendente.
- **LIT granular manual é adotada por completo** ([ADR-030](adrs/generated/MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)): o sync cria a linha de `chunks` que uma nota escrita à mão nunca teve (mais um capítulo sintético `Manual` por fonte), e a lista no mapa de capítulos (`auto-chapter-map`) do índice da fonte. A partir daí ela é indistinguível de uma LIT aprovada pelo pipeline e pode virar uma ZTL. **Não** aparece em `ask`/`article`: o `Retriever` pontua notas permanentes, nunca LIT (ADR-036) — e desde a remoção da coleção `literature_notes` não há sequer um vetor de LIT. Notas manuais **não passam pelo portão de aprovação** — entram como `persisted` direto, porque o portão existe para conteúdo gerado por LLM.
- **Imagens coladas no Obsidian são adotadas no sync** ([ADR-031](adrs/generated/ASSETS/ADR-031-vault-first-image-adoption.md)): cole ou arraste a imagem na nota (`![[figura.png]]` ou `![alt](pasta/figura.png)`) e o `sync-manual` copia o arquivo para `90_Assets/` com nome por hash de conteúdo, reescreve a referência para o caminho canônico e registra a linha em `assets` — a mesma que o harvest cria. Duas ressalvas: o arquivo original **não é apagado** (fica uma cópia onde você o colocou), e a **descrição multimodal não roda no sync** — o asset fica `pending` e é descrito depois por `zettel extract` (ou pelo botão *retry assets* na web), respeitando `images.enabled`. No formulário **Criar notas**, uma LIT granular ou uma ZTL aceita até seis imagens no próprio envio: o arquivo já nasce em `90_Assets/` e a linha `pending` é gravada se a fonte já está no SQLite. A descrição continua fora desse envio. Um ZTL criado a partir de uma LIT herda as figuras já embutidas nela e soma as enviadas no formulário, sem duplicar o caminho.

Além disso, o sync:

- Escreve sugestões de conexão num bloco gerenciado `auto-connections` (usando o mesmo `Retriever` do `connect`).
- Persiste como arestas `related` com `origin=manual` os `[[wikilinks]]` encontrados **no corpo** das notas, fora dos blocos gerenciados — sem nunca rebaixar uma aresta já tipada. O grafo pondera essas arestas como `manual` (0.95), não como `related` (0.5). As arestas são extraídas depois que todas as notas foram adotadas, então a ordem da varredura não perde links. Apagar um link do corpo remove a aresta manual, e os formatos `|apelido`, `#seção` e `[[ZTL - ID]]` também contam (#347). Veja [recuperacao.md](recuperacao.md#fechando-o-ciclo-do-grafo-notas-manuais).
- Roda `repair_permanent_links`, que reescreve alvos `[[ZTL - ULID]]` nus ou com prefixo duplicado para o stem atual do arquivo, e reconstrói todo bloco `auto-backlinks` a partir do grafo.
- Chama `sync_moc_backrefs` para MOCs manuais ou editados à mão.
- Um MOC gerado pelo `garden` só passa por aqui depois de adotado: `zettel adopt-moc <moc_id>` (ou **Adotar MOC** na web) troca a origem para `manual`, e a partir daí você pode editá-lo à vontade.

```bash
python -m zettel sync-manual
python -m zettel sync-manual --rebuild-graph   # re-deriva as arestas de todo o vault
```

### Sync sem internet e sem embedding

O `sync-manual` funciona offline (#343). Ele grava as notas primeiro no SQLite e no FTS, e é isso que as torna notas do acervo: elas aparecem na busca por texto, nas páginas da web, no grafo (as arestas dos `[[wikilinks]]` do corpo) e nos backrefs de MOC. O índice vetorial só é aberto quando alguma nota precisa de vetor.

Sem credencial de embedding, ou se o provedor cair no meio do run, o sync segue até o fim. A SRC e a ZTL que ficaram sem vetor contam como **vetores pendentes**: a CLI e o job da web mostram quantas são e por quê, e `zettel status` e `zettel doctor` mostram as notas nessa situação. As sugestões em `auto-connections`, que dependem da busca vetorial, ficam para depois.

O próximo `sync-manual` com embedding disponível grava os vetores pendentes e escreve as sugestões. Não é preciso editar as notas. Uma nota só é pulada quando o conteúdo e o vetor estão atuais.

### Notas apagadas ou movidas no Obsidian

O sync acompanha o que acontece com os arquivos (#348):

- **Renomear ou mover** uma ZTL ou um MOC, mantendo o `note_id`/`moc_id` no frontmatter, só atualiza o caminho no banco. A nota não é duplicada, e isso vale também para notas do pipeline. O resultado conta essas notas em **movidas**.
- **Apagar** uma ZTL ou um MOC deixa a linha no banco. O sync relata a nota como **sem arquivo no vault**, assim como o job da web e o `zettel doctor`.
- `zettel sync-manual --prune` remove as notas **manuais** apagadas:
  - da ZTL, saem a linha no SQLite, a busca por texto, as arestas e o vetor no Chroma, e os `auto-backlinks` das vizinhas são reconstruídos;
  - do MOC, saem os backrefs das notas que ele listava, o índice de tópicos e a linha no banco.

  Uma nota do pipeline apagada é só relatada, porque `zettel rebuild --what vault` a reescreve a partir do banco. O `--prune` fica só na CLI, por ser destrutivo.

Um MOC escrito à mão com nome livre (`MOC - tema.md`) agora também perde o backref de uma nota que sai da lista dele. Antes, esses backrefs nunca eram removidos.

### Notas que o sync não adota

Cada nota é processada por conta própria (#342). Uma nota que o sync não consegue adotar fica de fora sem impedir as demais. A CLI e a página do job na web listam essas notas como **não adotadas**, com o caminho e o motivo, e o arquivo não é alterado. Os casos são:

| Caso | O que fazer |
|---|---|
| Frontmatter com YAML inválido, aberto com `---` e nunca fechado, ou que não é um mapa `chave: valor` | Corrigir o frontmatter. O sync nunca reescreve um frontmatter que não conseguiu ler. |
| LIT dentro da pasta de uma fonte (`20_Literature/{Citekey}/`) sem `type: literature` e `chunk_id` | Acrescentar `chunk_id: '@Citekey::manual::NNNN'`, ou mover a nota para a raiz de `20_Literature/` se ela for o índice. |
| LIT granular com `origin` diferente de `manual` (ou sem `origin`) e diferente de `pipeline` | Usar `origin: manual`. |
| Dois arquivos com o mesmo `chunk_id` | O primeiro em ordem alfabética é adotado; trocar o `chunk_id` do outro. |
| Qualquer outro erro inesperado numa nota | A mensagem traz o tipo do erro, e o detalhe vai para o log. |

Um erro do SQLite não é culpa da nota e interrompe o sync inteiro. Uma queda do provedor de embedding não interrompe: ela deixa os vetores pendentes (veja acima).

Outras tolerâncias:

- Datas sem aspas, como a propriedade Date do Obsidian grava (`created: 2026-11-03`), são aceitas e gravadas no banco como texto ISO.
- `author: "Daniel Kahneman"` como texto vale como um autor, e não como uma lista de letras.
- Os textos de preenchimento do scaffold (`_Preencha a tese._`, `_Preencha a definicao._`, `_Preencha os limites._`, `_Preencha o resumo._`, `_Cole o trecho da fonte aqui._`) não entram no embedding nem no checksum semântico.

> Nota importante sobre o dispatch: `sync._sync_literature` só encaminha para a adoção completa quando a nota tem `origin: manual` (ou não tem `origin`). LITs granulares do pipeline continuam no caminho leve: se o caminho e o `literature_id` já batem com o chunk, a nota é `skipped`; se o arquivo foi movido, só o path no SQLite é atualizado. O `status: approved` do frontmatter **não** é copiado para `chunks.status` — depois do `review` essa linha é `persisted`.

---

## Scaffold com `zettel new-note`

Para criar notas manuais com frontmatter e corpo padronizados (sem indexar ainda), use **`zettel new-note`**. O comando só grava o `.md` no vault com `origin: manual`; rode **`zettel sync-manual`** em seguida para registrar no SQLite/Chroma, sugerir conexões e sincronizar backrefs de MOC.

A página **Criar notas** (`/notes/new`) oferece o mesmo scaffold para SRC, LIT e ZTL, sempre sem sobrescrita forçada. Fontes e LITs granulares já conhecidas aparecem como seleções seguras. Na SRC, **Montar referência ABNT** preenche a citação a partir dos campos já digitados (sem LLM); **Completar com LLM** só dispara com DOI, URL ou trecho colado e devolve campos para você revisar — a nota só é gravada em **Criar nota**. Na LIT granular o formulário inclui trecho da fonte, resumo, conceitos-chave e um candidato a permanente; o índice da fonte continua só metadados (ele já nasce com a SRC). ZTL a partir de LIT é enfileirada; com LLM, ela reutiliza o connector e já é indexada. LIT granular, ZTL em branco e ZTL a partir de LIT aceitam até seis imagens (8 MB cada). A ZTL a partir de LIT também herda as figuras da LIT. A ZTL em branco só anexa imagem se a fonte já estiver no SQLite. MOC continua exclusiva da CLI.

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
- **`--from-lit`** (so para `ztl`): cria a nota permanente **a partir de uma nota de literatura** — aceita o caminho do `.md` ou o `chunk_id`. Preenche `source_id`, `literature_ref` (apontando para a LIT granular, nao para o indice) e `source_locator` automaticamente; as figuras já embutidas na LIT vão para `## Figuras`
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

## Criar notas direto no Obsidian (templates)

As notas também podem nascer no Obsidian, sem rede e sem LLM, com a mesma estrutura que o `new-note` grava (#344). O comando abaixo gera os templates e o script em `99_Templates/`:

```bash
python -m zettel obsidian-templates           # grava o que falta
python -m zettel obsidian-templates --force   # regrava tudo (depois de atualizar o Zettel)
```

Os templates não são escritos à mão. `zettel/obsidian.py` chama os mesmos scaffolds do `new-note` (`source_scaffold`, `literature_scaffold`, `moc_scaffold`, e para o ZTL o corpo de `--from-lit` via `scaffold_permanent_note_body`, todos sobre `zettel/vault.py`) com valores-sentinela e traduz cada sentinela para JavaScript. Uma mudança num builder chega aos templates na próxima geração, e um teste roda o script gerado no Node e compara cada nota com a do `new-note`.

O que o script `99_Templates/scripts/zettel.js` faz em cada template:

| Template | Pergunta | Gera | Arquivo |
|---|---|---|---|
| `ZTL.md` | fonte; LIT granular dessa fonte; tese (Enter deriva da LIT, como `--thesis`) | ULID, `literature_ref`, `chunk_id`, citação ABNT no `auto-evidence` — o scaffold de `new-note ztl --from-lit` **sem** `--llm` | `30_Permanent/ZTL - <ULID> - <slug>.md` |
| `SRC.md` | título; autores (`Nome Sobrenome; ...`); ano (opcional) | citekey provisório, com sufixo `a`, `b`... se já existir no vault | `10_Sources/SRC - <AutorAno> - <slug>.md` e o índice `20_Literature/LIT - <AutorAno> - <slug>.md` |
| `LIT - granular.md` | fonte; tópico; página impressa (opcional) | o próximo `chunk_index` da fonte no vault, e com ele `chunk_id` e `literature_id` | `20_Literature/<Citekey>/LIT - <AutorAno> - pNNN - <topico>-NNNN.md` |
| `LIT - indice.md` | fonte | — | `20_Literature/LIT - <AutorAno> - <slug>.md` |
| `MOC.md` | tema | ULID | `40_MOCs/MOC - <ULID> - <slug>.md` |

O ZTL do Templater exige uma LIT já preenchida (`## Resumo` ou `## Candidatos a Nota Permanente`, e de preferência o trecho em `auto-source-excerpt`). Placeholders do scaffold não derivam tese. O Prompt 2 (`--llm`) continua só na CLI e na web: `zettel new-note ztl --from-lit … --llm`. ZTL em branco (só título, sem LIT) também: `zettel new-note ztl` ou `/notes/new`.

Use **Templater: Create new note from template** (ou o atalho *Create* do template). *Insert template* e o ícone da faixa escrevem na nota aberta: o fluxo move esse arquivo, e um ZTL já gerado seria reescrito.

Como o ULID já está no nome, os `[[wikilinks]]` que o Obsidian completa saem no formato `[[ZTL - <ULID> - slug]]` que o `sync-manual` transforma em arestas. As datas vão entre aspas, no fuso do vault (`vault_timezone`), e os valores do frontmatter vão como JSON, então um título com `:` continua sendo um texto só.

### O vault já sai pronto para o Obsidian

O `zettel init` prepara o vault (#345), e o `zettel obsidian-setup` faz o mesmo num vault que já existe:

- grava os templates e o script, como o `obsidian-templates`;
- habilita o plugin **Templater** em `.obsidian/community-plugins.json`, mantendo os outros plugins da lista;
- configura o Templater em `.obsidian/plugins/templater-obsidian/data.json`:
  - pasta de templates `99_Templates` e de scripts `99_Templates/scripts`;
  - *folder templates*: `10_Sources` → SRC, `20_Literature` → LIT granular, `30_Permanent` → ZTL, `40_MOCs` → MOC;
  - um atalho registrado para cada template.

  As outras configurações e as *folder templates* de outras pastas são preservadas.
- instala os arquivos do plugin (`main.js`, `manifest.json`, `styles.css`) da versão fixada em `obsidian.templater_version`. O config operacional fixa a 2.26.0, que exige Obsidian 1.13 ou mais novo, com os sha256 em `obsidian.templater_sha256`. A ordem de busca é:
  1. a pasta indicada em `--templater-from`;
  2. o cache em `data/cache/obsidian/`;
  3. o release no GitHub, baixado uma única vez para o cache.

  Um hash que não confere é recusado. Sem rede e sem cache, o resto é gravado mesmo assim, e o comando explica como instalar o plugin pela loja do Obsidian.

```bash
python -m zettel obsidian-setup                                # vault existente
python -m zettel obsidian-setup --templater-from ./templater   # maquina sem rede
python -m zettel obsidian-setup --force                        # regrava templates e reinstala o plugin
```

Como o vault não é recriado, recriar a base (`init --reset`) não remove o plugin nem as configurações (#341).

Duas ações no próprio Obsidian continuam sendo suas, e o Zettel não as faz por você:

1. **Confiar no vault.** Na primeira abertura, o Obsidian pergunta se você confia no autor do vault e quer habilitar os plugins da comunidade.
2. **Ligar o gatilho nas pastas, se quiser.** Para que uma nota nova criada numa pasta já saia com o template, ligue *Trigger Templater on new file creation* nas configurações do Templater, **em cada dispositivo**. Desde a 2.x o Templater guarda essa chave fora do `data.json`, de propósito: com ela ligada, ele executa as tags `<% %>` de todo arquivo novo, inclusive o texto de uma fonte que o pipeline grava enquanto o Obsidian está aberto. Sem o gatilho, use *Templater: Create new note from template* (ou o atalho do template). O fluxo move a nota para a pasta certa de qualquer jeito.

Para adotar as notas no banco, rode `zettel sync-manual`, que também funciona offline. O `zettel doctor` mostra se os templates e o Templater estão no vault.

O `new-note lit --granular` sem `--chunk-index` usa o mesmo critério: o próximo índice livre entre o vault e o SQLite (`new_note.next_manual_chunk_index`). Antes o padrão era sempre 1, e duas notas escritas offline colidiam. O formulário da web também passou a contar o vault.

## Por dentro da adoção (`manual_lit.py`)

A adoção completa de uma LIT escrita à mão ([ADR-030](adrs/generated/MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)):

- `adopt_manual_literature` sintetiza a linha de `chunks` que a nota nunca teve (mais um capítulo `{source_id}::manual` "Manual" por fonte, separado do `::ch000` que o harvest de um PDF da mesma fonte grava depois — `chunks.chapter_id` é `NOT NULL` com FK), reconstrói o `summary_json` a partir das seções `## Resumo` / `## Conceitos-chave` / `## Candidatos a Nota Permanente` da própria nota, tira o `chunks.text` do bloco `auto-source-excerpt`, grava `status='persisted'` e chama `review._refresh_literature_index` (sem embedding, como no caminho do pipeline). É idempotente por checksum sobre excerto + corpo.
- Deliberadamente **não** escreve na coleção Chroma `chunks`: os limiares dessa coleção são calibrados para a dedupe do harvest sobre distância L2 crua.
- `create_permanent_from_literature` deriva um `PermanentNoteCandidate` da nota. Com `--llm`, persiste um concept `approved` e chama `connector.run_connect(..., origin="manual")` — mesmo Prompt 2, mesmo RAG, mesmas relações e backlinks. Sem `--llm`, escreve um scaffold pré-preenchido e **consome** conceitos `approved` já existentes daquele chunk/tese (`status=noted`), para que um `connect` posterior não duplique a nota. `sync-manual` faz o mesmo ao adotar uma ZTL escrita à mão.

---

## Notas de leitura pela Estante

Na página de um livro (`/books/{id}`), **Nova nota de leitura** escreve uma LIT manual granular e a adota na hora, como um `sync-manual` daquela nota ([ADR-068](adrs/generated/WEB/ADR-068-estante-books-as-liv-notes.md)).

- **Livro sem fonte.** Ganha antes um SRC manual com o título, os autores e o ISBN da LIV. Assim o PDF colhido depois cai na mesma fonte, pelo ISBN.
- **Leitura reflexiva.** O formulário pede o tipo (metáfora, pensamento, analogia, questão filosófica, personagem, cena), os personagens, a ideia, a reflexão e o uso como analogia. A LIT guarda `reading_mode`, `reflection_kind` e `characters` no frontmatter, e a ZTL gerada dela herda esses campos.
- **Gerar ZTL.** É o mesmo job `manual-ztl-from-lit` do `/notes/new`, com ou sem LLM.

---

## Conexões sem reescrita (`zettel suggest-links`)

Para ficção, filme ou nota autoral, o caminho é SRC → ZTL à mão — **não** harvest + extract do livro inteiro:

```bash
python -m zettel new-note src "QualityLand" --citekey Kling2017 --author "Marc-Uwe Kling" --year 2017 --document-type livro
python -m zettel new-note ztl "Classificacao de cidadaos como condicao de acesso" --source-id @Kling2017
python -m zettel sync-manual
python -m zettel suggest-links 01HABC00000000000000000000
```

`suggest-links` grava RAG (incluindo analogias distantes) em `auto-connections` e **deixa o texto do autor intacto**. Não chama o Prompt 2 e não herda o portão `ConnectRejected` — esse portão foi desenhado para saída de LLM. Uma sugestão só vira aresta quando você move o wikilink para a prosa.

A nota precisa estar indexada (`sync-manual` antes). Flags em [cli.md](cli.md#suggest-links).

---

## Ver também

- [Comandos](cli.md#new-note) — flags de `new-note`, `sync-manual` e `suggest-links`
- [Notas geradas](notas.md) — o formato que suas notas manuais devem seguir
- [Recuperação](recuperacao.md) — como as notas manuais entram no grafo e no `ask`
- [Operação](operacao.md) — `rebuild` nunca sobrescreve uma nota `origin: manual`
