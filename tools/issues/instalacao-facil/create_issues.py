"""Create the installation epic and its sub-issues on Project #3.

Runs ``tools/gh_project_task.py add`` once per body file in this folder: the epic
first, then each sub-issue linked to it. ``gh_project_task.py`` is idempotent by
title, so re-running after a partial failure only fills in what is missing.

    .venv/Scripts/python.exe tools/issues/instalacao-facil/create_issues.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parents[1] / "gh_project_task.py"
MILESTONE = "Instalação facilitada"

EPIC = (
    "00-epic.md",
    "Epic: instalação do zero ao primeiro harvest sem tropeços",
    ["--type-label", "epic", "--label", "enhancement"],
)

SUB_ISSUES = (
    (
        "01-remover-replit.md",
        "Remover o Replit e as referências a ele",
        ["--priority", "p1"],
    ),
    (
        "02-torch-extras.md",
        "Instalar torch CPU ou CUDA por extra do uv",
        ["--priority", "p1"],
    ),
    (
        "03-higiene-pyproject.md",
        "Limpar dependências do pyproject e arquivos soltos da raiz",
        ["--priority", "p2"],
    ),
    (
        "04-doctor-init.md",
        "Fazer o doctor checar a chave de cada fase e o init funcionar sem embedding",
        ["--priority", "p1", "--area", "cli"],
    ),
    (
        "05-scripts-setup.md",
        "Criar scripts de setup para Windows e Linux/macOS",
        ["--priority", "p2"],
    ),
    (
        "06-docker.md",
        "Empacotar web UI e CLI em Docker",
        ["--priority", "p2", "--area", "web"],
    ),
    (
        "07-docs-instalacao.md",
        "Atualizar README, instalacao.md e .env.example com os requisitos reais",
        ["--type-label", "documentation", "--priority", "p1"],
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
    # The child prints accented titles; on Windows its stdout would be cp1252.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, check=True, text=True, encoding="utf-8", capture_output=True, env=env
    )
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
