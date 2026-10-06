# Pré-registro — #206: o Jev como camada de decisão

Registrado em 2026-10-06, **antes** de qualquer chamada ao Jev. O commit deste arquivo precede os commits dos resultados; é isso que impede escolher a regra depois de ver o número.

## Pergunta

O Jev (`typesafe/jev-1.13.0`), um modelo que responde perguntas tipadas (`noul`, `choice`, `score`) com probabilidade em vez de gerar texto, separa o que um humano guardaria do que descartaria **em PT-BR**, e com que estabilidade? A resposta decide se vale abrir uma issue de *gate* — o Jev passando a decidir, com o caminho atual como fallback — para algum dos três sites que esta issue liga em modo shadow (dedupe, categoria do cluster no garden, notas do juiz do artigo).

Nenhuma decisão do pipeline muda nesta issue, qualquer que seja o resultado.

## Por que estas medidas

A documentação do Jev declara três limites que atingem este projeto: precisão menor fora do inglês, viés para a primeira opção de um `choice` e calibração numérica fraca no `score`. O único gabarito humano do repositório é o de #175 (`evals/gold/extracao-rotulos.json`): ele não rotula dedupe, categoria nem juiz, mas rotula exatamente o julgamento "isto merece nota permanente?", que é a capacidade de que os três sites dependem. É a medida limpa de PT-BR disponível hoje.

A sonda do `extract` é **medição, não porteira**: a ADR-049 (sem gate pré-LLM no extract) continua valendo, e nenhum resultado aqui a reabre por si só.

## Rodadas

Mesmo gabarito, mesmas linhas de `data/state.db`, modelo fixo `jev-1.13.0`, 3 permutações de ordem por pergunta `choice`.

| rótulo | tarefa | itens | idioma das instruções | perguntas |
|---|---|---|---|---|
| `extract-en` | extract | os itens julgados (`keep`/`discard`) dos 120 | `en` | `noul` guardar, `choice` categoria |
| `extract-pt` | extract | idem | `pt` | idem |
| `reader-en` | leitor | os 38 aceitos julgados (o estrato de `probe_reader_signal.py`) | `en` | `noul` guardar, `score` 1–5 |
| `reader-pt` | leitor | idem | `pt` | idem |

O conteúdo (passagem, notas) é sempre o texto original em PT-BR; só as instruções e os critérios mudam de idioma.

## Regra de decisão

1. **Sinal.** Uma rodada *tem sinal* quando o limite inferior do IC95 da AUC do `noul` "guardar" contra o rótulo humano fica **acima de 0,5** (`calibrate_review_confidence.auc_ci`, mesmo critério de #176). No leitor, o `score` é reportado ao lado, mas a regra usa o `noul`.
2. **Idioma.** Vence `pt` **se, e somente se**, a AUC de `extract-pt` superar a de `extract-en` em **≥ 0,05**; caso contrário vence `en`, a língua primária do modelo. Decide-se no `extract` porque tem a maior amostra. O vencedor vira `decision.instructions_language` em `config/config.yaml`.
3. **Estabilidade.** Na rodada vencedora, o desvio-padrão médio das probabilidades entre as 3 permutações da `choice` de categoria fica **≤ 0,05**. Acima disso, o viés de ordem é material e qualquer gate futuro precisa manter as permutações.
4. **Comparação com o extract de produção (informativa).** O GABARITO do Jev em limiar 0,5 é comparado com `gemini-t01-a` (modelo de produção, ADR-050) por `compare_gold_runs.py`. O número é reportado; não entra no desfecho, porque o extract gera a nota e o Jev só classifica.

### Critério para abrir a issue de gate (por site, sobre o shadow acumulado)

Só depois que a regra 1 der sinal na rodada vencedora:

- **n ≥ 30** decisões shadow no site, com taxa de erro de API ≤ 5%;
- concordância com a decisão atual **≥ 95%** entre as decisões com confiança do Jev **≥ 0,9**;
- no dedupe, quando houver ≥ 10 rótulos humanos (`m`/`d` do revisor), a concordância do Jev com o humano **≥** a do LLM atual.

Abrir a issue não troca nada: a issue de gate traz seu próprio pré-registro.

## Condições de validade

- **Falhas de API.** Cada rodada com no máximo 5% dos itens sem resposta. Acima disso, a rodada é inconclusiva.
- **Itens ausentes.** Item do gabarito sem linha em `data/state.db` sai da conta e é reportado; se faltarem mais de 10%, a rodada é inconclusiva.

## Desfechos possíveis

| situação | desfecho |
|---|---|
| regra 1 com sinal na rodada vencedora, estabilidade ok | **seguir** acumulando shadow; avaliar o critério de gate por site |
| regra 1 com sinal, estabilidade acima de 0,05 | **seguir**, com permutações obrigatórias em qualquer gate |
| regra 1 sem sinal nas duas línguas | **resultado nulo** — shadow pode ser desligado; nenhum gate |
| falhas acima do limite | **inconclusivo** — repetir a rodada, sem mudar a regra |

Resultado nulo é válido e será publicado como tal.

## Comandos

```bash
.venv/Scripts/python.exe scripts/probe_jev_gold.py --task extract --lang en --yes --out evals/results/jev-gold-extract-en.json --out-key evals/gold/extracao-GABARITO-jev-en.json
.venv/Scripts/python.exe scripts/probe_jev_gold.py --task extract --lang pt --yes --out evals/results/jev-gold-extract-pt.json --out-key evals/gold/extracao-GABARITO-jev-pt.json
.venv/Scripts/python.exe scripts/probe_jev_gold.py --task reader --lang en --yes --out evals/results/jev-gold-reader-en.json
.venv/Scripts/python.exe scripts/probe_jev_gold.py --task reader --lang pt --yes --out evals/results/jev-gold-reader-pt.json

.venv/Scripts/python.exe scripts/compare_gold_runs.py \
    --run gemini-t01-a=evals/gold/extracao-GABARITO-gemini-t01-a.json \
    --run jev-en=evals/gold/extracao-GABARITO-jev-en.json \
    --run jev-pt=evals/gold/extracao-GABARITO-jev-pt.json \
    --pair gemini-t01-a:jev-en \
    --pair gemini-t01-a:jev-pt \
    --pair jev-en:jev-pt \
    --out evals/results/jev-vs-extract-206.json

.venv/Scripts/python.exe scripts/report_decision_shadow.py --out evals/results/decision-shadow.json
```
