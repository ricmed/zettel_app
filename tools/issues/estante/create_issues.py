"""Cria o épico da Estante e as sub-issues no Project #3.

Roda ``tools/gh_project_task.py add`` uma vez por arquivo de corpo desta pasta:
primeiro o épico, depois cada sub-issue ligada a ele. ``gh_project_task.py`` é
idempotente pelo título, então rodar de novo após uma falha parcial só completa
o que falta.

    .venv/Scripts/python.exe tools/issues/estante/create_issues.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parents[1] / "gh_project_task.py"
MILESTONE = "Estante"
AREA = ["--label", "area:estante"]

EPIC = (
    "00-epic.md",
    "Epic: Estante — gestão de livros integrada ao vault",
    ["--type-label", "epic", "--label", "enhancement"],
)

SUB_ISSUES = (
    ("01-nota-liv.md", "Nota LIV, espelho SQLite e config da Estante", ["--priority", "p1"]),
    ("02-avaliacao.md", "Avaliação ponderada por perfil configurável", ["--priority", "p1"]),
    (
        "03-ligacao-fontes.md",
        "Ligar livros às fontes: harvest cria a LIV, religação após reset, SRC manual",
        ["--priority", "p1", "--area", "harvest"],
    ),
    (
        "04-harvest-fonte-sem-texto.md",
        "Harvest preenche fonte sem texto achada por DOI/ISBN",
        ["--priority", "p2", "--area", "harvest"],
    ),
    (
        "05-paginas-books.md",
        "Páginas /books: lista, galeria, quadro, detalhe, edição e capa",
        ["--priority", "p1", "--area", "web"],
    ),
    (
        "06-notas-de-leitura.md",
        "Notas de leitura e ZTL a partir do livro",
        ["--priority", "p2", "--area", "web"],
    ),
    (
        "07-painel.md",
        "Painel da Estante com gráficos SVG",
        ["--priority", "p2", "--area", "web"],
    ),
    (
        "08-prompts-reflexivos.md",
        "Prompts reflexivos de LIT e ZTL para livros da Estante, com pré-registro",
        ["--priority", "p1", "--area", "extract"],
    ),
    (
        "09-ask-article.md",
        "Notas reflexivas como analogia em ask e no article blog",
        ["--priority", "p2", "--area", "ask"],
    ),
    (
        "10-docs.md",
        "ADRs, README, CLAUDE.md e docs da Estante",
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
        *AREA,
    ]
    # O processo filho imprime títulos acentuados; no Windows o stdout seria cp1252.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(  # noqa: S603 - argv fixo, sem shell
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
