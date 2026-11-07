# Pré-registro — #355: thinking desligado no Prompt 2 do connect (Claude Haiku 5.5)

Registrado em 2026-11-07, **antes** de qualquer chamada com o thinking desligado. Neste commit já existem:
- os rótulos humanos da validação da #231 (`evals/gold/relacoes-r2-rotulos.json`, gabarito `relacoes-r2-GABARITO-NAO-ABRIR.json`, snapshot `n40-s1`);
- a opção `--prompt NOME=CAMINHO@THINKING` em `scripts/probe_connect_context.py`.

Nenhuma resposta do `claude-haiku-5-5` foi gravada para esse snapshot com qualquer das duas condições.

## Pergunta

Com o Prompt 2 de produção e o contexto de produção (`trunc`, 150 caracteres), o `claude-haiku-5-5` com o thinking **desligado** (`thinking: {type: disabled}`, effort `low`) escolhe relações que concordam com o julgamento humano **tão bem quanto** com o thinking **ligado em effort low**, a condição de produção até aqui? E quanto ele economiza em tokens de saída e em latência?

## O que já se sabe

- O último connect (2 livros, 1.259 chamadas) teve ~2.550 tokens de saída por chamada e ~27 s por nota. Pela razão chars/token da própria requisição, ~1.450 desses tokens são a resposta visível. Os ~1.100 restantes (≈43%) são thinking.
- Na #231, com `gemini-3.1-flash-lite`, o prompt atual acertou **20/69** relações exatas na validação (29%, IC95 de Wilson [20%, 41%]), contra 17/69 do prompt anterior. Esses números são de outro modelo: descrevem a dificuldade da tarefa e **não** servem de linha de base.

## Condições

| nome | `llm.connect.thinking` | fio |
|---|---|---|
| `low` | `low` | `thinking: {type: adaptive}`, `output_config.effort: low` (produção) |
| `off` | `false` | `thinking: {type: disabled}`, `output_config.effort: low` |

As duas condições usam o mesmo modelo (`anthropic/claude-haiku-5-5`), o mesmo prompt (`prompts/permanent_note.md` neste commit) e o mesmo snapshot, e recebem as mesmas vizinhas na mesma ordem. Só o thinking muda.

## Material

- **Itens:** os pares rotulados da validação da #231 (`relacoes-r2`), 69 julgados, do snapshot `n40-s1` (40 conceitos de duas obras, sem os do `s0`).
- **Rótulos:** os rótulos cegos que já existem, sob as definições de relação da #231. Não se rotula nada novo **antes** das respostas.
- **Pares fora do gabarito:** uma condição pode propor uma aresta para um par que nenhum prompt da #231 propôs. Esses pares não têm rótulo e são **reportados, não pontuados**. Se mais de 20% das arestas de qualquer condição caírem fora do gabarito, a rodada é inconclusiva. Nesse caso exporta-se uma planilha suplementar (`--export --compare low:trunc-a,off:trunc-a --sheet-prefix relacoes-r3`), rotula-se às cegas e pontua-se de novo, sem mudar a regra.

## Tamanho da amostra e poder

- **Discordância esperada:** 20% dos pares (desconhecida para este par de condições; mesma ordem da #231).
- **Efeito mínimo de interesse:** 10 pontos de acurácia, a perda que impediria a troca.
- `scripts/plan_gold_round.py --discordance 0.2 --effect 0.1`: 168 itens para poder 0,8 no McNemar exato.
- A população rotulada tem **69** itens, com **poder de 37%** (exato) e 46% (mid-p). A rodada é, portanto, **exploratória quanto à perda de qualidade**: um p acima de 0,05 não prova equivalência. Por isso a regra abaixo é de **não-inferioridade por margem**, e não um teste de diferença.

## Métrica

- **Decide:** acertos de relação exata (contando `nenhuma`) de `off:trunc-a` contra `low:trunc-a` nos mesmos pares, com IC95 de Wilson para cada proporção e McNemar (p exato e mid-p) para a diferença pareada.
- **Decide:** a média de tokens de saída por chamada (`usage` das gravações).
- **Reportado:** latência média por chamada, matriz de confusão por tipo, arestas por nota e a concordância entre as duas condições.

## Regra de decisão

Trocar `llm.connect.thinking` de `low` para `false` **se, e somente se**:

1. **Não-inferioridade:** `off` não acerta menos de `low − 3` pares (≈4 pontos em 69), **e** o McNemar exato **não** favorece `low` com p < 0,05.
2. **Economia:** a média de tokens de saída de `off` fica pelo menos 25% abaixo da de `low`.
3. **Validade:** no máximo 5% de respostas inválidas por condição e pelo menos 60 pares julgados.

| situação | desfecho |
|---|---|
| 1 a 3 atendidas | `thinking: false` no connect, com um adendo ao ADR-048 e os números |
| 1 falha | manter `low` e publicar o resultado |
| 2 falha | manter `low`: a economia não paga o risco |
| 3 falha ou pares fora do gabarito > 20% | inconclusivo: planilha suplementar ou nova rodada, sem mudar a regra |

## Fora desta rodada

- Outros níveis de effort (`medium`, `high`) e outros modelos.
- A qualidade do texto da nota (tese, definição, limites). Esta rodada mede as relações; se `off` passar, 20 notas das duas condições serão lidas às cegas antes da troca, registradas como observação.
- O connect paralelo por fonte, que é independente.

## Comandos

```bash
.venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 1 --runs trunc-a --prompt low=prompts/permanent_note.md@low --prompt off=prompts/permanent_note.md@false --yes
```

```bash
.venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 1 --runs trunc-a --prompt low=prompts/permanent_note.md@low --prompt off=prompts/permanent_note.md@false --score-key evals/gold/relacoes-r2-GABARITO-NAO-ABRIR.json --labels evals/gold/relacoes-r2-rotulos.json --out evals/results/355-connect-thinking.json
```
