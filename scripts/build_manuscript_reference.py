"""Build ``zettel/resources/manuscript-reference.docx`` (ADR-067).

Pandoc's own reference document plus one paragraph style, "Complementar",
for the gaps the author fills: shaded and in italics, so Word's "find style"
lists every one. Pandoc keeps a style the reference already defines, so the
look comes from here. Run again after a pandoc upgrade:

    .venv/Scripts/python.exe scripts/build_manuscript_reference.py
"""

from __future__ import annotations

import subprocess
import tempfile
import zipfile
from pathlib import Path

import pypandoc
from zettel.manuscript import PLACEHOLDER_STYLE

TARGET = Path(__file__).resolve().parents[1] / "zettel" / "resources" / "manuscript-reference.docx"
STYLE = (
    f'<w:style w:type="paragraph" w:customStyle="1" w:styleId="{PLACEHOLDER_STYLE}">'
    f'<w:name w:val="{PLACEHOLDER_STYLE}"/><w:basedOn w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:shd w:val="clear" w:color="auto" w:fill="FFF2CC"/>'
    '<w:spacing w:before="120" w:after="120"/></w:pPr>'
    '<w:rPr><w:i/><w:color w:val="7F6000"/></w:rPr></w:style>'
)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        default = Path(tmp) / "default.docx"
        with default.open("wb") as out:
            subprocess.run(  # noqa: S603 -- the pandoc pypandoc ships, fixed arguments
                [pypandoc.get_pandoc_path(), "--print-default-data-file", "reference.docx"],
                stdout=out,
                check=True,
            )
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(default) as source, zipfile.ZipFile(TARGET, "w") as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "word/styles.xml":
                    xml = data.decode("utf-8")
                    if f'w:styleId="{PLACEHOLDER_STYLE}"' not in xml:
                        xml = xml.replace("</w:styles>", STYLE + "</w:styles>")
                    data = xml.encode("utf-8")
                target.writestr(item, data)
    print(f"{TARGET} (pandoc {pypandoc.get_pandoc_version()})")


if __name__ == "__main__":
    main()
