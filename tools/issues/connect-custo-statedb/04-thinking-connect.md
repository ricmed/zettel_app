## Resumo

Fazer `thinking: false` desligar de verdade o thinking dos modelos Anthropic que aceitam isso, e medir no gold set se o connect pode rodar com o thinking desligado.

## Motivação

No último connect (1.259 chamadas ao `claude-haiku-5-5`, 9h25), cada nota levou ~27 s, estável do início ao fim. Por chamada foram ~12,4k tokens de entrada e **~2.550 de saída**. Pela razão chars/token da própria requisição (27.003 chars → 12.366 tokens), a resposta visível (3.167 chars) dá ~1.450 tokens. Os ~1.100 tokens restantes, **≈43% da saída**, são thinking oculto. Como a saída domina o tempo de uma chamada, o thinking é a maior parcela evitável dos 27 s.

- `config.yaml` tem `llm.connect.thinking: false` com o comentário "Haiku 5.5: effort low (nao da para desligar)". Isso está errado: a API do Haiku 5.5 aceita `thinking: {type: "disabled"}` com effort `low`, `medium` e `high`. Só Opus 5.5 e Fable/Mythos recusam `disabled`.
- `llm._anthropic_adaptive_thinking_kwargs(False)` envia só `output_config.effort = "low"`, então o modelo continua pensando.

Desligar o thinking pode afetar a qualidade das relações e da escrita, por isso a troca em produção depende de medição.

## Contrato / comportamento

- `thinking: false`/`0` no Anthropic:
  - **Haiku 5.5** e **Opus 5** → `{"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}`.
  - **Opus 5.5** e **Fable/Mythos** → só `effort: low`, como hoje, porque recusam `disabled`.
- `config.yaml`: `llm.connect.thinking: low`, que é o mesmo fio de hoje (adaptive + effort low). Assim a correção não muda a produção antes da medição.
- `scripts/probe_connect_context.py --thinking VALOR`: sobrescreve `llm.connect.thinking`, entra na chave das gravações e o relatório traz tokens de saída e latência por rodada.
- Pré-registro `evals/preregistration/<esta issue>-connect-thinking.md` (modelo `MODELO.md`, tamanho via `scripts/plan_gold_round.py`): `low` × `false` sobre os rótulos de relação do #231 (validação). A regra é de não-inferioridade nas relações exatas, com a redução de tokens de saída registrada. Custo estimado: ~US$ 0,30.

## Arquivos a tocar

- `zettel/llm.py`
- `config/config.yaml`
- `scripts/probe_connect_context.py`
- `evals/preregistration/`
- `tests/test_llm_phase.py`

## Fora de escopo

- Trocar `llm.connect.thinking` em produção antes da medição.

## Critérios de aceite

- [ ] Haiku 5.5 com `thinking: false` envia `disabled` + effort low; Opus 5.5 com `false` envia só effort.
- [ ] O probe aceita `--thinking` e grava as rodadas separadamente.
- [ ] O pré-registro está commitado antes da rodada paga.
- [ ] Depois da medição: a decisão fica registrada (adendo ao ADR-048) e o config é ajustado se a regra passar.

## Dependências

Nenhuma.
