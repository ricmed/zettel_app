# Pré-registro — #273: modelo da dedupe (`gpt-6-luna` contra `gpt-4o-mini`)

Registrado em 2026-10-19, **antes** de qualquer chamada desta rodada. O commit deste arquivo precede os resultados. A regra abaixo não muda depois de ver o número.

## Pergunta

No prompt de produção (`prompts/dedupe_decision.md`, o de #218, texto completo das notas), `openai/gpt-6-luna` concorda mais com o julgamento humano do que `openai/gpt-4o-mini` — o único modelo em que #209 e #218 foram medidos?

Uma segunda pergunta, **separada**, não altera a primeira: o contrato novo (afirmações e `reason` antes de `decision`; alvo obrigatório e pertencente às notas comparadas) melhora o `gpt-6-luna` em relação ao contrato atual?

## O que já se sabe e não decide esta rodada

- #209 e #218 mediram só `gpt-4o-mini` @ 0,2. O prompt atual foi mantido por decisão explícita (ADR-016): na rodada 2, 25/42 contra 16/42 do prompt anterior, McNemar p = 0,093.
- Nas duas rodadas o prompt atual deu `ignore` a **0 de 10** duplicatas reais. Isso é o motivo da métrica secundária, não uma regra de corte.
- `gpt-6-luna` entrou em `llm.review` no commit `4f3e80a` sem essa régua.
- A escala de distância (#267) não entra aqui: a sonda reusa o estado gravado em `decision_shadow`, não consulta o Chroma.

## Dados

Só a **rodada 2** decide. A rodada 1 (64 itens) serviu para escrever o prompt; usá-la para escolher o modelo seria ajustar no mesmo conjunto.

- Chave: `evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json`
- Rótulos: `evals/gold/dedupe-r2-rotulos.json` (`method: manual_blind`, 42 itens, 7 `ignore`)
- Contexto: **full** (o de produção desde #209). `trunc` não é pedido e não entra na regra.
- Temperatura: `effective_temperature` da fase `review` (hoje 0,20, herdada de `llm.temperature`). Igual nos dois modelos.
- Duas repetições, `full-a` e `full-b`. A regra lê `full-a`. `full-b` só entra na estabilidade, que é reportada e não é corte.

## Condições

| nome | modelo | prompt |
|---|---|---|
| `mini` | `openai/gpt-4o-mini` | `prompts/dedupe_decision.md` (produção, contrato atual) |
| `luna` | `openai/gpt-6-luna` | o mesmo arquivo |
| `luna-contract` | `openai/gpt-6-luna` | `evals/prompts/dedupe_decision_contract.md` (congelado neste pré-registro) |

`luna-contract` não participa da regra do modelo.

## Regra do modelo (rodada 2, `full-a`)

Ficar com `gpt-6-luna` em `llm.review` **se, e somente se**:

1. **Melhora.** `luna` acerta mais itens que `mini`, com McNemar exato **p < 0,05** (pares discordantes: só um dos dois acerta).
2. **Validade.** `luna` em `full-a` e em `full-b` tem no máximo 5% de respostas inválidas.

Se a regra falha, `llm.review.model` volta para `gpt-4o-mini` e o ADR-016 registra o desvio do commit `4f3e80a`.

Reportado, **sem poder de corte**:

- recall de `ignore`: entre os itens cujo rótulo humano é `ignore`, a fração em que `llm_decision` é `ignore`, para `mini` e para `luna`;
- estabilidade `full-a` × `full-b`;
- acertos absolutos dos dois.

## Regra do contrato (não muda o modelo)

Adotar o contrato novo no parser de produção **se, e somente se** a regra do modelo já tiver sido aplicada **e**:

1. `luna-contract` acerta mais itens que `luna` em `full-a`, McNemar exato **p < 0,05**;
2. validade de `luna-contract` em `full-a` e `full-b` ≤ 5%.

O recall de `ignore` das duas condições é reportado e não é corte. Se esta regra falha, o prompt e o `DedupeResult` de produção ficam como estão. O validador estrito existe como função à parte (`checked_dedupe_result`) e só passa a ser o parser de `deduplicate_candidates` se a regra adotar o contrato.

## Desfechos

| situação | desfecho |
|---|---|
| regra do modelo atendida | `llm.review` permanece `gpt-6-luna`; ADR-016 com os números |
| regra do modelo falha | `llm.review.model` = `gpt-4o-mini` |
| regra do contrato atendida | o prompt de produção e o parser passam a exigir alvo nas decisões que não são `create_new`, com um reparo e depois `create_new` |
| regra do contrato falha ou a condição não rodou | contrato de produção intacto |
| validade falha | **inconclusivo** para aquela pergunta; não trocar o que a validade derrubou |

## Comandos

```bash
.venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes --context full --model openai/gpt-4o-mini \
    --key evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json --labels evals/gold/dedupe-r2-rotulos.json \
    --out evals/results/dedupe-model-273-r2-mini.json
.venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes --context full --model openai/gpt-6-luna \
    --key evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json --labels evals/gold/dedupe-r2-rotulos.json \
    --out evals/results/dedupe-model-273-r2-luna.json
.venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes --context full --model openai/gpt-6-luna --strict-target \
    --prompt current=evals/prompts/dedupe_decision_contract.md \
    --key evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json --labels evals/gold/dedupe-r2-rotulos.json \
    --out evals/results/dedupe-model-273-r2-luna-contract.json
```

A comparação lê esses JSON pela função `model_comparison_rule` em `scripts/probe_dedupe_context.py`, que implementa as duas regras acima e nada além delas.

## Adendo (2026-10-19, antes de qualquer resposta pontuada)

A primeira chamada de `gpt-6-luna` com a temperatura da fase (`0.2`) foi recusada pela API: `temperature` só aceita o default `1`. Nenhuma resposta foi gravada. Isso não é uma rodada: não há acerto, McNemar nem recall.

Consequência, pela regra já escrita (a condição não pôde ser executada; validade não se aplica a uma chamada que o modelo recusa): `llm.review` volta para `gpt-4o-mini`, o modelo medido em #209/#218. Uma rodada a temperatura `1` seria outra condição e precisa do próprio pré-registro antes de qualquer chamada. O contrato novo não foi medido e não entra em produção.

## Adendo 2 (2026-10-19, antes de qualquer item do gabarito)

A recusa de `temperature` 0,2 e de `top_p` ocorre com `thinking` nulo na fase `review`: o cliente não envia `reasoning_effort`. Um ping de uma palavra, fora do gabarito, com `thinking: none` (`reasoning_effort: none`, o mesmo ajuste de harvest/garden/images/summarize para este modelo) devolveu `ok` em temperatura 0,2 e `top_p` 0,5.

A regra não muda. A rodada executa a temperatura já registrada (0,20 nos dois modelos). `gpt-6-luna` e `luna-contract` usam `--thinking none`. `gpt-4o-mini` continua o replay já gravado, sem chamada nova. Temperatura 1 não é esta rodada.

```bash
.venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes --context full --model openai/gpt-6-luna --thinking none \
    --key evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json --labels evals/gold/dedupe-r2-rotulos.json \
    --out evals/results/dedupe-model-273-r2-luna.json
.venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes --context full --model openai/gpt-6-luna --thinking none --strict-target \
    --prompt current=evals/prompts/dedupe_decision_contract.md \
    --key evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json --labels evals/gold/dedupe-r2-rotulos.json \
    --out evals/results/dedupe-model-273-r2-luna-contract.json
```
