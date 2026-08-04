# Redacao de secao — artigo academico (ABNT)

Voce redige **uma secao** de um artigo academico em Markdown, usando
**exclusivamente** as evidencias do acervo abaixo. Nao invente fatos,
numeros, autores nem anos.

## Idioma

Escreva em **{language}**.

## Contexto do artigo

- Tema: {topic}
- Titulo do artigo: {article_title}
- Tese: {thesis}
- Notas de tom: {style_notes}

## Secao a redigir

- Heading: {heading}
- Objetivo: {goal}
- Extensao alvo: cerca de {target_chars} caracteres

## Regras (academico / ABNT NBR 10520)

1. Tom formal, preciso e objetivo.
2. Toda afirmacao substantiva deve trazer citacao autor-data usando
   **exatamente** a forma em `citacao_abnt` fornecida no contexto
   (ex.: `(NEGRO et al., 2026)`). Nao invente sobrenomes nem anos.
3. **Nao** use wikilinks `[[ZTL - ...]]` no corpo.
4. Se houver figura sugerida e for pertinente, embuta com
   `![[90_Assets/...]]` e uma legenda descritiva.
5. Comece a secao com `## {heading}` (exatamente esse heading).
6. Nao invente dados fora das evidencias. Se o acervo for insuficiente
   para um ponto, nao o afirme.

{anti_ai}

## Feedback do juiz (se houver reescrita)

{judge_feedback}

## Fontes disponiveis (citacoes e referencias)

{sources}

## Figuras sugeridas

{figures}

## Evidencias (notas do vault)

{evidence}

## Metadados obrigatorios no final

Ao final da secao, em uma linha isolada, liste as fontes realmente citadas
(citekeys / source_ids), no formato:

`<!-- cites: @Citekey1,@Citekey2 -->`

Se nao citou fonte, escreva `<!-- cites: -->`.

## Secao
