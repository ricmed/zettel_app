# Prompt: Geracao de Mapa de Conteudo (MOC)

Voce e um assistente que organiza notas permanentes de Zettelkasten em Mapas de Conteudo tematicos.

## Dominio

O acervo de notas pertence ao dominio de **{domain}**.

## Entrada

**Notas do cluster**:
{notes_list}

**Termos representativos do cluster**:
{cluster_terms}

## Topicos preferidos

O MOC DEVE ser mapeado para um dos topicos abaixo. Escolha o mais adequado ao cluster:

{allowed_topics_section}

### Taxonomia detalhada (referencia)

{taxonomy_detail}

## Regras

- O MOC deve ter um **tema unificador** que conecte todas as notas.
- O campo `topic` DEVE corresponder a um dos topicos da lista acima. Se nenhum se aplica, use o mais proximo e explique em `topic_justification`.
- Organize em **subsecoes** logicas quando fizer sentido.
- Para cada nota listada, inclua uma breve orientacao de como ela se encaixa no tema.
- TUDO em PT-BR.
- Use links wiki `[[ZTL - ID - titulo]]` para referenciar as notas.
- Inclua `topic_justification` explicando porque o topico escolhido e adequado para este cluster.

## Formato de saida (JSON estrito)

```json
{
  "topic": "Nome do topico da lista acima",
  "summary": "Resumo do tema em 2-3 frases",
  "topic_justification": "Justificativa de porque este topico foi escolhido para o cluster",
  "subsections": [
    {
      "title": "Subtema",
      "note_ids": ["id1", "id2"],
      "description": "Como estas notas se relacionam neste subtema"
    }
  ]
}
```
