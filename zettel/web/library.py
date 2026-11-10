"""Estante (ADR-068): livros como notas LIV, com lista, painel, detalhe e notas de leitura.

Rotas em inglês (``/books``), página em PT-BR. Toda edição é um POST de
formulário com CSRF, feito sob a trava de operação e conferido contra a versão
do arquivo que a página mostrou: uma edição no Obsidian nesse meio-tempo dá 409,
nunca é sobrescrita. O espelho SQLite é posto em dia no início de cada página.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from zettel.markdown import asset_url
from zettel.web.enqueue import operation_lock, post_job
from zettel.web.forms import read_form
from zettel.web.mesa import desk_choices
from zettel.web.rendering import error_page, render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

router = APIRouter()

LAYOUTS = ("table", "gallery", "board")
SORTS = {
    "code": "Código",
    "title": "Título",
    "score": "Nota",
    "finished": "Término",
    "added": "Inclusão",
}
EDIT_ACTIONS = (
    "metadata",
    "bibliography",
    "reading-start",
    "reading-finish",
    "reading-remove",
    "rating",
    "summary",
    "status",
    "cover",
    "cover-isbn",
    "complete",
)
_LOCK = "estante"
# O que um candidato da pesquisa pode pré-preencher no formulário de cadastro.
PREFILL_KEYS = (
    "title",
    "original_title",
    "isbn",
    "pages",
    "original_publication_year",
    "publisher",
    "place",
    "year",
    "cover_ref",
    "country",
    "original_language",
)
# Campos da edição: vão à nota SRC, não à LIV (ADR-057).
SOURCE_KEYS = ("publisher", "edition", "place", "translator", "isbn")


def _today(cfg: Any) -> datetime.date:
    from zoneinfo import ZoneInfo

    return datetime.datetime.now(ZoneInfo(cfg.vault_timezone)).date()


def _open(request: Request) -> tuple[Any, Any]:
    """``(cfg, db)`` com o espelho da Estante em dia; quem chama fecha o db."""
    from zettel.library.mirror import refresh

    svc = service(request)
    db = svc.db()
    refresh(db, svc.cfg)
    return svc.cfg, db


def _book_card(row: dict[str, Any]) -> dict[str, Any]:
    import json

    from zettel.library.book_file import FORMAT_LABELS, STATUS_LABELS

    return {
        **row,
        "authors": json.loads(row.get("authors_json") or "[]"),
        "genres": json.loads(row.get("genres_json") or "[]"),
        "cover_url": asset_url(row.get("cover")),
        "status_label": STATUS_LABELS.get(row.get("status") or "", "—"),
        "format_label": FORMAT_LABELS.get(row.get("format") or "", "—"),
    }


def _filters(request: Request) -> dict[str, Any]:
    q = request.query_params

    def number(key: str, cast):
        try:
            return cast(q.get(key)) if q.get(key) else None
        except ValueError:
            return None

    return {
        "status": q.get("status", ""),
        "genre": q.get("genre", ""),
        "nature": q.get("nature", ""),
        "format": q.get("format", ""),
        "country": q.get("country", ""),
        "reading_mode": q.get("reading_mode", ""),
        "year": number("year", int),
        "min_score": number("min_score", float),
        "query": q.get("q", "")[:200],
        "favorite": q.get("favorite") == "1",
        "sort": q.get("sort", "code") if q.get("sort") in SORTS else "code",
    }


@router.get("/books", response_class=HTMLResponse)
def books_list(request: Request):
    if not authenticated(request):
        return redirect_login()
    from zettel.library.book_file import FORMAT_LABELS, NATURE_LABELS, STATUS_LABELS, STATUSES
    from zettel.library.schema import LibrarySchemaError, schema_for

    filters = _filters(request)
    layout = request.query_params.get("layout", "table")
    layout = layout if layout in LAYOUTS else "table"
    try:
        cfg, db = _open(request)
    except LibrarySchemaError as exc:
        return error_page(request, 500, f"Vocabulário da Estante inválido: {exc}")
    try:
        books = [_book_card(r) for r in db.list_book_rows(**filters)]
        total = len(db.list_book_identities())
    finally:
        db.close()
    board = [
        {"status": s, "label": STATUS_LABELS[s], "books": [b for b in books if b["status"] == s]}
        for s in STATUSES
    ]
    codes = [b["code_number"] for b in books if b.get("code_number")]
    duplicated = sorted({c for c in codes if codes.count(c) > 1})
    return render(
        request,
        "books.html",
        page="library",
        books=books,
        board=board,
        total=total,
        filters=filters,
        layout=layout,
        sorts=SORTS,
        duplicated_codes=duplicated,
        genre_groups=schema_for(cfg).grouped_genres(
            sorted({g for b in books for g in b["genres"]})
        ),
        status_labels=STATUS_LABELS,
        format_labels=FORMAT_LABELS,
        nature_labels=NATURE_LABELS,
        this_year=_today(cfg).year,
    )


@router.get("/books/dashboard", response_class=HTMLResponse)
def books_dashboard(request: Request):
    if not authenticated(request):
        return redirect_login()
    from zettel.library.schema import schema_for
    from zettel.library.stats import build_dashboard

    cfg, db = _open(request)
    try:
        raw = request.query_params.get("year", "")
        year = int(raw) if raw.isdecimal() and len(raw) == 4 else _today(cfg).year
        stats = build_dashboard(db, schema_for(cfg), year)
    finally:
        db.close()
    return render(request, "books_dashboard.html", page="library", stats=stats)


def _sources_without_book(db: Any, cfg: Any) -> list[dict[str, Any]]:
    linked = {r["source_id"] for r in db.list_book_identities() if r.get("source_id")}
    return [
        s
        for s in db.list_sources()
        if s["source_id"] not in linked and s.get("document_type") in cfg.library.document_types
    ][:200]


def _shelf_match(identities: list[dict[str, Any]], candidate: Any) -> dict[str, Any] | None:
    """O livro da estante que já é este candidato: mesmo ISBN, ou título + sobrenome."""
    import json

    from zettel.harvester.biblio_dedupe import normalize_isbn
    from zettel.library.linking import same_work

    as_source = {"title": candidate.title, "authors": json.dumps(candidate.authors)}
    for row in identities:
        if candidate.isbn and normalize_isbn(row.get("isbn")) == candidate.isbn:
            return row
    return next((row for row in identities if same_work(row, as_source)), None)


def _prefill(request: Request) -> dict[str, str]:
    q = request.query_params
    values = {k: q.get(k, "")[:300].strip() for k in PREFILL_KEYS if q.get(k)}
    authors = [a.strip()[:300] for a in q.getlist("authors_list") if a.strip()]
    if authors:
        values["authors"] = "\n".join(authors[:10])
    return values


@router.get("/books/new", response_class=HTMLResponse)
def book_new_page(request: Request):
    """Cadastro em dois passos: pesquisar (ISBN ou título) e conferir o formulário.

    ``?q=`` pesquisa no Google Books, ou no serviço de ``?provider=`` (o usuário
    refaz a busca na Open Library quando a lista não serve). ``?step=form``
    mostra o formulário, pré-preenchido pelo candidato escolhido (os campos vêm
    na query string) ou vazio. Com a pesquisa desligada na configuração, a
    página já abre no formulário.
    """
    if not authenticated(request):
        return redirect_login()
    from zettel.library.book_file import FORMAT_LABELS, STATUS_LABELS
    from zettel.library.lookup import (
        PROVIDER_ARTICLES,
        PROVIDER_LABELS,
        PROVIDERS,
        BookLookupError,
        search_books,
    )
    from zettel.library.schema import schema_for

    cfg, db = _open(request)
    try:
        sources = _sources_without_book(db, cfg)
        identities = db.list_book_identities()
        codes = {r["book_id"]: r.get("code_number") for r in identities}
    finally:
        db.close()
    online = cfg.library.online_lookup
    query = " ".join(request.query_params.get("q", "").split())[:200]
    step = "form" if request.query_params.get("step") == "form" or not online else "search"
    provider = request.query_params.get("provider", PROVIDERS[0])
    provider = provider if provider in PROVIDERS else PROVIDERS[0]
    results: list[dict[str, Any]] = []
    lookup_error = ""
    if step == "search" and query:
        # Só leitura e fora da trava: nada é gravado até o formulário ser enviado.
        try:
            found = search_books(query, provider)
        except BookLookupError as exc:
            found, lookup_error = [], str(exc)
        for cand in found:
            match = _shelf_match(identities, cand)
            results.append(
                {
                    "c": cand,
                    "fields": cand.form_fields(),
                    # Sugestão pelas categorias do catálogo; o usuário confere no formulário.
                    "genres": schema_for(cfg).genres_for_categories(cand.categories),
                    "on_shelf": match,
                    "code": codes.get(match["book_id"]) if match else None,
                }
            )
    schema = schema_for(cfg)
    prefill = _prefill(request) if step == "form" else {}
    prefill_genres = [g[:80] for g in request.query_params.getlist("genres") if g.strip()][:12]
    work, work_error = None, ""
    if step == "form" and online and prefill.get("title"):
        work, work_error = _work_facts(
            prefill["title"],
            prefill.get("authors", "").splitlines(),
            prefill.get("original_title", ""),
        )
        if work:
            # A obra (Wikidata) vale mais que o "ano da obra" do catálogo de edições,
            # que costuma ser o de uma edição antiga; o que já veio digitado fica.
            facts = work.book_fields()
            for key, value in facts.items():
                if key == "original_publication_year" or not prefill.get(key):
                    prefill[key] = str(value)
    return render(
        request,
        "book_new.html",
        page="library",
        step=step,
        online=online,
        query=query,
        results=results,
        lookup_error=lookup_error,
        provider=provider,
        provider_label=PROVIDER_LABELS[provider],
        provider_article=PROVIDER_ARTICLES[provider],
        other_providers=[
            (p, PROVIDER_ARTICLES[p], PROVIDER_LABELS[p]) for p in PROVIDERS if p != provider
        ],
        prefill=prefill,
        work=work,
        work_error=work_error,
        sources=sources,
        genre_groups=schema.grouped_genres(prefill_genres),
        prefill_genres=prefill_genres,
        subject_options=schema.subjects,
        format_labels=FORMAT_LABELS,
        status_labels=STATUS_LABELS,
        initial_status=cfg.library.initial_status,
    )


@router.get("/books/lookup/cover")
def book_lookup_cover(request: Request):
    """A miniatura de um candidato, servida daqui para a CSP seguir só com ``img-src 'self'``."""
    if not authenticated(request):
        return Response(status_code=401)
    from zettel.library.covers import CoverError, fetch_cover_ref

    if not service(request).cfg.library.online_lookup:
        return Response(status_code=404)
    try:
        data, suffix = fetch_cover_ref(request.query_params.get("ref", ""), large=False)
    except CoverError:
        return Response(status_code=404)
    media = {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}.get(
        suffix, "image/webp"
    )
    return Response(data, media_type=media, headers={"Cache-Control": "private, max-age=86400"})


def _work_facts(title: str, authors: list[str], original_title: str = "") -> tuple[Any, str]:
    """``(WorkFacts | None, erro)``: o Wikidata fora do ar não impede o cadastro."""
    from zettel.library.lookup import BookLookupError
    from zettel.library.wikidata import work_facts

    try:
        authors = [a for a in authors if a.strip()]
        return work_facts(title, authors, original_title=original_title), ""
    except BookLookupError as exc:
        return None, str(exc)


def _search_cover(cfg: Any, ref: str) -> tuple[bytes, str] | None:
    """A capa grande de um candidato; uma falha não impede o cadastro (o detalhe tem o botão)."""
    import logging

    from zettel.library.covers import CoverError, fetch_cover_ref

    if not ref or not cfg.library.online_lookup:
        return None
    try:
        return fetch_cover_ref(ref, large=True)
    except CoverError as exc:
        logging.getLogger(__name__).info("Capa da pesquisa não baixada (%s): %s", ref, exc)
        return None


@router.post("/books")
def book_create(request: Request, form: FormData = Depends(read_form)):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    from zettel.library.book_file import BookFileBusy, create_book, save_book
    from zettel.library.covers import store_cover
    from zettel.library.editing import BookEditError, apply_metadata, set_cover
    from zettel.library.linking import fields_from_source
    from zettel.library.mirror import refresh
    from zettel.library.schema import schema_for
    from zettel.oplock import LockBusy

    cfg = service(request).cfg
    values = _form_values(form)
    source_id = str(values.get("source_id") or "").strip()
    try:
        source_updates = _source_updates(values)
    except BookEditError as exc:
        return error_page(request, 400, str(exc))
    # A rede fica fora da trava: só a gravação precisa dela.
    cover = _search_cover(cfg, str(values.get("cover_ref") or ""))
    try:
        lock = operation_lock(request, _LOCK)
    except LockBusy as busy:
        return error_page(request, 409, f"O vault está em uso. {busy}")
    db = service(request).db()
    try:
        seed: dict[str, Any] = {}
        if source_id:
            source = db.get_source(source_id)
            if not source:
                return error_page(request, 400, "Fonte não encontrada.")
            seed = fields_from_source(source)
        if not str(values.get("title") or "").strip() and seed:
            values["title"] = seed["title"]
            values["authors"] = "\n".join(seed["authors"])
            values.setdefault("isbn", seed.get("isbn") or "")
        if not str(values.get("title") or "").strip():
            return error_page(request, 400, "Dê um título ao livro ou escolha uma fonte.")
        try:
            book = create_book(cfg, {"title": values["title"], "format": values.get("format")})
        except BookFileBusy as exc:
            return error_page(request, 409, f"A nota do livro não foi criada. {exc}")
        try:
            apply_metadata(book, {**_metadata_defaults(book), **values}, schema_for(cfg))
        except BookEditError as exc:
            book.path.unlink(missing_ok=True)
            return error_page(request, 400, str(exc))
        if cover:
            set_cover(book, store_cover(cfg.vault_path, *cover))
        try:
            book = save_book(cfg, book, expected_version=book.version)
        except BookFileBusy as exc:
            return error_page(
                request,
                409,
                f"O livro «{book.title}» ({book.code}) ficou só com o título. {exc} "
                "Enviar o formulário outra vez cria um segundo livro. Feche a nota e "
                "complete a ficha na Estante, por esse título.",
            )
        if any(source_updates.values()):
            # A edição achada na pesquisa (ou digitada) vai ao SRC, criado agora se preciso.
            _update_source(cfg, db, book, source_updates)
        refresh(db, cfg)
    finally:
        db.close()
        lock.release()
    return RedirectResponse(f"/books/{book.book_id}", status_code=303)


def _form_values(form: FormData) -> dict[str, Any]:
    """O formulário como dict; ``genres`` (checkboxes) vira lista."""
    values: dict[str, Any] = {k: v for k, v in form.items() if isinstance(v, str)}
    for key in ("genres", "subjects"):
        values[key] = [str(v) for v in form.getlist(key) if isinstance(v, str)]
    return values


def _metadata_defaults(book: Any) -> dict[str, Any]:
    """O que o formulário de metadados mandaria para um livro recém-criado."""
    return {
        "title": book.title,
        "format": book.format,
        "status": book.status,
        "code_number": str(book.meta.get("code_number") or ""),
    }


def _bibliography(source: dict[str, Any] | None) -> dict[str, Any]:
    import json

    if not source:
        return {}
    try:
        biblio = json.loads(source.get("bibliography_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return biblio if isinstance(biblio, dict) else {}


def _source_view(db: Any, source_id: str | None) -> dict[str, Any]:
    from zettel.vault import literature_chunk_wikilink_for_row

    if not source_id:
        return {"source": None, "lits": [], "notes": []}
    source = db.get_source(source_id)
    if not source:
        return {"source": None, "missing_source": source_id, "lits": [], "notes": []}
    lits = []
    for chunk in db.get_chunks_for_source(source_id):
        if chunk.get("status") not in ("approved", "persisted"):
            continue
        link = literature_chunk_wikilink_for_row(source["citekey"], chunk, with_alias=True)
        label = link.split("|", 1)[1].rstrip("]") if "|" in link else link.strip("[]")
        lits.append(
            {
                "chunk_id": chunk["chunk_id"],
                "label": label,
                "manual": chunk.get("status") == "persisted",
                "page": chunk.get("page_in_book"),
            }
        )
    notes = [
        {"note_id": n["note_id"], "title": n.get("title") or n["note_id"]}
        for n in db.get_notes_for_source(source_id)
    ]
    return {"source": source, "lits": lits, "notes": notes}


@router.get("/books/{book_id}", response_class=HTMLResponse)
def book_detail(request: Request, book_id: str):
    if not authenticated(request):
        return redirect_login()
    from zettel.credentials import llm_phase_ready
    from zettel.library import book_file as bf
    from zettel.library.book_file import as_str_list
    from zettel.library.covers import OPEN_LIBRARY_HOME
    from zettel.library.linking import source_candidates
    from zettel.library.rating import rate
    from zettel.library.reading_mode import effective_nature, effective_reading_mode
    from zettel.library.reading_notes import REFLECTION_KINDS
    from zettel.library.schema import schema_for

    cfg, db = _open(request)
    try:
        try:
            book = bf.load_book(cfg, book_id)
        except bf.BookNotFound:
            return error_page(request, 404, "Livro não encontrado.")
        schema = schema_for(cfg)
        row = db.get_book(book_id) or {}
        view = _source_view(db, book.source_id)
        candidates = []
        if view.get("missing_source"):
            candidates = [db.get_source(s) for s in source_candidates(db, row)]
    finally:
        db.close()
    rating = rate(schema, book.meta)
    m_subjects = book.meta.get("subjects")
    # O que "Automático" daria agora: a natureza só pelos gêneros; o modo pela
    # natureza (escolhida ou automática) e pelos gêneros reflexivos.
    auto_nature = effective_nature({**book.meta, "nature": None}, schema)
    auto_mode = effective_reading_mode({**book.meta, "reading_mode": None}, schema)
    biblio = _bibliography(view.get("source"))
    summary = bf.section_text(book.body, bf.SUMMARY_HEADING)
    impressions = bf.section_text(book.body, bf.IMPRESSIONS_HEADING)
    return render(
        request,
        "book_detail.html",
        page="library",
        book=book,
        row=row,
        cover_url=asset_url(book.meta.get("cover")),
        rating=rating,
        schema=schema,
        summary=summary,
        impressions=impressions,
        readings=book.readings,
        open_reading=bool(book.readings and book.readings[-1].start and not book.readings[-1].end),
        status_labels=bf.STATUS_LABELS,
        format_labels=bf.FORMAT_LABELS,
        nature_labels=bf.NATURE_LABELS,
        mode_labels=bf.READING_MODE_LABELS,
        auto_nature=auto_nature,
        auto_mode=auto_mode,
        reflection_kinds=REFLECTION_KINDS,
        genre_groups=schema.grouped_genres(book.genres),
        subject_options=list(dict.fromkeys([*schema.subjects, *as_str_list(m_subjects)])),
        online_lookup=cfg.library.online_lookup,
        open_library_home=OPEN_LIBRARY_HOME,
        connect_llm_ready=llm_phase_ready(cfg, "connect"),
        desks=desk_choices(request),
        completed=_completed_notice(request),
        candidates=[c for c in candidates if c],
        biblio=biblio,
        today=_today(cfg).isoformat(),
        **view,
    )


COMPLETED_LABELS = {
    "authors": "autores",
    "isbn": "ISBN",
    "pages": "páginas",
    "cover": "capa",
    "country": "país do autor",
    "original_language": "idioma original",
    "original_title": "título original",
    "original_publication_year": "ano da 1ª publicação",
    "publisher": "editora",
    "place": "local",
    "year": "ano desta edição",
}


def _completed_notice(request: Request) -> dict[str, Any] | None:
    """O resultado de "Completar dados", lido da query do redirect (só códigos conhecidos)."""
    raw = request.query_params.get("completed")
    if raw is None:
        return None
    filled = [COMPLETED_LABELS[k] for k in raw.split(",") if k in COMPLETED_LABELS]
    work = request.query_params.get("work", "none")
    return {"filled": filled, "work": work if work in ("work", "author", "none") else "none"}


def _change(action: str, values: dict[str, Any], form: FormData, cfg: Any, schema: Any):
    """A edição pedida por ``action``, como função do livro (e do db)."""
    from zettel.library import editing as ed
    from zettel.library.covers import store_cover

    today = _today(cfg)
    if action == "metadata":
        return lambda book, db: ed.apply_metadata(book, values, schema)
    if action == "reading-start":
        return lambda book, db: ed.start_reading(book, values.get("date"), today)
    if action == "reading-finish":
        return lambda book, db: ed.finish_reading(book, values.get("date"), today)
    if action == "reading-remove":
        return lambda book, db: ed.remove_reading(book, values.get("position"))
    if action == "rating":
        return lambda book, db: ed.set_ratings(book, values, schema)
    if action == "summary":
        return lambda book, db: ed.set_texts(book, values.get("summary"), values.get("impressions"))
    if action == "status":
        return lambda book, db: ed.set_status(book, str(values.get("status") or ""))
    if action == "bibliography":
        updates = _source_updates(values)
        return lambda book, db: _update_source(cfg, db, book, updates)
    if action == "cover":
        cover = _uploaded_cover(form)
        return lambda book, db: ed.set_cover(book, store_cover(cfg.vault_path, *cover))
    raise ed.BookEditError("Ação desconhecida.")


def _uploaded_cover(form: FormData) -> tuple[bytes, str]:
    from zettel.library.editing import BookEditError
    from zettel.web.manual_images import read_image_uploads

    try:
        uploads = read_image_uploads(form)
    except ValueError as exc:
        raise BookEditError(str(exc)) from exc
    if len(uploads) != 1:
        raise BookEditError("Envie uma imagem de capa.")
    name, data = uploads[0]
    return data, "." + name.rsplit(".", 1)[-1].lower()


def _source_updates(values: dict[str, Any]) -> dict[str, Any]:
    """Os campos da edição de um formulário, validados; vazio é ``""``/``None``."""
    from zettel.library.editing import BookEditError

    updates: dict[str, Any] = {
        key: " ".join(str(values.get(key) or "").split())[:300] for key in SOURCE_KEYS
    }
    year = str(values.get("year") or "").strip()
    if year and not (year.isdecimal() and len(year) == 4):
        raise BookEditError("Ano inválido.")
    updates["year"] = int(year) if year else None
    return updates


def _update_source(
    cfg: Any, db: Any, book: Any, updates: dict[str, Any], *, only_missing: bool = False
) -> list[str]:
    """Grava a edição no SRC (a bibliografia fica lá, ADR-057), criando o SRC manual se faltar.

    ``only_missing`` só preenche o que o SRC não tem: é o modo da pesquisa, que
    completa sem nunca trocar o que o usuário escreveu.
    """
    from zettel.library.editing import BookEditError
    from zettel.library.reading_notes import ensure_source
    from zettel.sync import sync_source_note
    from zettel.vault import compose_note, parse_frontmatter

    source_id = ensure_source(cfg, db, book)
    path = _source_note_path(cfg, source_id)
    if path is None:
        raise BookEditError("A nota SRC desta fonte não está no vault.")
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    written = []
    for key, value in updates.items():
        if only_missing and meta.get(key):
            continue
        if value:
            meta[key] = value
            written.append(key)
        elif not only_missing and key != "year":
            meta.pop(key, None)
    path.write_text(compose_note(meta, body), encoding="utf-8")
    sync_source_note(cfg, db, path)
    return written


def _source_note_path(cfg: Any, source_id: str):
    from zettel.vault import parse_frontmatter

    folder = cfg.vault_path / "10_Sources"
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if meta.get("type") == "source" and meta.get("source_id") == source_id:
            return path
    return None


def _edit(
    request: Request,
    book_id: str,
    version: str | None,
    change,
    anchor: str = "",
    query: Any = None,
):
    """Carrega, muda e grava um livro sob a trava; o espelho é posto em dia em seguida.

    ``query`` (opcional) é chamado depois da gravação e devolve a query string
    do redirect: é como "Completar dados" conta à página o que preencheu.
    """
    from zettel.library import book_file as bf
    from zettel.library.covers import CoverError
    from zettel.library.editing import BookEditError
    from zettel.library.linking import write_book_notes_block
    from zettel.library.mirror import refresh
    from zettel.library.reading_notes import ReadingNoteError
    from zettel.oplock import LockBusy

    cfg = service(request).cfg
    try:
        lock = operation_lock(request, _LOCK)
    except LockBusy as busy:
        return error_page(request, 409, f"O vault está em uso. {busy}")
    db = service(request).db()
    try:
        book = bf.load_book(cfg, book_id)
        if version is not None and book.version != version:
            raise bf.BookConflict(book_id)
        # ``change`` pode gravar o livro (ao ligar a fonte); save_book atualiza
        # book.version, então a gravação seguinte confere contra o arquivo certo.
        if change(book, db) != "saved":
            bf.save_book(cfg, book, expected_version=book.version)
        refresh(db, cfg)
        write_book_notes_block(cfg, db, bf.load_book(cfg, book_id))
        refresh(db, cfg)
    except bf.BookNotFound:
        return error_page(request, 404, "Livro não encontrado.")
    except bf.BookConflict:
        return error_page(
            request,
            409,
            "O livro mudou desde que esta página foi aberta (talvez no Obsidian). "
            "Recarregue a página e refaça a alteração.",
        )
    except bf.BookFileBusy as exc:
        return error_page(
            request,
            409,
            f"{exc} Feche a nota e repita a alteração. O arquivo no disco continua como estava.",
        )
    except (BookEditError, CoverError, ReadingNoteError) as exc:
        return error_page(request, 400, str(exc))
    finally:
        db.close()
        lock.release()
    suffix = query() if query else ""
    return RedirectResponse(
        f"/books/{book_id}" + (f"?{suffix}" if suffix else "") + (f"#{anchor}" if anchor else ""),
        303,
    )


@router.post("/books/{book_id}")
def book_edit(request: Request, book_id: str, form: FormData = Depends(read_form)):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    from zettel.library.covers import CoverError, fetch_open_library_cover, store_cover
    from zettel.library.editing import BookEditError, set_cover
    from zettel.library.lookup import BookLookupError
    from zettel.library.schema import schema_for

    cfg = service(request).cfg
    action = str(form.get("action") or "")
    if action not in EDIT_ACTIONS:
        return error_page(request, 400, "Ação desconhecida.")
    values = _form_values(form)
    try:
        if action in ("cover-isbn", "complete") and not cfg.library.online_lookup:
            return error_page(request, 409, "A pesquisa online está desligada na configuração.")
        report: dict[str, Any] = {}
        if action == "complete":
            change = _complete_change(cfg, _load_or_none(cfg, book_id), report)
        elif action == "cover-isbn":
            # A rede fica fora da trava: só a gravação precisa dela.
            data, suffix = fetch_open_library_cover(str(values.get("isbn") or ""))

            def change(book, db):
                return set_cover(book, store_cover(cfg.vault_path, data, suffix))
        else:
            change = _change(action, values, form, cfg, schema_for(cfg))
    except (BookEditError, CoverError, BookLookupError) as exc:
        return error_page(request, 400, str(exc))
    anchor = {"rating": "avaliacao", "summary": "resumo"}.get(action, "")
    if action.startswith("reading-"):
        anchor = "leituras"
    query = None
    if action == "complete":
        from urllib.parse import urlencode

        def query():
            return urlencode(
                {"completed": ",".join(report["filled"]) or "-", "work": report["work"]}
            )

    return _edit(request, book_id, str(form.get("version") or ""), change, anchor, query)


def _load_or_none(cfg: Any, book_id: str):
    from zettel.library import book_file as bf

    try:
        return bf.load_book(cfg, book_id)
    except bf.BookNotFound:
        return None


def _complete_change(cfg: Any, book: Any, report: dict[str, Any]):
    """Completar dados: pesquisa agora (fora da trava) e preenche só o que está vazio.

    Pelo ISBN (Google Books, depois Open Library): autores, páginas, capa e a
    edição no SRC. Pelo Wikidata (título + autores): país do autor, idioma,
    título e ano originais. Basta uma das duas responder. ``report`` recebe o
    que foi preenchido (``filled``) e o que o Wikidata achou (``work``: work,
    author ou none), para o aviso da página.
    """
    from zettel.library.covers import store_cover
    from zettel.library.editing import BookEditError, fill_missing, set_cover
    from zettel.library.lookup import BookLookupError, lookup_isbn

    if book is None:
        raise BookEditError("Livro não encontrado.")
    errors: list[str] = []
    found = None
    if book.meta.get("isbn"):
        try:
            found = lookup_isbn(str(book.meta["isbn"]))
        except BookLookupError as exc:
            errors.append(str(exc))
    authors = book.authors or (found.authors if found else [])
    work, work_error = _work_facts(book.title, authors, str(book.meta.get("original_title") or ""))
    report.update(filled=[], work=work.match if work else "none")
    if work_error:
        errors.append(work_error)
    if found is None and work is None:
        raise BookEditError(
            " ".join(errors) or "Nem os catálogos (pelo ISBN) nem o Wikidata acharam este livro."
        )
    fields: dict[str, Any] = {}
    if found:
        fields = {**found.form_fields(), "authors": found.authors}
        fields.pop("original_publication_year", None)  # o ano da obra vem do Wikidata
    if work:
        fields.update(work.book_fields())
    cover = _search_cover(cfg, found.cover_ref) if found else None
    source = (
        {"publisher": found.publisher, "place": found.place, "year": found.year} if found else {}
    )

    def change(book, db):
        filled = fill_missing(book, fields)
        if cover and not book.meta.get("cover"):
            set_cover(book, store_cover(cfg.vault_path, *cover))
            filled.append("cover")
        if any(source.values()):
            # Pode criar o SRC e gravar a LIV (source_id); a versão segue o arquivo.
            filled += _update_source(cfg, db, book, source, only_missing=True)
        report["filled"] = filled

    return change


@router.post("/books/{book_id}/reading-notes")
def book_reading_note(request: Request, book_id: str, form: FormData = Depends(read_form)):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, str(form.get("csrf") or "")):
        return error_page(request, 403, "CSRF inválido")
    from zettel.library.reading_notes import ReadingNote, ReadingNoteError, create_reading_note

    try:
        note = ReadingNote.from_form(form)
    except ReadingNoteError as exc:
        return error_page(request, 400, str(exc))
    cfg = service(request).cfg

    def change(book, db):
        create_reading_note(cfg, db, book, note)
        return "saved"

    # Só anexa uma nota e, no máximo, liga a fonte: não depende da versão da página.
    return _edit(request, book_id, None, change, "notas")


@router.post("/books/{book_id}/permanent")
def book_permanent(request: Request, book_id: str, form: FormData = Depends(read_form)):
    """ZTL a partir de uma nota de leitura: o job ``manual-ztl-from-lit`` de sempre."""
    if not authenticated(request):
        return redirect_login()
    chunk_id = str(form.get("chunk_id") or "").strip()
    use_llm = str(form.get("use_llm") or "") == "1"
    if not chunk_id.startswith("@"):
        return error_page(request, 400, "Escolha a nota de leitura.")
    if use_llm:
        from zettel.credentials import llm_phase_ready

        if not llm_phase_ready(service(request).cfg, "connect"):
            return error_page(request, 409, "A fase connect não tem credencial de LLM.")
    payload = {"ref": chunk_id, "chunk_id": chunk_id, "thesis": None, "use_llm": use_llm}
    return post_job(request, "manual-ztl-from-lit", payload, str(form.get("csrf") or ""))
