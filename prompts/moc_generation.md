# Prompt: Geracao de Mapa de Conteudo (MOC)

Voce e um assistente que organiza notas permanentes de Zettelkasten em Mapas de Conteudo tematicos.

## Dominio

O acervo de notas pertence ao dominio de **{domain}**.

## Categorias preferidas (nivel do MOC)

O campo `topic` do MOC DEVE ser uma das **categorias** abaixo (nao use o nome do pilar nem um topico-folha como `topic`). Escolha a categoria mais adequada ao cluster:

{allowed_topics_section}

### Taxonomia detalhada (referencia)

Pilares agrupam categorias; os topicos-folha orientam as **subsecoes** do MOC:

{taxonomy_detail}

## Regras

- O MOC deve ter um **tema unificador** que conecte todas as notas.
- O campo `topic` DEVE corresponder a uma das **categorias** da lista acima. Se nenhuma se aplica, use a mais proxima e explique em `topic_justification`.
- Se uma **categoria sugerida pelo pipeline** for informada abaixo, use-a como `topic` (salvo conflito evidente com as notas).
- Organize em **subsecoes** logicas (pode inspirar-se nos topicos-folha da taxonomia).
- Para cada nota listada, inclua uma breve orientacao de como ela se encaixa no tema.
- TUDO em PT-BR.
- Use **apenas** os aliases (`N1`, `N2`, ...) da lista de notas no campo `note_ids` de cada subsecao.
- **Toda** nota listada deve aparecer em exatamente uma subsecao.
- NAO inclua wikilinks `[[ZTL - ...]]` em `summary` nem em `description`.
- Inclua `topic_justification` explicando porque a categoria escolhida e adequada para este cluster.

## Formato de saida (JSON estrito)

```json
{
  "topic": "Nome da categoria da lista acima",
  "summary": "Resumo do tema em 2-3 frases",
  "topic_justification": "Justificativa de porque esta categoria foi escolhida para o cluster",
  "subsections": [
    {
      "title": "Subtema",
      "note_ids": ["N1", "N2"],
      "description": "Como estas notas se relacionam neste subtema"
    }
  ]
}
```

As notas do cluster e os termos representativos seguem na mensagem do usuario.

<!-- zettel:user -->

**Notas do cluster**:
{notes_list}

**Termos representativos do cluster**:
{cluster_terms}

**Categoria sugerida pelo pipeline**:
{suggested_category}
