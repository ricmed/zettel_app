## Resumo

O custo estimado passa a refletir o que os provedores cobram. Para isso: desconto do cache de prompt, Jev separado das chamadas LLM, uso gravado por provedor e modelo, e um pré-voo calibrado pelo histórico do modelo atual.

## Motivação

No último run (2 livros), o custo estimado divergiu dos painéis. Os valores dos painéis vieram do usuário.

| Provedor | Estimado (`runs`) | Painel | Causa |
|---|---|---|---|
| Anthropic (connect, `claude-haiku-5-5`) | US$ 3,09 | US$ 2,46 | 7,79M tokens lidos do cache de prompt cobrados a preço cheio. Recalculado com o desconto (US$ 0,01/M em vez de 0,10/M): **US$ 2,46 exatos** |
| Gemini (extract + embeddings) | US$ 5,25 (≈ 5,11 com desconto) | R$ 30,96 | Compatível com câmbio + impostos ≈ R$ 6/US$; o painel pode incluir outros dias |
| OpenAI | US$ 0,18 | US$ 0,00 | Painel atrasado ou cota gratuita |
| Typesafe (Jev) | US$ 0,045 | US$ 0,01 | `decision.input_price_per_mtok: 0.042` acima do real |

Causas no código:

1. `pricing.estimate_llm_cost` recebe só os tokens de entrada e saída. `litellm.cost_per_token` aceita `cache_read_input_tokens` e `cache_creation_input_tokens`, e o `call_llm` já tem esses números (`TokenUsage`).
2. O Jev é registrado com `record_llm` (`decision/client.py`). As 644 chamadas dele no connect entram em `runs.llm_calls`, `tokens_prompt` e `cost_usd_llm`. Por isso `preflight._output_tokens_per_call` deu 1.691 tokens de saída por chamada contra 2.550 reais, e o custo do Jev aparece misturado ao do Anthropic.
3. O pré-voo mede a entrada por `chars // 4`. No Claude em PT o real é ~2,2 chars/token: 27.003 chars viraram 12.366 tokens. A entrada sai ~45% subestimada.
4. `runs` não guarda provedor nem modelo, então não há como comparar com os painéis.

## Contrato / comportamento

- `estimate_llm_cost(model, prompt, completion, *, cache_read_tokens=0, cache_write_tokens=0, provider=None)`, com `prompt_tokens` inclusivo (semântica do LangChain em Anthropic e Gemini).
- `UsageEvent.provider`. `CostTracker` agrega por `(kind, provider, model)`, ganha `record_decision` (fora de `llm_calls`/`tokens_*`/`cost_usd_llm`, dentro de `cost_usd_total`) e um `threading.Lock` nos `record_*`.
- Nova tabela `run_usage(run_id, kind, provider, model, calls, tokens_prompt, tokens_completion, cache_read_tokens, cache_write_tokens, tokens_embedding, cost_usd)`, criada com `CREATE TABLE IF NOT EXISTS` (sem reset) e gravada em `finish_pipeline_run`.
- `zettel status`: tabela "Custo por provedor" (total e último run).
- Pré-voo: médias de entrada, saída e fração lida do cache vêm de `run_usage`, filtradas pelo **modelo atual** da fase. Sem histórico, volta para `chars // 4` e o alvo do config. O preço usa o desconto de cache.
- `decision.input_price_per_mtok` recalibrado (~0,01), conferindo antes o preço oficial.

## Arquivos a tocar

- `zettel/pricing.py`, `zettel/llm.py`, `zettel/usage.py`, `zettel/decision/client.py`
- `zettel/state/schema.py`, `zettel/state/runs.py`
- `zettel/preflight.py`, `zettel/cli/diagnostics.py`, `zettel/cli/formatting.py`
- `config/config.yaml`

## Fora de escopo

- Conciliar câmbio e impostos do painel Gemini.
- Explicar o zero do painel OpenAI.

## Critérios de aceite

- [ ] Recalcular o run do connect com os tokens reais dá o valor do painel Anthropic (±2%).
- [ ] Chamadas Jev não alteram `llm_calls` nem a média de saída do pré-voo.
- [ ] `zettel status` lista o custo por provedor.

## Testes

- `tests/test_pricing.py`: desconto de cache com modelos do mapa local.
- Testes do tracker e de `run_usage`.
- Pré-voo com histórico de outro modelo, que é ignorado.

## Dependências

Nenhuma. O lock no tracker é pré-requisito do connect paralelo.
