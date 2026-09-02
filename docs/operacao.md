# Operação: retenção, reconstrução e remoção

[← Voltar ao README](../README.md)

O que é fonte de verdade, o que é cache descartável, como reconstruir cada camada sem gastar LLM e como remover dados de forma permanente.

---

## Retenção e reconstrução

O **SQLite é a fonte de verdade durável**: além do estado do pipeline, ele persiste tudo que é caro de reproduzir — o texto extraído completo de cada fonte, o corpo integral das notas LIT/ZTL/MOC (com frontmatter), os candidatos completos (`candidate_json`) e as descrições de imagens. Os **embeddings não** são guardados no SQLite (são baratos de recomputar via API ou modelo local).

Como consequência:

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

Nunca sobrescreve um arquivo existente sem `--force`, e **nunca** sobrescreve uma nota `origin: manual` (mesmo com `--force`).

### `zettel rechunk`

Re-aplica a configuração de chunking atual a partir do texto extraído persistido, sem reprocessar o arquivo original; completa capítulos faltantes após harvest interrompido e re-vincula imagens aos capítulos corretos.

```bash
python -m zettel rechunk --all
python -m zettel rechunk --source-id @AutorAnoTitulo
python -m zettel rechunk --source-id @AutorAnoTitulo --dump-chunks
```

Se o texto extraído tiver marcadores `<!-- zettel:page-break -->`, o mapa de páginas é reconstruído a partir deles; senão fica sem mapa de páginas (não há fallback de extrator — [ADR-012](adrs/generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md)). Com `--dump-chunks`, grava um markdown com todos os chunks (texto + metadados) em `data/cache/chunk-dumps/` (ou `--dump-dir`).

### `zettel dump-chunks`

Reexporta os chunks já persistidos no SQLite como markdown, sem rechunkar nem chamar o LLM. Use para inspecionar cortes, `section_path`, páginas e overlap **antes** de mudar `chunk_size` / `chunk_overlap` / `min_section_chars`.

```bash
python -m zettel dump-chunks --source-id @AutorAnoTitulo
python -m zettel dump-chunks --all --dump-dir ./tmp/chunks
```

### `zettel dump-extraction`

Reexporta o Markdown extraído (`sources.extracted_text`: saída do Docling em PDF, ou o corpo MD nativo) com headings H1–H6 intactos, sem rerodar o extrator. Em PDF, o texto pode incluir comentários `<!-- zettel:page-break -->` (fronteiras de página do arquivo).

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

---

## `purge-rejected`

Remove permanentemente os chunks marcados como `rejected` no review.

```bash
python -m zettel purge-rejected                # apaga rejected + VACUUM
python -m zettel purge-rejected --yes          # sem confirmacao
python -m zettel purge-rejected --no-compact   # so apaga, sem compactar disco
python -m zettel purge-rejected --source-id @Citekey
```

O que sai: linhas em `chunks` e `concepts` (+ FTS) no SQLite, embeddings na coleção Chroma `chunks` e quaisquer ids em `literature_notes` associados. Notas permanentes e LITs aprovadas **não** são afetadas.

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
- Chroma: collection `sources`, chunks da fonte, entradas `literature_notes` dos chunks/índice

**Mantido por padrão (sem `--delete-permanent`):**

- Notas **permanentes (ZTL)** geradas a partir da fonte — o campo `source_id` é limpo no banco e wikilinks mortos para SRC/LIT removidos são stripados de **todo** o vault (incluindo MOCs e outras ZTL)
- MOCs, notas de outras fontes e o restante do acervo

**Com `--delete-permanent`:** apaga também as ZTL ligadas à fonte (vault + SQLite + Chroma `permanent_notes`).

Por padrão roda **`VACUUM`** em `state.db` e `chroma.sqlite3` após a exclusão (como `purge-rejected`); use **`--no-compact`** para pular. Confirme com **`--yes`** / **`-y`** em scripts.

---

## `init --reset`

```bash
python -m zettel init            # recria so o vault vazio (apaga ./vault)
python -m zettel init --reset    # + apaga State DB, ChromaDB e cache (pede confirmacao)
```

`init --reset` é o único comando que descarta a fonte de verdade. Depois dele, só o reprocessamento completo dos arquivos originais (com custo de LLM) recupera o acervo.

---

## Backup

O que precisa ir para um backup/armazenamento persistente:

| Caminho | Conteúdo | Reconstruível? |
|---|---|---|
| `vault/` | As notas `.md` | Sim, via `rebuild --what vault` (exceto notas manuais) |
| `data/state.db` | Fonte de verdade: estado, textos, corpos, grafo, cache de LLM | **Não** |
| `data/chroma/` | Vetores | Sim, via `reindex` |
| `data/inbox/`, `data/processed/` | Arquivos originais | **Não** (são a entrada) |
| `data/cache/` | Dumps e checkpointer | Sim (descartável) |

Ou seja: um backup de `data/state.db` + arquivos originais + notas manuais reconstrói o resto sem gastar LLM.

---

## Testes

```bash
uv run pytest tests/ -v
uv run pytest tests/test_hashing.py -v
uv run pytest tests/test_hashing.py::test_normalize_collapses_whitespace -v
uv run pytest tests/test_web.py tests/test_web_state.py -v
```

---

## Ver também

- [Comandos](cli.md) — todas as flags
- [Configuração](configuracao.md#trocar-o-modelo-de-embedding) — troca de embedding e recalibração
- [Arquitetura](arquitetura.md) — o que cada store guarda
- [Solução de problemas](troubleshooting.md)
