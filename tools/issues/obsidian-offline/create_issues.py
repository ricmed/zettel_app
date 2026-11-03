"""Create the Obsidian offline-authoring epic and its sub-issues on Project #3.

Runs ``tools/gh_project_task.py add`` once per body file in this folder: the epic
first, then each sub-issue linked to it. ``gh_project_task.py`` is idempotent by
title, so re-running after a partial failure only fills in what is missing.

    .venv/Scripts/python.exe tools/issues/obsidian-offline/create_issues.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parents[1] / "gh_project_task.py"
MILESTONE = "Fluxo manual de notas: paridade com o pipeline"

EPIC = (
    "00-epic.md",
    "Epic: autoria offline no Obsidian com a estrutura do pipeline e sync completo",
    ["--type-label", "epic", "--label", "enhancement"],
)

SUB_ISSUES = (
    (
        "01-init-preserva-vault.md",
        "Impedir que zettel init apague o vault e as notas manuais",
        ["--type-label", "bug", "--priority", "p0", "--area", "cli"],
    ),
    (
        "02-sync-tolerante.md",
        "Tornar o sync-manual tolerante a erro por nota, YAML inválido e datas",
        ["--type-label", "bug", "--priority", "p1", "--area", "cli"],
    ),
    (
        "03-sync-offline.md",
        "Sincronizar notas manuais sem embedding e completar os vetores depois",
        ["--priority", "p1", "--area", "cli", "--label", "area:web"],
    ),
    (
        "04-templates-obsidian.md",
        "Gerar templates do Obsidian a partir dos builders do vault",
        ["--priority", "p1", "--area", "cli"],
    ),
    (
        "05-init-obsidian.md",
        "Configurar o vault do Obsidian no init com Templater e folder templates",
        ["--priority", "p1", "--area", "cli"],
    ),
    (
        "06-resync-src.md",
        "Ressincronizar SRC editada no Obsidian",
        ["--priority", "p2", "--area", "cli"],
    ),
    (
        "07-grafo-manual.md",
        "Fechar o grafo das notas manuais em duas passadas e remover links apagados",
        ["--priority", "p2", "--label", "area:connect"],
    ),
    (
        "08-notas-apagadas.md",
        "Detectar notas manuais apagadas ou movidas no vault",
        ["--priority", "p2", "--area", "cli"],
    ),
    (
        "09-docs-adr.md",
        "Documentar a autoria no Obsidian e registrar o ADR-066",
        ["--type-label", "documentation", "--priority", "p2"],
    ),
)


def add(body_file: str, title: str, extra: list[str], parent: list[str]) -> dict:
    args = [
        sys.executable,
        str(TOOL),
        "add",
        "--title",
        title,
        "--body-file",
        str(HERE / body_file),
        "--milestone",
        MILESTONE,
        *parent,
        *extra,
    ]
    proc = subprocess.run(args, check=True, text=True, encoding="utf-8", capture_output=True)  # noqa: S603 - fixed argv, no shell
    sys.stderr.write(proc.stderr)
    return json.loads(proc.stdout)


def main() -> int:
    epic = add(*EPIC, parent=["--no-parent"])
    print(f"epic #{epic['number']}: {epic['url']}")
    for body_file, title, extra in SUB_ISSUES:
        issue = add(body_file, title, extra, parent=["--parent", str(epic["number"])])
        print(f"  #{issue['number']}: {issue['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
