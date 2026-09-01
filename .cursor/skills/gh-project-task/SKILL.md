---
name: gh-project-task
description: >-
  Creates a GitHub issue in ricmed/zettel_app and adds it to Project
  "Zettel - Board" (project #3) via tools/gh_project_task.py. Use when
  the user defines a backlog task, asks to criar issue, adicionar ao Project,
  registrar no board, track a all items, or pin work to "Zettel - Board".
  Do not use for local code TODOs unless they explicitly want it on GitHub.
---

# GitHub Project task (Zettel)

When the user defines a task to track, **do not** recreate GraphQL/`gh` by hand. Draft the issue, then run the repo script so the card lands on the Project with Status `Todo`.

## When to run

Run if the user:

- asks to criar/registrar/adicionar uma issue ou tarefa no Project / board
- defines a `Low`, `Medium`, `High`, or `Critical` backlog item “para o GitHub”
- says “adicione ao Zettel Board”

Do **not** run for implementation scratch TODOs, drive-by refactors, or “we should maybe…” unless they confirm they want a GitHub issue.

## Draft the issue first

Title: imperative PT-BR, outcome-focused (same style as #4–#9). No conventional-commit prefix.

Body (markdown), keep it implementable:

```markdown
## Resumo
## Motivação
## User story
## Contrato / comportamento
## Arquivos a tocar
## Fora de escopo
## Critérios de aceite
- [ ]
## Testes
## Dependências
```

Omit empty sections. Infer:

| Flag | Values | How |
| --- | --- | --- |
| `--priority` | `Low`…`Critical` | user uses low, medium, high, or critical |
| `--area` | `cli` `extract` `harvest` `ask` `evals` | module touched |
| `--parent` | default `#id` | create a epic name; `--no-parent` if they say it is a new epic or unrelated |
| `--milestone` | None. Create one. | `--no-milestone` if unrelated to that roadmap |

Always keep type label `enhancement` unless they ask for `bug`.

## Execute

Write the body to a temp `.md` file (UTF-8), then from the repo root:

```bash
python tools/gh_project_task.py add \
  --title "TITULO" \
  --body-file PATH_TO_BODY.md \
  --priority p1 \
  --area extract \
  --parent 10
```

On this machine the venv interpreter is also fine: `.venv/Scripts/python.exe tools/gh_project_task.py add ...`

Requires `gh` with scopes `repo` and `project`. If `gh` fails on `read:project` / `project`, tell the user to run:

```bash
gh auth refresh -s repo -s project -s read:project -s read:org
```

Do **not** spawn a subagent for this. One shell call is enough.

## After it runs

The script prints JSON (`number`, `url`, `project`). Reply with the issue URL and that Status is `Todo`. If `issue exists` / `already in project`, report the existing URL instead of creating a duplicate.

Idempotent: same `--title` among open issues reuses that issue and still ensures it is on the board.

## Terminal-only (no agent)

Same command. Env overrides: `ZETTEL_GH_REPO`, `ZETTEL_GH_OWNER`, `ZETTEL_GH_PROJECT` (number, default `3`), `ZETTEL_GH_MILESTONE`, `ZETTEL_GH_PARENT`.
