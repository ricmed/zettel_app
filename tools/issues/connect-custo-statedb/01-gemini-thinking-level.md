## Resumo

Parar de enviar `thinking_budget`, `temperature` e `top_p` para modelos Gemini 3+. O raciocínio passa a ser controlado só por `thinking_level`.

## Motivação

O Google avisou o projeto de que ele enviou requisições com `thinking_budget`, `temperature`, `top_p` e `top_k`. Nos próximos modelos Gemini:

- `thinking_budget` deixa de ser remapeado para `thinking_level` e passa a devolver `400 INVALID_ARGUMENT`;
- `temperature`, `top_p` e `top_k` passam a dar erro. Desde o Gemini 3.6 Flash eles já não têm efeito na saída.

O código atual (`zettel/llm.py`) faz duas coisas que vão quebrar:

- `get_llm`, no ramo `gemini`, sempre passa `temperature` e `top_p` (`llm.top_p: 0.5`, `llm.extract.temperature: 0.1`).
- `_thinking_client_kwargs("gemini", ...)` envia `thinking_budget: 0` para `false`/`0` e `thinking_budget: N` para um inteiro.

O extract atual (`gemini-3.5-flash-lite`, `thinking: null`) só escapa por acaso. O `langchain-google-genai` 4.3.7 descarta a amostragem apenas para uma lista fixa (`gemini-3.5-flash-lite`, `gemini-3.6-flash`), e `thinking: null` não envia budget. Qualquer outro Gemini recebe os parâmetros: os dos probes do ADR-050 ou um 3.8 futuro. O comentário "cache determinístico" sobre `temperature: 0.1` também é falso nesses modelos.

## Contrato / comportamento

- Gemini 3+ (`_gemini_omits_sampling(model)`, no mesmo padrão de `_anthropic_omits_sampling`): `get_llm` não passa `temperature` nem `top_p`.
- `llm.<fase>.thinking` no Gemini usa só `thinking_level`:
  - `false`/`0` → `minimal`
  - nível → o mesmo nível
  - `true` → omitido (padrão do modelo)
  - inteiro → erro de validação no `config.py`, com a mensagem "use um nível".
- A chave do `llm_cache` só inclui a temperatura quando ela de fato vai na requisição. Para modelos que omitem amostragem (Gemini 3+ e Anthropic sem amostragem), o fragmento fica vazio. Efeito único: entradas antigas de extract e connect deixam de bater, e isso só custa se os mesmos chunks forem reprocessados.
- `config/config.yaml`: `llm.extract.temperature: null` e `llm.connect.temperature: null`, com os comentários corrigidos.

## Arquivos a tocar

- `zettel/llm.py` (`_thinking_client_kwargs`, `get_llm`, `cached_call_llm`)
- `zettel/config.py` (`LLMPhaseConfig`, `effective_temperature`)
- `zettel/extractor.py` (checksum do Prompt 1)
- `config/config.yaml`
- `tests/test_llm_phase.py`, `tests/test_config.py`

## Fora de escopo

- Migrar para a Interactions API. `generateContent` continua com suporte total e o projeto usa `langchain-google-genai`.
- Embeddings (`gemini-embedding-2`), que não são afetados.

## Critérios de aceite

- [ ] Nenhuma requisição Gemini 3+ leva `temperature`, `top_p`, `top_k` ou `thinking_budget`.
- [ ] `thinking: false` no Gemini vira `thinking_level: minimal`.
- [ ] Um budget inteiro com `provider: gemini` é recusado no carregamento do config.
- [ ] O checksum do cache não muda quando só a temperatura de um modelo sem amostragem muda.

## Testes

- Testes de unidade no mapeamento e em `get_llm` (com o construtor capturado), além do validador do config.

## Dependências

Nenhuma.
