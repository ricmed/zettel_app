"""A estante antiga não volta para o código de produto.

O módulo de migração é a única exceção: ele precisa nomear a pasta e o prefixo
que remove. A prosa histórica dos ADRs fica de fora desta varredura.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("60_Estante", "LIV - ")
ALLOWED = {ROOT / "zettel" / "library" / "migrate.py"}


def test_product_code_does_not_name_the_old_shelf():
    roots = [ROOT / "zettel", ROOT / "zettel" / "templates"]
    hits = []
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".html", ".js", ".css"} or path in ALLOWED:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in FORBIDDEN:
                if token in text:
                    hits.append(f"{path.relative_to(ROOT)}: {token}")
    assert hits == []
