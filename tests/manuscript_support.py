"""Shared seed for the manuscript tests (#385): sources, notes and a desk body.

A helper module, not a test file: test files import from it, never from each other.
"""

from __future__ import annotations

import json
from pathlib import Path

from zettel.config import AppConfig
from zettel.project_file import parse_body
from zettel.projects import Project
from zettel.state import StateDB

N1, N2, N3, N4, N5 = (f"01J{c * 23}" for c in "ABCDE")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
ASSET = "img-0123456789abcdef.png"


def seed_desk(tmp_path: Path) -> tuple[AppConfig, StateDB]:
    """A vault with one figure and a StateDB with two sources and five notes."""
    vault = tmp_path / "vault"
    (vault / "90_Assets").mkdir(parents=True)
    (vault / "90_Assets" / ASSET).write_bytes(PNG)
    cfg = AppConfig(vault_path=vault, state_db_path=tmp_path / "state.db")
    db = StateDB(tmp_path / "state.db")
    db.upsert_source(
        "@Zuboff2019",
        "Zuboff2019",
        "A era do capitalismo de vigilância",
        ["Shoshana Zuboff"],
        2019,
        "f1",
        "x",
        "pdf",
        abnt_reference="ZUBOFF, Shoshana. A era do capitalismo de vigilância. 2019.",
    )
    db.upsert_source(
        "@Alvares2020",
        "Alvares2020",
        "Modelos de crédito",
        ["Ana Álvares"],
        2020,
        "f2",
        "y",
        "markdown",
        bibliography_json=json.dumps(
            {
                "document_type": "livro",
                "title": "Modelos de crédito",
                "authors": ["Ana Álvares"],
                "year": 2020,
                "place": "São Paulo",
                "publisher": "Atlas",
            }
        ),
    )
    figure_body = f"\n\n## Figuras\n\n![[90_Assets/{ASSET}]]\n\nCurva ROC dos modelos.\n"
    notes = [
        (N1, "@Zuboff2019", "Dados como matéria-prima", "(ZUBOFF, 2019, p. 12)", figure_body),
        (N2, "@Zuboff2019", "Previsão como produto", "(ZUBOFF, 2019, p. 40)", figure_body),
        (N3, "@Zuboff2019", "Mercados de comportamento", "(ZUBOFF, 2019, p. 77)", ""),
        (N4, "@Alvares2020", "Boosting supera logística", "(ÁLVARES, 2020)", ""),
        (N5, None, "Nota manual sem fonte", "", ""),
    ]
    for note_id, source, title, cite, extra in notes:
        provenance = {"citation": cite, "anchor_quote": f"trecho de {title}"} if cite else {}
        db.upsert_note(
            note_id,
            source,
            f"30_Permanent/ZTL - {note_id} - n.md",
            title=title,
            body=f"> **Tese**: {title} é a tese.\n\nDefinição de {title}, ver "
            f"[[ZTL - {N4} - b|o boosting]].\n\n## Limites\n\nLimite de {title}.{extra}",
            provenance_json=json.dumps(provenance),
            origin="manual" if source is None else "pipeline",
        )
    return cfg, db


def desk_project(tmp_path: Path, body: str, style: str = "academic") -> Project:
    meta = {"title": "ML em crédito", "style": style}
    return Project("01JPROJECT0000000000000000", tmp_path / "PRJ.md", meta, parse_body(body), "v")


BODY = f"""Por que olhar o crédito com dados.

## Dados
- [[ZTL - {N1} - a]]
- [[ZTL - {N2} - b]]
  serve de ponte

## Modelos
Abertura escrita pelo autor.
- [[ZTL - {N4} - d]]
- [[ZTL - {N3} - c]]
- [[ZTL - {N5} - e]]
"""
