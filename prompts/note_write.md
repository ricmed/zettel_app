# Escrita da nota permanente

Você escreve uma nota de Zettelkasten em **{language}**, no domínio **{domain}**.

O review humano já aprovou o candidato. Esta chamada não recusa a nota.

- Mantenha a afirmação da tese aprovada. Só corrija forma: pontuação, concordância, espaço.
- Não acrescente afirmação que não esteja no candidato ou no trecho-fonte.
- Definição, intuição, exemplo e limites podem organizar o que já está no candidato e no trecho. Não invente caso, número ou mecanismo ausentes dos dois.
- Devolva somente um objeto JSON.

```json
{
  "title": "Titulo declarativo",
  "thesis": "A tese aprovada, com a mesma afirmacao.",
  "definition": "O que o trecho e o candidato ja dizem.",
  "intuition": "",
  "example": "",
  "limits": ""
}
```

<!-- zettel:user -->

Candidato aprovado:

- Tese: {thesis}
- Definição: {definition}
- Intuição: {intuition}
- Limites: {limits}

Citação-âncora (literal, já conferida no extract):

{anchor_quote}

Trecho-fonte:

{source_excerpt}
