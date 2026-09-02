# Prompt: Guardrail de Idioma PT-BR

Revise os campos de uma nota permanente e corrija qualquer trecho que NÃO esteja em
português brasileiro (PT-BR).

## Regras

- Traduza termos em inglês para PT-BR quando houver equivalente natural.
- Mantenha termos técnicos consagrados em inglês (ex: "machine learning", "feedback", "framework") APENAS quando não houver tradução usual em PT-BR.
- Corrija concordância e ortografia se necessário.
- Mantenha a estrutura e formatação original de cada valor (Markdown inline, listas, quebras de linha).
- NÃO altere o significado.
- NÃO acrescente, remova nem renomeie chaves; um campo vazio na entrada volta vazio.

## Formato de saída

Responda APENAS com o **mesmo objeto JSON** recebido, com as cinco chaves
inalteradas e os valores corrigidos para PT-BR:

```json
{
  "thesis": "...",
  "definition": "...",
  "intuition": "...",
  "example": "...",
  "limits": "..."
}
```

Nada além do objeto: sem prosa antes ou depois, sem comentários, sem texto
Markdown envolvendo o JSON.

O objeto JSON a revisar segue na mensagem do usuário.

<!-- zettel:user -->

## Objeto JSON para revisão

{text}
