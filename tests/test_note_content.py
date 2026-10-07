"""The content of a permanent note as prompts read it (#209)."""

from zettel.note_content import note_sections, render_note_content, thesis_from_permanent_note


def _ztl(thesis, definition, intuition=""):
    body = f"> **Tese**: {thesis}\n\n## Definição\n\n{definition}\n"
    if intuition:
        body += f"\n## Intuição\n\n{intuition}\n"
    body += "\n## Fonte\n\n[[LIT - X]]\n"
    body += "\n<!-- zettel:auto-evidence:start -->\n> citacao\n<!-- zettel:auto-evidence:end -->\n"
    return body + "\n## Conexões\n\n- corroborado por [[ZTL - 01X]]\n"


def test_sections_keep_content_and_drop_source_evidence_and_connections():
    sections = note_sections("T", _ztl("Tese longa", "Def longa", "Intu"))
    assert sections == {
        "thesis": "Tese longa",
        "definition": "Def longa",
        "intuition": "Intu",
        "example": "",
        "limits": "",
    }


def test_thesis_falls_back_to_the_title():
    assert thesis_from_permanent_note({"title": "Titulo"}, "sem linha de tese") == "Titulo"


def test_render_labels_filled_fields_in_reading_order():
    text = render_note_content({"limits": "L", "thesis": "T", "example": ""})
    assert text == "Tese: T\n\nLimites: L"


def test_max_chars_is_a_ceiling_not_an_excerpt():
    fields = {"thesis": "T" * 50}
    assert render_note_content(fields, max_chars=1000) == "Tese: " + "T" * 50
    assert render_note_content(fields, max_chars=20).endswith("...")
