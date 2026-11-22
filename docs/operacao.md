# Operação: retenção, reconstrução e remoção

[← Voltar ao README](../README.md)

O que é fonte de verdade, o que é cache descartável, como reconstruir cada camada sem gastar LLM e como remover dados de forma permanente.

---

## Retenção e reconstrução

O **SQLite é a fonte de verdade durável** do estado do pipeline: o texto extraído de cada fonte, o grafo, o cache de LLM, os candidatos (`candidate_json`) e as descrições de imagens. Os **bytes das imagens não** ficam no SQLite — estão em `vault/90_Assets/` e, num `zettel backup`, dentro do arquivo. Os **embeddings não** ficam no SQLite; `zettel reindex` os recomputa.

O Markdown que o `rebuild` e o `init` conseguem repor vem da retenção (`retention.flush`), no fim do `sync-manual` e no começo do `backup`. Ela corre para toda origem. A chave é o sha256 do arquivo inteiro, então uma edição só de frontmatter também atualiza a linha. Não é só o texto que vai para o embedding.

| O quê | Coluna | Se o arquivo sumiu |
|---|---|---|
| ZTL | `notes.body` + `notes.frontmatter_json`, com `notes.file_checksum` | a nota, blocos gerenciados inclusive |
| MOC | `mocs.body` + `mocs.frontmatter_json`, com `mocs.file_checksum` | o MOC |
| LIT granular | `chunks.literature_body` (o arquivo inteiro) | esse Markdown; coluna vazia cai em `summary_json` |
| Índice LIT | `sources.lit_body` (o arquivo inteiro) | o índice |
| Ficha SRC | `sources.card_body` (o arquivo inteiro) | a ficha, com `## Resumo` e `## Impressões` |
| Mesa | `projects` (`project_id`, `path`, `body`, `frontmatter_json`, `file_checksum`) | o arquivo, se o caminho gravado continua dentro do vault |

O arquivo da Mesa continua o registro vivo: a Mesa não lê o SQLite para abrir um projeto. Um arquivo que já existe não é sobrescrito. `--force` no `rebuild` também não sobrescreve `origin: manual`.

### `zettel reindex`

Reconstrói o ChromaDB inteiro a partir do SQLite, sem nenhuma chamada de LLM e sem reescrever o vault. O índice vetorial passa a ser um cache descartável. Um `reindex` completo também reconstrói o índice lexical FTS5 (`fts_notes`/`fts_chunks`), igualmente descartável.

```bash
python -m zettel reindex
python -m zettel reindex --collection chunks --force
python -m zettel reindex --force            # obrigatorio apos trocar embedding
python -m zettel reindex --force --yes      # sem prompt (scripts/CI)
```

Após trocar `embedding.provider` / `model` / `dimensions`, use **`--force`** (o CLI também detecta o drift e força o reset sob confirmação). Veja [configuracao.md](configuracao.md#trocar-o-modelo-de-embedding).

### `zettel rebuild`

Recria os arquivos `.md` do vault a partir dos corpos persistidos, também sem LLM.

```bash
python -m zettel rebuild --what vault          # recria os .md
python -m zettel rebuild --what chroma         # equivalente a reindex
python -m zettel rebuild --what all --dry-run  # simula vault + chroma
python -m zettel rebuild --what vault --force  # sobrescreve arquivos existentes
```

Nunca sobrescreve um arquivo existente sem `--force`, e **nunca** sobrescreve uma nota `origin: manual` (mesmo com `--force`). Um arquivo manual que sumiu, e cuja linha a retenção ainda tem, é recriado. `zettel init` sem `--reset` faz o mesmo para o que falta no disco e, quando grava algum, imprime `{n} arquivo(s) recriado(s) a partir do banco.`

### `zettel rechunk`

Re-aplica a configuração de chunking atual a partir do texto extraído persistido, sem reprocessar o arquivo original; completa capítulos faltantes após harvest interrompido e re-vincula imagens aos capítulos corretos.

```bash
python -m zettel rechunk --all
python -m zettel rechunk --source-id @AutorAnoTitulo
python -m zettel rechunk --source-id @AutorAnoTitulo --dump-chunks
```

Se o texto extraído tiver marcadores `<!-- zettel:page N -->` cobrindo `1..total_pages_file`, o mapa de páginas é reconstruído a partir deles; senão fica sem mapa de páginas (não há fallback de extrator — [ADR-012](adrs/generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md)). O marcador antigo `<!-- zettel:page-break -->` não conta. Com `--dump-chunks`, grava um markdown com todos os chunks (texto + metadados) em `data/cache/chunk-dumps/` (ou `--dump-dir`).

### `zettel dump-chunks`

Reexporta os chunks já persistidos no SQLite como markdown, sem rechunkar nem chamar o LLM. Use para inspecionar cortes, `section_path`, páginas e overlap **antes** de mudar `chunk_size` / `chunk_overlap` / `min_section_chars`.

```bash
python -m zettel dump-chunks --source-id @AutorAnoTitulo
python -m zettel dump-chunks --all --dump-dir ./tmp/chunks
```

### `zettel dump-extraction`

Reexporta o Markdown extraído (`sources.extracted_text`: saída do Docling em PDF, ou o corpo MD nativo) com headings H1–H6 intactos, sem rerodar o extrator. Em PDF, o texto pode incluir comentários `<!-- zettel:page N -->` (uma entrada por página do arquivo, inclusive vazias).

```bash
python -m zettel dump-extraction --source-id @AutorAnoTitulo
python -m zettel dump-extraction --all --dump-dir ./tmp/extraction
```

`harvest --dump-extraction` grava o mesmo arquivo assim que o texto é persistido (antes dos embeddings). Default: `data/cache/extraction-dumps/` (ou `--dump-extraction-dir` no harvest / `--dump-dir` no comando dedicado).

### `zettel retry-failed`

```bash
python -m zettel retry-failed                        # chunks com falha -> pending
python -m zettel retry-failed --source-id @Citekey
python -m zettel retry-failed --assets               # imagens com falha -> pending
```

Depois de resetar, rode `extract` novamente.

### `zettel garden --recreate`

Depois de mudar `category_label_template` ou `config/moc_topics.yaml`, os MOCs existentes ficam desalinhados com os rótulos embeddados. Regenere:

```bash
python -m zettel garden --recreate -y
python -m zettel garden --hubs --recreate -y   # so os MOCs de hub
```

`--recreate` no pipeline taxonômico não apaga MOCs `origin=hub_pipeline`, e o inverso também vale.

### Modelos de cluster e `zettel garden --refit`

O garden guarda o modelo de cluster de cada categoria em `data/cache/garden/` ([ADR-061](adrs/generated/GARDEN/ADR-061-stable-mocs-persisted-bucket-models.md)). Uma execução normal reaproveita o modelo: notas novas entram nos clusters existentes e uma execução sem nota nova não muda nenhum MOC nem chama o LLM. O modelo é reajustado com `--refit`, quando mudam o embedding, a taxonomia ou os parâmetros de cluster, ou quando as notas novas de uma categoria passam de `gardener.model_refit_growth` (50%). Depois de um reajuste, uma nota que mudou de cluster sai do MOC antigo (o relatório mostra quantas saíram). Apagar `data/cache/garden/` equivale a um `--refit`.

### Adotar um MOC gerado

`zettel adopt-moc <moc_id>` (ou **Adotar MOC** na página do MOC na web) transforma um MOC do garden em MOC manual: `origin: manual` no arquivo e no banco, sem assinatura. Ele mantém os backrefs e passa a ser sincronizado por `zettel sync-manual`; o garden e o `--recreate` não o tocam mais. MOCs hub cuja nota-hub foi apagada aparecem no relatório do `garden --hubs` e no `zettel doctor`.

---

## `purge-rejected`

Remove permanentemente os chunks marcados como `rejected` no review.

```bash
python -m zettel purge-rejected                # apaga rejected + VACUUM
python -m zettel purge-rejected --yes          # sem confirmacao
python -m zettel purge-rejected --no-compact   # so apaga, sem compactar disco
python -m zettel purge-rejected --source-id @Citekey
```

O que sai: linhas em `chunks` e `concepts` (+ FTS) no SQLite e embeddings na coleção Chroma `chunks`. Notas permanentes e LITs aprovadas **não** são afetadas.

Por padrão roda `VACUUM` em `state.db` e `chroma.sqlite3` — recupera espaço em disco sem alterar os dados restantes. `--no-compact` pula essa etapa.

---

## Remover fonte com `zettel delete-source`

Comando **irreversível** que apaga uma fonte harvestada por `@Citekey` do vault, do SQLite e do Chroma. Diferente de `purge-rejected` (só chunks rejeitados na revisão), remove a fonte inteira.

```bash
python -m zettel delete-source '@Citekey'
python -m zettel delete-source '@Citekey' --yes              # sem confirmacao
python -m zettel delete-source '@Citekey' --delete-permanent # apaga ZTL ligadas
python -m zettel delete-source '@Citekey' --no-compact       # sem VACUUM
```

**Removido:**

- Vault: nota **SRC**, **índice LIT**, pasta de **LIT granulares** (`20_Literature/{Citekey}/`), **drafts** em `00_Inbox/Review/{Citekey}/`, **assets** em `90_Assets/` ligados à fonte
- SQLite: fonte, capítulos, chunks, concepts, assets, arquivos (`files`) — cascade completo
- Chroma: collection `sources` e chunks da fonte (LIT não tem vetor)

**Mantido por padrão (sem `--delete-permanent`):**

- Notas **permanentes (ZTL)** geradas a partir da fonte — o campo `source_id` é limpo no banco e wikilinks mortos para SRC/LIT removidos são stripados de **todo** o vault (incluindo MOCs e outras ZTL)
- MOCs, notas de outras fontes e o restante do acervo

**Com `--delete-permanent`:** apaga também as ZTL ligadas à fonte (vault + SQLite + Chroma `permanent_notes`).

Por padrão roda **`VACUUM`** em `state.db` e `chroma.sqlite3` após a exclusão (como `purge-rejected`); use **`--no-compact`** para pular. Confirme com **`--yes`** / **`-y`** em scripts.

---

## `init --reset`

```bash
python -m zettel init            # cria as pastas que faltam e recria do banco o .md que o disco não tem
python -m zettel init --reset    # + apaga State DB, ChromaDB, cache e as notas do pipeline (pede confirmacao)
```

`init` nunca apaga o vault (#341). Sem `--reset`, recria os arquivos que o banco já tem e o disco não tem. Quando grava algum, imprime `{n} arquivo(s) recriado(s) a partir do banco.` Com `obsidian.setup_on_init`, grava os templates e configura o Templater no vault ([notas-manuais.md](notas-manuais.md#o-vault-já-sai-pronto-para-o-obsidian)). O `--reset` remove apenas as notas cujo frontmatter declara `origin: pipeline` ou `hub_pipeline`, além das subpastas de citekey que ficarem vazias. Ficam no disco:

- as notas do usuário: `origin: manual`, sem `origin` ou com frontmatter ilegível;
- as notas `ask` e `article`;
- `.obsidian/` e as demais pastas ocultas;
- `90_Assets/`.

A confirmação informa quantas notas de cada grupo existem e avisa: `As linhas das notas manuais somem com o banco. 'zettel backup' e o que as traz de volta.` Os arquivos manuais continuam no vault. Depois do reset, rode `zettel sync-manual` para readotá-los. O backup é o que devolve as linhas — e o banco — sem reprocessar o pipeline.

Sem um backup, `init --reset` descarta a fonte de verdade do pipeline. Só o reprocessamento completo dos arquivos originais (com custo de LLM) recupera o acervo gerado. As linhas manuais não voltam com esse reprocessamento.

---

## Backup

`zettel backup PATH` grava um `.zettelbak` cifrado no arquivo `PATH`. Esse argumento é obrigatório: relativo ao diretório em que o comando roda, ou absoluto. A pasta é criada se faltar, e um arquivo já existente nesse caminho é substituído. `zettel restore PATH` lê esse arquivo e aplica neste acervo — o projeto do comando. Não há um caminho de destino separado. Os dois tomam a trava `data/zettel.lock`. A mesma operação está em `/backup` (menu e link em Configuração): a senha vai num arquivo temporário fora do vault e não entra no trabalho. Detalhe do `PATH` e das flags: [cli.md](cli.md#backup). A decisão é [ADR-072](adrs/generated/INFRA/ADR-072-encrypted-backup-archive.md). O restore em outra pasta acha as notas porque `origin_path`, `files.path`, `literature_note_path`, `notes.path`, `mocs.path` e `projects.path` são relativos ao vault ou à inbox.

```bash
python -m zettel backup acervo.zettelbak
python -m zettel backup ../backups/acervo.zettelbak --passphrase-file segredo.txt --no-chroma
python -m zettel backup acervo.zettelbak --with-inbox
python -m zettel restore acervo.zettelbak
python -m zettel restore ../backups/acervo.zettelbak --force-env --rotate-session
```

Não existe `--passphrase`. A senha vem do terminal (`Senha do backup:`) ou de `--passphrase-file`. Sem terminal e sem esse arquivo, o comando sai com código 2: `Sem terminal para ler a senha. Use --passphrase-file.` Senha vazia sai com código 1. No POSIX o arquivo de senha precisa estar em modo 0600. A senha não entra no arquivo e não fica na linha de comando.

### O que entra no arquivo

O disco guarda um blob, não um zip aberto. Por dentro, depois da cifra: magic `ZETTELBAK1` (10 bytes), salt scrypt de 16 bytes, nonce AES-GCM de 12 bytes, ciphertext AES-256-GCM (a tag de 16 bytes vai no fim). A chave é scrypt com `n=2**14`, `r=8`, `p=1`. O zip interno leva:

| Membro | Quando |
|---|---|
| `manifest.json` | Sempre. Versão 1, `created_at` em UTC, sha256 de cada membro, `include_chroma`, `include_inbox` |
| `state.db` | Sempre. Snapshot pela API de backup do SQLite, não uma cópia do arquivo ao vivo |
| `.env` | Se o arquivo do projeto existe |
| `chroma/` | Salvo com `--no-chroma` |
| `inbox/` | Só com `--with-inbox` |
| `vault/10_Sources`, `20_Literature`, `30_Permanent`, `40_MOCs`, `50_Projetos`, `90_Assets`, `99_Templates`, `vault/.obsidian` | Quando a pasta existe |

`.obsidian` deixa de fora pastas chamadas `cache` e, dentro de `plugins/`, `main.js` e `styles.css`. Nunca entram `data/cache`, `zettel.lock` nem `.venv`. Também não entram `00_Inbox/` (drafts, respostas do `ask`, artigos) nem uma `60_Estante/` antiga.

Antes do snapshot, o backup roda a mesma retenção do `sync-manual`, então o `state.db` do pacote já tem o Markdown atual.

### O que o SQLite reconstrói, e o que não

| Caminho | Reconstruível a partir do SQLite? |
|---|---|
| ZTL, MOC, LIT granular, índice LIT, ficha SRC (Resumo e Impressões), Mesa | Sim, pelas colunas da tabela acima, via `rebuild --what vault` ou `init`. Arquivo que já existe não é sobrescrito |
| `data/state.db` | Não. É o que o backup guarda. `init --reset` apaga, inclusive as linhas manuais |
| `data/chroma/` | Sim, via `zettel reindex`. O backup inclui o diretório, salvo `--no-chroma` |
| `data/inbox/` | Não. Só entra no arquivo com `--with-inbox` |
| Bytes de imagem em `90_Assets/` | Não. Estão no arquivo, na pasta do vault, não no SQLite |
| `data/cache/` | Descartável, e não entra no arquivo |

### Restore

O restore recusa o pacote antes de escrever qualquer arquivo se um membro tem caminho absoluto ou `..` (também barra invertida, `.`, letra de unidade), se há mais de 100.000 membros ou se o zip descompactado passa de 10 GiB.

Os membros são gravados por cima dos caminhos correspondentes. O `.env` existente só é trocado com `--force-env`; sem a flag, ele fica e o comando avisa. O `.env` gravado sai em modo 0600 quando o sistema permite — no Windows o modo pode não pegar.

`--rotate-session` troca `web_auth.session_key` e não imprime a chave nova. Sem essa flag, restaurar o `state.db` recoloca a chave antiga. Ela viaja dentro do banco cifrado: quem abre um arquivo vazado forja o cookie de sessão até a chave ser trocada (esta flag, ou Sair na web).

Em seguida, os `.md` que o banco tem e o disco não tem são recriados (`run_rebuild_vault` com `force` desligado). Essa etapa não sobrescreve arquivo que já existe, inclusive nota manual. Se o backup não trouxe o Chroma, o comando diz para rodar `zettel reindex` e não roda o reindex. Um Chroma que já estava no disco não é apagado.

Senha errada ou arquivo danificado no restore: código 1, mensagem `senha incorreta ou arquivo danificado`, sem imprimir segredo. Uma falha ao cifrar, no backup, é outra mensagem (`Nao foi possivel cifrar o backup.`) e também sai com código 1.

---

## Testes

```bash
uv run --no-sync pytest tests/ -v
uv run --no-sync pytest tests/test_hashing.py -v
uv run --no-sync pytest tests/test_hashing.py::test_normalize_collapses_whitespace -v
uv run --no-sync pytest tests/test_web.py tests/test_web_state.py -v
```

---

## Ver também

- [Comandos](cli.md) — todas as flags
- [Configuração](configuracao.md#trocar-o-modelo-de-embedding) — troca de embedding e recalibração
- [Arquitetura](arquitetura.md) — o que cada store guarda
- [Solução de problemas](troubleshooting.md)
