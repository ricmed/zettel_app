# Documentação do Zettelkasten Pipeline

[← Voltar ao README](../README.md)

Índice dos guias. Cada um é autossuficiente: começa pelo escopo e linka os assuntos vizinhos.

## Começando

| Guia | O que responde |
|---|---|
| [Instalação](instalacao.md) | Pré-requisitos, `uv sync`, `.env`, GPU, `init`, testes |
| [Configuração](configuracao.md) | Catálogo completo do `config.yaml`, provedores de LLM e embedding, caches, troca de modelo |
| [Comandos (CLI)](cli.md) | Referência de todos os comandos e flags |

## Entendendo o sistema

| Guia | O que responde |
|---|---|
| [Arquitetura](arquitetura.md) | Mapa dos módulos, estrutura do vault, anti-drift, IDs estáveis, custos |
| [Pipeline](pipeline.md) | O que cada fase faz: harvest, paginação, extract, review, connect, garden, garden hub |
| [Notas geradas](notas.md) | Formato de SRC, índice LIT, LIT granular e ZTL; tipos ABNT |
| [Recuperação](recuperacao.md) | Busca híbrida (vetor + BM25 + RRF), GraphRAG, piso de relevância, `ask` e `article` |

## Trabalhando com o vault

| Guia | O que responde |
|---|---|
| [Notas manuais](notas-manuais.md) | `new-note`, `sync-manual`, adoção de LIT e de imagens, caminho LIT → ZTL |
| [Interface web](interface-web.md) | Subir a UI, páginas, fila de jobs, o que é exclusivo da CLI |

## Operando

| Guia | O que responde |
|---|---|
| [Operação](operacao.md) | Retenção, `reindex`/`rebuild`/`rechunk`, dumps, purga, remoção de fonte, backup |
| [Solução de problemas](troubleshooting.md) | Sintomas comuns e como sair deles |
| [Prompts e taxonomia](prompts.md) | Personalizar `prompts/`, `moc_topics.yaml` e as personalidades do `article` |

## Decisões de arquitetura

| Documento | Conteúdo |
|---|---|
| [Índice de ADRs](adrs/ADR-INDEX.md) | 31 decisões formais em 12 módulos |
| [Visão geral dos ADRs](adrs/ADR-OVERVIEW.md) | Panorama e relações entre decisões |
| [RUNBOOK](adrs/RUNBOOK.md) | Procedimentos operacionais e critérios de ajuste de limiares |
| [Checklist de code review](code-review-adr-checklist.md) | O que verificar em mudanças que tocam decisões registradas |
