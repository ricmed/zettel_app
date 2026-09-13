# Pré-registro — #181: modelo do `extract`

Registrado em 2026-09-13, **antes** de qualquer rodada do Gemini. O commit deste arquivo precede os commits dos resultados; é isso que impede escolher a regra depois de ver o número.

## Pergunta

Trocar `llm.extract` de `gpt-4o-mini` @ 0,1 (produção) para `gemini-3.5-flash-lite` melhora o julgamento de extração, medido contra os rótulos humanos do gold set de #175?

## Rodadas

Todas sobre os 120 itens do gold set, com prompt e few-shots de produção (`prompt_sha 9f849789e727`, `examples_sha e078fb5c0465`) e *thinking* no padrão do fornecedor — a mesma condição da extração histórica do `@Kim2022KantAnd`.

| rótulo | modelo | temperatura | papel |
|---|---|---|---|
| `gpt4omini-atual` | `openai/gpt-4o-mini` | 0,1 | linha de base — já gravada em `86924c6` |
| `gemini-t01-a` | `gemini/gemini-3.5-flash-lite` | 0,1 | **candidato: a decisão usa esta rodada** |
| `gemini-t01-b` | `gemini/gemini-3.5-flash-lite` | 0,1 | ruído do candidato |
| `gemini-t00` | `gemini/gemini-3.5-flash-lite` | 0,0 | efeito da temperatura — informativo, fora da regra |

## Regra de decisão

Escolhida pelo usuário: **acertos líquidos**, com uma nota perdida e um lixo aceito pesando igual. O custo fica fora da decisão.

Trocar para o Gemini **se, e somente se**:

1. **Acertos líquidos.** No par `gpt4omini-atual × gemini-t01-a`, sobre os itens julgados (`s`/`n`) presentes nas duas rodadas, o Gemini acerta mais itens que o `gpt-4o-mini` **e** o teste de McNemar exato dá **p < 0,05**.
2. **Estabilidade.** As rodadas `gemini-t01-a` e `gemini-t01-b` dão o mesmo veredito em **≥ 95%** dos itens.

## Condições de validade

Não são preferência; sem elas o número não mede o que diz medir.

- **Falhas de parse.** Cada rodada do Gemini com no máximo 5% dos itens (6 de 120) sem resposta válida. Acima disso, o resultado é inconclusivo.
- **Conflito com o corpus.** A regra 1 conta itens da amostra, e a amostra super-representa a fronteira de rejeição (censo das 50 rejeições contestadas; 40 dos 474 aceitos). A comparação também reporta os acertos líquidos **ponderados pelo corpus** (`weighted_net_correct_b_minus_a`). Se o sinal dessa diferença for **oposto** ao da regra 1, o resultado é marcado como **conflitante** e a decisão volta para o usuário — não é resolvida por quem roda.

## Desfechos possíveis

| situação | desfecho |
|---|---|
| regras 1 e 2 atendidas, validade ok, sem conflito | **trocar** — ADR com os números e mudança em `config/config.yaml` |
| regra 1 falha | **manter** o `gpt-4o-mini` |
| regra 2 falha, ou falhas de parse acima do limite | **inconclusivo** — não trocar com base nestas rodadas |
| conflito entre a regra 1 e a diferença ponderada | **decisão do usuário**, com os dois números na mesa |

A rodada a 0,0 é reportada para explicar a extração histórica, mas não entra em nenhuma linha acima.

## Comandos

```bash
.venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label gemini-t01-a --provider gemini --model gemini-3.5-flash-lite --temperature 0.1 --yes --out-key evals/gold/extracao-GABARITO-gemini-t01-a.json
.venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label gemini-t01-b --provider gemini --model gemini-3.5-flash-lite --temperature 0.1 --yes --out-key evals/gold/extracao-GABARITO-gemini-t01-b.json
.venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label gemini-t00 --provider gemini --model gemini-3.5-flash-lite --temperature 0.0 --yes --out-key evals/gold/extracao-GABARITO-gemini-t00.json

.venv/Scripts/python.exe scripts/compare_gold_runs.py \
    --run gpt4omini=evals/gold/extracao-GABARITO-gpt4omini-atual.json \
    --run gemini-t01-a=evals/gold/extracao-GABARITO-gemini-t01-a.json \
    --run gemini-t01-b=evals/gold/extracao-GABARITO-gemini-t01-b.json \
    --run gemini-t00=evals/gold/extracao-GABARITO-gemini-t00.json \
    --pair gpt4omini:gemini-t01-a \
    --pair gemini-t01-a:gemini-t01-b \
    --pair gemini-t01-a:gemini-t00 \
    --out evals/results/extract-models-181.json
```
