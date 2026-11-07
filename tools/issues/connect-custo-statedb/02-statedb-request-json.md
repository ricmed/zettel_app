## Resumo

`llm_cache.request_json` passa a guardar só metadado (`phase`, `model`, `prompt_checksum`) em vez do prompt inteiro. O `state.db` existente é limpo uma vez.

## Motivação

Medido em 2026-11-07 com 2 livros processados (1.437 chunks, 1.216 ZTL): `state.db` = **138 MB**.

| Objeto | Tamanho |
|---|---|
| `llm_cache` | 88 MB, dos quais **76 MB são `request_json`** |
| `review_events` | 8,0 MB |
| `chunks` | 7,9 MB |
| `notes` | 6,7 MB |
| FTS (notes + chunks) | ~12 MB |

`llm.cached_call_llm` grava `json.dumps({"system": system, "user": user})` em toda chamada. O system prompt do connect tem ~20 mil caracteres e se repete idêntico em cada linha. Nenhum código lê `request_json`: só `db_report` mede o tamanho dela. Na escala atual, ~38 MB por livro só nessa coluna, ou ~3,8 GB para 100 livros.

## Contrato / comportamento

- `StateDB.cache_llm_response(call_checksum, response_json, *, phase, model, prompt_checksum="")` grava `request_json = {"phase", "model", "prompt_checksum"}`.
- Os chamadores passam esse metadado: `llm.cached_call_llm`, `extractor` (Prompt 1), `bibliography` e `assets` (visão).
- A coluna continua no schema, sem reset.
- Limpeza única do banco atual, com backup antes: `UPDATE llm_cache SET request_json='{}'; VACUUM;`. Esperado: ~62 MB (≈31 MB/livro, só conteúdo real).

## Arquivos a tocar

- `zettel/state/llm_cache.py`, `zettel/llm.py`, `zettel/extractor.py`, `zettel/bibliography.py`, `zettel/assets.py`
- Fakes em `tests/test_connector.py` e `tests/test_llm_unavailable.py`; `tests/test_state.py`, `tests/test_db_report.py`

## Fora de escopo

- Remover a coluna do schema, o que exigiria `zettel init --reset`.
- `review_events.snapshot_json` (~3 MB repetem `chunks.text`) e FTS com conteúdo externo: ganho pequeno, avaliar depois.

## Critérios de aceite

- [ ] Uma linha nova de `llm_cache` tem `request_json` com menos de 200 bytes.
- [ ] O cache continua acertando: mesma chave, mesma resposta.
- [ ] `zettel db-report` mostra o banco limpo com ~62 MB.

## Dependências

Nenhuma.
