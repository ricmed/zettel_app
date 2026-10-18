# Pré-registro — #260: cache explícito, batch e paralelismo do extract

Registrado em 2026-10-18, **antes** de qualquer medição. Este arquivo não implementa workers, cache explícito nem modo batch, e não executa a comparação.

## Pergunta

No mesmo Prompt 1, um subconjunto fixo de 100 chunks pendentes gasta menos ou termina antes se o envio mudar, sem mudar o veredito?

## Condições

O prompt, os few-shots, o modelo e a chave de cache SQLite ficam iguais. O modo de envio **não** entra em `compute_llm_call_checksum`.

| rótulo | envio |
|---|---|
| `atual` | sequencial, cache implícito do provedor |
| `cache-explicito` | prefixo de system em cache explícito |
| `workers-2` | 2 workers, pacing existente |
| `workers-4` | 4 workers, pacing existente |
| `batch` | modo batch do provedor, quando existir |

## Regra de decisão

Cada variante é julgada **sozinha** contra `atual`. Adotar a variante **se, e somente se**:

1. Os vereditos do subconjunto são **idênticos** aos de `atual` (mesma chave, mesma resposta aceita).
2. O custo em USD é **≤ 80%** do atual **ou** o tempo de parede é **≤ 50%** do atual.
3. Falhas de parse não aumentam.

Falhar qualquer linha **rejeita** a variante, com o motivo na tabela do PR. Paralelismo que não propaga `LLMUnavailableError` para todos os workers é rejeitado mesmo que o tempo caia.

## Desfechos

| situação | desfecho |
|---|---|
| as três linhas valem | **adotar** essa variante, atrás de flag, com ADR |
| vereditos divergem, ou o ganho não atinge a barra | **rejeitar** a variante |
| o provedor não oferece a variante | **não medir** — registrar como indisponível, não como rejeitada |

## Comandos

Ainda não há script. A medição futura grava, para cada rótulo, custo, fração de tokens lidos do cache, tempo de parede e a igualdade dos vereditos. Não rodar neste branch.
