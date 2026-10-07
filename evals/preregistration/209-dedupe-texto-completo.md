# Pré-registro — #209: texto completo para o LLM de dedupe

Registrado em 2026-10-07, depois da rotulagem humana da planilha `dedupe` do #206 (64 itens, `evals/gold/dedupe-rotulos.json`) e **antes** de qualquer chamada com texto completo. O commit deste arquivo precede os resultados.

## Pergunta

O LLM de dedupe da mesma fonte (`llm.review` = `openai/gpt-4o-mini`, temperatura herdada de `llm.temperature` = 0,2, prompt `prompts/dedupe_decision.md` atual) concorda mais com o julgamento humano quando vê o **conteúdo completo** de cada nota existente, em vez dos 200 primeiros caracteres?

## O que já se sabe (não muda a regra)

Placar da decisão como foi gravada no pipeline (texto cortado em 200 caracteres, com distância L2 no prompt): 28 de 64 acertos (44%), 58% ponderado pela população. Você marcou "desenvolve" em 45 dos 64 itens, e o LLM respondeu "nova" em 22 deles.

## Rodadas

Mesmos 64 itens, mesmo candidato (tese e definição exatamente como o LLM viu, lidas do `state_json` do shadow), mesmas notas existentes (os ids do `state_json`), mesmo prompt e mesmo modelo. A única variável é o texto de cada nota existente.

| rótulo | texto da nota existente | papel |
|---|---|---|
| `trunc-a` | os 200 primeiros caracteres do texto embedável (o trecho que o pipeline mostrava) | **linha de base: a decisão usa esta rodada** |
| `trunc-b` | idem | ruído da linha de base |
| `full-a` | tese, definição, intuição, exemplo e limites completos, sem managed blocks nem `## Conexões` | **candidato: a decisão usa esta rodada** |
| `full-b` | idem | ruído do candidato |

As quatro rodadas omitem a distância L2 que o prompt de produção mostrava. O `state_json` não a guardou, e mantê-la só em uma condição confundiria o efeito. A mudança de produção também a remove.

## Regra de decisão

Acerto = decisão de três vias (`create_new` / `ignore` / `link`) igual à humana, com `refine_existing` e `merge` contados como `link`, como no pipeline.

Adotar o texto completo no pipeline **se, e somente se**:

1. **Não inferioridade.** `full-a` acerta pelo menos tantos itens quanto `trunc-a`.
2. **Validade.** No máximo 5% de respostas inválidas (sem JSON ou sem decisão) em cada rodada.

O McNemar exato entre `full-a` e `trunc-a` é **reportado** e diz se a melhora é distinguível do ruído, mas não bloqueia a adoção. Dar o texto completo a um julgamento que compara notas é a correção de um defeito, não uma aposta. O que bloquearia é o texto completo **piorar** o acerto.

## Reportado ao lado (informativo)

- Acerto ponderado pela população de cada estrato.
- Acerto do alvo (qual nota) onde humano e LLM apontam uma.
- Concordância `-a` × `-b` em cada condição (estabilidade a temperatura 0,2).
- Taxa de "desenvolve" em cada condição.
- O placar do Jev (#206) nos mesmos itens. O Jev continua vendo o texto da condição em que o shadow rodou; não é refeito aqui.

## Desfechos possíveis

| situação | desfecho |
|---|---|
| regra 1 atendida, validade ok | **adotar**: `linking.dedupe_note_chars` sem corte efetivo; ADR-016 com adendo e números |
| regra 1 falha (texto completo acerta menos) | **não adotar**; investigar o prompt antes de mexer no contexto |
| validade falha | **inconclusivo**: repetir as rodadas |

## Comandos

```bash
.venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes --out evals/results/dedupe-context-209.json
```
