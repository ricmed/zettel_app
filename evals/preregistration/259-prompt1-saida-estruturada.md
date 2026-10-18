# Pré-registro — #259: saída estruturada no Prompt 1

Registrado em 2026-10-18, **antes** de qualquer chamada. Este arquivo não executa a medição. A flag de saída estruturada permanece desligada.

## Pergunta

A saída estruturada do provedor (`responseSchema` derivado de `LiteratureChunkOutput`, mesma ordem de chaves do schema) melhora o Prompt 1 contra o contrato atual (JSON por instrução + `parse_llm_json`), no gold set de #175?

## Condições

Mesmo prompt, mesmos few-shots, mesmo modelo e temperatura de produção. Só a forma de saída muda.

| rótulo | forma | papel |
|---|---|---|
| `instrucao` | JSON por instrução, como hoje | linha de base |
| `schema-a` | `responseSchema` / `json_schema` de `LiteratureChunkOutput` | **candidato: a decisão usa esta rodada** |
| `schema-b` | a mesma forma, segunda rodada | estabilidade |

Variante opcional, fora da regra: `rejection_reason` antes de `chunk_status`. Só é reportada.

## Regra de decisão

Adotar a saída estruturada **se, e somente se**, as três linhas valem ao mesmo tempo:

1. **Acertos líquidos.** No par `instrucao × schema-a`, sobre os itens julgados presentes nas duas rodadas, `schema-a` acerta mais que a instrução **e** o McNemar exato dá **p < 0,05**.
2. **Estabilidade.** `schema-a` e `schema-b` dão o mesmo veredito em **≥ 95%** dos itens.
3. **Parse.** `schema-a` e `schema-b` têm **zero** falhas de parse.

## Desfechos

| situação | desfecho |
|---|---|
| as três linhas valem | **adotar** — ligar a flag por fase e registrar no ADR-050 |
| a regra 1 falha | **manter** o JSON por instrução |
| a regra 2 ou 3 falha | **inconclusivo** — não ligar a flag com base nestas rodadas |

## Comandos (não executados neste branch)

```bash
.venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label schema-a --yes
.venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label schema-b --yes
.venv/Scripts/python.exe scripts/compare_gold_runs.py --pair instrucao:schema-a --pair schema-a:schema-b
```
