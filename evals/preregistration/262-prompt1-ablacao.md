# Pré-registro — #262 parte B: ablação das seções do Prompt 1

Registrado em 2026-10-18, **antes** de qualquer chamada. A parte A (alinhar o texto ao código) não é esta medição e não espera este resultado. Nenhuma variante é adotada neste branch.

## Pergunta

Qual seção do system do Prompt 1 pode sair sem piorar o julgamento no gold set de #175?

## Condições

Uma seção removida por variante. O resto do prompt, os few-shots não citados e o modelo de produção ficam iguais. Cada variante tem duas rodadas (`-a` e `-b`).

Seções candidatas, nesta ordem, cada uma contra o prompt alinhado da parte A:

1. "Tratamento de múltiplos conceitos"
2. "Checklist de validação"
3. metade dos few-shots, nomeada antes da rodada: a **segunda metade** da tupla `LITERATURE_NOTE_KEYS` em `zettel/domain_examples.py`, nesta ordem fixa. Sai `tag_examples`, `rejection_examples` e `accepted_example`. Fica `relevance_examples`, `thesis_examples` e `judgement_examples`.

Itens do gold cujo `chunk_id` não está mais em `state.db` não entram na chamada. A regra vale sobre os itens presentes nas duas rodadas da comparação.

## Regra de decisão

Para cada seção, **remover** só se as duas linhas valem:

1. **Não piora.** No par `atual × variante-a`, o McNemar exato **não** mostra a variante pior com **p < 0,05**. Se a variante for pior com p < 0,05, a seção fica.
2. **Estabilidade.** `variante-a` e `variante-b` concordam em **≥ 95%** dos itens.

Empate ou resultado inconclusivo **mantém** a seção. Não se encurta o prompt com ruído.

## Desfechos

| situação | desfecho |
|---|---|
| as duas linhas valem | **remover** essa seção, num PR posterior, e declarar a invalidação do cache |
| a regra 1 mostra piora significativa | **manter** a seção |
| a regra 2 falha | **inconclusivo** — manter a seção |

## Comandos (não executados neste branch)

```bash
.venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label ablacao-checklist-a --prompt <variante> --yes
.venv/Scripts/python.exe scripts/compare_gold_runs.py --pair atual:ablacao-checklist-a --pair ablacao-checklist-a:ablacao-checklist-b
```
