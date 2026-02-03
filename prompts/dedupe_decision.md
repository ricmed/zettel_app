# Prompt: Decisão de Deduplicação

Você é um assistente que decide se um novo candidato a nota permanente é duplicata de notas existentes.

## Entrada

**Candidato novo**:
- Tese: {new_thesis}
- Definição: {new_definition}

**Notas existentes mais similares**:
{existing_notes}

## Decisões possíveis

- `create_new`: o candidato é suficientemente distinto — criar nova nota
- `ignore`: o candidato é idêntico ou trivialmente redundante — descartar
- `refine_existing`: o candidato traz nuance nova sobre um tema já coberto — criar nova nota conectada à existente, formando uma linha de pensamento

## Formato de saída (JSON estrito)

```json
{
  "decision": "create_new | ignore | refine_existing",
  "target_note_id": "ID da nota alvo (se refine_existing, senão null)",
  "reason": "Justificativa breve da decisão"
}
```
