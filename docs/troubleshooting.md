# Solução de problemas

[← Voltar ao README](../README.md)

Sintomas comuns e como sair deles. Antes de qualquer coisa, rode o diagnóstico:

```bash
python -m zettel doctor
python -m zettel status
```

O `doctor` valida configuração, dependências, disponibilidade de FTS5, cobertura de capítulos e drift do espaço de embedding. O `status` mostra o funil do pipeline e alerta sobre chunking incompleto.

---

## "Docling não instalado"

Docling é obrigatório para harvest de PDF — não há extrator alternativo ([ADR-012](adrs/generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md); o PyMuPDF foi removido por licenciamento AGPL-3.0). A falha é intencionalmente ruidosa (`harvester.extract.PdfExtractionError`), sem degradação silenciosa para texto puro.

```bash
uv sync
```

---

## "Nenhum cluster encontrado" no garden

- Você precisa de pelo menos `gardener.min_cluster_size` notas permanentes (padrão: 5)
- Ajuste na execução: `python -m zettel garden --min-cluster-size 3`
- Um cluster ainda precisa de `gardener.min_notes_for_moc` notas para virar MOC

---

## Chunks ficam "pending" após o extract

- Verifique os logs para erros de LLM
- Execute `python -m zettel doctor` para validar dependências
- Verifique se a API key está configurada no `.env`
- Chunks marcados como `failed` voltam para a fila com `python -m zettel retry-failed`

---

## Fonte com pouco conteúdo / conceitos "sumiram" após o harvest

- Harvest interrompido no meio pode deixar só os primeiros capítulos no SQLite, enquanto o texto completo já está em `extracted_text`
- Sintoma: `doctor`/`status` reportam **chunking incompleto**; imagens apontam para `chapter_id` sem chunks
- Recuperação: `python -m zettel rechunk --source-id @Citekey` e depois `extract` + `connect`
- O próximo `harvest` do mesmo arquivo também tenta completar automaticamente

---

## Páginas erradas nas notas (p.NNN não bate com o livro)

- Confira o início do conteúdo com `python -m zettel set-paging --source-id @Citekey --content-start-file N --content-start-book M` (recalcula sem chamar o LLM e renomeia as LIT quando o token `pNNN` muda)
- Se `page_in_file` estiver **vazio** (fonte harvestada antes do mapa Docling), o conserto é reprocessar: `zettel harvest` no arquivo original, ou `zettel rechunk` se o `extracted_text` já tiver os marcadores `<!-- zettel:page-break -->`
- Markdown nativo **não tem páginas** — o localizador é o `section_path`. Dígitos soltos no texto não viram página
- Detalhes do modelo de paginação: [pipeline.md](pipeline.md#paginacao-arquivo-vs-impressa)

---

## ZTL sem seção Figuras

- Figuras dependem de `relevant_image_ids` no candidato (Prompt 1) ou do **fallback** (path `90_Assets/...` presente no texto do chunk)
- Se a imagem está em outro capítulo/chunk, o fallback não a anexa — o LLM precisa marcá-la via `{images_context}` do mesmo capítulo
- Confira o bloco `## Imagens` da LIT e se o asset tem `status: described`
- Imagens com falha de descrição voltam para a fila com `python -m zettel retry-failed --assets`

---

## Poucos candidatos aprovados após o extract

- O sistema filtra candidatos por qualidade. Verifique os logs por mensagens de "candidatos rejeitados"
- Ajuste os thresholds em `config/config.yaml` na seção `extraction:`:
  - `min_relevance_score: 2` para ser mais permissivo (padrão: 3)
  - `min_thesis_words: 3` para aceitar teses mais curtas
  - `min_definition_words: 5` para aceitar definições mais curtas
  - `require_anchor_quote: false` para não exigir citação-âncora

---

## Muitos (ou poucos) drafts na fila do review

Os cortes de confiança são **heurísticas iniciais** ([ADR-017](adrs/generated/REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)), não calibração empírica:

- `literature_review.auto_approve_min_confidence` no YAML define a faixa alta
- O corte "baixíssima" (`0.4`) vive em `review.py`

Monitore o volume por faixa após harvest/extract. Se a carga do operador ficar desbalanceada, proponha ajuste via issue (ver [RUNBOOK](adrs/RUNBOOK.md)) em vez de mudar o número em silêncio.

---

## MOCs sendo rejeitados

- Se `strict_topics: true`, MOCs cujo `topic` não casa com uma **categoria** de `config/moc_topics.yaml` serão rejeitados
- Verifique os logs por "MOC rejeitado: topico ... fora da lista"
- Opções:
  - Adicione/ajuste a categoria em `config/moc_topics.yaml`
  - Use `strict_topics: false` para aprovar todos os tópicos (com aviso no log)
  - Confirme que `gardener.topics_path` aponta para o YAML correto

---

## MOCs duplicados

- O sistema detecta MOCs existentes com o mesmo tópico e atualiza incrementalmente em vez de criar duplicados
- Se ainda houver duplicatas de execuções anteriores, remova manualmente os MOCs duplicados do vault e do StateDB
- Verifique os logs por "MOC existente para topico" para confirmar que o update incremental está funcionando

---

## `garden --hubs` não gera nada

- O pipeline hub depende do **grafo populado**: rode `connect` e, se você escreve notas à mão, `sync-manual --rebuild-graph`
- `hub_mocs.min_neighbors` (padrão 8) descarta hubs com vizinhança pequena
- No modo `percentile`, `hub_percentile: 0.90` só considera o top 10% por grau ponderado — em vaults pequenos, o modo `absolute` com `min_weighted_degree` menor tende a funcionar melhor

---

## `ask` responde "não encontrei evidência"

Isso é comportamento **projetado**: se nenhuma nota passa do piso de relevância, o LLM nem é chamado. Para entender o porquê:

```bash
python -m zettel ask "sua pergunta" --show-context
```

A tabela mostra similaridade, rank BM25, salto no grafo e o **motivo exato** da decisão de cada candidato, junto com os parâmetros de recuperação usados. Se o piso estiver alto demais para o seu corpus, ajuste `retrieval.relevance_floor.min_vector_similarity` — veja [recuperacao.md](recuperacao.md#piso-de-relevancia-absoluto).

---

## Busca híbrida degradada para vetorial

O sistema avisa e cai para busca vetorial pura quando o SQLite não tem FTS5. Confirme com `zettel doctor`. Depois de resolver, rode `zettel reindex` para reconstruir `fts_notes`/`fts_chunks`.

---

## Erros de Unicode no console (Windows)

O console em cp1252 quebra com setas e caracteres especiais. Por isso as strings de ajuda da CLI evitam esses caracteres — se você editar prompts ou textos da CLI, mantenha a mesma disciplina.

---

## Depois de trocar o modelo de embedding a busca piorou

Espaço vetorial novo exige recalibração:

- `retrieval.relevance_floor.min_vector_similarity`
- `linking.dedupe_threshold` e `harvest.duplicate_chunk_threshold`

E confirme que o `reindex --force` de fato rodou (o CLI aplica automaticamente ao detectar drift). Detalhes em [configuracao.md](configuracao.md#trocar-o-modelo-de-embedding).

---

## Ver também

- [Operação](operacao.md) — reconstrução e remoção de dados
- [Comandos](cli.md) — flags de cada comando citado aqui
- [RUNBOOK dos ADRs](adrs/RUNBOOK.md) — procedimentos operacionais e critérios de ajuste
