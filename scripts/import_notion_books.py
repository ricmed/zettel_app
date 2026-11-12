"""Importa o banco "Leituras" exportado do Notion para a Estante (ADR-068).

Importação única. Sem ``--apply``, só mostra o relatório: o que seria removido
e como cada livro seria lido. Com ``--apply``:

1. remove as LIV que já estão em ``60_Estante/`` e as fontes delas (``purge_source``);
2. cria uma LIV por livro do CSV, na ordem da data de término (o código segue as
   leituras), com notas, capa e a resenha da página em ``## Resumo``;
3. grava editora, edição, ano desta edição, tradutor e ISBN no SRC manual (ADR-057).

As colunas ``FNQBIC Nome`` são os critérios da Estante; as letras dizem em que
tipo de livro o critério vale (F ficção, N não ficção, Q quadrinho, B biografia,
I ilustrado, C contos). Quando o perfil que os gêneros escolhem não é dessa
família, a LIV ganha ``rating_profile`` com a família das notas dadas.

    .venv/Scripts/python.exe scripts/import_notion_books.py
    .venv/Scripts/python.exe scripts/import_notion_books.py --apply --download-covers
"""

from __future__ import annotations

import argparse
import csv
import datetime
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zettel.config import load_config, setup_logging  # noqa: E402
from zettel.harvester.biblio_dedupe import normalize_isbn  # noqa: E402
from zettel.library import book_file as bf  # noqa: E402
from zettel.library import editing as ed  # noqa: E402
from zettel.library.rating import is_translated, profile_for, rate  # noqa: E402
from zettel.library.schema import LibrarySchema, schema_for  # noqa: E402

STATUS = {"Lido": "lido", "Lendo": "lendo"}
FORMAT = {"Físico": "fisico", "E-book": "digital", "Áudio Livro": "audiolivro"}

# Gênero do Notion -> gêneros da Estante. Um nome igual nos dois não precisa estar aqui.
GENRES = {
    "Contos": ["Conto"],
    "Biográfico": ["Biografia"],
    "Religião": ["Religião e Espiritualidade"],
    "Fábula": ["Mitologia e Fábula"],
    "Thriller": ["Suspense"],
    "Thriller Psicológico": ["Suspense", "Ficção Psicológica"],
    "Drama Psicológico": ["Drama", "Ficção Psicológica"],
    "Romance Filosófico": ["Romance", "Ficção Filosófica"],
    "Ópera Espacial": ["Ficção Científica"],
    "Steampunk": ["Ficção Científica"],
    "Roubo": ["Policial"],
    "Apocalipse": ["Pós-apocalíptico"],
    "LGBT": ["LGBTQIA+"],
    "Fatos Reais": ["Baseado em fatos reais"],
    "Não-Ficção": [],  # vira a natureza, não um gênero
}
SUBJECTS = {
    "Análise de Dados": "Dados e IA",
    "Data Science": "Dados e IA",
    "Mineração de Dados": "Dados e IA",
    "SQL": "Dados e IA",
    "Banco de Dados": "Dados e IA",
    "Python": "Programação",
    "Numpy": "Programação",
    "Pandas": "Programação",
    "Foco": "Produtividade",
    "Produtividade": "Produtividade",
}
# Rótulo do Notion que difere do rótulo do critério na Estante.
CRITERION_ALIASES = {"Integração TI": "text_image_integration"}
# Letra da máscara -> família de perfis da Estante (os pesos são os mesmos).
FAMILY = {
    "F": "ficcao",
    "N": "tecnico",
    "Q": "quadrinho",
    "B": "biografia",
    "I": "ilustrado",
    "C": "conto",
}
TRANSLATED_NAME = {
    "ficcao": "ficcao_traduzida",
    "tecnico": "tecnico_traduzido",
    "quadrinho": "quadrinho_traduzido",
    "biografia": "biografia_traduzida",
    "ilustrado": "ilustrado_traduzido",
    "conto": "conto_traduzido",
}
MASK = re.compile(r"^([FNQBICx]{6}) (.+)$")
NOTION_ID = re.compile(r" ([0-9a-f]{32})$")
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})([ \t]+.*)$")
FENCE = re.compile(r"^\s*(```|~~~)")
SUFFIXES = {".jfif": ".jpg", ".jpeg": ".jpeg", ".jpg": ".jpg", ".png": ".png", ".webp": ".webp"}


@dataclass
class Entry:
    row: dict[str, str]
    page: Path | None
    notion_id: str | None
    fields: dict[str, Any] = field(default_factory=dict)
    ratings: dict[str, float] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    cover: str = ""
    finished: str | None = None
    profile: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.fields["title"]


# ── Leitura do export ──────────────────────────────────────────────────


def read_pages(folder: Path) -> dict[str, Path]:
    """Página .md por título (o H1); o nome do arquivo vem truncado pelo Notion."""
    pages = {}
    for path in sorted(folder.glob("*.md")):
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
        pages[first.removeprefix("# ").strip()] = path
    return pages


def split_page(text: str) -> tuple[dict[str, str], str]:
    """Propriedades (linhas ``Chave: valor`` depois do H1) e o corpo depois delas."""
    lines = text.split("\n")
    props: dict[str, str] = {}
    i = 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines) and lines[i].strip() and ":" in lines[i]:
        key, value = lines[i].split(":", 1)
        props[key.strip()] = value.strip()
        i += 1
    return props, "\n".join(lines[i:]).strip("\n")


def demote_headings(body: str) -> str:
    """Títulos um nível abaixo e nunca acima de H3: o texto fica dentro de ``## Resumo``."""
    out, in_fence = [], False
    for line in body.split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING.match(line)
        if m:
            level = min(6, max(3, len(m.group(1)) + 1))
            line = "#" * level + m.group(2)
        out.append(line)
    return "\n".join(out)


def catalog_value(body: str, *labels: str) -> str:
    """O valor de ``- **Rótulo:** valor`` em "Dados Catalográficos"."""
    for label in labels:
        m = re.search(rf"^\s*[-*] \*\*{label}[^*]*\*\*\s*(.+)$", body, re.M)
        if m:
            return m.group(1).strip()
    return ""


def clean_translator(raw: str) -> str:
    text = re.sub(r"\[\[\d+\]\]\([^)]*\)", "", raw)
    text = re.split(r" [\u2013\u2014] |;| \(|\*\(|,\s*diretamente", text)[0]
    text = text.replace("*", "").strip(" .")
    return "" if not text or "http" in text or len(text) > 80 else text


def find_isbn(body: str) -> str:
    for label in ("ISBN-13", "ISBN", "ISBN-10"):
        raw = catalog_value(body, re.escape(label))
        raw = re.sub(r"\[\[\d+\]\]\([^)]*\)", "", raw)
        for candidate in re.findall(r"[0-9][0-9\-\s]{8,16}[0-9Xx]", raw):
            isbn = normalize_isbn(candidate)
            if isbn:
                return isbn
    return ""


def split_authors(raw: str) -> list[str]:
    raw = re.sub(r"\s*\([^)]*\)", "", raw)
    parts = re.split(r",| e ", raw)
    return [" ".join(p.split()) for p in parts if p.strip()]


def edition_number(raw: str) -> str:
    m = re.fullmatch(r"(\d+)\s*ª?(?:\s*(?:edição|ed\.))?", raw.strip())
    return m.group(1) if m else ""


def finish_date(raw: str) -> str | None:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", raw.strip())
    return datetime.date(int(m[3]), int(m[2]), int(m[1])).isoformat() if m else None


def criterion_keys(schema: LibrarySchema) -> dict[str, str]:
    keys = {spec.label: key for key, spec in schema.criteria.items()}
    keys.update(CRITERION_ALIASES)
    return keys


def coverage(schema: LibrarySchema, name: str | None, rated: set[str]) -> int:
    profile = schema.profile(name) if name else None
    return len(rated & set(profile.weights)) if profile else 0


def fit_profile(entry: Entry, schema: LibrarySchema) -> None:
    """Encaixa o livro num perfil que use as notas dadas, mexendo o mínimo.

    O perfil vem dos gêneros. Um livro sem gênero que dê natureza (nenhum, ou só
    neutros) recebe a da família de perfis que cobre mais critérios avaliados; gêneros
    que se contradizem (Biografia + Quadrinho) ficam como estão. Só quando o perfil
    dos gêneros cobre menos da metade das notas a LIV ganha ``rating_profile``.
    """
    rated = set(entry.ratings)
    if not rated:
        return
    meta = {
        "genres": entry.fields["genres"],
        "nature": entry.fields["nature"] or None,
        "country": entry.fields["country"],
    }
    translated = is_translated(meta)
    names = [TRANSLATED_NAME[f] if translated else f for f in FAMILY.values()]
    best = max(names, key=lambda n: coverage(schema, n, rated))
    natures = {schema.genre(g).nature for g in meta["genres"]} - {"neutro"}
    if not meta["nature"] and not natures:
        fiction = best.startswith(("ficcao", "quadrinho", "conto"))
        meta["nature"] = entry.fields["nature"] = "ficcao" if fiction else "nao_ficcao"
        entry.notes.append(f"natureza {meta['nature']} pelas notas")
    chosen = profile_for(schema, meta)
    chosen_name = chosen.name if chosen else None
    if 2 * coverage(schema, chosen_name, rated) < len(rated):
        entry.profile = best
        entry.notes.append(f"perfil {best} (gêneros dariam {chosen_name or '-'})")


def build_entry(row: dict[str, str], page: Path | None, schema: LibrarySchema) -> Entry:
    m = NOTION_ID.search(page.stem) if page else None
    entry = Entry(row=row, page=page, notion_id=m.group(1) if m else None)
    notion_genres = [g.strip() for g in row["Gênero"].split(",") if g.strip()]
    genres: list[str] = []
    for g in notion_genres:
        genres.extend(GENRES.get(g, [g]))
    unknown = [g for g in dict.fromkeys(genres) if g not in schema.genres]
    if unknown:
        entry.notes.append(f"gênero fora do vocabulário: {', '.join(unknown)}")
    subjects = [SUBJECTS.get(s.strip(), s.strip()) for s in row["Assunto"].split(",") if s.strip()]
    body = ""
    if page:
        _, body = split_page(page.read_text(encoding="utf-8"))
    isbn = find_isbn(body)
    entry.fields = {
        "title": row["Título"].strip(),
        "authors": "\n".join(split_authors(row["Autor"])),
        "original_title": row["Título Original"].strip(),
        "series": row["Série"].strip(),
        "pages": row["Pág Totais"].strip(),
        "country": row["Pais"].strip(),
        "original_publication_year": row["Ano Publicação Original"].strip(),
        "isbn": isbn,
        "genres": list(dict.fromkeys(genres)),
        "subjects": list(dict.fromkeys(subjects)),
        "nature": "nao_ficcao" if "Não-Ficção" in notion_genres else "",
        "format": FORMAT[row["Tipo"].strip()],
        "status": STATUS[row["Status"].strip()],
    }
    keys = criterion_keys(schema)
    for column, value in row.items():
        mm = MASK.match(column)
        if not mm or not value.strip():
            continue
        entry.ratings[keys[mm.group(2)]] = float(value)
    edition_raw = row["Edição"].strip()
    edition = edition_number(edition_raw)
    if edition_raw and not edition:
        entry.notes.append(f"edição descartada: {edition_raw!r}")
    year = row["Ano Publicação"].strip()
    entry.source = {
        "publisher": " ".join(row["Editora"].split()),
        "edition": edition,
        "translator": clean_translator(catalog_value(body, "Tradu")),
        "year": int(year) if year.isdecimal() and len(year) == 4 else None,
    }
    entry.summary = demote_headings(body)
    entry.cover = row["Capa"].strip()
    entry.finished = finish_date(row["Data Término"])
    if not entry.cover:
        entry.notes.append("sem capa")
    elif entry.cover.startswith("http"):
        entry.notes.append("capa remota")

    fit_profile(entry, schema)
    return entry


def load_entries(export: Path, schema: LibrarySchema) -> list[Entry]:
    csv_path = next(p for p in sorted(export.glob("Leituras *.csv")) if "_all" not in p.name)
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    pages = read_pages(export / "Leituras")
    entries = [
        build_entry(row, pages.get(row["Título"].strip()), schema)
        for row in rows
        if row["Status"].strip() in STATUS
    ]
    # O código universal segue a ordem das leituras; sem data vai ao fim.
    return sorted(entries, key=lambda e: (e.finished is None, e.finished or "", e.title))


# ── Gravação ───────────────────────────────────────────────────────────


def cover_bytes(entry: Entry, export: Path, download: bool) -> tuple[bytes, str] | None:
    ref = entry.cover
    if not ref:
        return None
    if ref.startswith("http"):
        if not download:
            return None
        agent = {"User-Agent": "zettel-notion-import"}
        req = urllib.request.Request(ref, headers=agent)  # noqa: S310 - URL http(s) do export
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - URL do export
            data = resp.read()
        suffix = Path(urllib.parse.urlparse(ref).path).suffix.lower()
        return data, SUFFIXES.get(suffix, ".jpg")
    path = export / urllib.parse.unquote(ref)
    if not path.is_file():
        entry.notes.append(f"capa não encontrada: {path.name}")
        return None
    suffix = SUFFIXES.get(path.suffix.lower())
    if suffix is None:
        entry.notes.append(f"capa em formato não aceito: {path.name}")
        return None
    return path.read_bytes(), suffix


def localize_images(summary: str, page: Path, vault: Path) -> str:
    """Imagens locais da página vão para ``90_Assets/``; as remotas ficam como estão."""
    from zettel.library.covers import store_cover

    def repl(m: re.Match) -> str:
        target = m.group(2)
        if target.startswith("http"):
            return m.group(0)
        path = page.parent / urllib.parse.unquote(target)
        suffix = SUFFIXES.get(path.suffix.lower())
        if not path.is_file() or suffix is None:
            return m.group(0)
        return f"![[{store_cover(vault, path.read_bytes(), suffix)}]]"

    return IMAGE.sub(repl, summary)


def purge_existing(cfg: Any, db: Any, apply: bool) -> None:
    from zettel.purge_source import purge_source

    books = bf.list_books(cfg)
    print(f"\n== Remoção: {len(books)} LIV existentes ==")
    idx = None
    for book in books:
        source = db.get_source(book.source_id) if book.source_id else None
        detail = ""
        if source:
            chunks = db.get_chunks_for_source(book.source_id)
            detail = f" fonte {book.source_id} ({len(chunks)} chunks)"
        print(f"  - {book.path.name}{detail}")
        if not apply:
            continue
        if source:
            if idx is None:
                from zettel.index import VectorIndex, index_kwargs

                idx = VectorIndex(**index_kwargs(cfg))
            purge_source(cfg, db, idx, book.source_id, compact=False)
        cover = str(book.meta.get("cover") or "")
        book.path.unlink()
        if cover and not cover_referenced(cfg, cover):
            (Path(cfg.vault_path) / cover).unlink(missing_ok=True)


def cover_referenced(cfg: Any, relpath: str) -> bool:
    name = Path(relpath).name
    for path in Path(cfg.vault_path).rglob("*.md"):
        if name in path.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def retry_busy(action: Any, tries: int = 20) -> Any:
    """Repete a gravação enquanto o Windows nega o arquivo (Obsidian, antivírus, indexador)."""
    for attempt in range(tries):
        try:
            return action()
        except bf.BookFileBusy:
            if attempt == tries - 1:
                raise
            time.sleep(0.5)
    return None


def import_entry(cfg: Any, db: Any, schema: LibrarySchema, entry: Entry, args: Any) -> None:
    from zettel.library.covers import store_cover
    from zettel.library.sources import update_source

    book = bf.create_book(cfg, {"title": entry.title, "format": entry.fields["format"]})
    ed.apply_metadata(book, {**entry.fields, "code_number": str(book.meta["code_number"])}, schema)
    book.meta["readings"] = [{"start": None, "end": entry.finished}] if entry.finished else []
    form = {f"rating_{k}": str(v) for k, v in entry.ratings.items()}
    ed.set_ratings(book, {**form, "rating_profile": entry.profile or ""}, schema)
    book.meta["notion_id"] = entry.notion_id
    book.meta["notion_score"] = float(entry.row["Nota Final"] or 0) or None
    summary = entry.summary
    if entry.page and summary:
        summary = localize_images(summary, entry.page, Path(cfg.vault_path))
    ed.set_texts(book, summary, "")
    try:
        cover = cover_bytes(entry, args.export_dir, args.download_covers)
    except OSError as exc:
        entry.notes.append(f"capa não baixada: {exc}")
        cover = None
    if cover:
        ed.set_cover(book, store_cover(cfg.vault_path, *cover))
    book = retry_busy(lambda: bf.save_book(cfg, book, expected_version=None))
    # O SRC nasce com o ano da obra (o do citekey); o ano desta edição o substitui.
    updates = {**entry.source, "isbn": entry.fields["isbn"]}
    retry_busy(lambda: update_source(cfg, db, book, {k: v for k, v in updates.items() if v}))


def report(entries: list[Entry], schema: LibrarySchema) -> None:
    print(f"\n== Importação: {len(entries)} livros ==")
    families: Counter = Counter()
    gaps = []
    for e in entries:
        meta = {
            "genres": e.fields["genres"],
            "nature": e.fields["nature"] or None,
            "country": e.fields["country"],
            "ratings": e.ratings,
            "rating_profile": e.profile,
        }
        result = rate(schema, meta)
        families[result.profile.name if result.profile else "-"] += 1
        notion = float(e.row["Nota Final"] or 0)
        if result.score is not None and abs(result.score - notion) >= 0.15:
            gaps.append((e.title, notion, result.score))
        if e.notes or not e.page:
            extra = [] if e.page else ["sem página .md"]
            print(f"  {e.title}: {'; '.join(e.notes + extra)}")
    print("\nPerfis:", dict(families.most_common()))
    print(f"ISBN achado: {sum(1 for e in entries if e.fields['isbn'])}")
    print(f"Tradutor achado: {sum(1 for e in entries if e.source['translator'])}")
    print(f"\nNota do Notion x Estante (diferença >= 0.15): {len(gaps)}")
    for title, notion, score in sorted(gaps, key=lambda g: -abs(g[2] - g[1])):
        print(f"  {notion:.2f} -> {score:.2f}  {title}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="grava (sem isto, só o relatório)")
    parser.add_argument("--export-dir", type=Path, default=ROOT / "notion-export")
    parser.add_argument("--download-covers", action="store_true", help="baixa capas http")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.log_level)
    schema = schema_for(cfg)
    entries = load_entries(args.export_dir, schema)

    from zettel.library.mirror import refresh
    from zettel.oplock import OperationLock, lock_path
    from zettel.state import StateDB

    lock = OperationLock(lock_path(cfg))
    if args.apply:
        lock.acquire("import-notion-books", "script")
    db = StateDB(cfg.state_db_path)
    try:
        purge_existing(cfg, db, args.apply)
        if args.apply:
            for n, entry in enumerate(entries, 1):
                import_entry(cfg, db, schema, entry, args)
                print(f"  [{n}/{len(entries)}] {entry.title}", flush=True)
            refresh(db, cfg)
        report(entries, schema)
    finally:
        db.close()
        lock.release()
    if not args.apply:
        print("\n(dry-run: nada foi gravado; use --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
