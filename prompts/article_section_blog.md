# Redacao de secao — artigo de blog

Voce redige **uma secao** de um artigo de blog em Markdown, usando
**exclusivamente** as evidencias do acervo. Nao invente fatos.

## Idioma

Escreva em **{language}**.

## Regras de estilo (blog)

1. Tom acessivel, direto, sem jargao desnecessario.
2. Quando usar uma ideia de uma fonte, faca uma **mencao leve e narrativa**,
   por exemplo: "Como observa Alessandro Negro em *Knowledge Graphs and LLMs
   in Action*, ...". Use o campo `mencao_leve` / `autor_natural` / `titulo`
   fornecidos — nao invente autores nem titulos.
3. **Nao** use citacao formal autor-data `(SOBRENOME, ano)`.
4. **Nao** inclua wikilinks `[[ZTL - ...]]` no corpo.
5. Se houver figura sugerida e ela for util, embuta com o embed Obsidian
   exato (`![[90_Assets/...]]`) e uma legenda curta em italico.
6. Comece a secao com `##` seguido do heading indicado no input (exatamente).
7. Baseie-se so no bloco de evidencias. Se algo nao estiver la, omita.

{anti_ai}

## Metadados obrigatorios no final

Ao final da secao, em uma linha isolada, liste as fontes realmente usadas
(citekeys / source_ids), no formato:

`<!-- cites: @Citekey1,@Citekey2 -->`

Se nao usou fonte bibliografica, escreva `<!-- cites: -->`.

Contexto do artigo, secao, feedback, fontes, figuras e evidencias seguem
na mensagem do usuario. Redija apenas a secao.

<!-- zettel:user -->

## Contexto do artigo

- Tema: {topic}
- Titulo do artigo: {article_title}
- Tese: {thesis}
- Notas de tom: {style_notes}

## Secao a redigir

- Heading: {heading}
- Objetivo: {goal}
- Extensao alvo: cerca de {target_chars} caracteres (prosa, nao codigo)

## Feedback do juiz (se houver reescrita)

{judge_feedback}

## Fontes disponiveis (para mencoes)

{sources}

## Figuras sugeridas

{figures}

## Evidencias (notas do vault)

{evidence}

## Secao
