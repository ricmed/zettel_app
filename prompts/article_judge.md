# Juiz de qualidade do artigo

Voce avalia um artigo gerado **apenas** com base nas notas de contexto
fornecidas. Seja rigoroso com fidelidade factual.

## Idioma

Responda em **{language}** (campos de texto do JSON).

## Tema

{topic}

## Estilo

`{style}`

## Notas de contexto (evidencia permitida)

{notes_catalog}

## Artigo a avaliar

{article_body}

## Criterios (0 a 10 cada)

1. **fidelity** — afirmacoes sustentadas pelo contexto; sem invencao
2. **coverage** — cobre o tema proposto de forma adequada
3. **references** — qualidade das mencoes/citas conforme o estilo
4. **naturalness** — prosa natural, sem padroes roboticos excessivos

## Saida

JSON unico:

```json
{
  "fidelity": 0,
  "coverage": 0,
  "references": 0,
  "naturalness": 0,
  "average": 0.0,
  "verdict": "APPROVED",
  "feedback": "Se REJECTED, feedback acionavel citando lacunas ou contradicoes com as notas. Se APPROVED, breve justificativa."
}
```

`verdict` deve ser `APPROVED` ou `REJECTED`.
Calcule `average` como media aritmetica dos quatro scores.
