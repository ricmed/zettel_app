# Avaliação do `zettel ask`

Infraestrutura **opcional** de pesquisa: separa *roteamento* (a nota-alvo chegou ao pool?) de *representação* (tendo chegado, sobreviveu ao piso e sustentou a resposta?). Decisão e alternativas: [ADR-038](../docs/adrs/generated/QA-WRITING/ADR-038-ask-trajectory-evals-offline-replay.md).

## Rodar

```bash
.venv/Scripts/python.exe -m zettel.evals.replay evals/configs/current-ask.yaml --out evals/results/current-ask.json
.venv/Scripts/python.exe -m pytest tests/evals/ -v
```

Nada aqui chama LLM nem abre rede — há testes que fazem `socket.connect` e `llm.call_llm` explodirem para garantir isso.

## Estrutura

| Caminho | O que é |
|---|---|
| `zettel/evals/manifest.py` | identidade do run (o envelope em que o número foi medido) |
| `zettel/evals/score.py` | veredictos determinísticos |
| `zettel/evals/replay.py` | runner offline + CLI |
| `evals/fixtures/` | só sintético ou domínio público |
| `evals/configs/` | YAML sem segredos |
| `evals/results/` | agregados pequenos, commitáveis |
| `.eval-work/` | **gitignored**: trajetórias cruas, vaults privados |

## Veredictos

| Veredicto | Significado |
|---|---|
| `routing_miss` | a nota-alvo nunca entrou no pool de candidatos |
| `floor_reject` | a nota-alvo **foi** recuperada; o piso a barrou |
| `answer_fail` | a nota-alvo foi usada; a resposta não bateu a rubrica declarada |
| `ok` | como esperado |
| `unknown` | o gold não nomeia alvo — medido, não chutado |

Pergunta marcada `expect_no_evidence` é julgada pelo comportamento que importa proteger: `hits` vazio tem que significar **LLM não chamado**. Se respondeu mesmo assim, é `answer_fail`.

A única checagem sobre a resposta é a rubrica de substring declarada no gold (`answer_must_contain`). Nada infere raciocínio oculto do texto. Sem rubrica, o julgamento é só de recuperação.

## Identidade do run

Dois runs só são comparáveis se o manifesto bater: mesmas perguntas, mesmo fixture, mesmos modelos, mesmos limiares — e **mesmo commit**. Uma comparação entre commits tem identidade diferente de propósito; tem que ser um ato deliberado, não um acidente.

O manifesto recusa campo obrigatório vazio e valor que pareça credencial (`sk-`, `api_key`, `Bearer `, `ghp_`). Ele é commitável; segredo fica no `.env`.

## Guardrail de afirmações

Um **null result é resultado válido**. Não publique comparação entre condições sem envelope idêntico (mesmas perguntas, mesmo modelo, mesmos limiares) dos dois lados, e não afirme que uma abordagem "vence" outra a partir de um fixture sintético — ele prova que as classificações funcionam, não que o vault recupera bem.

## Ablações

O campo `condition` do manifesto aceita qualquer nome (`current_ask`, `no_graph`, `vector_only`, `lit_only`, `topic_index_off`), **uma por run**. Nenhuma está implementada: o campo existe para que adicionar uma depois não exija redefinir a identidade do run.

Runner ao vivo (com orçamento declarado em `max_calls` / `max_input_tokens`, fail-closed) fica para um follow-up, e só se o replay estiver verde.
